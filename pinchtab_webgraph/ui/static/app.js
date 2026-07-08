// pinchtab-webgraph web UI — a single-page controller.
//
// The app is a two-pane workspace: pick a crawled graph (host) in the sidebar and
// it (1) shows a header with the graph's kind + element counts, (2) opens a live
// CHAT socket (left pane), and (3) opens a live BROWSER screencast socket (right
// pane). Switching hosts closes the prior sockets and opens fresh ones. A modal
// credentials vault stores per-host logins (password write-only).
//
// SAFETY DISCIPLINE (kept from the earlier phases):
//   * textContent ONLY for every server-sourced string — never innerHTML with data.
//   * encodeURIComponent for host/goal in every URL.
//   * close prior sockets on host switch; the password input is cleared after submit
//     and its value is never read back from the server.
"use strict";

// --- element handles ---------------------------------------------------------
const el = (id) => document.getElementById(id);

const hostsEl = el("hosts");
const cachesDirEl = el("caches-dir");

const hostHeaderEl = el("host-header");
const hostNameEl = el("host-name");
const hostKindEl = el("host-kind");
const hostCountsEl = el("host-counts");
const placeholderEl = el("placeholder");
const panesEl = el("panes");

// view switcher (Workspace | Graph | Explore) + the lazily-populated graph container
const viewTabsEl = el("view-tabs");
const tabWorkspaceEl = el("tab-workspace");
const tabGraphEl = el("tab-graph");
const tabExploreEl = el("tab-explore");
const graphViewEl = el("graph-view");
const exploreViewEl = el("explore-view");

// command palette (Ctrl/Cmd+K)
const cmdkModalEl = el("cmdk-modal");
const cmdkOpenEl = el("cmdk-open");
const cmdkBackdropEl = el("cmdk-backdrop");
const cmdkInputEl = el("cmdk-input");
const cmdkResultsEl = el("cmdk-results");

const chatLogEl = el("chat-log");
const chatStatusEl = el("chat-status");
const chatFormEl = el("chat-form");
const chatInputEl = el("chat-input");
const chatSendEl = chatFormEl ? chatFormEl.querySelector("button") : null;

// per-host chat sessions (chips + "+ New")
const chatSessionsEl = el("chat-sessions");
const chatSessionNewEl = el("chat-session-new");
const chatSessionListEl = el("chat-session-list");

const liveViewEl = el("live-view");
const liveStatusEl = el("live-status");

// guided-tour overlay
const tourOverlayEl = el("tour-overlay");
const tourBoxEl = el("tour-box");
const tourTipEl = el("tour-tip");
const tourTipTextEl = el("tour-tip-text");
const tourPrevEl = el("tour-prev");
const tourNextEl = el("tour-next");
const tourDoneEl = el("tour-done");

// vault modal
const vaultModalEl = el("vault-modal");
const vaultOpenEl = el("vault-open");
const vaultCloseEl = el("vault-close");
const vaultBackdropEl = el("vault-backdrop");
const vaultStatusEl = el("vault-status");
const credsEl = el("creds");
const credFormEl = el("cred-form");
const credHostEl = el("cred-host");
const credPasswordEl = el("cred-password");
const credMsgEl = el("cred-msg");

const CRED_FIELDS = ["url", "username", "userField", "passField", "submit",
  "successUrl", "keyringService"];

// new-crawl form
const crawlFormEl = el("crawl-form");
const crawlUrlEl = el("crawl-url");
const crawlSubmitEl = el("crawl-submit");
const crawlCancelEl = el("crawl-cancel");
const crawlMaxStatesEl = el("crawl-max-states");
const crawlMaxDepthEl = el("crawl-max-depth");
const crawlProgressEl = el("crawl-progress");

// --- shared socket / selection state -----------------------------------------
let chatWs = null;
let liveWs = null;
let crawlWs = null;              // the live-crawl progress socket (one at a time)
let selectedHost = null;
let chatSessions = [];           // the selected host's session summaries (updated_at desc)
let activeSessionId = null;      // the session id the chat socket is currently bound to
let currentView = "workspace";   // "workspace" | "graph" — flips ONLY the `hidden` panes
let vendorPromise = null;        // memoized sequential load of the Cytoscape libs + graph.js
let selectedGraphKind = null;    // the selected host's graph_kind, for the Graph view
let currentLiveUrl = null; // the live pane's current page, tracked from `location` frames
let currentBubble = null; // the assistant bubble currently streaming

// --- guided-tour ("Show me How") state ---------------------------------------
let lastTour = null;            // most recent tour payload from the chat socket
let tourSteps = null;           // steps of the tour currently being driven (or null)
let tourIndex = 0;              // index of the step currently shown
let pendingLocateStepId = null; // step id we are awaiting a `located` response for
let currentRect = null;         // last resolved viewport-CSS-px rect for the step

function wsUrl(pathAndQuery) {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return proto + "//" + location.host + pathAndQuery;
}

// --- view switcher: Workspace <-> Graph --------------------------------------
// The Graph view is heavy (785KB of Cytoscape) and only needed on demand, so its
// libs + controller (graph.js) are injected lazily on the first switch, memoized in
// vendorPromise. Toggling views ONLY flips the `hidden` panes + the active tab — it
// NEVER opens/closes chatWs/liveWs (selectHost owns the socket lifecycle).

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = src;
    s.onload = () => resolve(src);
    s.onerror = () => reject(new Error("failed to load " + src));
    document.head.appendChild(s);
  });
}

// Loaded SEQUENTIALLY: cytoscape core first, then the layout-base/cose-base deps, then
// the fcose extension that registers itself against them, then our controller. A wrong
// ORDER silently no-ops the fcose registration (the layout would fall back / throw), so
// this list order is significant — do not parallelize it.
const VENDOR_SCRIPTS = [
  "/vendor/cytoscape.min.js",
  "/vendor/dagre.min.js",
  "/vendor/cytoscape-dagre.min.js",
  "/vendor/layout-base.min.js",
  "/vendor/cose-base.min.js",
  "/vendor/cytoscape-fcose.min.js",
  "/graph.js",
];

function loadVendorSequential() {
  let chain = Promise.resolve();
  for (const src of VENDOR_SCRIPTS) chain = chain.then(() => loadScript(src));
  return chain;
}

// The Graph view's DOM lives in index.html and is DRIVEN by the lazily-loaded graph.js
// (which resolves these by id). app.js only asserts the markup is present before the
// hand-off; listing the ids here also keeps the app.js<->index.html id cross-check honest.
const GRAPH_VIEW_IDS = ["graph-canvas", "graph-detail", "graph-search", "graph-status"];

// The Explore view's DOM lives in index.html and is DRIVEN by explore.js (loaded eagerly
// after this file). app.js only lists the ids here to keep the app.js<->index.html id
// cross-check honest (the same discipline as GRAPH_VIEW_IDS).
const EXPLORE_VIEW_IDS = ["explore-tab-search", "explore-search-input",
  "explore-search-results", "explore-tab-forms", "explore-goal-input",
  "explore-goal-result", "explore-forms-list", "explore-tab-content",
  "explore-content-list"];

