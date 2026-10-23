"""Turn a CS2 demo (.dem) into a match record with positions, using demoparser2 (PC agent side).

Positions are projected onto the map's radar image (0..1 coordinates) with the overview data
published by awpy-data (pos_x, pos_y, scale per map; same numbers as Valve's overview files).
"""
import hashlib
import json
import math
import os
import re
import time

from . import net

AWPY_RELEASES = "https://api.github.com/repos/pnxenopoulos/awpy-data/releases/latest"
TICKRATE = 64
NADES = {"smokegrenade_detonate": "smoke", "flashbang_detonate": "flash", "hegrenade_detonate": "he",
         "inferno_startburn": "molotov"}
# round_end "reason" is Valve's numeric RoundEndReason; older demos used text codes.
ENDS = {1: "bomb exploded", 7: "bomb defused", 8: "eliminated", 9: "eliminated", 10: "draw", 12: "time ran out",
        17: "surrender", 18: "surrender",
        "bomb_exploded": "bomb exploded", "target_bombed": "bomb exploded", "bomb_defused": "bomb defused",
        "ct_killed": "eliminated", "t_killed": "eliminated", "target_saved": "time ran out",
        "t_surrender": "surrender", "ct_surrender": "surrender"}


WEAPONS = {
    "ak47": "AK-47", "m4a1": "M4A4", "m4a1_silencer": "M4A1-S", "m4a1_silencer_off": "M4A1-S", "awp": "AWP",
    "galilar": "Galil AR", "famas": "FAMAS", "sg556": "SG 553", "aug": "AUG", "ssg08": "SSG 08", "scar20": "SCAR-20",
    "g3sg1": "G3SG1", "glock": "Glock-18", "hkp2000": "P2000", "usp_silencer": "USP-S", "usp_silencer_off": "USP-S",
    "p250": "P250", "deagle": "Desert Eagle", "fiveseven": "Five-SeveN", "tec9": "Tec-9", "cz75a": "CZ75-Auto",
    "elite": "Dual Berettas", "revolver": "R8 Revolver", "mac10": "MAC-10", "mp9": "MP9", "mp7": "MP7", "mp5sd": "MP5-SD",
    "ump45": "UMP-45", "p90": "P90", "bizon": "PP-Bizon", "nova": "Nova", "xm1014": "XM1014", "mag7": "MAG-7",
    "sawedoff": "Sawed-Off", "negev": "Negev", "m249": "M249", "taser": "Zeus x27", "hegrenade": "HE grenade",
    "inferno": "Molotov", "molotov": "Molotov", "incgrenade": "Incendiary", "flashbang": "Flashbang",
    "smokegrenade": "Smoke", "decoy": "Decoy", "knife": "Knife", "world": "World", "planted_c4": "Bomb",
}


def weapon_name(w):
    """ak47 -> AK-47, weapon_usp_silencer -> USP-S; anything unknown is tidied up rather than dropped."""
    if not w:
        return None
    w = str(w).lower().removeprefix("weapon_")
    if w.startswith(("knife", "bayonet")):
        return "Knife"
    return WEAPONS.get(w) or (w.upper() if len(w) <= 4 else w.replace("_", " ").title())


def _place(name):
    """Valve's callout ids read as words: BombsiteA -> Bombsite A, CTSpawn -> CT Spawn."""
    if not name:
        return None
    name = re.sub(r"(?<=[a-z]{2})of(?=[A-Z])", " of", str(name))  # TopofMid
    return re.sub(r"(?<=[a-z])(?=[A-Z0-9])|(?<=[A-Z])(?=[A-Z][a-z])", " ", name)


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _end_text(end):
    reason = end.get("reason")
    try:
        reason = int(reason)
    except (TypeError, ValueError):
        reason = str(reason or "").lower()
    return ENDS.get(reason) or (str(end.get("message") or "").replace("#SFUI_Notice_", "").replace("_", " ").lower() or None)


def map_data():
    tag = net.cached_json("awpy_latest", AWPY_RELEASES, ttl=7 * 86400)["tag_name"]
    return net.cached_json("awpy_map_data",
                           f"https://github.com/pnxenopoulos/awpy-data/releases/download/{tag}/map_data.json", ttl=7 * 86400)


def demo_id(path):
    st = os.stat(path)
    return "cs2demo:" + hashlib.sha1(f"{os.path.basename(path)}|{st.st_size}".encode()).hexdigest()[:16]


