"""
End-to-end sync tests for the booki:start / booki:end body markers.

Unit tests in `test_store.py` exercise `_compose_file` directly. These
go through the full sync pipeline (`SyncEngine.sync_sources`) with a
fake registered Source, to make sure the marker invariants hold across
the parts of the pipeline that real users hit:

  • first sync → files are written with markers wrapping the body.
  • subsequent sync → user prose before/after the markers is preserved
    byte-for-byte.
  • subsequent sync on a file the user stripped of markers → body
    untouched, frontmatter still flows.
  • subsequent sync on a file missing only the END marker → log.warn,
    body untouched.
  • orphan detach (item disappears from source) → outside-marker user
    prose still survives the rewrite that flips removed_from_source.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from core.store import (
    BOOKI_END_MARKER,
    BOOKI_START_MARKER,
    ItemStore,
)
from core.sync import SyncEngine
from plugins.base import Item, Source


class _FakeSource(Source):
    """In-test source that yields whatever items it's been handed."""
    name = "fakesrc"

    def __init__(self) -> None:
        super().__init__()
        self._items: list[Item] = []

    def set_items(self, items: list[Item]) -> None:
        self._items = items

    def is_available(self) -> bool:
        return True

    def fetch(self):
        yield from self._items


def _item(url: str, title: str, *, badge: str = "") -> Item:
    """`badge` flows through `extras` — it's a non-enrichment custom field
    so it gets overwritten on every re-fetch (unlike `summary` which is
    treated as enricher-owned and preserved across syncs)."""
    return Item(
        title=title,
        url=url,
        source="fakesrc",
        kind="bookmark",
        path=["fakesrc"],
        date_added="2026-05-06",
        extras={"badge": badge} if badge else {},
    )


@pytest.fixture
def engine(tmp_path: Path) -> tuple[SyncEngine, _FakeSource, ItemStore]:
    out = tmp_path / "bookmarks"
    out.mkdir()
    store = ItemStore(out)
    src = _FakeSource()
    return SyncEngine(store), src, store


# ─── scenarios ───────────────────────────────────────────────────────────

