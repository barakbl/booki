# 🏛️ Architecture

## Design philosophy

Booki is a **personal RAG pipeline**. Four principles drive every design choice:

1. **One Markdown file per item is the source of truth.** Not a database row, not an opaque blob — a plain `.md` file with YAML frontmatter you can read, edit, grep, version-control, or open in Obsidian. The vector DB is a *derived index* you can throw away and rebuild from the markdown at any time.
2. **Everything interesting is a plugin.** The core pipeline (`sync → store → ingest → chat`) is source-agnostic and kind-agnostic — it works on `Item`s. Four plugin types extend it: **sources** produce items, **enrichers** add metadata to items that already exist, **exporters** turn a selection into an artifact, and **tab contributions** add UI surfaces to the web app. Adding a new one is ~30 lines and changes nothing else.
3. **Local by default, cloud opt-in.** The default config runs entirely on your machine: Ollama for the LLM, local sentence-transformers for embeddings, ChromaDB on disk. Cloud LLMs and embeddings are one toggle in `config.toml`; nothing leaves your machine unless you ask it to.
4. **Proudly no-build.** The web UI is plain `index.html` + one `app.js` + one `styles.css`, served as-is. No bundler, no transpiler, no `node_modules`. Plugin tabs ship a `tab.js` + `tab.css` next to their Python and the host loads them via dynamic `import()`. The whole front-end is auditable in one editor session, and anyone with a browser and `python3` can hack on it.

## The pipeline

```
Sources (plugins)         One .md per item        Vector DB        You
─────────────────         ────────────────        ─────────        ───
Chrome    ──┐
Safari    ──┤
Firefox   ──┼──▶  sync.py  ──▶  bookmarks/  ──▶  ingest.py  ──▶  db/  ──▶  chat.py
YouTube   ──┤     (+ --enrich)                   (ChromaDB)            (or web.py)
RSS       ──┤
your own  ──┘
```

