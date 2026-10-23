"""Server-side SQLite storage: games, play sessions, stat snapshots, matches and coach notes."""
import contextlib
import json
import os
import sqlite3
import threading
import time

from .config import DATA_DIR

DB_PATH = os.path.join(DATA_DIR, "gametracker.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    id TEXT PRIMARY KEY,
    source TEXT, name TEXT, install_dir TEXT,
    exe_names TEXT DEFAULT '[]', strict_exes TEXT DEFAULT '[]', extra TEXT DEFAULT '{}',
    hidden INTEGER DEFAULT 0, tracked INTEGER DEFAULT 1, installed INTEGER DEFAULT 1,
    custom INTEGER DEFAULT 0, notes TEXT DEFAULT '', first_seen REAL
);
CREATE TABLE IF NOT EXISTS sessions (
    uid TEXT PRIMARY KEY,
    game_id TEXT, start_ts REAL, end_ts REAL, exe TEXT
);
CREATE INDEX IF NOT EXISTS sessions_game ON sessions(game_id, start_ts);
CREATE INDEX IF NOT EXISTS sessions_start ON sessions(start_ts);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT, kind TEXT, ts REAL, data TEXT
);
CREATE INDEX IF NOT EXISTS snapshots_game ON snapshots(game_id, kind, ts);
CREATE TABLE IF NOT EXISTS matches (
    id TEXT PRIMARY KEY, game_id TEXT, ts REAL, data TEXT
);
CREATE INDEX IF NOT EXISTS matches_game ON matches(game_id, ts);
CREATE TABLE IF NOT EXISTS coach (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT, ts REAL, model TEXT, spoiler TEXT, question TEXT, content TEXT
);
"""


# Knowledge base: notes Claude wrote from YouTube guides (no transcripts are stored).
KB_SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_videos (
    video_id TEXT, game_id TEXT, title TEXT, channel TEXT, published TEXT, duration INTEGER, views INTEGER,
    query TEXT, topic TEXT, status TEXT, reason TEXT, tokens INTEGER, ts REAL, PRIMARY KEY (video_id, game_id)
);
CREATE TABLE IF NOT EXISTS kb_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, game_id TEXT, video_id TEXT, topic TEXT, title TEXT, points TEXT,
    t INTEGER, ts REAL
);
CREATE INDEX IF NOT EXISTS kb_notes_game ON kb_notes(game_id, ts);
CREATE TABLE IF NOT EXISTS kb_runs (game_id TEXT PRIMARY KEY, ts REAL, ok INTEGER, msg TEXT, running INTEGER DEFAULT 0);
"""