// Three-way view switcher: Workspace (chat + live) | Graph (Cytoscape) | Explore (a
// read-only browser over the cache). Toggling ONLY flips the `hidden` panes + the active
// tab — it NEVER opens/closes chatWs/liveWs (selectHost owns the socket lifecycle).
function setView(view) {
  currentView = view;
  const graph = view === "graph";
  const explore = view === "explore";
  const workspace = !graph && !explore;
  if (panesEl) panesEl.hidden = !workspace;
  if (graphViewEl) graphViewEl.hidden = !graph;
  if (exploreViewEl) exploreViewEl.hidden = !explore;
  if (tabWorkspaceEl) tabWorkspaceEl.classList.toggle("on", workspace);
  if (tabGraphEl) tabGraphEl.classList.toggle("on", graph);
  if (tabExploreEl) tabExploreEl.classList.toggle("on", explore);
  if (graph) ensureGraphView();
  if (explore) ensureExploreView();
}

// explore.js is eager (no vendor deps), so the controller is already present — just hand
// it the selected host. Guarded in case the markup or the script is missing.
function ensureExploreView() {
  const status = el("explore-status");
  const missing = EXPLORE_VIEW_IDS.filter((id) => !el(id));
  if (missing.length) {
    if (status) status.textContent = "explore markup missing: " + missing.join(", ");
    return;
  }
  if (typeof openExploreView === "function") openExploreView(selectedHost);
}

async function ensureGraphView() {
  const statusEl = el("graph-status");
  const missing = GRAPH_VIEW_IDS.filter((id) => !el(id));
  if (missing.length) {
    if (statusEl) statusEl.textContent = "graph markup missing: " + missing.join(", ");
    return;
  }
  if (statusEl && !statusEl.textContent) statusEl.textContent = "loading graph…";
  if (!vendorPromise) vendorPromise = loadVendorSequential();
  try {
    await vendorPromise;
    if (typeof openGraphView === "function") openGraphView(selectedHost);
  } catch (err) {
    // memoized failure would poison every retry — clear it so a later switch retries.
    vendorPromise = null;
    if (statusEl) {
      statusEl.textContent = "Graph libraries failed to load — " +
        (err && err.message ? err.message : "unknown error");
    }
  }
}

// graph.js keeps chat-input ownership here (app.js): its "Ask in chat" button calls
// this to prefill the chat box without reaching into the chat socket itself.
function prefillChat(text) {
  if (!chatInputEl) return;
  chatInputEl.value = text;
  chatInputEl.focus();
}

// --- sidebar: list of crawled graphs -----------------------------------------
async function loadHosts() {
  try {
    const res = await fetch("/api/hosts");
    const data = await res.json();
    hostsEl.textContent = "";
    const rows = data.hosts || [];
    if (rows.length === 0) {
      const li = document.createElement("li");
      li.className = "muted pad";
      li.textContent = "no crawled graphs yet";
      hostsEl.appendChild(li);
    } else {
      for (const h of rows) {
        hostsEl.appendChild(buildHostRow(h));
      }
    }
    if (data.caches_dir) cachesDirEl.textContent = data.caches_dir;
  } catch (err) {
    hostsEl.textContent = "";
    const li = document.createElement("li");
    li.className = "muted pad";
    li.textContent = "failed to load graphs";
    hostsEl.appendChild(li);
  }
}

function summaryCount(summary) {
  // interaction graphs report states/edges/triggers; link graphs report nodes/edges.
  if (!summary) return null;
  if (typeof summary.states === "number") return summary.states + " states";
  if (typeof summary.nodes === "number") return summary.nodes + " pages";
  return null;
}

function buildHostRow(h) {
  const li = document.createElement("li");
  li.className = "host-row";

  const label = document.createElement("span");
  label.className = "host-label";
  label.textContent = h.host;
  li.appendChild(label);

  const meta = document.createElement("span");
  meta.className = "host-meta";
  const kind = h.summary ? h.summary.graph_kind : (h.error ? "error" : "?");
  const badge = document.createElement("span");
  badge.className = "badge";
  badge.textContent = kind;
  meta.appendChild(badge);
  const count = summaryCount(h.summary);
  if (count) {
    const c = document.createElement("span");
    c.className = "count";
    c.textContent = count;
    meta.appendChild(c);
  }
  li.appendChild(meta);

  li.addEventListener("click", () => selectHost(h));
  return li;
}

// --- host selection: header + BOTH sockets -----------------------------------
async function selectHost(h) {
  const host = h.host;
  selectedHost = host;
  // a host switch drops the prior host's session set — loadChatSessions repopulates it.
  chatSessions = [];
  activeSessionId = null;

  // A host switch always returns to the Workspace view and tears down any prior
  // graph render (destroyGraphView is defined in the lazily-loaded graph.js, so it may
  // not exist yet). setView here only flips `hidden` — it does NOT touch the sockets,
  // which the openChat/openLiveView calls below own.
  setView("workspace");
  if (typeof destroyGraphView === "function") destroyGraphView();
  if (typeof destroyExploreView === "function") destroyExploreView();
  // Stash the (cheap, index) graph_kind now; the fresh-summary .then refreshes it.
  selectedGraphKind = h.summary ? h.summary.graph_kind : null;
  // A host whose cache failed to load can't render a graph or be explored — disable both.
  if (tabGraphEl) tabGraphEl.disabled = !!h.error;
  if (tabExploreEl) tabExploreEl.disabled = !!h.error;

  // reflect selection in the sidebar.
  for (const row of hostsEl.children) row.classList.remove("selected");
  // find the clicked row by matching label text (rows carry the host as first child).
  for (const row of hostsEl.children) {
    const label = row.querySelector(".host-label");
    if (label && label.textContent === host) row.classList.add("selected");
  }

  // header — filled from the (cheap) summary already in the index; refresh live.
  renderHostHeader(host, h.summary, h.error);
  if (placeholderEl) placeholderEl.hidden = true;
  if (panesEl) panesEl.hidden = false;
  if (hostHeaderEl) hostHeaderEl.hidden = false;

  // fetch a fresh summary so counts are authoritative even if the index was stale.
  try {
    const res = await fetch("/api/hosts/" + encodeURIComponent(host) + "/summary");
    const summary = await res.json();
    if (selectedHost === host && summary && !summary.status) {
      renderHostHeader(host, summary, null);
      selectedGraphKind = summary.graph_kind;
    }
  } catch (err) { /* keep the index summary */ }

  // loadChatSessions lists (or mints) this host's chats, renders the chips, and opens
  // the most-recent one on the chat socket (openChat owns the socket lifecycle).
  await loadChatSessions(host);
  openLiveView(host);
}

function renderHostHeader(host, summary, error) {
  hostNameEl.textContent = host;
  const kind = summary ? summary.graph_kind : (error ? "error" : "?");
  hostKindEl.textContent = kind;
  const parts = [];
  if (summary) {
    if (typeof summary.states === "number") parts.push(summary.states + " states");
    if (typeof summary.nodes === "number") parts.push(summary.nodes + " pages");
    if (typeof summary.triggers === "number") parts.push(summary.triggers + " triggers");
    if (typeof summary.edges === "number") parts.push(summary.edges + " edges");
  }
  hostCountsEl.textContent = parts.join("  ·  ");
}

// --- chat pane ---------------------------------------------------------------
function chatSetEnabled(on) {
  if (chatInputEl) chatInputEl.disabled = !on;
  if (chatSendEl) chatSendEl.disabled = !on;
}

