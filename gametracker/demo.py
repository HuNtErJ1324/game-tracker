"""Demo mode: made-up games, play sessions and matches in a throwaway data folder, with the dashboard on top.

    python -m gametracker demo

The data goes to a new temp folder (deleted when the demo stops), or to GAMETRACKER_DATA when that is set and is
empty or an earlier demo's folder; any other folder is refused, so a real library is never touched. Only the server
runs: no agent, no scan of this PC, and the server fetches nothing from the internet (the browser still loads cover
art and agent icons from Steam's and valorant-api.com's public image servers). Every playtime, match, player name,
review and coach answer is invented. API keys in the environment are ignored, so the coach says it isn't set up.

Everything is written through db.py (and improve.py's tables), so the demo stays in step with the real schema.
"""
import json
import math
import os
import random
import shutil
import statistics
import sys
import tempfile
import time
import uuid

PORT = 8788  # not 8787, so the demo can run next to a real tracker
MARKER = ".gametracker-demo"
R = random.Random(37)  # the same demo every time; only the dates move with today
ME = "DemoPlayer#0001"
PARTNER = "Tidepool#3381"  # duo partner in some matches
NAMES = ["Marmalade#EUW1", "quietcarrot#777", "Lanternfish#2210", "FigJam#NA1", "Corduroy#0042", "Bramble#5150",
         "Pocket Comet#1999", "Oatcake#TEA", "Saltwater#8080", "Gravel#3312", "Wren#4411", "Juniper#606", "Halftone#7071",
         "Snowpea#1203", "Cobalt Fern#9090", "Driftwood#2525", "Pinecone#3141", "Larkspur#1717", "Inkwell#4040",
         "Toffee#8888", "Kettle#1010", "MossyStep#2468", "papercrane#1357", "Sundial#1200", "Nimbus#6060", "Okra#3030"]

STEAM_ART = "https://cdn.akamai.steamstatic.com/steam/apps/{}/"
STEAM = r"C:\Program Files (x86)\Steam\steamapps\common"
# id, source, name, install folder, Steam's own playtime before tracking (hours), Steam's last played (days ago)
LIBRARY = [
    ("riot:valorant", "riot", "VALORANT", r"C:\Riot Games\VALORANT\live", 0, None),
    ("riot:league_of_legends", "riot", "League of Legends", r"C:\Riot Games\League of Legends", 0, None),
    ("steam:730", "steam", "Counter-Strike 2", STEAM + r"\Counter-Strike Global Offensive", 642, None),
    ("steam:1245620", "steam", "ELDEN RING", STEAM + r"\ELDEN RING", 71, None),
    ("steam:1145350", "steam", "Hades II", STEAM + r"\Hades II", 9, None),
    ("steam:1086940", "steam", "Baldur's Gate 3", STEAM + r"\Baldurs Gate 3", 118, None),
    ("steam:413150", "steam", "Stardew Valley", STEAM + r"\Stardew Valley", 204, None),
    ("steam:2379780", "steam", "Balatro", STEAM + r"\Balatro", 37, None),
    ("steam:367520", "steam", "Hollow Knight", STEAM + r"\Hollow Knight", 46, 160),
    ("steam:1091500", "steam", "Cyberpunk 2077", STEAM + r"\Cyberpunk 2077", 88, 240),
    ("steam:646570", "steam", "Slay the Spire", STEAM + r"\SlayTheSpire", 131, 75),
    ("steam:892970", "steam", "Valheim", STEAM + r"\Valheim", 64, 310),
    ("steam:431960", "steam", "Wallpaper Engine", STEAM + r"\wallpaper_engine", 1900, 1),  # a tool: hidden
    ("epic:Sugar", "epic", "Rocket League", r"C:\Program Files\Epic Games\rocketleague", 0, None),
    ("battlenet:Diablo IV", "battlenet", "Diablo IV", r"C:\Program Files (x86)\Diablo IV", 0, None),
]
# What the made-up player has been into lately: game id -> (played from, to days ago, sessions a week, usual minutes)
ROTATION = {
    "riot:valorant": (1, 34, 3.4, 150), "steam:1245620": (2, 34, 1.6, 115), "steam:1145350": (1, 12, 3.0, 55),
    "steam:1086940": (15, 32, 1.1, 190), "steam:413150": (3, 26, 0.9, 85), "steam:2379780": (1, 34, 1.4, 35),
    "steam:730": (2, 22, 1.4, 95), "riot:league_of_legends": (4, 30, 0.8, 100), "epic:Sugar": (6, 18, 0.6, 45),
    "battlenet:Diablo IV": (20, 33, 0.7, 120),
}

# ---------------- VALORANT reference data (valorant-api.com ids, so icons load like the real tracker's) ----------------

VAL, CS2, LOL, ER = "riot:valorant", "steam:730", "riot:league_of_legends", "steam:1245620"
VAL_MEDIA = "https://media.valorant-api.com"
TIER_SET = "03621f52-342b-cf4e-4f86-9350a49c6d04"
TIERS = {3 + 3 * i + j: f"{d} {j + 1}" for i, d in enumerate(("Iron", "Bronze", "Silver", "Gold", "Platinum", "Diamond",
                                                               "Ascendant", "Immortal")) for j in range(3)} | {27: "Radiant"}
