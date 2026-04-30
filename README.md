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
- **🤖 LLM answers** — asks an AI to synthesize an answer over the retrieved items.
- **🔗 Dead link detection** — flags broken bookmarks and suggests Wayback Machine archives.
- **📤 Pluggable exporters** — turn a selection into a themed HTML page, an offline ZIP (full pages + downloaded videos), a JSON dump, an LLM-ready prompt, or a browser-importable bookmarks file.
- **🌐 Tab-based web UI** — Search, Photos, Documents, Videos, Ask, Manage. Each tab has its own scoped search; the Photos / Videos tabs render thumbnail grids; Documents toggles between list and grid; Manage hosts inline doctor / status, general info, plugin admin, and a syntax-highlighted log viewer.
- **🧩 Plugin-contributed tabs** — plugins can ship a `tab.js` + `tab.css` next to their Python and add a top-level tab to the UI through a stable `window.booki` host API.
- **🚫 Proudly no-build** — the web UI is plain `index.html` + one `app.js` + one `styles.css`. No bundler, no transpiler, no `node_modules`. Edit the file, refresh the browser. Plugin tabs follow the same rule.
- **🔒 Privacy-first** — defaults run fully locally with Ollama + local embeddings. Cloud LLMs are opt-in.

---

## 🚀 Install

```bash
git clone https://github.com/barakbl/booki.git
cd booki

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt

# Start from the documented example. Edit it to taste.
cp config.toml.example config.toml
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

## 📖 Documentation

- [`docs/architecture.md`](docs/architecture.md) — design philosophy, directory layout, the sync → ingest → chat pipeline.
- [`docs/cli.md`](docs/cli.md) — the `./booki` dispatcher, every subcommand, and the fzf-powered `browse` picker.
- [`docs/plugins.md`](docs/plugins.md) — the built-in sources, enrichers, exporters, and tab contributions.
- [`docs/plugins_dev.md`](docs/plugins_dev.md) — write your own source / enricher / exporter / tab plugin.
- [`docs/web.md`](docs/web.md) — the tab-based web UI and its JSON API.
- [`extra/extension/README.md`](extra/extension/README.md) — the browser extension (Chrome + Firefox) — side-panel / sidebar picker + right-click "Add to Booki".
- [`config.toml.example`](config.toml.example) — fully-commented reference config.
- [`CHANGELOG.md`](CHANGELOG.md) — release history.

---

## 📄 License

MIT — see [LICENSE](LICENSE).
