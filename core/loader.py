"""
loader.py — robust frontmatter loading for bookmark `.md` files.

Every reader in the codebase (search index, ingest, doctor, browse, web)
needs to walk `bookmarks/**/*.md` and pull the YAML frontmatter out. A
single hand-edited file with broken YAML used to wedge whichever pass
hit it first (a stack trace on `int(fm["importance"])`, or a silent
None that dropped the bookmark from the listing).

This module centralises that read so:

  * Bad files are *skipped*, never raise.
  * Each skip is captured as a `LoadError` with enough detail that the
    user can find and fix it (path, error kind, message, and — for
    schema problems — the field, expected type, and what was got).
  * Callers (CLI doctor, web UI) can surface the list to the user
    instead of pretending everything's fine.

The parser is intentionally minimal — it matches `core.store._parse_yaml_block`
exactly so files that load through this module also round-trip through
the writer. Type checking is *opportunistic*: only fields with a known
expected shape are validated; unknown keys are passed through untouched
so plugins / user extras keep working.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from dataclasses import field as dc_field
from pathlib import Path
from typing import Iterable, Iterator, Optional

from .store import _parse_yaml_block

log = logging.getLogger("booki.loader")


FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
# Looser match for diagnostics: `^---\n(.*?)\n---` without requiring the
# trailing newline, so files that end exactly at the closing fence still
# parse and aren't reported as "unclosed".
_FRONTMATTER_RE_NO_TRAIL = re.compile(r"^---\n(.*?)\n---\s*$", re.DOTALL)


# ─── Schema ───────────────────────────────────────────────────────────────────
#
# Only fields with a stable contract across sources are listed. Plugin /
# enricher / user-added keys pass through untouched. Each entry maps to a
# canonical type label used in error messages.

_INT_FIELDS = {
    "importance", "view_count",
}
_STR_FIELDS = {
    "title", "url", "source", "kind", "status",
    "browser_path", "folder_path", "notes", "summary",
    "archive_url", "channel", "channel_id", "video_id",
    "duration", "published_at", "date_bookmarked", "last_sync",
    "last_enriched", "enrich_source", "page_title",
}
_LIST_FIELDS = {
    "tags", "lists", "keywords", "sources", "youtube_tags",
}
_BOOL_FIELDS = {
    "removed_from_browser", "removed_from_source",
    "liked", "watched", "subscribed", "subscribed_to_channel",
    "downloaded",
}


def _type_label(v) -> str:
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, list):
        return "list"
    if isinstance(v, dict):
        return "dict"
    if isinstance(v, str):
        return "string"
    if v is None:
        return "null"
    return type(v).__name__


# ─── Errors ───────────────────────────────────────────────────────────────────

@dataclass
class LoadError:
    """One reason a bookmark `.md` couldn't be loaded.

    Stable shape — serialised straight to JSON for the web UI and to
    plain text for the CLI.

    `kind` is a short slug so the UI can branch on it; `message` is
    always human-readable. For `schema` errors, `field`, `expected`,
    and `got` carry the structured details so the UI can render
    "field <X> should be <Y> but got <Z>".
    """
    path: str
    kind: str          # "read" | "missing_frontmatter" | "unclosed" | "yaml" | "schema"
    message: str
    field: Optional[str] = None
    expected: Optional[str] = None
    got: Optional[str] = None
    line: Optional[int] = None
    extra: dict = dc_field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        if not d.get("extra"):
            d.pop("extra", None)
        return d

    def short(self) -> str:
        """One-line summary suitable for CLI / log output."""
        if self.kind == "schema" and self.field:
            return (f"field `{self.field}` should be {self.expected} "
                    f"but got {self.got}")
        return self.message


# ─── Core load ────────────────────────────────────────────────────────────────

def _validate_schema(path: Path, fm: dict) -> list[LoadError]:
    """Type-check known fields. Unknown keys are not touched."""
    errors: list[LoadError] = []
    for key, value in fm.items():
        if key in _INT_FIELDS:
            # bools are a subclass of int — accept them silently here, the
            # int coercion at use-site is fine.
            if isinstance(value, bool) or not isinstance(value, int):
                if value in ("", None):
                    continue
                errors.append(LoadError(
                    path=str(path), kind="schema",
                    message=f"`{key}` must be an integer",
                    field=key, expected="int", got=_type_label(value),
                ))
        elif key in _LIST_FIELDS:
            if not isinstance(value, list):
                errors.append(LoadError(
                    path=str(path), kind="schema",
                    message=f"`{key}` must be a list (use `[a, b]` syntax)",
                    field=key, expected="list", got=_type_label(value),
                ))
        elif key in _BOOL_FIELDS:
            if not isinstance(value, bool):
                # Tolerate empty string ⇒ unset.
                if value in ("", None):
                    continue
                errors.append(LoadError(
                    path=str(path), kind="schema",
                    message=f"`{key}` must be `true` or `false`",
                    field=key, expected="bool", got=_type_label(value),
                ))
        elif key in _STR_FIELDS:
            if not isinstance(value, str):
                # Numbers / bools accidentally written without quotes are
                # the most common shape here. Flag, don't crash.
                errors.append(LoadError(
                    path=str(path), kind="schema",
                    message=f"`{key}` must be a string",
                    field=key, expected="string", got=_type_label(value),
                ))
    return errors


def load_bookmark(path: Path) -> tuple[Optional[dict], Optional[LoadError]]:
    """Read one `.md` file. Returns `(fm, None)` on success or `(None, err)`
    on any structural failure. Schema warnings (type mismatches) do NOT
    block loading — see `load_bookmark_full` for the full list.

    Designed to be a drop-in replacement for the old per-module
    `parse_bookmark_file` helpers."""
    fm, errs = load_bookmark_full(path)
    # Surface only the first *blocking* error here (read / missing /
    # unclosed / yaml). Schema errors are non-blocking — the fm is
    # still usable for downstream consumers.
    blocking = next((e for e in errs if e.kind != "schema"), None)
    if blocking is not None:
        return None, blocking
    return fm, None


def load_bookmark_full(path: Path) -> tuple[Optional[dict], list[LoadError]]:
    """Same as `load_bookmark` but returns ALL errors found, including
    non-blocking schema mismatches. The fm is returned alongside any
    schema errors so callers can choose to use the partial data."""
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, [LoadError(
            path=str(path), kind="read",
            message="file not found",
        )]
    except UnicodeDecodeError as e:
        return None, [LoadError(
            path=str(path), kind="read",
            message=f"file is not valid UTF-8 ({e.reason})",
        )]
    except OSError as e:
        return None, [LoadError(
            path=str(path), kind="read",
            message=f"could not read file: {e}",
        )]

    if not content.lstrip():
        return None, [LoadError(
            path=str(path), kind="missing_frontmatter",
            message="file is empty",
        )]

    if not content.startswith("---\n") and not content.startswith("---\r\n"):
        return None, [LoadError(
            path=str(path), kind="missing_frontmatter",
            message="missing YAML frontmatter — file must start with `---`",
        )]

    m = FRONTMATTER_RE.match(content)
    if not m:
        m_alt = _FRONTMATTER_RE_NO_TRAIL.match(content)
        if not m_alt:
            return None, [LoadError(
                path=str(path), kind="unclosed",
                message="frontmatter block is not closed — expected a `---` "
                        "line before the body",
            )]
        block = m_alt.group(1)
    else:
        block = m.group(1)

    errors: list[LoadError] = []
    try:
        fm = _parse_yaml_block(block)
    except Exception as e:
        # The home-grown parser is permissive and won't throw, but a
        # future swap (PyYAML, etc.) might. Catch defensively.
        return None, [LoadError(
            path=str(path), kind="yaml",
            message=f"could not parse YAML frontmatter: {e}",
        )]

    # Soft YAML diagnostics. The parser silently drops lines without a
    # colon; surface those so users notice typos that would otherwise
    # vanish without trace.
    for i, raw_line in enumerate(block.splitlines(), start=1):
        s = raw_line.strip()
        if not s or s.startswith("#") or s.startswith("---"):
            continue
        if ":" not in raw_line:
            snippet = s if len(s) <= 120 else s[:117] + "…"
            errors.append(LoadError(
                path=str(path), kind="yaml",
                message=f"line {i}: missing `:` — expected `key: value` "
                        f"(got: {snippet!r})",
                line=i,
            ))

    # The view layer overlays user-overrides — but loader-level type
    # checks should look at what's *on disk* (if a top-level field is
    # broken we still want to flag it, even when overlaid by the user
    # block), so we validate the raw fm here.
    errors.extend(_validate_schema(path, fm))
    return fm, errors


# ─── Bulk scan ────────────────────────────────────────────────────────────────

@dataclass
class ScanResult:
    """Outcome of walking a bookmarks directory."""
    items: list[tuple[Path, dict]]  # (path, fm) for usable files
    errors: list[LoadError]         # one-per-problem-file (worst error wins)
    scanned: int                     # total `.md` files attempted

    @property
    def skipped(self) -> int:
        # A "skipped" file is one we couldn't usefully load (read/missing/
        # unclosed/yaml). Schema-only warnings don't kick the file out.
        bad_paths = {e.path for e in self.errors if e.kind != "schema"}
        return len(bad_paths)


def scan_bookmarks(
    bookmarks_dir: Path,
    *,
    paths: Optional[Iterable[Path]] = None,
) -> ScanResult:
    """Walk `bookmarks_dir/**/*.md`, returning every file we could read
    plus a flat list of every error encountered.

    `paths` overrides the rglob — used by tests and by callers that
    already know the file set. The walk is deterministic (sorted) so
    output ordering is stable across runs.
    """
    items: list[tuple[Path, dict]] = []
    errors: list[LoadError] = []
    scanned = 0

    if paths is None:
        if not bookmarks_dir.exists():
            return ScanResult(items=[], errors=[], scanned=0)
        paths_iter: Iterator[Path] = iter(sorted(bookmarks_dir.rglob("*.md")))
    else:
        paths_iter = iter(sorted(paths))

    for md in paths_iter:
        scanned += 1
        fm, errs = load_bookmark_full(md)
        if errs:
            errors.extend(errs)
            for e in errs:
                if e.kind == "schema":
                    log.warning(
                        "bookmark_schema_warning",
                        extra={"path": str(md), "field": e.field,
                               "expected": e.expected, "got": e.got},
                    )
                else:
                    log.warning(
                        "bookmark_skipped",
                        extra={"path": str(md), "kind": e.kind,
                               "reason": e.message},
                    )
        if fm is not None:
            items.append((md, fm))

    return ScanResult(items=items, errors=errors, scanned=scanned)
