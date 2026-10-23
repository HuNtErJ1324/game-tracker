"""Steam achievements: read from Steam's local cache files, or the Steam Web API if a key is set.

Achievements are the best generic signal of story progress, so the Claude coach leans on them.
Global unlock percentages (no key needed) show which locked ones most players reach next.
"""
import os
import time

import requests

from .. import db, net, vdf
from . import Integration

ICON_URL = "https://cdn.akamai.steamstatic.com/steamcommunity/public/images/apps/{appid}/{icon}"
WEBAPI = "https://api.steampowered.com/ISteamUserStats"


def _text(d):
    if isinstance(d, dict):
        return d.get("english") or next((v for k, v in d.items() if k != "token" and isinstance(v, str)), "")
    return d or ""


def _local(appid):
    from .. import detect  # Windows-only module; imported here so the server never needs it
    root = detect.steam_root()
    _, account = detect.steam_active_account()
    if not root or not account:
        return None
    stats_dir = os.path.join(root, "appcache", "stats")
    try:
        schema = next(iter(vdf.load_binary(os.path.join(stats_dir, f"UserGameStatsSchema_{appid}.bin")).values()))
    except (OSError, StopIteration, ValueError):
        return None
    try:
        user = vdf.load_binary(os.path.join(stats_dir, f"UserGameStats_{account}_{appid}.bin")).get("cache", {})
    except (OSError, ValueError):
        user = {}
    out = []
    for stat_id, stat in (schema.get("stats") or {}).items():
        if not isinstance(stat, dict) or "bits" not in stat:
            continue
        mine = user.get(stat_id) or {}
        bits = (mine.get("data") or 0) & 0xFFFFFFFF
        times = mine.get("AchievementTimes") or {}
        for bit, a in stat["bits"].items():
            disp = a.get("display") or {}
            got = bool(bits >> int(bit) & 1)
            out.append({
                "api": a.get("name"), "name": _text(disp.get("name")), "desc": _text(disp.get("desc")),
                "hidden": str(disp.get("hidden", "0")) == "1", "achieved": got,
                "unlock_ts": times.get(bit) if got else None,
                "icon": disp.get("icon") if got else disp.get("icon_gray") or disp.get("icon"),
            })
    return out


def _webapi(appid, key, steamid):
    mine = net.get_json(f"{WEBAPI}/GetPlayerAchievements/v1/?appid={appid}&key={key}&steamid={steamid}&l=english")
    schema = net.get_json(f"https://api.steampowered.com/ISteamUserStats/GetSchemaForGame/v2/?appid={appid}&key={key}&l=english")
    meta = {a["name"]: a for a in ((schema.get("game") or {}).get("availableGameStats") or {}).get("achievements", [])}
    out = []
    for a in (mine.get("playerstats") or {}).get("achievements") or []:
        m = meta.get(a["apiname"], {})
        got = bool(a.get("achieved"))
        out.append({"api": a["apiname"], "name": a.get("name") or m.get("displayName"),
                    "desc": a.get("description") or m.get("description", ""), "hidden": bool(m.get("hidden")),
                    "achieved": got, "unlock_ts": a.get("unlocktime") or None,
                    "icon_url": m.get("icon") if got else m.get("icongray")})
    return out


class SteamAchievements(Integration):
    key = "achievements"
    title = "Achievements & progress"
    sync_delays = (20,)

    def applies(self, game):
        return game.get("source") == "steam"

    def sync(self, game, sink):
        appid = (game.get("extra") or {}).get("appid") or game["id"].split(":", 1)[1]
        achs = _local(appid)
        key = sink.settings.get("steam_api_key")
        if not achs and key:
            from .. import detect
            steamid, _ = detect.steam_active_account()
            try:
                achs = _webapi(appid, key, steamid)
            except requests.RequestException as e:
                raise net.Unavailable(f"Steam Web API: {e}")
        if not achs:
            return "no achievements for this game"
        try:
            pct = net.cached_json(f"steam_global_{appid}",
                                  f"{WEBAPI}/GetGlobalAchievementPercentagesForApp/v0002/?gameid={appid}", ttl=86400)
            pct = {a["name"]: float(a["percent"]) for a in (pct.get("achievementpercentages") or {}).get("achievements", [])}
        except Exception:
            pct = {}
        for a in achs:
            a["pct"] = round(pct[a["api"]], 1) if a["api"] in pct else None
            if a.get("icon") and not a.get("icon_url"):
                a["icon_url"] = ICON_URL.format(appid=appid, icon=a.pop("icon"))
            a.pop("icon", None)
        sink.snapshot(game["id"], "achievements", {
            "total": len(achs), "unlocked": sum(a["achieved"] for a in achs), "list": achs})
        return f"{sum(a['achieved'] for a in achs)}/{len(achs)} unlocked"

    # ---------- dashboard ----------

    def panel(self, game):
        p = {"title": self.title, "tiles": [], "tables": [], "notes": []}
        snap = db.latest_snapshot(game["id"], "achievements")
        if not snap:
            p["notes"].append("No achievement data yet (syncs automatically after you play).")
            return p
        d = snap["data"]
        got = sorted([a for a in d["list"] if a["achieved"]], key=lambda a: a.get("unlock_ts") or 0, reverse=True)
        locked = sorted([a for a in d["list"] if not a["achieved"]], key=lambda a: -(a.get("pct") or 0))
        p["tiles"].append({"label": "Unlocked", "value": f"{d['unlocked']}/{d['total']}",
                           "sub": f"{100 * d['unlocked'] // max(1, d['total'])}% complete", "progress": d["unlocked"] / max(1, d["total"])})
        if got and got[0].get("unlock_ts"):
            p["tiles"].append({"label": "Latest unlock", "value": got[0]["name"], "icon": got[0].get("icon_url"),
                               "sub": {"ts": got[0]["unlock_ts"]}})
        rare = min((a for a in got if a.get("pct") is not None), key=lambda a: a["pct"], default=None)
        if rare:
            p["tiles"].append({"label": "Rarest unlock", "value": rare["name"], "icon": rare.get("icon_url"),
                               "sub": f"{rare['pct']}% of players"})
        p["achievements"] = {"unlocked": got, "locked": locked}
        return p

    def coach_context(self, game):
        snap = db.latest_snapshot(game["id"], "achievements")
        if not snap:
            return ""
        d = snap["data"]
        got = sorted([a for a in d["list"] if a["achieved"]], key=lambda a: a.get("unlock_ts") or 0)
        locked = sorted([a for a in d["list"] if not a["achieved"]], key=lambda a: -(a.get("pct") or 0))

        def line(a, with_date):
            when = time.strftime("%Y-%m-%d", time.localtime(a["unlock_ts"])) + " " if with_date and a.get("unlock_ts") else ""
            pct = f" [{a['pct']}% of players]" if a.get("pct") is not None else ""
            hid = " (hidden/secret)" if a.get("hidden") else ""
            return f"- {when}{a['name']}: {a['desc']}{hid}{pct}"

        return (f"Steam achievements: {d['unlocked']}/{d['total']} unlocked.\n"
                "Unlocked, oldest first:\n" + "\n".join(line(a, True) for a in got) +
                "\n\nStill locked, most common first:\n" + "\n".join(line(a, False) for a in locked))