def _info_matchtime(path):
    """Match start time from the .dem.info file CS2 writes next to downloaded demos (protobuf field 2)."""
    try:
        with open(path + ".info", "rb") as f:
            buf = f.read(64)
    except OSError:
        return None
    pos = 0
    while pos < len(buf):
        key, pos = _varint(buf, pos)
        field, wire = key >> 3, key & 7
        if wire == 0:
            val, pos = _varint(buf, pos)
            if field == 2 and 1_500_000_000 < val < 4_000_000_000:
                return val
        elif wire == 2:
            ln, pos = _varint(buf, pos)
            pos += ln
        elif wire == 1:
            pos += 8
        elif wire == 5:
            pos += 4
        else:
            return None
    return None


def _varint(buf, pos):
    shift = result = 0
    while pos < len(buf):
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7
    return result, pos


def _records(df):
    if df is None:
        return []
    try:
        return df.to_dict("records")
    except AttributeError:
        return list(df)


def _side(team_num):
    try:
        return {2: "t", 3: "ct"}.get(int(team_num))
    except (TypeError, ValueError):
        return None


def _sid(v):
    return str(v) if v not in (None, "", 0) else None


def _buy(v):
    if v is None:
        return None
    return "eco" if v < 1500 else "half" if v < 3700 else "full"


class NotInDemo(ValueError):
    pass


