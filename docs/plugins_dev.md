# 🛠️ Writing your own plugin

Booki has four plugin types — **sources** (produce items), **enrichers** (annotate existing items), **exporters** (turn a selection into an artifact), and **tab contributions** (add a UI surface to the web app). All four are auto-discovered, all four are decorated, and all four pick up config from a per-name subtable in `config.toml`.

---

## 📥 A minimal source plugin

Drop a module under `plugins/<name>/__init__.py` — it's auto-discovered on import. A working source is ~30 lines:

```python
# plugins/my_source/__init__.py
from ..base import Item, Source, register

@register
class MySource(Source):
    name = "mysource"                  # CLI token + output dir slug

    def is_available(self) -> bool:
        return True                    # check creds / deps / files here

    def fetch(self):
        yield Item(
            title="Hello",
            url="https://example.com/hello",
            source=self.name,
            kind="bookmark",            # or "video" / "channel" / your own
            path=["MySource", "group-a"],
            date_added="2026-04-18",
            extras={"my_custom_field": 42},
        )

    # Optional — lets the web UI render your custom extras.
    @classmethod
    def field_specs(cls):
        return [{
            "name": "my_custom_field",
            "label": "My field",
            "group": "MySource",
            "format": "number",
        }]
```

Add an optional `[sources.mysource]` section to `config.toml` — its contents are passed to `configure(cfg)` before `is_available` / `fetch`. The source then shows up in `booki sync --list-sources` and runs alongside the built-ins.

### Source feature overview

- **`Item`** — the universal currency. Required fields: `title`, `url`, `source`, `kind`. Optional: `path` (directory hierarchy under `bookmarks/<source>/`), `date_added`, and `extras` (free-form frontmatter dict, preserved verbatim across re-syncs).
- **Identity is the URL hash** — re-fetching the same URL updates the existing file and preserves user edits (importance, tags, notes), even if the title changed.
- **`is_available()`** — return `False` (and optionally implement `availability_hint()`) when the source can't run, e.g. missing creds or files.
- **`field_specs()`** — declare types for keys you put in `extras`. `format` ∈ `{"text", "number", "date", "duration", "bool", "list", "url", "tags"}`. The web UI uses these to render the item drawer; `kinds=[…]` restricts a spec to specific item kinds.
- **`description` in extras** — anything text-heavy you put under the `description` key feeds the enricher (gives the LLM more to work with than a bare title).
- **`kind`** — semantic type. `bookmark`, `video`, `channel` are conventional, but invent your own freely; the rest of the pipeline is kind-agnostic.

---

## 🧬 A minimal enricher plugin

An *enricher* runs over items that already exist (`booki sync --enrich-meta`) and returns a dict of frontmatter fields to merge in. Drop the module under `plugins/enrichers/<name>/__init__.py`:

```python
# plugins/enrichers/my_enricher/__init__.py
from datetime import date
from typing import Optional
from ...base import Enricher, register_enricher

@register_enricher
class MyEnricher(Enricher):
    name = "my_enricher"

    # Lifted by `booki sync --enrich-meta --all`.
    force_all: bool = False

    def configure(self, cfg: dict) -> None:
        super().configure(cfg)
        self.cooldown_days = int(cfg.get("cooldown_days", 7))

    def is_applicable(self, fm: dict) -> bool:
        url = str(fm.get("url", "") or "")
        if "example.com" not in url:
            return False
        if self.force_all:
            return True
        # Skip items we touched recently.
        last = str(fm.get("my_last_enriched", "") or "")
        if last and last >= date.today().isoformat():
            return False
        return True

    def enrich(self, fm: dict) -> Optional[dict]:
        # Return the fields to merge into the item's frontmatter, or None.
        existing = [str(s) for s in (fm.get("sources") or []) if str(s).strip()]
        if "my_enricher" not in existing:
            existing.append("my_enricher")
        return {
            "sources":            existing,
            "my_field":           "computed value",
            "my_last_enriched":   date.today().isoformat(),
        }

    @classmethod
    def field_specs(cls):
        return [
            {"name": "my_field",          "label": "My field",   "group": "MyEnricher", "format": "text"},
            {"name": "my_last_enriched",  "label": "Enriched on","group": "MyEnricher", "format": "date"},
        ]
```

### Enricher feature overview

