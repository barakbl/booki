"""Tests for the corrupted-file loader and the surfaces that consume it.

The loader is the single point of contact between every reader (CLI
ingest, browse, doctor, web) and the on-disk markdown. The contract is:

  * Good files load and return the parsed frontmatter.
  * Bad files never raise — they return a structured `LoadError`.
  * Schema mismatches are non-blocking — the file still loads, but the
    error is reported so the UI can flag it.

These tests pin the contract; they're not testing the YAML parser itself
(that's covered indirectly by `tests/test_store.py`).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.loader import (
    LoadError,
    load_bookmark,
    load_bookmark_full,
    scan_bookmarks,
)


def _write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# ─── load_bookmark / load_bookmark_full ──────────────────────────────────

def test_load_bookmark_happy_path(tmp_path: Path):
    p = _write(tmp_path / "ok.md", """\
---
title: OK
url: "https://example.com"
importance: 5
tags: ["a", "b"]
---

body
""")
    fm, err = load_bookmark(p)
    assert err is None
    assert fm["title"] == "OK"
    assert fm["importance"] == 5
    assert fm["tags"] == ["a", "b"]


def test_load_bookmark_missing_file(tmp_path: Path):
    fm, err = load_bookmark(tmp_path / "nope.md")
    assert fm is None
    assert err is not None
    assert err.kind == "read"


def test_load_bookmark_no_frontmatter(tmp_path: Path):
    p = _write(tmp_path / "raw.md", "just a body, no frontmatter at all\n")
    fm, err = load_bookmark(p)
    assert fm is None
    assert err.kind == "missing_frontmatter"


def test_load_bookmark_unclosed_frontmatter(tmp_path: Path):
    p = _write(tmp_path / "bad.md", "---\ntitle: never closes\n\nbody body body\n")
    fm, err = load_bookmark(p)
    assert fm is None
    assert err.kind == "unclosed"
    assert "not closed" in err.message


def test_load_bookmark_empty_file(tmp_path: Path):
    p = _write(tmp_path / "empty.md", "")
    fm, err = load_bookmark(p)
    assert fm is None
    assert err.kind == "missing_frontmatter"


def test_schema_warning_importance_string(tmp_path: Path):
    p = _write(tmp_path / "imp.md", """\
---
title: Wrong importance
url: "https://example.com"
importance: ten
---
""")
    # Schema errors are non-blocking — fm is still returned.
    fm, err = load_bookmark(p)
    assert fm is not None
    assert err is None  # blocking-only API hides schema warnings

    fm2, errs = load_bookmark_full(p)
    assert fm2 is not None
    schema_errs = [e for e in errs if e.kind == "schema"]
    assert any(e.field == "importance" and e.expected == "int"
               and e.got == "string" for e in schema_errs)


def test_schema_warning_tags_scalar(tmp_path: Path):
    # Without [...] the parser leaves it as a string — flag the type mismatch.
    p = _write(tmp_path / "tags.md", """\
---
title: Wrong tags
url: "https://example.com"
tags: just-a-string
---
""")
    fm, errs = load_bookmark_full(p)
    assert fm is not None
    assert any(e.field == "tags" and e.expected == "list" for e in errs)


def test_schema_warning_bool_unset_tolerated(tmp_path: Path):
    # Empty boolean = "unset" — should NOT produce a warning.
    p = _write(tmp_path / "bool.md", """\
---
title: OK
url: "https://example.com"
removed_from_browser:
---
""")
    fm, errs = load_bookmark_full(p)
    assert fm is not None
    assert [e for e in errs if e.kind == "schema"] == []


def test_yaml_diagnostic_line_without_colon(tmp_path: Path):
    p = _write(tmp_path / "nocolon.md", """\
