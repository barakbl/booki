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
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

# Reuse parsers / writers from the CLI tools.
from .ingest import FRONTMATTER_RE, bm_id, parse_bookmark_file
from .loader import LoadError, scan_bookmarks
from .store import ItemStore, today_str, view_fm
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
    """Only these fields may be edited from the UI.

    Caps are intentionally loose enough that no real user edit hits them
    but tight enough that a single PUT can't bloat the on-disk MD or the
    in-memory index (`svc.refresh()` re-reads every file). A 10 MB notes
    field would otherwise wedge every subsequent list call.
    """
    title:      Optional[str]       = Field(default=None, max_length=500)
    importance: Optional[int]       = Field(default=None, ge=0, le=10)
    tags:       Optional[list[str]] = Field(default=None, max_length=64)
    lists:      Optional[list[str]] = Field(default=None, max_length=64)
    notes:      Optional[str]       = Field(default=None, max_length=20_000)
    summary:    Optional[str]       = Field(default=None, max_length=5_000)
    keywords:   Optional[list[str]] = Field(default=None, max_length=64)

    @field_validator("tags", "lists", "keywords")
    @classmethod
    def _cap_string_items(cls, v):
        # Cap each entry at 200 chars — `max_length` on the list only
        # bounds the count, not individual element length.
        if v is None:
            return v
        return [str(x)[:200] for x in v]


class ListRename(BaseModel):
    old: str
    new: str


class AskQuery(BaseModel):
    # Hard caps on the request shape so an attacker that reaches /api/ask
    # (e.g. via a malicious page if the server is bound off-loopback) can't
    # blow remote-LLM token budgets or DoS the chroma collection.
    query: str = Field(min_length=1, max_length=2000)
    n: int = Field(default=5, ge=1, le=50)
    min_importance: int = Field(default=0, ge=0, le=10)
    use_llm: bool = True


class LinkAddRequest(BaseModel):
    # Bound URL + title at the request edge so a hostile / careless
    # caller can't push a 1 MB URL through the title-fetch path. (P1-04)
    url: str = Field(min_length=1, max_length=4096)
    title: Optional[str] = Field(default=None, max_length=500)


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
    # Where the *query string* gets embedded. When this is anything other
    # than "local", the user's query leaves the machine on every Ask —
    # even with `use_llm: false`. Surfaced so the UI can disclose it.
    embeddings_provider: str = "local"


# ─── Bookmark service ─────────────────────────────────────────────────────────

