"""The improvement loop: habit metrics from your matches, Claude reviews of each death, and focus habits.

Facts come from the match data (positions, who was alive, which way you faced) and, when the tracker was reading
your inputs, from your mouse and keyboard (did you fire, were you moving). With video evidence on, a death also
comes with six frames from the seconds before it, which Claude looks at too. Each habit is a rate per match. You work
on one or two at a time; a habit graduates once three matches in a row are at or under its target.
"""
import base64
import glob
import json
import os
import re
import shutil
import threading
import time

from . import config, db

GAMES = ("riot:valorant", "steam:730")
SCHEMA = """
CREATE TABLE IF NOT EXISTS death_reviews (
    match_id TEXT, round_n INTEGER, game_id TEXT, tags TEXT, verdict TEXT, fix TEXT, model TEXT, ts REAL,
    feedback INTEGER, PRIMARY KEY (match_id, round_n)
);
CREATE TABLE IF NOT EXISTS improve_focus (
    id INTEGER PRIMARY KEY AUTOINCREMENT, game_id TEXT, habit TEXT, baseline REAL, target REAL, started REAL,
    status TEXT, ended REAL
);
"""

# id, label, what a "hit" means, starting target (%), and what to do about it
HABITS = [
    {"id": "isolated", "label": "Dying away from your team", "short": "Isolated deaths", "target": 30,
     "means": "deaths more than 15 m from your nearest living teammate",
     "fix": "Play within trade range: take fights where a teammate can swing right after you."},
    {"id": "untraded", "label": "Deaths nobody traded", "short": "Untraded deaths", "target": 55,
     "means": "deaths your team didn't trade within 5 seconds (when a teammate was alive)",
     "fix": "Call your fights and peek with a teammate, so your death costs them a player too."},
    {"id": "caught", "label": "Caught looking elsewhere", "short": "Caught off-angle", "target": 30,
     "means": "deaths where you faced more than 60 degrees away from your killer",
     "fix": "Clear angles one at a time and check the likely ones before you rotate or plant."},
    {"id": "no_shot", "label": "Dying without firing", "short": "Died without firing", "target": 25,
     "means": "deaths with no shot from you in the last 2 seconds",
     "fix": "Hold angles you expect a fight from with your crosshair ready; avoid wide swings into unknowns."},
    {"id": "moving_shots", "label": "Shooting while moving", "short": "Moving first shots", "target": 20,
     "means": "deciding fights (ending in your kill or death) you opened by firing while still moving",
     "fix": "Counter-strafe or stop before the first shot: running shots go wide."},
    {"id": "long_sprays", "label": "Holding the trigger too long", "short": "Long sprays", "target": 20,
     "means": "deciding fights you opened with a burst over about a second (CS2: more than 8 shots)",
     "fix": "Burst and reset: tap or short-burst at range, and re-centre between bursts."},
    {"id": "repeek", "label": "Re-peeking after a kill", "short": "Re-peek deaths", "target": 15,
     "means": "deaths within 5 seconds of your own kill",
     "fix": "After a kill, reposition or fall back instead of swinging the same angle again."},
    {"id": "first_death", "label": "Dying first", "short": "First deaths", "target": 18,
     "means": "rounds where you were the first player to die",
     "fix": "Unless you're the entry, let the round develop and take info before taking the first duel."},
]
HABIT = {h["id"]: h for h in HABITS}
MIN_SAMPLES = {"moving_shots": 25, "long_sprays": 25, "first_death": 30}  # others: deaths
TAGS = [h["id"] for h in HABITS] + ["overextended", "bad_timing", "no_utility", "outnumbered", "eco_risk",
                                    "post_plant", "retake", "clutch", "fair_fight", "unlucky",
                                    "crosshair_placement", "wide_peek"]  # the last two only from video frames

_lock = threading.Lock()
_running = set()


def init():
    with db.connect() as con:
        con.executescript(SCHEMA)
        if "video" not in {r["name"] for r in con.execute("PRAGMA table_info(death_reviews)")}:
            con.execute("ALTER TABLE death_reviews ADD COLUMN video TEXT")


# ---------------- video frames (cut on the PC, stored here) ----------------

