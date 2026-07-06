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

const chatLogEl = el("chat-log");
const chatStatusEl = el("chat-status");
const chatFormEl = el("chat-form");
const chatInputEl = el("chat-input");
const chatSendEl = chatFormEl ? chatFormEl.querySelector("button") : null;

const liveViewEl = el("live-view");
const liveStatusEl = el("live-status");

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

// --- shared socket / selection state -----------------------------------------
let chatWs = null;
let liveWs = null;
let selectedHost = null;
let currentBubble = null; // the assistant bubble currently streaming

function wsUrl(pathAndQuery) {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return proto + "//" + location.host + pathAndQuery;
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
    }
  } catch (err) { /* keep the index summary */ }

  openChat(host);
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
  for (const line of lines) {
    if (line.trim().startsWith("```")) {
      if (inCode) { out.push("<pre><code>" + code.join("\n") + "</code></pre>"); code = []; inCode = false; }
      else { closeList(); inCode = true; }
      continue;
    }
    if (inCode) { code.push(line); continue; }
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

function openChat(host) {
  if (chatWs) {
    try { chatWs.close(); } catch (e) { /* ignore */ }
    chatWs = null;
  }
  currentBubble = null;
  chatLogEl.textContent = "";
  chatSetEnabled(false);
  chatStatusEl.textContent = "connecting…";

  const ws = new WebSocket(wsUrl("/ws/chat?host=" + encodeURIComponent(host)));
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
        break;
      case "done":
        finalizeBubble();
        break;
      default:
        break;
    }
  };
}

if (chatFormEl) {
  chatFormEl.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = chatInputEl.value.trim();
    if (!text || !chatWs || chatWs.readyState !== WebSocket.OPEN) return;
    chatAddLine("msg-user", text);
    currentBubble = null;
    chatWs.send(JSON.stringify({ type: "user_message", text }));
    chatInputEl.value = "";
  });
}

// --- live browser pane -------------------------------------------------------
function openLiveView(host) {
  if (liveWs) {
    try { liveWs.close(); } catch (e) { /* ignore */ }
    liveWs = null;
  }
  if (liveViewEl) liveViewEl.removeAttribute("src");
  if (liveStatusEl) liveStatusEl.textContent = "connecting…";

  const ws = new WebSocket(wsUrl("/ws/screencast?host=" + encodeURIComponent(host)));
  liveWs = ws;

  ws.onclose = () => {
    if (liveWs === ws && liveStatusEl) liveStatusEl.textContent = "live view closed.";
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

if (vaultOpenEl) vaultOpenEl.addEventListener("click", openVault);
if (vaultCloseEl) vaultCloseEl.addEventListener("click", closeVault);
if (vaultBackdropEl) vaultBackdropEl.addEventListener("click", closeVault);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && vaultModalEl && !vaultModalEl.hidden) closeVault();
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

// --- boot --------------------------------------------------------------------
loadHosts();
