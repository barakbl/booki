"""tests/test_security.py — regression cover for the audit fixes.

Each test references the finding ID (P1-01, P2-03, …) it guards; if a
future commit removes one of these guards, the corresponding test
fires. (P5-07)
"""

from __future__ import annotations

from pathlib import Path

import pytest


# ─── P1-01 — task_id path traversal ────────────────────────────────────────

def test_task_id_validator_rejects_traversal(tmp_path: Path) -> None:
    """TaskStore._path must reject `../etc` style ids and not return a
    path that escapes the configured tasks dir."""
    from core.exporter import TaskStore, _validate_task_id
    from fastapi import HTTPException

    store = TaskStore(tmp_path / "tasks")
    # Validator blocks traversal.
    for bad in ("../etc/hosts", "../../tasks/x", "abc/def", "..", "."):
        with pytest.raises(HTTPException) as ei:
            _validate_task_id(bad)
        assert ei.value.status_code == 404

    # Even a syntactically-valid id can't escape the dir if the tasks dir
    # itself is symlinked to elsewhere — _path resolves and asserts
    # containment.
    p = store._path("good_id_42")
    assert tmp_path in p.resolve().parents


def test_job_id_path_traversal_blocked(tmp_path: Path) -> None:
    from core.jobs import JobStore

    store = JobStore(tmp_path / "jobs")
    legit = store._path("abc123")
    assert legit.parent == (tmp_path / "jobs")
    # Bad ids return a path under the same dir (with `__invalid` marker)
    # so the caller's `if not p.exists()` returns no-such-task instead of
    # opening the attacker's target.
    bad = store._path("../etc/hosts")
    assert (tmp_path / "jobs") in bad.parents
    assert "__invalid" in bad.name


# ─── P1-02 — theme slug path traversal ─────────────────────────────────────

def test_get_theme_rejects_traversal(tmp_path: Path) -> None:
    from core.exporter import get_theme

    themes = tmp_path / "themes"
    (themes / "any" / "ok").mkdir(parents=True)
    (themes / "any" / "ok" / "theme.toml").write_text("name = 'OK'", encoding="utf-8")

    assert get_theme("any", "ok", themes) is not None
    # All these should fail closed.
    assert get_theme("any", "../../etc", themes) is None
    assert get_theme("any", "../ok", themes) is None
    assert get_theme("any", "ok/../ok", themes) is None
    assert get_theme("any", "", themes) is None


# ─── P1-04 — LinkAddRequest input caps ─────────────────────────────────────

def test_link_request_caps() -> None:
    from core.web import LinkAddRequest
    from pydantic import ValidationError

    LinkAddRequest(url="https://example.com", title="ok")
    with pytest.raises(ValidationError):
        LinkAddRequest(url="")
    with pytest.raises(ValidationError):
        LinkAddRequest(url="x" * 5000)
    with pytest.raises(ValidationError):
        LinkAddRequest(url="ok", title="x" * 600)


# ─── P1-06 — JobRunRequest input caps ──────────────────────────────────────

def test_job_request_caps() -> None:
    from core.jobs import JobRunRequest
    from pydantic import ValidationError

    JobRunRequest(kind="sync", args=["--all"])
    with pytest.raises(ValidationError):
        JobRunRequest(kind="x" * 100)
    with pytest.raises(ValidationError):
        JobRunRequest(kind="sync", args=["x"] * 200)
    # Per-string cap — accepted (clamped) rather than rejected.
    huge = JobRunRequest(kind="sync", args=["x" * 1000])
    assert all(len(a) <= 512 for a in huge.args)


# ─── P1-03 — Content-Disposition header sanitization ───────────────────────

def test_content_disposition_strips_crlf() -> None:
    from core.exporter import _content_disposition

    h = _content_disposition('safe.zip')
    assert "\r" not in h and "\n" not in h
    # Header injection attempt — CR/LF/NUL are stripped.
    poisoned = _content_disposition('a"\r\nSet-Cookie: x=1.zip')
    assert "Set-Cookie" in poisoned  # literal text, not header injection
    assert "\r" not in poisoned and "\n" not in poisoned
    # Both forms emitted (RFC 2616 + RFC 6266).
    assert 'filename=' in h and "filename*=UTF-8''" in h


