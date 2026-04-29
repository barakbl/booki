"""
plugins/rss — RSS/Atom feeds as a source.

Each entry in each configured feed becomes one Item(kind="article"), one
`.md` file. Identity is the entry URL (so re-fetch preserves user tags /
notes / enrichment, and feeds that republish entries don't produce
duplicates).

Configure in config.toml:

    [sources.rss]
    max_items_per_feed = 100    # optional cap per feed, 0 = unlimited

    [[sources.rss.feeds]]
    url   = "https://example.com/feed.xml"
    title = "Example"           # optional — falls back to feed's own <title>
    tags  = ["news", "tech"]    # optional — stored as feed_tags extra
"""

from __future__ import annotations

from typing import Iterable, Iterator

from ..base import Item, Source, register


DEFAULT_MAX_ITEMS = 100
DEFAULT_DESCRIPTION_CHARS = 800


def _deps_available() -> tuple[bool, str]:
    try:
        import feedparser  # noqa: F401
        return True, ""
    except ImportError:
        return False, "missing dependency: feedparser. Install with: pip install feedparser"


def _entry_url(entry) -> str:
    link = (getattr(entry, "link", "") or entry.get("link", "") or "").strip()
    if link:
        return link
    return (getattr(entry, "id", "") or entry.get("id", "") or "").strip()


def _entry_summary(entry) -> str:
    import re as _re
    text = (
        entry.get("summary")
        or entry.get("description")
        or (entry.get("content", [{}])[0].get("value") if entry.get("content") else "")
        or ""
    )
    # Strip HTML tags and collapse whitespace — feeds routinely embed markup.
    text = _re.sub(r"<[^>]+>", " ", str(text))
    text = _re.sub(r"\s+", " ", text).strip()
    return text


def _entry_date(entry) -> str:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            try:
                return f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}"
            except Exception:
                pass
    return ""


@register
class RSSSource(Source):
    name = "rss"

    @classmethod
    def kind_specs(cls) -> list[dict]:
        return [{"slug": "article", "glyph": "📰", "label": "Article"}]

    @classmethod
    def field_specs(cls) -> list[dict]:
        return [
            {"name": "feed_title",  "label": "Feed",        "group": "RSS", "format": "text"},
            {"name": "feed_url",    "label": "Feed URL",    "group": "RSS", "format": "url"},
            {"name": "author",      "label": "Author",      "group": "RSS", "format": "text"},
            {"name": "published_at","label": "Published",   "group": "RSS", "format": "date"},
            {"name": "feed_tags",   "label": "Feed tags",   "group": "RSS", "format": "tags"},
            {"name": "description", "label": "Summary",     "group": "Description", "format": "text"},
        ]

    def is_available(self) -> bool:
        ok, _ = _deps_available()
        if not ok:
            return False
        return bool(self.cfg.get("feeds"))

    def availability_hint(self) -> str:
        ok, msg = _deps_available()
        if not ok:
            return msg
        if not self.cfg.get("feeds"):
            return "no feeds configured — add [[sources.rss.feeds]] entries to config.toml"
        return ""

    def fetch(self) -> Iterable[Item]:
        import feedparser

        feeds = self.cfg.get("feeds") or []
        max_items = int(self.cfg.get("max_items_per_feed", DEFAULT_MAX_ITEMS) or 0)

        for feed_cfg in feeds:
            url = (feed_cfg.get("url") or "").strip()
            if not url:
                continue
            user_title = (feed_cfg.get("title") or "").strip()
            feed_tags = [str(t) for t in (feed_cfg.get("tags") or [])]

            print(f"  [rss] fetching {user_title or url}")
            parsed = feedparser.parse(url)
            if parsed.bozo and not parsed.entries:
                reason = getattr(parsed, "bozo_exception", "parse error")
                print(f"  [rss] {url}: {reason}")
                continue

            feed_title = user_title or (parsed.feed.get("title") or url).strip()
            count = 0

            for entry in parsed.entries:
                entry_url = _entry_url(entry)
                if not entry_url:
                    continue

                title = (entry.get("title") or "(untitled)").strip()
                author = (entry.get("author") or parsed.feed.get("author") or "").strip()
                published = _entry_date(entry)
                summary = _entry_summary(entry)[:DEFAULT_DESCRIPTION_CHARS]

                yield Item(
                    title=title,
                    url=entry_url,
                    source=self.name,
                    kind="article",
                    path=["RSS", feed_title],
                    date_added=published or None,
                    extras={
                        "feed_title":   feed_title,
                        "feed_url":     url,
                        "author":       author,
                        "published_at": published,
                        "feed_tags":    feed_tags,
                        "description":  summary,
                    },
                )
                count += 1
                if max_items and count >= max_items:
                    break

            print(f"  [rss] {feed_title}: {count} entr{'y' if count == 1 else 'ies'}")
