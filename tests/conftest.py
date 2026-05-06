"""
Shared pytest fixtures for the Booki Python test suite.

The fixtures here exist so each test can describe *what* it needs (a
bookmarks directory, an HTTP client) without spelling out *how* to wire it
up. That keeps individual tests short and focused on the behavior under
test rather than on setup.

Design notes:
  * Every fixture is `tmp_path`-scoped so tests are hermetic — no test
    touches the user's real `config.toml` or `bookmarks/` dir.
  * `seed_bookmarks` writes a small, deterministic set of three items that
    cover the common shapes (a plain Chrome bookmark, an enriched item
    with extras, and a removed-from-source item). Most tests can rely on
    that set without seeding their own.
  * The `client` fixture builds the real FastAPI app via `create_app()`
    pointed at the temp config — i.e. exercises the production wiring,
    not a mocked-up app.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make `core`, `plugins`, etc. importable when pytest is launched from
# anywhere. We can't rely on the user installing the project as a package.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ─── Bookmarks directory + seed ──────────────────────────────────────────

# Three canonical fixtures. Keeping them as raw strings (rather than
# building them with the writer) lets tests assert on the exact frontmatter
# the parser accepts — if someone tightens YAML parsing, these tests fail
# loud and early.

_BM_PLAIN = """\
---
title: Plain Bookmark
url: "https://example.com/one"
source: Chrome
sources: ["chrome"]
kind: bookmark
browser_path: Chrome > Bookmarks Bar
folder_path: chrome/bookmarks_bar
importance: 3
tags: ["work"]
date_bookmarked: 2026-01-15
last_sync: 2026-05-01
status: unchecked
removed_from_browser: false
---

# Plain Bookmark
"""

_BM_ENRICHED = """\
---
title: Enriched GitHub Repo
url: "https://github.com/example/repo"
source: Chrome
sources: ["chrome", "github"]
kind: bookmark
browser_path: Chrome > Bookmarks Bar
folder_path: chrome/bookmarks_bar
importance: 7
tags: ["dev", "ai"]
date_bookmarked: 2026-02-01
last_sync: 2026-05-03
status: unchecked
removed_from_browser: false
last_enriched: 2026-05-03
enrich_source: page
summary: An LLM summary of the repository.
keywords: ["python", "agents"]
github_stars: 42
github_full_name: example/repo
---

# Enriched GitHub Repo
"""

_BM_REMOVED = """\
---
title: Gone From Disk
url: "file:///tmp/old.txt"
source: directory
kind: file
removed_from_source: true
date_bookmarked: 2026-03-01
last_sync: 2026-04-19
---

# Gone From Disk
"""


@pytest.fixture
def bookmarks_dir(tmp_path: Path) -> Path:
    """A temp directory with three seeded bookmark files in a nested layout.

    Layout matches what real syncs produce: source slug as the top folder,
    then the source's own sub-paths.
    """
    root = tmp_path / "bookmarks"
    (root / "chrome" / "bookmarks_bar").mkdir(parents=True)
    (root / "directory").mkdir(parents=True)
    (root / "chrome" / "bookmarks_bar" / "plain--aaaa1111.md").write_text(
        _BM_PLAIN, encoding="utf-8"
    )
    (root / "chrome" / "bookmarks_bar" / "repo--bbbb2222.md").write_text(
        _BM_ENRICHED, encoding="utf-8"
    )
    (root / "directory" / "old--cccc3333.md").write_text(
        _BM_REMOVED, encoding="utf-8"
    )
    return root


@pytest.fixture
def empty_bookmarks_dir(tmp_path: Path) -> Path:
    """A bookmarks dir with no files — used to test add-link and empty-state behavior."""
    root = tmp_path / "bookmarks-empty"
    root.mkdir()
    return root


# ─── App + TestClient ────────────────────────────────────────────────────

def _write_config(config_path: Path, bookmarks_dir: Path) -> None:
    """Minimal config.toml that's enough for the FastAPI app to boot."""
    config_path.write_text(
        f"""\
[bookmarks]
dir = "{bookmarks_dir}"
min_importance = 0

[vector_db]
type = "chromadb"
persist_dir = "{config_path.parent / 'db'}"
collection = "bookmarks"

[downloads]
dir = "{config_path.parent / 'downloads'}"

[web]
host = "127.0.0.1"
port = 8000
""",
        encoding="utf-8",
    )


@pytest.fixture
def app(tmp_path: Path, bookmarks_dir: Path):
    """A real FastAPI app bound to the seeded temp bookmarks dir."""
    from core.web import create_app

    cfg = tmp_path / "config.toml"
    _write_config(cfg, bookmarks_dir)
    return create_app(cfg)


@pytest.fixture
def empty_app(tmp_path: Path, empty_bookmarks_dir: Path):
    """A FastAPI app pointed at an empty bookmarks dir (no seeded items)."""
    from core.web import create_app

    cfg = tmp_path / "config.toml"
    _write_config(cfg, empty_bookmarks_dir)
    return create_app(cfg)


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient
    # Use a loopback Host header so the TrustedHostMiddleware allow-list
    # accepts the request — the default `http://testserver` would 400.
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        yield c


@pytest.fixture
def empty_client(empty_app):
    from fastapi.testclient import TestClient
    with TestClient(empty_app, base_url="http://127.0.0.1:8000") as c:
        yield c
