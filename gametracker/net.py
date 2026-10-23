"""Small HTTP helpers with an on-disk JSON cache for static game data."""
import json
import os
import time

import requests
import urllib3

from .config import CACHE_DIR

# Riot's local client APIs use self-signed certs on 127.0.0.1; we only skip verification there.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

http = requests.Session()
http.headers["User-Agent"] = "GameTracker/0.1 (personal local tracker)"


offline = False  # the demo sets this: the server then fetches nothing from the internet


class Unavailable(Exception):
    """Data source can't be reached right now (client closed, no key, private profile...)."""


def get_json(url, timeout=15, **kw):
    if offline:
        raise Unavailable("offline (demo)")
    r = http.get(url, timeout=timeout, **kw)
    r.raise_for_status()
    return r.json()


def local_get(port, password, path, user="riot", timeout=5):
    r = http.get(f"https://127.0.0.1:{port}{path}", auth=(user, password), verify=False, timeout=timeout)
    r.raise_for_status()
    return r.json()


def cached_json(name, url, ttl=7 * 86400, **kw):
    path = os.path.join(CACHE_DIR, name + ".json")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < ttl:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    try:
        data = get_json(url, **kw)
    except requests.RequestException:
        if os.path.exists(path):  # stale cache beats nothing
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        raise
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return data