function chatAddLine(cls, text) {
  const div = document.createElement("div");
  div.className = "msg " + cls;
  div.textContent = text;
  chatLogEl.appendChild(div);
  chatLogEl.scrollTop = chatLogEl.scrollHeight;
  return div;
}

function escapeHtml(s) {
  // Escape quotes too, not just <>&: the markdown renderer injects text into an
  // href="..." ATTRIBUTE, so an unescaped " / ' would break out of the attribute
  // (XSS). Chat content is LLM-echoed crawled-site text — treat it as untrusted.
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
          .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

// Minimal, SAFE markdown -> HTML. The raw text is HTML-ESCAPED FIRST, so the only
// markup in the output is the fixed tag set we inject below — no model/server text
// can inject HTML. Deliberately small: bold/italic/inline+block code, #-headings,
// ordered/unordered lists, http(s) links, paragraphs.
function renderMarkdown(raw) {
  const inline = (t) =>
    t.replace(/`([^`]+)`/g, "<code>$1</code>")
     .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
     .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
     .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)"'<>]+)\)/g,
              '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  const lines = escapeHtml(raw).replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let listType = null, inCode = false, code = [];
  const closeList = () => { if (listType) { out.push("</" + listType + ">"); listType = null; } };
  // GitHub-style tables: split a `| a | b |` row into trimmed cells; a separator row is
  // one whose cells are all `---` / `:--:` dashes. A table is a row immediately followed
  // by a separator (so a lone paragraph pipe is never mistaken for one).
  const cellsOf = (row) => row.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
  const isRow = (l) => l.includes("|") && l.trim() !== "";
  const isSep = (l) => isRow(l) && cellsOf(l).every((c) => /^:?-{1,}:?$/.test(c));
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line.trim().startsWith("```")) {
      if (inCode) { out.push("<pre><code>" + code.join("\n") + "</code></pre>"); code = []; inCode = false; }
      else { closeList(); inCode = true; }
      continue;
    }
    if (inCode) { code.push(line); continue; }
    if (isRow(line) && !isSep(line) && i + 1 < lines.length && isSep(lines[i + 1])) {
      closeList();
      out.push("<table><thead><tr>"
        + cellsOf(line).map((c) => "<th>" + inline(c) + "</th>").join("")
        + "</tr></thead><tbody>");
      i += 2;  // consume header + separator
      while (i < lines.length && isRow(lines[i]) && !isSep(lines[i])) {
        out.push("<tr>" + cellsOf(lines[i]).map((c) => "<td>" + inline(c) + "</td>").join("") + "</tr>");
        i++;
      }
      i--;  // step back: the for-loop will re-increment
      out.push("</tbody></table>");
      continue;
    }
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    const ol = line.match(/^\s*\d+\.\s+(.*)$/);
    const ul = line.match(/^\s*[-*]\s+(.*)$/);
    if (h) { closeList(); out.push("<h4>" + inline(h[2]) + "</h4>"); }
    else if (ol) { if (listType !== "ol") { closeList(); out.push("<ol>"); listType = "ol"; } out.push("<li>" + inline(ol[1]) + "</li>"); }
    else if (ul) { if (listType !== "ul") { closeList(); out.push("<ul>"); listType = "ul"; } out.push("<li>" + inline(ul[1]) + "</li>"); }
    else if (line.trim() === "") { closeList(); }
    else { closeList(); out.push("<p>" + inline(line) + "</p>"); }
  }
  if (inCode) out.push("<pre><code>" + code.join("\n") + "</code></pre>");
  closeList();
  return out.join("");
}

// Close the streaming assistant bubble, rendering its accumulated raw text as markdown.
function finalizeBubble() {
  if (currentBubble && currentBubble._md) {
    currentBubble.innerHTML = renderMarkdown(currentBubble._md);
    chatLogEl.scrollTop = chatLogEl.scrollHeight;
  }
  currentBubble = null;
}

function openChat(host, sessionId) {
  endTour();                         // drop any active tour on host switch
  if (chatWs) {
    try { chatWs.close(); } catch (e) { /* ignore */ }
    chatWs = null;
  }
  currentBubble = null;
  chatLogEl.textContent = "";
  chatSetEnabled(false);
  chatStatusEl.textContent = "connecting…";

  // The session id (when known) binds the socket to a persisted chat so its transcript
  // is restored via the leading `session` frame; without it the server mints a new chat.
  let q = "/ws/chat?host=" + encodeURIComponent(host);
  if (sessionId) q += "&session=" + encodeURIComponent(sessionId);
  const ws = new WebSocket(wsUrl(q));
  chatWs = ws;

  ws.onopen = () => {
    if (chatWs !== ws) return;
    chatStatusEl.textContent = "ready — ask where to go.";
    chatSetEnabled(true);
  };
  ws.onclose = () => {
    if (chatWs === ws) {
      chatStatusEl.textContent = "chat closed.";
      chatSetEnabled(false);
    }
  };
  ws.onerror = () => {
    if (chatWs === ws) chatStatusEl.textContent = "chat connection error.";
  };
  ws.onmessage = (ev) => {
    if (chatWs !== ws) return;
    let data;
    try { data = JSON.parse(ev.data); } catch (e) { return; }
    switch (data.type) {
      case "session":
        // leading bootstrap frame: bind to this session + replay its stored transcript.
        activeSessionId = data.id;
        highlightActiveChip();
        currentBubble = null;
        chatLogEl.textContent = "";
        if (Array.isArray(data.transcript)) {
          for (const entry of data.transcript) renderTranscriptEntry(entry);
        }
        maybeShowRestoreNote(data);
        break;
      case "text":
        if (!currentBubble) { currentBubble = chatAddLine("msg-assistant", ""); currentBubble._md = ""; }
        currentBubble._md += (data.delta || "");
        currentBubble.textContent = currentBubble._md; // plaintext while streaming
        chatLogEl.scrollTop = chatLogEl.scrollHeight;
        break;
      case "tool_use":
        finalizeBubble();
        chatAddLine("msg-tool", "→ " + (data.name || "tool"));
        break;
      case "tool_result":
        chatAddLine("msg-tool", "✓ " + (data.name || "tool") + " · " +
          (data.status || "?"));
        break;
      case "error":
        finalizeBubble();
        if (data.status === "chat_unavailable") {
          // graceful no-key (or missing-dep) case — the rest of the UI still works.
          chatSetEnabled(false);
          chatStatusEl.textContent = "chat unavailable (" + (data.reason || "?") + ")";
        }
        chatAddLine("msg-error", "chat unavailable: " +
          (data.reason || data.status || "unknown") +
          (data.detail ? " — " + data.detail : ""));
        refreshChatSessions();          // an error can still have auto-titled the chat
        break;
      case "tour":
        finalizeBubble();
        addTourOffer(data.data);
        break;
      case "done":
        finalizeBubble();
        // a completed turn may have auto-titled the chat + bumped its recency — refresh
        // the chips (fire-and-forget) without touching the live socket.
        refreshChatSessions();
        break;
      default:
        break;
    }
  };
}

