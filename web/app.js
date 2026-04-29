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
  lists: [],        // [{name, count, smart?, predicates?, order?}] — from /api/lists
  activeList: null, // name → filter main list to items in this list; null = no filter
  activeSmartList: null, // SmartList spec ref — only one of activeList/activeSmartList is set
  adv: {            // advanced search filters (persisted to localStorage)
    tags: new Set(),
    lists: new Set(),
    sources: new Set(),
    kinds: new Set(),
    impMin: null,
    impMax: null,
    hasSummary: false,
    hasNotes: false,
    includeRemoved: true,
  },
};

const ADV_STORAGE_KEY = "booki.advSearch.v1";

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
  detailLists: $("detailLists"),
  detailKeywords: $("detailKeywords"),
  listAddForm: $("listAddForm"),
  listAddInput: $("listAddInput"),
  listSuggestions: $("listSuggestions"),
  secLists: $("secLists"),
  statLists: $("statLists"),
  listFilterClear: $("listFilterClear"),
  detailSource: $("detailSource"),
  detailBookmarked: $("detailBookmarked"),
  advSearch: $("advSearch"),
  advTags: $("advTags"),
  advLists: $("advLists"),
  advSources: $("advSources"),
  advKinds: $("advKinds"),
  advImpMin: $("advImpMin"),
  advImpMax: $("advImpMax"),
  advHasSummary: $("advHasSummary"),
  advHasNotes: $("advHasNotes"),
  advIncludeRemoved: $("advIncludeRemoved"),
  advCount: $("advCount"),
  advClear: $("advClear"),
  detailLastsync: $("detailLastsync"),
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

// ─── Load + render list ────────────────────────────────────────────

async function loadStats() {
  try {
    const r = await fetch("/api/stats");
    if (!r.ok) return;
    renderStats(await r.json());
  } catch { /* optional */ }
}

