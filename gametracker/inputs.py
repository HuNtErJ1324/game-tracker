"""Reads how you move and shoot while a supported shooter is in focus, for the improvement reviews.

Uses Windows Raw Input in passive mode (RIDEV_INPUTSINK on a message-only window): nothing is hooked or injected
and no input is ever sent, the same way voice-chat push-to-talk works. Only movement, crouch, walk and jump keys and
the ability and weapon-slot keys (so ability casts, knife swings and plants aren't counted as shots), and the mouse
buttons and movement are recorded, only while the game's window is in front, so nothing you type can be read back. Logs stay on this PC (data/inputs) and are deleted after two weeks; matches only get small summaries.

Log lines: "k,<ms>,<key>,<1|0>" key down/up · "b,<ms>,<L|R>,<1|0>" mouse button · "m,<ms>,<dx>,<dy>" mouse movement
summed per 25 ms · "f,<ms>,<game id or ->" which game had focus.
"""
import bisect
import glob
import os
import sys
import threading
import time

from .config import DATA_DIR

INPUT_DIR = os.path.join(DATA_DIR, "inputs")
KEEP_DAYS = 14
BIN_MS = 25
# Virtual-key codes: W A S D move, Ctrl crouch, Shift walk, Space jump (the defaults in VALORANT and CS2);
# C Q E X abilities and 1-4 weapon slots (VALORANT defaults), to tell gunfire from ability casts and knife swings.
KEYS = {0x57: "w", 0x41: "a", 0x53: "s", 0x44: "d", 0x11: "c", 0x10: "h", 0x20: "j",
        0x43: "u1", 0x51: "u2", 0x45: "u3", 0x58: "u4", 0x31: "g1", 0x32: "g2", 0x33: "g3", 0x34: "g4"}
ABILITY = ("u1", "u2", "u3", "u4")
SLOT = {"g1": "gun", "g2": "gun", "g3": "knife", "g4": "spike"}
STARTING_MS = 150  # a movement key pressed this recently hasn't built up speed yet
ABILITY_SLOT = {"u1": "c", "u2": "q", "u3": "e", "u4": "x"}  # VALORANT: C grenade, Q ability 1, E ability 2, X ultimate
MOVE = ("w", "a", "s", "d")
OPPOSITE = {"w": "s", "s": "w", "a": "d", "d": "a"}


# ---------------- recording (gaming PC) ----------------

