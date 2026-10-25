"""VALORANT stats via the Riot Client running on this PC.

Uses the local client's lockfile to get your own auth tokens, then reads your match history,
match details and rank from Riot's game servers (the same calls the game client makes).
These endpoints are unofficial: they need the Riot Client open and can change with patches.
"""
import base64
import json
import math
import os
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime

import requests

from .. import db, net
from . import Integration

LOCAL = os.environ.get("LOCALAPPDATA", "")
LOCKFILE = os.path.join(LOCAL, "Riot Games", "Riot Client", "Config", "lockfile")
SHOOTER_LOG = os.path.join(LOCAL, "VALORANT", "Saved", "Logs", "ShooterGame.log")
PLATFORM = base64.b64encode(json.dumps({
    "platformType": "PC", "platformOS": "Windows",
    "platformOSVersion": "10.0.19042.1.256.64bit", "platformChipset": "Unknown",
}).encode()).decode()
REGION_SHARD = {"na": "na", "latam": "na", "br": "na", "eu": "eu", "ap": "ap", "kr": "kr", "pbe": "pbe"}
QUEUES = {
    "competitive": "Competitive", "unrated": "Unrated", "swiftplay": "Swiftplay", "spikerush": "Spike Rush",
    "deathmatch": "Deathmatch", "ggteam": "Escalation", "onefa": "Replication", "hurm": "Team Deathmatch",
    "premier": "Premier", "newmap": "New Map", "abilitydraftarena": "Ability Draft", "": "Custom",
}
# Mode selector order; anything else follows, most played first.
MODE_ORDER = ("Competitive", "Premier", "Unrated", "Swiftplay", "Spike Rush", "Deathmatch", "Team Deathmatch",
              "Escalation", "Replication", "Ability Draft", "New Map", "Custom")
PANEL_MATCHES = 150


def queue_name(q):
    """Display name for a stored queue (older rows stored the raw id title-cased)."""
    return {v.replace(" ", "").lower(): v for v in QUEUES.values()}.get((q or "").replace(" ", "").lower()) \
        or QUEUES.get((q or "").lower()) or q or "Custom"
GAME_ID = "riot:valorant"
MATCH_VERSION = 7  # bump when the stored summary gains fields; older matches get re-fetched
ECON_QUEUES = ("Competitive", "Unrated", "Premier", "Swiftplay", "Custom")  # modes where buying matters
NON_ROUND_QUEUES = {"deathmatch", "hurm"}  # no rounds: ACS/ADR and round scores don't apply


def standard(m):
    """Round-based game on a regular map (not Deathmatch, not arena modes like Ability Draft on Gauntlet)."""
    return round_based(m) and (bool(m.get("map_icon")) if "map_icon" in m else True)  # v1 rows predate map_icon


def round_based(m):
    """Whether a stored match is a normal round-based game (older rows lack the flag)."""
    if "round_based" in m:
        return m["round_based"]
    return m["queue"] not in ("Deathmatch", "Team Deathmatch") and bool((m.get("rounds_won") or 0) + (m.get("rounds_lost") or 0))


# ---------------- static content (names/icons) from valorant-api.com ----------------

ABILITY_SLOTS = {"Grenade": "c", "Ability1": "q", "Ability2": "e", "Ultimate": "x"}  # valorant-api slot -> default key
RIOT_CASTS = {"grenadeCasts": "c", "ability1Casts": "q", "ability2Casts": "e", "ultimateCasts": "x"}
KEY_LABEL = {"c": "C", "q": "Q", "e": "E", "x": "X"}


def _content():
    maps = net.cached_json("val_maps", "https://valorant-api.com/v1/maps")["data"]
    agents = net.cached_json("val_agents", "https://valorant-api.com/v1/agents?isPlayableCharacter=true")["data"]
    tiers = net.cached_json("val_tiers", "https://valorant-api.com/v1/competitivetiers")["data"][-1]["tiers"]
    weapons = net.cached_json("val_weapons", "https://valorant-api.com/v1/weapons")["data"]
    seasons = net.cached_json("val_seasons", "https://valorant-api.com/v1/seasons")["data"]
    return {
        "acts": _acts(seasons),
        "maps": {m["mapUrl"]: m for m in maps},
        "agents": {a["uuid"].lower(): {"name": a["displayName"], "icon": a.get("displayIcon"), "abilities": {
            ABILITY_SLOTS[x["slot"]]: x["displayName"] for x in a.get("abilities") or [] if x.get("slot") in ABILITY_SLOTS}}
                   for a in agents},
        "tiers": {t["tier"]: {"name": t["tierName"].title(), "icon": t.get("smallIcon") or t.get("largeIcon")} for t in tiers},
        "weapons": {w["uuid"].lower(): w["displayName"] for w in weapons},
    }


ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8, "IX": 9}


def _acts(seasons):
    """Season id -> {"label": "E4 A2" / "V26 A6", "start": unix time} for every act."""
    episodes = {s["uuid"].lower(): s["displayName"] for s in seasons if not s.get("parentUuid")}
    out = {}
    for s in seasons:
        if s.get("type") != "EAresSeasonType::Act":
            continue
        ep = re.sub(r"(?i)episode\s*(\d+)", r"E\1", episodes.get((s.get("parentUuid") or "").lower(), ""))
        act = re.sub(r"(?i)act\s+([IVX]+)", lambda m: f"A{ROMAN.get(m.group(1).upper(), m.group(1))}", s["displayName"])
        try:
            start = datetime.fromisoformat(s["startTime"].replace("Z", "+00:00")).timestamp()
        except (KeyError, TypeError, ValueError):
            start = None
        out[s["uuid"].lower()] = {"label": f"{ep} {act}".strip(), "start": start}
    return out


CAREER_QUEUES = {"competitive": "Competitive", "premier": "Premier", "unrated": "Unrated", "swiftplay": "Swiftplay"}


def career(mmr, content):
    """Riot's record of every act you played, per queue: games and wins, plus final and peak rank in Competitive.
    (Riot keeps only your last 25 matches, but these totals go back to your first act.) Old acts use today's rank
    names, which match up to Diamond; Riot renumbered the ranks above it when Ascendant was added."""
    tiers, acts = content["tiers"], content.get("acts") or {}
    queues = {}
    for q in CAREER_QUEUES:
        rows = []
        for sid, s in (((mmr.get("QueueSkills") or {}).get(q) or {}).get("SeasonalInfoBySeasonID") or {}).items():
            s = s or {}
            if not s.get("NumberOfGames"):
                continue
            act = acts.get(sid.lower(), {})
            row = {"act": act.get("label") or sid[:8], "start": act.get("start"), "games": s["NumberOfGames"],
                   "wins": s.get("NumberOfWinsWithPlacements") or s.get("NumberOfWins") or 0}
            if q == "competitive":
                tier = s.get("CompetitiveTier") or 0
                peak = max([int(t) for t, n in (s.get("WinsByTier") or {}).items() if n and str(t).isdigit()] + [tier])
                row.update(tier=tier, rank=tiers.get(tier, {}).get("name", "Unranked"), icon=tiers.get(tier, {}).get("icon"),
                           peak_tier=peak, peak_rank=tiers.get(peak, {}).get("name", "Unranked"), rr=s.get("RankedRating"),
                           leaderboard=s.get("LeaderboardRank") or None)
            rows.append(row)
        rows.sort(key=lambda r: r["start"] or 0)
        if rows:
            queues[q] = rows
    return {"queues": queues}


def _wl_tone(win_pct):
    return {"text": f"{win_pct:g}%", "tone": "good" if win_pct > 50 else "bad" if win_pct < 50 else None}


def career_text(d):
    comp = d["queues"].get("competitive") or []
    lines = []
    if comp:
        games, wins = sum(r["games"] for r in comp), sum(r["wins"] for r in comp)
        peak = max(comp, key=lambda r: (r.get("peak_tier") or 0, r["start"] or 0))
        lines.append(f"Ranked history (Riot's record of every act): {len(comp)} acts, {games} competitive games, {wins} wins "
                     f"({100 * wins / games:.1f}%); peak {peak['peak_rank']} in {peak['act']}. By act, oldest first (final rank, games, win rate): "
                     + "; ".join(f"{r['act']} {r['rank']} {r['games']}G {pct(r['wins'], r['games'])}" for r in comp))
    other = [(CAREER_QUEUES[q], sum(r["games"] for r in rows), sum(r["wins"] for r in rows))
             for q, rows in d["queues"].items() if q != "competitive"]
    if other:
        lines.append("Other queues, all acts: " + "; ".join(f"{n} {g} games, {pct(w, g)} won" for n, g, w in other))
    return "\n".join(lines)


