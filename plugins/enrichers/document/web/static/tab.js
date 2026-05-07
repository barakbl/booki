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
    const cur = readView();
    const btn = (id, glyph, label) => {
      const on = id === cur;
      return `<button type="button" class="view-btn${on ? " active" : ""}"
                      data-view="${id}" title="${label} view"
                      aria-pressed="${on ? "true" : "false"}">
        <span class="view-glyph" aria-hidden="true">${glyph}</span>
        <span class="view-label">${label}</span>
      </button>`;
    };
    el.innerHTML = `
      <div class="doc-tab scoped-tab">
        <header class="tab-header">
          <h2>📄 Documents</h2>
          <p class="tab-sub" id="docCount">—</p>
          <div class="view-toggle" role="group" aria-label="View mode">
            ${btn("list", "≡", "List")}
            ${btn("grid", "▦", "Grid")}
          </div>
          <button type="button" class="btn tab-export-btn" data-tab-export="documents"
                  title="Export the documents currently shown" disabled>Export</button>
        </header>
        <div class="search-box scoped-search" id="docSearchBox">
          <span class="search-icon">🔎</span>
          <input id="docFindInput" type="search" autocomplete="off"
                 autocapitalize="off" autocorrect="off" spellcheck="false"
                 placeholder="Search documents by title or URL…">
          <span class="hint">↵ open · click for details</span>
        </div>
        <div class="adv-host" id="docAdvHost"></div>
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

    el.querySelectorAll(".view-toggle .view-btn").forEach(b => {
      b.addEventListener("click", () => {
        writeView(b.dataset.view);
        render(el);
      });
    });

    const advHost = el.querySelector("#docAdvHost");
    if (advHost && booki.adv?.mountInto) {
      booki.adv.mountInto(advHost, { scope: "document" });
      // Re-render only when our own scope changes — other tabs' filters
      // don't affect this view.
      booki.adv.onChange((_adv, scope) => {
        if (scope === "document") render(el);
      });
    }
  },
  onShow(el) {
    render(el);
    el.querySelector("#docFindInput")?.focus();
    booki.ui?.refreshExportButton?.();
  },
  // Expose the currently-rendered document ids so the host's "⬇ Export"
  // header button activates and the wizard filters exporters by kind.
  getSelection() {
    const root = document.getElementById("docResults");
    if (!root) return { kind: "document", ids: [] };
    const ids = [...root.querySelectorAll("[data-id]")].map(n => n.dataset.id);
    return { kind: "document", ids };
  },
});

function render(el) {
  const count   = el.querySelector("#docCount");
  const empty   = el.querySelector("#docEmpty");
  const noMatch = el.querySelector("#docNoMatch");
  const host    = el.querySelector("#docResults");
  const input   = el.querySelector("#docFindInput");
  const view    = readView();

  el.querySelectorAll(".view-toggle .view-btn").forEach(b => {
    const on = b.dataset.view === view;
    b.classList.toggle("active", on);
    b.setAttribute("aria-pressed", on ? "true" : "false");
  });

  const advPred = booki.adv?.predicate?.("document") || (() => true);
  const all = booki.bookmarks.all().filter(isDocument).filter(advPred);
  const fullCount = booki.bookmarks.all().filter(isDocument).length;

  if (!fullCount) {
    host.innerHTML = "";
    count.textContent = "0 documents";
    empty.classList.remove("hidden");
    noMatch.classList.add("hidden");
    booki.ui?.refreshTabExportButton?.("documents");
    return;
  }
  empty.classList.add("hidden");

  const topSorts = !!booki.adv?.hasTopSort?.("document");
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
    if (topSorts) {
      const byId = new Map(scored.map(r => [r.bm.id, r]));
      const ordered = booki.adv.applySort(scored.map(r => r.bm), "document");
      scored = ordered.map(b => byId.get(b.id)).filter(Boolean);
    } else {
      scored.sort((a, b) => b.score - a.score);
    }
  } else if (topSorts) {
    scored = booki.adv.applySort(all, "document")
      .map(b => ({ bm: b, score: 0, titleMatches: [], urlMatches: [] }));
  } else {
    scored = [...all]
      .sort((a, b) => (b.importance || 0) - (a.importance || 0))
      .map(b => ({ bm: b, score: 0, titleMatches: [], urlMatches: [] }));
  }

  count.textContent = (q || all.length !== fullCount)
    ? `${scored.length} of ${fullCount} documents`
    : (fullCount === 1 ? "1 document" : `${fullCount} documents`);

  if (!scored.length) {
    host.innerHTML = "";
    noMatch.classList.remove("hidden");
    booki.ui?.refreshTabExportButton?.("documents");
    return;
  }
  noMatch.classList.add("hidden");

  // Top-sort already enforces its own count limit; skip the cap then.
  const rows = topSorts ? scored : scored.slice(0, 200);
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
  // Update the inline `<button data-tab-export="documents">` label with the
  // count of currently-rendered rows. Strictly per-tab — peer tabs aren't
  // touched.
  booki.ui?.refreshTabExportButton?.("documents");
}

function renderList(rows) {
  const esc = booki.ui.escapeHtml, hl = booki.ui.highlight;
  const topChipFor = (bm) => booki.adv?.topChip?.(bm, "document") || "";
  const actionsFor = (bm) => booki.ui.rowActionsHtml?.(bm) || "";
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
          ${topChipFor(bm)}
          ${type ? `<span class="doc-type-chip">${esc(type)}</span>` : ""}
          ${bm.importance ? `<span class="doc-imp">★${bm.importance}</span>` : ""}
          ${actionsFor(bm)}
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
  const topChipFor = (bm) => booki.adv?.topChip?.(bm, "document") || "";
  const actionsFor = (bm) => booki.ui.rowActionsHtml?.(bm) || "";
  const tiles = rows.map(({ bm }) => {
    const type    = docType(bm);
    const icon    = iconFor(bm);
    const summary = truncate(bm.summary, SUMMARY_MAX);
    const top     = topChipFor(bm);
    return `
      <li class="doc-tile" tabindex="0" data-id="${esc(bm.id)}">
        <div class="doc-tile-thumb">
          <span class="doc-tile-icon">${icon}</span>
          <div class="tile-actions">${actionsFor(bm)}</div>
        </div>
        <div class="doc-tile-meta">
          <div class="doc-tile-title" title="${esc(bm.title || '')}">${esc(bm.title || "(untitled)")}</div>
          ${summary
            ? `<p class="doc-tile-summary" title="${esc(bm.summary || '')}">${esc(summary)}</p>`
            : ""}
          <div class="doc-tile-sub">
            ${type ? `<span class="doc-type-chip">${esc(type)}</span>` : ""}
            ${bm.importance ? `<span class="doc-imp">★${bm.importance}</span>` : ""}
            ${top}
          </div>
        </div>
      </li>`;
  }).join("");
  return `<ul class="doc-grid">${tiles}</ul>`;
}
