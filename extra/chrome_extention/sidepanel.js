// Booki side panel — fzf-style picker.
//
// Fetch is delegated to the background service worker (it owns the cache).
// On Enter we navigate the active tab. Cmd/Ctrl+Enter opens a new tab.

const KIND_GLYPH = {
  bookmark: "🔖",
  video: "🎬",
  channel: "📺",
  photo: "🖼",
  document: "📄",
  github: "🐙",
  file: "📁",
  podcast: "🎧",
  article: "📰",
};

const els = {
  search: document.getElementById("search"),
  results: document.getElementById("results"),
  empty: document.getElementById("empty"),
  modeBtn: document.getElementById("modeBtn"),
  refresh: document.getElementById("refresh"),
  addCurrent: document.getElementById("addCurrent"),
  openOptions: document.getElementById("openOptions"),
  status: document.getElementById("status"),
};

const state = {
  items: [],          // raw items from /api/bookmarks
  filtered: [],       // post-filter list rendered in the UI
  kinds: ["bookmark"],
  fuzzy: true,
  selected: 0,
};

// ─── Search algorithms ─────────────────────────────────────────────────────
// Substring is a simple includes(); fuzzy is a port of the project's existing
// fzf-ish scorer (chars in order, contiguous wins, anchored wins more).

function substringMatch(q, text) {
  if (!q) return { score: 1, matches: [] };
  const idx = (text || "").toLowerCase().indexOf(q);
  if (idx < 0) return null;
  const matches = [];
  for (let i = 0; i < q.length; i++) matches.push(idx + i);
  return { score: q.length / (text.length + 1), matches };
}

function fuzzyMatch(q, text) {
  if (!q) return { score: 1, matches: [] };
  const t = (text || "").toLowerCase();
  let qi = 0, lastHit = -2, score = 0;
  const matches = [];
  for (let i = 0; i < t.length && qi < q.length; i++) {
    if (t[i] === q[qi]) {
      matches.push(i);
      let bonus = 1;
      if (i === 0 || /[\s\-_/.:]/.test(t[i - 1])) bonus += 2;        // anchored
      if (i === lastHit + 1) bonus += 2;                              // contiguous
      score += bonus;
      lastHit = i;
      qi++;
    }
  }
  if (qi !== q.length) return null;
  return { score: score / (t.length + 1), matches };
}

// ─── Render ────────────────────────────────────────────────────────────────

function render() {
  const q = els.search.value.trim().toLowerCase();
  const fn = state.fuzzy ? fuzzyMatch : substringMatch;

  let pool = state.items;
  if (state.kinds.length) {
    const set = new Set(state.kinds);
    pool = pool.filter(b => set.has(b.kind));
  }

  let items;
  if (!q) {
    items = pool.slice().sort((a, b) => (b.importance || 0) - (a.importance || 0));
  } else {
    const scored = [];
    for (const b of pool) {
      const tm = fn(q, b.title || "");
      const um = fn(q, b.url || "");
      if (!tm && !um) continue;
      const s = (tm ? tm.score * 3 : 0) + (um ? um.score * 1.5 : 0)
              + (b.importance || 0) * 0.4;
      scored.push({ b, s });
    }
    scored.sort((a, b) => b.s - a.s);
    items = scored.map(x => x.b);
  }

  state.filtered = items.slice(0, 200);
  state.selected = 0;

  if (!state.filtered.length) {
    els.results.innerHTML = "";
    els.empty.classList.remove("hidden");
    return;
  }
  els.empty.classList.add("hidden");

  els.results.innerHTML = state.filtered.map((b, i) => `
    <li class="row${i === 0 ? " active" : ""}" data-i="${i}">
      <span class="glyph">${escapeHtml(KIND_GLYPH[b.kind] || "·")}</span>
      <span class="title">${escapeHtml(b.title || "(untitled)")}</span>
      <span class="url">${escapeHtml(b.url || "")}</span>
    </li>
  `).join("");

  els.results.querySelectorAll(".row").forEach(li => {
    li.addEventListener("click", (e) => {
      const i = Number(li.dataset.i);
      state.selected = i;
      openSelected(e.metaKey || e.ctrlKey ? "new" : "current");
    });
  });
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function setStatus(text) { els.status.textContent = text; }

// ─── Open ──────────────────────────────────────────────────────────────────

async function openSelected(where) {
  const b = state.filtered[state.selected];
  if (!b?.url) return;
  if (where === "new") {
    await chrome.tabs.create({ url: b.url });
  } else {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (tab) await chrome.tabs.update(tab.id, { url: b.url });
  }
  // Side panel intentionally stays open after a navigation — user may pick another.
}

function moveSelection(delta) {
  if (!state.filtered.length) return;
  const max = state.filtered.length;
  state.selected = (state.selected + delta + max) % max;
  els.results.querySelectorAll(".row").forEach((li, i) => {
    li.classList.toggle("active", i === state.selected);
  });
  const active = els.results.querySelector(".row.active");
  active?.scrollIntoView({ block: "nearest" });
}

// ─── Loading ───────────────────────────────────────────────────────────────

async function load(force = false) {
  setStatus("Loading…");
  let res;
  try {
    res = await chrome.runtime.sendMessage({ type: "booki:list", force });
  } catch (err) {
    setStatus(`Error: ${err.message}`);
    return;
  }
  if (!res || !res.items) {
    setStatus("Booki not reachable. Check Settings.");
    return;
  }
  state.items = res.items;
  const ts = res.fetchedAt ? new Date(res.fetchedAt).toLocaleTimeString() : "—";
  setStatus(`${res.items.length} item${res.items.length === 1 ? "" : "s"} · cached ${ts}${res.stale ? " (refreshing…)" : ""}`);
  render();
}

async function loadSettings() {
  const stored = await chrome.storage.sync.get({ kinds: ["bookmark"], fuzzy: true });
  state.kinds = stored.kinds;
  state.fuzzy = !!stored.fuzzy;
  els.modeBtn.textContent = state.fuzzy ? "fuzzy" : "substring";
}

// ─── Wiring ────────────────────────────────────────────────────────────────

els.search.addEventListener("input", render);
els.search.addEventListener("keydown", (e) => {
  if (e.key === "ArrowDown") { e.preventDefault(); moveSelection(1); }
  else if (e.key === "ArrowUp") { e.preventDefault(); moveSelection(-1); }
  else if (e.key === "Enter") { e.preventDefault(); openSelected(e.metaKey || e.ctrlKey ? "new" : "current"); }
  else if (e.key === "Escape") {
    if (els.search.value) { els.search.value = ""; render(); }
  }
});

els.modeBtn.addEventListener("click", async () => {
  state.fuzzy = !state.fuzzy;
  els.modeBtn.textContent = state.fuzzy ? "fuzzy" : "substring";
  chrome.storage.sync.set({ fuzzy: state.fuzzy });
  render();
});

els.refresh.addEventListener("click", () => load(true));

els.addCurrent.addEventListener("click", async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.url) return;
  setStatus("Adding…");
  await chrome.runtime.sendMessage({ type: "booki:add", url: tab.url, title: tab.title || "" });
  setStatus("Added — refreshing…");
  await load(true);
});

els.openOptions.addEventListener("click", () => chrome.runtime.openOptionsPage());

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "sync") return;
  if (changes.kinds || changes.fuzzy) {
    loadSettings().then(render);
  }
});

(async () => {
  await loadSettings();
  await load(false);
  els.search.focus();
})();
