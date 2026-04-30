// Firefox service worker entry — wires the cross-browser core to Firefox's
// `sidebarAction` API. Firefox accepts the chrome.* aliases and exposes the
// sidebar through chrome.sidebarAction.

import { init } from "./background-core.js";

// Open the sidebar when the toolbar icon is clicked. (Chrome handles this
// automatically via setPanelBehavior; Firefox needs an explicit listener.)
chrome.action?.onClicked?.addListener(() => {
  chrome.sidebarAction.toggle().catch(err => console.error("[booki] sidebar toggle:", err));
});

init({
  async openPicker(_tab) {
    // sidebarAction.open() requires a user gesture, which the keyboard
    // command provides; otherwise toggle() always succeeds.
    try { await chrome.sidebarAction.open(); }
    catch { await chrome.sidebarAction.toggle(); }
  },
});
