// Booki — client-side logic.
// Top-level navigation is a tab bar (Search / Photos / Videos / Manage,
// plus any plugin-contributed tabs from /api/tabs). Built-in tabs are
// registered inline below; plugin tabs ship a JS module that calls
// `booki.tabs.implement(id, { mount, onShow, onHide })` after import.
//
// Within the Search tab there are still two sub-modes:
//   • find — fast in-browser fuzzy match over the full bookmark list.
//   • ask  — POST /api/ask for semantic search + LLM synthesis.
// Stage 5 will promote Ask to its own top-level tab and remove the toggle.

const DEFAULT_FAV =
  "data:image/svg+xml;utf8," +
  encodeURIComponent(
    `<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 40 40'>
       <defs>
         <linearGradient id='g' x1='0' x2='1' y1='0' y2='1'>
           <stop offset='0' stop-color='#8b5cf6'/>
           <stop offset='1' stop-color='#06b6d4'/>
         </linearGradient>
       </defs>
       <rect width='40' height='40' rx='9' fill='url(#g)'/>
       <text x='50%' y='56%' font-size='20' text-anchor='middle'
             fill='white' font-family='-apple-system,Segoe UI,sans-serif'
             font-weight='700' dominant-baseline='middle'>🔖</text>
     </svg>`
  );

const state = {
  all: [],          // list of bookmarks (compact)
  filtered: [],     // current filtered+scored view
  selected: 0,      // index into `filtered`
  tab: "search",    // top-level tab id (managed by Tabs registry)
  fuzzy: false,     // substring/word search (default) vs fzf-style fuzzy
  currentId: null,  // bookmark id open in drawer
  detail: null,     // full detail payload
  schema: {},       // { sourceSlug: [FieldSpec, ...] } — from /api/schema
  // Per-tab advanced filter state. Each scope has its own copy so the
  // Search / Photos / Videos / Documents forms don't bleed into each other.
  // Initialised below by `_initAdvScopes()` once helpers are defined.
  advByScope: {},
};

const ADV_SCOPES = ["search", "photo", "video", "document"];

// Which `kind` values are relevant per scope — used to filter the Top-N
// field combo so e.g. the Videos tab doesn't suggest GitHub-only fields.
const SCOPE_KINDS = {
  search:   null,                                // null = all kinds allowed
  photo:    new Set(["photo", "file"]),
  video:    new Set(["video", "channel"]),
  document: new Set(["document"]),
};

const ADV_STORAGE_KEY = "booki.advSearch.v2";
const ADV_STORAGE_KEY_V1 = "booki.advSearch.v1";

function makeDefaultAdv() {
  return {
    sources: new Set(),
    impMin: null,
    impMax: null,
    hasSummary: false,
    hasNotes: false,
    includeRemoved: true,
    top: { field: "", direction: "top", count: 10 },
    _open: false,
  };
}

for (const s of ADV_SCOPES) state.advByScope[s] = makeDefaultAdv();

// Populated at boot from /api/kinds, which aggregates every plugin's
// kind_specs(). Adding a new kind is a plugin-side change — no edit here.
const KIND_GLYPH = {};
const KIND_LABEL = {};

async function loadKinds() {
  try {
    const r = await fetch("/api/kinds");
    if (!r.ok) return;
    const data = await r.json();
    for (const [slug, spec] of Object.entries(data || {})) {
      if (spec.glyph) KIND_GLYPH[slug] = spec.glyph;
      if (spec.label) KIND_LABEL[slug] = spec.label;
    }
  } catch { /* aggregator missing → fallback "·" via the lookup default */ }
}

// ─── DOM refs ──────────────────────────────────────────────────────

const $ = (id) => document.getElementById(id);
const els = {
  results: $("results"),
  empty: $("empty"),
  count: $("countLine"),
  findBox: $("findBox"),
  askBox: $("askBox"),
  findInput: $("findInput"),
  askInput: $("askInput"),
  useLlm: $("useLlm"),
  askResult: $("askResult"),
  askStatus: $("askStatus"),
  askAnswer: $("askAnswer"),
  askSources: $("askSources"),
  drawer: $("drawer"),
  drawerClose: $("drawerClose"),
  detailFav: $("detailFav"),
  detailTitle: $("detailTitle"),
  detailUrl: $("detailUrl"),
  detailOpen: $("detailOpen"),
  detailEdit: $("detailEdit"),
  detailDlVideo: $("detailDlVideo"),
  detailDlAudio: $("detailDlAudio"),
  detailImp: $("detailImp"),
  detailStatus: $("detailStatus"),
  detailPath: $("detailPath"),
  detailSummary: $("detailSummary"),
  detailNotes: $("detailNotes"),
  detailTags: $("detailTags"),
  detailKeywords: $("detailKeywords"),
  detailSource: $("detailSource"),
  detailBookmarked: $("detailBookmarked"),
  advSearchHost: $("advSearchHost"),
  detailLastsync: $("detailLastsync"),
  detailLastenriched: $("detailLastenriched"),
  detailArchive: $("detailArchive"),
  detailFile: $("detailFile"),
  secSummary: $("secSummary"),
  secNotes: $("secNotes"),
  secTags: $("secTags"),
  secKeywords: $("secKeywords"),
  secExtras: $("secExtras"),
  detailExtras: $("detailExtras"),
  editModal: $("editModal"),
  editClose: $("editClose"),
  editCancel: $("editCancel"),
  editForm: $("editForm"),
  editTitle: $("editTitle"),
  editImp: $("editImp"),
  editImpVal: $("editImpVal"),
  editTags: $("editTags"),
  editSummary: $("editSummary"),
  editNotes: $("editNotes"),
  editKeywords: $("editKeywords"),
  editStatus: $("editStatus"),
  toast: $("toast"),
  fuzzyToggle: $("fuzzyToggle"),
};

// ─── Utilities ─────────────────────────────────────────────────────

function domain(url) {
  try { return new URL(url).hostname; } catch { return ""; }
}
function faviconUrl(url) {
  const d = domain(url);
  if (!d) return DEFAULT_FAV;
  return `https://www.google.com/s2/favicons?sz=64&domain=${encodeURIComponent(d)}`;
}
function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}
function showToast(msg, ms = 1800) {
  els.toast.textContent = msg;
  els.toast.classList.remove("hidden");
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => els.toast.classList.add("hidden"), ms);
}

// ─── Substring / word search (default) ─────────────────────────────
// Splits the query on whitespace. Every term must appear as a substring.
// Returns {score, matches} of the union of matched char positions, or null.

function substringMatch(query, text) {
  const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (terms.length === 0) return { score: 0, matches: [] };
  const t = text.toLowerCase();
  const matched = new Set();
  let score = 0;
  for (const term of terms) {
    const idx = t.indexOf(term);
    if (idx < 0) return null;
    for (let i = 0; i < term.length; i++) matched.add(idx + i);
    const prev = idx > 0 ? t[idx - 1] : " ";
    if (!/[a-z0-9]/.test(prev)) score += 10;         // word-boundary start
    score += Math.max(0, 40 - idx);                  // earlier = better
    score += term.length;                            // longer terms worth more
  }
  return { score, matches: [...matched].sort((a, b) => a - b) };
}

// ─── Fuzzy search (fzf-ish scoring) ────────────────────────────────
// Returns {score, matches} where matches is the array of matched indices in
// `text` (used for highlighting), or null if the pattern isn't a subsequence.

function fuzzyMatch(pattern, text) {
  if (!pattern) return { score: 0, matches: [] };
  const p = pattern.toLowerCase();
  const t = text.toLowerCase();
  let pi = 0;
  let score = 0;
  let prevIdx = -2;
  const matches = [];

  for (let i = 0; i < t.length && pi < p.length; i++) {
    if (t[i] === p[pi]) {
      let bonus = 1;
      if (prevIdx === i - 1) bonus += 5;              // consecutive
      const prev = i > 0 ? t[i - 1] : " ";
      if (!/[a-z0-9]/.test(prev)) bonus += 3;         // word boundary
      if (i === 0) bonus += 2;                        // prefix
      score += bonus;
      matches.push(i);
      prevIdx = i;
      pi++;
    }
  }
  if (pi < p.length) return null;
  // Shorter hit-span is better (tighter cluster)
  score -= (matches[matches.length - 1] - matches[0]) * 0.1;
  return { score, matches };
}

function highlight(text, matches) {
  if (!matches || matches.length === 0) return escapeHtml(text);
  const set = new Set(matches);
  let out = "";
  let hlOpen = false;
  for (let i = 0; i < text.length; i++) {
    const inHl = set.has(i);
    if (inHl && !hlOpen) { out += '<mark class="hl">'; hlOpen = true; }
    if (!inHl && hlOpen) { out += '</mark>'; hlOpen = false; }
    out += escapeHtml(text[i]);
  }
  if (hlOpen) out += '</mark>';
  return out;
}

// ─── View-mode toggle (list / grid / table) ────────────────────────
//
// A small button group every results-bearing tab (Search, Photos, Videos,
// Ask) renders next to its search box. The active mode persists in
// `localStorage("booki.view.<tab-id>")`. Each tab provides:
//   - a list renderer (its own — keeps detailed per-tab affordances like
//     score chips for Search or duration overlays for Videos),
//   - whatever it wants for grid (Photos/Videos keep their image-thumb
//     grids; Search/Ask use the generic favicon-glyph grid),
//   - the generic table renderer below for table mode.

const VIEW_MODES = [
  { id: "list",  glyph: "≡", label: "List" },
  { id: "grid",  glyph: "▦", label: "Grid" },
  { id: "table", glyph: "⊞", label: "Table" },
];

function _viewModeFor(tabId, fallback = "list") {
  try {
    const v = localStorage.getItem(`booki.view.${tabId}`);
    if (v && VIEW_MODES.some(m => m.id === v)) return v;
  } catch {}
  return fallback;
}

function _saveViewMode(tabId, mode) {
  try { localStorage.setItem(`booki.view.${tabId}`, mode); } catch {}
}

function viewToggleHtml(tabId, allowed = ["list", "grid", "table"], fallback = "list") {
  const cur = _viewModeFor(tabId, fallback);
  const buttons = VIEW_MODES
    .filter(m => allowed.includes(m.id))
    .map(m =>
      `<button type="button" class="view-btn ${m.id === cur ? "active" : ""}"
               data-view="${m.id}" title="${escapeHtml(m.label)} view"
               aria-pressed="${m.id === cur ? "true" : "false"}">
        <span class="view-glyph" aria-hidden="true">${m.glyph}</span>
        <span class="view-label">${escapeHtml(m.label)}</span>
      </button>`
    ).join("");
  return `<div class="view-toggle" data-tab="${escapeHtml(tabId)}" role="group" aria-label="View mode">${buttons}</div>`;
}

function wireViewToggle(rootEl, tabId, onChange) {
  rootEl.querySelectorAll(`.view-toggle[data-tab="${tabId}"] .view-btn`).forEach(btn => {
    btn.addEventListener("click", () => {
      const mode = btn.dataset.view;
      _saveViewMode(tabId, mode);
      btn.parentElement.querySelectorAll(".view-btn").forEach(b => {
        b.classList.toggle("active", b === btn);
        b.setAttribute("aria-pressed", b === btn ? "true" : "false");
      });
      onChange?.(mode);
    });
  });
}

function _bmSourceLabel(bm) {
  return sourceLabels(bm).join(", ") || (bm.source || "—");
}

