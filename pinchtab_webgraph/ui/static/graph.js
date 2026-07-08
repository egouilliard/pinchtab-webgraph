// pinchtab-webgraph web UI — the interactive Graph view controller.
//
// Lazily injected by app.js (after the 6 vendored Cytoscape libs) the FIRST time the
// "Graph" tab is opened. It fetches the existing GET /api/hosts/{host}/graph, adapts the
// interaction-graph schema to Cytoscape elements CLIENT-SIDE, and renders it with the
// Phase-1 fcose layout + node/edge visual language (a flat version of crawl.py's viewer).
//
// SAFETY DISCIPLINE (kept from app.js):
//   * textContent ONLY for every crawled string (labels/urls/selectors are LLM-adjacent,
//     untrusted site text) — node labels ride Cytoscape's CANVAS renderer (no HTML surface),
//     and the detail panel is built with createElement + textContent, never innerHTML.
//   * chat-input ownership stays in app.js: the "Ask in chat" button calls prefillChat().
//   * cy.destroy() on every host switch + before any rebuild — no leaked WebGL/canvas ctx.
"use strict";

// module state — a single live Cytoscape instance + the host it was built for.
let cy = null;
let cyHost = null;
let graphSearchWired = false;

// static palette (Cytoscape draws on a canvas, so it can't read CSS custom properties;
// these mirror crawl.py's node/edge language: accent blue states, green triggers, gray links).
const C_ACCENT = "#2563eb";
const C_TRIGGER = "#16a34a";
const C_LINK = "#94a3b8";

// The Phase-1 fcose config, VERBATIM (kept byte-identical to crawl.py's LAYOUTS.fcose so
// the SPA graph lays out exactly like the standalone .html viewer). No compound/group
// nodes in v1, so packComponents/tile still apply but there are no parents.
const FCOSE = {
  name: "fcose", quality: "default", animate: false, randomize: false,
  nodeDimensionsIncludeLabels: false, nodeRepulsion: 14000, idealEdgeLength: 75,
  nestingFactor: 0.2, gravity: 0.12, gravityCompound: 1.4, gravityRangeCompound: 2,
  packComponents: true, nodeSeparation: 130, tile: true, componentSpacing: 140,
};

// --- pure adapter: interaction-graph schema -> Cytoscape elements ------------
// states -> state nodes; edges {from,to,...} -> {source,target,...}; each trigger[i]
// becomes a diamond node "trigger:i" plus a state->trigger edge (kind:"trigger"). A
// trigger whose `state` is not a known state id is DROPPED silently (never a dangling edge).
function adaptInteractionGraph(raw) {
  const states = Array.isArray(raw.states) ? raw.states : [];
  const edges = Array.isArray(raw.edges) ? raw.edges : [];
  const triggers = Array.isArray(raw.triggers) ? raw.triggers : [];

  const stateIds = new Set(states.map((s) => s.id));
  const els = [];

  for (const s of states) {
    els.push({ data: {
      id: s.id,
      label: s.label || urlPath(s.url) || s.id,
      url: s.url,
      depth: s.depth,
      type: "state",
    } });
  }

  edges.forEach((e, i) => {
    els.push({ data: {
      id: "e" + i,
      source: e.from,
      target: e.to,
      label: e.label,
      kind: e.kind,
      selector: e.selector,
    } });
  });

  triggers.forEach((t, i) => {
    if (!stateIds.has(t.state)) return;   // drop a trigger anchored to an unknown state
    const tid = "trigger:" + i;
    const form = t.form || {};
    els.push({ data: {
      id: tid,
      type: "trigger",
      label: t.label,
      formTitle: form.title,
      fieldCount: form.fieldCount,
      opensAt: t.opensAt,
    } });
    els.push({ data: { id: "te" + i, source: t.state, target: tid, kind: "trigger" } });
  });

  return els;
}

function urlPath(u) {
  try {
    const segs = new URL(u).pathname.split("/").filter(Boolean);
    return segs.length ? "/" + segs.slice(-2).join("/") : "/";
  } catch (e) { return null; }
}