class Recorder:
    def __init__(self, game_for_pid, enabled_games):
        """game_for_pid(pid) -> game id or None; enabled_games: ids to record (VALORANT, CS2)."""
        self.game_for_pid = game_for_pid
        self.enabled = set(enabled_games)
        self.focus = None
        self.focus_checked = 0.0
        self.held = set()
        self.bin_start, self.bin_dx, self.bin_dy = 0, 0, 0
        self.buf, self.lock = [], threading.Lock()
        self.file_hour, self.fh = None, None
        self.thread = None

    # -- writing --
    def _emit(self, line):
        with self.lock:
            self.buf.append(line)

    def _flush(self):
        with self.lock:
            lines, self.buf = self.buf, []
        if not lines:
            return
        hour = time.strftime("%Y%m%d-%H")
        if hour != self.file_hour:
            if self.fh:
                self.fh.close()
            os.makedirs(INPUT_DIR, exist_ok=True)
            self.fh = open(os.path.join(INPUT_DIR, f"inputs-{hour}.log"), "a", encoding="ascii")
            self.file_hour = hour
        self.fh.write("\n".join(lines) + "\n")
        self.fh.flush()

    def _flusher(self):
        last_prune = 0
        while True:
            time.sleep(2)
            try:
                self._flush()
                if time.time() - last_prune > 3600:
                    prune()
                    last_prune = time.time()
            except Exception as e:
                print(f"[inputs] couldn't write: {e}")

    # -- focus --
    def _focused_game(self, now):
        if now - self.focus_checked < 1.0:
            return self.focus
        self.focus_checked = now
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        gid = self.game_for_pid(pid.value) if pid.value else None
        gid = gid if gid in self.enabled else None
        if gid != self.focus:
            if self.focus:  # left the game: forget held keys, close the mouse bin
                self._end_bin()
                for k in sorted(self.held):
                    self._emit(f"k,{int(now * 1000)},{k},0")
                self.held.clear()
            self.focus = gid
            self._emit(f"f,{int(now * 1000)},{gid or '-'}")
        return gid

    def _end_bin(self):
        if self.bin_dx or self.bin_dy:
            self._emit(f"m,{self.bin_start},{self.bin_dx},{self.bin_dy}")
        self.bin_dx = self.bin_dy = 0

    # -- raw input events --
    def on_mouse(self, buttons, dx, dy):
        now = time.time()
        if not self._focused_game(now):
            return
        ms = int(now * 1000)
        if dx or dy:
            b = ms - ms % BIN_MS
            if b != self.bin_start:
                self._end_bin()
                self.bin_start = b
            self.bin_dx += dx
            self.bin_dy += dy
        for flag, name, down in ((0x1, "L", 1), (0x2, "L", 0), (0x4, "R", 1), (0x8, "R", 0)):
            if buttons & flag:
                self._emit(f"b,{ms},{name},{down}")

    def on_key(self, vkey, up):
        now = time.time()
        name = KEYS.get(vkey)
        if not name or not self._focused_game(now):
            return
        if up:
            if name in self.held:
                self.held.discard(name)
                self._emit(f"k,{int(now * 1000)},{name},0")
        elif name not in self.held:  # ignore key-repeat
            self.held.add(name)
            self._emit(f"k,{int(now * 1000)},{name},1")

    # -- Windows plumbing --
    def start(self):
        if sys.platform != "win32":
            return False
        threading.Thread(target=self._flusher, daemon=True, name="inputs-flush").start()
        self.thread = threading.Thread(target=self._run, daemon=True, name="inputs")
        self.thread.start()
        return True

    def _run(self):
        import ctypes
        from ctypes import wintypes

        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

        class RAWINPUTDEVICE(ctypes.Structure):
            _fields_ = [("usUsagePage", wintypes.USHORT), ("usUsage", wintypes.USHORT),
                        ("dwFlags", wintypes.DWORD), ("hwndTarget", wintypes.HWND)]

        class RAWINPUTHEADER(ctypes.Structure):
            _fields_ = [("dwType", wintypes.DWORD), ("dwSize", wintypes.DWORD),
                        ("hDevice", wintypes.HANDLE), ("wParam", wintypes.WPARAM)]

        class _BTN(ctypes.Structure):
            _fields_ = [("usButtonFlags", wintypes.USHORT), ("usButtonData", wintypes.USHORT)]

        class _BTNU(ctypes.Union):
            _fields_ = [("ulButtons", wintypes.ULONG), ("b", _BTN)]

        class RAWMOUSE(ctypes.Structure):
            _anonymous_ = ("u",)
            _fields_ = [("usFlags", wintypes.USHORT), ("u", _BTNU), ("ulRawButtons", wintypes.ULONG),
                        ("lLastX", wintypes.LONG), ("lLastY", wintypes.LONG), ("ulExtraInformation", wintypes.ULONG)]

        class RAWKEYBOARD(ctypes.Structure):
            _fields_ = [("MakeCode", wintypes.USHORT), ("Flags", wintypes.USHORT), ("Reserved", wintypes.USHORT),
                        ("VKey", wintypes.USHORT), ("Message", wintypes.UINT), ("ExtraInformation", wintypes.ULONG)]

        class _DATA(ctypes.Union):
            _fields_ = [("mouse", RAWMOUSE), ("keyboard", RAWKEYBOARD)]

        class RAWINPUT(ctypes.Structure):
            _fields_ = [("header", RAWINPUTHEADER), ("data", _DATA)]

        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = LRESULT
        user32.GetRawInputData.argtypes = [wintypes.HANDLE, wintypes.UINT, ctypes.c_void_p,
                                           ctypes.POINTER(wintypes.UINT), wintypes.UINT]
        user32.GetRawInputData.restype = wintypes.UINT
        user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                           wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.RegisterRawInputDevices.argtypes = [ctypes.POINTER(RAWINPUTDEVICE), wintypes.UINT, wintypes.UINT]

        WM_INPUT, RID_INPUT, RIDEV_INPUTSINK, MOUSE_MOVE_ABSOLUTE, RI_KEY_BREAK = 0x00FF, 0x10000003, 0x100, 0x1, 0x1
        raw = RAWINPUT()
        size = wintypes.UINT(ctypes.sizeof(raw))
        hdr = ctypes.sizeof(RAWINPUTHEADER)

        def wndproc(hwnd, msg, wparam, lparam):
            if msg == WM_INPUT:
                size.value = ctypes.sizeof(raw)
                if user32.GetRawInputData(lparam, RID_INPUT, ctypes.byref(raw), ctypes.byref(size), hdr) != 0xFFFFFFFF:
                    try:
                        if raw.header.dwType == 0:  # mouse
                            m = raw.data.mouse
                            rel = not (m.usFlags & MOUSE_MOVE_ABSOLUTE)
                            self.on_mouse(m.b.usButtonFlags, m.lLastX if rel else 0, m.lLastY if rel else 0)
                        elif raw.header.dwType == 1:  # keyboard
                            k = raw.data.keyboard
                            self.on_key(k.VKey, bool(k.Flags & RI_KEY_BREAK))
                    except Exception as e:  # never let a bad event kill the message loop
                        print(f"[inputs] {type(e).__name__}: {e}")
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc = WNDPROC(wndproc)  # keep a reference so it isn't garbage-collected
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW(lpfnWndProc=self._wndproc, hInstance=hinst, lpszClassName="GameTrackerInputs")
        user32.RegisterClassW(ctypes.byref(wc))
        hwnd = user32.CreateWindowExW(0, "GameTrackerInputs", "GameTracker inputs", 0, 0, 0, 0, 0,
                                      wintypes.HWND(-3), None, hinst, None)  # HWND_MESSAGE: invisible
        devs = (RAWINPUTDEVICE * 2)(RAWINPUTDEVICE(1, 2, RIDEV_INPUTSINK, hwnd), RAWINPUTDEVICE(1, 6, RIDEV_INPUTSINK, hwnd))
        if not user32.RegisterRawInputDevices(devs, 2, ctypes.sizeof(RAWINPUTDEVICE)):
            print("[inputs] couldn't register for raw input; input reading is off")
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))


