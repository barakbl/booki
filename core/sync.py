#!/usr/bin/env python3
"""
sync.py — pluggable sources → Markdown files → (optional) dead-link check
          → (optional) LLM enrichment.

Each source is a plugin registered in `plugins/`. Built-ins ship for:
    chrome, safari, firefox, youtube.

Usage:
    booki sync                              Sync every available source
    booki sync --source chrome              Sync one source
    booki sync --source chrome youtube      Sync several
    booki sync --list-sources               Show registered sources + status
    booki sync --check-dead-links           Sync, then check unchecked URLs
    booki sync --check-dead-links --all     Re-check ALL URLs
    booki sync --enrich                     Fetch + summarize with Ollama
    booki sync --enrich --all               Re-enrich everything
    booki sync --no-sync --enrich           Only enrich, skip sync
    booki sync --dry-run                    Preview without writing
    booki sync --output ./my-items          Custom output directory
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger("booki.sync")

import requests

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore

import plugins
from plugins.base import Source
from .store import ItemStore, today_str


# ─── Constants ────────────────────────────────────────────────────────────────

_PROJECT_ROOT      = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = _PROJECT_ROOT / "bookmarks"
DEFAULT_CONFIG     = _PROJECT_ROOT / "config.toml"
WAYBACK_API        = "https://archive.org/wayback/available"
REQUEST_TIMEOUT    = 10
DEAD_LINK_DELAY    = 0.3
ENRICH_DELAY       = 0.2

BROWSER_SOURCES = {"chrome", "safari", "firefox"}


# ─── Exclude rules ────────────────────────────────────────────────────────────

class ExcludeFilter:
    """
    Global URL-based exclusion — evaluated before writing any item, whether
    it came from a plugin sync or a manual `--link` add.

    Configure in config.toml:

        [exclude]
        domains   = ["facebook.com", "twitter.com"]   # suffix-matched
        url_regex = ["/login", "utm_"]                # re.search against full URL
    """

    def __init__(self, domains: Iterable[str] = (), url_regex: Iterable[str] = ()):
        self.domains = [d.lower().lstrip(".") for d in domains if d]
        self.patterns: list[re.Pattern] = []
        for pat in url_regex:
            if not pat:
                continue
            try:
                self.patterns.append(re.compile(pat))
            except re.error as e:
                print(f"  [exclude] ignoring invalid regex {pat!r}: {e}")

    @classmethod
    def from_cfg(cls, cfg: dict) -> "ExcludeFilter":
        sect = cfg.get("exclude", {}) or {}
        return cls(
            domains=sect.get("domains", []) or [],
            url_regex=sect.get("url_regex", []) or [],
        )

    def match(self, url: str) -> str:
        """Return a short reason ('domain:x' / 'regex:y') if excluded, else ''."""
        if not url:
            return ""
        if self.domains:
            try:
                from urllib.parse import urlparse
                host = (urlparse(url).hostname or "").lower()
            except Exception:
                host = ""
            for d in self.domains:
                if host == d or host.endswith("." + d):
                    return f"domain:{d}"
        for p in self.patterns:
            if p.search(url):
                return f"regex:{p.pattern}"
        return ""

MANUAL_SOURCE = "manual"
MANUAL_PATH = ["manual"]


# ─── Manual link sync ─────────────────────────────────────────────────────────

def _fetch_page_title(url: str, timeout: int = 5) -> str:
    """Best-effort <title> scrape. Returns "" on any failure.

    Encoding handling: requests defaults to ISO-8859-1 when the server
    doesn't declare a charset, which mangles Hebrew/Arabic/CJK pages
    served as Windows-1255/1256/GBK/etc. We prefer (in order):
      1. <meta charset="…"> from the HTML body
      2. r.apparent_encoding (chardet on the bytes)
      3. the header's charset
    """
    from .url_safety import is_fetchable_url

    # SSRF guard: `/api/link` accepts any scheme that looks like a URL,
    # so without this we'd happily issue requests at file://, gopher://,
    # or http://internal-service from user-supplied input.
    if not is_fetchable_url(url):
        return ""
    import html as _html
    try:
        r = requests.get(url, timeout=timeout, allow_redirects=True, headers={
            "User-Agent": "Mozilla/5.0 (booki link sync)"
        })
        r.raise_for_status()

        meta = re.search(rb'<meta[^>]+charset=["\']?([\w-]+)', r.content, re.IGNORECASE)
        picked = None
        if meta:
            try:
                picked = meta.group(1).decode("ascii")
                text = r.content.decode(picked, errors="replace")
            except (LookupError, UnicodeDecodeError):
                picked = None

        if picked is None:
            header_enc = (r.encoding or "").lower()
            if not header_enc or header_enc in ("iso-8859-1", "latin-1"):
                r.encoding = r.apparent_encoding or "utf-8"
            text = r.text

        m = re.search(r"<title[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
        if m:
            title = re.sub(r"\s+", " ", _html.unescape(m.group(1))).strip()
            return title[:200]
    except Exception:
        pass
    return ""


class LinkExcluded(ValueError):
    """Raised by sync_link when the URL matches an [exclude] rule."""
    def __init__(self, reason: str):
        super().__init__(f"excluded by rule: {reason}")
        self.reason = reason


def sync_link(url: str, store: ItemStore, title: Optional[str] = None,
              *, dry_run: bool = False,
              exclude: Optional[ExcludeFilter] = None) -> tuple[Path, bool, str]:
    """
    Write a single manually-supplied link as a bookmark MD file.

    Returns (path, is_new, resolved_title). No enrichment, no dead-link check.
    Raises LinkExcluded if `exclude` matches the URL.
    """
    from plugins.base import Item
    from .url_safety import is_safe_url

    url = url.strip()
    if not url:
        raise ValueError("empty URL")

    # Detect scheme — supports both `scheme://...` and `scheme:...` forms.
    # Refuse non-allowlisted schemes BEFORE prepending https://, so a
    # `javascript:alert(1)` paste doesn't get mangled into "https://..."
    # and silently swallowed; we want the user to see the rejection.
    scheme_m = re.match(r"^([a-zA-Z][a-zA-Z0-9+\-.]*):", url)
    if scheme_m and not is_safe_url(url):
        raise ValueError(f"unsupported URL scheme: {scheme_m.group(1)}:")
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+\-.]*://", url):
        url = "https://" + url

    if exclude is not None:
        reason = exclude.match(url)
        if reason:
            raise LinkExcluded(reason)

    resolved_title = (title or "").strip() or _fetch_page_title(url) or url

    item = Item(
        title=resolved_title,
        url=url,
        source=MANUAL_SOURCE,
        kind="bookmark",
        path=list(MANUAL_PATH),
        date_added=today_str(),
    )
    result = store.write(item, today_str(), dry_run=dry_run)
    return result.path, result.is_new, resolved_title


# ─── Stats ────────────────────────────────────────────────────────────────────

@dataclass
class SyncStats:
    sources_processed:  int = 0
    items_new:          int = 0
    items_preserved:    int = 0
    items_removed:      int = 0
    items_excluded:     int = 0
    links_checked:      int = 0
    links_dead:         int = 0
    links_archived:     int = 0
    enriched:           int = 0
    enrich_skipped:     int = 0
    enrich_failed:      int = 0
    meta_enriched:      int = 0
    meta_skipped:       int = 0
    meta_failed:        int = 0
    errors:             list[str] = field(default_factory=list)


# ─── Dead Link Checker ────────────────────────────────────────────────────────

class DeadLinkChecker:
    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    }

    def __init__(self, timeout: int = REQUEST_TIMEOUT):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(self.HEADERS)
        self.session.max_redirects = 10

    def check(self, url: str) -> str:
        try:
            r = self.session.head(url, timeout=self.timeout, allow_redirects=True)
            if r.status_code < 400:
                return "alive"
            r = self.session.get(url, timeout=self.timeout, allow_redirects=True, stream=True)
            r.close()
            return "alive" if r.status_code < 400 else "dead"
        except Exception:
            return "dead"

    def wayback_url(self, url: str) -> Optional[str]:
        try:
            r = self.session.get(WAYBACK_API, params={"url": url}, timeout=self.timeout)
            closest = r.json().get("archived_snapshots", {}).get("closest", {})
            if closest.get("available"):
                return closest["url"]
        except Exception:
            pass
        return None


# ─── Enrichment ───────────────────────────────────────────────────────────────
#
# The enricher gives every item a short natural-language summary + keyword
# list. For bookmarks, we try to extract real page content via trafilatura.
# For items that already ship descriptive text in their frontmatter (e.g.
# YouTube videos carry `description`), we use that text directly — it's
# usually richer than whatever a scraper would get from youtube.com anyway.

DEFAULT_ENRICH_PROMPT = """You analyze a bookmarked item so it can be searched later by natural language.

