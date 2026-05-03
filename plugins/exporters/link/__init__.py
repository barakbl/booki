"""
plugins.exporters.link — single-file themed link page with inline filter.

Output is one self-contained HTML file (CSS embedded inside the Jinja
template, no external assets). The page ships a tiny inline JS snippet
that filters items as the user types — no build, no framework.
"""

from __future__ import annotations

from datetime import datetime

from jinja2 import Environment, FileSystemLoader, select_autoescape

from core.exporter import Exporter, register_exporter

KIND_GLYPHS = {
    "bookmark": "🔖",
    "video": "🎬",
    "channel": "📺",
    "photo": "🖼",
    "document": "📄",
    "github": "🐙",
    "file": "📁",
    "podcast": "🎧",
    "article": "📰",
}


def _glyph(kind: str) -> str:
    return KIND_GLYPHS.get(kind or "", "·")


@register_exporter
class LinkPageExporter(Exporter):
    slug = "link"
    name = "Link page"
    description = "A single themed HTML page with all your links and inline search."
    applicable_kinds = ["any"]
    execution_mode = "immediate"
    uses_themes = True

    options_schema = [
        {"name": "page_title", "type": "text", "label": "Page title",
         "default": "My Booki Links"},
        {"name": "footer_text", "type": "text", "label": "Footer text",
         "default": "",
         "help": "Optional text rendered at the bottom of the page (credit "
                 "line, contact, license, etc.). Leave blank to omit."},
        {"name": "show_search", "type": "bool", "label": "Show inline search box",
         "default": True,
         "help": "Uncheck to remove the type-to-filter input from the exported page."},
        {"name": "rtl", "type": "bool", "label": "Right-to-left (Arabic / Hebrew)",
         "default": False,
         "help": "Sets dir=\"rtl\" on the page. Themes that support BiDi "
                 "mirror their layout."},
        # NOTE: `group_by` was removed from the Options step — use the
        # Organize tree (step 3) for grouping. The runtime still honors
        # an incoming `group_by` for back-compat with saved configs.
    ]

    def run_immediate(self, items, options, theme, theme_vars, tree=None):
        if theme is None:
            raise ValueError("Link page exporter requires a theme.")
        page_title = options.get("page_title") or "My Booki Links"
        footer_text = (options.get("footer_text") or "").strip()
        show_search = bool(options.get("show_search", True))
        rtl = bool(options.get("rtl", False))
        group_by = (options.get("group_by") or "none").lower()
        groups = _group(items, group_by)

        env = Environment(
            loader=FileSystemLoader(str(theme.path)),
            autoescape=select_autoescape(["html", "j2"]),
        )
        env.filters["glyph"] = _glyph
        tmpl = env.get_template("main.html.j2")
        html = tmpl.render(
            title=page_title,
            footer_text=footer_text,
            show_search=show_search,
            rtl=rtl,
            groups=groups,
            theme_vars=theme_vars,
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
            item_count=len(items),
        )
        ts = datetime.now().strftime("%Y-%m-%dT%H-%M")
        return html.encode("utf-8"), f"booki-links-{ts}.html", "text/html"


def _group(items, group_by: str):
    if group_by == "none" or group_by not in ("source", "kind", "tag"):
        return [{"name": "", "items": list(items)}]

    if group_by == "tag":
        bucket: dict[str, list] = {}
        for it in items:
            tags = it.get("tags") or []
            if not tags:
                bucket.setdefault("(untagged)", []).append(it)
            else:
                for t in tags:
                    bucket.setdefault(str(t), []).append(it)
        return [{"name": k, "items": v} for k, v in sorted(bucket.items())]

    bucket = {}
    for it in items:
        key = str(it.get(group_by) or "(unknown)")
        bucket.setdefault(key, []).append(it)
    return [{"name": k, "items": v} for k, v in sorted(bucket.items())]
