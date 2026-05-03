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
from core.store import ItemStore, today_str
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
