"""Tools for the coach agent, copied into its workspace as tracker_tools.py (see AGENTS.md there).

The database the agent sees is a read-only snapshot, so knowledge-base changes are queued in outbox/ and the tracker
applies them when the answer is sent. Web search goes through the Claude API key the agent runs with.
"""
import html
import json
import os
import re
import time
import urllib.error
import urllib.request

OUTBOX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outbox")


def _queue(op):
    os.makedirs(OUTBOX, exist_ok=True)
    with open(os.path.join(OUTBOX, f"{os.getpid()}.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({**op, "ts": time.time()}) + "\n")


def save_note(game_id, topic, title, points, source_url=None, replaces=None):
    """Add a note to the player's guides knowledge base, or replace note id `replaces` with it.

    topic: what it's about (a hero, agent, map, mechanic, boss...). title: a short imperative.
    points: 2-4 short strings in your own words. source_url: the page or video it came from, if any."""
    if isinstance(points, str):
        points = [points]
    points = [" ".join(str(p).split()) for p in points or [] if str(p).strip()]
    if not (str(game_id).strip() and str(topic).strip() and str(title).strip() and points):
        raise ValueError("save_note needs game_id, topic, title and at least one point")
    if source_url and not str(source_url).startswith(("http://", "https://")):
        raise ValueError("source_url must be a web link")
    _queue({"op": "add", "game_id": str(game_id), "topic": str(topic), "title": str(title), "points": points[:6],
            "source": source_url or "", "replaces": int(replaces) if replaces else None})
    return "Queued: the note is saved when your answer is sent."


def delete_note(game_id, note_id):
    """Remove a note (wrong, outdated after a patch, or a duplicate)."""
    _queue({"op": "delete", "game_id": str(game_id), "note_id": int(note_id)})
    return "Queued: the note is removed when your answer is sent."


def read_video(game_id, url):
    """Have the tracker read a YouTube guide into the knowledge base. It runs after your answer and takes a minute."""
    if not re.search(r"(youtube\.com|youtu\.be)/", str(url)) and not re.fullmatch(r"[A-Za-z0-9_-]{11}", str(url)):
        raise ValueError("read_video takes a YouTube link")
    _queue({"op": "read_video", "game_id": str(game_id), "url": str(url)})
    return "Queued: the tracker reads it after your answer."


def web_search(query, max_searches=3):
    """Search the web. Returns the findings as short bullets with the source link after each one."""
    if os.environ.get("GAMETRACKER_WEB_SEARCH") != "1":
        return "Web search is turned off in the tracker's Settings."
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return "Web search isn't available here: there's no API key in this environment."
    messages = [{"role": "user", "content": "Search the web for this and report what you find as short bullet points, "
                 "each followed by its source URL. Say plainly if the results don't answer it.\n\n" + str(query)}]
    body = {"model": os.environ.get("GAMETRACKER_SEARCH_MODEL") or "claude-haiku-5-5", "max_tokens": 4000,
            "tools": [{"type": "web_search_20260209", "name": "web_search", "max_uses": int(max_searches)}],
            "messages": messages}
    resp = {}
    for _ in range(4):  # the server-side search loop can pause; resume it where it stopped
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(), headers={
            "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                resp = json.load(r)
        except urllib.error.HTTPError as e:
            return f"Web search failed: HTTP {e.code}: {e.read()[:300].decode(errors='replace')}"
        if resp.get("stop_reason") != "pause_turn":
            break
        body["messages"] = messages + [{"role": "assistant", "content": resp["content"]}]
    text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text").strip()
    urls = []
    for b in resp.get("content", []):
        for c in b.get("citations") or []:
            if c.get("url") and c["url"] not in urls:
                urls.append(c["url"])
    return (text or "No results.") + ("\n\nSources:\n" + "\n".join(urls[:10]) if urls else "")


def read_page(url, max_chars=20000):
    """A web page as plain text, without scripts, styles or tags."""
    if not str(url).startswith(("http://", "https://")):
        raise ValueError("read_page takes a web link")
    req = urllib.request.Request(str(url), headers={"User-Agent": "Mozilla/5.0 (personal game tracker coach)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read(3_000_000).decode(r.headers.get_content_charset() or "utf-8", errors="replace")
    raw = re.sub(r"(?is)<(script|style|noscript|svg)\b[^>]*>.*?</\1>", " ", raw)
    text = html.unescape(re.sub(r"(?s)<[^>]+>", " ", raw))
    return re.sub(r"\s+", " ", text).strip()[:max_chars]
