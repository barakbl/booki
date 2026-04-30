// Options page — host URL + which kinds to include in the picker.
// Available kinds are inferred from the live bookmarks list.

const els = {
  host: document.getElementById("hostUrl"),
  hostHint: document.getElementById("hostHint"),
  kinds: document.getElementById("kinds"),
  save: document.getElementById("save"),
  test: document.getElementById("test"),
  status: document.getElementById("status"),
};

const FALLBACK_KINDS = ["bookmark", "video", "channel", "photo", "document", "github", "file", "podcast", "article"];

let availableKinds = [...FALLBACK_KINDS];
let selectedKinds = new Set(["bookmark"]);

function renderKinds() {
  els.kinds.innerHTML = "";
  for (const k of availableKinds) {
    const id = `kind-${k}`;
    const wrap = document.createElement("label");
    wrap.className = "check";
    wrap.innerHTML = `
      <input type="checkbox" id="${id}" ${selectedKinds.has(k) ? "checked" : ""}>
      <span>${escapeHtml(k)}</span>
    `;
    wrap.querySelector("input").addEventListener("change", e => {
      if (e.target.checked) selectedKinds.add(k);
      else selectedKinds.delete(k);
    });
    els.kinds.appendChild(wrap);
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

async function loadKindsFromBooki(hostUrl) {
  try {
    const r = await fetch(joinUrl(hostUrl, "/api/bookmarks"));
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const items = await r.json();
    const set = new Set(FALLBACK_KINDS);
    for (const it of items) {
      if (it.kind) set.add(String(it.kind));
    }
    availableKinds = [...set].sort();
  } catch {
    availableKinds = [...FALLBACK_KINDS];
  }
  renderKinds();
}

function joinUrl(base, path) {
  return base.replace(/\/+$/, "") + path;
}

async function load() {
  const stored = await chrome.storage.sync.get({
    hostUrl: "http://127.0.0.1:8765",
    kinds: ["bookmark"],
  });
  els.host.value = stored.hostUrl;
  selectedKinds = new Set(stored.kinds);
  await loadKindsFromBooki(stored.hostUrl);
}

els.save.addEventListener("click", async () => {
  const hostUrl = els.host.value.trim().replace(/\/+$/, "") || "http://127.0.0.1:8765";
  await chrome.storage.sync.set({
    hostUrl,
    kinds: [...selectedKinds],
  });
  els.status.textContent = "Saved.";
  els.status.className = "status ok";
  // Invalidate cache so the new host is queried fresh.
  chrome.storage.local.remove("booki.cache").catch(() => {});
  setTimeout(() => { els.status.textContent = ""; }, 2000);
});

els.test.addEventListener("click", async () => {
  const hostUrl = els.host.value.trim().replace(/\/+$/, "") || "http://127.0.0.1:8765";
  els.status.textContent = "Testing…";
  els.status.className = "status";
  try {
    const r = await fetch(joinUrl(hostUrl, "/api/health"));
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const j = await r.json();
    els.status.textContent = `OK · ${j.indexed ?? "?"} indexed items`;
    els.status.className = "status ok";
    await loadKindsFromBooki(hostUrl);
  } catch (err) {
    els.status.textContent = `Failed: ${err.message}`;
    els.status.className = "status err";
  }
});

load();
