/* Local Document Q&A: web interface.
 *
 * Plain JavaScript, no framework or build step. Every piece of data comes from the local API
 * (docqa/server.py); nothing here is simulated. All text from the API is escaped before it is
 * put into the page.
 */
"use strict";

/* ---------------------------------------------------------------------------------------
 * Small helpers
 * ------------------------------------------------------------------------------------- */

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

function esc(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

const ICON_PATHS = {
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  layers: '<path d="m12 3 9 5-9 5-9-5 9-5z"/><path d="m3 13 9 5 9-5"/>',
  help: '<circle cx="12" cy="12" r="9"/><path d="M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .9-1 1.6v.6"/><path d="M12 17h.01"/>',
  file: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M9 13h6M9 17h4"/>',
  moon: '<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  external: '<path d="M14 4h6v6M20 4l-9 9"/><path d="M19 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
  alert: '<circle cx="12" cy="12" r="9"/><path d="M12 7v6M12 16.5h.01"/>',
  check: '<path d="m5 12 5 5 9-10"/>',
  trash: '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/>',
  refresh: '<path d="M20 11a8 8 0 0 0-14.3-4.9L4 8"/><path d="M4 4v4h4M4 13a8 8 0 0 0 14.3 4.9L20 16"/><path d="M20 20v-4h-4"/>',
  download: '<path d="M12 4v11M7 10l5 5 5-5M5 20h14"/>',
  chat: '<path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.4A8 8 0 1 1 21 12z"/>',
  minus: '<circle cx="12" cy="12" r="9"/><path d="M8 12h8"/>',
  x: '<path d="M6 6l12 12M18 6 6 18"/>',
};

function icon(name) {
  return `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">${ICON_PATHS[name] || ""}</svg>`;
}

function fillIcons(root = document) {
  $$("[data-icon]", root).forEach((el) => { el.innerHTML = icon(el.dataset.icon); });
}

function plural(n, word, pluralWord) {
  return `${n} ${n === 1 ? word : pluralWord || word + "s"}`;
}

function formatDate(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  return Number.isNaN(date.getTime())
    ? iso.slice(0, 10)
    : date.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

function timeAgo(iso) {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const seconds = Math.max(0, (Date.now() - then) / 1000);
  if (seconds < 60) return "just now";
  const units = [[86400, "day"], [3600, "hour"], [60, "min"]];
  for (const [size, name] of units) {
    if (seconds >= size) return `${plural(Math.floor(seconds / size), name)} ago`;
  }
  return "";
}

function authorLine(authors, limit = 3) {
  if (!authors || !authors.length) return "No authors listed";
  return authors.length > limit ? `${authors.slice(0, limit).join(", ")} et al.` : authors.join(", ");
}

function storageGet(key, fallback) {
  try { const v = localStorage.getItem(key); return v === null ? fallback : JSON.parse(v); } catch (e) { return fallback; }
}
function storageSet(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* not essential */ }
}

/* ---------------------------------------------------------------------------------------
 * API
 * ------------------------------------------------------------------------------------- */

async function api(path, { method = "GET", body } = {}) {
  let response;
  try {
    response = await fetch(path, {
      method,
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    throw new Error("Could not reach the local server. Is `python app.py serve` still running?");
  }
  let data = null;
  try { data = await response.json(); } catch (e) { /* empty or non-JSON body */ }
  if (!response.ok) {
    const detail = data && data.detail;
    const message = typeof detail === "string" ? detail
      : Array.isArray(detail) ? detail.map((d) => d.msg).join("; ")
      : `The request failed (HTTP ${response.status}).`;
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return data;
}

/* ---------------------------------------------------------------------------------------
 * State
 * ------------------------------------------------------------------------------------- */

const IN_PROGRESS = ["queued", "downloading", "processing"];

// The public demo (docqa/demo.py) reports mode "demo": read-only, no search, no model.
function isDemo() {
  return state.status?.mode === "demo";
}
const INDEXED = ["full_text", "abstract_only"];

const state = {
  view: "discover",
  status: null,
  search: {
    query: "", sort: "relevance", results: [], total: null, loading: false, loadingMore: false,
    error: null, searched: false, lastQuery: "", selected: new Set(), expanded: new Set(), adding: false,
  },
  collection: { papers: [], stats: null, loaded: false, error: null, filter: "", chip: "all" },
  ask: {
    question: "", paperId: "", generate: storageGet("docqa-generate", true), loading: false, result: null,
    error: null, tab: "cited", showNearest: false, highlighted: null,
  },
};

/* ---------------------------------------------------------------------------------------
 * Status: sidebar services and badges
 * ------------------------------------------------------------------------------------- */

async function refreshStatus() {
  try {
    state.status = await api("/api/status");
  } catch (e) {
    state.status = null;
  }
  renderStatus();
  return state.status;
}

function setService(id, dotClass, detail, title) {
  const el = document.getElementById(id);
  if (!el) return;
  $(".dot", el).className = `dot ${dotClass}`;
  const detailEl = $(".service-detail", el);
  detailEl.textContent = detail;
  detailEl.title = title || "";
}

function renderStatus() {
  const s = state.status;
  if (!s) {
    setService("service-arxiv", "error", "Local server not reachable");
    setService("service-index", "error", "Local server not reachable");
    setService("service-model", "error", "Local server not reachable");
    return;
  }
  if (isDemo()) {
    setService("service-arxiv", "", "Turned off in the public demo", s.arxiv.message);
    setService("service-model", "", "Not available in the public demo", s.model.detail);
    $$('[data-view="discover"]').forEach((a) => { a.hidden = true; });
  } else {
    const arxivClass = { ok: "ok", error: "error" }[s.arxiv.state] || "warn";
    const arxivText = { ok: "Connected · last search OK", error: "Last request failed" }[s.arxiv.state] || "Ready · not contacted yet";
    setService("service-arxiv", arxivClass, arxivText, s.arxiv.message);
    if (s.model.available) setService("service-model", "ok", s.model.name, s.model.detail);
    else setService("service-model", "", "Not installed — passages only", s.model.detail);
  }
  setService("service-index", "ok", `${s.index.engine} · ${plural(s.index.chunks, "chunk")}`);

  const setBadge = (name, value) => {
    const badge = $(`[data-badge="${name}"]`);
    badge.hidden = !value;
    badge.textContent = value;
  };
  setBadge("collection", s.stats.papers);
  setBadge("ask", s.stats.indexed);
  if (s.stats.in_progress) startPolling();
}

/* ---------------------------------------------------------------------------------------
 * Polling while papers are being ingested
 * ------------------------------------------------------------------------------------- */

let pollTimer = null;

function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(pollOnce, 1500);
}

async function pollOnce() {
  try {
    await loadCollection();
  } catch (e) {
    return; // keep trying; the server may be busy
  }
  const stillWorking = state.collection.papers.some((p) => IN_PROGRESS.includes(p.status));
  refreshStatus();
  if (!stillWorking) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

// Papers the user started ingesting in this tab; each gets one "finished" message.
const watching = new Set();

async function loadCollection() {
  const data = await api("/api/papers");
  state.collection.papers = data.papers;
  state.collection.stats = data.stats;
  state.collection.loaded = true;
  state.collection.error = null;
  announceFinished(data.papers);
  // Keep search results in step with the collection (e.g. "In your collection · Indexed").
  const byId = new Map(data.papers.map((p) => [p.arxiv_id, p]));
  state.search.results = state.search.results.map((r) => byId.get(r.arxiv_id) || { ...r, in_collection: false, status: "not_ingested" });
  if (state.view === "collection") renderCollectionBody();
  if (state.view === "discover") renderSearchResults();
  return data;
}

function announceFinished(papers) {
  for (const paper of papers) {
    if (!watching.has(paper.arxiv_id) || IN_PROGRESS.includes(paper.status)) continue;
    watching.delete(paper.arxiv_id);
    const title = paper.title.length > 60 ? paper.title.slice(0, 57) + "…" : paper.title;
    if (paper.status === "full_text") toast(`Indexed “${title}” (${plural(paper.chunk_count, "passage")}).`, "success");
    else if (paper.status === "abstract_only") toast(`“${title}” is abstract only: ${paper.note}`, "info");
    else if (paper.status === "failed") toast(`Could not ingest “${title}”: ${paper.note}`, "error");
  }
}

/* ---------------------------------------------------------------------------------------
 * Toasts and dialog
 * ------------------------------------------------------------------------------------- */

function toast(message, kind = "info", action) {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.innerHTML = `<div class="toast-body">${esc(message)}</div>`;
  if (action) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn btn-sm";
    button.textContent = action.label;
    button.addEventListener("click", () => { action.run(); el.remove(); });
    el.appendChild(button);
  }
  const close = document.createElement("button");
  close.type = "button";
  close.className = "icon-button";
  close.setAttribute("aria-label", "Dismiss");
  close.innerHTML = icon("x");
  close.addEventListener("click", () => el.remove());
  el.appendChild(close);
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), kind === "error" ? 9000 : 6000);
}

function confirmDialog({ title, bodyHtml, confirmLabel, danger = true }) {
  return new Promise((resolve) => {
    const root = $("#modal-root");
    const previousFocus = document.activeElement;
    root.innerHTML = `
      <div class="modal-backdrop">
        <div class="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title">
          <h2 id="modal-title">${esc(title)}</h2>
          ${bodyHtml}
          <div class="actions">
            <button type="button" class="btn" data-answer="no">Cancel</button>
            <button type="button" class="btn ${danger ? "btn-danger" : "btn-primary"}" data-answer="yes">${esc(confirmLabel)}</button>
          </div>
        </div>
      </div>`;
    const finish = (answer) => {
      root.innerHTML = "";
      document.removeEventListener("keydown", onKey);
      if (previousFocus) previousFocus.focus();
      resolve(answer);
    };
    const onKey = (event) => { if (event.key === "Escape") finish(false); };
    document.addEventListener("keydown", onKey);
    $(".modal-backdrop", root).addEventListener("click", (event) => {
      if (event.target.classList.contains("modal-backdrop")) finish(false);
    });
    $$("[data-answer]", root).forEach((b) => b.addEventListener("click", () => finish(b.dataset.answer === "yes")));
    $('[data-answer="no"]', root).focus();
  });
}

/* ---------------------------------------------------------------------------------------
 * Router
 * ------------------------------------------------------------------------------------- */

const VIEWS = { discover: mountDiscover, collection: mountCollection, ask: mountAsk };

function route() {
  const home = isDemo() ? "ask" : "discover";
  const [path, query] = (location.hash.replace(/^#\/?/, "") || home).split("?");
  const view = VIEWS[path] ? path : home;
  const params = new URLSearchParams(query || "");
  state.view = view;
  $$("[data-view]").forEach((a) => {
    if (a.dataset.view === view) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  const titles = { discover: "Discover papers", collection: "Collection", ask: "Ask your collection" };
  document.title = `${titles[view]} · Local Document Q&A`;
  if (isDemo() && view === "discover") mountDemoDiscover();
  else VIEWS[view](params);
  if (isDemo()) $("#main").prepend(demoBanner());
  $("#main").focus({ preventScroll: true });
  window.scrollTo(0, 0);
}

function demoBanner() {
  const s = state.status;
  const el = document.createElement("div");
  el.className = "demo-banner";
  el.setAttribute("role", "note");
  el.innerHTML = s.demo.available
    ? `<strong>Public demo</strong> · read-only · ${plural(s.stats.papers, "openly licensed paper")} · passages only.
       <a href="${esc(s.demo.repo_url)}" target="_blank" rel="noopener">Run the full app on your computer</a> to search arXiv and build your own library.`
    : `<strong>Public demo unavailable:</strong> ${esc(s.demo.problem)}`;
  return el;
}

function mountDemoDiscover() {
  $("#main").innerHTML = `
    <p class="eyebrow">Step 1 · Find papers</p>
    <h1>Discover papers</h1>
    <div class="card state-card">
      <h2>Searching arXiv is turned off in this public demo</h2>
      <p>The demo only answers questions about a small, fixed set of openly licensed papers, and nobody can add
        or remove papers here. To search arXiv and build your own private library, run the full app on your computer.</p>
      <div class="actions">
        <a class="btn btn-primary" href="#/ask">Ask the demo papers</a>
        <a class="btn" href="#/collection">See the demo papers</a>
        <a class="btn" href="${esc(state.status.demo.repo_url)}" target="_blank" rel="noopener">How to run it yourself${icon("external")}</a>
      </div>
    </div>`;
}

function licenseLine(license) {
  if (!license) return "";
  return `<div class="license-line">${icon("file")}<span>Licensed <a href="${esc(license.url)}" target="_blank" rel="noopener">${esc(license.name)}</a>
    (<a href="${esc(license.evidence)}" target="_blank" rel="noopener">source</a>). ${esc(license.changes)}</span></div>`;
}

function go(view, params) {
  const hash = `#/${view}${params ? "?" + new URLSearchParams(params).toString() : ""}`;
  if (location.hash === hash) route();
  else location.hash = hash;
}

/* ---------------------------------------------------------------------------------------
 * Discover
 * ------------------------------------------------------------------------------------- */

function mountDiscover(params) {
  const s = state.search;
  if (params.get("q")) s.query = params.get("q");
  $("#main").innerHTML = `
    <p class="eyebrow">Step 1 · Find papers</p>
    <h1>Discover papers</h1>
    <p class="lede">Search arXiv by keyword, review the results, and add the papers you need to your local collection.</p>
    <div class="two-col">
      <div>
        <form class="card" id="search-form" role="search">
          <div class="search-row">
            <div class="search-field">
              <label class="field-label" for="search-input">Search arXiv</label>
              <div class="input-icon">${icon("search")}
                <input class="input" id="search-input" type="search" autocomplete="off" maxlength="300"
                  placeholder="e.g. retrieval augmented generation" value="${esc(s.query)}">
              </div>
            </div>
            <div>
              <label class="field-label" for="sort-select">Sort by</label>
              <select class="input" id="sort-select">
                <option value="relevance"${s.sort === "relevance" ? " selected" : ""}>Relevance</option>
                <option value="recent"${s.sort === "recent" ? " selected" : ""}>Newest</option>
              </select>
            </div>
            <button class="btn btn-primary" id="search-button" type="submit">Search</button>
          </div>
          <p class="note">${icon("info")}<span>Search results contain arXiv <strong>metadata and abstracts only</strong>.
            A paper’s full text is downloaded and indexed after you add it to your collection.</span></p>
        </form>
        <div id="search-results" aria-live="polite"></div>
      </div>
      <aside class="rail">
        <section class="card">
          <h2>What adding a paper does</h2>
          <ol class="steps">
            <li><span class="step-num">1</span><div><strong>Save metadata</strong>Title, authors, abstract and links are stored in a local SQLite file.</div></li>
            <li><span class="step-num">2</span><div><strong>Download the PDF</strong>Fetched from arXiv once, then reused.</div></li>
            <li><span class="step-num">3</span><div><strong>Extract and chunk text</strong>Split page by page, so every passage keeps its page number.</div></li>
            <li><span class="step-num">4</span><div><strong>Index for search</strong>Passages go into a keyword index (SQLite FTS5, BM25 ranking).</div></li>
          </ol>
          <div class="divider"></div>
          <p class="muted" style="margin:0;font-size:13.5px">If the PDF can’t be downloaded or has no extractable text, only the abstract
            is indexed and the paper is marked <strong style="color:var(--yellow-text)">abstract only</strong>.</p>
        </section>
        <section class="card">
          <h2>Status guide</h2>
          <div class="guide">
            <div>${statusPill({ status: "not_ingested" })}<div>Saved, but no text is indexed yet.</div></div>
            <div>${statusPill({ status: "downloading", progress: null })}<div>The PDF is being fetched from arXiv.</div></div>
            <div>${statusPill({ status: "processing" })}<div>Text is being extracted and indexed.</div></div>
            <div>${statusPill({ status: "full_text" })}<div>Full text is searchable, with page numbers.</div></div>
            <div>${statusPill({ status: "abstract_only" })}<div>Only the abstract is searchable; the reason is shown.</div></div>
            <div>${statusPill({ status: "failed" })}<div>Something went wrong; you can retry.</div></div>
          </div>
        </section>
      </aside>
    </div>
    <div id="selection-bar"></div>`;

  $("#search-form").addEventListener("submit", (event) => {
    event.preventDefault();
    s.query = $("#search-input").value.trim();
    s.sort = $("#sort-select").value;
    runSearch();
  });
  renderSearchResults();
  renderSelectionBar();
  if (params.get("q") && params.get("run") === "1") runSearch();
  else if (!s.searched) $("#search-input").focus();
}

async function runSearch({ more = false } = {}) {
  const s = state.search;
  if (!s.query) {
    toast("Type some words to search for.", "info");
    $("#search-input")?.focus();
    return;
  }
  if (more) s.loadingMore = true;
  else {
    s.loading = true;
    s.error = null;
    s.lastQuery = s.query;
    s.selected.clear();
    s.expanded.clear();
  }
  renderSearchResults();
  renderSelectionBar();
  const start = more ? s.results.length : 0;
  try {
    const data = await api(`/api/search?${new URLSearchParams({ q: s.lastQuery, sort: s.sort, start, max: 10 })}`);
    s.results = more ? s.results.concat(data.results) : data.results;
    s.total = data.total_results;
    s.skipped = data.skipped;
    s.searched = true;
  } catch (error) {
    if (more) toast(error.message, "error");
    else { s.error = error.message; s.results = []; s.searched = true; }
  } finally {
    s.loading = false;
    s.loadingMore = false;
    refreshStatus();
    if (state.view === "discover") { renderSearchResults(); renderSelectionBar(); }
  }
}

function renderSearchResults() {
  const root = $("#search-results");
  if (!root) return;
  const s = state.search;
  const button = $("#search-button");
  if (button) button.disabled = s.loading;

  if (s.loading) {
    root.innerHTML = `
      <div class="results-head"><span class="spinner" style="color:var(--link)"></span>
        <h2>Searching arXiv for “${esc(s.lastQuery)}”…</h2></div>
      ${'<div class="skeleton"><span style="width:62%"></span><span style="width:30%"></span><span></span><span style="width:88%"></span><span style="width:24%"></span></div>'.repeat(3)}`;
    return;
  }
  if (s.error) {
    root.innerHTML = `
      <div class="card state-card error" style="margin-top:24px" role="alert">
        <h2>${icon("alert")}arXiv search didn’t complete</h2>
        <p style="color:var(--text)">${esc(s.error)}</p>
        <p>Your collection was not changed. arXiv limits requests to one every 3 seconds and is sometimes busy,
          so trying again in a moment usually works.</p>
        <div class="actions">
          <button type="button" class="btn btn-primary" data-action="retry">${icon("refresh")}Try again</button>
          <a class="btn" href="#/collection">Open your collection</a>
        </div>
      </div>`;
    $('[data-action="retry"]', root).addEventListener("click", () => runSearch());
    return;
  }
  if (!s.searched) {
    root.innerHTML = `<p class="note" style="margin-top:18px">${icon("info")}<span>Tip: use quotes for an exact phrase
      (<code>"large language model"</code>) or arXiv’s own syntax, e.g. <code>ti:transformer AND cat:cs.CL</code>.</span></p>`;
    return;
  }
  if (!s.results.length) {
    root.innerHTML = `
      <div class="card state-card" style="margin-top:24px">
        <h2>No papers matched “${esc(s.lastQuery)}”</h2>
        <p>Try fewer or broader keywords, or check the spelling. Every word you type must appear in a paper’s
          metadata, so shorter searches find more.</p>
      </div>`;
    return;
  }

  const selectable = s.results.filter((r) => !r.in_collection);
  const total = s.total != null && s.total > s.results.length ? ` of about ${s.total.toLocaleString()}` : "";
  root.innerHTML = `
    <div class="results-head">
      <h2>${s.results.length}${esc(total)} results for “${esc(s.lastQuery)}”</h2>
      <span class="tag-sample" style="background:var(--blue-soft);color:var(--link)">Live from arXiv</span>
    </div>
    <div class="results-actions">
      ${selectable.length ? `<button type="button" class="link-button" data-action="select-all">Select all not in collection</button>` : ""}
      ${s.skipped ? `<span class="muted" style="font-size:13px">${plural(s.skipped, "malformed result")} skipped</span>` : ""}
    </div>
    <div id="result-list">${s.results.map(resultCard).join("")}</div>
    ${s.total != null && s.results.length < s.total ? `
      <div style="text-align:center;margin-top:8px">
        <button type="button" class="btn" data-action="more" ${s.loadingMore ? "disabled" : ""}>
          ${s.loadingMore ? '<span class="spinner"></span>Loading…' : "Load more results"}</button>
      </div>` : ""}`;

  $('[data-action="select-all"]', root)?.addEventListener("click", () => {
    selectable.forEach((r) => s.selected.add(r.arxiv_id));
    renderSearchResults();
    renderSelectionBar();
  });
  $('[data-action="more"]', root)?.addEventListener("click", () => runSearch({ more: true }));
  $$(".paper-card", root).forEach((card) => {
    const id = card.dataset.id;
    $(".select-box", card)?.addEventListener("change", (event) => {
      if (event.target.checked) s.selected.add(id); else s.selected.delete(id);
      card.classList.toggle("selected", event.target.checked);
      renderSelectionBar();
    });
    $('[data-action="expand"]', card)?.addEventListener("click", () => {
      if (s.expanded.has(id)) s.expanded.delete(id); else s.expanded.add(id);
      renderSearchResults();
    });
    $('[data-action="add"]', card)?.addEventListener("click", () => addPapers([id]));
  });
}

function resultCard(paper) {
  const s = state.search;
  const selected = s.selected.has(paper.arxiv_id);
  const expanded = s.expanded.has(paper.arxiv_id);
  const long = paper.summary.length > 280;
  return `
    <article class="card paper-card${selected ? " selected" : ""}${paper.in_collection ? " in-collection" : ""}" data-id="${esc(paper.arxiv_id)}">
      ${paper.in_collection ? "" : `<input type="checkbox" class="select-box" ${selected ? "checked" : ""}
        aria-label="Select this paper: ${esc(paper.title)}">`}
      <h3>${esc(paper.title)}</h3>
      <div class="byline">${esc(authorLine(paper.authors))} · ${esc(formatDate(paper.published))}</div>
      <p class="abstract${expanded ? " expanded" : ""}">${esc(paper.summary)}</p>
      <div class="card-foot">
        ${arxivLink(paper)}
        ${paper.primary_category ? `<span class="chip">${esc(paper.primary_category)}</span>` : ""}
        ${paper.in_collection ? "" : `<span class="chip">Metadata + abstract</span>`}
        ${long ? `<button type="button" class="link-button" data-action="expand">${expanded ? "Show less" : "Show full abstract"}</button>` : ""}
        <span class="push">
          ${paper.in_collection
            ? `<a class="pill ${statusTone(paper.status)}" href="#/collection" title="Open your collection">In your collection · ${esc(statusLabel(paper))}</a>`
            : `<button type="button" class="btn btn-sm" data-action="add" ${s.adding ? "disabled" : ""}>Add</button>`}
        </span>
      </div>
    </article>`;
}

function arxivLink(paper) {
  return `<a class="arxiv-link" href="${esc(paper.abs_url)}" target="_blank" rel="noopener">arXiv: ${esc(paper.arxiv_id)}${icon("external")}
    <span class="visually-hidden">(opens arXiv in a new tab)</span></a>`;
}

function renderSelectionBar() {
  const root = $("#selection-bar");
  if (!root) return;
  const s = state.search;
  const ids = Array.from(s.selected);
  if (!ids.length || s.loading) { root.innerHTML = ""; return; }
  const titles = ids.map((id) => s.results.find((r) => r.arxiv_id === id)?.title || id)
    .map((t) => (t.length > 44 ? t.slice(0, 42) + "…" : t)).join(" · ");
  root.innerHTML = `
    <div class="selection-bar" role="region" aria-label="Selected papers">
      <div class="summary"><strong>${plural(ids.length, "paper")} selected</strong><div class="titles">${esc(titles)}</div></div>
      <button type="button" class="btn" data-action="clear">Clear</button>
      <button type="button" class="btn btn-accent" data-action="add-selected" ${s.adding ? "disabled" : ""}>
        ${s.adding ? '<span class="spinner"></span>Adding…' : `Add ${plural(ids.length, "paper")} to collection`}</button>
    </div>`;
  $('[data-action="clear"]', root).addEventListener("click", () => {
    s.selected.clear();
    renderSearchResults();
    renderSelectionBar();
  });
  $('[data-action="add-selected"]', root).addEventListener("click", () => addPapers(ids));
}

async function addPapers(ids) {
  const s = state.search;
  s.adding = true;
  renderSearchResults();
  renderSelectionBar();
  try {
    const result = await api("/api/collection", { method: "POST", body: { ids } });
    ids.forEach((id) => s.selected.delete(id));
    result.queued.forEach((id) => watching.add(id));
    const queued = result.queued.length;
    if (queued) {
      toast(`Added ${plural(queued, "paper")}. Downloading and indexing in the background.`, "success",
        { label: "Open collection", run: () => go("collection") });
    }
    if (result.already_in_collection.length) {
      toast(`${plural(result.already_in_collection.length, "paper")} already indexed; nothing to download.`, "info");
    }
    result.problems.forEach((p) => toast(`${p.id}: ${p.note}`, "error"));
    await loadCollection();
    startPolling();
  } catch (error) {
    toast(`Could not add papers: ${error.message}`, "error");
  } finally {
    s.adding = false;
    refreshStatus();
    if (state.view === "discover") { renderSearchResults(); renderSelectionBar(); }
  }
}

/* ---------------------------------------------------------------------------------------
 * Status labels (shared by Discover and Collection)
 * ------------------------------------------------------------------------------------- */

function statusLabel(paper) {
  switch (paper.status) {
    case "not_ingested": return "Metadata only";
    case "queued": return "Queued";
    case "downloading": return paper.progress != null ? `Downloading ${paper.progress}%` : "Downloading";
    case "processing": return "Processing";
    case "full_text": return "Indexed";
    case "abstract_only": return "Indexed · abstract only";
    case "failed": return "Failed";
    default: return paper.status;
  }
}

function statusTone(status) {
  return { full_text: "green", abstract_only: "yellow", failed: "red", downloading: "blue", processing: "blue", queued: "blue" }[status] || "";
}

function statusPill(paper) {
  const spinning = IN_PROGRESS.includes(paper.status) ? '<span class="spinner"></span>' : "";
  const glyph = { full_text: icon("check"), abstract_only: icon("file"), failed: icon("alert") }[paper.status] || "";
  return `<span class="pill ${statusTone(paper.status)}">${spinning}${glyph}${esc(statusLabel(paper))}</span>`;
}

/* ---------------------------------------------------------------------------------------
 * Collection
 * ------------------------------------------------------------------------------------- */

const COLLECTION_FILTERS = {
  all: { label: "All", test: () => true },
  indexed: { label: "Indexed", test: (p) => p.status === "full_text" },
  progress: { label: "In progress", test: (p) => IN_PROGRESS.includes(p.status) },
  attention: { label: "Needs attention", test: (p) => p.status === "failed" || p.status === "abstract_only" },
  metadata: { label: "Metadata only", test: (p) => p.status === "not_ingested" },
};

function mountCollection() {
  $("#main").innerHTML = `
    <p class="eyebrow">Step 2 · Build your library</p>
    <h1>Collection</h1>
    <p class="lede">Papers saved on this computer. Only indexed papers are searched when you ask a question.</p>
    <div id="collection-body" aria-live="polite">
      <div class="skeleton"><span style="width:50%"></span><span style="width:30%"></span></div>
    </div>`;
  loadCollection().catch((error) => {
    state.collection.error = error.message;
    renderCollectionBody();
  });
  if (state.collection.loaded) renderCollectionBody();
}

function renderCollectionBody() {
  const root = $("#collection-body");
  if (!root) return;
  const c = state.collection;
  if (c.error && !c.loaded) {
    root.innerHTML = `<div class="card state-card error" role="alert"><h2>${icon("alert")}Could not load your collection</h2>
      <p style="color:var(--text)">${esc(c.error)}</p></div>`;
    return;
  }
  if (!c.papers.length) {
    root.innerHTML = `
      <div class="card state-card dashed">
        <div class="empty-icon">${icon("layers")}</div>
        <h2>Your collection is empty</h2>
        <p>Papers you add from Discover appear here, with their download and indexing status.
          Once a paper is indexed you can ask questions about it.</p>
        <div class="actions"><a class="btn btn-primary" href="#/discover">${icon("search")}Find papers in Discover</a></div>
      </div>`;
    return;
  }

  const st = c.stats;
  const inProgress = c.papers.filter((p) => IN_PROGRESS.includes(p.status)).length;
  const filterText = c.filter.trim().toLowerCase();
  const visible = c.papers.filter((p) => COLLECTION_FILTERS[c.chip].test(p)).filter((p) => !filterText
    || p.title.toLowerCase().includes(filterText)
    || p.arxiv_id.toLowerCase().includes(filterText)
    || p.authors.some((a) => a.toLowerCase().includes(filterText)));

  // Keep the filter input (and its cursor) intact while the list refreshes during polling.
  let listRoot = $("#collection-list", root);
  if (!listRoot) {
    root.innerHTML = `
      <div class="stat-row" id="collection-stats"></div>
      <div class="filter-row">
        <div class="filter-input">
          <label class="field-label" for="collection-filter">Filter collection</label>
          <input class="input" id="collection-filter" type="search" placeholder="Title, author or arXiv ID" value="${esc(c.filter)}">
        </div>
        <div class="chips" id="collection-chips" role="group" aria-label="Filter by status"></div>
      </div>
      <div id="collection-banner"></div>
      <div id="collection-list"></div>`;
    $("#collection-filter", root).addEventListener("input", (event) => {
      c.filter = event.target.value;
      renderCollectionBody();
    });
    listRoot = $("#collection-list", root);
  }

  $("#collection-stats", root).innerHTML = `
    <span class="stat"><b>${st.papers}</b>${st.papers === 1 ? "paper" : "papers"}</span>
    <span class="stat"><b>${st.indexed}</b>indexed</span>
    <span class="stat"><b>${st.abstract_only}</b>abstract only</span>
    <span class="stat"><b>${st.chunks}</b>searchable chunks</span>`;
  $("#collection-chips", root).innerHTML = Object.entries(COLLECTION_FILTERS).map(([key, f]) =>
    `<button type="button" class="chip-button" data-chip="${key}" aria-pressed="${c.chip === key}">
      ${f.label} (${c.papers.filter(f.test).length})</button>`).join("");
  $$("[data-chip]", root).forEach((b) => b.addEventListener("click", () => { c.chip = b.dataset.chip; renderCollectionBody(); }));
  $("#collection-banner", root).innerHTML = inProgress
    ? `<div class="banner"><span class="spinner"></span>Ingesting ${plural(inProgress, "paper")}. Statuses update automatically, and you can keep working.</div>`
    : "";

  listRoot.innerHTML = visible.length ? visible.map(collectionCard).join("")
    : `<div class="card state-card"><p style="margin:0">No papers match this filter.</p></div>`;
  $$(".col-card", listRoot).forEach((card) => {
    const id = card.dataset.id;
    $$("[data-action]", card).forEach((b) => b.addEventListener("click", () => collectionAction(b.dataset.action, id)));
  });
}

function collectionCard(p) {
  const busy = IN_PROGRESS.includes(p.status);
  let detail = "";
  let progress = "";
  let actions = "";
  switch (p.status) {
    case "not_ingested":
      detail = "Only the title and abstract are saved. Download the full text to make this paper searchable.";
      actions = `<button type="button" class="btn btn-primary btn-sm" data-action="ingest">${icon("download")}Download &amp; index full text</button>`;
      break;
    case "queued":
      detail = "Waiting for the previous download to finish (arXiv allows one request every 3 seconds).";
      progress = `<div class="progress indeterminate"><span></span></div>`;
      break;
    case "downloading":
      detail = "Downloading the PDF from arXiv…";
      progress = p.progress != null
        ? `<div class="progress" role="progressbar" aria-valuenow="${p.progress}" aria-valuemin="0" aria-valuemax="100"><span style="width:${p.progress}%"></span></div>`
        : `<div class="progress indeterminate"><span></span></div>`;
      break;
    case "processing":
      detail = "Extracting text page by page and adding it to the search index…";
      progress = `<div class="progress indeterminate"><span></span></div>`;
      break;
    case "full_text":
      detail = `${plural(p.page_count || 0, "page")} · ${plural(p.chunk_count, "searchable passage")}`
        + (p.newer_version_available ? ` · ${esc(p.ingested_version)} indexed; ${esc(p.version)} is available` : "");
      actions = `<button type="button" class="btn btn-sm" data-action="ask">${icon("chat")}Ask about this paper</button>`
        + (p.newer_version_available ? `<button type="button" class="btn btn-sm" data-action="reindex">${icon("refresh")}Index ${esc(p.version)}</button>` : "");
      break;
    case "abstract_only":
      detail = `${esc(capitalize(p.note))}. Only the abstract is searchable.`;
      actions = `<button type="button" class="btn btn-sm" data-action="ask">${icon("chat")}Ask about this paper</button>
        <button type="button" class="btn btn-sm" data-action="ingest">${icon("refresh")}Retry full text</button>`;
      break;
    case "failed":
      detail = `${esc(capitalize(p.note))}.`;
      actions = `<button type="button" class="btn btn-primary btn-sm" data-action="ingest">${icon("refresh")}Retry ingestion</button>`;
      break;
  }
  return `
    <article class="card col-card" data-id="${esc(p.arxiv_id)}">
      <div class="col-top">
        <div>
          <h3>${esc(p.title)}</h3>
          <div class="byline">${esc(authorLine(p.authors))} · ${esc(formatDate(p.published))}</div>
          <div class="meta-row">${arxivLink(p)}
            ${p.primary_category ? `<span class="chip">${esc(p.primary_category)}</span>` : ""}
            ${isDemo() ? "" : `<span>Saved ${esc(timeAgo(p.first_saved_at))}</span>`}</div>
          ${licenseLine(p.license)}
        </div>
        <div class="status-box">
          ${statusPill(p)}
          ${progress}
          <div class="detail${p.status === "failed" ? " error" : ""}">${p.status === "full_text" || p.status === "abstract_only" || p.status === "failed" ? detail : esc(detail)}</div>
        </div>
      </div>
      <div class="col-actions">
        ${isDemo() ? `<button type="button" class="btn btn-sm" data-action="ask">${icon("chat")}Ask about this paper</button>` : actions}
        <a class="btn btn-ghost btn-sm" href="${esc(p.abs_url)}" target="_blank" rel="noopener">View source on arXiv${icon("external")}</a>
        ${isDemo() ? "" : `<button type="button" class="btn btn-ghost danger btn-sm push" data-action="remove" ${busy ? 'disabled title="Wait until ingestion finishes"' : ""}>${icon("trash")}Remove</button>`}
      </div>
    </article>`;
}

function capitalize(text) {
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : "";
}

async function collectionAction(action, id) {
  const paper = state.collection.papers.find((p) => p.arxiv_id === id);
  if (!paper) return;
  if (action === "ask") {
    go("ask", { paper: id });
    return;
  }
  if (action === "ingest" || action === "reindex") {
    try {
      await api(`/api/papers/${encodeURIComponent(id)}/ingest`, { method: "POST", body: { force: action === "reindex" } });
      watching.add(id);
      await loadCollection();
      startPolling();
    } catch (error) {
      toast(error.message, "error");
    }
    return;
  }
  if (action === "remove") {
    const ok = await confirmDialog({
      title: "Remove this paper?",
      bodyHtml: `<p>“${esc(paper.title)}” will be removed from your collection. This deletes its saved metadata,
        the downloaded PDF and ${plural(paper.chunk_count, "indexed chunk")} from this computer.</p>
        <p class="small">The paper stays on arXiv, and you can add it again from Discover.</p>`,
      confirmLabel: "Remove paper",
    });
    if (!ok) return;
    try {
      const removed = await api(`/api/papers/${encodeURIComponent(id)}`, { method: "DELETE" });
      toast(`Removed “${removed.title}” (${plural(removed.chunks_deleted, "chunk")}, ${plural(removed.pdfs_deleted, "PDF")}).`, "success");
      if (state.ask.paperId === id) state.ask.paperId = "";
      await loadCollection();
      refreshStatus();
    } catch (error) {
      toast(error.message, "error");
    }
  }
}

/* ---------------------------------------------------------------------------------------
 * Ask
 * ------------------------------------------------------------------------------------- */

async function mountAsk(params) {
  const a = state.ask;
  if (params.get("paper")) a.paperId = params.get("paper");
  $("#main").innerHTML = `
    <p class="eyebrow">Step 3 · Ask</p>
    <h1>Ask your collection</h1>
    <p class="lede">Answers are built only from passages in your indexed papers, and every claim links back to the passage it came from.</p>
    <div id="ask-body"><div class="skeleton"><span style="width:40%"></span><span></span></div></div>`;
  try {
    await Promise.all([loadCollection(), refreshStatus()]);
  } catch (error) {
    $("#ask-body").innerHTML = `<div class="card state-card error" role="alert"><h2>${icon("alert")}Could not load your collection</h2>
      <p style="color:var(--text)">${esc(error.message)}</p></div>`;
    return;
  }
  if (state.view !== "ask") return;
  renderAskBody();
}

function indexedPapers() {
  return state.collection.papers.filter((p) => INDEXED.includes(p.status));
}

function renderAskBody() {
  const root = $("#ask-body");
  if (!root) return;
  const a = state.ask;
  const papers = indexedPapers();
  if (!papers.length) {
    root.innerHTML = `
      <div class="card state-card dashed">
        <div class="empty-icon">${icon("help")}</div>
        <h2>No indexed papers yet</h2>
        <p>Questions are answered only from papers whose text has been indexed. Add papers from Discover and wait until
          they show <strong style="color:var(--green)">Indexed</strong> in your collection.</p>
        <div class="actions"><a class="btn btn-primary" href="#/discover">Find papers</a><a class="btn" href="#/collection">Open collection</a></div>
      </div>`;
    return;
  }
  if (a.paperId && !papers.some((p) => p.arxiv_id === a.paperId)) a.paperId = "";
  const model = state.status?.model;
  const recent = storageGet("docqa-recent-questions", []);

  root.innerHTML = `
    <form class="card" id="ask-form">
      <label class="field-label" for="question">Your question</label>
      <textarea class="input" id="question" maxlength="${isDemo() ? state.status.demo.limits.max_question_chars : 1000}" rows="3" placeholder="e.g. How does RAG combine retrieval with generation?">${esc(a.question)}</textarea>
      <div class="ask-row">
        <label class="visually-hidden" for="paper-scope">Papers to search</label>
        <select class="input" id="paper-scope">
          <option value="">Search all indexed papers (${papers.length})</option>
          ${papers.map((p) => `<option value="${esc(p.arxiv_id)}"${p.arxiv_id === a.paperId ? " selected" : ""}>
            Only: ${esc(p.title.length > 70 ? p.title.slice(0, 68) + "…" : p.title)}${p.status === "abstract_only" ? " (abstract only)" : ""}</option>`).join("")}
        </select>
        <div class="hint">
          ${isDemo()
            ? `Public demo: you’ll see passages quoted from the papers; no answer model runs here.`
            : model && model.available
              ? `<label class="toggle"><input type="checkbox" id="generate-toggle" ${a.generate ? "checked" : ""}>
                   Write an answer with ${esc(model.name)}</label>`
              : `No answer model installed: you’ll see retrieved passages.`}
          <div>Press Ctrl + Enter to ask.</div>
        </div>
        <button class="btn btn-primary" type="submit" id="ask-button">Ask</button>
      </div>
    </form>
    ${recent.length ? `<div class="try-row"><span class="label">Recent</span>
      ${recent.map((q) => `<button type="button" class="chip-button" data-question="${esc(q)}">${esc(q)}</button>`).join("")}</div>`
      : `<div style="height:22px"></div>`}
    <div id="answer-area" aria-live="polite"></div>`;

  const question = $("#question", root);
  question.addEventListener("input", () => { a.question = question.value; });
  question.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); submitQuestion(); }
  });
  $("#paper-scope", root).addEventListener("change", (event) => { a.paperId = event.target.value; });
  $("#generate-toggle", root)?.addEventListener("change", (event) => {
    a.generate = event.target.checked;
    storageSet("docqa-generate", a.generate);
  });
  $("#ask-form", root).addEventListener("submit", (event) => { event.preventDefault(); submitQuestion(); });
  $$("[data-question]", root).forEach((b) => b.addEventListener("click", () => {
    a.question = b.dataset.question;
    question.value = a.question;
    submitQuestion();
  }));
  renderAnswer();
  if (!a.result) question.focus();
}

async function submitQuestion() {
  const a = state.ask;
  a.question = ($("#question")?.value || a.question).trim();
  if (!a.question) {
    toast("Type a question first.", "info");
    return;
  }
  const model = state.status?.model;
  a.loading = true;
  a.error = null;
  a.showNearest = false;
  a.highlighted = null;
  renderAnswer();
  const button = $("#ask-button");
  if (button) button.disabled = true;
  try {
    a.result = await api("/api/ask", {
      method: "POST",
      // Without a model we still ask for generation, so the server explains why no answer was written.
      // The public demo accepts no "generate" field at all (it never runs a model).
      body: isDemo()
        ? { question: a.question, paper_id: a.paperId || null }
        : { question: a.question, paper_id: a.paperId || null, generate: model?.available ? a.generate : true },
    });
    a.tab = a.result.mode === "generated" ? "cited" : "all";
    const recent = storageGet("docqa-recent-questions", []).filter((q) => q !== a.question);
    storageSet("docqa-recent-questions", [a.question, ...recent].slice(0, 5));
  } catch (error) {
    a.result = null;
    a.error = error.message;
  } finally {
    a.loading = false;
    if (state.view === "ask") renderAskBody();
  }
}

function renderAnswer() {
  const root = $("#answer-area");
  if (!root) return;
  const a = state.ask;
  if (a.loading) {
    const generating = state.status?.model?.available && a.generate;
    root.innerHTML = `
      <div class="results-head" style="margin-top:0"><span class="spinner" style="color:var(--link)"></span>
        <h2>${generating ? "Retrieving passages and writing an answer with the local model… (can take a minute)" : "Searching your papers…"}</h2></div>
      <div class="skeleton"><span style="width:30%"></span><span></span><span style="width:92%"></span><span style="width:70%"></span></div>`;
    return;
  }
  if (a.error) {
    root.innerHTML = `<div class="card state-card error" role="alert"><h2>${icon("alert")}The question could not be answered</h2>
      <p style="color:var(--text)">${esc(a.error)}</p></div>`;
    return;
  }
  const r = a.result;
  if (!r) { root.innerHTML = ""; return; }

  root.innerHTML = `<div class="answer-grid"><section class="card answer-card">${answerPanel(r)}</section><section>${sourcesPanel(r)}</section></div>`;

  $$(".cite", root).forEach((b) => b.addEventListener("click", () => focusPassage(Number(b.dataset.rank))));
  $$("[data-tab]", root).forEach((b) => b.addEventListener("click", () => { a.tab = b.dataset.tab; renderAnswer(); }));
  $('[data-action="show-nearest"]', root)?.addEventListener("click", () => { a.showNearest = true; renderAnswer(); });
  $('[data-action="search-arxiv"]', root)?.addEventListener("click", () => {
    state.search.query = r.keywords.join(" ");
    go("discover", { q: state.search.query, run: "1" });
  });
  if (a.highlighted) $(`#passage-${a.highlighted}`)?.classList.add("highlight");
}

function keywordBlock(r) {
  const missing = new Set(r.missing_keywords);
  return `
    <div class="keyword-block">
      <span class="section-label">Keywords searched</span>
      <span class="chips">${r.keywords.map((k) => `<span class="chip mono${missing.has(k) ? " dashed" : ""}">${esc(k)}</span>`).join("")}</span>
      <p>${esc(r.reason)}${missing.size ? " Dashed keywords were not found in any retrieved passage." : ""}</p>
    </div>`;
}

function answerPanel(r) {
  const notes = r.notes.map((n) => `<p class="note">${icon("info")}<span>${esc(n)}</span></p>`).join("");
  if (r.mode === "generated") {
    // Escape the model's text first, then turn "[n]" markers into buttons linked to passage n.
    const text = esc(r.answer).replace(/\[(\d+)\]/g, (_, n) =>
      `<button type="button" class="cite" data-rank="${n}" aria-label="Show passage ${n}">${n}</button>`);
    return `
      <span class="pill blue">${icon("check")}Grounded answer</span>
      <div class="sub">Written by ${esc(r.generator)} from ${plural(r.cited.length, "cited passage")}.
        Citations were checked to exist; read the passages to confirm each claim.</div>
      <h2>${esc(r.question)}</h2>
      <p class="answer-text">${text}</p>
      ${notes}
      ${keywordBlock(r)}`;
  }
  if (r.mode === "retrieval_only") {
    return `
      <span class="pill yellow">${icon("file")}Retrieved passages only</span>
      <div class="sub">Quoted from your papers · nothing generated</div>
      <h2>${esc(r.question)}</h2>
      <p style="margin:0">No answer was written for this question. The ${plural(r.passages.length, "passage")} beside this
        ${r.passages.length === 1 ? "is" : "are"} quoted directly from your papers and ranked by how well they match your
        question’s keywords (BM25). Read them to find the answer.</p>
      ${notes}
      ${keywordBlock(r)}`;
  }
  return `
    <span class="pill">${icon("minus")}Not enough information</span> <span class="muted" style="font-size:13.5px">No answer or citations shown</span>
    <h2>${esc(r.question)}</h2>
    <div class="inset">
      <h3>Not enough information in your collection</h3>
      ${r.notes.map((n) => `<p>${esc(n)}</p>`).join("")}
      <p class="muted" style="margin:0">Rather than guess, the app shows no answer and no citations.</p>
    </div>
    <h3 style="font:600 15px/1.3 var(--sans);margin:18px 0 4px">What you can do</h3>
    <ul class="todo-list">
      <li>Add papers that cover this topic.
        ${isDemo()
          ? `This demo has a fixed set of papers; <a href="${esc(state.status.demo.repo_url)}" target="_blank" rel="noopener">run the full app</a> to add your own.`
          : r.keywords.length ? `<button type="button" class="link-button" data-action="search-arxiv">Search arXiv for “${esc(r.keywords.join(" "))}”</button>` : ""}</li>
      <li>Rephrase using the terms your papers use. Search matches keywords, not synonyms.</li>
    </ul>
    ${r.keywords.length ? keywordBlock(r) : ""}`;
}

function sourcesPanel(r) {
  const a = state.ask;
  if (r.mode === "insufficient_evidence") {
    if (!r.passages.length) {
      return `<div class="sources-head"><h2>Nearest passages</h2></div>
        <div class="card state-card dashed"><p style="margin:0">No passage in your collection contains any of your question’s key words.</p></div>`;
    }
    if (!a.showNearest) {
      return `<div class="sources-head"><h2>Nearest passages</h2><span class="muted">Not enough to answer</span></div>
        <div class="card state-card dashed">
          <p>The closest passages matched too few of your question’s keywords to count as evidence, so they’re hidden.</p>
          <button type="button" class="btn" data-action="show-nearest">Show nearest passages anyway</button>
        </div>`;
    }
    return `<div class="sources-head"><h2>Nearest passages</h2><span class="muted">Weak matches · not evidence</span></div>
      ${r.passages.map((p) => passageCard(p, false)).join("")}`;
  }
  if (r.mode === "generated") {
    const cited = r.passages.filter((p) => p.cited);
    const shown = a.tab === "cited" ? cited : r.passages;
    return `
      <div class="sources-head"><h2>Sources</h2><span class="muted">${cited.length} cited · ${r.passages.length} retrieved</span></div>
      <div class="tabs" role="group" aria-label="Which sources to show">
        <button type="button" class="chip-button" data-tab="cited" aria-pressed="${a.tab === "cited"}">Cited (${cited.length})</button>
        <button type="button" class="chip-button" data-tab="all" aria-pressed="${a.tab === "all"}">All retrieved (${r.passages.length})</button>
      </div>
      ${shown.map((p) => passageCard(p, true)).join("")}`;
  }
  return `
    <div class="sources-head"><h2>Retrieved passages</h2><span class="muted">${plural(r.passages.length, "passage")}, best match first</span></div>
    ${r.passages.map((p) => passageCard(p, false)).join("")}`;
}

function passageCard(p, showCited) {
  const where = p.page != null ? `page ${p.page}` : "abstract only";
  return `
    <article class="card passage" id="passage-${p.rank}">
      <div class="passage-head">
        <span class="rank">${p.rank}</span>
        <div><h3>${esc(p.title)}</h3><div class="where">arXiv:${esc(p.arxiv_id)}${esc(p.version)} · ${esc(where)}</div></div>
        ${showCited && p.cited ? `<span class="chip">Cited</span>` : ""}
      </div>
      <p class="excerpt">${esc(p.text)}</p>
      <div class="matched"><span class="section-label">Matched</span>${p.matched_keywords.map((k) => `<span class="chip mono">${esc(k)}</span>`).join("")}</div>
      <a class="open" href="${esc(p.link)}" target="_blank" rel="noopener">${p.page != null ? `Open PDF at page ${p.page}` : "Open abstract page"}${icon("external")}
        <span class="visually-hidden">(opens in a new tab)</span></a>
      ${licenseLine(p.license)}
    </article>`;
}

function focusPassage(rank) {
  const a = state.ask;
  const passage = a.result?.passages.find((p) => p.rank === rank);
  if (!passage) return;
  a.highlighted = rank;
  if (a.tab === "cited" && !passage.cited) a.tab = "all";
  renderAnswer();
  $$(".cite").forEach((b) => b.classList.toggle("active", Number(b.dataset.rank) === rank));
  $(`#passage-${rank}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
}

/* ---------------------------------------------------------------------------------------
 * Theme
 * ------------------------------------------------------------------------------------- */

function currentTheme() {
  const explicit = document.documentElement.dataset.theme;
  if (explicit) return explicit;
  return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem("docqa-theme", theme); } catch (e) { /* not essential */ }
  renderThemeControls();
}

function renderThemeControls() {
  const theme = currentTheme();
  $$("[data-theme-choice]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.themeChoice === theme)));
  const mobile = $("#mobile-theme");
  mobile.innerHTML = icon(theme === "dark" ? "sun" : "moon");
  mobile.setAttribute("aria-label", theme === "dark" ? "Switch to light theme" : "Switch to dark theme");
}

/* ---------------------------------------------------------------------------------------
 * Start
 * ------------------------------------------------------------------------------------- */

function init() {
  fillIcons();
  $$("[data-theme-choice]").forEach((b) => b.addEventListener("click", () => setTheme(b.dataset.themeChoice)));
  $("#mobile-theme").addEventListener("click", () => setTheme(currentTheme() === "dark" ? "light" : "dark"));
  renderThemeControls();
  // Load the status first: it tells the page whether it is the full app or the read-only public demo.
  refreshStatus().then(() => {
    window.addEventListener("hashchange", route);
    route();
  });
  setInterval(refreshStatus, 15000);
}

init();
