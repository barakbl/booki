// Cross-browser background logic. The platform-specific service-worker
// entry point (chrome/platform.js, firefox/platform.js) imports `init` and
// passes a small adapter — currently just `openPicker(tab)` — that wraps
// the browser-specific way of opening the side panel / sidebar.

const DEFAULTS = {
  hostUrl: "http://127.0.0.1:8765",
  kinds: ["bookmark"],
  fuzzy: true,
};

const CACHE_KEY = "booki.cache";
const CACHE_TTL_MS = 24 * 60 * 60 * 1000;

async function getSettings() {
  const stored = await chrome.storage.sync.get(DEFAULTS);
  return { ...DEFAULTS, ...stored };
}

function joinUrl(base, path) {
  return base.replace(/\/+$/, "") + path;
}

function showBadge(text, color) {
  chrome.action.setBadgeText({ text }).catch(() => {});
  chrome.action.setBadgeBackgroundColor({ color }).catch(() => {});
  setTimeout(() => chrome.action.setBadgeText({ text: "" }).catch(() => {}), 2500);
}

async function notifyTab(tabId, payload) {
  if (!tabId) return;
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      files: ["content.js"],
    });
    await chrome.tabs.sendMessage(tabId, { type: "booki:toast", ...payload });
  } catch {
    chrome.notifications?.create({
      type: "basic",
      iconUrl: "icons/icon-48.png",
      title: "Booki",
      message: payload.text,
    });
  }
}

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
  chrome.storage.local.remove(CACHE_KEY).catch(() => {});
  showBadge(result.is_new ? "✓" : "·", result.is_new ? "#22c55e" : "#9aa3b2");
  notifyTab(tab?.id, {
    kind: "ok",
    text: result.is_new
      ? `Added: ${result.title || url}`
      : `Already in Booki: ${result.title || url}`,
  });
}

async function getBookmarks(force) {
  const { hostUrl } = await getSettings();
  const cached = (await chrome.storage.local.get(CACHE_KEY))[CACHE_KEY];
  const fresh = cached && (Date.now() - cached.fetchedAt < CACHE_TTL_MS);
  if (cached && !force) {
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

export function init({ openPicker }) {
  chrome.runtime.onInstalled.addListener(() => {
    installContextMenus();
  });

  chrome.commands.onCommand.addListener(async (cmd) => {
    if (cmd !== "open-picker") return;
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab) return;
    try { await openPicker(tab); }
    catch (err) { console.error("[booki] openPicker:", err); }
  });

  chrome.contextMenus.onClicked.addListener(async (info, tab) => {
    let url = "", title = "";
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
    if (url) await addLink(url, title, tab);
  });

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    (async () => {
      if (msg?.type === "booki:list") {
        sendResponse(await getBookmarks(!!msg.force));
      } else if (msg?.type === "booki:add") {
        await addLink(msg.url, msg.title || "", null);
        sendResponse({ ok: true });
      }
    })();
    return true;
  });
}
