# 🔌 Plugins

Booki has four kinds of plugins:

- **Sources** — produce items to index (a bookmark, a video, a channel, an RSS entry, a file on disk, …). Each source lives under [`plugins/<name>/`](../plugins/) and registers itself with `@register`.
- **Enrichers** — inspect items that already exist and add metadata: a richer `kind`, a cross-cutting `sources` tag, scalar fields (stars, duration, channel, document_type, …). Each enricher lives under [`plugins/enrichers/<name>/`](../plugins/enrichers/) and registers itself with `@register_enricher`.
- **Exporters** — turn a selection of items into a downloadable artifact (HTML page, ZIP archive, JSON, LLM prompt, …). Each exporter lives under [`plugins/exporters/<name>/`](../plugins/exporters/) and registers itself with `@register_exporter`.
- **Tab contributions** — add a top-level tab to the web UI. A plugin (typically an enricher) ships a `tab.js` + optional `tab.css` next to its Python and registers a `TabContribution`; the host loads the JS via dynamic `import()` and the CSS via a runtime `<link>`.

All four are auto-discovered on import, configured via per-plugin subtables in `config.toml`, and surfaced in the CLI / web UI without further wiring.

> **Want to write your own?** See [`plugins_dev.md`](plugins_dev.md).

---

## 📥 Built-in sources

