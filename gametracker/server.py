"""HTTP server: the dashboard + JSON API, and the endpoints the PC agent reports to.

Runs anywhere Python does (your gaming PC, or a Linux home server on your tailnet).
"""
import hmac
import ipaddress
import json
import mimetypes
import os
import re
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import art, config, db, improve, kb
from .integrations import all_integrations

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
INTEGRATIONS = all_integrations()


class State:
    """What the agent last told us, plus commands waiting for it."""

    def __init__(self):
        self.lock = threading.Lock()
        self.agent = {"ts": 0, "host": None, "now_playing": [], "sync_status": {}}
        self.commands = []

    def push(self, cmd):
        with self.lock:
            if cmd not in self.commands:
                self.commands.append(cmd)

    def take(self):
        with self.lock:
            out, self.commands = self.commands, []
            return out

    def online(self):
        poll = int(config.get("poll_interval_sec") or 5)
        return time.time() - self.agent["ts"] < max(30, poll * 4)


STATE = State()
kb.relay.push, kb.relay.online = STATE.push, STATE.online  # the gaming PC downloads guide captions for the server


# ---------------- handlers ----------------

def game_detail(gid, mode=None):
    g = db.get_game(gid)
    if not g:
        return None
    lib = next((x for x in db.library() if x["id"] == gid), g)
    lib["art"] = art.for_game(lib)
    modes, chosen, owner = [], None, None
    for integ in INTEGRATIONS:  # the first stats source with modes (e.g. Competitive / Quick Play) splits the page
        if integ.applies(g):
            try:
                modes = integ.modes(g)
            except Exception:
                modes = []
            if modes:
                keys = [m["key"] for m in modes]
                chosen = mode if mode in keys else next((m["key"] for m in modes if m.get("default")), keys[0])
                owner = integ
                break
    panels = []
    for integ in INTEGRATIONS:
        if integ.applies(g):
            try:
                ps = integ.panel(g, chosen) if integ is owner else integ.panel(g)
            except Exception as e:
                ps = {"title": integ.title, "notes": [f"Couldn't build panel: {e}"]}
            for i, p in enumerate(ps if isinstance(ps, list) else [ps]):  # an integration may return several
                p["key"] = integ.key
                p["sync"] = STATE.agent["sync_status"].get(f"{gid}|{integ.key}") if i == 0 else None
                panels.append(p)
    from . import coach  # imported lazily so the server starts without the anthropic package
    links, gen = db.coach_match_links(), db.coach_gen(gid)
    return {
        "game": lib,
        "sessions": db.sessions(gid, limit=100),
        "panels": panels,
        "coach": [c for c in db.coach_history(gid) if (gid, c["gen"] or 0) not in links],  # matches have their own
        "coach_gen": gen,
        "coach_status": coach.status(gid, gen),
        "playing": any(p["game_id"] == gid for p in STATE.agent["now_playing"]) and STATE.online(),
        "modes": modes, "mode": chosen,
        "kb": kb.summary(gid),
        "improve": improve.summary(gid),
    }


def page_detail(gid, key):
    g = db.get_game(gid)
    if not g:
        return None
    for integ in INTEGRATIONS:
        if integ.applies(g):
            d = integ.detail(g, key)
            if d:
                return {**d, "game": {"id": gid, "name": g.get("name")}}
    return None


def overview():
    since = time.time() - 35 * 86400
    return {
        "agent": {"online": STATE.online(), "host": STATE.agent["host"], "last_seen": STATE.agent["ts"]},
        "now_playing": STATE.agent["now_playing"] if STATE.online() else [],
        "sessions": db.sessions(since=since, limit=2000),
        "library": [{**{k: g[k] for k in ("id", "name", "source", "total_sec", "last_played", "hidden")},
                     "art": art.for_game(g)} for g in db.library()],
    }


def match_brief(data):
    """Map, score and result of a match, to name its coach conversation."""
    d = data or {}
    rw, rl = d.get("rounds_won"), d.get("rounds_lost")
    if d.get("round_based") is False and d.get("placement"):
        result = f"placed {d['placement']} of {d.get('players')}"
    else:
        result = "win" if d.get("won") else "loss" if d.get("won") is False else "draw" if rw is not None else ""
    return {"map": d.get("map") or d.get("queue") or "Match", "score": f"{rw}–{rl}" if rw is not None and rl is not None else "",
            "result": result, "start": d.get("start")}