def test_first_sync_writes_markers_around_body(engine) -> None:
    eng, src, store = engine
    src.set_items([_item("https://a.example", "Alpha", badge="auto")])

    eng.sync_sources([src])

    files = list(store.output_dir.rglob("*.md"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")

    assert BOOKI_START_MARKER in text
    assert BOOKI_END_MARKER in text
    inside = text.split(BOOKI_START_MARKER, 1)[1].split(BOOKI_END_MARKER, 1)[0]
    assert "# Alpha" in inside
    # Nothing outside END on first write.
    after_end = text.split(BOOKI_END_MARKER, 1)[1]
    assert after_end.strip() == ""


def test_second_sync_preserves_user_prose_outside_markers(engine) -> None:
    """The realistic flow: user opens the .md, adds personal notes
    above and below the booki block, then a re-sync touches source-
    side fields. Outside content has to survive byte-for-byte."""
    eng, src, store = engine
    src.set_items([_item("https://a.example", "Alpha", badge="v1")])
    eng.sync_sources([src])

    md = next(store.output_dir.rglob("*.md"))
    text = md.read_text(encoding="utf-8")
    augmented = text.replace(
        BOOKI_START_MARKER,
        "## My personal notes\n\nThis link is gold.\n\n" + BOOKI_START_MARKER,
    ).replace(
        BOOKI_END_MARKER,
        BOOKI_END_MARKER + "\n\n## After the block\n\nMore prose here.",
    )
    md.write_text(augmented, encoding="utf-8")

    # Re-sync with a different summary so the inside block actually changes.
    src.set_items([_item("https://a.example", "Alpha", badge="v2")])
    eng.sync_sources([src])

    after = md.read_text(encoding="utf-8")
    assert "## My personal notes" in after
    assert "This link is gold." in after
    assert "## After the block" in after
    assert "More prose here." in after
    assert BOOKI_START_MARKER in after
    assert BOOKI_END_MARKER in after


def test_second_sync_leaves_body_untouched_when_markers_stripped(engine) -> None:
    """User opted out of booki-managed body by removing the markers.
    A re-sync must not re-inject markers or touch the body at all."""
    eng, src, store = engine
    src.set_items([_item("https://a.example", "Alpha", badge="v1")])
    eng.sync_sources([src])

    md = next(store.output_dir.rglob("*.md"))
    fm_part, _, _ = md.read_text(encoding="utf-8").partition(BOOKI_START_MARKER)
    user_body = "# I rewrote this title myself\n\nMy own prose.\n"
    md.write_text(fm_part + user_body, encoding="utf-8")
    body_before = md.read_text(encoding="utf-8").split("---\n", 2)[2]

    src.set_items([_item("https://a.example", "Alpha", badge="v2")])
    eng.sync_sources([src])

    after = md.read_text(encoding="utf-8")
    body_after = after.split("---\n", 2)[2]
    assert body_after == body_before                # body byte-identical
    assert BOOKI_START_MARKER not in after          # not re-injected
    assert BOOKI_END_MARKER not in after
    # Frontmatter still flowed (badge updated).
    assert store.read_frontmatter(md)["badge"] == "v2"


def test_second_sync_warns_when_only_end_marker_missing(engine, caplog) -> None:
    """START present, END missing → log.warn every sync, body untouched.
    Booki refuses to guess where the managed block should end."""
    eng, src, store = engine
    src.set_items([_item("https://a.example", "Alpha", badge="v1")])
    eng.sync_sources([src])

    md = next(store.output_dir.rglob("*.md"))
    text = md.read_text(encoding="utf-8")
    md.write_text(text.replace(BOOKI_END_MARKER, ""), encoding="utf-8")
    body_before = md.read_text(encoding="utf-8").split("---\n", 2)[2]

    src.set_items([_item("https://a.example", "Alpha", badge="v2")])
    with caplog.at_level(logging.WARNING, logger="booki.store"):
        eng.sync_sources([src])

    body_after = md.read_text(encoding="utf-8").split("---\n", 2)[2]
    assert body_after == body_before                # body byte-identical
    assert any(
        "booki_end_marker_missing" in rec.message
        for rec in caplog.records
    )


def test_hostile_source_cannot_plant_fake_end_marker(engine) -> None:
    """Source-supplied strings (title / summary / notes / keywords) must
    not be able to forge a `<!-- booki:end -->` token that would split
    the managed block. Confirms the `_neutralize_html_comments` guard
    in _render_body_content."""
    eng, src, store = engine
    poison = f"sneaky {BOOKI_END_MARKER} ## attacker section"
    item = Item(
        title=poison, url="https://e.example",
        source="fakesrc", kind="bookmark", path=["fakesrc"],
        date_added="2026-05-06",
        extras={"summary": poison, "keywords": [poison], "badge": ""},
    )
    src.set_items([item])
    eng.sync_sources([src])

    md = next(store.output_dir.rglob("*.md"))
    text = md.read_text(encoding="utf-8")
    # The marker regex only ever scans the body (frontmatter is split off
    # before scanning), so what matters is that the body section contains
    # exactly one real start + one real end. Frontmatter may still have
    # literal tokens in YAML strings — they're inert there.
    body = text.split("---\n", 2)[2]
    assert body.count(BOOKI_END_MARKER) == 1
    assert body.count(BOOKI_START_MARKER) == 1
    # The injected token is still visible to the user as plain text
    # (rendered `&lt;!--` becomes `<!--` in any markdown viewer), so the
    # attack is observable rather than silently absorbed.
    assert "&lt;!-- booki:end -->" in body

    # And — the actual reason this matters — re-syncs stay clean. With
    # the unsanitized version, end-marker count grew on each re-sync
    # because the regex would lock onto the planted token.
    eng.sync_sources([src])
    eng.sync_sources([src])
    body_after = md.read_text(encoding="utf-8").split("---\n", 2)[2]
    assert body_after.count(BOOKI_END_MARKER) == 1
    assert body_after.count(BOOKI_START_MARKER) == 1


def test_orphan_detach_preserves_outside_marker_content(engine) -> None:
    """When a source stops reporting an item, `detach_source` rewrites
    the file to flip the removed flag. That rewrite has to honor the
    same markers — user prose outside the block must survive it."""
    eng, src, store = engine
    src.set_items([_item("https://a.example", "Alpha")])
    eng.sync_sources([src])

    md = next(store.output_dir.rglob("*.md"))
    text = md.read_text(encoding="utf-8")
    md.write_text(
        text.replace(BOOKI_END_MARKER, BOOKI_END_MARKER + "\n\n## My take\n\nKeep this.\n"),
        encoding="utf-8",
    )

    # Source no longer lists this item.
    src.set_items([])
    eng.sync_sources([src])

    after = md.read_text(encoding="utf-8")
    assert "## My take" in after
    assert "Keep this." in after
    assert store.read_frontmatter(md)["removed_from_source"] is True
