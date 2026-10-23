"""Per-game stat sources.

Each integration produces a "panel" the dashboard renders generically:
    {"title", "status", "tiles": [{label, value, sub, tone, icon}],
     "tables": [{title, columns, rows}], "notes": [str], ...extra keys}
Table cells are strings/numbers or {"text", "tone", "icon"} or {"ts": unix}; tone is "good" | "bad" | None.

sync() runs on the gaming PC (agent) and writes through a sink:
    sink.snapshot(game_id, kind, data) / sink.match(match_id, game_id, ts, data) / sink.known_matches(game_id)
panel() and coach_context() run on the server and read from the database.
"""


class Integration:
    key = ""
    title = ""
    sync_on_start = False      # take a baseline snapshot when a session starts
    sync_delays = (30,)        # seconds after a session ends to sync (can be several)
    sync_while_playing = False  # also sync every auto_sync_minutes during a session

    def applies(self, game):
        raise NotImplementedError

    def sync(self, game, sink):
        """Agent side: fetch fresh data and hand it to the sink. Returns a short status string."""
        raise NotImplementedError

    def panel(self, game):
        raise NotImplementedError

    def modes(self, game):
        """Game modes the panels can be split by: [{key, label, count, default?}]. When there are any,
        panel() is called as panel(game, mode)."""
        return []

    def live(self, game):
        """Optional in-match data for the Now Playing card."""
        return None

    def coach_context(self, game):
        """Compact text summary handed to Claude."""
        return ""

    def detail(self, game, key):
        """Optional sub-page of a game (e.g. one Overwatch hero): {title, icon, sub, facts, panels} or None."""
        return None


def fmt_duration(sec):
    sec = int(sec or 0)
    h, m = divmod(sec // 60, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def all_integrations():
    from .achievements import SteamAchievements
    from .cs2 import CS2
    from .league import League
    from .overwatch import Overwatch
    from .valorant import Valorant
    return [Valorant(), League(), Overwatch(), CS2(), SteamAchievements()]
