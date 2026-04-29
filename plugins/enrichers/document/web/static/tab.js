// Documents tab — contributed by the document enricher plugin.
//
// Entry point: this module is dynamically imported by the host bootstrap
// after /api/tabs returns its TabContribution. The module's job is to call
// `booki.tabs.implement(id, behavior)` with the mount/onShow/onHide hooks;
// the host has already registered the tab's metadata.
//
// All cross-cutting helpers come from `window.booki`:
//   - booki.bookmarks.all()       → snapshot of all loaded bookmarks
//   - booki.search.{fuzzy,substring,useFuzzy}
//   - booki.ui.{escapeHtml,highlight,openDrawer,toast}
// Nothing reaches into private host internals.

const ICONS = {
  pdf: "📕",
  doc: "📘", docx: "📘", odt: "📘", pages: "📘", rtf: "📘",
  md: "📝",
  txt: "📃", rst: "📃", org: "📃",
  tex: "📐",
  epub: "📖", mobi: "📖", azw3: "📖", azw: "📖",
  csv: "📊", tsv: "📊",
};

const VIEW_KEY = "booki.documents.view";

function isDocument(b) {
  return b.kind === "document" || (b.sources || []).includes("document");
}

function docType(b) {
  const t = (b.extras && b.extras.document_type) || "";
  if (t) return String(t).toLowerCase();
  // Fall back to extension on URL path.
  const url = b.url || "";
  const m = url.split(/[?#]/, 1)[0].toLowerCase().match(/\.([a-z0-9]+)$/);
  return m ? m[1] : "";
}

function iconFor(b) {
  return ICONS[docType(b)] || "📄";
}

function readView() {
  try {
    const v = localStorage.getItem(VIEW_KEY);
    if (v === "list" || v === "grid") return v;
  } catch {}
  return "list";
}

function writeView(v) {
  try { localStorage.setItem(VIEW_KEY, v); } catch {}
}

booki.tabs.implement("documents", {
  mount(el) {
    el.innerHTML = `
      <div class="doc-tab scoped-tab">
        <header class="tab-header">
          <h2>📄 Documents</h2>
          <p class="tab-sub" id="docCount">—</p>
          <div class="doc-view-toggle" role="group" aria-label="View mode">
            <button type="button" class="doc-view-btn" data-view="list" title="List view">≡ List</button>
            <button type="button" class="doc-view-btn" data-view="grid" title="Grid view">▦ Grid</button>
          </div>
        </header>
        <div class="search-box scoped-search" id="docSearchBox">
          <span class="search-icon">🔎</span>
          <input id="docFindInput" type="search" autocomplete="off"
                 autocapitalize="off" autocorrect="off" spellcheck="false"
                 placeholder="Search documents by title or URL…">
          <span class="hint">↵ open · click for details</span>
        </div>
        <div id="docResults"></div>
        <p class="tab-empty hidden" id="docEmpty">
          No documents yet — the document enricher tags items by URL extension.<br>
          Run <code>booki sync --no-sync --enrich-meta --enricher document --all</code>
          to backfill.
        </p>
        <p class="tab-empty hidden" id="docNoMatch">
          No documents match your search.
        </p>
      </div>`;

    el.querySelector("#docFindInput")
      .addEventListener("input", () => render(el));
    el.querySelector("#docFindInput")
      .addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          const first = el.querySelector("[data-id]");
          const id = first?.dataset.id;
          const bm = id && booki.bookmarks.byId(id);
          if (bm?.url) window.open(bm.url, "_blank", "noopener");
        } else if (e.key === "Escape") {
          const inp = e.target;
          if (inp.value) { inp.value = ""; render(el); }
          else inp.blur();
        }
      });

    el.querySelectorAll(".doc-view-btn").forEach(btn => {
      btn.addEventListener("click", () => {
        writeView(btn.dataset.view);
        render(el);
      });
    });
  },
  onShow(el) {
    render(el);
    el.querySelector("#docFindInput")?.focus();
  },
});