@contextlib.contextmanager
def connect():
    """Connection that commits on success and is always closed."""
    os.makedirs(DATA_DIR, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    try:
        with con:
            yield con
    finally:
        con.close()


def init():
    with connect() as con:
        con.executescript(SCHEMA)
        cols = {r["name"] for r in con.execute("PRAGMA table_info(coach)")}
        if "gen" not in cols:
            con.execute("ALTER TABLE coach ADD COLUMN gen INTEGER DEFAULT 0")
        con.execute("CREATE TABLE IF NOT EXISTS coach_threads (game_id TEXT PRIMARY KEY, gen INTEGER DEFAULT 0)")
        con.execute("CREATE TABLE IF NOT EXISTS coach_titles (game_id TEXT, gen INTEGER, title TEXT, PRIMARY KEY (game_id, gen))")
        con.execute("CREATE TABLE IF NOT EXISTS coach_matches (match_id TEXT PRIMARY KEY, game_id TEXT, gen INTEGER)")
        con.executescript(KB_SCHEMA)
        if "source" not in {r["name"] for r in con.execute("PRAGMA table_info(kb_notes)")}:
            con.execute("ALTER TABLE kb_notes ADD COLUMN source TEXT DEFAULT ''")  # coach notes: where it came from
        gcols = {r["name"] for r in con.execute("PRAGMA table_info(games)")}
        for col in ("art", "art_override"):
            if col not in gcols:
                con.execute(f"ALTER TABLE games ADD COLUMN {col} TEXT")


def _row(r):
    d = dict(r)
    for k in ("exe_names", "strict_exes", "extra", "data", "art", "art_override"):
        if k in d and isinstance(d[k], str):
            d[k] = json.loads(d[k] or "null")
    return d


# ---------------- games ----------------

def sync_games(scanned):
    """Merge a library scan from the agent. User choices (hidden/tracked/notes) are kept."""
    now = time.time()
    with connect() as con:
        known = {r["id"] for r in con.execute("SELECT id FROM games")}
        for g in scanned:
            vals = (g["source"], g["name"], g["install_dir"], json.dumps(g.get("exe_names") or []),
                    json.dumps(g.get("strict_exes") or []), json.dumps(g.get("extra") or {}))
            if g["id"] in known:
                con.execute("UPDATE games SET source=?, name=?, install_dir=?, exe_names=?, strict_exes=?, "
                            "extra=?, installed=1 WHERE id=?", vals + (g["id"],))
            else:
                tool = bool((g.get("extra") or {}).get("tool"))
                con.execute("INSERT INTO games (source, name, install_dir, exe_names, strict_exes, extra, "
                            "hidden, tracked, first_seen, id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            vals + (int(tool), int(not tool), now, g["id"]))
        ids = [g["id"] for g in scanned] or [""]
        con.execute(f"UPDATE games SET installed=0 WHERE custom=0 AND id NOT IN ({','.join('?' * len(ids))})", ids)


def add_custom_game(name, path):
    """Path is on the gaming PC: a game folder or a specific .exe."""
    is_exe = path.lower().endswith(".exe")
    sep = "\\" if "\\" in path else "/"
    install_dir = path.rsplit(sep, 1)[0] if is_exe else path.rstrip("\\/")
    exe = [path.rsplit(sep, 1)[-1]] if is_exe else []
    gid = "custom:" + "".join(c if c.isalnum() else "_" for c in name.strip().lower())
    with connect() as con:
        con.execute("INSERT OR REPLACE INTO games (id, source, name, install_dir, exe_names, strict_exes, extra, "
                    "custom, first_seen) VALUES (?, 'custom', ?, ?, ?, ?, '{}', 1, ?)",
                    (gid, name.strip(), install_dir, json.dumps(exe), json.dumps(exe), time.time()))
    return gid


def all_games():
    with connect() as con:
        return [_row(r) for r in con.execute("SELECT * FROM games")]


def set_art(gid, art):
    with connect() as con:
        con.execute("UPDATE games SET art=? WHERE id=?", (json.dumps(art), gid))


def update_game(gid, **fields):
    if "art_override" in fields:
        fields["art_override"] = json.dumps(fields["art_override"]) if fields["art_override"] else None
    allowed = {k: v for k, v in fields.items() if k in ("hidden", "tracked", "notes", "name", "art_override")}
    if not allowed:
        return
    with connect() as con:
        con.execute(f"UPDATE games SET {', '.join(k + '=?' for k in allowed)} WHERE id=?",
                    list(allowed.values()) + [gid])


def delete_game(gid):
    with connect() as con:
        con.execute("DELETE FROM games WHERE id=? AND custom=1", (gid,))


def trackable_games():
    with connect() as con:
        return [_row(r) for r in con.execute("SELECT * FROM games WHERE tracked=1 AND installed=1")]


def get_game(gid):
    with connect() as con:
        r = con.execute("SELECT * FROM games WHERE id=?", (gid,)).fetchone()
        return _row(r) if r else None


def library():
    q = """
    SELECT g.*, COALESCE(SUM(s.end_ts - s.start_ts), 0) AS tracked_sec,
           COUNT(s.uid) AS session_count, MAX(s.end_ts) AS last_session
    FROM games g LEFT JOIN sessions s ON s.game_id = g.id
    GROUP BY g.id
    """
    out = []
    with connect() as con:
        for r in con.execute(q):
            g = _row(r)
            extra = g["extra"] or {}
            # Steam's own counter includes time we tracked too, so take the larger rather than summing.
            g["total_sec"] = max(extra.get("imported_playtime_min", 0) * 60, g["tracked_sec"])
            g["last_played"] = max(extra.get("imported_last_played", 0) or 0, g["last_session"] or 0)
            out.append(g)
    out.sort(key=lambda g: g["last_played"] or 0, reverse=True)
    return out


# ---------------- sessions ----------------

def upsert_sessions(items):
    """Idempotent: the agent resends a session's latest state until the server acknowledges it."""
    with connect() as con:
        for s in items:
            if s.get("discard"):
                con.execute("DELETE FROM sessions WHERE uid=?", (s["uid"],))
                continue
            con.execute("INSERT INTO sessions (uid, game_id, start_ts, end_ts, exe) VALUES (?,?,?,?,?) "
                        "ON CONFLICT(uid) DO UPDATE SET end_ts=MAX(end_ts, excluded.end_ts)",
                        (s["uid"], s["game_id"], s["start_ts"], s["end_ts"], s.get("exe")))


def sessions(gid=None, since=None, limit=200):
    q, args = "SELECT s.*, g.name FROM sessions s LEFT JOIN games g ON g.id = s.game_id WHERE 1=1", []
    if gid:
        q += " AND s.game_id=?"
        args.append(gid)
    if since:
        q += " AND s.end_ts >= ?"
        args.append(since)
    q += " ORDER BY s.start_ts DESC LIMIT ?"
    args.append(limit)
    with connect() as con:
        return [dict(r) for r in con.execute(q, args)]


# ---------------- stats ----------------

def ingest(snapshots=(), matches=()):
    with connect() as con:
        for s in snapshots:
            data = json.dumps(s["data"], sort_keys=True)
            last = con.execute("SELECT data FROM snapshots WHERE game_id=? AND kind=? ORDER BY ts DESC LIMIT 1",
                               (s["game_id"], s["kind"])).fetchone()
            if last and last["data"] == data:
                continue  # unchanged since last sync
            con.execute("INSERT INTO snapshots (game_id, kind, ts, data) VALUES (?,?,?,?)",
                        (s["game_id"], s["kind"], s.get("ts") or time.time(), data))
        for m in matches:
            con.execute("INSERT OR REPLACE INTO matches (id, game_id, ts, data) VALUES (?,?,?,?)",
                        (m["id"], m["game_id"], m["ts"], json.dumps(m["data"])))


def snapshots(gid, kind, limit=50, before=None):
    q, args = "SELECT * FROM snapshots WHERE game_id=? AND kind=?", [gid, kind]
    if before:
        q += " AND ts < ?"
        args.append(before)
    q += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    with connect() as con:
        return [_row(r) for r in con.execute(q, args)]


def latest_snapshot(gid, kind, before=None):
    s = snapshots(gid, kind, 1, before)
    return s[0] if s else None


def known_matches(gid, limit=300, min_v=0):
    with connect() as con:
        return [r["id"] for r in con.execute(
            "SELECT id FROM matches WHERE game_id=? AND COALESCE(json_extract(data, '$.v'), 1) >= ? "
            "ORDER BY ts DESC LIMIT ?", (gid, min_v, limit))]


def delete_match(mid):
    with connect() as con:
        con.execute("DELETE FROM matches WHERE id=?", (mid,))


def get_match(mid):
    with connect() as con:
        r = con.execute("SELECT * FROM matches WHERE id=?", (mid,)).fetchone()
        return _row(r) if r else None


def matches(gid, limit=50):
    with connect() as con:
        return [_row(r) for r in con.execute(
            "SELECT * FROM matches WHERE game_id=? ORDER BY ts DESC LIMIT ?", (gid, limit))]


def add_coach(gid, model, spoiler, question, content, gen=0):
    with connect() as con:
        con.execute("INSERT INTO coach (game_id, ts, model, spoiler, question, content, gen) VALUES (?,?,?,?,?,?,?)",
                    (gid, time.time(), model, spoiler, question, content, gen))


def coach_history(gid, limit=40):
    with connect() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM coach WHERE game_id=? ORDER BY ts DESC LIMIT ?", (gid, limit))]


def coach_gen(gid):
    with connect() as con:
        r = con.execute("SELECT gen FROM coach_threads WHERE game_id=?", (gid,)).fetchone()
        return r["gen"] if r else 0


_gen_lock = threading.Lock()


def _next_gen(con, gid):
    """A conversation number this game hasn't used: not the current one, an earlier one or a match's."""
    r = con.execute("""SELECT MAX(g) AS g FROM (
        SELECT gen AS g FROM coach WHERE game_id=? UNION ALL SELECT gen FROM coach_threads WHERE game_id=?
        UNION ALL SELECT gen FROM coach_matches WHERE game_id=?)""", (gid, gid, gid)).fetchone()
    return (r["g"] if r["g"] is not None else 0) + 1


def bump_coach_gen(gid):
    """Start a new current conversation for the game."""
    with _gen_lock, connect() as con:
        gen = _next_gen(con, gid)
        con.execute("INSERT INTO coach_threads (game_id, gen) VALUES (?, ?) "
                    "ON CONFLICT(game_id) DO UPDATE SET gen = excluded.gen", (gid, gen))
    return gen


def match_coach_gen(match_id, gid=None):
    """The conversation about one match. With gid, one is started if there isn't one yet; the game's current
    conversation stays as it is."""
    with _gen_lock, connect() as con:
        r = con.execute("SELECT gen FROM coach_matches WHERE match_id=?", (match_id,)).fetchone()
        if r or not gid:
            return r["gen"] if r else None
        gen = _next_gen(con, gid)
        con.execute("INSERT INTO coach_matches (match_id, game_id, gen) VALUES (?,?,?)", (match_id, gid, gen))
        return gen


def coach_match_of(gid, gen):
    """The match a conversation is about, or None."""
    with connect() as con:
        r = con.execute("SELECT match_id FROM coach_matches WHERE game_id=? AND gen=?", (gid, gen)).fetchone()
        return r["match_id"] if r else None


def coach_match_links():
    """{(game_id, gen): match_id} for every match conversation."""
    with connect() as con:
        return {(r["game_id"], r["gen"]): r["match_id"] for r in con.execute("SELECT * FROM coach_matches")}


def coach_conversations():
    """Every coach conversation (one per game and thread number), most recently active first."""
    with connect() as con:
        return [dict(r) for r in con.execute("""
            SELECT c.game_id, c.gen, COUNT(*) AS turns, MIN(c.ts) AS first_ts, MAX(c.ts) AS last_ts,
                   (SELECT question FROM coach f WHERE f.game_id = c.game_id AND f.gen = c.gen ORDER BY f.ts LIMIT 1) AS first_q,
                   t.title
            FROM coach c LEFT JOIN coach_titles t ON t.game_id = c.game_id AND t.gen = c.gen
            GROUP BY c.game_id, c.gen ORDER BY last_ts DESC""")]


def coach_thread(gid, gen):
    with connect() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM coach WHERE game_id=? AND gen=? ORDER BY ts", (gid, gen))]


def set_coach_title(gid, gen, title):
    with connect() as con:
        if title:
            con.execute("INSERT INTO coach_titles (game_id, gen, title) VALUES (?,?,?) "
                        "ON CONFLICT(game_id, gen) DO UPDATE SET title = excluded.title", (gid, gen, title))
        else:
            con.execute("DELETE FROM coach_titles WHERE game_id=? AND gen=?", (gid, gen))


def delete_coach_thread(gid, gen):
    with connect() as con:
        con.execute("DELETE FROM coach WHERE game_id=? AND gen=?", (gid, gen))
        con.execute("DELETE FROM coach_titles WHERE game_id=? AND gen=?", (gid, gen))
        con.execute("DELETE FROM coach_matches WHERE game_id=? AND gen=?", (gid, gen))


def kb_seen(gid):
    with connect() as con:
        return {r[0] for r in con.execute("SELECT video_id FROM kb_videos WHERE game_id=?", (gid,))}


def kb_add_video(gid, v, query, topic, status, reason, tokens=0):
    with connect() as con:
        con.execute("INSERT OR REPLACE INTO kb_videos (video_id, game_id, title, channel, published, duration, views, "
                    "query, topic, status, reason, tokens, ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (v["video_id"], gid, v.get("title"), v.get("channel"), v.get("published"), v.get("duration"),
                     v.get("views"), query, topic, status, reason, tokens, time.time()))


def kb_add_notes(gid, vid, notes):
    now = time.time()
    with connect() as con:
        con.execute("DELETE FROM kb_notes WHERE game_id=? AND video_id=?", (gid, vid))  # re-reading replaces
        for n in notes:
            con.execute("INSERT INTO kb_notes (game_id, video_id, topic, title, points, t, ts) VALUES (?,?,?,?,?,?,?)",
                        (gid, vid, n["topic"].strip(), n["title"].strip(), json.dumps(n["points"]), int(n.get("t") or 0), now))


def kb_add_coach_note(gid, topic, title, points, source=""):
    """A note the coach agent saved (no video behind it)."""
    with connect() as con:
        con.execute("INSERT INTO kb_notes (game_id, video_id, topic, title, points, t, ts, source) VALUES (?,?,?,?,?,?,?,?)",
                    (gid, "", topic, title, json.dumps(points), 0, time.time(), source or ""))


def kb_notes(gid):
    with connect() as con:
        return [dict(r, points=json.loads(r["points"] or "[]")) for r in con.execute(
            "SELECT * FROM kb_notes WHERE game_id=? ORDER BY ts DESC, id", (gid,))]


def kb_videos(gid, limit=300):
    with connect() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM kb_videos WHERE game_id=? ORDER BY ts DESC LIMIT ?", (gid, limit))]


def kb_delete_note(gid, note_id):
    with connect() as con:
        con.execute("DELETE FROM kb_notes WHERE game_id=? AND id=?", (gid, note_id))


def kb_set_run(gid, ok, msg, running):
    with connect() as con:
        if running:  # keep the last finished time while a run is going
            con.execute("INSERT INTO kb_runs (game_id, ts, ok, msg, running) VALUES (?, 0, 1, ?, 1) "
                        "ON CONFLICT(game_id) DO UPDATE SET msg=excluded.msg, running=1", (gid, msg))
        else:
            con.execute("INSERT INTO kb_runs (game_id, ts, ok, msg, running) VALUES (?,?,?,?,0) "
                        "ON CONFLICT(game_id) DO UPDATE SET ts=excluded.ts, ok=excluded.ok, msg=excluded.msg, running=0",
                        (gid, time.time(), int(ok), msg))


def kb_run(gid):
    with connect() as con:
        r = con.execute("SELECT * FROM kb_runs WHERE game_id=?", (gid,)).fetchone()
        return dict(r) if r else None


def kb_runs():
    with connect() as con:
        return [dict(r) for r in con.execute("SELECT * FROM kb_runs")]


def snapshot_to(path):
    """Consistent read-only copy of the database for the coach agent (settings live elsewhere)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    src = sqlite3.connect(DB_PATH, timeout=15)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    os.replace(tmp, path)
