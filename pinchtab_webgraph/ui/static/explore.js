// pinchtab-webgraph web UI — the Explore view controller (Phase 5).
//
// Loaded EAGERLY (right after app.js, which it depends on) because it has NO vendor
// deps — unlike graph.js. It drives three sub-tabs over the read-only crawled cache:
//   * Search  — full-text search of captured page data (GET /content/search).
//   * Forms   — a path-finder box (GET /howto?goal=…) + the create-form inventory.
//   * Content — the per-view inventory of captured data collections (GET /content).
//
// SAFETY DISCIPLINE (kept from graph.js / app.js):
//   * textContent ONLY for every crawled string — createElement, never innerHTML.
//   * crawled URLs (state_url / view_url) are shown as PLAIN TEXT, never a live <a href>
//     (clicking one would navigate the browser away from the SPA).
//   * URL-encode every query param; regex-escape a label before it becomes a `match=`.
//   * setView / startTour / newChat live in app.js — explore.js CALLS them, never
//     duplicates the chat/live/tour socket ownership.
"use strict";

// module state — the host the panels were built for, plus per-host caches so switching
// sub-tabs never refetches Forms/Content (Search stays empty until the user submits).
let exploreHost = null;
let exploreSubtab = "search";
let formsCache = null;
let contentCache = null;

// --- small DOM helpers (createElement + textContent only) --------------------
function exEl(id) { return document.getElementById(id); }

function exBadge(text, cls) {
  const b = document.createElement("span");
  b.className = "badge" + (cls ? " " + cls : "");
  b.textContent = String(text);           // server value — textContent only
  return b;
}

function exEmpty(text) {
  const p = document.createElement("p");
  p.className = "explore-empty";
  p.textContent = text;                    // fixed UI copy
  return p;
}

// A crawled URL rendered as PLAIN TEXT (never a live href — see the file header).
function exUrlLine(url) {
  const span = document.createElement("span");
  span.className = "explore-card-url";
  span.textContent = url || "";            // crawled url — textContent only
  return span;
}

// --- lifecycle: open / destroy (app.js owns the host-switch calls) -----------
function openExploreView(host) {
  if (host && host === exploreHost) return;   // same host already rendered — no-op
  exploreHost = host || null;
  formsCache = null;
  contentCache = null;

  // Search stays empty until submitted; Forms + Content load eagerly so the sub-tabs
  // are instant. Clear the inputs and reset to the Search sub-tab.
  const searchInput = exEl("explore-search-input");
  const goalInput = exEl("explore-goal-input");
  if (searchInput) searchInput.value = "";
  if (goalInput) goalInput.value = "";
  const results = exEl("explore-search-results");
  const goalResult = exEl("explore-goal-result");
  if (results) results.textContent = "";
  if (goalResult) goalResult.textContent = "";
  setExploreSubtab("search");

  const status = exEl("explore-status");
  if (!host) {
    if (status) status.textContent = "Pick a host first.";
    return;
  }
  if (status) status.textContent = "";
  loadForms(host);
  loadContent(host);
}

// app.js calls this on EVERY host switch (before opening the new one) so a stale host's
// data never bleeds into the next — mirrors destroyGraphView().
function destroyExploreView() {
  exploreHost = null;
  formsCache = null;
  contentCache = null;
  for (const id of ["explore-search-results", "explore-goal-result",
                    "explore-forms-list", "explore-content-list"]) {
    const node = exEl(id);
    if (node) node.textContent = "";
  }
  for (const id of ["explore-search-input", "explore-goal-input"]) {
    const input = exEl(id);
    if (input) input.value = "";
  }
  const status = exEl("explore-status");
  if (status) status.textContent = "";
  setExploreSubtab("search");
}

// Flip the 3 panels' `hidden` + the 3 sub-tab buttons' `.on`.
function setExploreSubtab(name) {
  exploreSubtab = name;
  const map = {
    search: ["explore-tab-search", "explore-panel-search"],
    forms: ["explore-tab-forms", "explore-panel-forms"],
    content: ["explore-tab-content", "explore-panel-content"],
  };
  for (const key of Object.keys(map)) {
    const [tabId, panelId] = map[key];
    const tab = exEl(tabId);
    const panel = exEl(panelId);
    if (tab) tab.classList.toggle("on", key === name);
    if (panel) panel.hidden = key !== name;
  }
}