def lifetime_text(snap):
    d, o, a, df = snap["data"], snap["data"]["overall"], snap["data"].get("attack") or {}, snap["data"].get("defense") or {}
    when = datetime.fromtimestamp(d.get("imported") or snap["ts"]).strftime("%Y-%m-%d")
    lines = [f"Lifetime competitive stats from tracker.gg (all acts, imported {when}; tracker.gg has stored your matches for years, "
             f"so this covers far more than the recent matches below): {o.get('Matches Played')} matches, {o.get('Time Played')} played, "
             f"{o.get('Wins')}W-{o.get('Losses')}L-{o.get('Ties', 0)}T ({o.get('Win %')}), round win {o.get('Round Win %')}, "
             f"K/D {o.get('K/D Ratio')}, KAD {o.get('KAD Ratio')}, ACS {o.get('ACS')}, ADR {o.get('Damage/Round')}, "
             f"HS {o.get('Headshot %')}, KAST {o.get('KAST')}, DDΔ/round {o.get('DDΔ/Round')}, first bloods {o.get('First Bloods')} vs "
             f"first deaths {o.get('First Deaths')}, clutches {o.get('Clutches')} ({o.get('Clutches (1v1)')} 1v1, clutch rate "
             f"{o.get('Clutch %')}), {o.get('Aces')} aces, peak rank {o.get('Peak Rating')}."]
    for name, s in (("Attack", a), ("Defense", df)):
        if s:
            lines.append(f"{name}: round win {s.get('Round Win %')}, ACS {s.get('ACS')}, ADR {s.get('Damage/Round')}, "
                         f"K/D {s.get('K/D Ratio')}, KAST {s.get('KAST')}, first bloods {s.get('First Bloods')} vs first deaths {s.get('First Deaths')}.")
    if d.get("loadouts"):
        lines.append("By your buy: " + "; ".join(f"{x['loadout']} ({x['range']}) K/D {x['kd']}, ACS {x['acs']}, KAST {x['kast']}%"
                                              for x in d["loadouts"]))
    if d.get("roles"):
        lines.append("Roles: " + "; ".join(f"{x['role']} {x['wins']}W-{x['losses']}L ({x['win_pct']}%), KDA {x['kda']}" for x in d["roles"]))
    if d.get("agents"):
        lines.append("Agents (matches, win %, K/D, ACS, ADR, HS %, KAST): " + "; ".join(
            f"{x['agent']} {x['matches']}, {x['win_pct']}%, {x['kd']}, {x['acs']}, {x['adr']}, {x['hs_pct']}%, {x['kast']}%"
            for x in d["agents"][:14]))
    if d.get("maps"):
        lines.append("Maps (W-L, win %, K/D, ACS; your best agents there by win %): " + "; ".join(
            f"{x['map']} {x['wins']}-{x['losses']} {x['win_pct']}% K/D {x['kd']} ACS {x['acs']}"
            + (f" ({', '.join(t['agent'] + ' ' + format(t['win_pct'], 'g') + '%' for t in x.get('top_agents') or [])})" if x.get("top_agents") else "")
            for x in d["maps"]))
    if d.get("weapons"):
        lines.append("Weapons (kills, kills per match): " + ", ".join(
            f"{x['weapon']} {x['kills']} ({x['kills_per_match']}/match" + (f", HS {x['hs_pct']}%" if x.get("hs_pct") is not None else "") + ")"
            for x in d["weapons"][:10]))
    return "\n".join(lines)


# ---------------- auth ----------------

class _Client:
    def __init__(self):
        try:
            with open(LOCKFILE, encoding="utf-8") as f:
                _, _, port, password, _ = f.read().split(":")
        except (OSError, ValueError):
            raise net.Unavailable("Open the Riot Client (or VALORANT) to sync stats.")
        try:
            tok = net.local_get(port, password, "/entitlements/v1/token")
        except requests.RequestException:
            # With no window open the client runs a stripped-down background service with no auth endpoints.
            raise net.Unavailable("Riot Client is only running in the background. Open it (or VALORANT) to sync.")
        self.puuid = tok["subject"]
        self.region, self.shard = self._region(port, password)
        self.headers = {
            "Authorization": f"Bearer {tok['accessToken']}",
            "X-Riot-Entitlements-JWT": tok["token"],
            "X-Riot-ClientPlatform": PLATFORM,
            "X-Riot-ClientVersion": self._version(),
        }
        self.pd = f"https://pd.{self.shard}.a.pvp.net"

    @staticmethod
    def _region(port, password):
        try:
            with open(SHOOTER_LOG, encoding="utf-8", errors="ignore") as f:
                m = re.search(r"https://glz-([a-z0-9]+)-1\.([a-z0-9]+)\.a\.pvp\.net", f.read())
            if m:
                return m.group(1), m.group(2)
        except OSError:
            pass
        try:
            sessions = net.local_get(port, password, "/product-session/v1/external-sessions")
            for s in sessions.values():
                for arg in s.get("launchConfiguration", {}).get("arguments", []):
                    if arg.startswith("-ares-deployment="):
                        region = arg.split("=", 1)[1]
                        return region, REGION_SHARD.get(region, region)
        except requests.RequestException:
            pass
        raise net.Unavailable("Couldn't work out your VALORANT region. Launch VALORANT once, then sync.")

    @staticmethod
    def _version():
        try:
            return net.cached_json("val_version", "https://valorant-api.com/v1/version", ttl=6 * 3600)["data"]["riotClientVersion"]
        except Exception:
            with open(SHOOTER_LOG, encoding="utf-8", errors="ignore") as f:
                m = re.search(r"CI server version: (release-[\w.\-]+)", f.read())
            return m.group(1) if m else "release-00.00-shipping-0-0"

    def names(self, puuids):
        """Riot IDs for players. Match details no longer carry names; the name service does."""
        try:
            r = net.http.put(self.pd + "/name-service/v2/players", json=list(puuids), headers=self.headers, timeout=15)
            r.raise_for_status()
            return {n["Subject"]: f"{n['GameName']}#{n.get('TagLine') or ''}" for n in r.json() if n.get("GameName")}
        except (requests.RequestException, ValueError, KeyError):
            return {}

    def get(self, path):
        r = net.http.get(self.pd + path, headers=self.headers, timeout=15)
        if r.status_code == 429:
            raise net.Unavailable("Riot rate limit hit; will retry on the next sync.")
        r.raise_for_status()
        return r.json()


# ---------------- match summarising ----------------

def summarize(d, puuid, rr_info, content, names=None):
    names = names or {}
    agents, tiers = content["agents"], content["tiers"]
    info = d["matchInfo"]
    mapinfo = content["maps"].get(info.get("mapId")) or {}
    players = d.get("players") or []
    me = next((p for p in players if p["subject"] == puuid), None)
    if not me:
        return None
    st = me.get("stats") or {}
    queue = info.get("queueID") or info.get("queueId") or ""
    rounds = st.get("roundsPlayed") or len(d.get("roundResults") or [])
    is_rounds = queue not in NON_ROUND_QUEUES and rounds > 0
    dmg = hs = bs = ls = 0
    first_kills = 0
    for rr in d.get("roundResults") or []:
        for ps in rr.get("playerStats") or []:
            if ps.get("subject") == puuid:
                for dm in ps.get("damage") or []:
                    dmg += dm.get("damage", 0)
                    hs += dm.get("headshots", 0)
                    bs += dm.get("bodyshots", 0)
                    ls += dm.get("legshots", 0)
        kills = sorted((k for ps in rr.get("playerStats") or [] for k in ps.get("kills") or []),
                       key=lambda k: k.get("roundTime", 0))
        if kills and kills[0].get("killer") == puuid:
            first_kills += 1
    teams = {t["teamId"]: t for t in d.get("teams") or []}
    mine = teams.get(me.get("teamId"), {})
    other = next((t for tid, t in teams.items() if tid != me.get("teamId")), {})
    by_score = sorted(players, key=lambda p: (p.get("stats") or {}).get("score", 0), reverse=True)
    rr = rr_info.get(info["matchId"], {})
    agent = agents.get((me.get("characterId") or "").lower(), {})
    tier = rr.get("TierAfterUpdate") or me.get("competitiveTier") or 0
    won = mine.get("won")
    if is_rounds and mine and other and mine.get("roundsWon") == other.get("roundsWon"):
        won = None  # draw
    if queue == "deathmatch":
        won = None  # free-for-all: placement is the result
    s = {
        "match_id": info["matchId"],
        "start": info.get("gameStartMillis", 0) / 1000,
        "length_sec": info.get("gameLengthMillis", 0) / 1000,
        "queue": QUEUES.get(queue, queue.title()),
        "ranked": bool(info.get("isRanked")),
        "v": MATCH_VERSION,
        "map": mapinfo.get("displayName") or (info.get("mapId") or "?").rsplit("/", 1)[-1],
        "map_icon": mapinfo.get("displayIcon") if mapinfo.get("xMultiplier") else None,
        "map_splash": mapinfo.get("splash"),
        "agent": agent.get("name", "?"), "agent_icon": agent.get("icon"),
        "kills": st.get("kills", 0), "deaths": st.get("deaths", 0), "assists": st.get("assists", 0),
        "round_based": is_rounds,
        "acs": round(st.get("score", 0) / rounds) if is_rounds else None,
        "adr": round(dmg / rounds) if is_rounds else None,
        "hs_pct": round(100 * hs / (hs + bs + ls), 1) if hs + bs + ls else None,
        "first_kills": first_kills,
        "won": won,
        "rounds_won": mine.get("roundsWon") if is_rounds else None,
        "rounds_lost": other.get("roundsWon") if is_rounds else None,
        "placement": by_score.index(me) + 1, "players": len(players),
        "rr_change": rr.get("RankedRatingEarned"), "rank": tiers.get(tier, {}).get("name") if tier else None,
        "name": names.get(puuid) or f"{me.get('gameName', '')}#{me.get('tagLine', '')}",
        "rounds": extract_rounds(d, puuid, mapinfo, content) if is_rounds else [],
        "scoreboard": _scoreboard(d, puuid, content, rounds, is_rounds, names),
    }
    for n, gt0 in _round_starts(d).items():  # when each round's barriers dropped (game time, ms)
        if 0 <= n < len(s["rounds"]) and s["rounds"][n]["n"] == n:
            s["rounds"][n]["gt0"] = gt0
    _add_inputs(s, d, puuid, agent.get("abilities") or {}, st.get("abilityCasts"))
    _add_video(s)
    return s


