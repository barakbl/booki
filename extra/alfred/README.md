# 🔍 Booki — Alfred Workflow

A minimal [Alfred](https://www.alfredapp.com/) workflow for searching your Booki bookmarks from anywhere on macOS.

```
bk <query>     fuzzy-search Booki bookmarks
↵              open the highlighted item in your default browser
⌘ + ↵          copy the URL to the clipboard
```

---

## Install

1. **Download** [`Booki.alfredworkflow`](Booki.alfredworkflow).
2. **Double-click** it. Alfred will import the workflow.
3. Make sure Booki is running locally (`./booki web`).
4. Trigger Alfred (default `⌥ Space`) and type `bk` — you should see your bookmarks.

> Alfred Powerpack license required (Workflows are a paid Alfred feature).

---

## Configure

Open Alfred → **Workflows** → **Booki** → **\[x\]** (Workflow Environment Variables, top right).

| Variable | Default | What it does |
|----------|---------|-------------|
| `booki_host` | `http://127.0.0.1:8765` | URL of your running Booki web UI. |
| `booki_kinds` | (empty = all) | Comma-separated allow-list of `kind` values, e.g. `bookmark` or `bookmark,video`. |

---

## Build from source

The source lives in [`src/`](src/) — a `search.py` script filter, an `info.plist` workflow definition, and an `icon.png`. To rebuild the `.alfredworkflow` bundle from source:

```bash
bash extra/alfred/build.sh
```

The build is just `zip` — no bundler, no transpiler. Output: `Booki.alfredworkflow` next to `build.sh`.

---

## Layout

```
extra/alfred/
├── README.md
├── build.sh                # zips src/ → Booki.alfredworkflow
├── Booki.alfredworkflow    # prebuilt; downloadable directly from the repo
└── src/
    ├── info.plist          # Alfred workflow definition (script filter + open URL + clipboard)
    ├── search.py           # the script filter — hits /api/bookmarks and emits Alfred JSON
    └── icon.png
```

---

## How it works

Three Alfred objects, two connections:

```
[Script Filter "bk"] ──↵──→ [Open URL]
                     └──⌘↵──→ [Copy to Clipboard]
```

`search.py` reads the query, fetches `GET /api/bookmarks` from `$booki_host`, ranks results (title-prefix → title-contains → tag-contains → URL-contains → importance), and emits up to 50 items as Alfred-format JSON. Empty query → top items by importance.

If Booki isn't reachable, the workflow shows a single non-actionable row with the error reason — useful for debugging the host setting.
