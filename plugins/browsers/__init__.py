"""
Browser sources — Chrome, Safari, Firefox bookmarks as Source plugins.

Each browser is exposed as its own Source (name = "chrome" | "safari" |
"firefox"). Items are yielded with kind="bookmark" and a `path` that mirrors
the browser's folder hierarchy, so the on-disk layout stays identical to
what sync.py produced before the plugin refactor:

    bookmarks/chrome/bookmarks_bar/ai/cursor--a1b2c3d4.md
"""

from __future__ import annotations

import json
import plistlib
import shutil
import sqlite3
import sys
import tempfile
from abc import abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional

from ..base import Item, Source, register


# ─── Shared browser source base ───────────────────────────────────────────────

class BrowserSource(Source):
    """
    Common helpers for browser-based sources.

    Subclasses walk the browser's native storage and yield `Item`s directly
    via `fetch()`. The `path` on each Item is the folder hierarchy as a list
    of human-readable segments; the store slugifies segments into directories.
    """

    display_name: str = ""       # what shows up as `source:` in MD frontmatter

    @abstractmethod
    def is_available(self) -> bool: ...

    @abstractmethod
    def fetch(self) -> Iterable[Item]: ...

    def _item(self, title: str, url: str, path: list[str],
              date_added: Optional[str] = None) -> Item:
        return Item(
            title=(title or url).strip(),
            url=url,
            source=self.name,
            kind="bookmark",
            path=path[:],
            date_added=date_added,
            extras={
                # Kept so the rendered frontmatter matches what pre-plugin
                # sync.py emitted — no churn on existing files.
                "browser_path":  " › ".join(path),
                "browser_label": self.display_name or self.name.title(),
            },
        )


# ─── Chrome ───────────────────────────────────────────────────────────────────

