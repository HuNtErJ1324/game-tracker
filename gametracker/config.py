"""Settings stored as JSON in the data folder. Secrets may also come from environment variables."""
import json
import os
import secrets
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("GAMETRACKER_DATA") or os.path.join(ROOT, "data")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
CACHE_DIR = os.path.join(DATA_DIR, "cache")

DEFAULTS = {
    "web_port": 8787,
    "poll_interval_sec": 5,
    "min_session_sec": 60,
    # Claude story coach. Leave blank to use the ANTHROPIC_API_KEY environment variable.
    "anthropic_api_key": "",
    "claude_model": "claude-opus-5-5",
    # How the coach reaches Claude: "prime-agent" (needs PRIME_AGENT_BIN or prime-agent on PATH) or "anthropic".
    "coach_backend": "prime-agent",
    "coach_web_search": True,
    "spoiler_level": "light",  # none | light | full
    # Optional: Steam Web API key (local achievement files are used without one).
    "steam_api_key": "",
    # Overwatch BattleTag like Name#1234 (career profile must be public).
    "overwatch_battletag": "",
    # Guides knowledge base: YouTube guides distilled into notes per game.
    "youtube_api_key": "",
    "kb_enabled": True,
    "kb_model": "claude-haiku-5-5",
    "kb_interval_days": 7,      # how often each recently played game gets new guides
    "kb_videos_per_run": 4,     # guides read per game per refresh
    "kb_active_days": 21,       # only games played within this many days are refreshed automatically
    # Improvement reviews: movement/shooting timing while VALORANT or CS2 is in front, and Claude death reviews.
    "input_reading": True,
    "death_reviews": True,
    "review_model": "claude-haiku-5-5",
    # Video evidence: the NVIDIA App records VALORANT matches / saves CS2 Instant Replays, the PC keeps six small
    # frames per death for the reviews and deletes the video. Off until you turn it on.
    "video_evidence": False,
    "auto_sync_minutes": 10,
    # Shared secret the PC agent uses to talk to the server (generated on first run).
    "agent_token": "",
    # Extra hostnames the dashboard may be reached by (localhost, IPs, *.ts.net and bare
    # MagicDNS names are always allowed).
    "allowed_hosts": [],
}

SECRET_KEYS = ("anthropic_api_key", "steam_api_key", "agent_token", "youtube_api_key")
AGENT_KEYS = ("poll_interval_sec", "min_session_sec", "auto_sync_minutes", "steam_api_key", "overwatch_battletag",
              "input_reading", "video_evidence")
ENV_FALLBACK = {"anthropic_api_key": "ANTHROPIC_API_KEY", "steam_api_key": "STEAM_API_KEY", "youtube_api_key": "YOUTUBE_API_KEY"}

_lock = threading.Lock()
_cache = None


def load():
    global _cache
    with _lock:
        if _cache is None:
            os.makedirs(CACHE_DIR, exist_ok=True)
            data = {}
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, encoding="utf-8") as f:
                    data = json.load(f)
            _cache = {**DEFAULTS, **data}
        return dict(_cache)


def get(key):
    val = load().get(key)
    if not val and key in ENV_FALLBACK:
        val = os.environ.get(ENV_FALLBACK[key], "")
    return val


def update(changes):
    global _cache
    cfg = load()
    for k, v in changes.items():
        if k not in DEFAULTS:
            continue
        if k in SECRET_KEYS and isinstance(v, str) and v.startswith("•"):
            continue  # masked value echoed back from the UI: keep existing secret
        if isinstance(DEFAULTS[k], bool):
            v = bool(v)
        elif isinstance(DEFAULTS[k], int):
            v = int(v)
        elif isinstance(DEFAULTS[k], list) and isinstance(v, str):
            v = [x.strip() for x in v.split(",") if x.strip()]
        cfg[k] = v
    with _lock:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({k: v for k, v in cfg.items() if v != DEFAULTS.get(k)}, f, indent=2)
        os.replace(tmp, CONFIG_PATH)
        _cache = cfg
    return cfg


def agent_token():
    tok = load().get("agent_token")
    if not tok:
        tok = secrets.token_urlsafe(24)
        update({"agent_token": tok})
    return tok


def for_agent():
    """Settings the PC agent needs (no Claude key)."""
    return {k: get(k) for k in AGENT_KEYS}


# --- agent-side connection settings (where the server is) ---
AGENT_PATH = os.path.join(DATA_DIR, "agent.json")


def load_agent():
    try:
        with open(AGENT_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_agent(server_url, token):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(AGENT_PATH, "w", encoding="utf-8") as f:
        json.dump({"server_url": server_url, "token": token}, f, indent=2)


def public():
    """Config safe to send to the browser: secrets masked."""
    cfg = load()
    for k in SECRET_KEYS:
        has = bool(get(k))
        cfg[k] = ("••••••••" + (" (env)" if not cfg[k] and has else "")) if has else ""
    return cfg