// --- Forms sub-tab: the create-form inventory --------------------------------
async function loadForms(host) {
  const list = exEl("explore-forms-list");
  if (!list) return;
  list.textContent = "";
  let data = formsCache;
  if (!data) {
    try {
      const res = await fetch("/api/hosts/" + encodeURIComponent(host) + "/forms");
      data = await res.json();
    } catch (err) {
      if (exploreHost === host) list.appendChild(exEmpty("Could not load forms."));
      return;
    }
    if (exploreHost !== host) return;        // a newer host switch took over
    formsCache = data;
  }
  const forms = (data && Array.isArray(data.forms)) ? data.forms : [];
  if (forms.length === 0) {
    list.appendChild(exEmpty("No create-forms were discovered for this host."));
    return;
  }
  for (const f of forms) list.appendChild(buildFormRow(host, f));
}

function buildFormRow(host, f) {
  const li = document.createElement("li");
  li.className = "explore-card";

  const head = document.createElement("div");
  head.className = "explore-card-head";
  const title = document.createElement("span");
  title.className = "explore-card-title";
  title.textContent = f.label || "(form)";          // crawled label — textContent only
  head.appendChild(title);
  if (typeof f.clicks === "number") head.appendChild(exBadge(f.clicks + " clicks"));
  if (typeof f.field_count === "number") head.appendChild(exBadge(f.field_count + " fields"));

  const how = document.createElement("button");
  how.type = "button";
  how.className = "explore-how-btn";
  how.textContent = "Show me how";                  // fixed label, not crawled data
  const note = document.createElement("span");
  note.className = "explore-how-note";
  how.addEventListener("click", () => runHowtoForLabel(host, f.label || "", note));
  head.appendChild(how);
  li.appendChild(head);

  li.appendChild(exUrlLine(f.state_url));           // plain text, never a live href
  li.appendChild(note);
  return li;
}

// --- Content sub-tab: per-view collection inventory --------------------------
async function loadContent(host) {
  const list = exEl("explore-content-list");
  if (!list) return;
  list.textContent = "";
  let data = contentCache;
  if (!data) {
    try {
      const res = await fetch("/api/hosts/" + encodeURIComponent(host) + "/content");
      data = await res.json();
    } catch (err) {
      if (exploreHost === host) list.appendChild(exEmpty("Could not load content."));
      return;
    }
    if (exploreHost !== host) return;
    contentCache = data;
  }
  const views = (data && Array.isArray(data.views)) ? data.views : [];
  if (views.length === 0 || (data && data.status === "empty")) {
    list.appendChild(exEmpty("This host captured no data collections."));
    return;
  }
  for (const v of views) list.appendChild(buildContentRow(v));
}

function buildContentRow(v) {
  const li = document.createElement("li");
  li.className = "explore-card";

  const head = document.createElement("div");
  head.className = "explore-card-head";
  const title = document.createElement("span");
  title.className = "explore-card-title";
  title.textContent = v.view_label || "(view)";     // crawled label — textContent only
  head.appendChild(title);
  li.appendChild(head);

  li.appendChild(exUrlLine(v.view_url));            // plain text, never a live href

  const cols = document.createElement("ul");
  cols.className = "explore-items";
  for (const c of (v.collections || [])) {
    const row = document.createElement("li");
    const kind = document.createElement("span");
    kind.className = "explore-item-kind";
    kind.textContent = (c.kind || "?") + " ×" + (typeof c.count === "number" ? c.count : "?");
    row.appendChild(kind);
    const sample = document.createElement("span");
    sample.textContent = c.sample || "";           // crawled sample — textContent only
    row.appendChild(sample);
    cols.appendChild(row);
  }
  li.appendChild(cols);
  return li;
}