// Generic table renderer — name / source / type / importance / tags.
// Click → openDetail(id). Pass opts.onClick to override. When opts.adv has
// an active Top filter, an extra "Sort key" column shows the field value.
function renderItemsTable(host, items, opts = {}) {
  const onClick = opts.onClick || ((bm) => openDetail(bm.id));
  const adv = opts.adv;
  const showTop = advHasTop(adv);
  const topLabel = showTop ? _topFieldLabel(adv.top.field) : "";
  host.classList.add("items-host");
  if (!items.length) { host.innerHTML = ""; return; }
  const rows = items.map(bm => {
    const kind = bm.kind || "bookmark";
    const imp = bm.importance > 0 ? `★${bm.importance}` : "";
    const topCell = showTop
      ? `<td class="col-top">${escapeHtml(_formatTopValue(topFieldRaw(bm, adv.top.field), adv.top.field))}</td>`
      : "";
    return `<tr data-id="${escapeHtml(bm.id)}">
      <td class="col-glyph">${KIND_GLYPH[kind] || "🔖"}</td>
      <td class="col-name">
        <div class="t-name">${escapeHtml(bm.title || bm.url || "(untitled)")}</div>
        <div class="t-url">${escapeHtml(bm.url || "")}</div>
      </td>
      <td class="col-source">${escapeHtml(_bmSourceLabel(bm))}</td>
      <td class="col-kind">${escapeHtml(kind)}</td>
      <td class="col-imp">${imp}</td>
      ${topCell}
    </tr>`;
  }).join("");
  const topHead = showTop ? `<th class="col-top-head">${escapeHtml(topLabel)}</th>` : "";
  host.innerHTML = `
    <table class="items-table">
      <thead>
        <tr><th></th><th>Name</th><th>Source</th><th>Type</th><th>★</th>${topHead}</tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;
  host.querySelectorAll("tbody tr").forEach(tr => {
    tr.addEventListener("click", () => {
      const bm = items.find(b => b.id === tr.dataset.id);
      if (bm) onClick(bm);
    });
  });
}

// Generic favicon/glyph grid for tabs that don't have a specialised
// thumbnail (Search, Ask). Photos/Videos keep their richer grids.
function renderItemsGrid(host, items, opts = {}) {
  const onClick = opts.onClick || ((bm) => openDetail(bm.id));
  const adv = opts.adv;
  host.classList.add("items-host");
  if (!items.length) { host.innerHTML = ""; return; }
  const tiles = items.map(bm => {
    const kind = bm.kind || "bookmark";
    const glyph = KIND_GLYPH[kind] || "🔖";
    const tags = (bm.tags || []).slice(0, 3)
      .map(t => `<span class="g-tag">${escapeHtml(t)}</span>`).join("");
    const imp = bm.importance > 0 ? `<span class="g-imp">★${bm.importance}</span>` : "";
    const fav = faviconUrl(bm.url);
    const topChip = topFieldChipHtml(bm, adv);
    return `<li class="g-tile" data-id="${escapeHtml(bm.id)}" tabindex="0">
      <div class="g-thumb">
        <img class="g-fav" src="${escapeHtml(fav)}" alt="" loading="lazy"
             onerror="this.onerror=null;this.src='${DEFAULT_FAV}';">
        <span class="g-glyph" title="${escapeHtml(kind)}">${glyph}</span>
        ${imp}
      </div>
      <div class="g-meta">
        <div class="g-title" title="${escapeHtml(bm.title || "")}">${escapeHtml(bm.title || "(untitled)")}</div>
        <div class="g-source">${escapeHtml(_bmSourceLabel(bm))}</div>
        <div class="g-tags">${tags}</div>
        ${topChip ? `<div class="g-top">${topChip}</div>` : ""}
      </div>
    </li>`;
  }).join("");
  host.innerHTML = `<ul class="items-grid">${tiles}</ul>`;
  host.querySelectorAll(".g-tile").forEach(tile => {
    const bm = items.find(b => b.id === tile.dataset.id);
    tile.addEventListener("click", () => bm && onClick(bm));
    tile.addEventListener("keydown", (e) => {
      if ((e.key === "Enter" || e.key === " ") && bm) {
        e.preventDefault();
        if (bm.url && e.key === "Enter") window.open(bm.url, "_blank", "noopener");
        else onClick(bm);
      }
    });
  });
}

// Compact list renderer for tabs that don't have a custom list (Photos,
// Videos use this when the user picks "list" mode). Search keeps its
// own rich rows so highlights / score chips survive.
function renderItemsList(host, items, opts = {}) {
  const onClick = opts.onClick || ((bm) => openDetail(bm.id));
  const adv = opts.adv;
  host.classList.add("items-host");
  if (!items.length) { host.innerHTML = ""; return; }
  const rows = items.map(bm => {
    const kind = bm.kind || "bookmark";
    const glyph = KIND_GLYPH[kind] || "🔖";
    const tags = (bm.tags || []).slice(0, 4)
      .map(t => `<span class="tag">${escapeHtml(t)}</span>`).join("");
    const imp = bm.importance > 0 ? `<span class="star">★${bm.importance}</span>` : "";
    const topChip = topFieldChipHtml(bm, adv);
    return `<li class="items-row" data-id="${escapeHtml(bm.id)}">
      <span class="row-glyph" title="${escapeHtml(kind)}">${glyph}</span>
      <div class="row-body">
        <div class="row-title">${escapeHtml(bm.title || bm.url || "(untitled)")}</div>
        <div class="row-url">${escapeHtml(bm.url || "")}</div>
        <div class="row-meta">
          <span class="tag src">${escapeHtml(_bmSourceLabel(bm))}</span>
          ${tags}
        </div>
      </div>
      <div class="row-right">
        ${topChip}
        ${imp}
      </div>
    </li>`;
  }).join("");
  host.innerHTML = `<ul class="items-list">${rows}</ul>`;
  host.querySelectorAll(".items-row").forEach(li => {
    li.addEventListener("click", () => {
      const bm = items.find(b => b.id === li.dataset.id);
      if (bm) onClick(bm);
    });
  });
}

// ─── Load + render list ────────────────────────────────────────────

async function loadStats() {
  try {
    const r = await fetch("/api/stats");
    if (!r.ok) return;
    renderStats(await r.json());
  } catch { /* optional */ }
}

// Stats live inside Manage > General, which is mounted lazily — `renderStats`
// caches the most-recent payload so the panel can re-render whenever the
// sub-tab is activated.
let _lastStatsPayload = null;

function renderStats(s) {
  _lastStatsPayload = s;
  const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
  set("statTotal",    (s.total ?? 0).toLocaleString());
  set("statEnriched", (s.enriched ?? 0).toLocaleString());
  set("statSources",  Object.keys(s.by_source || {}).length);
  set("statLastSync", s.last_sync || "—");
  set("statDir",      s.bookmarks_dir || "");
  const bySource = document.getElementById("statBySource");
  if (bySource) renderBars(bySource, s.by_source || {});
  const byKind = document.getElementById("statByKind");
  if (byKind) renderBars(byKind, s.by_kind || {});
}

function renderBars(ul, obj) {
  const entries = Object.entries(obj).sort((a, b) => b[1] - a[1]);
  const max = entries.reduce((m, [, n]) => Math.max(m, n), 1);
  ul.innerHTML = entries.map(([name, n]) => {
    const pct = Math.max(4, Math.round((n / max) * 100));
    return `<li><div class="bar-row">
      <div class="bar-fill" style="width:${pct}%"></div>
      <div class="bar-label"><span>${escapeHtml(name)}</span><b>${n.toLocaleString()}</b></div>
    </div></li>`;
  }).join("") || `<li class="bar-row"><div class="bar-label"><span>—</span></div></li>`;
}

async function loadSchema() {
  try {
    const r = await fetch("/api/schema");
    if (r.ok) state.schema = await r.json();
  } catch { /* schema is optional — UI degrades gracefully without it */ }
}

// Subscribers fired when state.all changes. Plugin tabs use this through
// `booki.bookmarks.onChange(cb)` to re-render without polling.
const _bookmarkChangeListeners = new Set();

async function loadBookmarks() {
  const r = await fetch("/api/bookmarks");
  if (!r.ok) throw new Error(`GET /api/bookmarks → ${r.status}`);
  state.all = await r.json();
  try {
    const enriched = state.all.filter(b => b.has_summary).length;
    els.count.textContent = `${state.all.length} bookmarks · ${enriched} enriched`;
  } catch (e) { console.error(e); }
  try { refreshAdvancedFilters(); } catch (e) { console.error(e); }
  try { applyFilter(); } catch (e) { console.error(e); }

  // Re-fire onShow on whichever tab is active so it picks up the new data
  // (matters on first boot — `Tabs.activate` may run before bookmarks
  // finish loading, so the active tab's first onShow saw an empty list).
  // Generic — works for built-in AND plugin tabs without hardcoded knowledge.
  const cur = Tabs.current && Tabs.get(Tabs.current());
  if (cur && cur._mounted) {
    try { cur.onShow?.(cur._container); } catch (e) { console.error(e); }
  }

  for (const cb of _bookmarkChangeListeners) {
    try { cb(state.all); } catch (e) { console.error(e); }
  }
}

// ─── Advanced search: predicate + sort + storage ───────────────────
//
// Each results-bearing tab (Search, Photos, Videos, Documents) owns its own
// adv state under state.advByScope[scope]. Tabs render a fresh UI instance
// via mountAdvancedSearch(host, { scope }); changes to one scope's form do
// NOT touch the others. Instances are tracked in _advInstances[] so they
// stay in sync after bookmarks reload.

function getFieldValue(b, field) {
  if (!field) return undefined;
  switch (field) {
    case "importance":      return b.importance || 0;
    case "tags_count":      return (b.tags || []).length;
    case "keywords_count":  return (b.keywords || []).length;
    case "summary_length":  return (b.summary || "").length;
    case "notes_length":    return (b.notes || "").length;
    case "title_length":    return (b.title || "").length;
    case "url_length":      return (b.url || "").length;
    case "date_bookmarked": return parseFieldValue(b.date_bookmarked);
    case "last_sync":       return parseFieldValue(b.last_sync);
    case "last_enriched":   return parseFieldValue(b.last_enriched ?? (b.extras || {}).last_enriched);
  }
  if (b.extras && Object.prototype.hasOwnProperty.call(b.extras, field)) {
    return parseFieldValue(b.extras[field]);
  }
  if (Object.prototype.hasOwnProperty.call(b, field)) return b[field];
  return undefined;
}

// Coerce a raw frontmatter value into something comparable. Strings shaped
// like "MM:SS" / "HH:MM:SS" become seconds; numeric strings become numbers;
// ISO-ish dates become unix-ms timestamps.
function parseFieldValue(v) {
  if (v == null || v === "") return null;
  if (typeof v === "number" || typeof v === "boolean") return v;
  if (typeof v === "string") {
    const s = v.trim();
    if (!s) return null;
    const dur = s.match(/^(\d+):(\d{1,2})(?::(\d{1,2}))?$/);
    if (dur) {
      const a = +dur[1], b = +dur[2], c = dur[3] ? +dur[3] : null;
      return c == null ? a * 60 + b : a * 3600 + b * 60 + c;
    }
    if (/^-?\d+(\.\d+)?$/.test(s)) return Number(s);
    const t = Date.parse(s);
    if (!Number.isNaN(t)) return t;
    return s;
  }
  return v;
}

function makeAdvPredicate(adv) {
  return (b) => {
    if (adv.sources.size) {
      const all = new Set([b.source, ...(b.sources || [])].filter(Boolean));
      let ok = false;
      for (const s of adv.sources) if (all.has(s)) { ok = true; break; }
      if (!ok) return false;
    }
    const imp = b.importance || 0;
    if (adv.impMin != null && imp < adv.impMin) return false;
    if (adv.impMax != null && imp > adv.impMax) return false;
    if (adv.hasSummary && !b.has_summary) return false;
    if (adv.hasNotes && !(b.notes && b.notes.trim().length)) return false;
    if (!adv.includeRemoved && (b.removed_from_browser || b.removed_from_source)) return false;
    // When a Top-N field is set, drop items that have no numeric value for it
    // — they're not orderable, so they don't belong in a "top N by X" view.
    const top = adv.top;
    if (top && top.field) {
      const v = getFieldValue(b, top.field);
      const n = typeof v === "number" ? v : Number(v);
      if (!Number.isFinite(n)) return false;
    }
    return true;
  };
}

// Sort + limit by the selected field. "top" → biggest first; "bottom" →
// smallest first. Items missing the field are filtered out by makeAdvPredicate
// before this runs, so we just sort the survivors.
function applyAdvSort(items, adv) {
  const top = adv?.top;
  if (!top || !top.field) return items;
  const dir = top.direction === "bottom" ? 1 : -1;
  const keyed = items.map(b => {
    const v = getFieldValue(b, top.field);
    const n = typeof v === "number" ? v : Number(v);
    return { b, k: Number.isFinite(n) ? n : null };
  });
  keyed.sort((x, y) => {
    if (x.k == null && y.k == null) return 0;
    if (x.k == null) return 1;
    if (y.k == null) return -1;
    return (x.k - y.k) * dir;
  });
  const sorted = keyed.map(x => x.b);
  const count = Number(top.count);
  return Number.isFinite(count) && count > 0 ? sorted.slice(0, count) : sorted;
}

// True when a Top-N field is set — callers replace their default sort with
// applyAdvSort and drop the usual cap.
function advHasTop(adv) { return !!(adv?.top && adv.top.field); }

// Raw display value for the Top field on this bookmark — preserves the
// original on-disk string so dates stay "2026-05-03" rather than a unix-ms
// timestamp. Computed fields (tags_count etc.) get derived inline.
function topFieldRaw(bm, field) {
  if (!field) return undefined;
  switch (field) {
    case "tags_count":     return (bm.tags || []).length;
    case "keywords_count": return (bm.keywords || []).length;
    case "summary_length": return (bm.summary || "").length;
    case "notes_length":   return (bm.notes || "").length;
    case "title_length":   return (bm.title || "").length;
    case "url_length":     return (bm.url || "").length;
  }
  if (Object.prototype.hasOwnProperty.call(bm, field)) return bm[field];
  if (bm.extras && Object.prototype.hasOwnProperty.call(bm.extras, field)) {
    return bm.extras[field];
  }
  return undefined;
}

const TOP_FIELD_LABELS = {
  importance: "Importance",
  tags_count: "Tags",
  keywords_count: "Keywords",
  summary_length: "Summary",
  notes_length: "Notes",
  title_length: "Title",
  url_length: "URL",
  date_bookmarked: "Bookmarked",
  last_sync: "Last sync",
  last_enriched: "Last enriched",
};

function _topFieldLabel(field) {
  if (TOP_FIELD_LABELS[field]) return TOP_FIELD_LABELS[field];
  for (const key of Object.keys(state.schema || {})) {
    for (const spec of state.schema[key] || []) {
      if (spec && spec.name === field) return spec.label || field;
    }
  }
  return field;
}

function _formatTopValue(v, field) {
  if (v == null || v === "") return "—";
  if (Array.isArray(v)) return `${v.length}`;
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "number") {
    if (field === "file_size") {
      if (v >= 1e9) return (v / 1e9).toFixed(1) + " GB";
      if (v >= 1e6) return (v / 1e6).toFixed(1) + " MB";
      if (v >= 1e3) return (v / 1e3).toFixed(1) + " KB";
      return v + " B";
    }
    if (field === "github_size_kb") {
      if (v >= 1e6) return (v / 1e6).toFixed(1) + " GB";
      if (v >= 1e3) return (v / 1e3).toFixed(1) + " MB";
      return v + " KB";
    }
    return v.toLocaleString();
  }
  return String(v);
}

// Returns an HTML chip showing the Top-field value for `bm`, or "" if the
// scope's Top filter is inactive. Used by row/list/table/tile renderers.
function topFieldChipHtml(bm, adv) {
  if (!advHasTop(adv)) return "";
  const field = adv.top.field;
  const raw = topFieldRaw(bm, field);
  const display = _formatTopValue(raw, field);
  const label = _topFieldLabel(field);
  return `<span class="score-chip top" title="Sorted by ${escapeHtml(field)}">`
       + `📊 ${escapeHtml(label)}: ${escapeHtml(display)}</span>`;
}

function advActiveCount(adv) {
  let n = adv.sources.size;
  if (adv.impMin != null) n++;
  if (adv.impMax != null) n++;
  if (adv.hasSummary) n++;
  if (adv.hasNotes) n++;
  if (!adv.includeRemoved) n++;
  if (adv.top && adv.top.field) n++;
  return n;
}

function _hydrateAdvFromJson(adv, j) {
  if (!j) return;
  adv.sources = new Set(j.sources || []);
  adv.impMin = j.impMin ?? null;
  adv.impMax = j.impMax ?? null;
  adv.hasSummary = !!j.hasSummary;
  adv.hasNotes = !!j.hasNotes;
  adv.includeRemoved = j.includeRemoved !== false;
  if (j.top && typeof j.top === "object") {
    adv.top = {
      field: String(j.top.field || ""),
      direction: j.top.direction === "bottom" ? "bottom" : "top",
      count: Number.isFinite(+j.top.count) ? +j.top.count : 10,
    };
  }
  adv._open = !!j.open;
}

function _serializeAdv(adv) {
  return {
    sources: [...adv.sources],
    impMin: adv.impMin,
    impMax: adv.impMax,
    hasSummary: adv.hasSummary,
    hasNotes: adv.hasNotes,
    includeRemoved: adv.includeRemoved,
    top: { ...adv.top },
    open: !!adv._open,
  };
}

function loadAdvFromStorage() {
  try {
    const raw = localStorage.getItem(ADV_STORAGE_KEY);
    if (raw) {
      const j = JSON.parse(raw);
      for (const s of ADV_SCOPES) {
        if (j[s]) _hydrateAdvFromJson(state.advByScope[s], j[s]);
      }
      return;
    }
    // First-run migration from v1 (single shared state) — apply the same
    // saved filters to every scope so users don't lose their selection.
    const old = localStorage.getItem(ADV_STORAGE_KEY_V1);
    if (old) {
      const j = JSON.parse(old);
      for (const s of ADV_SCOPES) _hydrateAdvFromJson(state.advByScope[s], j);
    }
  } catch { /* ignore corrupt storage */ }
}

function saveAdvToStorage() {
  try {
    const out = {};
    for (const s of ADV_SCOPES) out[s] = _serializeAdv(state.advByScope[s]);
    localStorage.setItem(ADV_STORAGE_KEY, JSON.stringify(out));
  } catch { /* quota / private mode — fail silently */ }
}

function clearAdvFilters(scope) {
  state.advByScope[scope] = makeDefaultAdv();
  saveAdvToStorage();
  notifyAdvChange(scope);
}

// ─── Shared chip-picker / set helpers ──────────────────────────────

function toggleSet(set, val) {
  if (set.has(val)) set.delete(val); else set.add(val);
}

function renderChipPicker(host, entries, selectedSet, onToggle) {
  if (!host) return;
  host.innerHTML = "";
  if (!entries.length) {
    host.innerHTML = `<span class="hint-text">(none)</span>`;
    return;
  }
  const frag = document.createDocumentFragment();
  for (const [name, count] of entries) {
    const btn = document.createElement("button");
    btn.type = "button";
    const on = selectedSet.has(name);
    btn.className = "adv-chip" + (on ? " on" : "");
    btn.dataset.value = name;
    btn.setAttribute("aria-pressed", on ? "true" : "false");
    const label = (name === "bookmark" || name === "photo" || name === "video"
                || name === "document" || name === "file" || name === "channel"
                || name === "post")
      ? (KIND_LABEL[name] || name) : name;
    btn.innerHTML = `<span class="adv-chip-label">${escapeHtml(label)}</span>`
                  + ` <span class="adv-chip-count">${count}</span>`;
    btn.addEventListener("click", () => onToggle(name));
    frag.appendChild(btn);
  }
  host.appendChild(frag);
}

// ─── Advanced UI: per-tab mounting ─────────────────────────────────
//
// Each instance binds to one entry of state.advByScope. Updating that scope
// fires notifyAdvChange(scope) which only re-renders the matching tab and
// any same-scope listeners — other tabs' forms keep their own state.

const _advInstances = [];          // [{el, refresh, onApply}, ...]
const _advChangeListeners = new Set();

// Only numeric/orderable fields qualify for Top-N — the comparison is
// strictly numeric (durations and dates parse to seconds / unix-ms).
const NUMERIC_FORMATS = new Set(["number", "duration", "date"]);

function buildFieldOptions(scope) {
  const core = [
    { name: "importance",      label: "Importance" },
    { name: "tags_count",      label: "Tag count" },
    { name: "keywords_count",  label: "Keyword count" },
    { name: "summary_length",  label: "Summary length" },
    { name: "notes_length",    label: "Notes length" },
    { name: "title_length",    label: "Title length" },
    { name: "url_length",      label: "URL length" },
    { name: "date_bookmarked", label: "Date bookmarked" },
    { name: "last_sync",       label: "Last sync" },
    { name: "last_enriched",   label: "Last enriched" },
  ];
  const allowedKinds = SCOPE_KINDS[scope || "search"] || null;  // null → all
  const seen = new Set(core.map(c => c.name));
  const extras = [];
  for (const key of Object.keys(state.schema || {})) {
    for (const spec of state.schema[key] || []) {
      if (!spec || !spec.name || seen.has(spec.name)) continue;
      if (!NUMERIC_FORMATS.has(spec.format)) continue;
      // If the spec restricts itself to certain kinds, only include it when
      // at least one of those kinds is relevant to this tab.
      if (allowedKinds && Array.isArray(spec.kinds) && spec.kinds.length) {
        if (!spec.kinds.some(k => allowedKinds.has(k))) continue;
      }
      seen.add(spec.name);
      extras.push({ name: spec.name, label: spec.label || spec.name, group: spec.group });
    }
  }
  extras.sort((a, b) => (a.group || "").localeCompare(b.group || "")
                    || a.label.localeCompare(b.label));
  return [...core, ...extras];
}

function mountAdvancedSearch(host, opts = {}) {
  if (!host) return null;
  const scope = opts.scope || "search";
  if (!state.advByScope[scope]) state.advByScope[scope] = makeDefaultAdv();
  const adv = state.advByScope[scope];
  const fieldsListId = `advTopFields_${_advInstances.length}`;
  host.innerHTML = `
    <details class="adv-search">
      <summary>
        <span class="adv-caret" aria-hidden="true">▸</span>
        <span class="adv-title">Advanced</span>
        <span class="adv-count hidden" data-role="count" title="Active filters">0</span>
        <button type="button" class="adv-clear hidden" data-role="clear"
                title="Clear all advanced filters">✕ Clear</button>
      </summary>
      <div class="adv-grid">
        <div class="adv-group adv-misc">
          <h4>Sources <span class="hint-text">(any)</span></h4>
          <div class="chip-picker" data-role="sources"></div>
        </div>
        <div class="adv-group adv-misc">
          <h4>Top <span class="hint-text">(field · direction · count)</span></h4>
          <div class="adv-row adv-top-row">
            <input type="text" class="adv-top-field" data-role="topField"
                   list="${fieldsListId}" placeholder="field…" autocomplete="off"
                   spellcheck="false">
            <datalist id="${fieldsListId}" data-role="topFieldList"></datalist>
            <div class="adv-top-dirs" role="group" aria-label="Direction" data-role="topDirs">
              <button type="button" class="adv-dir-btn" data-dir="top"
                      title="largest values first">▲ Top</button>
              <button type="button" class="adv-dir-btn" data-dir="bottom"
                      title="smallest values first">▼ Bottom</button>
            </div>
            <input type="number" class="adv-top-count" data-role="topCount" min="1" step="1" placeholder="10">
            <button type="button" class="adv-top-clear" data-role="topClear"
                    title="Clear top filter">✕</button>
          </div>
        </div>
        <div class="adv-group adv-misc">
          <h4>Other</h4>
          <div class="adv-row">
            <label>Importance ≥
              <input type="number" data-role="impMin" min="0" max="10" step="1" placeholder="0">
            </label>
            <label>≤
              <input type="number" data-role="impMax" min="0" max="10" step="1" placeholder="10">
            </label>
            <label class="toggle"><input type="checkbox" data-role="hasSummary"> has summary</label>
            <label class="toggle"><input type="checkbox" data-role="hasNotes"> has notes</label>
            <label class="toggle"><input type="checkbox" data-role="includeRemoved" checked> include removed</label>
          </div>
        </div>
      </div>
    </details>`;
  const root = host.querySelector(".adv-search");
  if (adv._open) root.setAttribute("open", "");
  // Fresh getter — `clearAdvFilters` replaces the whole object, so handlers
  // must always read the current scope state instead of a captured ref.
  const get = () => state.advByScope[scope];
  root.addEventListener("toggle", () => {
    get()._open = root.open;
    saveAdvToStorage();
  });

  const $$ = (role) => root.querySelector(`[data-role="${role}"]`);
  const apply = () => { saveAdvToStorage(); notifyAdvChange(scope); };

  $$("clear").addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    clearAdvFilters(scope);
  });

  $$("impMin").addEventListener("input", (e) => {
    const v = e.target.value === "" ? null : Number(e.target.value);
    get().impMin = Number.isFinite(v) ? v : null;
    apply();
  });
  $$("impMax").addEventListener("input", (e) => {
    const v = e.target.value === "" ? null : Number(e.target.value);
    get().impMax = Number.isFinite(v) ? v : null;
    apply();
  });
  $$("hasSummary").addEventListener("change", (e) => {
    get().hasSummary = e.target.checked; apply();
  });
  $$("hasNotes").addEventListener("change", (e) => {
    get().hasNotes = e.target.checked; apply();
  });
  $$("includeRemoved").addEventListener("change", (e) => {
    get().includeRemoved = e.target.checked; apply();
  });

  // Top filter wiring.
  const topField = $$("topField");
  const topCount = $$("topCount");
  const topDirs  = $$("topDirs");
  topField.addEventListener("input", (e) => {
    get().top.field = e.target.value.trim();
    apply();
  });
  topField.addEventListener("change", (e) => {
    get().top.field = e.target.value.trim();
    apply();
  });
  topCount.addEventListener("input", (e) => {
    const v = Number(e.target.value);
    get().top.count = Number.isFinite(v) && v > 0 ? Math.floor(v) : 0;
    apply();
  });
  topDirs.querySelectorAll(".adv-dir-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      get().top.direction = btn.dataset.dir;
      apply();
    });
  });
  $$("topClear").addEventListener("click", () => {
    get().top = { field: "", direction: "top", count: 10 };
    apply();
  });

  const refresh = () => {
    // Re-bind to whatever's currently in state — clearAdvFilters() replaces
    // the whole object, so an older closure could go stale otherwise.
    const cur = state.advByScope[scope];

    // Counts derived from the current bookmark snapshot. For non-search
    // scopes, restrict the count pool to items relevant to that tab so the
    // chips reflect what's actually filterable here.
    const allowed = SCOPE_KINDS[scope];
    const pool = allowed
      ? state.all.filter(b => allowed.has(b.kind || "bookmark"))
      : state.all;
    const sourceCounts = {};
    for (const b of pool) {
      [b.source, ...(b.sources || [])].filter(Boolean).forEach(s => {
        sourceCounts[s] = (sourceCounts[s] || 0) + 1;
      });
    }
    const sortPairs = (m) => Object.entries(m)
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));

    renderChipPicker($$("sources"), sortPairs(sourceCounts), cur.sources,
      (n) => { toggleSet(cur.sources, n); apply(); });

    // Datalist for the top-field combobox — narrowed to fields that make
    // sense in this scope (e.g. Videos hides github_* / photo_*).
    const dl = $$("topFieldList");
    dl.innerHTML = "";
    for (const f of buildFieldOptions(scope)) {
      const opt = document.createElement("option");
      opt.value = f.name;
      opt.label = f.label + (f.group ? ` · ${f.group}` : "");
      dl.appendChild(opt);
    }

    // Sync misc inputs.
    $$("impMin").value = cur.impMin ?? "";
    $$("impMax").value = cur.impMax ?? "";
    $$("hasSummary").checked = cur.hasSummary;
    $$("hasNotes").checked = cur.hasNotes;
    $$("includeRemoved").checked = cur.includeRemoved;
    topField.value = cur.top.field || "";
    topCount.value = (cur.top.count ?? 10);
    topDirs.querySelectorAll(".adv-dir-btn").forEach(btn => {
      btn.classList.toggle("on", btn.dataset.dir === (cur.top.direction || "top"));
    });

    const n = advActiveCount(cur);
    const countEl = $$("count");
    const clearEl = $$("clear");
    countEl.textContent = n;
    countEl.classList.toggle("hidden", n === 0);
    clearEl.classList.toggle("hidden", n === 0);
  };

  const inst = { el: root, host, refresh, scope };
  _advInstances.push(inst);
  refresh();
  return inst;
}

function refreshAdvancedFilters(scope) {
  // Refresh instances. If `scope` is omitted, refresh all (used after the
  // initial bookmarks load to populate freshly-mounted UIs). Drops detached
  // instances along the way so the registry doesn't grow unbounded.
  for (let i = _advInstances.length - 1; i >= 0; i--) {
    const inst = _advInstances[i];
    if (!document.body.contains(inst.host)) {
      _advInstances.splice(i, 1);
      continue;
    }
    if (scope && inst.scope !== scope) continue;
    try { inst.refresh(); } catch (e) { console.error(e); }
  }
}

function notifyAdvChange(scope) {
  refreshAdvancedFilters(scope);
  if (scope === "search") { try { applyFilter(); } catch (e) { console.error(e); } }
  if (scope === "photo")  { try { renderPhotoGrid(); } catch {} }
  if (scope === "video")  { try { renderVideoGrid(); } catch {} }
  for (const cb of _advChangeListeners) {
    try { cb(state.advByScope[scope], scope); } catch (e) { console.error(e); }
  }
}

function applyFilter() {
  const q = els.findInput.value.trim();
  const adv = state.advByScope.search;
  const matchAdv = makeAdvPredicate(adv);
  const pool = state.all.filter(matchAdv);
  const topSorts = advHasTop(adv);

  if (!q) {
    let rows = pool.map(b => ({ bm: b, score: b.importance * 2, titleMatches: [], urlMatches: [] }));
    if (topSorts) {
      const sorted = applyAdvSort(pool, adv);
      rows = sorted.map(b => ({ bm: b, score: 0, titleMatches: [], urlMatches: [] }));
    } else {
      rows.sort((a, b) => b.score - a.score);
    }
    state.filtered = rows;
  } else {
    const match = state.fuzzy ? fuzzyMatch : substringMatch;
    const out = [];
    for (const b of pool) {
      const titleText = b.title || "";
      const urlText = b.url || "";
      const srcs = sourceLabels(b).join(" ");
      const extra = `${(b.tags || []).join(" ")} ${b.browser_path || ""} ${srcs}`;

      const tm = match(q, titleText);
      const um = match(q, urlText);
      const em = match(q, extra);

      const parts = [tm, um, em].filter(Boolean);
      if (parts.length === 0) continue;

      // Title hits count more; then URL; then tags/path.
      const score =
        (tm ? tm.score * 3 : 0) +
        (um ? um.score * 1.2 : 0) +
        (em ? em.score * 0.8 : 0) +
        (b.importance || 0) * 0.5;

      out.push({
        bm: b,
        score,
        fuzzyScore: score,
        titleMatches: tm ? tm.matches : [],
        urlMatches: um ? um.matches : [],
      });
    }
    if (topSorts) {
      // When Top sort is active, replace fuzzy/substring score order with the
      // field-based order (already limited to count) — keep highlight matches.
      const byId = new Map(out.map(r => [r.bm.id, r]));
      const ordered = applyAdvSort(out.map(r => r.bm), adv);
      state.filtered = ordered.map(b => byId.get(b.id)).filter(Boolean);
    } else {
      out.sort((a, b) => b.score - a.score);
      state.filtered = out.slice(0, 200);
    }
  }
  state.selected = 0;
  renderResults();
}

function renderResults() {
  // Per-tab count line: matches the "X of Y bookmarks" pattern used by the
  // Photos / Videos tabs, so the user can see how many results their query
  // and Advanced filter are surfacing.
  const sc = document.getElementById("searchCount");
  if (sc) {
    const total = state.all.length;
    const shown = state.filtered.length;
    const q = (els.findInput?.value || "").trim();
    const adv = state.advByScope.search;
    const filtered = q || advActiveCount(adv) > 0;
    const word = total === 1 ? "bookmark" : "bookmarks";
    sc.textContent = filtered ? `${shown} of ${total} ${word}` : `${total} ${word}`;
  }

  if (state.filtered.length === 0) {
    els.results.innerHTML = "";
    els.results.className = "results";
    els.empty.classList.remove("hidden");
    return;
  }
  els.empty.classList.add("hidden");

  const mode = _viewModeFor("search", "list");
  const items = state.filtered.map(r => r.bm);
  const adv = state.advByScope.search;
  els.results.className = "results";
  if (mode === "grid")  { renderItemsGrid(els.results, items, { adv }); return; }
  if (mode === "table") { renderItemsTable(els.results, items, { adv }); return; }

  // mode === "list" — keep rich rows with score chips + match highlights.
  const frag = document.createDocumentFragment();
  state.filtered.forEach((row, i) => {
    frag.appendChild(renderRow(row, i === state.selected, adv));
  });
  els.results.replaceChildren(frag);
}

function renderRow(row, selected, adv) {
  const { bm, titleMatches, urlMatches, fuzzyScore, vectorScore } = row;
  const li = document.createElement("li");
  const removed = bm.removed_from_browser || bm.removed_from_source;
  li.className = "bm" + (selected ? " selected" : "") + (removed ? " removed" : "");
  li.dataset.id = bm.id;

  const favWrap = document.createElement("div");
  favWrap.className = "bm-fav-wrap";
  const fav = document.createElement("img");
  fav.className = "bm-fav";
  fav.alt = "";
  fav.loading = "lazy";
  fav.src = faviconUrl(bm.url);
  fav.onerror = () => { fav.onerror = null; fav.src = DEFAULT_FAV; };
  favWrap.appendChild(fav);
  const kind = bm.kind || "bookmark";
  if (kind !== "bookmark") {
    const badge = document.createElement("span");
    badge.className = "bm-kind";
    badge.textContent = KIND_GLYPH[kind] || "·";
    badge.title = kind;
    favWrap.appendChild(badge);
  }
  li.appendChild(favWrap);

  const body = document.createElement("div");
  body.className = "bm-body";
  body.innerHTML = `
    <div class="bm-title">${highlight(bm.title || bm.url, titleMatches)}</div>
    <div class="bm-url">${highlight(bm.url, urlMatches)}</div>
    ${renderMetaRow(bm)}
  `;
  li.appendChild(body);

  const right = document.createElement("div");
  right.className = "bm-right";
  const scoreChips = [];
  // Show the sort-key value first so the user can see why this row is here.
  const topChip = topFieldChipHtml(bm, adv);
  if (topChip) scoreChips.push(topChip);
  if (typeof vectorScore === "number") {
    const pct = Math.round(vectorScore * 100);
    scoreChips.push(`<span class="score-chip vec" title="Vector similarity (cosine)">🧭 ${pct}%</span>`);
  }
  if (typeof fuzzyScore === "number" && fuzzyScore > 0) {
    scoreChips.push(`<span class="score-chip fuzz" title="Fuzzy match score">⚡ ${fuzzyScore.toFixed(0)}</span>`);
  }
  if (bm.importance > 0) {
    scoreChips.push(`<span class="star">★${bm.importance}</span>`);
  }
  right.innerHTML = scoreChips.join("");
  li.appendChild(right);

  li.addEventListener("click", () => openDetail(bm.id));
  return li;
}

function renderMetaRow(bm) {
  const tags = (bm.tags || []).slice(0, 4).map(t => `<span class="tag">${escapeHtml(t)}</span>`).join("");
  const labels = sourceLabels(bm);
  const source = labels
    .map(s => `<span class="tag src">${escapeHtml(s)}</span>`)
    .join("");
  const enriched = bm.has_summary ? `<span class="tag">✨ summary</span>` : "";
  return `<div class="bm-meta">${source}${tags}${enriched}</div>`;
}

function sourceLabels(bm) {
  // Union of `sources:` slugs and legacy `source:` label, dedup'd
  // case-insensitively. Keeps first-seen casing for display.
  const out = [];
  const seen = new Set();
  const add = (v) => {
    const s = String(v || "").trim();
    if (!s) return;
    const k = s.toLowerCase();
    if (seen.has(k)) return;
    seen.add(k);
    out.push(s);
  };
  (bm.sources || []).forEach(add);
  add(bm.source);
  return out;
}

// ─── Keyboard navigation ───────────────────────────────────────────

function moveSelection(delta) {
  if (state.filtered.length === 0) return;
  state.selected = Math.max(0, Math.min(state.filtered.length - 1, state.selected + delta));
  renderResults();
  const el = els.results.children[state.selected];
  if (el) el.scrollIntoView({ block: "nearest" });
  const row = state.filtered[state.selected];
  if (row) previewDetail(row.bm.id);
}

// Coalesce rapid arrow-key nav into one fetch per ~120ms.
let _previewTimer = null;
function previewDetail(id) {
  clearTimeout(_previewTimer);
  _previewTimer = setTimeout(() => openDetail(id), 120);
}

window.addEventListener("keydown", (e) => {
  const inTyping = ["INPUT", "TEXTAREA"].includes(document.activeElement?.tagName);

  if (e.key === "/" && !inTyping) {
    e.preventDefault();
    focusSearch();
    return;
  }
  // Number-key tab jump (1–9): activate the Nth tab in the tab bar order.
  // Skipped when typing in an input or when modifier keys are held.
  if (!inTyping && !e.metaKey && !e.ctrlKey && !e.altKey
      && /^[1-9]$/.test(e.key)) {
    const idx = parseInt(e.key, 10) - 1;
    const tabs = Tabs.all();
    if (idx < tabs.length) {
      e.preventDefault();
      Tabs.activate(tabs[idx].id);
      return;
    }
  }
  if (e.key === "Escape") {
    if (!els.editModal.classList.contains("hidden")) closeEdit();
    else if (!els.drawer.classList.contains("hidden")) closeDrawer();
    else if (document.activeElement === els.findInput || document.activeElement === els.askInput) {
      document.activeElement.blur();
    }
    return;
  }
  // List nav only when on Search tab and focus is body or the find input.
  if (state.tab === "search"
      && (!inTyping || document.activeElement === els.findInput)) {
    if (e.key === "ArrowDown") { e.preventDefault(); moveSelection(1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); moveSelection(-1); }
    else if (e.key === "Enter" && document.activeElement === els.findInput) {
      const row = state.filtered[state.selected];
      if (row && row.bm.url) window.open(row.bm.url, "_blank", "noopener");
    } else if (e.key === "e" && !inTyping) {
      const row = state.filtered[state.selected];
      if (row) openDetail(row.bm.id).then(openEdit);
    }
  }
});

function focusSearch() {
  if (state.tab !== "search") Tabs.activate("search");
  els.findInput.focus();
  els.findInput.select();
}

// ─── Detail drawer ─────────────────────────────────────────────────

async function openDetail(id) {
  state.currentId = id;
  try {
    const r = await fetch(`/api/bookmarks/${id}`);
    if (!r.ok) throw new Error(`GET → ${r.status}`);
    state.detail = await r.json();
  } catch (err) {
    showToast(`Could not load bookmark: ${err.message}`);
    return;
  }
  const d = state.detail;

  els.detailFav.src = faviconUrl(d.url);
  els.detailFav.onerror = () => { els.detailFav.onerror = null; els.detailFav.src = DEFAULT_FAV; };

  els.detailTitle.textContent = d.title || d.url;
  els.detailUrl.textContent = d.url;
  els.detailUrl.href = d.url;
  els.detailOpen.href = d.url;

  els.detailImp.textContent = `★${d.importance}`;
  els.detailImp.className = "chip imp";
  els.detailStatus.textContent = d.status || "unchecked";
  els.detailStatus.className = "chip";
  els.detailPath.textContent = d.browser_path || "—";
  els.detailPath.className = "chip";

  renderExtras(d);

  toggleSection(els.secSummary, d.summary, () => els.detailSummary.textContent = d.summary);
  toggleSection(els.secNotes,   d.notes,   () => els.detailNotes.textContent = d.notes);
  toggleSection(els.secTags,    d.tags?.length, () => renderChips(els.detailTags, d.tags));
  toggleSection(els.secKeywords, d.keywords?.length, () => renderChips(els.detailKeywords, d.keywords));

  els.detailSource.textContent     = d.source || "—";
  els.detailBookmarked.textContent = d.date_bookmarked || "—";
  els.detailLastsync.textContent   = d.last_sync || "—";
  // last_enriched lives in extras (it's not part of CORE_FIELDS server-side)
  // until it gets promoted; surface it here so users can confirm an enrich run.
  els.detailLastenriched.textContent = ((d.extras || {}).last_enriched) || d.last_enriched || "—";
  els.detailArchive.innerHTML      = d.archive_url
    ? `<a href="${escapeHtml(d.archive_url)}" target="_blank" rel="noopener">${escapeHtml(d.archive_url)}</a>`
    : "—";
  els.detailFile.textContent = d.file || "—";

  // Download buttons only for video items
  const isVideo = d.kind === "video";
  els.detailDlVideo.classList.toggle("hidden", !isVideo);
  els.detailDlAudio.classList.toggle("hidden", !isVideo);
  if (isVideo) applyDownloadButtonState(d);

  els.drawer.classList.remove("hidden");
  els.drawer.setAttribute("aria-hidden", "false");
}

// ─── Downloads ─────────────────────────────────────────────────────

function applyDownloadButtonState(d) {
  const ex = d.extras || {};
  const hasVideo = !!ex.download_path_video;
  const hasAudio = !!ex.download_path_audio;

  els.detailDlVideo.classList.remove("hidden");
  els.detailDlAudio.classList.remove("hidden");
  els.detailDlVideo.disabled = hasVideo;
  els.detailDlAudio.disabled = hasAudio;
  els.detailDlVideo.classList.toggle("btn-done", hasVideo);
  els.detailDlAudio.classList.toggle("btn-done", hasAudio);
  els.detailDlVideo.textContent = hasVideo ? "✓ mp4" : "⬇️ mp4";
  els.detailDlAudio.textContent = hasAudio ? "✓ mp3" : "🎵 mp3";
}

async function startDownload(format) {
  if (!state.currentId) return;
  const btns = [els.detailDlVideo, els.detailDlAudio];
  btns.forEach(b => b.disabled = true);
  els.detailDlVideo.textContent = format === "audio" ? "⬇️ mp4" : "Queued…";
  els.detailDlAudio.textContent = format === "audio" ? "Queued…" : "🎵 mp3";
  showToast(`Queued ${format === "audio" ? "audio" : "video"} download…`);

  try {
    const r = await fetch(`/api/bookmarks/${state.currentId}/download`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ format }),
    });
    if (!r.ok) throw new Error(`${r.status}`);
    pollDownload(state.currentId);
  } catch (err) {
    showToast(`Download failed: ${err.message}`);
    btns.forEach(b => b.disabled = false);
  }
}

function pollDownload(bid) {
  const tick = async () => {
    if (state.currentId !== bid) return;   // user moved on
    try {
      const r = await fetch(`/api/bookmarks/${bid}/download`);
      if (!r.ok) return;
      const s = await r.json();
      if (s.status === "done") {
        showToast("Downloaded ✓");
        await openDetail(bid);             // refresh drawer (now has downloaded: true)
        return;
      }
      if (s.status === "error") {
        showToast(`Download error: ${s.message || "unknown"}`, 4000);
        [els.detailDlVideo, els.detailDlAudio].forEach(b => b.disabled = false);
        applyDownloadButtonState(state.detail);
        return;
      }
      setTimeout(tick, 2000);
    } catch { setTimeout(tick, 3000); }
  };
  setTimeout(tick, 1500);
}

els.detailDlVideo.addEventListener("click", () => startDownload("video"));
els.detailDlAudio.addEventListener("click", () => startDownload("audio"));

// ─── Dynamic extras (plugin-contributed fields) ────────────────────

function formatValue(v, spec) {
  const fmt = spec.format || "text";
  if (v == null || v === "") return "";
  switch (fmt) {
    case "number":
      return typeof v === "number" ? v.toLocaleString() : String(v);
    case "bool": {
      if (!v) return "";                         // false → hide row entirely
      const icon = spec.icon || "✅";
      return `<span class="badge badge-on">${icon} <span>${escapeHtml(spec.label || "yes")}</span></span>`;
    }
    case "date":
      return String(v).slice(0, 10);
    case "duration":
      return spec.icon
        ? `${spec.icon} ${escapeHtml(String(v))}`
        : escapeHtml(String(v));
    case "url": {
      const href = spec.url_template
        ? spec.url_template.replace("{value}", encodeURIComponent(String(v)))
        : String(v);
      return `<a href="${escapeHtml(href)}" target="_blank" rel="noopener">${escapeHtml(String(v))}</a>`;
    }
    case "image": {
      // Inline thumbnail. The metadata stores a URL string; we render a small
      // <img> wrapped in an anchor so click opens the full-resolution image
      // in a new tab. `loading=lazy` so off-screen drawer items don't fetch
      // until the user opens them.
      const src = String(v);
      const alt = escapeHtml(spec.label || "image");
      return `<a href="${escapeHtml(src)}" target="_blank" rel="noopener" class="extra-thumb-link">`
           + `<img class="extra-thumb" src="${escapeHtml(src)}" alt="${alt}" loading="lazy">`
           + `</a>`;
    }
    case "file_link": {
      // Server-relative path stored in FM. `url_prefix` mounts it under a
      // StaticFiles route so the link works from the browser.
      const prefix = spec.url_prefix || "/";
      const segments = String(v).split("/").map(encodeURIComponent);
      const href = prefix + segments.join("/");
      const label = segments[segments.length - 1] ? decodeURIComponent(segments[segments.length - 1]) : String(v);
      return `<a href="${escapeHtml(href)}" target="_blank" rel="noopener">📂 ${escapeHtml(label)}</a>`;
    }
    case "tags":
    case "list":
      if (!Array.isArray(v) || v.length === 0) return "";
      return v.map(x => `<span class="tag">${escapeHtml(x)}</span>`).join(" ");
    default:
      return escapeHtml(String(v));
  }
}

function renderExtras(d) {
  const extras = d.extras || {};
  const itemKind = d.kind || "bookmark";

  // Effective-kinds set: the item's own `kind` plus any `*_kind` hint written
  // by an enricher (e.g. `youtube_kind: "video"` on a kind=bookmark item that
  // happens to be a YouTube link). This lets enricher-contributed fields with
  // `kinds: ["video"]` render correctly even when the original item kind is
  // "bookmark".
  const effectiveKinds = new Set([itemKind]);
  for (const k of Object.keys(extras)) {
    if (k.endsWith("_kind") && typeof extras[k] === "string" && extras[k]) {
      effectiveKinds.add(extras[k]);
    }
  }

  // Walk every plugin's specs (sources + enrichers — both registered under
  // the same /api/schema map). Iterating all of them — not just the ones
  // keyed by `d.source` — is what lets a manual or browser-bookmark item
  // pick up its github_* / youtube_* fields once an enricher has written
  // them. Dedupe by spec.name so a field declared in two schema entries
  // (e.g. by both a source and its enricher) only renders once.
  const seenNames = new Set();
  const specs = [];
  for (const key of Object.keys(state.schema || {})) {
    for (const spec of state.schema[key] || []) {
      if (!spec || !spec.name || seenNames.has(spec.name)) continue;
      seenNames.add(spec.name);
      specs.push(spec);
    }
  }

  // Group specs that apply to this kind AND have a non-empty value.
  // Bool flags within the same group collapse into a single "Flags" row.
  const groups = new Map();          // groupName -> [{label, html}]
  const flagsByGroup = new Map();    // groupName -> [badgeHtml, ...]
  for (const spec of specs) {
    if (spec.kinds && spec.kinds.length
        && !spec.kinds.some(k => effectiveKinds.has(k))) continue;
    const raw = extras[spec.name];
    const html = formatValue(raw, spec);
    if (!html) continue;
    const g = spec.group || "Details";
    if (spec.format === "bool") {
      if (!flagsByGroup.has(g)) flagsByGroup.set(g, []);
      flagsByGroup.get(g).push(html);
    } else {
      if (!groups.has(g)) groups.set(g, []);
      groups.get(g).push({ label: spec.label || spec.name, html });
    }
  }
  for (const [g, badges] of flagsByGroup) {
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push({ label: "Flags", html: badges.join(" ") });
  }

  if (groups.size === 0) {
    els.secExtras.classList.add("hidden");
    els.detailExtras.innerHTML = "";
    return;
  }

  const parts = [];
  for (const [group, rows] of groups) {
    parts.push(`<h3>${escapeHtml(group)}</h3>`);
    parts.push('<dl class="kv">');
    for (const r of rows) {
      parts.push(`<dt>${escapeHtml(r.label)}</dt><dd>${r.html}</dd>`);
    }
    parts.push("</dl>");
  }
  els.detailExtras.innerHTML = parts.join("");
  els.secExtras.classList.remove("hidden");
}

function toggleSection(section, has, render) {
  if (has) { section.classList.remove("hidden"); render(); }
  else     { section.classList.add("hidden"); }
}

function renderChips(container, items) {
  container.innerHTML = (items || []).map(t =>
    `<span class="tag">${escapeHtml(t)}</span>`
  ).join("");
}

function closeDrawer() {
  els.drawer.classList.add("hidden");
  els.drawer.setAttribute("aria-hidden", "true");
  state.currentId = null;
}
els.drawerClose.addEventListener("click", closeDrawer);

// ─── Edit modal ────────────────────────────────────────────────────

function openEdit() {
  if (!state.detail) return;
  const d = state.detail;
  els.editTitle.value = d.title || "";
  els.editImp.value = d.importance || 0;
  els.editImpVal.textContent = d.importance || 0;
  els.editTags.value = (d.tags || []).join(", ");
  els.editSummary.value = d.summary || "";
  els.editNotes.value = d.notes || "";
  els.editKeywords.value = (d.keywords || []).join(", ");
  els.editStatus.textContent = "";
  els.editStatus.className = "edit-status";
  els.editModal.classList.remove("hidden");
  els.editModal.setAttribute("aria-hidden", "false");
  els.editTitle.focus();
}

function closeEdit() {
  els.editModal.classList.add("hidden");
  els.editModal.setAttribute("aria-hidden", "true");
}

els.detailEdit.addEventListener("click", openEdit);
els.editClose.addEventListener("click", closeEdit);
els.editCancel.addEventListener("click", closeEdit);
els.editImp.addEventListener("input", () => els.editImpVal.textContent = els.editImp.value);

els.editModal.addEventListener("click", (e) => {
  if (e.target === els.editModal) closeEdit();
});

els.editForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!state.currentId) return;
  const parseList = (s) => s.split(",").map(x => x.trim()).filter(Boolean);
  const patch = {
    title: els.editTitle.value.trim(),
    importance: parseInt(els.editImp.value, 10),
    tags: parseList(els.editTags.value),
    summary: els.editSummary.value,
    notes: els.editNotes.value,
    keywords: parseList(els.editKeywords.value),
  };

  els.editStatus.textContent = "Saving…";
  els.editStatus.className = "edit-status";
  try {
    const r = await fetch(`/api/bookmarks/${state.currentId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
    if (!r.ok) {
      const txt = await r.text();
      throw new Error(`${r.status}: ${txt}`);
    }
    const updated = await r.json();
    // Update caches
    state.detail = updated;
    const idx = state.all.findIndex(x => x.id === updated.id);
    if (idx >= 0) {
      state.all[idx] = { ...state.all[idx], ...updated, has_summary: !!updated.summary };
    }
    applyFilter();
    closeEdit();
    openDetail(state.currentId);
    showToast("Saved ✓");
  } catch (err) {
    els.editStatus.textContent = `Error: ${err.message}`;
    els.editStatus.className = "edit-status error";
  }
});

