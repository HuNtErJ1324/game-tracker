"""Cover (portrait) and hero (wide) art for every game, from official store sources.

- Steam games: Steam's store API (exact current asset URLs, incl. newer hashed paths).
- Epic games: Epic's own store art, read by the PC agent from the launcher's catalog cache.
- Xbox / Game Pass: Microsoft Store catalog, by the Store ID in the game's MicrosoftGame.config.
- Anything else: Microsoft Store search by name, then Steam store search by name.
- A cover/hero URL you set yourself always wins.
Results are cached on the game row for a week.
"""
import json
import re
import threading
import time

from . import db, net

STEAM_ASSETS = "https://shared.akamai.steamstatic.com/store_item_assets/"
MS_PRODUCTS = "https://displaycatalog.mp.microsoft.com/v7.0/products?bigIds={ids}&market=US&languages=en-US"
MS_SEARCH = "https://displaycatalog.mp.microsoft.com/v7.0/productFamilies/autosuggest"
TTL = 7 * 86400
_running = threading.Lock()


def _norm(name):
    name = re.sub(r"[®™©]", "", name or "").lower()
    name = re.sub(r"\b(launcher|pc|game|the)\b", " ", name)
    return re.sub(r"[^a-z0-9]+", "", name)


# ---------------- Steam ----------------

def _steam_assets(appids):
    out = {}
    for i in range(0, len(appids), 50):
        chunk = appids[i:i + 50]
        q = {"ids": [{"appid": int(a)} for a in chunk], "context": {"language": "english", "country_code": "US"},
             "data_request": {"include_assets": True}}
        data = net.get_json("https://api.steampowered.com/IStoreBrowseService/GetItems/v1/",
                            params={"input_json": json.dumps(q)}, timeout=20)
        for item in (data.get("response") or {}).get("store_items") or []:
            a = item.get("assets") or {}
            fmt = a.get("asset_url_format")
            if not fmt:
                continue
            url = lambda f: STEAM_ASSETS + fmt.replace("${FILENAME}", f) if f else None
            out[str(item.get("appid"))] = {
                "cover": url(a.get("library_capsule") or a.get("library_capsule_2x")),
                "hero": url(a.get("library_hero")) or url(a.get("header")),
            }
    return out


def _steam_search(name):
    data = net.get_json("https://store.steampowered.com/api/storesearch/",
                        params={"term": name, "cc": "us", "l": "en"}, timeout=15)
    for item in data.get("items") or []:
        if _norm(item.get("name")) == _norm(name):
            return _steam_assets([str(item["id"])]).get(str(item["id"]))
    return None


# ---------------- Microsoft Store ----------------

def _ms_images(product_id):
    data = net.get_json(MS_PRODUCTS.format(ids=product_id), timeout=15)
    products = data.get("Products") or []
    if not products:
        return None
    imgs = (products[0].get("LocalizedProperties") or [{}])[0].get("Images") or []

    def pick(*purposes):
        for purpose in purposes:
            cands = [i for i in imgs if i.get("ImagePurpose") == purpose and i.get("Uri")]
            if cands:
                best = max(cands, key=lambda i: i.get("Width", 0))
                uri = best["Uri"]
                return ("https:" + uri) if uri.startswith("//") else uri
        return None

    cover, hero = pick("Poster", "BoxArt"), pick("SuperHeroArt", "TitledHeroArt")
    cover = cover and cover + "?w=600"   # the store image server resizes on request
    hero = hero and hero + "?w=1920"
    return {"cover": cover, "hero": hero} if cover or hero else None


def _ms_search(name):
    data = net.get_json(MS_SEARCH, params={"market": "US", "languages": "en-US", "query": name,
                                           "productFamilyNames": "Games,Apps"}, timeout=15)
    want = _norm(name)
    for fam in data.get("Results") or []:
        for p in fam.get("Products") or []:
            if _norm(p.get("Title")) == want:
                return _ms_images(p["ProductId"])
    return None


# ---------------- resolution ----------------

def _resolve_one(g, steam_cache):
    extra = g.get("extra") or {}
    if g["source"] == "steam":
        found = steam_cache.get(str(extra.get("appid") or g["id"].split(":", 1)[1]))
        if found and found.get("cover"):
            return {**found, "source": "steam"}
    if extra.get("cover_url"):  # Epic: provided by the agent from the launcher catalog
        return {"cover": extra["cover_url"], "hero": extra.get("hero_url"), "source": g["source"]}
    if extra.get("store_id"):
        found = _ms_images(extra["store_id"])
        if found:
            return {**found, "source": "microsoft-store"}
    for finder, label in ((_ms_search, "microsoft-store"), (_steam_search, "steam")):
        try:
            found = finder(g["name"])
        except Exception:
            found = None
        if found and found.get("cover"):
            return {**found, "source": label}
    return {"cover": None, "hero": None, "source": None}


def refresh(force=False):
    """Resolve art for games that have none (or stale). Safe to call often; one run at a time."""
    if not _running.acquire(blocking=False):
        return 0
    try:
        now = time.time()
        games = [g for g in db.all_games()
                 if force or not g.get("art") or now - (g["art"].get("ts") or 0) > TTL]
        if not games:
            return 0
        steam_ids = [str((g.get("extra") or {}).get("appid") or g["id"].split(":", 1)[1])
                     for g in games if g["source"] == "steam"]
        try:
            steam_cache = _steam_assets(steam_ids) if steam_ids else {}
        except Exception:
            steam_cache = {}
        for g in games:
            try:
                art = _resolve_one(g, steam_cache)
            except Exception:
                continue  # network hiccup: try again next refresh
            db.set_art(g["id"], {**art, "ts": now})
        return len(games)
    finally:
        _running.release()


def refresh_async(force=False):
    threading.Thread(target=refresh, kwargs={"force": force}, daemon=True, name="art").start()


def for_game(g):
    """Art the UI should use: your override, else the resolved art."""
    art = dict(g.get("art") or {})
    over = g.get("art_override") or {}
    if over.get("cover"):
        art["cover"] = over["cover"]
        art.setdefault("hero", None)
        art["hero"] = over.get("hero") or art.get("hero") or over["cover"]
    if over.get("hero"):
        art["hero"] = over["hero"]
    return {"cover": art.get("cover"), "hero": art.get("hero")}