def prune():
    cutoff = time.time() - KEEP_DAYS * 86400
    for p in glob.glob(os.path.join(INPUT_DIR, "inputs-*.log")):
        if os.path.getmtime(p) < cutoff:
            os.remove(p)


# ---------------- reading back ----------------

class Log:
    """Inputs between two times (unix seconds), as sorted per-kind lists in milliseconds."""

    def __init__(self, t0, t1, game=None):
        self.t0, self.t1 = int(t0 * 1000), int(t1 * 1000)
        self.game = game
        self.keys = []      # (ms, key, down)
        self.buttons = []   # (ms, button, down)
        self.mouse = []     # (ms, dx, dy)
        self.focus = []     # (ms, game id or None)
        first, last = time.strftime("%Y%m%d-%H", time.localtime(t0 - 3600)), time.strftime("%Y%m%d-%H", time.localtime(t1 + 3600))
        for p in sorted(glob.glob(os.path.join(INPUT_DIR, "inputs-*.log"))):
            stamp = os.path.basename(p)[7:-4]
            if not first <= stamp <= last:
                continue
            with open(p, encoding="ascii", errors="ignore") as f:
                for line in f:
                    parts = line.rstrip("\n").split(",")
                    try:
                        ms = int(parts[1])
                    except (IndexError, ValueError):
                        continue
                    if not self.t0 <= ms <= self.t1:
                        continue
                    kind = parts[0]
                    if kind == "k":
                        self.keys.append((ms, parts[2], parts[3] == "1"))
                    elif kind == "b":
                        self.buttons.append((ms, parts[2], parts[3] == "1"))
                    elif kind == "m":
                        self.mouse.append((ms, int(parts[2]), int(parts[3])))
                    elif kind == "f":
                        self.focus.append((ms, None if parts[2] == "-" else parts[2]))
        for lst in (self.keys, self.buttons, self.mouse, self.focus):
            lst.sort(key=lambda e: e[0])
        if game:  # only moments when that game was in front
            self.buttons = [e for e in self.buttons if self.focused(e[0]) == game]
        self.clicks = [ms for ms, b, down in self.buttons if b == "L" and down]
        self.kind = self._classify()
        self.gun_clicks = [ms for ms in self.clicks if self.kind.get(ms) == "gun"]
        self._held_cache = {}

    def _classify(self):
        """'gun', 'cast', 'knife' or 'spike' for each left click. An ability key arms a cast that the next click (or a
        right-click) uses up; slot 3 is the knife and 4 the spike until a gun slot is picked again. Logs from before
        these keys were recorded have no such presses, so every click counts as gunfire there."""
        events = [(ms, 0, k, down) for ms, k, down in self.keys if down and (k in ABILITY or k in SLOT)]
        events += [(ms, 1, b, True) for ms, b, down in self.buttons if down and b in ("L", "R")]
        out, slot, armed = {}, "gun", None
        for ms, _, what, _ in sorted(events):
            if what in ABILITY:
                armed = ms
            elif what in SLOT:
                slot, armed = SLOT[what], None
            elif what == "R":
                if armed is not None and ms - armed < 6000:
                    armed = None  # alt-fire cast
            elif armed is not None and ms - armed < 6000:
                out[ms], armed = "cast", None
            else:
                out[ms] = slot
        return out

    def focused(self, ms):
        i = bisect.bisect_right([f[0] for f in self.focus], ms) - 1
        return self.focus[i][1] if i >= 0 else None

    def empty(self):
        return not self.clicks

    def held_at(self, ms):
        """Movement keys held at a moment, and ms since each was last pressed or released."""
        held, changed = set(), {}
        for t, k, down in self.keys:
            if t > ms:
                break
            changed[k] = t
            if down:
                held.add(k)
            else:
                held.discard(k)
        return held, {k: ms - t for k, t in changed.items()}

    def bursts(self, gap_ms=300):
        """Gunfire grouped into bursts: (start ms, end ms, clicks). A held button counts until release; ability casts,
        knife swings and spike plants are left out."""
        presses, skip = [], False
        for ms, b, down in self.buttons:
            if b != "L":
                continue
            if down:
                skip = self.kind.get(ms, "gun") != "gun"
            if not skip:
                presses.append((ms, down))
        out, start, last, n, down_at = [], None, None, 0, None
        for ms, down in presses:
            if down:
                if start is None or ms - last > gap_ms:
                    if start is not None:
                        out.append((start, last, n))
                    start, n = ms, 0
                n += 1
                down_at = ms
                last = ms
            elif down_at is not None:
                last = max(last, ms)
                down_at = None
        if start is not None:
            out.append((start, last, n))
        return out


