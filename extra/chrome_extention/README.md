# 📚 Booki Chrome Extension

A Chrome extension that gives Booki two superpowers in your browser:

1. **Quick picker** — a side-panel list of your Booki items with fzf-style fuzzy / substring search. Hit **Alt+Shift+B** (rebindable) to open. Enter opens the highlighted item in the current tab; ⌘/Ctrl+Enter opens it in a new tab.
2. **Right-click → Add to Booki** — adds a link, page, or image to your Booki via the existing `POST /api/link` endpoint, with a green ✓ badge on the toolbar icon and a top-right toast on the page.

---

## Install (unpacked / dev mode)

1. Run Booki locally (`./booki web` — defaults to `http://127.0.0.1:8765`).
2. Open `chrome://extensions/`.
3. Toggle **Developer mode** (top-right).
4. Click **Load unpacked** → pick this folder (`extra/chrome_extention/`).
5. Open the extension's **Settings** (gear icon in the side panel, or right-click the toolbar icon → Options) and confirm the host URL. Click **Test connection** to verify.

---

## Settings

| Setting | What it does |
|---------|-------------|
| **Booki host** | Default `http://127.0.0.1:8765`. Change if your Booki runs elsewhere. |
| **Show in picker** | Which item kinds appear in the picker list. Default: just `bookmark`. The list of available kinds is auto-discovered from your live Booki items. |

The fuzzy-vs-substring toggle lives **inside** the picker (top-right of the search input) so you can flip it per session. The choice is persisted.

---

## Caching

The picker list is cached in `chrome.storage.local` indefinitely. It's re-fetched in the background after 24h, after **Save** in Settings, after a successful **Add to Booki**, or when you hit the ↻ button in the side panel header.

---

## Keyboard shortcut

Default: **Alt+Shift+B**. Rebind via `chrome://extensions/shortcuts`.

In the picker:

| Key | Action |
|-----|-------|
| `↑` / `↓` | Move selection |
| `Enter` | Open in current tab |
| `⌘ Enter` / `Ctrl+Enter` | Open in new tab |
| `Esc` | Clear search |

---

## Files

```
manifest.json     — MV3 manifest
background.js     — service worker (commands, context menus, fetch + cache)
sidepanel.html    — picker UI
sidepanel.js      — picker logic + search algorithms
sidepanel.css
options.html      — settings page
options.js
options.css
content.js        — page-injected toast helper
icons/            — 16/48/128 PNG icons
```
