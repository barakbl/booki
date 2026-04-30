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
INTERNAL = {"id", "file"}


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

    def run_immediate(self, items, options, theme, theme_vars):
        fmt = (options.get("format") or "json").lower()
        all_fields = bool(options.get("all_fields", True))
        include_body = bool(options.get("include_body", False))

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
            data = _to_csv(items, fields)
            return data.encode("utf-8"), f"booki-data-{ts}.csv", "text/csv"
        if fmt == "json":
            data = json.dumps(_select(items, fields), indent=2,
                              ensure_ascii=False, default=str)
            return data.encode("utf-8"), f"booki-data-{ts}.json", "application/json"
        if fmt == "yaml":
            data = _to_yaml(_select(items, fields))
            return data.encode("utf-8"), f"booki-data-{ts}.yaml", "application/x-yaml"
        if fmt == "md":
            data = _to_markdown(items, fields, include_body=include_body)
            return data.encode("utf-8"), f"booki-data-{ts}.md", "text/markdown"
        raise ValueError(f"Unknown format: {fmt}")


# ─── helpers ────────────────────────────────────────────────────────────────

def _select(items: list[dict], fields: list[str]) -> list[dict]:
    return [{f: it.get(f) for f in fields} for it in items]


def _to_csv(items: list[dict], fields: list[str]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(fields)
    for it in items:
        w.writerow([_csv_cell(it.get(f)) for f in fields])
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


def _to_markdown(items: list[dict], fields: list[str], *, include_body: bool) -> str:
    lines: list[str] = ["# Booki export", ""]
    for it in items:
        title = str(it.get("title") or "(untitled)").strip()
        url = str(it.get("url") or "").strip()
        lines.append(f"## {title}")
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
    return "\n".join(lines)


def _md_inline(v) -> str:
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    return str(v)