Item title: {title}
URL: {url}
Kind: {kind}
User tags: {tags}
User notes: {notes}

{content_block}

Respond with a single JSON object, no prose, no markdown:
{{
  "summary": "ONE sentence (max 25 words) — what this is and what it's useful for. Be concrete, not generic.",
  "keywords": ["5-10 distinctive searchable terms — prefer topics/tools/concepts over generic words"],
  "page_title": "the real page title if different from the item title, else empty string"
}}"""

_CONTENT_BLOCK_WITH = 'Content (may be truncated):\n"""\n{content}\n"""'
_CONTENT_BLOCK_EMPTY = (
    "Content: (not available — the page may be auth-gated, JS-rendered, "
    "or unreachable). Infer from the title, URL, and user annotations. "
    "Stay conservative — do NOT invent specific features you can't verify."
)


IMAGE_EXTS = {
    "jpg", "jpeg", "png", "heic", "heif", "tif", "tiff", "gif", "webp",
    "bmp", "avif", "raw", "cr2", "cr3", "nef", "arw", "dng", "rw2", "orf",
}

DEFAULT_EXIFTOOL_FIELDS = [
    "Composite:Aperture", "Composite:ShutterSpeed", "Composite:GPSPosition",
    "Composite:ImageSize", "Composite:LensID",
    "File:MIMEType", "File:FileSize",
    "EXIF:Make", "EXIF:Model", "EXIF:LensModel", "EXIF:FocalLength",
    "EXIF:FNumber", "EXIF:ExposureTime", "EXIF:ISO",
    "EXIF:DateTimeOriginal", "EXIF:Flash", "EXIF:WhiteBalance",
    "EXIF:Orientation", "EXIF:GPSLatitude", "EXIF:GPSLongitude", "EXIF:GPSAltitude",
    "IPTC:Keywords", "IPTC:Caption-Abstract", "IPTC:By-line",
    "XMP:Subject", "XMP:Description", "XMP:Creator", "XMP:Rating",
    "XMP:Label", "XMP:HierarchicalSubject",
]


def _exiftool_resolve_binary(binary: str) -> Optional[str]:
    """Return an absolute path to the exiftool binary, or None if not found."""
    if not binary:
        return None
    p = Path(binary).expanduser()
    if p.is_file():
        return str(p)
    return shutil.which(binary)


def run_exiftool(path: Path, *, binary_path: str,
                 fields: Optional[list[str]] = None,
                 timeout: int = 30) -> Optional[dict]:
    """
    Run exiftool on `path` and return a flat dict of {short_tag: value}
    filtered to `fields` (group:tag form). None on any failure (timeout,
    missing binary, parse error, no metadata).
    """
    wanted = list(fields or DEFAULT_EXIFTOOL_FIELDS)
    try:
        proc = subprocess.run(
            # -c "%+.6f"  → signed decimal degrees for GPS (default is DMS).
            [binary_path, "-j", "-G", "-q", "-q", "-c", "%+.6f", str(path)],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    if not data:
        return None
    rec = data[0] if isinstance(data, list) else data
    out: dict = {}
    for grouped in wanted:
        v = rec.get(grouped)
        if v in (None, "", []):
            continue
        short = grouped.split(":", 1)[-1]
        out[short] = v
    return out or None


def _format_exif_for_llm(meta: dict) -> str:
    """Render the metadata dict as a compact key: value text block."""
    lines = []
    for k, v in meta.items():
        if isinstance(v, list):
            v = ", ".join(str(x) for x in v)
        lines.append(f"{k}: {v}")
    return "Image metadata (from EXIF/IPTC/XMP):\n" + "\n".join(lines)


# ─── EXIF → flat semantic fields ──────────────────────────────────────────────

# Map raw exiftool tag names → semantic frontmatter field names. Stored on the
# bookmark as top-level fields (not nested) so the web UI can render them via
# the directory source's field_specs().
def _parse_number(v) -> Optional[float]:
    """Pull a leading float out of values like '24.0 mm', '1.8', 1.8, '12 m Above Sea Level'."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d+(?:\.\d+)?", str(v))
    return float(m.group(0)) if m else None


