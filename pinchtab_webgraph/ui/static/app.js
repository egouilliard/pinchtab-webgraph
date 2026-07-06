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