// ─── Tabs registry ─────────────────────────────────────────────────
//
// Tabs are top-level sections (Search / Photos / Videos / Manage / plugins).
// The registry is dogfooded — built-ins below register themselves through
// the same API plugin tabs use. Public surface exposed at `window.booki`
// for plugin JS modules.

const Tabs = (() => {
  const list = [];
  const byId = new Map();
  let active = null;

  function _sort() {
    list.sort((a, b) => (a.order | 0) - (b.order | 0) || a.id.localeCompare(b.id));
  }

  function register(spec) {
    if (!spec || !spec.id) throw new Error("Tabs.register: id is required");
    let tab = byId.get(spec.id);
    if (!tab) {
      tab = { id: spec.id, label: "", icon: "", order: 100, _mounted: false };
      byId.set(spec.id, tab);
      list.push(tab);
    }
    Object.assign(tab, spec);
    _sort();
    renderTabBar();
    return tab;
  }

  // Plugin modules call this after import to fill in behavior on a tab
  // whose metadata was registered up-front by the bootstrap.
  function implement(id, behavior) {
    const tab = byId.get(id);
    if (!tab) {
      console.warn(`[booki] Tabs.implement('${id}'): unknown tab`);
      return;
    }
    Object.assign(tab, behavior || {});
    if (active && active.id === id) {
      // Module loaded after activation. The bootstrap stub-mount already
      // ran ("Loading plugin tab…") and set _mounted=true, so the previous
      // !_mounted guard skipped the real mount. Replace the stub now.
      if (tab._container && typeof tab.mount === "function") {
        try { tab.mount(tab._container); } catch (e) { console.error(e); }
        tab._mounted = true;
      }
      try { tab.onShow?.(tab._container); } catch (e) { console.error(e); }
    }
  }

  function _ensurePanel(id) {
    const sel = `.tab-panel[data-panel="${CSS.escape(id)}"]`;
    const existing = document.querySelector(sel);
    if (existing) return existing;
    const host = document.getElementById("tabPanels");
    const sec = document.createElement("section");
    sec.className = "tab-panel";
    sec.dataset.panel = id;
    host.appendChild(sec);
    return sec;
  }

  function _mountIfNeeded(tab) {
    if (tab._mounted) return;
    tab._container = _ensurePanel(tab.id);
    try { tab.mount?.(tab._container); } catch (e) { console.error(e); }
    tab._mounted = true;
  }

  function activate(id) {
    const tab = byId.get(id);
    if (!tab) return false;
    if (active && active.id === id) return true;
    if (active) {
      try { active.onHide?.(active._container); } catch (e) { console.error(e); }
    }
    // Force-strip .active from every tab-panel — covers the initial-boot
    // case where a panel ships pre-marked active in index.html.
    document.querySelectorAll(".tab-panel.active")
            .forEach(el => el.classList.remove("active"));
    active = tab;
    state.tab = id;
    _mountIfNeeded(tab);
    tab._container?.classList.add("active");
    try { tab.onShow?.(tab._container); } catch (e) { console.error(e); }

    // Fallback re-render: if bookmark data hasn't loaded yet, this tab's
    // first onShow saw an empty state.all. Hook into the next data update
    // so the tab populates without the user having to switch tabs.
    if (!state.all || state.all.length === 0) {
      const off = window.booki?.bookmarks?.onChange?.(() => {
        try { tab.onShow?.(tab._container); } catch (e) { console.error(e); }
        try { off?.(); } catch {}
      });
    }

    document.querySelectorAll("#tabBar .tab-btn").forEach(btn => {
      btn.classList.toggle("active", btn.dataset.tab === id);
    });
    try { localStorage.setItem("booki.tab", id); } catch {}
    if (location.hash !== "#" + id) {
      try { history.replaceState(null, "", "#" + id); } catch {}
    }
    return true;
  }

  function renderTabBar() {
    const bar = document.getElementById("tabBar");
    if (!bar) return;
    bar.innerHTML = "";
    list.forEach((t, i) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "tab-btn" + (active && active.id === t.id ? " active" : "");
      btn.dataset.tab = t.id;
      btn.setAttribute("role", "tab");
      btn.setAttribute("aria-selected", active && active.id === t.id ? "true" : "false");
      const label = t.label || t.id;
      if (i < 9) btn.title = `${label} — press ${i + 1}`;
      const ico = t.icon ? `<span class="tab-ico">${t.icon}</span>` : "";
      const num = i < 9 ? `<span class="tab-num">${i + 1}</span>` : "";
      btn.innerHTML = `${ico}<span class="tab-label">${label}</span>${num}`;
      btn.addEventListener("click", () => activate(t.id));
      bar.appendChild(btn);
    });
  }

  return {
    register, implement, activate,
    current: () => active?.id || null,
    get: (id) => byId.get(id),
    all: () => [...list],
  };
})();