def parse(path, my_steamid, maps=None):
    from demoparser2 import DemoParser  # agent-only dependency

    me = str(my_steamid)
    p = DemoParser(path)
    header = p.parse_header() or {}
    mraw = header.get("map_name") or "?"
    maps = maps if maps is not None else map_data()
    md = maps.get(mraw) or {}
    lower_max = None
    try:
        lower_max = float(((md.get("verticalsections") or {}).get("lower") or {}).get("AltitudeMax"))
    except (TypeError, ValueError):
        pass

    def spot(x, y, z=None, place=None):
        if x is None or y is None or not md.get("scale"):
            return {"x": None, "y": None, "area": _place(place)}
        s = md["scale"] * 1024
        out = {"x": round((x - md["pos_x"]) / s, 4), "y": round((md["pos_y"] - y) / s, 4), "area": _place(place)}
        if lower_max is not None and z is not None and z < lower_max:
            out["lower"] = True
        return out

    def victim_at(k):
        v = spot(k.get("user_X"), k.get("user_Y"))
        return {"vx": v["x"], "vy": v["y"]}

    names = ["round_freeze_end", "round_end", "player_death", "player_hurt", "bomb_planted", *NADES]
    evs = dict(p.parse_events(names, player=["X", "Y", "Z", "last_place_name", "team_num"],
                              other=["total_rounds_played", "is_warmup_period"]))
    ev = {n: [e for e in _records(evs.get(n)) if not e.get("is_warmup_period")] for n in names}

    ends = sorted(ev["round_end"], key=lambda e: e["tick"])
    starts = sorted(e["tick"] for e in ev["round_freeze_end"])
    if not ends:
        raise ValueError("no rounds in this demo")
    windows, prev = [], 0
    for e in ends:
        st = max([s for s in starts if prev <= s < e["tick"]], default=prev)
        windows.append((st, e["tick"], e))
        prev = e["tick"]

    props = ["team_num", "current_equip_value", "balance", "kills_total", "deaths_total", "assists_total",
             "headshot_kills_total", "mvps", "score", "name"]
    snap_ticks = sorted({w[0] for w in windows} | {ends[-1]["tick"]})
    ticks = _records(p.parse_ticks(props, ticks=snap_ticks))
    at = {}
    for r in ticks:
        at.setdefault(r["tick"], {})[_sid(r.get("steamid"))] = r
    if not any(me in rows for rows in at.values()):
        raise NotInDemo("your Steam account isn't in this demo")

    def team_at(tick, sid):
        row = (at.get(tick) or {}).get(sid)
        return _side(row.get("team_num")) if row else None

    rounds, my_damage = [], 0
    dmg_by = {}
    for e in ev["player_hurt"]:
        a = _sid(e.get("attacker_steamid"))
        if a and a != _sid(e.get("user_steamid")):
            dmg_by[a] = dmg_by.get(a, 0) + min(100, e.get("dmg_health") or 0)
    fk_count = {}
    my_deaths = {}  # round index -> death event, for the improvement facts below
    # First round of each half is a pistol round: MR12 in Premier/Competitive, MR8 in Wingman (2v2).
    half = 8 if len(at.get(ends[-1]["tick"]) or {}) <= 4 else 12
    for i, (st, en, end) in enumerate(windows):
        my_side = team_at(st, me) or team_at(ends[-1]["tick"], me)
        inside = lambda e: st <= e["tick"] <= en
        kills = sorted((k for k in ev["player_death"] if inside(k)), key=lambda k: k["tick"])
        sec = lambda tick: round((tick - st) / TICKRATE, 1)
        team_of = lambda sid: team_at(st, sid)
        rel = lambda sid: "me" if sid == me else ("ally" if team_of(sid) == my_side else "enemy")
        if kills:
            first = _sid(kills[0].get("attacker_steamid"))
            fk_count[first] = fk_count.get(first, 0) + 1
        mine = [k for k in kills if _sid(k.get("attacker_steamid")) == me and _sid(k.get("user_steamid")) != me]
        death = next((k for k in kills if _sid(k.get("user_steamid")) == me), None)
        traded = bool(death) and any(
            _sid(k.get("user_steamid")) == _sid(death.get("attacker_steamid")) and rel(_sid(k.get("attacker_steamid"))) == "ally"
            and 0 <= k["tick"] - death["tick"] <= 5 * TICKRATE for k in kills)
        if death:
            my_deaths[i] = death
        assists = sum(1 for k in kills if _sid(k.get("assister_steamid")) == me)
        rdmg = sum(min(100, h.get("dmg_health") or 0) for h in ev["player_hurt"]
                   if inside(h) and _sid(h.get("attacker_steamid")) == me and _sid(h.get("user_steamid")) != me)
        my_damage += rdmg
        econ = at.get(st) or {}
        team_vals = [r.get("current_equip_value") or 0 for sid, r in econ.items() if _side(r.get("team_num")) == my_side]
        enemy_vals = [r.get("current_equip_value") or 0 for sid, r in econ.items()
                      if _side(r.get("team_num")) not in (None, my_side)]
        mine_row = econ.get(me) or {}
        winner = str(end.get("winner", "")).lower()
        win_side = "ct" if winner in ("3", "ct", "cts") else "t" if winner in ("2", "t", "terrorist", "terrorists") else None
        plant = next((b for b in ev["bomb_planted"] if inside(b)), None)
        rec = {
            "n": i, "side": my_side, "won": bool(my_side) and win_side == my_side,
            "end": _end_text(end),
            "loadout": _int(mine_row.get("current_equip_value")), "money": _int(mine_row.get("balance")),
            "team_loadout": round(sum(team_vals) / len(team_vals)) if team_vals else None,
            "enemy_loadout": round(sum(enemy_vals) / len(enemy_vals)) if enemy_vals else None,
            "buy": "pistol" if i in (0, half) else _buy(sum(team_vals) / len(team_vals) if team_vals else None),
            "enemy_buy": "pistol" if i in (0, half) else _buy(sum(enemy_vals) / len(enemy_vals) if enemy_vals else None),
            "weapon": None,
            "kills": [{"t": sec(k["tick"]), "weapon": weapon_name(k.get("weapon")), "victim": k.get("user_name"), "hs": bool(k.get("headshot")),
                       **victim_at(k), **spot(k.get("attacker_X"), k.get("attacker_Y"), k.get("attacker_Z"), k.get("attacker_last_place_name"))}
                      for k in mine],
            "death": _death_rec(death, sec, spot) if death else None,
            "assists": assists, "traded": traded, "damage": rdmg, "survived": death is None,
            "fk": bool(kills) and _sid(kills[0].get("attacker_steamid")) == me,
            "fd": bool(kills) and _sid(kills[0].get("user_steamid")) == me,
            "plant": ({"site": (plant.get("user_last_place_name") or "").replace("Bombsite", "") or str(plant.get("site")),
                       "t": sec(plant["tick"]), **spot(plant.get("user_X"), plant.get("user_Y"), plant.get("user_Z"))} if plant else None),
            "utility": [{"type": NADES[n], "t": sec(g["tick"]), **spot(g.get("x"), g.get("y"), g.get("z"))}
                        for n in NADES for g in ev[n] if inside(g) and _sid(g.get("user_steamid")) == me],
            "feed": [{"t": sec(k["tick"]), "killer": k.get("attacker_name") or "world", "ks": rel(_sid(k.get("attacker_steamid"))),
                      "victim": k.get("user_name"), "vs": rel(_sid(k.get("user_steamid"))), "weapon": weapon_name(k.get("weapon")),
                      "hs": bool(k.get("headshot"))} for k in kills],
        }
        rec["kast"] = bool(rec["kills"]) or assists > 0 or death is None or traded
        rounds.append(rec)

    final = at.get(ends[-1]["tick"]) or {}
    my_final_side = _side((final.get(me) or {}).get("team_num"))
    won_rounds = sum(r["won"] for r in rounds)
    lost_rounds = len(rounds) - won_rounds
    hs = sum(1 for r in rounds for k in r["kills"] if k.get("hs"))
    nkills = sum(len(r["kills"]) for r in rounds)
    scoreboard = []
    for sid, r in final.items():
        if not sid:
            continue
        k = r.get("kills_total") or 0
        scoreboard.append({
            "name": r.get("name") or "?", "me": sid == me,
            "team": "ally" if _side(r.get("team_num")) == my_final_side else "enemy", "party": False,
            "agent": None, "agent_icon": None, "rank": None, "rank_icon": None,
            "kills": k, "deaths": r.get("deaths_total") or 0, "assists": r.get("assists_total") or 0,
            "score": r.get("score") or 0, "mvps": r.get("mvps") or 0, "acs": None,
            "adr": round(dmg_by.get(sid, 0) / max(1, len(rounds))),
            "hs_pct": round(100 * (r.get("headshot_kills_total") or 0) / k, 1) if k else None,
            "fk": fk_count.get(sid, 0),
        })
    scoreboard.sort(key=lambda x: (-x["kills"], x["deaths"]))
    me_row = next((s for s in scoreboard if s["me"]), {})
    info_time = _info_matchtime(path)
    shots = _improvement_facts(p, me, rounds, my_deaths, (windows[0][0], ends[-1]["tick"]))
    return {
        "v": 1, "source": "demo", "demo": os.path.basename(path), "shots": shots,
        "start": info_time or os.path.getmtime(path), "start_exact": bool(info_time),
        "length_sec": round((ends[-1]["tick"] - windows[0][0]) / TICKRATE),
        "map": _pretty(mraw), "map_raw": mraw,
        "map_icon": f"/radar/{mraw}.png" if md else None,
        "map_icon_lower": f"/radar/{mraw}_lower.png" if lower_max is not None else None,
        "queue": "Wingman" if len(final) <= 5 else "Competitive / Premier",
        "rounds_won": won_rounds, "rounds_lost": lost_rounds,
        "won": None if won_rounds == lost_rounds else won_rounds > lost_rounds,
        "kills": me_row.get("kills", nkills), "deaths": me_row.get("deaths", sum(not r["survived"] for r in rounds)),
        "assists": me_row.get("assists", 0), "mvps": me_row.get("mvps", 0), "score": me_row.get("score"),
        "adr": round(my_damage / max(1, len(rounds))), "hs_pct": round(100 * hs / nkills, 1) if nkills else None,
        "rounds": rounds, "scoreboard": scoreboard,
    }


