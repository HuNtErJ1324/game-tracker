"""Find installed games across launchers (Steam, Riot, Epic, Battle.net, EA, Ubisoft, GOG, Xbox)."""
import base64
import glob
import json
import os
import re
import string
import winreg

from . import vdf

# Processes that live inside game folders but don't mean "playing".
HELPER_EXE = re.compile(
    r"(crash|unins|setup|redist|vcredist|dxsetup|directx|easyanticheat|beservice|battleye|"
    r"report|updater|_cef|cefsharp|cefsubprocess|webhelper|dotnet|prereq|bugsplat|crs-|createdump|"
    r"activationui|cleanup|installer|browser|bootstrapper|vconsole|launchhelper|inject)",
    re.I,
)

# Games whose folder also hosts a long-running client/lobby: only these exes count as playing.
STRICT_EXES = {
    "riot:valorant": ["VALORANT-Win64-Shipping.exe"],
    "riot:league_of_legends": ["League of Legends.exe"],
}

RIOT_NAMES = {
    "valorant": "VALORANT",
    "league_of_legends": "League of Legends",
    "teamfighttactics": "Teamfight Tactics",
    "bacon": "Legends of Runeterra",
    "lion": "2XKO",
}

STEAM_SKIP_APPIDS = {"228980", "1070560", "1391110", "1628350", "1493710", "1826330", "2180100"}
# Steam "games" that are really always-on tools (Wallpaper Engine, SteamVR, Soundpad); listed but hidden + never
# tracked by default.
STEAM_TOOL_APPIDS = {"431960", "250820", "629520"}
STEAM_SKIP_NAMES = re.compile(r"^(Steamworks Common|Steam Linux Runtime|Proton )", re.I)


def norm(path):
    return os.path.normcase(os.path.normpath(path)) if path else ""


def _reg_value(root, subkey, name, view=0):
    try:
        with winreg.OpenKey(root, subkey, 0, winreg.KEY_READ | view) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def _reg_subkeys(root, subkey, view=0):
    try:
        with winreg.OpenKey(root, subkey, 0, winreg.KEY_READ | view) as k:
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(k, i)
                except OSError:
                    return
                i += 1
                vals = {}
                try:
                    with winreg.OpenKey(k, name) as sk:
                        j = 0
                        while True:
                            try:
                                vn, vv, _ = winreg.EnumValue(sk, j)
                            except OSError:
                                break
                            vals[vn] = vv
                            j += 1
                except OSError:
                    pass
                yield name, vals
    except OSError:
        return


def _game(gid, source, name, install_dir, **extra):
    return {
        "id": gid,
        "source": source,
        "name": name,
        "install_dir": os.path.normpath(install_dir) if install_dir else "",
        "strict_exes": STRICT_EXES.get(gid, []),
        "extra": extra,
    }


# ---------------- Steam ----------------

