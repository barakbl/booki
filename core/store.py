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
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from plugins.base import Item


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
    "user",
]

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
    Return a flat view of `fm` with the nested `user:` block overlaid on top.

    On disk, frontmatter has two layers:
      - top-level keys written by sources & enrichers (authoritative source data)
      - `user: { … }` block — a user's hand-edits that should shadow the
        authoritative values everywhere we display, search, or export.

    Readers go through this so a user's edited title, summary, tags, etc.
    win over whatever the source / enricher last wrote, while the original
    values are preserved underneath as a fallback. The `user` key itself
    is dropped from the view — it's an internal detail.
    """
    overrides = fm.get("user")
    if not isinstance(overrides, dict) or not overrides:
        return {k: v for k, v in fm.items() if k != "user"}
    out = {k: v for k, v in fm.items() if k != "user"}
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

        existing_primary_slug = _slug(str(existing.get("source", ""))) if existing else ""
        incoming_slug = item.source
        pin_to_existing = bool(
            old_path
            and existing_primary_slug
            and existing_primary_slug != incoming_slug
        )

        target = old_path if pin_to_existing else self.file_for(item)

        fm = self._build_fm(item, today, existing, pin=pin_to_existing)
        content = self._render(fm)

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
        file_path.write_text(self._render(fm), encoding="utf-8")
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
            file_path.write_text(self._render(fm), encoding="utf-8")
            return "detached"

        # Sole (or no) source left — fall through to the removed-flag flip.
        if fm.get(removed_flag) or fm.get("removed_from_browser") or fm.get("removed_from_source"):
            return "noop"
        fm[removed_flag] = True
        fm["sources"] = []
        fm["last_sync"] = today
        file_path.write_text(self._render(fm), encoding="utf-8")
        return "removed"

    def update_fields(self, file_path: Path, **updates) -> bool:
        fm = self.read_frontmatter(file_path)
        if not fm:
            return False
        fm.update(updates)
        new_content = self._render(fm)
        if new_content != file_path.read_text(encoding="utf-8"):
            file_path.write_text(new_content, encoding="utf-8")
            return True
        return False

    def update_user_fields(self, file_path: Path, **updates) -> bool:
        """
        Merge `updates` into the file's nested `user:` override block.

        This is how UI / manual edits land on disk: the user's value goes
        into `user.<key>` and shadows the authoritative top-level value at
        read time (see `view_fm`). The top-level value is left intact so
        clearing the override (deleting the key from `user`) reveals the
        original source / enricher value again.
        """
        fm = self.read_frontmatter(file_path)
        if not fm:
            return False
        existing_user = fm.get("user")
        user_block = dict(existing_user) if isinstance(existing_user, dict) else {}
        for k, v in updates.items():
            user_block[k] = v
        fm["user"] = user_block
        new_content = self._render(fm)
        if new_content != file_path.read_text(encoding="utf-8"):
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
            for k, v in item.extras.items():
                if k in ("browser_path", "browser_label"):
                    continue   # already folded into core fields above
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

    def _render(self, fm: dict) -> str:
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
        lines.append("")

        # Body rendering reflects the view (user-overridden values shadow
        # source / enricher values) so the human-readable section of the
        # file matches what the UI / search / export show.
        view = view_fm(fm)
        title = _escape_md(str(view.get("title", "")))
        lines.append(f"# {title}")
        lines.append("")
        lines.append(f"**URL:** {view.get('url','')}  ")
        lines.append(f"**Path:** {view.get('browser_path','')}  ")
        lines.append(f"**Importance:** ★{view.get('importance', 0)}")

        if view.get("removed_from_browser") or view.get("removed_from_source"):
            lines.append("")
            lines.append("> ⚠️ No longer present at source (kept for your records)")
        lines.append("")

        if notes := str(view.get("notes", "")).strip():
            lines.extend(["## Notes", "", notes, ""])
        if summary := str(view.get("summary", "")).strip():
            lines.extend(["## Summary", "", summary, ""])
        if keywords := view.get("keywords") or []:
            lines.extend(["## Keywords", "", ", ".join(str(k) for k in keywords), ""])

        return "\n".join(lines).rstrip() + "\n"

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
