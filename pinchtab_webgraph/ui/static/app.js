// Phase-1 placeholder front-end: populate the host sidebar from /api/hosts, let a
// click pick a host, and run a how-to against /api/hosts/<host>/howto. Vanilla JS,
// no framework, no build step — Phase 5's SPA replaces this file wholesale.
"use strict";

const hostsEl = document.getElementById("hosts");
const cachesDirEl = document.getElementById("caches-dir");
const hostInput = document.getElementById("host");
const goalInput = document.getElementById("goal");
const resultsEl = document.getElementById("results");

async function loadHosts() {
  try {
    const res = await fetch("/api/hosts");
    const data = await res.json();
    hostsEl.innerHTML = "";
    if (!data.hosts || data.hosts.length === 0) {
      hostsEl.innerHTML = '<li class="muted">no cached hosts yet</li>';
    } else {
      for (const h of data.hosts) {
        const li = document.createElement("li");
        const kind = h.summary ? h.summary.graph_kind : (h.error ? "error" : "?");
        li.textContent = h.host;
        const badge = document.createElement("span");
        badge.className = "badge";
        badge.textContent = kind;
        li.appendChild(badge);
        li.addEventListener("click", () => {
          hostInput.value = h.host;
          // reflect the selection in the sidebar.
          for (const el of hostsEl.children) el.classList.remove("selected");
          li.classList.add("selected");
          openChat(h.host);
          openLiveView(h.host);
        });
        hostsEl.appendChild(li);
      }
    }
    if (data.caches_dir) cachesDirEl.textContent = "caches: " + data.caches_dir;
  } catch (err) {
    hostsEl.innerHTML = '<li class="muted">failed to load hosts</li>';
  }
}

function renderResult(data) {
  resultsEl.innerHTML = "";
  const status = document.createElement("p");
  status.className = "status";
  status.textContent = "status: " + (data.status || "(none)");
  resultsEl.appendChild(status);

  // structured render of the first shortest result, if the how-to matched.
  if (data.status === "ok" && Array.isArray(data.results)) {
    for (const r of data.results) {
      const box = document.createElement("div");
      box.className = "result-box";
      const h = document.createElement("h3");
      h.textContent = r.trigger_label + "  (" + r.clicks + " clicks)";
      box.appendChild(h);
      const ol = document.createElement("ol");
      for (const step of r.steps || []) {
        const li = document.createElement("li");
        li.textContent = step;
        ol.appendChild(li);
      }
      box.appendChild(ol);
      resultsEl.appendChild(box);
    }
  }

  // always show the raw JSON too — this is a debugging placeholder UI.
  const pre = document.createElement("pre");
  pre.textContent = JSON.stringify(data, null, 2);
  resultsEl.appendChild(pre);
}

document.getElementById("howto-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const host = hostInput.value.trim();
  const goal = goalInput.value.trim();
  if (!host) {
    resultsEl.innerHTML = '<p class="muted">enter a host first.</p>';
    return;
  }
  resultsEl.innerHTML = '<p class="muted">querying…</p>';
  try {
    const url = "/api/hosts/" + encodeURIComponent(host) +
      "/howto?goal=" + encodeURIComponent(goal);
    const res = await fetch(url);
    const data = await res.json();
    renderResult(data);
  } catch (err) {
    resultsEl.innerHTML = '<p class="muted">request failed.</p>';
  }
});

loadHosts();

// --- Phase-2 credentials vault (placeholder; Phase 5's SPA replaces this) -----
// Plain-DOM, textContent only (never innerHTML with server data). The password is
// write-only: we never receive it back, never prefill it, and clear the input the
// instant a save request returns — success or failure.
const credsEl = document.getElementById("creds");
const vaultStatusEl = document.getElementById("vault-status");
const credMsgEl = document.getElementById("cred-msg");
const credHost = document.getElementById("cred-host");
const credPassword = document.getElementById("cred-password");

const CRED_FIELDS = ["url", "username", "userField", "passField", "submit",
  "successUrl", "keyringService"];

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
    credsEl.innerHTML = "";
    const rows = data.credentials || [];
    if (rows.length === 0) {
      credsEl.innerHTML = '<li class="muted">no stored credentials yet</li>';
      return;
    }
    for (const c of rows) {
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

      credsEl.appendChild(li);
    }
  } catch (err) {
    credsEl.innerHTML = '<li class="muted">failed to load credentials</li>';
  }
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

document.getElementById("cred-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const host = credHost.value.trim();
  if (!host) {
    credMsgEl.textContent = "enter a host first.";
    return;
  }
  const payload = { password: credPassword.value };
  for (const f of CRED_FIELDS) {
    const el = document.getElementById("cred-" + f);
    const v = el ? el.value.trim() : "";
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
    credPassword.value = "";
  }
  loadCredentials();
});