| Source     | Kind(s) produced     | Requires |
|------------|----------------------|----------|
| `chrome`   | `bookmark`           | Chrome installed |
| `safari`   | `bookmark`           | Safari + Full Disk Access for Terminal (macOS) |
| `firefox`  | `bookmark`           | Firefox installed |
| `youtube`  | `video`, `channel`   | OAuth credentials — see [`plugins/youtube/README.md`](../plugins/youtube/README.md) |
| `rss`      | `bookmark`           | A list of feeds in `[[sources.rss.feeds]]` |
| `directory`| `bookmark` (file://) | Local directories listed under `[[sources.directory.dirs]]` |

Each source has its own `[sources.<name>]` subtable in `config.toml` for per-source options. Run `booki sync --list-sources` to see what's registered and whether it's currently usable.

### Per-source documentation

- [`plugins/youtube/`](../plugins/youtube/README.md) — the YouTube source, including the OAuth setup walkthrough.

---

## 🔄 Running sources — `sync.py`

```bash
booki sync                                # run every available source
booki sync --list-sources                 # show registered sources + availability
booki sync --source chrome                # one source
booki sync --source chrome safari youtube # multiple
booki sync --dry-run                      # preview without writing

booki sync --check-dead-links             # sync + check unchecked links
booki sync --check-dead-links --all       # re-check all links
booki sync --no-sync --check-dead-links   # only check links

booki sync --enrich                       # sync + LLM-summarize new items
booki sync --enrich --all                 # re-enrich everything
booki sync --no-sync --enrich             # only enrich
```

Sync behavior:

- ✅ **New items** are added with default metadata.
- ✅ **Your edits** (importance, tags, notes) are preserved by URL hash, even when an item's title changes.
- ✅ **Renamed items** get the file renamed — nothing is lost.
- ✅ **Removed at source** items are flagged with `removed_from_source: true`; your notes are kept.
- ✅ **Dead links** are flagged with a Wayback Machine suggestion (only for `bookmark`-kind items).
- ✅ **Orphan detection is scoped per source** — `--source chrome` won't mark Safari items as removed.

---

## 🧬 Built-in enrichers

Enrichers run via `booki sync --enrich-meta` (or `--enricher <name>` to scope to one). They iterate every existing `.md`, ask each enricher `is_applicable(fm)`, and merge the returned dict into frontmatter via `ItemStore.update_fields`. Idempotent — each enricher records `<slug>_last_enriched` and skips items inside its cooldown window unless `--all` is passed.

| Enricher    | What it tags                                                      | Requires |
|-------------|-------------------------------------------------------------------|----------|
| `github`    | `github_stars / forks / topics / languages / top_contributors / license / archived / pushed_at / …` for `github.com/<owner>/<repo>` URLs | optional `GITHUB_TOKEN` (60 → 5000 req/h) |
| `youtube`   | `channel / channel_id / duration / view_count / published_at / youtube_thumbnail / …` for YouTube videos and channels | `yt-dlp` |
| `photo`     | sets `kind=photo` (when the existing kind is soft) and tags `"photo"` in `sources` for image-extension URLs (incl. camera-raw: `.raf .cr2 .cr3 .nef .arw .dng …`) and known photo hosts | — |
| `document`  | sets `kind=document` (when the existing kind is `bookmark`/`article`/empty) and tags `"document"` in `sources` for PDF / Word / OpenOffice / ebook / Markdown / plain-text / CSV URLs. Writes `document_type` (the slug) so the UI can pick a per-type icon | — |

Common patterns across enrichers:

- **Soft-kind ownership.** An enricher only takes over the canonical `kind` field when the existing kind is a default (`bookmark`, `article`, empty). Explicit kinds set by source plugins (`file` from directory, `video` from youtube, `channel`) are sticky — the enricher still adds itself to the cross-cutting `sources` list so cross-cut views (Photos / Documents tabs) can find the item via `sources.includes("…")` even when `kind` is owned elsewhere.
- **Cooldown via `<slug>_last_enriched`.** Re-runs are cheap. Pass `--all` to lift the cooldown.
- **Disabling.** Add `disabled = true` under `[enrichers.<name>]` in `config.toml` to skip an enricher entirely.
- **Tab contribution.** The `document` enricher also contributes the **Documents tab** to the web UI, demonstrating the end-to-end plugin-tab pipeline.

```bash
booki sync --enrich-meta                          # run every registered enricher
booki sync --enrich-meta --enricher photo         # one
booki sync --enrich-meta --enricher photo --all   # ignore cooldowns, re-tag everything
booki sync --no-sync --enrich-meta                # only enrich, skip sync
booki sync --list-enrichers                       # show what's registered
```

---

## 🧩 Tab contributions

A plugin can add a top-level tab to the web UI. Two pieces:

1. **Python:** call `register_tab(TabContribution(id, label, icon, order, module, styles))` from the plugin's `__init__.py`. The `plugin` field is auto-inferred from the caller's package path.
2. **Static assets:** drop `tab.js` (and optional CSS) under `plugins/<slug>/web/static/`. The host mounts it at `/plugins/<slug>/static/...` and:
   - registers the tab's metadata up-front (so the tab bar renders without flash),
   - adds `<link>` tags for each declared CSS file,
   - dynamically `import()`s the JS module, which calls `booki.tabs.implement(id, { mount, onShow, onHide })` to wire behavior.

Built-in tabs (Search / Photos / Videos / Ask / Manage) use the same registry — the contract is dogfooded.

The Documents tab in [`plugins/enrichers/document/`](../plugins/enrichers/document/) is the reference example. See [`plugins_dev.md`](plugins_dev.md#tab-plugins) for the public host surface (`window.booki.{tabs, api, bookmarks, ui, search}`).

---

## 📤 Built-in exporters

An *exporter* takes a selection of items and produces an artifact. Selections are built in the web wizard (lists / tags / filters / manual ids — combined as a union) and resolved against the live index before being passed to the chosen exporter.

| Exporter           | Output                                  | Notes |
|--------------------|-----------------------------------------|-------|
| `link_page`        | Themed `index.html`                     | Cappuccino color scheme, live filter, mixed LTR/RTL safe |
| `offline_archive`  | Self-contained ZIP                      | `monolith` for pages, `yt-dlp` for videos, raw save for PDFs, browsable `index.html` inside |
| `data_dump`        | JSON or CSV of items                    | Round-trip of the underlying data, configurable field set |
| `llm_prompt`       | Markdown prompt file                    | Drop into Claude / GPT / NotebookLM with a chosen preamble (summarize / Q&A / outline) |
| `bookmark_file`    | `bookmarks.html` (Netscape format)      | Re-importable into any browser |

### `link_page` — themed HTML page

Renders selected items into one `index.html` + `styles.css`. Each card has favicon, title, host, summary, importance star, and source / kind / tag chips. A search input filters live; empty group headings disappear when filtered out. Titles and summaries use `dir="auto"` so mixed LTR/RTL collections render correctly.

Themes live under [`plugins/exporters/link_page/themes/`](../plugins/exporters/link_page/themes/):

| Theme                    | Look |
|--------------------------|------|
| `default`                | Warm cappuccino cream (light) — soft cream gradient, foam-white rounded cards, subtle noise texture, mocha/latte accents |
| `dark-cappuchine-latte`  | Dark, [Catppuccin Mocha](https://github.com/catppuccin/catppuccin) palette — Peach / Mauve / Rosewater accents on the Mocha base |

User themes are also discovered under `<themes_root>/link_page/<name>/` and override built-ins of the same name. Each theme is a `main.html.j2` + `styles.css`.

### `offline_archive` — self-contained ZIP

Bundles a selection into a ZIP that works offline. Per item we attempt:

- **Web pages** → `monolith` saves the page as a single self-contained HTML with images / CSS / JS inlined as data URIs.
- **Videos** (YouTube, Vimeo, TikTok, Twitch, and ~1000 other sites yt-dlp supports) → reuses `download_one()` from `download.py` with your `[downloads]` config (mp4 ≤1080p + subs + thumbnail by default).
- **PDFs** (URL ends in `.pdf`) → saved raw via `requests`.
- **Cache reuse** — when an item has `download_path_video` from a prior `download.py` run, the file is copied from `./downloads/` instead of re-fetched.

Other behaviors: parallel workers (default 4), per-item size cap (default 500 MB), continue-on-failure (per-item errors land in `manifest.json` and the index page), and a tools-missing banner when `monolith` or `yt-dlp` aren't on PATH.

Layout inside the ZIP:

```
archive.zip
├── index.html                   ← cappuccino landing page, links to local copies
├── styles.css
├── manifest.json                ← stats + per-item status / size / error
└── items/
    ├── 001-<slug>/{page.html, thumb.*, meta.json}
    ├── 002-<slug>/{video.mp4, subs.en.vtt, thumb.jpg, meta.json}
    └── 003-<slug>/{file.pdf, meta.json}
```

**Required external tools** (missing tools don't fail the export — affected items show a `failed` badge with install hints):

| Tool       | Install                  | Used for |
|------------|--------------------------|----------|
| `monolith` | `brew install monolith`  | Single-file HTML page archive |
| `yt-dlp`   | `pip install yt-dlp`     | Video downloads |
| `ffmpeg`   | `brew install ffmpeg`    | yt-dlp muxing + audio extraction |

### `data_dump` — JSON / CSV

Pure serialization of the selected items, configurable field set. Use it to pipe Booki's metadata into other tools.

### `llm_prompt` — LLM-ready bundle

A single Markdown file with a preamble + one block per item (title, URL, summary, keywords, tags, notes). Three built-in preambles: `summarize`, `qa`, `outline`.

### `bookmark_file` — browser-importable bookmarks

Standard Netscape-format `bookmarks.html` re-importable by Chrome, Firefox, Safari, and Edge. Optionally embeds the summary as the bookmark description.
