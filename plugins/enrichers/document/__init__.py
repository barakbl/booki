"""
plugins.enrichers.document — identify document bookmarks (PDFs, ebooks,
office files, plain-text formats) AND contribute the Documents tab to the
web UI.

Detection is URL-pattern-only (extension on the URL path, query stripped).
Per the Stage 1 photo precedent, the enricher takes ownership of the
canonical `kind` field only when it's `bookmark`, `article`, or empty —
preserving any explicit kind set by another source (e.g. `kind=file` from
the directory plugin). It always adds `"document"` to the cross-cutting
`sources` list, so the Documents tab can find every matched item via
``kind == "document"`` OR ``"document" in sources``.

Config (all optional):

    [enrichers.document]
    # disabled = true
    cooldown_days = 30
    types = ["pdf", "docx", "epub"]   # restrict; empty / omitted = all known

Supported type slugs (default = all of them):

    pdf · doc · docx · odt · rtf · pages
    epub · mobi · azw3
    md · markdown · txt · rst · org · tex
    csv · tsv

Each slug maps to one or more extensions. The enricher writes
`document_type` (the matching slug) so the UI can pick a per-type icon.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Optional
from urllib.parse import urlsplit

from ...base import Enricher, TabContribution, register_enricher, register_tab

log = logging.getLogger("booki.enrichers.document")


# slug → tuple of file extensions (lowercase, leading dot). The slug is what
# the user types in [enrichers.document].types; the extension is what we
# match against the URL path.
TYPE_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "pdf":      (".pdf",),
    "doc":      (".doc",),
    "docx":     (".docx",),
    "odt":      (".odt",),
    "rtf":      (".rtf",),
    "pages":    (".pages",),
    "epub":     (".epub",),
    "mobi":     (".mobi",),
    "azw3":     (".azw3", ".azw"),
    "md":       (".md", ".markdown"),
    "markdown": (".md", ".markdown"),   # alias
    "txt":      (".txt",),
    "rst":      (".rst",),
    "org":      (".org",),
    "tex":      (".tex",),
    "csv":      (".csv",),
    "tsv":      (".tsv",),
}

DEFAULT_TYPES: tuple[str, ...] = (
    "pdf", "doc", "docx", "odt", "rtf", "pages",
    "epub", "mobi", "azw3",
    "md", "txt", "rst", "org", "tex",
    "csv", "tsv",
)

# Kinds the enricher is willing to overwrite when claiming an item. `bookmark`
# is the default; `article` is a soft web-content label that's more precise
# when re-classified; empty string covers items missing the field. Anything
# else (e.g. `file` from the directory plugin, `video`, `channel`) is sticky —
# we still tag `sources` but leave `kind` alone.
SOFT_KINDS: frozenset[str] = frozenset({"", "bookmark", "article"})


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


def _detect_type(url: str, ext_to_slug: dict[str, str]) -> Optional[str]:
    """Return the matched type slug or None."""
    if not url:
        return None
    try:
        path = (urlsplit(url).path or "").lower()
    except ValueError:
        return None
    if not path:
        return None
    for ext, slug in ext_to_slug.items():
        if path.endswith(ext):
            return slug
    return None


@register_enricher
class DocumentEnricher(Enricher):
    name = "document"

    force_all: bool = False

    def configure(self, cfg: dict) -> None:
        super().configure(cfg)
        self.cooldown_days = int(cfg.get("cooldown_days", 30) or 30)

        raw_types = cfg.get("types")
        # Empty list / missing = enable all known types. List of slugs
        # restricts to those.
        if not raw_types:
            allowed = DEFAULT_TYPES
        else:
            allowed = tuple(str(t).strip().lower() for t in raw_types if str(t).strip())

        # Build a flat extension → slug map for the chosen types. If a user
        # restricts to ["md"], we still map both .md and .markdown to "md"
        # (since slug "md" registers both extensions).
        ext_to_slug: dict[str, str] = {}
        for slug in allowed:
            mapped = TYPE_EXTENSIONS.get(slug)
            if not mapped:
                log.warning("document_type_unknown",
                            extra={"slug": slug,
                                   "known": sorted(TYPE_EXTENSIONS)})
                continue
            for ext in mapped:
                # First match wins so a user-supplied `markdown` doesn't
                # shadow `md` (or vice versa); both produce slug=md.
                canonical = "md" if slug in ("md", "markdown") else slug
                ext_to_slug.setdefault(ext, canonical)
        self._ext_to_slug = ext_to_slug

    # — gating —

    def is_applicable(self, fm: dict) -> bool:
        url = str(fm.get("url", "") or "").strip()
        if not url or not self._ext_to_slug:
            return False
        if _detect_type(url, self._ext_to_slug) is None:
            return False
        if self.force_all:
            return True
        last = str(fm.get("document_last_enriched", "") or "")
        days = _days_since_iso(last)
        if days is not None and days < self.cooldown_days:
            return False
        return True

    # — work —

    def enrich(self, fm: dict) -> Optional[dict]:
        url = str(fm.get("url", "") or "").strip()
        slug = _detect_type(url, self._ext_to_slug)
        if slug is None:
            return None

        existing_sources = [str(s) for s in (fm.get("sources") or []) if str(s).strip()]
        if "document" not in existing_sources:
            existing_sources.append("document")

        updates: dict = {
            "sources":                existing_sources,
            "document_kind":          "document",
            "document_type":          slug,
            "document_status":        "ok",
            "document_last_enriched": _today_iso(),
        }

        # Take ownership of canonical `kind` only for soft kinds. `kind=file`
        # (directory plugin), `video`, `channel`, etc. are left alone.
        current_kind = str(fm.get("kind", "") or "").strip().lower()
        if current_kind in SOFT_KINDS:
            updates["kind"] = "document"

        return updates

    @classmethod
    def kind_specs(cls) -> list[dict]:
        return [{"slug": "document", "glyph": "📄", "label": "Document"}]

    @classmethod
    def field_specs(cls) -> list[dict]:
        g = "Document"
        return [
            {"name": "document_kind",          "label": "Kind",        "group": g, "format": "text"},
            {"name": "document_type",          "label": "Type",        "group": g, "format": "text"},
            {"name": "document_status",        "label": "Status",      "group": g, "format": "text"},
            {"name": "document_last_enriched", "label": "Enriched on", "group": g, "format": "date"},
        ]


# Tab contribution — exercised by the Stage 2 plugin tab pipeline.
register_tab(TabContribution(
    id="documents",
    label="Documents",
    icon="📄",
    order=25,                # between Photos (20) and Videos (30)
    module="tab.js",
    styles=["tab.css"],
))
