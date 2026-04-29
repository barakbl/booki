"""
plugins.exporters.link_page — themed HTML page listing selected bookmarks.

Renders a single `index.html` via a Jinja2 theme. Ships a `default` theme in
`./themes/default/`; users can add more under `<themes_dir>/link_page/<name>/`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from ...base import ExportOption, ExportResult, Exporter, Item, register_exporter


@register_exporter
class LinkPageExporter(Exporter):
    name = "link_page"
    label = "Link page (HTML)"
    description = "A single themed HTML page listing your selected items."
    supports_themes = True
    default_theme = "default"

    def options(self) -> list[ExportOption]:
        return [
            ExportOption("title", "Page title", "string", default="My Bookmarks"),
            ExportOption("footer", "Footer text", "string",
                         default="Brewed with Booki",
                         help="Plain text shown in the page footer. Edit inline in preview to override."),
            ExportOption("group_by", "Group by", "select",
                         default="none",
                         choices=["none", "source", "kind", "tag"],
                         help="Group items by source, kind, tag, or leave flat."),
            ExportOption("include_summary", "Include summaries", "bool", default=True),
            ExportOption("include_tags", "Include tags", "bool", default=True),
            ExportOption("include_notes", "Include notes", "bool", default=False),
        ]

    def export(self, items: list[Item], *, theme: Optional[str],
               options: dict, out_dir: Path) -> ExportResult:
        from jinja2 import Environment, FileSystemLoader, select_autoescape

        theme_name = theme or self.default_theme or "default"
        builtin_root = Path(__file__).parent / "themes"
        user_root = Path(options.get("_themes_root") or "") / self.name if options.get("_themes_root") else None

        search_paths: list[str] = []
        if user_root and (user_root / theme_name).exists():
            search_paths.append(str(user_root / theme_name))
        search_paths.append(str(builtin_root / theme_name))
        if not (builtin_root / theme_name).exists():
            # fall back to built-in default if the requested theme is missing
            search_paths.append(str(builtin_root / "default"))

        env = Environment(
            loader=FileSystemLoader(search_paths),
            autoescape=select_autoescape(["html", "htm", "j2"]),
            trim_blocks=True, lstrip_blocks=True,
        )
        tpl = env.get_template("main.html.j2")

        # Per-item inline-preview overrides: { item_id: {title?, summary?, notes?} }
        overrides = options.get("_overrides") or {}
        if overrides:
            items = [_apply_overrides(it, overrides.get(str(it.get("id")))) for it in items]

        groups = _group_items(items, options.get("group_by", "none"))
        html = tpl.render(
            title=options.get("title") or "My Bookmarks",
            footer=options.get("footer") or "Brewed with Booki",
            groups=groups,
            item_count=sum(len(g["entries"]) for g in groups),
            include_summary=bool(options.get("include_summary", True)),
            include_tags=bool(options.get("include_tags", True)),
            include_notes=bool(options.get("include_notes", False)),
        )

        # Inline local <link rel=stylesheet> into <style> blocks so the artifact
        # is a single self-contained file (no sibling assets needed) and the
        # sandboxed preview renders identically.
        inlined = _inline_local_styles(html, [Path(p) for p in search_paths])

        out_dir.mkdir(parents=True, exist_ok=True)
        artifact = out_dir / "index.html"
        artifact.write_text(inlined, encoding="utf-8")
        # Copy any non-stylesheet static assets (images, fonts) alongside.
        _copy_static(Path(search_paths[0]), out_dir, skip_suffixes={".css"})

        return ExportResult(artifact_path=artifact, preview_text=inlined, mime="text/html")


def _apply_overrides(item: dict, ov: Optional[dict]) -> dict:
    if not ov:
        return item
    out = dict(item)
    for key in ("title", "summary", "notes"):
        if key in ov and ov[key] is not None:
            out[key] = ov[key]
    return out


def _group_items(items: list, group_by: str) -> list[dict]:
    """
    Normalize items into a [{label, items: [...]}] shape.
    Each passed item is a dict-like frontmatter (the wizard passes dicts, not
    Source Items — see web.py adapter). `group_by` ∈ none/source/kind/tag.

    Returns [{label, entries: [...]}] — note `entries`, not `items`, because
    Jinja2 shadows `.items` with the dict's builtin method.
    """
    if group_by == "none" or not group_by:
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


_LOCAL_CSS_LINK_RE = re.compile(
    r"""<link\b[^>]*?\brel=["']stylesheet["'][^>]*?\bhref=["'](?!https?://|//|data:)([^"']+)["'][^>]*?>""",
    re.IGNORECASE,
)


def _inline_local_styles(html: str, theme_dirs: list[Path]) -> str:
    """Replace <link rel=stylesheet href=relative.css> with inline <style> blocks
    so the iframe preview (srcdoc, no base URL) renders the same as the artifact."""
    def repl(m):
        href = m.group(1)
        for d in theme_dirs:
            p = (d / href).resolve()
            if p.is_file():
                try:
                    return f"<style>\n{p.read_text(encoding='utf-8')}\n</style>"
                except OSError:
                    return m.group(0)
        return m.group(0)
    return _LOCAL_CSS_LINK_RE.sub(repl, html)


def _copy_static(theme_dir: Path, out_dir: Path,
                 skip_suffixes: Optional[set[str]] = None) -> None:
    """Copy non-template files from the theme dir next to the rendered HTML.
    `skip_suffixes` (e.g. {".css"}) skips assets already inlined into the HTML."""
    import shutil
    if not theme_dir.exists():
        return
    skip = {s.lower() for s in (skip_suffixes or set())}
    for p in theme_dir.iterdir():
        if not p.is_file():
            continue
        if p.suffix in (".j2", ".jinja", ".jinja2"):
            continue
        if p.suffix.lower() in skip:
            continue
        shutil.copy2(p, out_dir / p.name)
