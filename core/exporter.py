"""
core.exporter — exporter framework + FastAPI wiring.

Plugins subclass `Exporter`, declare `applicable_kinds` / `execution_mode`,
and implement `run_immediate(...)` or `run_background(...)`. The web UI
discovers them via `iter_exporters()`, posts a list of item ids + form
inputs to `/api/export/run`, and either streams a file back (immediate)
or returns a task_id (background).

Background tasks are persisted as Markdown files under `exports/tasks/`.
A single worker thread drains a serial queue. On restart, any task whose
status is `running` (i.e. it was mid-flight when the server died) gets
auto-retried *once*; if that retry also fails it's marked `failed`.

Themes live at `themes/export/<kind>/<theme>/`. Each theme dir contains
a `theme.toml` declaring vars (color/text/number/bool/select). `<kind>`
is one of {"any", "photo", "video", "document"}; "any" themes are visible
to every cross-kind exporter (data + link).
"""

from __future__ import annotations

import io
import json
import logging
import queue
import re
import shutil
import threading
import time
import uuid
import zipfile

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore
from abc import ABC
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .ingest import FRONTMATTER_RE

log = logging.getLogger("booki.exporter")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EXPORTS_DIR = ROOT / "exports"
DEFAULT_THEMES_ROOT = ROOT / "themes" / "export"

VALID_KINDS = {"any", "photo", "video", "document"}
VALID_VAR_TYPES = {"color", "text", "number", "bool", "select"}


# ─── Theme ────────────────────────────────────────────────────────────────────

@dataclass
class ThemeVar:
    name: str
    type: str
    label: str
    default: Any = None
    options: Optional[list[Any]] = None   # for type="select"

    def to_dict(self) -> dict:
        d = {"name": self.name, "type": self.type, "label": self.label,
             "default": self.default}
        if self.options is not None:
            d["options"] = self.options
        return d


@dataclass
class Theme:
    slug: str               # dir name
    label: str
    description: str
    kind: str               # any | photo | video | document
    path: Path
    vars: list[ThemeVar] = field(default_factory=list)

    @classmethod
    def from_dir(cls, kind: str, dir_path: Path) -> "Theme":
        toml_path = dir_path / "theme.toml"
        meta: dict = {}
        if toml_path.exists():
            with open(toml_path, "rb") as f:
                meta = tomllib.load(f)

        vars_list: list[ThemeVar] = []
        raw_vars = meta.get("vars", {}) or {}
        for name, spec in raw_vars.items():
            spec = spec or {}
            vtype = str(spec.get("type") or "text").lower()
            if vtype not in VALID_VAR_TYPES:
                log.warning("theme_var_unknown_type",
                            extra={"theme": dir_path.name, "var": name, "type": vtype})
                vtype = "text"
            vars_list.append(ThemeVar(
                name=name,
                type=vtype,
                label=str(spec.get("label") or name.replace("_", " ").title()),
                default=spec.get("default"),
                options=list(spec["options"]) if "options" in spec else None,
            ))

        return cls(
            slug=dir_path.name,
            label=str(meta.get("name") or dir_path.name.replace("_", " ").title()),
            description=str(meta.get("description") or ""),
            kind=kind,
            path=dir_path,
            vars=vars_list,
        )

    def resolve_vars(self, user_vars: dict) -> dict:
        """Merge user-supplied values with declared defaults."""
        out: dict = {}
        for v in self.vars:
            if user_vars and v.name in user_vars and user_vars[v.name] not in (None, ""):
                out[v.name] = user_vars[v.name]
            else:
                out[v.name] = v.default
        return out

    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "label": self.label,
            "description": self.description,
            "kind": self.kind,
            "vars": [v.to_dict() for v in self.vars],
            "has_thumbnail": (self.path / "thumbnail.png").is_file(),
        }


def _themes_dir(kind: str, themes_root: Path) -> Path:
    return themes_root / kind