def steam_root():
    p = _reg_value(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath")
    if p and os.path.isdir(p):
        return os.path.normpath(p)
    for p in (r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"):
        if os.path.isdir(p):
            return p
    return None


def steam_active_account():
    """Return (steamid64, account_id) of the most recently logged-in Steam user."""
    root = steam_root()
    if not root:
        return None, None
    try:
        users = vdf.iget(vdf.load(os.path.join(root, "config", "loginusers.vdf")), "users") or {}
    except OSError:
        return None, None
    best = None
    for sid, info in users.items():
        if not isinstance(info, dict):
            continue
        score = (info.get("MostRecent") == "1", int(info.get("Timestamp", "0") or 0))
        if best is None or score > best[0]:
            best = (score, sid)
    if not best:
        return None, None
    sid64 = int(best[1])
    return sid64, sid64 - 76561197960265728


def steam_running_appid():
    v = _reg_value(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "RunningAppID")
    return str(v) if v else None


def _steam_playtime(root, account_id):
    """Historic playtime/last-played Steam already recorded, keyed by appid."""
    if not account_id:
        return {}
    path = os.path.join(root, "userdata", str(account_id), "config", "localconfig.vdf")
    try:
        data = vdf.load(path)
    except OSError:
        return {}
    apps = vdf.iget(data, "UserLocalConfigStore", "Software", "Valve", "Steam", "apps") or {}
    out = {}
    for appid, info in apps.items():
        if isinstance(info, dict):
            out[appid] = {
                "playtime_min": int(vdf.iget(info, "Playtime", default="0") or 0),
                "last_played": int(vdf.iget(info, "LastPlayed", default="0") or 0),
            }
    return out


def scan_steam():
    root = steam_root()
    if not root:
        return []
    libs = {root}
    for cand in ("steamapps/libraryfolders.vdf", "config/libraryfolders.vdf"):
        try:
            lf = vdf.iget(vdf.load(os.path.join(root, cand)), "libraryfolders") or {}
        except OSError:
            continue
        for v in lf.values():
            p = v.get("path") if isinstance(v, dict) else v
            if isinstance(p, str) and os.path.isdir(p):
                libs.add(os.path.normpath(p))
    _, account = steam_active_account()
    playtime = _steam_playtime(root, account)
    games, seen = [], set()
    for lib in libs:
        for acf in glob.glob(os.path.join(lib, "steamapps", "appmanifest_*.acf")):
            try:
                st = vdf.iget(vdf.load(acf), "AppState") or {}
            except OSError:
                continue
            appid, name = st.get("appid"), st.get("name") or ""
            if not appid or appid in seen or appid in STEAM_SKIP_APPIDS or STEAM_SKIP_NAMES.match(name):
                continue
            seen.add(appid)
            pt = playtime.get(appid, {})
            games.append(_game(
                f"steam:{appid}", "steam", name or f"Steam app {appid}",
                os.path.join(lib, "steamapps", "common", st.get("installdir", "")),
                appid=appid,
                imported_playtime_min=pt.get("playtime_min", 0),
                imported_last_played=pt.get("last_played", 0),
                tool=appid in STEAM_TOOL_APPIDS,
            ))
    return games


# ---------------- Riot ----------------

def scan_riot():
    base = os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"), "Riot Games", "Metadata")
    games = []
    for yml in glob.glob(os.path.join(base, "*", "*.product_settings.yaml")):
        product = os.path.basename(os.path.dirname(yml))  # e.g. valorant.live
        game, _, patchline = product.partition(".")
        if patchline != "live":
            continue
        try:
            with open(yml, encoding="utf-8") as f:
                m = re.search(r'product_install_full_path:\s*"?([^"\n]+)"?', f.read())
        except OSError:
            continue
        if not m or not os.path.isdir(m.group(1)):
            continue
        games.append(_game(f"riot:{game}", "riot", RIOT_NAMES.get(game, game.replace("_", " ").title()),
                           m.group(1), product=product))
    return games


# ---------------- Epic ----------------

def _epic_catalog_art():
    """Epic's own store art (portrait 'DieselGameBoxTall', wide 'DieselGameBox') from the launcher's catalog cache."""
    path = os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"), "Epic", "EpicGamesLauncher", "Data",
                        "Catalog", "catcache.bin")
    try:
        with open(path, "rb") as f:
            items = json.loads(base64.b64decode(f.read()))
    except (OSError, ValueError):
        return {}
    out = {}
    for it in items if isinstance(items, list) else []:
        imgs = {k.get("type"): k.get("url") for k in it.get("keyImages") or []}
        out[it.get("id")] = {"cover_url": imgs.get("DieselGameBoxTall"), "hero_url": imgs.get("DieselGameBox")}
    return out


def scan_epic():
    base = os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
                        "Epic", "EpicGamesLauncher", "Data", "Manifests")
    art = _epic_catalog_art()
    games = []
    for item in glob.glob(os.path.join(base, "*.item")):
        try:
            with open(item, encoding="utf-8") as f:
                j = json.load(f)
        except (OSError, ValueError):
            continue
        cats = j.get("AppCategories") or []
        if cats and "games" not in cats:
            continue
        loc = j.get("InstallLocation")
        if not loc or not os.path.isdir(loc):
            continue
        games.append(_game(f"epic:{j.get('AppName') or j.get('DisplayName')}", "epic",
                           j.get("DisplayName") or j.get("AppName"), loc,
                           launch_exe=j.get("LaunchExecutable"),
                           **{k: v for k, v in art.get(j.get("CatalogItemId"), {}).items() if v}))
    return games