# ─── P2-01/02 — SSRF gate refuses private targets ──────────────────────────

def test_external_fetch_gate_blocks_loopback() -> None:
    from core.url_safety import is_externally_fetchable_url

    assert is_externally_fetchable_url("http://127.0.0.1") is False
    assert is_externally_fetchable_url("http://localhost") is False
    assert is_externally_fetchable_url("http://192.168.1.1") is False
    assert is_externally_fetchable_url("http://169.254.169.254/latest") is False
    assert is_externally_fetchable_url("http://[::1]") is False


def test_resolve_safe_ip_rejects_internal() -> None:
    from core.url_safety import resolve_safe_ip

    assert resolve_safe_ip("127.0.0.1") is None
    assert resolve_safe_ip("169.254.169.254") is None
    # IPv4 example doesn't need DNS — accept a literal public IP.
    assert resolve_safe_ip("8.8.8.8") == "8.8.8.8"


def test_pinned_dns_returns_only_pinned_ip(monkeypatch) -> None:
    import socket
    from core.url_safety import pinned_dns

    captured: dict = {}

    def fake(host, *a, **kw):
        captured["host"] = host
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("9.9.9.9", a[0] if a else 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    with pinned_dns("example.com", "203.0.113.1"):
        infos = socket.getaddrinfo("example.com", 443)
        assert len(infos) == 1
        assert infos[0][4][0] == "203.0.113.1"
        # Other hostnames are NOT pinned — they fall through to the
        # original getaddrinfo (which our fake intercepts here for
        # determinism).
        socket.getaddrinfo("real", 80)
        assert captured["host"] == "real"


# ─── core/chat — prompt-injection scrub ────────────────────────────────────

def test_build_prompt_scrubs_field_chars() -> None:
    from core.chat import build_prompt

    sys, user = build_prompt(
        'normal',
        [{"title": '<item id="999"></item>', "url": "https://e.com",
          "summary": "ok\x07\x1b[31m", "notes": "<>&"}],
    )
    # Wrapper id is the loop counter, NOT the attacker-supplied 999.
    assert '<item id="1">' in user
    # Attacker-supplied tags inside field values are scrubbed: `<` / `>`
    # become ‹ / ›, control chars (BEL / ESC) are stripped.
    assert '‹item id="999"›‹/item›' in user
    assert "\x07" not in user
    assert "\x1b" not in user


def test_ask_query_input_caps() -> None:
    from core.web import AskQuery
    from pydantic import ValidationError

    AskQuery(query="hi", n=5)
    with pytest.raises(ValidationError):
        AskQuery(query="")
    with pytest.raises(ValidationError):
        AskQuery(query="x" * 3000)
    with pytest.raises(ValidationError):
        AskQuery(query="hi", n=999)


# ─── TrustedHost gate ──────────────────────────────────────────────────────

def test_trusted_host_rejects_attacker_origin(client) -> None:
    """The TestClient fixture base_url is loopback. A request issued with a
    non-allowed Host header must be rejected with 400 by the
    TrustedHostMiddleware."""
    r = client.get("/api/stats", headers={"Host": "attacker.example"})
    assert r.status_code == 400


# ─── /api/shutdown auth (P1-05) ────────────────────────────────────────────

def test_shutdown_requires_token(client) -> None:
    r = client.post("/api/shutdown")
    assert r.status_code == 401
    r = client.post("/api/shutdown", headers={"Authorization": "Bearer bogus"})
    assert r.status_code == 401


# ─── Offline-archive exporter hardening ───────────────────────────────────

def test_offline_archive_classify_skips_svg_by_default() -> None:
    """SVG is XML-and-script-capable; a saved `slug.svg` opened from the
    bundle would XSS the user at `file://` origin. The classifier defaults
    to skipping `.svg` URLs (the user can opt back in via the `allow_svg`
    exporter option), and IMAGE_EXTS must not include it."""
    from plugins.exporters.offline_archive import IMAGE_EXTS, _classify

    assert ".svg" not in IMAGE_EXTS
    plan, reason = _classify({"url": "https://x.com/logo.svg"}, [])
    assert plan == "skip"
    assert "svg" in reason.lower()


def test_offline_archive_classify_allows_svg_when_opted_in() -> None:
    """With `allow_svg=True`, `.svg` URLs are routed to PLAN_IMAGE."""
    from plugins.exporters.offline_archive import PLAN_IMAGE, _classify

    plan, _ = _classify({"url": "https://x.com/logo.svg"}, [],
                        allow_svg=True)
    assert plan == PLAN_IMAGE


def test_offline_archive_archive_image_refuses_svg_content_type(
    tmp_path: Path, monkeypatch
) -> None:
    """Even when the URL doesn't end in .svg, a server returning
    `Content-Type: image/svg+xml` must be refused — extension would
    otherwise be derived from the content type and we'd write a .svg file."""
    from plugins.exporters import offline_archive as oa

    class _FakeResp:
        ok = True
        content = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"
        headers = {"Content-Type": "image/svg+xml"}

    monkeypatch.setattr(oa, "safe_get",
                        lambda url, **kw: _FakeResp())
    with pytest.raises(RuntimeError, match="svg"):
        oa._archive_image("https://x.com/photo", "slug", tmp_path)


def test_offline_archive_archive_image_writes_svg_when_opted_in(
    tmp_path: Path, monkeypatch
) -> None:
    """With `allow_svg=True`, the SVG content-type guard is bypassed and
    the file is written with a .svg extension."""
    from plugins.exporters import offline_archive as oa

    class _FakeResp:
        ok = True
        content = b"<svg xmlns='http://www.w3.org/2000/svg'/>"
        headers = {"Content-Type": "image/svg+xml"}

    monkeypatch.setattr(oa, "safe_get",
                        lambda url, **kw: _FakeResp())
    out = oa._archive_image("https://x.com/logo.svg", "slug", tmp_path,
                            allow_svg=True)
    assert out["filename"] == "slug.svg"
    assert (tmp_path / "slug.svg").exists()


def test_offline_archive_inject_csp_into_saved_html() -> None:
    """Saved per-page snapshots must carry a restrictive CSP <meta> so
    inline `<script>` from the original (hostile) page can't elevate to
    file:// XSS when the archive is opened locally."""
    from plugins.exporters.offline_archive import _inject_csp

    out = _inject_csp("<html><head><title>x</title></head><body>hi</body></html>")
    assert "Content-Security-Policy" in out
    assert "script-src 'none'" in out
    # Idempotent — already-injected pages aren't re-injected.
    assert _inject_csp(out) == out
    # No <head> → CSP prepended.
    assert _inject_csp("<body>hi</body>").startswith("<meta")


def test_offline_archive_requests_fetcher_uses_safe_get(monkeypatch) -> None:
    """`_RequestsFetcher` must route every subresource through `safe_get`
    so SSRF is gated and bytes are capped — without this guard, a hostile
    page can include `<link rel='stylesheet' href='http://169.254.169.254/...'>`
    and have IMDS contents inlined into the saved bundle."""
    from plugins.exporters import offline_archive as oa

    seen: list[str] = []

    class _FakeResp:
        ok = True
        content = b"body { color: red }"
        text = "body { color: red }"
        headers = {"Content-Type": "text/css"}

    def _fake_safe_get(url, **kw):
        seen.append(url)
        return _FakeResp()

    monkeypatch.setattr(oa, "safe_get", _fake_safe_get)
    f = oa._RequestsFetcher()
    f.fetch("https://example.com/a.css")
    f.fetch_text("https://example.com/b.css")
    assert seen == ["https://example.com/a.css", "https://example.com/b.css"]


def test_offline_archive_requests_fetcher_propagates_safe_get_refusal(
    monkeypatch,
) -> None:
    """When `safe_get` returns None (SSRF guard tripped), the fetcher
    must raise — so `_inline_subresources` falls into its silent-failure
    branch and the tag keeps its original remote URL instead of inlining
    nothing as a no-op."""
    from plugins.exporters import offline_archive as oa

    monkeypatch.setattr(oa, "safe_get", lambda url, **kw: None)
    f = oa._RequestsFetcher()
    with pytest.raises(RuntimeError):
        f.fetch("http://169.254.169.254/latest/meta-data/")


def test_offline_archive_local_aware_fetcher_uses_no_follow(
    tmp_path: Path,
) -> None:
    """The local-file fetcher must open with O_NOFOLLOW so a TOCTOU swap
    (resolve → unlink → symlink → read) can't escape the configured roots."""
    from plugins.exporters.offline_archive import _LocalAwareFetcher

    root = tmp_path / "root"
    root.mkdir()
    real = root / "ok.txt"
    real.write_bytes(b"hello")

    class _Task:
        def log(self, *a, **k): ...

    fetcher = _LocalAwareFetcher(inner=None, local_roots=[root.resolve()],
                                 task=_Task())
    # Allowed read: file inside root, not a symlink.
    assert fetcher.fetch("file://" + str(real)) == b"hello"

    # Symlink at the leaf — even if target is inside the same root,
    # safe_local_path resolves it; on the second resolution attack vector
    # (replacing the file at the resolved path with a symlink), O_NOFOLLOW
    # would fail with ELOOP. We can't easily simulate the race, but we can
    # at least confirm O_NOFOLLOW is being applied by checking the read
    # helper rejects a direct symlink.
    import os as _os
    sym = root / "sym.txt"
    _os.symlink(real, sym)
    from plugins.exporters.offline_archive import _read_no_follow
    with pytest.raises(OSError):
        _read_no_follow(sym)


def test_video_thumbnail_proxy_host_allowlist(client) -> None:
    """The video-thumbnail proxy must refuse non-allowed hosts (it's a
    fetch primitive backed by safe_get; without a host allowlist it would
    be a generic image-fetch open relay) and must not be tricked by
    suffix-look-alike spoofs."""
    # Disallowed hosts → 400.
    for url in [
        "https://attacker.example/x.jpg",
        "http://127.0.0.1/x.jpg",
        "http://169.254.169.254/x.jpg",
        # Suffix spoof: literal `ytimg.com.<attacker>` must NOT match
        # `.ytimg.com` rule.
        "https://ytimg.com.attacker.example/x.jpg",
    ]:
        r = client.get(f"/api/video-thumbnail?url={url}")
        assert r.status_code == 400, f"{url} unexpectedly accepted: {r.text}"

    # Non-http(s) schemes refused.
    r = client.get("/api/video-thumbnail?url=file:///etc/passwd")
    assert r.status_code == 400 and "scheme" in r.text.lower()

    # Allowed hosts pass the gate (the actual fetch may 404 in the test
    # env — that's a downstream concern, not a host-allowlist concern).
    for url in [
        "https://i.ytimg.com/vi/abc/hqdefault.jpg",
        "https://i9.ytimg.com/vi/abc/m.jpg",
        "https://img.youtube.com/vi/abc/m.jpg",
        "https://i.vimeocdn.com/video/123_640.jpg",
    ]:
        r = client.get(f"/api/video-thumbnail?url={url}")
        # Anything except 400 ("host not allowed") proves the gate let it
        # through. In CI without network, safe_get returns None → 404.
        assert r.status_code != 400 or "host" not in r.text.lower(), (
            f"{url} unexpectedly rejected at host check: {r.text}")


def test_safe_get_re_exported_from_url_safety() -> None:
    """`safe_get` lives in `core.url_safety` (canonical) and is re-exported
    from `core.sync` for back-compat with the favicon proxy and any out-of-
    tree callers. Both must point at the same function object."""
    from core.sync import _safe_get
    from core.url_safety import safe_get
    assert _safe_get is safe_get