1. **`sync.py`** runs each registered source plugin and writes one `.md` per item via `store.py`. Optional `--enrich` fetches each page with `trafilatura`, sends content to your configured LLM, and writes back a one-sentence summary + keywords. Re-running is idempotent — items are identified by URL hash, so user edits (importance, tags, notes) are preserved across re-syncs and re-enrichments.
2. **`ingest.py`** parses every `.md` file and embeds it into ChromaDB. Safe to re-run — it upserts by URL hash, no duplicates. The URL itself is deliberately excluded from the embedding text (it's noise); titles, paths, summaries, keywords, tags, notes, and source-specific scalars feed the index.
3. **`chat.py`** turns a natural-language query into a vector search and asks the LLM to synthesize an answer over the retrieved items.
4. **`web.py`** serves the same data via a FastAPI UI for browsing, editing, and running the export wizard. See [`web.md`](web.md).

## What Booki is *not* for

- **Not a research-grade RAG.** No re-ranking, no hybrid retrieval, no query rewriting. It's tuned for personal-scale collections, where simple semantic search over enriched titles works surprisingly well.
- **Not a multi-user / shared service.** No auth, no tenancy, no user model. It assumes one human, one machine, one library.
- **Not a backup tool.** It indexes references *to* content. The `offline_archive` exporter saves snapshots, but Booki is not a "save the whole web" archiver like ArchiveBox.
- **Not for high-frequency ingestion.** The pipeline is batch-oriented (`sync → ingest`); it's not a streaming firehose. Run it once a day, not once a second.
- **Not a bookmark *manager***. Booki doesn't replace your browser's bookmarks UI — it pulls *from* it. Continue using the browser to add and organize bookmarks; Booki indexes them.

## Project structure

```
booki/
├── booki               # 🎬 entry point — `booki <subcommand>` dispatcher (no .py suffix)
│
├── core/               # all main pipeline modules — imported by `booki`
│   ├── __init__.py
│   ├── sync.py         #   plugin orchestrator: sources → markdown + --enrich
│   ├── store.py        #   ItemStore — generic "one .md per item" persistence
│   ├── ingest.py       #   markdown → vector DB
│   ├── chat.py         #   natural language search + LLM answer
│   ├── web.py          #   FastAPI UI (browse / search / edit / export wizard)
│   ├── exporter.py     #   selection model + resolver for the export system
│   ├── download.py     #   yt-dlp wrapper (used by web UI + offline_archive)
│   ├── browse.py       #   fzf TUI browser
│   ├── smart_lists.py  #   dynamic lists (filters/queries) used by the web UI
│   └── system_status.py#   availability checks for binaries / configs / providers
│
├── shells/             # shell integrations (PATH + tab-completion)
│   ├── booki.fish
│   └── booki.zsh
│
├── plugins/            # 🔌 sources / enrichers / exporters / tabs — auto-discovered on import
│   ├── __init__.py     #   discovery: imports each subpackage so decorators fire
│   ├── base.py         #   Item, Source / Enricher / Exporter ABCs, TabContribution, registries
│   ├── browsers/       #   Chrome / Safari / Firefox sources
│   ├── youtube/        #   YouTube account (OAuth) — see README inside
│   │   └── README.md
│   ├── rss/            #   RSS / Atom feeds
│   ├── directory/      #   local file trees as bookmarks
│   ├── enrichers/      # 🧬 metadata enrichers — promote `kind`, tag `sources`
│   │   ├── github/           #   stars / forks / topics / languages / contributors
│   │   ├── youtube/          #   channel / duration / view_count via yt-dlp
│   │   ├── photo/            #   kind=photo by URL extension or known host
│   │   └── document/         #   kind=document for PDFs / docx / md / epub / …
│   │       └── web/static/   #   tab.js + tab.css — contributes the Documents tab
│   └── exporters/      # 📤 exporter plugins
│       ├── link_page/        #   themed HTML page (cappuccino + Catppuccin Mocha)
│       ├── offline_archive/  #   ZIP with monolith pages + yt-dlp videos
│       ├── data_dump/        #   JSON / CSV
│       ├── llm_prompt/       #   Markdown prompt bundle
│       └── bookmark_file/    #   Netscape bookmarks.html (browser-importable)
│
├── web/                # static front-end assets served by web.py — no build step
│   ├── index.html
│   ├── app.js          #   tab registry, plugin-tab loader, host surface (window.booki)
│   └── styles.css
├── themes/             # user-installed exporter themes (override built-ins)
│
├── config.toml.example # fully-commented reference config — copy to config.toml
├── config.toml         # your configuration (includes [sources.*], [enrichers.*], [downloads])
├── requirements.txt
├── CHANGELOG.md        # release history (Keep a Changelog format)
│
├── bookmarks/          # generated markdown files (gitignore if private)
├── downloads/          # yt-dlp video cache (gitignore)
├── exports/            # exporter artifacts + saved configs (gitignore)
└── db/                 # ChromaDB vector store (gitignore)
```

> 💡 Add `bookmarks/`, `db/`, `downloads/`, and `exports/` to `.gitignore` if your bookmarks contain private URLs.

## The item file

Each `.md` looks like this:

```markdown
---
title: "Cursor — AI Code Editor"
url: https://cursor.sh
source: Chrome
kind: bookmark
browser_path: "Chrome › Bookmarks Bar › ai"
folder_path: chrome/bookmarks_bar/ai
importance: 9
tags: ["ai", "tools", "coding"]
notes: "great reference for RAG"
date_bookmarked: 2024-01-15
last_sync: 2026-04-16
status: unchecked
archive_url: ""
removed_from_browser: false
# ── Enrichment (filled by --enrich) ──
last_enriched: 2026-04-16
enrich_source: page                 # "page" | "title-only" | "description"
page_title: "Cursor — The AI Code Editor"
summary: "AI-first code editor built on VSCode with deep copilot integration."
keywords: ["ai", "editor", "vscode", "copilot"]
---

# Cursor — AI Code Editor
…
```

Filename convention: `slug(title)--hash(url).md`. The URL hash is the stable identity, so renaming the title renames the file but preserves all your edits and enrichment.

**User-editable fields** — preserved across re-syncs and re-enrichments:

| Field        | Description |
|--------------|-------------|
| `importance` | `0`–`10` — how important this link is to you |
| `tags`       | Free-form tags, e.g. `["ai", "tools", "research"]` |
| `notes`      | Your personal annotation |

**Source-specific fields** — each plugin may add its own keys (declared via `field_specs()`); see the per-plugin docs for what each one writes (e.g. [`plugins/youtube/README.md`](../plugins/youtube/README.md)).

## Enrichment

Titles like `"clerks - comments"` or `"API keys | Google AI Studio"` give a vector search nothing to work with. `--enrich` fixes that:

1. Fetch the page with `trafilatura` → main content stripped of ads and nav.
2. Send `title + URL + tags + notes + page_content` to your configured LLM.
3. Get back a one-sentence summary, 5–10 keywords, and a cleaned page title.
4. Write them into the item's frontmatter.

Pages that can't be fetched (auth-gated, JS-rendered, dead) still get a conservative **title-only** summary. The result: English queries match Hebrew titles, cryptic dashboard URLs become findable, and semantic search actually feels semantic.

## What gets embedded

For each item, `ingest.py` builds an embedding text like:

```
Title: Cursor — AI Code Editor
Kind: bookmark              (omitted if "bookmark")
Path: Chrome › Bookmarks Bar › ai
Channel: Fireship           (YouTube items)
Tags: ai, tools, coding
YouTube tags: rust, intro   (YouTube items)
Keywords: ai, editor, vscode, copilot
Notes: great reference for RAG
Summary: AI-first code editor built on VSCode with deep copilot integration.
Description: …             (source-provided text)
```

Stored as filterable metadata: `url`, `title`, `kind`, `importance`, `tags`, `keywords`, `notes`, `summary`, `status`, `source`, `browser_path`, `folder_path`, `archive_url`, `enriched`, plus source-specific scalars: `channel`, `channel_id`, `video_id`, `duration`, `published_at`, `view_count`, `liked`, `watched`, `subscribed`, `subscribed_to_channel`.

## Configuration — `config.toml`

All scripts read from one TOML file:

```toml
[bookmarks]
dir = "./bookmarks"
min_importance = 0                    # skip items below this score at ingest

[vector_db]
type = "chromadb"
persist_dir = "./db"
collection = "bookmarks"

[embeddings]
provider = "local"                     # "local" | "openai"
local_model = "all-MiniLM-L6-v2"       # free, ~80MB, runs offline
openai_model = "text-embedding-3-small"

[llm]
provider = "ollama"                    # "ollama" | "claude" | "openai"
model = "llama3.2:3b"
base_url = "http://localhost:11434"    # ollama only
n_results = 5

[enrichment]
enabled = true
max_content_chars = 4000
fetch_timeout = 15
llm_timeout = 120

[web]
host = "127.0.0.1"
port = 8765

[logs]
level         = "INFO"                # global default for booki.* loggers
console       = "human"               # "human" | "json" | "off"
file          = "./logs/booki.log"    # relative paths resolve from project root; "" = no file
file_format   = "json"                # "human" | "json"
max_bytes     = 10_485_760            # 10 MB before rotating
backup_count  = 5

[logs.levels]                          # per-logger overrides
"chromadb"        = "WARNING"
"uvicorn.access"  = "INFO"

# ─── Sources (plugins) ─────────────────────────────────────────────
# Each [sources.<name>] subtable is passed to that source via
# Source.configure(). Sources without a subtable use built-in defaults.

[sources.chrome]    # no options
[sources.safari]    # no options (needs Full Disk Access on macOS)
[sources.firefox]   # no options
[sources.youtube]   # see plugins/youtube/README.md

[[sources.rss.feeds]]
name = "Hacker News"
url  = "https://hnrss.org/frontpage"

[[sources.directory.dirs]]
name = "research-papers"
path = "~/Documents/papers"
```

### Provider matrix

| Embedding provider | Model | Privacy | Cost | Requires |
|---|---|---|---|---|
| `local` ✅ default | `all-MiniLM-L6-v2` | 🔒 fully local | Free | `sentence-transformers` |
| `openai`          | `text-embedding-3-small` | ☁️ API | ~$0 | `OPENAI_API_KEY` |

| LLM provider | Example models | Privacy | Requires |
|---|---|---|---|
| `ollama` ✅ default | `llama3.2:3b`, `mistral`, `gemma3` | 🔒 fully local | [Ollama](https://ollama.ai) running |
| `claude` | `claude-sonnet-4-6`, `claude-haiku-4-5` | ☁️ API | `ANTHROPIC_API_KEY` |
| `openai` | `gpt-4o-mini`, `gpt-4o` | ☁️ API | `OPENAI_API_KEY` |

## Typical workflow

```bash
# First-time setup
booki sync                          # pull from all sources
booki sync --no-sync --enrich       # LLM-summarize every item (one-time)
booki ingest                        # build the vector index

# Daily / weekly
booki sync --enrich                 # pick up new items + enrich them
booki ingest                        # update the index

# Occasionally
booki sync --check-dead-links       # find and archive broken links
booki sync --no-sync --enrich --all # re-enrich everything (after model change)

# Search anytime
booki chat "anything you want to find"
booki web                               # or use the web UI
```

## Dependencies

| Package | Used by | Purpose |
|---|---|---|
| `requests`              | sync, chat, offline_archive | HTTP, Ollama API, raw PDF download |
| `trafilatura`           | sync                        | Page content extraction for `--enrich` |
| `chromadb`              | ingest, chat                | Vector database |
| `sentence-transformers` | ingest, chat                | Local embeddings |
| `jinja2`                | exporters                   | Theme rendering |
| `fastapi` / `uvicorn`   | web                         | Web UI server |
| `anthropic`             | chat (optional)             | Claude LLM |
| `openai`                | chat / ingest (optional)    | OpenAI LLM / embeddings |
| `yt-dlp`                | download, offline_archive   | Video downloads |

External binaries (only when you use the matching feature):

| Tool       | Install                  | Used by |
|------------|--------------------------|---------|
| `monolith` | `brew install monolith`  | `offline_archive` exporter |
| `ffmpeg`   | `brew install ffmpeg`    | `yt-dlp` muxing / audio extraction |

## Logging

Stdlib `logging`, configured once from the `[logs]` section of `config.toml` and reapplied inside `core.web.create_app()` so uvicorn `--reload` children pick it up too. The dispatcher accepts `--log-level LEVEL`, `-v` (INFO), `-vv` (DEBUG), `-q` (WARNING), and the `BOOKI_LOG_LEVEL` env var as one-shot overrides.

- **Console** — colored human format on stderr when it's a TTY; `[logs] console = "json" | "off"` to switch.
- **File** — rotating JSON lines under `./logs/booki.log` by default (gitignored). Configurable via `[logs] file`, `file_format`, `max_bytes`, `backup_count`.
- **Per-logger overrides** — `[logs.levels]` quietens noisy libraries (`chromadb`, `sentence_transformers`, `urllib3`, `httpx`, `httpcore`, `watchfiles`) and tunes uvicorn (`uvicorn.access`).
- **Logger names** — every module uses `logging.getLogger("booki.<area>")` (`booki.sync`, `booki.web`, `booki.chat`), so per-component filtering is trivial.
- **Convention** — user-facing CLI feedback (`[chrome] Checking availability... ok`, summary tables, search results) stays as `print()`. `logging` is reserved for warnings, errors, and structured internal events (LLM calls with provider/model/duration, page enrichment with fetch timing, export runs with selection size + duration).

## Release history

See [`CHANGELOG.md`](../CHANGELOG.md) for what's landed in each version.