// --- render ------------------------------------------------------------------
async function openGraphView(host) {
  const statusEl = document.getElementById("graph-status");
  if (!host) { if (statusEl) statusEl.textContent = "Pick a host first."; return; }

  // Same host already rendered — just re-fit to the (possibly resized) canvas; no refetch.
  if (cy && cyHost === host) {
    cy.resize();
    cy.fit(null, 40);
    return;
  }
  if (cy) { cy.destroy(); cy = null; cyHost = null; }

  if (statusEl) statusEl.textContent = "loading graph…";
  let data;
  try {
    const res = await fetch("/api/hosts/" + encodeURIComponent(host) + "/graph");
    data = await res.json();
  } catch (err) {
    if (statusEl) statusEl.textContent = "Could not load the graph (network error).";
    return;
  }

  // Branch SAFELY on payload shape — never call undefined.map on a non-interaction graph:
  //   * a structured error (data.status) -> report it
  //   * a link-graph (or anything without a states[] array) -> point at the .html viewer
  if (data && data.status) {
    if (statusEl) statusEl.textContent = "Cannot render graph: " + data.status +
      (data.host ? " (" + data.host + ")" : "");
    return;
  }
  if (!data || !Array.isArray(data.states)) {
    if (statusEl) {
      statusEl.textContent = "Graph view supports interaction graphs; this host is a " +
        "link graph — open its standalone .html viewer to explore it.";
    }
    return;
  }

  const elements = adaptInteractionGraph(data);
  cy = cytoscape({
    container: document.getElementById("graph-canvas"),
    elements: elements,
    wheelSensitivity: 0.2,
    style: [
      // state nodes: accent ellipse, sized by out-degree (mirrors crawl.py's sqrt sizing)
      { selector: 'node[type="state"]', style: {
        "shape": "ellipse",
        "background-color": C_ACCENT,
        "border-width": 1.5,
        "border-color": "#ffffff",
        "width": (ele) => 16 + Math.min(34, Math.sqrt(ele.outdegree(false)) * 5),
        "height": (ele) => 16 + Math.min(34, Math.sqrt(ele.outdegree(false)) * 5),
        "label": "data(label)",
        "font-size": 10,
        "color": "#1a1d21",
        "text-valign": "bottom",
        "text-margin-y": 3,
        "text-wrap": "ellipsis",
        "text-max-width": 120,
        "min-zoomed-font-size": 9,
        "text-background-color": "#ffffff",
        "text-background-opacity": 0.7,
        "text-background-padding": 2,
      } },
      // trigger nodes: green diamond, fixed small
      { selector: 'node[type="trigger"]', style: {
        "shape": "diamond",
        "background-color": C_TRIGGER,
        "border-width": 1.5,
        "border-color": "#ffffff",
        "width": 18,
        "height": 18,
        "label": "data(label)",
        "font-size": 10,
        "color": "#1a1d21",
        "text-valign": "bottom",
        "text-margin-y": 3,
        "text-wrap": "ellipsis",
        "text-max-width": 120,
        "min-zoomed-font-size": 9,
        "text-background-color": "#ffffff",
        "text-background-opacity": 0.7,
        "text-background-padding": 2,
      } },
      // edges: link = solid gray arrow; any other non-trigger edge = dashed; trigger = dotted green
      { selector: "edge", style: {
        "width": 1.4,
        "line-color": C_LINK,
        "line-style": "dashed",
        "curve-style": "bezier",
        "target-arrow-shape": "triangle",
        "target-arrow-color": C_LINK,
        "arrow-scale": 0.7,
        "opacity": 0.7,
      } },
      { selector: 'edge[kind="link"]', style: { "line-style": "solid" } },
      { selector: 'edge[kind="trigger"]', style: {
        "line-style": "dotted", "line-color": C_TRIGGER, "target-arrow-color": C_TRIGGER,
      } },
      // focus classes (adjacency highlight): dim everything, spotlight the neighborhood
      { selector: ".dim", style: { "opacity": 0.12 } },
      { selector: "node.hl", style: { "border-width": 3, "border-color": "#0f172a", "z-index": 30 } },
      { selector: "edge.hl", style: { "opacity": 1, "width": 2.4, "z-index": 25 } },
      { selector: ".filtered-out", style: { "display": "none" } },
    ],
  });
  cyHost = host;

  cy.layout(FCOSE).run();
  cy.fit(null, 40);

  cy.on("tap", "node", (evt) => { focusNode(evt.target); });
  cy.on("tap", (evt) => { if (evt.target === cy) resetFocus(); });

  wireGraphSearch();
  const searchEl = document.getElementById("graph-search");
  if (searchEl) searchEl.value = "";
  applyGraphFilter();

  resetDetail();
  if (statusEl) {
    statusEl.textContent = data.states.length + " states · " +
      (Array.isArray(data.triggers) ? data.triggers.length : 0) + " triggers · " +
      (Array.isArray(data.edges) ? data.edges.length : 0) + " edges";
  }
}

