"""
plugins.exporters.data — raw item data exporter.

Format options: csv / json / yaml / md (default json). The user picks fields
explicitly or accepts "all fields" (the union of frontmatter keys actually
present in the selected items, well-known fields ordered first).

Items are sorted importance-desc, then title-asc, before serialization.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime

from core.exporter import Exporter, register_exporter

# Render these first when listing fields. Anything else is sorted alphabetically.
WELL_KNOWN = [
    "title", "url", "source", "kind", "importance",
    "tags", "lists", "keywords", "summary", "notes",
    "sources", "browser_path", "folder_path",
    "date_bookmarked", "last_sync", "archive_url", "status",
]

# Fields that come from the resolver but plugins shouldn't expose by default.
INTERNAL = {"id", "file", "_path"}


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H-%M")


def _sort_key(item: dict):
    return (-int(item.get("importance") or 0),
            str(item.get("title") or "").lower())


def _ordered_union(items: list[dict], include_body: bool) -> list[str]:
    seen: set[str] = set()
    for it in items:
        seen.update(it.keys())
    seen -= INTERNAL
    if not include_body:
        seen.discard("body")
    return [k for k in WELL_KNOWN if k in seen] + sorted(seen - set(WELL_KNOWN))


@register_exporter
class DataExporter(Exporter):
    slug = "data"
    name = "Data export"
    description = "Raw item data as CSV, JSON, YAML, or Markdown."
    applicable_kinds = ["any"]
    execution_mode = "immediate"
    uses_themes = False
    supports_hierarchy = True

    options_schema = [
        {"name": "format", "type": "select", "label": "Format",
         "options": ["json", "csv", "yaml", "md"], "default": "json"},
        {"name": "all_fields", "type": "bool", "label": "All fields",
         "default": True,
         "help": "Include every available field. Uncheck to pick a subset below."},
        {"name": "fields", "type": "multiselect", "label": "Pick fields",
         "options": [], "default": []},
        {"name": "include_body", "type": "bool", "label": "Include body text",
         "default": False,
         "help": "Append the .md body of each item as a 'body' field."},
    ]

    def options_for(self, items):
        # Populate the multiselect with the fields actually present in the
        # selection, plus 'body' so users can opt it in via the picker too.
        ordered = _ordered_union(items, include_body=True)
        out = []
        for opt in self.options_schema:
            o = dict(opt)
            if o["name"] == "fields":
                o["options"] = ordered
                # Default selection mirrors the "All fields" default — the
                # picker is empty until the user unchecks "All fields".
                o["default"] = []
            out.append(o)
        return out

    def run_immediate(self, items, options, theme, theme_vars, tree=None):
        fmt = (options.get("format") or "json").lower()
        all_fields = bool(options.get("all_fields", True))
        include_body = bool(options.get("include_body", False))

        # When the wizard's Refine step provided a tree, items already arrive
        # in tree order with a `_path` field. Otherwise apply the default
        # importance-desc sort.
        used_tree = bool(tree) or any(("_path" in it) for it in items)
        if not used_tree:
            items = sorted(items, key=_sort_key)

        if all_fields:
            fields = _ordered_union(items, include_body=include_body)
        else:
            fields = [f for f in (options.get("fields") or []) if isinstance(f, str)]
            if include_body and "body" not in fields:
                fields.append("body")
            if not fields:
                raise ValueError("No fields selected")

        ts = _ts()
        if fmt == "csv":
            csv_fields = list(fields)
            if used_tree and "path" not in csv_fields:
                csv_fields = ["path"] + csv_fields
            data = _to_csv(items, csv_fields, used_tree=used_tree)
            return data.encode("utf-8"), f"booki-data-{ts}.csv", "text/csv"
        if fmt == "json":
            payload = (_nest(items, fields) if used_tree
                       else _select(items, fields))
            data = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
            return data.encode("utf-8"), f"booki-data-{ts}.json", "application/json"
        if fmt == "yaml":
            payload = (_nest(items, fields) if used_tree
                       else _select(items, fields))
            data = _to_yaml_payload(payload) if used_tree else _to_yaml(_select(items, fields))
            return data.encode("utf-8"), f"booki-data-{ts}.yaml", "application/x-yaml"
        if fmt == "md":
            data = _to_markdown(items, fields, include_body=include_body, used_tree=used_tree)
            return data.encode("utf-8"), f"booki-data-{ts}.md", "text/markdown"
        raise ValueError(f"Unknown format: {fmt}")


# ─── helpers ────────────────────────────────────────────────────────────────

def _select(items: list[dict], fields: list[str]) -> list[dict]:
    return [{f: it.get(f) for f in fields} for it in items]


def _path_str(it: dict) -> str:
    return "/".join(it.get("_path") or [])


def _nest(items: list[dict], fields: list[str]) -> dict:
    """
    Build a nested dict mirroring the tree. Folders become dict keys whose
    values are nested dicts; items at each level live under "_items" as a
    list of selected-field rows. `_items` was chosen over `items` so it
    can't collide with a folder named "items".
    """
    root: dict = {}
    for it in items:
        node = root
        for part in (it.get("_path") or []):
            sub = node.setdefault(part, {})
            if not isinstance(sub, dict):
                sub = {}
                node[part] = sub
            node = sub
        node.setdefault("_items", []).append({f: it.get(f) for f in fields})
    return root


def _to_yaml_payload(payload) -> str:
    """YAML serializer for the nested-dict shape produced by `_nest`."""
    lines: list[str] = []

    def emit(obj, indent: int):
        pad = "  " * indent
        if isinstance(obj, dict):
            for k in sorted(obj.keys(), key=lambda x: (x != "_items", str(x))):
                v = obj[k]
                if isinstance(v, dict):
                    lines.append(f"{pad}{k}:")
                    emit(v, indent + 1)
                elif isinstance(v, list):
                    lines.append(f"{pad}{k}:")
                    for row in v:
                        first = True
                        for fk, fv in row.items():
                            prefix = "- " if first else "  "
                            lines.append(f"{pad}  {prefix}{fk}: {_yaml_value(fv)}")
                            first = False
                        if first:
                            lines.append(f"{pad}  - {{}}")
                else:
                    lines.append(f"{pad}{k}: {_yaml_value(v)}")
        else:
            lines.append(f"{pad}{_yaml_value(obj)}")

    emit(payload, 0)
    return "\n".join(lines) + ("\n" if lines else "")


def _to_csv(items: list[dict], fields: list[str], *, used_tree: bool = False) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(fields)
    for it in items:
        row = []
        for f in fields:
            if f == "path" and used_tree:
                row.append(_path_str(it))
            else:
                row.append(_csv_cell(it.get(f)))
        w.writerow(row)
    return buf.getvalue()


def _csv_cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, list):
        return "|".join(str(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _to_yaml(rows: list[dict]) -> str:
    """
    Tiny YAML-ish writer for a list of flat dicts. Avoids the PyYAML dependency.
    Complex values get JSON-encoded, which is valid YAML flow syntax.
    """
    lines: list[str] = []
    for r in rows:
        first = True
        for k, v in r.items():
            prefix = "- " if first else "  "
            lines.append(f"{prefix}{k}: {_yaml_value(v)}")
            first = False
        if first:
            lines.append("- {}")
    return "\n".join(lines) + ("\n" if lines else "")


def _yaml_value(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    s = str(v)
    if any(c in s for c in ":#{}[]&*!|>'\","):
        return json.dumps(s, ensure_ascii=False)
    return s


def _to_markdown(items: list[dict], fields: list[str], *,
                 include_body: bool, used_tree: bool = False) -> str:
    """
    When a tree is in play we emit folder headings (## level per nesting),
    items as ### subheadings. Without a tree we keep the original flat
    "## title per item" shape.
    """
    lines: list[str] = ["# Booki export", ""]

    def emit_item(it: dict, item_level: int):
        title = str(it.get("title") or "(untitled)").strip()
        url = str(it.get("url") or "").strip()
        lines.append(f"{'#' * item_level} {title}")
        if url:
            lines.append(f"<{url}>")
        lines.append("")
        for f in fields:
            if f in ("title", "url", "body"):
                continue
            v = it.get(f)
            if v in (None, "", [], {}):
                continue
            lines.append(f"- **{f}**: {_md_inline(v)}")
        if include_body:
            body = (it.get("body") or "").strip()
            if body:
                lines.append("")
                lines.append(body)
        lines.append("")
        lines.append("---")
        lines.append("")

    if not used_tree:
        for it in items:
            emit_item(it, 2)
        return "\n".join(lines)

    # Hierarchy: emit folder headings as we walk path changes.
    last_path: list[str] = []
    for it in items:
        path = it.get("_path") or []
        # Walk down to the common prefix, then write any new folder headings.
        common = 0
        while common < len(path) and common < len(last_path) and path[common] == last_path[common]:
            common += 1
        for i in range(common, len(path)):
            level = min(2 + i, 5)        # cap at h5 so item h6 still renders
            lines.append(f"{'#' * level} {path[i]}")
            lines.append("")
        item_level = min(2 + len(path), 6)
        emit_item(it, item_level)
        last_path = list(path)
    return "\n".join(lines)


def _md_inline(v) -> str:
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    return str(v)