loadVaultStatus();
loadCredentials();

// --- Phase-3 chat agent (placeholder; Phase 5's SPA replaces this) ------------
// Streams over a WebSocket to /ws/chat?host=<host>. Plain-DOM, textContent ONLY —
// server text (model output, tool names, errors) never touches innerHTML.
const chatLogEl = document.getElementById("chat-log");
const chatStatusEl = document.getElementById("chat-status");
const chatFormEl = document.getElementById("chat-form");
const chatInputEl = document.getElementById("chat-input");
const chatSendEl = chatFormEl ? chatFormEl.querySelector("button") : null;

let chatWs = null;
let chatHost = null;
let currentBubble = null; // the assistant bubble being streamed into

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
  // Close any prior socket before opening a new one.
  if (chatWs) {
    try { chatWs.close(); } catch (e) { /* ignore */ }
    chatWs = null;
  }
  chatHost = host;
  currentBubble = null;
  chatLogEl.innerHTML = "";
  chatSetEnabled(false);
  chatStatusEl.textContent = "connecting to " + host + "…";

  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const url = proto + "//" + location.host + "/ws/chat?host=" +
    encodeURIComponent(host);
  const ws = new WebSocket(url);
  chatWs = ws;

  ws.onopen = () => {
    chatStatusEl.textContent = "chatting about " + host;
    chatSetEnabled(true);
  };
  ws.onclose = () => {
    if (chatWs === ws) {
      chatStatusEl.textContent = "chat closed — pick a host to reconnect.";
      chatSetEnabled(false);
    }
  };
  ws.onerror = () => {
    chatStatusEl.textContent = "chat connection error.";
  };
  ws.onmessage = (ev) => {
    let data;
    try { data = JSON.parse(ev.data); } catch (e) { return; }
    switch (data.type) {
      case "text":
        if (!currentBubble) currentBubble = chatAddLine("msg-assistant", "");
        // extend the current assistant bubble via textContent (never innerHTML).
        currentBubble.textContent += (data.delta || "");
        chatLogEl.scrollTop = chatLogEl.scrollHeight;
        break;
      case "tool_use":
        currentBubble = null;
        chatAddLine("msg-tool", "used tool " + (data.name || "?"));
        break;
      case "tool_result":
        chatAddLine("msg-tool", "tool " + (data.name || "?") + " → " +
          (data.status || "?"));
        break;
      case "error":
        currentBubble = null;
        chatAddLine("msg-error", "error: " + (data.reason || data.status ||
          "unknown") + (data.detail ? " — " + data.detail : ""));
        break;
      case "done":
        // finalize the current assistant bubble.
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

// --- Phase-4 live browser pane (placeholder; Phase 5 owns the two-pane layout) --
// A CDP screencast of a headless Chrome streams over /ws/screencast?host=<host> as
// base64 JPEG frames; we paint each into an <img>. Read-only: no client->server
// input. Server text (status/reason) never touches innerHTML — textContent only.
const liveViewEl = document.getElementById("live-view");
const liveStatusEl = document.getElementById("live-status");

let liveWs = null;

function openLiveView(host) {
  // Close any prior live socket before opening a new one.
  if (liveWs) {
    try { liveWs.close(); } catch (e) { /* ignore */ }
    liveWs = null;
  }
  if (liveViewEl) liveViewEl.removeAttribute("src");
  if (liveStatusEl) liveStatusEl.textContent = "connecting to " + host + "…";

  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const url = proto + "//" + location.host + "/ws/screencast?host=" +
    encodeURIComponent(host);
  const ws = new WebSocket(url);
  liveWs = ws;

  ws.onclose = () => {
    if (liveWs === ws && liveStatusEl) {
      liveStatusEl.textContent = "live view closed — pick a host to reconnect.";
    }
  };
  ws.onerror = () => {
    if (liveStatusEl) liveStatusEl.textContent = "live view connection error.";
  };
  ws.onmessage = (ev) => {
    let data;
    try { data = JSON.parse(ev.data); } catch (e) { return; }
    switch (data.type) {
      case "status":
        if (liveStatusEl) {
          liveStatusEl.textContent = "live: " + host +
            (data.authenticated ? " (authenticated)" :
              data.reason ? " (" + data.reason + ")" : "");
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
          liveStatusEl.textContent = "live view error: " + (data.reason ||
            data.status || "unknown") + (data.detail ? " — " + data.detail : "");
        }
        break;
      default:
        break;
    }
  };
}