class BookmarkService:
    """
    In-memory index of all bookmark MD files, keyed by id (sha256(url)[:16]).

    `refresh()` is the only entry point that touches disk. It uses an
    mtime fingerprint — a sorted list of `(path, mtime)` for every `.md`
    under the bookmarks dir — so repeated calls in a single page load
    (search list, stats, library errors, …) only do the cheap
    `stat`-each-file walk; the parse step is skipped when nothing has
    changed. External edits (a hand-tweaked .md, a CLI `booki sync`)
    bump file mtimes and are therefore picked up immediately on the
    next refresh.
    """

    def __init__(self, bookmarks_dir: Path):
        self.dir = bookmarks_dir
        self.store = ItemStore(bookmarks_dir)
        self._index: dict[str, tuple[Path, dict]] = {}
        # Errors from the most recent scan — broken files, schema mismatches.
        # Surfaced via /api/library/errors and rendered as a banner in the
        # search header so users notice files quietly disappearing from the
        # index instead of just seeing a smaller count.
        self._errors: list[LoadError] = []
        self._scanned: int = 0
        # Sorted list of `(path_str, mtime)` from the last full scan.
        # `None` forces the next refresh to do a full re-parse (used after
        # writes to bypass the no-change shortcut).
        self._fingerprint: Optional[list[tuple[str, float]]] = None
        self.refresh()

    def _fingerprint_dir(self) -> list[tuple[str, float]]:
        """Walk the bookmarks dir and return a sorted (path, mtime) list.

        One `stat` per `.md` file — no reads, no parsing. For a few
        thousand files this is sub-millisecond on SSD."""
        if not self.dir.exists():
            return []
        out: list[tuple[str, float]] = []
        for p in sorted(self.dir.rglob("*.md")):
            try:
                out.append((str(p), p.stat().st_mtime))
            except OSError:
                # Race with deletion or a permission flip — drop from the
                # fingerprint and let the next refresh notice when stable.
                continue
        return out

    def refresh(self, *, force: bool = False) -> None:
        """Sync the in-memory index with disk.

        Cheap when nothing has changed: walks each `.md` for an mtime
        fingerprint and short-circuits when it matches the cached one.
        Pass `force=True` after a write that bypasses mtime granularity
        (e.g. two writes within the same filesystem-mtime tick).
        """
        if not self.dir.exists():
            self._index = {}
            self._errors = []
            self._scanned = 0
            self._fingerprint = []
            return

        fp = self._fingerprint_dir()
        if not force and self._fingerprint is not None and fp == self._fingerprint:
            # No file changed since last full parse — reuse the index.
            return

        # Something changed (or first run / forced) — re-parse.
        scan = scan_bookmarks(self.dir, paths=[Path(p) for p, _ in fp])
        new_index: dict[str, tuple[Path, dict]] = {}
        for path, raw_fm in scan.items:
            fm = view_fm(raw_fm)
            if not fm.get("url"):
                continue
            new_index[bm_id(fm)] = (path, fm)
        self._index = new_index
        self._errors = scan.errors
        self._scanned = scan.scanned
        self._fingerprint = fp

    def invalidate(self) -> None:
        """Drop the cached fingerprint so the next refresh re-parses.

        Use after a same-process write whose mtime change might collide
        with the cached fingerprint (rare, but possible when a write
        finishes inside the same filesystem-mtime tick as the prior scan).
        """
        self._fingerprint = None

    def errors(self) -> list[LoadError]:
        return list(self._errors)

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

        # Manual edits land in the nested `user:` override block — the
        # authoritative top-level field (set by sources / enrichers) stays
        # intact underneath, and `view_fm` overlays the user value at read
        # time. `last_sync` is tracking source freshness, not user edits,
        # so it stays at the top level.
        self.store.update_user_fields(path, **updates)
        self.store.update_fields(path, last_sync=today_str())
        # Same-process writes can land within one filesystem-mtime tick of
        # the prior scan; force the next refresh to actually re-parse.
        self.refresh(force=True)
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
        cfg = tomllib.load(f)
    _validate_config_security_posture(cfg)
    return cfg


def _validate_config_security_posture(cfg: dict) -> None:
    """Refuse obviously-unsafe config. Logs warnings for risky-but-allowed
    combinations so operators see them on every server start.

    Specifically:
      - `[web].host = 0.0.0.0` (or any non-loopback) without a configured
        auth token → log a loud WARNING. (P5-01)
      - `[web].cors_origins = ["*"]` → log WARNING (we still honour it
        if explicitly set, but the user should see it on every start).
        (P5-02)
      - `[embeddings].openai_api_key` set inline → log WARNING and
        recommend the env var. (P5-09)
    """
    web_cfg = (cfg.get("web") or {})
    host = str(web_cfg.get("host", "127.0.0.1")).lower()
    is_loopback = host in ("127.0.0.1", "localhost", "::1", "")
    if not is_loopback:
        log.warning(
            "lan_bind_no_auth",
            extra={
                "host": host,
                "msg": ("Booki has no built-in authentication — binding to a "
                        "non-loopback host exposes every /api/* route, "
                        "including bookmark exfiltration via /api/ask, to "
                        "anyone who can reach this address. (P5-01)"),
            },
        )

    cors = web_cfg.get("cors_origins") or []
    if any(str(o).strip() == "*" for o in cors):
        log.warning(
            "cors_wildcard_enabled",
            extra={"msg": "[web].cors_origins includes '*' — every origin "
                          "can read /api/* responses. (P5-02)"},
        )

    embeddings = (cfg.get("embeddings") or {})
    if str(embeddings.get("openai_api_key", "")).strip():
        log.warning(
            "openai_api_key_inline",
            extra={"msg": ("[embeddings].openai_api_key is set inline in "
                          "config.toml. Prefer the OPENAI_API_KEY env var "
                          "so the key doesn't end up in backups / shell "
                          "history / accidental git pushes. (P5-09)")},
        )