def movement_at(log, ms):
    """'stopped', 'counter' (counter-strafing or barely moving), or 'moving' at the moment of a shot."""
    held, since = log.held_at(ms)
    moving = [k for k in held if k in MOVE]
    if not moving:
        return "counter" if min(since.get(k, 10 ** 9) for k in MOVE) < 80 else "stopped"
    for k in moving:
        opp = OPPOSITE[k]
        if opp in held:
            continue  # opposite keys cancel out
        if since.get(k, 10 ** 9) < STARTING_MS:
            continue  # only just started moving: not up to speed yet
        if since.get(k, 10 ** 9) < 150 and since.get(opp, 10 ** 9) < 220:
            continue  # a counter-strafe tap against the earlier movement
        return "moving"
    return "counter"


def _fire_intervals(log):
    """(press, release) for each left-button pull."""
    out, down = [], None
    for ms, b, is_down in log.buttons:
        if b != "L":
            continue
        if is_down:
            if down is not None:
                out.append((down, down + 100))
            down = ms
        elif down is not None:
            out.append((down, ms))
            down = None
    if down is not None:
        out.append((down, down + 100))
    return out


def align(log, kill_times, window=120.0):
    """Find the clock offset (seconds) that lines your kills up with your trigger pulls.

    Each kill should land while the trigger is pulled (or just after). The right offset explains nearly every kill;
    to rule out chance, it also has to clearly beat the best offset more than a second away.
    Returns (offset, share of kills explained) or (None, share)."""
    pulls = _fire_intervals(log)
    if len(kill_times) < 3 or len(pulls) < 10:
        return None, 0.0
    starts = [p[0] for p in pulls]

    def score(off):
        hit = 0
        for t in kill_times:
            ms = (t + off) * 1000
            i = bisect.bisect_right(starts, ms + 30) - 1
            if i >= 0 and ms <= pulls[i][1] + 120:
                hit += 1
        return hit

    steps = [round(-window + i * 0.05, 2) for i in range(int(2 * window / 0.05) + 1)]
    scores = [score(o) for o in steps]
    best = max(scores)
    plateau = [o for o, sc in zip(steps, scores) if sc == best]
    # the middle of the run of best offsets around the first one
    run = [plateau[0]]
    for o in plateau[1:]:
        if o - run[-1] <= 0.051:
            run.append(o)
        else:
            break
    off = round((run[0] + run[-1]) / 2, 2)
    rival = max((sc for o, sc in zip(steps, scores) if abs(o - off) > 1.0), default=0)
    share, rival_share = best / len(kill_times), rival / len(kill_times)
    ok = share >= 0.6 and share - rival_share >= 0.3
    return (off, round(share, 2)) if ok else (None, round(share, 2))