def match_coach(gid, match_id):
    """The match's own coach conversation: its number (None before the first question), turns and status."""
    from . import coach
    gen = db.match_coach_gen(match_id)
    return {"gen": gen, "turns": db.coach_thread(gid, gen) if gen is not None else [],
            "status": coach.status(gid, gen) if gen is not None else None}


def run_coach(gid, body):
    from . import coach  # imported lazily so the server starts without the anthropic package
    g = coach.ALL_GAMES if gid == coach.ALL else db.get_game(gid)
    if not g:
        raise KeyError(gid)
    return coach.ask(g, body.get("preset") or "auto", body.get("question") or "", body.get("spoiler"),
                     match_id=body.get("match_id"), gen=body.get("gen"), new=bool(body.get("new")))


def conversations():
    from . import coach
    preset_title = {text: coach.PRESET_TITLES[k] for k, text in coach.PRESETS.items() if k in coach.PRESET_TITLES}
    lib = {g["id"]: g for g in db.library()}
    links, writing = db.coach_match_links(), coach.writing()
    rows = db.coach_conversations()
    have = {(c["game_id"], c["gen"]) for c in rows}
    # a match breakdown still being written has no turns yet, but should be in the list already
    rows += [{"game_id": g, "gen": n, "turns": 0, "first_ts": w["since"], "last_ts": w["since"], "first_q": w["question"],
              "title": None} for (g, n), w in writing.items() if (g, n) in links and (g, n) not in have]
    rows.sort(key=lambda c: -c["last_ts"])
    current, briefs = {}, {}
    out = []
    for c in rows:
        g = lib.get(c["game_id"])
        if c["game_id"] not in current:
            current[c["game_id"]] = db.coach_gen(c["game_id"])
        mid = links.get((c["game_id"], c["gen"]))
        if mid and mid not in briefs:
            m = db.get_match(mid)
            briefs[mid] = dict(match_brief(m["data"] if m else None), id=mid)
        q = " ".join((c["first_q"] or "").split())
        out.append({
            "game_id": c["game_id"], "gen": c["gen"], "turns": c["turns"], "first_ts": c["first_ts"], "last_ts": c["last_ts"],
            "title": c["title"] or preset_title.get(c["first_q"]) or (q[:80] + ("…" if len(q) > 80 else "")) or "Conversation",
            "renamed": bool(c["title"]), "current": c["gen"] == current[c["game_id"]] and not mid,
            "game_name": "All games" if c["game_id"] == "*" else (g or {}).get("name") or c["game_id"],
            "cover": art.for_game(g).get("cover") if g else None,
            "match": briefs.get(mid), "writing": writing.get((c["game_id"], c["gen"])),
        })
    return out


# ---------------- request plumbing ----------------

def _host_allowed(host):
    """Blocks DNS-rebinding: only names you'd actually type for this server are accepted."""
    host = (host or "").strip().lower()
    if host.startswith("["):
        host = host[1:host.find("]")]
    elif host.count(":") == 1:
        host = host.split(":")[0]
    if host == "localhost" or host.endswith(".ts.net") or (host and "." not in host and ":" not in host):
        return True  # localhost, Tailscale MagicDNS names, bare hostnames
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    return host in [h.lower() for h in config.get("allowed_hosts") or []]