// Public surface for plugin tabs. Kept tiny and stable on purpose — plugins
// reach the rest of the app through these helpers, never private internals.
window.booki = window.booki || {};
window.booki.tabs = Tabs;
window.booki.api = {
  fetch: (path, opts) => fetch(path, opts),
  get:   (path) => fetch(path).then(r => r.ok ? r.json()
                                              : Promise.reject(new Error(`HTTP ${r.status}`))),
};
window.booki.bookmarks = {
  all: () => state.all.slice(),
  byId: (id) => state.all.find(b => b.id === id) || null,
  // Subscribe to bookmark refreshes. Returns an unsubscribe function.
  // If bookmarks are already loaded, the callback is invoked once on the
  // microtask queue so late subscribers (plugin modules that finish loading
  // after the initial fetch) still see the current data without waiting
  // for the next change.
  onChange: (cb) => {
    if (typeof cb !== "function") return () => {};
    _bookmarkChangeListeners.add(cb);
    if (state.all && state.all.length) {
      Promise.resolve().then(() => {
        try { cb(state.all); } catch (e) { console.error(e); }
      });
    }
    return () => _bookmarkChangeListeners.delete(cb);
  },
};
window.booki.ui = {
  toast: (msg) => showToast(msg),
  // Open the bookmark detail drawer by id. Aliased as openDetail too —
  // the host's internal function is `openDetail`, kept for callers who
  // already know the name.
  openDrawer: (id) => openDetail(id),
  openDetail: (id) => openDetail(id),
  // Helpers a plugin tab is likely to want when rendering items inline.
  escapeHtml: (s) => escapeHtml(s),
  highlight:  (text, matches) => highlight(text, matches),
  // Plugin tabs that own their results container (and therefore declare
  // `getSelection`) call this after re-rendering so the topbar's
  // "⬇ Export N items" label refreshes. Built-in tabs use it implicitly
  // via the MutationObserver below.
  refreshExportButton: () => refreshExportButton(),
};
window.booki.search = {
  fuzzy:     (q, text) => fuzzyMatch(q, text),
  substring: (q, text) => substringMatch(q, text),
  // Live read of the global "fuzzy on/off" toggle so plugin tabs honor it.
  get useFuzzy() { return !!state.fuzzy; },
};
// Plugin tabs use this to embed the same Advanced filter UI and apply the
// scoped filters/sort. Each scope's state is independent.
window.booki.adv = {
  // Plugin tabs pass `{ scope: "<tab-id>" }` so the form has its own
  // independent state, plus a field combo narrowed to that scope's kinds.
  mountInto: (host, opts = {}) => mountAdvancedSearch(host, opts),
  predicate:  (scope) => makeAdvPredicate(state.advByScope[scope] || makeDefaultAdv()),
  applySort:  (items, scope) => applyAdvSort(items, state.advByScope[scope] || makeDefaultAdv()),
  hasTopSort: (scope) => advHasTop(state.advByScope[scope] || makeDefaultAdv()),
  // For inline rendering: returns an HTML chip showing the Top-field value
  // for `bm`, or "" if the scope's Top filter isn't active.
  topChip: (bm, scope) => topFieldChipHtml(bm, state.advByScope[scope] || makeDefaultAdv()),
  // Listener fires with (adv, scope) — callers can ignore other scopes.
  onChange: (cb) => {
    if (typeof cb !== "function") return () => {};
    _advChangeListeners.add(cb);
    return () => _advChangeListeners.delete(cb);
  },
};

// ─── Built-in tabs ─────────────────────────────────────────────────

Tabs.register({
  id: "search", label: "Search", icon: "🔎", order: 10,
  // Search panel is pre-rendered in index.html — mount only injects the
  // view-mode toggle into the pre-existing results toolbar.
  mount() {
    const tb = document.getElementById("searchViewToolbar");
    if (tb) {
      tb.innerHTML = viewToggleHtml("search", ["list", "grid", "table"], "list");
      wireViewToggle(tb, "search", () => renderResults());
    }
    if (els.advSearchHost) mountAdvancedSearch(els.advSearchHost, { scope: "search" });
  },
  onShow() { els.findInput?.focus?.(); refreshExportButton(); },
  getSelection: () => ({ kind: "any", ids: idsFromContainer("#results") }),
});

Tabs.register({
  id: "photos", label: "Photos", icon: "🖼", order: 20,
  mount(el) {
    el.innerHTML = `
      <div class="photo-tab scoped-tab">
        <header class="tab-header">
          <h2>🖼 Photos</h2>
          <p class="tab-sub" id="photoCount">—</p>
          ${viewToggleHtml("photos", ["list", "grid", "table"], "grid")}
        </header>
        <div class="search-box scoped-search" id="photoSearchBox">
          <span class="search-icon">🔎</span>
          <input id="photoFindInput" type="search" autocomplete="off"
                 autocapitalize="off" autocorrect="off" spellcheck="false"
                 placeholder="Search photos by title or URL…">
          <span class="hint">↵ open · click for details</span>
        </div>
        <div class="adv-host" id="photoAdvHost"></div>
        <ul class="photo-grid" id="photoGrid"></ul>
        <p class="tab-empty hidden" id="photoEmpty">
          No photos yet — the photo enricher tags items by URL pattern.<br>
          Run <code>booki sync --no-sync --enrich-meta --enricher photo --all</code>
          to backfill.
        </p>
        <p class="tab-empty hidden" id="photoNoMatch">
          No photos match your search.
        </p>
      </div>`;

    wireViewToggle(el, "photos", () => renderPhotoGrid());

    const input = document.getElementById("photoFindInput");
    input.addEventListener("input", renderPhotoGrid);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        const first = document.querySelector("#photoGrid [data-id]");
        if (first) {
          const id = first.dataset.id;
          const bm = state.all.find(b => b.id === id);
          if (bm?.url) window.open(bm.url, "_blank", "noopener");
        }
      } else if (e.key === "Escape") {
        if (input.value) { input.value = ""; renderPhotoGrid(); }
        else input.blur();
      }
    });

    const advHost = document.getElementById("photoAdvHost");
    if (advHost) mountAdvancedSearch(advHost, { scope: "photo" });
  },
  onShow() {
    renderPhotoGrid();
    document.getElementById("photoFindInput")?.focus();
    refreshExportButton();
  },
  getSelection: () => ({ kind: "photo", ids: idsFromContainer("#photoGrid") }),
});

function isPhotoBookmark(b) {
  return b.kind === "photo" || (b.sources || []).includes("photo");
}

// Browsers block `file:` resources from an http(s): page. Route those
// through `/api/local-file`, which streams the file from an allow-listed
// directory-plugin root.
function imageSrcFor(url) {
  if (!url) return "";
  if (url.startsWith("file:")) {
    let abs = url.replace(/^file:\/\/(?:[^/]*)?/, "");
    try { abs = decodeURIComponent(abs); } catch {}
    return `/api/local-file?path=${encodeURIComponent(abs)}`;
  }
  return url;
}

const PHOTO_DIRECT_RE = /\.(jpg|jpeg|png|gif|webp|heic|avif|bmp|tiff?|raf|cr[23]|nef|nrw|arw|sr[fr2]|dng|orf|rw2|pef|srw|raw|x3f|iiq|3fr|erf|kdc|mef|mrw|rwl)$/i;

function isDirectImageUrl(url) {
  if (!url) return false;
  try {
    const u = new URL(url);
    return PHOTO_DIRECT_RE.test(u.pathname);
  } catch {
    return PHOTO_DIRECT_RE.test(url.split(/[?#]/)[0]);
  }
}

function renderPhotoGrid() {
  const grid    = document.getElementById("photoGrid");
  const count   = document.getElementById("photoCount");
  const empty   = document.getElementById("photoEmpty");
  const noMatch = document.getElementById("photoNoMatch");
  const input   = document.getElementById("photoFindInput");
  if (!grid) return;

  // Items the photo enricher claimed: either it took over `kind` (for
  // generic bookmarks) or it added `"photo"` to the cross-cutting sources
  // list while leaving an explicit `kind` (e.g. "file" from the directory
  // plugin) intact.
  const all = state.all.filter(isPhotoBookmark);

  if (!all.length) {
    grid.innerHTML = "";
    count.textContent = "0 photos";
    empty.classList.remove("hidden");
    noMatch?.classList.add("hidden");
    return;
  }
  empty.classList.add("hidden");

  // Photos tab uses its own scope so changes here don't bleed into Search.
  const adv = state.advByScope.photo;
  const advPred = makeAdvPredicate(adv);
  const filtered = all.filter(advPred);
  const topSorts = advHasTop(adv);

  // Apply this tab's local search; falls back to importance-sort when empty.
  const q = (input?.value || "").trim();
  let photos;
  if (q) {
    const match = state.fuzzy ? fuzzyMatch : substringMatch;
    const scored = [];
    for (const b of filtered) {
      const tm = match(q, b.title || "");
      const um = match(q, b.url || "");
      const em = match(q, (b.tags || []).join(" "));
      if (!tm && !um && !em) continue;
      const s = (tm ? tm.score * 3 : 0)
              + (um ? um.score * 1.2 : 0)
              + (em ? em.score * 0.6 : 0)
              + (b.importance || 0) * 0.5;
      scored.push({ bm: b, score: s });
    }
    photos = topSorts
      ? applyAdvSort(scored.map(x => x.bm), adv)
      : scored.sort((a, b) => b.score - a.score).map(x => x.bm).slice(0, 200);
  } else {
    photos = topSorts
      ? applyAdvSort(filtered, adv)
      : [...filtered].sort((a, b) => (b.importance || 0) - (a.importance || 0));
  }

  count.textContent = q
    ? `${photos.length} of ${all.length} photos`
    : (all.length === 1 ? "1 photo" : `${all.length} photos`);

  if (!photos.length) {
    grid.innerHTML = "";
    noMatch?.classList.remove("hidden");
    return;
  }
  noMatch?.classList.add("hidden");

  const mode = _viewModeFor("photos", "grid");
  // Wipe the host's class state — different modes set different classes
  // (.photo-grid for the photo-specific image grid, .items-host for the
  // generic list/table renderers).
  grid.className = "";
  if (mode === "table") {
    renderItemsTable(grid, photos, { adv });
    return;
  }
  if (mode === "list") {
    renderItemsList(grid, photos, { adv });
    return;
  }
  // mode === "grid" — keep the existing photo-thumbnail tile grid.
  grid.classList.add("photo-grid");
  const frag = document.createDocumentFragment();
  for (const b of photos) {
    const li = document.createElement("li");
    li.className = "photo-tile";
    li.tabIndex = 0;
    li.dataset.id = b.id;

    const direct = isDirectImageUrl(b.url);
    const imgHtml = direct
      ? `<img loading="lazy" src="${escapeHtml(imageSrcFor(b.url))}" alt="${escapeHtml(b.title || '')}">`
      : `<span class="photo-placeholder">🖼</span>`;

    const topChip = topFieldChipHtml(b, adv);
    li.innerHTML = `
      <div class="photo-thumb">${imgHtml}</div>
      <div class="photo-meta">
        <div class="photo-title" title="${escapeHtml(b.title || '')}">${escapeHtml(b.title || "(untitled)")}</div>
        ${b.importance ? `<div class="photo-imp">★${b.importance}</div>` : ""}
        ${topChip ? `<div class="tile-top">${topChip}</div>` : ""}
      </div>`;

    li.addEventListener("click", () => openDetail(b.id));
    li.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        openDetail(b.id);
      }
    });

    const img = li.querySelector("img");
    if (img) {
      img.addEventListener("error", () => {
        const thumb = li.querySelector(".photo-thumb");
        if (thumb) thumb.innerHTML = `<span class="photo-placeholder">🖼</span>`;
      });
    }
    frag.appendChild(li);
  }
  grid.innerHTML = "";
  grid.appendChild(frag);
}

Tabs.register({
  id: "videos", label: "Videos", icon: "🎬", order: 30,
  mount(el) {
    el.innerHTML = `
      <div class="video-tab scoped-tab">
        <header class="tab-header">
          <h2>🎬 Videos</h2>
          <p class="tab-sub" id="videoCount">—</p>
          ${viewToggleHtml("videos", ["list", "grid", "table"], "grid")}
        </header>
        <div class="search-box scoped-search" id="videoSearchBox">
          <span class="search-icon">🔎</span>
          <input id="videoFindInput" type="search" autocomplete="off"
                 autocapitalize="off" autocorrect="off" spellcheck="false"
                 placeholder="Search videos by title, channel, or URL…">
          <span class="hint">↵ open · click for details</span>
        </div>
        <div class="adv-host" id="videoAdvHost"></div>
        <ul class="video-grid" id="videoGrid"></ul>
        <p class="tab-empty hidden" id="videoEmpty">
          No videos yet. The YouTube source plugin pulls liked / watched videos
          and recent uploads from subscribed channels — wire it up in
          <code>config.toml</code> and run <code>booki sync</code>.
        </p>
        <p class="tab-empty hidden" id="videoNoMatch">
          No videos match your search.
        </p>`;

    wireViewToggle(el, "videos", () => renderVideoGrid());

    const input = document.getElementById("videoFindInput");
    input.addEventListener("input", renderVideoGrid);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        const first = document.querySelector("#videoGrid [data-id]");
        if (first) {
          const id = first.dataset.id;
          const bm = state.all.find(b => b.id === id);
          if (bm?.url) window.open(bm.url, "_blank", "noopener");
        }
      } else if (e.key === "Escape") {
        if (input.value) { input.value = ""; renderVideoGrid(); }
        else input.blur();
      }
    });
    const advHost = document.getElementById("videoAdvHost");
    if (advHost) mountAdvancedSearch(advHost, { scope: "video" });
  },
  onShow() {
    renderVideoGrid();
    document.getElementById("videoFindInput")?.focus();
    refreshExportButton();
  },
  getSelection: () => ({ kind: "video", ids: idsFromContainer("#videoGrid") }),
});

function isVideoBookmark(b) {
  if (b.kind === "video") return true;
  const e = b.extras || {};
  return e.youtube_kind === "video";
}

function renderVideoGrid() {
  const grid    = document.getElementById("videoGrid");
  const count   = document.getElementById("videoCount");
  const empty   = document.getElementById("videoEmpty");
  const noMatch = document.getElementById("videoNoMatch");
  const input   = document.getElementById("videoFindInput");
  if (!grid) return;

  const all = state.all.filter(isVideoBookmark);

  if (!all.length) {
    grid.innerHTML = "";
    count.textContent = "0 videos";
    empty.classList.remove("hidden");
    noMatch?.classList.add("hidden");
    return;
  }
  empty.classList.add("hidden");

  const adv = state.advByScope.video;
  const advPred = makeAdvPredicate(adv);
  const filtered = all.filter(advPred);
  const topSorts = advHasTop(adv);

  const q = (input?.value || "").trim();
  let videos;
  if (q) {
    const match = state.fuzzy ? fuzzyMatch : substringMatch;
    const scored = [];
    for (const b of filtered) {
      const e = b.extras || {};
      const channel = String(e.channel || "");
      const tm = match(q, b.title || "");
      const um = match(q, b.url || "");
      const cm = match(q, channel);
      const em = match(q, (b.tags || []).join(" "));
      if (!tm && !um && !cm && !em) continue;
      const s = (tm ? tm.score * 3   : 0)
              + (cm ? cm.score * 1.8 : 0)
              + (um ? um.score * 1.2 : 0)
              + (em ? em.score * 0.6 : 0)
              + (b.importance || 0) * 0.5;
      scored.push({ bm: b, score: s });
    }
    videos = topSorts
      ? applyAdvSort(scored.map(x => x.bm), adv)
      : scored.sort((a, b) => b.score - a.score).map(x => x.bm).slice(0, 200);
  } else {
    videos = topSorts
      ? applyAdvSort(filtered, adv)
      : [...filtered].sort((a, b) => (b.importance || 0) - (a.importance || 0))
                     .slice(0, 200);
  }

  count.textContent = q
    ? `${videos.length} of ${all.length} videos`
    : (all.length === 1 ? "1 video" : `${all.length} videos`);

  if (!videos.length) {
    grid.innerHTML = "";
    noMatch?.classList.remove("hidden");
    return;
  }
  noMatch?.classList.add("hidden");

  const mode = _viewModeFor("videos", "grid");
  grid.className = "";
  if (mode === "table") {
    renderItemsTable(grid, videos, { adv });
    return;
  }
  if (mode === "list") {
    renderItemsList(grid, videos, { adv });
    return;
  }
  // mode === "grid" — keep existing video-poster grid.
  grid.classList.add("video-grid");
  const frag = document.createDocumentFragment();
  for (const b of videos) {
    const e = b.extras || {};
    const thumb   = e.youtube_thumbnail || e.photo_thumbnail || "";
    const dur     = String(e.duration || "");
    const channel = String(e.channel || "");

    const li = document.createElement("li");
    li.className = "video-tile";
    li.tabIndex = 0;
    li.dataset.id = b.id;

    const thumbHtml = thumb
      ? `<img loading="lazy" src="${escapeHtml(imageSrcFor(thumb))}" alt="">`
      : `<span class="video-placeholder">🎬</span>`;
    const durHtml = dur ? `<span class="video-duration">${escapeHtml(dur)}</span>` : "";

    const topChip = topFieldChipHtml(b, adv);
    li.innerHTML = `
      <div class="video-thumb">
        ${thumbHtml}
        ${durHtml}
      </div>
      <div class="video-meta">
        <div class="video-title" title="${escapeHtml(b.title || '')}">${escapeHtml(b.title || "(untitled)")}</div>
        ${channel
          ? `<div class="video-channel" title="${escapeHtml(channel)}">${escapeHtml(channel)}</div>`
          : ""}
        ${topChip ? `<div class="tile-top">${topChip}</div>` : ""}
      </div>`;

    li.addEventListener("click", () => openDetail(b.id));
    li.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        openDetail(b.id);
      }
    });

    const img = li.querySelector("img");
    if (img) {
      img.addEventListener("error", () => {
        const tw = li.querySelector(".video-thumb");
        if (tw) tw.innerHTML = `<span class="video-placeholder">🎬</span>${durHtml}`;
      });
    }
    frag.appendChild(li);
  }
  grid.innerHTML = "";
  grid.appendChild(frag);
}

Tabs.register({
  id: "ask", label: "Ask", icon: "✨", order: 40,
  // The askBox + askResult elements live in index.html under the search panel
  // for historical reasons. We re-parent them into this panel on first mount;
  // listeners attached to those elements are preserved across moves.
  mount(el) {
    el.innerHTML = `
      <div class="ask-tab scoped-tab">
        <header class="tab-header">
          <h2>✨ Ask</h2>
          <p class="tab-sub">
            Semantic search over your bookmarks, optionally synthesized by an LLM.
          </p>
        </header>
      </div>`;
    const root = el.querySelector(".ask-tab");
    if (els.askBox) {
      els.askBox.classList.remove("hidden");
      els.askBox.classList.add("scoped-search");
      root.appendChild(els.askBox);
    }
    if (els.askResult) {
      els.askResult.classList.remove("hidden");
      root.appendChild(els.askResult);
    }
  },
  onShow() { els.askInput?.focus?.(); refreshExportButton(); },
  // Ask tab renders into the same #askSources <ul class="results"> below the
  // synthesized answer; that's the export selection.
  getSelection: () => ({ kind: "any", ids: idsFromContainer("#askSources") }),
});