function renderStats(s) {
  const $$ = (id) => document.getElementById(id);
  $$("statTotal").textContent    = (s.total ?? 0).toLocaleString();
  $$("statEnriched").textContent = (s.enriched ?? 0).toLocaleString();
  $$("statSources").textContent  = Object.keys(s.by_source || {}).length;
  $$("statLastSync").textContent = s.last_sync || "—";
  $$("statDir").textContent      = s.bookmarks_dir || "";
  renderBars($$("statBySource"), s.by_source || {});
  renderBars($$("statByKind"),   s.by_kind   || {});
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
  const enriched = state.all.filter(b => b.has_summary).length;
  els.count.textContent = `${state.all.length} bookmarks · ${enriched} enriched`;
  refreshAdvancedFilters();
  applyFilter();

  // Re-fire onShow on whichever tab is active so it picks up the new data
  // (this matters on first boot — `Tabs.activate` runs before bookmarks
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

// ─── Smart-list evaluator (mirrors smart_lists.py) ─────────────────

function smartListMatches(bm, spec) {
  for (const p of (spec.predicates || [])) {
    const actual = bm[p.field];
    const expected = p.value;
    switch (p.op) {
      case "eq":  if (!_slEq(actual, expected))  return false; break;
      case "ne":  if ( _slEq(actual, expected))  return false; break;
      case "any": if (!_slAny(actual, expected)) return false; break;
      case "gt":  case "gte": case "lt": case "lte": {
        const c = _slCmp(actual, expected);
        if (c === null) return false;
        if (p.op === "gt"  && !(c >  0)) return false;
        if (p.op === "gte" && !(c >= 0)) return false;
        if (p.op === "lt"  && !(c <  0)) return false;
        if (p.op === "lte" && !(c <= 0)) return false;
        break;
      }
      default: return false;
    }
  }
  return true;
}

function _slEq(actual, expected) {
  if (typeof expected === "boolean") return Boolean(actual) === expected;
  if (expected === null || expected === undefined)
    return actual === null || actual === undefined || actual === ""
        || (Array.isArray(actual) && actual.length === 0);
  if (Array.isArray(actual)) return actual.some(x => String(x) === String(expected));
  return String(actual) === String(expected);
}

function _slAny(actual, wanted) {
  if (!Array.isArray(wanted) || wanted.length === 0) return false;
  const pool = Array.isArray(actual)
    ? actual : (actual ? [actual] : []);
  const have = new Set(pool.map(String));
  return wanted.some(w => have.has(String(w)));
}

function _slCmp(left, right) {
  if (left === null || left === undefined || left === "") return null;
  if (right === null || right === undefined || right === "") return null;
  // Numeric coercion if either side is a number.
  const ln = (typeof left === "number") ? left : Number(left);
  const rn = (typeof right === "number") ? right : Number(right);
  if (!Number.isNaN(ln) && !Number.isNaN(rn) && (typeof left === "number" || typeof right === "number")) {
    return ln < rn ? -1 : (ln > rn ? 1 : 0);
  }
  const ls = String(left), rs = String(right);
  return ls < rs ? -1 : (ls > rs ? 1 : 0);
}

function applySmartListOrder(rows, order) {
  if (!order || !Array.isArray(order) || !order[0]) return rows;
  const [field, dir] = order;
  const reverse = dir === "desc";
  return [...rows].sort((a, b) => {
    const av = a.bm[field], bv = b.bm[field];
    const aMissing = (av === null || av === undefined || av === "");
    const bMissing = (bv === null || bv === undefined || bv === "");
    if (aMissing && bMissing) return 0;
    if (aMissing) return 1;   // missing always last
    if (bMissing) return -1;
    if (typeof av === "number" && typeof bv === "number") {
      return reverse ? bv - av : av - bv;
    }
    const as = String(av), bs = String(bv);
    if (as === bs) return 0;
    return reverse ? (as < bs ? 1 : -1) : (as < bs ? -1 : 1);
  });
}

function makeAdvPredicate(adv) {
  return (b) => {
    if (adv.tags.size && !(b.tags || []).some(t => adv.tags.has(t))) return false;
    if (adv.lists.size && !(b.lists || []).some(l => adv.lists.has(l))) return false;
    if (adv.sources.size) {
      const all = new Set([b.source, ...(b.sources || [])].filter(Boolean));
      let ok = false;
      for (const s of adv.sources) if (all.has(s)) { ok = true; break; }
      if (!ok) return false;
    }
    if (adv.kinds.size && !adv.kinds.has(b.kind || "bookmark")) return false;
    const imp = b.importance || 0;
    if (adv.impMin != null && imp < adv.impMin) return false;
    if (adv.impMax != null && imp > adv.impMax) return false;
    if (adv.hasSummary && !b.has_summary) return false;
    if (adv.hasNotes && !(b.notes && b.notes.trim().length)) return false;
    if (!adv.includeRemoved && (b.removed_from_browser || b.removed_from_source)) return false;
    return true;
  };
}

function advActiveCount(adv) {
  let n = adv.tags.size + adv.lists.size + adv.sources.size + adv.kinds.size;
  if (adv.impMin != null) n++;
  if (adv.impMax != null) n++;
  if (adv.hasSummary) n++;
  if (adv.hasNotes) n++;
  if (!adv.includeRemoved) n++;
  return n;
}

function refreshAdvBadge() {
  const n = advActiveCount(state.adv);
  els.advCount.textContent = n;
  els.advCount.classList.toggle("hidden", n === 0);
  els.advClear.classList.toggle("hidden", n === 0);
}

function loadAdvFromStorage() {
  try {
    const raw = localStorage.getItem(ADV_STORAGE_KEY);
    if (!raw) return;
    const j = JSON.parse(raw);
    state.adv.tags = new Set(j.tags || []);
    state.adv.lists = new Set(j.lists || []);
    state.adv.sources = new Set(j.sources || []);
    state.adv.kinds = new Set(j.kinds || []);
    state.adv.impMin = j.impMin ?? null;
    state.adv.impMax = j.impMax ?? null;
    state.adv.hasSummary = !!j.hasSummary;
    state.adv.hasNotes = !!j.hasNotes;
    state.adv.includeRemoved = j.includeRemoved !== false;
    if (j.open) els.advSearch?.setAttribute("open", "");
  } catch { /* ignore corrupt storage */ }
}

function saveAdvToStorage() {
  try {
    localStorage.setItem(ADV_STORAGE_KEY, JSON.stringify({
      tags: [...state.adv.tags],
      lists: [...state.adv.lists],
      sources: [...state.adv.sources],
      kinds: [...state.adv.kinds],
      impMin: state.adv.impMin,
      impMax: state.adv.impMax,
      hasSummary: state.adv.hasSummary,
      hasNotes: state.adv.hasNotes,
      includeRemoved: state.adv.includeRemoved,
      open: !!els.advSearch?.open,
    }));
  } catch { /* quota / private mode — fail silently */ }
}

// Re-render the chip pickers from current state.all + state.adv.
function refreshAdvancedFilters() {
  const tagCounts = {}, listCounts = {}, sourceCounts = {}, kindCounts = {};
  state.all.forEach(b => {
    (b.tags || []).forEach(t => { tagCounts[t] = (tagCounts[t] || 0) + 1; });
    (b.lists || []).forEach(l => { listCounts[l] = (listCounts[l] || 0) + 1; });
    [b.source, ...(b.sources || [])].filter(Boolean).forEach(s => {
      sourceCounts[s] = (sourceCounts[s] || 0) + 1;
    });
    const k = b.kind || "bookmark";
    kindCounts[k] = (kindCounts[k] || 0) + 1;
  });
  const sorted = (m) => Object.entries(m).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  const onChange = () => { saveAdvToStorage(); refreshAdvBadge(); applyFilter(); };

  renderChipPicker(els.advTags, sorted(tagCounts), state.adv.tags,
                   (n) => { toggleSet(state.adv.tags, n); onChange(); });
  renderChipPicker(els.advLists, sorted(listCounts), state.adv.lists,
                   (n) => { toggleSet(state.adv.lists, n); onChange(); });
  renderChipPicker(els.advSources, sorted(sourceCounts), state.adv.sources,
                   (n) => { toggleSet(state.adv.sources, n); onChange(); });
  renderChipPicker(els.advKinds, sorted(kindCounts), state.adv.kinds,
                   (n) => { toggleSet(state.adv.kinds, n); onChange(); });

  // Sync misc inputs from state (covers reload-from-localStorage case).
  els.advImpMin.value = state.adv.impMin ?? "";
  els.advImpMax.value = state.adv.impMax ?? "";
  els.advHasSummary.checked = state.adv.hasSummary;
  els.advHasNotes.checked = state.adv.hasNotes;
  els.advIncludeRemoved.checked = state.adv.includeRemoved;
  refreshAdvBadge();
}

function clearAdvFilters() {
  state.adv.tags.clear();
  state.adv.lists.clear();
  state.adv.sources.clear();
  state.adv.kinds.clear();
  state.adv.impMin = null;
  state.adv.impMax = null;
  state.adv.hasSummary = false;
  state.adv.hasNotes = false;
  state.adv.includeRemoved = true;
  refreshAdvancedFilters();
  saveAdvToStorage();
  applyFilter();
}

function applyFilter() {
  const q = els.findInput.value.trim();
  const inList = state.activeList
    ? (b) => (b.lists || []).includes(state.activeList)
    : () => true;
  const matchSmart = state.activeSmartList
    ? (b) => smartListMatches(b, state.activeSmartList)
    : () => true;
  const matchAdv = makeAdvPredicate(state.adv);
  const pool = state.all.filter(b => inList(b) && matchSmart(b) && matchAdv(b));
  if (!q) {
    state.filtered = pool.map(b => ({ bm: b, score: b.importance * 2, titleMatches: [], urlMatches: [] }));
    const order = state.activeSmartList?.order;
    if (order) {
      state.filtered = applySmartListOrder(state.filtered, order);
    } else {
      state.filtered.sort((a, b) => b.score - a.score);
    }
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
    out.sort((a, b) => b.score - a.score);
    state.filtered = out.slice(0, 200);
  }
  state.selected = 0;
  renderResults();
}

function renderResults() {
  if (state.filtered.length === 0) {
    els.results.innerHTML = "";
    els.empty.classList.remove("hidden");
    return;
  }
  els.empty.classList.add("hidden");

  const frag = document.createDocumentFragment();
  state.filtered.forEach((row, i) => {
    frag.appendChild(renderRow(row, i === state.selected));
  });
  els.results.replaceChildren(frag);
}

function renderRow(row, selected) {
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
  const lists = (bm.lists || []).slice(0, 4)
    .map(l => `<span class="tag list">📋 ${escapeHtml(l)}</span>`).join("");
  const labels = sourceLabels(bm);
  const source = labels
    .map(s => `<span class="tag src">${escapeHtml(s)}</span>`)
    .join("");
  const enriched = bm.has_summary ? `<span class="tag">✨ summary</span>` : "";
  return `<div class="bm-meta">${source}${lists}${tags}${enriched}</div>`;
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
  renderDetailLists(d);
  toggleSection(els.secKeywords, d.keywords?.length, () => renderChips(els.detailKeywords, d.keywords));

  els.detailSource.textContent     = d.source || "—";
  els.detailBookmarked.textContent = d.date_bookmarked || "—";
  els.detailLastsync.textContent   = d.last_sync || "—";
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
    if (active && active.id === id && !tab._mounted) {
      // Module loaded after activation — mount + show now.
      _mountIfNeeded(tab);
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
  onChange: (cb) => {
    if (typeof cb !== "function") return () => {};
    _bookmarkChangeListeners.add(cb);
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
};
window.booki.search = {
  fuzzy:     (q, text) => fuzzyMatch(q, text),
  substring: (q, text) => substringMatch(q, text),
  // Live read of the global "fuzzy on/off" toggle so plugin tabs honor it.
  get useFuzzy() { return !!state.fuzzy; },
};

// ─── Built-in tabs ─────────────────────────────────────────────────

Tabs.register({
  id: "search", label: "Search", icon: "🔎", order: 10,
  // Search panel is pre-rendered in index.html — mount is a no-op.
  mount() {},
  onShow() { els.findInput?.focus?.(); },
});

Tabs.register({
  id: "photos", label: "Photos", icon: "🖼", order: 20,
  mount(el) {
    el.innerHTML = `
      <div class="photo-tab scoped-tab">
        <header class="tab-header">
          <h2>🖼 Photos</h2>
          <p class="tab-sub" id="photoCount">—</p>
        </header>
        <div class="search-box scoped-search" id="photoSearchBox">
          <span class="search-icon">🔎</span>
          <input id="photoFindInput" type="search" autocomplete="off"
                 autocapitalize="off" autocorrect="off" spellcheck="false"
                 placeholder="Search photos by title or URL…">
          <span class="hint">↵ open · click for details</span>
        </div>
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

    const input = document.getElementById("photoFindInput");
    input.addEventListener("input", renderPhotoGrid);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        const first = document.querySelector("#photoGrid .photo-tile");
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
  },
  onShow() {
    renderPhotoGrid();
    document.getElementById("photoFindInput")?.focus();
  },
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

  // Apply this tab's local search; falls back to importance-sort when empty.
  const q = (input?.value || "").trim();
  let photos;
  if (q) {
    const match = state.fuzzy ? fuzzyMatch : substringMatch;
    const scored = [];
    for (const b of all) {
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
    scored.sort((a, b) => b.score - a.score);
    photos = scored.map(x => x.bm).slice(0, 200);
  } else {
    photos = [...all].sort((a, b) => (b.importance || 0) - (a.importance || 0));
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

    li.innerHTML = `
      <div class="photo-thumb">${imgHtml}</div>
      <div class="photo-meta">
        <div class="photo-title" title="${escapeHtml(b.title || '')}">${escapeHtml(b.title || "(untitled)")}</div>
        ${b.importance ? `<div class="photo-imp">★${b.importance}</div>` : ""}
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
        </header>
        <div class="search-box scoped-search" id="videoSearchBox">
          <span class="search-icon">🔎</span>
          <input id="videoFindInput" type="search" autocomplete="off"
                 autocapitalize="off" autocorrect="off" spellcheck="false"
                 placeholder="Search videos by title, channel, or URL…">
          <span class="hint">↵ open · click for details</span>
        </div>
        <ul class="video-grid" id="videoGrid"></ul>
        <p class="tab-empty hidden" id="videoEmpty">
          No videos yet. The YouTube source plugin pulls liked / watched videos
          and recent uploads from subscribed channels — wire it up in
          <code>config.toml</code> and run <code>booki sync</code>.
        </p>
        <p class="tab-empty hidden" id="videoNoMatch">
          No videos match your search.
        </p>`;

    const input = document.getElementById("videoFindInput");
    input.addEventListener("input", renderVideoGrid);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        const first = document.querySelector("#videoGrid .video-tile");
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
  },
  onShow() {
    renderVideoGrid();
    document.getElementById("videoFindInput")?.focus();
  },
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

  const q = (input?.value || "").trim();
  let videos;
  if (q) {
    const match = state.fuzzy ? fuzzyMatch : substringMatch;
    const scored = [];
    for (const b of all) {
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
    scored.sort((a, b) => b.score - a.score);
    videos = scored.map(x => x.bm).slice(0, 200);
  } else {
    videos = [...all].sort((a, b) => (b.importance || 0) - (a.importance || 0))
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
  onShow() { els.askInput?.focus?.(); },
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
          <dl class="info-grid" id="manageInfo">
            <dt>Loading…</dt><dd></dd>
          </dl>
        </section>

        <section class="subtab-panel" data-subpanel="plugins">
          <div class="subtab-actions">
            <button type="button" class="btn manage-refresh" id="pluginsRefresh">↻ Refresh</button>
          </div>
          <div id="managePlugins">
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
  onHide() { stopLogsFollow(); },
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
    const rows = [
      ["Total items",    (stats.total ?? 0).toLocaleString()],
      ["Enriched",       (stats.enriched ?? 0).toLocaleString()],
      ["Last sync",      stats.last_sync || "—"],
      ["Bookmarks dir",  info.bookmarks_dir],
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

  sections.push(pluginGroup("Exporters", d.exporters.map(e => ({
    name:   e.label || e.name,
    badges: e.supports_themes ? [`<span class="plugin-badge">themed</span>`] : [],
    sub:    e.description || e.module,
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

function onAdvNumberChange(key, el) {
  const v = el.value === "" ? null : Number(el.value);
  state.adv[key] = Number.isFinite(v) ? v : null;
  saveAdvToStorage();
  refreshAdvBadge();
  applyFilter();
}

function onAdvBoolChange(key, el) {
  state.adv[key] = el.checked;
  saveAdvToStorage();
  refreshAdvBadge();
  applyFilter();
}

els.advImpMin.addEventListener("input", () => onAdvNumberChange("impMin", els.advImpMin));
els.advImpMax.addEventListener("input", () => onAdvNumberChange("impMax", els.advImpMax));
els.advHasSummary.addEventListener("change", () => onAdvBoolChange("hasSummary", els.advHasSummary));
els.advHasNotes.addEventListener("change", () => onAdvBoolChange("hasNotes", els.advHasNotes));
els.advIncludeRemoved.addEventListener("change", () => onAdvBoolChange("includeRemoved", els.advIncludeRemoved));
els.advSearch.addEventListener("toggle", saveAdvToStorage);
els.advClear.addEventListener("click", (e) => {
  e.preventDefault();
  e.stopPropagation();
  clearAdvFilters();
});

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
    els.askSources.replaceChildren(...data.bookmarks.map((bm) => {
      // Map to a row — find the full bookmark for richer rendering.
      const full = state.all.find(x => (x.url || "").replace(/\/$/,"").toLowerCase() ===
                                        (bm.url || "").replace(/\/$/,"").toLowerCase())
                   || { ...bm, id: null, has_summary: !!bm.summary, tags: (bm.tags || "").split(", ").filter(Boolean) };
      const row = renderRow(
        { bm: full, titleMatches: [], urlMatches: [], vectorScore: bm._score },
        false
      );
      return row;
    }));
  } catch (err) {
    els.askStatus.textContent = "";
    els.askAnswer.textContent = `Error: ${err.message}`;
  }
}

// ─── Lists ─────────────────────────────────────────────────────────

async function loadLists() {
  try {
    const r = await fetch("/api/lists");
    if (!r.ok) return;
    state.lists = await r.json();
    renderListSuggestions();
    renderListsSidebar();
  } catch { /* optional */ }
}

function renderListSuggestions() {
  if (!els.listSuggestions) return;
  els.listSuggestions.innerHTML = state.lists
    .map(l => `<option value="${escapeHtml(l.name)}">`).join("");
}

function renderListsSidebar() {
  if (!els.statLists) return;
  if (!state.lists.length) {
    els.statLists.innerHTML = `<li class="bar-row"><div class="bar-label"><span>—</span></div></li>`;
    return;
  }
  const max = state.lists.reduce((m, l) => Math.max(m, l.count || 0), 1);
  const regular = state.lists.filter(l => !l.smart);
  const smart   = state.lists.filter(l =>  l.smart);

  const renderRow = (l) => {
    const pct = Math.max(4, Math.round(((l.count || 0) / max) * 100));
    const isActive = l.smart
      ? state.activeSmartList?.name === l.name
      : state.activeList === l.name;
    const active = isActive ? " active" : "";
    const icon = l.smart ? (l.icon || "⚡") : "📋";
    const title = l.smart && l.description ? ` title="${escapeHtml(l.description)}"` : "";
    const dataAttr = l.smart ? `data-smart="${escapeHtml(l.name)}"` : `data-list="${escapeHtml(l.name)}"`;
    return `<li class="bar-row clickable${active}" ${dataAttr}${title}>
      <div class="bar-fill" style="width:${pct}%"></div>
      <div class="bar-label"><span>${icon} ${escapeHtml(l.name)}</span><b>${l.count || 0}</b></div>
    </li>`;
  };

  let html = regular.map(renderRow).join("");
  if (smart.length) {
    html += `<li class="bar-row bar-divider"><div class="bar-label"><span class="muted">smart lists</span></div></li>`;
    html += smart.map(renderRow).join("");
  }
  els.statLists.innerHTML = html;

  els.statLists.querySelectorAll("li[data-list]").forEach(li => {
    li.addEventListener("click", () => setActiveList(li.dataset.list));
  });
  els.statLists.querySelectorAll("li[data-smart]").forEach(li => {
    li.addEventListener("click", () => setActiveSmartList(li.dataset.smart));
  });
}

function setActiveList(name) {
  // Activating a regular list clears any smart-list filter.
  state.activeSmartList = null;
  state.activeList = (state.activeList === name) ? null : name;
  els.listFilterClear?.classList.toggle("hidden",
    !state.activeList && !state.activeSmartList);
  renderListsSidebar();
  applyFilter();
}

function setActiveSmartList(name) {
  state.activeList = null;
  const spec = state.lists.find(l => l.smart && l.name === name) || null;
  state.activeSmartList = (state.activeSmartList?.name === name) ? null : spec;
  els.listFilterClear?.classList.toggle("hidden",
    !state.activeList && !state.activeSmartList);
  renderListsSidebar();
  applyFilter();
}

function renderDetailLists(d) {
  const lists = d.lists || [];
  els.detailLists.innerHTML = lists.map(l =>
    `<span class="tag list" data-list="${escapeHtml(l)}">📋 ${escapeHtml(l)} <button class="chip-x" title="Remove">×</button></span>`
  ).join("") || `<span class="muted">— not in any list</span>`;
  els.detailLists.querySelectorAll(".chip-x").forEach(btn => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const name = btn.closest("[data-list]").dataset.list;
      removeFromList(name);
    });
  });
}

async function addToList(name) {
  if (!state.currentId) return;
  name = String(name || "").trim();
  if (!name) return;
  const current = state.detail?.lists || [];
  if (current.includes(name)) return;
  const next = [...current, name];
  await saveLists(next);
}

async function removeFromList(name) {
  if (!state.currentId) return;
  const current = state.detail?.lists || [];
  const next = current.filter(l => l !== name);
  if (next.length === current.length) return;
  await saveLists(next);
}

async function saveLists(next) {
  try {
    const r = await fetch(`/api/bookmarks/${state.currentId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ lists: next }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    state.detail = await r.json();
    // Patch the in-memory list too.
    const i = state.all.findIndex(b => b.id === state.currentId);
    if (i >= 0) state.all[i] = { ...state.all[i], lists: state.detail.lists };
    renderDetailLists(state.detail);
    renderResults();
    loadLists();
  } catch (err) {
    showToast(`List update failed: ${err.message}`);
  }
}

els.listAddForm?.addEventListener("submit", (e) => {
  e.preventDefault();
  const name = els.listAddInput.value.trim();
  if (!name) return;
  els.listAddInput.value = "";
  addToList(name);
});

els.listFilterClear?.addEventListener("click", () => {
  if (state.activeSmartList) setActiveSmartList(state.activeSmartList.name);
  else if (state.activeList) setActiveList(state.activeList);
});

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

// ─── Export wizard ─────────────────────────────────────────────────
//
// Single-modal wizard: select items (lists / tags / filter / manual),
// pick an exporter + theme, set options, preview, run. Optionally saves
// the configuration as a YAML file under exports/configs/.

const exportState = {
  lists: new Set(),        // selected list names
  tags: new Set(),         // selected tag names
  filter: {},              // {source, kind, importance_min, importance_max}
  manual: new Set(),       // selected bookmark ids
  exporters: [],           // [{name, label, description, supports_themes, default_theme, options:[...]}]
  current: null,           // currently selected exporter object
  themes: [],              // [{name, label}]
  overrides: {},           // { itemId: {title?, summary?, notes?} } — preview-only edits
  smartLists: new Set(),   // selected smart list names
};

const expEls = {
  open: $("openExportBtn"),
  modal: $("exportModal"),
  close: $("exportClose"),
  loadConfig: $("exportLoadConfig"),
  count: $("exportCount"),
  selLists: $("selLists"),
  selSmartLists: $("selSmartLists"),
  selTags: $("selTags"),
  selFilterSource: $("selFilterSource"),
  selFilterKind: $("selFilterKind"),
  selFilterImpMin: $("selFilterImpMin"),
  selFilterImpMax: $("selFilterImpMax"),
  selManualSearch: $("selManualSearch"),
  selManualResults: $("selManualResults"),
  selManualPicked: $("selManualPicked"),
  exporterSelect: $("exporterSelect"),
  exporterDesc: $("exporterDesc"),
  themeRow: $("themeRow"),
  themeSelect: $("themeSelect"),
  options: $("exporterOptions"),
  exportName: $("exportName"),
  saveConfigCheck: $("saveConfigCheck"),
  previewBtn: $("previewBtn"),
  runBtn: $("runBtn"),
  status: $("exportStatus"),
  previewSection: $("previewSection"),
  previewHost: $("previewHost"),
  previewText: $("previewText"),
  previewResetBtn: $("previewResetBtn"),
};

function openExport() {
  expEls.modal.classList.remove("hidden");
  expEls.modal.setAttribute("aria-hidden", "false");
  refreshExporters();
  refreshExportPickers();
  refreshConfigsDropdown();
}
function closeExport() {
  expEls.modal.classList.add("hidden");
  expEls.modal.setAttribute("aria-hidden", "true");
}
expEls.open.addEventListener("click", openExport);
expEls.close.addEventListener("click", closeExport);
expEls.modal.addEventListener("click", (e) => {
  if (e.target === expEls.modal) closeExport();
});

// Tabs
document.querySelectorAll(".export-tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    const target = btn.dataset.tab;
    document.querySelectorAll(".export-tab").forEach(b => b.classList.toggle("active", b === btn));
    document.querySelectorAll(".export-tab-panel").forEach(p => {
      p.classList.toggle("active", p.dataset.panel === target);
    });
  });
});

// ── Picker rendering ────────────────────────────────────────────────

function refreshExportPickers() {
  // Lists: prefer already-loaded state.lists; fall back to scanning bookmarks.
  const listCounts = {};
  state.all.forEach(b => (b.lists || []).forEach(l => {
    listCounts[l] = (listCounts[l] || 0) + 1;
  }));
  renderChipPicker(expEls.selLists, Object.entries(listCounts).sort((a,b)=>b[1]-a[1]),
                   exportState.lists, (name) => { toggleSet(exportState.lists, name); updateExportCount(); });

  // Smart lists: pulled from /api/lists (already loaded into state.lists with smart=true).
  const smartEntries = state.lists
    .filter(l => l.smart)
    .map(l => [l.name, l.count || 0]);
  renderChipPicker(expEls.selSmartLists, smartEntries,
                   exportState.smartLists,
                   (name) => { toggleSet(exportState.smartLists, name); updateExportCount(); });

  // Tags: union across all bookmarks.
  const tagCounts = {};
  state.all.forEach(b => (b.tags || []).forEach(t => {
    tagCounts[t] = (tagCounts[t] || 0) + 1;
  }));
  renderChipPicker(expEls.selTags, Object.entries(tagCounts).sort((a,b)=>b[1]-a[1]),
                   exportState.tags, (name) => { toggleSet(exportState.tags, name); updateExportCount(); });

  // Filter dropdowns: source + kind derived from bookmarks.
  const sources = new Set(), kinds = new Set();
  state.all.forEach(b => { if (b.source) sources.add(b.source); if (b.kind) kinds.add(b.kind); });
  fillSelect(expEls.selFilterSource, ["", ...[...sources].sort()],
             (v) => v === "" ? "(any)" : v);
  fillSelect(expEls.selFilterKind, ["", ...[...kinds].sort()],
             (v) => v === "" ? "(any)" : v);

  renderManualPicked();
  updateExportCount();
}

function renderChipPicker(host, entries, selectedSet, onToggle) {
  host.innerHTML = "";
  if (entries.length === 0) {
    host.innerHTML = `<span class="hint-text">Nothing available.</span>`;
    return;
  }
  entries.forEach(([name, count]) => {
    const chip = document.createElement("span");
    chip.className = "pick" + (selectedSet.has(name) ? " selected" : "");
    chip.innerHTML = `${escapeHtml(name)}<span class="count">${count}</span>`;
    chip.addEventListener("click", () => {
      onToggle(name);
      chip.classList.toggle("selected");
    });
    host.appendChild(chip);
  });
}

function fillSelect(sel, values, label) {
  const prev = sel.value;
  sel.innerHTML = "";
  values.forEach(v => {
    const o = document.createElement("option");
    o.value = v;
    o.textContent = label(v);
    sel.appendChild(o);
  });
  if (values.includes(prev)) sel.value = prev;
}

function toggleSet(s, v) { s.has(v) ? s.delete(v) : s.add(v); }

// ── Manual picker ───────────────────────────────────────────────────

expEls.selManualSearch.addEventListener("input", () => {
  const q = expEls.selManualSearch.value.trim().toLowerCase();
  expEls.selManualResults.innerHTML = "";
  if (!q) return;
  const matches = state.all
    .filter(b => b.title.toLowerCase().includes(q) || b.url.toLowerCase().includes(q))
    .slice(0, 30);
  matches.forEach(b => {
    const li = document.createElement("li");
    li.innerHTML = `<span>${escapeHtml(b.title || "(untitled)")}</span>` +
                   `<span class="mr-url">${escapeHtml(domain(b.url))}</span>`;
    li.addEventListener("click", () => {
      exportState.manual.add(b.id);
      renderManualPicked();
      updateExportCount();
    });
    expEls.selManualResults.appendChild(li);
  });
});

function renderManualPicked() {
  expEls.selManualPicked.innerHTML = "";
  if (exportState.manual.size === 0) {
    expEls.selManualPicked.innerHTML = `<span class="hint-text">None picked yet.</span>`;
    return;
  }
  const byId = Object.fromEntries(state.all.map(b => [b.id, b]));
  [...exportState.manual].forEach(id => {
    const b = byId[id];
    if (!b) return;
    const chip = document.createElement("span");
    chip.className = "pick selected";
    chip.innerHTML = `${escapeHtml(b.title || b.url)} ✕`;
    chip.addEventListener("click", () => {
      exportState.manual.delete(id);
      renderManualPicked();
      updateExportCount();
    });
    expEls.selManualPicked.appendChild(chip);
  });
}

// Filter inputs → update count on change
[expEls.selFilterSource, expEls.selFilterKind, expEls.selFilterImpMin, expEls.selFilterImpMax]
  .forEach(el => el.addEventListener("input", () => {
    readFilterFromInputs();
    updateExportCount();
  }));

function readFilterFromInputs() {
  const src = expEls.selFilterSource.value || null;
  const kind = expEls.selFilterKind.value || null;
  const mn = expEls.selFilterImpMin.value === "" ? null : parseInt(expEls.selFilterImpMin.value, 10);
  const mx = expEls.selFilterImpMax.value === "" ? null : parseInt(expEls.selFilterImpMax.value, 10);
  exportState.filter = { source: src, kind, importance_min: mn, importance_max: mx };
}

function filterIsEmpty(f) {
  return !f || (f.source == null && f.kind == null && f.importance_min == null && f.importance_max == null);
}
function itemMatchesFilter(b, f) {
  if (filterIsEmpty(f)) return false;
  if (f.source != null && b.source !== f.source && !(b.sources || []).includes(f.source)) return false;
  if (f.kind != null && b.kind !== f.kind) return false;
  if (f.importance_min != null && (b.importance || 0) < f.importance_min) return false;
  if (f.importance_max != null && (b.importance || 0) > f.importance_max) return false;
  return true;
}

function updateExportCount() {
  const seen = new Set();
  // Pre-resolve picked smart-list specs.
  const smartSpecs = [...exportState.smartLists]
    .map(name => state.lists.find(l => l.smart && l.name === name))
    .filter(Boolean);
  let n = 0;
  for (const b of state.all) {
    const match =
      exportState.manual.has(b.id) ||
      (exportState.lists.size > 0 && (b.lists || []).some(l => exportState.lists.has(l))) ||
      (exportState.tags.size > 0 && (b.tags || []).some(t => exportState.tags.has(t))) ||
      itemMatchesFilter(b, exportState.filter) ||
      (smartSpecs.length > 0 && smartSpecs.some(s => smartListMatches(b, s)));
    if (!match) continue;
    const key = (b.url || b.id).replace(/\/$/, "").toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    n += 1;
  }
  expEls.count.textContent = `${n} item${n === 1 ? "" : "s"}`;
}

// ── Exporters + options ─────────────────────────────────────────────

async function refreshExporters() {
  if (exportState.exporters.length === 0) {
    const r = await fetch("/api/exporters");
    exportState.exporters = await r.json();
  }
  expEls.exporterSelect.innerHTML = "";
  exportState.exporters.forEach((e, i) => {
    const o = document.createElement("option");
    o.value = e.name;
    o.textContent = e.label || e.name;
    expEls.exporterSelect.appendChild(o);
  });
  if (exportState.exporters.length) {
    expEls.exporterSelect.value = exportState.current?.name || exportState.exporters[0].name;
    onExporterChange();
  }
}
expEls.exporterSelect.addEventListener("change", onExporterChange);

async function onExporterChange() {
  const name = expEls.exporterSelect.value;
  const cur = exportState.exporters.find(e => e.name === name);
  exportState.current = cur;
  expEls.exporterDesc.textContent = cur?.description || "";
  renderExporterOptions(cur);

  if (cur?.supports_themes) {
    expEls.themeRow.classList.remove("hidden");
    const r = await fetch(`/api/themes?exporter=${encodeURIComponent(name)}`);
    exportState.themes = await r.json();
    expEls.themeSelect.innerHTML = "";
    exportState.themes.forEach(t => {
      const o = document.createElement("option");
      o.value = t.name;
      o.textContent = t.label + (t.builtin ? " (built-in)" : "");
      expEls.themeSelect.appendChild(o);
    });
    if (cur.default_theme && [...expEls.themeSelect.options].some(o => o.value === cur.default_theme)) {
      expEls.themeSelect.value = cur.default_theme;
    }
  } else {
    expEls.themeRow.classList.add("hidden");
    exportState.themes = [];
  }
}

function renderExporterOptions(exp, preset = {}) {
  expEls.options.innerHTML = "";
  (exp?.options || []).forEach(o => {
    const wrap = document.createElement("div");
    wrap.className = "opt";
    const val = preset[o.name] !== undefined ? preset[o.name] : o.default;

    if (o.type === "bool") {
      wrap.className = "opt bool";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = !!val;
      cb.dataset.opt = o.name;
      cb.dataset.type = "bool";
      const lbl = document.createElement("span");
      lbl.textContent = o.label;
      wrap.appendChild(cb); wrap.appendChild(lbl);
    } else if (o.type === "select") {
      wrap.innerHTML = `<span>${escapeHtml(o.label)}</span>`;
      const sel = document.createElement("select");
      sel.dataset.opt = o.name;
      sel.dataset.type = "select";
      (o.choices || []).forEach(c => {
        const opt = document.createElement("option");
        opt.value = c; opt.textContent = c;
        sel.appendChild(opt);
      });
      sel.value = val ?? (o.choices || [])[0] ?? "";
      wrap.appendChild(sel);
    } else if (o.type === "multiselect") {
      wrap.className = "opt full";
      wrap.innerHTML = `<span>${escapeHtml(o.label)}</span>`;
      const group = document.createElement("div");
      group.className = "opt-multi";
      const current = new Set(Array.isArray(val) ? val : []);
      (o.choices || []).forEach(c => {
        const chip = document.createElement("span");
        chip.className = "pick" + (current.has(c) ? " selected" : "");
        chip.textContent = c;
        chip.dataset.opt = o.name;
        chip.dataset.type = "multiselect";
        chip.dataset.value = c;
        chip.addEventListener("click", () => chip.classList.toggle("selected"));
        group.appendChild(chip);
      });
      wrap.appendChild(group);
    } else if (o.type === "number") {
      wrap.innerHTML = `<span>${escapeHtml(o.label)}</span>`;
      const inp = document.createElement("input");
      inp.type = "number"; inp.dataset.opt = o.name; inp.dataset.type = "number";
      if (val != null) inp.value = val;
      wrap.appendChild(inp);
    } else {
      // string (default)
      wrap.innerHTML = `<span>${escapeHtml(o.label)}</span>`;
      const inp = document.createElement("input");
      inp.type = "text"; inp.dataset.opt = o.name; inp.dataset.type = "string";
      if (val != null) inp.value = val;
      wrap.appendChild(inp);
    }
    if (o.help) {
      const h = document.createElement("span");
      h.className = "hint-text";
      h.style.textTransform = "none";
      h.style.letterSpacing = "normal";
      h.textContent = o.help;
      wrap.appendChild(h);
    }
    expEls.options.appendChild(wrap);
  });
}

function collectOptions() {
  const opts = {};
  expEls.options.querySelectorAll("[data-opt][data-type]").forEach(el => {
    const name = el.dataset.opt;
    const type = el.dataset.type;
    if (type === "bool") {
      opts[name] = el.checked;
    } else if (type === "number") {
      opts[name] = el.value === "" ? null : Number(el.value);
    } else if (type === "multiselect") {
      if (el.classList.contains("selected")) {
        (opts[name] ||= []).push(el.dataset.value);
      } else if (!(name in opts)) {
        opts[name] = opts[name] || [];
      }
    } else {
      opts[name] = el.value;
    }
  });
  // multiselect: ensure the key exists even if nothing was selected
  (exportState.current?.options || []).forEach(o => {
    if (o.type === "multiselect" && !(o.name in opts)) opts[o.name] = [];
  });
  return opts;
}

// ── Run / preview ───────────────────────────────────────────────────

function buildRunPayload(saveConfigAs) {
  readFilterFromInputs();
  const opts = collectOptions();
  if (Object.keys(exportState.overrides).length > 0) {
    opts._overrides = exportState.overrides;
  }
  return {
    exporter: exportState.current?.name,
    theme: exportState.current?.supports_themes ? expEls.themeSelect.value : null,
    options: opts,
    selection: {
      lists: [...exportState.lists],
      tags: [...exportState.tags],
      filters: exportState.filter,
      manual_ids: [...exportState.manual],
      smart_lists: [...exportState.smartLists],
    },
    name: (expEls.exportName.value || exportState.current?.name || "export").trim(),
    save_config_as: saveConfigAs || null,
  };
}

async function runExport(save) {
  const saveAs = save ? (expEls.exportName.value || "").trim() : null;
  if (save && !saveAs) {
    setStatus("Enter an export name to save the configuration.", "error");
    return null;
  }
  if (!exportState.current) {
    setStatus("Pick an exporter.", "error");
    return null;
  }
  setStatus("Running…");
  const r = await fetch("/api/export/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(buildRunPayload(saveAs)),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    setStatus(err.detail || `HTTP ${r.status}`, "error");
    return null;
  }
  const data = await r.json();
  const msg = [`${data.item_count} items → ${data.artifact_path.split("/").pop()}`];
  if (data.config_path) msg.push(`saved config: ${data.config_path.split("/").pop()}`);
  setStatus(msg.join(" · "), "ok");
  return data;
}

function setStatus(msg, cls = "") {
  expEls.status.textContent = msg;
  expEls.status.className = "edit-status" + (cls ? " " + cls : "");
}

expEls.previewBtn.addEventListener("click", async () => {
  const data = await runExport(false);
  if (!data) return;
  expEls.previewSection.classList.remove("hidden");
  if (data.mime === "text/html" && data.preview_text != null) {
    expEls.previewText.classList.add("hidden");
    expEls.previewHost.classList.remove("hidden");
    renderEditablePreview(data.preview_text);
  } else if (data.preview_text != null) {
    expEls.previewHost.classList.add("hidden");
    expEls.previewText.classList.remove("hidden");
    if (data.mime === "application/json") {
      expEls.previewText.classList.add("preview-json");
      expEls.previewText.innerHTML = highlightJson(data.preview_text);
    } else {
      expEls.previewText.classList.remove("preview-json");
      expEls.previewText.textContent = data.preview_text;
    }
  } else {
    expEls.previewHost.classList.add("hidden");
    expEls.previewText.classList.remove("hidden");
    expEls.previewText.classList.remove("preview-json");
    expEls.previewText.textContent = "(no inline preview available)";
  }
});

expEls.previewResetBtn.addEventListener("click", () => {
  if (!confirm("Discard all inline edits to title, footer, and links?")) return;
  exportState.overrides = {};
  expEls.previewResetBtn.classList.add("hidden");
  // Re-render so the user sees the original values returned by the server.
  expEls.previewBtn.click();
});

// ── Editable preview (shadow DOM + pencil icons) ───────────────────

const EDIT_STYLES = `
  [data-edit-key] { position: relative; }
  [data-edit-key]:hover { outline: 1px dashed rgba(124,92,255,0.55); outline-offset: 2px; cursor: text; }
  .booki-pencil {
    position: absolute;
    top: -10px;
    right: -10px;
    width: 22px; height: 22px;
    display: none;
    align-items: center;
    justify-content: center;
    background: #7c5cff;
    color: white;
    border: none;
    border-radius: 50%;
    box-shadow: 0 2px 6px rgba(0,0,0,0.25);
    cursor: pointer;
    font-size: 11px;
    line-height: 1;
    padding: 0;
    z-index: 5;
  }
  [data-edit-key]:hover > .booki-pencil,
  .booki-pencil:hover { display: inline-flex; }
  [data-edit-key][contenteditable="true"] {
    outline: 2px solid #7c5cff;
    outline-offset: 2px;
    background: rgba(124,92,255,0.08);
    cursor: text;
  }
  [data-edit-key][contenteditable="true"] .booki-pencil { display: none; }
  /* link cards are inside <a>; while editing we don't want the click to navigate */
  a[href].booki-edit-noclick { pointer-events: none; }
  a[href] [data-edit-key] { pointer-events: auto; }
`;

function renderEditablePreview(html) {
  const host = expEls.previewHost;
  host.innerHTML = "";
  // Re-attach a fresh shadow root each render. attachShadow is idempotent-once,
  // so swap the host element if a shadow already exists.
  let shadowHost = host;
  if (host.shadowRoot) {
    const fresh = host.cloneNode(false);
    host.parentNode.replaceChild(fresh, host);
    expEls.previewHost = fresh;
    shadowHost = fresh;
  }
  const shadow = shadowHost.attachShadow({ mode: "open" });

  const doc = new DOMParser().parseFromString(html, "text/html");
  // Pull stylesheets/styles from <head> into the shadow root so the theme renders.
  doc.head.querySelectorAll("style, link[rel=stylesheet], meta[name=color-scheme]")
    .forEach((el) => shadow.appendChild(el.cloneNode(true)));
  // Inject editing affordances stylesheet.
  const editStyle = document.createElement("style");
  editStyle.textContent = EDIT_STYLES;
  shadow.appendChild(editStyle);
  // Move body children into shadow root.
  Array.from(doc.body.children).forEach((el) => shadow.appendChild(el.cloneNode(true)));

  attachEditHandlers(shadow);
  refreshResetVisibility();
}

function refreshResetVisibility() {
  const has = Object.keys(exportState.overrides).length > 0;
  expEls.previewResetBtn.classList.toggle("hidden", !has);
}

function attachEditHandlers(shadow) {
  // Stop link cards from navigating while we're editing.
  shadow.querySelectorAll("a[href]").forEach((a) => {
    a.addEventListener("click", (e) => {
      const target = e.target;
      if (target.closest && target.closest("[data-edit-key]")) {
        e.preventDefault();
      }
    });
  });

  shadow.querySelectorAll("[data-edit-key]").forEach((el) => {
    const key = el.getAttribute("data-edit-key");
    const isMulti = key === "link.summary" || key === "link.notes";
    // Add pencil button.
    const pencil = document.createElement("button");
    pencil.type = "button";
    pencil.className = "booki-pencil";
    pencil.title = "Edit";
    pencil.textContent = "✎";
    pencil.addEventListener("mousedown", (e) => e.preventDefault());
    pencil.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      beginEdit(el, isMulti);
    });
    // Position parent must be relative; data-edit-key rule already handles that.
    el.appendChild(pencil);

    // Click on the element itself also begins edit (more discoverable).
    el.addEventListener("click", (e) => {
      if (el.getAttribute("contenteditable") === "true") return;
      // Don't hijack clicks on the pencil itself.
      if (e.target === pencil) return;
      e.preventDefault();
      e.stopPropagation();
      beginEdit(el, isMulti);
    });
  });
}

