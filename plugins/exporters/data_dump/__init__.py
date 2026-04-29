"""
plugins.exporters.data_dump — raw JSON / CSV dump of selected items.

For piping into other tools. No templating — pure Python serialization
over a configurable set of fields.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Optional

from ...base import ExportOption, ExportResult, Exporter, register_exporter


DEFAULT_FIELDS = [
    "title", "url", "source", "kind", "importance",
    "tags", "lists", "keywords", "summary", "notes",
    "folder_path", "date_bookmarked", "archive_url",
]

ALL_FIELDS = DEFAULT_FIELDS + [
    "status", "browser_path", "last_sync",
    "removed_from_browser", "removed_from_source",
]


@register_exporter
class DataDumpExporter(Exporter):
    name = "data_dump"
    label = "JSON / CSV dump"
    description = "Raw structured export of selected items' frontmatter — JSON or CSV."
    supports_themes = False

    def options(self) -> list[ExportOption]:
        return [
            ExportOption("format", "Format", "select",
                         default="json", choices=["json", "csv"]),
            ExportOption("fields", "Fields", "multiselect",
                         default=list(DEFAULT_FIELDS),
                         choices=list(ALL_FIELDS),
                         help="Frontmatter fields to include in each record."),
            ExportOption("pretty", "Pretty-print JSON", "bool", default=True),
        ]

    def export(self, items, *, theme: Optional[str], options: dict, out_dir: Path) -> ExportResult:
        fmt = str(options.get("format") or "json").lower()
        fields = [f for f in (options.get("fields") or DEFAULT_FIELDS) if f]
        if not fields:
            fields = list(DEFAULT_FIELDS)

        records = [{f: _coerce(it.get(f)) for f in fields} for it in items]

        out_dir.mkdir(parents=True, exist_ok=True)
        if fmt == "csv":
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for r in records:
                writer.writerow({k: _flatten_for_csv(v) for k, v in r.items()})
            text = buf.getvalue()
            artifact = out_dir / "data.csv"
            artifact.write_text(text, encoding="utf-8")
            preview = _preview_lines(text, max_lines=30)
            return ExportResult(artifact_path=artifact, preview_text=preview, mime="text/csv")

        # default: json
        indent = 2 if options.get("pretty", True) else None
        text = json.dumps(records, ensure_ascii=False, indent=indent)
        artifact = out_dir / "data.json"
        artifact.write_text(text, encoding="utf-8")
        preview = text if len(text) < 20_000 else text[:20_000] + "\n… (truncated)"
        return ExportResult(artifact_path=artifact, preview_text=preview, mime="application/json")


def _coerce(v):
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v]
    if isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


def _flatten_for_csv(v):
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v)
    return v


def _preview_lines(text: str, *, max_lines: int) -> str:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    return "\n".join(lines[:max_lines]) + f"\n… ({len(lines) - max_lines} more rows)"
