# Changelog

All notable changes to Booki are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
