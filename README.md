# 📚 Booki

> **Turn the things you've saved into a searchable, AI-powered knowledge base.**

Booki pulls items from **pluggable sources** — Chrome / Safari / Firefox bookmarks, your YouTube account, RSS feeds, local file trees, anything you can write a ~30-line plugin for — into **one Markdown file per item**, enriches each with an LLM-generated summary and keywords, indexes the lot into a vector database, and lets you search with natural language. Fully local if you want it.

**CLI-first and web-first, same data.** Every operation is a subcommand of a single `./booki` dispatcher; the web UI is a co-equal frontend over the same Markdown files and vector index. Live in your terminal, your browser, or both — edits flow either way.

🐙 **Repository:** [github.com/barakbl/booki](https://github.com/barakbl/booki) · 🐛 [Issues](https://github.com/barakbl/booki/issues) · 📜 [Changelog](CHANGELOG.md)

---

## ✨ Features

- **🔌 Pluggable sources** — Chrome / Safari / Firefox bookmarks, YouTube, RSS, local directories. Write your own in ~30 lines.

- **📝 One Markdown file per item** — frontmatter + body, editable by hand, friendly to Git and Obsidian.

- **🧠 LLM enrichment** — auto-summary + keywords written back into each file, so search works on cryptic titles.

- **🧬 Metadata enrichers** — per-URL classifiers promote items to richer kinds (`photo`, `video`, `channel`, `document`). Built-ins: GitHub, YouTube, photos, documents.

- **🔎 Semantic search** — finds items by meaning, not keywords. Cross-language — English queries match Hebrew, Arabic, and other-language titles.

- **🤖 LLM answers** — synthesizes an answer over the retrieved items.

- **⚡ Terminal browser** — `booki browse`: fzf picker with live preview. Enter opens, `Ctrl-Y` copies, `Ctrl-E` edits the `.md`.

- **🌐 Tab-based web UI** — Search / Photos / Videos / Documents / Ask / Manage. Consistent header, list / grid / table toggle, per-tab Advanced filter.

- **🔝 Top-N advanced filter** — sort by any numeric / date / duration field. Field combo scoped per tab; sort-key value rendered on each result row.

- **🪄 4-step export wizard** — *Exporter → Options → Organize → Preview*. Drag-and-drop tree, sandboxed live preview, per-export RTL / footer / search-toggle options.

- **📤 Pluggable exporters** — themed HTML page, offline ZIP (full pages + PDFs + videos), data dumps (CSV / JSON / YAML / Markdown), photo galleries, browser-importable bookmarks file.

- **🎨 Three themes out of the box** — **basic** (clean and readable), **ratatui** (homage to the terminal — monospace + ASCII box borders), **fun** (because it's fun — Comic Sans, rainbow gradients, tilted cards). Catppuccin and Tokyo Night palettes baked in.

- **🖥️ Menubar sidecar** — Rust `booki-manager` runs sync / ingest on a schedule, with `[manager.sync]` flags in `config.toml`.

- **🧩 Browser extension** (Chrome + Firefox) — side-panel picker (`Alt+Shift+B`) plus right-click "Add to Booki". See [`extra/extension/`](extra/extension/README.md).

- **🔍 Alfred workflow** (macOS) — `bk <query>` from anywhere; Enter opens, ⌘+Enter copies. See [`extra/alfred/`](extra/alfred/README.md).

- **🔗 Dead link detection** — flags broken bookmarks and suggests Wayback archives.

- **🩹 Corrupted file handling** — a hand-edited `.md` with broken YAML or wrong field types is skipped, not fatal. The web UI shows a banner ("Skipped 5 of 346 files") and a *Manage → Doctor* panel listing every problem file with a structured reason ("`importance` should be int, got string"); `booki doctor` prints the same list in the terminal.

- **🧩 Plugin tabs** — plugins ship `tab.js` + `tab.css` and add a top-level tab through a stable `window.booki` API.

- **🚫 #nobuild** — plain HTML, plain JavaScript, plain CSS. Ready to explore and hack — no bundler, no transpiler, no `node_modules`.

- **🔒 Privacy-first** — defaults run fully locally with Ollama + local embeddings. Cloud LLMs are opt-in.

---

## 📸 Screenshots

A quick visual tour — click any thumbnail to open it full-size. Captions
match the filenames in [`docs/screen/`](docs/screen/).

<!-- Inline HTML (instead of plain Markdown images) so the thumbnails sit
side-by-side and clicks open the full-size asset. GitHub renders this. -->

**Web UI**

<table>
<tr>
<td align="center" width="33%">
  <a href="docs/screen/web%20search.gif"><img src="docs/screen/web%20search.gif" width="100%" alt="web search"></a><br>
  <sub><b>web search</b></sub>
</td>
<td align="center" width="33%">
  <a href="docs/screen/web%20doctor.png"><img src="docs/screen/web%20doctor.png" width="100%" alt="web doctor"></a><br>
  <sub><b>web doctor</b></sub>
</td>
<td align="center" width="33%">
  <a href="docs/screen/ingest%20and%20sync%20web%20ui.png"><img src="docs/screen/ingest%20and%20sync%20web%20ui.png" width="100%" alt="ingest and sync web ui"></a><br>
  <sub><b>ingest and sync web ui</b></sub>
</td>
</tr>
</table>

**Export wizard** — *Exporter → Options → Organize → Preview*

<table>
<tr>
<td align="center" width="33%">
  <a href="docs/screen/export%20wizard%20step%201%20-%20select%20exporter.png"><img src="docs/screen/export%20wizard%20step%201%20-%20select%20exporter.png" width="100%" alt="export wizard step 1 - select exporter"></a><br>
  <sub><b>step 1 — select exporter</b></sub>
</td>
<td align="center" width="33%">
  <a href="docs/screen/export%20wizard%20step%202%20-%20options.png"><img src="docs/screen/export%20wizard%20step%202%20-%20options.png" width="100%" alt="export wizard step 2 - options"></a><br>
  <sub><b>step 2 — options</b></sub>
</td>
<td align="center" width="33%">
  <a href="docs/screen/export%20wizard%20step%203%20-%20organize%20items.png"><img src="docs/screen/export%20wizard%20step%203%20-%20organize%20items.png" width="100%" alt="export wizard step 3 - organize items"></a><br>
  <sub><b>step 3 — organize items</b></sub>
</td>
</tr>
<tr>
<td align="center" width="33%">
  <a href="docs/screen/export%20wizard%20step%204%20-%20select%20theme.png"><img src="docs/screen/export%20wizard%20step%204%20-%20select%20theme.png" width="100%" alt="export wizard step 4 - select theme"></a><br>
  <sub><b>step 4 — select theme</b></sub>
</td>
<td align="center" width="33%">
  <a href="docs/screen/export%20wizard%20step%204%20-%20preview%20fun%20theme.png"><img src="docs/screen/export%20wizard%20step%204%20-%20preview%20fun%20theme.png" width="100%" alt="export wizard step 4 - preview fun theme"></a><br>
  <sub><b>step 4 — preview (fun theme)</b></sub>
</td>
<td width="33%"></td>
</tr>
</table>

**CLI + manager**

<table>
<tr>
<td align="center" width="33%">
  <a href="docs/screen/browse%20in%20cli.png"><img src="docs/screen/browse%20in%20cli.png" width="100%" alt="browse in cli"></a><br>
  <sub><b>browse in cli</b></sub>
</td>
<td align="center" width="33%">
  <a href="docs/screen/autocomplete%20in%20cli.gif"><img src="docs/screen/autocomplete%20in%20cli.gif" width="100%" alt="autocomplete in cli"></a><br>
  <sub><b>autocomplete in cli</b></sub>
</td>
<td align="center" width="33%">
  <a href="docs/screen/manager%20tray%20app.png"><img src="docs/screen/manager%20tray%20app.png" width="100%" alt="manager tray app"></a><br>
  <sub><b>manager tray app</b></sub>
</td>
</tr>
</table>

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

Then, in a fresh shell, run the wizard to author your `config.toml`:

```sh
booki bootstrap
```

It walks you through bookmark sources, embeddings, LLM provider, and (optionally) the menubar manager. The Rust tray app itself is built by the installer — near the end you're asked `Build the optional booki-manager menubar app now? [y/N]`. Answer `y` to compile it and drop a `booki-manager` wrapper alongside `booki` in `$XDG_BIN_HOME`; answer `n` (the default) and build it later with `cd tools/booki-manager && cargo build --release`.

This installer is here for users who want to *try* Booki without thinking about Python venvs. Booki is a small, hackable, single-codebase tool — by philosophy you'll want the editable `git clone` flow above. Use the installer when you want a turnkey setup; use `git clone` when you want to live in the code.

The installer is **idempotent** (re-run any time to update) and **XDG-compliant**:

- Clones / fast-forwards the repo to `$XDG_DATA_HOME/booki` (default `~/.local/share/booki`).
- Creates a virtualenv at `$XDG_DATA_HOME/booki/.venv` and `pip install -r requirements.txt` inside it — your system Python is never touched.
- Leaves `config.toml` to `booki bootstrap` (only `config.toml.example` ships in the checkout as the documented reference).
- Drops a `booki` wrapper in `$XDG_BIN_HOME` (default `~/.local/bin`) that invokes the venv's python on the dispatcher script.
- Detects your shell (fish / zsh / bash) and idempotently appends a PATH export *and* the matching `shells/booki.fish` / `shells/booki.zsh` source line — completion works without duplicate entries on re-runs.
- Suggests `brew` / `apt` / `dnf` / `pacman` commands for the optional binaries Booki can use (`ffmpeg`, `fzf`, `ollama`).
- Asks at the end whether to build the Rust `booki-manager` tray app — `y` runs `cargo build --release` and drops a `booki-manager` wrapper in `$XDG_BIN_HOME`; `n` (the default) keeps the ~350 MB cargo target cache off your disk until you opt in. The prompt reads `/dev/tty`, so it still works under `curl … | sh`; fully non-interactive runs (CI / cron) fall through to no. Re-running the installer asks again.

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

A small, native macOS / Linux menubar app written in Rust. It lives next to your clock, watches the browser bookmark files for changes, and runs `booki sync` / `booki ingest` on a schedule so you never have to remember to. From the tray menu you can also trigger **Sync now** / **Ingest now**, control the **Web interface** (Open / Start / Stop / Restart), pick which Booki folder to talk to (so the same manager can switch between a dev clone and your installed copy), and toggle autostart.

The active Booki path is resolved in priority order: `$BOOKI_HOME` → the path you picked from the tray (`~/.config/booki-manager/settings.json`) → current working directory. The middle slot is what makes autostart work — login items don't inherit shell env, so `BOOKI_HOME` from your `~/.zshrc` isn't available; the saved setting is.

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

The Python suite lives under [`tests/`](tests/) and covers the bits that hurt most when broken: the bookmark file parser + URL-hash id (`test_ingest.py`), the corrupted-file loader contract + mtime-fingerprint cache (`test_loader.py`), the `ItemStore` write / update / removed-flag roundtrip (`test_store.py`), every key FastAPI route via `TestClient` against a real `create_app()` (`test_web.py`), and the `booki` CLI dispatcher as a real subprocess (`test_cli.py`). All fixtures are `tmp_path`-scoped — no test touches your real `bookmarks/` or `config.toml`.

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
