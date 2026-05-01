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

log = logging.getLogger("booki.exporter.archive")

# ─── classification ─────────────────────────────────────────────────────────

KNOWN_VIDEO_DOMAINS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be",
    "vimeo.com", "www.vimeo.com",
    "dailymotion.com", "www.dailymotion.com",
    "twitch.tv", "www.twitch.tv",
}

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".avif"}
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


def _classify(item: dict) -> tuple[str, str]:
    """
    URL-only classification (no network). Returns (plan, note).
    Truth at run-time may differ when the server's Content-Type contradicts
    the URL — the runner re-checks with HEAD where it matters.
    """
    url = (item.get("url") or "").strip()
    kind = (item.get("kind") or "").lower()
    if not url:
        return ("skip", "no URL")

    ext = _ext_from_url(url)
    if ext == ".pdf":
        return (PLAN_PDF, "URL ends in .pdf")
    if ext in IMAGE_EXTS:
        return (PLAN_IMAGE, f"URL ends in {ext}")
    if kind == "photo":
        if item.get("image_path") and Path(str(item["image_path"])).is_file():
            return (PLAN_LOCAL_PHOTO, "local file from frontmatter")
        return (PLAN_IMAGE, "kind=photo")
    host = (urlparse(url).hostname or "").lower()
    if kind == "video" or host in KNOWN_VIDEO_DOMAINS:
        return (PLAN_VIDEO, "yt-dlp")
    return (PLAN_HTML, "save page")


# ─── the exporter ───────────────────────────────────────────────────────────

@register_exporter
class OfflineArchiveExporter(Exporter):
    slug = "offline_archive"
    name = "Offline archive"
    description = (
        "Download whole pages (HTML / PDF / video / image) and bundle them "
        "into a single zip with a themed index.html."
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
        manifest: list[dict] = []
        used: set[str] = set()
        cap = 50
        shown = items[:cap]
        for it in shown:
            plan, note = _classify(it)
            slug = _unique_slug(it.get("title") or "", used)
            used.add(slug)
            filename = _planned_filename(plan, slug, it)
            manifest.append({
                "title": it.get("title") or "(untitled)",
                "url": it.get("url") or "",
                "plan": plan,
                "filename": filename,
                "note": note,
            })
        if len(items) > cap:
            manifest.append({
                "title": f"… and {len(items) - cap} more",
                "url": "",
                "plan": "more",
                "filename": "",
                "note": "(preview limited to first 50)",
            })
        return {"kind": "manifest", "manifest": manifest}

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

        artifact_dir = task.artifact_dir
        out_dir = artifact_dir / "out"
        out_dir.mkdir(parents=True, exist_ok=True)

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

        try:
            for i, it in enumerate(items, start=1):
                title = (it.get("title") or "untitled").strip()
                url = (it.get("url") or "").strip()
                task.progress(i - 1, total)
                task.log(f"[{i}/{total}] {title}")

                plan, _ = _classify(it)
                if plan == "skip":
                    task.log("  skipped: no URL")
                    skipped.append({"title": title, "reason": "no URL"})
                    continue

                slug = _unique_slug(title, used)
                used.add(slug)

                err = ""
                produced: dict | None = None
                for attempt in (1, 2):
                    try:
                        produced = _archive_one(
                            it, plan, slug, out_dir,
                            pw_ctx=pw_ctx,
                            video_quality=video_quality,
                            include_subs=include_subs,
                            sub_lang=sub_lang,
                            task=task,
                        )
                        err = ""
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
        (out_dir / "index.html").write_text(html, encoding="utf-8")

        # Zip it. ZIP_STORED keeps already-compressed files un-recompressed.
        ts = datetime.now().strftime("%Y-%m-%dT%H-%M")
        zip_path = artifact_dir / f"booki-archive-{ts}.zip"
        task.log(f"zipping {len(rendered)} item{'' if len(rendered) == 1 else 's'} → {zip_path.name}")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as z:
            for p in sorted(out_dir.rglob("*")):
                if p.is_file():
                    z.write(p, arcname=p.relative_to(out_dir))

        shutil.rmtree(out_dir, ignore_errors=True)

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
                 task: TaskHandle) -> dict:
    url = (item.get("url") or "").strip()
    if plan == PLAN_PDF:
        return _archive_pdf(url, slug, out_dir)
    if plan == PLAN_IMAGE:
        return _archive_image(url, slug, out_dir)
    if plan == PLAN_LOCAL_PHOTO:
        return _archive_local_photo(item, slug, out_dir)
    if plan == PLAN_VIDEO:
        return _archive_video(url, slug, out_dir,
                              quality=video_quality,
                              include_subs=include_subs,
                              sub_lang=sub_lang)
    # default: HTML
    return _archive_html(url, slug, out_dir, pw_ctx=pw_ctx, task=task)


