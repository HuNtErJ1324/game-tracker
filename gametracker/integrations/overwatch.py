"""Overwatch career stats via OverFast API (community API over Blizzard's public career profiles).

Needs your BattleTag in Settings and your career profile set to Public in-game
(Options > Social > Career Profile Visibility). Blizzard updates profiles with some delay,
so we snapshot at session start (baseline) and a few times after it ends, then diff.
"""
from collections import Counter
from urllib.parse import quote

import requests

from .. import db, net
from . import Integration, fmt_duration

BASE = "https://overfast-api.tekrop.fr"
ROLES = ("tank", "damage", "support", "open")


def _heroes():
    try:
        return {h["key"]: h for h in net.cached_json("ow_heroes", f"{BASE}/heroes")}
    except Exception:
        return {}


def _trim(stats):
    if not stats:
        return None
    return {"general": stats.get("general"), "roles": stats.get("roles"), "heroes": stats.get("heroes")}


def _diff(a, b):
    """Change in games/wins/time between two stat summaries (b newer)."""
    if not a or not b:
        return None
    out = {"games": 0, "won": 0, "lost": 0, "time": 0, "heroes": {}}
    ga, gb = a.get("general") or {}, b.get("general") or {}
    out["games"] = (gb.get("games_played") or 0) - (ga.get("games_played") or 0)
    out["won"] = (gb.get("games_won") or 0) - (ga.get("games_won") or 0)
    out["lost"] = (gb.get("games_lost") or 0) - (ga.get("games_lost") or 0)
    out["time"] = (gb.get("time_played") or 0) - (ga.get("time_played") or 0)
    ha, hb = a.get("heroes") or {}, b.get("heroes") or {}
    for key, s in hb.items():
        before = ha.get(key) or {}
        dt = (s.get("time_played") or 0) - (before.get("time_played") or 0)
        if dt > 0:
            out["heroes"][key] = {
                "time": dt,
                "games": (s.get("games_played") or 0) - (before.get("games_played") or 0),
                "won": (s.get("games_won") or 0) - (before.get("games_won") or 0),
                "lost": (s.get("games_lost") or 0) - (before.get("games_lost") or 0),
                "elims": ((s.get("total") or {}).get("eliminations") or 0) - ((before.get("total") or {}).get("eliminations") or 0),
                "deaths": ((s.get("total") or {}).get("deaths") or 0) - ((before.get("total") or {}).get("deaths") or 0),
                "damage": ((s.get("total") or {}).get("damage") or 0) - ((before.get("total") or {}).get("damage") or 0),
                "healing": ((s.get("total") or {}).get("healing") or 0) - ((before.get("total") or {}).get("healing") or 0),
            }
    return out


DIVISIONS = ["bronze", "silver", "gold", "platinum", "diamond", "master", "grandmaster", "ultimate"]


def _rank_value(r):
    """Bronze 5 = 1 ... Champion 1 = 40, so ranks plot on one axis."""
    if not r or r.get("division") not in DIVISIONS:
        return None
    return DIVISIONS.index(r["division"]) * 5 + (6 - int(r.get("tier") or 5))


def _rank_text(r):
    return f"{r['division'].title()} {r['tier']}" if r else "Unranked"


# ---------------- detailed career stats (per hero, per mode) ----------------

MODES = (("comp", "Competitive"), ("qp", "Quick Play"))
PCT_KEYS = ("accuracy", "_rate", "win_percentage", "of_match_on_fire")
SUFFIXES = (("_avg_per_10_min", "per10"), ("_most_in_game", "best"), ("_best_in_game", "best"),
            ("_most_in_life", "life"), ("_best", "best"), ("_average", "avg"))
ALIASES = {"objective_contest_time": "obj_contest_time", "damage_done": "all_damage_done"}
LABELS = {"all_damage_done": "All damage", "hero_damage_done": "Hero damage", "healing_done": "Healing",
          "obj_contest_time": "Objective contest time", "time_spent_on_fire": "Time on fire",
          "barrier_damage_done": "Barrier damage", "eliminations_per_life": "Eliminations per life",
          "win_percentage": "Win rate", "of_match_on_fire": "Match on fire", "multikill": "Multikill",
          "weapon_accuracy": "Weapon accuracy", "critical_hit_accuracy": "Critical hit accuracy"}
