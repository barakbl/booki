"""
Source plugin base — Item + Source ABC + a tiny registry.

A *Source* is anything that produces Items worth indexing: a browser's
bookmarks, a YouTube account's liked videos, a local database of links,
an RSS subscription list, ...

Each Source yields Items. The rest of the pipeline (sync → store → ingest
→ chat) is source-agnostic and just works on Items.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional


# ─── Item — the universal currency ────────────────────────────────────────────

@dataclass
class Item:
    """
    A single thing to index: a bookmark, a video, a channel, a history entry.

    `source`  — short slug of the producing plugin ("chrome", "youtube", …).
    `kind`    — semantic type ("bookmark", "video", "channel", "history", …).
    `path`    — directory hierarchy to place the item under, rooted at the
                source's own top-level dir. e.g. ["Chrome", "Bookmarks Bar", "AI"]
                or ["youtube", "videos", "Fireship"].
    `extras`  — free-form source-specific frontmatter fields. Preserved verbatim
                on write and merged on re-fetch.
    """
    title: str
    url: str
    source: str
    kind: str
    path: list[str] = field(default_factory=list)
    date_added: Optional[str] = None
    extras: dict = field(default_factory=dict)

    @property
    def url_key(self) -> str:
        return self.url.rstrip("/").lower()


# ─── Source ABC ───────────────────────────────────────────────────────────────

class Source(ABC):
    """
    A plugin that produces Items.

    Subclasses declare a class-level `name` (short slug — also used as the
    CLI token and the top-level output directory) and implement `is_available`
    and `fetch`.

    Config is injected via `configure(cfg)` before any other call; the default
    stores it on `self.cfg` which most subclasses can read directly.
    """

    name: str = ""          # must override

    def __init__(self):
        self.cfg: dict = {}

    def configure(self, cfg: dict) -> None:
        self.cfg = cfg or {}

    @abstractmethod
    def is_available(self) -> bool:
        """True if this source can run right now (creds present, files exist, …)."""

    @abstractmethod
    def fetch(self) -> Iterable[Item]:
        """Yield all items. Invocations are expected to be idempotent."""

    # Optional hook — override if the source wants to explain why it's unavailable.
    def availability_hint(self) -> str:
        return ""

    # Optional hook — describe source-specific frontmatter fields so the web UI
    # can render them without hard-coding. Each entry is a dict:
    #   {name, label, group?, format?, kinds?}
    # format ∈ {"text","number","date","duration","bool","list","url","tags"}
    # kinds restricts the spec to items of those kinds (empty = all).
    @classmethod
    def field_specs(cls) -> list[dict]:
        return []

    # Optional hook — declare any new `kind` slugs this plugin introduces so
    # the CLI fzf preview and the web UI's row badge can pick a glyph without
    # hard-coding it. Each entry: {slug, glyph?, label?}. Plugins that only
    # produce items with kind=bookmark (the universal default) don't need to
    # declare anything.
    @classmethod
    def kind_specs(cls) -> list[dict]:
        return []


# ─── Registry ─────────────────────────────────────────────────────────────────

_REGISTRY: dict[str, type[Source]] = {}


def register(cls: type[Source]) -> type[Source]:
    """Decorator — register a Source subclass by its class-level `name`."""
    if not getattr(cls, "name", ""):
        raise ValueError(f"{cls.__name__} must set a class-level 'name'")
    _REGISTRY[cls.name] = cls
    return cls


def get_source(name: str) -> Optional[type[Source]]:
    return _REGISTRY.get(name)


def all_source_names() -> list[str]:
    return sorted(_REGISTRY)


def iter_registered() -> Iterator[tuple[str, type[Source]]]:
    for name in all_source_names():
        yield name, _REGISTRY[name]


# ─── Exporter ABC ─────────────────────────────────────────────────────────────

@dataclass
class ExportOption:
    """
    One knob an exporter exposes to the wizard. The web UI renders a form
    field from this spec; the value is passed back as part of `options` in
    Exporter.export().
    """
    name: str
    label: str
    type: str = "string"          # "string"|"bool"|"number"|"select"|"multiselect"
    default: Any = None
    choices: Optional[list[Any]] = None   # for "select"/"multiselect"
    help: str = ""


@dataclass
class ExportResult:
    """
    What Exporter.export() returns.

    `artifact_path` — the file (or directory) the user will download.
    `preview_text`  — inline preview for the wizard; None for binary / heavy outputs.
    `mime`          — "text/html", "text/plain", "application/json", "text/csv",
                      "application/zip", ...
    """
    artifact_path: Path
    preview_text: Optional[str] = None
    mime: str = "text/plain"


class Exporter(ABC):
    """
    A plugin that turns a selection of Items into an artifact.

    Subclasses declare a class-level `name` (slug) and `label` (human text),
    implement `export`, and optionally declare Jinja2 theme support.
    """

    name: str = ""                  # must override — slug for registry + config
    label: str = ""                 # human-readable dropdown label
    description: str = ""           # one-line blurb shown in the wizard
    supports_themes: bool = False
    default_theme: Optional[str] = None

    def __init__(self):
        self.cfg: dict = {}

    def configure(self, cfg: dict) -> None:
        self.cfg = cfg or {}

    def options(self) -> list[ExportOption]:
        """Override to expose wizard form fields."""
        return []

    def available_themes(self, themes_root: Path) -> list[dict]:
        """
        Return themes this exporter can render with. Default: scans
            themes_root/<name>/*/              (user themes — take precedence)
            <this-package>/themes/*/           (built-in)
        Each returned dict: {name, label, description, path, builtin}.
        Subclasses that need custom discovery can override.
        """
        if not self.supports_themes:
            return []
        import sys
        out: dict[str, dict] = {}
        mod = sys.modules.get(self.__class__.__module__)
        builtin_dir = Path(mod.__file__).parent / "themes" if mod and getattr(mod, "__file__", None) else None
        if builtin_dir is None:
            builtin_dir = Path()
        # Built-ins first so user themes of the same name override.
        for base, builtin in [(builtin_dir, True), (themes_root / self.name, False)]:
            if not base.exists():
                continue
            for d in sorted(p for p in base.iterdir() if p.is_dir()):
                out[d.name] = {
                    "name": d.name,
                    "label": d.name.replace("_", " ").replace("-", " ").title(),
                    "description": "",
                    "path": str(d),
                    "builtin": builtin,
                }
        return list(out.values())

    @abstractmethod
    def export(self, items: list[Item], *, theme: Optional[str],
               options: dict, out_dir: Path) -> ExportResult:
        """Render `items` into an artifact under `out_dir`. Return an ExportResult."""


_EXPORTERS: dict[str, type[Exporter]] = {}


def register_exporter(cls: type[Exporter]) -> type[Exporter]:
    """Decorator — register an Exporter subclass by its class-level `name`."""
    if not getattr(cls, "name", ""):
        raise ValueError(f"{cls.__name__} must set a class-level 'name'")
    _EXPORTERS[cls.name] = cls
    return cls


def get_exporter(name: str) -> Optional[type[Exporter]]:
    return _EXPORTERS.get(name)


def all_exporter_names() -> list[str]:
    return sorted(_EXPORTERS)


def iter_exporters() -> Iterator[tuple[str, type[Exporter]]]:
    for name in all_exporter_names():
        yield name, _EXPORTERS[name]


# ─── Enricher ABC ─────────────────────────────────────────────────────────────

class Enricher(ABC):
    """
    A plugin that adds metadata fields to existing Items in-place.

    Where Source plugins *produce* new Items, Enricher plugins inspect items
    that already exist and return additional frontmatter fields to merge in.
    They run from `booki sync --enrich-meta` (one pass over bookmarks/, every
    applicable enricher applied to every applicable item).

    Subclasses declare a class-level `name` (slug) and implement:
        is_applicable(fm)  → bool   — does this enricher want this item?
        enrich(fm)         → dict?  — fields to merge into the item, or None.

    Config is injected via `configure(cfg)`; `cfg` is the `[enrichers.<name>]`
    subtable from config.toml (may be empty).
    """

    name: str = ""                    # must override

    def __init__(self):
        self.cfg: dict = {}

    def configure(self, cfg: dict) -> None:
        self.cfg = cfg or {}

    @abstractmethod
    def is_applicable(self, fm: dict) -> bool:
        """Return True if this enricher wants to operate on the given item."""

    @abstractmethod
    def enrich(self, fm: dict) -> Optional[dict]:
        """
        Return a dict of new/updated frontmatter fields to merge into the
        item, or None if nothing was found / fetch failed. Should not raise
        on remote-side errors — log and return None.
        """

    @classmethod
    def field_specs(cls) -> list[dict]:
        """Same shape as Source.field_specs — used by the web UI."""
        return []

    @classmethod
    def kind_specs(cls) -> list[dict]:
        """Same shape as Source.kind_specs — declare any new `kind` slugs
        this enricher promotes items to."""
        return []


_ENRICHERS: dict[str, type[Enricher]] = {}


def register_enricher(cls: type[Enricher]) -> type[Enricher]:
    """Decorator — register an Enricher subclass by its class-level `name`."""
    if not getattr(cls, "name", ""):
        raise ValueError(f"{cls.__name__} must set a class-level 'name'")
    _ENRICHERS[cls.name] = cls
    return cls


def get_enricher(name: str) -> Optional[type[Enricher]]:
    return _ENRICHERS.get(name)


def all_enricher_names() -> list[str]:
    return sorted(_ENRICHERS)


def iter_enrichers() -> Iterator[tuple[str, type[Enricher]]]:
    for name in all_enricher_names():
        yield name, _ENRICHERS[name]


# ─── Tab contribution ─────────────────────────────────────────────────────────
#
# Plugins can contribute a top-level UI tab (rendered as one of the items in
# the main tab bar). The Python side declares only metadata + the static
# assets to load; tab behavior (mount, onShow, onHide) lives entirely in the
# plugin's JS module, which calls `booki.tabs.implement(id, { ... })` after
# being imported by the frontend bootstrap.
#
# Plugin layout convention:
#   plugins/<slug>/web/static/<module.js>   ← served at
#       /plugins/<slug>/static/<module.js>
#

@dataclass
class TabContribution:
    """
    A top-level UI tab declared by a plugin.

    `id`        — unique slug (used as the tab's DOM data-tab and the JS
                  registry key).
    `label`     — human-readable label rendered in the tab bar.
    `icon`      — optional emoji / single-char glyph shown next to label.
    `order`     — sort key. Built-ins use 10/20/30/40/90; plugins default
                  to 100 so they slot in before "Manage".
    `plugin`    — slug of the contributing plugin package (auto-inferred
                  from the caller's `__module__` when omitted).
    `static_dir` — directory inside the plugin's package that holds the
                   JS/CSS assets. Defaults to `web/static`.
    `module`    — path to the entry JS module *within* `static_dir`. The
                  frontend dynamically `import()`s this. Empty = no module
                  (rare; pure-CSS or chrome-only tab).
    `styles`    — list of CSS paths within `static_dir` to add as
                  `<link rel="stylesheet">` before the module loads.
    """
    id: str
    label: str = ""
    icon: str = ""
    order: int = 100
    plugin: str = ""
    static_dir: str = "web/static"
    module: str = ""
    styles: list[str] = field(default_factory=list)


_TABS: dict[str, TabContribution] = {}


def register_tab(contrib: TabContribution) -> TabContribution:
    """
    Register a plugin tab. Call from a plugin's `__init__.py`:

        from plugins.base import TabContribution, register_tab
        register_tab(TabContribution(
            id="myplugin", label="My Plugin", icon="🎨",
            module="tab.js",
        ))

    `plugin` is auto-inferred from the caller's `__name__` (the package slug
    under `plugins/`) when not set explicitly.
    """
    if not contrib.id:
        raise ValueError("TabContribution.id is required")
    if not contrib.plugin:
        import inspect
        mod = inspect.stack()[1].frame.f_globals.get("__name__", "")
        # plugins.<slug>... → <slug>
        if mod.startswith("plugins."):
            parts = mod.split(".")
            # plugins.enrichers.foo → enrichers.foo (so static lives under
            # plugins/enrichers/foo/web/static); we keep the full sub-path.
            contrib.plugin = "/".join(parts[1:]) if len(parts) > 1 else ""
    if not contrib.plugin:
        raise ValueError("TabContribution.plugin could not be inferred — "
                         "set it explicitly")
    _TABS[contrib.id] = contrib
    return contrib


def get_tab(tab_id: str) -> Optional[TabContribution]:
    return _TABS.get(tab_id)


def all_tab_ids() -> list[str]:
    return [tid for tid, _ in iter_tabs()]


def iter_tabs() -> Iterator[tuple[str, TabContribution]]:
    """Yield (id, contribution) sorted by (order, id)."""
    for tid, c in sorted(_TABS.items(), key=lambda kv: (kv[1].order, kv[0])):
        yield tid, c


# ─── Kind aggregator ──────────────────────────────────────────────────────────
#
# Every place that needs to render a kind glyph (CLI fzf preview in
# core/browse.py, the search-row badge in web/app.js) builds its glyph map
# from this aggregator. Adding a new enricher / source that introduces a
# new `kind` only requires declaring `kind_specs()` on that plugin.

# Universal default. The pipeline uses `kind=bookmark` when nothing more
# specific is set — declared here so `bookmark` always has a glyph even when
# no plugin claims it.
CORE_KINDS: tuple[dict, ...] = (
    {"slug": "bookmark", "glyph": "🔖", "label": "Bookmark"},
)


def all_kind_specs() -> dict[str, dict]:
    """Aggregate `kind_specs()` from every registered source and enricher,
    keyed by slug. Plugin declarations take precedence over CORE_KINDS so a
    plugin can rebrand `bookmark` if it really wants to.

    Each value: {slug, glyph, label, plugin}. `plugin` is the registry slug
    of whichever plugin first declared the kind.
    """
    out: dict[str, dict] = {}

    def _absorb(plugin_name: str, specs: list[dict]) -> None:
        for spec in specs or []:
            slug = (spec or {}).get("slug")
            if not slug or slug in out:
                continue
            out[slug] = {
                "slug":   slug,
                "glyph":  spec.get("glyph") or "",
                "label":  spec.get("label") or slug.replace("_", " ").title(),
                "plugin": plugin_name,
            }

    for name, cls in iter_registered():
        _absorb(name, cls.kind_specs())
    for name, cls in iter_enrichers():
        _absorb(name, cls.kind_specs())

    for spec in CORE_KINDS:
        if spec["slug"] not in out:
            out[spec["slug"]] = {**spec, "plugin": "core"}

    return out
