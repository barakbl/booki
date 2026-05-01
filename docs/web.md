# 🌐 Web UI

`web.py` is a FastAPI server that serves a single-page UI for browsing, searching, editing, and exporting your library. The same data files (`bookmarks/<source>/...md`) drive the CLI tools and the web UI — edits in either flow through to the other.

## Run it

```bash
booki web                              # host/port from config.toml [web]
booki web --port 9000                  # override the port
booki web --host 0.0.0.0               # expose on the LAN
booki web --reload                     # uvicorn auto-reload (dev)
booki web --config other.toml          # alternate config file
```

By default it binds to `127.0.0.1:8765` (configurable in `[web]` of `config.toml`).

```toml
[web]
host = "127.0.0.1"
port = 8765
reload = false
```

## Tabs

Top navigation is a tab bar. Each tab owns its own panel; only one is visible at a time. State persists in `localStorage("booki.tab")` and mirrors to `location.hash` (`#search`, `#photos`, …) so links and the back button work.

| Tab           | What it does |
|---------------|--------------|
| 🔎 **Search** | Fast client-side fuzzy search over title + URL of every item. Toggle **Fuzzy** for fzf-style character-order matching; substring otherwise. The advanced-search drawer narrows by tags / lists / sources / kinds / importance. |
| 🖼 **Photos** | Scoped search restricted to photo items (`kind == "photo"` or the `photo` source tag). Rendered as a thumbnail grid. Direct-image URLs preview inline; `file://` images stream through `/api/local-file` (allow-listed to the directory plugin's roots). |
| 📄 **Documents** | Scoped search over PDFs, ebooks, office files, plain-text formats. Toggle list ↔ grid. Per-extension icons. *Contributed by the `document` enricher plugin.* |
| 🎬 **Videos** | Scoped search over `kind == "video"` items. Poster-thumbnail grid with duration overlay and channel name. |
| ✨ **Ask**    | Semantic vector search against the ChromaDB index plus an LLM-synthesized answer. Hits the same code path as `booki chat`. Requires `booki ingest` to have run at least once. |
| ⚙ **Manage** | Sub-tab interface — **Doctor / Status** (live availability checks: installed packages, external binaries, provider config), **General** (paths, providers, models), **Plugins** (installed sources / enrichers / exporters / tab contributions), **🔄 Sync & Ingest** (run `booki sync` / `booki ingest` as background subprocess jobs with chip-pickable sources / enrichers, indeterminate progress bar, exit-status pill, expandable log fold, ⏹ Stop), **✈️ Tasks** (background export tasks — same task-row UI), **Logs** (file picker + tail + follow with per-line JSON parsing and color-coded log levels). |

Plugins can contribute their own top-level tabs by registering a `TabContribution` and shipping a `tab.js` + `tab.css` — see [`plugins_dev.md`](plugins_dev.md#tab-plugins). Built-in tabs use the same registration API.

Press `/` to focus the Search input from anywhere. In Search, `↑/↓` previews, `↵` opens, `e` opens the edit drawer.

## What you can do

- **Browse and group** items by source / kind / tags / lists / folder. The Search sidebar shows live counts.
- **Edit** the user-editable fields (importance, tags, notes, lists) directly — changes are written back to the `.md` file.
- **Lists** — create regular lists by tagging items, or define **smart lists** in `config.toml` (filter / query specs). Smart-list specs are re-read from disk on every request, so edits take effect on the next page refresh — no restart.
- **Add a link** from the top bar; the URL is fetched, deduped against existing items, and dropped into a default location.
- **Download a video** for offline viewing — uses `yt-dlp` via `download.py`. The job runs in the background; the UI polls for completion and the `.md` file is updated with `download_path_video` so the `offline_archive` exporter can reuse it.
- **Switch view modes** — every result-bearing tab (Search, Photos, Videos, Ask) has a **list / grid / table** toggle in its header. Mode persists per tab in `localStorage`. List keeps each tab's rich rows (score chips, image thumbnails, video posters); grid is a generic favicon-glyph tile or the existing image grid; table shows compact rows of *glyph · Name · Source · Type · ★ · Tags*.
- **Run the export wizard** (`⬇ Export` in the header) — a 4-step inline panel mounted at the top of the active tab: **Exporter** (pick from registered plugins, with runtime notes flagging optional deps), **Options** (per-exporter form including footer text, `dir="rtl"` for Arabic / Hebrew, hide-inline-search, page title, etc.), **Organize** (drag-and-drop tree builder for the selection — auto-group by tag / kind / source / list / browser folder / importance, then drag items between folders, create / rename / delete folders, exclude items; forgiving drop targets), **Preview** (HTML output in a sandboxed iframe, JSON / YAML / CSV / Markdown syntax-highlighted, manifest table for background exporters; switching theme or color scheme re-renders live). Hierarchy-aware exporters (`bookmark_file`, `data`) emit nested folders; flat exporters honor the manual order. Background exporters (`offline_archive`) queue as a task in **Manage › Tasks**. While the wizard is open, the underlying results list / grid is hidden so the wizard is the focus.
- **System status** is in the Manage tab (Doctor sub-tab) — installed packages, optional deps (`playwright` + `chromium` browser for full-fidelity HTML capture in `offline_archive`, `yt-dlp` + `ffmpeg` for video downloads), and provider configuration (Ollama running? `OPENAI_API_KEY` set? Safari accessible?).

## API

The UI is a thin client over a JSON API. Useful endpoints if you want to script Booki:

| Endpoint                                        | Purpose |
|-------------------------------------------------|---------|
| `GET  /api/health`                              | liveness + index size |
| `GET  /api/status`                              | full system status (used by the Manage > Doctor sub-tab) |
| `GET  /api/info`                                | runtime config: bookmarks / db / log paths, embeddings + LLM provider/model |
| `GET  /api/stats`                               | aggregate counts + freshness |
| `GET  /api/schema`                              | per-source frontmatter field specs |
| `GET  /api/bookmarks`                           | list every item |
| `GET  /api/bookmarks/{id}`                      | one item with full detail |
| `PUT  /api/bookmarks/{id}`                      | patch user-editable fields |
| `GET  /api/lists`                               | regular + smart lists with counts |
| `POST /api/lists/rename`                        | rename a list across all items |
| `DELETE /api/lists/{name}`                      | remove a list from all items |
| `POST /api/link`                                | add a new link by URL |
| `POST /api/ask`                                 | semantic search + LLM synthesis |
| `POST /api/bookmarks/{id}/download`             | start a yt-dlp download |
| `GET  /api/bookmarks/{id}/download`             | poll the download status |
| `GET  /api/export/exporters?kind=…`             | list exporters applicable to this tab's kind, with `supports_hierarchy`, `runtime_notes`, options schema |
| `GET  /api/export/themes?exporter=…`            | list themes for an exporter (with `has_thumbnail`) |
| `GET  /api/export/themes/{kind}/{slug}/thumbnail` | serve a theme's `thumbnail.png` |
| `GET  /api/export/colorschemes`                 | named color palettes (Catppuccin, Tokyo Night, …) for the wizard's scheme picker |
| `POST /api/export/options`                      | dynamic options schema (exporters can customize per-selection) |
| `POST /api/export/preview`                      | render a preview of what `run` would produce — HTML / text / manifest, accepts the `tree` from the Organize step |
| `POST /api/export/run`                          | stream the file (immediate exporters) or return `{task_id}` (background); accepts `theme`, `theme_vars`, `options`, `item_ids`, `tree` |
| `GET  /api/export/tasks`                        | list background export tasks |
| `GET  /api/export/tasks/{id}` · `…/artifact` · `POST …/retry` · `DELETE …` | inspect / download / retry / delete |
| `POST /api/jobs/run`                            | queue a sync / ingest subprocess job (allowlisted flags + safe values) |
| `GET  /api/jobs` · `GET /api/jobs/{id}`         | list / inspect jobs (status, exit code, streamed log) |
| `POST /api/jobs/{id}/cancel` · `DELETE /api/jobs/{id}` | terminate / delete a job |
| `GET  /api/tabs`                                | plugin-contributed tab metadata + module / style URLs |
| `GET  /api/plugins`                             | enumerate sources / enrichers / exporters / tab contributions |
| `GET  /api/logs`                                | list rotated Booki log files in the configured logs dir |
| `GET  /api/logs/{name}?tail=N`                  | last N lines (≤ 5000) of one log file (path-traversal guarded) |
| `GET  /api/local-file?path=…`                   | stream a local image; allow-listed to `[[sources.directory.dirs]]` roots; image extensions only |
| `GET  /plugins/{plugin}/static/…`               | static assets shipped by a plugin (its `tab.js` / `tab.css` / images) |

The item id is the URL hash used everywhere else in Booki (`bm_id` in `ingest.py`), so it's stable across re-syncs.

## Static front-end — proudly no-build

The UI is plain HTML + a single `app.js` + `styles.css`, served from [`web/`](../web/). **No bundler, no transpiler, no `node_modules`, no `npm install`.** Edit the file, refresh the browser, see the change.

This is a deliberate design constraint. It means:

- The whole front-end is auditable in one editor session.
- Anyone with a browser and `python3` can hack on it — no JS toolchain required.
- Plugin tabs follow the same rule: a plugin ships `tab.js` + `tab.css` in `plugins/<slug>/web/static/`, the host loads the JS via dynamic `import()`, and the CSS via a runtime `<link>`. No bundling, no package manager, no compile step.

State management is hand-rolled, not framework-driven. Reactivity comes from `addEventListener` + a small `Tabs` registry. Cross-cutting helpers a plugin tab needs are exposed at `window.booki` (see [`plugins_dev.md`](plugins_dev.md#tab-plugins) for the public surface).
