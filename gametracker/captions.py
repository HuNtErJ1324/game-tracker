"""YouTube captions as text, for the guides knowledge base.

YouTube refuses caption downloads from many servers (it blocks the home server's address), so the gaming PC
fetches them when it's online and the server only tries itself when it isn't. Both use this module.
"""
import json
import os
import random
import threading
import time

MAX_CHARS = 60000  # about 15k tokens: a 40-minute guide fits
LANGS = ["en", "en-US", "en-GB", "en-CA", "en-AU"]

# The PC's downloads are kept gentle so YouTube doesn't block it as well
DAILY_CAP = 40         # downloads in any 24 hours
GAP_SEC = (4, 9)       # wait between downloads, picked at random
PAUSE_SEC = 24 * 3600  # no downloads for a day once YouTube refuses one


def fetch(vid):
    """(status, text). status is "ok" (text has [m:ss] markers about every 30 seconds), "none" when the video has no
    English captions, "blocked" when YouTube refused this connection, or "error" (text says what went wrong)."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        from youtube_transcript_api import _errors as yt
    except ImportError:
        return "error", "youtube-transcript-api isn't installed"
    try:
        fetched = YouTubeTranscriptApi().fetch(vid, languages=LANGS)
    except (yt.RequestBlocked, yt.PoTokenRequired, yt.YouTubeRequestFailed):  # IpBlocked is a RequestBlocked
        return "blocked", None
    except (yt.NoTranscriptFound, yt.TranscriptsDisabled, yt.VideoUnavailable, yt.VideoUnplayable,
            yt.AgeRestricted, yt.InvalidVideoId):
        return "none", None
    except Exception as e:
        return "error", f"{type(e).__name__}: {str(e)[:200]}"
    parts, mark = [], -30
    for s in fetched:
        if s.start - mark >= 30:
            mark = s.start
            parts.append(f"\n[{int(s.start) // 60}:{int(s.start) % 60:02d}]")
        parts.append(" " + " ".join(s.text.split()))
    text = "".join(parts).strip()
    return ("ok", text[:MAX_CHARS]) if text else ("none", None)


class Pacer:
    """Downloads on the PC: a few seconds apart, at most DAILY_CAP a day, and none for a day after YouTube refuses one.
    The counts live in a file, so restarting the tracker doesn't reset them."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        try:
            with open(path, encoding="utf-8") as f:
                s = json.load(f)
        except (OSError, ValueError):
            s = {}
        self.recent = [float(t) for t in s.get("recent") or []]
        self.paused_until = float(s.get("paused_until") or 0)
        self.last = max(self.recent, default=0.0)

    def _save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({"recent": self.recent, "paused_until": self.paused_until}, f)
        except OSError:
            pass

    def fetch_all(self, vids, sleep=time.sleep):
        """{video id: (status, text)} like fetch, plus "paused" (YouTube refused this PC in the last day) and "capped"
        (the day's downloads are used up) for videos it didn't try."""
        out = {}
        with self.lock:  # one batch at a time
            for vid in vids:
                now = time.time()
                self.recent = [t for t in self.recent if now - t < 86400]
                if now < self.paused_until:
                    out[vid] = ("paused", None)
                    continue
                if len(self.recent) >= DAILY_CAP:
                    out[vid] = ("capped", None)
                    continue
                wait = self.last + random.uniform(*GAP_SEC) - now
                if wait > 0:
                    sleep(wait)
                out[vid] = fetch(vid)
                self.last = time.time()
                self.recent.append(self.last)
                if out[vid][0] == "blocked":
                    self.paused_until = self.last + PAUSE_SEC
                self._save()
        return out
