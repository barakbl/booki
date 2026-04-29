"""
plugins/directory — local filesystem folders as a source.

Every matching file under a configured directory becomes one Item(kind="file").
Identity is `file://<abs-path>`, so moving a file changes its identity (it
will appear as a new item and the old one will be detached on next sync).

Configure in config.toml:

    [sources.directory]

    [[sources.directory.dirs]]
    path      = "/Users/me/notes"
    patterns  = ["*.md", "*.txt"]      # glob patterns (no path separators)
    recursive = true
    title     = "My notes"              # optional — display label for the set

    [[sources.directory.dirs]]
    path      = "~/papers"
    patterns  = ["*.pdf"]
    recursive = false

Per-directory last-fetch dates are tracked in
`<bookmarks>/.state/directory.json` so the plugin can report what changed
since the previous run. Deletion detection uses the sync engine's standard
orphan pass (every currently-present file is yielded each run).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from ..base import Item, Source, register


DEFAULT_PATTERNS = ["*"]


# ─── Config types ─────────────────────────────────────────────────────────────

@dataclass
class DirSpec:
    path: Path
    patterns: list[str]
    recursive: bool
    title: str            # "" means "derive from basename"
    hidden_files: bool    # include dotfiles / dot-directories

    @property
    def slug_label(self) -> str:
        return self.title or self.path.name or "root"


def _parse_specs(raw: list) -> list[DirSpec]:
    out: list[DirSpec] = []
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        p = entry.get("path")
        if not p:
            continue
        path = Path(os.path.expanduser(str(p))).resolve()
        patterns = entry.get("patterns") or DEFAULT_PATTERNS
        if isinstance(patterns, str):
            patterns = [patterns]
        out.append(DirSpec(
            path=path,
            patterns=[str(x) for x in patterns],
            recursive=bool(entry.get("recursive", False)),
            title=str(entry.get("title", "") or ""),
            hidden_files=bool(entry.get("hidden_files", False)),
        ))
    return out


# ─── State ────────────────────────────────────────────────────────────────────

def _state_path(output_dir: Path) -> Path:
    return output_dir / ".state" / "directory.json"


def _load_state(output_dir: Path) -> dict:
    p = _state_path(output_dir)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(output_dir: Path, state: dict) -> None:
    p = _state_path(output_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _iter_files(spec: DirSpec) -> Iterator[Path]:
    if not spec.path.exists():
        return
    seen: set[Path] = set()
    for pattern in spec.patterns:
        it = spec.path.rglob(pattern) if spec.recursive else spec.path.glob(pattern)
        for f in it:
            if not f.is_file() or f in seen:
                continue
            if not spec.hidden_files:
                # Skip dotfiles and any file inside a dot-directory (relative
                # to the configured root — `.` at the root itself is fine).
                try:
                    rel = f.relative_to(spec.path)
                except ValueError:
                    rel = Path(f.name)
                if any(part.startswith(".") for part in rel.parts):
                    continue
            seen.add(f)
            yield f


def _mtime_iso(p: Path) -> str:
    try:
        ts = p.stat().st_mtime
        return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
    except Exception:
        return ""


def _today_iso() -> str:
    return datetime.now(tz=timezone.utc).date().isoformat()


# ─── Source ───────────────────────────────────────────────────────────────────

@register
class DirectorySource(Source):
    name = "directory"

    @classmethod
    def kind_specs(cls) -> list[dict]:
        return [{"slug": "file", "glyph": "📁", "label": "File"}]

    @classmethod
    def field_specs(cls) -> list[dict]:
        return [
            {"name": "abs_path",        "label": "Path",           "group": "File", "format": "text"},
            {"name": "directory_root",  "label": "Root directory", "group": "File", "format": "text"},
            {"name": "directory_title", "label": "Set",            "group": "File", "format": "text"},
            {"name": "file_ext",        "label": "Extension",      "group": "File", "format": "text"},
            {"name": "file_mtime",      "label": "Modified",       "group": "File", "format": "date"},
            {"name": "file_size",       "label": "Size (bytes)",   "group": "File", "format": "number"},
            {"name": "changed_since_last_fetch", "label": "Changed", "group": "File", "format": "bool"},
            # Populated by the enricher when [enrichment.exiftool].enabled=true.
            {"name": "camera",          "label": "Camera",         "group": "Image", "format": "text", "kinds": ["file"]},
            {"name": "lens",            "label": "Lens",           "group": "Image", "format": "text", "kinds": ["file"]},
            {"name": "focal_length_mm", "label": "Focal length (mm)", "group": "Image", "format": "number", "kinds": ["file"]},
            {"name": "f_number",        "label": "f-number",       "group": "Image", "format": "number", "kinds": ["file"]},
            {"name": "shutter_speed",   "label": "Shutter",        "group": "Image", "format": "text", "kinds": ["file"]},
            {"name": "iso",             "label": "ISO",            "group": "Image", "format": "number", "kinds": ["file"]},
            {"name": "flash",           "label": "Flash",          "group": "Image", "format": "text", "kinds": ["file"]},
            {"name": "white_balance",   "label": "White balance",  "group": "Image", "format": "text", "kinds": ["file"]},
            {"name": "orientation",     "label": "Orientation",    "group": "Image", "format": "text", "kinds": ["file"]},
            {"name": "taken_at",        "label": "Taken",          "group": "Image", "format": "date", "kinds": ["file"]},
            {"name": "image_width",     "label": "Width",          "group": "Image", "format": "number", "kinds": ["file"]},
            {"name": "image_height",    "label": "Height",         "group": "Image", "format": "number", "kinds": ["file"]},
            {"name": "gps_lat",         "label": "GPS lat",        "group": "Image", "format": "number", "kinds": ["file"]},
            {"name": "gps_lon",         "label": "GPS lon",        "group": "Image", "format": "number", "kinds": ["file"]},
            {"name": "gps_alt",         "label": "GPS alt (m)",    "group": "Image", "format": "number", "kinds": ["file"]},
            {"name": "image_caption",   "label": "Caption",        "group": "Image", "format": "text", "kinds": ["file"]},
            {"name": "image_creator",   "label": "Creator",        "group": "Image", "format": "text", "kinds": ["file"]},
            {"name": "image_rating",    "label": "Rating",         "group": "Image", "format": "number", "kinds": ["file"]},
            {"name": "image_keywords",  "label": "Image keywords", "group": "Image", "format": "tags", "kinds": ["file"]},
            {"name": "mime_type",       "label": "MIME type",      "group": "Image", "format": "text", "kinds": ["file"]},
        ]

    def __init__(self):
        super().__init__()
        self._specs: list[DirSpec] = []
        # Where to persist last-fetch state. Sync engine sets this via
        # configure() indirectly — we derive it from the ItemStore's dir
        # on first use, but for cleanliness the config also accepts an
        # explicit `state_dir`. Default falls back to CWD/.booki-state.
        self._state_dir: Path | None = None

    def configure(self, cfg: dict) -> None:
        super().configure(cfg)
        self._specs = _parse_specs(cfg.get("dirs") or [])
        sd = cfg.get("state_dir")
        self._state_dir = Path(os.path.expanduser(sd)).resolve() if sd else None

    def is_available(self) -> bool:
        return any(s.path.exists() and s.path.is_dir() for s in self._specs)

    def availability_hint(self) -> str:
        if not self._specs:
            return "no dirs configured — add [[sources.directory.dirs]] blocks in config.toml"
        missing = [str(s.path) for s in self._specs if not s.path.exists()]
        if missing:
            return f"paths missing: {', '.join(missing)}"
        return ""

    # ── fetch ───────────────────────────────────────────────────────────────

    def fetch(self) -> Iterable[Item]:
        state_dir = self._resolve_state_dir()
        state = _load_state(state_dir)
        today = _today_iso()

        for spec in self._specs:
            if not spec.path.exists():
                print(f"  [directory] skip — missing: {spec.path}")
                continue

            key = str(spec.path)
            last_fetch = state.get(key, {}).get("last_fetch", "")
            changed = 0
            total = 0

            for file_path in _iter_files(spec):
                total += 1
                mtime_iso = _mtime_iso(file_path)
                is_changed = (not last_fetch) or (mtime_iso >= last_fetch)
                if is_changed:
                    changed += 1

                try:
                    rel = file_path.relative_to(spec.path)
                except ValueError:
                    rel = Path(file_path.name)

                # path[] = ["directory", <set-slug>, ...subdirs]
                sub_parts = list(rel.parts[:-1]) if spec.recursive else []
                item_path = ["directory", spec.slug_label] + sub_parts

                try:
                    size = file_path.stat().st_size
                except Exception:
                    size = 0

                extras = {
                    "abs_path":              str(file_path),
                    "directory_root":        str(spec.path),
                    "directory_title":       spec.title or spec.path.name,
                    "file_ext":              file_path.suffix.lstrip("."),
                    "file_mtime":            mtime_iso,
                    "file_size":             int(size),
                    "changed_since_last_fetch": bool(is_changed),
                    "last_fetch_before_run": last_fetch or "",
                }

                yield Item(
                    title=file_path.stem or file_path.name,
                    url=file_path.as_uri(),
                    source=self.name,
                    kind="file",
                    path=item_path,
                    date_added=mtime_iso or None,
                    extras=extras,
                )

            state[key] = {"last_fetch": today, "title": spec.title}
            print(f"  [directory] {spec.slug_label}: {total} file(s), {changed} changed since {last_fetch or 'never'}")

        _save_state(state_dir, state)

    # ── state location ──────────────────────────────────────────────────────

    def _resolve_state_dir(self) -> Path:
        if self._state_dir:
            return self._state_dir
        # Best-effort: put state next to the bookmarks dir if we can infer it.
        # Sync engine constructs ItemStore(output_dir=...) with a Path that
        # lives alongside where we want our state. The cleanest way to pass
        # it is via `state_dir` in config; otherwise fall back to CWD.
        return Path.cwd() / "bookmarks"