// Append a "Show me How" button to the chat log for a captured tour payload.
// Each button closes over ITS OWN tour so older offers keep working.
function addTourOffer(tour) {
  lastTour = tour;
  const line = document.createElement("div");
  line.className = "msg msg-assistant tour-offer";
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "show-how-btn";
  btn.textContent = "Show me How";   // fixed label, not server data
  const note = document.createElement("span");
  note.className = "show-how-note muted";
  btn.addEventListener("click", () => {
    if (!liveWs || liveWs.readyState !== WebSocket.OPEN) {
      note.textContent = "live view not ready";
      return;
    }
    note.textContent = "";
    startTour(tour);
  });
  line.appendChild(btn);
  line.appendChild(note);
  chatLogEl.appendChild(line);
  chatLogEl.scrollTop = chatLogEl.scrollHeight;
}

if (chatFormEl) {
  chatFormEl.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = chatInputEl.value.trim();
    if (!text || !chatWs || chatWs.readyState !== WebSocket.OPEN) return;
    chatAddLine("msg-user", text);
    currentBubble = null;
    chatWs.send(JSON.stringify({ type: "user_message", text, live_url: currentLiveUrl }));
    chatInputEl.value = "";
  });
}

// --- chat sessions (chips: one persisted chat each; "+ New" mints another) ---
// The chip bar lists /api/hosts/{host}/sessions. Clicking a chip reconnects the chat
// socket to that session (its transcript is restored via the leading `session` frame);
// "+ New" POSTs a session; dbl-click a title renames (PATCH); the trailing × confirms
// then DELETEs. All server strings go through textContent (chips) or renderMarkdown
// (restored assistant text, which escapes first) — never a raw-innerHTML path.

function sessionUrl(host, id) {
  let u = "/api/hosts/" + encodeURIComponent(host) + "/sessions";
  if (id) u += "/" + encodeURIComponent(id);
  return u;
}

// List (or, when a host has none yet, mint) this host's chats, render the chips, and
// open the most-recent one. Guards against a host switch racing the awaits.
async function loadChatSessions(host) {
  activeSessionId = null;
  chatSessions = [];
  let sessions = [];
  try {
    const res = await fetch(sessionUrl(host));
    const data = await res.json();
    sessions = data.sessions || [];
  } catch (err) { sessions = []; }
  if (sessions.length === 0) {
    try {
      const res = await fetch(sessionUrl(host), {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      const created = await res.json();
      if (created && created.id) sessions = [created];
    } catch (err) { sessions = []; }
  }
  if (selectedHost !== host) return;   // a newer host switch already took over
  chatSessions = sessions;
  renderChatChips();
  if (sessions.length) openChat(host, sessions[0].id);   // most recent (updated_at desc)
}

// Fire-and-forget re-list to keep chip titles + recency order fresh WITHOUT reconnecting
// the socket (called after each done/error frame, and after rename/delete of a non-active
// chat). Never disturbs the active socket.
async function refreshChatSessions() {
  const host = selectedHost;
  if (!host) return;
  try {
    const res = await fetch(sessionUrl(host));
    const data = await res.json();
    if (selectedHost !== host) return;
    chatSessions = data.sessions || [];
    renderChatChips();
  } catch (err) { /* keep the current chips */ }
}

function renderChatChips() {
  if (!chatSessionListEl) return;
  chatSessionListEl.textContent = "";
  for (const s of chatSessions) chatSessionListEl.appendChild(buildSessionChip(s));
  if (chatSessionsEl) chatSessionsEl.hidden = chatSessions.length === 0;
}

function highlightActiveChip() {
  if (!chatSessionListEl) return;
  for (const chip of chatSessionListEl.children) {
    chip.classList.toggle("on", chip.dataset.sessionId === activeSessionId);
  }
}

function buildSessionChip(s) {
  const chip = document.createElement("div");
  chip.className = "chat-session-chip" + (s.id === activeSessionId ? " on" : "");
  chip.dataset.sessionId = s.id;
  chip.setAttribute("role", "tab");

  const title = document.createElement("span");
  title.className = "chat-session-title";
  title.textContent = s.title || "Untitled chat";   // server string -> textContent
  chip.appendChild(title);

  const del = document.createElement("button");
  del.type = "button";
  del.className = "chat-session-del";
  del.textContent = "×";                             // fixed glyph, not server data
  del.title = "Delete chat";
  chip.appendChild(del);

  // click a different chip -> reconnect the socket to it (openChat closes the old one).
  chip.addEventListener("click", (e) => {
    if (e.target === del) return;
    if (chip.querySelector(".chat-session-rename")) return;   // mid-rename
    if (s.id === activeSessionId) return;
    openChat(selectedHost, s.id);
  });

  // dbl-click the title -> inline rename input.
  title.addEventListener("dblclick", (e) => {
    e.stopPropagation();
    beginRenameChip(chip, s);
  });

  // × -> first click arms (.confirm, auto-disarms ~2s); a second click deletes.
  del.addEventListener("click", (e) => {
    e.stopPropagation();
    if (del.classList.contains("confirm")) {
      deleteSession(s.id);
    } else {
      del.classList.add("confirm");
      del.textContent = "delete?";
      setTimeout(() => {
        del.classList.remove("confirm");
        del.textContent = "×";
      }, 2000);
    }
  });

  return chip;
}

function beginRenameChip(chip, s) {
  const titleEl = chip.querySelector(".chat-session-title");
  if (!titleEl) return;
  const input = document.createElement("input");
  input.type = "text";
  input.className = "chat-session-rename";
  input.value = s.title || "";
  chip.replaceChild(input, titleEl);
  input.focus();
  input.select();

  let settled = false;
  const commit = (save) => {
    if (settled) return;
    settled = true;
    const next = input.value.trim();
    if (save && next && next !== (s.title || "")) {
      renameSession(s.id, next);       // renameSession re-renders the chips
    } else {
      renderChatChips();               // restore the chip unchanged
      highlightActiveChip();
    }
  };
  input.addEventListener("click", (e) => e.stopPropagation());
  input.addEventListener("dblclick", (e) => e.stopPropagation());
  input.addEventListener("blur", () => commit(true));
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); commit(true); }
    else if (e.key === "Escape") { e.preventDefault(); commit(false); }
  });
}

async function renameSession(id, title) {
  const host = selectedHost;
  if (!host) return;
  try {
    await fetch(sessionUrl(host, id), {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    });
  } catch (err) { /* ignore — the refresh below reflects whatever stuck */ }
  await refreshChatSessions();
  highlightActiveChip();
}

async function deleteSession(id) {
  const host = selectedHost;
  if (!host) return;
  try {
    await fetch(sessionUrl(host, id), { method: "DELETE" });
  } catch (err) { /* ignore */ }
  if (id === activeSessionId) {
    // deleting the open chat: reload the set (loadChatSessions auto-creates + opens a
    // replacement when none are left) so the pane never ends up bound to nothing.
    activeSessionId = null;
    await loadChatSessions(host);
  } else {
    await refreshChatSessions();
    highlightActiveChip();
  }
}