AGENTS = {
    "Sova": "320b2a48-4d9b-a075-30f1-1f93a9b638fa", "Omen": "8e253930-4c05-31dd-1b6c-968525494517",
    "KAY/O": "601dbbe7-43ce-be57-2a40-4abd24953621", "Killjoy": "1e58de9c-4950-5125-93e9-a0aee9f98746",
    "Jett": "add6443a-41bd-e414-f6ad-e58d267f4e95", "Reyna": "a3bfb853-43b2-7238-a4f1-ad90e9e46bcc",
    "Raze": "f94c3b30-42be-e959-889c-5aa313dba261", "Sage": "569fdd95-4d10-43ab-ca70-79becc718b46",
    "Cypher": "117ed9e3-49f3-6512-3ccf-0cada7e3823b", "Brimstone": "9f0d8ba9-4140-b941-57d3-a7ad57c6b417",
    "Viper": "707eab51-4836-f488-046a-cda6bf494859", "Skye": "6f2a04ca-43e0-be17-7f36-b3908627744d",
    "Fade": "dade69b4-4f5a-8528-247b-219e5a1facd6", "Breach": "5f8d3a7f-467b-97f3-062c-13acf203c006",
    "Phoenix": "eb93336a-449b-9c1b-0a54-a891f7921d69", "Gekko": "e370fa57-4757-3604-3648-499e1f642d3f",
    "Clove": "1dbf2edd-4729-0984-3115-daa5eed44993", "Neon": "bb2a4828-46eb-8cd1-e765-15848195d751",
    "Chamber": "22697a3d-45bf-8dd7-4fec-84a9e28c69d7", "Astra": "41fb69c1-4189-7b37-f117-bcaf1e96f1bf",
    "Harbor": "95b78ed7-4637-86d9-7e41-71ba8c293152", "Iso": "0e38b510-41a8-5780-5e8f-568b2a4f2d6c",
}
MAINS = {  # the demo player's agents: how often picked, their C / Q / E / X abilities, and what they set up in buy phase
    "Sova": (45, {"c": "Owl Drone", "q": "Shock Bolt", "e": "Recon Bolt", "x": "Hunter's Fury"}, ""),
    "Omen": (25, {"c": "Shrouded Step", "q": "Paranoia", "e": "Dark Cover", "x": "From the Shadows"}, ""),
    "KAY/O": (18, {"c": "FRAG/ment", "q": "FLASH/drive", "e": "ZERO/point", "x": "NULL/cmd"}, ""),
    "Killjoy": (12, {"c": "Nanoswarm", "q": "ALARMBOT", "e": "TURRET", "x": "Lockdown"}, "cqe"),
}
# Callouts projected onto each minimap with valorant-api's transform, so the death maps put marks in real places.
MAPS = {
    "Ascent": ("7eaecc1b-4337-bbf6-6ab9-04b8f06b3319", {
        "A Site": (0.35, 0.142), "A Main": (0.484, 0.201), "A Tree": (0.398, 0.295), "A Garden": (0.285, 0.309),
        "A Lobby": (0.603, 0.259), "Mid Courtyard": (0.493, 0.488), "Mid Market": (0.298, 0.497), "Mid Catwalk": (0.525, 0.411),
        "Mid Bottom": (0.397, 0.495), "Mid Link": (0.514, 0.617), "B Site": (0.285, 0.737), "B Main": (0.405, 0.712),
        "B Lobby": (0.717, 0.678), "B Boat House": (0.27, 0.887)}),
    "Bind": ("2c9d57ec-4431-9c5e-2939-8f9ef6dd5cba", {
        "A Site": (0.734, 0.333), "A Short": (0.624, 0.497), "A Bath": (0.839, 0.43), "A Lamps": (0.582, 0.339),
        "A Lobby": (0.763, 0.607), "A Tower": (0.728, 0.208), "B Site": (0.292, 0.312), "B Long": (0.193, 0.515),
        "B Window": (0.323, 0.447), "B Garden": (0.247, 0.428), "B Hall": (0.285, 0.202), "B Short": (0.397, 0.53),
        "B Elbow": (0.158, 0.306)}),
    "Haven": ("2bee0dc9-4ffe-519b-1cbd-7fbe763a6047", {
        "A Site": (0.402, 0.17), "A Long": (0.576, 0.177), "A Sewer": (0.5, 0.384), "A Lobby": (0.624, 0.385),
        "A Link": (0.29, 0.324), "B Site": (0.401, 0.501), "Mid Courtyard": (0.59, 0.506), "Mid Window": (0.676, 0.496),
        "C Site": (0.418, 0.821), "C Long": (0.644, 0.894), "C Garage": (0.493, 0.629), "C Link": (0.343, 0.649),
        "C Lobby": (0.664, 0.766)}),
    "Lotus": ("2fe4ed3a-450a-948b-6d6b-e89a78e680a9", {
        "A Site": (0.855, 0.361), "A Main": (0.754, 0.537), "A Root": (0.692, 0.601), "A Link": (0.605, 0.485),
        "A Stairs": (0.733, 0.323), "B Site": (0.503, 0.459), "B Main": (0.451, 0.567), "B Upper": (0.564, 0.365),
        "C Site": (0.148, 0.437), "C Main": (0.228, 0.535), "C Mound": (0.341, 0.64), "C Hall": (0.148, 0.349),
        "C Waterfall": (0.311, 0.434)}),
    "Split": ("d960549e-485c-e861-8d71-aa9d1aed12a2", {
        "A Site": (0.315, 0.184), "A Main": (0.492, 0.208), "A Ramps": (0.472, 0.36), "A Rafters": (0.354, 0.274),
        "A Screens": (0.15, 0.257), "A Tower": (0.316, 0.336), "Mid Top": (0.484, 0.54), "Mid Mail": (0.467, 0.607),
        "Mid Vent": (0.426, 0.451), "B Site": (0.354, 0.867), "B Garage": (0.542, 0.868), "B Tower": (0.43, 0.684),
        "B Rafters": (0.369, 0.747), "B Alley": (0.213, 0.788)}),
}
GUNS = {  # (weapon, your loadout value) for each kind of buy
    "pistol": [("Ghost", 900), ("Classic", 800), ("Sheriff", 950), ("Frenzy", 850)],
    "eco": [("Sheriff", 800), ("Classic", 400), ("Ghost", 900), ("Stinger", 1500)],
    "half": [("Spectre", 2400), ("Bulldog", 2900), ("Marshal", 2350), ("Spectre", 2800), ("Judge", 2850)],
    "full": [("Vandal", 4300), ("Phantom", 4300), ("Vandal", 4700), ("Phantom", 4600), ("Operator", 5300)],
}
TEAM_VALUE = {"pistol": (650, 950), "eco": (600, 1700), "half": (2200, 3200), "full": (3700, 4700)}
# Valorant acts for the career page (Riot's record of every act).
ACTS = [("E8 A1", "2024-01-09"), ("E8 A2", "2024-03-05"), ("E8 A3", "2024-04-30"), ("E9 A1", "2024-06-26"),
        ("E9 A2", "2024-08-28"), ("E9 A3", "2024-10-23"), ("V25 A1", "2025-01-08"), ("V25 A2", "2025-03-05"),
        ("V25 A3", "2025-04-30"), ("V25 A4", "2025-06-25"), ("V25 A5", "2025-08-20"), ("V25 A6", "2025-10-15"),
        ("V26 A1", "2026-01-07"), ("V26 A2", "2026-03-18"), ("V26 A3", "2026-04-29"), ("V26 A4", "2026-06-24"),
        ("V26 A5", "2026-08-19"), ("V26 A6", "2026-10-14")]
ACT_TIERS = [9, 10, 10, 11, 12, 11, 12, 12, 13, 13, 14, 15, 14, 13, 14, 14, 13]  # final rank of each act but the last


def _icon(kind, uid, name="displayicon.png"):
    return f"{VAL_MEDIA}/{kind}/{uid}/{name}"


def _tier_icon(t):
    return f"{VAL_MEDIA}/competitivetiers/{TIER_SET}/{t}/smallicon.png"


def _day_start(days_ago):
    t = time.localtime(time.time() - days_ago * 86400)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def _gauss_int(mean, sd, lo, hi):
    return max(lo, min(hi, round(R.gauss(mean, sd))))


# ---------------- the data folder ----------------

def prepare():
    """Pick the demo's data folder. Returns (folder, keep it afterwards)."""
    want = os.environ.get("GAMETRACKER_DATA")
    if not want:
        return tempfile.mkdtemp(prefix="gametracker-demo-"), False
    want = os.path.abspath(want)
    if os.path.isdir(want) and os.listdir(want):
        if not os.path.exists(os.path.join(want, MARKER)):
            sys.exit(f"GAMETRACKER_DATA is {want}, which holds data that isn't from the demo. The demo won't touch it: "
                     "unset GAMETRACKER_DATA, or point it at a new folder.")
        shutil.rmtree(want)  # an earlier demo: start over with fresh dates
    return want, True


def start(host="127.0.0.1", port=None):
    """Make the demo folder, fill it and start the server. Returns (server, folder, keep folder)."""
    folder, keep = prepare()
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, MARKER), "w", encoding="utf-8") as f:
        f.write("Made by `python -m gametracker demo`. Everything in this folder is made up and safe to delete.\n")
    # config reads these when it's first imported, so they're set before any tracker module loads.
    os.environ["GAMETRACKER_DATA"] = folder
    for k in ("ANTHROPIC_API_KEY", "STEAM_API_KEY", "YOUTUBE_API_KEY", "COACH_URL"):
        os.environ.pop(k, None)
    from . import db, improve, net, server
    net.offline = True
    db.init()
    improve.init()
    last_seen = seed()
    server.STATE.agent["ts"] = last_seen  # "PC offline, last seen ..." rather than waiting for a PC that never comes
    return server.serve(host, port or PORT, background=False), folder, keep


def cleanup(folder, keep):
    if not keep:
        time.sleep(0.5)  # let the server's last connections close
        shutil.rmtree(folder, ignore_errors=True)


# ---------------- seeding ----------------

