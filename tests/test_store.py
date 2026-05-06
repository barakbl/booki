"""
Tests for `core.store.ItemStore`: write/read roundtrip, partial updates,
and the removed-flag flip.

The store is the on-disk source of truth. Anything that corrupts the
markdown layout, drops user-edited fields, or loses extras here is a data
loss bug — these tests catch the regressions that matter most.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.ingest import parse_bookmark_file
from core.store import (
    BOOKI_END_MARKER,
    BOOKI_START_MARKER,
    ItemStore,
    today_str,
    view_fm,
)
from plugins.base import Item


@pytest.fixture
def store(tmp_path: Path) -> ItemStore:
    out = tmp_path / "bookmarks"
    out.mkdir()
    return ItemStore(out)


def _make_item(url: str = "https://example.com/x",
               title: str = "Hello",
               source: str = "manual",
               extras: dict | None = None) -> Item:
    return Item(
        title=title,
        url=url,
        source=source,
        kind="bookmark",
        path=[source, "Inbox"],
        date_added=today_str(),
        extras=extras or {},
    )


# ─── write ───────────────────────────────────────────────────────────────

def test_write_creates_parseable_markdown(store: ItemStore) -> None:
    """End-to-end: an Item written to disk roundtrips through the parser."""
    result = store.write(_make_item(), today_str())

    assert result.is_new is True
    assert result.path.exists()
    fm = parse_bookmark_file(result.path)
    assert fm is not None
    assert fm["url"] == "https://example.com/x"
    assert fm["title"] == "Hello"
    assert fm["source"] == "manual"
    assert fm["kind"] == "bookmark"
    assert fm["last_sync"] == today_str()
    # `sources` is the canonical multi-source list — must include the slug
    # we wrote with even when the input Item didn't set one explicitly.
    assert "manual" in fm.get("sources", [])


def test_write_is_idempotent_for_same_url(store: ItemStore) -> None:
    """Writing the same URL twice produces one file, not two — and the
    second write reports `is_new=False` so a downstream sync can tell
    whether it was a creation or a refresh."""
    first = store.write(_make_item(title="Hello"), today_str())
    second = store.write(_make_item(title="Hello"), today_str())

    assert first.is_new is True
    assert second.is_new is False
    assert first.path == second.path
    assert len(list(store.output_dir.rglob("*.md"))) == 1


def test_write_persists_extras(store: ItemStore) -> None:
    item = _make_item(extras={"github_stars": 99, "channel": "Fireship"})
    result = store.write(item, today_str())

    fm = parse_bookmark_file(result.path)
    assert fm["github_stars"] == 99
    assert fm["channel"] == "Fireship"


# ─── update_fields ───────────────────────────────────────────────────────

def test_update_fields_mutates_only_targeted_keys(store: ItemStore) -> None:
    """A partial update must keep every other field — including extras and
    user-edited fields like notes — verbatim."""
    item = _make_item(extras={"github_stars": 5})
    path = store.write(item, today_str()).path
    before = parse_bookmark_file(path)

    changed = store.update_fields(path, importance=8, notes="my notes")

    assert changed is True
    after = parse_bookmark_file(path)
    assert after["importance"] == 8
    assert after["notes"] == "my notes"
    # Untouched fields preserved.
    assert after["title"] == before["title"]
    assert after["url"] == before["url"]
    assert after["github_stars"] == 5


def test_update_fields_returns_false_when_nothing_changes(store: ItemStore) -> None:
    """Idempotent update — same value in, same on-disk content, no rewrite."""
    path = store.write(_make_item(), today_str()).path
    assert store.update_fields(path, importance=0) is False


def test_update_fields_returns_false_for_unknown_path(store: ItemStore, tmp_path: Path) -> None:
    assert store.update_fields(tmp_path / "nope.md", importance=1) is False


# ─── mark_removed ────────────────────────────────────────────────────────

def test_mark_removed_sets_flag(store: ItemStore) -> None:
    path = store.write(_make_item(), today_str()).path
    assert parse_bookmark_file(path).get("removed_from_source") in (None, False)

    flipped = store.mark_removed(path, today_str())

    assert flipped is True
    assert parse_bookmark_file(path)["removed_from_source"] is True


def test_mark_removed_is_idempotent(store: ItemStore) -> None:
    """Second call should be a no-op — the file's already flagged."""
    path = store.write(_make_item(), today_str()).path
    store.mark_removed(path, today_str())

    assert store.mark_removed(path, today_str()) is False


# ─── user-override block ─────────────────────────────────────────────────

def test_view_fm_overlays_override_block_onto_top_level() -> None:
    """`view_fm` is the resolver every reader goes through — the override
    block has to win over top-level fields, and the override key itself
    must be hidden so it doesn't leak into UI extras / search."""
    fm = {"title": "Source Title", "summary": "auto",
          "booki_user_override": {"title": "My Title", "tags": ["x"]}}
    view = view_fm(fm)
    assert view["title"] == "My Title"
    assert view["tags"] == ["x"]
    assert view["summary"] == "auto"        # not overridden — fallback wins
    assert "booki_user_override" not in view


def test_update_user_fields_writes_into_override_block(store: ItemStore) -> None:
    """UI / manual edits land in `booki_user_override` so source-authored
    fields stay intact; reads come back through the view-overlay."""
    path = store.write(_make_item(title="Source Title"), today_str()).path

    changed = store.update_user_fields(path, title="My Title", notes="hi")

    assert changed is True
    raw = store.read_frontmatter(path)
    assert raw["title"] == "Source Title"          # original preserved
    assert raw["booki_user_override"] == {"title": "My Title", "notes": "hi"}
    # parse_bookmark_file returns the view — that's what readers see.
    view = parse_bookmark_file(path)
    assert view["title"] == "My Title"
    assert view["notes"] == "hi"