function beginEdit(el, isMulti) {
  const original = el.textContent.replace(/\s*✎\s*$/, "").trim();
  // Strip the pencil from the editable text.
  const pencil = el.querySelector(".booki-pencil");
  if (pencil) pencil.remove();
  el.setAttribute("contenteditable", "true");
  el.textContent = original;
  el.focus();
  // Place cursor at end.
  const range = document.createRange();
  range.selectNodeContents(el);
  range.collapse(false);
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);

  let cancelled = false;

  const finish = (commit) => {
    if (el.getAttribute("contenteditable") !== "true") return;
    el.removeAttribute("contenteditable");
    el.removeEventListener("keydown", onKey);
    el.removeEventListener("blur", onBlur);
    const value = el.textContent.trim();
    if (commit) {
      saveEdit(el, value);
    } else {
      el.textContent = original;
    }
    // Re-attach the pencil for future hovers.
    const p = document.createElement("button");
    p.type = "button";
    p.className = "booki-pencil";
    p.title = "Edit";
    p.textContent = "✎";
    p.addEventListener("mousedown", (e) => e.preventDefault());
    p.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      beginEdit(el, isMulti);
    });
    el.appendChild(p);
  };

  const onKey = (e) => {
    if (e.key === "Escape") {
      cancelled = true;
      e.preventDefault();
      finish(false);
    } else if (e.key === "Enter" && !e.shiftKey && !isMulti) {
      e.preventDefault();
      finish(true);
    }
  };
  const onBlur = () => {
    if (!cancelled) finish(true);
  };
  el.addEventListener("keydown", onKey);
  el.addEventListener("blur", onBlur);
}