def seed():
    """Fill the database. Returns when the made-up gaming PC was last seen."""
    from . import db
    now = time.time()
    sessions = _sessions(now)
    val_matches, tier, rr = _valorant(sessions)
    _library(sessions, now)
    db.upsert_sessions([{"uid": uuid.UUID(int=R.getrandbits(128)).hex, "game_id": g, "start_ts": s, "end_ts": e,
                         "exe": None} for g, s, e in sessions])
    db.ingest(_snapshots(val_matches, tier, rr, sessions, now), [
        *({"id": f"val:{m['match_id']}", "game_id": VAL, "ts": m["start"], "data": m} for m in val_matches),
        *_cs2(sessions), *_league(sessions)])
    _improve(val_matches, now)
    _coach(val_matches, now)
    return max(e for _, _, e in sessions) + 240


def _sessions(now):
    """[(game id, start, end)] over the last five weeks: evenings on weekdays, afternoons at weekends."""
    out = []
    for d in range(34, 0, -1):
        base = _day_start(d)
        if d == 1:  # last night: VALORANT, then ELDEN RING, so the Overview opens on a game with art
            games, t = [(VAL, 140), (ER, 95)], base + 18.6 * 3600
        else:
            games = [(g, mins * R.uniform(0.65, 1.35)) for g, (lo, hi, per_week, mins) in ROTATION.items()
                     if lo <= d <= hi and R.random() < per_week / 7]
            R.shuffle(games)
            games = games[:3]
            t = base + (R.uniform(13, 16) if time.localtime(base).tm_wday >= 5 else R.uniform(18, 20.5)) * 3600
        for g, mins in games:
            if t > base + 23.5 * 3600:
                break
            out.append([g, t, t + mins * 60])
            t += mins * 60 + R.uniform(5, 35) * 60
    return [s for s in out if s[2] < now - 600]


def _library(sessions, now):
    from . import db
    last = {}
    for g, _, e in sessions:
        last[g] = max(last.get(g, 0), e)
    games = []
    for gid, source, name, folder, hours, days_ago in LIBRARY:
        extra = {}
        if source == "steam":
            appid = gid.split(":", 1)[1]
            tracked = sum(e - s for g, s, e in sessions if g == gid) / 60
            extra = {"appid": appid, "imported_playtime_min": int(hours * 60 + tracked),
                     "imported_last_played": int(last.get(gid) or now - (days_ago or 400) * 86400 - R.uniform(0, 30000)),
                     "tool": appid == "431960"}
        games.append({"id": gid, "source": source, "name": name, "install_dir": folder, "extra": extra,
                      "exe_names": [], "strict_exes": []})
    db.sync_games(games)
    for gid, source, *_ in LIBRARY:
        appid = gid.split(":", 1)[1]
        db.set_art(gid, {"cover": STEAM_ART.format(appid) + "library_600x900.jpg" if source == "steam" else None,
                         "hero": STEAM_ART.format(appid) + "library_hero.jpg" if source == "steam" else None,
                         "source": "steam" if source == "steam" else None, "ts": now})  # fresh, so nothing re-fetches it
    db.update_game(ER, notes="Strength build, level 92, two-handing a greatsword. Just reached the capital after "
                             "Rykard. Skipped most of Caelid so far.")
    db.update_game(VAL, notes="Main Sova, flex Omen. Working on not dying alone on site.")


# ---------------- VALORANT ----------------

def _valorant(sessions):
    """Matches inside each VALORANT session, oldest first, with rank and RR walked forward from five weeks ago."""
    tier, rr = 13, 41
    out = []
    span = [s for s in sessions if s[0] == VAL]
    for sess in span:
        t = sess[1] + R.uniform(2, 6) * 60
        first = True
        while True:
            queue = "Deathmatch" if first and R.random() < 0.35 else R.choices(
                ("Competitive", "Unrated", "Swiftplay"), (78, 11, 11))[0]
            m = _val_match(t, queue, tier)
            if m["start"] + m["length_sec"] > sess[2] and not first:
                break
            if queue == "Competitive":
                gain = R.randint(17, 25) if m["won"] else -R.randint(13, 21) if m["won"] is False else R.randint(-2, 3)
                rr += gain
                if rr >= 100 and tier < 27:
                    tier, rr = tier + 1, rr - 100
                elif rr < 0:
                    tier, rr = tier - 1, rr + 100
                m.update(rr_change=gain, rank=TIERS[tier])
            out.append(m)
            first = False
            t = m["start"] + m["length_sec"] + R.uniform(3, 7) * 60
        sess[2] = out[-1]["start"] + out[-1]["length_sec"] + R.uniform(1, 5) * 60  # ends a few minutes after the last match
    return out, tier, rr


def _spot(callouts, near=None):
    """A position on the minimap at a callout, or near another position (one of its three closest callouts)."""
    if near is None:
        area = R.choice(list(callouts))
    else:
        by = sorted(callouts, key=lambda a: math.dist(callouts[a], (near["x"], near["y"])))
        area = R.choice(by[1:4])
    x, y = callouts[area]
    return {"x": round(x + R.uniform(-0.018, 0.018), 4), "y": round(y + R.uniform(-0.018, 0.018), 4), "area": area}


def _buy(lost_last, lost_two, pistol, after_pistol):
    if pistol:
        return "pistol"
    if after_pistol:
        return "eco" if lost_last and R.random() < 0.75 else "full" if not lost_last else "half"
    if lost_last and lost_two:
        return R.choices(("eco", "half", "full"), (35, 25, 40))[0]
    if lost_last:
        return R.choices(("eco", "half", "full"), (12, 28, 60))[0]
    return "full" if R.random() < 0.88 else "half"


EDGE = {("full", "eco"): 0.3, ("full", "half"): 0.15, ("half", "eco"): 0.12}
# The two habits the demo player works on: days since they started, their rate before, and their rate now.
FOCUS = {"isolated": (16, 0.43, 0.29), "moving_shots": (9, 0.31, 0.21)}


def _rate(habit, when):
    """A focus habit's rate at a moment: steady until the player started working on it, then better day by day."""
    days, before, now = FOCUS[habit]
    start = time.time() - days * 86400
    return before if when < start else before + (now - before) * min(1, (when - start) / (days * 86400))


def _some(n, rate):
    """How many of n chances hit at this rate, give or take one."""
    return max(0, min(n, round(n * rate + R.gauss(0, 0.15 * math.sqrt(n)))))


