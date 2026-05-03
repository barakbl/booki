"""
HTTP-level tests for the FastAPI app.

These tests boot the real `create_app()` against a temp config + seeded
bookmarks dir (see `conftest.py::client`), then drive it through Starlette's
TestClient. They verify the contract every frontend / external caller sees:
status codes, response shapes, and the side effects on disk for the
mutating routes.

Scope:
  * Read paths      — /api/health, /api/bookmarks, /api/bookmarks/{id},
                      /api/stats, /api/schema, /api/kinds
  * Mutating paths  — PUT /api/bookmarks/{id}, POST /api/link
  * Error paths     — unknown id → 404, malformed payload → 4xx

We intentionally don't test the exact contents of derived fields
(`source`, `sources`) for *every* route — the unit tests in test_ingest /
test_store cover those — only the integration boundary the API exposes.
"""

from __future__ import annotations

from pathlib import Path

import pytest


# ─── Liveness + listing ──────────────────────────────────────────────────

def test_health_returns_ok(client) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True
    # Health doubles as a quick sanity check on bookmark loading — every
    # caller (booki-manager, the dev script) reads `count` from here.
    assert body.get("count") == 3


def test_list_bookmarks_returns_seed(client) -> None:
    r = client.get("/api/bookmarks")
    assert r.status_code == 200
    items = r.json()

    titles = {b["title"] for b in items}
    assert {"Plain Bookmark", "Enriched GitHub Repo", "Gone From Disk"} <= titles
    # Every item must carry a stable id and a url — it's the contract every
    # frontend tab relies on.
    for b in items:
        assert b["id"] and isinstance(b["id"], str)
        assert b["url"]


def test_list_marks_enriched_via_has_summary(client) -> None:
    """`has_summary` is the field every UI uses to count enrichment.
    Asserted at the API level so a later refactor can't quietly break it."""
    r = client.get("/api/bookmarks")
    by_title = {b["title"]: b for b in r.json()}
    assert by_title["Enriched GitHub Repo"]["has_summary"] is True
    assert by_title["Plain Bookmark"]["has_summary"] is False


def test_list_surfaces_extras_for_plugin_fields(client) -> None:
    """github_* / last_enriched are NOT in CORE_FIELDS — they're served
    under `extras`. The advanced-search Top-N feature reads them from there."""
    items = client.get("/api/bookmarks").json()
    enriched = next(b for b in items if b["title"] == "Enriched GitHub Repo")
    assert enriched["extras"]["github_stars"] == 42
    assert enriched["extras"]["last_enriched"] == "2026-05-03"


# ─── Detail view ─────────────────────────────────────────────────────────

def test_get_bookmark_returns_full_detail(client) -> None:
    items = client.get("/api/bookmarks").json()
    target = next(b for b in items if b["title"] == "Enriched GitHub Repo")

    r = client.get(f"/api/bookmarks/{target['id']}")

    assert r.status_code == 200
    body = r.json()
    assert body["id"] == target["id"]
    assert body["url"] == target["url"]
    # The detail endpoint is the only one that returns `body` and `file`.
    assert "body" in body
    assert "file" in body and body["file"].endswith(".md")


def test_get_bookmark_unknown_id_is_404(client) -> None:
    r = client.get("/api/bookmarks/0000000000000000")
    assert r.status_code == 404


# ─── Update ──────────────────────────────────────────────────────────────

def test_put_bookmark_updates_frontmatter_on_disk(client, bookmarks_dir: Path) -> None:
    """The mutating route is the only place where bad behavior corrupts
    user data, so we verify both the response AND that the .md changed."""
    items = client.get("/api/bookmarks").json()
    target = next(b for b in items if b["title"] == "Plain Bookmark")
    file_path = bookmarks_dir / "chrome" / "bookmarks_bar" / "plain--aaaa1111.md"
    before = file_path.read_text()

    r = client.put(
        f"/api/bookmarks/{target['id']}",
        json={"importance": 9, "notes": "edited via API"},
    )

    assert r.status_code == 200
    body = r.json()
    assert body["importance"] == 9
    assert body["notes"] == "edited via API"

    # Disk reflects the change — and was actually rewritten.
    after = file_path.read_text()
    assert after != before
    assert "importance: 9" in after
    assert "edited via API" in after


def test_put_bookmark_unknown_id_is_404(client) -> None:
    r = client.put(
        "/api/bookmarks/0000000000000000",
        json={"importance": 1},
    )
    assert r.status_code == 404


# ─── Stats / schema / kinds ──────────────────────────────────────────────

def test_stats_counts_match_seed(client) -> None:
    r = client.get("/api/stats")
    assert r.status_code == 200
    body = r.json()

    # 3 seeded items, 1 with a non-empty summary.
    assert body["total"] == 3
    assert body["enriched"] == 1
    # Most recent last_sync among the seeds.
    assert body["last_sync"] == "2026-05-03"
    # Per-source / per-kind buckets cover the seeds we wrote.
    assert body["by_source"].get("Chrome") == 2
    assert body["by_kind"].get("bookmark") == 2
    assert body["by_kind"].get("file") == 1


def test_schema_is_a_keyed_map(client) -> None:
    """Don't assert specific plugins — just that the endpoint returns a map
    keyed by plugin slug. Frontend autocomplete builds off this."""
    r = client.get("/api/schema")
    assert r.status_code == 200
    schema = r.json()
    assert isinstance(schema, dict)
    for slug, specs in schema.items():
        assert isinstance(slug, str) and slug
        assert isinstance(specs, list)


def test_kinds_endpoint_is_a_map(client) -> None:
    r = client.get("/api/kinds")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, dict)


# ─── Add link ────────────────────────────────────────────────────────────

def test_post_link_with_explicit_title_writes_a_new_md(empty_client, empty_bookmarks_dir: Path) -> None:
    """An explicit `title` skips the network call to fetch <title>, so this
    test stays fully local. That's also the path the inline 'Add link'
    button takes when the user has typed a label."""
    r = empty_client.post(
        "/api/link",
        json={"url": "https://example.com/manual", "title": "Manually Added"},
    )

    assert r.status_code == 200
    body = r.json()
    assert body["is_new"] is True
    assert body["title"] == "Manually Added"
    assert body["url"] == "https://example.com/manual"

    # The file landed on disk and is parseable.
    md_files = list(empty_bookmarks_dir.rglob("*.md"))
    assert len(md_files) == 1


def test_post_link_rejects_empty_url(empty_client) -> None:
    r = empty_client.post("/api/link", json={"url": "", "title": "x"})
    assert r.status_code in (400, 422)