async function newChat() {
  const host = selectedHost;
  if (!host) return;
  try {
    const res = await fetch(sessionUrl(host), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    const created = await res.json();
    if (created && created.id) {
      await refreshChatSessions();
      openChat(host, created.id);
    }
  } catch (err) { /* ignore */ }
}

// Replay ONE stored transcript entry into the chat log. Reuses the SAME safe renderers
// the live stream uses — chatAddLine (textContent), renderMarkdown (escapes first), and
// addTourOffer — so restored (untrusted) content never hits a raw-innerHTML path.
function renderTranscriptEntry(entry) {
  if (!entry || typeof entry !== "object") return;
  switch (entry.type) {
    case "user":
      chatAddLine("msg-user", entry.text || "");
      break;
    case "text": {
      const bubble = chatAddLine("msg-assistant", "");
      bubble.innerHTML = renderMarkdown(entry.text || "");   // renderMarkdown escapes raw
      chatLogEl.scrollTop = chatLogEl.scrollHeight;
      break;
    }
    case "tool_use":
      chatAddLine("msg-tool", "→ " + (entry.name || "tool"));
      break;
    case "tool_result":
      chatAddLine("msg-tool", "✓ " + (entry.name || "tool") + " · " +
        (entry.status || "?"));
      break;
    case "tour":
      addTourOffer(entry.data);
      break;
    case "error":
      chatAddLine("msg-error", "chat unavailable: " +
        (entry.reason || entry.status || "unknown") +
        (entry.detail ? " — " + entry.detail : ""));
      break;
    default:
      break;
  }
}

// The Claude Code backend restores the transcript for DISPLAY only (v1) — it won't recall
// earlier turns yet, so flag a restored view so the user isn't surprised.
function maybeShowRestoreNote(data) {
  if (!chatStatusEl) return;
  if (data.backend === "claude_code" && Array.isArray(data.transcript) &&
      data.transcript.length > 0) {
    chatStatusEl.textContent =
      "restored view — this backend won't recall earlier turns yet";
  }
}

if (chatSessionNewEl) chatSessionNewEl.addEventListener("click", newChat);

// --- live browser pane -------------------------------------------------------
function openLiveView(host) {
  endTour();                         // drop any active tour on host switch
  currentLiveUrl = null;             // forget the prior host's live position
  if (liveWs) {
    try { liveWs.close(); } catch (e) { /* ignore */ }
    liveWs = null;
  }
  if (liveViewEl) liveViewEl.removeAttribute("src");
  if (liveStatusEl) liveStatusEl.textContent = "connecting…";

  const ws = new WebSocket(wsUrl("/ws/screencast?host=" + encodeURIComponent(host)));
  liveWs = ws;

  ws.onclose = () => {
    if (liveWs === ws) {
      endTour();                     // a dead socket can't drive/locate — tear down
      if (liveStatusEl) liveStatusEl.textContent = "live view closed.";
    }
  };
  ws.onerror = () => {
    if (liveWs === ws && liveStatusEl) liveStatusEl.textContent = "live view error.";
  };
  ws.onmessage = (ev) => {
    if (liveWs !== ws) return;
    let data;
    try { data = JSON.parse(ev.data); } catch (e) { return; }
    switch (data.type) {
      case "status":
        if (liveStatusEl) {
          liveStatusEl.textContent = data.authenticated ? "live · authenticated" :
            (data.reason ? "live · " + data.reason : "live");
        }
        break;
      case "frame":
        if (liveViewEl && data.data) {
          liveViewEl.src = "data:image/jpeg;base64," + data.data;
        }
        break;
      case "location":
        // the live browser navigated (the user clicked to a new page) — remember it so
        // the next chat message tells the agent where we are.
        currentLiveUrl = data.url || null;
        break;
      case "located":
        // response to our {type:"locate"} — position the highlight for the step.
        if (data.stepId !== pendingLocateStepId) break;
        if (tourOverlayEl) tourOverlayEl.classList.remove("locating");
        if (data.rect) {
          currentRect = data.rect;
          positionTourBox(currentRect);
          if (tourNextEl) tourNextEl.disabled = false;
        } else {
          // never trap the user: allow Next even when we can't find the element.
          currentRect = null;
          if (tourBoxEl) tourBoxEl.hidden = true;
          if (tourTipTextEl) {
            tourTipTextEl.textContent = "Couldn't find that element on screen — " +
              "you can click it yourself, then press Next";
          }
          if (tourNextEl) tourNextEl.disabled = false;
        }
        break;
      case "stopped":
        if (liveStatusEl) liveStatusEl.textContent = "live view stopped.";
        break;
      case "error":
        if (liveStatusEl) {
          liveStatusEl.textContent = "live unavailable: " +
            (data.reason || data.status || "unknown") +
            (data.detail ? " — " + data.detail : "");
        }
        break;
      default:
        break;
    }
  };
}

// --- interactive input: forward mouse/keyboard to the live browser -----------
// Any embedded live browser is a pixel stream; these handlers make it DRIVEABLE by
// sending CDP Input.* events back over the same socket. Coordinates are mapped from
// the displayed <img> to the browser's viewport via the frame's natural pixel size.
function sendInput(frame) {
  if (liveWs && liveWs.readyState === WebSocket.OPEN) {
    liveWs.send(JSON.stringify(Object.assign({ type: "input" }, frame)));
  }
}

function liveCoords(ev) {
  // The <img> is object-fit:contain, so the actual frame is scaled to fit and
  // letterboxed inside the element box. Undo the scale AND the centering offset so a
  // click maps to the right browser-viewport pixel (else clicks land off-target).
  const rect = liveViewEl.getBoundingClientRect();
  const nw = liveViewEl.naturalWidth, nh = liveViewEl.naturalHeight;
  if (!nw || !nh || !rect.width || !rect.height) return { x: 0, y: 0 };
  const scale = Math.min(rect.width / nw, rect.height / nh);
  const offX = (rect.width - nw * scale) / 2;
  const offY = (rect.height - nh * scale) / 2;
  const x = Math.max(0, Math.min(nw, (ev.clientX - rect.left - offX) / scale));
  const y = Math.max(0, Math.min(nh, (ev.clientY - rect.top - offY) / scale));
  return { x, y };
}

if (liveViewEl) {
  liveViewEl.tabIndex = 0;            // focusable, so it can receive keystrokes
  liveViewEl.draggable = false;
  let lastMove = 0;
  liveViewEl.addEventListener("mousemove", (e) => {
    const now = (window.performance && performance.now()) || Date.now();
    if (now - lastMove < 40) return;  // ~25fps of move events is plenty
    lastMove = now;
    const { x, y } = liveCoords(e);
    sendInput({ kind: "mousemoved", x, y, buttons: e.buttons });
  });
  liveViewEl.addEventListener("mousedown", (e) => {
    e.preventDefault();
    liveViewEl.focus();
    const { x, y } = liveCoords(e);
    sendInput({ kind: "mousepressed", x, y, button: e.button, buttons: e.buttons, clickCount: e.detail || 1 });
  });
  liveViewEl.addEventListener("mouseup", (e) => {
    e.preventDefault();
    const { x, y } = liveCoords(e);
    sendInput({ kind: "mousereleased", x, y, button: e.button, buttons: e.buttons, clickCount: e.detail || 1 });
  });
  liveViewEl.addEventListener("contextmenu", (e) => e.preventDefault()); // allow right-click
  liveViewEl.addEventListener("wheel", (e) => {
    e.preventDefault();
    const { x, y } = liveCoords(e);
    sendInput({ kind: "wheel", x, y, dx: e.deltaX, dy: e.deltaY });
  }, { passive: false });
  liveViewEl.addEventListener("keydown", (e) => {
    if (e.metaKey) return;            // leave OS/browser shortcuts alone
    e.preventDefault();
    if (e.key.length === 1 && !e.ctrlKey && !e.altKey) {
      sendInput({ kind: "text", text: e.key });      // printable char -> insertText
    } else {
      sendInput({ kind: "keydown", key: e.key, code: e.code, keyCode: e.keyCode });
    }
  });
}

// --- guided "Show me How" tour driver ----------------------------------------
// The tour walks the user through a discovered click-path on the SAME live browser
// pane. It highlights each step's element (resolved server-side via `locate`), and
// on "Next" drives a real CDP click through sendInput(), then advances.

// INVERSE of liveCoords(): map a viewport CSS-px rect (getBoundingClientRect space,
// same as liveCoords produces) to a pixel box in the DISPLAYED <img>'s coordinate
// space. The overlay is inset:0 over the same box as the <img>, so the img-relative
// left/top double as overlay-relative left/top. Uses the identical scale + letterbox
// offsets as liveCoords, run forwards: displayX = offX + viewportX * scale.
function rectToDisplay(rect) {
  if (!liveViewEl || !rect) return null;
  const dr = liveViewEl.getBoundingClientRect();
  const nw = liveViewEl.naturalWidth, nh = liveViewEl.naturalHeight;
  if (!nw || !nh || !dr.width || !dr.height) return null;
  const scale = Math.min(dr.width / nw, dr.height / nh);
  const offX = (dr.width - nw * scale) / 2;
  const offY = (dr.height - nh * scale) / 2;
  return {
    left: offX + rect.x * scale,
    top: offY + rect.y * scale,
    width: rect.width * scale,
    height: rect.height * scale,
  };
}

function positionTourBox(rect) {
  if (!tourBoxEl) return;
  const box = rectToDisplay(rect);
  if (!box) return;
  tourBoxEl.hidden = false;
  tourBoxEl.style.left = box.left + "px";
  tourBoxEl.style.top = box.top + "px";
  tourBoxEl.style.width = box.width + "px";
  tourBoxEl.style.height = box.height + "px";
}

function startTour(tour) {
  if (!tour || !liveWs || liveWs.readyState !== WebSocket.OPEN) return;
  tourSteps = Array.isArray(tour.steps) ? tour.steps : [];
  tourIndex = 0;
  pendingLocateStepId = null;
  currentRect = null;
  if (tourSteps.length === 0) { endTour(); return; }
  if (tourOverlayEl) tourOverlayEl.hidden = false;
  showStep(0);
}

function showStep(i) {
  if (!tourSteps) return;
  if (i >= tourSteps.length) { endTour(); return; }
  const step = tourSteps[i] || {};
  const n = tourSteps.length;

  if (tourPrevEl) tourPrevEl.disabled = i <= 0;
  if (tourDoneEl) tourDoneEl.disabled = false;

  if (step.kind === "form") {
    // terminal step — nothing to click; hide the highlight, disable Next.
    if (tourTipTextEl) {
      tourTipTextEl.textContent =
        "You're there — fill in this form to finish. (I won't submit it for you.)";
    }
    if (tourBoxEl) tourBoxEl.hidden = true;
    if (tourOverlayEl) tourOverlayEl.classList.remove("locating");
    currentRect = null;
    pendingLocateStepId = null;
    if (tourNextEl) tourNextEl.disabled = true;
    return;
  }

  // nav / trigger — label is CRAWLED (untrusted): textContent only.
  if (tourTipTextEl) {
    tourTipTextEl.textContent =
      "Step " + (i + 1) + " of " + n + ": Click “" + (step.label || "") + "”";
  }
  currentRect = null;
  pendingLocateStepId = i;
  if (tourNextEl) tourNextEl.disabled = true;     // until `located` arrives
  if (tourOverlayEl) tourOverlayEl.classList.add("locating");
  if (tourBoxEl) tourBoxEl.hidden = false;
  if (liveWs && liveWs.readyState === WebSocket.OPEN) {
    liveWs.send(JSON.stringify({
      type: "locate",
      stepId: i,
      selector: (step.selector != null ? step.selector : null),
      label: step.label || "",
    }));
  }
}

function nextStep() {
  if (!tourSteps) return;
  const step = tourSteps[tourIndex];
  if (!step) { endTour(); return; }
  // If we resolved a rect, drive a real click at its center via the existing input
  // path. If the locate failed (no rect), the user clicked manually — just advance.
  if ((step.kind === "nav" || step.kind === "trigger") && currentRect) {
    const cx = currentRect.x + currentRect.width / 2;
    const cy = currentRect.y + currentRect.height / 2;
    sendInput({ kind: "mousepressed", x: cx, y: cy, button: 0, buttons: 1, clickCount: 1 });
    sendInput({ kind: "mousereleased", x: cx, y: cy, button: 0, buttons: 0, clickCount: 1 });
  }
  tourIndex++;
  // disable Next + hide the stale highlight while the UI settles, so a fast
  // double-click can't skip a step before the next locate resolves.
  if (tourNextEl) tourNextEl.disabled = true;
  if (tourBoxEl) tourBoxEl.hidden = true;
  setTimeout(() => showStep(tourIndex), 600);
}

function prevStep() {
  if (!tourSteps) return;
  tourIndex = Math.max(0, tourIndex - 1);
  showStep(tourIndex);   // just re-highlights; does not un-click anything
}

function endTour() {
  if (tourOverlayEl) {
    tourOverlayEl.hidden = true;
    tourOverlayEl.classList.remove("locating");
  }
  if (tourBoxEl) {
    tourBoxEl.hidden = false;
    tourBoxEl.removeAttribute("style");
  }
  tourSteps = null;
  tourIndex = 0;
  pendingLocateStepId = null;
  currentRect = null;
}

if (tourNextEl) tourNextEl.addEventListener("click", nextStep);
if (tourPrevEl) tourPrevEl.addEventListener("click", prevStep);
if (tourDoneEl) tourDoneEl.addEventListener("click", endTour);

// The <img> scale changes with the pane size, so re-place the highlight on resize.
window.addEventListener("resize", () => {
  if (tourSteps && currentRect) positionTourBox(currentRect);
});

// --- credentials vault (modal) -----------------------------------------------
function openVault() {
  if (!vaultModalEl) return;
  vaultModalEl.hidden = false;
  loadVaultStatus();
  loadCredentials();
}

function closeVault() {
  if (vaultModalEl) vaultModalEl.hidden = true;
}

if (tabWorkspaceEl) tabWorkspaceEl.addEventListener("click", () => setView("workspace"));
if (tabGraphEl) tabGraphEl.addEventListener("click", () => setView("graph"));
if (tabExploreEl) tabExploreEl.addEventListener("click", () => setView("explore"));

if (vaultOpenEl) vaultOpenEl.addEventListener("click", openVault);
if (vaultCloseEl) vaultCloseEl.addEventListener("click", closeVault);
if (vaultBackdropEl) vaultBackdropEl.addEventListener("click", closeVault);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && vaultModalEl && !vaultModalEl.hidden) closeVault();
  if (e.key === "Escape" && cmdkModalEl && !cmdkModalEl.hidden) closeCmdk();
});