def _add_video(s):
    """Frames from the last seconds before each death, cut from the NVIDIA recording of this match (when video
    evidence is on). Needs the input log's clock offset to find the deaths in the video. Runs on the gaming PC."""
    try:
        from .. import capture
    except Exception:
        return
    cap = capture.active()
    if not cap or not s["start"]:
        return
    off = (s.get("inputs") or {}).get("offset")
    t0 = s["start"]
    deaths = None if off is None else [(r["n"], t0 + r["death"]["gt"] / 1000 + off) for r in s["rounds"]
                                       if r.get("death") and r["death"].get("gt") is not None]
    # each death's combat report, as it shows a few seconds into the next round's buy phase
    gt0 = {r["n"]: r.get("gt0") for r in s["rounds"]}
    reports = None if off is None else {r["n"]: t0 + (gt0[r["n"] + 1] / 1000 - capture.REPORT_LEAD) + off
                                        for r in s["rounds"] if r.get("death") and gt0.get(r["n"] + 1) is not None}
    video = cap.attach_match(GAME_ID, f"val:{s['match_id']}", t0, t0 + (s["length_sec"] or 0), deaths, reports)
    if video:
        s["video"] = video


def match_state():
    """Where you are in VALORANT, from the Riot client's presence: {"loop": "MENUS" | "PREGAME" | "INGAME", "queue",
    "flow", "map"}, or None when the client can't be read. Only works while VALORANT is running."""
    global _my_puuid
    try:
        with open(LOCKFILE, encoding="utf-8") as f:
            _, _, port, password, _ = f.read().split(":")
        if not _my_puuid:
            _my_puuid = net.local_get(port, password, "/chat/v1/session").get("puuid")
        presences = net.local_get(port, password, "/chat/v4/presences").get("presences") or []
    except (OSError, ValueError, requests.RequestException):
        _my_puuid = None
        return None
    for p in presences:
        if p.get("puuid") != _my_puuid or p.get("product") != "valorant" or not p.get("private"):
            continue
        try:
            d = json.loads(base64.b64decode(p["private"]))
        except (ValueError, TypeError):
            return None
        # Newer clients nest these under matchPresenceData / partyPresenceData; older ones keep them at the top.
        nested = [d.get("matchPresenceData") or {}, d, d.get("partyPresenceData") or {}]
        pick = lambda k: next((x[k] for x in nested if x.get(k)), "")
        return {"loop": pick("sessionLoopState"), "queue": str(pick("queueId")).lower(),
                "flow": pick("provisioningFlow"), "map": pick("matchMap")}
    return None


_my_puuid = None


def _add_inputs(s, d, puuid, ability_names=None, riot_casts=None):
    """Lines your mouse and keyboard log (if the tracker was reading it) up with the match, then adds what your
    hands were doing at each death, how you shot overall and when you used your abilities. Runs on the gaming PC."""
    try:
        from .. import inputs
    except Exception:
        return
    kill_gt = [k["gameTime"] for k in d.get("kills") or [] if k.get("killer") == puuid and k.get("gameTime") is not None]
    if not kill_gt:
        kill_gt = [k["gt"] for r in s["rounds"] for k in r.get("kills") or [] if k.get("gt") is not None]
    t0, t1 = s["start"], s["start"] + (s["length_sec"] or 0)
    if not kill_gt or not t0:
        return
    log = inputs.Log(t0 - 180, t1 + 180, game=GAME_ID)
    if log.empty():
        return
    off, share = inputs.align(log, [t0 + g / 1000 for g in kill_gt])
    s["inputs"] = {"aligned": off is not None, "share": share, "offset": off}
    if off is None:
        return
    for r in s["rounds"]:
        dth = r.get("death")
        if dth and dth.get("gt") is not None:
            dth.setdefault("facts", {}).update(inputs.death_facts(log, int((t0 + dth["gt"] / 1000 + off) * 1000)))
    fights = [int((t0 + k["gameTime"] / 1000 + off) * 1000) for k in d.get("kills") or []
              if puuid in (k.get("killer"), k.get("victim")) and k.get("gameTime") is not None]
    s["shots"] = inputs.shot_summary(log, int((t0 + off) * 1000), int((t1 + off) * 1000), fights)
    if s["rounds"]:
        _add_abilities(s, d, log, off, ability_names or {}, riot_casts or {})


BUY_PHASE_MS = 30000


def _round_starts(d):
    """Round number -> game time (ms) its barriers dropped. Every kill carries both clocks, so this is exact for any
    round with a kill; a round without one has its abilities counted in the round before."""
    by = defaultdict(list)
    for k in d.get("kills") or []:
        if None not in (k.get("gameTime"), k.get("roundTime"), k.get("round")):
            by[k["round"]].append(k["gameTime"] - k["roundTime"])
    return {n: statistics.median(v) for n, v in by.items()}


def _pick_casts(presses, riot):
    """Which presses were casts, per ability. Riot counts each ability's casts in the match, so when you pressed more
    often than you cast (no charges left, cancelled), the presses followed by a throw-click are kept if that's closer to
    Riot's count. Returns ({slot: [cast times]}, {slot: {"riot", "timed", "status"}}); status says how well it matched."""
    casts, check = {}, {}
    for slot in "cqex":
        ps = [p for p in presses if p[1] == slot]
        n = riot.get(slot)
        chosen = ps
        if n == 0:
            chosen = []  # Riot saw none: the presses did nothing (an ultimate that wasn't ready, say)
        elif n is not None and len(ps) > n:
            thrown = [p for p in ps if p[2] is not None]
            if abs(len(thrown) - n) < abs(len(ps) - n):
                chosen = thrown
        casts[slot] = [p[2] or p[0] for p in chosen]
        if n is not None or ps:
            diff = abs(len(chosen) - n) if n is not None else 0
            check[slot] = {"riot": n, "timed": len(chosen), "status": "unknown" if n is None else "exact" if diff == 0
                           else "close" if diff <= max(1, round(0.25 * n)) else "off"}
    return casts, check


def _add_abilities(s, d, log, off, names, riot_raw):
    """When in each round you used each ability, from your C/Q/E/X presses (default keys), checked against Riot's
    count of casts for the match. Times are seconds after the barriers dropped; negative means the buy phase."""
    from .. import inputs
    t0, starts = s["start"], _round_starts(d)
    if not starts:
        return
    wall = lambda gt: int((t0 + gt / 1000 + off) * 1000)  # game time (ms) -> the input log's clock
    order = sorted(starts)
    presses = inputs.ability_presses(log, wall(starts[order[0]]) - 60000, int((t0 + (s["length_sec"] or 0) + off) * 1000))
    if not presses:
        return  # nothing recorded: logs from before ability keys were read, or the keys are bound elsewhere
    casts, check = _pick_casts(presses, {RIOT_CASTS[k]: v for k, v in riot_raw.items() if k in RIOT_CASTS})
    rec = {r["n"]: r for r in s["rounds"]}
    for i, n in enumerate(order):
        # a round runs from its buy phase to the next round's (the first one also has the match's opening seconds)
        lo = wall(starts[n]) - (60000 if i == 0 else BUY_PHASE_MS + 2000)
        hi = wall(starts[order[i + 1]]) - BUY_PHASE_MS - 2000 if i + 1 < len(order) else float("inf")
        r = rec.get(n)
        if r is None:
            continue
        r["abilities"] = sorted(({"k": slot, "t": round((ms - wall(starts[n])) / 1000, 1)}
                                 for slot, times in casts.items() for ms in times if lo <= ms < hi), key=lambda a: a["t"])
        dth = r.get("death")
        if dth and dth.get("t") is not None:
            before = [a for a in r["abilities"] if a["t"] <= dth["t"]]
            dth.setdefault("facts", {}).update({
                "util_before": len(before), "util_used": "".join(sorted({a["k"] for a in before}, key="cqex".index)),
                "last_util_s": round(dth["t"] - before[-1]["t"], 1) if before else None})
    rounds = [r for r in s["rounds"] if "abilities" in r]
    firsts = [next((a["t"] for a in r["abilities"] if a["t"] >= 0), None) for r in rounds]
    firsts = [t for t in firsts if t is not None]
    deaths = [r for r in rounds if r.get("death")]
    ranks = {"exact": 0, "close": 1, "off": 2}
    known = [c["status"] for c in check.values() if c["status"] in ranks]
    s["abilities"] = {
        "names": {k: names.get(k) for k in "cqex"}, "check": check,
        "status": max(known, key=ranks.get) if known else "unknown",
        "first_s": round(statistics.median(firsts), 1) if firsts else None,
        "buy_phase": sum(1 for r in rounds for a in r["abilities"] if a["t"] < 0),
        "deaths": len(deaths), "deaths_no_util": sum(1 for r in deaths if not r["death"]["facts"].get("util_before")),
        "ult_rounds": [r["n"] + 1 for r in rounds if any(a["k"] == "x" for a in r["abilities"])],
        "per_slot": {k: {"rounds": sum(1 for r in rounds if any(a["k"] == k for a in r["abilities"])),
                         "median_t": round(statistics.median(ts), 1) if (ts := [a["t"] for r in rounds for a in r["abilities"]
                                                                              if a["k"] == k and a["t"] >= 0]) else None}
                     for k in "cqex" if casts.get(k)},
    }


