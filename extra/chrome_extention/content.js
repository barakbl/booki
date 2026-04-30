// Toast helper, injected into pages on demand by the service worker.
// Idempotent: safe to inject multiple times — guards on a global flag.

(() => {
  if (window.__bookiToastReady) return;
  window.__bookiToastReady = true;

  function ensureRoot() {
    let r = document.getElementById("__booki_toast_root");
    if (r) return r;
    r = document.createElement("div");
    r.id = "__booki_toast_root";
    Object.assign(r.style, {
      position: "fixed",
      top: "16px",
      right: "16px",
      zIndex: "2147483647",
      display: "flex",
      flexDirection: "column",
      gap: "8px",
      pointerEvents: "none",
      fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
      fontSize: "13px",
    });
    (document.documentElement || document.body).appendChild(r);
    return r;
  }

  function show(text, kind) {
    const root = ensureRoot();
    const t = document.createElement("div");
    Object.assign(t.style, {
      padding: "10px 14px",
      borderRadius: "8px",
      background: kind === "error" ? "#dc2626" : "#22c55e",
      color: "#fff",
      boxShadow: "0 6px 24px rgba(0,0,0,0.25)",
      maxWidth: "380px",
      pointerEvents: "auto",
      opacity: "0",
      transform: "translateY(-6px)",
      transition: "opacity 180ms ease, transform 180ms ease",
    });
    t.textContent = `📚 ${text}`;
    root.appendChild(t);
    requestAnimationFrame(() => {
      t.style.opacity = "1";
      t.style.transform = "translateY(0)";
    });
    setTimeout(() => {
      t.style.opacity = "0";
      t.style.transform = "translateY(-6px)";
      setTimeout(() => t.remove(), 220);
    }, 2500);
  }

  chrome.runtime.onMessage.addListener((msg) => {
    if (msg?.type === "booki:toast") show(msg.text, msg.kind);
  });
})();
