#!/usr/bin/env python3
"""
web.py — FastAPI UI for browsing, searching, and editing bookmarks.

Two search modes:
  • Fast find-as-you-type (client-side fzf-style fuzzy match over title + URL)
  • Ask (semantic vector search over the ChromaDB index + LLM synthesis)

Run:
    booki web                    # host/port from config.toml [web]
    booki web --port 9000        # override
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("booki.web")

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# Reuse parsers / writers from the CLI tools.
from .ingest import FRONTMATTER_RE, bm_id, parse_bookmark_file
from .store import ItemStore, today_str
import plugins as _plugins_pkg  # noqa: F401 — triggers plugin registration
from plugins.base import iter_enrichers, iter_registered, iter_tabs
from .download import DownloadConfig, download_one, update_md_for_download
from .sync import ExcludeFilter, LinkExcluded, sync_link


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.toml"
WEB_DIR = ROOT / "web"
PLUGINS_DIR = ROOT / "plugins"


# ─── Pydantic models ──────────────────────────────────────────────────────────

class Bookmark(BaseModel):
    """Compact view — what the list UI needs for fuzzy search + rendering."""
    id: str
    title: str = ""
    url: str = ""
    source: str = ""
    sources: list[str] = Field(default_factory=list)
    kind: str = "bookmark"
    browser_path: str = ""
    folder_path: str = ""
    importance: int = 0
    tags: list[str] = Field(default_factory=list)
    lists: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    notes: str = ""
    summary: str = ""
    status: str = "unchecked"
    date_bookmarked: str = ""
    last_sync: str = ""
    archive_url: str = ""
    removed_from_browser: bool = False
    removed_from_source: bool = False
    has_summary: bool = False
    extras: dict[str, Any] = Field(default_factory=dict)


class BookmarkDetail(Bookmark):
    body: str = ""       # markdown body (everything after frontmatter)
    file: str = ""       # path relative to bookmarks dir


class BookmarkUpdate(BaseModel):
    """Only these fields may be edited from the UI."""
    title: Optional[str] = None
    importance: Optional[int] = Field(default=None, ge=0, le=10)
    tags: Optional[list[str]] = None
    lists: Optional[list[str]] = None
    notes: Optional[str] = None
    summary: Optional[str] = None
    keywords: Optional[list[str]] = None


class ListRename(BaseModel):
    old: str
    new: str


class AskQuery(BaseModel):
    query: str
    n: int = 5
    min_importance: int = 0
    use_llm: bool = True


class LinkAddRequest(BaseModel):
    url: str
    title: Optional[str] = None


class LinkAddResponse(BaseModel):
    id: str
    url: str
    title: str
    is_new: bool


class DownloadRequest(BaseModel):
    format: str = "video"   # "video" | "audio"


class DownloadResponse(BaseModel):
    queued: bool
    status: str             # "queued" | "running" | "done" | "error" | "unsupported"
    message: str = ""


class AskResult(BaseModel):
    answer: str = ""
    bookmarks: list[dict] = Field(default_factory=list)
    provider: str = ""
    model: str = ""


# ─── Bookmark service ─────────────────────────────────────────────────────────

class BookmarkService:
    """
    In-memory index of all bookmark MD files, keyed by id (sha256(url)[:16]).
    Re-reads from disk whenever `.refresh()` is called — cheap for O(100) files.
    """

    def __init__(self, bookmarks_dir: Path):
        self.dir = bookmarks_dir
        self.store = ItemStore(bookmarks_dir)
        self._index: dict[str, tuple[Path, dict]] = {}
        self.refresh()

    def refresh(self) -> None:
        self._index.clear()
        if not self.dir.exists():
            return
        for md_file in sorted(self.dir.rglob("*.md")):
            fm = parse_bookmark_file(md_file)
            if not fm or not fm.get("url"):
                continue
            self._index[bm_id(fm)] = (md_file, fm)

    def list(self) -> list[Bookmark]:
        self.refresh()
        return [_to_bookmark(bid, fm) for bid, (_, fm) in self._index.items()]

    def get(self, bid: str) -> BookmarkDetail:
        entry = self._index.get(bid)
        if not entry:
            self.refresh()
            entry = self._index.get(bid)
        if not entry:
            raise HTTPException(404, f"Bookmark {bid} not found")
        path, fm = entry
        content = path.read_text(encoding="utf-8")
        m = FRONTMATTER_RE.match(content)
        body = content[m.end():] if m else content
        bm = _to_bookmark(bid, fm)
        return BookmarkDetail(
            **bm.model_dump(),
            body=body,
            file=str(path.relative_to(self.dir)),
        )

    def update(self, bid: str, patch: BookmarkUpdate) -> BookmarkDetail:
        entry = self._index.get(bid)
        if not entry:
            self.refresh()
            entry = self._index.get(bid)
        if not entry:
            raise HTTPException(404, f"Bookmark {bid} not found")
        path, _ = entry

        updates = {k: v for k, v in patch.model_dump().items() if v is not None}
        if not updates:
            return self.get(bid)

        # ItemStore.update_fields re-renders both frontmatter and body,
        # so the on-disk MD stays in sync with the YAML truth.
        self.store.update_fields(path, **updates, last_sync=today_str())
        self.refresh()
        return self.get(bid)


CORE_FIELDS = {
    "title", "url", "source", "sources", "kind", "browser_path", "folder_path",
    "importance", "tags", "lists", "keywords", "notes", "summary", "status",
    "date_bookmarked", "last_sync", "archive_url",
    "removed_from_browser", "removed_from_source",
}


def _to_bookmark(bid: str, fm: dict) -> Bookmark:
    summary = str(fm.get("summary", "") or "").strip()
    extras = {k: v for k, v in fm.items() if k not in CORE_FIELDS}
    return Bookmark(
        id=bid,
        title=str(fm.get("title", "") or ""),
        url=str(fm.get("url", "") or ""),
        source=str(fm.get("source", "") or ""),
        sources=[str(s) for s in (fm.get("sources") or [])],
        kind=str(fm.get("kind", "bookmark") or "bookmark"),
        browser_path=str(fm.get("browser_path", "") or ""),
        folder_path=str(fm.get("folder_path", "") or ""),
        importance=int(fm.get("importance", 0) or 0),
        tags=[str(t) for t in (fm.get("tags") or [])],
        lists=[str(l) for l in (fm.get("lists") or [])],
        keywords=[str(k) for k in (fm.get("keywords") or [])],
        notes=str(fm.get("notes", "") or ""),
        summary=summary,
        status=str(fm.get("status", "unchecked") or "unchecked"),
        date_bookmarked=str(fm.get("date_bookmarked", "") or ""),
        last_sync=str(fm.get("last_sync", "") or ""),
        archive_url=str(fm.get("archive_url", "") or ""),
        removed_from_browser=bool(fm.get("removed_from_browser")),
        removed_from_source=bool(fm.get("removed_from_source")),
        has_summary=bool(summary),
        extras=extras,
    )


def _collect_schema() -> dict[str, list[dict]]:
    """
    Merge field_specs() across all registered sources AND enrichers,
    keyed by plugin slug (e.g. "youtube", "github").

    The UI doesn't distinguish source-supplied vs enricher-supplied fields —
    both are just frontmatter. Enricher specs typically declare a `group`
    label so the drawer can render them as their own collapsible block.
    """
    out: dict[str, list[dict]] = {}
    for name, cls in iter_registered():
        specs = list(cls.field_specs() or [])
        if specs:
            out[name] = specs
    for name, cls in iter_enrichers():
        specs = list(cls.field_specs() or [])
        if specs:
            # Namespace under the enricher slug; if a source plugin happens to
            # share the slug we merge so neither is lost.
            existing = out.get(name, [])
            out[name] = existing + specs
    return out


# ─── App factory ──────────────────────────────────────────────────────────────

def load_config(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _is_within(target: Path, root: Path) -> bool:
    """True if `target` is `root` or a descendant of it (both already resolved)."""
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False


def _resolved_log_path(cfg: dict) -> Optional[Path]:
    """Resolve `[logs].file` the same way core.logs.setup_logging does."""
    s = ((cfg.get("logs", {}) or {}).get("file") or "").strip()
    if not s:
        return None
    p = Path(s).expanduser()
    if not p.is_absolute():
        p = (ROOT / p).resolve()
    return p


def _tail_lines(path: Path, n: int) -> list[str]:
    """Return last `n` lines of `path` as decoded text. Reads from the tail
    so huge files don't get fully slurped."""
    n = max(1, n)
    with path.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        block = 8192
        offset = size
        data = b""
        while offset > 0 and data.count(b"\n") <= n:
            read = min(block, offset)
            offset -= read
            f.seek(offset)
            data = f.read(read) + data
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    return lines[-n:]


