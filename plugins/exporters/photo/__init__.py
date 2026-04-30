"""
plugins.exporters.photo — self-contained photo gallery.

Single HTML file (CSS + JS embedded) with a responsive grid, click-to-enlarge
lightbox, and optional inline search. Local images (file:// or absolute paths)
are embedded as base64 data URIs so the exported file works offline. Remote
http(s) images are referenced by URL.

A 25 MB-per-image cap keeps the output file from blowing up on stray RAW or
huge PNG sources; oversize / missing images render as a placeholder tile.
"""

from __future__ import annotations

import base64
import mimetypes
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

from jinja2 import Environment, FileSystemLoader, select_autoescape

from core.exporter import Exporter, register_exporter

MAX_EMBED_BYTES = 25 * 1024 * 1024


def _local_path_from_url(url: str) -> Path | None:
    if not url:
        return None
    if url.startswith("file://"):
        rest = url[len("file://"):]
        # file:///abs/path or file://localhost/abs/path
        if rest.startswith("/"):
            return Path(unquote(rest))
        if rest.startswith("localhost/"):
            return Path(unquote(rest[len("localhost"):]))
    if url.startswith("/"):
        return Path(url)
    return None


def _to_data_uri(p: Path) -> str | None:
    try:
        if not p.is_file():
            return None
        if p.stat().st_size > MAX_EMBED_BYTES:
            return None
        mime, _ = mimetypes.guess_type(str(p))
        if not mime:
            mime = "application/octet-stream"
        b64 = base64.b64encode(p.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{b64}"
    except OSError:
        return None


def _resolve_image(item: dict) -> tuple[str | None, str | None]:
    """Return (src, error). `src` is a string suitable for an <img src>
    attribute in the exported HTML. `error` is set when no src could be
    produced (used to render a placeholder tile)."""
    url = str(item.get("url") or "").strip()
    if not url:
        return None, "no url"
    if url.startswith(("http://", "https://")):
        return url, None
    p = _local_path_from_url(url)
    if p is None:
        return None, "unsupported scheme"
    src = _to_data_uri(p)
    if src is None:
        return None, "missing or too large"
    return src, None


@register_exporter
class PhotoGalleryExporter(Exporter):
    slug = "photo_gallery"
    name = "Photo gallery"
    description = "Self-contained HTML gallery with lightbox and inline search."
    applicable_kinds = ["photo"]
    execution_mode = "immediate"
    uses_themes = True

    options_schema = [
        {"name": "page_title", "type": "text", "label": "Page title",
         "default": "My Photos"},
        {"name": "show_search", "type": "bool", "label": "Show inline search",
         "default": True},
    ]

    def run_immediate(self, items, options, theme, theme_vars):
        if theme is None:
            raise ValueError("Photo gallery exporter requires a theme.")
        page_title = options.get("page_title") or "My Photos"
        show_search = bool(options.get("show_search", True))

        photos = []
        for it in items:
            src, err = _resolve_image(it)
            photos.append({
                "title": it.get("title") or "",
                "url": it.get("url") or "",
                "src": src,
                "error": err,
                "tags": it.get("tags") or [],
                "summary": it.get("summary") or "",
                "importance": int(it.get("importance") or 0),
            })

        env = Environment(
            loader=FileSystemLoader(str(theme.path)),
            autoescape=select_autoescape(["html", "j2"]),
        )
        tmpl = env.get_template("main.html.j2")
        html = tmpl.render(
            title=page_title,
            photos=photos,
            theme_vars=theme_vars,
            show_search=show_search,
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
            item_count=len(photos),
        )
        ts = datetime.now().strftime("%Y-%m-%dT%H-%M")
        return html.encode("utf-8"), f"booki-photos-{ts}.html", "text/html"
