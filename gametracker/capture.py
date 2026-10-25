"""Short video evidence for the death reviews, recorded by the NVIDIA App (gaming-PC side).

The NVIDIA App runs a local MCP server (127.0.0.1:13508, loopback only, needs the In-Game Overlay on). Through it:
- VALORANT: a recording starts when a match starts and stops when it ends (match state comes from the Riot client's
  own presence). After the match syncs, each death's last seconds are cut from it using the clock offset that lining
  up your kills with your clicks gives (inputs.align).
- CS2: an Instant Replay is saved the moment Game State Integration shows you died.

From each death the tracker keeps six small frames, cropped to the crosshair area, the minimap and the kill feed, plus
(VALORANT) the combat report as it shows during the next round's buy phase, and deletes the video. Only those frames leave the PC. Nothing here reads game memory or sends input to a game.
"""
import base64
import collections
import glob
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import queue

import requests

from .config import DATA_DIR

MCP_URL = "http://127.0.0.1:13508/mcp"
CAPTURE_DIR = os.path.join(DATA_DIR, "capture")
STATE_PATH = os.path.join(CAPTURE_DIR, "state.json")
OUTBOX = os.path.join(CAPTURE_DIR, "outbox")  # frames waiting for the server, one folder per match
VALORANT, CS2 = "riot:valorant", "steam:730"
BEFORE = (5.0, 3.8, 2.7, 1.7, 0.9, 0.2)  # seconds before the death for each frame
GSI_LAG = 0.3       # CS2 reports a death up to ~0.6 s late (throttle + buffer in the GSI cfg)
MIN_FREE_GB = 25    # a 40-minute match recorded at 1440p60 came to 8.5 GB
KEEP_HOURS = 12     # a match recording no synced match claimed by then is deleted
VAL_SKIP_QUEUES = {"deathmatch", "hurm", "ggteam"}  # no rounds, so no death reviews
VAL_SKIP_FLOWS = {"ShootingRange", "NewPlayerExperience", "ReplayNewPlayerExperience"}
CS2_MODES = {"competitive", "premier", "scrimcomp2v2", "casual"}
# HUD regions in units of the frame height (the HUD scales with height and hugs the corners, so this holds for
# 16:9, 16:10 and ultrawide): (edge, distance from that edge, top, width, height).
LAYOUT = {  # VALORANT checked against a real 1440p recording; CS2 not yet
    VALORANT: {"minimap": ("left", 0.0, 0.0, 0.46, 0.47), "feed": ("right", 0.52, 0.075, 0.52, 0.19),
               "report": ("right", 0.39, 0.14, 0.39, 0.72)},  # combat report: centred on the screen, grows both ways
    CS2: {"minimap": ("left", 0.0, 0.0, 0.34, 0.34), "feed": ("right", 0.52, 0.06, 0.52, 0.22)},
}
OUT_W, TOP_H, ROW_H = 768, 432, 220  # crosshair area on top (768x432), minimap and kill feed side by side below
REPORT_H = 800       # the combat report crop is scaled to this height (hit counts stay readable)
REPORT_LEAD = 25.0   # grab it this long before the next round's barriers drop: a few seconds into its buy phase

_instance = None
_outbox_lock = threading.Lock()


def active():
    """The running capture, when the Settings toggle is on."""
    return _instance if _instance and _instance.enabled else None


class NvError(Exception):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


# ---------------- NVIDIA App MCP client ----------------