function destroyGraphView() {
  if (cy) { cy.destroy(); }
  cy = null;
  cyHost = null;
}

// --- adjacency focus (mirrors crawl.py focus()) ------------------------------
function focusNode(node) {
  if (!cy) return;
  cy.elements().removeClass("hl").addClass("dim");
  node.closedNeighborhood().removeClass("dim");
  node.connectedEdges().removeClass("dim").addClass("hl");
  node.addClass("hl");
  showDetail(node);
}

function resetFocus() {
  if (!cy) return;
  cy.elements().removeClass("dim hl");
  resetDetail();
}

// --- client-side search/filter (mirrors crawl.py #q/applyFilter) -------------
function wireGraphSearch() {
  if (graphSearchWired) return;
  const searchEl = document.getElementById("graph-search");
  if (!searchEl) return;
  searchEl.addEventListener("input", applyGraphFilter);
  graphSearchWired = true;
}

function applyGraphFilter() {
  if (!cy) return;
  const searchEl = document.getElementById("graph-search");
  const q = (searchEl ? searchEl.value : "").toLowerCase().trim();
  cy.batch(() => {
    cy.nodes().forEach((n) => {
      const d = n.data();
      const ok = !q ||
        (d.label || "").toLowerCase().includes(q) ||
        (d.url || "").toLowerCase().includes(q);
      n.toggleClass("filtered-out", !ok);
    });
  });
}

// --- detail panel (textContent ONLY for every crawled string) ----------------
function resetDetail() {
  const detail = document.getElementById("graph-detail");
  if (!detail) return;
  detail.textContent = "";
  const p = document.createElement("p");
  p.className = "hint muted";
  p.textContent = "Click a node to inspect it.";
  detail.appendChild(p);
  detail.appendChild(buildLegend());
}

function showDetail(node) {
  const detail = document.getElementById("graph-detail");
  if (!detail) return;
  const d = node.data();
  detail.textContent = "";

  const title = document.createElement("div");
  title.className = "detail-title";
  title.textContent = d.label || d.id;   // crawled label — textContent only
  detail.appendChild(title);

  const dl = document.createElement("dl");
  const addRow = (term, value, cls) => {
    if (value === undefined || value === null || value === "") return;
    const dt = document.createElement("dt");
    dt.textContent = term;
    const dd = document.createElement("dd");
    if (cls) dd.className = cls;
    dd.textContent = String(value);          // crawled value — textContent only
    dl.appendChild(dt);
    dl.appendChild(dd);
  };

  if (d.type === "trigger") {
    addRow("kind", "trigger");
    addRow("form", d.formTitle);
    addRow("fields", d.fieldCount);
    addRow("opens at", d.opensAt);
    addRow("selector", d.selector, "detail-selector");
  } else {
    if (d.url) {
      const url = document.createElement("span");
      url.className = "detail-url";
      url.textContent = d.url;               // crawled url — textContent only, not a live href
      detail.appendChild(url);
    }
    addRow("kind", "state");
    addRow("depth", d.depth);
  }
  detail.appendChild(dl);

  const ask = document.createElement("button");
  ask.type = "button";
  ask.className = "detail-ask";
  ask.textContent = "Ask in chat";          // fixed label, not crawled data
  const label = d.label || d.id;
  ask.addEventListener("click", () => {
    if (typeof prefillChat === "function") prefillChat("How do I get to " + label + "?");
  });
  detail.appendChild(ask);

  detail.appendChild(buildLegend());
}

function buildLegend() {
  const legend = document.createElement("div");
  legend.className = "graph-legend";
  const items = [["state", "State (page / SPA view)"], ["trigger", "Trigger (opens a form)"]];
  for (const [kind, text] of items) {
    const lg = document.createElement("div");
    lg.className = "lg";
    const sw = document.createElement("span");
    sw.className = "sw " + kind;
    lg.appendChild(sw);
    const label = document.createElement("span");
    label.textContent = text;
    lg.appendChild(label);
    legend.appendChild(lg);
  }
  return legend;
}
