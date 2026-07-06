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
        if (!currentBubble) currentBubble = chatAddLine("msg-assistant", "");
        currentBubble.textContent += (data.delta || "");
        chatLogEl.scrollTop = chatLogEl.scrollHeight;
        break;
      case "tool_use":
        currentBubble = null;
        chatAddLine("msg-tool", "→ " + (data.name || "tool"));
        break;
      case "tool_result":
        chatAddLine("msg-tool", "✓ " + (data.name || "tool") + " · " +
          (data.status || "?"));
        break;
      case "error":
        currentBubble = null;
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
        currentBubble = null;
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