def _val_match(start, queue, tier):
    """One match in the shape valorant.summarize stores, rounds, scoreboard and input summaries included."""
    from .integrations.valorant import MATCH_VERSION
    agent = R.choices(list(MAINS), [m[0] for m in MAINS.values()])[0]
    _, abilities, setup = MAINS[agent]
    mp = R.choice(list(MAPS))
    map_id, callouts = MAPS[mp]
    sites = sorted({a.split()[0] for a in callouts if a.endswith("Site")})
    others = [a for a in AGENTS if a != agent]
    team = [agent] + R.sample(others, 4)
    enemy = R.sample(list(AGENTS), 5)
    players = [("a", i) for i in range(5)] + [("e", i) for i in range(5)]
    me = ("a", 0)
    agent_of = {("a", i): a for i, a in enumerate(team)} | {("e", i): a for i, a in enumerate(enemy)}
    party = R.random() < 0.35
    names = dict(zip(players, [ME, PARTNER if party else None] + R.sample(NAMES, 8)))
    names[("a", 1)] = names[("a", 1)] or R.choice([n for n in NAMES if n not in names.values()])
    base = {"match_id": str(uuid.UUID(int=R.getrandbits(128))), "start": start, "queue": queue,
            "ranked": queue == "Competitive", "v": MATCH_VERSION, "map": mp, "map_icon": _icon("maps", map_id),
            "map_splash": _icon("maps", map_id, "splash.png"), "agent": agent, "agent_icon": _icon("agents", AGENTS[agent]),
            "name": ME, "rank": TIERS[tier]}
    ranks = {p: max(3, min(26, tier + R.choice((-2, -1, -1, 0, 0, 0, 1, 1, 2)))) for p in players}
    ranks[me] = tier
    if queue == "Deathmatch":
        return _deathmatch(base)

    skill = R.uniform(0.85, 1.3)  # how well the demo player is playing today
    edge = R.uniform(-0.07, 0.09)
    inputs_on = R.random() < 0.85
    p_move = _rate("moving_shots", start)
    half_at, win_at = (4, 5) if queue == "Swiftplay" else (12, 13)
    first_side = R.choice(("attack", "defense"))
    ult_rounds = set(range(R.randint(4, 7), 30, R.randint(5, 7)))
    tally = {p: {"k": 0, "d": 0, "a": 0, "fk": 0, "dmg": 0} for p in players}
    rounds, results = [], []
    won_n = lost_n = n = 0
    gt = 60000  # game time (ms) when the barriers drop in the first round
    while True:
        side = first_side if n < half_at else ("defense" if first_side == "attack" else "attack")
        if n >= 2 * half_at:  # overtime: sides swap every round
            side = first_side if n % 2 == 0 else ("defense" if first_side == "attack" else "attack")
        pistol = n in (0, half_at)
        lost = [not r for r in results]
        mine = _buy(lost and lost[-1], len(lost) > 1 and lost[-1] and lost[-2], pistol, n in (1, half_at + 1))
        theirs = _buy(results and results[-1], len(results) > 1 and results[-1] and results[-2], pistol,
                      n in (1, half_at + 1))
        p = 0.5 + edge + EDGE.get((mine, theirs), 0) - EDGE.get((theirs, mine), 0)
        won = R.random() < p
        rec = _val_round(n, side, won, mine, theirs, me, players, agent_of, callouts, sites, tally, skill, p_move,
                         inputs_on, setup, n in ult_rounds, gt)
        rounds.append(rec)
        results.append(won)
        won_n, lost_n, n = won_n + won, lost_n + (not won), n + 1
        gt += R.uniform(95, 135) * 1000
        if max(won_n, lost_n) >= win_at and (queue == "Swiftplay" or abs(won_n - lost_n) >= 2) or n >= 30:
            break
    nr = len(rounds)
    hs = {p: R.uniform(13, 31) for p in players} | {me: R.uniform(18, 28)}
    board = []
    for p in players:
        t = tally[p]
        dmg = t["dmg"] + nr * R.uniform(28, 45)
        score = dmg * 1.15 + t["k"] * 55 + t["a"] * 25
        board.append({"name": names[p], "me": p == me, "team": "ally" if p[0] == "a" else "enemy",
                      "party": party and p == ("a", 1), "agent": agent_of[p], "agent_icon": _icon("agents", AGENTS[agent_of[p]]),
                      "rank": TIERS[ranks[p]], "rank_icon": _tier_icon(ranks[p]), "kills": t["k"], "deaths": t["d"],
                      "assists": t["a"], "score": round(score), "acs": round(score / nr), "adr": round(dmg / nr),
                      "hs_pct": round(hs[p], 1), "fk": t["fk"]})
    board.sort(key=lambda x: -x["score"])
    you = next(x for x in board if x["me"])
    m = dict(base, length_sec=round(gt / 1000 - 60 + R.uniform(40, 90)), kills=you["kills"], deaths=you["deaths"],
             assists=you["assists"], round_based=True, acs=you["acs"], adr=you["adr"], hs_pct=you["hs_pct"],
             first_kills=you["fk"], won=won_n > lost_n if won_n != lost_n else None, rounds_won=won_n, rounds_lost=lost_n,
             placement=board.index(you) + 1, players=10, rr_change=None, rounds=rounds, scoreboard=board)
    alone = [r["death"]["facts"] for r in rounds if r["death"] and r["death"]["facts"]["ally_m"] is not None]
    for f in R.sample(alone, _some(len(alone), _rate("isolated", start))):
        f["ally_m"] = round(R.uniform(16, 38), 1)
    if inputs_on:
        fights = m["kills"] + m["deaths"]
        m["inputs"] = {"aligned": True, "share": round(R.uniform(0.75, 0.97), 2), "offset": round(R.uniform(-2, 2), 2)}
        m["shots"] = {"engagements": fights, "moving_first": _some(fights, p_move),
                      "bursts": fights, "long_bursts": sum(R.random() < 0.16 for _ in range(fights)), "source": "inputs"}
        m["abilities"] = _ability_summary(rounds, abilities)
    return m