FRAMES_DIR = os.path.join(config.DATA_DIR, "frames")
FRAMES_KEEP_DAYS = 120
MAX_IMAGES = 96  # per review call; deaths get fewer frames each when a match has more than 16 with video


def _frame_dir(match_id):
    return os.path.join(FRAMES_DIR, re.sub(r"[^A-Za-z0-9_.-]", "_", match_id).lstrip("."))


def save_frames(match_id, game_id, round_n, before, images, report=None):
    """Frames from the seconds before one death (base64 JPEGs, oldest first), and optionally the game's combat report
    for it (VALORANT, from the next round's buy phase), sent by the PC agent."""
    if game_id not in GAMES or not isinstance(round_n, int) or not 0 <= round_n < 100 or not match_id:
        raise ValueError("bad frames")
    imgs = [base64.b64decode(x) for x in images[:8]]
    rep = base64.b64decode(report) if report else None
    if not imgs or any(not b.startswith(b"\xff\xd8") or len(b) > 800_000 for b in imgs + ([rep] if rep else [])):
        raise ValueError("frames must be JPEGs")
    d = _frame_dir(match_id)
    os.makedirs(d, exist_ok=True)
    for old in glob.glob(os.path.join(d, f"r{round_n}_*.jpg")):
        os.remove(old)
    for i, b in enumerate(imgs):
        with open(os.path.join(d, f"r{round_n}_{i}.jpg"), "wb") as f:
            f.write(b)
    rep_path = os.path.join(d, f"r{round_n}_report.jpg")
    if rep:
        with open(rep_path, "wb") as f:
            f.write(rep)
    elif os.path.exists(rep_path):
        os.remove(rep_path)
    meta_path = os.path.join(d, "meta.json")
    try:
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        meta = {"match_id": match_id, "game_id": game_id, "deaths": {}}
    meta["deaths"][str(round_n)] = {"before": [float(x) for x in before][:len(imgs)], "n": len(imgs), "report": bool(rep),
                                    "ts": time.time()}
    with open(meta_path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(meta, f)
    os.replace(meta_path + ".tmp", meta_path)
    for e in os.scandir(FRAMES_DIR):  # old matches' frames go after a few months
        if e.is_dir() and time.time() - e.stat().st_mtime > FRAMES_KEEP_DAYS * 86400:
            shutil.rmtree(e.path, ignore_errors=True)


def frames(match_id, paths=False):
    """{round index: {"before": [seconds], "urls": [...], "report": url or None}} for the deaths of a match that have
    frames ("report" is the game's combat report from the next round's buy phase)."""
    d = _frame_dir(match_id or "")
    try:
        with open(os.path.join(d, "meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return {}
    out, safe = {}, os.path.basename(d)
    for rn, info in meta.get("deaths", {}).items():
        n = int(info.get("n") or 0)
        out[int(rn)] = {"before": info.get("before") or [], "urls": [f"/api/frames/{safe}/r{rn}_{i}.jpg" for i in range(n)],
                        "report": f"/api/frames/{safe}/r{rn}_report.jpg" if info.get("report") else None}
        if paths:
            out[int(rn)]["paths"] = [os.path.join(d, f"r{rn}_{i}.jpg") for i in range(n)]
            out[int(rn)]["report_path"] = os.path.join(d, f"r{rn}_report.jpg") if info.get("report") else None
    return out


def frames_for(m, paths=False):
    """A match's frames. A CS2 demo finds the ones captured live under the live-tracked copy it replaced."""
    return frames(m.get("_id"), paths) or (frames(m["merged_from"], paths) if m.get("merged_from") else {})


def frame_file(folder, name):
    if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", folder) or not re.fullmatch(r"r\d+_(\d+|report)\.jpg", name):
        return None
    p = os.path.join(FRAMES_DIR, folder, name)
    return p if os.path.isfile(p) else None


# ---------------- habit metrics ----------------

def _death_rounds(m):
    if m.get("round_based") is False or m.get("source") == "gsi":
        return []
    return [r for r in m.get("rounds") or [] if r.get("death") and r["death"].get("facts")]


def match_habits(m):
    """{habit: (hits, chances)} for one match; habits without data are left out."""
    out = {}
    rows = _death_rounds(m)
    if rows:
        f = [(r, r["death"]["facts"]) for r in rows]
        known = [(r, x) for r, x in f if x.get("ally_m") is not None and x.get("allies_alive", 1) > 0]
        if known:
            out["isolated"] = (sum(1 for _, x in known if x["ally_m"] > 15), len(known))
        tradable = [(r, x) for r, x in f if x.get("allies_alive", 1) > 0]
        if tradable:
            out["untraded"] = (sum(1 for r, _ in tradable if not r.get("traded")), len(tradable))
        facing = [x for _, x in f if x.get("facing") is not None]
        if facing:
            out["caught"] = (sum(1 for x in facing if x["facing"] > 60), len(facing))
        fired = [x for _, x in f if x.get("shot_2s") is not None]
        if fired:
            out["no_shot"] = (sum(1 for x in fired if not x["shot_2s"]), len(fired))
        out["repeek"] = (sum(1 for _, x in f if (x.get("after_kill_s") or 99) <= 5), len(f))
    rounds = [r for r in m.get("rounds") or [] if "fd" in r] if m.get("round_based") is not False else []
    if rounds and rows:
        out["first_death"] = (sum(1 for r in rounds if r.get("fd")), len(rounds))
    sh = m.get("shots")
    if sh and sh.get("engagements"):
        out["moving_shots"] = (sh["moving_first"], sh["engagements"])
        out["long_sprays"] = (sh["long_bursts"], sh["bursts"])
    return out


def _matches(gid, n=60):
    rows = db.matches(gid, n)
    return [dict(r["data"], _id=r["id"], _ts=r["ts"]) for r in rows]


def habit_table(gid, ms=None):
    ms = ms if ms is not None else _matches(gid)
    per = [(m, match_habits(m)) for m in ms]
    out = []
    for h in HABITS:
        have = [(m, v[h["id"]]) for m, v in per if h["id"] in v and v[h["id"]][1]]
        if not have:
            continue
        recent, older = have[:5], have[5:20]
        rate = lambda xs: round(100 * sum(v[0] for _, v in xs) / sum(v[1] for _, v in xs), 1) if xs and sum(v[1] for _, v in xs) else None
        out.append({**h, "recent": rate(recent), "previous": rate(older), "samples": sum(v[1] for _, v in have[:10]),
                    "matches": len(have),
                    "trend": [{"ts": m["start"], "v": round(100 * v[0] / v[1], 1), "label": f"{v[0]}/{v[1]}"}
                              for m, v in reversed(have[:30])]})
    return out


# ---------------- focus habits ----------------

def focus(gid, status="active"):
    with db.connect() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM improve_focus WHERE game_id=? AND status=? ORDER BY started", (gid, status))]


def set_focus(gid, habit):
    if habit not in HABIT:
        raise ValueError("unknown habit")
    table = {h["id"]: h for h in habit_table(gid)}
    base = (table.get(habit) or {}).get("recent")
    with db.connect() as con:
        if con.execute("SELECT 1 FROM improve_focus WHERE game_id=? AND habit=? AND status='active'", (gid, habit)).fetchone():
            return
        active = con.execute("SELECT id FROM improve_focus WHERE game_id=? AND status='active' ORDER BY started", (gid,)).fetchall()
        if len(active) >= 2:  # two at a time: the oldest makes room
            con.execute("UPDATE improve_focus SET status='dropped', ended=? WHERE id=?", (time.time(), active[0]["id"]))
        target = HABIT[habit]["target"] if base is None else round(max(HABIT[habit]["target"], base * 0.75))
        con.execute("INSERT INTO improve_focus (game_id, habit, baseline, target, started, status) VALUES (?,?,?,?,?, 'active')",
                    (gid, habit, base, target, time.time()))


def drop_focus(gid, habit):
    with db.connect() as con:
        con.execute("UPDATE improve_focus SET status='dropped', ended=? WHERE game_id=? AND habit=? AND status='active'",
                    (time.time(), gid, habit))


def update_focus(gid):
    """Graduate habits that held their target, then pick new ones from the data when there's room."""
    ms = _matches(gid)
    for f in focus(gid):
        since = [m for m in ms if m["start"] >= f["started"]]
        last = [match_habits(m).get(f["habit"]) for m in since]
        last = [v for v in last if v and v[1]][:3]
        if len(last) == 3 and all(100 * a / b <= f["target"] for a, b in last):
            with db.connect() as con:
                con.execute("UPDATE improve_focus SET status='done', ended=? WHERE id=?", (time.time(), f["id"]))
    active = {f["habit"] for f in focus(gid)}
    if len(active) >= 2:
        return
    recent_done = {f["habit"] for f in focus(gid, "done") if time.time() - (f["ended"] or 0) < 14 * 86400}
    dropped = {f["habit"] for f in focus(gid, "dropped") if time.time() - (f["ended"] or 0) < 14 * 86400}
    picks = []
    for h in habit_table(gid, ms):
        need = MIN_SAMPLES.get(h["id"], 12)
        if h["id"] in active | recent_done | dropped or h["samples"] < need or h["recent"] is None:
            continue
        rate = h["recent"] if h["previous"] is None else (h["recent"] + h["previous"]) / 2
        gap = (rate - h["target"]) / h["target"]
        if gap > 0.15:
            picks.append((gap, h["id"]))
    for _, habit in sorted(picks, reverse=True)[:2 - len(active)]:
        set_focus(gid, habit)


# ---------------- Claude death reviews ----------------

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {"deaths": {"type": "array", "items": {
        "type": "object",
        "properties": {"round": {"type": "integer"}, "tags": {"type": "array", "items": {"type": "string", "enum": TAGS}},
                       "verdict": {"type": "string"}, "fix": {"type": "string"}, "video": {"type": "string"}},
        "required": ["round", "tags", "verdict", "fix", "video"], "additionalProperties": False}}},
    "required": ["deaths"], "additionalProperties": False,
}

VIDEO_HELP = """
Some deaths come with frames from the player's screen in the seconds before they died (how long before is given).
Each frame shows the middle of the screen around the crosshair on top, and below it the minimap (left) and the kill
feed (right). Use them to judge crosshair placement (at head height? on the angle the killer came from?), how they
peeked (wide swing, jiggle, holding an angle, running into the open, re-peeking) and utility (used any? could they
have?). Tag "crosshair_placement" when the crosshair was clearly low or off the angle the killer came from, and
"wide_peek" when they swung wide or ran into an angle without information. In "video", say in one sentence (max 22
words) what the frames show that matters; leave it empty for deaths without frames. Frames can be mid-flick or
blurry: only describe what you can actually see.
Some deaths also come with the game's own combat report, captured during the next round's buy phase: who killed them,
and per enemy the damage the player dealt (outgoing, left) and took (incoming, right) with head, body and leg hits.
Its numbers come from the game, like the facts: use them (e.g. dealt 0 damage before dying, took a headshot).
The frames only add to the facts; they never override them. The facts (positions, distances, who was alive, timing,
what the player's inputs did) come from the game's own data and are exact. A frame can't measure distance: field of
view, zoom, crops and blur all distort it. If a frame seems to disagree with a fact, the fact is right: describe only
what the frame adds, and never call a fact wrong or doubtful because of a frame."""

REVIEW_SYSTEM = """You review every death of one player in one {game} match, like a coach going through a demo.
For each death you get the facts the tracker measured: round situation, who was alive, distances, which way the
player faced, whether they fired in the last 2 seconds and whether that shot was on the move, and which abilities
they had used that round and when (the last two only when their inputs were recorded), trades and timing. Positions
and distances come from the game's own match data: treat them as exact. Some facts can be missing.

For each death:
- tags: the habits it shows (only ones the facts support; "fair_fight" for an even duel lost, "unlucky" when nothing
  went wrong). Their meanings: {tag_help}
- verdict: one sentence, max 22 words, what went wrong (or that it was fine), citing the facts.
- fix: one sentence, max 18 words, what to do instead next time.
- video: what the video frames show (see below), or "" when the death has none.
Be honest and specific; do not invent details the facts don't support. Weigh the player's current focus habits:
{focus}
{feedback}{video}"""


def _death_line(m, r):
    d, f = r["death"], r["death"].get("facts") or {}
    bits = [f"R{r['n'] + 1}", r.get("side") or "?", f"{r.get('buy') or '?'} buy vs {r.get('enemy_buy') or '?'}",
            f"died {d['t']}s into the round to {d.get('by')} ({d.get('weapon')})"]
    if f.get("killer_m") is not None:
        bits.append(f"killer {f['killer_m']} m away")
    if f.get("facing") is not None:
        bits.append(f"facing {f['facing']} deg away from the killer")
    if f.get("ally_m") is not None:
        bits.append(f"nearest teammate {f['ally_m']} m")
    if "allies_alive" in f:
        bits.append(f"{f['allies_alive']} teammates and {f.get('enemies_alive', '?')} enemies alive")
    if f.get("shot_2s") is not None:
        bits.append("fired in last 2 s" if f["shot_2s"] else "did not fire in last 2 s")
    if f.get("moving_last_shot") is not None:
        bits.append("last shot while moving" if f["moving_last_shot"] else "last shot while stopped")
    if f.get("util_before") is not None:
        names = (m.get("abilities") or {}).get("names") or {}
        used = ", ".join(names.get(k) or k.upper() for k in f.get("util_used") or "")
        bits.append(f"had used {f['util_before']} {'ability' if f['util_before'] == 1 else 'abilities'} this round ({used}), "
                    f"the last {f['last_util_s']} s before dying"
                    if f["util_before"] else "had not used any ability yet this round")
    if f.get("after_kill_s") is not None:
        bits.append(f"{f['after_kill_s']} s after their own kill")
    bits.append("traded" if r.get("traded") else "not traded")
    if r.get("fd"):
        bits.append("first death of the round")
    if (r.get("plant") or {}).get("site"):
        bits.append(f"spike/bomb planted {r['plant']['site']}")
    bits.append("round won" if r.get("won") else "round lost")
    return ", ".join(str(b) for b in bits)


def _feedback_notes(gid):
    with db.connect() as con:
        rows = con.execute("SELECT tags, verdict FROM death_reviews WHERE game_id=? AND feedback=-1 ORDER BY ts DESC LIMIT 8",
                           (gid,)).fetchall()
    if not rows:
        return ""
    return "The player disagreed with these earlier verdicts; avoid the same over-calls:\n" + "\n".join(
        f"- {r['verdict']} (tags {', '.join(json.loads(r['tags']))})" for r in rows)


def reviewed(match_id):
    with db.connect() as con:
        return {r["round_n"]: dict(r, tags=json.loads(r["tags"] or "[]")) for r in con.execute(
            "SELECT * FROM death_reviews WHERE match_id=?", (match_id,))}


def review_match(gid, m):
    """One Claude call for all of a match's deaths. Returns the number of deaths reviewed."""
    import anthropic

    rows = _death_rounds(m)
    if not rows:
        return 0
    key = config.get("anthropic_api_key")
    if not key:
        raise RuntimeError("Add your Claude API key in Settings first.")
    game = "VALORANT" if gid == "riot:valorant" else "Counter-Strike 2"
    act = [HABIT[f["habit"]]["label"] for f in focus(gid)]
    fr = frames_for(m, paths=True)
    with_video = [r for r in rows if fr.get(r["n"])]
    slots = MAX_IMAGES // len(with_video) if with_video else 0  # images per death; a combat report takes one
    system = REVIEW_SYSTEM.format(
        game=game, tag_help="; ".join(f"{h['id']}: {h['means']}" for h in HABITS),
        focus=("Current focus: " + ", ".join(act)) if act else "No focus habits chosen yet.",
        feedback=_feedback_notes(gid), video=VIDEO_HELP if with_video else "")
    head = (f"{game} {m.get('queue') or ''} on {m.get('map')}, playing {m.get('agent') or 'their usual role'}, "
            f"{'won' if m.get('won') else 'lost' if m.get('won') is False else 'drew'} "
            f"{m.get('rounds_won')}-{m.get('rounds_lost')}, {m.get('kills')}/{m.get('deaths')}/{m.get('assists')}.")
    content = [{"type": "text", "text": head + "\n\nDeaths:"}]
    for r in rows:
        line, f = f"- {_death_line(m, r)}", fr.get(r["n"]) if slots else None
        rep = f.get("report_path") if f and slots > 1 and f.get("report_path") and os.path.exists(f["report_path"]) else None
        per = min(6, slots - (1 if rep else 0))
        keep = [i for i in range(len(f["paths"])) if os.path.exists(f["paths"][i])][-per:] if f else []
        if keep:
            secs = ", ".join(f"{f['before'][i]:g}" if i < len(f["before"]) else "?" for i in keep)
            line += f"\n  Video frames from {secs} seconds before the death:"
        content.append({"type": "text", "text": line})
        for p in [f["paths"][i] for i in keep] + ([rep] if rep else []):
            if p == rep:
                content.append({"type": "text", "text": "  The game's combat report for this death (next round's buy phase):"})
            with open(p, "rb") as fh:
                content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                            "data": base64.standard_b64encode(fh.read()).decode()}})
    client = anthropic.Anthropic(api_key=key)
    try:
        resp = client.messages.create(
            model=config.get("review_model") or "claude-haiku-5-5", max_tokens=8000, system=system,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": REVIEW_SCHEMA}})
    except anthropic.AuthenticationError:
        raise RuntimeError("Claude rejected the API key. Check it in Settings.")
    except anthropic.APIStatusError as e:
        raise RuntimeError(f"Claude API error {e.status_code}: {e.message}")
    except anthropic.APIConnectionError:
        raise RuntimeError("Couldn't reach the Claude API from the server.")
    if resp.stop_reason in ("refusal", "max_tokens"):
        return 0
    data = json.loads(next((b.text for b in resp.content if b.type == "text"), "{}"))
    by_round = {r["n"] + 1: r for r in rows}
    n = 0
    with db.connect() as con:
        for item in data.get("deaths", []):
            r = by_round.get(item.get("round"))
            if not r:
                continue
            con.execute("INSERT INTO death_reviews (match_id, round_n, game_id, tags, verdict, fix, model, ts, feedback, video) "
                        "VALUES (?,?,?,?,?,?,?,?, NULL, ?) ON CONFLICT(match_id, round_n) DO UPDATE SET tags=excluded.tags, "
                        "verdict=excluded.verdict, fix=excluded.fix, model=excluded.model, ts=excluded.ts, video=excluded.video",
                        (m["_id"], r["n"], gid, json.dumps(item.get("tags") or []), (item.get("verdict") or "").strip(),
                         (item.get("fix") or "").strip(), resp.model, time.time(),
                         (item.get("video") or "").strip() if fr.get(r["n"]) else None))
            n += 1
    return n


def pending(gid, limit=15):
    """Recent matches with death facts that haven't been reviewed yet."""
    ms = _matches(gid, 40)
    with db.connect() as con:
        done = {r[0] for r in con.execute("SELECT DISTINCT match_id FROM death_reviews WHERE game_id=?", (gid,))}
    return [m for m in ms if m["_id"] not in done and _death_rounds(m)][:limit]


def run(gid, limit=15):
    """Review what's pending and update the focus habits. Returns a one-line summary."""
    with _lock:
        if gid in _running:
            return "Already reviewing."
        _running.add(gid)
    try:
        reviewed_n = matches_n = 0
        if config.get("death_reviews") and config.get("anthropic_api_key"):
            for m in pending(gid, limit):
                reviewed_n += review_match(gid, m)
                matches_n += 1
        update_focus(gid)
        return f"Reviewed {reviewed_n} deaths in {matches_n} matches." if matches_n else "Nothing new to review."
    finally:
        with _lock:
            _running.discard(gid)


def run_async(gid):
    def go():
        try:
            print(f"[improve] {gid}: {run(gid)}")
        except Exception as e:
            print(f"[improve] {gid}: {type(e).__name__}: {e}")
    threading.Thread(target=go, daemon=True, name=f"improve-{gid}").start()


def is_running(gid):
    with _lock:
        return gid in _running


def forget_reviews(match_id):
    """Drop a match's reviews so they're redone with newer facts; ones you marked fair or not fair stay."""
    with db.connect() as con:
        con.execute("DELETE FROM death_reviews WHERE match_id=? AND feedback IS NULL", (match_id,))


def set_feedback(match_id, round_n, value):
    with db.connect() as con:
        con.execute("UPDATE death_reviews SET feedback=? WHERE match_id=? AND round_n=?", (value, match_id, round_n))


# ---------------- reading it back ----------------

def summary(gid, full=False):
    if gid not in GAMES:
        return None
    ms = _matches(gid)
    table = habit_table(gid, ms)
    by_id = {h["id"]: h for h in table}
    act = []
    for f in focus(gid):
        h = by_id.get(f["habit"]) or {**HABIT[f["habit"]], "recent": None, "trend": []}
        act.append({**f, "label": h["label"], "short": h["short"], "fix": h["fix"], "means": h["means"],
                    "recent": h.get("recent"), "trend": [p for p in h.get("trend", []) if p["ts"] >= f["started"] - 30 * 86400]})
    with_facts = [m for m in ms if _death_rounds(m) or m.get("shots")]
    out = {"focus": act, "habits": table, "matches": len(with_facts),
           "inputs": sum(1 for m in ms if (m.get("inputs") or {}).get("aligned") or (m.get("shots") or {}).get("source") == "demo"),
           "running": is_running(gid), "ready": bool(config.get("anthropic_api_key")),
           "done": [dict(f, label=HABIT[f["habit"]]["label"]) for f in focus(gid, "done")][-5:]}
    if full:
        out["video"] = sum(1 for m in with_facts if frames_for(m))
        out["reviews"] = recent_reviews(gid, ms)
        out["catalog"] = [{k: h[k] for k in ("id", "label", "means", "target")} for h in HABITS]
    return out


def recent_reviews(gid, ms=None, limit=60):
    ms = {m["_id"]: m for m in (ms if ms is not None else _matches(gid))}
    with db.connect() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM death_reviews WHERE game_id=? ORDER BY ts DESC, round_n LIMIT ?", (gid, limit))]
    out, fr = [], {}
    for r in rows:
        m = ms.get(r["match_id"])
        if not m:
            continue
        rnd = next((x for x in m.get("rounds") or [] if x.get("n") == r["round_n"]), None)
        if not rnd or not rnd.get("death"):
            continue
        if m["_id"] not in fr:
            fr[m["_id"]] = frames_for(m)
        out.append({"match_id": r["match_id"], "round": r["round_n"], "tags": json.loads(r["tags"] or "[]"),
                    "verdict": r["verdict"], "fix": r["fix"], "feedback": r["feedback"], "map": m.get("map"),
                    "video": r.get("video"), "frames": fr[m["_id"]].get(r["round_n"]),
                    "start": m.get("start"), "agent": m.get("agent"), "won": rnd.get("won"), "side": rnd.get("side"),
                    "death": {k: rnd["death"].get(k) for k in ("t", "by", "weapon", "area")}, "facts": rnd["death"].get("facts") or {}})
    out.sort(key=lambda x: (-(x["start"] or 0), x["round"]))
    return out


def coach_context(gid):
    if gid not in GAMES:
        return ""
    s = summary(gid)
    if not s or not (s["focus"] or s["habits"]):
        return ""
    lines = ["Improvement tracking (habit rates per death/fight, recent 5 matches vs the 15 before):"]
    for h in s["habits"]:
        lines.append(f"- {h['label']}: {h['recent']}% recent, {h['previous']}% before (target {h['target']}%; {h['means']})")
    if s["focus"]:
        lines.append("Current focus habits: " + "; ".join(f"{f['label']} (baseline {f['baseline']}%, target {f['target']}%)" for f in s["focus"]))
    revs = recent_reviews(gid, limit=12)
    if revs:
        lines.append("Latest death reviews:")
        lines += [f"- {r['map']} R{r['round'] + 1}: {r['verdict']} Fix: {r['fix']}"
                  + (f" Video: {r['video']}" if r.get("video") else "") for r in revs[:8]]
    return "\n".join(lines)