def ability_text(m, r=None):
    """Ability use as text for the coach: the match summary, or one round's casts."""
    a = m.get("abilities") or {}
    name = lambda k: f"{KEY_LABEL[k]} {a.get('names', {}).get(k) or ''}".strip()
    if r is not None:
        return ", ".join(f"{name(x['k'])} {'buy phase' if x['t'] < 0 else str(x['t']) + 's'}" for x in r.get("abilities") or [])
    if not a:
        return ""
    slots = "; ".join(f"{name(k)}: {c['timed']} timed of {c['riot'] if c['riot'] is not None else '?'} cast"
                      + (f", usually {a['per_slot'][k]['median_t']}s into the round" if (a.get("per_slot") or {}).get(k, {}).get("median_t") is not None else "")
                      for k, c in a["check"].items())
    return (f"Abilities (from their key presses; {'matches' if a['status'] == 'exact' else 'close to' if a['status'] == 'close' else 'does not match'} "
            f"Riot's cast counts): {slots}. First ability after barriers drop: median {a['first_s']}s; {a['buy_phase']} cast in buy phase; "
            f"died before using any ability in {a['deaths_no_util']} of {a['deaths']} deaths; ultimate in "
            + (f"rounds {', '.join(map(str, a['ult_rounds']))}." if a["ult_rounds"] else "no round."))


def _scoreboard(d, puuid, content, rounds, is_rounds, names=None):
    names = names or {}
    """Every player's line: agent, rank, K/D/A, ACS, ADR, HS%, first kills; marked ally/enemy/party."""
    agents, tiers = content["agents"], content["tiers"]
    dmg, hs, shots, fk = Counter(), Counter(), Counter(), Counter()
    for r in d.get("roundResults") or []:
        kills = sorted((k for ps in r.get("playerStats") or [] for k in ps.get("kills") or []),
                       key=lambda k: k.get("roundTime", 0))
        if kills:
            fk[kills[0].get("killer")] += 1
        for ps in r.get("playerStats") or []:
            for dm in ps.get("damage") or []:
                sid = ps.get("subject")
                dmg[sid] += dm.get("damage", 0)
                hs[sid] += dm.get("headshots", 0)
                shots[sid] += dm.get("headshots", 0) + dm.get("bodyshots", 0) + dm.get("legshots", 0)
    players = d.get("players") or []
    me = next(p for p in players if p["subject"] == puuid)
    out = []
    for p in players:
        sid, st = p["subject"], p.get("stats") or {}
        agent = agents.get((p.get("characterId") or "").lower(), {})
        tier = p.get("competitiveTier") or 0
        out.append({
            "name": names.get(sid) or (f"{p['gameName']}#{p.get('tagLine') or ''}" if p.get("gameName") else None),
            "me": sid == puuid,
            "team": "ally" if p.get("teamId") == me.get("teamId") else "enemy",
            "party": sid != puuid and bool(p.get("partyId")) and p.get("partyId") == me.get("partyId"),
            "agent": agent.get("name", "?"), "agent_icon": agent.get("icon"),
            "rank": tiers.get(tier, {}).get("name") if tier else None,
            "rank_icon": tiers.get(tier, {}).get("icon") if tier else None,
            "kills": st.get("kills", 0), "deaths": st.get("deaths", 0), "assists": st.get("assists", 0),
            "score": st.get("score", 0),
            "acs": round(st.get("score", 0) / rounds) if is_rounds and rounds else None,
            "adr": round(dmg[sid] / rounds) if is_rounds and rounds else None,
            "hs_pct": round(100 * hs[sid] / shots[sid], 1) if shots[sid] else None,
            "fk": fk[sid],
        })
    out.sort(key=lambda x: -x["score"])
    return out


# ---------------- round-by-round ----------------

def _buy(values):
    """Classify a buy from the average loadout value: rifle + light shields (3300) counts as a full buy."""
    if not values:
        return None
    avg = sum(values) / len(values)
    return "eco" if avg < 2000 else "half" if avg < 3300 else "full"


def _project(mapinfo, loc):
    """Game coordinates -> 0..1 minimap coordinates (valorant-api's transform; note the x/y swap)."""
    if not loc or not mapinfo.get("xMultiplier"):
        return None, None
    return (round(loc["y"] * mapinfo["xMultiplier"] + mapinfo["xScalarToAdd"], 4),
            round(loc["x"] * mapinfo["yMultiplier"] + mapinfo["yScalarToAdd"], 4))


def _area(mapinfo, loc):
    """Nearest named callout, e.g. 'B Main'."""
    best = None
    for c in mapinfo.get("callouts") or []:
        cl = c.get("location") or {}
        dist = (cl.get("x", 0) - loc["x"]) ** 2 + (cl.get("y", 0) - loc["y"]) ** 2
        if best is None or dist < best[0]:
            best = (dist, f"{c.get('superRegionName', '')} {c.get('regionName', '')}".strip())
    return best[1] if best else None


def _weapon(kill, weapons):
    fd = kill.get("finishingDamage") or {}
    kind = fd.get("damageType")
    if kind == "Weapon":
        return weapons.get((fd.get("damageItem") or "").lower(), "Weapon")
    return {"Melee": "Knife", "Bomb": "Spike"}.get(kind, kind or "?")


def _assisted(kill, puuid):
    for a in kill.get("assistants") or []:
        if (a.get("assistantPuuid") if isinstance(a, dict) else a) == puuid:
            return True
    return False


def _spot(mapinfo, loc):
    x, y = _project(mapinfo, loc)
    return {"x": x, "y": y, "area": _area(mapinfo, loc) if loc else None}


def _dist(a, b):
    return math.hypot(a["x"] - b["x"], a["y"] - b["y"]) / 100  # map units are centimetres


def _death_facts(death, puuid, team_of, my_team, killer_at, kills_before, last_view=None):
    """Who was alive and how close at the moment you died. Returns (facts, (your view, dx, dy) to the killer).

    Riot lists only living players at a kill, so you never appear at your own death: your view comes from the last
    kill earlier in the round where you were still listed (last_view), when that was within 1.5 seconds."""
    locs = {pl.get("subject"): pl for pl in death.get("playerLocations") or []}
    me_at = death.get("victimLocation") or (locs.get(puuid) or {}).get("location")
    allies = [pl for sid, pl in locs.items() if sid != puuid and team_of.get(sid) == my_team and pl.get("location")]
    enemies = [pl for sid, pl in locs.items() if team_of.get(sid) not in (None, my_team) and pl.get("location")]
    facts = {"allies_alive": len(allies), "enemies_alive": len(enemies), "ally_m": None}
    if me_at and allies:
        facts["ally_m"] = round(min(_dist(me_at, pl["location"]) for pl in allies), 1)
    view = None
    if me_at and killer_at and death.get("killer") != puuid:
        facts["killer_m"] = round(_dist(me_at, killer_at), 1)
        mine = (locs.get(puuid) or {}).get("viewRadians")
        if mine is None:
            mine = last_view
        if mine is not None:
            view = (mine, killer_at["x"] - me_at["x"], killer_at["y"] - me_at["y"])
    if kills_before:
        facts["after_kill_s"] = round((death.get("roundTime", 0) - max(k.get("roundTime", 0) for k in kills_before)) / 1000, 1)
    return facts, view