// --- Forms sub-tab: "Show me how" -> drive the live-pane tour ----------------
// Regex-escape the crawled label so it becomes a LITERAL `match=` (a label with regex
// metacharacters like "(" would otherwise be an invalid/over-broad pattern).
function exRegexEscape(s) {
  return String(s).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

async function runHowtoForLabel(host, label, noteEl) {
  if (noteEl) noteEl.textContent = "finding a path…";
  let data;
  try {
    const q = "/api/hosts/" + encodeURIComponent(host) + "/howto?match=" +
      encodeURIComponent(exRegexEscape(label));
    const res = await fetch(q);
    data = await res.json();
  } catch (err) {
    if (noteEl) noteEl.textContent = "could not load the path.";
    return;
  }
  if (data && data.status === "ok" && Array.isArray(data.results) && data.results.length) {
    const r = data.results[0];
    if (noteEl) noteEl.textContent = "";
    // The tour overlay lives in the Workspace (live) pane — switch there, then drive it.
    if (typeof setView === "function") setView("workspace");
    if (typeof startTour === "function") {
      startTour({ trigger_label: r.trigger_label, steps: r.tour });
    }
    return;
  }
  if (noteEl) noteEl.textContent = "No reachable path found for this form.";
}

// --- Forms sub-tab: the path-finder box (goal -> howto) ----------------------
async function runGoalSearch(host, goal) {
  const out = exEl("explore-goal-result");
  if (!out) return;
  out.textContent = "";
  if (!goal) return;                               // guard: an empty goal is a no-op
  const loading = exEmpty("searching…");
  out.appendChild(loading);
  let data;
  try {
    const res = await fetch("/api/hosts/" + encodeURIComponent(host) +
      "/howto?goal=" + encodeURIComponent(goal));
    data = await res.json();
  } catch (err) {
    out.textContent = "";
    out.appendChild(exEmpty("Path search failed."));
    return;
  }
  if (exploreHost !== host) return;
  out.textContent = "";

  if (data && data.status === "ok" && Array.isArray(data.results) && data.results.length) {
    const ul = document.createElement("ul");
    ul.className = "explore-results";
    for (const r of data.results) ul.appendChild(buildHowtoRow(host, r));
    out.appendChild(ul);
    return;
  }

  // no_match / unreachable / invalid_args — surface any candidates + the muted
  // low_confidence list (free data the server already computed; show it dimmed).
  out.appendChild(exEmpty("No confident path found."));
  const candidates = (data && Array.isArray(data.candidates)) ? data.candidates : [];
  if (candidates.length) {
    const note = document.createElement("p");
    note.className = "explore-low-note";
    note.textContent = "Candidates:";
    out.appendChild(note);
    const cu = document.createElement("ul");
    cu.className = "explore-items";
    for (const c of candidates) {
      const li = document.createElement("li");
      li.textContent = c;                          // crawled label — textContent only
      cu.appendChild(li);
    }
    out.appendChild(cu);
  }
  const low = (data && Array.isArray(data.low_confidence)) ? data.low_confidence : [];
  if (low.length) {
    const note = document.createElement("p");
    note.className = "explore-low-note";
    note.textContent = "Low-confidence matches:";
    out.appendChild(note);
    const lu = document.createElement("ul");
    lu.className = "explore-results";
    for (const r of low) {
      const row = buildHowtoRow(host, r);
      row.classList.add("muted-card");
      lu.appendChild(row);
    }
    out.appendChild(lu);
  }
}

function buildHowtoRow(host, r) {
  const li = document.createElement("li");
  li.className = "explore-card";

  const head = document.createElement("div");
  head.className = "explore-card-head";
  const title = document.createElement("span");
  title.className = "explore-card-title";
  title.textContent = r.trigger_label || "(form)";   // crawled label — textContent only
  head.appendChild(title);
  if (typeof r.clicks === "number") head.appendChild(exBadge(r.clicks + " clicks"));
  if (r.confidence) head.appendChild(exBadge(r.confidence,
    r.confidence === "high" ? "ok" : "warn"));

  const how = document.createElement("button");
  how.type = "button";
  how.className = "explore-how-btn";
  how.textContent = "Show me how";
  const note = document.createElement("span");
  note.className = "explore-how-note";
  how.addEventListener("click", () => runHowtoForLabel(host, r.trigger_label || "", note));
  head.appendChild(how);
  li.appendChild(head);

  li.appendChild(exUrlLine(r.state_url));            // plain text, never a live href

  if (Array.isArray(r.steps) && r.steps.length) {
    const steps = document.createElement("ul");
    steps.className = "explore-steps";
    for (const s of r.steps) {
      const sl = document.createElement("li");
      sl.textContent = s;                            // crawled step text — textContent only
      steps.appendChild(sl);
    }
    li.appendChild(steps);
  }
  li.appendChild(note);
  return li;
}

// --- Search sub-tab: full-text search of captured data -----------------------
async function runSearch(host, query) {
  const out = exEl("explore-search-results");
  const status = exEl("explore-status");
  if (!out) return;
  out.textContent = "";
  const q = (query || "").trim();
  if (!q) {                                          // guard: empty text -> 422, never send
    if (status) status.textContent = "Type something to search.";
    return;
  }
  if (status) status.textContent = "searching…";
  let data;
  try {
    const res = await fetch("/api/hosts/" + encodeURIComponent(host) +
      "/content/search?text=" + encodeURIComponent(q) + "&limit=40");
    data = await res.json();
  } catch (err) {
    if (status) status.textContent = "";
    out.appendChild(exEmpty("Search failed."));
    return;
  }
  if (exploreHost !== host) return;

  if (!data || data.status === "no_match" || !Array.isArray(data.views) ||
      data.views.length === 0) {
    if (status) status.textContent = "No matches.";
    out.appendChild(exEmpty("No captured data matched “" + q + "”."));
    return;
  }
  if (status) {
    status.textContent = (data.total_matches || 0) + " matches · " +
      (data.views_matched || 0) + " views · showing " + (data.shown || 0);
  }
  for (const v of data.views) out.appendChild(buildSearchRow(v));
}

function buildSearchRow(v) {
  const li = document.createElement("li");
  li.className = "explore-card";

  const head = document.createElement("div");
  head.className = "explore-card-head";
  const title = document.createElement("span");
  title.className = "explore-card-title";
  title.textContent = v.view_label || "(view)";      // crawled label — textContent only
  head.appendChild(title);
  head.appendChild(exBadge(v.reachable ? "reachable" : "unreachable",
    v.reachable ? "ok" : "warn"));
  if (typeof v.distance_clicks === "number") {
    head.appendChild(exBadge(v.distance_clicks + " clicks"));
  }
  li.appendChild(head);

  li.appendChild(exUrlLine(v.view_url));             // plain text, never a live href

  // the click-path to reach this view (one textContent line per step).
  if (Array.isArray(v.steps) && v.steps.length) {
    const steps = document.createElement("ul");
    steps.className = "explore-steps";
    for (const s of v.steps) {
      const sl = document.createElement("li");
      sl.textContent = s;                            // crawled step text — textContent only
      steps.appendChild(sl);
    }
    li.appendChild(steps);
  }

  // the matched items (kind + text).
  const items = document.createElement("ul");
  items.className = "explore-items";
  for (const it of (v.items || [])) {
    const row = document.createElement("li");
    const kind = document.createElement("span");
    kind.className = "explore-item-kind";
    kind.textContent = it.kind || "?";
    row.appendChild(kind);
    const txt = document.createElement("span");
    txt.textContent = it.text || "";                 // crawled text — textContent only
    row.appendChild(txt);
    items.appendChild(row);
  }
  li.appendChild(items);

  if (v.truncated) {
    const more = document.createElement("div");
    more.className = "explore-more";
    more.textContent = "+ more (server-truncated)";
    li.appendChild(more);
  }
  return li;
}

// --- wiring (explore.js loads exactly once, so top-level listeners are safe) --
(function wireExplore() {
  const st = exEl("explore-tab-search");
  const ft = exEl("explore-tab-forms");
  const ct = exEl("explore-tab-content");
  if (st) st.addEventListener("click", () => setExploreSubtab("search"));
  if (ft) ft.addEventListener("click", () => setExploreSubtab("forms"));
  if (ct) ct.addEventListener("click", () => setExploreSubtab("content"));

  const searchForm = exEl("explore-search-form");
  if (searchForm) {
    searchForm.addEventListener("submit", (e) => {
      e.preventDefault();
      const input = exEl("explore-search-input");
      if (exploreHost) runSearch(exploreHost, input ? input.value : "");
    });
  }
  const goalForm = exEl("explore-goal-form");
  if (goalForm) {
    goalForm.addEventListener("submit", (e) => {
      e.preventDefault();
      const input = exEl("explore-goal-input");
      if (exploreHost) runGoalSearch(exploreHost, input ? input.value.trim() : "");
    });
  }
})();
