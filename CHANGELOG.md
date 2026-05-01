# Changelog

All notable changes to Booki are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

#### Export wizard

- **Inline panel** — the wizard now mounts at the top of the active tab's
  content (Search / Photos / Videos / Manage / plugin tabs) instead of a
  right-edge slide-out drawer. Bounded to 1100 px and centered, with the
  rest of the page still scrollable.
- **4-step flow**: Exporter → Options → **Organize** → **Preview**.
- **Preview step** with a sandboxed iframe for HTML output, syntax-highlighted
  `<pre>` for JSON / YAML / CSV / Markdown, and a manifest table for
  background exporters (per-item plan + planned filename). Theme controls
  live inside the Preview step now — switching theme / color scheme /
  individual color re-fetches the preview (250 ms debounced, with
  sequence-guarded stale-response drops).
- **Organize step** *(was Refine — renamed to fit the actual UX)* — drag-and-drop
  tree builder for the export's items. Auto-grouping strategies
  (none / tag / kind / source / list / browser folder / importance) with
  hidden-when-empty options and inline counts (`By tag (47)`). Manual edits
  flip a *dirty* badge that prompts before re-grouping. **Forgiving drop
  targets**: inter-row gaps inflate to 14 px during drag with a dashed
  outline; rows themselves accept drops on top half (insert before) /
  bottom half (insert after); folder headers split into thirds (insert
  before / drop into / insert after). Descendant-cycle prevention; rename
  / delete actions on every node.
- **Wizard focus**: while the panel is open, every following sibling in the
  host container is hidden via a single CSS rule, so the underlying
  results / grid / per-tab search box don't clutter the view. Closing the
  wizard removes the panel and the rest reappears.
- **Hierarchy contract** — `Exporter.supports_hierarchy: bool = False`
  declares whether an exporter renders nested folders. `tree` is plumbed
  through `/api/export/preview`, `/api/export/run`, the background runner,
  and `Task` frontmatter (so background jobs persist their tree across
  restarts). `flatten_tree()` / `order_items_by_tree()` helpers reorder
  items in tree-walk order, attach `_path`, and keep duplicates when an
  item appears under multiple folders (intentional for tag/list groupings).
- **JSON / YAML syntax highlighting** in the preview pane — single-pass
  tokenizers, no external library; token classes scoped to `.preview-pre`.

#### Exporter plugins

- **`bookmark_file`** *(new)* — Netscape Bookmark File exporter. Drop the
  `.html` into Chrome / Firefox / Safari / Edge to import. Supports
  `group_by` (none / source / kind / tag / list), `include_tags`
  (`TAGS="a,b"` attr — Firefox & Vivaldi pick it up), `only_with_url`,
  `root_folder`. Hierarchy-aware: when an Organize tree is supplied it walks
  the tree to emit nested `<DT><H3>` folders.
- **`data`** — gains `supports_hierarchy = True`. With a tree:
  - JSON / YAML emit a nested object mirroring the tree (items at each
    level under `_items`).
  - CSV prepends a `path` column (`IT/Cloud`).
  - Markdown turns folder names into heading levels.
- **`offline_archive`** *(replaces `offline_video`)* — `applicable_kinds=["any"]`
  background exporter that downloads whole pages and packages them as a
  single zip with a themed `index.html`. Per-item dispatch:
  - HTML pages → Playwright headless render (when installed) with
    subresource inliner that base64-embeds CSS / images / fonts and
    fishes embedded `<iframe src="*.pdf">` PDFs out as sibling files.
    Falls back to plain HTTP fetch when Playwright isn't installed and
    surfaces a runtime note explaining the trade-off + install command.
  - PDFs → direct download.
  - Videos → `yt-dlp` (kept from `offline_video`).
  - Images → direct download.
  - Local-file photos → `shutil.copy2`.
  - Per-item retry-once + partial-success semantics.