# ---------------- Uninstall-registry launchers (Battle.net, EA, Ubisoft) ----------------

UNINSTALL = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"


def _uninstall_entries():
    for root, view in ((winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
                       (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
                       (winreg.HKEY_CURRENT_USER, 0)):
        yield from _reg_subkeys(root, UNINSTALL, view)


def scan_uninstall_launchers():
    games = []
    for key, v in _uninstall_entries():
        name = str(v.get("DisplayName") or "").strip()
        loc = str(v.get("InstallLocation") or "").strip().strip('"')
        unins = str(v.get("UninstallString") or "")
        pub = str(v.get("Publisher") or "")
        if not name or not loc or not os.path.isdir(loc):
            continue
        if "Battle.net" in unins and name != "Battle.net":
            source = "battlenet"
        elif key.startswith("Uplay Install") or "Ubisoft Game Launcher" in unins:
            source = "ubisoft"
        elif pub.startswith("Electronic Arts") and name not in ("EA app", "Origin"):
            source = "ea"
        else:
            continue
        games.append(_game(f"{source}:{key}", source, name, loc))
    return games


# ---------------- GOG ----------------

def scan_gog():
    games = []
    for key, v in _reg_subkeys(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\GOG.com\Games", winreg.KEY_WOW64_32KEY):
        path = v.get("path")
        if path and os.path.isdir(path):
            games.append(_game(f"gog:{key}", "gog", v.get("gameName") or key, path, exe=v.get("exe")))
    return games


# ---------------- Xbox / Game Pass ----------------

def _xbox_store_id(content):
    """The Microsoft Store product ID from MicrosoftGame.config (used to fetch official store art)."""
    for fname in os.listdir(content):
        if fname.lower() == "microsoftgame.config":
            try:
                with open(os.path.join(content, fname), encoding="utf-8", errors="ignore") as f:
                    text = re.sub(r"<!--.*?-->", "", f.read(), flags=re.S)
            except OSError:
                return {}
            m = re.search(r"<StoreId>\s*([A-Za-z0-9]+)\s*</StoreId>", text)
            return {"store_id": m.group(1)} if m else {}
    return {}


def scan_xbox():
    games = []
    for letter in string.ascii_uppercase:
        base = f"{letter}:\\XboxGames"
        if not os.path.isdir(base):
            continue
        for name in os.listdir(base):
            content = os.path.join(base, name, "Content")
            if os.path.isdir(content):
                games.append(_game(f"xbox:{name}", "xbox", name, content, **_xbox_store_id(content)))
    return games


SCANNERS = [scan_steam, scan_riot, scan_epic, scan_uninstall_launchers, scan_gog, scan_xbox]


def _exe_names(install_dir, max_depth=4, cap=60):
    """Collect game executable names so we can match processes whose path we can't read."""
    out = []
    base_depth = install_dir.rstrip("\\/").count(os.sep)
    for dirpath, dirnames, filenames in os.walk(install_dir):
        if dirpath.count(os.sep) - base_depth >= max_depth:
            dirnames[:] = []
        dirnames[:] = [d for d in dirnames if not HELPER_EXE.search(d) and d.lower() not in ("_commonredist", "redist", "support")]
        for f in filenames:
            if f.lower().endswith(".exe") and not HELPER_EXE.search(f):
                out.append(f)
                if len(out) >= cap:
                    return out
    return out


def scan_all():
    """Run every scanner and return a de-duplicated list of games."""
    games, dirs = [], set()
    for scanner in SCANNERS:
        try:
            found = scanner()
        except Exception as e:  # one broken launcher shouldn't hide the rest
            print(f"[scan] {scanner.__name__} failed: {e}")
            continue
        for g in found:
            d = norm(g["install_dir"])
            if d in dirs:
                continue
            dirs.add(d)
            g["exe_names"] = g["strict_exes"] or _exe_names(g["install_dir"])
            games.append(g)
    return games
