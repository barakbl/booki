"""
plugins.exporters.offline_archive — background plugin: download a whole
page (or PDF / image / video) per item and bundle a themed index.html
inside a single zip.

Per-item dispatch (run-time):
  • URL ends in .pdf  OR  HEAD says Content-Type: application/pdf  → fetch PDF
  • URL is image-like  OR  Content-Type: image/*                    → fetch image
  • Item kind == "photo" with a local image_path                    → copy file
  • Item kind == "video"  OR  domain ∈ KNOWN_VIDEO_DOMAINS           → yt-dlp
  • Otherwise (HTML page)                                           → archive HTML
        - Playwright available + JS render enabled  → render headless,
          inline subresources (CSS / images / fonts) into a single .html
        - Otherwise                                  → plain HTTP fetch,
          inline subresources via Python `requests`
        - Either way, scan the rendered DOM for <iframe src="*.pdf"> or
          <embed type="application/pdf"> and download the embedded PDF as
          a sibling file (plus link it from the saved page).

Per-item failures retry once, then the item is skipped and the task
finishes with status=success (skipped items are listed in index.html).

Playwright is optional. When missing, the exporter falls back to plain
HTTP — JavaScript-heavy sites won't render correctly. The wizard surfaces
a runtime note explaining the trade-off and the install command.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import os
import re
import shutil
import zipfile
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from jinja2 import Environment, FileSystemLoader, select_autoescape

from core.exporter import Exporter, TaskHandle, register_exporter
from core.local_files import safe_local_path
from core.url_safety import is_externally_fetchable_url, is_safe_url, safe_get


# Per-fetch byte caps. Without these, a hostile bookmarked page can pin
# arbitrary memory by serving a 100 GiB CSS or by 302-chaining to a
# never-ending stream. Numbers picked to comfortably cover real pages
# (a fat news article + media is ~10-20 MiB) without OOMing the worker.
_MAX_HTML_BYTES = 50 * 1024 * 1024          # one page fetch
_MAX_PDF_BYTES = 200 * 1024 * 1024          # direct PDF download
_MAX_IMAGE_BYTES = 100 * 1024 * 1024        # direct image download
_MAX_SUBRESOURCE_BYTES = 25 * 1024 * 1024   # per <img>/<link>/CSS url(...)
_MAX_VIDEO_BYTES = 2 * 1024 * 1024 * 1024   # yt-dlp cap

# Refused content types. SVG is XML and can carry <script> + event
# handlers; if we save it as `slug.svg` and the index links to it, the
# user opening the bundle (often from `file://`) executes hostile script
# in a context that can read sibling files. Refuse at fetch time AND at
# URL classification (`.svg` removed from IMAGE_EXTS below).
_REFUSED_IMAGE_CTS = {"image/svg+xml"}


def _safe_href(url) -> str:
    """Same scheme guard as the link exporter — see its module docstring."""
    s = "" if url is None else str(url)
    return s if is_safe_url(s) else "#"

log = logging.getLogger("booki.exporter.archive")


# Raised when a per-item plan touches a local file outside the configured
# `[[sources.directory.dirs]]` roots. Caught by the per-item retry loop in
# run_background, which appends the message to `skipped` so the user sees
# the reason both in the live task log and the rendered index.html.
class _LocalFileSkipped(Exception):
    pass


def _is_file_url(url: str) -> bool:
    return bool(url) and url.lower().startswith("file://")

# ─── classification ─────────────────────────────────────────────────────────

KNOWN_VIDEO_DOMAINS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be",
    "vimeo.com", "www.vimeo.com",
    "dailymotion.com", "www.dailymotion.com",
    "twitch.tv", "www.twitch.tv",
}

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif"}
DIRECT_DOWNLOAD_EXTS = {".pdf"} | IMAGE_EXTS
PLAN_HTML = "html"
PLAN_PDF = "pdf"
PLAN_IMAGE = "image"
PLAN_VIDEO = "video"
PLAN_LOCAL_PHOTO = "local_photo"

_SLUG_BAD = re.compile(r"[^\w\s-]", re.UNICODE)
_SLUG_WS = re.compile(r"\s+")


def _slug(title: str) -> str:
    s = _SLUG_BAD.sub("", title or "").strip()
    s = _SLUG_WS.sub("-", s)
    return (s[:80] or "untitled").lower()


def _unique_slug(title: str, used: set[str]) -> str:
    base = _slug(title)
    if base not in used:
        return base
    n = 2
    while f"{base}-{n}" in used:
        n += 1
    return f"{base}-{n}"


def _ext_from_url(url: str) -> str:
    path = urlparse(url).path
    return Path(path).suffix.lower()


def _classify(item: dict, local_roots: list[Path],
              allow_svg: bool = False) -> tuple[str, str]:
    """
    URL-only classification (no network). Returns (plan, note).
    Truth at run-time may differ when the server's Content-Type contradicts
    the URL — the runner re-checks with HEAD where it matters.

    Local files (`file://` URLs or `image_path` from frontmatter) are only
    accepted when their resolved real path lives under one of `local_roots`
    (the configured `[[sources.directory.dirs]]`). Anything else returns
    a `("skip", reason)` so the runner records it without ever opening
    the file.

    `allow_svg`: opt-in to SVG archiving. Off by default because SVG can
    carry <script> and on `file://` opens runs in a context Firefox treats
    as same-origin against sibling archive files. Wired from the exporter
    option of the same name.
    """
    url = (item.get("url") or "").strip()
    kind = (item.get("kind") or "").lower()
    if not url:
        return ("skip", "no URL")

    # file:// URLs as the item's primary URL: only honored if inside roots.
    if _is_file_url(url):
        if safe_local_path(url, local_roots) is None:
            return ("skip", f"file:// URL outside configured directories: {url}")
        # Treat as a local image when the extension or kind says so;
        # otherwise it's a local file we can't meaningfully bundle.
        ext = _ext_from_url(url)
        if kind == "photo" or ext in IMAGE_EXTS:
            return (PLAN_LOCAL_PHOTO, "local file:// from frontmatter")
        return ("skip", f"unsupported local file type: {url}")

    ext = _ext_from_url(url)
    if ext == ".pdf":
        return (PLAN_PDF, "URL ends in .pdf")
    # SVG is image-like by extension but XML-by-content: a saved svg opened
    # from `file://` can run script. Skip unless the user explicitly opts
    # in via the `allow_svg` exporter option. (See _REFUSED_IMAGE_CTS.)
    if ext == ".svg":
        if not allow_svg:
            return ("skip",
                    "SVG images are not archived by default — "
                    "enable 'Include SVG images' to override")
        return (PLAN_IMAGE, "URL ends in .svg (allow_svg on)")
    if ext in IMAGE_EXTS:
        return (PLAN_IMAGE, f"URL ends in {ext}")
    if kind == "photo":
        raw_ip = item.get("image_path")
        if raw_ip:
            if safe_local_path(str(raw_ip), local_roots) is not None:
                return (PLAN_LOCAL_PHOTO, "local file from frontmatter")
            return ("skip",
                    f"image_path outside configured directories: {raw_ip}")
        return (PLAN_IMAGE, "kind=photo")
    host = (urlparse(url).hostname or "").lower()
    if kind == "video" or host in KNOWN_VIDEO_DOMAINS:
        return (PLAN_VIDEO, "yt-dlp")
    return (PLAN_HTML, "save page")


# ─── the exporter ───────────────────────────────────────────────────────────

@register_exporter
class OfflineArchiveExporter(Exporter):
    slug = "offline_archive"
    name = "📥 Offline bundle (Flight mode)"
    description = (
        "Download whole pages (HTML / PDF / video / image) and bundle them "
        "into a single zip with a themed index.html — great for vacations 🏖️."
    )
    applicable_kinds = ["any"]
    execution_mode = "background"
    uses_themes = True

    options_schema = [
        {"name": "page_title", "type": "text", "label": "Page title",
         "default": "My Offline Archive"},
        {"name": "footer_text", "type": "text", "label": "Footer text",
         "default": "",
         "help": "Optional text rendered at the bottom of the index page."},
        {"name": "show_search", "type": "bool", "label": "Show inline search",
         "default": True,
         "help": "Uncheck to remove the type-to-filter input from the index."},
        {"name": "rtl", "type": "bool", "label": "Right-to-left (Arabic / Hebrew)",
         "default": False},
        {"name": "js_render", "type": "bool", "label": "Render JavaScript (Playwright)",
         "default": True,
         "help": "When on, HTML pages are rendered in headless Chromium "
                 "and saved with subresources inlined. Falls back to a plain "
                 "HTTP fetch when Playwright is not installed."},
        {"name": "js_timeout_s", "type": "number", "label": "JS render timeout (s)",
         "default": 20},
        {"name": "video_quality", "type": "select", "label": "Video quality (yt-dlp)",
         "options": ["360p", "480p", "720p", "1080p", "best"], "default": "720p"},
        {"name": "include_subs", "type": "bool", "label": "Video subtitles",
         "default": True},
        {"name": "sub_lang", "type": "text", "label": "Subtitle language",
         "default": "en"},
        # SVG is XML and may carry <script> + event handlers. Saving one as
        # `slug.svg` and linking it from the index means a click navigates
        # to a `file://` URL where Firefox grants same-origin access to
        # sibling files. Default off; users who trust their sources (e.g.
        # an archive of their own design portfolio) can flip this on.
        {"name": "allow_svg", "type": "bool", "label": "Include SVG images",
         "default": False,
         "help": "SVG files can contain <script> and run when opened from "
                 "the saved bundle. Off by default — turn on only if you "
                 "trust the source of every selected item."},
    ]

    # ── notes / preview ─────────────────────────────────────────────────────

    def runtime_notes(self) -> list[dict]:
        notes = []
        if _playwright_available():
            notes.append({
                "level": "info",
                "text": "Playwright detected — HTML pages will be rendered "
                        "in headless Chromium with CSS, fonts and images inlined.",
            })
        else:
            notes.append({
                "level": "warning",
                "text": "Playwright is not installed. HTML pages will be saved "
                        "via a plain HTTP fetch — JavaScript-rendered sites won't "
                        "look right. To enable full-fidelity capture (~300 MB): "
                        "`pip install playwright && playwright install chromium`.",
            })
        if not _yt_dlp_available():
            notes.append({
                "level": "warning",
                "text": "yt-dlp is not installed. Video items will be skipped. "
                        "Install with `pip install yt-dlp` (ffmpeg also required for muxing).",
            })
        return notes

    def preview(self, items, options, theme, theme_vars, tree=None):
        # Render the *same* archive.html.j2 the runner will use, but with
        # planned filenames instead of actual downloaded files. Links in
        # the preview point at `./files/<slug>.<ext>` which won't resolve
        # (no zip yet), but the layout, theme, and per-item plan badges
        # are exactly what the user will see in the bundle.
        if theme is None:
            return {"kind": "manifest", "manifest": []}

        cap = 50
        shown = items[:cap]
        used: set[str] = set()
        rendered: list[dict] = []
        local_roots = list(self.local_roots)
        allow_svg = bool(options.get("allow_svg", False))
        for it in shown:
            plan, _ = _classify(it, local_roots, allow_svg=allow_svg)
            if plan == "skip":
                continue
            slug = _unique_slug(it.get("title") or "", used)
            used.add(slug)
            rendered.append({
                "title": (it.get("title") or "untitled").strip(),
                "url": it.get("url") or "",
                "slug": slug,
                "plan": plan,
                "filename": _planned_filename(plan, slug, it),
                "extra_files": [],
                "thumb": None,
                "summary": it.get("summary") or "",
                "kind": it.get("kind") or "",
            })

        page_title = options.get("page_title") or "My Offline Archive"
        footer_bits = []
        if (options.get("footer_text") or "").strip():
            footer_bits.append((options.get("footer_text") or "").strip())
        footer_bits.append(
            f"preview · planned bundle of {len(items)} item(s)"
            + (f" (showing first {cap})" if len(items) > cap else "")
        )
        footer_text = " · ".join(footer_bits)

        env = Environment(
            loader=FileSystemLoader(str(theme.path)),
            autoescape=select_autoescape(["html", "j2"]),
        )
        env.filters["safe_href"] = _safe_href
        tmpl = env.get_template("archive.html.j2")
        html = tmpl.render(
            title=page_title,
            footer_text=footer_text,
            show_search=bool(options.get("show_search", True)),
            rtl=bool(options.get("rtl", False)),
            items=rendered,
            skipped=[],
            theme_vars=theme_vars,
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M") + " (preview)",
        )
        return {
            "kind": "html",
            "filename": "index.html",
            "mime": "text/html",
            "content": html,
            "truncated": len(items) > cap,
            "preview_lines": cap if len(items) > cap else None,
        }

    # ── runner ──────────────────────────────────────────────────────────────

    def run_background(self, items, options, theme, theme_vars, task: TaskHandle, tree=None):
        if theme is None:
            raise ValueError("Offline archive exporter requires a theme.")

        page_title = options.get("page_title") or "My Offline Archive"
        footer_text = (options.get("footer_text") or "").strip()
        show_search = bool(options.get("show_search", True))
        rtl = bool(options.get("rtl", False))
        js_render = bool(options.get("js_render", True)) and _playwright_available()
        js_timeout = float(options.get("js_timeout_s") or 20)
        video_quality = options.get("video_quality") or "720p"
        include_subs = bool(options.get("include_subs", True))
        sub_lang = (options.get("sub_lang") or "en").strip() or "en"
        allow_svg = bool(options.get("allow_svg", False))
        if allow_svg:
            task.log("note: SVG archiving is enabled — saved .svg files "
                     "can run script when opened from the bundle")

        artifact_dir = task.artifact_dir
        # Working tree is shaped like the final zip:
        #   zip_root/
        #     index.html       ← the themed listing
        #     files/           ← every per-item download lives in here so
        #                        the root stays clean.
        zip_root = artifact_dir / "out"
        files_dir = zip_root / "files"
        zip_root.mkdir(parents=True, exist_ok=True)
        files_dir.mkdir(parents=True, exist_ok=True)

        # Lazy-init Playwright once for the whole task.
        pw_ctx: Any = None
        if js_render:
            try:
                pw_ctx = _PlaywrightContext(timeout_s=js_timeout)
                pw_ctx.start()
                task.log("playwright: chromium launched")
            except Exception as e:
                task.log(f"playwright: failed to launch ({e}); falling back to plain HTTP")
                pw_ctx = None
                js_render = False

        rendered: list[dict] = []
        skipped: list[dict] = []
        used: set[str] = set()
        total = len(items)
        local_roots = list(self.local_roots)

        try:
            for i, it in enumerate(items, start=1):
                title = (it.get("title") or "untitled").strip()
                url = (it.get("url") or "").strip()
                task.progress(i - 1, total)
                task.log(f"[{i}/{total}] {title}")

                plan, reason = _classify(it, local_roots, allow_svg=allow_svg)
                if plan == "skip":
                    task.log(f"  skipped: {reason}")
                    skipped.append({"title": title, "reason": reason})
                    continue

                slug = _unique_slug(title, used)
                used.add(slug)

                err = ""
                produced: dict | None = None
                for attempt in (1, 2):
                    try:
                        produced = _archive_one(
                            it, plan, slug, files_dir,
                            pw_ctx=pw_ctx,
                            video_quality=video_quality,
                            include_subs=include_subs,
                            sub_lang=sub_lang,
                            task=task,
                            local_roots=local_roots,
                            allow_svg=allow_svg,
                        )
                        err = ""
                        break
                    except _LocalFileSkipped as e:
                        # Containment failure — no point retrying. Skip
                        # cleanly with the reason so the user sees it
                        # both in the live log and the rendered index.
                        err = str(e)
                        task.log(f"  skipped: {err}")
                        break
                    except Exception as e:
                        err = f"{type(e).__name__}: {e}"
                        task.log(f"  attempt {attempt} failed: {err}")

                if produced is None:
                    skipped.append({"title": title, "reason": err or "unknown error"})
                    continue

                rendered.append({
                    "title": title,
                    "url": url,
                    "slug": slug,
                    "plan": plan,
                    "filename": produced["filename"],
                    "extra_files": produced.get("extra", []),
                    "thumb": produced.get("thumb"),
                    "summary": it.get("summary") or "",
                    "kind": it.get("kind") or "",
                })
        finally:
            if pw_ctx is not None:
                try: pw_ctx.stop()
                except Exception: pass

        task.progress(total, total)

        # Render index.html
        env = Environment(
            loader=FileSystemLoader(str(theme.path)),
            autoescape=select_autoescape(["html", "j2"]),
        )
        env.filters["safe_href"] = _safe_href
        tmpl = env.get_template("archive.html.j2")
        html = tmpl.render(
            title=page_title,
            footer_text=footer_text,
            show_search=show_search,
            rtl=rtl,
            items=rendered,
            skipped=skipped,
            theme_vars=theme_vars,
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        )
        (zip_root / "index.html").write_text(html, encoding="utf-8")

        # Zip layout: index.html at the root, every download under files/.
        # ZIP_STORED keeps already-compressed files un-recompressed.
        ts = datetime.now().strftime("%Y-%m-%dT%H-%M")
        zip_path = artifact_dir / f"booki-archive-{ts}.zip"
        task.log(f"zipping {len(rendered)} item{'' if len(rendered) == 1 else 's'} → {zip_path.name}")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as z:
            for p in sorted(zip_root.rglob("*")):
                if p.is_file():
                    z.write(p, arcname=p.relative_to(zip_root))

        shutil.rmtree(zip_root, ignore_errors=True)

        suffix = f", {len(skipped)} skipped" if skipped else ""
        task.log(f"done: {zip_path.name} ({len(rendered)} items{suffix})")
        return zip_path


# ─── per-item archivers ─────────────────────────────────────────────────────

def _planned_filename(plan: str, slug: str, item: dict) -> str:
    if plan == PLAN_PDF:
        return f"{slug}.pdf"
    if plan == PLAN_IMAGE or plan == PLAN_LOCAL_PHOTO:
        url = item.get("url") or ""
        ext = _ext_from_url(url) or ".jpg"
        if plan == PLAN_LOCAL_PHOTO:
            local = Path(str(item.get("image_path") or ""))
            ext = local.suffix.lower() or ext
        return f"{slug}{ext}"
    if plan == PLAN_VIDEO:
        return f"{slug}.mp4"
    return f"{slug}.html"


def _archive_one(item: dict, plan: str, slug: str, out_dir: Path, *,
                 pw_ctx, video_quality: str, include_subs: bool, sub_lang: str,
                 task: TaskHandle, local_roots: list[Path],
                 allow_svg: bool = False) -> dict:
    url = (item.get("url") or "").strip()
    # Belt-and-suspenders: _classify already rejects file:// URLs that
    # aren't in roots, but if a plan ever points at one and it slipped
    # through (e.g. PDF/IMAGE plans that read by URL extension), refuse
    # before handing it to `requests`, which would raise a noisy
    # InvalidSchema. Keep the skip reason crisp.
    if _is_file_url(url) and plan in (PLAN_PDF, PLAN_IMAGE, PLAN_VIDEO, PLAN_HTML):
        if safe_local_path(url, local_roots) is None:
            raise _LocalFileSkipped(
                f"file:// URL outside configured directories: {url}")
    if plan == PLAN_PDF:
        return _archive_pdf(url, slug, out_dir)
    if plan == PLAN_IMAGE:
        return _archive_image(url, slug, out_dir, allow_svg=allow_svg)
    if plan == PLAN_LOCAL_PHOTO:
        return _archive_local_photo(item, slug, out_dir, local_roots=local_roots)
    if plan == PLAN_VIDEO:
        return _archive_video(url, slug, out_dir,
                              quality=video_quality,
                              include_subs=include_subs,
                              sub_lang=sub_lang)
    # default: HTML
    return _archive_html(url, slug, out_dir, pw_ctx=pw_ctx, task=task,
                         local_roots=local_roots)


def _archive_pdf(url: str, slug: str, out_dir: Path) -> dict:
    r = safe_get(url, timeout=60, headers={"User-Agent": _UA},
                 max_bytes=_MAX_PDF_BYTES)
    if r is None or not r.ok:
        raise RuntimeError(f"refused or failed to fetch PDF: {url}")
    dest = out_dir / f"{slug}.pdf"
    dest.write_bytes(r.content)
    return {"filename": dest.name}


def _archive_image(url: str, slug: str, out_dir: Path,
                   allow_svg: bool = False) -> dict:
    r = safe_get(url, timeout=60, headers={"User-Agent": _UA},
                 max_bytes=_MAX_IMAGE_BYTES)
    if r is None or not r.ok:
        raise RuntimeError(f"refused or failed to fetch image: {url}")
    ct = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if ct in _REFUSED_IMAGE_CTS and not allow_svg:
        raise RuntimeError(f"refused image content-type: {ct}")
    ext = _ext_from_url(url)
    if not ext:
        ext = mimetypes.guess_extension(ct) or ".bin"
    if ext == ".svg" and not allow_svg:
        # Belt-and-suspenders: classifier filters .svg URLs by default,
        # but extension can also be derived from Content-Type above.
        raise RuntimeError("SVG images are not archived (script-capable)")
    dest = out_dir / f"{slug}{ext}"
    dest.write_bytes(r.content)
    return {"filename": dest.name}


def _archive_local_photo(item: dict, slug: str, out_dir: Path, *,
                         local_roots: list[Path]) -> dict:
    raw = str(item.get("image_path") or item.get("url") or "")
    src = safe_local_path(raw, local_roots)
    if src is None:
        raise _LocalFileSkipped(
            f"image_path outside configured directories: {raw}")
    ext = src.suffix.lower() or ".jpg"
    dest = out_dir / f"{slug}{ext}"
    _copy_no_follow(src, dest)
    return {"filename": dest.name}


def _no_follow_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _copy_no_follow(src: Path, dest: Path) -> None:
    """`shutil.copy2` follows symlinks; between `safe_local_path`'s
    resolve+validate and the actual open, a local writer with access to
    the configured root could replace the file with a symlink to e.g.
    `/etc/shadow`. Open with `O_NOFOLLOW` so the leaf-component swap
    fails closed instead of leaking the symlink target into the bundle.
    """
    fd = os.open(str(src), _no_follow_flags())
    with os.fdopen(fd, "rb") as f, open(dest, "wb") as out:
        shutil.copyfileobj(f, out, length=1 << 16)


def _read_no_follow(src: Path) -> bytes:
    """Same TOCTOU rationale as `_copy_no_follow`."""
    fd = os.open(str(src), _no_follow_flags())
    with os.fdopen(fd, "rb") as f:
        return f.read()


def _archive_video(url: str, slug: str, out_dir: Path, *,
                   quality: str, include_subs: bool, sub_lang: str) -> dict:
    if not _yt_dlp_available():
        raise RuntimeError("yt-dlp is not installed (pip install yt-dlp)")
    import yt_dlp  # type: ignore

    outtmpl = str(out_dir / f"{slug}.%(ext)s")
    opts = {
        "outtmpl": outtmpl,
        "restrictfilenames": False,
        "noprogress": True,
        "quiet": True,
        "no_warnings": True,
        "format": _format_for_quality(quality),
        "merge_output_format": "mp4",
        "writethumbnail": True,
        "writesubtitles": include_subs,
        "writeautomaticsub": include_subs,
        "subtitleslangs": [sub_lang] if include_subs else [],
        "continue": True,
        "overwrites": False,
        # Hard cap on per-video size so a hostile / mis-classified URL can't
        # fill the disk. yt-dlp aborts when the format's reported size
        # exceeds the cap.
        "max_filesize": _MAX_VIDEO_BYTES,
        "postprocessors": [
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"},
        ],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    requested = info.get("requested_downloads") or []
    raw = (requested[0].get("filepath") or requested[0].get("_filename")) if requested else None
    raw = raw or info.get("_filename") or info.get("filename")
    if not raw:
        raise RuntimeError("yt-dlp did not report a filename")
    video_path = Path(raw)
    if not video_path.exists():
        merged = video_path.with_suffix(".mp4")
        if merged.exists():
            video_path = merged
        else:
            raise RuntimeError(f"downloaded file missing: {video_path}")
    # Defense in depth: the outtmpl prefix should already keep yt-dlp inside
    # `out_dir`, but a misbehaving extractor / postprocessor that returned
    # a path outside would otherwise leak files onto the booki host
    # filesystem (the zip step skips them, but they'd persist).
    out_dir_real = out_dir.resolve()
    try:
        video_path.resolve().relative_to(out_dir_real)
    except ValueError:
        raise RuntimeError(
            f"yt-dlp wrote outside output directory: {video_path}")

    thumb = None
    for ext in (".jpg", ".jpeg", ".webp", ".png"):
        candidate = video_path.with_suffix(ext)
        if candidate.exists() and candidate != video_path:
            thumb = candidate.name
            break
    return {"filename": video_path.name, "thumb": thumb}


def _format_for_quality(quality: str) -> str:
    if quality == "best":
        return "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
    h = int(str(quality).rstrip("p"))
    return (
        f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/"
        f"best[height<={h}][ext=mp4]/"
        f"bestvideo[height<={h}]+bestaudio/best[height<={h}]"
    )


# ─── HTML archiver ──────────────────────────────────────────────────────────

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 13_0) "
       "AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/126.0.0.0 Safari/537.36")


def _archive_html(url: str, slug: str, out_dir: Path, *, pw_ctx,
                  task: TaskHandle, local_roots: list[Path]) -> dict:
    """
    Save a single-file HTML snapshot of `url`. With Playwright: navigates,
    waits for networkidle, returns rendered HTML. Without: plain GET.
    Either way: subresources are inlined and embedded PDFs are extracted.

    `local_roots` gates any `file://` subresource the page references —
    only paths inside those roots get inlined, the rest are dropped.
    """
    if pw_ctx is not None:
        # Initial gate: refuse private / loopback / IMDS targets up front.
        # Subresources/redirects past page.goto are still subject to whatever
        # Chromium's network stack does — we re-validate every subresource
        # in `_PlaywrightFetcher`, but in-page redirects during page.goto
        # itself are not interceptable here.
        if not is_externally_fetchable_url(url):
            raise RuntimeError(f"refusing to fetch internal URL: {url}")
        html, final_url, fetcher = pw_ctx.fetch(url)
    else:
        r = safe_get(url, timeout=60, headers={"User-Agent": _UA},
                     max_bytes=_MAX_HTML_BYTES)
        if r is None or not r.ok:
            raise RuntimeError(f"refused or failed to fetch page: {url}")
        # Decode using the response's chosen encoding; safe_get already
        # capped the body so this is bounded.
        html = r.text
        final_url = r.url
        fetcher = _RequestsFetcher()
    fetcher = _LocalAwareFetcher(fetcher, local_roots, task)

    # Extract embedded PDFs *before* rewriting subresources so we can rewrite
    # the iframe src to a local sibling file.
    extras: list[str] = []
    pdf_map: dict[str, str] = {}
    for i, pdf_url in enumerate(_find_iframe_pdfs(html, final_url), start=1):
        try:
            pdf_bytes = fetcher.fetch(pdf_url)
            ct = fetcher.last_content_type or ""
            if "pdf" not in ct.lower() and not pdf_url.lower().endswith(".pdf"):
                continue
            pdf_name = f"{slug}.embed{i}.pdf"
            (out_dir / pdf_name).write_bytes(pdf_bytes)
            pdf_map[pdf_url] = pdf_name
            extras.append(pdf_name)
            task.log(f"  fished embedded PDF → {pdf_name}")
        except Exception as e:
            task.log(f"  embedded PDF skipped ({pdf_url}): {e}")

    inlined = _inline_subresources(html, final_url, fetcher, pdf_map=pdf_map)
    inlined = _inject_csp(inlined)
    dest = out_dir / f"{slug}.html"
    dest.write_text(inlined, encoding="utf-8")
    return {"filename": dest.name, "extra": extras}


# Restrictive CSP we inject into every saved per-page snapshot. The page's
# inline scripts (which we intentionally don't strip — see _inline_subresources)
# would otherwise execute at `file://` origin and could fetch sibling files.
# This blocks script execution and external network entirely; everything we
# care about is already inlined as data: URIs or sibling files.
#   - default-src 'none'         : deny everything not explicitly allowed
#   - img-src data: 'self'       : inlined images + sibling thumbs
#   - style-src 'unsafe-inline' data: : inlined <style> blocks
#   - font-src data:             : inlined @font-face
#   - media-src data: 'self'     : inlined audio/video, sibling files
#   - frame-src 'self' data:     : embedded sibling PDFs, data-uri previews
#   - object-src 'self' data:    : same, for <object>/<embed>
#   - script-src 'none'          : no script, period (kills file:// XSS)
_CSP_META = (
    '<meta http-equiv="Content-Security-Policy" '
    'content="default-src \'none\'; '
    "img-src data: 'self'; "
    "style-src 'unsafe-inline' data:; "
    "font-src data:; "
    "media-src data: 'self'; "
    "frame-src 'self' data:; "
    "object-src 'self' data:; "
    'script-src \'none\'">'
)
_HEAD_OPEN_RE = re.compile(r"<head\b[^>]*>", re.IGNORECASE)


def _inject_csp(html: str) -> str:
    """Inject a restrictive CSP <meta> right after <head>, or prepend it
    when the page has no <head> tag. Idempotent if the meta is already
    present (we don't re-inject)."""
    if "Content-Security-Policy" in html:
        return html
    m = _HEAD_OPEN_RE.search(html)
    if m:
        return html[:m.end()] + _CSP_META + html[m.end():]
    return _CSP_META + html


# Patterns (tag-attr level) we want to walk. We intentionally avoid pulling
# in BeautifulSoup; the regex set below is narrow and only operates on
# well-known attribute syntaxes.
_TAG_RE = re.compile(
    r"""<(?P<tag>img|link|script|source|video|audio|iframe|embed)
        \b(?P<attrs>[^>]*)>""",
    re.IGNORECASE | re.VERBOSE)
_ATTR_RE = re.compile(
    r"""(?P<name>\w[\w:-]*)\s*=\s*(?P<q>"|'|)(?P<val>.*?)(?P=q)\s*""",
    re.IGNORECASE | re.VERBOSE)


def _find_iframe_pdfs(html: str, base_url: str) -> list[str]:
    out: list[str] = []
    for m in _TAG_RE.finditer(html):
        tag = m.group("tag").lower()
        if tag not in ("iframe", "embed"):
            continue
        attrs = _parse_attrs(m.group("attrs"))
        src = attrs.get("src") or attrs.get("data") or ""
        typ = (attrs.get("type") or "").lower()
        if not src:
            continue
        full = urljoin(base_url, src)
        if full.lower().split("?", 1)[0].endswith(".pdf") or "pdf" in typ:
            if full not in out:
                out.append(full)
    return out


def _parse_attrs(s: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _ATTR_RE.finditer(s):
        out[m.group("name").lower()] = m.group("val")
    return out


def _inline_subresources(html: str, base_url: str, fetcher,
                         pdf_map: dict[str, str]) -> str:
    """
    Rewrite `html` so subresources are self-contained:
      - <img src=...>, <source src=...>            → data URI
      - <link rel="stylesheet" href=...>           → <style>...inlined CSS...</style>
      - <link rel="icon" href=...>                 → data URI
      - <script src=...>                            → stripped (saved page is static)
      - <iframe src=...> / <embed src=...> → PDFs   → rewrite src to local file
    Failures are silently dropped (the tag keeps its remote URL).
    """
    def _rewrite_tag(m: re.Match) -> str:
        tag = m.group("tag").lower()
        attrs = _parse_attrs(m.group("attrs"))
        if tag == "script":
            # Drop external scripts. Inline scripts (no src) we leave alone.
            if "src" in attrs:
                return ""
            return m.group(0)
        if tag == "link":
            rel = (attrs.get("rel") or "").lower()
            href = attrs.get("href") or ""
            if not href:
                return m.group(0)
            full = urljoin(base_url, href)
            if "stylesheet" in rel:
                try:
                    css = fetcher.fetch_text(full)
                    css = _inline_css(css, full, fetcher)
                    return f"<style>{css}</style>"
                except Exception:
                    return m.group(0)
            if "icon" in rel or "apple-touch-icon" in rel:
                try:
                    data = fetcher.fetch(full)
                    ct = fetcher.last_content_type or "image/x-icon"
                    return _replace_attr(m.group(0), "href",
                                         _data_uri(data, ct))
                except Exception:
                    return m.group(0)
            return m.group(0)
        if tag in ("img", "source", "video", "audio"):
            src = attrs.get("src") or ""
            if not src or src.startswith("data:"):
                return m.group(0)
            try:
                full = urljoin(base_url, src)
                data = fetcher.fetch(full)
                ct = fetcher.last_content_type or _guess_ct(full)
                return _replace_attr(m.group(0), "src", _data_uri(data, ct))
            except Exception:
                return m.group(0)
        if tag in ("iframe", "embed"):
            src = attrs.get("src") or attrs.get("data") or ""
            if not src:
                return m.group(0)
            full = urljoin(base_url, src)
            if full in pdf_map:
                return _replace_attr(m.group(0), "src", pdf_map[full])
            return m.group(0)
        return m.group(0)

    return _TAG_RE.sub(_rewrite_tag, html)


_URL_RE = re.compile(r"""url\(\s*(?P<q>"|'|)(?P<u>.*?)(?P=q)\s*\)""")


def _inline_css(css: str, base_url: str, fetcher) -> str:
    def _rewrite(m: re.Match) -> str:
        u = m.group("u")
        if not u or u.startswith("data:"):
            return m.group(0)
        full = urljoin(base_url, u)
        try:
            data = fetcher.fetch(full)
            ct = fetcher.last_content_type or _guess_ct(full)
            return f"url({_data_uri(data, ct)})"
        except Exception:
            return m.group(0)
    return _URL_RE.sub(_rewrite, css)


def _replace_attr(tag_text: str, attr: str, new_value: str) -> str:
    pat = re.compile(rf'\b{re.escape(attr)}\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+)',
                     re.IGNORECASE)
    quoted = '"' + new_value.replace('"', "&quot;") + '"'
    if pat.search(tag_text):
        return pat.sub(f'{attr}={quoted}', tag_text, count=1)
    # Insert before closing >
    return tag_text[:-1] + f' {attr}={quoted}>'


def _data_uri(data: bytes, ct: str) -> str:
    ct = (ct or "application/octet-stream").split(";")[0].strip() or "application/octet-stream"
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{ct};base64,{b64}"


def _guess_ct(url: str) -> str:
    ct, _ = mimetypes.guess_type(urlparse(url).path)
    return ct or "application/octet-stream"


# ─── fetchers ───────────────────────────────────────────────────────────────

class _LocalAwareFetcher:
    """
    Decorator around the real fetcher (Playwright or requests) that
    intercepts `file://` subresource URLs. Local files are only read when
    inside the allow-listed roots; otherwise the fetch raises so
    `_inline_subresources` falls into its silent-failure branch and the
    tag keeps its original href (rendered as a broken link in the saved
    page rather than a leaked filesystem read).

    Non-`file://` URLs are forwarded unchanged.
    """
    def __init__(self, inner, local_roots: list[Path], task: TaskHandle):
        self._inner = inner
        self._roots = local_roots
        self._task = task
        self.last_content_type = ""

    def _local_bytes(self, url: str) -> bytes:
        p = safe_local_path(url, self._roots)
        if p is None:
            self._task.log(f"  inline skipped (outside configured directories): {url}")
            raise RuntimeError(f"local file outside configured directories: {url}")
        self.last_content_type = _guess_ct(url)
        return _read_no_follow(p)

    def fetch(self, url: str) -> bytes:
        if _is_file_url(url):
            return self._local_bytes(url)
        data = self._inner.fetch(url)
        self.last_content_type = self._inner.last_content_type
        return data

    def fetch_text(self, url: str) -> str:
        if _is_file_url(url):
            return self._local_bytes(url).decode("utf-8", errors="replace")
        text = self._inner.fetch_text(url)
        self.last_content_type = self._inner.last_content_type
        return text


class _RequestsFetcher:
    """Plain HTTP fetcher used when Playwright isn't available.

    Routes every request through `core.url_safety.safe_get` so each
    subresource hop is re-validated against the SSRF allowlist, DNS-pinned
    against rebinding, and capped at `_MAX_SUBRESOURCE_BYTES`. Without this
    layer, a hostile page could include `<link rel="stylesheet"
    href="http://169.254.169.254/...">` and have IMDS contents inlined as
    base64 into the saved archive.
    """

    def __init__(self):
        self.last_content_type = ""

    def fetch(self, url: str) -> bytes:
        r = safe_get(url, timeout=60, headers={"User-Agent": _UA},
                     max_bytes=_MAX_SUBRESOURCE_BYTES)
        if r is None or not r.ok:
            raise RuntimeError(f"refused or failed: {url}")
        self.last_content_type = r.headers.get("Content-Type", "")
        return r.content

    def fetch_text(self, url: str) -> str:
        r = safe_get(url, timeout=60, headers={"User-Agent": _UA},
                     max_bytes=_MAX_SUBRESOURCE_BYTES)
        if r is None or not r.ok:
            raise RuntimeError(f"refused or failed: {url}")
        self.last_content_type = r.headers.get("Content-Type", "")
        return r.text


class _PlaywrightContext:
    """
    Lazy-initialized headless Chromium. One instance is reused for the whole
    task so we pay the launch cost once. fetch() returns rendered HTML; the
    nested `request` API shares cookies with the rendered page.
    """

    def __init__(self, *, timeout_s: float = 20.0):
        self.timeout_s = timeout_s
        self._pw = None
        self._browser = None
        self._context = None

    def start(self) -> None:
        from playwright.sync_api import sync_playwright  # type: ignore
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        self._context = self._browser.new_context(user_agent=_UA)

    def stop(self) -> None:
        try:
            if self._context: self._context.close()
        finally:
            try:
                if self._browser: self._browser.close()
            finally:
                if self._pw: self._pw.stop()

    def fetch(self, url: str) -> tuple[str, str, "_PlaywrightFetcher"]:
        page = self._context.new_page()
        try:
            page.goto(url, wait_until="networkidle",
                      timeout=int(self.timeout_s * 1000))
            html = page.content()
            final_url = page.url
        finally:
            page.close()
        return html, final_url, _PlaywrightFetcher(self._context)


class _PlaywrightFetcher:
    """Subresource fetcher backed by a Playwright APIRequestContext.

    Pre-flights every URL through `is_externally_fetchable_url`; we cannot
    DNS-pin Chromium's resolver from Python, but a static allowlist still
    blocks the obvious LAN/IMDS exfil shapes. Body size is capped at
    `_MAX_SUBRESOURCE_BYTES` to bound memory.
    """

    def __init__(self, context):
        self._req = context.request
        self.last_content_type = ""

    def _get(self, url: str):
        if not is_externally_fetchable_url(url):
            raise RuntimeError(f"refused (not externally fetchable): {url}")
        r = self._req.get(url, timeout=60_000)
        if not r.ok:
            raise RuntimeError(f"HTTP {r.status} on {url}")
        return r

    def fetch(self, url: str) -> bytes:
        r = self._get(url)
        self.last_content_type = r.headers.get("content-type", "")
        body = r.body()
        if len(body) > _MAX_SUBRESOURCE_BYTES:
            body = body[:_MAX_SUBRESOURCE_BYTES]
        return body

    def fetch_text(self, url: str) -> str:
        r = self._get(url)
        self.last_content_type = r.headers.get("content-type", "")
        text = r.text()
        # Approximate cap (chars vs bytes); good enough to bound memory.
        if len(text) > _MAX_SUBRESOURCE_BYTES:
            text = text[:_MAX_SUBRESOURCE_BYTES]
        return text


# ─── feature probes ─────────────────────────────────────────────────────────

def _playwright_available() -> bool:
    try:
        import playwright  # noqa: F401
        from playwright.sync_api import sync_playwright  # noqa: F401
        return True
    except Exception:
        return False


def _yt_dlp_available() -> bool:
    try:
        import yt_dlp  # noqa: F401
        return True
    except Exception:
        return False