- `Exporter.runtime_notes() -> [{level, text}]` — per-exporter notices the
  wizard surfaces when the exporter is selected. Used by `offline_archive`
  to flag missing optional Playwright / yt-dlp.

#### Exporter HTML output options

Three new options apply to every HTML-emitting exporter (`link`,
`photo_gallery`, `offline_archive`); themes opt into honoring them.

- **`footer_text`** (text) — optional line rendered at the bottom of the
  page. Themes wrap it in their own footer styling.
- **`show_search`** (bool, default on) — uncheck to remove the
  type-to-filter `<input>` *and* the inline JavaScript that wires it from
  the exported HTML. The static page becomes smaller and free of dead
  scripts.
- **`rtl`** (bool, default off) — sets `dir="rtl"` on `<html>` for
  Arabic / Hebrew content. Themes that ship `[dir="rtl"]` rules
  (basic, ratatui, fun) mirror lists, item heads, tag rows, and status
  bars.

#### Themes

- **Per-theme `thumbnail.png`** — every theme dir gets a 280×176 mock the
  picker renders above the name + description. `tools/gen_theme_thumbs.py`
  (re)generates them from each `theme.toml`'s declared color vars; slug-
  specific renderers ship for `ratatui` (TUI mock with labelled box,
  numbered rows, amber status bar) and `fun` (rainbow gradient title,
  rotated white "card" rows with sticker emoji and pill chips). Emoji
  paint through Apple Color Emoji / Noto Color Emoji at native bitmap
  size and downscale onto the canvas with `embedded_color=True`.
- **Theme catalog refresh** — dropped the `light` themes, renamed `dark`
  → `basic`. Added two new themes for `any/` and `photo/`:
  - **`ratatui`** — terminal-TUI homage: monospace, ASCII box borders
    (`┤ Title ├`), labelled blocks, sticky amber status bar,
    blinking-caret filter input, `/` and `Esc` keybindings, framed
    lightbox dialog.
  - **`fun`** — childish, kid-friendly: Comic Sans / Marker Felt
    typeface stack, rainbow-gradient `<h1>`, white card rows with hard
    drop-shadows and ±0.4° tilt, sticker emoji (`🌟`, `🎈`, `🦄`),
    bouncy hover, dashed-border footer pill. `prefers-reduced-motion`
    disables the wiggles.
- **Theme contract widened** — themes can now declare `secondary`,
  `muted`, `success`, `warning`, `danger` color vars on top of the
  original `bg / text / link / accent`. Built-in `basic`, `ratatui`,
  and `fun` themes opt in: tag pills use `--secondary`, dim text uses
  `--muted`, the offline-archive's "skipped" section uses `--danger`.
  Unknown vars fall through to a sensible default expression.

#### Color schemes (`themes/export/colorschemes.toml`)

- New **`/api/export/colorschemes`** endpoint serves named palettes; the
  wizard surfaces them as a custom dropdown above the theme-vars form
  with each palette rendered as a row of color swatches (native `<option>`
  can't show colors).
- **Theme defaults appear as the first scheme**, auto-selected and applied
  on theme change — so the picker always reflects the theme's intended
  palette and gives a one-click revert from any scheme / manual edit.
- Schemes ship with up to nine roles: `bg / text / link / accent /
  secondary / muted / success / warning / danger`. Apply only writes to
  vars the active theme actually declares; unused roles still show in the
  swatch row so the palette's character is visible at a glance — Mocha
  and Tokyo Night look subtly similar in `bg/text/link/accent` but their
  `secondary / warning / danger` swatches differ visibly side-by-side.
- The dropdown trigger and each option render the full role palette as a
  row of color squares so flavors are comparable at a glance.
- Initial schemes:
  - **Catppuccin** — Latte / Frappé / Macchiato / Mocha (full nine roles).
  - **Tokyo Night** — Night / Storm / Moon / Day (canonical hex from
    `folke/tokyonight.nvim`; secondary/warning/danger pulled from each
    variant's wider palette so they look distinct in the picker even
    though Night and Storm share text + accent upstream).
  - **Tokyo Neon** — color-hex remix #91636 (deep navy + neon cyan + hot
    pink).