class Handler(BaseHTTPRequestHandler):
    server_version = "GameTracker/0.1"

    def log_message(self, fmt, *args):
        pass  # keep the console for tracker events

    # --- helpers ---
    def _json(self, obj, status=200):
        body = json.dumps(obj, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 20 * 1024 * 1024:
            raise ValueError("body too large")
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def _agent_ok(self):
        auth = self.headers.get("Authorization", "")
        return auth.startswith("Bearer ") and hmac.compare_digest(auth[7:], config.agent_token())

    def _static(self, path):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        full = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not full.startswith(WEB_DIR) or not os.path.isfile(full):
            full = os.path.join(WEB_DIR, "index.html")
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(full)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _radar(self, name):
        from . import radar
        full = radar.path_for(name)
        if not full:
            return self._json({"error": "no radar image for that map"}, 404)
        self._file(full, "image/png", "public, max-age=604800")

    def _file(self, full, ctype, cache):
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def _dispatch(self, method):
        if not _host_allowed(self.headers.get("Host", "")):
            return self._json({"error": "host not allowed; add it to allowed_hosts"}, 403)
        url = urlparse(self.path)
        path, qs = url.path, parse_qs(url.query)
        try:
            if path.startswith("/api/agent/"):
                if not self._agent_ok():
                    return self._json({"error": "bad agent token"}, 401)
                return self._agent_api(method, path, qs)
            if path.startswith("/api/"):
                # Custom header => browsers must preflight cross-site requests, which we never approve.
                if method != "GET" and self.headers.get("X-GameTracker") != "1":
                    return self._json({"error": "missing X-GameTracker header"}, 403)
                return self._api(method, path, qs)
            if method == "GET" and path.startswith("/radar/") and path.endswith(".png"):
                return self._radar(path[len("/radar/"):-len(".png")])
            if method == "GET":
                return self._static(path)
            return self._json({"error": "not found"}, 404)
        except KeyError as e:
            return self._json({"error": f"not found: {e}"}, 404)
        except Exception as e:
            traceback.print_exc()
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    # --- agent API ---
    def _agent_api(self, method, path, qs):
        if path == "/api/agent/config" and method == "GET":
            return self._json(config.for_agent())
        if path == "/api/agent/games" and method == "GET":
            return self._json({"games": db.trackable_games()})
        if path == "/api/agent/library" and method == "POST":
            db.sync_games(self._body()["games"])
            art.refresh_async()  # new games get cover art in the background
            return self._json({"games": db.trackable_games()})
        if path == "/api/agent/heartbeat" and method == "POST":
            b = self._body()
            db.upsert_sessions(b.get("sessions") or [])
            with STATE.lock:
                STATE.agent = {"ts": time.time(), "host": b.get("host"),
                               "now_playing": b.get("now_playing") or [],
                               "sync_status": b.get("sync_status") or STATE.agent["sync_status"],
                               "capture": b.get("capture")}
            return self._json({"commands": STATE.take()})
        if path == "/api/agent/ingest" and method == "POST":
            b = self._body()
            for m in b.get("matches") or []:
                old = db.get_match(m.get("id")) if m.get("game_id") in improve.GAMES else None
                if old:
                    od, nd = old["data"] or {}, m.get("data") or {}
                    if od.get("video") and not nd.get("video"):
                        nd["video"] = od["video"]  # a re-download after the recording is gone keeps its frames' summary
                    if (nd.get("v") or 0) > (od.get("v") or 0):
                        improve.forget_reviews(m["id"])  # better facts: review it again (your feedback is kept)
            db.ingest(b.get("snapshots") or [], b.get("matches") or [])
            if any(m.get("game_id") == "steam:730" for m in b.get("matches") or []):
                from .integrations import cs2
                cs2.merge_demo_matches()  # a parsed demo replaces the live-tracked copy (and finds its video frames)
            for gid in {m.get("game_id") for m in b.get("matches") or []} & set(improve.GAMES):
                improve.run_async(gid)  # review the new deaths and update focus habits
            return self._json({"ok": True})
        if path == "/api/agent/transcripts" and method == "POST":
            kb.relay.deliver(self._body().get("results") or {})
            return self._json({"ok": True})
        if path == "/api/agent/frames" and method == "POST":
            b = self._body()
            try:
                improve.save_frames(str(b.get("match_id") or ""), str(b.get("game_id") or ""), b.get("round"),
                                    b.get("before") or [], b.get("images") or [], b.get("report"))
            except (ValueError, TypeError) as e:
                return self._json({"error": str(e)}, 400)
            return self._json({"ok": True})
        if path == "/api/agent/known_matches" and method == "GET":
            min_v = int(qs.get("min_v", ["0"])[0] or 0)
            return self._json({"ids": db.known_matches(qs.get("game_id", [""])[0], min_v=min_v)})
        return self._json({"error": "not found"}, 404)

    # --- dashboard API ---
    def _api(self, method, path, qs):
        m = re.fullmatch(r"/api/games/([^/]+)(/sync|/coach|/coach/new)?", path)
        if path == "/api/overview" and method == "GET":
            return self._json(overview())
        fm = re.fullmatch(r"/api/frames/([^/]+)/([^/]+)", path)
        if fm and method == "GET":
            full = improve.frame_file(fm.group(1), fm.group(2))
            return self._file(full, "image/jpeg", "private, max-age=86400") if full else self._json({"error": "no such frame"}, 404)
        if path == "/api/capture" and method == "GET":
            return self._json({"enabled": bool(config.get("video_evidence")), "online": STATE.online(),
                               "status": STATE.agent.get("capture") if STATE.online() else None})
        if path == "/api/capture/instant-replay" and method == "POST":
            STATE.push({"type": "capture", "action": "instant_replay"})
            return self._json({"queued": True, "agent_online": STATE.online()})
        if path == "/api/games" and method == "GET":
            return self._json({"games": [{**g, "art": art.for_game(g)} for g in db.library()]})
        if path == "/api/games" and method == "POST":
            b = self._body()
            if not b.get("name") or not b.get("path"):
                return self._json({"error": "name and path are required"}, 400)
            gid = db.add_custom_game(b["name"], b["path"])
            STATE.push({"type": "reload"})
            return self._json({"id": gid})
        if m:
            gid, action = unquote(m.group(1)), m.group(2)
            if action is None and method == "GET":
                d = game_detail(gid, (qs.get("mode") or [None])[0])
                return self._json(d) if d else self._json({"error": "unknown game"}, 404)
            if action is None and method == "POST":
                b = self._body()
                if "cover_url" in b or "hero_url" in b:
                    urls = {k: (b.get(f"{k}_url") or "").strip() for k in ("cover", "hero")}
                    if any(u and not re.match(r"https?://", u) for u in urls.values()):
                        return self._json({"error": "Image links must start with http:// or https://"}, 400)
                    b["art_override"] = {k: v for k, v in urls.items() if v} or None
                db.update_game(gid, **{k: b[k] for k in ("hidden", "tracked", "notes", "name", "art_override") if k in b})
                if "tracked" in b:
                    STATE.push({"type": "reload"})
                return self._json({"ok": True})
            if action is None and method == "DELETE":
                db.delete_game(gid)
                STATE.push({"type": "reload"})
                return self._json({"ok": True})
            if action == "/sync" and method == "POST":
                STATE.push({"type": "sync", "game_id": gid})
                return self._json({"queued": True, "agent_online": STATE.online()})
            if action == "/coach/new" and method == "POST":
                from . import coach
                g = db.get_game(gid)
                if not g:
                    return self._json({"error": "unknown game"}, 404)
                return self._json({"gen": coach.new_conversation(g)})
            if action == "/coach" and method == "POST":
                try:
                    return self._json(run_coach(gid, self._body()))
                except RuntimeError as e:  # friendly, user-fixable problems (no key, rate limit...)
                    return self._json({"error": str(e)}, 400)
        if path == "/api/coach/conversations" and method == "GET":
            from . import coach
            comp = lambda g: any(i.key in coach.COMPETITIVE and i.applies(g) for i in INTEGRATIONS)
            return self._json({"conversations": conversations(),
                               "games": [{"id": g["id"], "name": g["name"], "cover": art.for_game(g).get("cover"),
                                          "competitive": comp(g)} for g in db.library() if not g.get("hidden")]})
        cm = re.fullmatch(r"/api/coach/conversations/([^/]+)/(\d+)", path)
        if cm:
            cgid, gen = unquote(cm.group(1)), int(cm.group(2))
            if method == "GET":
                from . import coach
                return self._json({"turns": db.coach_thread(cgid, gen), "status": coach.status(cgid, gen)})
            if method == "POST":
                db.set_coach_title(cgid, gen, " ".join(str(self._body().get("title") or "").split())[:120])
                return self._json({"ok": True})
            if method == "DELETE":
                from . import coach
                coach.delete_conversation(cgid, gen)
                return self._json({"ok": True})
        if path == "/api/coach/ask" and method == "POST":
            b = self._body()
            try:
                return self._json(run_coach(str(b.get("game_id") or "*"), b))
            except KeyError:
                return self._json({"error": "That game isn't in the library any more."}, 404)
            except RuntimeError as e:
                return self._json({"error": str(e)}, 400)
        im = re.fullmatch(r"/api/games/([^/]+)/improve(/review|/focus|/feedback)?", path)
        if im:
            igid, sub = unquote(im.group(1)), im.group(2) or ""
            if igid not in improve.GAMES:
                return self._json({"error": "Improvement reviews are for VALORANT and CS2."}, 404)
            if not sub and method == "GET":
                return self._json(improve.summary(igid, full=True))
            if sub == "/review" and method == "POST":
                if not config.get("anthropic_api_key"):
                    return self._json({"error": "Add your Claude API key in Settings first."}, 400)
                if not improve.is_running(igid):
                    improve.run_async(igid)
                return self._json({"started": True})
            if sub == "/focus" and method == "POST":
                b = self._body()
                try:
                    (improve.drop_focus if b.get("drop") else improve.set_focus)(igid, str(b.get("habit") or ""))
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                return self._json(improve.summary(igid, full=True))
            if sub == "/feedback" and method == "POST":
                b = self._body()
                improve.set_feedback(str(b.get("match_id")), int(b.get("round", -1)), {1: 1, -1: -1}.get(b.get("value")))
                return self._json({"ok": True})
        km = re.fullmatch(r"/api/games/([^/]+)/kb(/refresh|/notes/\d+)?", path)
        if km:
            kgid, sub = unquote(km.group(1)), km.group(2) or ""
            g = db.get_game(kgid)
            if not g:
                return self._json({"error": "unknown game"}, 404)
            if not sub and method == "GET":
                notes = db.kb_notes(kgid)
                return self._json({"notes": notes, "videos": db.kb_videos(kgid), "summary": kb.summary(kgid),
                                   "game": {"id": kgid, "name": g["name"], "hero": art.for_game(g).get("hero")}})
            if sub == "/refresh" and method == "POST":
                b = self._body()
                urls = [u for u in (b.get("urls") or []) if isinstance(u, str)][:5]
                if not config.get("anthropic_api_key"):
                    return self._json({"error": "Add your Claude API key in Settings first."}, 400)
                if not urls and not config.get("youtube_api_key"):
                    return self._json({"error": "Add a YouTube API key in Settings to search for guides, "
                                                "or paste a video link to add one directly."}, 400)
                if urls and not any(kb.video_id(u) for u in urls):
                    return self._json({"error": "That doesn't look like a YouTube link."}, 400)
                if kb.is_running(kgid):
                    return self._json({"error": "Already looking for guides for this game."}, 409)
                kb.refresh_async(g, urls or None)
                return self._json({"started": True})
            if sub.startswith("/notes/") and method == "DELETE":
                db.kb_delete_note(kgid, int(sub.rsplit("/", 1)[1]))
                return self._json({"ok": True})
        dm = re.fullmatch(r"/api/games/([^/]+)/detail/([a-z0-9_-]+)", path)
        if dm and method == "GET":
            d = page_detail(unquote(dm.group(1)), dm.group(2))
            return self._json(d) if d else self._json({"error": "Nothing to show for that"}, 404)
        mm = re.fullmatch(r"/api/matches/([^/]+)", path)
        if mm and method == "GET":
            match = db.get_match(unquote(mm.group(1)))
            if not match:
                return self._json({"error": "unknown match"}, 404)
            game = db.get_game(match["game_id"]) or {}
            return self._json({"match": match, "game": {"id": match["game_id"], "name": game.get("name")},
                               "reviews": improve.reviewed(match["id"]), "coach": match_coach(match["game_id"], match["id"]),
                               "frames": improve.frames_for(dict(match["data"] or {}, _id=match["id"]))})
        if path == "/api/scan" and method == "POST":
            STATE.push({"type": "scan"})
            return self._json({"queued": True, "agent_online": STATE.online()})
        if path == "/api/settings" and method == "GET":
            return self._json(config.public())
        if path == "/api/settings" and method == "POST":
            config.update(self._body())
            STATE.push({"type": "config"})
            return self._json(config.public())
        if path == "/api/settings/agent-token" and method == "GET":
            return self._json({"token": config.agent_token()})
        return self._json({"error": "not found"}, 404)


def serve(host="127.0.0.1", port=None, background=True):
    """background=False skips the jobs that fetch from the internet (cover art, CS2 radars, guides); the demo does."""
    db.init()
    improve.init()
    config.agent_token()
    if background:
        art.refresh_async()
        from . import radar
        radar.ensure_async()
        kb.start_scheduler()
    port = port or int(config.get("web_port") or 8787)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True, name="http")
    t.start()
    return httpd
