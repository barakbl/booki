"""
plugins.exporters.bookmark_file — Netscape Bookmark File exporter.

Produces the de-facto bookmarks interchange format that every major browser
(Chrome, Firefox, Safari, Edge, Vivaldi, …) imports out of the box. The
output is a single self-contained HTML file shaped as a tree of `<DL>`
folders + `<A HREF>` links.

Spec reference (Netscape):
    <!DOCTYPE NETSCAPE-Bookmark-file-1>
    <DL><p>
        <DT><H3 ADD_DATE="…">Folder</H3>
        <DL><p>
            <DT><A HREF="…" ADD_DATE="…" TAGS="t1,t2">Title</A>
        </DL><p>
    </DL><p>
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from html import escape

from core.exporter import Exporter, register_exporter


@register_exporter
class BookmarkFileExporter(Exporter):
    slug = "bookmark_file"
    name = "Bookmark file (browsers)"
    description = (
        "Netscape Bookmark File — drop the .html into Chrome, Firefox, "
        "Safari or Edge to import every link."
    )
    applicable_kinds = ["any"]
    execution_mode = "immediate"
    uses_themes = False
    supports_hierarchy = True

    options_schema = [
        {"name": "root_folder", "type": "text", "label": "Root folder",
         "default": "Booki Export"},
        {"name": "group_by", "type": "select", "label": "Group by (when no Refine tree)",
         "options": ["none", "source", "kind", "tag", "list"], "default": "none",
         "help": "Used when the Organize step is skipped. With a tree the "
                 "structure you built there wins."},
        {"name": "include_tags", "type": "bool", "label": "Include tags",
         "default": True,
         "help": "Adds a TAGS=\"a,b\" attribute on each link. Firefox and "
                 "Vivaldi pick this up; Chrome ignores it harmlessly."},
        {"name": "only_with_url", "type": "bool", "label": "Skip items without URL",
         "default": True},
    ]

    def run_immediate(self, items, options, theme, theme_vars, tree=None):
        root_folder = (options.get("root_folder") or "Booki Export").strip() or "Booki Export"
        include_tags = bool(options.get("include_tags", True))
        only_with_url = bool(options.get("only_with_url", True))

        by_id: dict[str, dict] = {str(it.get("id") or ""): it for it in items}
        now_ts = _epoch(datetime.now())

        out: list[str] = []
        out.append("<!DOCTYPE NETSCAPE-Bookmark-file-1>")
        out.append("<!-- This is an automatically generated file. -->")
        out.append('<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">')
        out.append("<TITLE>Bookmarks</TITLE>")
        out.append("<H1>Bookmarks</H1>")
        out.append("<DL><p>")
        out.append(f'    <DT><H3 ADD_DATE="{now_ts}">{escape(root_folder)}</H3>')
        out.append("    <DL><p>")

        if tree:
            _emit_tree(out, tree, by_id, include_tags, only_with_url, now_ts, depth=2)
        else:
            group_by = (options.get("group_by") or "none").lower()
            rows = [it for it in items
                    if not only_with_url or (it.get("url") or "").strip()]
            rows = sorted(rows, key=_sort_key)
            groups = _group(rows, group_by)
            if group_by == "none" or len(groups) == 1 and groups[0]["name"] == "":
                for it in (groups[0]["items"] if groups else []):
                    out.append("        " + _link_line(it, include_tags))
            else:
                for g in groups:
                    gname = g["name"] or "(unsorted)"
                    out.append(f'        <DT><H3 ADD_DATE="{now_ts}">{escape(gname)}</H3>')
                    out.append("        <DL><p>")
                    for it in g["items"]:
                        out.append("            " + _link_line(it, include_tags))
                    out.append("        </DL><p>")

        out.append("    </DL><p>")
        out.append("</DL><p>")

        text = "\n".join(out) + "\n"
        ts = datetime.now().strftime("%Y-%m-%dT%H-%M")
        return text.encode("utf-8"), f"booki-bookmarks-{ts}.html", "text/html"


def _emit_tree(out: list[str], nodes: list, by_id: dict, include_tags: bool,
               only_with_url: bool, now_ts: int, depth: int) -> None:
    indent = "    " * depth
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        if n.get("type") == "folder":
            name = str(n.get("name") or "(unsorted)")
            out.append(f'{indent}<DT><H3 ADD_DATE="{now_ts}">{escape(name)}</H3>')
            out.append(f"{indent}<DL><p>")
            _emit_tree(out, n.get("children") or [], by_id,
                       include_tags, only_with_url, now_ts, depth + 1)
            out.append(f"{indent}</DL><p>")
        elif n.get("type") == "item":
            it = by_id.get(str(n.get("item_id") or ""))
            if it is None:
                continue
            if only_with_url and not (it.get("url") or "").strip():
                continue
            out.append(indent + _link_line(it, include_tags))


# ─── helpers ────────────────────────────────────────────────────────────────

def _sort_key(item: dict):
    return (-int(item.get("importance") or 0),
            str(item.get("title") or "").lower())


def _link_line(item: dict, include_tags: bool) -> str:
    url = (item.get("url") or "").strip()
    title = (item.get("title") or "").strip() or url or "(untitled)"
    add_date = _epoch_from_item(item)
    attrs = [f'HREF="{escape(url, quote=True)}"',
             f'ADD_DATE="{add_date}"']
    if include_tags:
        tags = item.get("tags") or []
        if isinstance(tags, list) and tags:
            tag_str = ",".join(re.sub(r"[,\s]+", "_", str(t).strip()) for t in tags if t)
            if tag_str:
                attrs.append(f'TAGS="{escape(tag_str, quote=True)}"')
    icon = (item.get("favicon") or item.get("icon") or "").strip()
    if icon:
        attrs.append(f'ICON="{escape(icon, quote=True)}"')
    return f'<DT><A {" ".join(attrs)}>{escape(title)}</A>'


def _group(items: list[dict], group_by: str):
    if group_by == "none" or group_by not in ("source", "kind", "tag", "list"):
        return [{"name": "", "items": list(items)}]

    if group_by in ("tag", "list"):
        plural_key = "tags" if group_by == "tag" else "lists"
        bucket: dict[str, list] = {}
        for it in items:
            keys = it.get(plural_key) or []
            if not keys:
                bucket.setdefault(f"(no {group_by})", []).append(it)
                continue
            for k in keys:
                bucket.setdefault(str(k), []).append(it)
        return [{"name": k, "items": v} for k, v in sorted(bucket.items())]

    bucket = {}
    for it in items:
        key = str(it.get(group_by) or "(unknown)")
        bucket.setdefault(key, []).append(it)
    return [{"name": k, "items": v} for k, v in sorted(bucket.items())]


def _epoch(dt: datetime) -> int:
    try:
        return int(dt.timestamp())
    except Exception:
        return int(time.time())


_DATE_FORMATS = [
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
]


def _epoch_from_item(item: dict) -> int:
    raw = (item.get("date_bookmarked")
           or item.get("date_added")
           or item.get("last_sync")
           or "")
    if not raw:
        return int(time.time())
    s = str(raw).strip().replace("Z", "")
    # Strip timezone offset like +03:00 — naive parse is enough for ADD_DATE.
    s = re.sub(r"([+-]\d{2}):?\d{2}$", "", s)
    for fmt in _DATE_FORMATS:
        try:
            return _epoch(datetime.strptime(s, fmt))
        except ValueError:
            continue
    return int(time.time())