def _load_colorschemes(themes_root: Path) -> list[dict]:
    """
    Read `<themes_root>/colorschemes.toml` and return a list of
    {slug, name, description, colors} entries. Missing file → []. Malformed
    file → [] with a warning logged (UI just shows no scheme picker).
    """
    p = themes_root / "colorschemes.toml"
    if not p.is_file():
        return []
    try:
        with open(p, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        log.exception("colorschemes_parse_failed", extra={"path": str(p)})
        return []
    out: list[dict] = []
    for s in data.get("schemes") or []:
        if not isinstance(s, dict):
            continue
        slug = str(s.get("slug") or "").strip()
        if not slug:
            continue
        out.append({
            "slug": slug,
            "name": str(s.get("name") or slug),
            "description": str(s.get("description") or ""),
            "colors": {k: str(v) for k, v in (s.get("colors") or {}).items()},
        })
    return out


def list_themes(kind: str, themes_root: Path) -> list[Theme]:
    base = _themes_dir(kind, themes_root)
    if not base.is_dir():
        return []
    return [Theme.from_dir(kind, d) for d in sorted(base.iterdir()) if d.is_dir()]


_THEME_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def get_theme(kind: str, slug: str, themes_root: Path) -> Optional[Theme]:
    # Block path-traversal via slug. Without this guard,
    # `slug = "../../../etc"` makes `p` resolve outside themes_root,
    # and the `/themes/{kind}/{slug}/thumbnail` endpoint becomes a
    # read primitive for any file named thumbnail.png. (P1-02)
    if not _THEME_SLUG_RE.match(slug or ""):
        return None
    base = _themes_dir(kind, themes_root).resolve()
    p = (base / slug).resolve()
    try:
        p.relative_to(base)
    except (ValueError, OSError):
        return None
    if not p.is_dir():
        return None
    return Theme.from_dir(kind, p)


# ─── Exporter ABC + registry ──────────────────────────────────────────────────

@dataclass
class TaskHandle:
    """
    Passed to background exporters. They use it to log progress and update
    the on-disk task record. Logging is line-buffered to the task .md body.
    """
    task_id: str
    artifact_dir: Path
    _store: "TaskStore"

    def log(self, msg: str) -> None:
        self._store.append_log(self.task_id, msg)

    def progress(self, done: int, total: int) -> None:
        self._store.update_progress(self.task_id, done, total)


class Exporter(ABC):
    slug: str = ""
    name: str = ""
    description: str = ""
    applicable_kinds: list[str] = ["any"]
    execution_mode: str = "immediate"     # "immediate" | "background"
    uses_themes: bool = False
    options_schema: list[dict] = []        # each item = {name, type, label, default, options?, help?}

    # Resolved `[[sources.directory.dirs]]` paths. The runner injects this
    # on the instance before run_*/preview. Exporters that touch local
    # files (copy, base64-embed) MUST gate that on
    # `core.local_files.safe_local_path(raw, self.local_roots)` and skip
    # anything that returns None — the empty-list default means "no local
    # files allowed at all", which is the correct fail-safe.
    local_roots: list[Path] = []

    # When True the exporter can render nested folders (the wizard's Refine
    # step will tell the user "this exporter supports hierarchy"). Even when
    # False the exporter still receives the tree-flattened item order, so
    # manual reorder works for flat exporters too.
    supports_hierarchy: bool = False

    def applies_to(self, kind: str) -> bool:
        if "any" in self.applicable_kinds:
            return True
        if kind == "any":
            # An "any" tab (e.g. Search) only sees exporters with applicable_kinds=["any"].
            return False
        return kind in self.applicable_kinds

    # Implement one of these in subclasses:
    #
    # `tree` (when not None) is the user-edited folder/item structure from
    # the wizard's Refine step. For exporters with `supports_hierarchy=True`
    # it dictates output structure; otherwise items are also pre-sorted in
    # tree-flattened order, so flat exporters get the user's manual ordering
    # for free.

    def run_immediate(self, items: list[dict], options: dict,
                      theme: Optional[Theme], theme_vars: dict,
                      tree: Optional[list] = None
                      ) -> tuple[bytes, str, str]:
        """Return (raw_bytes, filename, mime_type)."""
        raise NotImplementedError(
            f"{type(self).__name__} declares immediate but does not implement run_immediate")

    def run_background(self, items: list[dict], options: dict,
                       theme: Optional[Theme], theme_vars: dict,
                       task: TaskHandle,
                       tree: Optional[list] = None) -> Path:
        """Return the absolute path to the produced artifact file."""
        raise NotImplementedError(
            f"{type(self).__name__} declares background but does not implement run_background")

    def options_for(self, items: list[dict]) -> list[dict]:
        """
        Return the options schema, possibly customized for the items at hand.
        Default is the static class-level `options_schema`. Plugins that need
        runtime-dependent options (e.g. a field picker driven by which keys
        the selection actually contains) override this.
        """
        return list(self.options_schema or [])

    def runtime_notes(self) -> list[dict]:
        """
        Per-exporter notices the UI surfaces when this exporter is selected.
        Each entry: {"level": "info" | "warning", "text": "..."} where text
        may contain `inline code` and **bold**. Default: none.
        """
        return []

    def preview(self, items: list[dict], options: dict,
                theme: Optional[Theme], theme_vars: dict,
                tree: Optional[list] = None) -> dict:
        """
        Build a UI preview of what `run_immediate` / `run_background` would
        produce. Default behavior:
          - immediate exporters: invoke run_immediate(); render bytes as html
            or text depending on the mime type. Truncated to PREVIEW_TEXT_LINES
            for plain-text formats.
          - background exporters: empty manifest (override to populate).

        Returns a JSON-shaped dict:
          {"kind": "html" | "text" | "manifest" | "none",
           "filename": str?, "mime": str?, "content": str?,
           "manifest": [...], "truncated": bool, "preview_lines": int?}
        """
        if self.execution_mode == "immediate":
            data, filename, mime = self.run_immediate(items, options, theme, theme_vars, tree=tree)
            return _default_immediate_preview(data, filename, mime)
        return {"kind": "manifest", "manifest": []}

    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "name": self.name or self.slug,
            "description": self.description,
            "applicable_kinds": list(self.applicable_kinds),
            "execution_mode": self.execution_mode,
            "uses_themes": bool(self.uses_themes),
            "supports_hierarchy": bool(self.supports_hierarchy),
            "options_schema": list(self.options_schema or []),
            "runtime_notes": list(self.runtime_notes() or []),
        }


