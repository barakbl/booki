"""
CLI dispatcher tests for `booki`.

The dispatcher is a thin shim that:
  1. Pulls global flags (`-v`, `--log-level`) off `argv`.
  2. Routes the first remaining token to the matching `core.<sub>` module.

These tests exercise it as a real subprocess so we catch regressions in
shebang / sys.path / import order — the kind of breakage unit-importing
the file would mask.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BOOKI = ROOT / "booki"


def _run(*args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    # Use the same interpreter pytest is running under so we hit the same
    # virtualenv, regardless of where the user invoked `pytest` from.
    return subprocess.run(
        [sys.executable, str(BOOKI), *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def test_help_prints_usage_and_exits_zero() -> None:
    """`booki --help` is the first thing every new user runs. It must
    succeed and list the subcommands."""
    r = _run("--help")
    assert r.returncode == 0
    assert "subcommands:" in r.stdout
    # All advertised subcommands appear in --help.
    for sub in ("sync", "ingest", "chat", "web", "browse", "download"):
        assert sub in r.stdout


def test_no_args_prints_usage_and_exits_nonzero() -> None:
    """Running bare `booki` is a usage error — should exit 2 (POSIX
    convention for argparse-style misuse)."""
    r = _run()
    assert r.returncode == 2
    assert "usage:" in (r.stdout + r.stderr)


def test_unknown_subcommand_exits_two_with_message() -> None:
    r = _run("not-a-real-subcommand")
    assert r.returncode == 2
    assert "unknown subcommand" in r.stderr


def test_invalid_log_level_is_rejected_early() -> None:
    """Bad --log-level should fail before any subcommand work."""
    r = _run("--log-level", "VERBOSE", "doctor")
    assert r.returncode == 2
    assert "invalid --log-level" in r.stderr


def test_subcommand_help_dispatches_to_module() -> None:
    """`booki sync --help` proves the dispatcher actually loaded the
    submodule and handed off argv. We check argparse's stable header
    rather than fragile flag wording."""
    r = _run("sync", "--help")
    assert r.returncode == 0
    assert "usage:" in r.stdout