def ability_presses(log, t0_ms, t1_ms, repeat_ms=300, throw_ms=6000):
    """C, Q, E and X presses in a stretch of play, as [(ms, slot, throw ms)]. slot is "c", "q", "e" or "x". The throw
    is the left or right click that sent it (the first within 6 s, before another ability or weapon key), or None when
    the key itself set it off or it was cancelled. Pressing the same key again within 300 ms counts once."""
    keys, last = [], {}
    for ms, k, down in log.keys:
        if not down or not (k in ABILITY_SLOT or k in SLOT) or (log.game and log.focused(ms) != log.game):
            continue
        if ms - last.get(k, -10 ** 15) >= repeat_ms:  # a quick repeat of the same key is the same press
            keys.append((ms, k))
        last[k] = ms
    clicks = [ms for ms, b, down in log.buttons if down and b in ("L", "R")]
    out = []
    for i, (ms, k) in enumerate(keys):
        if k not in ABILITY_SLOT or not t0_ms <= ms <= t1_ms:
            continue
        nxt = keys[i + 1][0] if i + 1 < len(keys) else float("inf")
        j = bisect.bisect_right(clicks, ms)
        throw = clicks[j] if j < len(clicks) and clicks[j] - ms <= throw_ms and clicks[j] < nxt else None
        out.append((ms, ABILITY_SLOT[k], throw))
    return out


def death_facts(log, ms):
    """What your hands were doing just before a death at local time ms."""
    shots = [c for c in log.gun_clicks if ms - 2000 <= c <= ms]
    facts = {"shot_2s": bool(shots)}
    if shots:
        facts["moving_last_shot"] = movement_at(log, shots[-1]) == "moving"
    return facts


def opening_bursts(bursts, fights, window_ms=3000):
    """The first burst in the few seconds before each fight that ended in your kill or death (sorted times)."""
    out, used = [], set()
    for b in bursts:
        i = bisect.bisect_left(fights, b[0])
        if i < len(fights) and fights[i] - b[0] <= window_ms and fights[i] not in used:
            used.add(fights[i])
            out.append(b)
    return out


def shot_summary(log, t0_ms, t1_ms, fights=None):
    """How you opened your deciding fights (those ending in your kill or death, when fights are given; otherwise
    every burst): first shots while moving, and long sprays."""
    bursts = [b for b in log.bursts() if t0_ms <= b[0] <= t1_ms]
    if fights is not None:
        bursts = opening_bursts(bursts, sorted(fights))
    if not bursts:
        return None
    moving = sum(1 for b in bursts if movement_at(log, b[0]) == "moving")
    long_ = sum(1 for b in bursts if b[1] - b[0] > 1200)
    return {"engagements": len(bursts), "moving_first": moving, "bursts": len(bursts), "long_bursts": long_,
            "source": "inputs"}