# Rates compared with your all-hero average. Deaths are the one where lower is better.
PER10 = ("eliminations", "final_blows", "solo_kills", "deaths", "assists", "offensive_assists", "defensive_assists",
         "hero_damage_done", "healing_done", "objective_kills", "objective_time", "obj_contest_time", "time_spent_on_fire")
BESTS = (("eliminations_most_in_game", "Most eliminations"), ("final_blows_most_in_game", "Most final blows"),
         ("hero_damage_done_most_in_game", "Most hero damage"), ("healing_done_most_in_game", "Most healing"),
         ("kill_streak_best", "Best kill streak"), ("multikill_best", "Best multikill"),
         ("solo_kills_most_in_game", "Most solo kills"), ("objective_time_most_in_game", "Most objective time"))
BEST_ORDER = ("eliminations", "final_blows", "hero_damage_done", "all_damage_done", "healing_done", "kill_streak",
              "multikill", "solo_kills", "assists", "offensive_assists", "defensive_assists", "objective_kills", "objective_time")
ROLE_PLURAL = {"tank": "tanks", "damage": "damage heroes", "support": "supports"}
CAREER_HISTORY = 60  # snapshots read for a hero page (each is a full career profile)


def _norm(k):
    for a, b in ALIASES.items():
        if k.startswith(a):
            return b + k[len(a):]
    return k


def _flat(h):
    """One hero's stats from every category in one dict, with Blizzard's two spellings of some keys merged."""
    out = {}
    for cat in (h or {}).values():
        if isinstance(cat, dict):
            out.update({_norm(k): v for k, v in cat.items()})
    return out


def _split(key):
    for suf, kind in SUFFIXES:
        if key.endswith(suf):
            return key[:-len(suf)], kind
    return key, "total"


def _label(base):
    if base in LABELS:
        return LABELS[base]
    s = base.replace("obj_", "objective_").replace("_", " ")
    return s[:1].upper() + s[1:]


def _is_pct(key):
    return any(p in key for p in PCT_KEYS)


def _fmt(key, v):
    if v is None:
        return "–"
    if _is_pct(key):
        return f"{v:g}%"
    if "time" in key and "per_life" not in key:  # seconds
        v = int(v or 0)
        return fmt_duration(v) if v >= 3600 else f"{v // 60}:{v % 60:02d}"
    if isinstance(v, float) and not v.is_integer():
        return f"{v:,.2f}"
    return f"{int(v):,}"


def _secs(f):
    return f.get("time_played") or 0


def _career(game):
    snap = db.latest_snapshot(game["id"], "ow_career")
    return snap["data"] if snap else None


def _played(career):
    """(mode, hero key, flat stats) for every hero with time in that mode, most played first."""
    out = []
    for mode, _ in MODES:
        for k, h in ((career or {}).get(mode) or {}).items():
            f = _flat(h)
            if k != "all-heroes" and _secs(f):
                out.append((mode, k, f))
    return sorted(out, key=lambda t: -_secs(t[2]))


def _vs(key, v, avg):
    """'+12%' against your all-hero average, toned good or bad once it's 5% or more either way."""
    if v is None or not avg:
        return "–"
    d = round(100 * (v - avg) / avg)
    better = d < 0 if key.startswith("deaths") else d > 0
    return {"text": f"{d:+d}%" if d else "0%", "tone": ("good" if better else "bad") if abs(d) >= 5 else None}


def hero_href(gid, key):
    return f"#/hero/{quote(gid, safe='')}/{quote(key, safe='')}"


def _hero_cell(heroes, k):
    return {"text": heroes.get(k, {}).get("name", k), "icon": heroes.get(k, {}).get("portrait")}