def test_update_user_fields_leaves_body_untouched(store: ItemStore) -> None:
    """Metadata edits must not rewrite the markdown body — only the YAML
    frontmatter changes. The body is written by sources/enrichers and is
    a human-readable rendering of the source-authoritative fields."""
    path = store.write(_make_item(title="Source Title"), today_str()).path
    original = path.read_text(encoding="utf-8")
    body_before = original.split("---\n", 2)[2]

    store.update_user_fields(path, title="My Title", notes="hi")

    body_after = path.read_text(encoding="utf-8").split("---\n", 2)[2]
    assert body_after == body_before
    # And the source title is still in the body — overrides are
    # frontmatter-only, view-overlay is a read-time concern.
    assert "# Source Title" in body_after


# ─── body markers ────────────────────────────────────────────────────────

def test_first_write_wraps_body_in_booki_markers(store: ItemStore) -> None:
    """A freshly created file has the START/END markers around all of
    Booki's auto-rendered body content, with nothing outside."""
    path = store.write(_make_item(title="Hello"), today_str()).path
    text = path.read_text(encoding="utf-8")

    assert BOOKI_START_MARKER in text
    assert BOOKI_END_MARKER in text
    # Title heading lives inside the markers (option 2a — entire body managed).
    after_marker = text.split(BOOKI_START_MARKER, 1)[1]
    inside, after_end = after_marker.split(BOOKI_END_MARKER, 1)
    assert "# Hello" in inside
    # Nothing outside the END marker on first write.
    assert after_end.strip() == ""


def test_resync_preserves_user_content_outside_markers(store: ItemStore) -> None:
    """User prose before booki:start and after booki:end is sacred — a
    re-sync that updates source-side fields must leave it byte-identical."""
    path = store.write(_make_item(title="Original"), today_str()).path
    original = path.read_text(encoding="utf-8")

    # User adds prose before and after the booki block.
    augmented = original.replace(
        BOOKI_START_MARKER,
        "## My personal notes\n\nThis link is gold.\n\n" + BOOKI_START_MARKER,
    ).replace(
        BOOKI_END_MARKER,
        BOOKI_END_MARKER + "\n\n## After\n\nMore prose here.",
    )
    path.write_text(augmented, encoding="utf-8")

    # Source re-sync with a different importance so the inside content changes.
    store.write(_make_item(title="Original",
                           extras={"importance_hint": 5}), today_str())

    after = path.read_text(encoding="utf-8")
    assert "## My personal notes" in after
    assert "This link is gold." in after
    assert "## After" in after
    assert "More prose here." in after
    # Markers still both present.
    assert BOOKI_START_MARKER in after
    assert BOOKI_END_MARKER in after


def test_resync_skips_body_when_start_marker_removed(store: ItemStore) -> None:
    """If the user removes the START marker, Booki keeps its hands off the
    body entirely — no warning, no edit. Frontmatter still flows."""
    path = store.write(_make_item(title="Hello"), today_str()).path
    # User strips both markers and writes their own body.
    fm_block, _, _ = path.read_text(encoding="utf-8").partition(BOOKI_START_MARKER)
    custom_body = "# My own title\n\nMy personal notes only.\n"
    path.write_text(fm_block + custom_body, encoding="utf-8")

    # A re-sync changes notes (frontmatter field) — body must stay user-owned.
    store.update_fields(path, notes="should not appear in body")

    after = path.read_text(encoding="utf-8")
    assert "# My own title" in after
    assert "My personal notes only." in after
    # Notes section was NOT injected — no markers means no body edits.
    assert "## Notes" not in after.split("---\n", 2)[2]
    # Frontmatter was still updated.
    assert store.read_frontmatter(path)["notes"] == "should not appear in body"


def test_resync_logs_warning_when_only_end_marker_missing(
    store: ItemStore, caplog
) -> None:
    """START present, END missing → log.warn every sync; body untouched.
    The user's broken markup is a structural issue we surface but never
    silently repair."""
    path = store.write(_make_item(title="Hello"), today_str()).path
    # Strip the END marker, keep START.
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace(BOOKI_END_MARKER, ""), encoding="utf-8")

    pre = path.read_text(encoding="utf-8")
    body_pre = pre.split("---\n", 2)[2]

    import logging as _logging
    with caplog.at_level(_logging.WARNING, logger="booki.store"):
        store.update_fields(path, notes="another note")

    body_post = path.read_text(encoding="utf-8").split("---\n", 2)[2]
    assert body_post == body_pre   # body byte-identical
    assert any(
        "booki_end_marker_missing" in rec.message
        for rec in caplog.records
    )


def test_user_block_survives_resync(store: ItemStore) -> None:
    """Re-fetching the same URL from the source must not clobber the
    user's overrides — the override block has to be preserved verbatim
    just like enrichment fields are."""
    path = store.write(_make_item(title="Original"), today_str()).path
    store.update_user_fields(path, title="My Title", notes="my notes")

    # Source re-syncs the same item — extras refreshed, overrides untouched.
    store.write(_make_item(title="Original",
                           extras={"github_stars": 7}), today_str())

    raw = store.read_frontmatter(path)
    assert raw["title"] == "Original"
    assert raw["github_stars"] == 7
    assert raw["booki_user_override"]["title"] == "My Title"
    assert raw["booki_user_override"]["notes"] == "my notes"