async function loadVaultStatus() {
  try {
    const res = await fetch("/api/vault/status");
    const data = await res.json();
    if (data.available) {
      vaultStatusEl.textContent = "keyring backend: " + (data.backend || "ok");
    } else {
      vaultStatusEl.textContent = "keyring unavailable (" + (data.reason || "?") +
        ") — " + (data.detail || "");
    }
  } catch (err) {
    vaultStatusEl.textContent = "could not read keyring status";
  }
}

async function loadCredentials() {
  try {
    const res = await fetch("/api/vault/credentials");
    const data = await res.json();
    credsEl.textContent = "";
    const rows = data.credentials || [];
    if (rows.length === 0) {
      const li = document.createElement("li");
      li.className = "muted pad";
      li.textContent = "no stored credentials yet";
      credsEl.appendChild(li);
      return;
    }
    for (const c of rows) credsEl.appendChild(buildCredRow(c));
  } catch (err) {
    credsEl.textContent = "";
    const li = document.createElement("li");
    li.className = "muted pad";
    li.textContent = "failed to load credentials";
    credsEl.appendChild(li);
  }
}

function buildCredRow(c) {
  const li = document.createElement("li");

  const main = document.createElement("span");
  main.className = "cred-main";
  main.textContent = c.host + "  ·  " + (c.username || "?") +
    (c.url ? "  ·  " + c.url : "");
  li.appendChild(main);

  const badge = document.createElement("span");
  // has_password: true (stored) / false (missing) / null (keyring unreadable).
  badge.className = "badge " + (c.has_password === true ? "ok" :
    c.has_password === false ? "warn" : "unknown");
  badge.textContent = c.has_password === true ? "has password" :
    c.has_password === false ? "no password" : "keyring ?";
  li.appendChild(badge);

  const del = document.createElement("button");
  del.type = "button";
  del.className = "cred-del";
  del.textContent = "delete";
  del.addEventListener("click", () => deleteCredential(c.host));
  li.appendChild(del);

  return li;
}