class Overwatch(Integration):
    key = "overwatch"
    title = "Overwatch career"
    sync_on_start = True
    sync_while_playing = True
    sync_delays = (60, 900, 2700)

    def applies(self, game):
        return "overwatch" in (game.get("name") or "").lower()

    def sync(self, game, sink):
        tag = "".join((sink.settings.get("overwatch_battletag") or "").split())  # "Name #1234" -> "Name#1234"
        if not tag:
            raise net.Unavailable("Add your BattleTag in Settings to track Overwatch stats.")
        pid = requests.utils.quote(tag.replace("#", "-"))
        try:
            summary = net.get_json(f"{BASE}/players/{pid}/summary", timeout=30)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                raise net.Unavailable(f"OverFast can't find {tag}. Check the BattleTag (case-sensitive).")
            raise
        comp = net.get_json(f"{BASE}/players/{pid}/stats/summary?gamemode=competitive", timeout=30)
        qp = net.get_json(f"{BASE}/players/{pid}/stats/summary?gamemode=quickplay", timeout=30)
        if not (comp or {}).get("general") and not (qp or {}).get("general"):
            raise net.Unavailable("Career profile looks private. In Overwatch: Options > Social > "
                                  "Career Profile Visibility > Public.")
        ranks = (summary.get("competitive") or {})
        plat = ranks.get("pc") or ranks.get("console") or {}
        sink.snapshot(game["id"], "ow", {
            "username": summary.get("username"), "avatar": summary.get("avatar"),
            "endorsement": (summary.get("endorsement") or {}).get("level"),
            "season": plat.get("season"),
            "ranks": {r: plat.get(r) for r in ROLES if plat.get(r)},
            "comp": _trim(comp), "qp": _trim(qp),
            "profile_updated": summary.get("last_updated_at"),
        })
        g = (comp or {}).get("general") or {}
        msg = f"{g.get('games_played', 0)} competitive games on record"
        try:
            career = {mode: net.get_json(f"{BASE}/players/{pid}/stats/career?gamemode={gm}", timeout=30) or {}
                      for mode, gm in (("comp", "competitive"), ("qp", "quickplay"))}
        except Exception as e:  # the summary above is still worth keeping
            return f"{msg} (detailed hero stats unavailable: {type(e).__name__})"
        sink.snapshot(game["id"], "ow_career", career)
        n = len({k for k in list(career["comp"]) + list(career["qp"]) if k != "all-heroes"})
        return f"{msg}, detailed stats for {n} heroes"

    # ---------- dashboard ----------

    def _last_session_delta(self, game):
        sess = db.sessions(game["id"], limit=1)
        if not sess:
            return None, None
        s = sess[0]
        before = db.latest_snapshot(game["id"], "ow", before=s["start_ts"] + 120)
        after = db.latest_snapshot(game["id"], "ow")
        if not before or not after or after["id"] == before["id"]:
            return s, None
        return s, {m: _diff(before["data"].get(m), after["data"].get(m)) for m in ("comp", "qp")}

    def modes(self, game):
        snap = db.latest_snapshot(game["id"], "ow")
        out = []
        for key, label in MODES:  # Competitive first, so it's the default when you play it
            g = (((snap or {}).get("data") or {}).get(key) or {}).get("general") or {}
            if g.get("games_played") or g.get("time_played"):
                out.append({"key": key, "label": label, "count": g.get("games_played") or 0})
        if out:
            out[0]["default"] = True
        return out

    def panel(self, game, mode=None):
        snap = db.latest_snapshot(game["id"], "ow")
        if not snap:
            return {"title": self.title, "notes": ["No data yet. Add your BattleTag in Settings, make your career "
                                                   "profile public, then Sync."]}
        mode = mode if mode in dict(MODES) else "comp"
        d, heroes = snap["data"], _heroes()
        panels = [self._career_panel(game, d, heroes, mode)]
        hp = self._heroes_panel(game, _career(game), heroes, mode)
        if hp:
            panels.append(hp)
        sessions = self._session_rows(game, heroes, mode)
        if sessions:
            panels.append({"title": f"Overwatch sessions, {dict(MODES)[mode]}", "tables": [{"title": "What changed each time you played", "columns":
                           ["When", "Length", "Games", "Result", "Heroes", "Elims / deaths", "Damage", "Healing"], "rows": sessions}],
                           "notes": ["Worked out from your career profile before and after each session. Blizzard updates it "
                                     "with a delay, so the newest session can take a while to fill in."]})
        return panels

    def _career_panel(self, game, d, heroes, mode="comp"):
        p = {"title": self.title, "tiles": [], "tables": [], "notes": [], "bars": []}
        ml = dict(MODES)[mode]
        for role in ROLES if mode == "comp" else ():
            r = d["ranks"].get(role)
            if r:
                p["tiles"].append({"label": f"{role.title()} rank", "value": _rank_text(r), "icon": r.get("rank_icon"),
                                   "sub": f"Season {d.get('season')}" if d.get("season") else None})
        g = ((d.get(mode) or {}).get("general")) or {}
        if g.get("games_played"):
            p["tiles"].append({"label": f"{ml} win rate", "value": f"{g['winrate']:.0f}%",
                               "sub": f"{g['games_won']}W {g['games_lost']}L in {g['games_played']} games"})
            p["tiles"].append({"label": "KDA", "value": f"{g['kda']:.2f}", "sub": f"{fmt_duration(g['time_played'])} played"})

        roles = ((d.get(mode) or {}).get("roles")) or {}
        total = sum((v or {}).get("time_played") or 0 for v in roles.values())
        if total:
            p["bars"].append({"title": f"{ml} time by role", "items": [
                {"label": r.title(), "value": v.get("time_played") or 0, "group": r,
                 "display": f"{100 * (v.get('time_played') or 0) // total}%",
                 "sub": f"{v.get('winrate', 0):.0f}% WR in {v.get('games_played', 0)} games, KDA {v.get('kda', 0):.2f}"}
                for r, v in sorted(roles.items(), key=lambda t: -((t[1] or {}).get("time_played") or 0)) if v]})
        for m, label in ((mode, f"{ml} hero pool"),):
            pool = ((d.get(m) or {}).get("heroes")) or {}
            top = sorted(pool.items(), key=lambda t: -(t[1].get("time_played") or 0))[:12]
            if top:
                p["bars"].append({"title": label, "items": [
                    {"label": heroes.get(k, {}).get("name", k), "icon": heroes.get(k, {}).get("portrait"),
                     "href": hero_href(game["id"], k),
                     "value": h.get("time_played") or 0, "display": fmt_duration(h.get("time_played")),
                     "group": heroes.get(k, {}).get("role"),
                     "sub": f"{h.get('winrate', 0):.0f}% WR, {h.get('games_played', 0)} games, KDA {h.get('kda', 0):.2f}"}
                    for k, h in top]})

        mode_heroes = ((d.get(mode) or {}).get("heroes")) or {}
        if mode_heroes:
            top = sorted(mode_heroes.items(), key=lambda t: -(t[1].get("time_played") or 0))[:12]
            p["tables"].append({"title": f"{ml} heroes, per 10 minutes", "columns":
                                ["Hero", "Time", "Games", "Win %", "KDA", "Elims", "Deaths", "Damage", "Healing"], "rows": [
                [{"text": heroes.get(k, {}).get("name", k), "icon": heroes.get(k, {}).get("portrait")},
                 fmt_duration(h.get("time_played")), h.get("games_played"),
                 {"text": f"{h.get('winrate', 0):.0f}%", "tone": "good" if (h.get("winrate") or 0) >= 50 else "bad"},
                 f"{h.get('kda', 0):.2f}", (h.get("average") or {}).get("eliminations"), (h.get("average") or {}).get("deaths"),
                 f"{(h.get('average') or {}).get('damage', 0):,.0f}", f"{(h.get('average') or {}).get('healing', 0):,.0f}"]
                for k, h in top], "links": [hero_href(game["id"], k) for k, _ in top], "link_title": "Open hero"})

        history = list(reversed(db.snapshots(game["id"], "ow", 300))) if mode == "comp" else []
        series = {}
        for snap in history:
            for role, r in (snap["data"].get("ranks") or {}).items():
                v = _rank_value(r)
                if v is None:
                    continue
                pts = series.setdefault(role, [])
                if not pts or pts[-1]["v"] != v:
                    pts.append({"ts": snap["ts"], "v": v, "label": _rank_text(r)})
        if any(len(pts) >= 2 for pts in series.values()):
            p["timeline"] = {"title": "Rank history", "series": [{"name": r.title(), "group": r, "points": pts}
                                                                 for r, pts in series.items()],
                             "ticks": [{"v": i * 5 + 1, "label": DIVISIONS[i].title()} for i in range(len(DIVISIONS))]}
        elif history:
            p["notes"].append("Rank history builds up as the tracker keeps syncing your profile.")
        return p

    def _heroes_panel(self, game, career, heroes, mode="comp"):
        played = [x for x in _played(career) if x[0] == mode]
        if not played:
            return None
        p = {"title": "Hero details", "tiles": [], "tables": [], "notes": []}
        for key, label in BESTS:
            v, _, k = max(((f.get(key) or 0, m, k) for m, k, f in played), default=(0, None, None))
            if v:
                p["tiles"].append({"label": label, "value": _fmt(key, v), "sub": heroes.get(k, {}).get("name", k)})
        per10 = lambda f, s: _fmt(s, f.get(f"{s}_avg_per_10_min"))
        for tm, mlabel in MODES:
            rows = [(k, f) for m, k, f in played if m == tm][:12]
            if rows:
                p["tables"].append({
                    "title": f"Accuracy and impact, {mlabel}",
                    "columns": ["Hero", "Time", "Weapon acc.", "Crit acc.", "Elims / life", "Final blows / 10",
                                "Solo kills / 10", "Obj. time / 10", "On fire / 10"],
                    "rows": [[_hero_cell(heroes, k), fmt_duration(_secs(f)),
                              _fmt("weapon_accuracy", f.get("weapon_accuracy")),
                              _fmt("critical_hit_accuracy", f.get("critical_hit_accuracy")),
                              _fmt("eliminations_per_life", f.get("eliminations_per_life")),
                              per10(f, "final_blows"), per10(f, "solo_kills"), per10(f, "objective_time"),
                              per10(f, "time_spent_on_fire")] for k, f in rows],
                    "links": [hero_href(game["id"], k) for k, _ in rows], "link_title": "Open hero"})
        p["notes"].append(f"Records are your best single {dict(MODES)[mode]} game on any hero. Open a hero for ability stats, personal bests, "
                          "how they compare with your other heroes, and how each session on them went.")
        return p

    def detail(self, game, key):
        """A hero page: everything Blizzard's career profile has for one hero, both modes."""
        heroes = _heroes()
        career = _career(game) or {}
        raw = {m: ((career.get(m) or {}).get(key)) or {} for m, _ in MODES}
        f = {m: _flat(raw[m]) for m, _ in MODES}
        if key not in heroes and not any(f.values()):
            return None
        allh = {m: _flat((career.get(m) or {}).get("all-heroes")) for m, _ in MODES}
        hero = heroes.get(key, {})
        modes = [(m, ml) for m, ml in MODES if _secs(f[m])]
        main = modes[0][0] if modes else "comp"
        out = {"title": hero.get("name", key), "icon": hero.get("portrait"),
               "sub": f"{hero['role'].title()} hero" if hero.get("role") else None, "facts": [], "panels": []}
        if not modes:
            out["panels"].append({"title": "No detailed stats yet", "notes": [
                "Detailed hero stats come with the next sync of your career profile, and only for heroes you've played."]})
            return out

        facts = out["facts"]
        facts.append({"v": fmt_duration(sum(_secs(x) for x in f.values())), "l": "Time played"})
        for m, ml in modes:
            if f[m].get("games_played"):
                won, lost = f[m].get("games_won") or 0, f[m].get("games_lost") or 0
                facts.append({"v": f"{won}W {lost}L", "l": ml, "tone": "good" if won > lost else "bad" if lost > won else None})
        for k in ("weapon_accuracy", "critical_hit_accuracy", "eliminations_per_life", "final_blows_avg_per_10_min"):
            if f[main].get(k) is not None:
                facts.append({"v": _fmt(k, f[main][k]), "l": {"final_blows_avg_per_10_min": "Final blows / 10 min",
                                                                "eliminations_per_life": "Elims per life"}.get(k, _label(k))})

        # Accuracy-type stats as tiles, with the best game and the other mode alongside.
        acc = {"title": "Accuracy", "tiles": []}
        hs_keys = {m: {_norm(k) for k in (raw[m].get("hero_specific") or {})} for m, _ in MODES}
        for k, v in f[main].items():
            base, kind = _split(k)
            if _is_pct(k) and kind == "total" and k != "win_percentage":
                sub = [f"best game {_fmt(k, f[main].get(base + '_best_in_game'))}" if f[main].get(base + "_best_in_game") else None]
                other = [m for m, _ in modes if m != main]
                if other and f[other[0]].get(k) is not None:
                    sub.append(f"{dict(MODES)[other[0]]} {_fmt(k, f[other[0]][k])}")
                acc["tiles"].append({"label": _label(base), "value": _fmt(k, v), "sub": ", ".join(x for x in sub if x) or None})
        first = ("Weapon accuracy", "Critical hit accuracy")
        acc["tiles"].sort(key=lambda t: (first.index(t["label"]) if t["label"] in first else len(first), t["label"]))
        snaps = list(reversed(db.snapshots(game["id"], "ow_career", CAREER_HISTORY)))
        series = []
        for k, group in (("weapon_accuracy", "weapon"), ("critical_hit_accuracy", "critical")):
            pts = []
            for s in snaps:
                v = _flat(((s["data"].get(main) or {}).get(key))).get(k)
                if v is not None and (not pts or pts[-1]["v"] != v):
                    pts.append({"ts": s["ts"], "v": v, "label": f"{v:g}%"})
            if len(pts) >= 2:
                series.append({"name": _label(k), "group": group, "points": pts})
        if series:
            vals = [pt["v"] for sr in series for pt in sr["points"]]
            span = max(vals) - min(vals) + 6  # the chart pads 3 points either side
            step = 1 if span <= 12 else 2 if span <= 24 else 5
            acc["timeline"] = {"title": f"Career accuracy over time, {dict(MODES)[main]}", "series": series,
                               "ticks": [{"v": v, "label": f"{v}%"} for v in range(0, 101, step)]}
        if acc["tiles"]:
            out["panels"].append(acc)

        # Per-10-minute rates against your other heroes of the same role (time-weighted), else all heroes.
        role, played = hero.get("role"), _played(career)
        cols, rows, base_of = ["Stat"], [], {}
        for m, ml in modes:
            peers = [pf for pm, pk, pf in played if pm == m and pk != key and heroes.get(pk, {}).get("role") == role] if role else []
            if peers:
                def avg(k, peers=peers):
                    have = [(pf[k], _secs(pf)) for pf in peers if pf.get(k) is not None]
                    t = sum(w for _, w in have)
                    # Only compare when most of your time on those heroes has this stat (some are hero-specific).
                    return sum(v * w for v, w in have) / t if t and t >= 0.5 * sum(_secs(pf) for pf in peers) else None
                base_of[m], label = avg, f"vs your other {ROLE_PLURAL.get(role, role + ' heroes')}"
            else:
                base_of[m], label = (lambda k, m=m: allh[m].get(k)), "vs all your heroes"
            cols += [ml, label]
        for base in PER10:
            k = f"{base}_avg_per_10_min"
            if any(f[m].get(k) is not None for m, _ in modes):
                row = [_label(base)]
                for m, _ in modes:
                    row += [_fmt(k, f[m].get(k)), _vs(base, f[m].get(k), base_of[m](k))]
                rows.append(row)
        if rows:
            out["panels"].append({"title": "Per 10 minutes", "tables": [{"title": "Compared with your other heroes", "columns": cols, "rows": rows}],
                                  "notes": ["Compared with the other heroes of the same role you've played in that mode, weighted by "
                                            "time played. Green and red mark a 5% or bigger gap; fewer deaths counts as better."]})

        # Ability and hero-specific stats, one row per stat.
        abil = {"title": "Abilities and hero stats", "tables": []}
        for m, ml in modes:
            grouped = {}
            for k in sorted(hs_keys[m]):
                base, kind = _split(k)
                if not _is_pct(k):
                    grouped.setdefault(base, {})[kind] = f[m].get(k)
            if grouped:
                kinds = [k for k in ("total", "per10", "best", "avg")  # Blizzard's per-game "average" is often just 0
                         if any(v.get(k) not in ((None, 0) if k == "avg" else (None,)) for v in grouped.values())]
                names = {"total": "Total", "per10": "Per 10 min", "best": "Best in a game", "avg": "Average"}
                abil["tables"].append({"title": ml, "columns": ["Stat"] + [names[k] for k in kinds],
                                       "rows": [[_label(b)] + [_fmt(b, v.get(k)) for k in kinds]
                                                for b, v in sorted(grouped.items(), key=lambda t: -(t[1].get("total") or 0))]})
        if abil["tables"]:
            out["panels"].append(abil)

        # Personal bests outside the hero-specific ones.
        best_keys = sorted({k for m, _ in modes for k in f[m]
                            if _split(k)[1] in ("best", "life") and not _is_pct(k) and k not in hs_keys[m]},
                           key=lambda k: (_split(k)[1] == "life", BEST_ORDER.index(_split(k)[0])
                                          if _split(k)[0] in BEST_ORDER else len(BEST_ORDER), k))
        if best_keys:
            out["panels"].append({"title": "Personal bests", "tables": [{
                "title": "Best single game", "columns": ["Record"] + [ml for _, ml in modes],
                "rows": [[_label(_split(k)[0]) + (" in one life" if _split(k)[1] == "life" else "")]
                         + [_fmt(k, f[m].get(k)) for m, _ in modes] for k in best_keys]}]})

        sess = self._hero_sessions(snaps, key)
        out["panels"].append({"title": f"Each time you played {out['title']}", "tables": [{
            "title": "Change in your career profile between syncs",
            "columns": ["When", "Mode", "Time", "Games", "Result", "Elims / 10", "Deaths / 10", "Final blows / 10",
                        "Damage / 10", "Healing / 10"], "rows": sess}],
            "notes": [] if sess else ["Fills in from your next session on this hero: the tracker compares your profile "
                                      "before and after you play. Several games in one session show as one row."]})
        return out

    @staticmethod
    def _hero_sessions(snaps, key):
        rows = []
        for a, b in zip(snaps, snaps[1:]):
            for m, ml in MODES:
                fa, fb = _flat(((a["data"].get(m)) or {}).get(key)), _flat(((b["data"].get(m)) or {}).get(key))
                dt = _secs(fb) - _secs(fa)
                if dt <= 0:
                    continue
                d = lambda s: (fb.get(s) or 0) - (fa.get(s) or 0)
                rate = lambda s, nd=1: f"{600 * d(s) / dt:,.{nd}f}"
                won, lost = d("games_won"), d("games_lost")
                rows.append([{"ts": b["ts"]}, ml, fmt_duration(dt), d("games_played"),
                             {"text": f"{won}W {lost}L", "tone": "good" if won > lost else "bad" if lost > won else None},
                             rate("eliminations"), rate("deaths"), rate("final_blows"),
                             rate("hero_damage_done", 0), rate("healing_done", 0)])
        return rows[::-1][:20]

    def _session_rows(self, game, heroes, mode=None):
        sess = sorted(db.sessions(game["id"], limit=30), key=lambda s: s["start_ts"])
        rows = []
        for i, s in enumerate(sess):
            nxt = sess[i + 1]["start_ts"] if i + 1 < len(sess) else None
            before = db.latest_snapshot(game["id"], "ow", before=s["start_ts"] + 120)
            after = db.latest_snapshot(game["id"], "ow", before=(nxt + 120) if nxt else None)
            if not before or not after or after["id"] == before["id"]:
                continue
            games = won = lost = 0
            hero_time, elims, deaths, dmg, heal = Counter(), 0, 0, 0, 0
            for m in (mode,) if mode else ("comp", "qp"):
                dm = _diff(before["data"].get(m), after["data"].get(m))
                if not dm:
                    continue
                games, won, lost = games + dm["games"], won + dm["won"], lost + dm["lost"]
                for k, h in dm["heroes"].items():
                    hero_time[k] += h["time"]
                    elims, deaths, dmg, heal = elims + h["elims"], deaths + h["deaths"], dmg + h["damage"], heal + h["healing"]
            if not games and not hero_time:
                continue
            top = ", ".join(f"{heroes.get(k, {}).get('name', k)} {fmt_duration(t)}" for k, t in hero_time.most_common(3))
            rows.append([{"ts": s["start_ts"]}, fmt_duration(s["end_ts"] - s["start_ts"]), games,
                         {"text": f"{won}W {lost}L", "tone": "good" if won > lost else "bad" if lost > won else None},
                         top, f"{elims} / {deaths}", f"{dmg:,}", f"{heal:,}"])
        return rows[::-1][:15]

    def coach_context(self, game):
        snap = db.latest_snapshot(game["id"], "ow")
        if not snap:
            return ""
        d, heroes = snap["data"], _heroes()
        lines = ["Overwatch ranks: " + ", ".join(f"{r} {_rank_text(v)}" for r, v in d["ranks"].items())]
        for mode in ("comp", "qp"):
            g = ((d.get(mode) or {}).get("general")) or {}
            if g:
                lines.append(f"{mode}: {g.get('games_played')} games, {g.get('winrate')}% WR, KDA {g.get('kda')}, "
                             f"per-10-min averages {g.get('average')}")
            for k, h in sorted((((d.get(mode) or {}).get("heroes")) or {}).items(),
                               key=lambda t: -(t[1].get("time_played") or 0))[:10]:
                lines.append(f"  - {heroes.get(k, {}).get('name', k)}: {fmt_duration(h.get('time_played'))}, "
                             f"{h.get('games_played')} games, {h.get('winrate')}% WR, KDA {h.get('kda')}, per-10 {h.get('average')}")
        played = _played(_career(game))
        if played:
            lines.append("Detailed hero stats from the career profile (per 10 min unless noted):")
            for mode, k, f in played[:12]:
                stat = lambda key, label: f"{label} {_fmt(key, f[key])}" if f.get(key) is not None else None
                core = [stat("weapon_accuracy", "weapon acc"), stat("critical_hit_accuracy", "crit acc"),
                        stat("eliminations_per_life", "elims/life"), stat("final_blows_avg_per_10_min", "final blows"),
                        stat("solo_kills_avg_per_10_min", "solo kills"), stat("objective_time_avg_per_10_min", "obj time"),
                        stat("deaths_avg_per_10_min", "deaths")]
                hs = ((((_career(game) or {}).get(mode) or {}).get(k) or {}).get("hero_specific") or {})
                abil = [f"{_label(_split(_norm(x))[0])} {_fmt(x, v)}" for x, v in hs.items()
                        if x.endswith("_avg_per_10_min") or (_is_pct(x) and _split(x)[1] == "total")]
                bests = [stat(key, label.replace("Most ", "").replace("Best ", "").lower()) for key, label in BESTS]
                lines.append(f"  - {heroes.get(k, {}).get('name', k)} ({mode}, {fmt_duration(_secs(f))}): "
                             + ", ".join(x for x in core if x)
                             + (f"; abilities: {', '.join(abil)}" if abil else "")
                             + f"; best game: {', '.join(x for x in bests if x)}")
        _, delta = self._last_session_delta(game)
        if delta:
            lines.append(f"Last session change: {delta}")
        return "\n".join(lines)