def _val_round(n, side, won, mine, theirs, me, players, agent_of, callouts, sites, tally, skill, p_move,
               inputs_on, setup, ult, gt):
    """One round: who killed whom and when, and the demo player's buy, kills, death and abilities."""
    attackers_won = (side == "attack") == won
    if attackers_won:
        end = R.choices(("Eliminated", "Bomb detonated"), (60, 40))[0]
    else:
        end = R.choices(("Eliminated", "Bomb defused", "Round timer expired"), (58, 27, 15))[0]
    planted = end in ("Bomb detonated", "Bomb defused") or (end == "Eliminated" and attackers_won and R.random() < 0.45)
    loser, winner = ("a", "e") if not won else ("e", "a")
    need = {winner: R.choices(range(5), (12, 25, 30, 22, 11))[0]}
    if end == "Eliminated" or (end == "Bomb defused" and R.random() < 0.5):
        need[loser] = 5
    else:
        need[loser] = R.randint(1 if end != "Round timer expired" else 0, 4)
    alive = {"a": {p for p in players if p[0] == "a"}, "e": {p for p in players if p[0] == "e"}}
    other = {"a": "e", "e": "a"}
    events, t, forced = [], R.uniform(6, 26), None
    while need["a"] or need["e"]:
        if forced:
            victim, killer, team = forced[0], forced[1], forced[0][0]
            forced = None
        else:
            opts = [x for x in ("a", "e") if need[x] and (len(alive[x]) > 1 or not need[other[x]])]
            team = R.choices(opts, [need[x] for x in opts])[0]
            if team == "a":
                victim = me if me in alive["a"] and (R.random() < (0.26 if not events else 0.2) / skill
                                                     or alive["a"] == {me}) else R.choice(sorted(alive["a"] - {me}))
                killer = R.choice(sorted(alive["e"]))
            else:
                victim = R.choice(sorted(alive["e"]))
                killer = me if me in alive["a"] and (R.random() < 0.26 * skill or alive["a"] == {me}) \
                    else R.choice(sorted(alive["a"] - {me}))
        events.append({"t": round(t, 1), "killer": killer, "victim": victim})
        alive[team].discard(victim)
        need[team] -= 1
        tally[killer]["k"] += 1
        tally[killer]["dmg"] += R.uniform(120, 165)
        tally[victim]["d"] += 1
        helpers = sorted(alive[killer[0]] - {killer})
        if helpers and R.random() < 0.5:
            events[-1]["assist"] = R.choice(helpers)
            tally[events[-1]["assist"]]["a"] += 1
        avenger = sorted(alive[victim[0]] - {victim})
        if (avenger and need[killer[0]] and killer in alive[killer[0]] and (len(alive[killer[0]]) > 1 or not need[victim[0]])
                and R.random() < (0.38 if victim == me else 0.3)):
            forced = (killer, R.choice(avenger))  # traded a moment later
        t += R.uniform(0.8, 3.6) if forced else R.uniform(2.5, 15)
    if events:
        tally[events[0]["killer"]]["fk"] += 1

    def who(p):
        return "me" if p == me else "ally" if p[0] == "a" else "enemy"

    weapon, loadout = R.choice(GUNS[mine])
    loadout += R.choice((-100, -50, 0, 50, 100))
    gun = lambda p: weapon if p == me else R.choice(GUNS[mine if p[0] == "a" else theirs])[0]
    site = R.choice(sites)
    pool = {a: xy for a, xy in callouts.items() if a.startswith((site, "Mid"))} or callouts
    rec = {"n": n, "side": side, "won": won, "end": end,
           "team_loadout": int(round(R.uniform(*TEAM_VALUE[mine]), -1)),
           "enemy_loadout": int(round(R.uniform(*TEAM_VALUE[theirs]), -1)), "feed": [], "loadout": loadout,
           "spent": loadout if n == 0 or R.random() < 0.6 else int(round(loadout * R.uniform(0.2, 0.6), -2)),
           "weapon": weapon, "armor": mine != "eco" or R.random() < 0.5, "buy": mine, "enemy_buy": theirs,
           "kills": [], "death": None, "assists": sum(1 for e in events if e.get("assist") == me), "traded": False,
           "fk": bool(events) and events[0]["killer"] == me, "fd": bool(events) and events[0]["victim"] == me,
           "plant": None, "defuse_t": None}
    for e in events:
        e["weapon"] = gun(e["killer"])
        rec["feed"].append({"t": e["t"], "killer": agent_of[e["killer"]], "ks": who(e["killer"]), "victim": agent_of[e["victim"]],
                            "vs": who(e["victim"]), "weapon": e["weapon"]})
        if e["killer"] == me:
            at = _spot(pool)
            vic = _spot(callouts, at)
            rec["kills"].append({"t": e["t"], "gt": round(gt + e["t"] * 1000), "weapon": e["weapon"], "victim": agent_of[e["victim"]],
                                 "vx": vic["x"], "vy": vic["y"], **at})
    if planted:
        last = events[-1]["t"] if events else 40
        p_t = round(min(85, max(30, statistics.median(e["t"] for e in events) + R.uniform(2, 9) if events else 45)), 1)
        rec["plant"] = {"site": site, "t": p_t, **_spot({f"{site} Site": callouts[f"{site} Site"]})}
        if end == "Bomb defused":
            rec["defuse_t"] = round(min(p_t + 44, max(last + R.uniform(3, 7), p_t + 25)), 1)
    casts = []
    if inputs_on:
        for k, p_use in (("c", 0.55), ("q", 0.5), ("e", 0.7)):
            if R.random() < p_use:
                early = R.random() < (0.45 if k in setup else 0.03)  # set-up utility goes down in the buy phase
                casts.append({"k": k, "t": round(R.uniform(-22, -2) if early else R.uniform(2, 48), 1)})
        if ult:
            casts.append({"k": "x", "t": round(R.uniform(15, 60), 1)})
    death = next((e for e in events if e["victim"] == me), None)
    if death:
        casts = [c for c in casts if c["t"] <= death["t"]]
        at = _spot(pool)
        kpos = _spot(callouts, at)
        before = events[:events.index(death)]
        allies = 4 - sum(1 for e in before if e["victim"][0] == "a")
        facts = {"allies_alive": allies, "enemies_alive": 5 - sum(1 for e in before if e["victim"][0] == "e"),
                 "ally_m": round(R.uniform(3, 14.5), 1) if allies else None,  # _val_match isolates some
                 "killer_m": round(max(2.5, math.dist((at["x"], at["y"]), (kpos["x"], kpos["y"])) * 105), 1)}
        if R.random() < 0.85:
            facts["facing"] = round(R.uniform(62, 150) if R.random() < 0.33 else R.uniform(3, 57))
        mine_before = [k for k in rec["kills"] if k["t"] <= death["t"]]
        if mine_before:
            facts["after_kill_s"] = round(death["t"] - mine_before[-1]["t"], 1)
        if inputs_on:
            facts["shot_2s"] = R.random() < 0.77
            if facts["shot_2s"]:
                facts["moving_last_shot"] = R.random() < p_move
            used = sorted(casts, key=lambda c: c["t"])
            facts.update(util_before=len(used), util_used="".join(sorted({c["k"] for c in used}, key="cqex".index)),
                         last_util_s=round(death["t"] - used[-1]["t"], 1) if used else None)
        rec["death"] = {"t": death["t"], "gt": round(gt + death["t"] * 1000), "weapon": death["weapon"], "by": agent_of[death["killer"]],
                        "kx": kpos["x"], "ky": kpos["y"], **at, "facts": facts}
        rec["traded"] = any(e["victim"] == death["killer"] and e["killer"][0] == "a" and 0 <= e["t"] - death["t"] <= 5
                            for e in events)
    if inputs_on:
        rec["abilities"] = sorted(casts, key=lambda c: c["t"])
    rec["kast"] = bool(rec["kills"]) or rec["assists"] > 0 or death is None or rec["traded"]
    return rec


def _ability_summary(rounds, names):
    """The match's ability summary, as valorant.py builds it from your key presses (here they match Riot's counts)."""
    casts = [(r, a) for r in rounds for a in r.get("abilities") or []]
    check = {}
    for k in "cqex":
        n = sum(1 for _, a in casts if a["k"] == k)
        if n:
            off = R.random() < 0.15
            check[k] = {"riot": n, "timed": n + (1 if off else 0), "status": "close" if off else "exact"}
    firsts = [next((a["t"] for a in r["abilities"] if a["t"] >= 0), None) for r in rounds]
    firsts = [t for t in firsts if t is not None]
    deaths = [r for r in rounds if r["death"]]
    return {
        "names": names, "check": check, "status": "close" if any(c["status"] == "close" for c in check.values()) else "exact",
        "first_s": round(statistics.median(firsts), 1) if firsts else None,
        "buy_phase": sum(1 for _, a in casts if a["t"] < 0),
        "deaths": len(deaths), "deaths_no_util": sum(1 for r in deaths if not r["death"]["facts"].get("util_before")),
        "ult_rounds": [r["n"] + 1 for r in rounds if any(a["k"] == "x" for a in r["abilities"])],
        "per_slot": {k: {"rounds": sum(1 for r in rounds if any(a["k"] == k for a in r["abilities"])),
                         "median_t": round(statistics.median(ts), 1) if (ts := [a["t"] for _, a in casts if a["k"] == k and a["t"] >= 0])
                         else None} for k in check},
    }


def _deathmatch(base):
    """Free-for-all: no rounds, so no ACS, ADR or round data; placement is the result."""
    kills = {n: R.randint(14, 37) for n in [ME] + R.sample(NAMES, 13)}
    kills[max(kills, key=kills.get)] = 40  # first to 40 wins
    order = sorted(kills, key=lambda n: -kills[n])
    agents = R.sample(list(AGENTS), 14)
    board = [{"name": n, "me": n == ME, "team": "ally" if n == ME else "enemy", "party": False,
              "agent": base["agent"] if n == ME else agents[i], "agent_icon": base["agent_icon"] if n == ME
              else _icon("agents", AGENTS[agents[i]]), "rank": None, "rank_icon": None, "kills": kills[n],
              "deaths": R.randint(18, 33), "assists": R.randint(0, 4), "score": kills[n] * 100, "acs": None, "adr": None,
              "hs_pct": None, "fk": 0} for i, n in enumerate(order)]
    me = board[order.index(ME)]
    return dict(base, length_sec=R.uniform(470, 600), kills=me["kills"], deaths=me["deaths"], assists=me["assists"],
                round_based=False, acs=None, adr=None, hs_pct=None, first_kills=0, won=None, rounds_won=None,
                rounds_lost=None, placement=order.index(ME) + 1, players=14, rr_change=None, rounds=[], scoreboard=board)


# ---------------- other games ----------------