def _is_within(target: Path, root: Path) -> bool:
    """True if `target` is `root` or a descendant of it (both already resolved)."""
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_runtime_token_path(port: int) -> Path:
    """Pick a writable, user-private location for the shutdown token.

    Order of preference:
      1. $XDG_RUNTIME_DIR/booki/shutdown-<port>.token  — Linux session dir
         (typically /run/user/$UID, already 0700).
      2. ~/.cache/booki/shutdown-<port>.token         — XDG fallback.
      3. tempfile.gettempdir()/booki-<uid>-shutdown-<port>.token
         — last resort (still chmod 0600 by caller).
    """
    import getpass
    import os as _os
    import tempfile

    name = f"shutdown-{int(port)}.token"
    runtime_dir = _os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if runtime_dir:
        try:
            p = Path(runtime_dir) / "booki" / name
            return p
        except (OSError, ValueError):
            pass
    cache_home = _os.environ.get("XDG_CACHE_HOME", "").strip() or str(Path.home() / ".cache")
    try:
        return Path(cache_home) / "booki" / name
    except (OSError, ValueError):
        pass
    user = getpass.getuser() if hasattr(getpass, "getuser") else "anon"
    return Path(tempfile.gettempdir()) / f"booki-{user}-{name}"


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

    # CORS: default to same-origin only (the configured host/port plus the
    # localhost loopback variants on that port). Users can extend or relax
    # this via `[web].cors_origins` — set to ["*"] to disable origin checks.
    web_cfg = cfg.get("web", {}) or {}
    web_host = str(web_cfg.get("host", "127.0.0.1"))
    web_port = int(web_cfg.get("port", 8765))
    configured = [str(o).strip().rstrip("/")
                  for o in (web_cfg.get("cors_origins") or [])
                  if str(o).strip()]
    if "*" in configured:
        cors_kwargs: dict[str, Any] = {"allow_origins": ["*"]}
    else:
        default_origins = {
            f"http://localhost:{web_port}",
            f"http://127.0.0.1:{web_port}",
        }
        if web_host not in ("0.0.0.0", "127.0.0.1", "localhost", "::", ""):
            default_origins.add(f"http://{web_host}:{web_port}")
        cors_kwargs = {
            "allow_origins": sorted(default_origins | set(configured)),
            "allow_credentials": True,
        }
    app.add_middleware(
        CORSMiddleware,
        allow_methods=["*"],
        allow_headers=["*"],
        **cors_kwargs,
    )

    # Host header allow-list — stops DNS-rebinding attacks. CORS only protects
    # against cross-origin *reads*; a malicious page can rebind its own
    # hostname to 127.0.0.1, then the browser treats requests to localhost as
    # same-origin (no CORS preflight) but sends the original `Host:
    # attacker.example` header. Without this middleware, those requests would
    # reach /api/ask and exfiltrate bookmarks. Users can add custom names via
    # [web].allowed_hosts; "*" disables the check.
    allowed_hosts_cfg = [str(h).strip() for h in (web_cfg.get("allowed_hosts") or [])
                         if str(h).strip()]
    if "*" in allowed_hosts_cfg:
        allowed_hosts: list[str] = ["*"]
    else:
        allowed_hosts = sorted({
            "localhost", "127.0.0.1", "[::1]",
            f"localhost:{web_port}", f"127.0.0.1:{web_port}", f"[::1]:{web_port}",
            *([web_host, f"{web_host}:{web_port}"]
              if web_host and web_host not in ("0.0.0.0", "::", "") else []),
            *allowed_hosts_cfg,
        })
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    app.state.config_path = config_path
    app.state.cfg = cfg
    app.state.svc = svc

    # Disclose remote embedding once at boot — easy to miss that toggling off
    # the LLM doesn't keep the query string local when `embeddings.provider`
    # is OpenAI.
    em_provider = str(cfg.get("embeddings", {}).get("provider", "local")).lower()
    if em_provider and em_provider != "local":
        log.warning("remote_embeddings_active",
                    extra={"provider": em_provider})
        print(f"  [ask] embeddings.provider = {em_provider!r} — every Ask "
              f"query is sent to {em_provider} for embedding, even with "
              f"'use_llm: false'.")

    # ── routes ───────────────────────────────────────────────────────────────

    # Allow-list of roots from the directory source plugin. Used by the local
    # image proxy below — file:// images can't be loaded directly into an HTTP
    # page, but we can stream them back through this allow-listed endpoint.
    from .local_files import directory_roots, safe_local_path
    _local_roots = directory_roots(cfg)

    _IMG_EXT = {
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".avif",
        ".bmp", ".tiff", ".tif",
        # camera raw
        ".raf", ".cr2", ".cr3", ".nef", ".nrw", ".arw", ".sr2", ".srf",
        ".dng", ".orf", ".rw2", ".pef", ".srw", ".raw", ".x3f", ".iiq",
        ".3fr", ".erf", ".kdc", ".mef", ".mrw", ".rwl",
    }

    # Favicon proxy — serves the favicon for `domain` via the
    # SSRF-gated _safe_get path. Lets the frontend display favicons
    # without leaking every bookmark's hostname to Google. Cache in
    # memory for a short TTL so repeated lookups don't repeat fetch.
    # (P4-02)
    _favicon_cache: dict[str, tuple[float, bytes, str]] = {}
    _favicon_lock = threading.Lock()
    _FAVICON_TTL = 12 * 3600.0  # 12h
    _FAVICON_DOMAIN_RE = __import__("re").compile(r"^[A-Za-z0-9.\-]{1,253}$")

    @app.get("/api/favicon")
    def favicon_proxy(domain: str):
        if not _FAVICON_DOMAIN_RE.match(domain or ""):
            raise HTTPException(400, "invalid domain")
        domain = domain.lower()
        now = time.monotonic()
        with _favicon_lock:
            entry = _favicon_cache.get(domain)
        if entry and entry[0] > now:
            _, blob, mime = entry
            return Response(content=blob, media_type=mime,
                            headers={"Cache-Control": "public, max-age=43200"})
        from .url_safety import safe_get
        url = f"https://{domain}/favicon.ico"
        r = safe_get(url, timeout=4,
                     headers={"User-Agent": "booki/1.0 favicon-proxy"},
                     max_bytes=256 * 1024)
        if r is None or not r.ok or not r.content:
            raise HTTPException(404, "favicon not found")
        mime = (r.headers.get("Content-Type") or "image/x-icon").split(";")[0].strip()
        if not mime.startswith("image/"):
            mime = "image/x-icon"
        with _favicon_lock:
            _favicon_cache[domain] = (now + _FAVICON_TTL, r.content, mime)
        return Response(content=r.content, media_type=mime,
                        headers={"Cache-Control": "public, max-age=43200"})

    # Video-thumbnail proxy — same shape as the favicon proxy. Lets the UI
    # render YouTube / Vimeo poster frames without an `img-src https://*.ytimg.com`
    # CSP allowance and without leaking the user's IP / Referer to the CDN
    # for every bookmarked video. Host allowlist keeps this from becoming
    # a generic image-fetch open relay; safe_get covers SSRF on top.
    _thumb_cache: dict[str, tuple[float, bytes, str]] = {}
    _thumb_lock = threading.Lock()
    _THUMB_TTL = 12 * 3600.0  # 12h
    # Exact hosts + suffix-matched parent domains. Suffix matching covers
    # YouTube's load-balanced thumbnail CDN (`i.ytimg.com`, `i9.ytimg.com`,
    # `i123.ytimg.com`, …) without allowing `ytimg.com.attacker.example`.
    _THUMB_HOST_EXACT = frozenset({
        "img.youtube.com",
        "yt3.ggpht.com",
        "yt3.googleusercontent.com",
        "i.vimeocdn.com",
    })
    _THUMB_HOST_SUFFIX = (".ytimg.com",)

    @app.get("/api/video-thumbnail")
    def video_thumbnail_proxy(url: str):
        from urllib.parse import urlparse
        try:
            parsed = urlparse(url)
        except ValueError:
            raise HTTPException(400, "invalid url")
        if parsed.scheme not in ("http", "https"):
            raise HTTPException(400, "scheme not allowed")
        host = (parsed.hostname or "").lower()
        ok = host in _THUMB_HOST_EXACT or any(
            host.endswith(s) for s in _THUMB_HOST_SUFFIX
        )
        if not ok:
            raise HTTPException(400, "host not allowed")
        now = time.monotonic()
        with _thumb_lock:
            entry = _thumb_cache.get(url)
        if entry and entry[0] > now:
            _, blob, mime = entry
            return Response(content=blob, media_type=mime,
                            headers={"Cache-Control": "public, max-age=43200"})
        from .url_safety import safe_get
        r = safe_get(url, timeout=6,
                     headers={"User-Agent": "booki/1.0 video-thumb-proxy"},
                     max_bytes=2 * 1024 * 1024)
        if r is None or not r.ok or not r.content:
            raise HTTPException(404, "thumbnail not found")
        mime = (r.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
        if not mime.startswith("image/"):
            mime = "image/jpeg"
        with _thumb_lock:
            _thumb_cache[url] = (now + _THUMB_TTL, r.content, mime)
        return Response(content=r.content, media_type=mime,
                        headers={"Cache-Control": "public, max-age=43200"})

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
        target = safe_local_path(path, _local_roots)
        if target is None:
            raise HTTPException(404)
        if target.suffix.lower() not in _IMG_EXT:
            raise HTTPException(415, "unsupported file type")
        import mimetypes
        mime, _ = mimetypes.guess_type(str(target))
        return FileResponse(target, media_type=mime or "application/octet-stream")

    @app.get("/api/health")
    def health():
        return {"ok": True, "count": len(svc._index), "bookmarks_dir": str(svc.dir)}

    # Per-process shutdown token. Generated at create_app() time and
    # written to a 0600 file under XDG_RUNTIME_DIR (or $TMPDIR / /tmp
    # as a fallback). The booki-manager tray app reads it from the
    # same file. Unauthenticated requests get 401 — without this any
    # local process (browser extension, curl, malicious VS Code task)
    # could SIGTERM the server. (P1-05)
    import secrets as _secrets
    _shutdown_token = _secrets.token_urlsafe(32)
    _token_path = _resolve_runtime_token_path(web_port)
    try:
        _token_path.parent.mkdir(parents=True, exist_ok=True)
        _token_path.write_text(_shutdown_token, encoding="utf-8")
        try:
            import os as _os
            _os.chmod(_token_path, 0o600)
        except OSError:
            pass
        log.info("shutdown_token_written", extra={"path": str(_token_path)})
    except OSError:
        log.warning("shutdown_token_write_failed",
                    extra={"path": str(_token_path)})

    from fastapi import Header

    @app.post("/api/shutdown")
    def shutdown(authorization: Optional[str] = Header(default=None)):
        """Trigger a clean uvicorn shutdown.

        Caller must present `Authorization: Bearer <token>` matching the
        per-process token written to the runtime token file. The
        booki-manager reads this file when it's started by the same
        user; nobody else should have read access (chmod 0600).
        """
        expected = f"Bearer {_shutdown_token}"
        if not authorization or not _secrets.compare_digest(authorization, expected):
            raise HTTPException(401, "Unauthorized — present the per-process shutdown token.")
        import os, signal, threading
        threading.Thread(
            target=lambda: (time.sleep(0.05), os.kill(os.getpid(), signal.SIGTERM)),
            daemon=True,
        ).start()
        return {"ok": True, "message": "shutting down"}

    @app.get("/api/info")
    def info(detail: bool = False):
        """Static-ish runtime info for the Manage tab.

        By default we strip absolute paths and remote-endpoint URLs to
        their basenames — even on a localhost-only deployment those
        details make lateral movement easier if the page is ever framed
        or the response sniffed via a future browser-side hole. Set
        `?detail=true` to opt back in to the full picture. (P1-07)
        """
        embed = (cfg.get("embeddings", {}) or {})
        llm = (cfg.get("llm", {}) or {})
        web_cfg = (cfg.get("web", {}) or {})
        vec = (cfg.get("vector_db", {}) or {})
        log_path = _resolved_log_path(cfg)
        from .ingest import chromadb_installed, vector_db_enabled
        vec_enabled = vector_db_enabled(cfg)
        vec_installed = chromadb_installed()

        def _maybe_path(p: str | Path | None) -> str:
            if not p:
                return ""
            sp = Path(str(p)).expanduser()
            return str(sp) if detail else sp.name

        def _maybe_url(u: str) -> str:
            if not u or detail:
                return str(u or "")
            try:
                from urllib.parse import urlparse
                parsed = urlparse(str(u))
                return f"{parsed.scheme}://{parsed.hostname or ''}" if parsed.scheme else ""
            except Exception:
                return ""

        persist_raw = vec.get("persist_dir", "./db")
        persist = (ROOT / persist_raw) if not Path(persist_raw).is_absolute() else Path(persist_raw)
        return {
            "bookmarks_dir": _maybe_path(svc.dir),
            "vector_db": {
                "type":        vec.get("type", "chromadb"),
                "persist_dir": _maybe_path(persist.resolve()),
                "collection":  vec.get("collection", "bookmarks"),
                "enabled":     vec_enabled,
                "installed":   vec_installed,
                "available":   vec_enabled and vec_installed,
            },
            "embeddings": {
                "provider":     embed.get("provider", "local"),
                "local_model":  embed.get("local_model", ""),
                "openai_model": embed.get("openai_model", ""),
            },
            "llm": {
                "provider":  llm.get("provider", ""),
                "model":     llm.get("model", ""),
                "base_url":  _maybe_url(llm.get("base_url", "")),
                "n_results": int(llm.get("n_results", 5) or 5),
            },
            "web": {
                "host": web_cfg.get("host", "127.0.0.1"),
                "port": int(web_cfg.get("port", 8765)),
                "favicon_provider": str(web_cfg.get("favicon_provider", "none")),
            },
            "logs": {
                "file": _maybe_path(log_path),
                "dir":  _maybe_path(log_path.parent) if log_path else "",
            },
        }

    # Cache /api/plugins for ~30 s to bound the cost of a polling client.
    # Each call instantiates every source plugin and runs is_available()
    # — which can do real work (read disk, contact an API). Without the
    # cache, a malicious page (or a hot-loaded developer dashboard) hammers
    # them at request rate. (P3-04)
    _plugins_cache: dict[str, Any] = {"value": None, "expires": 0.0}
    _plugins_cache_ttl = 30.0

    @app.get("/api/plugins")
    def plugins_list(refresh: bool = False):
        """Enumerate registered plugins for the Manage-tab plugin admin."""
        from plugins.base import iter_tabs as _iter_tabs

        now = time.monotonic()
        if not refresh and _plugins_cache["value"] is not None and now < _plugins_cache["expires"]:
            return _plugins_cache["value"]

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

        result = {
            "sources":   sources,
            "enrichers": enrichers,
            "tabs":      tabs,
        }
        _plugins_cache["value"] = result
        _plugins_cache["expires"] = now + _plugins_cache_ttl
        return result

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
        # Path-traversal guard: refuse anything containing a separator,
        # a NUL, or a `..` segment. The realpath/within check below is
        # the load-bearing defence; this is just early input rejection.
        if (
            "/" in name
            or "\\" in name
            or "\x00" in name
            or ".." in name
        ):
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
    def status(detail: bool = False):
        """System status — installed packages, tools, services.

        `?detail=true` runs the `--version` probe per binary (P3-05).
        Default off so a casual hit doesn't fork shell commands.
        """
        from . import system_status
        return system_status.collect(cfg, deep=detail)

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
        # Bubble up corrupted-file counts so the search UI can render a
        # banner without a separate request — `errors_count` is the number
        # of *files* skipped, not the raw error count.
        errs = svc.errors()
        skipped_paths = {e.path for e in errs if e.kind != "schema"}
        schema_paths = {e.path for e in errs if e.kind == "schema"}
        return {
            "total": len(svc._index),
            "enriched": enriched,
            "last_sync": last_sync,
            "by_source": by_source,
            "by_kind": by_kind,
            "bookmarks_dir": str(svc.dir),
            "scanned": svc._scanned,
            "errors_count": len(skipped_paths),
            "schema_warnings_count": len(schema_paths),
        }

    @app.get("/api/library/errors")
    def library_errors():
        """List every parse/schema problem from the most recent scan.

        Each entry is the serialised `LoadError` plus a `rel_path` for
        display. The frontend renders this in the Manage > Doctor tab so
        users can click through and fix corrupted files.
        """
        svc.refresh()
        out: list[dict] = []
        for e in svc.errors():
            d = e.to_dict()
            try:
                d["rel_path"] = str(Path(e.path).relative_to(svc.dir))
            except ValueError:
                d["rel_path"] = e.path
            out.append(d)
        # Sort: blocking errors first (they actually break the index),
        # then schema warnings, then by path for stable ordering.
        out.sort(key=lambda d: (d["kind"] == "schema", d["rel_path"]))
        return {
            "scanned": svc._scanned,
            "skipped": len({e.path for e in svc.errors() if e.kind != "schema"}),
            "schema_warnings": len({e.path for e in svc.errors() if e.kind == "schema"}),
            "errors": out,
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
            svc.store.update_user_fields(path, lists=merged)
            svc.store.update_fields(path, last_sync=today_str())
            changed += 1
        svc.refresh(force=True)
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
            svc.store.update_user_fields(path, lists=[l for l in lists if l != name])
            svc.store.update_fields(path, last_sync=today_str())
            changed += 1
        svc.refresh(force=True)
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
                    svc.refresh(force=True)
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

    _security_cfg = (cfg.get("security", {}) or {})
    _allow_internal = bool(_security_cfg.get("allow_internal_targets", False))

    @app.post("/api/link", response_model=LinkAddResponse)
    def add_link(req: LinkAddRequest):
        try:
            path, is_new, title = sync_link(
                req.url, svc.store, title=req.title, exclude=exclude_filter,
                allow_internal_targets=_allow_internal,
            )
        except LinkExcluded as e:
            raise HTTPException(409, f"Excluded — {e.reason}")
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception:
            # Don't echo internal exception text to the client — it's
            # the most common path for filesystem layout / auth-layer
            # detail to leak. The full traceback is in the server log.
            # (P1-08)
            log.exception("api_link_failed", extra={"url": req.url[:200]})
            raise HTTPException(500, "Failed to add link — see server logs.")
        svc.refresh(force=True)
        fm = svc.store.read_frontmatter(path)
        return LinkAddResponse(id=bm_id(fm), url=str(fm.get("url", "")),
                               title=title, is_new=is_new)

    @app.post("/api/ask", response_model=AskResult)
    def ask(q: AskQuery):
        # Vector search is opt-in twice: by config flag AND by chromadb
        # being installed. Surface 503 with a precise reason so the UI can
        # render "disabled" rather than a 500 stack trace.
        from .ingest import chromadb_installed, vector_db_enabled
        if not vector_db_enabled(cfg):
            raise HTTPException(503, "Vector search disabled in config.toml — "
                                     "set [vector_db] enabled = true to re-enable.")
        if not chromadb_installed():
            raise HTTPException(503, "Vector search disabled — install chromadb "
                                     "(pip install 'chromadb>=0.5.0' "
                                     "'sentence-transformers>=2.2.0') and run "
                                     "`booki ingest` to enable Ask.")
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
            # SystemExit messages here are pre-canned, user-facing strings
            # (e.g. "Collection not found — run: booki ingest") so it's
            # safe to pass them through.
            raise HTTPException(400, str(e))
        except Exception:
            log.exception("ask_vector_search_failed")
            raise HTTPException(500, "Vector search failed — see server logs.")

        llm_cfg = cfg.get("llm", {})
        result = AskResult(
            bookmarks=hits,
            provider=str(llm_cfg.get("provider", "")),
            model=str(llm_cfg.get("model", "")),
            embeddings_provider=str(cfg.get("embeddings", {}).get("provider", "local")),
        )
        if not q.use_llm or not hits:
            return result

        try:
            result.answer = ask_llm(build_prompt(q.query, hits), llm_cfg)
        except Exception:
            log.exception("ask_llm_failed",
                          extra={"provider": str(llm_cfg.get("provider", ""))})
            raise HTTPException(502, "LLM call failed — see server logs.")
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
    port = args.port or int(web_cfg.get("port", 8765))

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