PREVIEW_TEXT_LINES = 80


def _default_immediate_preview(data: bytes, filename: str, mime: str) -> dict:
    is_html = (mime or "").startswith("text/html")
    is_textish = (mime or "").startswith("text/") or any(
        s in (mime or "") for s in ("json", "yaml", "csv", "markdown", "xml")
    )
    if is_html:
        return {"kind": "html", "filename": filename, "mime": mime,
                "content": data.decode("utf-8", errors="replace"),
                "truncated": False}
    if is_textish:
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        truncated = len(lines) > PREVIEW_TEXT_LINES
        if truncated:
            text = "\n".join(lines[:PREVIEW_TEXT_LINES])
        return {"kind": "text", "filename": filename, "mime": mime,
                "content": text, "truncated": truncated,
                "preview_lines": PREVIEW_TEXT_LINES if truncated else len(lines)}
    return {"kind": "none", "filename": filename, "mime": mime,
            "truncated": False}


_REGISTRY: dict[str, type[Exporter]] = {}


def register_exporter(cls: type[Exporter]) -> type[Exporter]:
    if not getattr(cls, "slug", ""):
        raise ValueError(f"{cls.__name__} must set a class-level `slug`")
    if cls.execution_mode not in ("immediate", "background"):
        raise ValueError(f"{cls.__name__}: invalid execution_mode {cls.execution_mode!r}")
    for k in cls.applicable_kinds:
        if k not in VALID_KINDS:
            raise ValueError(f"{cls.__name__}: invalid kind {k!r}")
    _REGISTRY[cls.slug] = cls
    return cls


def iter_exporters() -> Iterator[tuple[str, type[Exporter]]]:
    for slug in sorted(_REGISTRY):
        yield slug, _REGISTRY[slug]


def get_exporter(slug: str) -> Optional[type[Exporter]]:
    return _REGISTRY.get(slug)


def exporters_for_kind(kind: str) -> list[type[Exporter]]:
    return [c for _slug, c in iter_exporters() if c().applies_to(kind)]


# ─── Item resolver ────────────────────────────────────────────────────────────

def items_from_ids(svc, item_ids: list[str]) -> list[dict]:
    """
    Resolve a list of bookmark ids into dicts the plugins consume.
    Preserves the order of `item_ids`. Skips unknown ids.
    """
    out: list[dict] = []
    for bid in item_ids:
        entry = svc._index.get(bid)
        if not entry:
            continue
        path, fm = entry
        try:
            content = path.read_text(encoding="utf-8")
            m = FRONTMATTER_RE.match(content)
            body = content[m.end():] if m else content
        except Exception:
            body = ""
        d = dict(fm)
        d["id"] = bid
        d["body"] = body.strip()
        d["file"] = str(path)
        out.append(d)
    return out


# ─── Task store ───────────────────────────────────────────────────────────────