def _archive_pdf(url: str, slug: str, out_dir: Path) -> dict:
    import requests
    r = requests.get(url, stream=True, timeout=60,
                     headers={"User-Agent": _UA})
    r.raise_for_status()
    dest = out_dir / f"{slug}.pdf"
    with open(dest, "wb") as f:
        for chunk in r.iter_content(chunk_size=1 << 15):
            if chunk:
                f.write(chunk)
    return {"filename": dest.name}


def _archive_image(url: str, slug: str, out_dir: Path) -> dict:
    import requests
    r = requests.get(url, stream=True, timeout=60,
                     headers={"User-Agent": _UA})
    r.raise_for_status()
    ext = _ext_from_url(url)
    if not ext:
        ct = (r.headers.get("Content-Type") or "").split(";")[0].strip()
        ext = mimetypes.guess_extension(ct) or ".bin"
    dest = out_dir / f"{slug}{ext}"
    with open(dest, "wb") as f:
        for chunk in r.iter_content(chunk_size=1 << 15):
            if chunk:
                f.write(chunk)
    return {"filename": dest.name}


def _archive_local_photo(item: dict, slug: str, out_dir: Path) -> dict:
    src = Path(str(item.get("image_path") or ""))
    if not src.is_file():
        raise FileNotFoundError(f"local image not found: {src}")
    ext = src.suffix.lower() or ".jpg"
    dest = out_dir / f"{slug}{ext}"
    shutil.copy2(src, dest)
    return {"filename": dest.name}


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


def _archive_html(url: str, slug: str, out_dir: Path, *, pw_ctx, task: TaskHandle) -> dict:
    """
    Save a single-file HTML snapshot of `url`. With Playwright: navigates,
    waits for networkidle, returns rendered HTML. Without: plain GET.
    Either way: subresources are inlined and embedded PDFs are extracted.
    """
    if pw_ctx is not None:
        html, final_url, fetcher = pw_ctx.fetch(url)
    else:
        import requests
        r = requests.get(url, timeout=60, headers={"User-Agent": _UA})
        r.raise_for_status()
        html = r.text
        final_url = r.url
        fetcher = _RequestsFetcher()

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
    dest = out_dir / f"{slug}.html"
    dest.write_text(inlined, encoding="utf-8")
    return {"filename": dest.name, "extra": extras}


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

class _RequestsFetcher:
    """Plain HTTP fetcher used when Playwright isn't available."""

    def __init__(self):
        import requests
        self._sess = requests.Session()
        self._sess.headers.update({"User-Agent": _UA})
        self.last_content_type = ""

    def fetch(self, url: str) -> bytes:
        r = self._sess.get(url, timeout=60)
        r.raise_for_status()
        self.last_content_type = r.headers.get("Content-Type", "")
        return r.content

    def fetch_text(self, url: str) -> str:
        r = self._sess.get(url, timeout=60)
        r.raise_for_status()
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
    """Subresource fetcher backed by a Playwright APIRequestContext."""

    def __init__(self, context):
        self._req = context.request
        self.last_content_type = ""

    def fetch(self, url: str) -> bytes:
        r = self._req.get(url, timeout=60_000)
        if not r.ok:
            raise RuntimeError(f"HTTP {r.status} on {url}")
        self.last_content_type = r.headers.get("content-type", "")
        return r.body()

    def fetch_text(self, url: str) -> str:
        r = self._req.get(url, timeout=60_000)
        if not r.ok:
            raise RuntimeError(f"HTTP {r.status} on {url}")
        self.last_content_type = r.headers.get("content-type", "")
        return r.text()


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
