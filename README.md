# 📚 Booki

> **Turn the things you've saved into a searchable, AI-powered knowledge base.**

Booki pulls items from **pluggable sources** — Chrome / Safari / Firefox bookmarks, your YouTube account, RSS feeds, local file trees, anything you can write a ~30-line plugin for — into **one Markdown file per item**, enriches each with an LLM-generated summary and keywords, indexes the lot into a vector database, and lets you search with natural language. Fully local if you want it.

**CLI-first and web-first, same data.** Every operation is a subcommand of a single `./booki` dispatcher; the web UI is a co-equal frontend over the same Markdown files and vector index. Live in your terminal, your browser, or both — edits flow either way.

🐙 **Repository:** [github.com/barakbl/booki](https://github.com/barakbl/booki) · 🐛 [Issues](https://github.com/barakbl/booki/issues) · 📜 [Changelog](CHANGELOG.md)

---

## ✨ Features

- **🔌 Pluggable sources** — bookmarks from Chrome / Safari / Firefox, videos & channels from YouTube, RSS feeds, local directories. Write your own in ~30 lines.
- **📝 One Markdown file per item** — every bookmark, video, or channel is a first-class document with frontmatter. Editable by hand. Friendly to Git and Obsidian.
- **🧠 LLM enrichment** — fetches each page and writes a one-sentence summary + keywords back into the file, so search works even on cryptic titles.
- **🧬 Metadata enrichers** — pluggable per-URL classifiers that promote items to a richer `kind` (`photo`, `video`, `channel`, `document`, …) and tag a cross-cutting `sources` list. Built-ins: GitHub repos, YouTube videos / channels, photos, documents (PDF / docx / md / epub / …).
- **🔎 Semantic search** — finds items by meaning, not keywords. Cross-language too — English queries match Hebrew titles.
- **⚡ fzf-powered terminal browser** — `booki browse` opens an instant, fuzzy-matchable picker over your whole library with a live preview pane. Enter to open in your browser, `Ctrl-Y` to copy, `Ctrl-E` to edit the underlying `.md`. No embedding model, no startup cost.
- **🧩 Browser extension** (Chrome + Firefox) — a side-panel / sidebar picker for your Booki items (Alt+Shift+B by default) with fuzzy search and Enter-to-open, plus right-click "Add to Booki" for any link, page, or image. See [`extra/extension/`](extra/extension/README.md).
- **🔍 Alfred workflow** (macOS) — `bk <query>` searches your bookmarks from anywhere, Enter opens in your browser, ⌘+Enter copies the URL. See [`extra/alfred/`](extra/alfred/README.md).
- **🤖 LLM answers** — asks an AI to synthesize an answer over the retrieved items.
- **🔗 Dead link detection** — flags broken bookmarks and suggests Wayback Machine archives.
- **📤 Pluggable exporters** — turn a selection into a themed HTML page, an offline ZIP (full pages + PDFs + downloaded videos via `offline_archive`), a CSV / JSON / YAML / Markdown data dump, a themed photo gallery, or a browser-importable Netscape bookmarks file (`bookmark_file`).
- **🪄 4-step export wizard** — *Exporter → Options → Organize → Preview*, mounted inline at the top of the active tab (and hides the underlying results while it's open so the wizard is the focus). The **Organize** step is a drag-and-drop tree builder with auto-grouping (by tag / kind / source / list / browser folder / importance) and folder rename / delete; forgiving drop targets (gaps inflate during drag, top/bottom of each row act as insert-before/after, folder middle = drop into); hierarchy-aware exporters (`bookmark_file`, `data`) emit nested folders, flat exporters honor the manual order. The **Preview** step renders HTML in a sandboxed iframe, JSON / YAML / CSV with syntax highlighting, and a per-item *plan + filename* manifest for background exporters; switching theme or color scheme re-renders the preview live. Per-export **footer text**, **right-to-left (Arabic / Hebrew)**, and **hide inline search** options apply across HTML themes.
- **🎨 Themes + named color schemes** — every theme dir ships a thumbnail mock (renders Apple Color Emoji / Noto Color Emoji at native bitmap size); the wizard's color picker offers Catppuccin (Latte / Frappé / Macchiato / Mocha), Tokyo Night (Night / Storm / Moon / Day / Neon), and a per-theme "default" first option, each with full nine-role swatch rows (`bg / text / link / accent / secondary / muted / success / warning / danger`). Built-in themes: **basic** (clean dark), **ratatui** (terminal-TUI homage — monospace, ASCII box borders, sticky status bar), and **fun** (Comic-Sans, rainbow gradients, sticker emoji, ±0.4° tilted cards — perfect for kids).
- **🌐 Tab-based web UI** — Search, Photos, Documents, Videos, Ask, Manage. Every result-bearing tab shares the same header layout (icon + title + live "N of M" count + **list / grid / table** view toggle, all on one line; mode persists per tab) and the same purple button style, so adding new tabs feels native. Each tab has its own scoped search and its own independent **Advanced** filter form (Sources chips + the **Top-N** sort below) — tweaking Photos doesn't touch Search.
- **🔝 Top-N advanced filter** — pick a numeric / date / duration field (e.g. `last_sync`, `duration`, `github_stars`, `file_size`), choose **▲ Top** or **▼ Bottom**, set a count, and the tab shows that many items in sorted order. The autocomplete is **scoped per tab** — the Videos combo offers `view_count` / `duration`, Photos offers `image_width` / `iso`, Search offers everything — so no irrelevant fields get suggested. Each result also displays its sort-key value as a chip / column so you see *why* a row is where it is. Manage hosts inline doctor / status, general info, plugin admin, **🔄 Sync & Ingest** (run sync / ingest as background subprocess jobs with progress + log + exit status), background **✈️ Tasks** for exports, and a syntax-highlighted log viewer.
- **🧩 Plugin-contributed tabs** — plugins can ship a `tab.js` + `tab.css` next to their Python and add a top-level tab to the UI through a stable `window.booki` host API.
- **🚫 Proudly no-build** — the web UI is plain `index.html` + one `app.js` + one `styles.css`. No bundler, no transpiler, no `node_modules`. Edit the file, refresh the browser. Plugin tabs follow the same rule.
- **🔒 Privacy-first** — defaults run fully locally with Ollama + local embeddings. Cloud LLMs are opt-in.

---

## 📸 Screenshots

A quick visual tour. Captions match the filenames in [`docs/screen/`](docs/screen/) — open any image to see it full-size.

**Web UI**

**web search**

![web search](docs/screen/web%20search.gif)

**web doctor**

![web doctor](docs/screen/web%20doctor.png)

**injest and sync web ui**

![injest and sync web ui](docs/screen/injest%20and%20sync%20web%20ui.png)

**export wizard**

![export wizard](docs/screen/export%20wizard.png)

**CLI**

**browse in cli**

![browse in cli](docs/screen/browse%20in%20cli.png)

**autocomplete in cli**

![autocomplete in cli](docs/screen/autocomplete%20in%20cli.gif)

**Manager (menubar sidecar)**

**manager tray app**

![manager tray app](docs/screen/manager%20tray%20app.png)

---

## 🚀 Install

The recommended path — Booki is meant to be read, edited, and tweaked:

```bash
git clone https://github.com/barakbl/booki.git
cd booki

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt

# Start from the documented example. Edit it to taste.
cp config.toml.example config.toml
```

### One-liner installer (less recommended)

```sh
curl -sSfL https://raw.githubusercontent.com/barakbl/booki/main/install/install.sh | sh
```

This is here for users who want to *try* Booki without thinking about Python venvs. Booki is a small, hackable, single-codebase tool — by philosophy you'll want the editable `git clone` flow above. Use the installer when you want a turnkey setup; use `git clone` when you want to live in the code.

The installer is **idempotent** (re-run any time to update) and **XDG-compliant**:

- Clones / fast-forwards the repo to `$XDG_DATA_HOME/booki` (default `~/.local/share/booki`).
- Creates a virtualenv at `$XDG_DATA_HOME/booki/.venv` and `pip install -r requirements.txt` inside it — your system Python is never touched.
- Copies `config.toml.example` → `$XDG_CONFIG_HOME/booki/config.toml` (only if you don't already have one).
- Drops a `booki` wrapper in `$XDG_BIN_HOME` (default `~/.local/bin`) that invokes the venv's python on the dispatcher script.
- Detects your shell (fish / zsh / bash) and idempotently appends a PATH export *and* the matching `shells/booki.fish` / `shells/booki.zsh` source line — completion works without duplicate entries on re-runs.
- Closes by suggesting `brew` / `apt` / `dnf` / `pacman` commands for the optional binaries Booki can use (`ffmpeg`, `fzf`, `ollama`).

Pin a branch or fork via env vars:

```sh
BOOKI_REPO=https://github.com/you/booki.git BOOKI_BRANCH=feature \
  curl -sSfL https://raw.githubusercontent.com/barakbl/booki/main/install/install.sh | sh
```

Optional cloud-LLM extras:

```bash
pip install anthropic   # for Claude
pip install openai      # for OpenAI (LLM and/or embeddings)
```

All commands are wired through a single dispatcher script — `./booki`:

```bash
./booki sync                       # pull items from every available source
./booki sync --no-sync --enrich    # LLM-summarize each item (one-time)
./booki sync --enrich-meta --all   # run every metadata enricher (github / youtube / photo / document / …)
./booki ingest                     # build the vector index
./booki chat "what AI tools do I have bookmarked?"
./booki browse                     # fzf-powered fast picker in the terminal
./booki web                        # browse / search / edit in your browser
```

### Put `booki` on PATH (optional)

The repo ships shell integrations under [`shells/`](shells/) that prepend the project to `$PATH` and register tab-completion for every subcommand:

```fish
# ~/.config/fish/config.fish
source /path/to/booki/shells/booki.fish
```

```zsh
# ~/.zshrc
source /path/to/booki/shells/booki.zsh
```

After sourcing, drop the leading `./` — `booki sync`, `booki chat "..."`, etc.

---

## 🖥️ booki-manager (menubar sidecar)

A small, native macOS / Linux menubar app written in Rust. It lives next to your clock, watches the browser bookmark files for changes, and runs `booki sync` / `booki ingest` on a schedule so you never have to remember to. From the tray menu you can also trigger **Sync now** / **Ingest now**, open the web UI, and toggle autostart.

```bash
cd tools/booki-manager
cargo run --release           # foreground — useful while configuring
cargo build --release         # binary lands at target/release/booki-manager
```

Configuration lives in the same `config.toml` as the rest of Booki, under `[manager.*]`:

```toml
# When to run the periodic jobs.
[manager.schedule.sync]
cadence = "daily"             # off | daily | weekly
window  = "02:00-05:00"       # local time; wraps over midnight if end ≤ start

[manager.schedule.ingest]
cadence = "weekly"
window  = "03:00-05:00"

# What flags the manager appends to every `booki sync` it triggers — the
# manual "Sync now" *and* the scheduled syncs above. Both default to true,
# so out of the box your schedule keeps summaries + plugin enrichers fresh.
[manager.sync]
enrich      = true            # adds --enrich      (LLM summary + keywords)
enrich-meta = true            # adds --enrich-meta (github / photo / document …)
```

Either `enrich-meta` (CLI-flag spelling) or `enrich_meta` (TOML-idiomatic) works — there's a serde alias so you can pick whichever reads cleaner to you.

A job runs when **both** (a) at least one cadence period has elapsed since its last successful run *and* (b) we're inside `window` now, OR the window already ended today (catch-up — covers laptops that slept through 02:00).

---

## 🧪 Tests

```bash
pip install pytest                     # one-time, dev-only
python -m pytest                       # ~1.5s, hermetic
```

The Python suite lives under [`tests/`](tests/) and covers the bits that hurt most when broken: the bookmark file parser + URL-hash id (`test_ingest.py`), the `ItemStore` write / update / removed-flag roundtrip (`test_store.py`), every key FastAPI route via `TestClient` against a real `create_app()` (`test_web.py`), and the `booki` CLI dispatcher as a real subprocess (`test_cli.py`). All fixtures are `tmp_path`-scoped — no test touches your real `bookmarks/` or `config.toml`.

The Rust manager has its own suite under `tools/booki-manager/`:

```bash
cd tools/booki-manager
cargo test
```

It includes config-parsing tests for `[manager.sync]` (defaults, dash/underscore alias, partial overrides) and a smoke test that loads the project's real `config.toml` to confirm the parser still agrees with the example.

---

## 📖 Documentation

- [`docs/architecture.md`](docs/architecture.md) — design philosophy, directory layout, the sync → ingest → chat pipeline.
- [`docs/cli.md`](docs/cli.md) — the `./booki` dispatcher, every subcommand, and the fzf-powered `browse` picker.
- [`docs/plugins.md`](docs/plugins.md) — the built-in sources, enrichers, exporters, and tab contributions.
- [`docs/plugins_dev.md`](docs/plugins_dev.md) — write your own source / enricher / exporter / tab plugin.
- [`docs/web.md`](docs/web.md) — the tab-based web UI and its JSON API.
- [`extra/extension/README.md`](extra/extension/README.md) — the browser extension (Chrome + Firefox) — side-panel / sidebar picker + right-click "Add to Booki".
- [`extra/alfred/README.md`](extra/alfred/README.md) — Alfred workflow (macOS): `bk <query>` to search bookmarks from anywhere.
- [`config.toml.example`](config.toml.example) — fully-commented reference config.
- [`CHANGELOG.md`](CHANGELOG.md) — release history.

---

## 📄 License

MIT — see [LICENSE](LICENSE).
