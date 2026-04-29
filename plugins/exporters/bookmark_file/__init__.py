"""
plugins.exporters.bookmark_file — Netscape Bookmark File Format exporter.

Produces the de-facto standard `bookmarks.html` that Chrome, Firefox, Safari,
Edge and most bookmark managers can import directly. Output is a single
self-contained HTML file using the well-known <DL>/<DT>/<H3>/<A> structure.
"""

from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path
from typing import Optional

from ...base import ExportOption, ExportResult, Exporter, register_exporter


@register_exporter
class BookmarkFileExporter(Exporter):
    name = "bookmark_file"
    label = "Bookmark file (Netscape HTML)"
    description = "Standard browser-importable bookmarks.html (Chrome / Firefox / Safari / Edge)."
    supports_themes = False

    def options(self) -> list[ExportOption]:
        return [
            ExportOption("root_folder", "Root folder name", "string", default="Booki",
                         help="Top-level folder under which all exported items live."),
            ExportOption("group_by", "Group by", "select",
                         default="folder",
                         choices=["folder", "source", "list", "tag", "flat"],
                         help="folder = original browser hierarchy; flat = all in root."),
            ExportOption("include_description", "Include descriptions", "bool", default=True,
                         help="Emit <DD> with the summary (or notes if no summary)."),
            ExportOption("include_tags", "Include TAGS attribute", "bool", default=True,
                         help="Firefox/Pinboard-style TAGS=\"a,b,c\" on each <A>."),
        ]

    def export(self, items, *, theme: Optional[str], options: dict, out_dir: Path) -> ExportResult:
        root_folder = (options.get("root_folder") or "Booki").strip() or "Booki"
        group_by = str(options.get("group_by") or "folder").lower()
        include_desc = bool(options.get("include_description", True))
        include_tags = bool(options.get("include_tags", True))

        tree = _build_tree(items, group_by)

        lines: list[str] = [
            "<!DOCTYPE NETSCAPE-Bookmark-file-1>",
            "<!-- This is an automatically generated file.",
            "     It will be read and overwritten.",
            "     DO NOT EDIT! -->",
            '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">',
            "<TITLE>Bookmarks</TITLE>",
            "<H1>Bookmarks</H1>",
            "<DL><p>",
        ]
        # Wrap everything under a single named root folder.
        lines.append(f'    <DT><H3>{html.escape(root_folder)}</H3>')
        lines.append("    <DL><p>")
        _emit_node(tree, indent=8, lines=lines,
                   include_desc=include_desc, include_tags=include_tags)
        lines.append("    </DL><p>")
        lines.append("</DL><p>")

        text = "\n".join(lines) + "\n"
        out_dir.mkdir(parents=True, exist_ok=True)
        artifact = out_dir / "bookmarks.html"
        artifact.write_text(text, encoding="utf-8")
        return ExportResult(artifact_path=artifact, preview_text=text, mime="text/html")


# ─── Tree building ───────────────────────────────────────────────────

def _build_tree(items: list[dict], group_by: str) -> dict:
    """
    Returns a nested dict: {"_items": [...], "<sub>": {...}, ...}
    where "_items" holds the leaf bookmarks at that node.
    """
    root: dict = {"_items": []}

    def insert(path_parts: list[str], item: dict) -> None:
        node = root
        for part in path_parts:
            part = part.strip()
            if not part:
                continue
            node = node.setdefault(part, {"_items": []})
        node["_items"].append(item)

    for it in items:
        if group_by == "flat":
            insert([], it)
        elif group_by == "folder":
            raw = str(it.get("folder_path") or it.get("browser_path") or "").strip()
            parts = [p for p in raw.replace("\\", "/").split("/") if p]
            insert(parts, it)
        elif group_by == "source":
            insert([str(it.get("source") or "—")], it)
        elif group_by == "list":
            lists = [str(l) for l in (it.get("lists") or []) if l]
            if not lists:
                insert(["(no list)"], it)
            else:
                # An item in N lists shows up under each list folder.
                for lst in lists:
                    insert([lst], it)
        elif group_by == "tag":
            tags = [str(t) for t in (it.get("tags") or []) if t]
            if not tags:
                insert(["(untagged)"], it)
            else:
                for tag in tags:
                    insert([tag], it)
        else:
            insert([], it)

    return root


# ─── Emitting ────────────────────────────────────────────────────────

def _emit_node(node: dict, *, indent: int, lines: list[str],
               include_desc: bool, include_tags: bool) -> None:
    pad = " " * indent
    # Sub-folders first (sorted), then items.
    sub_keys = sorted(k for k in node.keys() if k != "_items")
    for key in sub_keys:
        lines.append(f'{pad}<DT><H3>{html.escape(key)}</H3>')
        lines.append(f"{pad}<DL><p>")
        _emit_node(node[key], indent=indent + 4, lines=lines,
                   include_desc=include_desc, include_tags=include_tags)
        lines.append(f"{pad}</DL><p>")

    for it in node.get("_items", []):
        url = str(it.get("url") or "").strip()
        if not url:
            continue
        title = str(it.get("title") or url)
        attrs = [f'HREF="{html.escape(url, quote=True)}"']
        ts = _to_unix(it.get("date_bookmarked"))
        if ts is not None:
            attrs.append(f'ADD_DATE="{ts}"')
        if include_tags:
            tags = [str(t) for t in (it.get("tags") or []) if t]
            if tags:
                attrs.append(f'TAGS="{html.escape(",".join(tags), quote=True)}"')
        lines.append(f'{pad}<DT><A {" ".join(attrs)}>{html.escape(title)}</A>')
        if include_desc:
            desc = (str(it.get("summary") or "").strip()
                    or str(it.get("notes") or "").strip())
            if desc:
                lines.append(f"{pad}<DD>{html.escape(desc)}")


def _to_unix(value) -> Optional[int]:
    """Best-effort: convert ISO date / datetime / int → unix seconds."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    # Try a handful of common shapes; fall through silently on failure.
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(s, fmt).timestamp())
        except ValueError:
            continue
    try:
        # ISO with timezone, e.g. "2024-09-01T12:34:56+00:00"
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None
