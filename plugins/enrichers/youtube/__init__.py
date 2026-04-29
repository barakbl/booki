"""
plugins.enrichers.youtube — tag and enrich YouTube video / channel links.

Detects URLs of the form:
  • https://www.youtube.com/watch?v=<id>     (canonical video URL)
  • https://youtu.be/<id>                    (short share form)
  • https://www.youtube.com/shorts/<id>      (Shorts)
  • https://www.youtube.com/embed/<id>       (embed)
  • https://www.youtube.com/live/<id>        (live)
  • https://www.youtube.com/channel/<id>     (channel by id)
  • https://www.youtube.com/@<handle>        (channel by handle)
  • https://www.youtube.com/c/<vanity>       (channel vanity URL)
  • https://www.youtube.com/user/<name>      (legacy user URL)
  • plus m.youtube.com / music.youtube.com mirrors

Uses yt-dlp (already a Booki dependency for the download feature) — no API
key required. Adds "youtube" to the item's `sources` list so any item from
any source becomes findable as a YouTube link.

Where it overlaps with the youtube source plugin (channel / channel_id /
video_id / duration / published_at / view_count / description / youtube_tags)
both write to the same canonical fields; whoever ran most recently wins.
The OAuth-only signals (`liked`, `watched`, `subscribed_to_channel`) are
NEVER touched by the enricher — only the source plugin sets those.

Config (all optional):

    [enrichers.youtube]
    timeout       = 30      # seconds, per yt-dlp extract
    cooldown_days = 7       # skip items enriched within the last N days
    # disabled    = true    # uncomment to skip this enricher entirely
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Optional

from ...base import Enricher, register_enricher

log = logging.getLogger("booki.enrichers.youtube")


# Video URLs — all return an 11-character ID.
YT_VIDEO_RE = re.compile(
    r"^https?://(?:www\.|m\.|music\.)?(?:"
        r"youtube\.com/(?:watch\?(?:[^#?]*&)?v=|embed/|shorts/|live/|v/)([A-Za-z0-9_-]{11})"
        r"|youtu\.be/([A-Za-z0-9_-]{11})"
    r")",
    re.IGNORECASE,
)

# Channel URLs — four common shapes.
YT_CHANNEL_RE = re.compile(
    r"^https?://(?:www\.|m\.)?youtube\.com/("
        r"channel/[A-Za-z0-9_-]+"
        r"|@[A-Za-z0-9_.\-]+"
        r"|c/[A-Za-z0-9_.\-]+"
        r"|user/[A-Za-z0-9_.\-]+"
    r")/?(?:[?#]|$)",
    re.IGNORECASE,
)


def _today_iso() -> str:
    return date.today().isoformat()


def _days_since_iso(iso: str) -> Optional[int]:
    if not iso:
        return None
    try:
        d = datetime.strptime(iso[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    return (date.today() - d).days


def _format_duration(seconds: Optional[int]) -> str:
    """`75` → `'1:15'`, `3725` → `'1:02:05'`."""
    if not seconds or seconds <= 0:
        return ""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _format_upload_date(yyyymmdd: Optional[str]) -> str:
    """yt-dlp emits `'YYYYMMDD'` — turn it into `'YYYY-MM-DD'` ISO form."""
    if not yyyymmdd or len(yyyymmdd) != 8 or not yyyymmdd.isdigit():
        return ""
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"


def _is_youtube_url(url: str) -> Optional[str]:
    """Return 'video' / 'channel' / None."""
    if not url:
        return None
    if YT_VIDEO_RE.match(url):
        return "video"
    if YT_CHANNEL_RE.match(url):
        return "channel"
    return None


@register_enricher
class YouTubeEnricher(Enricher):
    name = "youtube"

    # Set by the sync engine when --all is passed; lifts the cooldown.
    force_all: bool = False

    def configure(self, cfg: dict) -> None:
        super().configure(cfg)
        self.timeout = int(cfg.get("timeout", 30) or 30)
        self.cooldown_days = int(cfg.get("cooldown_days", 7) or 7)

    # — gating —

    def is_applicable(self, fm: dict) -> bool:
        url = str(fm.get("url", "") or "").strip()
        if _is_youtube_url(url) is None:
            return False
        if self.force_all:
            return True
        last = str(fm.get("youtube_last_enriched", "") or "")
        days = _days_since_iso(last)
        if days is not None and days < self.cooldown_days:
            return False
        return True

    # — work —

    def enrich(self, fm: dict) -> Optional[dict]:
        url = str(fm.get("url", "") or "").strip()
        kind = _is_youtube_url(url)
        if kind is None:
            return None

        try:
            import yt_dlp
        except ImportError:
            log.warning("yt_dlp_missing",
                        extra={"url": url, "hint": "pip install yt-dlp"})
            return None

        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "socket_timeout": self.timeout,
        }
        # For channels we don't want yt-dlp to enumerate every video — only
        # the channel-level metadata. extract_flat truncates the entries list.
        if kind == "channel":
            opts["extract_flat"] = "in_playlist"
            opts["playlistend"] = 1

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as e:
            err = str(e).splitlines()[0][:200]
            log.warning("yt_dlp_extract_failed",
                        extra={"url": url, "error": err})
            return {
                "youtube_status":        "failed",
                "youtube_last_enriched": _today_iso(),
            }

        if not info:
            return None

        # Tag the item as a youtube link via the multi-source list.
        existing_sources = [str(s) for s in (fm.get("sources") or []) if str(s).strip()]
        if "youtube" not in existing_sources:
            existing_sources.append("youtube")

        if kind == "video":
            return self._video_updates(info, existing_sources)
        return self._channel_updates(url, info, existing_sources)

    # — builders —

    @staticmethod
    def _video_updates(info: dict, sources: list[str]) -> dict:
        return {
            "sources": sources,

            # Canonical fields — shared with the youtube source plugin.
            "channel":      str(info.get("channel") or info.get("uploader") or ""),
            "channel_id":   str(info.get("channel_id") or ""),
            "video_id":     str(info.get("id") or ""),
            "duration":     _format_duration(info.get("duration")),
            "published_at": _format_upload_date(info.get("upload_date")),
            "view_count":   int(info.get("view_count") or 0),
            "description":  str(info.get("description") or "")[:1000],
            "youtube_tags": [str(t) for t in (info.get("tags") or [])][:30],

            # Enricher-specific.
            "youtube_kind":          "video",
            "youtube_thumbnail":     str(info.get("thumbnail") or ""),
            "youtube_uploader_url":  str(info.get("uploader_url") or info.get("channel_url") or ""),
            "youtube_like_count":    int(info["like_count"]) if info.get("like_count") is not None else 0,
            "youtube_comment_count": int(info["comment_count"]) if info.get("comment_count") is not None else 0,
            "youtube_age_limit":     int(info.get("age_limit") or 0),
            "youtube_language":      str(info.get("language") or ""),
            "youtube_categories":    [str(c) for c in (info.get("categories") or [])],
            "youtube_is_live":       bool(info.get("is_live")),
            "youtube_was_live":      bool(info.get("was_live")),
            "youtube_availability":  str(info.get("availability") or ""),
            "youtube_status":        "ok",
            "youtube_last_enriched": _today_iso(),
        }

    @staticmethod
    def _channel_updates(url: str, info: dict, sources: list[str]) -> dict:
        return {
            "sources": sources,

            "channel":          str(info.get("channel") or info.get("uploader") or info.get("title") or ""),
            "channel_id":       str(info.get("channel_id") or info.get("id") or ""),
            "subscriber_count": int(info.get("channel_follower_count") or 0),
            "video_count":      int(info.get("playlist_count") or 0),
            "description":      str(info.get("description") or "")[:1000],

            "youtube_kind":          "channel",
            "youtube_thumbnail":     str(info.get("thumbnail") or ""),
            "youtube_uploader_url":  str(info.get("uploader_url") or info.get("channel_url") or url),
            "youtube_status":        "ok",
            "youtube_last_enriched": _today_iso(),
        }

    @classmethod
    def field_specs(cls) -> list[dict]:
        # Only the fields the youtube *source* doesn't already declare. The
        # web schema endpoint merges these onto the source's existing block
        # under the "youtube" key.
        g = "YouTube"
        return [
            {"name": "youtube_kind",          "label": "Kind",         "group": g, "format": "text"},
            {"name": "youtube_thumbnail",     "label": "Thumbnail",    "group": g, "format": "image"},
            {"name": "youtube_uploader_url",  "label": "Channel URL",  "group": g, "format": "url"},
            {"name": "youtube_like_count",    "label": "Likes",        "group": g, "format": "number", "kinds": ("video",)},
            {"name": "youtube_comment_count", "label": "Comments",     "group": g, "format": "number", "kinds": ("video",)},
            {"name": "youtube_age_limit",     "label": "Age limit",    "group": g, "format": "number", "kinds": ("video",)},
            {"name": "youtube_language",      "label": "Language",     "group": g, "format": "text",   "kinds": ("video",)},
            {"name": "youtube_categories",    "label": "Categories",   "group": g, "format": "list",   "kinds": ("video",)},
            {"name": "youtube_is_live",       "label": "Live",         "group": g, "format": "bool",   "kinds": ("video",)},
            {"name": "youtube_was_live",      "label": "Was live",     "group": g, "format": "bool",   "kinds": ("video",)},
            {"name": "youtube_availability",  "label": "Availability", "group": g, "format": "text",   "kinds": ("video",)},
            {"name": "youtube_status",        "label": "Status",       "group": g, "format": "text"},
            {"name": "youtube_last_enriched", "label": "Enriched on",  "group": g, "format": "date"},
        ]
