"""
Tests for `core.ingest`: bookmark-file parsing and the URL → id hash.

These two functions sit at the bottom of every read path (web, sync,
exporters, vector index) — getting them wrong silently corrupts the rest
of the system. The id function in particular is a stable identifier
written into every URL so tests double down on its invariants.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.ingest import bm_id, parse_bookmark_file


# ─── parse_bookmark_file ─────────────────────────────────────────────────

def test_parse_returns_frontmatter_dict(tmp_path: Path) -> None:
    md = tmp_path / "valid.md"
    md.write_text(
        "---\n"
        'title: Hello\n'
        'url: "https://example.com"\n'
        "importance: 5\n"
        "tags: [a, b]\n"
        "---\n\n"
        "# Body ignored by the parser.\n",
        encoding="utf-8",
    )

    fm = parse_bookmark_file(md)

    assert fm is not None
    assert fm["title"] == "Hello"
    assert fm["url"] == "https://example.com"
    assert fm["importance"] == 5
    assert fm["tags"] == ["a", "b"]


def test_parse_returns_none_when_frontmatter_missing(tmp_path: Path) -> None:
    md = tmp_path / "no-frontmatter.md"
    md.write_text("# Just a heading, no YAML block.\n", encoding="utf-8")

    assert parse_bookmark_file(md) is None


def test_parse_returns_none_for_unreadable_path(tmp_path: Path) -> None:
    # Path that doesn't exist — parser should swallow the IOError.
    assert parse_bookmark_file(tmp_path / "missing.md") is None


# ─── bm_id ───────────────────────────────────────────────────────────────

def test_bm_id_is_deterministic_across_calls() -> None:
    a = bm_id({"url": "https://example.com/x"})
    b = bm_id({"url": "https://example.com/x"})
    assert a == b
    assert len(a) == 16  # documented contract — 16 hex chars


def test_bm_id_is_case_and_trailing_slash_insensitive() -> None:
    """The id is the dedup key. Two URLs that the user would consider the
    same item must collapse to one id."""
    canonical = bm_id({"url": "https://Example.com/x"})
    assert bm_id({"url": "https://example.com/x"})  == canonical
    assert bm_id({"url": "https://example.com/x/"}) == canonical
    assert bm_id({"url": "HTTPS://EXAMPLE.COM/X"})  == canonical


def test_bm_id_distinguishes_distinct_urls() -> None:
    a = bm_id({"url": "https://example.com/a"})
    b = bm_id({"url": "https://example.com/b"})
    assert a != b


def test_bm_id_treats_missing_url_as_empty_string() -> None:
    """Defensive: callers occasionally hand us partial frontmatter when a
    file is mid-write. We don't want a crash there."""
    assert bm_id({}) == bm_id({"url": ""})
