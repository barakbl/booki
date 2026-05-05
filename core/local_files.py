"""
core.local_files — shared guardrail for any code path that copies, embeds,
or otherwise reads a local file referenced from a bookmark.

Rule: the only local files we trust are those under one of the directories
declared in `[[sources.directory.dirs]]`. Anything else (`/root/*`,
`~/Downloads/*`, `/etc/*`) is refused — including symlinks that *resolve*
outside those roots.

Callers branch on the return value rather than catching exceptions: this
is a containment check, not an error.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import unquote, urlparse


def directory_roots(cfg: dict) -> list[Path]:
    """Resolved real paths from `[[sources.directory.dirs]]` in config.

    Each path is expanded (`~`) and resolved (symlinks followed) so that
    `safe_local_path` can compare apples to apples. Entries that don't
    resolve (missing dir, permission error) are dropped silently — the
    user can't read what isn't there anyway.
    """
    raw = ((cfg.get("sources", {}) or {})
           .get("directory", {}) or {}).get("dirs", []) or []
    out: list[Path] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        p = entry.get("path") or ""
        if not p:
            continue
        try:
            resolved = Path(os.path.expanduser(str(p))).resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            continue
        if resolved.is_dir():
            out.append(resolved)
    return out


def _path_from_value(raw: str) -> Path | None:
    """Accept either a plain path or a `file://` URL; return a Path or None.

    `file://localhost/x` and `file:///x` both yield `/x`. Anything else
    (`http://`, `https://`, empty, weird scheme) returns None — those are
    not local files and aren't this module's concern.
    """
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if s.startswith("file://"):
        parsed = urlparse(s)
        # Reject `file://host/...` where host is something other than
        # localhost — that's an SMB/NFS-style reference, not a local file.
        host = (parsed.netloc or "").lower()
        if host and host != "localhost":
            return None
        path = unquote(parsed.path or "")
        if not path:
            return None
        return Path(path)
    if s.startswith(("http://", "https://", "data:")):
        return None
    return Path(os.path.expanduser(s))


def safe_local_path(raw: str, roots: list[Path]) -> Path | None:
    """Return the resolved real path if it's a regular file under one of
    `roots`; otherwise None.

    `roots` should already be resolved (`directory_roots` does this).
    Symlinks are followed before the containment check, so a symlink at
    `/Users/me/notes/escape -> /etc/passwd` is rejected.

    A None return means "do not read this file." Callers should record a
    skip with a user-facing reason rather than retry.
    """
    if not roots:
        return None
    p = _path_from_value(raw)
    if p is None:
        return None
    try:
        resolved = p.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    if not resolved.is_file():
        return None
    for root in roots:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    return None
