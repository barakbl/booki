"""
store.py — persist Items to Markdown files with YAML frontmatter.

One Item = one `.md` file. The frontmatter is the source of truth; the body
is a human-readable rendering.

Layout:

    <output_dir>/<slug(item.path[0])>/<slug(item.path[1])>/.../<title>--<urlhash>.md

Stable identity is the URL hash — re-fetch preserves user-editable fields
(importance, tags, notes) and enrichment fields, regardless of title changes.

Backward-compatible with the pre-plugin BookmarkStore: existing
`bookmarks/chrome/...` files round-trip unchanged.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from plugins.base import Item

log = logging.getLogger("booki.store")


# ─── Constants ────────────────────────────────────────────────────────────────

MAX_SLUG_LEN = 60

# Canonical emit order for core frontmatter keys. Unknown (extras / user-added)
# keys are emitted alphabetically after these, preserving stable diffs.
FM_ORDER = [
    "title", "url",
    "source", "sources", "kind",
    "browser_path", "folder_path",
    "importance", "tags", "lists", "notes",
    "date_bookmarked", "last_sync",
    "status", "archive_url",
    "removed_from_browser", "removed_from_source",
    "last_enriched", "enrich_source", "page_title", "summary", "keywords",
    "booki_user_override",
]

# Frontmatter key holding the user's manual-override block. Namespaced with
# the `booki_` prefix so it can't collide with any source-supplied or
# enricher-supplied field name and is obvious in raw YAML.
USER_OVERRIDE_KEY = "booki_user_override"


# ─── Body markers ─────────────────────────────────────────────────────────────
#
# Booki's auto-rendered body content lives between two HTML-comment markers,
# so users can write their own prose before or after the block without it
# being clobbered on re-sync. Markers are HTML comments so they render
# invisibly in any markdown viewer.
#
# Rules:
#   • New file → body is `START\n…content…\nEND`, nothing outside.
#   • Existing file with both markers → only the content between is replaced;
#     anything before START or after END is preserved verbatim.
#   • No START marker (user removed it, or a legacy file pre-dating this
#     feature) → Booki keeps its hands off the body entirely. Frontmatter
#     edits still flow.
#   • START present but END missing → log.warn every sync; body left as-is.
#
# Detection is token-based (`booki:start` / `booki:end`) so users can rewrite
# the trailing comment text without breaking marker recognition.

BOOKI_START_MARKER = (
    "<!-- booki:start — auto-managed by booki; "
    "edits inside will be overwritten -->"
)
BOOKI_END_MARKER = "<!-- booki:end -->"

_BOOKI_START_RE = re.compile(r"<!--\s*booki:start\b[^\n]*?-->", re.IGNORECASE)
_BOOKI_END_RE   = re.compile(r"<!--\s*booki:end\b[^\n]*?-->",   re.IGNORECASE)


def _neutralize_html_comments(s: str) -> str:
    """Stop source-supplied content from forging marker tags.

    A hostile source could otherwise plant `<!-- booki:end -->` inside a
    title / summary / notes string; the next sync's marker regex would
    pick that up as the end of the managed block and let post-marker
    content leak across re-syncs. We replace the literal comment opener
    with `&lt;!--` — viewers render `&lt;` as `<` so the user still sees
    the attempted injection in plain text, but Booki's regex never
    matches a real HTML comment in the body.
    """
    return s.replace("<!--", "&lt;!--")

# Fields the user may edit by hand — never overwritten by a re-fetch.
USER_EDITABLE = {"importance", "tags", "lists", "notes"}

# Fields written by the enricher — preserved across re-fetch.
ENRICH_FIELDS = {"last_enriched", "enrich_source", "page_title", "summary", "keywords"}


# ─── Utilities ────────────────────────────────────────────────────────────────

def _slug(name: str, max_len: int = MAX_SLUG_LEN) -> str:
    """Filesystem-safe lowercase slug. ASCII-only — falls back to 'x' if empty."""
    s = name.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s, flags=re.ASCII)
    s = re.sub(r"[\s_-]+", "_", s).strip("_")
    return (s or "x")[:max_len]


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.rstrip("/").lower().encode()).hexdigest()[:8]


def _escape_md(text: str) -> str:
    return text.replace("\n", " ").replace("\r", "").strip()


def _yaml_str(val) -> str:
    """Render a Python value as an inline YAML scalar / flow-style sequence."""
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, int):
        return str(val)
    if isinstance(val, float):
        return repr(val)
    if isinstance(val, list):
        if not val:
            return "[]"
        return "[" + ", ".join(json.dumps(str(v)) for v in val) + "]"
    if isinstance(val, dict):
        if not val:
            return "{}"
        return json.dumps(val, ensure_ascii=False)
    s = "" if val is None else str(val)
    if not s:
        return '""'
    needs_quote = any(c in s for c in ':#{}[]&*!|>\'",%@`\\') or s != s.strip()
    return json.dumps(s) if needs_quote else s


def _parse_yaml_block(block: str) -> dict:
    """Minimal YAML parser: scalars (str/int/bool), flow-style lists."""
    result: dict = {}
    for line in block.splitlines():
        line = line.rstrip()
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, _, raw = line.partition(":")
        key, raw = key.strip(), raw.strip()

        if raw.startswith("[") and raw.endswith("]"):
            try:
                val = json.loads(raw)
                result[key] = val if isinstance(val, list) else [val]
            except json.JSONDecodeError:
                inner = raw[1:-1].strip()
                result[key] = (
                    [i.strip().strip("\"'") for i in inner.split(",") if i.strip()]
                    if inner else []
                )
            continue

        if raw.startswith("{") and raw.endswith("}"):
            try:
                val = json.loads(raw)
                result[key] = val if isinstance(val, dict) else {}
            except json.JSONDecodeError:
                result[key] = {}
            continue

        if raw.lower() == "true":
            result[key] = True;  continue
        if raw.lower() == "false":
            result[key] = False; continue

        if raw.lstrip("-").isdigit():
            result[key] = int(raw); continue

        if raw.startswith('"') and raw.endswith('"'):
            try:
                result[key] = json.loads(raw); continue
            except json.JSONDecodeError:
                result[key] = raw[1:-1]; continue

        if raw.startswith("'") and raw.endswith("'"):
            result[key] = raw[1:-1]; continue

        result[key] = raw
    return result


def today_str() -> str:
    return datetime.now(tz=timezone.utc).date().isoformat()


# ─── User-override view ───────────────────────────────────────────────────────

def view_fm(fm: dict) -> dict:
    """
    Return a flat view of `fm` with the nested override block overlaid.

    On disk, frontmatter has two layers:
      - top-level keys written by sources & enrichers (authoritative source data)
      - `booki_user_override: { … }` — a user's hand-edits that should shadow
        the authoritative values everywhere we display, search, or export.

    Readers go through this so a user's edited title, summary, tags, etc.
    win over whatever the source / enricher last wrote, while the original
    values are preserved underneath as a fallback. The override key itself
    is dropped from the view — it's an internal detail.
    """
    overrides = fm.get(USER_OVERRIDE_KEY)
    if not isinstance(overrides, dict) or not overrides:
        return {k: v for k, v in fm.items() if k != USER_OVERRIDE_KEY}
    out = {k: v for k, v in fm.items() if k != USER_OVERRIDE_KEY}
    for k, v in overrides.items():
        out[k] = v
    return out


# ─── Store ────────────────────────────────────────────────────────────────────

@dataclass
class WriteResult:
    path: Path
    is_new: bool


class ItemStore:
    """
    One `.md` file per Item, organized by `item.path`.

    Write semantics:
      - User-editable fields (importance/tags/notes) survive a re-fetch.
      - Enrichment fields survive a re-fetch.
      - Source-provided fields (title, url, extras) overwrite — the source
        is the authority for those.
      - When an item later appears under a new path (folder moved, or a
        video found in a new sub-feed), the old file is removed and content
        rewritten at the new path, preserving user/enrich fields.
    """

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir

    # ── paths ────────────────────────────────────────────────────────────────

    def _folder_dir(self, path: list[str]) -> Path:
        return self.output_dir.joinpath(*[_slug(p) for p in path])

    def file_for(self, item: Item) -> Path:
        return self._folder_dir(item.path) / f"{_slug(item.title)}--{_url_hash(item.url)}.md"

    def find_existing(self, path: list[str], url: str) -> Optional[Path]:
        """Look up by URL hash so title edits don't break identity."""
        folder = self._folder_dir(path)
        if not folder.exists():
            return None
        matches = list(folder.glob(f"*--{_url_hash(url)}.md"))
        return matches[0] if matches else None

    def find_anywhere(self, url: str) -> Optional[Path]:
        """
        Find this URL's file regardless of folder.

        Useful when an Item's path changed since the last sync (e.g. a YouTube
        video that moved from `subscriptions/<chan>/` into `liked/`) — we
        want to migrate the existing file instead of orphaning it.
        """
        if not self.output_dir.exists():
            return None
        suffix = f"*--{_url_hash(url)}.md"
        matches = list(self.output_dir.rglob(suffix))
        return matches[0] if matches else None

    def source_files(self, source_slug: str) -> list[Path]:
        """All `.md`s produced by this source.

        Primary check: `sources:` list in frontmatter contains the slug.
        Legacy fallback: file lives under a top-level dir matching the slug
        (covers files written before `sources:` was introduced, and Chrome's
        multi-profile subdirs like `chrome_profile_1`).
        """
        if not self.output_dir.exists():
            return []
        prefix = _slug(source_slug)
        result: set[Path] = set()

        for path in self.output_dir.rglob("*.md"):
            fm = self.read_frontmatter(path)
            srcs = fm.get("sources") or []
            if source_slug in srcs or prefix in srcs:
                result.add(path)
                continue
            # Legacy fallback — no `sources:` yet, infer from folder layout.
            try:
                rel = path.relative_to(self.output_dir)
            except ValueError:
                continue
            top = rel.parts[0] if rel.parts else ""
            if top == prefix or top.startswith(prefix + "_"):
                result.add(path)
        return sorted(result)

    def all_files(self) -> list[Path]:
        if not self.output_dir.exists():
            return []
        return sorted(self.output_dir.rglob("*.md"))

    # ── read ────────────────────────────────────────────────────────────────

    def read_frontmatter(self, file_path: Path) -> dict:
        if not file_path.exists():
            return {}
        content = file_path.read_text(encoding="utf-8")
        m = re.match(r"^---\n(.*?)\n---\n", content, re.DOTALL)
        return _parse_yaml_block(m.group(1)) if m else {}

    # ── write ───────────────────────────────────────────────────────────────

    def write(self, item: Item, today: str, *, dry_run: bool = False) -> WriteResult:
        """
        Write (or update) the `.md` file for this Item.

        Identity is the URL hash. One file per URL — regardless of how many
        sources surface it.

        Location rules:
          - New URL → file is placed at `file_for(item)` (Item's own path).
          - Same-source rewrite → file migrates if Item's path changed
            (folder rename, feed reshuffle, …).
          - Different-source rewrite → file stays put. The first source to
            create the URL "owns" the location and the source-specific
            extras (browser_path, folder_path, channel, duration, …). The
            new source's slug is merged into `sources:` so it still counts
            as that source's item for search/orphan tracking.
        """
        old_path = self.find_existing(item.path, item.url) or self.find_anywhere(item.url)
        existing = self.read_frontmatter(old_path) if old_path else {}
        is_new = old_path is None
        prior_content = (
            old_path.read_text(encoding="utf-8") if old_path and old_path.exists() else ""
        )

        existing_primary_slug = _slug(str(existing.get("source", ""))) if existing else ""
        incoming_slug = item.source
        pin_to_existing = bool(
            old_path
            and existing_primary_slug
            and existing_primary_slug != incoming_slug
        )

        target = old_path if pin_to_existing else self.file_for(item)

        fm = self._build_fm(item, today, existing, pin=pin_to_existing)
        content = self._compose_file(fm, prior_content=prior_content, file_path=target)

        if dry_run:
            return WriteResult(path=target, is_new=is_new)

        target.parent.mkdir(parents=True, exist_ok=True)
        if old_path and old_path.resolve() != target.resolve():
            old_path.unlink(missing_ok=True)
            self._prune_empty(old_path.parent)

        target.write_text(content, encoding="utf-8")
        return WriteResult(path=target, is_new=is_new)

    def mark_removed(self, file_path: Path, today: str, *, flag: str = "removed_from_source") -> bool:
        """Flip a `removed_*` flag on a file whose source no longer lists it."""
        fm = self.read_frontmatter(file_path)
        if not fm:
            return False
        if fm.get(flag) or fm.get("removed_from_browser") or fm.get("removed_from_source"):
            return False
        fm[flag] = True
        fm["last_sync"] = today
        prior = file_path.read_text(encoding="utf-8")
        file_path.write_text(
            self._compose_file(fm, prior_content=prior, file_path=file_path),
            encoding="utf-8",
        )
        return True

    def detach_source(self, file_path: Path, source_slug: str, today: str, *,
                      removed_flag: str = "removed_from_source") -> str:
        """
        Called when `source_slug` no longer reports this URL.

        Returns:
          - "removed"  → was the last source; `removed_flag` was flipped.
          - "detached" → other sources still list it; slug removed from `sources`.
          - "noop"     → nothing to change.
        """
        fm = self.read_frontmatter(file_path)
        if not fm:
            return "noop"

        sources = [str(s) for s in (fm.get("sources") or [])]
        primary = _slug(str(fm.get("source", "")))

        # If `sources` is missing (legacy file), bootstrap from primary.
        if not sources and primary:
            sources = [primary]

        slug = source_slug
        had = slug in sources
        if had:
            sources = [s for s in sources if s != slug]

        # Multi-source: other sources still own it — just detach.
        if sources:
            if not had:
                return "noop"
            fm["sources"] = sources
            # Demote primary if this was the primary source.
            if primary == slug:
                fm["source"] = sources[0]
            fm["last_sync"] = today
            prior = file_path.read_text(encoding="utf-8")
            file_path.write_text(
                self._compose_file(fm, prior_content=prior, file_path=file_path),
                encoding="utf-8",
            )
            return "detached"

        # Sole (or no) source left — fall through to the removed-flag flip.
        if fm.get(removed_flag) or fm.get("removed_from_browser") or fm.get("removed_from_source"):
            return "noop"
        fm[removed_flag] = True
        fm["sources"] = []
        fm["last_sync"] = today
        prior = file_path.read_text(encoding="utf-8")
        file_path.write_text(
            self._compose_file(fm, prior_content=prior, file_path=file_path),
            encoding="utf-8",
        )
        return "removed"

    def update_fields(self, file_path: Path, **updates) -> bool:
        if not file_path.exists():
            return False
        prior = file_path.read_text(encoding="utf-8")
        fm = self.read_frontmatter(file_path)
        if not fm:
            return False
        # Routing the override block through `update_fields` would let any
        # caller bypass the `update_user_fields` invariant that overrides
        # never touch the body. Refuse and force callers to use the
        # dedicated method.
        updates = {k: v for k, v in updates.items() if k != USER_OVERRIDE_KEY}
        fm.update(updates)
        new_content = self._compose_file(fm, prior_content=prior, file_path=file_path)
        if new_content != prior:
            file_path.write_text(new_content, encoding="utf-8")
            return True
        return False

    def update_user_fields(self, file_path: Path, **updates) -> bool:
        """
        Merge `updates` into the file's `booki_user_override` block.

        This is how UI / manual edits land on disk: the user's value goes
        into the override block and shadows the authoritative top-level
        value at read time (see `view_fm`). The top-level value is left
        intact so clearing an override (popping the key) reveals the
        original source / enricher value again.

        Surgical: only the frontmatter YAML is rewritten — the markdown
        body below `---` is left byte-for-byte unchanged. UI metadata
        edits never touch the human-readable content.
        """
        if not file_path.exists():
            return False
        original = file_path.read_text(encoding="utf-8")
        m = re.match(r"^---\n(.*?)\n---\n", original, re.DOTALL)
        if not m:
            return False
        body = original[m.end():]

        fm = _parse_yaml_block(m.group(1))
        existing_user = fm.get(USER_OVERRIDE_KEY)
        user_block = dict(existing_user) if isinstance(existing_user, dict) else {}
        for k, v in updates.items():
            user_block[k] = v
        fm[USER_OVERRIDE_KEY] = user_block

        new_content = self._render_frontmatter(fm) + body
        if new_content != original:
            file_path.write_text(new_content, encoding="utf-8")
            return True
        return False

    # ── internal ────────────────────────────────────────────────────────────

    def _build_fm(self, item: Item, today: str, existing: dict,
                  *, pin: bool = False) -> dict:
        """
        Build the frontmatter dict for this Item.

        Layering (bottom → top, later wins):
          1. Core fields derived from Item (title/url/source/kind/...).
          2. Source-provided `extras` — authoritative for source-specific data.
          3. User-editable fields pulled from `existing` (importance/tags/notes).
          4. Enrichment fields pulled from `existing`.
          5. Any other unknown keys in `existing` — preserved verbatim.

        When `pin=True`, the file already exists under a different primary
        source. We keep the existing location/label/extras (that source
        owns the item) and only merge the new source's slug into `sources`.
        """
        # Display label for `source:` — for browser items we emit the
        # human-readable browser name (e.g. "Chrome") for backward compat
        # with existing files. Other sources emit their slug.
        source_label = item.extras.get("browser_label") or item.source

        breadcrumb = item.extras.get("browser_path") or " › ".join(item.path)
        folder_rel = "/".join(_slug(p) for p in item.path)

        if pin:
            # Existing file owned by another source — keep its location fields.
            source_label = existing.get("source", source_label)
            breadcrumb = existing.get("browser_path", breadcrumb)
            folder_rel = existing.get("folder_path", folder_rel)

        fm: dict = {
            "title":                item.title if not pin else existing.get("title", item.title),
            "url":                  item.url,
            "source":               source_label,
            "kind":                 existing.get("kind", item.kind) if pin else item.kind,
            "browser_path":         breadcrumb,
            "folder_path":          folder_rel,
            "importance":           int(existing.get("importance", 0)),
            "tags":                 existing.get("tags", []) or [],
            "lists":                existing.get("lists", []) or [],
            "notes":                str(existing.get("notes", "")),
            "date_bookmarked":      item.date_added or str(existing.get("date_bookmarked", "")),
            "last_sync":            today,
            "status":               str(existing.get("status", "unchecked")),
            "archive_url":          str(existing.get("archive_url", "")),
        }

        # Union of all source slugs that have produced this URL.
        # The primary source (fm["source"]) must always be represented —
        # older writes (pre-multi-source code) may have left `sources`
        # missing that slug; keep the list self-healing.
        existing_sources = [str(s) for s in (existing.get("sources") or [])]
        primary_slug = _slug(str(fm.get("source", "")))
        merged: list[str] = []
        for s in [primary_slug, *existing_sources, item.source]:
            s = str(s).strip()
            if s and s not in merged:
                merged.append(s)
        fm["sources"] = merged

        # `removed_*` starts false on an active fetch — we only flip it via
        # detach_source() during orphan detection, never during write().
        if item.source in ("chrome", "safari", "firefox"):
            fm["removed_from_browser"] = False
        else:
            fm["removed_from_source"] = False
        # If this source is re-appearing after being marked removed, clear
        # any stale removed flags so the file is considered live again.
        if pin:
            for flag in ("removed_from_browser", "removed_from_source"):
                if existing.get(flag) is False:
                    fm[flag] = False

        if pin:
            # Pinned: the primary source owns extras. Don't let the secondary
            # source overwrite them. But do preserve them verbatim.
            skip_extras_overwrite = True
        else:
            skip_extras_overwrite = False

        if not skip_extras_overwrite:
            # Source-provided extras (authoritative — overwrite existing).
            # USER_OVERRIDE_KEY is owned by `update_user_fields` only — a
            # source that tries to inject it would shadow its own (or worse,
            # another source's) authoritative fields via `view_fm`.
            for k, v in item.extras.items():
                if k in ("browser_path", "browser_label"):
                    continue   # already folded into core fields above
                if k == USER_OVERRIDE_KEY:
                    log.warning(
                        "source_attempted_override_injection",
                        extra={"source": item.source, "url": item.url},
                    )
                    continue
                fm[k] = v

        # Preserve enrichment fields written on earlier runs.
        for k in ENRICH_FIELDS:
            if k in existing:
                fm[k] = existing[k]

        # Preserve any other keys the user or older code may have added
        # (this is also what keeps the primary source's extras intact on a
        # pinned write — they flow through from `existing`).
        skip = set(fm.keys()) | {"browser_path", "browser_label"}
        for k, v in existing.items():
            if k not in skip:
                fm[k] = v

        return fm

    def _render_frontmatter(self, fm: dict) -> str:
        """Render just the YAML frontmatter block including the closing
        `---\\n`. Pair with the file's existing body to do surgical
        frontmatter-only rewrites (see `update_user_fields`)."""
        lines = ["---"]
        seen: set[str] = set()
        for key in FM_ORDER:
            if key in fm:
                lines.append(f"{key}: {_yaml_str(fm[key])}")
                seen.add(key)
        # Remaining keys (extras, unknowns) — alphabetical for stable diffs.
        for key in sorted(k for k in fm if k not in seen):
            lines.append(f"{key}: {_yaml_str(fm[key])}")
        lines.append("---")
        return "\n".join(lines) + "\n"

    def _render_body_content(self, fm: dict) -> str:
        """Render the human-readable body content (no markers, no frontmatter).

        Every source-supplied string is run through `_neutralize_html_comments`
        before it lands in the body so a hostile source can't plant a fake
        `booki:end` marker that would split the managed block.
        """
        n = _neutralize_html_comments

        lines: list[str] = []
        title = n(_escape_md(str(fm.get("title", ""))))
        lines.append(f"# {title}")
        lines.append("")
        lines.append(f"**URL:** {n(str(fm.get('url','')))}  ")
        lines.append(f"**Path:** {n(str(fm.get('browser_path','')))}  ")
        lines.append(f"**Importance:** ★{fm.get('importance', 0)}")

        if fm.get("removed_from_browser") or fm.get("removed_from_source"):
            lines.append("")
            lines.append("> ⚠️ No longer present at source (kept for your records)")

        if notes := n(str(fm.get("notes", "")).strip()):
            lines.extend(["", "## Notes", "", notes])
        if summary := n(str(fm.get("summary", "")).strip()):
            lines.extend(["", "## Summary", "", summary])
        if keywords := fm.get("keywords") or []:
            lines.extend(["", "## Keywords", "",
                          ", ".join(n(str(k)) for k in keywords)])

        return "\n".join(lines).rstrip()

    def _render(self, fm: dict) -> str:
        """Render a fresh file (frontmatter + marker-wrapped body).

        Used when no prior content exists. Existing files go through
        `_compose_file` so user edits outside the markers are preserved.
        """
        return self._compose_file(fm, prior_content="", file_path=None)

    def _compose_file(self, fm: dict, *, prior_content: str,
                      file_path: Optional[Path]) -> str:
        """
        Build the full file text.

        Composition rules (see Body markers section above):
          • No prior content → frontmatter + START + body + END.
          • Prior content with START + END → replace only between markers,
            preserve the text before START and after END verbatim.
          • Prior content with START only → log.warn, keep prior body
            untouched. Frontmatter still gets the new values.
          • Prior content with neither marker → keep prior body untouched
            (user opted out, or legacy file). Frontmatter still updates.
        """
        fm_block = self._render_frontmatter(fm)
        body_inside = self._render_body_content(fm)

        if not prior_content:
            return (
                fm_block
                + "\n" + BOOKI_START_MARKER
                + "\n\n" + body_inside
                + "\n\n" + BOOKI_END_MARKER + "\n"
            )

        m = re.match(r"^---\n(.*?)\n---\n", prior_content, re.DOTALL)
        prior_body = prior_content[m.end():] if m else prior_content

        start_m = _BOOKI_START_RE.search(prior_body)
        if start_m is None:
            return fm_block + prior_body

        end_m = _BOOKI_END_RE.search(prior_body, start_m.end())
        if end_m is None:
            log.warning(
                "booki_end_marker_missing",
                extra={"file": str(file_path) if file_path else ""},
            )
            return fm_block + prior_body

        before = prior_body[:start_m.end()]
        after = prior_body[end_m.start():]
        new_body = before + "\n\n" + body_inside + "\n\n" + after
        return fm_block + new_body

    def _prune_empty(self, directory: Path) -> None:
        try:
            root = self.output_dir.resolve()
            cur = directory.resolve()
            while cur != root and cur.is_relative_to(root):
                if not any(cur.iterdir()):
                    cur.rmdir()
                    cur = cur.parent
                else:
                    break
        except Exception:
            pass