GUNLESS = ("knife", "bayonet", "grenade", "flashbang", "molotov", "incgrenade", "decoy", "c4", "taser")
MOVING_SPEED = 100  # units/s: well past where rifles and pistols lose accuracy


def _improvement_facts(p, me, rounds, my_deaths, span):
    """Adds what was around you when you died to each death, and returns how you shot over the match."""
    try:
        fires = [f for f in _records(p.parse_event("weapon_fire", player=["velocity_X", "velocity_Y"]))
                 if _sid(f.get("user_steamid")) == me and span[0] <= f["tick"] <= span[1]
                 and not any(g in str(f.get("weapon") or "") for g in GUNLESS)]
    except Exception:
        fires = []
    fires.sort(key=lambda f: f["tick"])
    speed = lambda f: math.hypot(f.get("user_velocity_X") or 0, f.get("user_velocity_Y") or 0)
    ticks = sorted({d["tick"] for d in my_deaths.values()})
    pos = {}
    if ticks:
        for r in _records(p.parse_ticks(["X", "Y", "yaw", "team_num", "is_alive"], ticks=ticks)):
            pos.setdefault(r["tick"], {})[_sid(r.get("steamid"))] = r
    for i, ev in my_deaths.items():
        rec = rounds[i]
        here = pos.get(ev["tick"]) or {}
        me_row = here.get(me) or {}
        team = me_row.get("team_num")
        mx, my_ = ev.get("user_X"), ev.get("user_Y")
        alive = [r for s, r in here.items() if s != me and r.get("is_alive") and r.get("X") is not None]
        allies = [r for r in alive if r.get("team_num") == team]
        enemies = [r for r in alive if r.get("team_num") not in (None, team)]
        facts = {"allies_alive": len(allies), "enemies_alive": len(enemies), "ally_m": None}
        if mx is not None and allies:
            facts["ally_m"] = round(min(math.hypot(r["X"] - mx, r["Y"] - my_) for r in allies) * 0.0254, 1)
        kx, ky = ev.get("attacker_X"), ev.get("attacker_Y")
        if mx is not None and kx is not None and _sid(ev.get("attacker_steamid")) not in (None, me):
            facts["killer_m"] = round(math.hypot(kx - mx, ky - my_) * 0.0254, 1)
            if me_row.get("yaw") is not None:
                to_killer = math.degrees(math.atan2(ky - my_, kx - mx))
                facts["facing"] = round(abs((me_row["yaw"] - to_killer + 180) % 360 - 180))
        recent = [f for f in fires if ev["tick"] - 2 * TICKRATE <= f["tick"] <= ev["tick"]]
        facts["shot_2s"] = bool(recent)
        if recent:
            facts["moving_last_shot"] = speed(recent[-1]) > MOVING_SPEED
        kill_ts = [k["t"] for k in rec["kills"] if k["t"] <= rec["death"]["t"]]
        if kill_ts:
            facts["after_kill_s"] = round(rec["death"]["t"] - max(kill_ts), 1)
        rec["death"]["facts"] = facts
    bursts, cur = [], []
    for f in fires:
        if cur and f["tick"] - cur[-1]["tick"] > 0.35 * TICKRATE:
            bursts.append(cur)
            cur = []
        cur.append(f)
    if cur:
        bursts.append(cur)
    # only deciding fights: the opening burst in the 3 s before each kill or death of yours
    try:
        fights = sorted(e["tick"] for e in _records(p.parse_event("player_death"))
                        if me in (_sid(e.get("attacker_steamid")), _sid(e.get("user_steamid"))) and span[0] <= e["tick"] <= span[1])
    except Exception:
        fights = []
    opening, used = [], set()
    for b in bursts:
        i = next((j for j, t in enumerate(fights) if t >= b[0]["tick"]), None)
        if i is not None and fights[i] - b[0]["tick"] <= 3 * TICKRATE and fights[i] not in used:
            used.add(fights[i])
            opening.append(b)
    bursts = opening
    if not bursts:
        return None
    return {"engagements": len(bursts), "moving_first": sum(1 for b in bursts if speed(b[0]) > MOVING_SPEED),
            "bursts": len(bursts), "long_bursts": sum(1 for b in bursts if len(b) > 8), "source": "demo"}


