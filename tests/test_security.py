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
