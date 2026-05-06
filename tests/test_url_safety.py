"""
Coverage for the URL scheme allowlist that guards Booki against
javascript:/data:/vbscript: bookmarks (clickable XSS sinks in any
HTML export / web-UI link), and against SSRF via `_fetch_page_title`.

These probes mirror the live security sweep that motivated the guard:
hostile source plugins that yield Items with arbitrary `url` strings,
and the manual-link API that accepts user-supplied URLs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.store import ItemStore, today_str
from core.sync import LinkExcluded, SyncEngine, sync_link
from core.url_safety import is_fetchable_url, is_safe_url
from plugins.base import Item, Source


# ─── unit allowlist ──────────────────────────────────────────────────────

@pytest.mark.parametrize("url,ok", [
    ("https://example.com",            True),
    ("HTTP://EXAMPLE.COM",             True),
    ("file:///Users/me/note.md",       True),
    ("",                               False),
    ("   ",                            False),
    ("javascript:alert(1)",            False),
    ("JaVaScRiPt:alert(1)",            False),
    ("data:text/html,<script>x</script>", False),
    ("vbscript:msgbox(1)",             False),
    ("mailto:a@b",                     False),
    ("gopher://x",                     False),
    # Tricky netloc-shaped scheme — urlparse treats this as netloc=example,
    # but the prefix-only check rejects it correctly.
    ("javascript://example.com/%0aalert(1)", False),
])
def test_is_safe_url_allowlist(url, ok):
    assert is_safe_url(url) is ok


@pytest.mark.parametrize("url,ok", [
    ("https://example.com", True),
    ("http://localhost",    True),
    # file:// is *display-safe* (proxy enforces containment) but we never
    # follow it with requests.get — that would be a separate exfiltration
    # lever beyond the scope of what _fetch_page_title is supposed to do.
    ("file:///etc/passwd",  False),
    ("javascript:alert(1)", False),
])
def test_is_fetchable_url_is_stricter_than_is_safe(url, ok):
    assert is_fetchable_url(url) is ok


# ─── sync_link refuses dangerous schemes ────────────────────────────────

def test_sync_link_refuses_javascript_url(tmp_path: Path):
    store = ItemStore(tmp_path / "bookmarks")
    store.output_dir.mkdir()
    with pytest.raises(ValueError, match="unsupported URL scheme"):
        sync_link("javascript:alert(1)", store, title="x")


def test_sync_link_refuses_data_url(tmp_path: Path):
    store = ItemStore(tmp_path / "bookmarks")
    store.output_dir.mkdir()
    with pytest.raises(ValueError, match="unsupported URL scheme"):
        sync_link("data:text/html,<script>1</script>", store, title="x")


# ─── SyncEngine drops items with non-allowlisted schemes ─────────────────

class _FakeSource(Source):
    name = "fakesrc"

    def __init__(self):
        super().__init__()
        self._items: list[Item] = []

    def set_items(self, items): self._items = items
    def is_available(self): return True
    def fetch(self): yield from self._items


def test_sync_engine_skips_javascript_url_items(tmp_path: Path, caplog):
    """A hostile (or buggy) source that yields a javascript:/data: URL
    must not get its item persisted as a clickable bookmark — those
    URLs become XSS the moment they reach an HTML export or the
    web UI's `<a href>` rendering."""
    store = ItemStore(tmp_path / "bookmarks")
    store.output_dir.mkdir()
    src = _FakeSource()
    src.set_items([
        Item(title="legit",    url="https://ok.example",
             source="fakesrc", kind="bookmark", path=["fakesrc"]),
        Item(title="poison",   url="javascript:alert(1)",
             source="fakesrc", kind="bookmark", path=["fakesrc"]),
        Item(title="poison2",  url="data:text/html,<script>x</script>",
             source="fakesrc", kind="bookmark", path=["fakesrc"]),
    ])

    import logging
    with caplog.at_level(logging.WARNING, logger="booki.sync"):
        stats = SyncEngine(store).sync_sources([src])

    files = list(store.output_dir.rglob("*.md"))
    assert len(files) == 1                      # only the legit https item
    assert stats.items_excluded == 2
    assert sum(
        1 for rec in caplog.records
        if "source_yielded_unsafe_url" in rec.message
    ) == 2


# ─── _fetch_page_title bails before issuing a request ────────────────────

def test_fetch_page_title_refuses_non_fetchable_scheme(monkeypatch):
    """SSRF guard: if `_fetch_page_title` ever issues `requests.get`
    against a non-http(s) URL, it's an exfiltration lever (file://,
    or hijacked-scheme tricks). Verify the function bails before
    even constructing the request."""
    from core import sync as sync_mod

    called = {"n": 0}
    def _fail(*a, **kw):
        called["n"] += 1
        raise AssertionError("requests.get should not have been called")
    monkeypatch.setattr(sync_mod.requests, "get", _fail)

    assert sync_mod._fetch_page_title("file:///etc/passwd") == ""
    assert sync_mod._fetch_page_title("javascript:alert(1)") == ""
    assert sync_mod._fetch_page_title("vbscript:msgbox(1)") == ""
    assert called["n"] == 0