Tabs.register({
  id: "manage", label: "Manage", icon: "⚙", order: 90,
  mount(el) {
    el.innerHTML = `
      <div class="manage-tab scoped-tab">
        <header class="tab-header">
          <h2>⚙ Manage</h2>
          <p class="tab-sub">System status, configuration, plugins, and logs.</p>
        </header>

        <nav class="subtabs" id="manageSubtabs" aria-label="Manage sections">
          <button type="button" class="subtab active" data-subtab="status">🩺 Doctor</button>
          <button type="button" class="subtab" data-subtab="info">📊 General</button>
          <button type="button" class="subtab" data-subtab="plugins">🔌 Plugins</button>
          <button type="button" class="subtab" data-subtab="jobs">🔄 Sync &amp; Ingest</button>
          <button type="button" class="subtab" data-subtab="tasks">✈️ Tasks</button>
          <button type="button" class="subtab" data-subtab="logs">📜 Logs</button>
        </nav>

        <section class="subtab-panel active" data-subpanel="status">
          <div class="subtab-actions">
            <button type="button" class="btn manage-refresh" id="statusRefresh"
                    title="Re-run checks">↻ Refresh</button>
          </div>
          <p class="status-platform" id="statusPlatform"></p>
          <div class="status-summary" id="statusSummary"></div>
          <div class="status-body" id="statusBody">
            <p class="hint-text">Loading…</p>
          </div>
        </section>

        <section class="subtab-panel" data-subpanel="info">
          <div class="info-cards">
            <article class="info-card info-card-wide">
              <header><span class="info-glyph">📊</span><h3>Library</h3></header>
              <dl class="info-grid">
                <dt>Items</dt>     <dd id="statTotal">—</dd>
                <dt>Enriched</dt>  <dd id="statEnriched">—</dd>
                <dt>Sources</dt>   <dd id="statSources">—</dd>
                <dt>Last sync</dt> <dd id="statLastSync">—</dd>
              </dl>
            </article>

            <article class="info-card">
              <header><span class="info-glyph">🔌</span><h3>By source</h3></header>
              <ul class="bar-list" id="statBySource"></ul>
            </article>

            <article class="info-card">
              <header><span class="info-glyph">🧩</span><h3>By kind</h3></header>
              <ul class="bar-list" id="statByKind"></ul>
            </article>

            <article class="info-card info-card-wide">
              <header><span class="info-glyph">⚙</span><h3>Runtime</h3></header>
              <dl class="info-grid" id="manageInfo">
                <dt>Loading…</dt><dd></dd>
              </dl>
            </article>
          </div>
          <p class="info-foot" id="statDir"></p>
        </section>

        <section class="subtab-panel" data-subpanel="plugins">
          <div class="subtab-actions">
            <button type="button" class="btn manage-refresh" id="pluginsRefresh">↻ Refresh</button>
          </div>
          <div id="managePlugins">
            <p class="hint-text">Loading…</p>
          </div>
        </section>

        <section class="subtab-panel" data-subpanel="jobs">
          <div class="job-launchers" id="jobLaunchers">
            <article class="job-launcher" data-kind="sync">
              <header>
                <h3>🔄 Sync</h3>
                <p class="hint-text">Pull from sources, optionally enrich.</p>
              </header>
              <fieldset class="job-options" data-kind="sync">
                <label class="check"><input type="checkbox" data-flag="--enrich"> Summarize via LLM (<code>--enrich</code>)</label>
                <label class="check"><input type="checkbox" data-flag="--enrich-meta"> Run enrichers (<code>--enrich-meta</code>)</label>
                <label class="check"><input type="checkbox" data-flag="--check-dead-links"> Check dead links</label>
                <label class="check"><input type="checkbox" data-flag="--all"> Re-process every item (<code>--all</code>)</label>
                <label class="check"><input type="checkbox" data-flag="--no-sync"> Skip sync step (<code>--no-sync</code>)</label>
                <label class="check"><input type="checkbox" data-flag="--dry-run"> Dry run</label>
                <div class="job-source-row">
                  <span class="job-source-label">Sources <span class="hint-text">(blank = all)</span></span>
                  <div class="job-source-chips" id="jobSyncSources"></div>
                </div>
                <div class="job-source-row">
                  <span class="job-source-label">Enrichers <span class="hint-text">(blank = all)</span></span>
                  <div class="job-source-chips" id="jobSyncEnrichers"></div>
                </div>
              </fieldset>
              <button type="button" class="btn primary job-run-btn" data-kind="sync">▶ Run sync</button>
            </article>
            <article class="job-launcher" data-kind="ingest">
              <header>
                <h3>📚 Ingest</h3>
                <p class="hint-text">Re-index bookmarks into the vector DB.</p>
              </header>
              <fieldset class="job-options" data-kind="ingest">
                <label class="check"><input type="checkbox" data-flag="--reset"> Reset collection (<code>--reset</code>)</label>
              </fieldset>
              <button type="button" class="btn primary job-run-btn" data-kind="ingest">▶ Run ingest</button>
            </article>
          </div>
          <div class="subtab-actions">
            <button type="button" class="btn manage-refresh" id="jobsRefresh">↻ Refresh</button>
          </div>
          <div id="manageJobs">
            <p class="hint-text">Loading…</p>
          </div>
        </section>

        <section class="subtab-panel" data-subpanel="tasks">
          <div class="subtab-actions">
            <button type="button" class="btn manage-refresh" id="tasksRefresh">↻ Refresh</button>
          </div>
          <div id="manageTasks">
            <p class="hint-text">Loading…</p>
          </div>
        </section>

        <section class="subtab-panel" data-subpanel="logs">
          <div class="logs-toolbar">
            <select id="logsFile" title="Log file"></select>
            <label>tail
              <input type="number" id="logsTail" value="500" min="1" max="5000">
            </label>
            <button type="button" class="btn" id="logsRefresh">↻ Refresh</button>
            <label class="toggle">
              <input type="checkbox" id="logsFollow"> follow
            </label>
            <span class="hint-text" id="logsMeta"></span>
          </div>
          <pre id="logsViewer" class="logs-viewer"><span class="hint-text">No log loaded.</span></pre>
        </section>
      </div>`;

    // Resolve the now-mounted status DOM and (re)wire the existing helpers.
    statusEls.platform = document.getElementById("statusPlatform");
    statusEls.summary  = document.getElementById("statusSummary");
    statusEls.body     = document.getElementById("statusBody");
    statusEls.refresh  = document.getElementById("statusRefresh");
    statusEls.refresh?.addEventListener("click", loadStatus);

    document.getElementById("pluginsRefresh")
            ?.addEventListener("click", loadManagePlugins);
    document.getElementById("tasksRefresh")
            ?.addEventListener("click", refreshManageTasks);
    document.getElementById("jobsRefresh")
            ?.addEventListener("click", refreshManageJobs);

    el.querySelectorAll(".job-run-btn").forEach(btn => {
      btn.addEventListener("click", () => runManageJob(btn.dataset.kind));
    });

    initManageLogs();

    // Sub-tab switcher.
    el.querySelectorAll(".subtab").forEach(btn => {
      btn.addEventListener("click", () => setManageSubtab(btn.dataset.subtab));
    });
    let initial = "status";
    try { initial = localStorage.getItem("booki.manage.subtab") || initial; } catch {}
    setManageSubtab(initial);
  },
  onShow() {
    // Refresh whatever sub-panel is currently active. Other sub-panels
    // refresh lazily when activated (see setManageSubtab).
    runManageSubtabLoader(_manageSubtab);
  },
  onHide() { stopLogsFollow(); stopTasksPoll(); stopJobsPoll(); },
});

let _manageSubtab = "status";
const _manageSubtabLoaded = new Set();

function setManageSubtab(id) {
  if (!id) return;
  document.querySelectorAll(".subtab").forEach(b => {
    b.classList.toggle("active", b.dataset.subtab === id);
  });
  document.querySelectorAll(".subtab-panel").forEach(p => {
    p.classList.toggle("active", p.dataset.subpanel === id);
  });
  _manageSubtab = id;
  try { localStorage.setItem("booki.manage.subtab", id); } catch {}
  if (id !== "logs") stopLogsFollow();
  if (id === "tasks") startTasksPoll(); else stopTasksPoll();
  if (id === "jobs") startJobsPoll(); else stopJobsPoll();
  // Lazy-load the first time; refresh-on-show is handled by onShow.
  if (!_manageSubtabLoaded.has(id)) {
    _manageSubtabLoaded.add(id);
    runManageSubtabLoader(id);
  }
}

function runManageSubtabLoader(id) {
  switch (id) {
    case "status":  return loadStatus();
    case "info":    return loadManageInfo();
    case "plugins": return loadManagePlugins();
    case "jobs":    return loadManageJobs();
    case "tasks":   return refreshManageTasks();
    case "logs":    return refreshManageLogs();
  }
}

// ─── Manage: General info ──────────────────────────────────────────

async function loadManageInfo() {
  const grid = document.getElementById("manageInfo");
  if (!grid) return;
  try {
    const [info, stats] = await Promise.all([
      fetch("/api/info").then(r => r.ok ? r.json() : Promise.reject(r.status)),
      fetch("/api/stats").then(r => r.ok ? r.json() : Promise.reject(r.status)),
    ]);
    // Populate the headline counters / by-source / by-kind cards too —
    // they share #statTotal / #statBySource / etc. with the search-tab
    // counterparts that no longer exist.
    renderStats(stats);

    const rows = [
      ["Vector DB",      `${info.vector_db.type} · ${info.vector_db.persist_dir} · ${info.vector_db.collection}`],
      ["Embeddings",     `${info.embeddings.provider} · ${info.embeddings.provider === "openai" ? info.embeddings.openai_model : info.embeddings.local_model}`],
      ["LLM",            `${info.llm.provider}${info.llm.model ? " · " + info.llm.model : ""}`],
      ["Web",            `${info.web.host}:${info.web.port}`],
      ["Logs",           info.logs.file || "(disabled)"],
    ];
    grid.innerHTML = rows.map(([k, v]) =>
      `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(String(v))}</dd>`
    ).join("");
  } catch (e) {
    grid.innerHTML = `<dt>Error</dt><dd>${escapeHtml(String(e))}</dd>`;
  }
}

// ─── Manage: Plugins ───────────────────────────────────────────────

async function loadManagePlugins() {
  const host = document.getElementById("managePlugins");
  if (!host) return;
  host.innerHTML = `<p class="hint-text">Loading…</p>`;
  try {
    const r = await fetch("/api/plugins");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    host.innerHTML = renderPluginGroups(data);
  } catch (e) {
    host.innerHTML = `<p class="hint-text">Failed to load: ${escapeHtml(e.message)}</p>`;
  }
}

function renderPluginGroups(d) {
  const sections = [];

  sections.push(pluginGroup("Sources", d.sources.map(s => ({
    name:   s.name,
    badges: [
      s.available ? `<span class="plugin-badge ok">available</span>`
                  : `<span class="plugin-badge warn" title="${escapeHtml(s.hint || '')}">unavailable</span>`,
    ],
    sub: s.module,
  }))));

  sections.push(pluginGroup("Enrichers", d.enrichers.map(e => ({
    name:   e.name,
    badges: e.disabled ? [`<span class="plugin-badge warn">disabled</span>`] : [],
    sub:    e.module,
  }))));

  sections.push(pluginGroup("Tab contributions", d.tabs.map(t => ({
    name:   `${t.icon || "·"} ${t.label || t.id}`,
    badges: [`<span class="plugin-badge">order ${t.order}</span>`],
    sub:    `${t.plugin}/${t.module || "(no module)"}`,
  }))));

  return sections.join("");
}

function pluginGroup(title, rows) {
  if (!rows.length) {
    return `<section class="plugin-group">
      <h4>${escapeHtml(title)}</h4>
      <p class="hint-text">— none —</p>
    </section>`;
  }
  const list = rows.map(r => `
    <li class="plugin-row">
      <div class="plugin-main">
        <span class="plugin-name">${escapeHtml(r.name)}</span>
        ${r.badges.join("")}
      </div>
      <div class="plugin-sub">${escapeHtml(r.sub || "")}</div>
    </li>`).join("");
  return `<section class="plugin-group">
    <h4>${escapeHtml(title)}</h4>
    <ul class="plugin-list">${list}</ul>
  </section>`;
}

// ─── Manage: Logs viewer ───────────────────────────────────────────

let _logsFollowTimer = null;

function initManageLogs() {
  document.getElementById("logsRefresh")
          ?.addEventListener("click", refreshManageLogs);
  document.getElementById("logsFile")
          ?.addEventListener("change", refreshManageLogs);
  document.getElementById("logsTail")
          ?.addEventListener("change", refreshManageLogs);
  document.getElementById("logsFollow")
          ?.addEventListener("change", (e) => {
            if (e.target.checked) startLogsFollow();
            else stopLogsFollow();
          });
}

function stopLogsFollow() {
  if (_logsFollowTimer) {
    clearInterval(_logsFollowTimer);
    _logsFollowTimer = null;
  }
}

function startLogsFollow() {
  stopLogsFollow();
  _logsFollowTimer = setInterval(refreshManageLogs, 2000);
}

async function refreshManageLogs() {
  const sel    = document.getElementById("logsFile");
  const tail   = document.getElementById("logsTail");
  const meta   = document.getElementById("logsMeta");
  const viewer = document.getElementById("logsViewer");
  if (!sel || !viewer) return;

  // Populate file dropdown the first time + when files appear/rotate.
  try {
    const files = await fetch("/api/logs").then(r => r.ok ? r.json() : []);
    const desired = sel.value || files[0]?.name || "";
    if (sel.options.length !== files.length
        || [...sel.options].some((o, i) => o.value !== files[i]?.name)) {
      sel.innerHTML = files.map(f =>
        `<option value="${escapeHtml(f.name)}">${escapeHtml(f.name)} ` +
        `(${formatBytes(f.size)})</option>`
      ).join("");
      sel.value = desired || files[0]?.name || "";
    }
    if (!sel.value) {
      viewer.innerHTML = `<span class="hint-text">No log files yet.</span>`;
      meta.textContent = "";
      return;
    }
  } catch {
    viewer.innerHTML = `<span class="hint-text">Failed to list logs.</span>`;
    return;
  }

  const n = Math.max(1, Math.min(parseInt(tail.value, 10) || 500, 5000));
  try {
    const r = await fetch(`/api/logs/${encodeURIComponent(sel.value)}?tail=${n}`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    meta.textContent = `${data.lines.length} lines · ${formatBytes(data.size)} · ` +
                       new Date(data.mtime * 1000).toLocaleString();
    viewer.innerHTML = data.lines.map(renderLogLine).join("");
    viewer.scrollTop = viewer.scrollHeight;
  } catch (e) {
    viewer.innerHTML = `<span class="hint-text">Failed to load: ${escapeHtml(e.message)}</span>`;
  }
}

// Render one log line. Booki writes JSON-formatted log records by default
// (see core/logs.py:JsonFormatter); we parse each line and color the parts.
// Lines that aren't valid JSON (human-format, partial writes, plain text)
// fall back to a single raw row.
function renderLogLine(line) {
  if (!line) return "";
  let parsed = null;
  if (line.startsWith("{")) {
    try { parsed = JSON.parse(line); }
    catch { /* fall through to raw */ }
  }
  if (!parsed || typeof parsed !== "object") {
    return `<div class="log-line log-raw">${escapeHtml(line)}</div>`;
  }
  const { ts = "", level = "", logger = "", msg = "", exc_info, ...extras } = parsed;
  const lvl  = String(level || "").toUpperCase();
  const slug = lvl.toLowerCase() || "x";

  let extraStr = "";
  for (const [k, v] of Object.entries(extras)) {
    if (v === null || v === undefined || v === "") continue;
    let val;
    if (typeof v === "string") val = v;
    else { try { val = JSON.stringify(v); } catch { val = String(v); } }
    extraStr += ` <span class="log-key">${escapeHtml(k)}</span>` +
                `=<span class="log-val">${escapeHtml(val)}</span>`;
  }

  const excHtml = exc_info
    ? `<pre class="log-exc">${escapeHtml(String(exc_info))}</pre>`
    : "";

  return `<div class="log-line log-lvl-${slug}">` +
           `<span class="log-ts">${escapeHtml(ts)}</span>` +
           `<span class="log-lvl">${escapeHtml(lvl)}</span>` +
           `<span class="log-logger">${escapeHtml(logger)}</span>` +
           `<span class="log-msg">${escapeHtml(String(msg))}</span>` +
           (extraStr ? `<span class="log-extras">${extraStr}</span>` : "") +
           excHtml +
         `</div>`;
}

function formatBytes(n) {
  if (!n && n !== 0) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

// ─── Plugin-tab loader ─────────────────────────────────────────────

async function loadPluginTabs() {
  let tabs = [];
  try {
    const r = await fetch("/api/tabs");
    if (!r.ok) return;
    tabs = await r.json();
  } catch (e) {
    console.warn("[booki] /api/tabs failed:", e);
    return;
  }
  // Register metadata first so the bar renders before modules finish.
  // Behavior gets filled in by `Tabs.implement(id, ...)` from each module.
  for (const t of tabs) {
    Tabs.register({
      id: t.id, label: t.label, icon: t.icon, order: t.order,
      mount(el) {
        el.innerHTML = `<p class="tab-stub">Loading plugin tab “${escapeHtml(t.label || t.id)}”…</p>`;
      },
    });
    for (const href of t.style_urls || []) {
      const link = document.createElement("link");
      link.rel = "stylesheet";
      link.href = href;
      document.head.appendChild(link);
    }
  }
  await Promise.all((tabs || []).map(async (t) => {
    if (!t.module_url) return;
    try { await import(t.module_url); }
    catch (e) { console.warn(`[booki] tab module failed: ${t.id}`, e); }
  }));
}

// ─── Search input wiring ───────────────────────────────────────────

els.findInput.addEventListener("input", applyFilter);

// ─── Advanced search wiring ────────────────────────────────────────
// All event handling lives in mountAdvancedSearch(); each tab mounts its own
// instance and they update their own scope via notifyAdvChange(scope).

els.fuzzyToggle.addEventListener("click", () => {
  state.fuzzy = !state.fuzzy;
  els.fuzzyToggle.setAttribute("aria-checked", String(state.fuzzy));
  document.getElementById("fuzzyState").textContent = state.fuzzy ? "on" : "off";
  els.findInput.placeholder = state.fuzzy
    ? "Fuzzy search title or URL… (press /)"
    : "Search title or URL… (press /)";
  applyFilter();
  els.findInput.focus();
});

els.askInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); runAsk(); }
});

// Cached results from the most recent /api/ask call so the view-mode
// toggle can re-render without re-querying the LLM.
let _askLastResults = [];

async function runAsk() {
  const q = els.askInput.value.trim();
  if (!q) return;
  els.askAnswer.textContent = "";
  els.askSources.innerHTML = "";
  els.askStatus.textContent = els.useLlm.checked
    ? "Searching + asking the LLM…"
    : "Searching…";

  try {
    const r = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        query: q,
        n: 8,
        min_importance: 0,
        use_llm: els.useLlm.checked,
      }),
    });
    if (!r.ok) {
      const text = await r.text();
      throw new Error(`${r.status}: ${text}`);
    }
    const data = await r.json();
    if (data.answer) {
      els.askAnswer.textContent = data.answer;
      els.askStatus.textContent = `${data.bookmarks.length} results · ${data.provider}/${data.model}`;
    } else {
      els.askStatus.textContent = `${data.bookmarks.length} results`;
    }
    _askLastResults = data.bookmarks.map((bm) => {
      // Resolve to the full bookmark so renderers have tags / kind / etc.
      const full = state.all.find(x => (x.url || "").replace(/\/$/,"").toLowerCase() ===
                                        (bm.url || "").replace(/\/$/,"").toLowerCase())
                   || { ...bm, id: bm.url, has_summary: !!bm.summary,
                        tags: (bm.tags || "").split(", ").filter(Boolean) };
      return { bm: full, score: bm._score };
    });
    _rerenderAskSources();
  } catch (err) {
    els.askStatus.textContent = "";
    els.askAnswer.textContent = `Error: ${err.message}`;
    _askLastResults = [];
  }
}

function _rerenderAskSources() {
  // Ask is list-only — the synthesized answer + ranked sources flow as
  // a reading experience, not a browsing one, so the list/grid/table
  // toggle would just add noise. Each source row renders with its
  // vector-score chip via renderRow.
  const host = els.askSources;
  if (!host) return;
  host.className = "results";
  host.innerHTML = "";
  if (!_askLastResults.length) return;
  const frag = document.createDocumentFragment();
  for (const { bm, score } of _askLastResults) {
    frag.appendChild(renderRow(
      { bm, titleMatches: [], urlMatches: [], vectorScore: score },
      false
    ));
  }
  host.replaceChildren(frag);
}

// ─── Add link ──────────────────────────────────────────────────────

const addLinkForm = $("addLinkForm");
const addLinkUrl = $("addLinkUrl");
const addLinkBtn = $("addLinkBtn");

