"""
Tests for the `booki bootstrap` config wizard's pure helpers.

The interactive prompts aren't covered here — they're driven by stdin
and are best exercised by hand. What we test is the deterministic part:

  * `_render_toml` produces a parseable TOML
  * `[manager.sync]` is emitted (and only emitted) when the user opts in
  * `_write_manager_settings` writes / merges the JSON the Rust manager
    later reads, without clobbering unrelated keys
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import tomllib

from core.bootstrap import (
    BootstrapAnswers,
    _render_manager_section,
    _render_toml,
    _write_manager_settings,
)


# ─── _render_toml + manager block ────────────────────────────────────────

def test_render_toml_is_parseable() -> None:
    """Smoke test — whatever bootstrap writes must round-trip through
    Python's tomllib without hand-editing."""
    ans = BootstrapAnswers()
    ans.sources_enabled = {"chrome": True, "rss": False}
    text = _render_toml(ans)
    parsed = tomllib.loads(text)
    assert "bookmarks" in parsed
    assert "vector_db" in parsed
    assert "embeddings" in parsed


def test_manager_section_emitted_only_when_opted_in() -> None:
    on  = BootstrapAnswers(); on.manager_setup = True
    off = BootstrapAnswers(); off.manager_setup = False
    assert _render_manager_section(on)
    assert _render_manager_section(off) == ""


def test_manager_block_reflects_enrich_choices() -> None:
    ans = BootstrapAnswers()
    ans.manager_setup = True
    ans.manager_enrich = True
    ans.manager_enrich_meta = False

    text = _render_toml(ans)
    parsed = tomllib.loads(text)
    sync = parsed["manager"]["sync"]
    assert sync["enrich"] is True
    # The serde alias on the Rust side accepts both kebab-case (CLI flag
    # spelling) and snake-case — bootstrap emits kebab-case since it's
    # a 1:1 echo of the underlying CLI flag.
    assert sync["enrich-meta"] is False


def test_manager_block_default_flags_are_both_true() -> None:
    ans = BootstrapAnswers()
    ans.manager_setup = True
    parsed = tomllib.loads(_render_toml(ans))
    assert parsed["manager"]["sync"] == {"enrich": True, "enrich-meta": True}


# ─── _write_manager_settings ────────────────────────────────────────────

def test_write_manager_settings_creates_file(tmp_path: Path, monkeypatch) -> None:
    """The function writes to MANAGER_SETTINGS, but tests must not touch
    the user's real ~/.config — point it at the tmp_path instead."""
    target = tmp_path / "booki-manager" / "settings.json"
    monkeypatch.setattr("core.bootstrap.MANAGER_SETTINGS", target)

    ans = BootstrapAnswers()
    ans.manager_setup = True
    ans.manager_booki_home = str(tmp_path / "fake-booki")

    written = _write_manager_settings(ans)
    assert written == target
    assert target.is_file()

    payload = json.loads(target.read_text(encoding="utf-8"))
    # Resolve so the test matches the function's resolve() call.
    assert payload["booki_home"] == str((tmp_path / "fake-booki").resolve())


def test_write_manager_settings_returns_none_when_skipped(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "booki-manager" / "settings.json"
    monkeypatch.setattr("core.bootstrap.MANAGER_SETTINGS", target)
    ans = BootstrapAnswers()
    ans.manager_setup = False
    assert _write_manager_settings(ans) is None
    assert not target.exists(), "no file should be written when user skips"


def test_write_manager_settings_preserves_unrelated_keys(tmp_path: Path, monkeypatch) -> None:
    """Future-proofing — the manager may grow other settings keys; we
    must not clobber them just because we know about `booki_home` here."""
    target = tmp_path / "booki-manager" / "settings.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({
        "booki_home": "/old/path",
        "future_key": "should survive",
    }) + "\n", encoding="utf-8")
    monkeypatch.setattr("core.bootstrap.MANAGER_SETTINGS", target)

    ans = BootstrapAnswers()
    ans.manager_setup = True
    ans.manager_booki_home = str(tmp_path / "new-booki")
    _write_manager_settings(ans)

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["booki_home"] == str((tmp_path / "new-booki").resolve())
    assert payload["future_key"] == "should survive"
