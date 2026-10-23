"""League of Legends via the League Client's local API (LCU) and the in-game Live Client Data API."""
import os
import statistics
from collections import defaultdict

import requests

from .. import db, net
from . import Integration

GAME_ID = "riot:league_of_legends"
QUEUES = {420: "Ranked Solo/Duo", 440: "Ranked Flex", 400: "Draft", 430: "Blind", 450: "ARAM",
          490: "Quickplay", 1700: "Arena", 1900: "URF", 900: "ARURF"}


class League(Integration):
    key = "league"
    title = "League of Legends"
    sync_delays = (90, 600)

    def applies(self, game):
        return game.get("id") == GAME_ID

    @staticmethod
    def _lcu(game):
        try:
            with open(os.path.join(game["install_dir"], "lockfile"), encoding="utf-8") as f:
                _, _, port, password, _ = f.read().split(":")
        except (OSError, ValueError):
            raise net.Unavailable("Open the League client to sync stats.")
        return lambda path: net.local_get(port, password, path)

    def sync(self, game, sink):
        get = self._lcu(game)
        try:
            me = get("/lol-summoner/v1/current-summoner")
        except requests.RequestException:
            raise net.Unavailable("League client is still starting; try again shortly.")
        ranked = get("/lol-ranked/v1/current-ranked-stats")
        sink.snapshot(GAME_ID, "lol_rank", {
            "name": f"{me.get('gameName') or me.get('displayName')}#{me.get('tagLine', '')}",
            "level": me.get("summonerLevel"),
            "queues": [{"queue": q.get("queueType"), "tier": q.get("tier"), "division": q.get("division"),
                        "lp": q.get("leaguePoints"), "wins": q.get("wins"), "losses": q.get("losses")}
                       for q in ranked.get("queues") or [] if q.get("tier") not in (None, "", "NONE")],
        })
        champs = {c["id"]: c["name"] for c in get("/lol-game-data/assets/v1/champion-summary.json")}
        games = (get("/lol-match-history/v1/products/lol/current-summoner/matches?begIndex=0&endIndex=19")
                 .get("games") or {}).get("games") or []
        known, new = sink.known_matches(GAME_ID), 0
        for g in games:
            mid = f"lol:{g['gameId']}"
            if mid in known or not g.get("participants"):
                continue
            p = g["participants"][0]
            st = p.get("stats") or {}
            sink.match(mid, GAME_ID, g["gameCreation"] / 1000, {
                "start": g["gameCreation"] / 1000, "length_sec": g.get("gameDuration"),
                "queue": QUEUES.get(g.get("queueId"), g.get("gameMode", "").title()),
                "champion": champs.get(p.get("championId"), str(p.get("championId"))),
                "kills": st.get("kills", 0), "deaths": st.get("deaths", 0), "assists": st.get("assists", 0),
                "cs": st.get("totalMinionsKilled", 0) + st.get("neutralMinionsKilled", 0),
                "gold": st.get("goldEarned"), "damage": st.get("totalDamageDealtToChampions"),
                "vision": st.get("visionScore"), "won": bool(st.get("win")),
            })
            new += 1
        return f"{new} new match{'es' if new != 1 else ''}"

    def live(self, game):
        try:
            d = net.http.get("https://127.0.0.1:2999/liveclientdata/allgamedata", verify=False, timeout=1).json()
        except (requests.RequestException, ValueError):
            return None
        me_id = (d.get("activePlayer") or {}).get("riotId") or (d.get("activePlayer") or {}).get("summonerName")
        me = next((p for p in d.get("allPlayers") or [] if me_id in (p.get("riotId"), p.get("summonerName"))), None)
        if not me:
            return None
        s, t = me.get("scores") or {}, int((d.get("gameData") or {}).get("gameTime") or 0)
        return {"headline": f"{me.get('championName')} · {s.get('kills', 0)}/{s.get('deaths', 0)}/{s.get('assists', 0)}",
                "details": [f"{s.get('creepScore', 0)} CS", f"{t // 60}:{t % 60:02d}",
                            (d.get("gameData") or {}).get("gameMode", "").title()]}

    def panel(self, game):
        p = {"title": self.title, "tiles": [], "tables": [], "notes": []}
        rank = db.latest_snapshot(GAME_ID, "lol_rank")
        if rank:
            for q in rank["data"]["queues"]:
                games = (q["wins"] or 0) + (q["losses"] or 0)
                p["tiles"].append({"label": q["queue"].replace("RANKED_", "").replace("_", " ").title(),
                                   "value": f"{q['tier'].title()} {q['division'] if q['division'] != 'NA' else ''}".strip(),
                                   "sub": f"{q['lp']} LP · {q['wins']}W {q['losses']}L ({100 * (q['wins'] or 0) // max(1, games)}%)"})
        ms = [m["data"] for m in db.matches(GAME_ID, 50)]
        if not ms:
            p["notes"].append("No matches synced yet. Open the League client and press Sync.")
            return p
        recent = ms[:20]
        k, d, a = (sum(m[x] for m in recent) for x in ("kills", "deaths", "assists"))
        p["tiles"] += [
            {"label": "Win rate (last 20)", "value": f"{100 * sum(m['won'] for m in recent) // len(recent)}%"},
            {"label": "KDA", "value": f"{(k + a) / max(1, d):.2f}", "sub": f"{k}/{d}/{a}"},
            {"label": "CS / min", "value": f"{statistics.mean(m['cs'] / max(1, (m['length_sec'] or 60) / 60) for m in recent):.1f}"},
        ]
        champs = defaultdict(list)
        for m in ms:
            champs[m["champion"]].append(m)
        p["tables"].append({"title": "Champions", "columns": ["Champion", "Games", "Win %", "KDA"], "rows": [
            [n, len(g), f"{100 * sum(x['won'] for x in g) // len(g)}%",
             f"{sum(x['kills'] + x['assists'] for x in g) / max(1, sum(x['deaths'] for x in g)):.2f}"]
            for n, g in sorted(champs.items(), key=lambda t: -len(t[1]))[:10]]})
        p["tables"].append({"title": "Recent matches", "columns": ["When", "Queue", "Champion", "Result", "K/D/A", "CS", "Damage"],
                            "rows": [[{"ts": m["start"]}, m["queue"], m["champion"],
                                      {"text": "Win" if m["won"] else "Loss", "tone": "good" if m["won"] else "bad"},
                                      f"{m['kills']}/{m['deaths']}/{m['assists']}", m["cs"], m.get("damage")]
                                     for m in ms[:20]]})
        return p

    def coach_context(self, game):
        rank = db.latest_snapshot(GAME_ID, "lol_rank")
        lines = [f"Ranked: {q['queue']} {q['tier']} {q['division']} {q['lp']}LP ({q['wins']}W/{q['losses']}L)"
                 for q in (rank["data"]["queues"] if rank else [])]
        for m in [m["data"] for m in db.matches(GAME_ID, 25)]:
            lines.append(f"- {m['queue']} {m['champion']}: {'W' if m['won'] else 'L'} {m['kills']}/{m['deaths']}/{m['assists']}, "
                         f"{m['cs']} CS in {int((m['length_sec'] or 0) // 60)}m, dmg {m.get('damage')}, vision {m.get('vision')}")
        return "League of Legends recent games (newest first):\n" + "\n".join(lines) if lines else ""