addLinkForm?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const url = addLinkUrl.value.trim();
  if (!url) return;
  addLinkBtn.disabled = true;
  try {
    const r = await fetch("/api/link", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${r.status}`);
    }
    const data = await r.json();
    addLinkUrl.value = "";
    showToast(data.is_new ? `Added: ${data.title}` : `Updated: ${data.title}`);
    await loadBookmarks();
    loadStats();
  } catch (err) {
    showToast(`Add failed: ${err.message}`);
  } finally {
    addLinkBtn.disabled = false;
  }
});

// ─── System status ──────────────────────────────────────────────────
// Lives inside the Manage tab (Stage 6). Element refs are populated when
// that tab mounts; renderStatus / loadStatus / renderCheckRow read through
// `statusEls`, so they keep working unchanged.

const statusEls = {
  refresh: null,
  platform: null,
  summary: null,
  body: null,
};

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => (
    {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]
  ));
}

function copyToClipboard(text, btn) {
  const done = () => {
    if (!btn) return;
    const prev = btn.textContent;
    btn.textContent = "✓ Copied";
    btn.classList.add("copied");
    setTimeout(() => { btn.textContent = prev; btn.classList.remove("copied"); }, 1400);
  };
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(() => fallback());
  } else {
    fallback();
  }
  function fallback() {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); done(); } catch (_) {}
    document.body.removeChild(ta);
  }
}

function renderStatus(payload) {
  const { platform, checks, summary } = payload;
  statusEls.platform.textContent =
    `${platform.system} ${platform.release} · Python ${platform.python} · ` +
    `package manager: ${platform.package_manager}`;

  const pills = [
    `<span class="status-pill ok">✓ ${summary.ok}/${summary.total} installed</span>`,
  ];
  if (summary.missing_required) {
    pills.push(`<span class="status-pill bad">✗ ${summary.missing_required} required missing</span>`);
  }
  if (summary.missing_optional) {
    pills.push(`<span class="status-pill warn">⚠ ${summary.missing_optional} optional missing</span>`);
  }
  statusEls.summary.innerHTML = pills.join("");

  // Group by category, preserving server order.
  const groups = new Map();
  for (const c of checks) {
    if (!groups.has(c.category)) groups.set(c.category, []);
    groups.get(c.category).push(c);
  }

  const sections = [];
  for (const [cat, items] of groups) {
    const rows = items.map(renderCheckRow).join("");
    sections.push(`
      <section class="status-group">
        <h3 class="status-group-title">${escapeHtml(cat)}</h3>
        <ul class="status-list">${rows}</ul>
      </section>
    `);
  }
  statusEls.body.innerHTML = sections.join("");

  // Wire copy buttons.
  statusEls.body.querySelectorAll("[data-copy]").forEach(btn => {
    btn.addEventListener("click", () => copyToClipboard(btn.dataset.copy, btn));
  });
}

function renderCheckRow(c) {
  const icon = c.ok
    ? `<span class="check-icon ok" title="installed">✓</span>`
    : (c.required
        ? `<span class="check-icon bad" title="required, missing">✗</span>`
        : `<span class="check-icon warn" title="optional, missing">○</span>`);

  const tag = c.required
    ? `<span class="check-tag req">required</span>`
    : `<span class="check-tag opt">optional</span>`;

  let install = "";
  if (!c.ok) {
    const cmd = c.fix_command;
    const altLines = Object.entries(c.install || {})
      .filter(([k, v]) => v && v !== cmd)
      .map(([k, v]) =>
        `<li><span class="alt-pm">${escapeHtml(k)}</span>` +
        `<code>${escapeHtml(v)}</code>` +
        `<button type="button" class="copy-btn small" data-copy="${escapeHtml(v)}">Copy</button></li>`
      ).join("");
    install = `
      <div class="check-install">
        ${cmd ? `
          <div class="install-primary">
            <code>${escapeHtml(cmd)}</code>
            <button type="button" class="copy-btn" data-copy="${escapeHtml(cmd)}">📋 Copy</button>
          </div>` : ""}
        ${altLines ? `<details class="install-alts"><summary>Other options</summary><ul>${altLines}</ul></details>` : ""}
        ${c.docs_url ? `<a class="check-docs" href="${escapeHtml(c.docs_url)}" target="_blank" rel="noopener">Docs ↗</a>` : ""}
      </div>
    `;
  }

  return `
    <li class="check-row ${c.ok ? "is-ok" : (c.required ? "is-bad" : "is-warn")}">
      ${icon}
      <div class="check-main">
        <div class="check-head">
          <span class="check-label">${escapeHtml(c.label)}</span>
          ${tag}
          <span class="check-detail">${escapeHtml(c.detail)}</span>
        </div>
        <p class="check-feature">${escapeHtml(c.feature)}</p>
        ${install}
      </div>
    </li>
  `;
}

async function loadStatus() {
  if (!statusEls.body) return;   // Manage tab not mounted yet.
  statusEls.body.innerHTML = `<p class="hint-text">Running checks…</p>`;
  try {
    const r = await fetch("/api/status");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    renderStatus(data);
  } catch (e) {
    statusEls.body.innerHTML = `<p class="hint-text">Failed to load: ${escapeHtml(e.message)}</p>`;
  }
}

// ─── Boot ──────────────────────────────────────────────────────────

loadAdvFromStorage();

loadPluginTabs().finally(() => {
  // Resolve initial tab: hash fragment > stored > default.
  const hash = (location.hash || "").replace(/^#/, "");
  let stored = "";
  try { stored = localStorage.getItem("booki.tab") || ""; } catch {}
  const initial = (hash && Tabs.get(hash))   ? hash
                : (stored && Tabs.get(stored)) ? stored
                : "search";
  Tabs.activate(initial);
});

window.addEventListener("hashchange", () => {
  const h = (location.hash || "").replace(/^#/, "");
  if (h && Tabs.get(h)) Tabs.activate(h);
});

loadKinds();
loadSchema().then(loadBookmarks).then(loadStats).catch(err => {
  els.count.textContent = `Error: ${err.message}`;
});


// ─── Export wizard ──────────────────────────────────────────────────
//
// Topbar "⬇ Export" button → inline panel mounted at the top of the
// active tab's content → 4-step wizard:
//   1. Pick exporter (filtered by active tab's kind; ✈️ marks background)
//   2. Pick theme + fill theme vars (skipped when exporter declines themes)
//   3. Fill exporter-specific options
//   4. Preview the output, then ▶ Run / ✈️ Queue
//
// The panel is cloned from <template id="exportPanelTpl">, prepended into
// the currently-active tab's content area, and removed on close.
//
// Selection comes from the active tab's getSelection() hook (declared on
// each Tabs.register call). Tabs that don't declare one fall through to
// the empty selection and the button stays disabled.

let exportPanelEl = null;
let exportState = _emptyExportState();
let _colorSchemesCache = null;       // module-scoped — survives panel reopens

function _emptyExportState() {
  return {
    step: 1,                  // 1 | 2 | 3 | 4
    kind: "any",
    itemIds: [],
    exporters: [],
    selectedExporter: null,
    themes: [],
    selectedTheme: null,
    themeVars: {},
    options: {},
    // Refine-step state
    treeGrouping: "none",     // none | tag | kind | source | list | path | importance
    tree: null,               // null = not yet built; [] = empty tree
    treeDirty: false,         // user dragged/edited → don't auto-rebuild on grouping
    treeSeq: 0,               // monotonic id seed for new nodes
    preview: null,            // {kind, content?, mime?, filename?, manifest?}
    previewBytes: null,       // bytes the run will reuse for immediate exporters
  };
}

function idsFromContainer(selector) {
  const root = document.querySelector(selector);
  if (!root) return [];
  return Array.from(root.querySelectorAll("[data-id]")).map(el => el.dataset.id);
}

function getActiveSelection() {
  const id = Tabs.current();
  if (!id) return { kind: "any", ids: [] };
  const spec = Tabs.get(id);
  if (spec && typeof spec.getSelection === "function") {
    try { return spec.getSelection() || { kind: "any", ids: [] }; }
    catch (e) { console.error(e); }
  }
  return { kind: "any", ids: [] };
}

function refreshExportButton() {
  const btn = document.getElementById("openExportBtn");
  const lbl = document.getElementById("exportBtnLabel");
  if (!btn || !lbl) return;
  const sel = getActiveSelection();
  if (!sel.ids.length) {
    btn.disabled = true;
    lbl.textContent = "Export";
  } else {
    btn.disabled = false;
    lbl.textContent = `Export ${sel.ids.length} ${sel.ids.length === 1 ? "item" : "items"}`;
  }
}

// Re-render the button label whenever results re-render. Tabs already call
// refreshExportButton() in their onShow; this MutationObserver covers the
// case where a tab's filter input fires synchronous DOM updates.
function _observeExportRoots() {
  const roots = ["#results", "#photoGrid", "#videoGrid", "#askSources", "#docResults"];
  for (const sel of roots) {
    const node = document.querySelector(sel);
    if (!node) continue;
    new MutationObserver(refreshExportButton).observe(node, { childList: true });
  }
}
document.addEventListener("DOMContentLoaded", _observeExportRoots);

function _exportMountTarget() {
  // Prefer the search tab's main-content area (skip the sidebar).
  const inSearchMain = document.querySelector(
    ".tab-panel[data-panel='search'].active .main-content");
  if (inSearchMain) return inSearchMain;
  // Photos / Videos tabs wrap content in .scoped-tab, which carries the
  // max-width + auto margins — the .tab-panel itself spans the viewport.
  const scoped = document.querySelector(".tab-panel.active .scoped-tab");
  if (scoped) return scoped;
  const active = document.querySelector(".tab-panel.active");
  return active || document.body;
}

function openExport() {
  closeExport();
  const sel = getActiveSelection();
  if (!sel.ids.length) return;

  const tpl = document.getElementById("exportPanelTpl");
  exportPanelEl = tpl.content.firstElementChild.cloneNode(true);
  exportPanelEl.querySelector("#exportPanelClose")
    .addEventListener("click", closeExport);
  exportPanelEl.querySelector("#exportNextBtn")
    .addEventListener("click", _onExportNext);
  exportPanelEl.querySelector("#exportBackBtn")
    .addEventListener("click", _onExportBack);
  exportPanelEl.querySelector("#exportStepnav")
    .addEventListener("click", _onExportStepnav);

  const host = _exportMountTarget();
  host.prepend(exportPanelEl);

  exportState = _emptyExportState();
  exportState.kind = sel.kind || "any";
  exportState.itemIds = sel.ids.slice();
  document.getElementById("exportItemCount").textContent =
    `· ${sel.ids.length} ${sel.ids.length === 1 ? "item" : "items"}`;
  document.getElementById("exportStatus").textContent = "";
  loadExporters().then(renderExportStep);
  exportPanelEl.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function closeExport() {
  if (exportPanelEl) {
    exportPanelEl.remove();
    exportPanelEl = null;
  }
}

async function loadExporters() {
  const r = await fetch(`/api/export/exporters?kind=${encodeURIComponent(exportState.kind)}`);
  exportState.exporters = r.ok ? await r.json() : [];
}

function renderExportStep() {
  if (!exportPanelEl) return;
  exportPanelEl.querySelectorAll(".export-step").forEach(el => {
    el.classList.toggle("active", el.dataset.step === String(exportState.step));
  });
  exportPanelEl.querySelectorAll("#exportStepnav .step-pill").forEach(el => {
    el.classList.toggle("active", el.dataset.step === String(exportState.step));
  });
  document.getElementById("exportBackBtn").disabled = exportState.step === 1;
  const next = document.getElementById("exportNextBtn");
  if (exportState.step === 4) {
    const e = exportState.selectedExporter;
    next.textContent = e?.execution_mode === "background" ? "✈️ Queue" : "▶ Run";
  } else {
    next.textContent = "Next →";
  }
  next.disabled = false;

  if (exportState.step === 1) renderExporterList();
  else if (exportState.step === 2) renderOptionsStep();
  else if (exportState.step === 3) renderRefineStep();
  else if (exportState.step === 4) renderPreviewStep();
}

function renderExporterList() {
  const host = document.getElementById("exporterList");
  host.innerHTML = "";
  _renderExporterNotes();
  if (!exportState.exporters.length) {
    host.innerHTML = `<p class="hint-text">No exporters available for this view.</p>`;
    return;
  }
  for (const e of exportState.exporters) {
    const li = document.createElement("li");
    li.className = "exporter-item";
    if (exportState.selectedExporter?.slug === e.slug) li.classList.add("selected");
    const bg = e.execution_mode === "background"
      ? `<span class="bg-badge" title="Runs in the background">✈️</span>` : "";
    li.innerHTML = `
      <div class="exporter-name">${escapeHtml(e.name)} ${bg}</div>
      <div class="exporter-desc">${escapeHtml(e.description || "")}</div>`;
    li.addEventListener("click", () => {
      exportState.selectedExporter = e;
      // Reset downstream state
      exportState.selectedTheme = null;
      exportState.themeVars = {};
      exportState.options = {};
      exportState.tree = null;
      exportState.treeDirty = false;
      exportState.preview = null;
      exportState.previewBytes = null;
      for (const o of e.options_schema || []) {
        if (o.default !== undefined) exportState.options[o.name] = o.default;
      }
      renderExporterList();
    });
    host.appendChild(li);
  }
}

function _renderExporterNotes() {
  const host = document.getElementById("exporterNotes");
  if (!host) return;
  const notes = exportState.selectedExporter?.runtime_notes || [];
  host.innerHTML = "";
  for (const n of notes) {
    const div = document.createElement("div");
    const level = (n.level === "warning" || n.level === "info") ? n.level : "info";
    div.className = `exporter-note ${level}`;
    // Notes may contain inline <code>; whitelist nothing else.
    div.innerHTML = _renderNoteText(n.text || "");
    host.appendChild(div);
  }
}

function _renderNoteText(text) {
  // Allow `inline code` and **bold**. Escape first, then re-mark — the
  // captured groups are already HTML-safe so we don't escape twice.
  const safe = escapeHtml(text);
  return safe
    .replace(/`([^`]+)`/g, (_, c) => `<code>${c}</code>`)
    .replace(/\*\*([^*]+)\*\*/g, (_, c) => `<strong>${c}</strong>`);
}

// Lazy-load themes for the active exporter and seed sensible defaults for
// the picker (first theme, defaults from theme.toml). Returns the resolved
// themes so callers can fall through to "no themes" UX.
async function _ensureThemesLoaded() {
  const e = exportState.selectedExporter;
  if (!e || !e.uses_themes) return [];
  if (!exportState.themes.length) {
    const r = await fetch(`/api/export/themes?exporter=${encodeURIComponent(e.slug)}`);
    exportState.themes = r.ok ? await r.json() : [];
  }
  if (exportState.themes.length && !exportState.selectedTheme) {
    exportState.selectedTheme = exportState.themes[0];
    exportState.themeVars = {};
    exportState.selectedScheme = null;
    for (const v of exportState.selectedTheme.vars || []) {
      exportState.themeVars[v.name] = v.default;
    }
  }
  return exportState.themes;
}

// Render the theme picker + var inputs into `host`. `onChange` runs every
// time the user picks a different theme, swaps a color scheme, or edits an
// individual var — the preview step uses it to debounce-refetch the preview.
async function _renderThemeControls(host, onChange) {
  host.innerHTML = "";
  const e = exportState.selectedExporter;
  if (!e || !e.uses_themes) return;

  const themes = await _ensureThemesLoaded();
  if (!themes.length) {
    host.innerHTML = `<p class="hint-text">No themes installed for this exporter.</p>`;
    return;
  }

  const kind = e.applicable_kinds?.[0] || "any";
  const list = document.createElement("div");
  list.className = "theme-list";
  for (const t of themes) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "theme-item";
    if (exportState.selectedTheme.slug === t.slug) btn.classList.add("selected");
    const thumb = t.has_thumbnail
      ? `<img class="theme-thumb"
              src="/api/export/themes/${encodeURIComponent(kind)}/${encodeURIComponent(t.slug)}/thumbnail"
              alt="" loading="lazy">`
      : `<div class="theme-thumb theme-thumb--blank"></div>`;
    btn.innerHTML = `
      ${thumb}
      <div class="theme-meta">
        <div class="theme-name">${escapeHtml(t.label)}</div>
        <div class="theme-desc">${escapeHtml(t.description || "")}</div>
      </div>`;
    btn.addEventListener("click", () => {
      exportState.selectedTheme = t;
      exportState.themeVars = {};
      exportState.selectedScheme = null;
      for (const v of t.vars || []) exportState.themeVars[v.name] = v.default;
      _renderThemeControls(host, onChange);
      onChange?.();
    });
    list.appendChild(btn);
  }
  host.appendChild(list);

  const theme = exportState.selectedTheme;
  if (!theme?.vars?.length) return;

  const vars_ = document.createElement("div");
  vars_.className = "theme-vars";
  const colorVarNames = theme.vars.filter(v => v.type === "color").map(v => v.name);
  if (colorVarNames.length) {
    await _ensureColorSchemes();
    // First "scheme" is always the theme's own theme.toml defaults — gives
    // the user a one-click revert to the theme's intended palette.
    const themeDefault = {
      slug: `__theme_default__:${theme.slug}`,
      name: `${theme.label} default`,
      description: "Original colors from theme.toml",
      colors: Object.fromEntries(
        theme.vars.filter(v => v.type === "color").map(v => [v.name, v.default])
      ),
    };
    const matching = [themeDefault].concat(
      exportState.colorSchemes.filter(s =>
        colorVarNames.some(n => s.colors && n in s.colors))
    );
    vars_.appendChild(_renderColorSchemePicker(matching, colorVarNames, () => {
      // Re-render so the color inputs reflect the new values, then notify.
      _renderThemeControls(host, onChange);
      onChange?.();
    }));
  }

  const form = document.createElement("div");
  form.className = "tv-form";
  for (const v of theme.vars) {
    form.appendChild(_renderField({
      kind: "tv",
      spec: v,
      value: exportState.themeVars[v.name],
      onChange: (val) => {
        exportState.themeVars[v.name] = val;
        onChange?.();
      },
    }));
  }
  vars_.appendChild(form);
  host.appendChild(vars_);
}

async function _ensureColorSchemes() {
  if (_colorSchemesCache !== null) {
    exportState.colorSchemes = _colorSchemesCache;
    return;
  }
  try {
    const r = await fetch("/api/export/colorschemes");
    _colorSchemesCache = r.ok ? await r.json() : [];
  } catch {
    _colorSchemesCache = [];
  }
  exportState.colorSchemes = _colorSchemesCache;
}

function _renderColorSchemePicker(schemes, colorVarNames, onPick) {
  // Default to the first scheme on initial render — but only paint its
  // colors into the form once, so re-renders (e.g. after a manual color
  // edit) don't clobber what the user typed.
  if (!exportState.selectedScheme) {
    exportState.selectedScheme = schemes[0].slug;
    _applyScheme(schemes[0], colorVarNames);
  }
  const current = schemes.find(s => s.slug === exportState.selectedScheme) || schemes[0];

  const wrap = document.createElement("div");
  wrap.className = "scheme-picker";
  wrap.innerHTML = `
    <label class="scheme-picker-label">Color scheme</label>
    <button type="button" class="scheme-trigger" aria-haspopup="listbox" aria-expanded="false">
      ${_swatchesHtml(current)}
      <span class="scheme-trigger-name">${escapeHtml(current.name)}</span>
      <span class="scheme-caret" aria-hidden="true">▾</span>
    </button>
    <ul class="scheme-options" role="listbox" hidden>
      ${schemes.map(s => `
        <li role="option"
            class="scheme-option ${s.slug === current.slug ? 'is-selected' : ''}"
            data-slug="${escapeHtml(s.slug)}">
          ${_swatchesHtml(s)}
          <span class="scheme-option-name">${escapeHtml(s.name)}</span>
        </li>`).join("")}
    </ul>
    <p class="hint-text">Picks fill the colors below — you can still tweak each one by hand.</p>`;

  const trigger = wrap.querySelector(".scheme-trigger");
  const list = wrap.querySelector(".scheme-options");

  function close() {
    list.hidden = true;
    trigger.setAttribute("aria-expanded", "false");
    document.removeEventListener("click", onDocClick, true);
  }
  function onDocClick(ev) {
    if (!wrap.contains(ev.target)) close();
  }
  trigger.addEventListener("click", () => {
    const willOpen = list.hidden;
    list.hidden = !willOpen;
    trigger.setAttribute("aria-expanded", String(willOpen));
    if (willOpen) document.addEventListener("click", onDocClick, true);
    else document.removeEventListener("click", onDocClick, true);
  });
  list.addEventListener("click", (ev) => {
    const li = ev.target.closest(".scheme-option");
    if (!li) return;
    const slug = li.dataset.slug;
    const scheme = schemes.find(s => s.slug === slug);
    if (!scheme) return;
    exportState.selectedScheme = slug;
    _applyScheme(scheme, colorVarNames);
    close();
    onPick?.();
  });
  return wrap;
}

function _applyScheme(scheme, colorVarNames) {
  for (const name of colorVarNames) {
    const c = scheme.colors?.[name];
    if (c) exportState.themeVars[name] = c;
  }
}

// Canonical order of role colors a scheme may provide. The picker renders
// swatches in this order regardless of which roles the theme actually
// consumes — so a scheme's character (e.g. "Tokyo Night uses cyan + green
// + yellow + red") is visible in the dropdown even when the theme only
// hooks up bg/text/link/accent.
const SCHEME_ROLE_ORDER = [
  "bg", "text", "link", "accent",
  "secondary", "muted", "success", "warning", "danger",
];

function _swatchesHtml(scheme) {
  const cells = SCHEME_ROLE_ORDER
    .map(role => scheme.colors?.[role])
    .filter(Boolean)
    .map(c => `<span class="scheme-swatch" style="background:${escapeHtml(c)}"></span>`)
    .join("");
  return `<span class="scheme-swatches" aria-hidden="true">${cells}</span>`;
}

async function renderOptionsStep() {
  const e = exportState.selectedExporter;
  const host = document.getElementById("optionsForm");
  host.innerHTML = `<p class="hint-text">Loading options…</p>`;
  // Always re-fetch on step 3 entry so plugins with dynamic options
  // (field pickers) see the current item set.
  let schema;
  try {
    const r = await fetch("/api/export/options", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ exporter: e.slug, item_ids: exportState.itemIds }),
    });
    schema = r.ok ? await r.json() : (e.options_schema || []);
  } catch {
    schema = e.options_schema || [];
  }
  host.innerHTML = "";
  if (!schema.length) {
    host.innerHTML = `<p class="hint-text">No options for this exporter — click <strong>Run</strong> to export.</p>`;
    return;
  }
  for (const opt of schema) {
    if (!(opt.name in exportState.options) && opt.default !== undefined) {
      exportState.options[opt.name] = opt.default;
    }
    host.appendChild(_renderField({
      kind: "opt",
      spec: opt,
      value: exportState.options[opt.name],
      onChange: (val) => { exportState.options[opt.name] = val; },
    }));
  }
}