- **`is_applicable(fm) → bool`** — gating. Cheap; called for every item on every `--enrich-meta` pass. Use it to filter on URL pattern, kind, or "is this stale?".
- **`enrich(fm) → dict | None`** — does the work. Return only the fields that change; the engine merges them into the item's frontmatter via `ItemStore.update_fields`. Returning `None` is fine ("I looked but found nothing"); never raise on remote-side errors — log and return `None`.
- **`force_all`** — the engine sets this on the instance when `--all` is passed; use it to lift your cooldown.
- **Soft-kind ownership** — convention: only overwrite the canonical `kind` field when the existing kind is a "soft" default (`bookmark`, `article`, empty). Always add your slug to the cross-cutting `sources` list so views can find the item even when `kind` is owned by another plugin (e.g. `kind=file` from the directory plugin). The `photo` and `document` enrichers use `SOFT_KINDS = {"", "bookmark"}` and `{"", "bookmark", "article"}` respectively.
- **Cooldown** — write `<slug>_last_enriched: YYYY-MM-DD` and skip items still inside the window in `is_applicable`.
- **Disabling** — `[enrichers.<name>].disabled = true` in `config.toml` skips your enricher entirely.
- **`field_specs()`** — same shape as for sources. Used by the web UI's drawer to render your fields under a labeled group.

---

## 🧩 Tab plugins {#tab-plugins}

A plugin can contribute a top-level tab to the web UI. Drop two files in addition to your `__init__.py`:

```
plugins/<slug>/
├── __init__.py
└── web/static/
    ├── tab.js
    └── tab.css      # optional
```

In `__init__.py`, register the contribution alongside any other plugin code:

```python
from plugins.base import TabContribution, register_tab

register_tab(TabContribution(
    id="my_tab",
    label="My Tab",
    icon="🎨",
    order=50,                 # built-ins use 10/20/25/30/40/90
    module="tab.js",          # path inside web/static/
    styles=["tab.css"],
))
# `plugin` is auto-inferred from the caller's __module__.
```

`tab.js` is a plain ES module. The host `import()`s it after registering metadata, and it calls `booki.tabs.implement(id, { mount, onShow, onHide })` to wire behavior:

```js
booki.tabs.implement("my_tab", {
  mount(el) {
    el.innerHTML = `<div class="my-tab"><h2>Hi</h2><ul id="my-list"></ul></div>`;
  },
  onShow(el) {
    const items = booki.bookmarks.all().filter(b => b.kind === "video");
    el.querySelector("#my-list").innerHTML = items
      .map(b => `<li data-id="${booki.ui.escapeHtml(b.id)}">${booki.ui.escapeHtml(b.title)}</li>`)
      .join("");
    el.querySelectorAll("li[data-id]").forEach(li => {
      li.addEventListener("click", () => booki.ui.openDrawer(li.dataset.id));
    });
  },
});
```

### Public host surface — `window.booki`

The host exposes a small, stable surface so plugin tabs don't reach into private internals (which churn freely). What's available today:

| Namespace           | Helper                                  | Notes |
|---------------------|------------------------------------------|-------|
| `booki.tabs`        | `register({id, label, icon, order, mount, onShow, onHide})` | Mostly used by the host bootstrap; plugins normally call `implement` instead. |
|                     | `implement(id, behavior)`               | Fill in `mount` / `onShow` / `onHide` on the metadata-registered tab. |
|                     | `activate(id)` / `current()` / `get(id)` / `all()` | Programmatic tab switching. |
| `booki.api`         | `fetch(path, opts)` / `get(path)`       | Wrapped fetch with JSON helper. |
| `booki.bookmarks`   | `all()` / `byId(id)`                    | Snapshot of the currently-loaded compact bookmark list. |
| `booki.ui`          | `openDrawer(id)` / `openDetail(id)`     | Open the bookmark detail drawer. |
|                     | `toast(msg)`                            | Reuse the existing toast. |
|                     | `escapeHtml(s)` / `highlight(text, matches)` | DOM-safety + match highlighting helpers. |
| `booki.search`      | `fuzzy(q, text)` / `substring(q, text)` | Same matchers the Search tab uses; both return `{score, matches} | null`. |
|                     | `useFuzzy` (getter)                     | Live-reads the global Fuzzy toggle so plugin tabs honor it. |

Anything not on this list is not part of the contract and may move around. If you need something that isn't here, open an issue / PR — we'd rather expand the surface deliberately than have plugins read host internals.

### Tab plugin gotchas