#### Tabs (results-bearing)

- **Unified view-mode toggle** on Search / Photos / Videos / Ask. A
  three-button widget in the tab header switches between **list** /
  **grid** / **table** for every result list. Mode persists per tab in
  `localStorage("booki.view.<tab>")`.
  - List mode keeps each tab's rich rows (score chips + match highlights
    on Search; image thumbnails on Photos; poster + duration on Videos).
  - Grid mode shows a generic favicon-+-glyph tile (Search / Ask) or the
    tab's existing image grid (Photos / Videos).
  - Table mode shows compact rows with **glyph · Name (title + URL) ·
    Source · Type · ★ · Tags** — sticky-header table, click-to-open.
- New shared helpers in `web/app.js`: `viewToggleHtml(tabId, allowed)`,
  `wireViewToggle(rootEl, tabId, onChange)`, `renderItemsTable(host, items)`,
  `renderItemsGrid(host, items)`, `renderItemsList(host, items)` — each
  defaults to `openDetail(id)` on click; pass `opts.onClick` to override.
- Ask tab caches results (`_askLastResults`) so re-rendering on toggle
  doesn't re-query the LLM.

#### Manage tab

- **🔄 Sync & Ingest** sub-tab — runs `booki sync` and `booki ingest` as
  background subprocess jobs with the same task-style UI as exports
  (status icon, kind, args inline, indeterminate progress bar while
  running, exit-status pill on completion, expandable log fold, ⏹ Stop /
  🗑 Delete). Sync launcher offers chip pickers for `--source` and
  `--enricher`, plus checkboxes for the boolean flags. Polls
  `/api/jobs` every 1.5 s while the sub-tab is active.
- New `core/jobs.py` — `Job` / `JobStore` / `JobRunner` mirror the export
  task store. Each run persists to `exports/jobs/<id>.md` (frontmatter +
  log body) so jobs survive server restarts (recovery marks them failed
  with a note). Subprocess command is `python booki <kind> <args…>`;
  flags are validated against per-kind allowlists (`SYNC_FLAGS`,
  `INGEST_FLAGS`) and values must match `^[\w][\w\-./]*$` — the UI
  cannot inject arbitrary shell.

### Changed

- **Tab boot ordering** — `Tabs.implement` now replaces the bootstrap
  stub mount even when the tab is already `_mounted` (fixes "Loading
  plugin tab…" stuck on the active plugin tab when its module finishes
  importing after `Tabs.activate`). `booki.bookmarks.onChange(cb)` also
  invokes the callback once on the microtask queue if `state.all` is
  already populated, so plugin tabs that subscribe late still see the
  current data without waiting for the next refresh. `Tabs.activate`
  registers a one-shot `onChange` listener as a fallback when bookmarks
  haven't loaded yet at activation time.
- **Inline export panel mount target** picks `.scoped-tab` on Photos /
  Videos / Ask / Manage so the panel inherits the existing 1100 px width
  cap; falls back to `.tab-panel.active` for plugin tabs that don't use
  `.scoped-tab`.
- `Task` frontmatter gains `tree` so background-export trees survive
  restarts.
- `Exporter.run_immediate / run_background / preview` accept a
  `tree=None` kwarg (back-compat default); existing exporters updated.

### Removed

- `plugins/exporters/offline_video/` — superseded by `offline_archive`,
  which handles videos as one of several per-item plans.
- `themes/export/video/` — videos now use the `any/` themes via
  `offline_archive`.
- All `themes/export/*/light/` themes.

### API additions