@register
class ChromeSource(BrowserSource):
    name = "chrome"
    display_name = "Chrome"

    ROOT_LABELS = {
        "bookmark_bar": "Bookmarks Bar",
        "other":        "Other Bookmarks",
        "synced":       "Mobile Bookmarks",
    }
    _EPOCH_DELTA = 11_644_473_600   # Chrome: µs since 1601-01-01

    def __init__(self):
        super().__init__()
        self._profile_paths: list[Path] = []

    def is_available(self) -> bool:
        base = Path.home() / "Library/Application Support/Google/Chrome"
        self._profile_paths = sorted(base.glob("*/Bookmarks")) if base.exists() else []
        return bool(self._profile_paths)

    def availability_hint(self) -> str:
        return "Chrome not installed, or no profiles with bookmarks found."

    def fetch(self) -> Iterator[Item]:
        if not self._profile_paths:
            self.is_available()

        multi_profile = len(self._profile_paths) > 1
        for profile_path in self._profile_paths:
            profile_name = profile_path.parent.name
            try:
                with open(profile_path, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                print(f"  [chrome/{profile_name}] Read error: {e}", file=sys.stderr)
                continue

            label = f"Chrome ({profile_name})" if multi_profile else "Chrome"
            roots = data.get("roots", {})
            for key, root_label in self.ROOT_LABELS.items():
                if key in roots:
                    yield from self._walk(roots[key], [label, root_label])

    def _walk(self, node: dict, path: list[str]) -> Iterator[Item]:
        for child in node.get("children", []):
            ctype = child.get("type")
            if ctype == "url":
                url = child.get("url", "")
                if url and not url.startswith(("javascript:", "data:", "place:")):
                    yield self._item(
                        title=child.get("name", ""),
                        url=url,
                        path=path,
                        date_added=self._chrome_ts(child.get("date_added")),
                    )
            elif ctype == "folder":
                yield from self._walk(child, path + [child.get("name", "Unnamed")])

    @classmethod
    def _chrome_ts(cls, raw) -> Optional[str]:
        if not raw:
            return None
        try:
            secs = int(raw) / 1_000_000 - cls._EPOCH_DELTA
            return datetime.fromtimestamp(secs, tz=timezone.utc).date().isoformat()
        except (ValueError, OSError):
            return None


# ─── Safari ───────────────────────────────────────────────────────────────────

@register
class SafariSource(BrowserSource):
    name = "safari"
    display_name = "Safari"

    PATH = Path.home() / "Library/Safari/Bookmarks.plist"

    def is_available(self) -> bool:
        return self.PATH.exists()

    def availability_hint(self) -> str:
        return ("Safari bookmarks not found, or Terminal lacks Full Disk Access. "
                "Grant it in System Settings → Privacy & Security → Full Disk Access.")

    def fetch(self) -> Iterator[Item]:
        try:
            with open(self.PATH, "rb") as f:
                data = plistlib.load(f)
        except PermissionError:
            print(
                "  [safari] Permission denied reading Bookmarks.plist.\n"
                "  Grant Full Disk Access to Terminal in:\n"
                "  System Settings → Privacy & Security → Full Disk Access",
                file=sys.stderr,
            )
            return
        except Exception as e:
            print(f"  [safari] Error: {e}", file=sys.stderr)
            return

        for child in data.get("Children", []):
            if child.get("WebBookmarkType") == "WebBookmarkTypeList":
                title = child.get("Title", "Bookmarks")
                yield from self._walk(child, ["Safari", title])

    def _walk(self, node: dict, path: list[str]) -> Iterator[Item]:
        for child in node.get("Children", []):
            btype = child.get("WebBookmarkType", "")
            if btype == "WebBookmarkTypeLeaf":
                url = child.get("URLString", "")
                if url and not url.startswith(("javascript:", "data:")):
                    title = (child.get("URIDictionary", {}).get("title")
                             or child.get("Title", ""))
                    yield self._item(title, url, path)
            elif btype == "WebBookmarkTypeList":
                yield from self._walk(child, path + [child.get("Title", "Unnamed")])


# ─── Firefox ──────────────────────────────────────────────────────────────────

@register
class FirefoxSource(BrowserSource):
    name = "firefox"
    display_name = "Firefox"

    PROFILES_DIR = Path.home() / "Library/Application Support/Firefox/Profiles"
    ROOT_IDS = {2: "Bookmarks Menu", 3: "Bookmarks Toolbar", 5: "Other Bookmarks"}

    def is_available(self) -> bool:
        return self.PROFILES_DIR.exists() and bool(
            list(self.PROFILES_DIR.glob("*/places.sqlite"))
        )

    def availability_hint(self) -> str:
        return "Firefox not installed, or no profiles with bookmarks found."

    def fetch(self) -> Iterator[Item]:
        dbs = list(self.PROFILES_DIR.glob("*/places.sqlite"))
        if not dbs:
            return
        db = max(dbs, key=lambda p: p.stat().st_mtime)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_db = Path(tmpdir) / "places.sqlite"
            shutil.copy2(db, tmp_db)
            for suffix in ("-wal", "-shm"):
                src = db.parent / (db.name + suffix)
                if src.exists():
                    shutil.copy2(src, tmp_db.parent / (tmp_db.name + suffix))
            try:
                yield from self._read_db(tmp_db)
            except Exception as e:
                print(f"  [firefox] Error reading bookmarks: {e}", file=sys.stderr)

    def _read_db(self, db_path: Path) -> Iterator[Item]:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("""
            SELECT b.id, b.parent, b.title, b.type, b.dateAdded, p.url
            FROM moz_bookmarks b
            LEFT JOIN moz_places p ON b.fk = p.id
            ORDER BY b.parent, b.position
        """)
        rows = {r["id"]: dict(r) for r in cur.fetchall()}
        conn.close()

        children_map: dict[int, list[int]] = {}
        for id_, row in rows.items():
            children_map.setdefault(row["parent"], []).append(id_)

        for root_id, root_name in self.ROOT_IDS.items():
            if root_id in rows:
                yield from self._walk(root_id, ["Firefox", root_name], rows, children_map)

    def _walk(self, node_id: int, path: list[str],
              rows: dict, children_map: dict) -> Iterator[Item]:
        for child_id in children_map.get(node_id, []):
            row = rows.get(child_id)
            if not row:
                continue
            if row["type"] == 1:    # bookmark
                url = row.get("url") or ""
                if url and not url.startswith(("javascript:", "data:", "place:")):
                    yield self._item(
                        title=row.get("title") or "",
                        url=url,
                        path=path,
                        date_added=self._ff_ts(row.get("dateAdded")),
                    )
            elif row["type"] == 2:  # folder
                name = row.get("title") or "Unnamed"
                yield from self._walk(child_id, path + [name], rows, children_map)

    @staticmethod
    def _ff_ts(raw) -> Optional[str]:
        if not raw:
            return None
        try:
            return datetime.fromtimestamp(raw / 1_000_000, tz=timezone.utc).date().isoformat()
        except (ValueError, OSError):
            return None
