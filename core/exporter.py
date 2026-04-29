"""
exporter.py — selection resolver + YAML config I/O for the export system.

The web wizard builds a `Selection`, the resolver turns it into a concrete
list of bookmarks (pulled from `BookmarkService._index`), and the chosen
Exporter plugin renders them into an artifact.

Selection semantics
-------------------
Four independent criterion groups (lists, tags, filters, manual_ids).
Each group is a union within itself; across groups it's also a union
("everything in list AI OR tagged rag OR imp>=6 OR these specific ids").
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


# ─── Selection model ──────────────────────────────────────────────────────────

@dataclass
class SelectionFilters:
    """Predicates applied AND-together within the 'filter' group."""
    source: Optional[str] = None
    kind: Optional[str] = None
    importance_min: Optional[int] = None
    importance_max: Optional[int] = None

    def is_empty(self) -> bool:
        return (self.source is None and self.kind is None
                and self.importance_min is None and self.importance_max is None)


@dataclass
class Selection:
    lists: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    filters: SelectionFilters = field(default_factory=SelectionFilters)
    manual_ids: list[str] = field(default_factory=list)
    smart_lists: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "Selection":
        d = d or {}
        f = d.get("filters") or {}
        return cls(
            lists=[str(x) for x in (d.get("lists") or [])],
            tags=[str(x) for x in (d.get("tags") or [])],
            filters=SelectionFilters(
                source=(f.get("source") or None),
                kind=(f.get("kind") or None),
                importance_min=(None if f.get("importance_min") in (None, "") else int(f["importance_min"])),
                importance_max=(None if f.get("importance_max") in (None, "") else int(f["importance_max"])),
            ),
            manual_ids=[str(x) for x in (d.get("manual_ids") or [])],
            smart_lists=[str(x) for x in (d.get("smart_lists") or [])],
        )

    def to_dict(self) -> dict:
        return {
            "lists": list(self.lists),
            "tags": list(self.tags),
            "filters": asdict(self.filters),
            "manual_ids": list(self.manual_ids),
            "smart_lists": list(self.smart_lists),
        }


# ─── Resolver ─────────────────────────────────────────────────────────────────

def _item_in_lists(fm: dict, wanted: list[str]) -> bool:
    if not wanted:
        return False
    have = {str(x).strip() for x in (fm.get("lists") or [])}
    return any(w in have for w in wanted)


def _item_has_tags(fm: dict, wanted: list[str]) -> bool:
    if not wanted:
        return False
    have = {str(x).strip().lower() for x in (fm.get("tags") or [])}
    return any(str(w).strip().lower() in have for w in wanted)


def _item_matches_filters(fm: dict, flt: SelectionFilters) -> bool:
    """All filter predicates AND'd together. An empty SelectionFilters matches nothing."""
    if flt.is_empty():
        return False
    if flt.source is not None:
        srcs = {str(fm.get("source") or "")} | {str(s) for s in (fm.get("sources") or [])}
        if flt.source not in srcs:
            return False
    if flt.kind is not None and str(fm.get("kind") or "") != flt.kind:
        return False
    imp = int(fm.get("importance") or 0)
    if flt.importance_min is not None and imp < flt.importance_min:
        return False
    if flt.importance_max is not None and imp > flt.importance_max:
        return False
    return True


def resolve(index: dict[str, tuple[Path, dict]], sel: Selection) -> list[tuple[str, dict]]:
    """
    Turn a Selection into a deduped, sorted list of (bid, frontmatter) tuples.

    `index` is `BookmarkService._index` — {bid: (path, fm)}.
    """
    manual = set(sel.manual_ids)
    seen_url: set[str] = set()
    out: list[tuple[str, dict]] = []

    def _url_key(fm: dict) -> str:
        return str(fm.get("url") or "").rstrip("/").lower()

    for bid, (_path, fm) in index.items():
        matched = (
            (bid in manual)
            or _item_in_lists(fm, sel.lists)
            or _item_has_tags(fm, sel.tags)
            or _item_matches_filters(fm, sel.filters)
        )
        if not matched:
            continue
        key = _url_key(fm) or bid
        if key in seen_url:
            continue
        seen_url.add(key)
        out.append((bid, fm))

    out.sort(key=lambda p: (str(p[1].get("title") or "").lower(), p[0]))
    return out


# ─── Config YAML I/O ──────────────────────────────────────────────────────────

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_config_name(name: str) -> str:
    """Sanitize a user-provided config name for use as a filename (no .yaml)."""
    n = _SAFE_NAME_RE.sub("-", (name or "").strip()).strip("-._")
    return n or "export"


def timestamp_slug() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


@dataclass
class ExportConfig:
    name: str
    exporter: str
    theme: Optional[str]
    selection: Selection
    options: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "ExportConfig":
        return cls(
            name=str(d.get("name") or "").strip() or "export",
            exporter=str(d.get("exporter") or "").strip(),
            theme=(str(d["theme"]).strip() or None) if d.get("theme") else None,
            selection=Selection.from_dict(d.get("selection") or {}),
            options=dict(d.get("options") or {}),
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "exporter": self.exporter,
            "theme": self.theme,
            "selection": self.selection.to_dict(),
            "options": dict(self.options),
        }


def _yaml():
    try:
        import yaml  # type: ignore
        return yaml
    except ImportError as e:
        raise RuntimeError(
            "PyYAML is required for export configs. Install with: pip install pyyaml"
        ) from e


def load_config(path: Path) -> ExportConfig:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        data = _yaml().safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: config must be a mapping")
    return ExportConfig.from_dict(data)


def save_config(cfg: ExportConfig, configs_dir: Path, *, overwrite: bool = True) -> Path:
    configs_dir.mkdir(parents=True, exist_ok=True)
    path = configs_dir / f"{safe_config_name(cfg.name)}.yaml"
    if path.exists() and not overwrite:
        raise FileExistsError(str(path))
    yaml = _yaml()
    text = yaml.safe_dump(cfg.to_dict(), sort_keys=False, allow_unicode=True)
    path.write_text(text, encoding="utf-8")
    return path


def list_configs(configs_dir: Path) -> list[dict]:
    if not configs_dir.exists():
        return []
    out = []
    for p in sorted(configs_dir.glob("*.yaml")):
        try:
            cfg = load_config(p)
            out.append({"name": cfg.name, "file": p.name,
                        "exporter": cfg.exporter, "theme": cfg.theme})
        except Exception:
            continue
    return out


# ─── Paths ────────────────────────────────────────────────────────────────────

@dataclass
class ExportPaths:
    configs_dir: Path
    artifacts_dir: Path
    themes_dir: Path

    @classmethod
    def from_toml(cls, cfg: dict, project_root: Path) -> "ExportPaths":
        ex = cfg.get("export", {}) or {}
        def _p(key: str, default: str) -> Path:
            val = str(ex.get(key) or default)
            p = Path(val)
            if not p.is_absolute():
                p = (project_root / p).resolve()
            return p
        return cls(
            configs_dir=_p("configs_dir", "./exports/configs"),
            artifacts_dir=_p("artifacts_dir", "./exports/artifacts"),
            themes_dir=_p("themes_dir", "./themes"),
        )

    def ensure(self) -> None:
        self.configs_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.themes_dir.mkdir(parents=True, exist_ok=True)