| Endpoint | Purpose |
|---|---|
| `POST /api/export/preview` | render a preview of what `run` would produce (HTML / text / manifest) |
| `GET  /api/export/colorschemes` | list named color palettes |
| `GET  /api/export/themes/{kind}/{slug}/thumbnail` | serve a theme's `thumbnail.png` |
| `POST /api/jobs/run` | queue a sync / ingest subprocess job |
| `GET  /api/jobs` · `GET /api/jobs/{id}` | list / inspect jobs |
| `POST /api/jobs/{id}/cancel` · `DELETE /api/jobs/{id}` | terminate / delete |
| `GET  /api/jobs/_meta` | per-kind flag allowlist (UI introspection) |

## [0.1.0] — 2026-04-29

First tagged release.

### Added

#### Pipeline

- **Pluggable source plugins** — Chrome / Safari / Firefox bookmarks, YouTube
  account (OAuth: subscriptions / liked / recent uploads), RSS feeds, local
  directory trees. Each source is one decorated class under `plugins/<name>/`
  and writes one Markdown-with-frontmatter file per item.
- **LLM enrichment** (`booki sync --enrich`) — fetches each page with
  `trafilatura`, asks the configured LLM for a one-sentence summary +
  keywords, writes them back to the item's frontmatter. Conservative
  title-only fallback for unfetchable pages.
- **Metadata enrichers** (`booki sync --enrich-meta`) — pluggable per-URL
  classifiers that add cross-cutting tags and promote items to a richer
  `kind`:
  - `github` — stars / forks / topics / languages / contributors / archived /
    pushed_at for `github.com/<owner>/<repo>`.
  - `youtube` — channel / duration / view_count / thumbnail / language /
    categories for YouTube videos and channels.
  - `photo` — sets `kind=photo` for image-extension URLs (incl. all common
    camera-raw formats) and known photo-hosting paths.
  - `document` — sets `kind=document` for PDFs / Word / OpenOffice / ebooks
    / Markdown / plain text / CSV. Configurable `[enrichers.document].types`.
- **Vector search** via ChromaDB (`booki ingest`); semantic Q&A through
  `booki chat` and the **Ask** web tab.
- **Pluggable exporters** — `link_page` (themed HTML, two themes), `offline_archive`
  (self-contained ZIP via `monolith` + `yt-dlp`), `data_dump` (JSON / CSV),
  `llm_prompt` (Markdown bundle), `bookmark_file` (Netscape).

#### Web UI

- **Tab-based navigation** with six top-level tabs:
  - 🔎 **Search** — fuzzy / substring matching over title + URL + tags;
    advanced filters by tags / lists / sources / kinds / importance.
  - 🖼 **Photos** — scoped search restricted to photo items; thumbnail grid;
    `file://` images proxied through `/api/local-file`.
  - 📄 **Documents** — scoped search; toggleable list / grid views with
    per-extension icons; grid tiles show truncated summaries.
  - 🎬 **Videos** — scoped search; poster grid with duration overlay and
    channel name.
  - ✨ **Ask** — semantic vector search + LLM-synthesized answer.
  - ⚙ **Manage** — sub-tab interface: Doctor (live system-status checks),
    General (paths / providers / models), Plugins (installed sources /
    enrichers / exporters / tab contributions), Logs (file picker, tail,
    follow mode, per-line JSON parsing with level color coding).
- **Plugin tab contributions** — plugins ship a `tab.js` + optional
  `tab.css` next to their Python and call `register_tab(TabContribution(…))`.
  The host loads the JS via dynamic `import()` and attaches CSS via runtime
  `<link>`. Built-in tabs use the same registry — the contract is
  dogfooded. Reference example: the Documents tab in the document enricher.
- **Public plugin host surface** at `window.booki`:
  - `booki.tabs` — `register` / `implement` / `activate` / `current` / `get` / `all`.
  - `booki.api` — wrapped `fetch` / `get` with JSON helper.
  - `booki.bookmarks` — `all()` / `byId(id)` / `onChange(cb)` subscription.
  - `booki.ui` — `openDrawer(id)` / `toast(msg)` / `escapeHtml` / `highlight`.
  - `booki.search` — `fuzzy` / `substring` matchers + live `useFuzzy` getter.
