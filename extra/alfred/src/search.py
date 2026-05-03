#!/usr/bin/env python3
"""
Booki Alfred script filter.

Reads the user's query from argv, fetches /api/bookmarks from the configured
Booki host, and prints Alfred-format JSON to stdout.

Workflow environment variables:
  booki_host   default "http://127.0.0.1:8765"
  booki_kinds  comma-separated kind allow-list. Empty = all kinds.
               Examples: "bookmark", "bookmark,video", "video,channel".
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

HOST = os.environ.get("booki_host", "http://127.0.0.1:8765").rstrip("/")
KIND_FILTER = (os.environ.get("booki_kinds") or "").strip()

GLYPHS = {
    "bookmark": "🔖",
    "video": "🎬",
    "channel": "📺",
    "photo": "🖼",
    "document": "📄",
    "github": "🐙",
    "file": "📁",
    "podcast": "🎧",
    "article": "📰",
}


def fetch_bookmarks() -> list[dict]:
    req = urllib.request.Request(f"{HOST}/api/bookmarks")
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.load(r)


def filter_kinds(items: list[dict]) -> list[dict]:
    if not KIND_FILTER:
        return items
    keep = {k.strip() for k in KIND_FILTER.split(",") if k.strip()}
    return [b for b in items if b.get("kind") in keep]


def rank(items: list[dict], query: str) -> list[dict]:
    q = query.strip().lower()
    if not q:
        return sorted(items, key=lambda b: -int(b.get("importance") or 0))
    scored = []
    for b in items:
        title = (b.get("title") or "").lower()
        url = (b.get("url") or "").lower()
        tags = " ".join(b.get("tags") or []).lower()
        # Tier 0: title prefix; 1: title contains; 2: tag contains; 3: url contains.
        if q == title[:len(q)]:
            scored.append((0, 0, b))
        elif q in title:
            scored.append((1, title.find(q), b))
        elif q in tags:
            scored.append((2, tags.find(q), b))
        elif q in url:
            scored.append((3, url.find(q), b))
    scored.sort(key=lambda t: (t[0], t[1], -int(t[2].get("importance") or 0)))
    return [b for _, _, b in scored]


def to_alfred(items: list[dict]) -> dict:
    out = {"items": []}
    for b in items[:50]:
        glyph = GLYPHS.get(b.get("kind"), "·")
        url = b.get("url", "")
        title = b.get("title") or "(untitled)"
        out["items"].append({
            "uid": b.get("id", url),
            "title": f"{glyph}  {title}",
            "subtitle": url,
            "arg": url,
            "autocomplete": title,
            "mods": {
                "cmd": {"subtitle": "⌘ Copy URL", "arg": url},
            },
        })
    return out


def error(msg: str) -> None:
    print(json.dumps({
        "items": [{
            "title": "Booki not reachable",
            "subtitle": msg,
            "valid": False,
        }]
    }))


def main() -> None:
    query = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        items = fetch_bookmarks()
    except (urllib.error.URLError, TimeoutError) as e:
        error(f"{HOST} — {e.reason if hasattr(e, 'reason') else e}")
        return
    except Exception as e:
        error(f"{type(e).__name__}: {e}")
        return

    items = filter_kinds(items)
    items = rank(items, query)
    print(json.dumps(to_alfred(items)))


if __name__ == "__main__":
    main()
