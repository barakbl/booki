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


# ─── SSRF gate: blocks loopback / private / IMDS / link-local ─────────────

from core.url_safety import is_externally_fetchable_url


@pytest.mark.parametrize("url", [
    # Public addresses pass.
    "https://example.com",
    "https://1.1.1.1",
    "https://[2606:4700:4700::1111]",
])
def test_externally_fetchable_allows_public(url, monkeypatch):
    # `example.com` needs DNS; stub it to a public IP so the test isn't
    # network-dependent. The other two URLs use literal IPs and skip DNS.
    import socket as _s
    real = _s.getaddrinfo
    def fake(host, *a, **kw):
        if host == "example.com":
            return [(2, 1, 6, "", ("93.184.216.34", 0))]
        return real(host, *a, **kw)
    monkeypatch.setattr(_s, "getaddrinfo", fake)
    assert is_externally_fetchable_url(url) is True


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/",
    "http://[::1]/",
    "http://localhost/",                 # resolves to loopback
    "http://10.0.0.1/admin",             # RFC1918
    "http://172.16.0.1/",                # RFC1918
    "http://192.168.1.1/",               # RFC1918
    "http://169.254.169.254/latest/",    # AWS / GCP IMDS
    "http://[fe80::1]/",                 # IPv6 link-local
    "http://[fc00::1]/",                 # IPv6 ULA / private
    "http://0.0.0.0/",                   # unspecified
    "javascript:alert(1)",               # not fetchable scheme
    "file:///etc/passwd",                # not fetchable scheme
])
def test_externally_fetchable_blocks_internal(url):
    assert is_externally_fetchable_url(url) is False


def test_externally_fetchable_blocks_dns_rebinding(monkeypatch):
    """A hostname whose A record contains both a public AND a loopback IP
    must be refused — otherwise an attacker controlling DNS can let the
    first probe succeed and the retry land on 127.0.0.1."""
    import socket as _s
    monkeypatch.setattr(_s, "getaddrinfo", lambda *a, **kw: [
        (2, 1, 6, "", ("93.184.216.34", 0)),
        (2, 1, 6, "", ("127.0.0.1", 0)),
    ])
    assert is_externally_fetchable_url("http://evil.example/") is False


def test_externally_fetchable_fails_closed_on_dns_error(monkeypatch):
    import socket as _s
    def boom(*a, **kw): raise _s.gaierror("nodename nor servname provided")
    monkeypatch.setattr(_s, "getaddrinfo", boom)
    assert is_externally_fetchable_url("http://does-not-resolve.invalid/") is False


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
