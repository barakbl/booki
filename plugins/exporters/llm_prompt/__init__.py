"""
plugins.exporters.llm_prompt — bundle selected items into a single prompt file.

Renders a Markdown file with a preamble + one block per item (title, URL,
summary, keywords, tags, notes). Drop it into NotebookLM/Claude/ChatGPT to
summarize, cluster, or answer questions against your curated set.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ...base import ExportOption, ExportResult, Exporter, register_exporter


PREAMBLES = {
    "summarize": (
        "You will be given a curated collection of bookmarks (with summaries, "
        "tags, and notes). Produce a clear thematic summary of what I seem to "
        "care about, group related items, and call out anything unusually "
        "important (high `importance` score) or out-of-place."
    ),
    "qa": (
        "The following is my personal bookmark collection with enriched "
        "metadata. Use ONLY this material to answer questions I ask afterwards. "
        "Cite items by title. If the answer is not in the collection, say so."
    ),
    "outline": (
        "Given the following curated bookmarks, produce a hierarchical outline "
        "grouping them by topic and sub-topic. For each group, write one "
        "sentence explaining what connects the items."
    ),
    "none": "",
}


@register_exporter
class LlmPromptExporter(Exporter):
    name = "llm_prompt"
    label = "LLM prompt bundle"
    description = "A Markdown file with a system preamble + one block per item — paste into NotebookLM/Claude/ChatGPT."
    supports_themes = True
    default_theme = "default"

    def options(self) -> list[ExportOption]:
        return [
            ExportOption("preamble_preset", "Preamble", "select",
                         default="summarize",
                         choices=list(PREAMBLES.keys()),
                         help="Preset instructions prepended to the bundle."),
            ExportOption("include_body", "Include markdown body", "bool",
                         default=False,
                         help="Include each item's full .md body (can get very long)."),
            ExportOption("include_url", "Include URLs", "bool", default=True),
            ExportOption("include_notes", "Include notes", "bool", default=True),
        ]

    def export(self, items, *, theme: Optional[str], options: dict, out_dir: Path) -> ExportResult:
        from jinja2 import Environment, FileSystemLoader

        theme_name = theme or self.default_theme or "default"
        builtin_root = Path(__file__).parent / "themes"
        user_root = Path(options.get("_themes_root") or "") / self.name if options.get("_themes_root") else None

        search_paths: list[str] = []
        if user_root and (user_root / theme_name).exists():
            search_paths.append(str(user_root / theme_name))
        search_paths.append(str(builtin_root / theme_name))
        if not (builtin_root / theme_name).exists():
            search_paths.append(str(builtin_root / "default"))

        env = Environment(
            loader=FileSystemLoader(search_paths),
            autoescape=False,
            trim_blocks=True, lstrip_blocks=True,
        )
        tpl = env.get_template("main.md.j2")

        preset = str(options.get("preamble_preset") or "summarize")
        text = tpl.render(
            preamble=PREAMBLES.get(preset, PREAMBLES["summarize"]),
            items=items,
            item_count=len(items),
            include_body=bool(options.get("include_body", False)),
            include_url=bool(options.get("include_url", True)),
            include_notes=bool(options.get("include_notes", True)),
        )

        out_dir.mkdir(parents=True, exist_ok=True)
        artifact = out_dir / "prompt.md"
        artifact.write_text(text, encoding="utf-8")
        return ExportResult(artifact_path=artifact, preview_text=text, mime="text/markdown")