- **No-build.** `tab.js` is loaded as-is. Stick to plain ES modules — no JSX, no TypeScript, no imports from npm.
- **No CSS isolation.** Selectors are global. Prefix your classes (`.my-tab-row`, not `.row`).
- **Lifecycle.** `mount` runs once per session, `onShow` runs every activation. Re-render in `onShow` if the tab depends on bookmark data — the host's bookmark refresh doesn't notify plugins automatically (yet).
- **Don't rely on `state` or other private host symbols.** They're not exported and they will rename. Use `booki.bookmarks.all()`.

For a worked example, the in-tree built-ins (Photos / Videos / Documents in `web/app.js`) follow the same `mount` / `onShow` / `getSelection` shape — they just live in core rather than in a plugin module. A plugin tab is structurally identical; the only difference is that it loads from `plugins/<slug>/web/static/tab.js` and is registered via `register_tab(TabContribution(...))` instead of being declared inline.

---

## 📤 A minimal exporter plugin

Drop the module under `plugins/exporters/<name>/__init__.py`:

```python
# plugins/exporters/my_export/__init__.py
from pathlib import Path
from ...base import Exporter, ExportOption, ExportResult, Item, register_exporter

@register_exporter
class MyExporter(Exporter):
    name = "my_export"
    label = "My Export"
    description = "One-line blurb shown in the wizard."

    def options(self):
        return [
            ExportOption("greeting", "Greeting", type="string", default="Hello"),
            ExportOption("uppercase", "Shout", type="bool", default=False),
        ]

    def export(self, items: list[Item], *, theme, options, out_dir: Path) -> ExportResult:
        greeting = options.get("greeting", "Hello")
        if options.get("uppercase"):
            greeting = greeting.upper()
        out = out_dir / "items.txt"
        with out.open("w", encoding="utf-8") as f:
            for it in items:
                f.write(f"{greeting}, {it.title} — {it.url}\n")
        return ExportResult(artifact_path=out, mime="text/plain",
                            preview_text=out.read_text("utf-8"))
```

It immediately appears in the web wizard's exporter dropdown with the option fields rendered automatically.

### Exporter feature overview

- **`options()`** — exposes wizard form fields. Supported `type` values: `string`, `bool`, `number`, `select`, `multiselect`. Add `choices=[…]` for the select types.
- **`supports_themes` + `default_theme`** — set `supports_themes = True` and Booki will discover Jinja2 themes under `<this_package>/themes/<name>/` (built-in) and `<themes_root>/<exporter_name>/<name>/` (user themes override built-ins). Each theme is typically a `main.html.j2` + `styles.css`.
- **`ExportResult`** — return the artifact path, the MIME type, and an optional `preview_text` shown inline in the wizard. The web UI takes care of packaging the download.
- **External tools** — if your exporter shells out (like `offline_archive` does to `monolith` and `yt-dlp`), prefer continue-on-failure: record errors per-item in your output rather than crashing the whole export.

---

## 🧰 Where to look in the code

| File                                | What's there |
|-------------------------------------|--------------|
| [`plugins/base.py`](../plugins/base.py)                   | `Item`, `Source` / `Enricher` / `Exporter` ABCs, `TabContribution` dataclass, `@register` / `@register_enricher` / `@register_exporter` / `register_tab`, the registries. |
| [`plugins/__init__.py`](../plugins/__init__.py)           | Auto-discovery (imports each subpackage so the decorators fire). |
| [`plugins/browsers/__init__.py`](../plugins/browsers/__init__.py) | A small, self-contained example of three sources sharing parsing helpers. |
| [`plugins/enrichers/photo/__init__.py`](../plugins/enrichers/photo/__init__.py)         | URL-pattern enricher — minimal template (no network calls). |
| [`plugins/enrichers/document/`](../plugins/enrichers/document/)             | Pure URL-pattern enricher (the Documents tab itself lives in core `web/app.js`). |
| [`plugins/enrichers/github/__init__.py`](../plugins/enrichers/github/__init__.py)       | Network-calling enricher with retries, rate-limit handling, cooldown. |
| [`plugins/exporters/data_dump/`](../plugins/exporters/data_dump/) | The simplest exporter — useful as a template. |
| [`plugins/exporters/link_page/`](../plugins/exporters/link_page/) | Themed-Jinja exporter — useful as a template if your output is HTML. |
| [`web/app.js`](../web/app.js) (`window.booki = …`) | The public host surface plugin tabs talk to. |
