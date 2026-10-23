"""The gaming-PC side: scans the library, tracks play, syncs stats, and reports to the server.

The server can be this same PC (default) or a home server reachable over Tailscale.
If the server is unreachable, session updates and stats are kept in an outbox and resent later.
"""
import heapq
import itertools
import json
import os
import socket
import threading
import time

import requests

from . import detect, net
from .config import DATA_DIR
from .integrations import all_integrations
from .tracker import Tracker

OUTBOX = os.path.join(DATA_DIR, "agent_outbox.json")


class ServerClient:
    """HTTP client for the server's /api/agent endpoints. Also the 'sink' integrations write to."""

    def __init__(self, url, token):
        self.url = url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}
        self.settings = {}  # server-provided settings integrations may need (BattleTag, Steam key)
        self.lock = threading.Lock()
        self.pending_sessions = {}
        self.pending_ingest = {"snapshots": [], "matches": []}
        try:
            with open(OUTBOX, encoding="utf-8") as f:
                saved = json.load(f)
            self.pending_sessions = saved.get("sessions", {})
            self.pending_ingest = saved.get("ingest", self.pending_ingest)
        except (OSError, ValueError):
            pass

    def _req(self, method, path, payload=None, timeout=15):
        r = net.http.request(method, self.url + path, json=payload, headers=self.headers, timeout=timeout)
        if r.status_code == 401:
            raise PermissionError("Server rejected the agent token. Check --token / Settings > Agent token.")
        r.raise_for_status()
        return r.json() if r.content else {}

    def _persist(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        if self.pending_sessions or self.pending_ingest["snapshots"] or self.pending_ingest["matches"]:
            with open(OUTBOX, "w", encoding="utf-8") as f:
                json.dump({"sessions": self.pending_sessions, "ingest": self.pending_ingest}, f)
        elif os.path.exists(OUTBOX):
            os.remove(OUTBOX)

    # --- sink API used by integrations ---
    def snapshot(self, game_id, kind, data):
        with self.lock:
            self.pending_ingest["snapshots"].append({"game_id": game_id, "kind": kind, "ts": time.time(), "data": data})

    def match(self, match_id, game_id, ts, data):
        with self.lock:
            self.pending_ingest["matches"].append({"id": match_id, "game_id": game_id, "ts": ts, "data": data})

    def known_matches(self, game_id, min_v=None):
        """IDs already stored; with min_v, only those stored in at least that summary version."""
        q = f"game_id={requests.utils.quote(game_id)}" + (f"&min_v={int(min_v)}" if min_v else "")
        return set(self._req("GET", f"/api/agent/known_matches?{q}")["ids"])

    def has_pending(self):
        from . import capture
        return bool(self.pending_ingest["snapshots"] or self.pending_ingest["matches"] or capture.has_pending())

    def flush_ingest(self):
        from . import capture
        # Video frames first, so a match's death review can already see them when the match lands.
        try:
            capture.upload_pending(lambda p: self._req("POST", "/api/agent/frames", p, timeout=60))
        except Exception:
            with self.lock:
                self._persist()
            raise
        with self.lock:
            batch = self.pending_ingest
            if not batch["snapshots"] and not batch["matches"]:
                return
            self.pending_ingest = {"snapshots": [], "matches": []}
        try:
            self._req("POST", "/api/agent/ingest", batch, timeout=30)
        except Exception:
            with self.lock:
                self.pending_ingest["snapshots"][:0] = batch["snapshots"]
                self.pending_ingest["matches"][:0] = batch["matches"]
                self._persist()
            raise
        with self.lock:
            self._persist()

    # --- agent lifecycle ---
    def heartbeat(self, session_updates, state):
        with self.lock:
            for s in session_updates:
                self.pending_sessions[s["uid"]] = s
            sessions = list(self.pending_sessions.values())
        try:
            resp = self._req("POST", "/api/agent/heartbeat", {"sessions": sessions, **state})
        except Exception:
            with self.lock:
                self._persist()
            raise
        with self.lock:
            for s in sessions:
                if self.pending_sessions.get(s["uid"]) is s:
                    del self.pending_sessions[s["uid"]]
            self._persist()
        return resp

    def transcripts(self, results):
        return self._req("POST", "/api/agent/transcripts", {"results": results}, timeout=60)

    def push_library(self, games):
        return self._req("POST", "/api/agent/library", {"games": games}, timeout=30)["games"]

    def trackable(self):
        return self._req("GET", "/api/agent/games")["games"]

    def config(self):
        self.settings = self._req("GET", "/api/agent/config")
        return self.settings


class Agent:
    def __init__(self, server_url, token):
        self.client = ServerClient(server_url, token)
        self.tracker = Tracker(os.path.join(DATA_DIR, "active_sessions.json"))
        self.integrations = all_integrations()
        self.cfg = {}
        self.sync_status = {}
        self.live = {}
        self._jobs = []
        self._seq = itertools.count()
        self._jobs_lock = threading.Lock()
        self._last_auto_sync = {}
        self._stop = threading.Event()
        self.host = socket.gethostname()
        self.recorder = None
        self.capture = None
        self._pid_games = {}

    # ---------- input reading (for improvement reviews) ----------
    INPUT_GAMES = ("riot:valorant", "steam:730")

    def _game_for_pid(self, pid):
        if pid in self._pid_games:
            return self._pid_games[pid]
        import psutil
        gid = None
        try:
            p = psutil.Process(pid)
            try:
                exe = p.exe()
            except psutil.Error:
                exe = None  # anti-cheat protected: match by name
            gid = self.tracker._match(p.name(), exe)
        except psutil.Error:
            pass
        if len(self._pid_games) > 500:
            self._pid_games.clear()
        self._pid_games[pid] = gid
        return gid

    def _input_games(self):
        return self.INPUT_GAMES if self.cfg.get("input_reading", True) else ()

    def start_inputs(self):
        from . import inputs
        self.recorder = inputs.Recorder(self._game_for_pid, self._input_games())
        if self.recorder.start():
            print("[agent] input reading: on for " + ", ".join(self.INPUT_GAMES) if self._input_games()
                  else "[agent] input reading: off (Settings)")

    # ---------- video evidence (NVIDIA App) ----------
    def start_capture(self):
        from . import capture
        from .integrations import cs2
        self.capture = capture.Capture()
        cs2.death_hook = self.capture.on_cs2_death
        self.capture.set_enabled(self.cfg.get("video_evidence"))
        st = self.capture.status
        print(f"[agent] video evidence: {'on' if st.get('enabled') else 'off (Settings)'}"
              + (f"; {st['problem']}" if st.get("enabled") and st.get("problem") else ""))

    # ---------- scheduling ----------
    def schedule(self, gid, delay, reason):
        run_at = time.time() + delay
        with self._jobs_lock:
            if any(j[2] == gid and abs(j[0] - run_at) < 15 for j in self._jobs):
                return
            heapq.heappush(self._jobs, (run_at, next(self._seq), gid, reason))

    def _integrations_for(self, game):
        return [i for i in self.integrations if i.applies(game)]

    def run_sync(self, gid, reason=""):
        if gid == "__scan__":
            return self.scan()
        game = self.tracker.games.get(gid)
        if not game:
            return
        for integ in self._integrations_for(game):
            key = f"{gid}|{integ.key}"
            try:
                msg = integ.sync(game, self.client)
                self.client.flush_ingest()
                self.sync_status[key] = {"ts": time.time(), "ok": True, "msg": msg, "reason": reason}
            except net.Unavailable as e:
                self.sync_status[key] = {"ts": time.time(), "ok": False, "msg": str(e), "reason": reason}
            except Exception as e:
                self.sync_status[key] = {"ts": time.time(), "ok": False, "msg": f"{type(e).__name__}: {e}",
                                         "reason": reason}
                print(f"[agent] {integ.key} sync failed for {gid}: {e}")

    def _worker(self):
        while not self._stop.is_set():
            job = None
            with self._jobs_lock:
                if self._jobs and self._jobs[0][0] <= time.time():
                    job = heapq.heappop(self._jobs)
            if job:
                self.run_sync(job[2], job[3])
            else:
                self._stop.wait(1)

    # ---------- library ----------
    def scan(self):
        games = detect.scan_all()
        trackable = self.client.push_library(games)
        self.tracker.set_games(trackable)
        print(f"[agent] library: {len(games)} installed, {len(trackable)} tracked")
        return trackable

    def reload(self):
        self.tracker.set_games(self.client.trackable())

    def _handle(self, cmd):
        kind = cmd.get("type")
        if kind == "sync":
            self.schedule(cmd["game_id"], 0, "manual")
        elif kind == "scan":
            self.schedule("__scan__", 0, "manual")
        elif kind == "reload":
            self.reload()
        elif kind == "config":
            self.cfg = self.client.config()
            if self.recorder:
                self.recorder.enabled = set(self._input_games())
            if self.capture:
                self.capture.set_enabled(self.cfg.get("video_evidence"))
        elif kind == "capture" and self.capture:
            threading.Thread(target=self.capture.command, args=(cmd.get("action"),), daemon=True).start()
        elif kind == "transcripts":
            threading.Thread(target=self._transcripts, args=(cmd.get("videos") or [],), daemon=True).start()

    def _transcripts(self, vids):
        """Download guide captions for the server's knowledge base: YouTube blocks the server, not this PC. The pacer
        spaces them out and caps them per day so this PC doesn't get blocked too."""
        from . import captions
        if not getattr(self, "pacer", None):
            self.pacer = captions.Pacer(os.path.join(DATA_DIR, "captions_pacing.json"))
        got = self.pacer.fetch_all([str(v) for v in vids][:20])
        results = {vid: {"status": status, "text": text} for vid, (status, text) in got.items()}
        statuses = [r["status"] for r in results.values()]
        if "blocked" in statuses:
            print("[agent] YouTube refused a caption download: pausing them for a day")
        try:
            self.client.transcripts(results)
            print(f"[agent] sent captions for {statuses.count('ok')} of {len(results)} guides"
                  + (f" ({statuses.count('capped')} over the daily limit)" if "capped" in statuses else "")
                  + (f" ({statuses.count('paused')} paused)" if "paused" in statuses else ""))
        except Exception as e:
            print(f"[agent] couldn't send guide captions: {type(e).__name__}: {e}")

    # ---------- main loop ----------
    def _connect(self):
        delay = 2
        while not self._stop.is_set():
            try:
                self.cfg = self.client.config()
                self.scan()
                return True
            except PermissionError as e:
                print(f"[agent] {e}")
                return False
            except Exception as e:
                print(f"[agent] waiting for server at {self.client.url} ({type(e).__name__}); retrying in {delay}s")
                self._stop.wait(delay)
                delay = min(delay * 2, 60)
        return False

    def run(self):
        if not self._connect():
            return
        try:
            self.start_inputs()
        except Exception as e:
            print(f"[agent] input reading unavailable: {type(e).__name__}: {e}")
        try:
            from .integrations import cs2
            print(f"[agent] CS2 live tracking: {cs2.start_gsi(DATA_DIR)}")
        except Exception as e:
            print(f"[agent] CS2 live tracking unavailable: {e}")
        try:
            self.start_capture()
        except Exception as e:
            print(f"[agent] video evidence unavailable: {type(e).__name__}: {e}")
        threading.Thread(target=self._worker, daemon=True, name="sync-worker").start()
        for i, g in enumerate(self.tracker.games.values()):  # catch up on anything played elsewhere/offline
            if self._integrations_for(g):
                self.schedule(g["id"], 3 + i, "startup")
        tick_no = 0
        while not self._stop.is_set():
            tick_no += 1
            try:
                self._tick(tick_no)
            except Exception as e:
                print(f"[agent] tick error: {type(e).__name__}: {e}")
            self._stop.wait(max(2, int(self.cfg.get("poll_interval_sec") or 5)))
        try:
            self.client.heartbeat(self.tracker.close_all(), {"now_playing": [], "host": self.host})
        except Exception:
            pass

    def _tick(self, tick_no):
        updates, started, ended = self.tracker.tick(int(self.cfg.get("min_session_sec") or 60))
        now = time.time()
        for gid in started:
            print(f"[agent] playing: {self.tracker.games[gid]['name']}")
            self._last_auto_sync[gid] = now
            if any(i.sync_on_start for i in self._integrations_for(self.tracker.games[gid])):
                self.schedule(gid, 0, "session start")
        for gid in ended:
            game = self.tracker.games.get(gid)
            print(f"[agent] stopped: {game['name'] if game else gid}")
            self.live.pop(gid, None)
            for integ in self._integrations_for(game or {"id": gid}):
                for d in integ.sync_delays:
                    self.schedule(gid, d, "session end")
        every = int(self.cfg.get("auto_sync_minutes") or 10) * 60
        for gid in list(self.tracker.active):
            game = self.tracker.games.get(gid)
            if not game:
                continue
            integs = self._integrations_for(game)
            if any(i.sync_while_playing for i in integs) and now - self._last_auto_sync.get(gid, now) >= every:
                self._last_auto_sync[gid] = now
                self.schedule(gid, 0, "periodic")
            if tick_no % 2 == 0:
                for integ in integs:
                    try:
                        live = integ.live(game)
                    except Exception:
                        live = None
                    if live:
                        self.live[gid] = live
        if self.capture:
            try:
                self.capture.tick(set(self.tracker.active))
            except Exception as e:
                print(f"[agent] video capture error: {type(e).__name__}: {e}")
        np = [{**p, "live": self.live.get(p["game_id"])} for p in self.tracker.now_playing()]
        state = {"now_playing": np, "host": self.host, "sync_status": self.sync_status}
        if self.capture:
            state["capture"] = self.capture.status
        try:
            resp = self.client.heartbeat(updates, state)
        except PermissionError:
            raise
        except Exception:
            return  # offline: updates stay in the outbox
        if self.client.has_pending():
            try:
                self.client.flush_ingest()
            except Exception:
                pass
        for cmd in resp.get("commands", []):
            self._handle(cmd)

    def stop(self):
        self._stop.set()