function saveEdit(el, value) {
  const key = el.getAttribute("data-edit-key");
  if (key === "title") {
    // Sync to the page-title Options input so a later run/save uses it.
    const titleInput = expEls.options.querySelector('[data-opt="title"]');
    if (titleInput) titleInput.value = value;
  } else if (key === "footer") {
    const footerInput = expEls.options.querySelector('[data-opt="footer"]');
    if (footerInput) footerInput.value = value;
  } else if (key === "link.title" || key === "link.summary" || key === "link.notes") {
    const li = el.closest("[data-item-id]");
    if (!li) return;
    const id = li.getAttribute("data-item-id");
    const field = key.split(".")[1];
    exportState.overrides[id] = exportState.overrides[id] || {};
    exportState.overrides[id][field] = value;
  }
  refreshResetVisibility();
}

function highlightJson(text) {
  const escaped = text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
  const re = /("(?:\\.|[^"\\])*"\s*:?)|(\b(?:true|false|null)\b)|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)|([{}\[\],])/g;
  return escaped.replace(re, (m, str, kw, num, punct) => {
    if (str) {
      const isKey = str.endsWith(":") || /:\s*$/.test(str);
      const cls = isKey ? "json-key" : "json-string";
      return `<span class="${cls}">${str}</span>`;
    }
    if (kw) return `<span class="json-${kw === "null" ? "null" : "bool"}">${kw}</span>`;
    if (num) return `<span class="json-number">${num}</span>`;
    if (punct) return `<span class="json-punct">${punct}</span>`;
    return m;
  });
}