def _cs2(sessions):
    """Matches CS2's Game State Integration would have recorded: money, gear and kills per round, no positions."""
    from .integrations.cs2 import map_name
    out = []
    for g, s, e in sessions:
        if g != CS2:
            continue
        t = s + R.uniform(3, 8) * 60
        while t + 2400 < e:
            raw = R.choice(("de_mirage", "de_inferno", "de_ancient", "de_nuke", "de_dust2", "de_anubis"))
            queue = R.choice(("Premier", "Premier", "Competitive"))
            side0, edge = R.choice(("ct", "t")), R.uniform(-0.08, 0.08)
            rounds, w, l, n, ot = [], 0, 0, 0, False
            while True:
                flip = "t" if side0 == "ct" else "ct"
                side = side0 if n < 12 else flip if n < 24 else side0 if (n - 24) // 3 % 2 == 0 else flip  # MR12, MR3 overtime
                won = R.random() < 0.5 + edge
                pistol = n in (0, 12)
                equip = R.randint(700, 1000) if pistol else R.choice((R.randint(200, 1400), R.randint(1600, 3500),
                                                                      R.randint(4200, 5800), R.randint(4200, 5800)))
                kills = R.choices((0, 1, 2, 3, 4), (34, 36, 19, 8, 3))[0]
                end = R.choices(("eliminated", "bomb exploded", "bomb defused", "time ran out"), (62, 16, 14, 8))[0]
                t_won = (side == "t") == won
                if (end == "bomb exploded" and not t_won) or (end in ("bomb defused", "time ran out") and t_won):
                    end = "eliminated"
                rounds.append({"n": n, "side": side, "won": won, "end": end,
                               "bomb": {"bomb exploded": "exploded", "bomb defused": "defused"}.get(end),
                               "money": 800 if pistol else R.randint(1400, 9000), "loadout": equip,
                               "buy": "pistol" if pistol else "eco" if equip < 1500 else "half" if equip < 3700 else "full",
                               "kills": kills, "hs": sum(R.random() < 0.45 for _ in range(kills)),
                               "damage": min(500, kills * R.randint(85, 110) + R.randint(0, 60)),
                               "survived": R.random() < (0.55 if won else 0.2)})
                w, l, n = w + won, l + (not won), n + 1
                if w == l == 12:
                    ot = True
                if (not ot and max(w, l) == 13) or (ot and max(w, l) == 16) or (ot and w == l == 15):
                    break
            k = sum(r["kills"] for r in rounds)
            mvps = sum(1 for r in rounds if r["won"] and r["kills"] >= 2)
            assists = R.randint(2, 7)
            out.append({"id": f"cs2:{int(t)}", "game_id": CS2, "ts": t, "data": {
                "v": 1, "source": "gsi", "partial": False, "start": t, "length_sec": len(rounds) * R.uniform(100, 115),
                "map": map_name(raw), "map_raw": raw, "queue": queue, "rounds_won": w, "rounds_lost": l,
                "won": None if w == l else w > l, "kills": k, "deaths": sum(not r["survived"] for r in rounds),
                "assists": assists, "mvps": mvps, "score": 2 * k + assists + 2 * mvps,
                "adr": round(sum(r["damage"] for r in rounds) / len(rounds)),
                "hs_pct": round(100 * sum(r["hs"] for r in rounds) / k, 1) if k else None, "rounds": rounds}})
            t += out[-1]["data"]["length_sec"] + R.uniform(4, 9) * 60
    return out


CHAMPIONS = ["Ahri", "Ahri", "Orianna", "Syndra", "Viktor", "Ahri", "Lux"]
ARAM = ["Jinx", "Thresh", "Brand", "Ezreal", "Veigar", "Sona", "Darius", "Ziggs"]


def _league(sessions):
    out = []
    for g, s, e in sessions:
        if g != LOL:
            continue
        t = s + R.uniform(2, 5) * 60
        while t + 1500 < e:
            aram = R.random() < 0.3
            length = R.uniform(1000, 1500) if aram else R.uniform(1450, 2300)
            won = R.random() < 0.53
            k, d, a = R.randint(2, 12 if aram else 10), R.randint(1, 9), R.randint(3, 25 if aram else 14)
            out.append({"id": f"lol:{R.randint(10 ** 9, 10 ** 10)}", "game_id": LOL, "ts": t, "data": {
                "start": t, "length_sec": round(length), "queue": "ARAM" if aram else "Ranked Solo/Duo",
                "champion": R.choice(ARAM if aram else CHAMPIONS), "kills": k, "deaths": d, "assists": a,
                "cs": round(length / 60 * R.uniform(3, 4.5 if aram else 7.8)), "gold": round(length / 60 * R.uniform(360, 470)),
                "damage": R.randint(12000, 38000), "vision": R.randint(3, 12) if aram else R.randint(14, 34), "won": won}})
            t += length + R.uniform(3, 8) * 60
    return out


ELDEN = [  # name, share of players who have it (made up), unlocked
    ("Roundtable Hold", 86.2, True), ("Margit, the Fell Omen", 71.4, True), ("Shardbearer Godrick", 61.8, True),
    ("Leonine Misbegotten", 55.1, True), ("Great Rune", 58.9, True), ("Red Wolf of Radagon", 52.3, True),
    ("Rennala, Queen of the Full Moon", 50.7, True), ("Magma Wyrm Makar", 47.6, True), ("Godskin Noble", 43.9, True),
    ("Shardbearer Radahn", 42.4, True), ("Royal Knight Loretta", 40.2, True), ("Elemer of the Briar", 39.8, True),
    ("Mimic Tear", 38.1, True), ("Shardbearer Rykard", 36.3, True), ("Valiant Gargoyle", 26.0, True),
    ("Ancestor Spirit", 30.5, True), ("Shardbearer Morgott", 37.7, False), ("Mohg, the Omen", 33.4, False),
    ("Fire Giant", 32.6, False), ("Godfrey the First Lord", 31.2, False), ("Commander Niall", 29.4, False),
    ("Godskin Duo", 28.8, False), ("Erdtree Aflame", 27.9, False), ("Regal Ancestor Spirit", 24.6, False),
    ("Astel, Naturalborn of the Void", 24.1, False), ("Loretta, Knight of the Haligtree", 23.7, False),
    ("Shardbearer Mohg", 22.9, False), ("Lichdragon Fortissax", 21.5, False), ("Elden Lord", 19.8, False),
    ("Shardbearer Malenia", 18.9, False), ("Dragonlord Placidusax", 17.6, False), ("Legendary Ashen Remains", 14.2, False),
    ("Age of the Stars", 12.8, False), ("Legendary Armaments", 12.1, False), ("Lord of Frenzied Flame", 11.6, False),
    ("Legendary Sorceries and Incantations", 10.3, False), ("Legendary Talismans", 9.1, False), ("Elden Ring", 5.2, False),
]
HIDDEN = {"Lord of Frenzied Flame", "Age of the Stars", "Elden Lord"}  # endings: secret until unlocked