function _renderField({ kind, spec, value, onChange }) {
  const wrap = document.createElement("div");
  wrap.className = kind === "tv" ? "tv" : "opt";
  const id = `${kind}-${spec.name}`;
  const label = spec.label || spec.name.replace(/_/g, " ");

  const make = (html) => { wrap.innerHTML = html; return wrap.querySelector("input,select,textarea"); };

  let inp;
  if (spec.type === "color") {
    inp = make(`<label for="${id}">${escapeHtml(label)}</label>
                <input type="color" id="${id}" value="${escapeHtml(value || "#000000")}">`);
    inp.addEventListener("input", () => onChange(inp.value));
  } else if (spec.type === "bool") {
    wrap.innerHTML = `<label class="check"><input type="checkbox" id="${id}" ${value ? "checked" : ""}> ${escapeHtml(label)}</label>`;
    inp = wrap.querySelector("input");
    inp.addEventListener("change", () => onChange(inp.checked));
  } else if (spec.type === "number") {
    inp = make(`<label for="${id}">${escapeHtml(label)}</label>
                <input type="number" id="${id}" value="${value ?? ""}">`);
    inp.addEventListener("input", () => onChange(inp.value === "" ? null : Number(inp.value)));
  } else if (spec.type === "select") {
    const opts = (spec.options || []).map(o => {
      const s = String(o);
      return `<option value="${escapeHtml(s)}" ${o === value ? "selected" : ""}>${escapeHtml(s)}</option>`;
    }).join("");
    inp = make(`<label for="${id}">${escapeHtml(label)}</label>
                <select id="${id}">${opts}</select>`);
    inp.addEventListener("change", () => onChange(inp.value));
  } else if (spec.type === "multiselect") {
    const checks = (spec.options || []).map(o => {
      const s = String(o);
      const checked = (value || []).includes(o) ? "checked" : "";
      return `<label class="check"><input type="checkbox" value="${escapeHtml(s)}" ${checked}> ${escapeHtml(s)}</label>`;
    }).join("");
    wrap.innerHTML = `<fieldset><legend>${escapeHtml(label)}</legend>${checks}</fieldset>`;
    wrap.querySelectorAll("input[type=checkbox]").forEach(cb => {
      cb.addEventListener("change", () => {
        const vals = [];
        wrap.querySelectorAll("input[type=checkbox]:checked").forEach(c => vals.push(c.value));
        onChange(vals);
      });
    });
  } else {
    inp = make(`<label for="${id}">${escapeHtml(label)}</label>
                <input type="text" id="${id}" value="${escapeHtml(value ?? "")}">`);
    inp.addEventListener("input", () => onChange(inp.value));
  }

  if (spec.help) {
    const hint = document.createElement("p");
    hint.className = "hint-text";
    hint.textContent = spec.help;
    wrap.appendChild(hint);
  }
  return wrap;
}

// ─── Refine step (tree builder + drag-and-drop) ─────────────────────

const REFINE_GROUPINGS = [
  { id: "none",        label: "None (flat list)" },
  { id: "tag",         label: "By tag" },
  { id: "kind",        label: "By kind" },
  { id: "source",      label: "By source" },
  { id: "list",        label: "By list" },
  { id: "path",        label: "By browser folder" },
  { id: "importance",  label: "By importance" },
];

function _newNodeId() { return `n-${++exportState.treeSeq}`; }

function _itemNode(itemId) {
  return { id: _newNodeId(), type: "item", item_id: String(itemId) };
}

function _folderNode(name, children = []) {
  return { id: _newNodeId(), type: "folder", name: String(name), expanded: true, children };
}

function _bmFor(id) {
  return window.booki?.bookmarks?.byId?.(id) || null;
}

function _itemTitle(id) {
  const b = _bmFor(id);
  return (b && (b.title || b.url)) || `(missing ${id})`;
}

function _glyphFor(id) {
  const b = _bmFor(id);
  const k = b?.kind || "bookmark";
  return ({ video: "🎬", photo: "🖼", document: "📄", channel: "📺",
            github: "🐙", file: "📁", podcast: "🎧", article: "📰" })[k] || "🔖";
}

// Build a tree from the active grouping. Items appearing in multiple
// folders (tag/list groupings) are duplicated. Items lacking a value go
// under "(no <field>)".
function _buildTreeFromGrouping(itemIds, grouping) {
  const items = itemIds.map(id => _bmFor(id)).filter(Boolean);
  const missingIds = itemIds.filter(id => !_bmFor(id));

  if (grouping === "none" || !grouping) {
    return [...itemIds.map(_itemNode)];
  }

  const buckets = new Map();      // name → ordered list of itemIds
  const unsorted = [];
  const noKey = `(no ${grouping})`;

  for (const b of items) {
    const keys = _bucketKeys(b, grouping);
    if (!keys.length) {
      buckets.set(noKey, (buckets.get(noKey) || []).concat(b.id));
    } else {
      for (const k of keys) {
        buckets.set(k, (buckets.get(k) || []).concat(b.id));
      }
    }
  }
  for (const mid of missingIds) unsorted.push(mid);

  const folderNames = [...buckets.keys()].sort((a, b) => {
    if (a === noKey) return 1;
    if (b === noKey) return -1;
    return a.localeCompare(b);
  });
  const folders = folderNames.map(name =>
    _folderNode(name, buckets.get(name).map(_itemNode))
  );
  if (unsorted.length) folders.push(_folderNode("(unknown)", unsorted.map(_itemNode)));
  return folders;
}

// For each non-"none" grouping, how many of the selected items would
// actually contribute a folder bucket. Used to hide irrelevant options
// (e.g. "By list" when nothing in the selection has a list).
function _groupingCounts(itemIds) {
  const counts = { tag: 0, kind: 0, source: 0, list: 0, path: 0, importance: 0 };
  for (const id of itemIds) {
    const b = _bmFor(id);
    if (!b) continue;
    if ((b.tags || []).length) counts.tag += 1;
    if ((b.lists || []).length) counts.list += 1;
    if (b.kind) counts.kind += 1;
    if (b.source || (b.sources && b.sources[0])) counts.source += 1;
    if ((b.folder_path || b.browser_path || "").trim()) counts.path += 1;
    // Importance is always defined (default 0) — only count items with
    // a meaningful, non-zero rating; otherwise the option is just a noop.
    if (Number(b.importance || 0) > 0) counts.importance += 1;
  }
  return counts;
}

function _bucketKeys(b, grouping) {
  switch (grouping) {
    case "tag":  return (b.tags || []).map(String).filter(Boolean);
    case "list": return (b.lists || []).map(String).filter(Boolean);
    case "kind": return [String(b.kind || "")].filter(Boolean);
    case "source":
      return [String(b.source || (b.sources && b.sources[0]) || "")].filter(Boolean);
    case "path": {
      const p = String(b.folder_path || b.browser_path || "").trim();
      if (!p) return [];
      return [p];                  // keep as a single bucket name; user can sub-foldering later
    }
    case "importance": {
      const i = Number(b.importance || 0);
      if (i >= 9) return ["★★★ 9–10"];
      if (i >= 7) return ["★★ 7–8"];
      if (i >= 4) return ["★ 4–6"];
      if (i >= 1) return ["1–3"];
      return ["0"];
    }
  }
  return [];
}

async function renderRefineStep() {
  const toolbar = document.getElementById("refineToolbar");
  const hint = document.getElementById("refineHint");
  const host = document.getElementById("refineTree");
  const e = exportState.selectedExporter;
  if (!e || !toolbar || !host) return;

  // Build initial tree if needed (preserve user edits across step changes).
  if (exportState.tree === null) {
    exportState.tree = _buildTreeFromGrouping(exportState.itemIds, exportState.treeGrouping);
  }

  hint.innerHTML = e.supports_hierarchy
    ? `<p class="hint-text">This exporter supports nested folders — they'll appear in the output.</p>`
    : `<p class="hint-text">This exporter is flat — folders below are flattened into ordered items in the output.</p>`;

  // Only surface groupings that would actually create folders for the
  // current selection. "None" is always shown; other strategies are kept
  // when at least one item contributes a bucket key.
  const counts = _groupingCounts(exportState.itemIds);
  const applicable = REFINE_GROUPINGS.filter(g => g.id === "none" || counts[g.id] > 0);
  // If the saved grouping is no longer applicable, fall back to "none".
  if (!applicable.some(g => g.id === exportState.treeGrouping)) {
    exportState.treeGrouping = "none";
  }
  const opts = applicable.map(g => {
    const c = counts[g.id];
    const label = g.id === "none" ? g.label : `${g.label} (${c})`;
    return `<option value="${g.id}" ${g.id === exportState.treeGrouping ? "selected" : ""}>${escapeHtml(label)}</option>`;
  }).join("");
  const dirtyBadge = exportState.treeDirty
    ? `<span class="refine-dirty" title="You've manually edited the tree">edited</span>` : "";
  toolbar.innerHTML = `
    <label class="refine-grouping">
      <span>Auto-group:</span>
      <select id="refineGrouping">${opts}</select>
    </label>
    <button type="button" class="btn small" id="refineNewFolder">+ New folder</button>
    <button type="button" class="btn small" id="refineRebuild" title="Discard manual edits and rebuild from the grouping">↺ Rebuild</button>
    <span class="refine-meta">${exportState.itemIds.length} item${exportState.itemIds.length === 1 ? "" : "s"} selected${dirtyBadge}</span>`;

  toolbar.querySelector("#refineGrouping").addEventListener("change", (ev) => {
    const next = ev.target.value;
    if (exportState.treeDirty
        && !confirm("Switching grouping will discard your manual edits. Continue?")) {
      ev.target.value = exportState.treeGrouping;
      return;
    }
    exportState.treeGrouping = next;
    exportState.tree = _buildTreeFromGrouping(exportState.itemIds, next);
    exportState.treeDirty = false;
    renderRefineStep();
  });
  toolbar.querySelector("#refineNewFolder").addEventListener("click", () => {
    const name = prompt("New folder name:", "New folder");
    if (!name || !name.trim()) return;
    exportState.tree.unshift(_folderNode(name.trim()));
    exportState.treeDirty = true;
    renderRefineStep();
  });
  toolbar.querySelector("#refineRebuild").addEventListener("click", () => {
    if (exportState.treeDirty
        && !confirm("Discard manual edits and rebuild from the grouping?")) return;
    exportState.tree = _buildTreeFromGrouping(exportState.itemIds, exportState.treeGrouping);
    exportState.treeDirty = false;
    renderRefineStep();
  });

  host.innerHTML = "";
  host.appendChild(_renderTreeRoot(exportState.tree));
}

function _renderTreeRoot(tree) {
  const ul = document.createElement("ul");
  ul.className = "tree tree-root";
  ul.dataset.parentId = "ROOT";
  _appendTreeChildren(ul, tree);
  _wireDropZone(ul, null);          // accept drops at the root
  return ul;
}

function _appendTreeChildren(ul, nodes) {
  ul.appendChild(_dropZone(0));
  nodes.forEach((n, i) => {
    ul.appendChild(_renderNode(n));
    ul.appendChild(_dropZone(i + 1));
  });
}

function _dropZone(index) {
  const li = document.createElement("li");
  li.className = "tree-dropzone";
  li.dataset.index = String(index);
  return li;
}

function _renderNode(node) {
  if (node.type === "folder") return _renderFolder(node);
  return _renderItem(node);
}

function _renderFolder(node) {
  const li = document.createElement("li");
  li.className = "tree-folder" + (node.expanded === false ? " collapsed" : "");
  li.dataset.nodeId = node.id;
  li.dataset.nodeType = "folder";
  li.draggable = true;

  const head = document.createElement("div");
  head.className = "tree-folder-head";
  head.innerHTML = `
    <button class="tree-caret" title="Toggle">${node.expanded === false ? "▸" : "▾"}</button>
    <span class="tree-glyph">📁</span>
    <span class="tree-name" tabindex="0">${escapeHtml(node.name || "")}</span>
    <span class="tree-count">${_countItems(node)}</span>
    <span class="tree-actions">
      <button class="tree-btn tree-rename" title="Rename">✎</button>
      <button class="tree-btn tree-remove" title="Remove folder (items inside are also removed)">✕</button>
    </span>`;
  li.appendChild(head);

  head.querySelector(".tree-caret").addEventListener("click", () => {
    node.expanded = node.expanded === false;
    renderRefineStep();
  });
  head.querySelector(".tree-rename").addEventListener("click", () => _renameFolder(node));
  head.querySelector(".tree-name").addEventListener("dblclick", () => _renameFolder(node));
  head.querySelector(".tree-remove").addEventListener("click", () => {
    if (!confirm(`Remove folder "${node.name}" and everything inside?`)) return;
    _removeNode(node.id);
    exportState.treeDirty = true;
    renderRefineStep();
  });

  const ul = document.createElement("ul");
  ul.className = "tree";
  ul.dataset.parentId = node.id;
  _appendTreeChildren(ul, node.children || []);
  li.appendChild(ul);

  // Drop targets: header (into / before-after sliver), inner UL (between-children),
  // and the row itself (drag source).
  _wireDragSource(li, node);
  _wireDropZone(ul, node);
  _wireFolderDrop(li, head, node);
  return li;
}

function _renderItem(node) {
  const li = document.createElement("li");
  li.className = "tree-item";
  li.dataset.nodeId = node.id;
  li.dataset.nodeType = "item";
  li.draggable = true;
  const title = _itemTitle(node.item_id);
  const glyph = _glyphFor(node.item_id);
  li.innerHTML = `
    <span class="tree-grip" aria-hidden="true">⋮⋮</span>
    <span class="tree-glyph">${glyph}</span>
    <span class="tree-name" title="${escapeHtml(title)}">${escapeHtml(title)}</span>
    <span class="tree-actions">
      <button class="tree-btn tree-remove" title="Remove from export">✕</button>
    </span>`;
  li.querySelector(".tree-remove").addEventListener("click", () => {
    _removeNode(node.id);
    exportState.treeDirty = true;
    renderRefineStep();
  });
  _wireDragSource(li, node);
  _wireItemDrop(li, node);
  return li;
}

function _renameFolder(node) {
  const name = prompt("Rename folder:", node.name || "");
  if (name === null) return;
  node.name = name.trim() || node.name;
  exportState.treeDirty = true;
  renderRefineStep();
}

function _countItems(node) {
  let n = 0;
  (function walk(x) {
    if (!x) return;
    if (x.type === "item") n += 1;
    else (x.children || []).forEach(walk);
  })(node);
  return n;
}

// ── tree mutations ─────────────────────────────────────────────────

function _findNode(id, nodes = exportState.tree, parent = null, parentList = exportState.tree) {
  for (let i = 0; i < (nodes || []).length; i++) {
    const n = nodes[i];
    if (n.id === id) return { node: n, parent, parentList: nodes, index: i };
    if (n.type === "folder") {
      const hit = _findNode(id, n.children || [], n, n.children || (n.children = []));
      if (hit) return hit;
    }
  }
  return null;
}

function _removeNode(id) {
  const hit = _findNode(id);
  if (!hit) return;
  hit.parentList.splice(hit.index, 1);
}

function _isDescendant(folderId, candidateId) {
  // Disallow dropping a folder into itself or its own subtree.
  const hit = _findNode(folderId);
  if (!hit) return false;
  let found = false;
  (function walk(x) {
    if (found || !x) return;
    if (x.id === candidateId) { found = true; return; }
    if (x.type === "folder") (x.children || []).forEach(walk);
  })(hit.node);
  return found;
}

function _moveNode(sourceId, targetParentId, targetIndex) {
  if (sourceId === targetParentId) return false;
  if (targetParentId && _isDescendant(sourceId, targetParentId)) return false;

  const src = _findNode(sourceId);
  if (!src) return false;
  // Determine the destination list.
  let destList;
  if (targetParentId === null || targetParentId === "ROOT") {
    destList = exportState.tree;
  } else {
    const dst = _findNode(targetParentId);
    if (!dst || dst.node.type !== "folder") return false;
    if (!Array.isArray(dst.node.children)) dst.node.children = [];
    destList = dst.node.children;
  }
  // Splice out, then adjust target index if removal happened in same list above target.
  let idx = targetIndex;
  if (src.parentList === destList && src.index < idx) idx -= 1;
  src.parentList.splice(src.index, 1);
  destList.splice(Math.min(Math.max(idx, 0), destList.length), 0, src.node);
  return true;
}

// ── drag and drop wiring ───────────────────────────────────────────
//
// Three drop targets per row to make aiming forgiving:
//   1. Inter-row drop zones (the thin <li class=tree-dropzone> slots)
//      inflate while a drag is in progress, so the gap is visibly clickable.
//   2. Item rows themselves accept drops — the cursor's Y position decides
//      "before" (top half) vs "after" (bottom half).
//   3. Folder headers' top/bottom 25% slivers act as "before/after the folder",
//      the middle 50% drops INTO the folder (existing behavior).

let _refineDragId = null;

function _clearDropHints() {
  document.querySelectorAll(
    ".tree-dropzone.over, .tree-folder-head.over, .tree-item.over-before, .tree-item.over-after, .tree-folder.over-before, .tree-folder.over-after"
  ).forEach(n => n.classList.remove("over", "over-before", "over-after"));
}

function _wireDragSource(el, node) {
  el.addEventListener("dragstart", (ev) => {
    _refineDragId = node.id;
    ev.dataTransfer.effectAllowed = "move";
    try { ev.dataTransfer.setData("text/plain", node.id); } catch {}
    el.classList.add("dragging");
    document.body.classList.add("refine-dragging");
    ev.stopPropagation();
  });
  el.addEventListener("dragend", () => {
    _refineDragId = null;
    el.classList.remove("dragging");
    document.body.classList.remove("refine-dragging");
    _clearDropHints();
  });
}

function _wireDropZone(ul, parentNode) {
  ul.addEventListener("dragover", (ev) => {
    if (!_refineDragId) return;
    const dz = ev.target.closest(".tree-dropzone");
    if (!dz || dz.parentElement !== ul) return;
    ev.preventDefault();
    ev.stopPropagation();
    ev.dataTransfer.dropEffect = "move";
    _clearDropHints();
    dz.classList.add("over");
  });
  ul.addEventListener("drop", (ev) => {
    const dz = ev.target.closest(".tree-dropzone");
    if (!dz || dz.parentElement !== ul || !_refineDragId) return;
    ev.preventDefault();
    ev.stopPropagation();
    const idx = Number(dz.dataset.index || 0);
    const ok = _moveNode(_refineDragId, parentNode ? parentNode.id : null, idx);
    _clearDropHints();
    if (ok) {
      exportState.treeDirty = true;
      renderRefineStep();
    }
  });
}

// Item rows: top half → insert before this item, bottom half → after.
function _wireItemDrop(li, node) {
  li.addEventListener("dragover", (ev) => {
    if (!_refineDragId || _refineDragId === node.id) return;
    ev.preventDefault();
    ev.stopPropagation();
    ev.dataTransfer.dropEffect = "move";
    const rect = li.getBoundingClientRect();
    const before = (ev.clientY - rect.top) < rect.height / 2;
    _clearDropHints();
    li.classList.add(before ? "over-before" : "over-after");
  });
  li.addEventListener("drop", (ev) => {
    if (!_refineDragId || _refineDragId === node.id) return;
    ev.preventDefault();
    ev.stopPropagation();
    const rect = li.getBoundingClientRect();
    const before = (ev.clientY - rect.top) < rect.height / 2;
    _clearDropHints();
    const hit = _findNode(node.id);
    if (!hit) return;
    const parentId = hit.parent ? hit.parent.id : null;
    const idx = before ? hit.index : hit.index + 1;
    if (_moveNode(_refineDragId, parentId, idx)) {
      exportState.treeDirty = true;
      renderRefineStep();
    }
  });
}

// Folder rows: top/bottom 25% slivers reorder the folder among siblings;
// middle 50% drops INTO the folder (children).
function _wireFolderDrop(li, head, folderNode) {
  head.addEventListener("dragover", (ev) => {
    if (!_refineDragId) return;
    if (_refineDragId === folderNode.id) return;
    if (_isDescendant(_refineDragId, folderNode.id)) return;
    ev.preventDefault();
    ev.stopPropagation();
    ev.dataTransfer.dropEffect = "move";
    const rect = head.getBoundingClientRect();
    const y = ev.clientY - rect.top;
    _clearDropHints();
    if (y < rect.height * 0.25) {
      li.classList.add("over-before");
    } else if (y > rect.height * 0.75) {
      li.classList.add("over-after");
    } else {
      head.classList.add("over");
    }
  });
  head.addEventListener("drop", (ev) => {
    if (!_refineDragId) return;
    if (_refineDragId === folderNode.id) return;
    if (_isDescendant(_refineDragId, folderNode.id)) return;
    ev.preventDefault();
    ev.stopPropagation();
    const rect = head.getBoundingClientRect();
    const y = ev.clientY - rect.top;
    _clearDropHints();

    let ok = false;
    if (y < rect.height * 0.25 || y > rect.height * 0.75) {
      // Insert before/after the folder among its siblings.
      const hit = _findNode(folderNode.id);
      if (!hit) return;
      const parentId = hit.parent ? hit.parent.id : null;
      const idx = (y < rect.height * 0.25) ? hit.index : hit.index + 1;
      ok = _moveNode(_refineDragId, parentId, idx);
    } else {
      // Drop INTO the folder (append at end).
      const childCount = (folderNode.children || []).length;
      ok = _moveNode(_refineDragId, folderNode.id, childCount);
      if (ok) folderNode.expanded = true;
    }
    if (ok) {
      exportState.treeDirty = true;
      renderRefineStep();
    }
  });
}


let _previewFetchTimer = null;
let _previewFetchSeq = 0;

async function renderPreviewStep() {
  const themeBar = document.getElementById("previewThemeBar");
  await _renderThemeControls(themeBar, _schedulePreviewFetch);
  await _fetchPreviewNow();
}

// Coalesce rapid changes (color picker, repeated theme switches) into a
// single preview re-fetch. Drops stale responses via a sequence guard.
function _schedulePreviewFetch() {
  if (_previewFetchTimer) clearTimeout(_previewFetchTimer);
  _previewFetchTimer = setTimeout(_fetchPreviewNow, 250);
}

async function _fetchPreviewNow() {
  if (_previewFetchTimer) { clearTimeout(_previewFetchTimer); _previewFetchTimer = null; }
  const host = document.getElementById("previewBody");
  if (!host) return;
  const seq = ++_previewFetchSeq;
  host.classList.add("preview-loading");
  if (!host.firstChild) {
    host.innerHTML = `<p class="hint-text">Building preview…</p>`;
  }
  const e = exportState.selectedExporter;
  const body = {
    exporter: e.slug,
    theme: exportState.selectedTheme?.slug || null,
    theme_vars: exportState.themeVars,
    options: exportState.options,
    item_ids: exportState.itemIds,
    tree: exportState.tree || null,
  };
  let preview;
  try {
    const r = await fetch("/api/export/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const t = await r.text();
      throw new Error(`HTTP ${r.status}: ${t}`);
    }
    preview = await r.json();
  } catch (err) {
    if (seq !== _previewFetchSeq) return;          // a newer fetch is in flight
    host.classList.remove("preview-loading");
    host.innerHTML = `<p class="hint-text">Preview unavailable: ${escapeHtml(err.message)}.<br>You can still ▶ Run the export.</p>`;
    exportState.preview = { kind: "none" };
    return;
  }
  if (seq !== _previewFetchSeq) return;            // stale response
  host.classList.remove("preview-loading");
  exportState.preview = preview;
  _renderPreviewBody(host, preview);
}