function render(el) {
  const count   = el.querySelector("#docCount");
  const empty   = el.querySelector("#docEmpty");
  const noMatch = el.querySelector("#docNoMatch");
  const host    = el.querySelector("#docResults");
  const input   = el.querySelector("#docFindInput");
  const view    = readView();

  el.querySelectorAll(".doc-view-btn").forEach(b => {
    b.classList.toggle("active", b.dataset.view === view);
  });

  const all = booki.bookmarks.all().filter(isDocument);

  if (!all.length) {
    host.innerHTML = "";
    count.textContent = "0 documents";
    empty.classList.remove("hidden");
    noMatch.classList.add("hidden");
    return;
  }
  empty.classList.add("hidden");

  const q = (input.value || "").trim();
  let scored;
  if (q) {
    const match = booki.search.useFuzzy ? booki.search.fuzzy : booki.search.substring;
    scored = [];
    for (const b of all) {
      const tm = match(q, b.title || "");
      const um = match(q, b.url || "");
      const em = match(q, (b.tags || []).join(" "));
      if (!tm && !um && !em) continue;
      const score = (tm ? tm.score * 3   : 0)
                  + (um ? um.score * 1.2 : 0)
                  + (em ? em.score * 0.6 : 0)
                  + (b.importance || 0) * 0.5;
      scored.push({
        bm: b, score,
        titleMatches: tm ? tm.matches : [],
        urlMatches:   um ? um.matches : [],
      });
    }
    scored.sort((a, b) => b.score - a.score);
  } else {
    scored = [...all]
      .sort((a, b) => (b.importance || 0) - (a.importance || 0))
      .map(b => ({ bm: b, score: 0, titleMatches: [], urlMatches: [] }));
  }

  count.textContent = q
    ? `${scored.length} of ${all.length} documents`
    : (all.length === 1 ? "1 document" : `${all.length} documents`);

  if (!scored.length) {
    host.innerHTML = "";
    noMatch.classList.remove("hidden");
    return;
  }
  noMatch.classList.add("hidden");

  const rows = scored.slice(0, 200);
  host.innerHTML = view === "grid"
    ? renderGrid(rows)
    : renderList(rows);

  host.querySelectorAll("[data-id]").forEach(node => {
    node.addEventListener("click", () => {
      const id = node.dataset.id;
      if (id) booki.ui.openDrawer(id);
    });
    node.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        booki.ui.openDrawer(node.dataset.id);
      }
    });
  });
}

function renderList(rows) {
  const esc = booki.ui.escapeHtml, hl = booki.ui.highlight;
  const items = rows.map(({ bm, titleMatches, urlMatches }) => {
    const type = docType(bm);
    const icon = iconFor(bm);
    return `
      <li class="doc-row" tabindex="0" data-id="${esc(bm.id)}">
        <span class="doc-icon" aria-hidden="true">${icon}</span>
        <div class="doc-row-body">
          <div class="doc-row-title">${hl(bm.title || bm.url || "(untitled)", titleMatches)}</div>
          <div class="doc-row-url">${hl(bm.url || "", urlMatches)}</div>
        </div>
        <div class="doc-row-meta">
          ${type ? `<span class="doc-type-chip">${esc(type)}</span>` : ""}
          ${bm.importance ? `<span class="doc-imp">★${bm.importance}</span>` : ""}
        </div>
      </li>`;
  }).join("");
  return `<ul class="doc-list">${items}</ul>`;
}

const SUMMARY_MAX = 180;

function truncate(s, n) {
  const t = String(s || "").trim().replace(/\s+/g, " ");
  return t.length > n ? t.slice(0, n - 1).trimEnd() + "…" : t;
}

function renderGrid(rows) {
  const esc = booki.ui.escapeHtml;
  const tiles = rows.map(({ bm }) => {
    const type    = docType(bm);
    const icon    = iconFor(bm);
    const summary = truncate(bm.summary, SUMMARY_MAX);
    return `
      <li class="doc-tile" tabindex="0" data-id="${esc(bm.id)}">
        <div class="doc-tile-thumb"><span class="doc-tile-icon">${icon}</span></div>
        <div class="doc-tile-meta">
          <div class="doc-tile-title" title="${esc(bm.title || '')}">${esc(bm.title || "(untitled)")}</div>
          ${summary
            ? `<p class="doc-tile-summary" title="${esc(bm.summary || '')}">${esc(summary)}</p>`
            : ""}
          <div class="doc-tile-sub">
            ${type ? `<span class="doc-type-chip">${esc(type)}</span>` : ""}
            ${bm.importance ? `<span class="doc-imp">★${bm.importance}</span>` : ""}
          </div>
        </div>
      </li>`;
  }).join("");
  return `<ul class="doc-grid">${tiles}</ul>`;
}