def _death_rec(death, sec, spot):
    """Where you died, who killed you with what, and where they were standing."""
    killer = spot(death.get("attacker_X"), death.get("attacker_Y"))
    return {"t": sec(death["tick"]), "by": death.get("attacker_name") or "world", "weapon": weapon_name(death.get("weapon")),
            "hs": bool(death.get("headshot")),
            **spot(death.get("user_X"), death.get("user_Y"), death.get("user_Z"), death.get("user_last_place_name")),
            "kx": killer["x"], "ky": killer["y"]}


def _pretty(raw):
    raw = (raw or "?").split("/")[-1]
    for prefix in ("de_", "cs_", "ar_"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
    return raw.replace("_", " ").title()


# ---------------- watching for new demos ----------------

def demo_dirs():
    from .integrations.cs2 import _cfg_dir
    cfg = _cfg_dir()
    if not cfg:
        return []
    csgo = os.path.dirname(cfg)
    return [d for d in (os.path.join(csgo, "replays"), csgo) if os.path.isdir(d)]


def pending(state_path, limit=3):
    """Demos not processed yet (oldest first)."""
    try:
        with open(state_path, encoding="utf-8") as f:
            done = set(json.load(f))
    except (OSError, ValueError):
        done = set()
    found = []
    for d in demo_dirs():
        for name in os.listdir(d):
            path = os.path.join(d, name)
            if name.endswith(".dem") and os.path.isfile(path) and time.time() - os.path.getmtime(path) > 30:
                if demo_id(path) not in done:
                    found.append(path)
    return sorted(found, key=os.path.getmtime)[:limit], done


def mark_done(state_path, done, ids):
    done |= set(ids)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(sorted(done), f)
