// Booki extension — service worker.
//
// Three responsibilities:
//   1. Open the side-panel picker on Alt+Shift+B (or toolbar-icon click).
//   2. Right-click context menus to add link / page / image to Booki.
//   3. Surface results: green badge on success, red on error, plus an
//      injected toast on the active tab.

const DEFAULTS = {
  hostUrl: "http://127.0.0.1:8765",
  kinds: ["bookmark"],     // which kinds appear in the picker
  fuzzy: true,             // search mode
};

const CACHE_KEY = "booki.cache";
const CACHE_TTL_MS = 24 * 60 * 60 * 1000;   // 24h

// ─── Settings ──────────────────────────────────────────────────────────────

async function getSettings() {
  const stored = await chrome.storage.sync.get(DEFAULTS);
  return { ...DEFAULTS, ...stored };
}

// ─── Action / shortcut → side panel ────────────────────────────────────────

chrome.runtime.onInstalled.addListener(() => {
  // Click the toolbar icon → open the side panel for the current tab.
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true })
    .catch(err => console.error("[booki] setPanelBehavior:", err));

  installContextMenus();
});

// chrome.commands fires the keyboard shortcut. We open the side panel
// explicitly because Chrome treats the keystroke as a user gesture.
chrome.commands.onCommand.addListener(async (cmd) => {
  if (cmd !== "open-picker") return;
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) return;
  try {
    await chrome.sidePanel.open({ tabId: tab.id, windowId: tab.windowId });
  } catch (err) {
    // Some Chrome versions need windowId only.
    try { await chrome.sidePanel.open({ windowId: tab.windowId }); }
    catch (e2) { console.error("[booki] sidePanel.open:", e2); }
  }
});

// ─── Context menus ─────────────────────────────────────────────────────────

function installContextMenus() {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: "booki-add-link",
      title: "Add link to Booki",
      contexts: ["link"],
    });
    chrome.contextMenus.create({
      id: "booki-add-page",
      title: "Add this page to Booki",
      contexts: ["page", "selection"],
    });
    chrome.contextMenus.create({
      id: "booki-add-image",
      title: "Add image to Booki",
      contexts: ["image"],
    });
  });
}

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  let url = "";
  let title = "";
  if (info.menuItemId === "booki-add-link") {
    url = info.linkUrl || "";
    title = info.selectionText || "";
  } else if (info.menuItemId === "booki-add-page") {
    url = info.pageUrl || tab?.url || "";
    title = tab?.title || "";
  } else if (info.menuItemId === "booki-add-image") {
    url = info.srcUrl || "";
    title = (tab?.title || "") + " (image)";
  }
  if (!url) return;
  await addLink(url, title, tab);
});

// ─── Link add (used by context menus) ──────────────────────────────────────

async function addLink(url, title, tab) {
  const { hostUrl } = await getSettings();
  let result;
  try {
    const r = await fetch(joinUrl(hostUrl, "/api/link"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, title: title || null }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`);
    result = await r.json();
  } catch (err) {
    showBadge("✗", "#ef4444");
    notifyTab(tab?.id, { kind: "error", text: `Booki: ${err.message}` });
    return;
  }
  // Invalidate cache so the picker shows the new item next time.
  chrome.storage.local.remove(CACHE_KEY).catch(() => {});

  showBadge(result.is_new ? "✓" : "·", result.is_new ? "#22c55e" : "#9aa3b2");
  notifyTab(tab?.id, {
    kind: "ok",
    text: result.is_new ? `Added: ${result.title || url}` : `Already in Booki: ${result.title || url}`,
  });
}

function showBadge(text, color) {
  chrome.action.setBadgeText({ text }).catch(() => {});
  chrome.action.setBadgeBackgroundColor({ color }).catch(() => {});
  setTimeout(() => chrome.action.setBadgeText({ text: "" }).catch(() => {}), 2500);
}

async function notifyTab(tabId, payload) {
  if (!tabId) return;
  // Inject the toast helper, then call it.
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      files: ["content.js"],
    });
    await chrome.tabs.sendMessage(tabId, { type: "booki:toast", ...payload });
  } catch {
    // Some pages forbid script injection (chrome://, devtools, ...). Fall back
    // to a system notification for those.
    chrome.notifications?.create({
      type: "basic",
      iconUrl: "icons/icon-48.png",
      title: "Booki",
      message: payload.text,
    });
  }
}

// ─── Bookmark fetch + cache ────────────────────────────────────────────────
// The side panel asks us for the bookmarks list; we serve from cache when
// fresh and refetch otherwise. Forced refresh arrives as `{type:"refresh"}`.

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    if (msg?.type === "booki:list") {
      const data = await getBookmarks(!!msg.force);
      sendResponse(data);
    } else if (msg?.type === "booki:add") {
      await addLink(msg.url, msg.title || "", null);
      sendResponse({ ok: true });
    }
  })();
  return true;   // keep the channel open for async sendResponse
});

async function getBookmarks(force) {
  const { hostUrl } = await getSettings();
  const cached = (await chrome.storage.local.get(CACHE_KEY))[CACHE_KEY];
  const fresh = cached && (Date.now() - cached.fetchedAt < CACHE_TTL_MS);
  if (cached && !force) {
    // Return cached immediately; trigger a background refetch if stale.
    if (!fresh) refreshInBackground(hostUrl).catch(() => {});
    return { items: cached.items, stale: !fresh, fetchedAt: cached.fetchedAt };
  }
  return await refreshInBackground(hostUrl);
}

async function refreshInBackground(hostUrl) {
  const r = await fetch(joinUrl(hostUrl, "/api/bookmarks"));
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  const items = await r.json();
  const payload = { items, fetchedAt: Date.now() };
  await chrome.storage.local.set({ [CACHE_KEY]: payload });
  return { items, stale: false, fetchedAt: payload.fetchedAt };
}

function joinUrl(base, path) {
  return base.replace(/\/+$/, "") + path;
}