---
title: OK
url: "https://example.com"
this_line_has_no_colon
---
""")
    fm, errs = load_bookmark_full(p)
    assert fm is not None
    # Line numbers are 1-indexed within the YAML block (after the opening `---`).
    yaml_errs = [e for e in errs if e.kind == "yaml"]
    assert yaml_errs and yaml_errs[0].line == 3


# ─── scan_bookmarks ───────────────────────────────────────────────────────

def test_scan_aggregates_errors(tmp_path: Path):
    root = tmp_path / "bookmarks"
    _write(root / "good.md", "---\ntitle: G\nurl: \"https://x\"\n---\n")
    _write(root / "broken.md", "---\ntitle: never closes\nbody\n")
    _write(root / "no-fm.md", "no frontmatter here\n")

    scan = scan_bookmarks(root)
    assert scan.scanned == 3
    assert len(scan.items) == 1
    assert scan.skipped == 2  # broken + no-fm

    kinds = {e.kind for e in scan.errors}
    assert "unclosed" in kinds
    assert "missing_frontmatter" in kinds


def test_scan_empty_dir(tmp_path: Path):
    scan = scan_bookmarks(tmp_path / "does-not-exist")
    assert scan.scanned == 0
    assert scan.items == []
    assert scan.errors == []


# ─── Web API surface ──────────────────────────────────────────────────────

def test_api_library_errors_lists_corrupted(client, bookmarks_dir):
    # Drop a broken file into the seeded fixture.
    (bookmarks_dir / "chrome" / "bookmarks_bar" / "broken--xxxx9999.md").write_text(
        "---\ntitle: dangling\nbody body body\n", encoding="utf-8"
    )
    r = client.get("/api/library/errors")
    assert r.status_code == 200
    data = r.json()
    assert data["skipped"] >= 1
    paths = [e["rel_path"] for e in data["errors"]]
    assert any(p.endswith("broken--xxxx9999.md") for p in paths)


def test_api_stats_reports_error_count(client, bookmarks_dir):
    (bookmarks_dir / "broken.md").write_text(
        "no frontmatter at all\n", encoding="utf-8"
    )
    r = client.get("/api/stats")
    assert r.status_code == 200
    data = r.json()
    # The seeded fixture has 3 valid files; the new broken one should not
    # be counted in `total` but should be reflected in `errors_count`.
    assert data["errors_count"] >= 1
    assert data["scanned"] >= data["total"]


def test_api_library_errors_empty_when_clean(client):
    r = client.get("/api/library/errors")
    assert r.status_code == 200
    data = r.json()
    assert data["skipped"] == 0
    assert data["errors"] == []


# ─── ingest skip behavior ─────────────────────────────────────────────────

def test_bookmark_service_cache_skips_reparse(bookmarks_dir, monkeypatch):
    """Repeat refresh() calls reuse the parsed index when no file changed.

    This is the win the search/stats/errors page-load triple-fetch needs:
    only the first refresh re-parses; the rest just stat-walk and bail.
    """
    from core import web as web_mod
    svc = web_mod.BookmarkService(bookmarks_dir)
    initial_index = svc._index

    # Spy on scan_bookmarks: it MUST NOT be invoked again when the
    # fingerprint matches (i.e. nothing changed on disk).
    calls = {"n": 0}
    real_scan = web_mod.scan_bookmarks
    def spy(d, **kw):
        calls["n"] += 1
        return real_scan(d, **kw)
    monkeypatch.setattr(web_mod, "scan_bookmarks", spy)

    svc.refresh()
    svc.refresh()
    svc.refresh()
    assert calls["n"] == 0
    assert svc._index is initial_index  # same object, not rebuilt

    # Touch a file → fingerprint changes → next refresh must re-parse.
    target = next(iter(bookmarks_dir.rglob("*.md")))
    import os, time as _t
    new_mtime = target.stat().st_mtime + 5
    os.utime(target, (new_mtime, new_mtime))
    svc.refresh()
    assert calls["n"] == 1


def test_bookmark_service_force_bypasses_cache(bookmarks_dir, monkeypatch):
    """force=True parses even when the fingerprint hasn't changed — used
    after same-process writes that may collide with the mtime tick."""
    from core import web as web_mod
    svc = web_mod.BookmarkService(bookmarks_dir)

    calls = {"n": 0}
    real_scan = web_mod.scan_bookmarks
    def spy(d, **kw):
        calls["n"] += 1
        return real_scan(d, **kw)
    monkeypatch.setattr(web_mod, "scan_bookmarks", spy)

    svc.refresh(force=True)
    assert calls["n"] == 1


def test_ingest_loader_skips_broken_files(bookmarks_dir):
    """`load_all_bookmarks` returns errors and silently skips bad files."""
    from core.ingest import load_all_bookmarks

    (bookmarks_dir / "garbage.md").write_text(
        "---\nthis: never closes\n", encoding="utf-8"
    )
    bookmarks, errors = load_all_bookmarks(bookmarks_dir, min_importance=0)
    # Original 3 good files - 1 is removed_from_source - the remaining 2 are
    # included; the broken one is not.
    assert len(bookmarks) == 2
    assert any(e.kind == "unclosed" for e in errors)
