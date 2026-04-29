"""
plugins.exporters.offline_archive — bundle selected bookmarks as an offline ZIP.

For each item we attempt to materialize a local artifact:
  • monolith → page.html  (single self-contained HTML for articles/web pages)
  • yt-dlp   → video.mp4  (+ subs, thumbnail) for YouTube and similar sites
  • requests → file.pdf   (raw save when the URL ends in .pdf)

A landing index.html (cappuccino theme) sits at the archive root and links to
each local artifact, so unzipping the archive yields a fully browsable offline
site. Per-item metadata + status live in manifest.json plus items/<slug>/meta.json.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import unicodedata
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from ...base import ExportOption, ExportResult, Exporter, register_exporter


# Hosts we route to yt-dlp by default even when the item's `kind` isn't "video".
# yt-dlp itself supports ~1000 sites; this is just the fast-path detector.
VIDEO_HOST_RE = re.compile(
    r"(?i)(?:^|\.)("
    r"youtube\.com|youtu\.be|vimeo\.com|tiktok\.com|twitch\.tv|"
    r"dailymotion\.com|facebook\.com|instagram\.com|twitter\.com|x\.com|"
    r"reddit\.com|soundcloud\.com|bandcamp\.com"
    r")$"
)


@register_exporter
class OfflineArchiveExporter(Exporter):
    name        = "offline_archive"
    label       = "Offline archive (ZIP)"
    description = "Self-contained ZIP — monolith-saved pages, downloaded videos, and a browsable index.html."
    supports_themes = True
    default_theme  = "default"

    def options(self) -> list[ExportOption]:
        return [
            ExportOption("title", "Page title", "string",
                         default="My Bookmarks (offline)"),
            ExportOption("group_by", "Group by", "select",
                         default="none",
                         choices=["none", "source", "kind", "tag"]),
            ExportOption("include_summary", "Include summaries", "bool", default=True),
            ExportOption("include_tags",    "Include tags",      "bool", default=True),
            ExportOption("include_notes",   "Include notes",     "bool", default=False),
            ExportOption("workers", "Parallel downloads", "number", default=4,
                         help="How many items to download concurrently."),
            ExportOption("max_per_item_mb",
                         "Per-item size cap (MB, 0 = unlimited)", "number", default=500,
                         help="Refuse any single artifact above this size."),
            ExportOption("reuse_cache", "Reuse already-downloaded videos",
                         "bool", default=True,
                         help="Copy from ./downloads/ when an item already has download_path set."),
            ExportOption("page_timeout_s", "Page-archive timeout (seconds)",
                         "number", default=45),
        ]

    def export(self, items, *, theme: Optional[str], options: dict, out_dir: Path) -> ExportResult:
        from jinja2 import Environment, FileSystemLoader, select_autoescape

        # ─── Theme + workspace ─────────────────────────────────────────────
        theme_name   = theme or self.default_theme or "default"
        builtin_root = Path(__file__).parent / "themes"
        user_root    = (Path(options.get("_themes_root") or "") / self.name
                        if options.get("_themes_root") else None)

        search_paths: list[str] = []
        if user_root and (user_root / theme_name).exists():
            search_paths.append(str(user_root / theme_name))
        search_paths.append(str(builtin_root / theme_name))
        if not (builtin_root / theme_name).exists():
            search_paths.append(str(builtin_root / "default"))

        env = Environment(
            loader=FileSystemLoader(search_paths),
            autoescape=select_autoescape(["html", "htm", "j2"]),
            trim_blocks=True, lstrip_blocks=True,
        )
        tpl = env.get_template("main.html.j2")

        out_dir.mkdir(parents=True, exist_ok=True)
        stage = out_dir / "_stage"
        if stage.exists():
            shutil.rmtree(stage)
        stage.mkdir(parents=True)
        items_root = stage / "items"
        items_root.mkdir()

        # ─── Resolve knobs ─────────────────────────────────────────────────
        workers      = max(1, int(options.get("workers", 4) or 4))
        cap_mb       = int(options.get("max_per_item_mb", 500) or 0)
        cap_bytes    = cap_mb * 1024 * 1024 if cap_mb > 0 else 0
        reuse_cache  = bool(options.get("reuse_cache", True))
        page_timeout = int(options.get("page_timeout_s", 45) or 45)
        dl_cfg       = _load_download_config()
        monolith_bin = shutil.which("monolith")
        ytdlp_ok     = _has_ytdlp()

        # ─── Plan jobs (per-item folders are created here, work runs below) ─
        jobs: list[dict] = []
        for idx, it in enumerate(items, start=1):
            url      = (it.get("url") or "").strip()
            title    = it.get("title") or url or f"item-{idx:03d}"
            slug     = _slugify(title) or f"item-{idx:03d}"
            folder   = items_root / f"{idx:03d}-{slug}"
            folder.mkdir(exist_ok=True)
            strategy = _decide_strategy(it, url)
            jobs.append({
                "idx": idx, "item": it, "url": url, "title": title,
                "slug": slug, "folder": folder,
                "rel": f"items/{idx:03d}-{slug}",
                "strategy": strategy,
            })

        # ─── Run downloads in parallel (continue on per-item failures) ─────
        results: dict[int, dict] = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {
                pool.submit(_run_job, j, dl_cfg, monolith_bin, ytdlp_ok,
                            cap_bytes, reuse_cache, page_timeout): j
                for j in jobs
            }
            for fut in as_completed(futs):
                j = futs[fut]
                try:
                    results[j["idx"]] = fut.result()
                except Exception as e:
                    results[j["idx"]] = _fail(j["strategy"],
                                              f"{type(e).__name__}: {e}")

        # ─── Per-item meta.json + augment items for the index render ───────
        rendered = []
        for j in jobs:
            r  = results[j["idx"]]
            it = dict(j["item"])  # shallow copy — we annotate locally
            it["_local_href"]  = (j["rel"] + "/" + r["artifact_rel"]) if r.get("artifact_rel") else None
            it["_local_kind"]  = r.get("kind") or j["strategy"]
            it["_local_size"]  = r.get("size") or 0
            it["_local_size_h"] = _human_bytes(r.get("size") or 0)
            it["_local_ok"]    = bool(r.get("ok"))
            it["_local_error"] = r.get("error") or ""
            it["_local_cached"] = bool(r.get("from_cache"))
            (j["folder"] / "meta.json").write_text(
                json.dumps({
                    "title": j["title"], "url": j["url"],
                    "source": j["item"].get("source"),
                    "kind":   j["item"].get("kind"),
                    "result": r,
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            rendered.append(it)

        # ─── Render landing index.html from theme ──────────────────────────
        groups = _group_items(rendered, options.get("group_by", "none"))
        html = tpl.render(
            title=options.get("title") or "My Bookmarks (offline)",
            groups=groups,
            item_count=len(rendered),
            include_summary=bool(options.get("include_summary", True)),
            include_tags   =bool(options.get("include_tags", True)),
            include_notes  =bool(options.get("include_notes", False)),
            tools_status   ={"monolith": bool(monolith_bin), "yt_dlp": ytdlp_ok},
        )
        (stage / "index.html").write_text(html, encoding="utf-8")
        _copy_static(Path(search_paths[0]), stage)

        # ─── Top-level manifest.json ───────────────────────────────────────
        total_bytes = sum((r.get("size") or 0) for r in results.values())
        ok_n        = sum(1 for r in results.values() if r.get("ok"))
        manifest = {
            "title":        options.get("title") or "My Bookmarks (offline)",
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tools":        {"monolith": bool(monolith_bin), "yt_dlp": ytdlp_ok},
            "stats": {
                "total":       len(jobs),
                "ok":          ok_n,
                "failed":      len(jobs) - ok_n,
                "total_bytes": total_bytes,
            },
            "items": [
                {"idx": j["idx"], "url": j["url"], "title": j["title"],
                 "folder": j["rel"], **results[j["idx"]]}
                for j in jobs
            ],
        }
        (stage / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        # ─── Zip the staged tree ───────────────────────────────────────────
        zip_path = out_dir / "archive.zip"
        if zip_path.exists():
            zip_path.unlink()
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for p in sorted(stage.rglob("*")):
                if p.is_file():
                    zf.write(p, arcname=p.relative_to(stage))

        return ExportResult(
            artifact_path=zip_path,
            preview_text=(f"{ok_n}/{len(jobs)} items archived "
                          f"({_human_bytes(total_bytes)}) → {zip_path.name}"),
            mime="application/zip",
        )


# ─── Strategy + per-item runner ──────────────────────────────────────────────

def _decide_strategy(item: dict, url: str) -> str:
    kind = (item.get("kind") or "").lower()
    if kind == "video":
        return "video"
    host = (urlparse(url).netloc or "").lower()
    if host and VIDEO_HOST_RE.search(host):
        return "video"
    if url.lower().endswith(".pdf"):
        return "pdf"
    return "page"


def _run_job(j: dict, dl_cfg, monolith_bin, ytdlp_ok: bool,
             cap_bytes: int, reuse_cache: bool, page_timeout: int) -> dict:
    if not j["url"]:
        return _fail(j["strategy"], "item has no URL")
    if j["strategy"] == "video":
        return _do_video(j, dl_cfg, ytdlp_ok, cap_bytes, reuse_cache)
    if j["strategy"] == "pdf":
        return _do_pdf(j, cap_bytes)
    return _do_page(j, monolith_bin, cap_bytes, page_timeout)


def _do_page(j: dict, monolith_bin: Optional[str],
             cap_bytes: int, timeout_s: int) -> dict:
    if not monolith_bin:
        return _fail("page",
                     "monolith not installed — install with `brew install monolith`")
    out_path = j["folder"] / "page.html"
    # We capture stderr ourselves; running without -q so monolith's actual
    # error message lands in the manifest instead of an opaque exit code.
    cmd = [monolith_bin, "-o", str(out_path),
           "-t", str(timeout_s), "-e", j["url"]]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout_s + 30)
    except subprocess.TimeoutExpired:
        out_path.unlink(missing_ok=True)
        return _fail("page", f"monolith timeout after {timeout_s}s")
    except FileNotFoundError as e:
        return _fail("page", f"monolith not runnable: {e}")
    if proc.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
        # monolith prints the actionable error on stderr's last line.
        err_lines = (proc.stderr or "").strip().splitlines() or ["unknown monolith error"]
        return _fail("page", err_lines[-1])
    size = out_path.stat().st_size
    if cap_bytes and size > cap_bytes:
        out_path.unlink(missing_ok=True)
        return _fail("page", f"size {size} bytes exceeds cap {cap_bytes} bytes")
    return {"ok": True, "kind": "page", "artifact_rel": "page.html",
            "size": size, "error": ""}


def _do_pdf(j: dict, cap_bytes: int) -> dict:
    try:
        import requests
    except ImportError:
        return _fail("pdf", "requests not installed")
    out_path = j["folder"] / "file.pdf"
    try:
        with requests.get(
            j["url"], stream=True, timeout=60,
            headers={"User-Agent": "Mozilla/5.0 BookiOfflineArchive"},
        ) as r:
            r.raise_for_status()
            written = 0
            with open(out_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if cap_bytes and written > cap_bytes:
                        f.close()
                        out_path.unlink(missing_ok=True)
                        return _fail("pdf",
                                     f"exceeded cap {cap_bytes} bytes during download")
                    f.write(chunk)
        return {"ok": True, "kind": "pdf", "artifact_rel": "file.pdf",
                "size": out_path.stat().st_size, "error": ""}
    except Exception as e:
        out_path.unlink(missing_ok=True)
        return _fail("pdf", f"{type(e).__name__}: {e}")


def _do_video(j: dict, dl_cfg, ytdlp_ok: bool,
              cap_bytes: int, reuse_cache: bool) -> dict:
    # 1. Cache hit from a prior `download.py` run.
    if reuse_cache:
        cached = _cached_video_path(j["item"], dl_cfg)
        if cached and cached.exists():
            ext = cached.suffix or ".mp4"
            dst = j["folder"] / f"video{ext}"
            shutil.copy2(cached, dst)
            size = dst.stat().st_size
            if cap_bytes and size > cap_bytes:
                dst.unlink(missing_ok=True)
                return _fail("video",
                             f"cached file ({size} bytes) exceeds cap {cap_bytes}")
            _copy_video_siblings(cached, j["folder"])
            return {"ok": True, "kind": "video", "artifact_rel": dst.name,
                    "size": size, "error": "", "from_cache": True}

    # 2. Live download via yt-dlp.
    if not ytdlp_ok:
        return _fail("video", "yt-dlp not installed (pip install yt-dlp)")
    try:
        from download import DownloadConfig, download_one  # type: ignore
    except ImportError as e:
        return _fail("video", f"download.py not importable: {e}")

    item_cfg = DownloadConfig(
        dir              = j["folder"],
        video_height_max = getattr(dl_cfg, "video_height_max", 1080),
        write_subs       = getattr(dl_cfg, "write_subs", True),
        write_thumbnail  = getattr(dl_cfg, "write_thumbnail", True),
        sub_langs        = getattr(dl_cfg, "sub_langs", "en.*"),
    )
    res = download_one(j["url"], item_cfg, audio=False)
    if not res.ok or not res.path:
        return _fail("video", res.error or "yt-dlp returned no path")

    media = res.path
    if not media.is_absolute():
        media = j["folder"] / media
    if not media.exists():
        return _fail("video", f"yt-dlp file vanished: {media}")

    dst = j["folder"] / f"video{media.suffix or '.mp4'}"
    if media.resolve() != dst.resolve():
        media.replace(dst)
    size = dst.stat().st_size
    if cap_bytes and size > cap_bytes:
        dst.unlink(missing_ok=True)
        return _fail("video", f"size {size} bytes exceeds cap {cap_bytes} bytes")

    # Sweep sibling thumbs/subs (yt-dlp drops them under %(uploader)s/...).
    for sib in list(j["folder"].rglob("*")):
        if not sib.is_file() or sib.parent == j["folder"]:
            continue
        suf = sib.suffix.lower()
        if suf in (".jpg", ".jpeg", ".png", ".webp"):
            shutil.copy2(sib, j["folder"] / f"thumb{suf}")
        elif suf in (".vtt", ".srt"):
            shutil.copy2(sib, j["folder"] / f"subs{suf}")

    return {"ok": True, "kind": "video", "artifact_rel": dst.name,
            "size": size, "error": ""}


def _copy_video_siblings(media: Path, dst_dir: Path) -> None:
    """Best-effort copy of thumbs / subs sitting next to a cached video file."""
    for sib in media.parent.glob(media.stem + ".*"):
        if sib == media:
            continue
        suf = sib.suffix.lower()
        if suf in (".jpg", ".jpeg", ".png", ".webp"):
            shutil.copy2(sib, dst_dir / f"thumb{suf}")
        elif suf in (".vtt", ".srt"):
            shutil.copy2(sib, dst_dir / f"subs{suf}")


def _cached_video_path(item: dict, dl_cfg) -> Optional[Path]:
    rel = item.get("download_path_video") or item.get("download_path") or ""
    if not rel:
        return None
    p = Path(rel)
    if not p.is_absolute():
        p = dl_cfg.dir / p
    return p


# ─── Config / tooling probes ────────────────────────────────────────────────

def _has_ytdlp() -> bool:
    try:
        import yt_dlp  # noqa: F401
        return True
    except ImportError:
        return False


def _load_download_config():
    """
    Load DownloadConfig from project's config.toml. Falls back to defaults
    pointing at <project-root>/downloads/ when config or import fails.
    """
    project_root = Path(__file__).resolve().parents[3]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    try:
        from download import DownloadConfig  # type: ignore
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib  # type: ignore
        cfg_path = project_root / "config.toml"
        if cfg_path.exists():
            return DownloadConfig.from_toml(
                tomllib.loads(cfg_path.read_text()), cfg_path)
        return DownloadConfig(dir=project_root / "downloads")
    except Exception:
        # Last-resort stub. Cache reuse won't work but jobs won't crash.
        from dataclasses import dataclass
        @dataclass
        class _Stub:
            dir: Path = project_root / "downloads"
            video_height_max: int = 1080
            write_subs: bool = True
            write_thumbnail: bool = True
            sub_langs: str = "en.*"
        return _Stub()


# ─── Small helpers ──────────────────────────────────────────────────────────

def _slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = s.encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s[:60]


def _fail(kind: str, msg: str) -> dict:
    return {"ok": False, "kind": kind, "artifact_rel": None,
            "size": 0, "error": msg}


def _human_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    x: float = float(n)
    for unit in ("KB", "MB", "GB", "TB"):
        x /= 1024.0
        if x < 1024:
            return f"{x:.1f} {unit}"
    return f"{x:.1f} PB"


def _group_items(items: list[dict], group_by: str) -> list[dict]:
    if not group_by or group_by == "none":
        return [{"label": "", "entries": list(items)}]
    buckets: dict[str, list] = {}
    for it in items:
        if group_by == "tag":
            keys = [str(t) for t in (it.get("tags") or [])] or ["(untagged)"]
        elif group_by == "source":
            keys = [str(it.get("source") or "—")]
        elif group_by == "kind":
            keys = [str(it.get("kind") or "—")]
        else:
            keys = ["(all)"]
        for k in keys:
            buckets.setdefault(k, []).append(it)
    return [{"label": k, "entries": buckets[k]} for k in sorted(buckets, key=str.lower)]


def _copy_static(theme_dir: Path, out_dir: Path) -> None:
    if not theme_dir.exists():
        return
    for p in theme_dir.iterdir():
        if p.is_file() and p.suffix not in (".j2", ".jinja", ".jinja2"):
            shutil.copy2(p, out_dir / p.name)