async function deleteCredential(host) {
  credMsgEl.textContent = "deleting " + host + "…";
  try {
    const res = await fetch("/api/vault/credentials/" + encodeURIComponent(host),
      { method: "DELETE" });
    const data = await res.json();
    credMsgEl.textContent = "deleted " + host + " (routing_removed=" +
      data.routing_removed + ", secret_removed=" + data.secret_removed + ")";
  } catch (err) {
    credMsgEl.textContent = "delete failed";
  }
  loadCredentials();
}

if (credFormEl) {
  credFormEl.addEventListener("submit", async (e) => {
    e.preventDefault();
    const host = credHostEl.value.trim();
    if (!host) {
      credMsgEl.textContent = "enter a host first.";
      return;
    }
    const payload = { password: credPasswordEl.value };
    for (const f of CRED_FIELDS) {
      const input = el("cred-" + f);
      const v = input ? input.value.trim() : "";
      if (v) payload[f] = v;
    }
    credMsgEl.textContent = "saving…";
    try {
      const res = await fetch("/api/vault/credentials/" + encodeURIComponent(host), {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (data.status === "ok") {
        credMsgEl.textContent = "saved " + host;
      } else {
        credMsgEl.textContent = "save failed: " + (data.status || "?") +
          (data.detail ? " — " + data.detail : "");
      }
    } catch (err) {
      credMsgEl.textContent = "save request failed";
    } finally {
      // Clear the password input immediately, regardless of the outcome.
      credPasswordEl.value = "";
    }
    loadCredentials();
  });
}

// --- new crawl ---------------------------------------------------------------
// Enter a URL -> open /ws/crawl, stream progress/log lines into #crawl-progress,
// and on a terminal frame re-enable the form + auto-select the new host so its graph
// opens immediately. The feature is off by default (a `crawl_unavailable`/`disabled`
// frame is shown gracefully). textContent only for every server-sourced string.

function crawlAddLine(text) {
  if (!crawlProgressEl) return;
  crawlProgressEl.hidden = false;
  const div = document.createElement("div");
  div.className = "crawl-line";
  div.textContent = text;                    // textContent — never innerHTML
  crawlProgressEl.appendChild(div);
  crawlProgressEl.scrollTop = crawlProgressEl.scrollHeight;
}

function crawlSetRunning(on) {
  if (crawlUrlEl) crawlUrlEl.disabled = on;
  if (crawlSubmitEl) crawlSubmitEl.disabled = on;
  if (crawlMaxStatesEl) crawlMaxStatesEl.disabled = on;
  if (crawlMaxDepthEl) crawlMaxDepthEl.disabled = on;
  if (crawlCancelEl) crawlCancelEl.hidden = !on;
}

// Re-fetch /api/hosts, find the freshly-promoted host, and open it via selectHost so
// its Graph view + chat are immediately usable.
async function autoSelectHost(host) {
  try {
    const res = await fetch("/api/hosts");
    const data = await res.json();
    const entry = (data.hosts || []).find((h) => h.host === host);
    if (entry) selectHost(entry);
  } catch (err) { /* the sidebar refresh already ran; ignore */ }
}

function cancelCrawl() {
  if (crawlWs && crawlWs.readyState === WebSocket.OPEN) {
    crawlWs.send(JSON.stringify({ type: "cancel" }));
  }
}

function startCrawl(url, opts) {
  if (crawlWs) { try { crawlWs.close(); } catch (e) { /* ignore */ } crawlWs = null; }
  if (crawlProgressEl) crawlProgressEl.textContent = "";
  crawlSetRunning(true);
  crawlAddLine("connecting…");

  let q = "/ws/crawl?url=" + encodeURIComponent(url);
  if (opts && opts.maxStates) q += "&max_states=" + encodeURIComponent(opts.maxStates);
  if (opts && opts.maxDepth) q += "&max_depth=" + encodeURIComponent(opts.maxDepth);

  const ws = new WebSocket(wsUrl(q));
  crawlWs = ws;
  let doneHost = null;

  ws.onmessage = (ev) => {
    if (crawlWs !== ws) return;
    let data;
    try { data = JSON.parse(ev.data); } catch (e) { return; }
    switch (data.type) {
      case "status":
        crawlAddLine("starting crawl of " + (data.host || "") + "…");
        break;
      case "progress":
        crawlAddLine("· " + (data.states || 0) + " states / " +
          (data.visits || 0) + " visits · depth " + (data.depth || 0) +
          " · " + (data.url || ""));
        break;
      case "log":
        crawlAddLine(data.line || "");
        break;
      case "done":
        doneHost = data.host || null;
        crawlAddLine("✓ done: " + (data.states || 0) + " states, " +
          (data.edges || 0) + " edges, " + (data.triggers || 0) + " triggers" +
          (data.complete ? "" : " (partial: " + (data.stopped || "stopped") + ")"));
        break;
      case "cancelled":
        doneHost = data.promoted ? (data.host || null) : null;
        crawlAddLine("cancelled" + (data.promoted ?
          " — saved partial graph (" + (data.states || 0) + " states)" :
          " (nothing saved)"));
        break;
      case "error":
        if (data.status === "crawl_unavailable" && data.reason === "disabled") {
          crawlAddLine("live crawl is disabled on this server.");
        } else {
          crawlAddLine("crawl failed: " + (data.reason || data.status || "unknown") +
            (data.detail ? " — " + data.detail : ""));
        }
        break;
      default:
        break;
    }
  };
  ws.onclose = () => {
    if (crawlWs !== ws) return;
    crawlWs = null;
    crawlSetRunning(false);
    loadHosts();
    if (doneHost) autoSelectHost(doneHost);
  };
  ws.onerror = () => {
    if (crawlWs === ws) crawlAddLine("crawl connection error.");
  };
}

if (crawlFormEl) {
  crawlFormEl.addEventListener("submit", (e) => {
    e.preventDefault();
    const url = crawlUrlEl ? crawlUrlEl.value.trim() : "";
    if (!url) { crawlAddLine("enter a URL first."); return; }
    startCrawl(url, {
      maxStates: crawlMaxStatesEl ? crawlMaxStatesEl.value.trim() : "",
      maxDepth: crawlMaxDepthEl ? crawlMaxDepthEl.value.trim() : "",
    });
  });
}
if (crawlCancelEl) crawlCancelEl.addEventListener("click", cancelCrawl);

// --- command palette (Ctrl/Cmd+K) --------------------------------------------
// A keyboard launcher over EXISTING state + functions — it opens NO new fetches. It
// mirrors openVault/closeVault for the modal lifecycle, builds its action list from the
// current view tabs + sidebar rows, and runs each action through the same globals the UI
// already exposes (setView / newChat / openVault / setExploreSubtab). Every row is built
// with createElement + textContent (host names are untrusted crawled data).

let cmdkItems = [];   // the currently rendered (filtered) action list
let cmdkIndex = 0;    // index of the highlighted row

function openCmdk() {
  if (!cmdkModalEl) return;
  cmdkModalEl.hidden = false;
  if (cmdkInputEl) cmdkInputEl.value = "";
  cmdkIndex = 0;
  renderCmdk();
  if (cmdkInputEl) cmdkInputEl.focus();
}

function closeCmdk() {
  if (cmdkModalEl) cmdkModalEl.hidden = true;
}

// Build the candidate actions from live state. `disabled` rows still render (greyed with a
// hint) so the palette explains WHY an action is unavailable instead of hiding it.
function cmdkActions() {
  const hasHost = !!selectedHost;
  const graphOff = !!(tabGraphEl && tabGraphEl.disabled);
  const exploreOff = !!(tabExploreEl && tabExploreEl.disabled);
  const actions = [
    { label: "Go to Workspace", run: () => setView("workspace") },
    { label: "Go to Graph", disabled: graphOff,
      hint: graphOff ? "unavailable for this host" : "", run: () => setView("graph") },
    { label: "Go to Explore", disabled: exploreOff,
      hint: exploreOff ? "unavailable for this host" : "", run: () => setView("explore") },
    { label: "New chat", disabled: !hasHost, hint: hasHost ? "" : "pick a host first",
      run: () => { if (typeof newChat === "function") newChat(); } },
    // The sidebar crawl form works with NO host selected (it's how you crawl your first
    // host), so this action is never host-gated — only the server's opt-in gate applies.
    { label: "New crawl", run: () => { if (crawlUrlEl) crawlUrlEl.focus(); } },
    { label: "Manage credentials", run: () => openVault() },
  ];
  // dynamic "Switch to <host>" rows — read each sidebar row's label + replay its click.
  if (hostsEl) {
    for (const row of hostsEl.children) {
      const labelEl = row.querySelector(".host-label");
      if (!labelEl || !labelEl.textContent) continue;
      const name = labelEl.textContent;          // untrusted — only ever used via textContent
      actions.push({ label: "Switch to " + name, run: () => row.click() });
    }
  }
  return actions;
}

function submitExploreSearch(query) {
  setView("explore");
  if (typeof setExploreSubtab === "function") setExploreSubtab("search");
  const input = el("explore-search-input");
  if (input) input.value = query;
  const form = el("explore-search-form");
  if (form) {
    if (typeof form.requestSubmit === "function") form.requestSubmit();
    else form.dispatchEvent(new Event("submit", { cancelable: true }));
  }
}

function renderCmdk() {
  if (!cmdkResultsEl) return;
  const q = (cmdkInputEl ? cmdkInputEl.value : "").trim();
  const ql = q.toLowerCase();
  let items = cmdkActions();
  if (ql) items = items.filter((a) => a.label.toLowerCase().includes(ql));
  // free-text fallback: search captured content for the typed query.
  if (q) {
    const hasHost = !!selectedHost;
    items.push({
      label: "Search content for “" + q + "”",
      disabled: !hasHost, hint: hasHost ? "" : "pick a host first",
      run: () => submitExploreSearch(q),
    });
  }
  cmdkItems = items;
  if (cmdkIndex >= items.length) cmdkIndex = Math.max(0, items.length - 1);

  cmdkResultsEl.textContent = "";
  items.forEach((a, i) => {
    const li = document.createElement("li");
    li.className = "cmdk-item" + (i === cmdkIndex ? " active" : "") +
      (a.disabled ? " disabled" : "");
    li.setAttribute("role", "option");
    li.setAttribute("aria-selected", i === cmdkIndex ? "true" : "false");
    const label = document.createElement("span");
    label.className = "cmdk-label";
    label.textContent = a.label;                 // includes untrusted host name — textContent
    li.appendChild(label);
    if (a.hint) {
      const hint = document.createElement("span");
      hint.className = "cmdk-hint";
      hint.textContent = a.hint;
      li.appendChild(hint);
    }
    li.addEventListener("mouseenter", () => { cmdkIndex = i; highlightCmdk(); });
    li.addEventListener("click", () => runCmdkItem(a));
    cmdkResultsEl.appendChild(li);
  });
}

function highlightCmdk() {
  if (!cmdkResultsEl) return;
  const rows = cmdkResultsEl.children;
  for (let i = 0; i < rows.length; i++) {
    const on = i === cmdkIndex;
    rows[i].classList.toggle("active", on);
    rows[i].setAttribute("aria-selected", on ? "true" : "false");
    if (on && rows[i].scrollIntoView) rows[i].scrollIntoView({ block: "nearest" });
  }
}

function runCmdkItem(item) {
  if (!item || item.disabled) return;
  closeCmdk();
  try { item.run(); } catch (err) { /* an action's own failure never breaks the palette */ }
}

if (cmdkOpenEl) cmdkOpenEl.addEventListener("click", openCmdk);
if (cmdkBackdropEl) cmdkBackdropEl.addEventListener("click", closeCmdk);
if (cmdkInputEl) {
  cmdkInputEl.addEventListener("input", () => { cmdkIndex = 0; renderCmdk(); });
  cmdkInputEl.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (cmdkItems.length) { cmdkIndex = (cmdkIndex + 1) % cmdkItems.length; highlightCmdk(); }
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (cmdkItems.length) {
        cmdkIndex = (cmdkIndex - 1 + cmdkItems.length) % cmdkItems.length;
        highlightCmdk();
      }
    } else if (e.key === "Enter") {
      e.preventDefault();
      runCmdkItem(cmdkItems[cmdkIndex]);
    }
  });
}
// The global Ctrl/Cmd+K shortcut — works even while a chat/crawl input is focused.
document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    openCmdk();
  }
});

// --- boot --------------------------------------------------------------------
loadHosts();