function _renderPreviewBody(host, p) {
  host.innerHTML = "";
  const meta = document.createElement("p");
  meta.className = "preview-meta";
  const filename = p.filename || "(stream)";
  const mime = p.mime || "";
  const truncated = p.truncated ? `<span>· truncated to ${p.preview_lines || "preview"} lines</span>` : "";
  meta.innerHTML = `<span>📦 <code>${escapeHtml(filename)}</code></span>${mime ? `<span>${escapeHtml(mime)}</span>` : ""}${truncated}`;
  host.appendChild(meta);

  if (p.kind === "html") {
    const frame = document.createElement("iframe");
    frame.className = "preview-frame";
    frame.setAttribute("sandbox", "");
    frame.setAttribute("srcdoc", p.content || "");
    host.appendChild(frame);
  } else if (p.kind === "text") {
    const pre = document.createElement("pre");
    pre.className = "preview-pre";
    const lang = _detectPreviewLang(p.mime || "", p.filename || "");
    const text = p.content || "";
    if (lang === "json") pre.innerHTML = _highlightJSON(text);
    else if (lang === "yaml") pre.innerHTML = _highlightYAML(text);
    else pre.textContent = text;
    host.appendChild(pre);
  } else if (p.kind === "manifest") {
    const wrap = document.createElement("div");
    wrap.className = "preview-manifest";
    const rows = (p.manifest || []).map(row => `
      <tr>
        <td>${escapeHtml(row.title || "(untitled)")}<br>
            <span class="hint-text">${escapeHtml(row.url || "")}</span></td>
        <td><span class="plan-pill ${row.plan === 'skip' ? 'skip' : ''}">${escapeHtml(row.plan || "")}</span></td>
        <td><code>${escapeHtml(row.filename || "")}</code></td>
        <td>${escapeHtml(row.note || "")}</td>
      </tr>`).join("");
    wrap.innerHTML = `<table>
      <thead><tr><th>Item</th><th>Plan</th><th>Filename</th><th>Note</th></tr></thead>
      <tbody>${rows}</tbody></table>`;
    host.appendChild(wrap);
  } else {
    host.innerHTML = `<p class="hint-text">No preview available for this exporter.</p>`;
  }
}

async function runExport() {
  const e = exportState.selectedExporter;
  const status = document.getElementById("exportStatus");
  const next = document.getElementById("exportNextBtn");
  const back = document.getElementById("exportBackBtn");
  const body = {
    exporter: e.slug,
    theme: exportState.selectedTheme?.slug || null,
    theme_vars: exportState.themeVars,
    options: exportState.options,
    item_ids: exportState.itemIds,
    tree: exportState.tree || null,
  };
  status.textContent = "Running…";
  next.disabled = true;
  back.disabled = true;
  try {
    const r = await fetch("/api/export/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const text = await r.text();
      throw new Error(`HTTP ${r.status}: ${text}`);
    }
    if (e.execution_mode === "immediate") {
      const blob = await r.blob();
      const cd = r.headers.get("Content-Disposition") || "";
      const m = /filename="([^"]+)"/.exec(cd);
      const filename = m ? m[1] : `booki-export-${Date.now()}`;
      const a = document.createElement("a");
      const url = URL.createObjectURL(blob);
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      status.textContent = `Done: ${filename}`;
      showToast(`Exported ${filename}`);
      setTimeout(closeExport, 900);
    } else {
      const data = await r.json();
      status.textContent = `Background task started (#${data.task_id}). See Manage › Tasks.`;
      showToast("Background task queued — see Manage › Tasks");
      setTimeout(closeExport, 1400);
    }
  } catch (err) {
    status.textContent = `Error: ${err.message}`;
  } finally {
    next.disabled = false;
    back.disabled = exportState.step === 1;
  }
}

function _onExportNext() {
  const status = document.getElementById("exportStatus");
  if (exportState.step === 1) {
    if (!exportState.selectedExporter) {
      status.textContent = "Pick an exporter first.";
      return;
    }
    status.textContent = "";
    exportState.step = 2;
  } else if (exportState.step === 2) {
    status.textContent = "";
    exportState.step = 3;
  } else if (exportState.step === 3) {
    status.textContent = "";
    exportState.step = 4;
  } else {
    return runExport();
  }
  renderExportStep();
}

function _onExportBack() {
  if (exportState.step > 1) exportState.step -= 1;
  renderExportStep();
}

function _onExportStepnav(ev) {
  const pill = ev.target.closest(".step-pill");
  if (!pill) return;
  const target = Number(pill.dataset.step);
  // Only allow stepping back to a completed step.
  if (target > exportState.step) return;
  exportState.step = target;
  renderExportStep();
}

document.getElementById("openExportBtn")?.addEventListener("click", openExport);
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (exportPanelEl) closeExport();
});

// ─── Preview syntax highlighting (JSON / YAML) ──────────────────────

function _detectPreviewLang(mime, filename) {
  const m = (mime || "").toLowerCase();
  if (m.includes("json")) return "json";
  if (m.includes("yaml")) return "yaml";
  const f = (filename || "").toLowerCase();
  if (f.endsWith(".json")) return "json";
  if (f.endsWith(".yaml") || f.endsWith(".yml")) return "yaml";
  return "plain";
}

// Single-pass JSON tokenizer. Matches strings (with optional trailing colon
// → key), numbers, and the bare literals true/false/null. Everything between
// matches gets HTML-escaped as-is so braces/brackets/commas render plainly.
const _JSON_TOKEN_RE = /("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false|null)\b|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g;

function _highlightJSON(text) {
  let out = "";
  let last = 0;
  for (const m of text.matchAll(_JSON_TOKEN_RE)) {
    out += escapeHtml(text.slice(last, m.index));
    const [whole, str, colon, lit, num] = m;
    if (str !== undefined) {
      const cls = colon ? "tk-key" : "tk-str";
      out += `<span class="${cls}">${escapeHtml(str)}</span>`;
      if (colon) out += escapeHtml(colon);
    } else if (lit !== undefined) {
      const cls = lit === "null" ? "tk-null" : "tk-bool";
      out += `<span class="${cls}">${lit}</span>`;
    } else if (num !== undefined) {
      out += `<span class="tk-num">${num}</span>`;
    }
    last = m.index + whole.length;
  }
  out += escapeHtml(text.slice(last));
  return out;
}

// Per-line YAML highlighter. Matches: leading list dash, key:, scalar value,
// trailing #comment. Flow-style values ([…] / {…}) are passed through the
// JSON highlighter for free.
function _highlightYAML(text) {
  return text.split("\n").map(_highlightYamlLine).join("\n");
}

function _highlightYamlLine(line) {
  // Pull off a trailing "#…" comment that isn't inside quotes.
  let code = line, comment = "";
  const cm = line.match(/^((?:[^"'#]|"(?:\\.|[^"\\])*"|'(?:''|[^'])*')*?)(\s+#.*)$/);
  if (cm) { code = cm[1]; comment = cm[2]; }

  const keyMatch = code.match(/^(\s*(?:- )?)([\w.][\w.\-]*)(\s*:)(\s*)(.*)$/);
  if (keyMatch) {
    const [, indent, key, colon, gap, val] = keyMatch;
    const indentHtml = indent.endsWith("- ")
      ? escapeHtml(indent.slice(0, -2)) + `<span class="tk-punct">- </span>`
      : escapeHtml(indent);
    return indentHtml
         + `<span class="tk-key">${escapeHtml(key)}</span>`
         + escapeHtml(colon + gap)
         + _highlightYamlScalar(val)
         + (comment ? `<span class="tk-comment">${escapeHtml(comment)}</span>` : "");
  }

  const liMatch = code.match(/^(\s*)(- )(.*)$/);
  if (liMatch) {
    return escapeHtml(liMatch[1])
         + `<span class="tk-punct">- </span>`
         + _highlightYamlScalar(liMatch[3])
         + (comment ? `<span class="tk-comment">${escapeHtml(comment)}</span>` : "");
  }

  return escapeHtml(code) + (comment ? `<span class="tk-comment">${escapeHtml(comment)}</span>` : "");
}

function _highlightYamlScalar(s) {
  if (s === "") return "";
  if (/^"(?:\\.|[^"\\])*"$/.test(s) || /^'(?:''|[^'])*'$/.test(s)) {
    return `<span class="tk-str">${escapeHtml(s)}</span>`;
  }
  if (/^(true|false|yes|no|on|off)$/i.test(s)) {
    return `<span class="tk-bool">${escapeHtml(s)}</span>`;
  }
  if (/^(null|~)$/i.test(s)) {
    return `<span class="tk-null">${escapeHtml(s)}</span>`;
  }
  if (/^-?\d+(\.\d+)?([eE][+-]?\d+)?$/.test(s)) {
    return `<span class="tk-num">${escapeHtml(s)}</span>`;
  }
  if ((s.startsWith("[") && s.endsWith("]")) ||
      (s.startsWith("{") && s.endsWith("}"))) {
    return _highlightJSON(s);
  }
  return escapeHtml(s);
}


// ─── Manage › Tasks sub-tab ─────────────────────────────────────────

let _tasksPollTimer = null;

function startTasksPoll() {
  stopTasksPoll();
  _tasksPollTimer = setInterval(() => {
    if (_manageSubtab === "tasks") refreshManageTasks();
  }, 3000);
}
function stopTasksPoll() {
  if (_tasksPollTimer) { clearInterval(_tasksPollTimer); _tasksPollTimer = null; }
}

async function refreshManageTasks() {
  const host = document.getElementById("manageTasks");
  if (!host) return;
  let tasks;
  try {
    const r = await fetch("/api/export/tasks");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    tasks = await r.json();
  } catch (e) {
    host.innerHTML = `<p class="hint-text">Failed to load tasks: ${escapeHtml(e.message)}</p>`;
    return;
  }
  if (!tasks.length) {
    host.innerHTML = `<p class="hint-text">No background tasks yet. Run a ✈️ exporter to create one.</p>`;
    return;
  }
  // Preserve which rows the user has expanded so the 3-second polling
  // re-render doesn't slam them shut while they're reading the log.
  const expanded = new Set(
    [...host.querySelectorAll(".task-row.expanded")].map(r => r.dataset.id)
  );

  host.innerHTML = tasks.map(_renderTaskRow).join("");
  host.querySelectorAll(".task-row").forEach(row => {
    const id = row.dataset.id;
    if (expanded.has(id)) {
      row.classList.add("expanded");
      const t = row.querySelector(".task-toggle");
      if (t) t.textContent = "▾";
    }
    row.querySelector(".task-toggle")?.addEventListener("click", () => {
      row.classList.toggle("expanded");
      const t = row.querySelector(".task-toggle");
      if (t) t.textContent = row.classList.contains("expanded") ? "▾" : "▸";
    });
    row.querySelector(".task-retry")?.addEventListener("click", async () => {
      await fetch(`/api/export/tasks/${id}/retry`, { method: "POST" });
      refreshManageTasks();
    });
    row.querySelector(".task-delete")?.addEventListener("click", async () => {
      if (!confirm("Delete this task and its artifact?")) return;
      await fetch(`/api/export/tasks/${id}`, { method: "DELETE" });
      refreshManageTasks();
    });
  });
}

function _renderTaskRow(t) {
  const STATUS = {
    pending: { ico: "⏳", cls: "pending" },
    running: { ico: "🏃", cls: "running" },
    success: { ico: "✓",  cls: "success" },
    failed:  { ico: "✗",  cls: "failed"  },
  }[t.status] || { ico: "·", cls: "" };

  const pct = t.progress_total > 0
    ? Math.min(100, Math.round((t.progress_done / t.progress_total) * 100))
    : 0;
  const progressBar = t.status === "running"
    ? `<div class="task-progress"><div class="task-progress-fill" style="width:${pct}%"></div></div>`
    : "";

  const dl = (t.status === "success" && t.artifact_path)
    ? `<a class="btn small" href="/api/export/tasks/${t.id}/artifact" download>⬇ ${escapeHtml(t.artifact_filename || "download")}</a>`
    : "";
  const retryBtn = (t.status === "failed")
    ? `<button class="btn small task-retry" type="button">↻ Retry</button>`
    : "";

  const itemSuffix = t.item_count === 1 ? "" : "s";
  const created = (t.created_at || "").replace("T", " ").slice(0, 16);

  const errorBlock = t.error
    ? `<div class="task-error">${escapeHtml(t.error)}</div>` : "";
  const pathBlock = t.artifact_path
    ? `<div class="task-artifact-path">📂 <code>${escapeHtml(t.artifact_path)}</code></div>` : "";
  const logBlock = t.log
    ? `<pre class="task-log">${escapeHtml(t.log)}</pre>` : "";

  // Prefer the user's chosen page_title (or root_folder for bookmark_file)
  // so the row reads like the artifact, not like the plugin slug.
  const userTitle = (t.options && (t.options.page_title || t.options.root_folder || "")).toString().trim();
  const titleText = userTitle || t.exporter;
  const exporterTag = userTitle
    ? `<span class="task-meta task-exporter-tag">${escapeHtml(t.exporter)}</span>`
    : "";

  return `
    <div class="task-row task-${STATUS.cls}" data-id="${t.id}">
      <div class="task-summary">
        <button class="task-toggle" type="button" aria-label="Toggle details">▸</button>
        <span class="task-status-icon">${STATUS.ico}</span>
        <span class="task-exporter">${escapeHtml(titleText)}</span>
        ${exporterTag}
        <span class="task-meta">${escapeHtml(created)}</span>
        <span class="task-meta">${t.item_count} item${itemSuffix}</span>
        ${progressBar}
        <div class="task-actions">
          ${dl}
          ${retryBtn}
          <button class="btn small task-delete" type="button" title="Delete task and artifact">🗑</button>
        </div>
      </div>
      <div class="task-fold">
        ${errorBlock}
        ${pathBlock}
        ${logBlock || `<p class="hint-text">No log yet.</p>`}
      </div>
    </div>`;
}


// ─── Manage › Sync & Ingest sub-tab ─────────────────────────────────

let _jobsPollTimer = null;

function startJobsPoll() {
  stopJobsPoll();
  _jobsPollTimer = setInterval(() => {
    if (_manageSubtab === "jobs") refreshManageJobs();
  }, 1500);
}
function stopJobsPoll() {
  if (_jobsPollTimer) { clearInterval(_jobsPollTimer); _jobsPollTimer = null; }
}

async function loadManageJobs() {
  await _populateJobChips();
  await refreshManageJobs();
}

async function _populateJobChips() {
  const sources = document.getElementById("jobSyncSources");
  const enrichers = document.getElementById("jobSyncEnrichers");
  if (!sources || !enrichers) return;
  if (sources.dataset.loaded === "1") return;
  try {
    const r = await fetch("/api/plugins");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    sources.innerHTML = (data.sources || [])
      .map(s => _chipBtn(s.name, s.available !== false)).join("");
    enrichers.innerHTML = (data.enrichers || [])
      .filter(e => !e.disabled)
      .map(e => _chipBtn(e.name, true)).join("");
    sources.dataset.loaded = "1";
    [sources, enrichers].forEach(host => {
      host.querySelectorAll(".job-chip").forEach(b => {
        b.addEventListener("click", () => b.classList.toggle("on"));
      });
    });
  } catch (e) {
    sources.innerHTML = `<span class="hint-text">Failed to load sources: ${escapeHtml(e.message)}</span>`;
  }
}

function _chipBtn(name, available) {
  const cls = available ? "job-chip" : "job-chip disabled";
  const title = available ? name : `${name} (unavailable)`;
  return `<button type="button" class="${cls}" data-name="${escapeHtml(name)}" title="${escapeHtml(title)}">${escapeHtml(name)}</button>`;
}

function _collectChipValues(hostId) {
  const host = document.getElementById(hostId);
  if (!host) return [];
  return [...host.querySelectorAll(".job-chip.on")].map(b => b.dataset.name);
}

async function runManageJob(kind) {
  const fieldset = document.querySelector(`.job-options[data-kind="${kind}"]`);
  if (!fieldset) return;
  const args = [];
  fieldset.querySelectorAll('input[type="checkbox"][data-flag]').forEach(cb => {
    if (cb.checked) args.push(cb.dataset.flag);
  });
  if (kind === "sync") {
    const srcs = _collectChipValues("jobSyncSources");
    if (srcs.length) args.push("--source", ...srcs);
    const ens = _collectChipValues("jobSyncEnrichers");
    if (ens.length) args.push("--enricher", ...ens);
  }
  const btn = document.querySelector(`.job-run-btn[data-kind="${kind}"]`);
  if (btn) { btn.disabled = true; btn.textContent = "Queueing…"; }
  try {
    const r = await fetch("/api/jobs/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, args }),
    });
    if (!r.ok) {
      const text = await r.text();
      throw new Error(`HTTP ${r.status}: ${text}`);
    }
    const data = await r.json();
    showToast(`Queued ${kind} (#${data.job_id})`);
    refreshManageJobs();
  } catch (e) {
    showToast(`Failed to queue ${kind}: ${e.message}`);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = kind === "sync" ? "▶ Run sync" : "▶ Run ingest";
    }
  }
}

async function refreshManageJobs() {
  const host = document.getElementById("manageJobs");
  if (!host) return;
  let jobs;
  try {
    const r = await fetch("/api/jobs");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    jobs = await r.json();
  } catch (e) {
    host.innerHTML = `<p class="hint-text">Failed to load jobs: ${escapeHtml(e.message)}</p>`;
    return;
  }
  if (!jobs.length) {
    host.innerHTML = `<p class="hint-text">No jobs yet. Pick options above and hit Run.</p>`;
    return;
  }

  // Preserve which rows the user has expanded across polls.
  const expanded = new Set(
    [...host.querySelectorAll(".task-row.expanded")].map(r => r.dataset.id)
  );

  host.innerHTML = jobs.map(_renderJobRow).join("");
  host.querySelectorAll(".task-row").forEach(row => {
    if (expanded.has(row.dataset.id)) {
      row.classList.add("expanded");
      const t = row.querySelector(".task-toggle");
      if (t) t.textContent = "▾";
    }
    row.querySelector(".task-toggle")?.addEventListener("click", () => {
      row.classList.toggle("expanded");
      const t = row.querySelector(".task-toggle");
      if (t) t.textContent = row.classList.contains("expanded") ? "▾" : "▸";
    });
    row.querySelector(".job-cancel")?.addEventListener("click", async () => {
      await fetch(`/api/jobs/${row.dataset.id}/cancel`, { method: "POST" });
      refreshManageJobs();
    });
    row.querySelector(".task-delete")?.addEventListener("click", async () => {
      if (!confirm("Delete this job record?")) return;
      await fetch(`/api/jobs/${row.dataset.id}`, { method: "DELETE" });
      refreshManageJobs();
    });
  });
}

function _renderJobRow(j) {
  const STATUS = {
    pending: { ico: "⏳", cls: "pending" },
    running: { ico: "🏃", cls: "running" },
    success: { ico: "✓",  cls: "success" },
    failed:  { ico: "✗",  cls: "failed"  },
  }[j.status] || { ico: "·", cls: "" };

  const progressBar = j.status === "running"
    ? `<div class="task-progress task-progress-indeterminate"><div class="task-progress-fill"></div></div>`
    : "";

  const cancelBtn = (j.status === "running" || j.status === "pending")
    ? `<button class="btn small job-cancel" type="button" title="Stop this job">⏹ Stop</button>`
    : "";

  const argsLine = (j.args && j.args.length)
    ? `<span class="task-meta job-args"><code>${escapeHtml(j.args.join(" "))}</code></span>`
    : `<span class="task-meta hint-text">no flags</span>`;

  const created = (j.created_at || "").replace("T", " ").slice(0, 16);
  const finished = (j.finished_at || "").replace("T", " ").slice(0, 16);
  const elapsed = j.started_at && j.finished_at
    ? _formatDurationMs(Date.parse(j.finished_at) - Date.parse(j.started_at))
    : (j.started_at && j.status === "running"
        ? _formatDurationMs(Date.now() - Date.parse(j.started_at)) : "");

  const result = j.status === "success"
    ? `<span class="job-result ok">✓ exit 0${elapsed ? ` · ${elapsed}` : ""}</span>`
    : (j.status === "failed"
        ? `<span class="job-result fail">✗ ${escapeHtml(j.error || "failed")}${elapsed ? ` · ${elapsed}` : ""}</span>`
        : (j.status === "running"
            ? `<span class="job-result running">running${elapsed ? ` · ${elapsed}` : ""}</span>` : ""));

  const errorBlock = j.error && j.status === "failed"
    ? `<div class="task-error">${escapeHtml(j.error)}</div>` : "";
  const finishedBlock = j.finished_at
    ? `<div class="hint-text">finished ${escapeHtml(finished)}</div>` : "";
  const logBlock = j.log
    ? `<pre class="task-log">${escapeHtml(j.log)}</pre>`
    : `<p class="hint-text">No output yet.</p>`;

  return `
    <div class="task-row task-${STATUS.cls}" data-id="${j.id}">
      <div class="task-summary">
        <button class="task-toggle" type="button" aria-label="Toggle details">▸</button>
        <span class="task-status-icon">${STATUS.ico}</span>
        <span class="task-exporter">${escapeHtml(j.kind)}</span>
        ${argsLine}
        <span class="task-meta">${escapeHtml(created)}</span>
        ${progressBar}
        ${result}
        <div class="task-actions">
          ${cancelBtn}
          <button class="btn small task-delete" type="button" title="Delete job record">🗑</button>
        </div>
      </div>
      <div class="task-fold">
        ${errorBlock}
        ${finishedBlock}
        ${logBlock}
      </div>
    </div>`;
}

function _formatDurationMs(ms) {
  if (!Number.isFinite(ms) || ms < 0) return "";
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rs = s % 60;
  if (m < 60) return `${m}m ${rs}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}