def _snapshots(val_matches, tier, rr, sessions, now):
    comp = [m for m in val_matches if m["queue"] == "Competitive"]
    season_games, season_wins = len(comp[-24:]) + 17, sum(1 for m in comp[-24:] if m["won"]) + 9
    acts = []
    for i, (label, day) in enumerate(ACTS):
        last = i == len(ACTS) - 1
        games = season_games if last else R.randint(18, 72)
        t = tier if last else ACT_TIERS[i]
        peak = t if last else t + R.choice((0, 0, 1, 1, 2))
        acts.append({"act": label, "start": time.mktime(time.strptime(day, "%Y-%m-%d")), "games": games,
                     "wins": season_wins if last else round(games * R.uniform(0.46, 0.56)), "tier": t, "rank": TIERS[t],
                     "icon": _tier_icon(t), "peak_tier": peak, "peak_rank": TIERS[peak], "rr": rr if last else R.randint(0, 99),
                     "leaderboard": None})

    def casual(most):
        out = []
        for a in acts[-9:]:
            games = R.randint(4, most)
            out.append({"act": a["act"], "start": a["start"], "games": games, "wins": round(games * R.uniform(0.44, 0.58))})
        return out

    snaps = [
        {"game_id": VAL, "kind": "val_rank", "ts": now - 3600, "data": {
            "tier": tier, "rank": TIERS[tier], "icon": _tier_icon(tier), "rr": rr, "season_wins": season_wins,
            "season_games": season_games, "leaderboard": None}},
        {"game_id": VAL, "kind": "val_career", "ts": now - 3600, "data": {"queues": {
            "competitive": acts, "unrated": casual(30), "swiftplay": casual(18)}}},
        {"game_id": LOL, "kind": "lol_rank", "ts": now - 86400 * 4, "data": {"name": ME, "level": 214, "queues": [
            {"queue": "RANKED_SOLO_5x5", "tier": "GOLD", "division": "II", "lp": 47, "wins": 38, "losses": 33}]}},
        {"game_id": CS2, "kind": "cs2_lifetime", "ts": now - 86400 * 2, "data": _cs2_lifetime()},
    ]
    er = [s for g, s, e in sessions if g == ER]
    got = [a for a in ELDEN if a[2]]  # the first few before tracking started, the rest during ELDEN RING sessions
    when = sorted([R.uniform(er[0] - 40 * 86400, er[0]) for _ in got[:7]]
                  + [R.choice(er) + R.uniform(1800, 5400) for _ in got[7:]])
    achs = []
    for i, (name, pct, have) in enumerate(sorted(ELDEN, key=lambda a: -a[1] if a[2] else 0)):
        desc = ACH_DESC.get(name) or ("" if name in HIDDEN else f"Defeated {name}")
        achs.append({"api": f"ACH_{i:02d}", "name": name, "desc": desc, "hidden": name in HIDDEN, "achieved": have,
                     "unlock_ts": round(when.pop(0)) if have else None, "pct": pct, "icon_url": None})
    snaps.append({"game_id": ER, "kind": "achievements", "ts": er[-1] + 600, "data": {
        "total": len(achs), "unlocked": sum(a["achieved"] for a in achs), "list": achs}})
    return snaps


ACH_DESC = {"Roundtable Hold": "Arrived at the Roundtable Hold", "Great Rune": "Restored the power of a Great Rune",
            "Erdtree Aflame": "Used the Frenzied Flame to set the Erdtree aflame",
            "Legendary Armaments": "Acquired all legendary armaments", "Legendary Ashen Remains": "Acquired all legendary ashen remains",
            "Legendary Sorceries and Incantations": "Acquired all legendary sorceries and incantations",
            "Legendary Talismans": "Acquired all legendary talismans", "Elden Ring": "Obtained all achievements"}


def _cs2_lifetime():
    s = {"total_kills": 18450, "total_deaths": 17120, "total_kills_headshot": 8320, "total_damage_done": 2310500,
         "total_rounds_played": 21640, "total_shots_fired": 412300, "total_shots_hit": 76100, "total_wins": 10870,
         "total_mvps": 2290, "total_time_played": 642 * 3600, "last_match_kills": 21, "last_match_deaths": 17,
         "last_match_wins": 13, "last_match_damage": 2310, "last_match_mvps": 4}
    for w, k in (("ak47", 6240), ("m4a1", 2380), ("awp", 1460), ("deagle", 1210), ("glock", 980), ("hkp2000", 640),
                 ("famas", 520), ("galilar", 610), ("mp9", 470), ("mac10", 430), ("ump45", 290), ("p250", 310)):
        s[f"total_kills_{w}"] = k
        s[f"total_shots_{w}"] = k * R.randint(14, 22)
        s[f"total_hits_{w}"] = round(s[f"total_shots_{w}"] * R.uniform(0.16, 0.24))
    for mp, r in (("de_mirage", 5210), ("de_inferno", 4380), ("de_dust2", 3920), ("de_nuke", 2210), ("de_ancient", 2460),
                  ("de_anubis", 1870), ("de_vertigo", 1590)):
        s[f"total_rounds_map_{mp}"] = r
        s[f"total_wins_map_{mp}"] = round(r * R.uniform(0.46, 0.55))
    return s


# ---------------- improvement loop ----------------

def _improve(val_matches, now):
    """Two focus habits started a couple of weeks ago (one graduated before that), and Claude-style reviews of the
    newest matches' deaths. The reviews are written here from each death's facts, the way the review prompt asks."""
    from . import db, improve
    ms = [dict(r["data"], _id=r["id"], _ts=r["ts"]) for r in db.matches(VAL, 60)]
    with db.connect() as con:
        con.execute("INSERT INTO improve_focus (game_id, habit, baseline, target, started, status, ended) "
                    "VALUES (?, 'first_death', 24.0, 18, ?, 'done', ?)", (VAL, now - 33 * 86400, now - 21 * 86400))
        for habit, (days, _, _) in FOCUS.items():
            started = now - days * 86400
            before = {h["id"]: h for h in improve.habit_table(VAL, [m for m in ms if m["start"] < started])}
            base = (before.get(habit) or {}).get("recent")
            target = improve.HABIT[habit]["target"] if base is None else round(max(improve.HABIT[habit]["target"], base * 0.75))
            con.execute("INSERT INTO improve_focus (game_id, habit, baseline, target, started, status) VALUES (?,?,?,?,?, 'active')",
                        (VAL, habit, base, target, started))
        reviewed = [m for m in ms if m["queue"] == "Competitive" and m.get("rounds")][:4]
        marks = iter([1, -1, 1])
        for m in reviewed:
            for r in m["rounds"]:
                if not r["death"]:
                    continue
                tags, verdict, fix = _review(r)
                fb = next(marks, None) if R.random() < 0.15 else None
                con.execute("INSERT INTO death_reviews (match_id, round_n, game_id, tags, verdict, fix, model, ts, feedback, video) "
                            "VALUES (?,?,?,?,?,?,?,?,?, NULL)", (m["_id"], r["n"], VAL, json.dumps(tags), verdict, fix,
                                                                 "claude-haiku-5-5", m["start"] + m["length_sec"] + 120, fb))


def _review(r):
    d, f = r["death"], r["death"]["facts"]
    area, by = d.get("area") or "site", d["by"]
    tags, lines = [], []
    if f.get("ally_m") and f["ally_m"] > 15 and f.get("allies_alive"):
        tags.append("isolated")
        lines.append((f"Took the fight at {area} {f['ally_m']} m from the nearest teammate, so nobody could trade.",
                      f"Play {area} with a teammate in trade range, or give it up."))
    if (f.get("facing") or 0) > 60:
        tags.append("caught")
        lines.append((f"Facing {f['facing']} degrees away when {by} shot from {f['killer_m']} m at {area}.",
                      f"Clear the angle {by} came from before turning to the next one."))
    if f.get("shot_2s") is False:
        tags.append("no_shot")
        lines.append((f"Died at {area} without firing; {by} had the first shot from {f['killer_m']} m.",
                      "Hold the likely angle with your crosshair ready instead of walking into it."))
    if (f.get("after_kill_s") or 99) <= 5:
        tags.append("repeek")
        lines.append((f"Re-peeked {f['after_kill_s']}s after your kill and {by} was ready for it at {area}.",
                      "After a kill, reposition or fall back before swinging the same angle."))
    if r["fd"]:
        tags.append("first_death")
        lines.append((f"First death of the round at {area}, so your team played the rest of it 4v5.",
                      "Let the entry or utility take the first contact here."))
    if f.get("allies_alive") is not None and f["allies_alive"] + 1 < f.get("enemies_alive", 0):
        tags.append("outnumbered")
    if f.get("util_before") == 0 and f.get("allies_alive"):
        tags.append("no_utility")
    if not r["traded"] and f.get("allies_alive") and "untraded" not in tags:
        tags.append("untraded")
    if not lines:
        tags = ["fair_fight"] + tags[:1]
        lines.append((f"Even duel with {by} at {f['killer_m']} m on {area}; nothing obvious went wrong.",
                      "Keep the crosshair at head height and take the same fight again."))
    return tags[:3], lines[0][0], lines[0][1]


