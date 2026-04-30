// Chrome service worker entry — wires the cross-browser core to Chrome's
// `chrome.sidePanel` API.

import { init } from "./background-core.js";

chrome.runtime.onInstalled.addListener(() => {
  // Clicking the toolbar icon opens the side panel for the current tab.
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true })
    .catch(err => console.error("[booki] setPanelBehavior:", err));
});

init({
  async openPicker(tab) {
    try {
      await chrome.sidePanel.open({ tabId: tab.id, windowId: tab.windowId });
    } catch {
      // Some Chrome builds reject tabId — fall back to windowId only.
      await chrome.sidePanel.open({ windowId: tab.windowId });
    }
  },
});