TASK_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def _now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _slug_safe(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-")
    return s or "task"


def _yaml_dump_flat(d: dict) -> str:
    """
    Flat YAML where complex values (lists, dicts) are serialized as one-line JSON.
    Strings always quoted as JSON strings to avoid escape headaches.
    """
    lines = []
    for k, v in d.items():
        if v is None:
            lines.append(f"{k}: null")
        elif isinstance(v, bool):
            lines.append(f"{k}: {'true' if v else 'false'}")
        elif isinstance(v, (int, float)):
            lines.append(f"{k}: {v}")
        elif isinstance(v, (list, dict)):
            lines.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")
        else:
            lines.append(f"{k}: {json.dumps(str(v), ensure_ascii=False)}")
    return "\n".join(lines)


def _yaml_load_flat(text: str) -> dict:
    out: dict = {}
    for line in text.splitlines():
        line = line.rstrip()
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, _, raw = line.partition(":")
        key, raw = key.strip(), raw.strip()
        if raw == "" or raw == "null":
            out[key] = None
        elif raw == "true":
            out[key] = True
        elif raw == "false":
            out[key] = False
        elif raw.lstrip("-").isdigit():
            out[key] = int(raw)
        elif raw.startswith(("[", "{", '"')):
            try:
                out[key] = json.loads(raw)
            except json.JSONDecodeError:
                out[key] = raw
        else:
            try:
                out[key] = float(raw)
            except ValueError:
                out[key] = raw
    return out


@dataclass
class Task:
    id: str
    exporter: str
    kind: str
    status: str               # pending | running | success | failed
    created_at: str
    started_at: str = ""
    finished_at: str = ""
    item_count: int = 0
    artifact_path: str = ""
    artifact_filename: str = ""
    artifact_mime: str = ""
    error: str = ""
    restart_count: int = 0
    progress_done: int = 0
    progress_total: int = 0
    options: dict = field(default_factory=dict)
    theme: Optional[str] = None
    theme_vars: dict = field(default_factory=dict)
    item_ids: list[str] = field(default_factory=list)
    tree: list = field(default_factory=list)
    log: str = ""

    def to_frontmatter(self) -> dict:
        return {
            "id": self.id,
            "exporter": self.exporter,
            "kind": self.kind,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "item_count": self.item_count,
            "artifact_path": self.artifact_path,
            "artifact_filename": self.artifact_filename,
            "artifact_mime": self.artifact_mime,
            "error": self.error,
            "restart_count": self.restart_count,
            "progress_done": self.progress_done,
            "progress_total": self.progress_total,
            "options": self.options,
            "theme": self.theme,
            "theme_vars": self.theme_vars,
            "item_ids": self.item_ids,
            "tree": self.tree,
        }

    def to_api(self) -> dict:
        d = self.to_frontmatter()
        d["log"] = self.log
        return d


_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _validate_task_id(task_id: str) -> None:
    """Reject any task id that could escape the tasks dir or smuggle a
    null byte through `Path` joining. The route signature also enforces
    the regex via Pydantic validators where it can; this is the load-
    bearing guard for path-traversal — `Path(self.dir) / f"{tid}.md"`
    would otherwise happily resolve to `../../etc/hosts.md`. (P1-01)"""
    if not _TASK_ID_RE.match(task_id or ""):
        raise HTTPException(404, "task not found")


class TaskStore:
    """Reads / writes task .md files. Thread-safe via a lock per file write."""

    def __init__(self, tasks_dir: Path):
        self.dir = tasks_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._dir_resolved = self.dir.resolve()
        self._lock = threading.Lock()

    def _path(self, task_id: str) -> Path:
        _validate_task_id(task_id)
        p = self.dir / f"{task_id}.md"
        # Defense-in-depth: even with the regex guard, assert the resolved
        # path stays inside `dir`. Any future tweak to the validator can't
        # reintroduce traversal without also breaking this check.
        try:
            p.resolve().relative_to(self._dir_resolved)
        except (ValueError, OSError):
            raise HTTPException(404, "task not found")
        return p

    def list(self) -> list[Task]:
        if not self.dir.is_dir():
            return []
        out: list[Task] = []
        for p in sorted(self.dir.glob("*.md")):
            try:
                t = self._read(p)
                if t is not None:
                    out.append(t)
            except Exception:
                log.exception("task_read_failed", extra={"path": str(p)})
        out.sort(key=lambda t: t.created_at, reverse=True)
        return out

    def get(self, task_id: str) -> Optional[Task]:
        p = self._path(task_id)
        if not p.exists():
            return None
        return self._read(p)

    def _read(self, path: Path) -> Optional[Task]:
        text = path.read_text(encoding="utf-8")
        m = TASK_FRONTMATTER_RE.match(text)
        if not m:
            return None
        fm = _yaml_load_flat(m.group(1))
        body = text[m.end():]
        return Task(
            id=str(fm.get("id") or path.stem),
            exporter=str(fm.get("exporter") or ""),
            kind=str(fm.get("kind") or "any"),
            status=str(fm.get("status") or "pending"),
            created_at=str(fm.get("created_at") or ""),
            started_at=str(fm.get("started_at") or ""),
            finished_at=str(fm.get("finished_at") or ""),
            item_count=int(fm.get("item_count") or 0),
            artifact_path=str(fm.get("artifact_path") or ""),
            artifact_filename=str(fm.get("artifact_filename") or ""),
            artifact_mime=str(fm.get("artifact_mime") or ""),
            error=str(fm.get("error") or ""),
            restart_count=int(fm.get("restart_count") or 0),
            progress_done=int(fm.get("progress_done") or 0),
            progress_total=int(fm.get("progress_total") or 0),
            options=dict(fm.get("options") or {}),
            theme=fm.get("theme"),
            theme_vars=dict(fm.get("theme_vars") or {}),
            item_ids=list(fm.get("item_ids") or []),
            tree=list(fm.get("tree") or []),
            log=body,
        )

    def write(self, task: Task) -> None:
        with self._lock:
            p = self._path(task.id)
            content = "---\n" + _yaml_dump_flat(task.to_frontmatter()) + "\n---\n" + (task.log or "")
            p.write_text(content, encoding="utf-8")

    def append_log(self, task_id: str, msg: str) -> None:
        with self._lock:
            t = self.get(task_id)
            if t is None:
                return
            stamped = f"[{_now_iso()}] {msg}\n"
            t.log = (t.log or "") + stamped
            self._write_unlocked(t)

    def update_progress(self, task_id: str, done: int, total: int) -> None:
        with self._lock:
            t = self.get(task_id)
            if t is None:
                return
            t.progress_done = max(0, int(done))
            t.progress_total = max(0, int(total))
            self._write_unlocked(t)

    def _write_unlocked(self, task: Task) -> None:
        p = self._path(task.id)
        content = "---\n" + _yaml_dump_flat(task.to_frontmatter()) + "\n---\n" + (task.log or "")
        p.write_text(content, encoding="utf-8")

    def delete(self, task_id: str) -> bool:
        with self._lock:
            p = self._path(task_id)
            if not p.exists():
                return False
            p.unlink()
            return True


# ─── Background runner ────────────────────────────────────────────────────────

class BackgroundRunner:
    """
    Single-worker, serial. Submitting a task pushes its id onto the queue; the
    worker drains the queue and runs each task in turn.

    Robustness:
      - run_background errors → status=failed, error captured.
      - On startup, find tasks whose status is "running" or "pending" and
        re-queue them, incrementing restart_count. If restart_count > 1 we
        give up and mark the task as failed (auto-retry-once policy).
    """

    def __init__(self, store: TaskStore, item_resolver: Callable[[list[str]], list[dict]],
                 artifacts_dir: Path, themes_root: Path,
                 local_roots: Optional[list[Path]] = None):
        self.store = store
        self.item_resolver = item_resolver
        self.artifacts_dir = artifacts_dir
        self.themes_root = themes_root
        self.local_roots = list(local_roots or [])
        self._q: "queue.Queue[str]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._running = False

    def start(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._running = True
        self._worker = threading.Thread(target=self._loop, name="exporter-worker", daemon=True)
        self._worker.start()
        # Recover any in-flight or pending tasks left over from a crash.
        self.recover_after_restart()

    def submit(self, task_id: str) -> None:
        self._q.put(task_id)

    def _loop(self) -> None:
        while self._running:
            try:
                task_id = self._q.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._run_one(task_id)
            except Exception:
                log.exception("worker_crash", extra={"task_id": task_id})

    def _run_one(self, task_id: str) -> None:
        t = self.store.get(task_id)
        if t is None:
            return
        cls = get_exporter(t.exporter)
        if cls is None:
            t.status = "failed"
            t.error = f"Unknown exporter: {t.exporter}"
            t.finished_at = _now_iso()
            self.store.write(t)
            return

        t.status = "running"
        t.started_at = _now_iso()
        t.error = ""
        self.store.write(t)

        artifact_dir = self.artifacts_dir / t.id
        artifact_dir.mkdir(parents=True, exist_ok=True)

        try:
            inst = cls()
            inst.local_roots = list(self.local_roots)
            theme = None
            if inst.uses_themes and t.theme:
                theme_kind = inst.applicable_kinds[0] if inst.applicable_kinds else "any"
                theme = get_theme(theme_kind, t.theme, self.themes_root)
            theme_vars = theme.resolve_vars(t.theme_vars or {}) if theme else {}
            items = order_items_by_tree(self.item_resolver(t.item_ids), t.tree)

            handle = TaskHandle(task_id=t.id, artifact_dir=artifact_dir, _store=self.store)
            handle.log(f"starting · exporter={t.exporter} · items={len(items)}")
            artifact_path = inst.run_background(
                items, t.options, theme, theme_vars, handle, tree=(t.tree or None))
            artifact_path = Path(artifact_path).resolve()

            # Refresh after potential progress writes.
            t = self.store.get(task_id) or t
            t.status = "success"
            t.artifact_path = str(artifact_path)
            t.artifact_filename = artifact_path.name
            t.finished_at = _now_iso()
            self.store.write(t)
            self.store.append_log(t.id, f"done · {artifact_path.name}")

        except Exception as e:
            log.exception("task_failed", extra={"task_id": t.id, "exporter": t.exporter})
            t = self.store.get(task_id) or t
            t.status = "failed"
            t.error = f"{type(e).__name__}: {e}"
            t.finished_at = _now_iso()
            self.store.write(t)
            self.store.append_log(t.id, f"failed · {t.error}")

    def manual_retry(self, task_id: str) -> bool:
        t = self.store.get(task_id)
        if t is None:
            return False
        t.status = "pending"
        t.error = ""
        t.started_at = ""
        t.finished_at = ""
        # Reset auto-retry counter so manual retries always get a fresh attempt window.
        t.restart_count = 0
        self.store.write(t)
        self.store.append_log(t.id, "manual retry")
        self.submit(t.id)
        return True

    def recover_after_restart(self) -> None:
        for t in self.store.list():
            if t.status in ("pending", "running"):
                t.restart_count += 1
                if t.restart_count > 1:
                    t.status = "failed"
                    t.error = "Server restarted before completion (auto-retry exhausted)"
                    t.finished_at = _now_iso()
                    self.store.write(t)
                    self.store.append_log(t.id, "auto-retry exhausted")
                    continue
                # Re-queue once.
                t.status = "pending"
                t.started_at = ""
                self.store.write(t)
                self.store.append_log(t.id, f"restart · auto-retry #{t.restart_count}")
                self.submit(t.id)


# ─── Pydantic request body ────────────────────────────────────────────────────

class ExportRunRequest(BaseModel):
    exporter: str
    theme: Optional[str] = None
    theme_vars: dict = Field(default_factory=dict)
    options: dict = Field(default_factory=dict)
    item_ids: list[str] = Field(default_factory=list)
    # Optional folder/item tree from the wizard's Refine step. Each node is
    # {"type": "folder", "name": str, "children": [...]} or
    # {"type": "item", "item_id": str}. When provided, items are reordered
    # to match tree-flattened order; hierarchy-aware exporters also walk
    # the structure to emit folders.
    tree: Optional[list] = None


class ExportOptionsRequest(BaseModel):
    exporter: str
    item_ids: list[str] = Field(default_factory=list)


# ─── Tree helpers ─────────────────────────────────────────────────────────────

def flatten_tree(tree: Optional[list]) -> list[tuple[list[str], str]]:
    """
    Walk `tree` depth-first, returning (path, item_id) for each item node in
    display order. `path` is the list of folder names from root to the
    item's parent (empty for items at the top level). Folders without items
    contribute nothing.
    """
    out: list[tuple[list[str], str]] = []

    def walk(nodes, path):
        for n in nodes or []:
            t = n.get("type") if isinstance(n, dict) else None
            if t == "folder":
                child_path = path + [str(n.get("name") or "")]
                walk(n.get("children") or [], child_path)
            elif t == "item":
                iid = n.get("item_id")
                if iid:
                    out.append((list(path), str(iid)))

    walk(tree or [], [])
    return out


def order_items_by_tree(items: list[dict], tree: Optional[list]) -> list[dict]:
    """
    Return items in tree-flattened order with `_path` populated. An item
    that appears in N tree folders shows up N times (intentional for tag/
    list groupings). Items NOT referenced anywhere in the tree are dropped
    — the wizard's Refine step is the user's chance to exclude items.
    """
    if not tree:
        return items
    by_id: dict[str, dict] = {}
    for it in items:
        by_id[str(it.get("id") or "")] = it
    out: list[dict] = []
    for path, iid in flatten_tree(tree):
        it = by_id.get(iid)
        if it is None:
            continue
        copy = dict(it)
        copy["_path"] = list(path)
        out.append(copy)
    return out


# ─── FastAPI wiring ───────────────────────────────────────────────────────────

def attach_routes(app: FastAPI, cfg: dict, config_path: Path, svc) -> None:
    """Wire all /api/export* routes onto `app`. Called from core.web.create_app()."""

    # Resolve paths from config (with fallbacks).
    exports_cfg = (cfg.get("exports", {}) or {})
    exports_root = Path(exports_cfg.get("dir") or DEFAULT_EXPORTS_DIR)
    if not exports_root.is_absolute():
        exports_root = (config_path.parent / exports_root).resolve()
    tasks_dir = exports_root / "tasks"
    artifacts_dir = exports_root / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    themes_root = Path(exports_cfg.get("themes_dir") or DEFAULT_THEMES_ROOT)
    if not themes_root.is_absolute():
        themes_root = (config_path.parent / themes_root).resolve()

    # Trigger plugin registration.
    from plugins import exporters as _exporters_pkg  # noqa: F401

    store = TaskStore(tasks_dir)

    # Allow-listed roots for any local-file read an exporter might do.
    # Re-read on every immediate/preview call so config edits take effect
    # without restarting the server (the background runner only reads
    # them at startup, which matches how it picks up other settings).
    from .local_files import directory_roots

    def _resolve(item_ids: list[str]) -> list[dict]:
        svc.refresh()
        return items_from_ids(svc, item_ids)

    def _live_local_roots() -> list[Path]:
        try:
            live_cfg = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            live_cfg = cfg
        return directory_roots(live_cfg)

    runner = BackgroundRunner(store, _resolve, artifacts_dir, themes_root,
                              local_roots=directory_roots(cfg))
    runner.start()

    # ── Discovery ───────────────────────────────────────────────────────

    @app.get("/api/export/exporters")
    def list_exporters_api(kind: str = "any"):
        kind = kind or "any"
        if kind not in VALID_KINDS:
            raise HTTPException(400, f"Unknown kind: {kind}")
        out = []
        for slug, cls in iter_exporters():
            inst = cls()
            if inst.applies_to(kind):
                out.append(inst.to_dict())
        return out

    @app.get("/api/export/themes")
    def list_themes_api(exporter: str):
        cls = get_exporter(exporter)
        if cls is None:
            raise HTTPException(404, f"Unknown exporter: {exporter}")
        inst = cls()
        if not inst.uses_themes:
            return []
        # Themes for an exporter are scoped to the *first* of its kinds.
        # Cross-kind exporters should declare ["any"] and pick from any/.
        theme_kind = inst.applicable_kinds[0] if inst.applicable_kinds else "any"
        return [t.to_dict() for t in list_themes(theme_kind, themes_root)]

    @app.post("/api/export/options")
    def discover_options(req: ExportOptionsRequest):
        cls = get_exporter(req.exporter)
        if cls is None:
            raise HTTPException(404, f"Unknown exporter: {req.exporter}")
        items = _resolve(req.item_ids) if req.item_ids else []
        return cls().options_for(items)

    @app.get("/api/export/themes/{kind}/{slug}")
    def get_theme_api(kind: str, slug: str):
        if kind not in VALID_KINDS:
            raise HTTPException(404, f"Unknown kind: {kind}")
        t = get_theme(kind, slug, themes_root)
        if t is None:
            raise HTTPException(404, f"Theme not found: {kind}/{slug}")
        return t.to_dict()

    @app.get("/api/export/themes/{kind}/{slug}/thumbnail")
    def get_theme_thumbnail(kind: str, slug: str):
        if kind not in VALID_KINDS:
            raise HTTPException(404, f"Unknown kind: {kind}")
        t = get_theme(kind, slug, themes_root)
        if t is None:
            raise HTTPException(404, f"Theme not found: {kind}/{slug}")
        png = t.path / "thumbnail.png"
        if not png.is_file():
            raise HTTPException(404, "No thumbnail for this theme")
        return FileResponse(png, media_type="image/png")

    @app.get("/api/export/colorschemes")
    def list_colorschemes():
        return _load_colorschemes(themes_root)

    # ── Preview ─────────────────────────────────────────────────────────

    @app.post("/api/export/preview")
    def export_preview(req: ExportRunRequest):
        cls = get_exporter(req.exporter)
        if cls is None:
            raise HTTPException(404, f"Unknown exporter: {req.exporter}")
        if not req.item_ids:
            raise HTTPException(400, "item_ids is empty")
        inst = cls()
        inst.local_roots = _live_local_roots()
        theme = None
        if inst.uses_themes and req.theme:
            theme_kind = inst.applicable_kinds[0] if inst.applicable_kinds else "any"
            theme = get_theme(theme_kind, req.theme, themes_root)
            if theme is None:
                raise HTTPException(404, f"Theme not found: {theme_kind}/{req.theme}")
        theme_vars = theme.resolve_vars(req.theme_vars or {}) if theme else {}
        items = order_items_by_tree(_resolve(req.item_ids), req.tree)
        if not items:
            raise HTTPException(400, "No items resolved from item_ids")
        try:
            return inst.preview(items, req.options, theme, theme_vars, tree=req.tree)
        except Exception as e:
            log.exception("preview_failed",
                          extra={"exporter": req.exporter, "item_count": len(items)})
            raise HTTPException(500, f"Preview failed: {type(e).__name__}: {e}")

    # ── Run ─────────────────────────────────────────────────────────────

    @app.post("/api/export/run")
    def export_run(req: ExportRunRequest):
        cls = get_exporter(req.exporter)
        if cls is None:
            raise HTTPException(404, f"Unknown exporter: {req.exporter}")
        if not req.item_ids:
            raise HTTPException(400, "item_ids is empty")

        inst = cls()
        inst.local_roots = _live_local_roots()
        theme = None
        if inst.uses_themes and req.theme:
            theme_kind = inst.applicable_kinds[0] if inst.applicable_kinds else "any"
            theme = get_theme(theme_kind, req.theme, themes_root)
            if theme is None:
                raise HTTPException(404, f"Theme not found: {theme_kind}/{req.theme}")
        theme_vars = theme.resolve_vars(req.theme_vars or {}) if theme else {}

        if inst.execution_mode == "immediate":
            items = order_items_by_tree(_resolve(req.item_ids), req.tree)
            if not items:
                raise HTTPException(400, "No items resolved from item_ids")
            t0 = time.monotonic()
            try:
                data, filename, mime = inst.run_immediate(
                    items, req.options, theme, theme_vars, tree=req.tree)
            except Exception as e:
                log.exception("immediate_export_failed",
                              extra={"exporter": req.exporter, "item_count": len(items)})
                raise HTTPException(500, f"Export failed: {type(e).__name__}: {e}")
            log.info("immediate_export_ok",
                     extra={"exporter": req.exporter, "item_count": len(items),
                            "duration_s": round(time.monotonic() - t0, 3),
                            "bytes": len(data)})
            return Response(
                content=data,
                media_type=mime,
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )

        # background
        task_id = uuid.uuid4().hex[:12]
        kind_label = inst.applicable_kinds[0] if inst.applicable_kinds else "any"
        task = Task(
            id=task_id,
            exporter=req.exporter,
            kind=kind_label,
            status="pending",
            created_at=_now_iso(),
            item_count=len(req.item_ids),
            options=dict(req.options or {}),
            theme=req.theme,
            theme_vars=dict(req.theme_vars or {}),
            item_ids=list(req.item_ids),
            tree=list(req.tree) if req.tree else [],
            progress_total=len(req.item_ids),
        )
        store.write(task)
        store.append_log(task_id, f"queued · exporter={req.exporter} · items={len(req.item_ids)}")
        runner.submit(task_id)
        return {"task_id": task_id, "status": "pending"}

    # ── Tasks ───────────────────────────────────────────────────────────

    @app.get("/api/export/tasks")
    def list_tasks():
        return [t.to_api() for t in store.list()]

    @app.get("/api/export/tasks/{task_id}")
    def get_task(task_id: str):
        t = store.get(task_id)
        if t is None:
            raise HTTPException(404, f"Task not found: {task_id}")
        return t.to_api()

    @app.post("/api/export/tasks/{task_id}/retry")
    def retry_task(task_id: str):
        ok = runner.manual_retry(task_id)
        if not ok:
            raise HTTPException(404, f"Task not found: {task_id}")
        return {"task_id": task_id, "status": "pending"}

    @app.delete("/api/export/tasks/{task_id}")
    def delete_task(task_id: str):
        t = store.get(task_id)
        if t is None:
            raise HTTPException(404, f"Task not found: {task_id}")
        # Best-effort: also remove the artifact dir.
        artifact_dir = artifacts_dir / task_id
        if artifact_dir.is_dir():
            shutil.rmtree(artifact_dir, ignore_errors=True)
        store.delete(task_id)
        return {"deleted": task_id}

    @app.get("/api/export/tasks/{task_id}/artifact")
    def task_artifact(task_id: str):
        t = store.get(task_id)
        if t is None:
            raise HTTPException(404, f"Task not found: {task_id}")
        if t.status != "success" or not t.artifact_path:
            raise HTTPException(409, f"Artifact not ready (status={t.status})")
        p = Path(t.artifact_path)
        if not p.exists():
            raise HTTPException(410, "Artifact file no longer on disk")
        return FileResponse(p, filename=t.artifact_filename or p.name,
                            media_type=t.artifact_mime or "application/octet-stream")
