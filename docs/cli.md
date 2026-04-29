# 🖥 CLI

Booki is **CLI-first**: every operation — sync, enrich, ingest, search, browse, export, download, doctor — is a subcommand of the single `./booki` dispatcher. The web UI is a co-equal frontend over the same data, not a wrapper around the CLI: edits made in either flow through to the same `bookmarks/<source>/<slug>.md` files and the same ChromaDB index.

If you want to script Booki, schedule it from `cron`, run it on a headless box, or never leave your terminal — the CLI is the whole product.

## Dispatcher

```bash
./booki [global flags] <subcommand> [subcommand options]
```

Global flags must precede the subcommand:

| Flag                  | Effect                                  |
|-----------------------|-----------------------------------------|
| `--log-level LEVEL`   | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` |
| `-v` / `-vv` / `-q`   | shorthand for `INFO` / `DEBUG` / `WARNING` |
| `BOOKI_LOG_LEVEL=...` | env-var equivalent (the flag wins)      |

Run `./booki <subcommand> --help` for that subcommand's flags.

After sourcing one of the shell integrations under [`shells/`](../shells/), drop the leading `./` and pick up tab-completion for every subcommand:

```fish
# ~/.config/fish/config.fish
source /path/to/booki/shells/booki.fish
```

```zsh
# ~/.zshrc
source /path/to/booki/shells/booki.zsh
```

## Subcommands

| Subcommand        | What it does |
|-------------------|--------------|
| `booki sync`      | Pull items from every registered source plugin (Chrome / Safari / Firefox / YouTube / RSS / directories / your own). Writes one Markdown file per item. Supports `--source <name>`, `--check-dead-links`, `--enrich`, `--enrich-meta`, `--all`, `--dry-run`. |
| `booki ingest`    | (Re-)build the ChromaDB vector index from the Markdown files. |
| `booki chat`      | Natural-language search + LLM-synthesized answer over the index. `--no-llm` for plain retrieval; `--n` to set hit count; `--min-importance` to filter. |
| `booki browse`    | **fzf-powered terminal browser** over your library — see below. |
| `booki web`       | Start the FastAPI web UI. See [`web.md`](web.md). |
| `booki download`  | Fetch full-page snapshots and (for videos) media files for offline use. Backs the offline-archive exporter. |
| `booki doctor`    | Availability checks: installed Python packages, external binaries (`monolith`, `yt-dlp`, `ffmpeg`, `fzf`), provider config (Ollama up? `OPENAI_API_KEY` set? Safari accessible?). |
| `booki bootstrap` | First-run scaffolding: copy `config.toml.example`, create the bookmarks directory, etc. |

## `booki browse` — fzf-powered fast search

`browse` is a fast, dependency-light terminal UI built on top of [fzf](https://github.com/junegunn/fzf). It opens **instantly** — it reads the Markdown frontmatter directly and never imports `chromadb`, so there's no embedding-model startup cost. Type to filter; the right-hand pane previews everything Booki knows about the highlighted item.

```bash
booki browse
```

Each row shows a kind glyph, an importance star, status flags (♥ liked, ✓ watched, ↻ subscribed, `[dead]`, `[removed]`), the title, and a context tail (channel for videos, folder path for bookmarks).

### Key bindings

| Key       | Action                                                |
|-----------|-------------------------------------------------------|
| `Enter`   | open the URL in your default browser, then exit       |
| `Ctrl-O`  | open the URL **without** exiting (keep browsing)      |
| `Ctrl-Y`  | copy the URL to the system clipboard                  |
| `Ctrl-E`  | open the underlying `.md` file in `$EDITOR`           |
| `Ctrl-R`  | reload the list from disk (after a `sync` from another shell) |
| `Ctrl-/`  | toggle the preview pane                               |

Plus all of fzf's own matching syntax: `'exact`, `^prefix`, `suffix$`, `!negate`, `term1 | term2`. `browse` runs fzf in `--exact` mode by default, so each space-separated token is matched as a substring; prefix a token with a single `'` to make it fuzzy for that token only.

### When to use which search

| | best for |
|--|--|
| `booki browse` | **fast lookup by title / channel / folder** when you mostly know what you're looking for. Zero startup cost. Pure-text matching, no LLM, no embeddings. |
| `booki chat "..."` | **semantic** search — finds items by meaning, cross-language. Requires `booki ingest` first. |
| `booki web` (Search tab) | same fast text search, but with thumbnails, advanced filters, and inline editing. |

### Requirements

- `fzf` on `$PATH`. On macOS: `brew install fzf`. On Debian/Ubuntu: `apt install fzf`.
- A populated bookmarks directory (`booki sync` at least once).
- Optional: `pbcopy` (macOS, built-in) or `wl-copy` / `xclip` / `xsel` (Linux) for `Ctrl-Y`.

## Logging

Booki logs to `logs/booki.log` (rotated). Anything you'd see at `--log-level DEBUG` is in the file even if the console is quiet — tail it with `booki web` running and watch the Manage → Logs sub-tab, or just `tail -f logs/booki.log`.

## Scripting tips

- All subcommands exit non-zero on failure and write a structured JSON line to the log on every meaningful event — `jq` over `logs/booki.log` is a viable observability story.
- `booki sync --dry-run` previews without writing, useful in CI sanity checks.
- `booki browse --list` emits the raw TSV (`url\tpath\tdisplay`) that fzf consumes — pipe it into your own picker if you don't want fzf.
- The web UI's JSON API ([`web.md`](web.md#api)) is the right surface for richer scripting (filters, exports, downloads).