class NvApp:
    """Just enough MCP (streamable HTTP, JSON-RPC) to call the NVIDIA App's tools."""

    def __init__(self, url=MCP_URL):
        self.url = url
        self.http = requests.Session()
        self.http.trust_env = False  # loopback: never through a proxy
        self.ids = itertools.count(1)
        self.sid = self.proto = None
        self.ready = False
        self.lock = threading.Lock()

    def _post(self, msg, timeout=10):
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.sid:
            headers["Mcp-Session-Id"] = self.sid
        if self.proto:
            headers["MCP-Protocol-Version"] = self.proto
        try:
            r = self.http.post(self.url, json=msg, headers=headers, timeout=timeout)
        except requests.RequestException:
            raise NvError("The NVIDIA App isn't running (its local server didn't answer).", "unreachable")
        if r.status_code == 404 and self.sid:  # session expired (the NVIDIA App restarted)
            self.ready, self.sid = False, None
            raise NvError("session expired", "expired")
        if r.status_code >= 400:
            raise NvError(f"The NVIDIA App answered HTTP {r.status_code}.", "http")
        if "id" not in msg:
            return None, r.headers
        if "text/event-stream" in r.headers.get("Content-Type", ""):
            reply = None
            for line in r.text.splitlines():
                if line.startswith("data:"):
                    try:
                        m = json.loads(line[5:])
                    except ValueError:
                        continue
                    if m.get("id") == msg["id"]:
                        reply = m
            if reply is None:
                raise NvError("No answer from the NVIDIA App.", "empty")
        else:
            reply = r.json()
        if reply.get("error"):
            raise NvError(reply["error"].get("message") or "MCP error", "rpc")
        return reply.get("result") or {}, r.headers

    def _connect(self):
        res, headers = self._post({"jsonrpc": "2.0", "id": next(self.ids), "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "gametracker", "version": "1"}}})
        self.sid = headers.get("Mcp-Session-Id")
        self.proto = res.get("protocolVersion")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.ready = True

    def call(self, tool, **args):
        with self.lock:
            for attempt in range(2):
                if not self.ready:
                    self._connect()
                try:
                    res, _ = self._post({"jsonrpc": "2.0", "id": next(self.ids), "method": "tools/call",
                                         "params": {"name": tool, "arguments": args}})
                    break
                except NvError as e:
                    if e.code != "expired" or attempt:
                        raise
        text = " ".join(c.get("text", "") for c in res.get("content") or [] if c.get("type") == "text").strip()
        if res.get("isError"):
            raise NvError(text or f"{tool} failed", (res.get("_meta") or {}).get("error_code"))
        if res.get("structuredContent") is not None:
            return res["structuredContent"]
        try:
            return json.loads(text)
        except ValueError:
            return {"text": text}


# ---------------- NVIDIA App settings (registry) ----------------

def _nvspcaps(name):
    if sys.platform != "win32":
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\NVIDIA Corporation\Global\ShadowPlay\NVSPCAPS") as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def save_dir():
    """Where the NVIDIA App saves videos (Settings > Recordings), one folder per game inside it."""
    raw = _nvspcaps("DefaultPathW")
    if isinstance(raw, bytes):
        path = raw.decode("utf-16-le", errors="ignore").split("\x00")[0]
        if path and os.path.isdir(path):
            return path
    return os.path.join(os.path.expanduser("~"), "Videos", "NVIDIA")


def replay_seconds():
    raw = _nvspcaps("DVRBufferLen")
    return int.from_bytes(raw[:4], "little") if isinstance(raw, bytes) and len(raw) >= 4 else None


# ---------------- video files and frames ----------------

def ffmpeg():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


def _run(args, timeout):
    flags = 0
    if sys.platform == "win32":  # quietly, and behind the game in CPU priority
        flags = subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW
    return subprocess.run(args, capture_output=True, text=True, errors="ignore", timeout=timeout, creationflags=flags)


def probe(path):
    """(duration in seconds, (width, height)) of a video."""
    err = _run([ffmpeg(), "-hide_banner", "-i", path], 30).stderr
    d = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", err)
    s = re.search(r"Video:.*?\b(\d{3,5})x(\d{3,5})\b", err)
    if not d or not s:
        raise ValueError(f"couldn't read {os.path.basename(path)}")
    return int(d[1]) * 3600 + int(d[2]) * 60 + float(d[3]), (int(s[1]), int(s[2]))


def _even(v, low=2):
    return max(low, int(v) // 2 * 2)


def _crop(spec, w, h):
    """A LAYOUT region as pixels (w, h, x, y) for a w x h video."""
    edge, x, y, cw, ch = spec
    cw, ch = min(cw * h, w), min(ch * h, h - y * h)
    left = x * h if edge == "left" else w - x * h
    return _even(cw), _even(ch), _even(max(0, min(left, w - cw)), 0), _even(y * h, 0)


def regions(game, w, h):
    """Pixel crops (w, h, x, y) for the crosshair area, minimap and kill feed, and the scaled row sizes below."""
    crop = lambda spec: _crop(spec, w, h)
    ch = _even(h * 0.5)
    cw = _even(min(w, ch * 16 / 9))
    center = (cw, ch, _even((w - cw) / 2, 0), _even((h - ch) / 2, 0))
    lay = LAYOUT[game]
    mm, kf = crop(lay["minimap"]), crop(lay["feed"])
    row = _even(min(ROW_H, OUT_W / (mm[0] / mm[1] + kf[0] / kf[1])))
    return center, mm, kf, row, _even(mm[0] * row / mm[1]), _even(kf[0] * row / kf[1])


def extract(path, times, game, out_dir):
    """One JPEG per time (seconds into the video) in out_dir: the crosshair area on top, minimap and kill feed below.
    Returns a path per time, None where that frame failed."""
    dur, (w, h) = probe(path)
    (cw, ch, cx, cy), (mw, mh, mx, my), (fw, fh, fx, fy), row, mw2, fw2 = regions(game, w, h)
    args = [ffmpeg(), "-hide_banner", "-loglevel", "error", "-y"]
    for t in times:
        args += ["-ss", f"{max(0.0, min(t, dur - 0.1)):.3f}", "-i", path]
    graph = [f"[{i}:v]split=3[c{i}][m{i}][f{i}];"
             f"[c{i}]crop={cw}:{ch}:{cx}:{cy},scale={OUT_W}:{TOP_H}[t{i}];"
             f"[m{i}]crop={mw}:{mh}:{mx}:{my},scale={mw2}:{row}[mm{i}];"
             f"[f{i}]crop={fw}:{fh}:{fx}:{fy},scale={fw2}:{row}[kf{i}];"
             f"[mm{i}][kf{i}]hstack=inputs=2,pad={OUT_W}:{row}:0:0:color=0x101418[r{i}];"
             f"[t{i}][r{i}]vstack=inputs=2[o{i}]" for i in range(len(times))]
    args += ["-filter_complex", ";".join(graph)]
    outs = [os.path.join(out_dir, f"f{i}.jpg") for i in range(len(times))]
    for i, o in enumerate(outs):
        args += ["-map", f"[o{i}]", "-frames:v", "1", "-q:v", "5", "-update", "1", o]
    res = _run(args, 180)
    if res.returncode != 0:
        print(f"[capture] ffmpeg: {res.stderr.strip()[-300:]}")
    return [o if os.path.exists(o) and os.path.getsize(o) > 0 else None for o in outs]


def extract_report(path, t, game, out):
    """The combat report panel t seconds into the video as one JPEG at out, or None (no layout, or ffmpeg failed)."""
    spec = LAYOUT[game].get("report")
    if not spec:
        return None
    dur, (w, h) = probe(path)
    cw, ch, cx, cy = _crop(spec, w, h)
    res = _run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{max(0.0, min(t, dur - 0.1)):.3f}",
                "-i", path, "-vf", f"crop={cw}:{ch}:{cx}:{cy},scale=-2:{min(REPORT_H, ch)}", "-frames:v", "1",
                "-q:v", "4", "-update", "1", out], 120)
    if res.returncode != 0:
        print(f"[capture] ffmpeg (combat report): {res.stderr.strip()[-300:]}")
    return out if os.path.exists(out) and os.path.getsize(out) > 0 else None


def _ready(path):
    """The NVIDIA App has finished writing the file: it opens and its size holds still."""
    try:
        a = os.path.getsize(path)
        with open(path, "rb"):
            pass
        time.sleep(1.5)
        return a > 0 and os.path.getsize(path) == a
    except OSError:
        return False


def find_video(since, replay, claimed=(), timeout=90):
    """The first video the NVIDIA App wrote after `since`. Instant Replays end in .DVR.mp4; after half the wait any
    new video counts, in case the naming differs."""
    root, end = save_dir(), time.time() + timeout
    while time.time() < end:
        loose = time.time() > end - timeout / 2
        new = []
        for p in glob.glob(os.path.join(root, "**", "*.mp4"), recursive=True):
            if p in claimed:
                continue
            try:
                mt = os.path.getmtime(p)
            except OSError:
                continue
            if mt >= since and (loose or (".dvr." in os.path.basename(p).lower()) == replay):
                new.append((mt, p))
        for _, p in sorted(new):
            if _ready(p):
                return p
        time.sleep(2)
    return None


def _delete_video(path):
    """Only ever files inside the NVIDIA save folder that the tracker itself had saved."""
    try:
        if path and os.path.commonpath([os.path.abspath(path), os.path.abspath(save_dir())]) == os.path.abspath(save_dir()):
            os.remove(path)
    except (OSError, ValueError) as e:
        print(f"[capture] couldn't delete {path}: {e}")


# ---------------- frames outbox (sent to the server by the agent) ----------------

def _safe(match_id):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", match_id)


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def outbox_put(match_id, game_id, round_n, before, jpgs, report=None):
    d = os.path.join(OUTBOX, _safe(match_id))
    with _outbox_lock:
        os.makedirs(d, exist_ok=True)
        for i, p in enumerate(jpgs):
            shutil.move(p, os.path.join(d, f"r{round_n}_{i}.jpg"))
        if report:
            shutil.move(report, os.path.join(d, f"r{round_n}_report.jpg"))
        meta = _read_json(os.path.join(d, "meta.json"), {"match_id": match_id, "game_id": game_id, "deaths": {}})
        meta["deaths"][str(round_n)] = {"before": list(before), "n": len(jpgs), "report": bool(report), "ts": time.time()}
        _write_json(os.path.join(d, "meta.json"), meta)


def has_pending():
    return os.path.isdir(OUTBOX) and any(os.scandir(OUTBOX))


def upload_pending(post):
    """Send waiting frames to the server, one death per request; post(payload) raises if the server can't take it."""
    if not os.path.isdir(OUTBOX):
        return 0
    sent = 0
    for d in [e.path for e in os.scandir(OUTBOX) if e.is_dir()]:
        with _outbox_lock:
            meta = _read_json(os.path.join(d, "meta.json"), None)
            if not meta or not meta.get("deaths"):
                shutil.rmtree(d, ignore_errors=True)
                continue
            for rn, info in list(meta["deaths"].items()):
                files = [os.path.join(d, f"r{rn}_{i}.jpg") for i in range(info["n"])]
                images, report = [], None
                for p in files:
                    try:
                        with open(p, "rb") as f:
                            images.append(base64.b64encode(f.read()).decode())
                    except OSError:
                        pass
                if info.get("report"):
                    files.append(os.path.join(d, f"r{rn}_report.jpg"))
                    try:
                        with open(files[-1], "rb") as f:
                            report = base64.b64encode(f.read()).decode()
                    except OSError:
                        pass
                if images:
                    try:
                        post({"match_id": meta["match_id"], "game_id": meta["game_id"], "round": int(rn),
                              "before": info["before"][-len(images):] if len(images) < len(info["before"]) else info["before"],
                              "images": images, **({"report": report} if report else {})})
                        sent += 1
                    except requests.HTTPError as e:  # the server refused these frames: drop them, don't block the rest
                        code = e.response.status_code if e.response is not None else 0
                        if not 400 <= code < 500:
                            raise
                        print(f"[capture] server refused frames for {meta['match_id']} round {int(rn) + 1}: HTTP {code}")
                for p in files:
                    try:
                        os.remove(p)
                    except OSError:
                        pass
                del meta["deaths"][rn]
                _write_json(os.path.join(d, "meta.json"), meta)
            shutil.rmtree(d, ignore_errors=True)
    return sent


# ---------------- the capture manager ----------------

class Capture:
    def __init__(self):
        global _instance
        self.nv = NvApp()
        self.enabled = False
        self.lock = threading.Lock()
        os.makedirs(CAPTURE_DIR, exist_ok=True)
        self.state = _read_json(STATE_PATH, {})
        self.state.setdefault("rec", None)       # the match recording in progress
        self.state.setdefault("stopping", [])    # recordings stopped, file not found yet
        self.state.setdefault("clips", [])       # finished match recordings waiting for their match to sync
        self.events = collections.deque(self.state.get("events") or [], maxlen=8)
        self.status = {"enabled": False}
        self.claimed = set()
        self.jobs = queue.Queue()
        self._status_at = self._pruned_at = 0
        threading.Thread(target=self._worker, daemon=True, name="capture").start()
        for s in self.state["stopping"]:
            self.jobs.put(("locate", s))
        _instance = self

    # -- bookkeeping --
    def _save(self):
        with self.lock:
            self.state["events"] = list(self.events)
            _write_json(STATE_PATH, self.state)

    def _event(self, text):
        print(f"[capture] {text}")
        self.events.append({"ts": time.time(), "text": text})
        self.status["events"] = list(self.events)

    def refresh_status(self):
        st = {"enabled": self.enabled, "checked": time.time(), "ffmpeg": bool(ffmpeg()), "replay_seconds": replay_seconds(),
              "recording_match": bool(self.state.get("rec") and self.state["rec"].get("ours")), "events": list(self.events)}
        try:
            s = self.nv.call("nvapp_overlay_get_status")
            st.update(available=True, problem=None, instant_replay=bool(s.get("instantReplayEnabled")),
                      recording=bool(s.get("recordingActive")))
        except NvError as e:
            st.update(available=False, problem=str(e))
        self.status, self._status_at = st, time.time()
        return st

    def set_enabled(self, on):
        on = bool(on)
        if on == self.enabled:
            return
        self.enabled = on
        rec = self.state.get("rec")
        if not on and rec and rec.get("ours"):
            self._stop_recording(discard=True)
        if on:
            self._reconcile()
        self.refresh_status()

    def _reconcile(self):
        """After a restart: forget a recording the NVIDIA App is no longer making."""
        rec = self.state.get("rec")
        if rec and rec.get("ours"):
            try:
                if not self.nv.call("nvapp_overlay_get_status").get("recordingActive"):
                    self.state["rec"] = None
                    self._save()
            except NvError:
                pass

    def command(self, action):
        """Things the dashboard asks for explicitly (the tracker never flips NVIDIA settings on its own)."""
        if action == "instant_replay":
            try:
                self.nv.call("nvapp_overlay_capture", action="toggle_instant_replay", enable=True)
                self._event("Turned on Instant Replay, as asked from the dashboard")
            except NvError as e:
                self._event(f"Couldn't turn on Instant Replay: {e}")
        self.refresh_status()

    # -- called every agent tick --
    def tick(self, active_games):
        now = time.time()
        if not self.enabled:
            return
        if now - self._status_at > 60:
            self.refresh_status()
        if now - self._pruned_at > 600:
            self._pruned_at = now
            self._prune()
        rec = self.state.get("rec")
        if VALORANT in active_games:
            from .integrations.valorant import match_state
            st = match_state()
            if st is None:
                return  # can't tell right now; change nothing
            in_match = st["loop"] == "INGAME" and st["queue"] not in VAL_SKIP_QUEUES and st["flow"] not in VAL_SKIP_FLOWS
            if in_match and not rec:
                self._start_recording(VALORANT, st)
            elif rec and rec["game"] == VALORANT and st["loop"] != "INGAME":
                self._stop_recording()
        elif rec and rec["game"] == VALORANT:
            self._stop_recording()

    def _start_recording(self, game, st):
        rec = {"game": game, "ours": False, "t_req": time.time(), "queue": st.get("queue"), "map": st.get("map")}
        try:
            status = self.nv.call("nvapp_overlay_get_status")
            free = shutil.disk_usage(save_dir()).free / 1e9
            if status.get("recordingActive"):
                self._event("A recording was already running, so the tracker left it alone for this match")
            elif free < MIN_FREE_GB:
                self._event(f"Not recording this match: only {free:.0f} GB free where the NVIDIA App saves videos")
            else:
                rec["t_req"] = time.time()
                self.nv.call("nvapp_overlay_capture", action="toggle_recording", enable=True)
                rec.update(ours=True, t_ack=time.time())
                self._event(f"Recording a VALORANT {st.get('queue') or 'custom'} match")
        except NvError as e:
            self._event(f"Couldn't start recording: {e}")
        self.state["rec"] = rec  # remembered either way, so it isn't retried every few seconds
        self._save()

    def _stop_recording(self, discard=False):
        rec, self.state["rec"] = self.state.get("rec"), None
        if rec and rec.get("ours"):
            t_req = time.time()
            try:
                self.nv.call("nvapp_overlay_capture", action="toggle_recording", enable=False)
            except NvError as e:
                self._event(f"Couldn't stop the recording: {e}")
            rec.update(t_stop_req=t_req, t_stop_ack=time.time(), discard=discard)
            self.state["stopping"].append(rec)
            self.jobs.put(("locate", rec))
        self._save()

    # -- background work --
    def _worker(self):
        while True:
            kind, job = self.jobs.get()
            try:
                if kind == "locate":
                    self._locate(job)
                elif kind == "replay":
                    self._replay(job)
            except Exception as e:
                self._event(f"{kind} failed: {type(e).__name__}: {e}")

    def _locate(self, rec):
        # The finished file is written when recording stops, so anything older (your own clips) is never picked.
        path = find_video(rec["t_stop_req"] - 10, replay=False, claimed=self.claimed, timeout=120)
        with self.lock:
            self.state["stopping"] = [s for s in self.state["stopping"] if s is not rec and s.get("t_req") != rec["t_req"]]
        if not path:
            self._event("The match recording didn't show up in the NVIDIA save folder")
        elif rec.get("discard"):
            _delete_video(path)
        else:
            self.claimed.add(path)
            dur, _ = probe(path)
            # The file ends when recording stopped; its start is worked back from the length.
            end = rec["t_stop_req"]
            self.state["clips"].append({"game": rec["game"], "path": path, "start": end - dur, "end": end, "dur": dur,
                                        "t_req": rec["t_req"], "t_ack": rec.get("t_ack"), "t_stop_ack": rec["t_stop_ack"]})
            self._event(f"Match recording saved ({dur / 60:.0f} min); frames are cut once the match syncs")
        self._save()

    def _prune(self):
        clips = sorted(self.state["clips"], key=lambda c: c["end"])
        keep = [c for c in clips if time.time() - c["end"] < KEEP_HOURS * 3600][-2:]
        for c in clips:
            if c not in keep:
                _delete_video(c["path"])
        self.state["clips"] = keep
        if os.path.isdir(OUTBOX):  # the server has been unreachable for two weeks: give up on those frames
            for e in os.scandir(OUTBOX):
                if e.is_dir() and time.time() - e.stat().st_mtime > 14 * 86400:
                    shutil.rmtree(e.path, ignore_errors=True)
        self._save()

    def _frames(self, path, game, match_id, round_n, times, report_t=None):
        """Cut a death's frames (and its combat report, at report_t seconds into the video) into the outbox.
        Returns (frames cut, whether the report was)."""
        with tempfile.TemporaryDirectory(dir=CAPTURE_DIR) as tmp:
            outs = extract(path, times, game, tmp)
            got = [(b, p) for b, p in zip(BEFORE, outs) if p]
            report = extract_report(path, report_t, game, os.path.join(tmp, "report.jpg")) if report_t is not None else None
            if got:
                outbox_put(match_id, game, round_n, [b for b, _ in got], [p for _, p in got], report)
            return len(got), bool(got and report)

    # -- VALORANT: called from the match sync --
    def attach_match(self, game, match_id, t0, t1, deaths, reports=None):
        """Cut frames for a synced match's deaths from its recording, then delete the recording.

        deaths: [(round index, PC time of death)], or None when the match couldn't be lined up with the PC clock.
        reports: {round index: PC time to grab that death's combat report} (in the next round's buy phase).
        Returns a small summary for the match record, {"status": "pending"} while its recording is still being saved,
        or None when no recording covers the match."""
        rec = self.state.get("rec")
        if rec and rec["game"] == game and rec.get("ours") and rec.get("t_ack", 0) < t1 and time.time() - t1 < 900:
            return {"status": "pending"}
        if any(s["game"] == game and s["t_req"] < t1 for s in self.state["stopping"]) and time.time() - t1 < 900:
            return {"status": "pending"}
        overlap = lambda c: min(c["end"], t1) - max(c["start"], t0)
        clips = [c for c in self.state["clips"] if c["game"] == game and overlap(c) > min(60, c["dur"] / 2)]
        if not clips:
            return None
        clip = max(clips, key=overlap)
        n = nrep = 0
        if deaths is None:
            self._event("A recorded VALORANT match couldn't be lined up with the PC clock (too few kills or no "
                        "input data), so no frames were cut")
        else:
            for rn, t in deaths:
                if clip["start"] + BEFORE[0] <= t <= clip["end"]:
                    rt = (reports or {}).get(rn)
                    rt = rt - clip["start"] if rt is not None and clip["start"] <= rt <= clip["end"] else None
                    try:
                        got, rep = self._frames(clip["path"], game, match_id, rn, [t - b - clip["start"] for b in BEFORE], rt)
                        n, nrep = n + bool(got), nrep + rep
                    except Exception as e:
                        print(f"[capture] frames for round {rn + 1} failed: {type(e).__name__}: {e}")
            self._event(f"VALORANT: frames for {n} of {len(deaths)} deaths, {nrep} with the combat report; recording deleted")
        _delete_video(clip["path"])
        self.state["clips"] = [c for c in self.state["clips"] if c is not clip]
        self._save()
        return {"status": "ok" if deaths is not None else "not_aligned", "deaths": n}

    # -- CS2: called from Game State Integration --
    def on_cs2_death(self, match_id, round_n, t, mode):
        if not self.enabled or mode not in CS2_MODES:
            return
        threading.Thread(target=self._save_replay, args=(match_id, round_n, t - GSI_LAG), daemon=True).start()

    def _save_replay(self, match_id, round_n, t):
        if self.status.get("instant_replay") is not True:
            self.refresh_status()
            if self.status.get("instant_replay") is not True:
                self._event(f"CS2 round {round_n + 1} death not saved: Instant Replay is off")
                return
        t_req = time.time()
        try:
            self.nv.call("nvapp_overlay_capture", action="save_instant_replay")
        except NvError as e:
            self._event(f"Couldn't save an Instant Replay: {e}")
            return
        self.jobs.put(("replay", {"match_id": match_id, "round": round_n, "t": t, "t_req": t_req}))

    def _replay(self, job):
        path = find_video(job["t_req"] - 1, replay=True, claimed=self.claimed, timeout=60)
        if not path:
            self._event(f"CS2 round {job['round'] + 1}: the Instant Replay didn't show up in the NVIDIA save folder")
            return
        self.claimed.add(path)
        try:
            dur, _ = probe(path)
            death = dur - (job["t_req"] - job["t"])  # the replay ends when it was saved
            n = self._frames(path, CS2, job["match_id"], job["round"], [death - b for b in BEFORE])[0] if death > 1 else 0
            self._event(f"CS2 round {job['round'] + 1}: {n} frames saved; replay deleted")
        finally:
            _delete_video(path)
