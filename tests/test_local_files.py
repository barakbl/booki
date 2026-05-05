"""Tests for the local-file containment guard.

Why these matter: every export path that touches a local file
(`shutil.copy2`, base64-embed, HTTP file:// streaming) routes through
`safe_local_path`. If the containment check regresses, an export could
silently read `/root/whatever` from a frontmatter `image_path:` value.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.local_files import directory_roots, safe_local_path


@pytest.fixture
def roots(tmp_path: Path) -> tuple[list[Path], Path, Path]:
    """One configured root with a sample file inside it, plus a sibling
    directory representing 'outside the allow-list'."""
    inside = tmp_path / "notes"
    inside.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (inside / "ok.jpg").write_bytes(b"x")
    (outside / "leak.jpg").write_bytes(b"x")
    return [inside.resolve()], inside, outside


def test_directory_roots_resolves_and_skips_missing(tmp_path: Path):
    real = tmp_path / "real"
    real.mkdir()
    cfg = {"sources": {"directory": {"dirs": [
        {"path": str(real)},
        {"path": str(tmp_path / "missing")},     # dropped — doesn't exist
        {"path": ""},                              # dropped — empty
        "not a dict",                              # dropped — wrong shape
    ]}}}
    out = directory_roots(cfg)
    assert out == [real.resolve()]


def test_safe_local_path_accepts_file_inside_root(roots):
    rs, inside, _ = roots
    p = safe_local_path(str(inside / "ok.jpg"), rs)
    assert p == (inside / "ok.jpg").resolve()


def test_safe_local_path_rejects_file_outside_root(roots):
    rs, _, outside = roots
    assert safe_local_path(str(outside / "leak.jpg"), rs) is None


def test_safe_local_path_rejects_symlink_escape(roots, tmp_path: Path):
    """Symlink inside the root pointing outside it must still be rejected
    — that's the whole point of resolving before the containment check."""
    rs, inside, outside = roots
    target = outside / "leak.jpg"
    link = inside / "trojan.jpg"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this filesystem")
    assert safe_local_path(str(link), rs) is None


def test_safe_local_path_handles_file_url(roots):
    rs, inside, _ = roots
    url = (inside / "ok.jpg").resolve().as_uri()
    p = safe_local_path(url, rs)
    assert p == (inside / "ok.jpg").resolve()


def test_safe_local_path_rejects_remote_urls(roots):
    rs, _, _ = roots
    assert safe_local_path("https://example.com/x.jpg", rs) is None
    assert safe_local_path("http://example.com/x.jpg", rs) is None
    assert safe_local_path("data:image/png;base64,xx", rs) is None


def test_safe_local_path_rejects_file_url_with_remote_host(roots):
    """`file://server/share/x` is an SMB/NFS reference, not a local file."""
    rs, _, _ = roots
    assert safe_local_path("file://fileserver/share/x.jpg", rs) is None


def test_safe_local_path_rejects_directory(roots):
    rs, inside, _ = roots
    assert safe_local_path(str(inside), rs) is None


def test_safe_local_path_empty_roots_rejects_everything(roots):
    _, inside, _ = roots
    assert safe_local_path(str(inside / "ok.jpg"), []) is None


# ─── offline_archive integration: classifier honors roots ────────────────

def test_offline_archive_classify_skips_image_path_outside_roots(roots):
    from plugins.exporters.offline_archive import _classify

    rs, _, outside = roots
    item = {"url": "https://example.com/p", "kind": "photo",
            "image_path": str(outside / "leak.jpg")}
    plan, reason = _classify(item, rs)
    assert plan == "skip"
    assert "outside configured directories" in reason
    assert "leak.jpg" in reason


def test_offline_archive_classify_accepts_image_path_inside_roots(roots):
    from plugins.exporters.offline_archive import _classify, PLAN_LOCAL_PHOTO

    rs, inside, _ = roots
    item = {"url": "https://example.com/p", "kind": "photo",
            "image_path": str(inside / "ok.jpg")}
    plan, _ = _classify(item, rs)
    assert plan == PLAN_LOCAL_PHOTO


def test_offline_archive_classify_skips_file_url_outside_roots(roots):
    from plugins.exporters.offline_archive import _classify

    rs, _, outside = roots
    item = {"url": (outside / "leak.jpg").resolve().as_uri(), "kind": "photo"}
    plan, reason = _classify(item, rs)
    assert plan == "skip"
    assert "outside configured directories" in reason


def test_offline_archive_classify_no_roots_means_no_local_files(roots):
    """Belt-and-suspenders: with `local_roots=[]`, even a real file under
    what *would* be a root gets refused. Matches the fail-safe default
    when `[[sources.directory.dirs]]` isn't configured at all."""
    from plugins.exporters.offline_archive import _classify

    _, inside, _ = roots
    item = {"url": "https://example.com/p", "kind": "photo",
            "image_path": str(inside / "ok.jpg")}
    plan, reason = _classify(item, [])
    assert plan == "skip"
    assert "outside configured directories" in reason


# ─── photo gallery integration ───────────────────────────────────────────

def test_photo_resolve_image_skips_outside_roots(roots):
    from plugins.exporters.photo import _resolve_image

    rs, _, outside = roots
    src, err = _resolve_image(
        {"url": (outside / "leak.jpg").resolve().as_uri()}, rs)
    assert src is None
    assert err == "outside configured directories"


def test_photo_resolve_image_embeds_inside_roots(roots):
    from plugins.exporters.photo import _resolve_image

    rs, inside, _ = roots
    src, err = _resolve_image(
        {"url": (inside / "ok.jpg").resolve().as_uri()}, rs)
    assert err is None
    assert src and src.startswith("data:")


def test_photo_resolve_image_passes_through_remote_urls(roots):
    from plugins.exporters.photo import _resolve_image

    rs, _, _ = roots
    src, err = _resolve_image({"url": "https://example.com/x.jpg"}, rs)
    assert err is None
    assert src == "https://example.com/x.jpg"