expEls.runBtn.addEventListener("click", async () => {
  const save = expEls.saveConfigCheck.checked;
  const data = await runExport(save);
  if (!data) return;
  window.location.href = data.download_url;
  if (save) refreshConfigsDropdown();
});

// ── Saved configs ───────────────────────────────────────────────────

async function refreshConfigsDropdown() {
  const r = await fetch("/api/export/configs");
  const list = await r.json();
  expEls.loadConfig.innerHTML = `<option value="">Load saved…</option>`;
  list.forEach(c => {
    const o = document.createElement("option");
    o.value = c.name;
    o.textContent = `${c.name} (${c.exporter})`;
    expEls.loadConfig.appendChild(o);
  });
}

expEls.loadConfig.addEventListener("change", async () => {
  const name = expEls.loadConfig.value;
  if (!name) return;
  const r = await fetch(`/api/export/configs/${encodeURIComponent(name)}`);
  if (!r.ok) { setStatus(`Failed to load config '${name}'`, "error"); return; }
  const cfg = await r.json();

  // Populate selection
  exportState.lists = new Set(cfg.selection?.lists || []);
  exportState.tags = new Set(cfg.selection?.tags || []);
  exportState.manual = new Set(cfg.selection?.manual_ids || []);
  exportState.smartLists = new Set(cfg.selection?.smart_lists || []);
  const f = cfg.selection?.filters || {};
  expEls.selFilterSource.value = f.source || "";
  expEls.selFilterKind.value = f.kind || "";
  expEls.selFilterImpMin.value = f.importance_min ?? "";
  expEls.selFilterImpMax.value = f.importance_max ?? "";

  // Exporter + options
  expEls.exporterSelect.value = cfg.exporter;
  await onExporterChange();
  if (cfg.theme) expEls.themeSelect.value = cfg.theme;
  renderExporterOptions(exportState.current, cfg.options || {});
  expEls.exportName.value = cfg.name || "";

  refreshExportPickers();
  setStatus(`Loaded config: ${cfg.name}`, "ok");
  expEls.loadConfig.value = "";
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
loadSchema().then(loadBookmarks).then(loadStats).then(loadLists).catch(err => {
  els.count.textContent = `Error: ${err.message}`;
});