def _facing_convention(samples):
    """Riot doesn't document which way viewRadians points. A killer is looking at their victim, so try the possible
    conventions on the match's kills and keep the one that fits. Returns (sign, offset) or None."""
    if len(samples) < 3:
        return None
    best = None
    for sign in (1, -1):
        for off in (0, math.pi / 2, math.pi, -math.pi / 2):
            errs = sorted(abs((sign * v + off - math.atan2(dy, dx) + math.pi) % (2 * math.pi) - math.pi) for v, dx, dy in samples)
            med = errs[len(errs) // 2]
            if best is None or med < best[0]:
                best = (med, sign, off)
    return (best[1], best[2]) if best[0] < math.radians(30) else None


def extract_rounds(d, puuid, mapinfo, content):
    """Compact per-round record for the player: side, buys, opening duels, kills/death with positions."""
    agents, weapons = content["agents"], content["weapons"]
    players = {p["subject"]: p for p in d.get("players") or []}
    my_team = players[puuid]["teamId"]
    team_of = {sid: p.get("teamId") for sid, p in players.items()}

    def agent_of(sid):
        return agents.get((players.get(sid, {}).get("characterId") or "").lower(), {}).get("name", "?")

    out, aim, facing_todo = [], [], []
    for r in d.get("roundResults") or []:
        won = r.get("winningTeam") == my_team
        role = r.get("winningTeamRole")
        side = None
        if role in ("Attacker", "Defender"):
            side = "attack" if (role == "Attacker") == won else "defense"
        kills = sorted((k for ps in r.get("playerStats") or [] for k in ps.get("kills") or []),
                       key=lambda k: k.get("roundTime", 0))
        eco = {ps.get("subject"): (ps.get("economy") or {}) for ps in r.get("playerStats") or []}
        mine = eco.get(puuid, {})
        my_kills = [k for k in kills if k.get("killer") == puuid]
        death = next((k for k in kills if k.get("victim") == puuid), None)
        traded = bool(death) and any(
            k.get("victim") == death.get("killer") and team_of.get(k.get("killer")) == my_team
            and 0 <= k.get("roundTime", 0) - death.get("roundTime", 0) <= 5000 for k in kills)
        assists = sum(1 for k in kills if _assisted(k, puuid))
        team_vals = [e.get("loadoutValue", 0) for sid, e in eco.items() if team_of.get(sid) == my_team]
        enemy_vals = [e.get("loadoutValue", 0) for sid, e in eco.items() if team_of.get(sid) not in (None, my_team)]

        def who(sid):
            return "me" if sid == puuid else "ally" if team_of.get(sid) == my_team else "enemy"

        rec = {
            "n": r.get("roundNum", len(out)), "side": side, "won": won, "end": r.get("roundResult"),
            "team_loadout": round(sum(team_vals) / len(team_vals)) if team_vals else None,
            "enemy_loadout": round(sum(enemy_vals) / len(enemy_vals)) if enemy_vals else None,
            "feed": [{"t": round(k.get("roundTime", 0) / 1000, 1), "killer": agent_of(k.get("killer")), "ks": who(k.get("killer")),
                      "victim": agent_of(k.get("victim")), "vs": who(k.get("victim")), "weapon": _weapon(k, weapons)}
                     for k in kills],
            "loadout": mine.get("loadoutValue"), "spent": mine.get("spent"),
            "weapon": weapons.get((mine.get("weapon") or "").lower()), "armor": bool(mine.get("armor")),
            "buy": _buy(team_vals), "enemy_buy": _buy(enemy_vals),
            "kills": [], "death": None, "assists": assists, "traded": traded,
            "fk": bool(kills) and kills[0].get("killer") == puuid,
            "fd": bool(kills) and kills[0].get("victim") == puuid,
            "plant": {"site": r["plantSite"], "t": round(r.get("plantRoundTime", 0) / 1000, 1),
                      **_spot(mapinfo, r.get("plantLocation"))} if r.get("plantSite") else None,
            "defuse_t": round(r["defuseRoundTime"] / 1000, 1) if r.get("defuseRoundTime") else None,
        }
        for k in my_kills:
            me_pl = next((pl for pl in k.get("playerLocations") or [] if pl.get("subject") == puuid), {})
            me_at = me_pl.get("location")
            vx, vy = _project(mapinfo, k.get("victimLocation"))
            rec["kills"].append({"t": round(k.get("roundTime", 0) / 1000, 1), "gt": k.get("gameTime"), "weapon": _weapon(k, weapons),
                                 "victim": agent_of(k.get("victim")), "vx": vx, "vy": vy, **_spot(mapinfo, me_at)})
        for k in kills:  # every killer faces their victim: calibrates which way viewRadians points
            pl = next((p for p in k.get("playerLocations") or [] if p.get("subject") == k.get("killer")), {})
            if pl.get("viewRadians") is not None and pl.get("location") and k.get("victimLocation"):
                aim.append((pl["viewRadians"], k["victimLocation"]["x"] - pl["location"]["x"],
                            k["victimLocation"]["y"] - pl["location"]["y"]))
        if death:
            killer_at = next((pl.get("location") for pl in death.get("playerLocations") or []
                              if pl.get("subject") == death.get("killer")), None)
            kx, ky = _project(mapinfo, killer_at)
            rec["death"] = {"t": round(death.get("roundTime", 0) / 1000, 1), "gt": death.get("gameTime"),
                            "weapon": _weapon(death, weapons), "by": agent_of(death.get("killer")), "kx": kx, "ky": ky,
                            **_spot(mapinfo, death.get("victimLocation"))}
            seen = [(k.get("roundTime", 0), pl.get("viewRadians")) for k in kills if k.get("roundTime", 0) <= death.get("roundTime", 0)
                    for pl in k.get("playerLocations") or [] if pl.get("subject") == puuid and pl.get("viewRadians") is not None]
            last_view = seen[-1][1] if seen and death.get("roundTime", 0) - seen[-1][0] <= 1500 else None
            rec["death"]["facts"], view = _death_facts(death, puuid, team_of, my_team, killer_at,
                                                       [k for k in my_kills if k.get("roundTime", 0) <= death.get("roundTime", 0)],
                                                       last_view)
            if view:
                facing_todo.append((rec["death"], view))
        rec["kast"] = bool(rec["kills"]) or assists > 0 or death is None or traded
        out.append(rec)
    conv = _facing_convention(aim)
    for dth, (view, dx, dy) in facing_todo:
        if conv:
            dth["facts"]["facing"] = round(math.degrees(abs((conv[0] * view + conv[1] - math.atan2(dy, dx) + math.pi) % (2 * math.pi) - math.pi)))
    # Pistol rounds: the first round of each half (the first side switch), not overtime.
    half = next((i for i in range(1, len(out)) if out[i]["side"] and out[i]["side"] != out[0]["side"]), None)
    for rec in out:
        if rec["n"] == 0 or (half and rec["n"] == half):
            rec["buy"] = rec["enemy_buy"] = "pistol"
    return out


class Valorant(Integration):
    key = "valorant"
    title = "VALORANT competitive"
    sync_delays = (45, 240)
    sync_while_playing = True

    def applies(self, game):
        return game["id"] == GAME_ID

    def sync(self, game, sink):
        c = _Client()
        content = _content()
        tiers = content["tiers"]
        mmr = c.get(f"/mmr/v1/players/{c.puuid}")
        updates = c.get(f"/mmr/v1/players/{c.puuid}/competitiveupdates?startIndex=0&endIndex=20&queue=competitive")
        rr_info = {m["MatchID"]: m for m in updates.get("Matches") or []}
        latest = (mmr.get("LatestCompetitiveUpdate") or {})
        season = latest.get("SeasonID")
        seasonal = ((mmr.get("QueueSkills") or {}).get("competitive") or {}).get("SeasonalInfoBySeasonID") or {}
        cur = seasonal.get(season) or {}
        tier = latest.get("TierAfterUpdate") or cur.get("CompetitiveTier") or 0
        sink.snapshot(GAME_ID, "val_rank", {
            "tier": tier, "rank": tiers.get(tier, {}).get("name", "Unranked"), "icon": tiers.get(tier, {}).get("icon"),
            "rr": latest.get("RankedRatingAfterUpdate", cur.get("RankedRating")),
            "season_wins": cur.get("NumberOfWinsWithPlacements", cur.get("NumberOfWins")),
            "season_games": cur.get("NumberOfGames"), "leaderboard": cur.get("LeaderboardRank"),
        })
        sink.snapshot(GAME_ID, "val_career", career(mmr, content))  # stored only when it changes
        known = sink.known_matches(GAME_ID, min_v=MATCH_VERSION)  # older summaries lack round data: refetch
        history = c.get(f"/match-history/v1/history/{c.puuid}?startIndex=0&endIndex=25").get("History") or []
        new = 0
        for h in history:
            mid = f"val:{h['MatchID']}"
            if mid in known:
                continue
            if new >= 10:  # stay well under rate limits; the rest come next sync
                break
            details = c.get(f"/match-details/v1/matches/{h['MatchID']}")
            try:  # the newest raw match, kept locally to check Riot's field names against
                from ..config import CACHE_DIR
                with open(os.path.join(CACHE_DIR, "val_last_match.json"), "w", encoding="utf-8") as f:
                    json.dump(details, f)
            except OSError:
                pass
            s = summarize(details, c.puuid, rr_info, content,
                          names=c.names(p["subject"] for p in details.get("players") or []))
            if s and (s.get("video") or {}).get("status") == "pending":
                continue  # its recording is still being saved; the next sync picks the match up
            if s:
                sink.match(mid, GAME_ID, s["start"], s)
                new += 1
        return f"{new} new match{'es' if new != 1 else ''}"

    # ---------- dashboard ----------

    def modes(self, game):
        counts = Counter((m["data"] or {}).get("queue") for m in db.matches(GAME_ID, PANEL_MATCHES))
        if not counts:
            return []
        rank = lambda q: MODE_ORDER.index(queue_name(q)) if queue_name(q) in MODE_ORDER else len(MODE_ORDER)
        out = [{"key": q, "label": queue_name(q), "count": n}
               for q, n in sorted(counts.items(), key=lambda t: (rank(t[0]), -t[1]))]
        default = "Competitive" if "Competitive" in counts else max(counts, key=counts.get)
        for m in out:
            m["default"] = m["key"] == default
        return out + [{"key": "all", "label": "All modes", "count": sum(counts.values())}]

    def panel(self, game, mode=None):
        allm = [m["data"] for m in db.matches(GAME_ID, PANEL_MATCHES)]
        ms = allm if mode in (None, "all") else [m for m in allm if m.get("queue") == mode]
        panels = [self._main_panel(ms, mode)]
        if mode in (None, "all", "Competitive"):  # all-time numbers are competitive only
            icons = {m["agent"]: m.get("agent_icon") for m in reversed(allm) if m.get("agent")}
            try:  # every agent's icon, for ones you haven't played lately
                icons = {a["name"]: a["icon"] for a in _content()["agents"].values()} | icons
            except Exception:
                pass
            life, car = db.latest_snapshot(GAME_ID, "val_trackergg"), db.latest_snapshot(GAME_ID, "val_career")
            panels += [p for p in (self._lifetime_panel(life, icons) if life else None,
                                   self._career_panel(car) if car else None) if p]
        st = round_stats(ms)
        if st:
            panels.append(self._rounds_panel(st))
        return panels

    def _main_panel(self, ms, mode=None):
        rank = db.latest_snapshot(GAME_ID, "val_rank") if mode in (None, "all", "Competitive") else None
        p = {"title": "VALORANT, all modes" if mode == "all" else self.title if mode is None else f"VALORANT {queue_name(mode)}",
             "tiles": [], "tables": [], "notes": []}
        if rank:
            r = rank["data"]
            p["tiles"].append({"label": "Rank", "value": r["rank"], "icon": r.get("icon"),
                               "sub": f"{r['rr']} RR" if r.get("rr") is not None else None})
            if r.get("season_games"):
                p["tiles"].append({"label": "Season", "value": f"{r['season_wins']}W / {r['season_games']}G",
                                   "sub": f"{100 * r['season_wins'] // max(1, r['season_games'])}% win rate"})
        if not ms:
            p["notes"].append("No matches synced yet. Open the Riot Client and press Sync.")
            return p
        core = [m for m in ms if standard(m)]  # deathmatch and arena modes would skew ACS, ADR and K/D
        recent = core[:20]
        if recent:
            decided = [m for m in recent if m["won"] is not None]
            wins = sum(1 for m in decided if m["won"])
            k, d, a = (sum(m[x] for m in recent) for x in ("kills", "deaths", "assists"))
            hs = [m["hs_pct"] for m in recent if m.get("hs_pct") is not None]
            p["tiles"] += [
                {"label": f"Win rate (last {len(recent)})", "value": f"{100 * wins // max(1, len(decided))}%",
                 "sub": f"{wins}W {len(decided) - wins}L" + (f" {len(recent) - len(decided)}D" if len(recent) > len(decided) else "")},
                {"label": "K/D", "value": f"{k / max(1, d):.2f}", "sub": f"{k}/{d}/{a} K/D/A"},
                {"label": "Avg ACS", "value": round(statistics.mean(m["acs"] for m in recent)),
                 "sub": f"ADR {round(statistics.mean(m['adr'] for m in recent))}"},
                {"label": "Headshot %", "value": f"{statistics.mean(hs):.1f}%" if hs else "–"},
            ]
            if mode in (None, "all"):
                p["notes"].append("Win rate, K/D, ACS and headshot % count standard round-based games only "
                                  "(not Deathmatch or arena modes). Pick a mode above to see those on their own.")
        elif ms:
            self._casual(p, ms)
            return self._recent_table(p, ms)
        agents = defaultdict(list)
        for m in core:
            agents[m["agent"]].append(m)
        p["tables"].append({"title": "Agents", "columns": ["Agent", "Games", "Win %", "K/D", "ACS"], "rows": [
            [{"text": n, "icon": g[0].get("agent_icon")}, len(g),
             f"{100 * sum(1 for x in g if x['won']) // len(g)}%",
             f"{sum(x['kills'] for x in g) / max(1, sum(x['deaths'] for x in g)):.2f}",
             round(statistics.mean(x["acs"] for x in g))]
            for n, g in sorted(agents.items(), key=lambda t: -len(t[1]))[:8]]})
        maps = defaultdict(list)
        for m in core:
            maps[m["map"]].append(m)
        p["tables"].append({"title": "Maps", "columns": ["Map", "Games", "Win %"], "rows": [
            [n, len(g), {"text": f"{100 * sum(1 for x in g if x['won']) // len(g)}%",
                         "tone": "good" if sum(1 for x in g if x["won"]) * 2 > len(g) else "bad"}]
            for n, g in sorted(maps.items(), key=lambda t: -len(t[1]))]})
        return self._recent_table(p, ms)

    @staticmethod
    def _casual(p, ms):
        """Modes without rounds (Deathmatch, Team Deathmatch, arenas): no ACS or ADR, placement or team result instead."""
        recent = ms[:20]
        k, d, a = (sum(m[x] for m in recent) for x in ("kills", "deaths", "assists"))
        decided = [m for m in recent if m.get("won") is not None]
        if decided:
            wins = sum(1 for m in decided if m["won"])
            p["tiles"].append({"label": f"Win rate (last {len(decided)})", "value": f"{100 * wins // len(decided)}%",
                               "sub": f"{wins}W {len(decided) - wins}L"})
        else:
            places = [m["placement"] for m in recent if m.get("placement")]
            if places:
                p["tiles"].append({"label": f"Avg placement (last {len(places)})", "value": f"#{statistics.mean(places):.1f}",
                                   "sub": f"of {round(statistics.mean(m['players'] for m in recent if m.get('players')))} players, "
                                          f"{sum(1 for x in places if x == 1)} first places"})
        hs = [m["hs_pct"] for m in recent if m.get("hs_pct") is not None]
        p["tiles"] += [
            {"label": "K/D", "value": f"{k / max(1, d):.2f}", "sub": f"{k}/{d}/{a} K/D/A"},
            {"label": "Kills per game", "value": f"{k / len(recent):.1f}", "sub": f"{len(recent)} games"},
        ] + ([{"label": "Headshot %", "value": f"{statistics.mean(hs):.1f}%"}] if hs else [])  # Riot has no hit data for Deathmatch
        team = any(m.get("won") is not None for m in ms)
        result_col = "Win %" if team else "Avg place"
        res = lambda g: (f"{100 * sum(1 for x in g if x.get('won')) // len(g)}%" if team
                         else f"#{statistics.mean(x['placement'] for x in g):.1f}")
        for key, title in (("agent", "Agents"), ("map", "Maps")):
            groups = defaultdict(list)
            for m in ms:
                groups[m[key]].append(m)
            p["tables"].append({"title": title, "columns": [title[:-1], "Games", "K/D", "Kills / game", result_col], "rows": [
                [{"text": n, "icon": g[0].get("agent_icon")} if key == "agent" else n, len(g),
                 f"{sum(x['kills'] for x in g) / max(1, sum(x['deaths'] for x in g)):.2f}",
                 f"{sum(x['kills'] for x in g) / len(g):.1f}", res(g)]
                for n, g in sorted(groups.items(), key=lambda t: -len(t[1]))[:10]]})

    @staticmethod
    def _recent_table(p, ms):
        def result(m):
            if not round_based(m):
                return {"text": f"#{m['placement']} of {m['players']}", "tone": "good" if m["placement"] == 1 else None}
            return {"text": ("Win" if m["won"] else "Loss" if m["won"] is False else "Draw")
                    + (f" {m['rounds_won']}-{m['rounds_lost']}" if m.get("rounds_won") is not None else ""),
                    "tone": "good" if m["won"] else "bad" if m["won"] is False else None}

        p["tables"].append({"title": "Recent matches", "columns":
                            ["When", "Mode", "Map", "Agent", "Result", "Rounds", "K/D/A", "ACS", "HS%", "RR"], "rows": [
            [{"ts": m["start"]}, m["queue"], m["map"], {"text": m["agent"], "icon": m.get("agent_icon")}, result(m),
             {"strip": [round_chip(r) for r in m.get("rounds") or []]} if m.get("rounds") else "",
             f"{m['kills']}/{m['deaths']}/{m['assists']}", m["acs"] if round_based(m) else "–",
             f"{m['hs_pct']}%" if m.get("hs_pct") is not None else "–",
             {"text": f"{m['rr_change']:+d}" if m.get("rr_change") is not None else "",
              "tone": "good" if (m.get("rr_change") or 0) > 0 else "bad" if (m.get("rr_change") or 0) < 0 else None}]
            for m in ms[:20]],
            "links": [f"#/match/val:{m['match_id']}" for m in ms[:20]]})
        return p

    def _career_panel(self, snap):
        d = snap["data"]
        comp = d["queues"].get("competitive") or []
        if not comp:
            return None
        games, wins = sum(r["games"] for r in comp), sum(r["wins"] for r in comp)
        peak = max(comp, key=lambda r: (r.get("peak_tier") or 0, r["start"] or 0))
        p = {"title": "VALORANT career, every act", "tiles": [
            {"label": "Competitive games", "value": f"{games:,}", "sub": f"{wins:,} wins, {100 * wins / games:.1f}%"},
            {"label": "Acts played", "value": len(comp), "sub": f"since {comp[0]['act']}"},
            {"label": "Peak rank", "value": peak["peak_rank"], "icon": None, "sub": f"in {peak['act']}"},
        ], "tables": [], "notes": []}
        for q, rows in d["queues"].items():
            if q != "competitive":
                g, w = sum(r["games"] for r in rows), sum(r["wins"] for r in rows)
                p["tiles"].append({"label": CAREER_QUEUES[q], "value": f"{g:,} games", "sub": f"{pct(w, g)} won"})
        ranked = [r for r in comp if r.get("tier") and r.get("start")]
        if len(ranked) >= 2:
            names = {r["tier"]: r["rank"] for r in comp if r.get("tier")}
            p["timeline"] = {"title": "Rank at the end of each act", "line": True, "series": [{"name": "Final rank", "group": "rank", "points": [
                {"ts": r["start"], "v": r["tier"], "label": f"{r['act']}, {r['rank']} ({r['games']} games, {pct(r['wins'], r['games'])} won)"}
                for r in ranked]}], "ticks": [{"v": t, "label": names.get(t) or TIER_TICKS[t]} for t in TIER_TICKS]}
        p["tables"].append({"title": "Every act", "columns": ["Act", "Final rank", "Peak", "RR", "Games", "Win %"], "rows": [
            # RR above 100 only exists at Immortal and up (Episode 1 reported other numbers here)
            [r["act"], {"text": r["rank"], "icon": r.get("icon")}, r["peak_rank"],
             r["rr"] if r.get("tier") and r.get("rr") is not None and (r["rr"] <= 100 or r["tier"] >= 24) else "–", r["games"],
             tone_pct(r["wins"], r["games"])] for r in reversed(comp)]})
        p["notes"].append("From Riot's own record of every act you played: games, wins and rank. Riot keeps only your last 25 "
                          "matches in full, so detailed stats for older games come from tracker.gg above.")
        return p

    def _lifetime_panel(self, snap, icons):
        d = snap["data"]
        o = d.get("overall") or {}
        when = datetime.fromtimestamp(d.get("imported") or snap["ts"])
        p = {"title": "Lifetime competitive (tracker.gg)", "tiles": [
            {"label": "Matches", "value": o.get("Matches Played", "–"), "sub": f"{o.get('Time Played', '')} played"},
            {"label": "Win rate", "value": o.get("Win %", "–"), "sub": f"{o.get('Wins')}W {o.get('Losses')}L"},
            {"label": "K/D", "value": o.get("K/D Ratio", "–"), "sub": f"KAD {o.get('KAD Ratio')}"},
            {"label": "ACS", "value": o.get("ACS", "–"), "sub": f"ADR {o.get('Damage/Round')}"},
            {"label": "Headshot %", "value": o.get("Headshot %", "–"), "sub": f"KAST {o.get('KAST')}"},
            {"label": "First bloods", "value": o.get("First Bloods", "–"), "sub": f"{o.get('First Deaths')} first deaths"},
            {"label": "Clutches", "value": o.get("Clutches", "–"), "sub": f"{o.get('Clutches (1v1)')} were 1v1s"},
            {"label": "Peak rank", "value": o.get("Peak Rating", "–"), "sub": f"now {o.get('Rating')}" if o.get("Rating") else None},
        ], "tables": [], "notes": []}
        p["tables"].append({"title": "Agents", "columns": ["Agent", "Matches", "Hours", "Win %", "K/D", "ACS", "ADR", "DDΔ", "HS %", "KAST"],
                            "rows": [[{"text": x["agent"], "icon": icons.get(x["agent"])}, x["matches"], f"{x['hours']:g}", _wl_tone(x["win_pct"]),
                                      f"{x['kd']:.2f}", x["acs"], x["adr"], f"{x['ddd']:+g}", f"{x['hs_pct']}%", f"{x['kast']}%"]
                                     for x in (d.get("agents") or [])[:14]]})
        p["tables"].append({"title": "Maps", "columns": ["Map", "W–L", "Win %", "K/D", "ACS", "Your best agents there"],
                            "rows": [[x["map"], f"{x['wins']}–{x['losses']}", _wl_tone(x["win_pct"]), f"{x['kd']:.2f}", x["acs"],
                                      ", ".join(f"{t['agent']} {t['win_pct']:g}%" for t in x.get("top_agents") or [])]
                                     for x in d.get("maps") or []]})
        sides = [(n, d.get(k) or {}) for n, k in (("Attack", "attack"), ("Defense", "defense"))]
        p["tables"].append({"title": "By side", "columns": ["Side", "Rounds", "Round win %", "ACS", "ADR", "K/D", "KAST", "First bloods", "First deaths"],
                            "rows": [[n, s.get("Rounds Played"), s.get("Round Win %"), s.get("ACS"), s.get("Damage/Round"), s.get("K/D Ratio"),
                                      s.get("KAST"), s.get("First Bloods"), s.get("First Deaths")] for n, s in sides if s]})
        p["tables"].append({"title": "By your buy", "columns": ["Buy", "Spent", "K/D", "ACS", "ADR", "KAST", "Kills", "Deaths"],
                            "rows": [[x["loadout"], x["range"], f"{x['kd']:.2f}", x["acs"], x["adr"], f"{x['kast']}%", f"{x['kills']:,}", f"{x['deaths']:,}"]
                                     for x in d.get("loadouts") or []]})
        p["tables"].append({"title": "Roles", "columns": ["Role", "W–L", "Win %", "KDA", "Kills / deaths / assists"],
                            "rows": [[x["role"], f"{x['wins']}–{x['losses']}", _wl_tone(x["win_pct"]), x["kda"], x["kda_totals"]]
                                     for x in d.get("roles") or []]})
        p["tables"].append({"title": "Weapons", "columns": ["Weapon", "Type", "Kills", "Kills / match", "Headshot %"],
                            "rows": [[x["weapon"], x["kind"], f"{x['kills']:,}", x["kills_per_match"], f"{x['hs_pct']:g}%" if x.get("hs_pct") is not None else "–"]
                                     for x in (d.get("weapons") or [])[:12]]})
        p["notes"].append(f"Your competitive record across every act, imported from tracker.gg on {when:%b} {when.day}, {when.year}. "
                          "It doesn't update on its own: ask for a re-import when you want newer numbers.")
        return p

    def _rounds_panel(self, st):
        p = {"title": "VALORANT rounds", "tiles": [], "tables": [], "notes": []}
        sides = st["sides"]
        p["tiles"] += [
            {"label": "KAST", "value": pct(st["kast"], 1), "sub": "rounds with a kill, assist, survival or trade"},
            {"label": "First kills", "value": pct(st["fk"], 1), "sub": "of rounds you opened"},
            {"label": "First deaths", "value": pct(st["fd"], 1), "sub": "of rounds you died first"},
            {"label": "Deaths traded", "value": pct(st["traded"], 1), "sub": "avenged within 5 s"},
            {"label": "Attack", "value": pct(sides["attack"]["won"], sides["attack"]["rounds"]),
             "sub": f"{sides['attack']['won']}/{sides['attack']['rounds']} rounds won"},
            {"label": "Defense", "value": pct(sides["defense"]["won"], sides["defense"]["rounds"]),
             "sub": f"{sides['defense']['won']}/{sides['defense']['rounds']} rounds won"},
        ]
        p["death_map"] = {"maps": st["map_points"]}
        p["tables"].append({"title": "By side", "columns": ["Side", "Rounds", "Win %", "Kills / round", "Deaths / round", "First kills", "First deaths"],
                            "rows": [[side.title(), v["rounds"], tone_pct(v["won"], v["rounds"]),
                                      f"{v['kills'] / max(1, v['rounds']):.2f}", f"{v['deaths'] / max(1, v['rounds']):.2f}", v["fk"], v["fd"]]
                                     for side, v in sides.items() if v["rounds"]]})
        p["tables"].append({"title": "By buy (your team vs theirs)", "columns": ["Buy", "Rounds", "Win %", "K/D"],
                            "rows": [[BUY_LABELS[b], v["rounds"], tone_pct(v["won"], v["rounds"]), f"{v['kills'] / max(1, v['deaths']):.2f}"]
                                     for b, v in st["buys"].items() if v["rounds"]]})
        p["tables"].append({"title": "Where you die", "columns": ["Map", "Area", "Deaths", "Died first there"],
                            "rows": [[mp, area, n, fd] for mp, area, n, fd in st["death_areas"]]})
        p["tables"].append({"title": "Kills by weapon", "columns": ["Weapon", "Kills", "Share"],
                            "rows": [[w, n, pct(n, st["total_kills"])] for w, n in st["weapons"]]})
        p["notes"].append(f"From {st['rounds']} rounds across your last {st['matches']} matches on standard maps. "
                          "Buy breakdowns use Competitive, Unrated, Premier and Swiftplay only.")
        return p

    def match_context(self, data):
        return match_context(data)

    def coach_context(self, game):
        rank = db.latest_snapshot(GAME_ID, "val_rank")
        ms = [m["data"] for m in db.matches(GAME_ID, 30)]
        history = []  # all-time numbers first: the recent matches below are only a small sample
        car, life = db.latest_snapshot(GAME_ID, "val_career"), db.latest_snapshot(GAME_ID, "val_trackergg")
        if life:
            history.append(lifetime_text(life))
        if car:
            history.append(career_text(car["data"]))
        lines = []
        if rank:
            lines.append(f"Current rank: {rank['data']['rank']} ({rank['data'].get('rr')} RR)")
        for m in ms:
            if not round_based(m):
                lines.append(f"- {m['queue']} on {m['map']} as {m['agent']}: placed {m['placement']}/{m['players']}, "
                             f"{m['kills']}/{m['deaths']}/{m['assists']} (no rounds, so no ACS/ADR)")
                continue
            lines.append(f"- {m['queue']} on {m['map']} as {m['agent']}: "
                         f"{'W' if m['won'] else 'L' if m['won'] is False else 'D'} {m.get('rounds_won')}-{m.get('rounds_lost')}, "
                         f"{m['kills']}/{m['deaths']}/{m['assists']}, ACS {m['acs']}, ADR {m['adr']}, HS {m.get('hs_pct')}%, "
                         f"first kills {m.get('first_kills')}, placed {m['placement']}/{m['players']}"
                         + (f", RR {m['rr_change']:+d}" if m.get("rr_change") is not None else ""))
        st = round_stats(ms)
        if st:
            sd = st["sides"]
            lines += ["", f"Round analysis ({st['rounds']} rounds, last {st['matches']} matches on standard maps):",
                      f"KAST {pct(st['kast'], 1)}, first-kill rate {pct(st['fk'], 1)}, first-death rate {pct(st['fd'], 1)}, "
                      f"deaths traded {pct(st['traded'], 1)}",
                      "Sides: " + "; ".join(f"{k} {v['won']}/{v['rounds']} won, {v['kills']} kills, {v['deaths']} deaths, "
                                            f"{v['fk']} first kills, {v['fd']} first deaths" for k, v in sd.items() if v["rounds"]),
                      "Buys (your team): " + "; ".join(f"{BUY_LABELS[b]} {v['won']}/{v['rounds']} won, K/D {v['kills']}/{v['deaths']}"
                                                      for b, v in st["buys"].items() if v["rounds"]),
                      "Most common death spots: " + "; ".join(f"{mp} {area} x{n} ({fd} as first death)" for mp, area, n, fd in st["death_areas"][:8]),
                      "Kills by weapon: " + ", ".join(f"{w} {n}" for w, n in st["weapons"])]
            last = next((m for m in ms if standard(m) and m.get("rounds")), None)
            if last:
                lines += ["", f"Most recent round-by-round ({last['map']}, {last['queue']}):"]
                for r in last["rounds"]:
                    kills = ", ".join(f"{k['weapon']} kill on {k['victim']} at {k['area']} {k['t']}s" for k in r["kills"])
                    d = r["death"]
                    lines.append(f"  R{r['n'] + 1} {r['side']} {'W' if r['won'] else 'L'} buy {r['buy']} vs {r['enemy_buy']} "
                                 f"({r.get('weapon') or '-'}, ${r.get('loadout')}): {kills or 'no kills'}; "
                                 + (f"died {d['t']}s to {d['by']} ({d['weapon']}) at {d['area']}" + (", traded" if r["traded"] else "")
                                    if d else "survived")
                                 + (" [first kill]" if r["fk"] else "") + (" [first death]" if r["fd"] else "")
                                 + (f"; abilities: {ability_text(last, r)}" if r.get("abilities") else ""))
                if last.get("abilities"):
                    lines.append("  " + ability_text(last))
        recent = "VALORANT recent matches (newest first):\n" + "\n".join(lines) if lines else ""
        return "\n\n".join(x for x in history + [recent] if x)


def match_context(m):
    """Detailed text of one match for the coach."""
    res = ("won" if m["won"] else "lost" if m["won"] is False else "drew") if round_based(m) else f"placed {m['placement']}/{m['players']}"
    lines = [f"Match focus: {m['queue']} on {m['map']} as {m['agent']}, {res}"
             + (f" {m['rounds_won']}-{m['rounds_lost']}" if m.get("rounds_won") is not None else "")
             + f"; {m['kills']}/{m['deaths']}/{m['assists']}"
             + (f", ACS {m['acs']}, ADR {m['adr']}" if m.get("acs") is not None else "")
             + (f", HS {m['hs_pct']}%" if m.get("hs_pct") is not None else "")
             + (f", RR {m['rr_change']:+d}" if m.get("rr_change") is not None else "")]
    if m.get("abilities"):
        lines.append(ability_text(m))
    if m.get("scoreboard"):
        lines.append("Scoreboard (sorted by combat score):")
        for p in m["scoreboard"]:
            lines.append(f"  {'YOU ' if p['me'] else ''}{p['team']}{' (your party)' if p['party'] else ''} {p['agent']}"
                         f"{' ' + p['rank'] if p.get('rank') else ''}: {p['kills']}/{p['deaths']}/{p['assists']}"
                         + (f", ACS {p['acs']}, ADR {p['adr']}" if p.get("acs") is not None else "")
                         + (f", HS {p['hs_pct']}%" if p.get("hs_pct") is not None else "") + f", first kills {p['fk']}")
    for r in m.get("rounds") or []:
        kills = ", ".join(f"{k['weapon']} on {k['victim']} {k['t']}s from {k.get('area') or '?'}" for k in r["kills"])
        d = r["death"]
        econ = (f"team avg ${r['team_loadout']} vs enemy ${r['enemy_loadout']}" if r.get("team_loadout") is not None
                else f"buy {r['buy']} vs {r['enemy_buy']}")
        lines.append(f"R{r['n'] + 1} {r['side']} {'W' if r['won'] else 'L'} ({r.get('end')}), {econ}, you had "
                     f"{r.get('weapon') or 'no gun'} ${r.get('loadout')}: {kills or 'no kills'}; "
                     + (f"died {d['t']}s to {d['by']} ({d['weapon']}) at {d.get('area') or '?'}" + (", traded" if r["traded"] else "")
                        if d else "survived")
                     + (" [first kill]" if r["fk"] else "") + (" [first death]" if r["fd"] else "")
                     + (f"; spike planted {r['plant']['site']} at {r['plant']['t']}s" if r.get("plant") else "")
                     + (f"; abilities: {ability_text(m, r)}" if r.get("abilities") else ""))
        if r.get("feed"):
            lines.append("    feed: " + "; ".join(f"{f['t']}s {f['killer']}({f['ks']}) > {f['victim']}({f['vs']}) {f['weapon']}"
                                                  for f in r["feed"]))
    return "\n".join(lines)


BUY_LABELS = {"pistol": "Pistol", "eco": "Eco", "half": "Half buy", "full": "Full buy",
              "anti": "Full vs their eco/half", "upset": "Eco/half vs their full"}


# Rank ticks for the career chart: the first division of each rank (today's numbering).
TIER_TICKS = {3: "Iron 1", 6: "Bronze 1", 9: "Silver 1", 12: "Gold 1", 15: "Platinum 1", 18: "Diamond 1", 21: "Ascendant 1",
              24: "Immortal 1", 27: "Radiant"}


def pct(a, b):
    return f"{round(100 * a / b)}%" if b else "–"


def tone_pct(a, b):
    return {"text": pct(a, b), "tone": None if not b else "good" if a * 2 > b else "bad" if a * 2 < b else None}


def round_chip(r):
    """One square in a match's round strip."""
    d = r.get("death")
    tip = (f"Round {r['n'] + 1}, {r.get('side') or '?'}, {BUY_LABELS.get(r.get('buy'), r.get('buy') or '')}: "
           f"{'won' if r['won'] else 'lost'}, {len(r['kills'])} kill{'s' if len(r['kills']) != 1 else ''}"
           + (f", died to {d['by']} at {d.get('area') or '?'}" if d else ", survived"))
    return {"w": r["won"], "s": (r.get("side") or "?")[0], "t": tip}


def round_stats(ms, limit=20):
    """Aggregate per-round data from the last `limit` round-based matches that have it."""
    games = [m for m in ms if standard(m) and m.get("rounds")][:limit]
    rows = [(m, r) for m in games for r in m["rounds"]]
    if not rows:
        return None
    n = len(rows)
    died = [r for _, r in rows if r["death"]]

    def summary(rs):
        return {"rounds": len(rs), "won": sum(r["won"] for r in rs), "kills": sum(len(r["kills"]) for r in rs),
                "deaths": sum(1 for r in rs if r["death"]), "fk": sum(r["fk"] for r in rs), "fd": sum(r["fd"] for r in rs)}

    econ = [r for m, r in rows if m["queue"] in ECON_QUEUES]
    buys = {b: summary([r for r in econ if r["buy"] == b]) for b in ("pistol", "eco", "half", "full")}
    buys["anti"] = summary([r for r in econ if r["buy"] == "full" and r["enemy_buy"] in ("eco", "half")])
    buys["upset"] = summary([r for r in econ if r["buy"] in ("eco", "half") and r["enemy_buy"] == "full"])

    areas, weapons, maps = Counter(), Counter(), {}
    for m, r in rows:
        mp = maps.setdefault(m["map"], {"name": m["map"], "icon": m.get("map_icon"), "points": []})
        when = m["start"]
        for k in r["kills"]:
            weapons[k["weapon"]] += 1
            if k.get("x") is not None:
                mp["points"].append({"x": k["x"], "y": k["y"], "kind": "kill", "side": r["side"], "ts": when,
                                     "label": f"Round {r['n'] + 1} ({r['side']}): {k['weapon']} kill on {k['victim']} at "
                                              f"{k['t']}s, from {k.get('area') or '?'}"})
        d = r["death"]
        if d:
            areas[(m["map"], d.get("area") or "?", r["fd"])] += 1
            if d.get("x") is not None:
                mp["points"].append({"x": d["x"], "y": d["y"], "kind": "death", "side": r["side"], "ts": when, "first": r["fd"],
                                     "label": f"Round {r['n'] + 1} ({r['side']}): died to {d['by']} ({d['weapon']}) at "
                                              f"{d['t']}s, {d.get('area') or '?'}" + (", first death" if r["fd"] else "")
                                              + (", traded" if r["traded"] else "")})
    death_areas = Counter()
    first_at = Counter()
    for (mp, area, fd), c in areas.items():
        death_areas[(mp, area)] += c
        if fd:
            first_at[(mp, area)] += c
    return {
        "matches": len(games), "rounds": n,
        "kast": sum(r["kast"] for _, r in rows) / n, "fk": sum(r["fk"] for _, r in rows) / n,
        "fd": sum(r["fd"] for _, r in rows) / n, "traded": sum(r["traded"] for r in died) / len(died) if died else 0,
        "sides": {side: summary([r for _, r in rows if r["side"] == side]) for side in ("attack", "defense")},
        "buys": buys,
        "death_areas": [(mp, area, c, first_at[(mp, area)]) for (mp, area), c in death_areas.most_common(12)],
        "weapons": weapons.most_common(10), "total_kills": sum(weapons.values()),
        "map_points": sorted((v for v in maps.values() if v["icon"] and v["points"]), key=lambda v: -len(v["points"])),
    }
