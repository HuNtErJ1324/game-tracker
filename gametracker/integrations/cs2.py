"""Counter-Strike 2: lifetime stats from Steam's local stats cache, plus live per-round tracking
through Valve's official Game State Integration (GSI).

GSI: CS2 POSTs its game state to a local URL listed in a cfg file in csgo/cfg. The PC agent
installs that file once (CS2 reads it at launch) and listens on 127.0.0.1. GSI shows your
money, equipment value, round kills/headshots/damage, health, side, round results and score,
but never player positions, so there is no death map for CS2.
"""
import json
import os
import secrets
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .. import db, vdf
from ..cs2_demo import weapon_name
from . import Integration, fmt_duration

GAME_ID = "steam:730"
GSI_PORT = 3727
GSI_CFG = "gamestate_integration_gametracker.cfg"
MODES = {"competitive": "Competitive", "premier": "Premier", "casual": "Casual", "deathmatch": "Deathmatch",
         "scrimcomp2v2": "Wingman", "gungameprogressive": "Arms Race", "survival": "Danger Zone", "custom": "Custom"}
REASONS = {"elimination": "eliminated", "bomb": "bomb exploded", "defuse": "bomb defused", "time": "time ran out",
           "surrender": "surrender"}


def map_name(raw):
    raw = (raw or "?").split("/")[-1]
    for prefix in ("de_", "cs_", "ar_", "dm_"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
    return raw.replace("_", " ").title()


def kills_in(r):
    """Kills in a round: a number for live-tracked rounds, a list (with positions) for demo rounds."""
    k = r.get("kills")
    return len(k) if isinstance(k, list) else (k or 0)


def hs_in(r):
    k = r.get("kills")
    return sum(1 for x in k if x.get("hs")) if isinstance(k, list) else (r.get("hs") or 0)


def merge_demo_matches():
    """A parsed demo replaces the live-tracked (GSI) copy of the same match: same map, score and K/D."""
    rows = db.matches(GAME_ID, 300)
    gsi = [r for r in rows if (r["data"] or {}).get("source") == "gsi"]
    for d in rows:
        dd = d["data"] or {}
        if dd.get("source") != "demo":
            continue
        for g in gsi:
            gd = g["data"]
            if (gd.get("map_raw") == dd.get("map_raw")
                    and sorted([gd.get("rounds_won") or 0, gd.get("rounds_lost") or 0]) == sorted([dd["rounds_won"], dd["rounds_lost"]])
                    and gd.get("kills") == dd.get("kills") and gd.get("deaths") == dd.get("deaths")):
                if not dd.get("start_exact"):
                    dd["start"] = gd["start"]  # the demo file's time is when it was downloaded
                dd["queue"] = gd.get("queue") or dd.get("queue")
                dd["merged_from"] = g["id"]
                db.ingest([], [{"id": d["id"], "game_id": GAME_ID, "ts": dd["start"], "data": dd}])
                db.delete_match(g["id"])
                gsi.remove(g)
                break


def buy_of(equip):
    if equip is None:
        return None
    return "eco" if equip < 1500 else "half" if equip < 3700 else "full"


# ---------------- lifetime stats (Steam's local stats cache) ----------------

def lifetime_stats():
    from .. import detect  # Windows-only; agent side
    root = detect.steam_root()
    _, account = detect.steam_active_account()
    if not root or not account:
        return None
    base = os.path.join(root, "appcache", "stats")
    try:
        schema = next(iter(vdf.load_binary(os.path.join(base, "UserGameStatsSchema_730.bin")).values()))
        user = vdf.load_binary(os.path.join(base, f"UserGameStats_{account}_730.bin")).get("cache", {})
    except (OSError, StopIteration, ValueError):
        return None
    names = {sid: st.get("name") for sid, st in (schema.get("stats") or {}).items()
             if isinstance(st, dict) and "bits" not in st and st.get("name")}
    out = {}
    for sid, v in user.items():
        if sid in names:
            val = v.get("data") if isinstance(v, dict) else v
            if isinstance(val, (int, float)):
                out[names[sid]] = val
    return out


# ---------------- Game State Integration ----------------

class GsiCollector:
    """Turns the stream of GSI updates into per-round records and finished matches."""

    def __init__(self, token):
        self.token = token
        self.deaths = []  # (match id, round index, time, mode) seen in the current update
        self.lock = threading.Lock()
        self.match = None
        self.finished = []  # matches waiting to be sent to the server
        self.last_seen = 0
        self.last_map_seen = 0  # GSI keeps heartbeating in menus; this is when we were last in a map
        self.live = None

    # --- per update ---
    def handle(self, data):
        if (data.get("auth") or {}).get("token") != self.token:
            return
        with self.lock:
            self.last_seen = time.time()
            self.deaths = []
            self._update(data)
            deaths = self.deaths
        for d in deaths:  # outside the lock: video capture saves an Instant Replay right away
            if death_hook:
                try:
                    death_hook(*d)
                except Exception as e:
                    print(f"[cs2] death hook error: {e}")

    def _check_death(self, m, mp, pl, own, phase):
        """You died: your health hit 0, or GSI switched to the player you're now spectating mid-round."""
        alive = m.get("alive")
        if own:
            health = (pl.get("state") or {}).get("health")
            if health is None:
                return
            m["alive"] = health > 0
            if not (health == 0 and alive and phase in ("live", "over")):
                return
        elif not (alive and phase == "live" and pl.get("steamid")):
            return
        m["alive"] = False
        n = mp.get("round") if isinstance(mp.get("round"), int) else len(m["rounds"])
        if m.get("died_round") != n:
            m["died_round"] = n
            self.deaths.append((f"cs2:{int(m['started'])}", n, time.time(), m.get("mode")))

    def _new_match(self, data, mp):
        self.match = {"started": time.time(), "map": mp.get("name"), "mode": mp.get("mode"), "rounds": [],
                      "own": {}, "phase": None, "start_state": {}, "round_wins_seen": set(),
                      "steamid": (data.get("provider") or {}).get("steamid")}

    def _update(self, data):
        mp, rnd, pl = data.get("map") or {}, data.get("round") or {}, data.get("player") or {}
        me = (data.get("provider") or {}).get("steamid")
        if not mp:
            self.live = None
            return
        self.last_map_seen = time.time()
        if mp.get("phase") == "warmup" or self.match is None or self.match["map"] != mp.get("name"):
            if self.match and self.match["rounds"] and self.match["map"] != mp.get("name"):
                self._finish(partial=True)
            self._new_match(data, mp)
        m = self.match
        m["mode"] = mp.get("mode") or m["mode"]
        own = pl.get("steamid") == me  # when you're dead GSI shows the player you spectate
        st, ms = pl.get("state") or {}, pl.get("match_stats") or {}
        if own:
            m["own"] = {"team": pl.get("team"), "state": st, "match_stats": ms, "name": pl.get("name")}
            m["team"] = pl.get("team") or m.get("team")
        phase = rnd.get("phase")
        if mp.get("phase") == "live":
            self._check_death(m, mp, pl, own, phase)
        if phase == "live" and m["phase"] != "live" and own:
            m["start_state"] = {"money": st.get("money"), "equip": st.get("equip_value"), "team": pl.get("team")}
        if phase == "over" and m["phase"] != "over":
            self._close_round(data, mp, rnd)
        m["phase"] = phase
        m["score"] = {"ct": (mp.get("team_ct") or {}).get("score"), "t": (mp.get("team_t") or {}).get("score")}
        o = m["own"]
        side = (o.get("team") or "").lower()
        mine, theirs = (m["score"]["ct"], m["score"]["t"]) if side == "ct" else (m["score"]["t"], m["score"]["ct"])
        mst = o.get("match_stats") or {}
        self.live = {"headline": f"{map_name(m['map'])} {mine if mine is not None else '?'}–{theirs if theirs is not None else '?'}",
                     "details": [f"{mst.get('kills', 0)}/{mst.get('deaths', 0)}/{mst.get('assists', 0)}",
                                 f"${(o.get('state') or {}).get('money', 0):,}", MODES.get(m["mode"], (m["mode"] or "").title())]}
        if mp.get("phase") == "gameover":
            self._finish(partial=False)

    def _close_round(self, data, mp, rnd):
        m = self.match
        wins = mp.get("round_wins") or {}
        new_keys = [k for k in wins if k not in m["round_wins_seen"]]
        num = max((int(k) for k in new_keys), default=len(m["rounds"]) + 1)
        m["round_wins_seen"].update(wins)
        reason = wins.get(str(num), "")
        o, start = m["own"], m["start_state"]
        st = o.get("state") or {}
        side = (start.get("team") or o.get("team") or "").lower()
        win_team = (rnd.get("win_team") or reason.split("_win")[0]).lower()
        m["rounds"].append({
            "n": num - 1, "side": side or None, "won": bool(side) and win_team == side,
            "end": REASONS.get(reason.split("_win_")[-1], reason.replace("_", " ") or None),
            "bomb": rnd.get("bomb"),
            "money": start.get("money"), "loadout": start.get("equip"),
            # MR12: rounds 1 and 13 are pistol rounds in Competitive and Premier
            "buy": "pistol" if m["mode"] in ("competitive", "premier") and num in (1, 13) else buy_of(start.get("equip")),
            "kills": st.get("round_kills", 0), "hs": st.get("round_killhs", 0), "damage": st.get("round_totaldmg", 0),
            "survived": (st.get("health") or 0) > 0,
        })
        m["start_state"] = {}

    def _finish(self, partial):
        m, self.match = self.match, None
        if not m or not m["rounds"]:
            return
        o = m["own"]
        side = (o.get("team") or "").lower()
        sc = m.get("score") or {}
        mine, theirs = (sc.get("ct"), sc.get("t")) if side == "ct" else (sc.get("t"), sc.get("ct"))
        ms = o.get("match_stats") or {}
        rounds = m["rounds"]
        n = len(rounds)
        kills = sum(r["kills"] for r in rounds)
        summary = {
            "v": 1, "source": "gsi", "partial": partial, "start": m["started"],
            "length_sec": time.time() - m["started"], "map": map_name(m["map"]), "map_raw": m["map"],
            "queue": MODES.get(m["mode"], (m["mode"] or "?").title()),
            "rounds_won": mine, "rounds_lost": theirs,
            "won": None if partial or mine is None or theirs is None or mine == theirs else mine > theirs,
            "kills": ms.get("kills", kills), "deaths": ms.get("deaths", sum(not r["survived"] for r in rounds)),
            "assists": ms.get("assists", 0), "mvps": ms.get("mvps", 0), "score": ms.get("score"),
            "adr": round(sum(r["damage"] for r in rounds) / max(1, n)),
            "hs_pct": round(100 * sum(r["hs"] for r in rounds) / kills, 1) if kills else None,
            "rounds": rounds,
        }
        self.finished.append(summary)

    def flush_stale(self, idle=60):
        """Left the match (or closed CS2) before it ended: keep what we have as a partial match."""
        with self.lock:
            if self.match and self.match["rounds"] and time.time() - self.last_map_seen > idle:
                self._finish(partial=True)

    def take(self):
        with self.lock:
            out, self.finished = self.finished, []
            return out


_collector = None
death_hook = None  # set by the agent: called with (match id, round index, time, mode) when you die


def _cfg_dir():
    from .. import detect
    for g in detect.scan_steam():
        if g["id"] == GAME_ID:
            d = os.path.join(g["install_dir"], "game", "csgo", "cfg")
            return d if os.path.isdir(d) else None
    return None


def start_gsi(data_dir):
    """Agent side: install the GSI cfg (once) and start listening. Returns a status string."""
    global _collector
    if _collector:
        return "running"
    cfg_dir = _cfg_dir()
    if not cfg_dir:
        return "CS2 not installed"
    token_path = os.path.join(data_dir, "cs2_gsi_token")
    try:
        with open(token_path, encoding="utf-8") as f:
            token = f.read().strip()
    except OSError:
        token = secrets.token_hex(16)
        os.makedirs(data_dir, exist_ok=True)
        with open(token_path, "w", encoding="utf-8") as f:
            f.write(token)
    cfg = os.path.join(cfg_dir, GSI_CFG)
    body = (f'"GameTracker"\n{{\n "uri" "http://127.0.0.1:{GSI_PORT}/cs2"\n "timeout" "5.0"\n "buffer" "0.1"\n'
            f' "throttle" "0.5"\n "heartbeat" "30.0"\n "auth" {{ "token" "{token}" }}\n "data"\n {{\n'
            '  "provider" "1"\n  "map" "1"\n  "round" "1"\n  "player_id" "1"\n  "player_state" "1"\n'
            '  "player_match_stats" "1"\n  "map_round_wins" "1"\n }\n}\n')
    try:
        current = open(cfg, encoding="utf-8").read() if os.path.exists(cfg) else None
    except OSError:
        current = None
    installed = False
    if current != body:
        with open(cfg, "w", encoding="utf-8") as f:
            f.write(body)
        installed = True
    collector = GsiCollector(token)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                collector.handle(json.loads(self.rfile.read(n) or b"{}"))
            except Exception as e:
                print(f"[cs2] GSI update error: {e}")
            self.send_response(200)
            self.end_headers()

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", GSI_PORT), Handler)
    except OSError as e:
        return f"GSI port {GSI_PORT} busy: {e}"
    threading.Thread(target=srv.serve_forever, daemon=True, name="cs2-gsi").start()
    _collector = collector
    return "installed cfg (restart CS2 once)" if installed else "listening"


# ---------------- integration ----------------

class CS2(Integration):
    key = "cs2"
    title = "Counter-Strike 2"
    sync_delays = (10, 120)
    sync_while_playing = True

    def applies(self, game):
        return game.get("id") == GAME_ID

    def sync(self, game, sink):
        msgs = []
        if _collector:
            _collector.flush_stale()
            done = _collector.take()
            for m in done:
                sink.match(f"cs2:{int(m['start'])}", GAME_ID, m["start"], m)
            msgs.append(f"{len(done)} new match{'es' if len(done) != 1 else ''} from live tracking")
        msgs += self._sync_demos(sink)
        stats = lifetime_stats()
        if stats:
            sink.snapshot(GAME_ID, "cs2_lifetime", stats)
            msgs.append(f"{stats.get('total_kills', 0):,} lifetime kills")
        return "; ".join(msgs) or "no CS2 stats on this PC yet"

    def _sync_demos(self, sink):
        from .. import cs2_demo, detect
        from ..config import DATA_DIR
        state = os.path.join(DATA_DIR, "cs2_demos.json")
        paths, done = cs2_demo.pending(state)
        if not paths:
            return []
        try:
            import demoparser2  # noqa: F401
        except ImportError:
            return [f"{len(paths)} demo(s) waiting: demoparser2 isn't installed yet (restart the tracker)"]
        sid64, _ = detect.steam_active_account()
        if not sid64:
            return [f"{len(paths)} demo(s) waiting: sign in to Steam so the tracker knows which player is you"]
        maps = cs2_demo.map_data()
        ok, bad = [], []
        for path in paths:
            mid = cs2_demo.demo_id(path)
            try:
                m = cs2_demo.parse(path, sid64, maps)
                sink.match(mid, GAME_ID, m["start"], m)
                ok.append(mid)
            except cs2_demo.NotInDemo:
                bad.append(mid)  # someone else's match (or another Steam account's)
            except Exception as e:
                print(f"[cs2] couldn't read demo {os.path.basename(path)}: {type(e).__name__}: {e}")
                bad.append(mid)  # don't retry a broken demo forever
        cs2_demo.mark_done(state, done, ok + bad)
        return [f"{len(ok)} demo{'s' if len(ok) != 1 else ''} read" + (f", {len(bad)} unreadable" if bad else "")]

    def live(self, game):
        return _collector.live if _collector else None

    # ---------- dashboard ----------

    def modes(self, game):
        counts = Counter((r["data"] or {}).get("queue") or "Unknown" for r in db.matches(GAME_ID, 150))
        if not counts:
            return []
        order = ("Premier", "Competitive", "Competitive / Premier", "Wingman", "Casual", "Deathmatch", "Arms Race")
        rank = lambda q: order.index(q) if q in order else len(order)
        out = [{"key": q, "label": q, "count": n} for q, n in sorted(counts.items(), key=lambda t: (rank(t[0]), -t[1]))]
        out[0]["default"] = True
        return out + [{"key": "all", "label": "All modes", "count": sum(counts.values())}]

    def panel(self, game, mode=None):
        snap = db.latest_snapshot(GAME_ID, "cs2_lifetime")
        ms = [dict(r["data"], _id=r["id"]) for r in db.matches(GAME_ID, 150)]
        if mode not in (None, "all"):
            ms = [m for m in ms if (m.get("queue") or "Unknown") == mode]
        panels = [self._lifetime_panel(snap["data"] if snap else None, ms)]
        if snap and mode not in (None, "all"):
            panels[0]["notes"].insert(0, f"Lifetime totals come from Steam and cover every mode; the tracked matches "
                                         f"below are {mode} only.")
        demos = [m for m in ms if m.get("source") == "demo"]
        if demos:
            panels.append(self._maps_panel(demos))
        if ms:
            panels.append(self._rounds_panel(ms))
        return panels

    def _lifetime_panel(self, s, ms):
        p = {"title": "Counter-Strike 2", "tiles": [], "tables": [], "notes": [], "bars": []}
        if s:
            k, d = s.get("total_kills", 0), s.get("total_deaths", 0)
            rounds = s.get("total_rounds_played", 0)
            shots, hits = s.get("total_shots_fired", 0), s.get("total_shots_hit", 0)
            p["tiles"] += [
                {"label": "K/D", "value": f"{k / max(1, d):.2f}", "sub": f"{k:,} kills, {d:,} deaths"},
                {"label": "Headshot %", "value": f"{100 * s.get('total_kills_headshot', 0) / max(1, k):.1f}%"},
                {"label": "Damage / round", "value": round(s.get("total_damage_done", 0) / max(1, rounds)), "sub": f"{rounds:,} rounds"},
                {"label": "Accuracy", "value": f"{100 * hits / max(1, shots):.1f}%", "sub": f"{hits:,} / {shots:,} shots hit"},
                {"label": "Round win rate", "value": f"{100 * s.get('total_wins', 0) / max(1, rounds):.0f}%", "sub": f"{s.get('total_mvps', 0)} MVPs"},
                {"label": "Time in matches", "value": fmt_duration(s.get("total_time_played", 0))},
            ]
            weapons = []
            for key, val in s.items():
                if key.startswith("total_kills_") and key not in ("total_kills_headshot", "total_kills_enemy_weapon",
                                                                  "total_kills_enemy_blinded", "total_kills_knife_fight",
                                                                  "total_kills_against_zoomed_sniper") and val:
                    w = key[len("total_kills_"):]
                    fired, hit = s.get(f"total_shots_{w}"), s.get(f"total_hits_{w}")
                    weapons.append((w, val, fired, hit))
            weapons.sort(key=lambda t: -t[1])
            if weapons:
                p["bars"].append({"title": "Kills by weapon (lifetime)", "items": [
                    {"label": weapon_name(w), "value": v,
                     "display": f"{v:,}", "sub": f"{100 * h / f:.0f}% accuracy" if f and h else None}
                    for w, v, f, h in weapons[:12]]})
            maps = []
            for key, val in s.items():
                if key.startswith("total_rounds_map_") and val:
                    mp = key[len("total_rounds_map_"):]
                    maps.append((map_name(mp), val, s.get(f"total_wins_map_{mp}", 0)))
            maps.sort(key=lambda t: -t[1])
            if maps:
                p["tables"].append({"title": "Maps (lifetime)", "columns": ["Map", "Rounds", "Rounds won", "Win %"], "rows": [
                    [n, r, w, {"text": f"{100 * w // max(1, r)}%", "tone": "good" if w * 2 > r else "bad"}] for n, r, w in maps]})
            if s.get("last_match_kills") is not None:
                p["notes"].append(f"Last match per Steam: {s.get('last_match_kills')}/{s.get('last_match_deaths')} K/D, "
                                  f"{s.get('last_match_wins')} rounds won, {s.get('last_match_damage')} damage, "
                                  f"{s.get('last_match_mvps', 0)} MVPs.")
        else:
            p["notes"].append("No lifetime stats yet. They come from Steam's local stats cache and sync after you play.")
        if ms:
            p["tables"].append({"title": "Tracked matches", "columns": ["When", "Source", "Mode", "Map", "Result", "Rounds", "K/D/A", "ADR", "HS%", "MVPs"],
                                "rows": [[{"ts": m["start"]}, {"text": "Demo" if m.get("source") == "demo" else "Live"}, m["queue"], m["map"],
                                          {"text": ("Win" if m["won"] else "Loss" if m["won"] is False else "Unfinished" if m.get("partial") else "Draw")
                                                   + (f" {m['rounds_won']}-{m['rounds_lost']}" if m.get("rounds_won") is not None else ""),
                                           "tone": "good" if m["won"] else "bad" if m["won"] is False else None},
                                          {"strip": [{"w": r["won"], "s": (r.get("side") or "?")[:1], "t": f"Round {r['n'] + 1}, {r.get('side') or '?'}: {'won' if r['won'] else 'lost'}, {kills_in(r)} kills"} for r in m["rounds"]]},
                                          f"{m['kills']}/{m['deaths']}/{m['assists']}", m["adr"],
                                          f"{m['hs_pct']}%" if m.get("hs_pct") is not None else "–", m.get("mvps", 0)] for m in ms[:20]],
                                "links": [f"#/match/{m['_id']}" for m in ms[:20]]})
        else:
            p["notes"].append("Match-by-match tracking uses CS2's official Game State Integration. The tracker installed its "
                              "config in CS2's cfg folder; it takes effect the next time CS2 starts.")
        if any(m.get("source") == "gsi" for m in ms[:20]) or not ms:
            p["notes"].append("For round maps, heatmaps and full scoreboards, download the match in CS2 (Watch > Your Matches). "
                              "The tracker reads new demos automatically.")
        return p

    def _rounds_panel(self, ms):
        rows = [(m, r) for m in ms[:20] for r in m.get("rounds") or []]
        p = {"title": "CS2 rounds", "tiles": [], "tables": [], "notes": []}

        def summary(rs):
            return {"rounds": len(rs), "won": sum(r["won"] for r in rs), "kills": sum(kills_in(r) for r in rs),
                    "deaths": sum(not r["survived"] for r in rs), "dmg": sum(r["damage"] for r in rs)}

        n = len(rows)
        multi = sum(1 for _, r in rows if kills_in(r) >= 2)
        p["tiles"] += [
            {"label": "Survived", "value": f"{100 * sum(r['survived'] for _, r in rows) // max(1, n)}%", "sub": f"of {n} rounds"},
            {"label": "Multi-kill rounds", "value": multi, "sub": f"{sum(1 for _, r in rows if kills_in(r) >= 3)} with 3+"},
            {"label": "Rounds with no damage", "value": f"{100 * sum(1 for _, r in rows if not r['damage']) // max(1, n)}%"},
        ]
        sides = {s: summary([r for _, r in rows if r.get("side") == s]) for s in ("ct", "t")}
        p["tables"].append({"title": "By side", "columns": ["Side", "Rounds", "Win %", "Kills / round", "Survived", "ADR"], "rows": [
            [{"ct": "CT", "t": "T"}[s], v["rounds"], f"{100 * v['won'] // max(1, v['rounds'])}%", f"{v['kills'] / max(1, v['rounds']):.2f}",
             f"{100 * (v['rounds'] - v['deaths']) // max(1, v['rounds'])}%", round(v["dmg"] / max(1, v["rounds"]))]
            for s, v in sides.items() if v["rounds"]]})
        econ = [r for m, r in rows if m["queue"] in ("Competitive", "Premier", "Wingman", "Casual", "Competitive / Premier")]
        buys = {b: summary([r for r in econ if r.get("buy") == b]) for b in ("pistol", "eco", "half", "full")}
        p["tables"].append({"title": "By your buy", "columns": ["Buy", "Rounds", "Win %", "K/D", "ADR"], "rows": [
            [{"pistol": "Pistol round", "eco": "Eco (under $1,500)", "half": "Half buy", "full": "Full buy ($3,700+)"}[b], v["rounds"],
             f"{100 * v['won'] // max(1, v['rounds'])}%", f"{v['kills'] / max(1, v['deaths']):.2f}", round(v["dmg"] / max(1, v["rounds"]))]
            for b, v in buys.items() if v["rounds"]]})
        ends = Counter(r.get("end") for _, r in rows if r.get("end"))
        p["tables"].append({"title": "How rounds ended", "columns": ["Ending", "Rounds", "You won"], "rows": [
            [e.capitalize(), c, sum(1 for _, r in rows if r.get("end") == e and r["won"])] for e, c in ends.most_common()]})
        p["notes"].append(f"From {n} rounds across {min(20, len(ms))} tracked matches.")
        return p

    def _maps_panel(self, demos):
        p = {"title": "CS2 maps", "tiles": [], "tables": [], "notes": []}
        rows = [(m, r) for m in demos[:20] for r in m.get("rounds") or []]
        n = len(rows)
        deaths = [r for _, r in rows if r.get("death")]
        p["tiles"] += [
            {"label": "KAST", "value": f"{100 * sum(1 for _, r in rows if r.get('kast')) // max(1, n)}%", "sub": "kill, assist, survive or trade"},
            {"label": "First kills", "value": f"{100 * sum(1 for _, r in rows if r.get('fk')) // max(1, n)}%", "sub": "of rounds you opened"},
            {"label": "First deaths", "value": f"{100 * sum(1 for _, r in rows if r.get('fd')) // max(1, n)}%", "sub": "of rounds you died first"},
            {"label": "Deaths traded", "value": f"{100 * sum(1 for r in deaths if r.get('traded')) // max(1, len(deaths))}%", "sub": "avenged within 5 s"},
        ]
        maps, areas, nades = {}, Counter(), Counter()
        first_at = Counter()
        for m, r in rows:
            mp = maps.setdefault(m["map"], {"name": m["map"], "icon": m.get("map_icon"), "points": []})
            side = r.get("side")
            for k in r.get("kills") or []:
                if k.get("x") is not None:
                    mp["points"].append({"x": k["x"], "y": k["y"], "kind": "kill", "side": side, "ts": m["start"], "lower": k.get("lower"),
                                         "label": f"Round {r['n'] + 1} ({(side or '?').upper()}): {k.get('weapon')} kill on {k.get('victim')} "
                                                  f"at {k['t']}s, from {k.get('area') or '?'}"})
            d = r.get("death")
            if d:
                areas[(m["map"], d.get("area") or "?")] += 1
                if r.get("fd"):
                    first_at[(m["map"], d.get("area") or "?")] += 1
                if d.get("x") is not None:
                    mp["points"].append({"x": d["x"], "y": d["y"], "kind": "death", "side": side, "ts": m["start"], "first": r.get("fd"),
                                         "lower": d.get("lower"),
                                         "label": f"Round {r['n'] + 1} ({(side or '?').upper()}): died to {d.get('by')} ({d.get('weapon')}) "
                                                  f"at {d['t']}s, {d.get('area') or '?'}" + (", first death" if r.get("fd") else "")})
            for u in r.get("utility") or []:
                nades[(m["map"], u["type"])] += 1
                if u.get("x") is not None:
                    mp["points"].append({"x": u["x"], "y": u["y"], "kind": "utility", "side": side, "ts": m["start"], "lower": u.get("lower"),
                                         "label": f"Round {r['n'] + 1} ({(side or '?').upper()}): {u['type']} at {u['t']}s"})
        p["death_map"] = {"maps": sorted((v for v in maps.values() if v["icon"] and v["points"]), key=lambda v: -len(v["points"])),
                          "sides": [["ct", "CT"], ["t", "T"]], "heat": True}
        p["tables"].append({"title": "Where you die", "columns": ["Map", "Callout", "Deaths", "Died first there"],
                            "rows": [[mp, area, c, first_at[(mp, area)]] for (mp, area), c in areas.most_common(12)]})
        per_map = Counter(m["map"] for m, _ in rows)
        if nades:
            p["tables"].append({"title": "Your utility per round", "columns": ["Map", "Smokes", "Flashes", "HE", "Molotovs"],
                                "rows": [[mp, *[f"{nades[(mp, t)] / max(1, per_map[mp]):.2f}" for t in ("smoke", "flash", "he", "molotov")]]
                                         for mp in per_map]})
        p["notes"].append(f"From {len(demos)} downloaded demo{'s' if len(demos) != 1 else ''} ({n} rounds).")
        return p

    def match_context(self, m):
        if m.get("source") == "demo":
            return _demo_context(m)
        lines = [f"Match focus: CS2 {m['queue']} on {m['map']}, "
                 f"{'won' if m['won'] else 'lost' if m['won'] is False else 'unfinished/draw'} {m.get('rounds_won')}-{m.get('rounds_lost')}; "
                 f"{m['kills']}/{m['deaths']}/{m['assists']}, ADR {m['adr']}, HS {m.get('hs_pct')}%, MVPs {m.get('mvps')}"]
        for r in m.get("rounds") or []:
            lines.append(f"R{r['n'] + 1} {r.get('side') or '?'} {'W' if r['won'] else 'L'} ({r.get('end') or '?'}), "
                         f"start ${r.get('money')} / equipment ${r.get('loadout')} ({r.get('buy')}): {kills_in(r)} kills "
                         f"({hs_in(r)} HS), {r['damage']} dmg, {'survived' if r['survived'] else 'died'}")
        return "\n".join(lines)

    def coach_context(self, game):
        snap = db.latest_snapshot(GAME_ID, "cs2_lifetime")
        lines = []
        if snap:
            s = snap["data"]
            keys = ["total_kills", "total_deaths", "total_kills_headshot", "total_rounds_played", "total_wins",
                    "total_mvps", "total_damage_done", "total_shots_fired", "total_shots_hit", "total_time_played"]
            lines.append("CS2 lifetime: " + ", ".join(f"{k.replace('total_', '')} {s.get(k)}" for k in keys if s.get(k) is not None))
            lines.append("Last match (Steam): " + ", ".join(f"{k.replace('last_match_', '')} {v}" for k, v in s.items() if k.startswith("last_match_")))
        for m in [m["data"] for m in db.matches(GAME_ID, 15)]:
            lines.append(f"- {m['queue']} {m['map']}: {'W' if m['won'] else 'L' if m['won'] is False else '?'} "
                         f"{m.get('rounds_won')}-{m.get('rounds_lost')}, {m['kills']}/{m['deaths']}/{m['assists']}, ADR {m['adr']}, HS {m.get('hs_pct')}%")
        return "\n".join(lines)


def _demo_context(m):
    """Detailed text of a demo-parsed CS2 match for the coach (positions as callouts)."""
    lines = [f"Match focus (from demo): CS2 on {m['map']}, {'won' if m['won'] else 'lost' if m['won'] is False else 'draw'} "
             f"{m['rounds_won']}-{m['rounds_lost']}; {m['kills']}/{m['deaths']}/{m['assists']}, ADR {m['adr']}, HS {m.get('hs_pct')}%"]
    if m.get("scoreboard"):
        lines.append("Scoreboard: " + "; ".join(f"{'YOU ' if p['me'] else ''}{p['team']} {p['kills']}/{p['deaths']}/{p['assists']} "
                                                 f"ADR {p['adr']} HS {p.get('hs_pct')}% FK {p['fk']}" for p in m["scoreboard"]))
    for r in m.get("rounds") or []:
        kills = ", ".join(f"{k.get('weapon')} on {k.get('victim')} {k['t']}s from {k.get('area') or '?'}" + (" (HS)" if k.get("hs") else "")
                          for k in r.get("kills") or [])
        d = r.get("death")
        util = Counter(u["type"] for u in r.get("utility") or [])
        lines.append(f"R{r['n'] + 1} {r.get('side')} {'W' if r['won'] else 'L'} ({r.get('end')}), buy {r.get('buy')} vs {r.get('enemy_buy')} "
                     f"(you ${r.get('loadout')}, team avg ${r.get('team_loadout')}, enemy avg ${r.get('enemy_loadout')}): "
                     f"{kills or 'no kills'}; {r.get('damage')} dmg; "
                     + (f"died {d['t']}s to {d.get('by')} ({d.get('weapon')}) at {d.get('area') or '?'}" + (", traded" if r.get("traded") else "")
                        if d else "survived")
                     + (" [first kill]" if r.get("fk") else "") + (" [first death]" if r.get("fd") else "")
                     + (f"; utility: {', '.join(f'{v} {k}' for k, v in util.items())}" if util else "")
                     + (f"; bomb planted {r['plant'].get('site')} at {r['plant']['t']}s" if r.get("plant") else ""))
    return "\n".join(lines)
