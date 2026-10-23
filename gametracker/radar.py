"""CS2 radar images for map views, served by the tracker from its own cache.

Downloaded once from the awpy-data release (the PNG radars that match Valve's overview coordinates).
"""
import io
import os
import re
import threading
import zipfile

from . import net
from .config import CACHE_DIR

RADAR_DIR = os.path.join(CACHE_DIR, "radars")
_lock = threading.Lock()


def ensure():
    """Download and unpack the radar images if we don't have them yet."""
    with _lock:
        if os.path.isdir(RADAR_DIR) and any(f.endswith(".png") for f in os.listdir(RADAR_DIR)):
            return True
        tag = net.cached_json("awpy_latest", "https://api.github.com/repos/pnxenopoulos/awpy-data/releases/latest",
                              ttl=7 * 86400)["tag_name"]
        r = net.http.get(f"https://github.com/pnxenopoulos/awpy-data/releases/download/{tag}/images.zip", timeout=60)
        r.raise_for_status()
        os.makedirs(RADAR_DIR, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            for name in z.namelist():
                base = os.path.basename(name)
                if re.fullmatch(r"[a-z0-9_]+\.png", base):
                    with open(os.path.join(RADAR_DIR, base), "wb") as f:
                        f.write(z.read(name))
        return True


def ensure_async():
    threading.Thread(target=lambda: _quiet(ensure), daemon=True, name="radars").start()


def _quiet(fn):
    try:
        fn()
    except Exception as e:
        print(f"[radar] couldn't fetch CS2 radar images: {e}")


def path_for(name):
    """Local file for /radar/<name>.png, or None. Only plain map names are accepted."""
    if not re.fullmatch(r"[a-z0-9_]+", name or ""):
        return None
    p = os.path.join(RADAR_DIR, name + ".png")
    if not os.path.exists(p):
        _quiet(ensure)
    return p if os.path.exists(p) else None