# ---------------- coach history ----------------

def _coach(val_matches, now):
    """A few earlier conversations, written for the demo from its own numbers, plus notes the coach saved."""
    from . import db
    from .integrations import valorant
    asked = now - 6 * 86400  # the VALORANT conversation only knows the matches before it
    comp = [m for m in val_matches if m["queue"] == "Competitive" and m["start"] < asked][::-1]
    recent = comp[:20]
    st = valorant.round_stats(recent)
    wins = sum(1 for m in recent if m["won"])
    k, d = sum(m["kills"] for m in recent), sum(m["deaths"] for m in recent)
    by_map = {}
    for m in recent:
        by_map.setdefault(m["map"], []).append(bool(m["won"]))  # a draw isn't a win
    maps = sorted(by_map.items(), key=lambda t: (sum(t[1]) / len(t[1]), -len(t[1])))
    worst, best = maps[0], maps[-1]
    side = max(st["sides"], key=lambda s: st["sides"][s]["won"] / max(1, st["sides"][s]["rounds"]))
    fd_side = max(st["sides"], key=lambda s: st["sides"][s]["fd"])
    died = [r for m in recent for r in m["rounds"] if r["death"]]
    iso = sum(1 for r in died if (r["death"]["facts"].get("ally_m") or 0) > 15) / max(1, len(died))
    shots = [m["shots"] for m in recent if m.get("shots")]
    moving = sum(s["moving_first"] for s in shots) / max(1, sum(s["engagements"] for s in shots))
    areas = {}
    for m in recent:
        for r in m["rounds"] if m["map"] == worst[0] else ():
            if r["death"]:
                areas[r["death"]["area"]] = areas.get(r["death"]["area"], 0) + 1
    spots = sorted(areas, key=lambda a: -areas[a])[:2] or ["the same spots"]
    week = {}
    for m in val_matches:
        if asked - 7 * 86400 < m["start"] < asked:
            week[m["map"]] = week.get(m["map"], 0) + 1
    convos = [
        (VAL, 1, asked, "Look at my recent performance. What am I doing well, what are my biggest weaknesses, "
         "and what 2-3 concrete things should I focus on next session?", f"""## Where you stand

Over your last {len(recent)} competitive matches you're {wins}W {len(recent) - wins}L with a **{k / max(1, d):.2f} K/D**. \
Your KAST is **{100 * st['kast']:.0f}%** and you take the first kill in {100 * st['fk']:.0f}% of rounds.

## What's going well

- **{side.title()} rounds**: {st['sides'][side]['won']} of {st['sides'][side]['rounds']} won, your stronger side.
- **{best[0]}**: {sum(best[1])} wins from {len(best[1])} games. Keep queuing it.

## What's costing you rounds

- **Dying alone.** {100 * iso:.0f}% of your deaths are more than 15 m from your nearest teammate, and only \
{100 * st['traded']:.0f}% of your deaths get traded.
- **First deaths** in {100 * st['fd']:.0f}% of rounds, more of them on {fd_side}.
- **Moving shots.** {100 * moving:.0f}% of the fights you finish start with a shot on the move.
- **{worst[0]}** is your weakest map: {sum(worst[1])} wins from {len(worst[1])}.

## For your next session

1. Before you take a fight, check that a teammate can swing right after you. If not, fall back.
2. Use your Owl Drone *before* the first contact, not after it.
3. Stop before the first shot: tap the opposite strafe key, then fire.

I'll keep an eye on the isolated-deaths number in your next matches."""),
        (VAL, 1, asked + 240, "Which map should I practise first?", f"""**{worst[0]}.** It's where you lose most \
({sum(worst[1])} wins from {len(worst[1])}), so it's where practice pays off first.

- Learn two Recon Bolt lineups for each site, one for attack and one for retakes. Write them down as you go.
- On defense, play the site next to your team's sentinel rather than alone at the far angle.
- Watch one of your losses there round by round (open the match and click through the rounds). Your deaths there \
cluster at {' and '.join(spots)}.

Your matches this week: {', '.join(f'{mp} {n}' for mp, n in sorted(week.items(), key=lambda t: -t[1])) or 'none yet'}."""),
        (ER, 1, db.sessions(ER, limit=1)[0]["end_ts"] + 900, "Based on my progress, where am I in the story right now, and what "
         "can I do next? Cover the main "
         "path, worthwhile side content to do at this point, and anything missable soon.", """## Where you are

You've beaten **Rykard** and **Radahn**, and your notes say you've just reached the capital. Your achievements match \
that: everything up to Mt. Gelmir is done, and *Shardbearer Morgott* is the most common one you don't have yet (37.7% of \
players). I'm fairly confident you're at the start of Leyndell.

## The main path

- Work through Leyndell to the **Erdtree** at its centre. There's one big fight on the way in.
- Then the throne room, where the area's lord waits. Level 92 with a greatsword is comfortably on curve.

## Worth doing now

- **Caelid**: you skipped most of it. Its dungeons have the best talismans for a strength build.
- **Volcano Manor** questline: finish it now if you haven't, before things change in the capital.
- Upgrade one spirit ash all the way rather than several halfway: it's the cheapest boost for the fights ahead.

## Missable soon

- Some side quests in the capital can't be finished once the city changes later. Talk to everyone at the Roundtable \
Hold before you go further.

*Light spoilers: nothing beyond the next area is named.*"""),
        ("*", 1, now - 1 * 86400, "Based on what I've been playing and what's in my library, what should I play next, and why?",
         """You've been splitting evenings between **VALORANT** and **ELDEN RING**, with **Hades II** and **Balatro** in \
between.

- **Finish ELDEN RING first.** You're close to the end of the main path and you're playing it steadily.
- **Then Hollow Knight.** You stopped about five months ago with 46 hours in. If you liked Elden Ring's bosses, the end \
of Hollow Knight is a good follow-up and doesn't take long.
- **Keep Balatro for short nights.** Your sessions there are about half an hour, which suits it.

You haven't touched **Valheim** in almost a year. It's best with friends, so I'd leave it until someone asks."""),
    ]
    with db.connect() as con:
        for gid, gen, ts, q, a in convos:
            con.execute("INSERT INTO coach (game_id, ts, model, spoiler, question, content, gen) VALUES (?,?,?,?,?,?,?)",
                        (gid, ts, "claude-opus-5-5 via prime-agent", "light", q, a, gen))
        for gid in (VAL, ER, "*"):
            con.execute("INSERT INTO coach_threads (game_id, gen) VALUES (?, 1)", (gid,))
    notes = [
        ("Sova", "Owl Drone before first contact", ["Fly the drone into the space you're about to take, not after the fight starts.",
                                                    "Tag one enemy, then let your duelist swing on the tag."]),
        ("Sova", "Learn two Recon Bolt lineups per site", ["One for taking the site, one for the retake.",
                                                           "Bounce bolts off high walls so they're harder to shoot."]),
        ("Trading", "Stay in trade range", ["Take fights where a teammate can swing right after you: within 10-15 m.",
                                            "If nobody is close, fall back and wait for them."]),
        (worst[0], "Don't hold the far angle alone", ["Play close to site with your sentinel's utility.",
                                                      "Give up space early and retake with the team."]),
    ]
    for i, (topic, title, points) in enumerate(notes):
        db.kb_add_coach_note(VAL, topic, title, points)
    with db.connect() as con:
        con.execute("UPDATE kb_notes SET ts = ? - id * 3600 WHERE game_id = ?", (now - 6 * 86400, VAL))
