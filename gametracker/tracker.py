"""Watches running processes on the gaming PC and turns them into play sessions."""
import json
import os
import time
import uuid

import psutil

from . import detect

GENERIC_EXES = {"launcher.exe", "game.exe", "start.exe", "client.exe", "setup.exe", "play.exe", "main.exe"}
END_GRACE_SEC = 20  # tolerate launcher -> game handoffs and quick restarts
RESUME_SEC = 300  # an agent restart within this long continues the same session instead of starting a new one


class Tracker:
    def __init__(self, state_path=None):
        self.active = {}  # game_id -> {uid, start, exe, missing_since}
        self.games = {}
        self._dir_index = []
        self._name_index = {}
        self.state_path = state_path
        self._resume = {}
        try:
            with open(state_path, encoding="utf-8") as f:
                cutoff = time.time() - RESUME_SEC
                self._resume = {gid: a for gid, a in json.load(f).items() if a.get("seen", 0) > cutoff}
        except (OSError, TypeError, ValueError, AttributeError):
            pass

    def _save(self, now):
        if not self.state_path:
            return
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({gid: {"uid": a["uid"], "start": a["start"], "exe": a["exe"], "seen": now}
                           for gid, a in self.active.items() if not a["missing_since"]}, f)
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    def set_games(self, games):
        self.games = {g["id"]: g for g in games}
        self._dir_index = sorted(((detect.norm(g["install_dir"]), g) for g in games if g.get("install_dir")),
                                 key=lambda t: len(t[0]), reverse=True)
        names = {}
        for g in games:
            for n in g.get("exe_names") or []:
                n = n.lower()
                if n not in GENERIC_EXES:
                    names.setdefault(n, set()).add(g["id"])
        self._name_index = {n: next(iter(ids)) for n, ids in names.items() if len(ids) == 1}

    def _match(self, name, exe):
        lname = name.lower()
        if detect.HELPER_EXE.search(lname):
            return None
        if exe:
            ne = detect.norm(exe)
            for d, g in self._dir_index:
                if ne.startswith(d + os.sep):
                    strict = [s.lower() for s in g.get("strict_exes") or []]
                    return g["id"] if not strict or lname in strict else None
            return None
        # Path unreadable (e.g. anti-cheat protected process): fall back to a unique exe name.
        return self._name_index.get(lname)

    def detect_running(self):
        running, steam_up = {}, False
        for p in psutil.process_iter(["name", "exe"]):
            name = p.info.get("name") or ""
            if name.lower() == "steam.exe":
                steam_up = True
            gid = self._match(name, p.info.get("exe"))
            if gid:
                running.setdefault(gid, p.info.get("exe") or name)
        if steam_up:
            appid = detect.steam_running_appid()
            if appid and appid != "0" and f"steam:{appid}" in self.games:
                running.setdefault(f"steam:{appid}", "steam:RunningAppID")
        return running

    def tick(self, min_session_sec=60):
        """Returns (session_updates, started_game_ids, ended_game_ids)."""
        now = time.time()
        running = self.detect_running()
        updates, started, ended = [], [], []
        for gid, exe in running.items():
            if gid in self.active:
                self.active[gid]["missing_since"] = None
                continue
            prev = self._resume.get(gid)
            if prev and prev.get("uid") and prev.get("start"):
                # Still running from before the agent restarted: keep extending that session.
                self.active[gid] = {"uid": prev["uid"], "start": prev["start"], "exe": exe, "missing_since": None}
            else:
                self.active[gid] = {"uid": uuid.uuid4().hex, "start": now, "exe": exe, "missing_since": None}
                started.append(gid)
        self._resume = {}  # only the first look after startup can resume
        for gid in list(self.active):
            a = self.active[gid]
            if gid not in running:
                a["missing_since"] = a["missing_since"] or now
                if now - a["missing_since"] < END_GRACE_SEC:
                    continue
                del self.active[gid]
                end = a["missing_since"]
                short = end - a["start"] < min_session_sec
                updates.append({"uid": a["uid"], "game_id": gid, "start_ts": a["start"], "end_ts": end,
                                "exe": a["exe"], "discard": short})
                if not short:
                    ended.append(gid)
                continue
            updates.append({"uid": a["uid"], "game_id": gid, "start_ts": a["start"], "end_ts": now, "exe": a["exe"]})
        self._save(now)
        return updates, started, ended

    def now_playing(self):
        return [{"game_id": gid, "name": self.games.get(gid, {}).get("name", gid), "start_ts": a["start"]}
                for gid, a in self.active.items() if not a["missing_since"]]

    def close_all(self):
        now = time.time()
        out = [{"uid": a["uid"], "game_id": gid, "start_ts": a["start"], "end_ts": now, "exe": a["exe"]}
               for gid, a in self.active.items()]
        self.active.clear()
        return out
