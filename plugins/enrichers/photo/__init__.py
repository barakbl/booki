"""
plugins.enrichers.photo — identify photo bookmarks.

Marks an item as `kind="photo"` when its URL matches one of:
  • A known image extension (.jpg, .png, … plus camera-raw .raf, .cr3, …)
  • A known photo-hosting URL pattern (instagram /p/, flickr /photos/, …)

The enricher only takes ownership of the canonical `kind` field when it's
the default `"bookmark"` (or empty). Anything explicitly set by another
plugin or by the user (`video`, `channel`, …) is left alone — we still
record `photo_kind: "photo"` and add `"photo"` to the multi-source list,
so the item is findable as a photo without losing its primary identity.

Stage 1 is **URL-pattern-only** — no HEAD requests, no og:image scraping.
A follow-up enricher pass can fill in `photo_thumbnail`/width/height.

Config (all optional):

    [enrichers.photo]
    # disabled = true
    cooldown_days = 30                # photos rarely change; long cooldown
    extensions    = [".jpg", ".jpeg", ".png", ".gif", ".webp",
                     ".heic", ".avif", ".bmp", ".tiff",
                     # camera raw
                     ".raf", ".cr2", ".cr3", ".nef", ".nrw",
                     ".arw", ".sr2", ".dng", ".orf", ".rw2",
                     ".pef", ".srw", ".raw", ".x3f", ".iiq"]
    hosts         = ["instagram.com/p/", "instagram.com/reel/",
                     "flickr.com/photos/", "imgur.com/",
                     "unsplash.com/photos/", "pexels.com/photo/",
                     "500px.com/photo/", "pixiv.net/en/artworks/"]
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Optional
from urllib.parse import urlsplit

from ...base import Enricher, register_enricher

log = logging.getLogger("booki.enrichers.photo")


DEFAULT_EXTENSIONS = (
    # web-friendly
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".avif",
    ".bmp", ".tiff", ".tif",
    # camera raw — manufacturers, in rough popularity order
    ".raf",                       # Fujifilm
    ".cr2", ".cr3",               # Canon
    ".nef", ".nrw",               # Nikon
    ".arw", ".sr2", ".srf",       # Sony
    ".dng",                       # Adobe / generic
    ".orf",                       # Olympus
    ".rw2",                       # Panasonic
    ".pef",                       # Pentax
    ".srw",                       # Samsung
    ".raw",                       # generic
    ".x3f",                       # Sigma
    ".iiq",                       # Phase One
    ".3fr",                       # Hasselblad
    ".erf",                       # Epson
    ".kdc",                       # Kodak
    ".mef",                       # Mamiya
    ".mrw",                       # Minolta
    ".rwl",                       # Leica
)

DEFAULT_HOSTS = (
    "instagram.com/p/",
    "instagram.com/reel/",
    "flickr.com/photos/",
    "imgur.com/",
    "unsplash.com/photos/",
    "pexels.com/photo/",
    "500px.com/photo/",
    "pixiv.net/en/artworks/",
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


def _normalize_host_pattern(p: str) -> str:
    p = p.strip().lower()
    if p.startswith("http://"):
        p = p[len("http://"):]
    elif p.startswith("https://"):
        p = p[len("https://"):]
    if p.startswith("www."):
        p = p[len("www."):]
    return p


def _url_match(url: str, exts: tuple[str, ...], hosts: tuple[str, ...]) -> bool:
    if not url:
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.netloc or "").lower()
    if host.startswith("www."):
        host = host[len("www."):]
    path = (parts.path or "").lower()
    haystack = f"{host}{path}"

    if path:
        # Strip query/fragment is already handled by urlsplit; check path tail.
        for ext in exts:
            if path.endswith(ext):
                return True

    for hp in hosts:
        if hp and hp in haystack:
            return True
    return False


@register_enricher
class PhotoEnricher(Enricher):
    name = "photo"

    # Set by the sync engine when --all is passed; lifts the cooldown.
    force_all: bool = False

    def configure(self, cfg: dict) -> None:
        super().configure(cfg)
        self.cooldown_days = int(cfg.get("cooldown_days", 30) or 30)
        exts = cfg.get("extensions") or DEFAULT_EXTENSIONS
        self.extensions = tuple(
            (e if e.startswith(".") else "." + e).lower() for e in exts
        )
        hosts = cfg.get("hosts") or DEFAULT_HOSTS
        self.hosts = tuple(_normalize_host_pattern(h) for h in hosts if h)

    # — gating —

    def is_applicable(self, fm: dict) -> bool:
        url = str(fm.get("url", "") or "").strip()
        if not url:
            return False
        if not _url_match(url, self.extensions, self.hosts):
            return False
        if self.force_all:
            return True
        last = str(fm.get("photo_last_enriched", "") or "")
        days = _days_since_iso(last)
        if days is not None and days < self.cooldown_days:
            return False
        return True

    # — work —

    def enrich(self, fm: dict) -> Optional[dict]:
        url = str(fm.get("url", "") or "").strip()
        if not _url_match(url, self.extensions, self.hosts):
            return None

        existing_sources = [str(s) for s in (fm.get("sources") or []) if str(s).strip()]
        if "photo" not in existing_sources:
            existing_sources.append("photo")

        updates: dict = {
            "sources":              existing_sources,
            "photo_kind":           "photo",
            "photo_status":         "ok",
            "photo_last_enriched":  _today_iso(),
        }

        # Take ownership of canonical `kind` only if it's the default. This
        # mirrors the "youtube enricher coexists with explicit kind=video"
        # pattern: explicit kinds set by source plugins or the user win.
        current_kind = str(fm.get("kind", "") or "").strip().lower()
        if current_kind in ("", "bookmark"):
            updates["kind"] = "photo"

        return updates

    @classmethod
    def kind_specs(cls) -> list[dict]:
        return [{"slug": "photo", "glyph": "🖼", "label": "Photo"}]

    @classmethod
    def field_specs(cls) -> list[dict]:
        g = "Photo"
        return [
            {"name": "photo_kind",          "label": "Kind",        "group": g, "format": "text"},
            {"name": "photo_thumbnail",     "label": "Thumbnail",   "group": g, "format": "image"},
            {"name": "photo_width",         "label": "Width",       "group": g, "format": "number"},
            {"name": "photo_height",        "label": "Height",      "group": g, "format": "number"},
            {"name": "photo_status",        "label": "Status",      "group": g, "format": "text"},
            {"name": "photo_last_enriched", "label": "Enriched on", "group": g, "format": "date"},
        ]