- **`kind_specs()` plugin contract** — plugins declare any new `kind` slugs
  they introduce (slug + glyph + label) on their `Source` / `Enricher` class.
  The CLI fzf preview, the web row badge, and `/api/kinds` all aggregate
  from the registry — adding a new kind never edits `core/browse.py` or
  `web/app.js`.
- **Background `yt-dlp` downloads** from the drawer; `download_path_video`
  is written to frontmatter so the `offline_archive` exporter reuses cached
  files.
- **Export wizard** — pick a selection (lists / smart lists / tags / filters
  / manual ids — combined as a union), an exporter, a theme, per-exporter
  options; preview inline; download the artifact.
- **Keyboard shortcuts** — `1`–`9` jump to the Nth tab when not typing,
  `/` focuses the Search input from any tab, `↑/↓` previews in Search,
  `↵` opens, `e` opens the edit drawer, `Esc` closes overlays.
- **URL hash routing** — `#photos`, `#docs`, etc. survive reload and
  back-button. Active tab is also persisted to `localStorage`.
- **Accessibility** — tab bar has `role="tablist"`, each tab carries
  `role="tab"` + live `aria-selected`.

#### JSON API

New endpoints (existing endpoints continue to work unchanged):

- `GET /api/tabs` — plugin-contributed tab metadata + module / style URLs
  with mtime-based cache-busts.
- `GET /api/kinds` — aggregated kind specs declared by every plugin.
- `GET /api/info` — runtime config: bookmarks / db / log paths, embeddings
  + LLM provider/model.
- `GET /api/plugins` — sources / enrichers / exporters / tab contributions
  for the Manage > Plugins admin.
- `GET /api/logs` + `GET /api/logs/{name}?tail=N` — list and tail Booki log
  files (1 ≤ N ≤ 5000).
- `GET /api/local-file?path=…` — image proxy for `file://` URLs the browser
  refuses to load directly. Allow-listed to the `[[sources.directory.dirs]]`
  roots; image extensions only.
- `GET /plugins/{plugin}/static/…` — static assets shipped by a plugin.

### Philosophy

- **Local by default, cloud opt-in.** The default config runs fully on the
  user's machine: Ollama for the LLM, local sentence-transformers for
  embeddings, ChromaDB on disk. Cloud providers (Claude, OpenAI) are one
  toggle in `config.toml`.
- **Markdown is the source of truth.** The vector DB is a derived index
  that can be thrown away and rebuilt from `bookmarks/` at any time. User
  edits (importance, tags, notes, lists) are preserved across re-syncs by
  URL hash.
- **Everything interesting is a plugin.** The core pipeline is source- and
  kind-agnostic; sources, enrichers, exporters, and tab contributions all
  follow the same auto-discovered, decorator-registered shape.
- **Proudly no-build.** The web UI is plain `index.html` + one `app.js` +
  one `styles.css` served as-is. No bundler, no transpiler, no
  `node_modules`. Plugin tabs follow the same rule: a `tab.js` + `tab.css`
  shipped next to the plugin's Python, loaded via dynamic `import()` and
  runtime `<link>`.

### Security

- `/api/local-file` resolves paths via `Path.resolve(strict=True)` and
  rejects anything outside the configured directory roots; an extension
  allow-list (image types only) prevents the endpoint from becoming a
  generic file server.
- `/api/logs/{name}` rejects names containing `/`, `\`, or `..`; the
  resolved path must live inside the logs dir; the filename must match the
  configured `booki.log` basename or its rotation suffix.
- Plugin static-dir mounts resolve under `plugins/` and skip anything
  resolving outside.

[Unreleased]: https://github.com/barakbl/booki/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/barakbl/booki/releases/tag/v0.1.0