def _parse_exif_datetime(v) -> Optional[str]:
    """EXIF 'YYYY:MM:DD HH:MM:SS' → ISO 'YYYY-MM-DDTHH:MM:SS'. Falls back to as-is."""
    if not v:
        return None
    s = str(v).strip()
    m = re.match(r"^(\d{4}):(\d{2}):(\d{2})[ T](\d{2}:\d{2}:\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}T{m.group(4)}"
    return s or None


def _flatten_exif(meta: dict) -> dict:
    """
    Translate raw exiftool output (already keyed by short tag name) into a flat
    dict of semantic frontmatter fields. Values are parsed to native types where
    sensible (numbers as float/int, dates as ISO strings).
    """
    out: dict = {}

    def put(name, value):
        if value not in (None, "", []):
            out[name] = value

    make  = (meta.get("Make")  or "").strip() if isinstance(meta.get("Make"),  str) else meta.get("Make")
    model = (meta.get("Model") or "").strip() if isinstance(meta.get("Model"), str) else meta.get("Model")
    if make:  put("camera_make", make)
    if model: put("camera_model", model)
    if model and make:
        # Avoid "Apple Apple iPhone" duplication when Model already contains Make.
        combined = model if make.lower() in str(model).lower() else f"{make} {model}"
        put("camera", combined)
    elif model:
        put("camera", model)

    lens = meta.get("LensModel") or meta.get("LensID")
    if lens: put("lens", str(lens).strip())

    fl = _parse_number(meta.get("FocalLength"))
    if fl is not None: put("focal_length_mm", fl)

    fn = _parse_number(meta.get("FNumber") or meta.get("Aperture"))
    if fn is not None: put("f_number", fn)

    if meta.get("ExposureTime") not in (None, "", []):
        put("shutter_speed", str(meta["ExposureTime"]))
    elif meta.get("ShutterSpeed") not in (None, "", []):
        put("shutter_speed", str(meta["ShutterSpeed"]))

    iso = meta.get("ISO")
    if iso not in (None, "", []):
        try: put("iso", int(iso))
        except (TypeError, ValueError): put("iso", iso)

    if meta.get("Flash"):          put("flash", str(meta["Flash"]))
    if meta.get("WhiteBalance"):   put("white_balance", str(meta["WhiteBalance"]))
    if meta.get("Orientation"):    put("orientation", str(meta["Orientation"]))

    taken = _parse_exif_datetime(meta.get("DateTimeOriginal"))
    if taken: put("taken_at", taken)

    lat = _parse_number(meta.get("GPSLatitude"))
    lon = _parse_number(meta.get("GPSLongitude"))
    alt = _parse_number(meta.get("GPSAltitude"))
    if lat is not None: put("gps_lat", lat)
    if lon is not None: put("gps_lon", lon)
    if alt is not None: put("gps_alt", alt)

    size = meta.get("ImageSize")
    if isinstance(size, str):
        m = re.match(r"^(\d+)\s*[xX]\s*(\d+)$", size.strip())
        if m:
            put("image_width", int(m.group(1)))
            put("image_height", int(m.group(2)))
    if "image_width" not in out and meta.get("ImageWidth"):
        put("image_width", int(_parse_number(meta["ImageWidth"]) or 0) or None)
    if "image_height" not in out and meta.get("ImageHeight"):
        put("image_height", int(_parse_number(meta["ImageHeight"]) or 0) or None)

    if meta.get("MIMEType"): put("mime_type", str(meta["MIMEType"]))

    caption = meta.get("Caption-Abstract") or meta.get("Description")
    if caption: put("image_caption", str(caption).strip())

    creator = meta.get("Creator") or meta.get("By-line")
    if creator:
        put("image_creator",
            ", ".join(str(c) for c in creator) if isinstance(creator, list)
            else str(creator).strip())

    if meta.get("Rating") not in (None, ""):
        try: put("image_rating", int(meta["Rating"]))
        except (TypeError, ValueError): pass

    # Keywords from IPTC/XMP — keep as a list under a distinct name so we don't
    # clobber the LLM-generated `keywords` field.
    raw_kw = meta.get("Keywords") or meta.get("Subject") or meta.get("HierarchicalSubject")
    if raw_kw:
        if isinstance(raw_kw, str):
            kw_list = [k.strip() for k in raw_kw.split(",") if k.strip()]
        else:
            kw_list = [str(k).strip() for k in raw_kw if str(k).strip()]
        if kw_list:
            put("image_keywords", kw_list)

    return out


@dataclass
class EnrichmentResult:
    summary:    str
    keywords:   list[str]
    page_title: str


class Enricher:
    def __init__(self, llm_cfg: dict, max_content_chars: int = 4000,
                 fetch_timeout: int = 15, llm_timeout: int = 120,
                 exiftool_cfg: Optional[dict] = None):
        self.llm_cfg           = llm_cfg
        self.max_content_chars = max_content_chars
        self.fetch_timeout     = fetch_timeout
        self.llm_timeout       = llm_timeout
        self.exiftool_cfg      = exiftool_cfg or {}
        # Resolved once per Enricher; None means disabled or binary missing.
        self._exiftool_bin: Optional[str] = None
        self._exiftool_warned = False
        if self.exiftool_cfg.get("enabled"):
            self._exiftool_bin = _exiftool_resolve_binary(
                str(self.exiftool_cfg.get("binary") or "exiftool")
            )
            if self._exiftool_bin is None and not self._exiftool_warned:
                print("  [exiftool] enabled in config but binary not found — "
                      "skipping image metadata. (install: brew install exiftool)")
                self._exiftool_warned = True

    def gather_content(self, fm: dict) -> tuple[str, str, dict]:
        """
        Return (content, source_tag, extra_fields).

        `extra_fields` is a dict to merge into the item's frontmatter alongside
        the LLM result (e.g. {"image_metadata": {...}} from exiftool).

        Preference order:
          1. `description` field from frontmatter (source-provided text —
             YouTube descriptions, RSS summaries, etc.).
          2. exiftool metadata for local image files (kind="file", file:// URL).
          3. trafilatura page extraction on the URL.
          4. Empty (→ title-only enrichment).
        """
        desc = str(fm.get("description", "")).strip()
        if desc:
            return desc[: self.max_content_chars], "description", {}

        url = str(fm.get("url", ""))
        kind = str(fm.get("kind", "bookmark"))

        # Local image file → exiftool.
        if self._exiftool_bin and kind == "file" and url.startswith("file://"):
            ext = str(fm.get("file_ext") or "").lower().lstrip(".")
            if ext in IMAGE_EXTS:
                meta = self._extract_exif(url)
                if meta:
                    flat = _flatten_exif(meta)
                    text = _format_exif_for_llm(meta)
                    return (text[: self.max_content_chars], "exiftool", flat)

        # Skip page-fetching for URLs where a scrape is near-useless.
        if kind in ("video", "channel"):
            return "", "title-only", {}

        try:
            import trafilatura
        except ImportError:
            sys.exit("Install trafilatura:  pip install trafilatura")

        t0 = time.monotonic()
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            log.debug("enrich_fetch_empty", extra={"url": url,
                      "duration_s": round(time.monotonic() - t0, 3)})
            return "", "title-only", {}
        text = trafilatura.extract(
            downloaded,
            include_comments=False,
            include_tables=False,
            favor_precision=True,
        )
        if not text:
            log.debug("enrich_extract_empty", extra={"url": url,
                      "duration_s": round(time.monotonic() - t0, 3)})
            return "", "title-only", {}
        log.debug("enrich_fetched", extra={"url": url,
                  "content_chars": len(text),
                  "duration_s": round(time.monotonic() - t0, 3)})
        return text[: self.max_content_chars], "page", {}

    def _extract_exif(self, file_url: str) -> Optional[dict]:
        from urllib.parse import unquote, urlparse
        parsed = urlparse(file_url)
        local_path = Path(unquote(parsed.path))
        if not local_path.is_file():
            return None
        return run_exiftool(
            local_path,
            binary_path=self._exiftool_bin,
            fields=self.exiftool_cfg.get("fields") or None,
            timeout=int(self.exiftool_cfg.get("timeout", 30)),
        )

    def summarize(self, *, title: str, url: str, kind: str, tags: list,
                  notes: str, content: str) -> EnrichmentResult:
        content_block = (
            _CONTENT_BLOCK_WITH.format(content=content)
            if content.strip()
            else _CONTENT_BLOCK_EMPTY
        )
        prompt = DEFAULT_ENRICH_PROMPT.format(
            title=title or "(untitled)",
            url=url,
            kind=kind or "bookmark",
            tags=", ".join(str(t) for t in tags) if tags else "(none)",
            notes=notes or "(none)",
            content_block=content_block,
        )
        raw = self._call_ollama(prompt)
        return self._parse_response(raw)

    def _call_ollama(self, prompt: str) -> str:
        base_url = self.llm_cfg.get("base_url", "http://localhost:11434")
        model    = self.llm_cfg.get("model", "llama3.2:3b")
        r = requests.post(
            f"{base_url.rstrip('/')}/api/generate",
            json={
                "model":   model,
                "prompt":  prompt,
                "stream":  False,
                "format":  "json",
                "options": {"temperature": 0.2},
            },
            timeout=self.llm_timeout,
        )
        r.raise_for_status()
        return r.json().get("response", "") or ""

    @staticmethod
    def _parse_response(raw: str) -> EnrichmentResult:
        raw = raw.strip()
        if not raw:
            raise ValueError("empty response from LLM")
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", raw).strip()
        data = json.loads(raw)
        return EnrichmentResult(
            summary=str(data.get("summary", "")).strip(),
            keywords=[
                str(k).strip().lower() for k in data.get("keywords", [])
                if str(k).strip()
            ],
            page_title=str(data.get("page_title", "")).strip(),
        )


# ─── Sync Engine ──────────────────────────────────────────────────────────────

class SyncEngine:
    def __init__(self, store: ItemStore, exclude: Optional[ExcludeFilter] = None):
        self.store = store
        self.exclude = exclude or ExcludeFilter()

    # ── per-source sync ─────────────────────────────────────────────────────

    def sync_sources(self, src_list: list[Source], dry_run: bool = False) -> SyncStats:
        stats = SyncStats()
        today = today_str()

        for source in src_list:
            print(f"\n[{source.name}] Checking availability...", end=" ", flush=True)
            if not source.is_available():
                hint = source.availability_hint() or "not available"
                print(f"skipped — {hint}")
                stats.errors.append(f"{source.name}: {hint}")
                continue
            print("ok")

            print(f"[{source.name}] Fetching...")
            before_files = set(self.store.source_files(source.name))
            written: set[Path] = set()
            count = 0

            try:
                from .url_safety import is_safe_url
                for item in source.fetch():
                    if not is_safe_url(item.url):
                        # Hostile / misbehaving source — refusing to persist
                        # javascript:/data:/vbscript: URLs which would become
                        # clickable XSS sinks in exports + the web UI.
                        log.warning("source_yielded_unsafe_url", extra={
                            "source": source.name,
                            "scheme": item.url.split(":", 1)[0] if ":" in item.url else "",
                        })
                        stats.items_excluded += 1
                        continue
                    reason = self.exclude.match(item.url)
                    if reason:
                        stats.items_excluded += 1
                        continue
                    result = self.store.write(item, today, dry_run=dry_run)
                    written.add(result.path.resolve())
                    if result.is_new:
                        stats.items_new += 1
                    else:
                        stats.items_preserved += 1
                    count += 1
            except Exception as e:
                msg = f"{source.name}: {type(e).__name__}: {e}"
                print(f"  Error during fetch: {msg}")
                stats.errors.append(msg)
                continue

            # Orphan detection — items that existed before this run but
            # weren't emitted by the source this time. Scope is per-source.
            # A multi-sourced item just loses this source's slug; only when
            # it was the last remaining source does the file get flagged
            # `removed_from_*`.
            orphans = before_files - written
            removed_count = 0
            detached_count = 0
            for path in orphans:
                if dry_run:
                    removed_count += 1
                    continue
                flag = ("removed_from_browser"
                        if source.name in BROWSER_SOURCES
                        else "removed_from_source")
                result = self.store.detach_source(path, source.name, today, removed_flag=flag)
                if result == "removed":
                    removed_count += 1
                elif result == "detached":
                    detached_count += 1
            stats.items_removed += removed_count

            tail = []
            if removed_count:
                tail.append(f"{removed_count} marked removed")
            if detached_count:
                tail.append(f"{detached_count} detached")

            suffix = f" ({', '.join(tail)})" if tail else ""
            print(f"[{source.name}] Done — {count} item(s){suffix}")
            stats.sources_processed += 1

        return stats

    # ── dead link checking ──────────────────────────────────────────────────

    def check_dead_links(self, check_all: bool = False,
                         dry_run: bool = False) -> SyncStats:
        stats = SyncStats()
        checker = DeadLinkChecker()
        today = today_str()

        to_check: list[tuple[Path, dict]] = []
        for path in self.store.all_files():
            fm = self.store.read_frontmatter(path)
            if not fm.get("url") or fm.get("removed_from_browser") or fm.get("removed_from_source"):
                continue
            # YouTube (and other account-sourced kinds) don't benefit from
            # dead-link probing — treat them as always-alive.
            if fm.get("kind") in ("video", "channel"):
                continue
            if check_all or fm.get("status", "unchecked") == "unchecked":
                to_check.append((path, fm))

        if not to_check:
            scope = "all links" if check_all else "unchecked links"
            print(f"\n  No {scope} to check.")
            return stats

        scope = "all" if check_all else "unchecked"
        print(f"\nChecking {len(to_check)} {scope} link(s)...\n")

        for i, (path, fm) in enumerate(to_check, 1):
            url = str(fm.get("url", ""))
            display = url[:72] + "…" if len(url) > 72 else url
            print(f"  [{i:>4}/{len(to_check)}] {display}", end="  ", flush=True)

            result = checker.check(url)
            stats.links_checked += 1

            if result == "alive":
                print("✓ alive")
                if not dry_run:
                    self.store.update_fields(path, status="alive", last_sync=today)
            else:
                print("✗ DEAD")
                stats.links_dead += 1
                self._handle_dead_link(checker, path, url, fm, today, dry_run, stats)

            if i < len(to_check):
                time.sleep(DEAD_LINK_DELAY)

        return stats

    def _handle_dead_link(self, checker: DeadLinkChecker, path: Path, url: str,
                          fm: dict, today: str, dry_run: bool,
                          stats: SyncStats) -> None:
        title = str(fm.get("title") or url)
        print(f"\n  {'─' * 60}")
        print(f"  Dead link detected")
        print(f"  Title : {title}")
        print(f"  URL   : {url}")

        archive = checker.wayback_url(url)
        if archive:
            print(f"  Archive: {archive}")
            print("\n  What should we do?")
            print("    [1] Use archive URL (status=archived)")
            print("    [2] Mark as dead only (no archive)")
            print("    [3] Skip for now (leave as unchecked)")

            choice = ""
            while choice not in ("1", "2", "3"):
                try:
                    choice = input("  Choice [1/2/3]: ").strip()
                except (EOFError, KeyboardInterrupt):
                    choice = "3"

            if choice == "1":
                if not dry_run:
                    self.store.update_fields(path, status="archived",
                                             archive_url=archive, last_sync=today)
                print("  → Saved archive URL")
                stats.links_archived += 1
            elif choice == "2":
                if not dry_run:
                    self.store.update_fields(path, status="dead",
                                             archive_url="", last_sync=today)
                print("  → Marked as dead")
            else:
                print("  → Skipped (will be rechecked next time)")
        else:
            print("  No Wayback Machine snapshot available.")
            print("  Marking as dead.")
            if not dry_run:
                self.store.update_fields(path, status="dead",
                                         archive_url="", last_sync=today)

        print(f"  {'─' * 60}\n")

    # ── enrichment ──────────────────────────────────────────────────────────

    def enrich(self, cfg: dict, enrich_all: bool = False,
               dry_run: bool = False) -> SyncStats:
        stats = SyncStats()
        today = today_str()

        enrich_cfg = cfg.get("enrichment", {})
        if not enrich_cfg.get("enabled", True):
            print("\n  Enrichment disabled in config.toml ([enrichment].enabled = false)")
            return stats

        llm_cfg = cfg.get("llm", {}).copy()
        if m := enrich_cfg.get("llm_model"):
            llm_cfg["model"] = m
        if b := enrich_cfg.get("base_url"):
            llm_cfg["base_url"] = b

        enricher = Enricher(
            llm_cfg=llm_cfg,
            max_content_chars=int(enrich_cfg.get("max_content_chars", 4000)),
            fetch_timeout=int(enrich_cfg.get("fetch_timeout", 15)),
            llm_timeout=int(enrich_cfg.get("llm_timeout", 120)),
            exiftool_cfg=enrich_cfg.get("exiftool"),
        )

        to_enrich: list[tuple[Path, dict]] = []
        for path in self.store.all_files():
            fm = self.store.read_frontmatter(path)
            if not fm.get("url"):
                continue
            if fm.get("removed_from_browser") or fm.get("removed_from_source"):
                continue
            if fm.get("status") == "dead":
                continue
            if not enrich_all and fm.get("last_enriched"):
                continue
            to_enrich.append((path, fm))

        if not to_enrich:
            scope = "all" if enrich_all else "new"
            print(f"\n  No {scope} items to enrich.")
            return stats

        model = llm_cfg.get("model", "llama3.2:3b")
        scope = "all" if enrich_all else "new"
        print(f"\nEnriching {len(to_enrich)} {scope} item(s) with Ollama ({model})...\n")

        for i, (path, fm) in enumerate(to_enrich, 1):
            url = str(fm.get("url", ""))
            display = url[:72] + "…" if len(url) > 72 else url
            print(f"  [{i:>4}/{len(to_enrich)}] {display}", end="  ", flush=True)

            try:
                content, source_tag, extra_fields = enricher.gather_content(fm)
                result = enricher.summarize(
                    title=str(fm.get("title", "")),
                    url=url,
                    kind=str(fm.get("kind", "bookmark")),
                    tags=fm.get("tags", []) or [],
                    notes=str(fm.get("notes", "")),
                    content=content,
                )

                summary_preview = result.summary[:60] + ("…" if len(result.summary) > 60 else "")
                tag = " (dry-run)" if dry_run else ""
                print(f"✓ [{source_tag}] {summary_preview}{tag}")

                if not dry_run:
                    self.store.update_fields(
                        path,
                        summary=result.summary,
                        keywords=result.keywords,
                        page_title=result.page_title,
                        last_enriched=today,
                        enrich_source=source_tag,
                        **extra_fields,
                    )
                stats.enriched += 1

            except Exception as e:
                print(f"✗ {type(e).__name__}: {e}")
                stats.enrich_failed += 1

            if i < len(to_enrich):
                time.sleep(ENRICH_DELAY)

        return stats

    # ─── Enricher-plugin pass ─────────────────────────────────────────────────

    def enrich_meta(self, cfg: dict, *, enrich_all: bool = False,
                    only: Optional[list[str]] = None,
                    dry_run: bool = False) -> SyncStats:
        """
        Run every registered Enricher plugin over every existing item.

        For each (item, applicable_enricher) pair the enricher's `enrich(fm)`
        is called; the returned dict (if any) is merged into the item's
        frontmatter via `ItemStore.update_fields`.
        """
        stats = SyncStats()
        enricher_cfgs = cfg.get("enrichers", {}) or {}
        enricher_classes = list(plugins.iter_enrichers())
        if only:
            enricher_classes = [(n, c) for n, c in enricher_classes if n in set(only)]
        else:
            # Drop ones marked `disabled = true` in [enrichers.<name>].
            enricher_classes = [
                (n, c) for n, c in enricher_classes
                if not (enricher_cfgs.get(n, {}) or {}).get("disabled", False)
            ]
        if not enricher_classes:
            print("No enrichers registered (or all disabled in config.toml).")
            return stats

        # Instantiate once per enricher and let `--all` lift their cooldown.
        instances = []
        for name, cls in enricher_classes:
            inst = cls()
            inst.configure(enricher_cfgs.get(name, {}) or {})
            inst.force_all = bool(enrich_all)
            instances.append((name, inst))

        names_str = ", ".join(n for n, _ in instances)
        mode = " (dry-run)" if dry_run else ""
        all_tag = " --all" if enrich_all else ""
        print(f"\nMeta-enrichment: {names_str}{all_tag}{mode}")

        files = self.store.all_files()
        if not files:
            print("  (no items to enrich)")
            return stats

        log.info("enrich_meta_started", extra={
            "enrichers": [n for n, _ in instances],
            "items_total": len(files),
            "all": bool(enrich_all),
            "dry_run": bool(dry_run),
        })

        examined = 0
        matched  = 0
        for path in files:
            fm = self.store.read_frontmatter(path)
            if not fm:
                continue
            examined += 1
            for name, inst in instances:
                try:
                    applicable = inst.is_applicable(fm)
                except Exception as e:
                    log.debug("enricher_is_applicable_raised",
                              extra={"enricher": name, "url": str(fm.get("url", "")),
                                     "error": str(e)})
                    applicable = False
                if not applicable:
                    # Silent — non-applicable items aren't user-visible work.
                    continue
                matched += 1

                title = str(fm.get("title", "(no title)"))[:60]
                tag = f"[{name}] {title}"
                try:
                    updates = inst.enrich(fm)
                except Exception as e:
                    print(f"✗ {tag} → {type(e).__name__}: {e}")
                    log.warning("enricher_raised", extra={"enricher": name,
                                                          "url": str(fm.get("url", "")),
                                                          "error": str(e)})
                    stats.meta_failed += 1
                    continue

                if not updates:
                    stats.meta_skipped += 1
                    print(f"· {tag} → no data")
                    continue

                # Reflect the merge in the in-memory `fm` so a follow-up
                # enricher in the same loop sees the latest state.
                fm.update(updates)

                if dry_run:
                    print(f"✓ {tag} (dry-run)")
                else:
                    self.store.update_fields(path, **updates,
                                             last_sync=today_str())
                    extra = _format_meta_summary(updates)
                    print(f"✓ {tag}{extra}")
                stats.meta_enriched += 1

        print(f"\n  Examined {examined} item(s), {matched} match(es)"
              f" → {stats.meta_enriched} enriched, "
              f"{stats.meta_skipped} skipped, {stats.meta_failed} failed")

        log.info("enrich_meta_finished", extra={
            "enriched": stats.meta_enriched,
            "skipped":  stats.meta_skipped,
            "failed":   stats.meta_failed,
        })
        return stats


# ─── CLI ──────────────────────────────────────────────────────────────────────

def _print_summary(sync_s: Optional[SyncStats],
                   link_s: Optional[SyncStats],
                   enrich_s: Optional[SyncStats],
                   meta_s: Optional[SyncStats] = None,
                   *, dry_run: bool) -> None:
    tag = " (dry-run)" if dry_run else ""
    print(f"\n{'═' * 50}")
    print(f"Summary{tag}")
    print("═" * 50)
    if sync_s:
        print(f"  Sources synced    : {sync_s.sources_processed}")
        print(f"  New items         : {sync_s.items_new}")
        print(f"  Preserved         : {sync_s.items_preserved}")
        print(f"  Marked removed    : {sync_s.items_removed}")
        if sync_s.items_excluded:
            print(f"  Excluded          : {sync_s.items_excluded}")
        if sync_s.errors:
            print(f"  Errors            : {len(sync_s.errors)}")
            for e in sync_s.errors:
                print(f"    • {e}")
    if link_s:
        print(f"  Links checked     : {link_s.links_checked}")
        print(f"  Dead              : {link_s.links_dead}")
        print(f"  Archived          : {link_s.links_archived}")
    if enrich_s:
        print(f"  Enriched          : {enrich_s.enriched}")
        print(f"  Enrich skipped    : {enrich_s.enrich_skipped}")
        print(f"  Enrich failed     : {enrich_s.enrich_failed}")
    if meta_s:
        print(f"  Meta-enriched     : {meta_s.meta_enriched}")
        print(f"  Meta skipped      : {meta_s.meta_skipped}")
        print(f"  Meta failed       : {meta_s.meta_failed}")
    print("═" * 50)


def _format_meta_summary(updates: dict) -> str:
    """One-liner shown after each successful enrichment — picks fields that
    are short and informative for the most common enrichers."""
    bits: list[str] = []
    # GitHub
    if "github_stars" in updates:
        bits.append(f"★{updates['github_stars']:,}")
    if "github_forks" in updates:
        bits.append(f"⑂{updates['github_forks']:,}")
    if updates.get("github_languages"):
        bits.append(", ".join(updates["github_languages"][:3]))
    # YouTube — video
    if updates.get("youtube_kind") == "video":
        if "view_count" in updates and updates["view_count"]:
            bits.append(f"👁 {int(updates['view_count']):,}")
        if "youtube_like_count" in updates and updates["youtube_like_count"]:
            bits.append(f"♥ {int(updates['youtube_like_count']):,}")
        if updates.get("duration"):
            bits.append(f"⏱ {updates['duration']}")
        if updates.get("channel"):
            bits.append(updates["channel"])
    # YouTube — channel
    elif updates.get("youtube_kind") == "channel":
        if "subscriber_count" in updates and updates["subscriber_count"]:
            bits.append(f"🔔 {int(updates['subscriber_count']):,}")
        if "video_count" in updates and updates["video_count"]:
            bits.append(f"🎬 {int(updates['video_count']):,}")
    return ("  " + " · ".join(bits)) if bits else ""


def _list_enrichers(cfg: dict) -> None:
    enricher_cfgs = cfg.get("enrichers", {}) or {}
    print("Registered enrichers:\n")
    names = plugins.all_enricher_names()
    if not names:
        print("  (none — drop a plugin under plugins/enrichers/<name>/__init__.py)")
        print()
        return
    for name in names:
        cls = plugins.get_enricher(name)
        if cls is None:
            continue
        sub = enricher_cfgs.get(name, {}) or {}
        notes = []
        if sub.get("disabled"):
            notes.append("disabled in config.toml")
        if name == "github" and not (sub.get("token") or os.environ.get("GITHUB_TOKEN")):
            notes.append("no token (60 req/h public limit)")
        suffix = f"  — {' · '.join(notes)}" if notes else ""
        print(f"  ✓ {name}{suffix}")
    print()


def _load_config(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def _is_disabled(name: str, cfg: dict) -> bool:
    """A source is disabled when its `[sources.<name>]` block sets `disabled = true`."""
    sub = (cfg.get("sources", {}) or {}).get(name, {}) or {}
    return bool(sub.get("disabled", False))


def _enabled_source_names(cfg: dict) -> list[str]:
    """Registered source names with `disabled = true` filtered out."""
    return [n for n in plugins.all_source_names() if not _is_disabled(n, cfg)]


def _build_sources(names: list[str], cfg: dict) -> list[Source]:
    """Instantiate sources by name and inject their config subtable."""
    source_cfgs = cfg.get("sources", {}) or {}
    instances: list[Source] = []
    for name in names:
        cls = plugins.get_source(name)
        if cls is None:
            sys.exit(f"Unknown source '{name}'. Registered: {', '.join(plugins.all_source_names())}")
        inst = cls()
        inst.configure(source_cfgs.get(name, {}) or {})
        instances.append(inst)
    return instances


def _list_sources(cfg: dict) -> None:
    source_cfgs = cfg.get("sources", {}) or {}
    print("Registered sources:\n")
    for name in plugins.all_source_names():
        cls = plugins.get_source(name)
        if cls is None:
            continue
        if _is_disabled(name, cfg):
            print(f"  ✗ {name}  — disabled in config.toml")
            continue
        inst = cls()
        inst.configure(source_cfgs.get(name, {}) or {})
        ok = inst.is_available()
        mark = "✓" if ok else "·"
        hint = "" if ok else f"  — {inst.availability_hint() or 'unavailable'}"
        print(f"  {mark} {name}{hint}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="booki sync",
        description="Sync sources → one Markdown file per item.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--link", "-L", metavar="URL",
                        help="Add a single link immediately (no full sync, no enrichment). "
                             "Saved under bookmarks/manual/.")
    parser.add_argument("--link-title", metavar="TITLE",
                        help="Title for --link (default: fetched from the page, "
                             "falling back to the URL).")
    parser.add_argument("--source", "-s", nargs="+", metavar="SOURCE",
                        help="Sources to sync (e.g. chrome safari youtube). "
                             "Default: every registered source.")
    parser.add_argument("--list-sources", action="store_true",
                        help="List all registered sources and their availability.")
    parser.add_argument("--check-dead-links", "-c", action="store_true",
                        help="After syncing, check unchecked links for availability.")
    parser.add_argument("--enrich", "-e", action="store_true",
                        help="Fetch content + summarize with Ollama.")
    parser.add_argument("--enrich-meta", action="store_true",
                        help="Run all registered Enricher plugins (e.g. github) "
                             "to add API-driven metadata to existing items.")
    parser.add_argument("--enricher", nargs="+", metavar="NAME",
                        help="Limit --enrich-meta to specific enrichers (e.g. github). "
                             "Default: every registered enricher.")
    parser.add_argument("--list-enrichers", action="store_true",
                        help="List all registered enricher plugins.")
    parser.add_argument("--all", action="store_true",
                        help="With --check-dead-links: recheck everything. "
                             "With --enrich / --enrich-meta: re-enrich everything.")
    parser.add_argument("--no-sync", action="store_true", help="Skip the sync step.")
    parser.add_argument("--dry-run", "-n", action="store_true",
                        help="Show what would change without writing.")
    parser.add_argument("--output", "-o", type=Path, default=DEFAULT_OUTPUT_DIR,
                        metavar="DIR", help=f"Output directory (default: {DEFAULT_OUTPUT_DIR})")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help=f"Config file (default: {DEFAULT_CONFIG})")

    args = parser.parse_args()
    cfg = _load_config(args.config)

    if args.list_sources:
        _list_sources(cfg)
        return

    if args.list_enrichers:
        _list_enrichers(cfg)
        return

    if args.link:
        store = ItemStore(output_dir=args.output)
        exclude = ExcludeFilter.from_cfg(cfg)
        try:
            path, is_new, title = sync_link(
                args.link, store, title=args.link_title, dry_run=args.dry_run,
                exclude=exclude,
            )
        except LinkExcluded as e:
            sys.exit(f"Link excluded — {e.reason}")
        except Exception as e:
            sys.exit(f"Link sync failed: {type(e).__name__}: {e}")
        tag = " (dry-run)" if args.dry_run else ""
        status = "added" if is_new else "updated"
        print(f"{status} {title!r}{tag}\n  → {path}")
        return

    # Choose sources: explicit --source list (overrides `disabled`), else every
    # registered source minus those marked `disabled = true` in config.toml.
    src_names = args.source or _enabled_source_names(cfg)
    src_list = _build_sources(src_names, cfg)

    store = ItemStore(output_dir=args.output)
    exclude = ExcludeFilter.from_cfg(cfg)
    if exclude.domains or exclude.patterns:
        print(f"Exclude: {len(exclude.domains)} domain(s), {len(exclude.patterns)} regex(es)")
    engine = SyncEngine(store, exclude=exclude)

    if args.dry_run:
        print("dry-run mode — no files will be written\n")
    print(f"Output: {args.output}")

    log.info("sync_started", extra={
        "sources": src_names,
        "do_sync": not args.no_sync,
        "do_check_dead_links": bool(args.check_dead_links),
        "do_enrich": bool(args.enrich),
        "all": bool(args.all),
        "dry_run": bool(args.dry_run),
        "output_dir": str(args.output),
    })
    t0 = time.monotonic()

    sync_stats:   Optional[SyncStats] = None
    link_stats:   Optional[SyncStats] = None
    enrich_stats: Optional[SyncStats] = None
    meta_stats:   Optional[SyncStats] = None

    if not args.no_sync:
        sync_stats = engine.sync_sources(src_list, dry_run=args.dry_run)

    if args.check_dead_links:
        link_stats = engine.check_dead_links(check_all=args.all, dry_run=args.dry_run)

    if args.enrich:
        enrich_stats = engine.enrich(cfg=cfg, enrich_all=args.all, dry_run=args.dry_run)

    if args.enrich_meta:
        meta_stats = engine.enrich_meta(cfg=cfg, enrich_all=args.all,
                                        only=args.enricher, dry_run=args.dry_run)

    if not any([args.check_dead_links, args.enrich, args.enrich_meta, args.no_sync]):
        print(
            "\nTip: run with --check-dead-links to verify URLs, "
            "--enrich to add LLM summaries, "
            "or --enrich-meta to run plugin enrichers (e.g. github)."
        )

    _print_summary(sync_stats, link_stats, enrich_stats, meta_stats, dry_run=args.dry_run)

    summary: dict = {"duration_s": round(time.monotonic() - t0, 3),
                     "dry_run": bool(args.dry_run)}
    if sync_stats is not None:
        summary.update({
            "sources_processed": sync_stats.sources_processed,
            "items_new":         sync_stats.items_new,
            "items_preserved":   sync_stats.items_preserved,
            "items_removed":     sync_stats.items_removed,
            "items_excluded":    sync_stats.items_excluded,
            "errors":            len(sync_stats.errors),
        })
    if link_stats is not None:
        summary.update({
            "links_checked":  link_stats.links_checked,
            "links_dead":     link_stats.links_dead,
            "links_archived": link_stats.links_archived,
        })
    if enrich_stats is not None:
        summary.update({
            "enriched":       enrich_stats.enriched,
            "enrich_skipped": enrich_stats.enrich_skipped,
            "enrich_failed":  enrich_stats.enrich_failed,
        })
    if meta_stats is not None:
        summary.update({
            "meta_enriched": meta_stats.meta_enriched,
            "meta_skipped":  meta_stats.meta_skipped,
            "meta_failed":   meta_stats.meta_failed,
        })
    log.info("sync_finished", extra=summary)


if __name__ == "__main__":
    main()