def create_app(config_path: Path = DEFAULT_CONFIG) -> FastAPI:
    cfg = load_config(config_path)

    # Re-apply logging config here too — `uvicorn --reload` spawns a child
    # process that re-imports `core.web` without going through the booki
    # dispatcher, so the dispatcher's setup_logging() doesn't reach it.
    # setup_logging is idempotent, so doing it twice in the parent is fine.
    from .logs import setup_logging
    setup_logging(cfg)

    bookmarks_dir = Path(cfg["bookmarks"]["dir"])
    if not bookmarks_dir.is_absolute():
        bookmarks_dir = (config_path.parent / bookmarks_dir).resolve()

    svc = BookmarkService(bookmarks_dir)
    dl_cfg = DownloadConfig.from_toml(cfg, config_path)
    dl_cfg.dir.mkdir(parents=True, exist_ok=True)

    # Minimal in-memory job tracker. Not persisted — the authoritative state
    # is the `downloaded: true` flag on the .md once the job completes.
    dl_jobs: dict[str, str] = {}    # bid -> "running" | "done" | "error:<msg>"
    dl_lock = threading.Lock()

    app = FastAPI(title="Booki", description="Bookmark explorer")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
    )
    app.state.config_path = config_path
    app.state.cfg = cfg
    app.state.svc = svc

    # ── routes ───────────────────────────────────────────────────────────────

    # Allow-list of roots from the directory source plugin. Used by the local
    # image proxy below — file:// images can't be loaded directly into an HTTP
    # page, but we can stream them back through this allow-listed endpoint.
    _local_roots: list[Path] = []
    for _d in ((cfg.get("sources", {}) or {}).get("directory", {}) or {}).get("dirs", []) or []:
        _p = (_d or {}).get("path", "")
        if _p:
            try:
                _local_roots.append(Path(_p).expanduser().resolve())
            except (OSError, RuntimeError):
                continue

    _IMG_EXT = {
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".avif",
        ".bmp", ".tiff", ".tif",
        # camera raw
        ".raf", ".cr2", ".cr3", ".nef", ".nrw", ".arw", ".sr2", ".srf",
        ".dng", ".orf", ".rw2", ".pef", ".srw", ".raw", ".x3f", ".iiq",
        ".3fr", ".erf", ".kdc", ".mef", ".mrw", ".rwl",
    }

    @app.get("/api/local-file")
    def local_file(path: str):
        """
        Stream a local image file by absolute path, restricted to:
          1. Roots configured under [[sources.directory.dirs]].
          2. Image extensions only (no generic file serving).
        Used by the Photos tab to preview file:// items the browser blocks.
        """
        if not _local_roots:
            raise HTTPException(404)
        try:
            target = Path(path).expanduser().resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            raise HTTPException(404)
        if not target.is_file():
            raise HTTPException(404)
        if target.suffix.lower() not in _IMG_EXT:
            raise HTTPException(415, "unsupported file type")
        # Containment check: target must live under one of the allow-listed
        # roots (after symlink resolution).
        if not any(_is_within(target, r) for r in _local_roots):
            raise HTTPException(403, "outside allowed roots")
        import mimetypes
        mime, _ = mimetypes.guess_type(str(target))
        return FileResponse(target, media_type=mime or "application/octet-stream")

    @app.get("/api/health")
    def health():
        return {"ok": True, "count": len(svc._index), "bookmarks_dir": str(svc.dir)}

    @app.post("/api/shutdown")
    def shutdown():
        """Trigger a clean uvicorn shutdown.

        Used by the Rust booki-manager's tray menu (Web interface →
        Stop / Restart) so the manager can stop *any* booki web server,
        including ones it didn't spawn itself. The HTTP path is portable
        (no PID hunting) and lets uvicorn drain in-flight requests
        before exiting.

        Implementation: send SIGTERM to our own process from a tiny
        background thread. The current request returns first; uvicorn's
        signal handler then runs the lifespan-shutdown sequence.
        """
        import os, signal, threading
        threading.Thread(
            target=lambda: (time.sleep(0.05), os.kill(os.getpid(), signal.SIGTERM)),
            daemon=True,
        ).start()
        return {"ok": True, "message": "shutting down"}

    @app.get("/api/info")
    def info():
        """Static-ish runtime info (provider names, paths) for the Manage tab."""
        embed = (cfg.get("embeddings", {}) or {})
        llm = (cfg.get("llm", {}) or {})
        web_cfg = (cfg.get("web", {}) or {})
        vec = (cfg.get("vector_db", {}) or {})
        log_path = _resolved_log_path(cfg)
        return {
            "bookmarks_dir": str(svc.dir),
            "vector_db": {
                "type":        vec.get("type", "chromadb"),
                "persist_dir": str((ROOT / vec.get("persist_dir", "./db"))
                                   .resolve()) if not Path(vec.get("persist_dir", "./db")).is_absolute()
                                   else str(Path(vec.get("persist_dir", "./db")).resolve()),
                "collection":  vec.get("collection", "bookmarks"),
            },
            "embeddings": {
                "provider":     embed.get("provider", "local"),
                "local_model":  embed.get("local_model", ""),
                "openai_model": embed.get("openai_model", ""),
            },
            "llm": {
                "provider":  llm.get("provider", ""),
                "model":     llm.get("model", ""),
                "base_url":  llm.get("base_url", ""),
                "n_results": int(llm.get("n_results", 5) or 5),
            },
            "web": {
                "host": web_cfg.get("host", "127.0.0.1"),
                "port": int(web_cfg.get("port", 1000)),
            },
            "logs": {
                "file": str(log_path) if log_path else "",
                "dir":  str(log_path.parent) if log_path else "",
            },
        }

    @app.get("/api/plugins")
    def plugins_list():
        """Enumerate registered plugins for the Manage-tab plugin admin."""
        from plugins.base import iter_tabs as _iter_tabs

        sources = []
        for name, cls in iter_registered():
            available = False
            hint = ""
            try:
                inst = cls()
                inst.configure((cfg.get("sources", {}) or {}).get(name, {}) or {})
                available = bool(inst.is_available())
                if not available:
                    hint = inst.availability_hint() or ""
            except Exception as e:
                hint = f"{type(e).__name__}: {e}"
            sources.append({
                "name":      name,
                "module":    cls.__module__,
                "available": available,
                "hint":      hint,
            })

        enrichers = [{
            "name":     name,
            "module":   cls.__module__,
            "disabled": bool(((cfg.get("enrichers", {}) or {})
                              .get(name, {}) or {}).get("disabled", False)),
        } for name, cls in iter_enrichers()]

        tabs = [{
            "id":     c.id,
            "label":  c.label,
            "icon":   c.icon,
            "order":  c.order,
            "plugin": c.plugin,
            "module": c.module,
        } for _tid, c in _iter_tabs()]

        return {
            "sources":   sources,
            "enrichers": enrichers,
            "tabs":      tabs,
        }

    @app.get("/api/logs")
    def list_logs():
        """List Booki log files in the configured logs dir."""
        log_path = _resolved_log_path(cfg)
        if log_path is None:
            return []
        log_dir = log_path.parent
        if not log_dir.is_dir():
            return []
        base = log_path.name
        out = []
        for p in log_dir.iterdir():
            if not p.is_file():
                continue
            if p.name == base or p.name.startswith(base + "."):
                try:
                    st = p.stat()
                except OSError:
                    continue
                out.append({
                    "name":  p.name,
                    "size":  st.st_size,
                    "mtime": int(st.st_mtime),
                })
        out.sort(key=lambda r: -r["mtime"])
        return out

    @app.get("/api/logs/{name}")
    def get_log(name: str, tail: int = 500):
        """Return the last `tail` lines (≤5000) of one Booki log file."""
        if "/" in name or "\\" in name or ".." in name.split("."):
            raise HTTPException(400, "invalid log name")
        log_path = _resolved_log_path(cfg)
        if log_path is None:
            raise HTTPException(404)
        log_dir = log_path.parent.resolve()
        target = (log_dir / name).resolve()
        if not _is_within(target, log_dir):
            raise HTTPException(403, "outside logs dir")
        if not target.is_file():
            raise HTTPException(404)
        base = log_path.name
        if not (target.name == base or target.name.startswith(base + ".")):
            raise HTTPException(404, "not a Booki log file")
        try:
            tail_n = max(1, min(int(tail), 5000))
        except (TypeError, ValueError):
            tail_n = 500
        st = target.stat()
        return {
            "name":  target.name,
            "size":  st.st_size,
            "mtime": int(st.st_mtime),
            "tail":  tail_n,
            "lines": _tail_lines(target, tail_n),
        }

    @app.get("/api/status")
    def status():
        """System status — installed packages, tools, services."""
        from . import system_status
        return system_status.collect(cfg)

    @app.get("/api/schema")
    def schema():
        """Per-source frontmatter schema — lets the UI render plugin fields."""
        return _collect_schema()

    @app.get("/api/kinds")
    def kinds():
        """Aggregate `kind` slugs declared by every registered plugin.
        Frontend uses this to render kind badges and CLI uses the same
        function for fzf-preview glyphs — adding a new enricher only
        requires declaring `kind_specs()` on it."""
        from plugins.base import all_kind_specs
        return all_kind_specs()

    @app.get("/api/tabs")
    def tabs():
        """
        Plugin-contributed top-level tabs.

        Built-in tabs (Search, Photos, Videos, …) are NOT returned here —
        they're hard-coded in `web/app.js`. Plugin tabs ship a JS module
        (and optionally CSS) under `plugins/<plugin>/web/static/`; the
        frontend bootstrap dynamically `import()`s each module URL after
        attaching declared stylesheets.

        Each module/style URL is suffixed with `?v=<mtime>` so the browser
        re-fetches when a plugin author edits their `tab.js` / `tab.css`.
        """
        def _bust(plugin: str, rel: str) -> str:
            base = f"/plugins/{plugin}/static"
            url = f"{base}/{rel.lstrip('/')}"
            disk = PLUGINS_DIR / plugin / "web/static" / rel.lstrip("/")
            try:
                mtime = int(disk.stat().st_mtime)
                return f"{url}?v={mtime}"
            except OSError:
                return url

        out = []
        for tid, c in iter_tabs():
            module_url = _bust(c.plugin, c.module) if c.module else ""
            style_urls = [_bust(c.plugin, s) for s in c.styles if s]
            out.append({
                "id":         c.id,
                "label":      c.label,
                "icon":       c.icon,
                "order":      c.order,
                "plugin":     c.plugin,
                "module_url": module_url,
                "style_urls": style_urls,
            })
        return out

    @app.get("/api/stats")
    def stats():
        """Aggregate counts + freshness for the sidebar status panel."""
        svc.refresh()
        by_source: dict[str, int] = {}
        by_kind: dict[str, int] = {}
        enriched = 0
        last_sync = ""
        for _, fm in svc._index.values():
            src = str(fm.get("source", "") or "—")
            kind = str(fm.get("kind", "bookmark") or "bookmark")
            by_source[src] = by_source.get(src, 0) + 1
            by_kind[kind] = by_kind.get(kind, 0) + 1
            if str(fm.get("summary", "") or "").strip():
                enriched += 1
            ls = str(fm.get("last_sync", "") or "")
            if ls > last_sync:
                last_sync = ls
        return {
            "total": len(svc._index),
            "enriched": enriched,
            "last_sync": last_sync,
            "by_source": by_source,
            "by_kind": by_kind,
            "bookmarks_dir": str(svc.dir),
        }

    @app.get("/api/bookmarks", response_model=list[Bookmark])
    def list_bookmarks():
        return svc.list()

    @app.get("/api/bookmarks/{bid}", response_model=BookmarkDetail)
    def get_bookmark(bid: str):
        return svc.get(bid)

    @app.put("/api/bookmarks/{bid}", response_model=BookmarkDetail)
    def update_bookmark(bid: str, patch: BookmarkUpdate):
        return svc.update(bid, patch)

    @app.get("/api/lists")
    def list_lists():
        """Regular lists (from frontmatter) + smart lists (from config.toml).

        Smart-list specs are re-read from disk on every call so edits take
        effect on next page refresh — no server restart needed.
        """
        from . import smart_lists as sl_mod
        svc.refresh()
        counts: dict[str, int] = {}
        for _, fm in svc._index.values():
            for name in (fm.get("lists") or []):
                key = str(name).strip()
                if not key:
                    continue
                counts[key] = counts.get(key, 0) + 1
        regular = [{"name": k, "count": v, "smart": False}
                   for k, v in sorted(counts.items(), key=lambda x: (-x[1], x[0].lower()))]

        # Re-read config from disk for hot-reload.
        try:
            live_cfg = load_config(app.state.config_path)
        except Exception:
            live_cfg = app.state.cfg
        smart = sl_mod.parse_smart_lists(live_cfg)
        smart_out = []
        for s in smart:
            count = sum(1 for _, fm in svc._index.values() if sl_mod.matches(fm, s))
            d = s.to_dict()
            d.update({"count": count, "smart": True})
            smart_out.append(d)
        return regular + smart_out

    @app.post("/api/lists/rename")
    def rename_list(req: ListRename):
        old, new = req.old.strip(), req.new.strip()
        if not old or not new:
            raise HTTPException(400, "old and new names are required")
        if old == new:
            return {"renamed": 0}
        svc.refresh()
        changed = 0
        for _, (path, fm) in list(svc._index.items()):
            lists = [str(l) for l in (fm.get("lists") or [])]
            if old not in lists:
                continue
            merged = []
            for l in lists:
                candidate = new if l == old else l
                if candidate not in merged:
                    merged.append(candidate)
            svc.store.update_fields(path, lists=merged, last_sync=today_str())
            changed += 1
        svc.refresh()
        return {"renamed": changed}

    @app.delete("/api/lists/{name}")
    def delete_list(name: str):
        name = name.strip()
        if not name:
            raise HTTPException(400, "list name is required")
        svc.refresh()
        changed = 0
        for _, (path, fm) in list(svc._index.items()):
            lists = [str(l) for l in (fm.get("lists") or [])]
            if name not in lists:
                continue
            svc.store.update_fields(path, lists=[l for l in lists if l != name],
                                    last_sync=today_str())
            changed += 1
        svc.refresh()
        return {"removed_from": changed}

    @app.post("/api/bookmarks/{bid}/download", response_model=DownloadResponse)
    def download_bookmark(bid: str, req: DownloadRequest):
        """Queue a background download. Returns immediately."""
        detail = svc.get(bid)
        if detail.kind != "video":
            return DownloadResponse(queued=False, status="unsupported",
                                    message="Only video items can be downloaded.")

        audio = req.format == "audio"
        with dl_lock:
            if dl_jobs.get(bid) == "running":
                return DownloadResponse(queued=True, status="running")
            dl_jobs[bid] = "running"

        def _run():
            try:
                result = download_one(detail.url, dl_cfg, audio=audio)
                if result.ok:
                    update_md_for_download(svc.store, detail.url, result, dl_cfg)
                    svc.refresh()
                    with dl_lock:
                        dl_jobs[bid] = "done"
                else:
                    with dl_lock:
                        dl_jobs[bid] = f"error:{result.error}"
            except Exception as e:
                with dl_lock:
                    dl_jobs[bid] = f"error:{e}"

        threading.Thread(target=_run, daemon=True).start()
        return DownloadResponse(queued=True, status="running")

    @app.get("/api/bookmarks/{bid}/download", response_model=DownloadResponse)
    def download_status(bid: str):
        with dl_lock:
            state = dl_jobs.get(bid, "")
        if not state:
            # Nothing queued in this process. The .md frontmatter is the truth.
            try:
                d = svc.get(bid)
                if d.extras.get("downloaded"):
                    return DownloadResponse(queued=False, status="done")
            except HTTPException:
                pass
            return DownloadResponse(queued=False, status="")
        if state == "running":
            return DownloadResponse(queued=True, status="running")
        if state == "done":
            return DownloadResponse(queued=False, status="done")
        return DownloadResponse(queued=False, status="error",
                                message=state.removeprefix("error:"))

    exclude_filter = ExcludeFilter.from_cfg(cfg)

    @app.post("/api/link", response_model=LinkAddResponse)
    def add_link(req: LinkAddRequest):
        try:
            path, is_new, title = sync_link(
                req.url, svc.store, title=req.title, exclude=exclude_filter,
            )
        except LinkExcluded as e:
            raise HTTPException(409, f"Excluded — {e.reason}")
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            raise HTTPException(500, f"{type(e).__name__}: {e}")
        svc.refresh()
        fm = svc.store.read_frontmatter(path)
        return LinkAddResponse(id=bm_id(fm), url=str(fm.get("url", "")),
                               title=title, is_new=is_new)

    @app.post("/api/ask", response_model=AskResult)
    def ask(q: AskQuery):
        # Imported lazily — sentence-transformers pulls in ~200MB of deps and
        # we don't want to block app startup for users who only use fuzzy search.
        try:
            from .chat import ask_llm, build_prompt
            from .chat import search as vector_search
        except Exception as e:
            raise HTTPException(500, f"LLM/search deps not installed: {e}")

        try:
            hits = vector_search(q.query, cfg, q.n, q.min_importance)
        except SystemExit as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            raise HTTPException(500, f"Vector search failed: {e}")

        llm_cfg = cfg.get("llm", {})
        result = AskResult(
            bookmarks=hits,
            provider=str(llm_cfg.get("provider", "")),
            model=str(llm_cfg.get("model", "")),
        )
        if not q.use_llm or not hits:
            return result

        try:
            result.answer = ask_llm(build_prompt(q.query, hits), llm_cfg)
        except Exception as e:
            raise HTTPException(502, f"LLM call failed: {e}")
        return result

    # ── export system ────────────────────────────────────────────────────────
    # Wired below by core.exporter.attach_routes().
    from .exporter import attach_routes as _attach_exporter_routes
    _attach_exporter_routes(app, cfg, config_path, svc)

    # ── admin jobs (sync, ingest) ────────────────────────────────────────────
    from .jobs import attach_routes as _attach_job_routes
    _exports_cfg = (cfg.get("exports", {}) or {})
    _exports_root = Path(_exports_cfg.get("dir") or (Path(__file__).resolve().parent.parent / "exports"))
    if not _exports_root.is_absolute():
        _exports_root = (config_path.parent / _exports_root).resolve()
    _attach_job_routes(app, _exports_root, Path(__file__).resolve().parent.parent)

    # ── static UI ────────────────────────────────────────────────────────────

    if dl_cfg.dir.exists():
        app.mount("/downloads", StaticFiles(directory=dl_cfg.dir), name="downloads")

    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

        # Mount each plugin's web/static dir under /plugins/<plugin>/static so
        # the frontend can load tab modules + CSS via dynamic import / <link>.
        _mounted_plugins: set[str] = set()
        for _tid, _contrib in iter_tabs():
            if _contrib.plugin in _mounted_plugins:
                continue
            _static = PLUGINS_DIR / _contrib.plugin / _contrib.static_dir
            # Resolve and re-anchor under PLUGINS_DIR to block `..` traversal.
            try:
                _static_resolved = _static.resolve()
                _static_resolved.relative_to(PLUGINS_DIR.resolve())
            except (ValueError, OSError):
                log.warning("plugin_static_dir_outside_plugins",
                            extra={"plugin": _contrib.plugin, "path": str(_static)})
                continue
            if not _static_resolved.is_dir():
                log.info("plugin_static_dir_missing",
                         extra={"plugin": _contrib.plugin, "path": str(_static_resolved)})
                continue
            app.mount(f"/plugins/{_contrib.plugin}/static",
                      StaticFiles(directory=_static_resolved),
                      name=f"plugin-{_contrib.plugin.replace('/', '-')}")
            _mounted_plugins.add(_contrib.plugin)

        @app.get("/")
        def index():
            idx = WEB_DIR / "index.html"
            if not idx.exists():
                return JSONResponse({"error": "web/index.html missing"}, status_code=500)
            return FileResponse(idx)

    return app


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(description="Serve the Booki web UI.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--reload", action="store_true")
    args = p.parse_args()

    cfg = load_config(args.config)
    web_cfg = cfg.get("web", {})
    host = args.host or web_cfg.get("host", "127.0.0.1")
    port = args.port or int(web_cfg.get("port", 1000))

    import uvicorn
    # log_config=None tells uvicorn not to install its own handlers — our
    # setup_logging() in create_app already routes uvicorn.* loggers through
    # the configured root handlers.
    if args.reload:
        uvicorn.run("core.web:create_app", factory=True, host=host, port=port,
                    reload=True, log_config=None)
    else:
        uvicorn.run(create_app(args.config), host=host, port=port, log_config=None)


app = None  # filled in lazily when imported by uvicorn --factory


if __name__ == "__main__":
    main()
