// pinchtab-webgraph web UI — the Flows view.
//
// Loaded EAGERLY (after explore.js, with flow_canvas.js before it) because it has NO
// vendor deps. It owns the 4th tab: the saved automations for the selected host. A flow is
// a declarative JSON document (goto/do/click/fill/select/check/upload/download/collect/
// wait/set/log + for_each / paginate) run by a step VM against a real browser.
//
// ONE FLOW DOC, THREE SYNCHRONIZED VIEWS — this is the shape of the whole file:
//
//     chat agent  ──┐                    ┌──> the JSON textarea
//                   ├──>  flowDoc  ──────┤
//     the canvas  ──┘   (the ONE truth)  └──> the visual canvas (flow_canvas.js)
//
// Every view can MUTATE the doc; setDoc() then re-renders the OTHER two and re-validates.
// The `source` argument is what stops the loop: a view is never re-rendered from its own
// edit (that would fight the caret in the textarea, and pointlessly repaint the canvas,
// which repaints itself). Validation runs on EVERY change, and an invalid verdict carries
// a path in flow.py's grammar — which is exactly a canvas box's `data-path`, so the bad
// box lights up instead of the user hunting for it.
//
// The whole point of the view is to make the CONTENT-HASH DEDUPE visible: a `download`
// step frame arrives with status "new" or "dupe", so a re-run of a flow reports the files
// it already has instead of re-fetching them. We surface that twice —
//   * LIVE, as a running "N new · M dupe" counter fed by the streaming step frames, and
//   * CUMULATIVELY, as the flow's all-time artifact ledger (name/size/when/sha).
//
// SAFETY DISCIPLINE (kept from explore.js / app.js):
//   * textContent ONLY for every server-sourced string — createElement, never innerHTML.
//   * A flow doc is authored by the user but its *echoed* summary (name, error paths,
//     artifact names, hrefs) is server data — same treatment.
//   * hrefs/paths are shown as PLAIN TEXT, never a live <a href> (clicking one would
//     navigate the browser away from the SPA).
//   * The run is SAFE BY DEFAULT: dry-run is pre-checked, submit/upload grants are NOT,
//     and a grant checkbox is disabled unless the flow itself DECLARES the capability —
//     a write only happens when the flow declares it AND the caller grants it.
//   * setView lives in app.js — flows.js CALLS it, never duplicates socket ownership of
//     the chat/live panes. The RUN socket is owned here.
"use strict";

// module state
let flowsHost = null;         // host the panes were built for
let flowList = [];            // the host's flow summaries
let flowId = null;            // the flow currently open in the editor (null = unsaved/new)
let flowSummary = null;       // the last `flow` bootstrap frame (name/steps/capabilities/inputs)
let flowWs = null;            // the run socket for the open flow (one at a time)
let flowRunning = false;      // a run is in flight on flowWs
let flowValidTimer = null;    // debounce handle for the live validator
let flowValidSeq = 0;         // guards against an out-of-order validate response
let flowValid = false;        // last validation verdict (gates Save)
let flowNewCount = 0;         // live dedupe counters, fed by `download` step frames
let flowDupeCount = 0;
let flowWsBootstrapped = false;  // this socket got its `flow` frame — i.e. the server ACCEPTED it
let flowWsReopenTimer = null;    // pending auto-reopen after an unsolicited drop

// the ONE flow doc + the machinery the three views share
let flowDoc = null;              // the single source of truth (chat, canvas and JSON edit THIS)
let flowDocGen = 0;              // monotonic doc generation — the anti-clobber guard (setDoc)
let opSchema = null;             // GET /api/flows/op_schema — the REAL per-op key tables
let opSchemaPromise = null;      // memoized in-flight fetch
let flowChat = null;             // the flow-mode chat pane (createChatPane, from app.js)

// A file input's value is NOT what the picker holds. The browser only ever exposes
// `C:\fakepath\invoice.pdf` for a chosen file, and the flow VM runs SERVER-side, so what the
// run frame must carry is a path the SERVER can open. The picker therefore STAGES the bytes
// (POST /api/flows/uploads) and we keep the staged path here, keyed by input name — the
// field itself stays a picker and never holds a value the server could use.
let flowFileValues = {};         // input name -> {path, name, size}
let flowFileSeq = {};            // input name -> upload generation (drops a superseded reply)
let flowInputsOk = true;         // every REQUIRED input is satisfied (gates the Run button)

const FLOW_CAPS = ["allow_submit", "allow_upload", "allow_download"];
const FLOW_CAP_IDS = {
  allow_submit: "flow-cap-submit",
  allow_upload: "flow-cap-upload",
  allow_download: "flow-cap-download",
};

// The doc a "+ New flow" seeds the editor with: minimal, valid, and demonstrating the
// dedupe story (a download flow) rather than a bare skeleton.
function flowStarterDoc(host) {
  return {
    name: "new-flow",
    host: host || "",
    description: "What this automation does.",
    capabilities: { allow_download: true },
    steps: [
      { op: "goto", goal: "reports" },
      { op: "for_each", match: { kind: "download" }, as: "item", body: [
        { op: "download", href: "${item.href}", name: "${item.text}" },
      ] },
    ],
  };
}

// --- small DOM helpers (createElement + textContent only) --------------------
function flEl(id) { return document.getElementById(id); }

function flBadge(text, cls) {
  const b = document.createElement("span");
  b.className = "badge" + (cls ? " " + cls : "");
  b.textContent = String(text);            // server value — textContent only
  return b;
}

function flEmpty(text) {
  const p = document.createElement("p");
  p.className = "flow-empty";
  p.textContent = text;                    // fixed UI copy
  return p;
}

function flBytes(n) {
  if (typeof n !== "number" || n < 0) return "";
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / (1024 * 1024)).toFixed(1) + " MB";
}

function flWhen(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d.getTime()) ? String(iso) : d.toLocaleString();
}

// --- shape normalizers -------------------------------------------------------
// `capabilities` may arrive as the flow's own object ({allow_download: true}) or as a
// list of granted names — accept both so the grant checkboxes are right either way.
function flCapSet(caps) {
  const out = new Set();
  if (Array.isArray(caps)) {
    for (const c of caps) if (FLOW_CAPS.includes(c)) out.add(c);
  } else if (caps && typeof caps === "object") {
    for (const k of FLOW_CAPS) if (caps[k]) out.add(k);
  }
  return out;
}

// `inputs` may arrive as the flow's own declaration ({since: {type, required}}), as a
// JSON Schema ({type:"object", properties:{…}, required:[…]}), or as a list of names.
// Normalize all three to [{name, type, required, default, description, enum}].
//
// `type` is normalized to ONE of: string | number | integer | boolean | file — the five the
// run panel knows how to draw a control for. A file is declared as {"type": "file"} by the
// flow doc, but the JSON Schema projection of the same flow (GET …/schema) has no "file"
// type and renders it as {"type": "string", "format": "path"} — both must land on "file",
// or the schema shape would give the user a text box again (the whole bug).
const FLOW_INPUT_TYPES = ["string", "number", "integer", "boolean", "file"];

function flInputType(d) {
  const t = typeof d.type === "string" ? d.type.toLowerCase() : "";
  const fmt = typeof d.format === "string" ? d.format.toLowerCase() : "";
  if (t === "file" || fmt === "path") return "file";
  return FLOW_INPUT_TYPES.includes(t) ? t : "string";
}

function flInputSpecs(inputs) {
  if (!inputs) return [];
  if (Array.isArray(inputs)) {
    return inputs.map((n) => ({ name: String(n), type: "string" }));
  }
  if (typeof inputs !== "object") return [];
  let decls = inputs;
  let required = null;
  if (inputs.type === "object" && inputs.properties &&
      typeof inputs.properties === "object") {
    decls = inputs.properties;                       // a JSON Schema
    required = Array.isArray(inputs.required) ? inputs.required : [];
  }
  const out = [];
  for (const name of Object.keys(decls)) {
    const d = decls[name] && typeof decls[name] === "object" ? decls[name] : {};
    out.push({
      name,
      type: flInputType(d),
      required: required ? required.includes(name) : !!d.required,
      "default": d["default"],
      description: d.description || "",
      "enum": Array.isArray(d["enum"]) && d["enum"].length ? d["enum"] : null,
    });
  }
  return out;
}

// A stable, predictable DOM id per input, for the e2e and for the <label for>. Input names
// come from the flow doc (an agent may have written it), so anything that is not id-safe is
// folded to "_" — for every ordinary name this is just `flow-input-<name>`.
function flInputId(name) {
  return "flow-input-" + String(name).replace(/[^A-Za-z0-9_-]/g, "_");
}

// --- the op schema: the ONLY description of what a step may contain ----------
// Fetched once and memoized. Every per-op edit form on the canvas is derived from it, so
// there is no second copy of the DSL in JS to drift when an op is added server-side.
function ensureOpSchema() {
  if (opSchema) return Promise.resolve(opSchema);
  if (!opSchemaPromise) {
    opSchemaPromise = fetch("/api/flows/op_schema")
      .then((res) => res.json())
      .then((data) => {
        opSchema = (data && typeof data === "object") ? data : null;
        return opSchema;
      })
      .catch(() => {
        opSchemaPromise = null;    // a memoized failure would poison every later retry
        return null;
      });
  }
  return opSchemaPromise;
}

// --- setDoc: the three-way sync, in one place --------------------------------
// `source` is the view the edit CAME FROM, and it is never re-rendered from its own edit:
//   "json"   — rewriting the textarea mid-keystroke would fight the caret.
//   "canvas" — the canvas repaints itself from the new doc (see flow_canvas.js's
//              canvasMutate); a second render here would be wasted work and a re-entrancy
//              hazard.
//   "chat" / "load" — both other views repaint.
//
// `gen` is the ANTI-CLOBBER GUARD, and it is why this is not a timing bet. Every act that
// REPLACES what the editor is working on (openFlow, newFlow, resetFlowEditor) bumps
// flowDocGen. An async writer — an openFlow() still awaiting its fetch — captures the
// generation it started under and hands it back here; if the generation has moved on, its
// document is a straggler from a superseded state and is DROPPED rather than painted over
// whatever the user is now looking at. Callers with nothing to be stale about (the JSON
// textarea, the canvas, a live chat draft, a human's Restore click — all synchronous with
// the current doc) pass no `gen` and are never gated.
function setDoc(doc, source, gen) {
  if (gen !== undefined && gen !== flowDocGen) return;   // superseded writer — drop it
  flowDoc = doc;
  const editor = flEl("flow-editor-text");
  const canvas = flEl("flow-canvas");
  if (editor && source !== "json") {
    editor.value = JSON.stringify(doc, null, 2);
  }
  if (canvas && source !== "canvas" && typeof renderCanvasFromDoc === "function") {
    renderCanvasFromDoc(canvas, doc, opSchema, {
      onMutate: (next) => setDoc(next, "canvas"),
    });
  }
  scheduleValidate();              // every change is validated — no exceptions
}

// --- lifecycle: open / destroy (app.js owns the host-switch calls) -----------
function openFlowsView(host) {
  if (host && host === flowsHost) return;    // same host already rendered — no-op
  flowsHost = host || null;
  resetFlowEditor();
  const status = flEl("flows-status");
  const list = flEl("flow-list");
  if (list) list.textContent = "";
  if (!host) {
    if (status) status.textContent = "Pick a host first.";
    if (flowChat) flowChat.destroy();
    return;
  }
  if (status) status.textContent = "";
  ensureOpSchema();                // the canvas needs it before it can offer an op picker
  const chat = ensureFlowChat();
  if (chat) chat.selectHost(host);
  loadFlowList(host);
}

// The Flows tab's chat pane: the SAME factory the Workspace tab uses (app.js), mounted a
// second time with flow-namespaced ids and `mode=flow` — so it lists only flow chats, talks
// to the flow-authoring agent, ships the LIVE doc as `draft` on every message, and feeds
// LIVE `flow_draft` frames into setDoc() (replayed ones only render — see applyFlowDraft).
function ensureFlowChat() {
  if (flowChat) return flowChat;
  if (typeof createChatPane !== "function") return null;   // app.js absent — degrade quietly
  flowChat = createChatPane({
    ids: {
      log: "flow-chat-log",
      status: "flow-chat-status",
      form: "flow-chat-form",
      input: "flow-chat-input",
      sessions: "flow-chat-sessions",
      sessionList: "flow-chat-session-list",
      sessionNew: "flow-chat-session-new",
    },
    mode: "flow",
    readyText: "ready — describe the automation you want.",
    getDraft: () => flowDoc,       // ALWAYS the live doc, never the agent's stale copy
    onFlowDraft: applyFlowDraft,
  });
  return flowChat;
}

// A `flow_draft` frame. TWO frames wear this type and they are NOT the same thing:
//
//   LIVE  (meta.live)  — the agent rewrote the doc, on this turn, in answer to what the
//                        human just asked. It is applied to the editor, EVEN WHEN
//                        status === "invalid". That is the point of the three-way view: a
//                        withheld bad draft teaches the user nothing, whereas an applied
//                        one shows them exactly WHAT the agent wrote and WHERE it's wrong
//                        — the canvas highlights the offending box and the banner names
//                        the error. They can then fix it by hand, or say so in the chat.
//
//   REPLAYED           — a stored draft, re-read out of a transcript when a chat session
//                        is opened. It is HISTORY: a doc from twenty turns ago, and quite
//                        possibly a doc for a different flow than the one now open. It is
//                        NEVER applied. Replaying a conversation must not mutate the
//                        document you are working on, and a replay landing a beat after a
//                        click on a saved flow would otherwise clobber it silently. It
//                        gets its chip plus a "Restore this draft" button — the ONLY way a
//                        historical doc reaches the editor is a human pressing that.
function applyFlowDraft(frame, pane, meta) {
  if (!frame) return;
  const live = !!(meta && meta.live);
  const doc = (frame.doc && typeof frame.doc === "object") ? frame.doc : null;
  if (live && doc) applyDraftDoc(frame, doc);
  if (pane) pane.appendNode(buildDraftChip(frame, live ? null : doc));
}

// Put a draft doc (live, or a restored historical one) into the three views, and paint the
// server's verdict NOW rather than waiting out the 300ms debounce — the canvas box lights
// up in the same frame the draft lands in.
function applyDraftDoc(frame, doc) {
  setDoc(doc, "chat");
  if (frame.status === "invalid") {
    const detail = (frame.path ? String(frame.path) + ": " : "") +
      (frame.error || "invalid flow");
    showValidation("bad", detail);
    const canvas = flEl("flow-canvas");
    if (canvas && typeof highlightPath === "function") highlightPath(canvas, frame.path);
  }
}

// One compact chip per draft, so the conversation reads as a HISTORY OF EDITS rather than
// a wall of prose. Every string on it (note / name / path / error) is model-authored —
// createElement + textContent, never innerHTML.
//
// `restoreDoc` (replayed drafts only) adds the Restore button that applies that historical
// document. A real <button>, so it is focusable and Enter/Space-activatable.
function buildDraftChip(frame, restoreDoc) {
  const invalid = frame.status === "invalid";
  const chip = document.createElement("div");
  chip.className = "flow-draft-chip" + (invalid ? " bad" : " ok");

  const dot = document.createElement("span");
  dot.className = "flow-draft-dot";
  dot.textContent = invalid ? "!" : "✓";        // fixed glyph, not model data
  chip.appendChild(dot);

  const note = document.createElement("span");
  note.className = "flow-draft-note";
  note.textContent = frame.note || frame.name || "flow updated";   // model string
  chip.appendChild(note);

  if (restoreDoc) {
    const btn = document.createElement("button");
    btn.type = "button";                        // never a form submit
    btn.className = "flow-draft-restore";
    btn.textContent = "Restore this draft";     // fixed UI copy
    btn.addEventListener("click", () => {
      applyDraftDoc(frame, restoreDoc);
      setFlowSaveMsg("Draft restored into the editor — Save to keep it.");
    });
    chip.appendChild(btn);
  }

  if (invalid) {
    const err = document.createElement("span");
    err.className = "flow-draft-err";
    err.textContent = (frame.path ? frame.path + ": " : "") + (frame.error || "invalid");
    chip.appendChild(err);
  }
  return chip;
}

function setFlowSaveMsg(text) {
  const msg = flEl("flow-save-msg");
  if (msg) msg.textContent = text;              // fixed UI copy
}

// app.js calls this on EVERY host switch (before opening the new one) so a stale host's
// flows — and its run socket — never bleed into the next. Mirrors destroyExploreView().
function destroyFlowsView() {
  closeFlowSocket();
  if (flowChat) flowChat.destroy();     // the flow chat is per-host too — never bleed it
  flowsHost = null;
  flowList = [];
  resetFlowEditor();
  const list = flEl("flow-list");
  if (list) list.textContent = "";
  const status = flEl("flows-status");
  if (status) status.textContent = "";
}

function closeFlowSocket() {
  if (flowWsReopenTimer) { clearTimeout(flowWsReopenTimer); flowWsReopenTimer = null; }
  if (flowWs) {
    // null FIRST: `ws.onclose` reads `flowWs !== ws` to tell a deliberate close (this one)
    // from an unsolicited drop (which auto-reopens). Order is load-bearing.
    const ws = flowWs;
    flowWs = null;
    try { ws.close(); } catch (e) { /* ignore */ }
  }
  flowWsBootstrapped = false;
  flowRunning = false;
  setFlowRunning(false);
}

// Clear the editor + every dependent pane back to "nothing open".
function resetFlowEditor() {
  flowId = null;
  flowSummary = null;
  flowValid = false;
  flowDoc = null;                      // the three views share ONE doc — drop it once
  flowDocGen += 1;                     // …and invalidate every in-flight writer of the old one
  if (flowValidTimer) { clearTimeout(flowValidTimer); flowValidTimer = null; }
  const editor = flEl("flow-editor-text");
  if (editor) editor.value = "";
  const canvas = flEl("flow-canvas");
  if (canvas && typeof renderCanvasFromDoc === "function") {
    renderCanvasFromDoc(canvas, null, opSchema, {});   // paints the "no flow open" note
  }
  for (const id of ["flow-validation", "flow-log", "flow-inputs", "flow-runs",
                    "flow-artifacts", "flow-dedupe"]) {
    const node = flEl(id);
    if (node) node.textContent = "";
  }
  const summary = flEl("flow-artifacts-summary");
  if (summary) summary.textContent = "";
  const msg = flEl("flow-save-msg");
  if (msg) msg.textContent = "";
  const runPanel = flEl("flow-run-panel");
  if (runPanel) runPanel.hidden = true;
  const results = flEl("flow-results");
  if (results) results.hidden = true;
  const del = flEl("flow-delete");
  if (del) del.hidden = true;
  const save = flEl("flow-save");
  if (save) { save.textContent = "Save flow"; save.disabled = true; }
}

// --- left pane: the host's flow list -----------------------------------------
async function loadFlowList(host) {
  const list = flEl("flow-list");
  if (!list) return;
  let data;
  try {
    const res = await fetch("/api/hosts/" + encodeURIComponent(host) + "/flows");
    data = await res.json();
  } catch (err) {
    if (flowsHost === host) {
      list.textContent = "";
      list.appendChild(flEmpty("Could not load flows."));
    }
    return;
  }
  if (flowsHost !== host) return;             // a newer host switch took over
  flowList = (data && Array.isArray(data.flows)) ? data.flows : [];
  renderFlowList();
}

function renderFlowList() {
  const list = flEl("flow-list");
  if (!list) return;
  list.textContent = "";
  if (flowList.length === 0) {
    list.appendChild(flEmpty("No flows yet — “+ New flow” writes the first one."));
    return;
  }
  for (const f of flowList) list.appendChild(buildFlowRow(f));
}

// The row is a real <button> inside the <li>: focusable, Enter/Space-activatable and
// exposed to the accessibility tree as a control — a bare <li> with a click handler is
// none of those. `aria-current` announces which flow is the open one.
function buildFlowRow(f) {
  const li = document.createElement("li");

  const btn = document.createElement("button");
  btn.type = "button";                                // never a form submit
  const selected = f.id === flowId;
  btn.className = "flow-row" + (selected ? " selected" : "");
  if (selected) btn.setAttribute("aria-current", "true");

  const label = document.createElement("span");
  label.className = "flow-row-name";
  label.textContent = f.name || "(unnamed flow)";     // server string — textContent only
  btn.appendChild(label);

  const meta = document.createElement("span");
  meta.className = "flow-row-meta";
  const steps = Array.isArray(f.steps) ? f.steps.length : f.steps;
  if (typeof steps === "number") meta.appendChild(flBadge(steps + " steps"));
  if (typeof f.run_count === "number") {
    const runs = document.createElement("span");
    runs.className = "count";
    runs.textContent = f.run_count + (f.run_count === 1 ? " run" : " runs");
    meta.appendChild(runs);
  }
  btn.appendChild(meta);

  btn.addEventListener("click", () => openFlow(f.id));
  li.appendChild(btn);
  return li;
}

// --- opening a flow: editor + run socket + history + ledger -------------------
async function openFlow(id) {
  const host = flowsHost;
  if (!host || !id) return;
  closeFlowSocket();
  resetFlowEditor();                           // bumps flowDocGen: THIS is now the doc
  const gen = flowDocGen;                      // …and anything that lands under an older
  flowId = id;                                 //    generation has been superseded
  renderFlowList();                            // reflect the selection

  let record;
  try {
    const res = await fetch(flowUrl(host, id));
    record = await res.json();
  } catch (err) {
    setFlowsStatus("Could not load that flow.");
    return;
  }
  await ensureOpSchema();                      // the canvas can't build forms without it
  if (flowsHost !== host || flowId !== id || flowDocGen !== gen) return;

  // ONE call populates BOTH the textarea and the canvas — the two views can no longer be
  // loaded out of step with each other, because there is only one way in.
  if (record && record.doc) setDoc(record.doc, "load", gen);

  const del = flEl("flow-delete");
  if (del) del.hidden = false;
  const save = flEl("flow-save");
  if (save) save.textContent = "Update flow";
  const results = flEl("flow-results");
  if (results) results.hidden = false;

  validateNow();                               // paint the banner for the loaded doc
  openRunSocket(host, id);                     // its `flow` frame builds the run form
  loadRuns(host, id);
  loadArtifacts(host, id);
}

function flowUrl(host, id) {
  let u = "/api/hosts/" + encodeURIComponent(host) + "/flows";
  if (id) u += "/" + encodeURIComponent(id);
  return u;
}

function setFlowsStatus(text) {
  const status = flEl("flows-status");
  if (status) status.textContent = text;
}

async function newFlow() {
  if (!flowsHost) { setFlowsStatus("Pick a host first."); return; }
  const host = flowsHost;
  closeFlowSocket();
  resetFlowEditor();                     // bumps flowDocGen — see setDoc()
  const gen = flowDocGen;
  renderFlowList();
  // A new flow also gets a FRESH chat: authoring it is a conversation, and starting one
  // inside the transcript of the last flow would have the agent editing the wrong story.
  if (flowChat) flowChat.newChat();
  await ensureOpSchema();
  if (flowsHost !== host || flowDocGen !== gen) return;
  setDoc(flowStarterDoc(host), "load", gen);
  const editor = flEl("flow-editor-text");
  if (editor) editor.focus();
  validateNow();
}

// --- live validation ---------------------------------------------------------
// Two layers, both offline: JSON.parse locally (a syntax error never leaves the browser),
// then POST /api/flows/validate for the STRUCTURAL verdict. That endpoint answers 200 with
// the verdict in the BODY — an invalid flow is not an HTTP error — so the status field is
// what we branch on, never the HTTP code.
function scheduleValidate() {
  if (flowValidTimer) clearTimeout(flowValidTimer);
  flowValidTimer = setTimeout(validateNow, 300);
}

function showValidation(cls, text) {
  const box = flEl("flow-validation");
  if (!box) return;
  box.textContent = "";
  box.className = "flow-validation " + cls;
  const span = document.createElement("span");
  span.textContent = text;                     // server error text — textContent only
  box.appendChild(span);
}

function setSaveEnabled(on) {
  const save = flEl("flow-save");
  if (save) save.disabled = !on;
}

async function validateNow() {
  if (flowValidTimer) { clearTimeout(flowValidTimer); flowValidTimer = null; }
  const editor = flEl("flow-editor-text");
  const text = editor ? editor.value : "";
  const seq = ++flowValidSeq;
  flowValid = false;
  setSaveEnabled(false);

  if (!text.trim()) {
    showValidation("", "");
    return;
  }
  let doc;
  try {
    doc = JSON.parse(text);                    // local syntax pass — no round trip
  } catch (err) {
    showValidation("bad", "not valid JSON — " + (err && err.message ? err.message : "parse error"));
    return;
  }

  let data;
  try {
    const res = await fetch("/api/flows/validate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(doc),
    });
    data = await res.json();
  } catch (err) {
    if (seq !== flowValidSeq) return;
    showValidation("bad", "could not reach the validator.");
    return;
  }
  if (seq !== flowValidSeq) return;            // a newer keystroke already won

  const canvas = flEl("flow-canvas");
  if (data && data.status === "ok") {
    flowValid = true;
    setSaveEnabled(true);                      // a WARNING IS NOT A BLOCKER — Save stays on
    const warns = flowWarnings(data);
    showValidation(warns.length ? "warn" : "ok", validSummaryText(data));
    if (canvas && typeof highlightPath === "function") highlightPath(canvas, null);  // clear
    if (canvas && typeof markWarnings === "function") markWarnings(canvas, warns);
    return;
  }
  const path = data && data.path ? String(data.path) : "";
  const detail = (data && (data.error || data.detail)) || "invalid flow";
  showValidation("bad", (path ? path + ": " : "") + detail);
  // flow.validate() reports its path in the SAME grammar the canvas uses for `data-path`,
  // so the error is ADDRESSABLE: light up the actual BOX instead of printing a path and
  // leaving the user to count brackets.
  if (canvas && typeof highlightPath === "function") highlightPath(canvas, path);
  if (canvas && typeof markWarnings === "function") markWarnings(canvas, []);  // clear amber
}

// RESOLVABILITY warnings from the validate response. A structurally VALID document whose
// `goal` matches nothing in the host's crawled graph: the flow saves, runs, and only then
// aborts — so it must be visible at authoring time. Advisory, hence a third banner state
// (amber) rather than "invalid", and Save is untouched.
function flowWarnings(data) {
  const list = (data && Array.isArray(data.warnings)) ? data.warnings : [];
  return list.filter((w) => w && typeof w === "object");
}

// "valid — 4 steps · download · inputs: since" — the one-line contract of the document,
// plus "⚠ 1 step may not resolve" when the graph disagrees with a goal.
function validSummaryText(data) {
  const steps = Array.isArray(data.steps) ? data.steps.length : data.steps;
  const parts = ["valid"];
  if (typeof steps === "number") parts.push(steps + (steps === 1 ? " step" : " steps"));
  const caps = Array.from(flCapSet(data.capabilities)).map((c) => c.replace("allow_", ""));
  parts.push(caps.length ? "capabilities: " + caps.join(", ") : "no capabilities");
  const names = flInputSpecs(data.inputs).map((i) => i.name);
  parts.push(names.length ? "inputs: " + names.join(", ") : "no inputs");
  const warns = flowWarnings(data);
  if (warns.length) {
    parts.push("⚠ " + warns.length + (warns.length === 1 ? " step may not resolve"
                                                         : " steps may not resolve"));
  }
  return parts[0] + " — " + parts.slice(1).join(" · ");
}

// --- save / update / delete --------------------------------------------------
async function saveFlow() {
  const host = flowsHost;
  const editor = flEl("flow-editor-text");
  const msg = flEl("flow-save-msg");
  if (!host || !editor) return;
  let doc;
  try {
    doc = JSON.parse(editor.value);
  } catch (err) {
    if (msg) msg.textContent = "fix the JSON first.";
    return;
  }
  if (msg) msg.textContent = "saving…";
  const editing = !!flowId;
  let data;
  try {
    const res = await fetch(flowUrl(host, flowId), {
      method: editing ? "PUT" : "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(doc),
    });
    data = await res.json();
  } catch (err) {
    if (msg) msg.textContent = "save request failed.";
    return;
  }
  if (flowsHost !== host) return;

  if (!data || data.status !== "ok") {
    // a structural miss comes back 200 with the verdict in the body (see validateNow).
    if (data && data.status === "too_many_flows") {
      if (msg) msg.textContent = "too many flows (max " + (data.max || "?") + ").";
    } else if (msg) {
      msg.textContent = "not saved: " + ((data && data.path) ? data.path + ": " : "") +
        ((data && (data.error || data.status)) || "invalid flow");
    }
    return;
  }
  // Saved — but if a goal doesn't resolve, say so HERE too: the save banner is the last
  // thing the user reads before they walk away thinking the flow is good to run.
  const saveWarns = flowWarnings(data);
  const warnNote = saveWarns.length
    ? " ⚠ " + saveWarns.length + (saveWarns.length === 1 ? " step may not resolve"
                                                         : " steps may not resolve")
    : "";
  if (msg) msg.textContent = (editing ? "updated." : "saved.") + warnNote;
  await loadFlowList(host);
  // A fresh POST mints an id server-side; find it by name so the new flow opens directly.
  if (!editing) {
    const created = flowList.find((f) => f.name === doc.name);
    if (created) { await openFlow(created.id); return; }
  }
  if (flowId) {
    renderFlowList();
    closeFlowSocket();
    openRunSocket(host, flowId);               // refresh the bootstrap summary
    loadRuns(host, flowId);
  }
}

async function deleteFlow() {
  const host = flowsHost;
  const id = flowId;
  const del = flEl("flow-delete");
  if (!host || !id || !del) return;
  // first click arms, a second confirms — the same pattern the chat chips use.
  if (!del.classList.contains("confirm")) {
    del.classList.add("confirm");
    del.textContent = "delete?";
    setTimeout(() => {
      del.classList.remove("confirm");
      del.textContent = "Delete";
    }, 2000);
    return;
  }
  del.classList.remove("confirm");
  del.textContent = "Delete";
  try {
    await fetch(flowUrl(host, id), { method: "DELETE" });
  } catch (err) { /* the reload below reflects whatever stuck */ }
  if (flowsHost !== host) return;
  closeFlowSocket();
  resetFlowEditor();
  await loadFlowList(host);
}

// --- the run socket ----------------------------------------------------------
// Opened when a saved flow is opened and kept open: a kickoff is REPEATABLE (a rejected
// one does not close the socket), so the same socket serves run after run.
function openRunSocket(host, id) {
  const ws = new WebSocket(wsUrl("/ws/flows/run?host=" + encodeURIComponent(host) +
    "&flow_id=" + encodeURIComponent(id)));
  flowWs = ws;

  ws.onclose = () => {
    if (flowWs !== ws) return;      // deliberate close (closeFlowSocket nulled flowWs first)
    flowWs = null;
    if (flowRunning) {
      // The server treats a client disconnect as an implicit CANCEL, so the run really did
      // stop. Say so: without this the log just trails off after the last step and the run
      // reads as if it finished (or hung) — the one thing the user must not be misled about.
      flowRunning = false;
      setFlowRunning(false);
      logAddLine("✗ run socket dropped — the run was cancelled", "error");
      setFlowsStatus("the run socket dropped; the run was cancelled. Reconnecting…");
    }
    // An UNSOLICITED drop (seen in the wild as a 1006 after a completed run). Without this
    // the socket is never rebuilt, so every later Run click dead-ends on "run socket is not
    // connected." with no recovery but switching flows — breaking the invariant this socket
    // is built on: a kickoff is REPEATABLE, the same socket serves run after run.
    // Only reopen a socket the server had ACCEPTED (`flow` frame seen): a rejected one
    // (flow_not_found / disabled / bad host) closes before bootstrap, and reopening THAT
    // would be a hot reconnect loop against a server that will just refuse again.
    if (!flowWsBootstrapped || !flowsHost || !flowId) return;
    flowWsBootstrapped = false;
    const host = flowsHost;
    const id = flowId;
    flowWsReopenTimer = setTimeout(() => {           // cooldown: never a tight respawn loop
      flowWsReopenTimer = null;
      if (flowsHost === host && flowId === id && !flowWs) openRunSocket(host, id);
    }, 1000);
  };
  ws.onerror = () => {
    if (flowWs === ws) setFlowsStatus("run socket error.");
  };
  ws.onmessage = (ev) => {
    if (flowWs !== ws) return;
    let data;
    try { data = JSON.parse(ev.data); } catch (e) { return; }
    switch (data.type) {
      case "flow":
        // bootstrap: the run form is built from THIS — no second fetch. Reaching here is
        // also the server's ACCEPT, which is what licenses an auto-reopen in `onclose`.
        flowWsBootstrapped = true;
        flowSummary = data;
        renderRunPanel(data);
        break;
      case "status":
        logAddLine("· run " + (data.run_id || "") +
          (data.dry_run ? " (dry run)" : "") + " — " + (data.state || "starting"), "info");
        break;
      case "step":
        logAddStep(data);
        countDedupe(data);
        break;
      case "log":
        logAddLine(String(data.line != null ? data.line : ""), "info");
        break;
      case "result":
        flowRunning = false;
        setFlowRunning(false);
        logAddResult(data);
        applyStats(data.stats);
        if (flowsHost && flowId) {
          loadRuns(flowsHost, flowId);
          loadArtifacts(flowsHost, flowId);    // the ledger grew — refresh it
          // …and so did the run count in the left pane, which is otherwise only fetched
          // when the view opens — it would sit at "0 runs" forever. Re-render keeps the
          // selection (renderFlowList reads flowId) and touches neither editor nor log.
          loadFlowList(flowsHost);
        }
        break;
      case "error":
        flowRunning = false;
        setFlowRunning(false);
        logAddLine("error: " + (data.status || "unknown") +
          (data.detail ? " — " + data.detail : ""), "error");
        // Belt and braces: the gate should have caught this before the click, but if the
        // server still rejects the inputs, say so AT the inputs — not only in the log.
        if (data.status === "invalid_input") showInputError(data.detail);
        break;
      default:
        break;
    }
  };
}

// --- run panel: inputs + the grant checkboxes --------------------------------
function renderRunPanel(summary) {
  const panel = flEl("flow-run-panel");
  if (panel) panel.hidden = false;

  const box = flEl("flow-inputs");
  if (box) {
    box.textContent = "";
    // A staged path belongs to the flow the panel was built for — never carry one across.
    flowFileValues = {};
    flowFileSeq = {};
    const specs = flInputSpecs(summary ? summary.inputs : null);
    if (specs.length === 0) {
      box.appendChild(flEmpty("This flow takes no inputs."));
    } else {
      for (const spec of specs) box.appendChild(buildInputField(spec));
    }
  }
  clearInputError();
  updateRunGate();

  // A grant checkbox is DISABLED unless the flow DECLARES that capability: a write only
  // happens when the flow declares it AND the caller grants it. Download is granted by
  // default (it is the read-only, dedupe-friendly one); submit + upload never are.
  const declared = flCapSet(summary ? summary.capabilities : null);
  for (const cap of FLOW_CAPS) {
    const cb = flEl(FLOW_CAP_IDS[cap]);
    if (!cb) continue;
    const allowed = declared.has(cap);
    cb.disabled = !allowed;
    cb.checked = allowed && cap === "allow_download";
    const label = cb.closest("label");
    if (label) {
      label.classList.toggle("undeclared", !allowed);
      label.title = allowed ? "" : "the flow does not declare this capability";
    }
  }
  const dry = flEl("flow-dry-run");
  if (dry) dry.checked = true;                 // safety: re-armed for every opened flow
}

// ONE control per declared type — a text box for everything was the bug: it asked the user
// to TYPE an absolute path for a file input, which no browser can even tell them.
//
//   string            <input type="text">   (a <select> instead if an enum is declared)
//   number / integer  <input type="number"> (step=1 for integer)
//   boolean           <input type="checkbox">
//   file              <input type="file">   — a real picker; the bytes are staged on choose
//   anything + enum   <select> of the allowed values
function buildInputField(spec) {
  const label = document.createElement("label");
  label.className = "flow-input";
  label.dataset.inputFor = spec.name;

  const name = document.createElement("span");
  name.className = "flow-input-name";
  name.textContent = spec.name;                // server string — textContent only
  if (spec.required) {
    const star = document.createElement("span");
    star.className = "flow-input-req";
    star.textContent = " *";
    star.title = "required";
    name.appendChild(star);
  }
  label.appendChild(name);

  let field;
  let status = null;
  if (spec["enum"]) {
    // An enum pins the value whatever the declared type is — a constrained choice is a
    // <select>, never a free-text box that can only be typed wrong.
    field = document.createElement("select");
    if (!spec.required && spec["default"] == null) {
      const blank = document.createElement("option");
      blank.value = "";
      blank.textContent = "—";                 // optional: allow "send nothing"
      field.appendChild(blank);
    }
    for (const opt of spec["enum"]) {
      const o = document.createElement("option");
      o.value = String(opt);
      o.textContent = String(opt);             // server string — textContent only
      field.appendChild(o);
    }
    if (spec["default"] != null) field.value = String(spec["default"]);
  } else if (spec.type === "boolean") {
    field = document.createElement("input");
    field.type = "checkbox";
    if (spec["default"] === true) field.checked = true;
  } else if (spec.type === "file") {
    field = document.createElement("input");
    field.type = "file";
    const accept = acceptFromDescription(spec.description);
    if (accept) field.accept = accept;
    status = document.createElement("span");
    status.className = "flow-input-file-status";
    status.id = flInputId(spec.name) + "-status";
    field.addEventListener("change", () => {
      stageFile(spec.name, field.files && field.files[0], status);
    });
  } else if (spec.type === "number" || spec.type === "integer") {
    field = document.createElement("input");
    field.type = "number";
    if (spec.type === "integer") field.step = "1";
    if (spec["default"] != null) field.value = String(spec["default"]);
  } else {
    field = document.createElement("input");
    field.type = "text";
    if (spec["default"] != null) field.value = String(spec["default"]);
  }

  field.id = flInputId(spec.name);
  field.dataset.inputName = spec.name;
  field.dataset.inputType = spec.type;
  if (spec.required) field.dataset.inputRequired = "1";
  // Any edit can satisfy (or un-satisfy) a required input — re-gate Run on every one of
  // them. A file re-gates from stageFile instead: choosing it is not yet having staged it.
  if (spec.type !== "file") {
    field.addEventListener("input", () => { clearInputError(); updateRunGate(); });
    field.addEventListener("change", () => { clearInputError(); updateRunGate(); });
  }
  label.appendChild(field);
  if (status) label.appendChild(status);

  // The description is the only place the flow says what it wants (the upload flow names
  // its accepted extensions there) — as HELP TEXT, not a placeholder the value hides.
  if (spec.description) {
    const help = document.createElement("span");
    help.className = "flow-input-help";
    help.textContent = spec.description;       // flow-doc string — textContent only
    label.appendChild(help);
  }
  return label;
}

// The contract has no `accept` field and we do NOT invent one. But a file input's
// description routinely NAMES the extensions it takes ("a .pdf, .docx or .txt"), and
// narrowing the picker to those is a pure convenience — so read them out if they are there,
// and pass nothing at all if they are not.
function acceptFromDescription(desc) {
  if (!desc) return "";
  const found = String(desc).match(/\.[A-Za-z0-9]{1,8}\b/g);
  if (!found) return "";
  const exts = [];
  for (const raw of found) {
    const ext = raw.toLowerCase();
    if (!exts.includes(ext)) exts.push(ext);
  }
  return exts.length ? exts.join(",") : "";
}

// --- file staging ------------------------------------------------------------
// The browser cannot hand the server a local path (a file input only ever exposes
// `C:\fakepath\…`), so the bytes go up the moment the file is chosen and what we keep is
// the STAGED SERVER PATH the endpoint hands back. That path — not the filename — is what
// the run frame's `inputs` carries. Until it arrives the input is UNSATISFIED, which is
// what keeps Run disabled while an upload is still in flight.
function stageFile(name, file, status) {
  const seq = (flowFileSeq[name] || 0) + 1;
  flowFileSeq[name] = seq;
  delete flowFileValues[name];                 // choosing a new file drops the old path
  clearInputError();
  setInputInvalid(name, false);

  if (!file) {
    setFileStatus(status, "", "");
    updateRunGate();
    return;
  }
  setFileStatus(status, "staging…", "pending");
  updateRunGate();                             // …and Run stays disabled meanwhile

  // A File IS a valid fetch body — raw bytes, no FormData, matching the endpoint.
  fetch("/api/flows/uploads?name=" + encodeURIComponent(file.name), {
    method: "POST",
    body: file,
  })
    .then((res) => res.json().then(
      (data) => ({ ok: res.ok, data: data && typeof data === "object" ? data : {} }),
      () => ({ ok: false, data: {} }),         // a non-JSON body (a proxy's 502 page)
    ))
    .then(({ ok, data }) => {
      if (flowFileSeq[name] !== seq) return;   // superseded by a later pick — drop it
      if (ok && data.status === "ok" && data.path) {
        flowFileValues[name] = {
          path: String(data.path),
          // `data.name` is the ORIGINAL name the user picked — what we show. The server may
          // have sanitised the on-disk basename (`data.stored_name`, e.g. a space -> `_`);
          // that is cosmetic and internal, so it never surfaces in the status line.
          name: String(data.name || file.name),
          size: typeof data.size === "number" ? data.size : file.size,
        };
        const v = flowFileValues[name];
        const size = flBytes(v.size);
        setFileStatus(status, v.name + (size ? " · " + size : ""), "ok");
      } else {
        setFileStatus(status, uploadErrorText(data), "error");
        setInputInvalid(name, true);           // unsatisfied — Run stays disabled
      }
      updateRunGate();
    })
    .catch(() => {
      if (flowFileSeq[name] !== seq) return;
      setFileStatus(status, "upload failed — the server could not be reached", "error");
      setInputInvalid(name, true);
      updateRunGate();
    });
}

function uploadErrorText(data) {
  const st = String(data.status || "upload_failed");
  if (st === "too_large") {
    const max = typeof data.max_bytes === "number" ? flBytes(data.max_bytes) : "";
    return "too large" + (max ? " — the limit is " + max : "");
  }
  if (st === "invalid_name") return "the file name was rejected";
  return st + (data.detail ? " — " + String(data.detail) : "");
}

function setFileStatus(status, text, cls) {
  if (!status) return;
  status.className = "flow-input-file-status" + (cls ? " flow-file-" + cls : "");
  status.textContent = text;                   // server string — textContent only
}

// Collect the run inputs. An untouched optional text field is OMITTED rather than sent as
// "" — the server would coerce an empty string into a real value and shadow the default.
function collectInputs() {
  const box = flEl("flow-inputs");
  const values = {};
  if (!box) return values;
  for (const field of box.querySelectorAll("[data-input-name]")) {
    const name = field.dataset.inputName;
    const type = field.dataset.inputType;
    if (type === "boolean") {
      values[name] = !!field.checked;
      continue;
    }
    if (type === "file") {
      // the STAGED SERVER PATH, never field.value (which is `C:\fakepath\…`).
      const staged = flowFileValues[name];
      if (staged && staged.path) values[name] = staged.path;
      continue;
    }
    const raw = field.value;
    if (raw === "" || raw == null) continue;
    if (type === "number" || type === "integer") {
      const n = Number(raw);
      if (!isNaN(n)) values[name] = n;
    } else {
      values[name] = raw;
    }
  }
  return values;
}

// --- required-input gating ---------------------------------------------------
// Failing BEFORE the click is the point: today the user can hit Run with a required input
// empty and only then learn, from the server, that it was `invalid_input`. A required input
// is satisfied when it holds a value the run frame can actually carry —
//   * boolean — ALWAYS (unchecked is a legitimate `false`, not a missing value),
//   * file    — only once its upload came back with a staged path,
//   * others  — a non-empty field.
function inputSatisfied(field) {
  const type = field.dataset.inputType;
  if (type === "boolean") return true;
  if (type === "file") {
    const staged = flowFileValues[field.dataset.inputName];
    return !!(staged && staged.path);
  }
  return String(field.value != null ? field.value : "").trim() !== "";
}

function updateRunGate() {
  const box = flEl("flow-inputs");
  let ok = true;
  if (box) {
    for (const field of box.querySelectorAll("[data-input-required]")) {
      const good = inputSatisfied(field);
      if (!good) ok = false;
      // Mark the offending field — a failed upload lands here too (no staged path is, for
      // the gate, exactly the same as no file), and its status line carries the reason.
      const label = field.closest(".flow-input");
      if (label) label.classList.toggle("invalid", !good);
    }
  }
  flowInputsOk = ok;
  syncRunButton();
}

// Run is disabled while a run is in flight OR while a required input is unsatisfied — two
// independent reasons, one button, so both live here rather than fighting over `disabled`.
function syncRunButton() {
  const run = flEl("flow-run");
  if (!run) return;
  run.disabled = flowRunning || !flowInputsOk;
  run.title = (!flowRunning && !flowInputsOk)
    ? "fill in every required input first"
    : "";
}

function setInputInvalid(name, bad) {
  const box = flEl("flow-inputs");
  if (!box) return;
  for (const label of box.querySelectorAll(".flow-input")) {
    if (label.dataset.inputFor === name) label.classList.toggle("invalid", !!bad);
  }
}

// Belt and braces: the server may still answer `invalid_input` (a flow reloaded under us, a
// value it rejects for a reason the client cannot know). Show it WHERE the inputs are, not
// only as a line buried in the run log — and mark the field it names, if it names one.
function showInputError(detail) {
  const box = flEl("flow-inputs");
  if (!box) return;
  clearInputError();
  const p = document.createElement("p");
  p.id = "flow-inputs-error";
  p.className = "flow-inputs-error";
  p.textContent = detail ? String(detail) : "the server rejected these inputs";
  box.appendChild(p);

  const text = String(detail || "");
  for (const field of box.querySelectorAll("[data-input-name]")) {
    const name = field.dataset.inputName;
    if (name && text.indexOf("'" + name + "'") !== -1) setInputInvalid(name, true);
  }
}

function clearInputError() {
  const p = flEl("flow-inputs-error");
  if (p && p.parentNode) p.parentNode.removeChild(p);
}

function setFlowRunning(on) {
  const cancel = flEl("flow-cancel");
  if (cancel) cancel.hidden = !on;
  syncRunButton();
}

function runFlow() {
  if (!flowWs || flowWs.readyState !== WebSocket.OPEN) {
    logAddLine("run socket is not connected.", "error");
    return;
  }
  // The button is already disabled in this state; this is the keyboard/programmatic guard.
  updateRunGate();
  if (!flowInputsOk) {
    showInputError("fill in every required input first");
    return;
  }
  clearInputError();
  const grant = {};
  for (const cap of FLOW_CAPS) {
    const cb = flEl(FLOW_CAP_IDS[cap]);
    grant[cap] = !!(cb && !cb.disabled && cb.checked);
  }
  const dry = flEl("flow-dry-run");
  const dryRun = !!(dry && dry.checked);

  const log = flEl("flow-log");
  if (log) log.textContent = "";
  flowNewCount = 0;
  flowDupeCount = 0;
  renderDedupe();
  flowRunning = true;
  setFlowRunning(true);

  flowWs.send(JSON.stringify({
    type: "run",
    inputs: collectInputs(),
    dry_run: dryRun,
    grant,
  }));
}

function cancelFlowRun() {
  if (flowWs && flowWs.readyState === WebSocket.OPEN) {
    flowWs.send(JSON.stringify({ type: "cancel" }));
    logAddLine("cancelling…", "info");
  }
}

// --- the run log (shared by the live stream AND the history replay) -----------
function logAddLine(text, cls) {
  const log = flEl("flow-log");
  if (!log) return;
  const div = document.createElement("div");
  div.className = "flow-log-line" + (cls ? " flow-" + cls : "");
  div.textContent = text;                      // server line — textContent only
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

// One line per `step` frame. The op-specific fields are the interesting part — a download
// carries name/size/sha/via and a status of new|dupe, which is the dedupe signal itself.
function logAddStep(step) {
  const log = flEl("flow-log");
  if (!log) return;
  const op = step.op || "?";
  const status = step.status || "";

  const div = document.createElement("div");
  div.className = "flow-log-line";

  const st = document.createElement("span");
  st.className = "flow-step-status st-" + String(status).replace(/[^a-z-]/gi, "");
  st.textContent = status;                     // server status — textContent only
  div.appendChild(st);

  const opEl = document.createElement("span");
  opEl.className = "flow-step-op";
  opEl.textContent = op;                       // server op — textContent only
  div.appendChild(opEl);

  const detail = document.createElement("span");
  detail.className = "flow-step-detail";
  detail.textContent = stepDetail(step);       // server strings — textContent only
  div.appendChild(detail);

  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

// A compact, op-aware one-liner. Unknown ops fall back to the fields the VM always sets,
// so a new op added server-side still logs something useful instead of a bare status.
function stepDetail(step) {
  const parts = [];
  switch (step.op) {
    case "download":
      if (step.name) parts.push(String(step.name));
      if (typeof step.size === "number") parts.push(flBytes(step.size));
      if (step.via) parts.push("via " + String(step.via));
      if (step.sha256) parts.push("sha " + String(step.sha256).slice(0, 12));
      if (step.href && !step.name) parts.push(String(step.href));
      break;
    case "goto":
      if (step.goal) parts.push("goal “" + String(step.goal) + "”");
      if (step.target) parts.push("→ " + String(step.target));
      if (typeof step.clicks === "number") parts.push(step.clicks + " clicks");
      break;
    case "for_each":
      if (step.match) {
        parts.push(typeof step.match === "string" ? step.match : JSON.stringify(step.match));
      }
      if (typeof step.found === "number") parts.push("found " + step.found);
      break;
    case "paginate":
      if (typeof step.page === "number") {
        parts.push("page " + step.page + (typeof step.pages === "number" ? "/" + step.pages : ""));
      }
      if (step.reason) parts.push(String(step.reason));
      break;
    default:
      for (const key of ["label", "selector", "value", "var", "into", "message",
                         "target", "url", "name", "count"]) {
        if (step[key] != null && step[key] !== "") parts.push(String(step[key]));
      }
      break;
  }
  if (step.error) parts.push(String(step.error));
  return parts.join("  ·  ");
}

function logAddResult(result) {
  const status = result.status || "?";
  const stats = result.stats || {};
  const ok = status === "ok";
  const bits = ["run " + status];
  if (typeof result.duration_s === "number") bits.push(result.duration_s.toFixed(1) + "s");
  if (typeof stats.steps_executed === "number") bits.push(stats.steps_executed + " steps");
  if (typeof stats.artifacts_new === "number") bits.push(stats.artifacts_new + " new");
  if (typeof stats.artifacts_dupe === "number") bits.push(stats.artifacts_dupe + " dupe");
  logAddLine((ok ? "✓ " : "✗ ") + bits.join(" · "), ok ? "ok" : "error");
  // WHY the reason gets its own line: a run can abort for a reason the static validator
  // cannot see (an unresolvable `goto` goal, a missing capability). `flow_cmd` prints it
  // ("✗ ABORTED — <reason>"); without this the web UI just said "aborted" and left the
  // user with no idea what to fix. Keep the two surfaces at parity.
  if (result.aborted) logAddLine("aborted — " + String(result.aborted), "error");
  if (result.detail) logAddLine(String(result.detail), "error");
}

// --- the dedupe counter (the differentiator, made visible DURING the run) -----
function countDedupe(step) {
  if (step.op !== "download") return;
  if (step.status === "new") flowNewCount++;
  else if (step.status === "dupe") flowDupeCount++;
  else return;
  renderDedupe();
}

function renderDedupe() {
  const box = flEl("flow-dedupe");
  if (!box) return;
  box.textContent = "";
  if (flowNewCount === 0 && flowDupeCount === 0) return;
  const n = document.createElement("span");
  n.className = "flow-dedupe-new";
  n.textContent = flowNewCount + " new";
  box.appendChild(n);
  const sep = document.createElement("span");
  sep.className = "flow-dedupe-sep";
  sep.textContent = " · ";
  box.appendChild(sep);
  const d = document.createElement("span");
  d.className = "flow-dedupe-dupe";
  d.textContent = flowDupeCount + " dupe";
  box.appendChild(d);
}

// The terminal `result` frame carries the authoritative counts — trust them over the
// ones we accumulated from the stream (a frame could have been missed on reconnect).
function applyStats(stats) {
  if (!stats) return;
  if (typeof stats.artifacts_new === "number") flowNewCount = stats.artifacts_new;
  if (typeof stats.artifacts_dupe === "number") flowDupeCount = stats.artifacts_dupe;
  renderDedupe();
}

// --- history: past runs, replayed read-only through the same log renderer -----
async function loadRuns(host, id) {
  const list = flEl("flow-runs");
  if (!list) return;
  let data;
  try {
    const res = await fetch(flowUrl(host, id) + "/runs");
    data = await res.json();
  } catch (err) {
    if (flowsHost === host) {
      list.textContent = "";
      list.appendChild(flEmpty("Could not load run history."));
    }
    return;
  }
  if (flowsHost !== host || flowId !== id) return;
  list.textContent = "";
  const runs = (data && Array.isArray(data.runs)) ? data.runs : [];
  if (runs.length === 0) {
    list.appendChild(flEmpty("No runs yet."));
    return;
  }
  for (const r of runs) list.appendChild(buildRunRow(host, id, r));
}

function buildRunRow(host, id, r) {
  const li = document.createElement("li");
  li.className = "flow-run-row";

  const head = document.createElement("div");
  head.className = "flow-run-head";
  head.appendChild(flBadge(r.status || "?", r.status === "ok" ? "ok" : "warn"));
  if (r.dry_run) head.appendChild(flBadge("dry run"));
  const when = document.createElement("span");
  when.className = "flow-run-when";
  when.textContent = flWhen(r.started_at);
  head.appendChild(when);
  li.appendChild(head);

  const stats = r.stats || {};
  const line = document.createElement("div");
  line.className = "flow-run-stats";
  const bits = [];
  if (typeof stats.steps_executed === "number") bits.push(stats.steps_executed + " steps");
  if (typeof stats.artifacts_new === "number") bits.push(stats.artifacts_new + " new");
  if (typeof stats.artifacts_dupe === "number") bits.push(stats.artifacts_dupe + " dupe");
  line.textContent = bits.join("  ·  ");
  li.appendChild(line);

  li.addEventListener("click", () => replayRun(host, id, r.run_id));
  return li;
}

// Load one stored run and replay its steps through logAddStep — the SAME renderer the
// live stream uses, so a historical run reads exactly like a fresh one (read-only: no
// socket is touched, and the Run button stays as it was).
async function replayRun(host, id, runId) {
  const log = flEl("flow-log");
  if (!log || !runId) return;
  log.textContent = "";
  let record;
  try {
    const res = await fetch(flowUrl(host, id) + "/runs/" + encodeURIComponent(runId));
    record = await res.json();
  } catch (err) {
    logAddLine("could not load that run.", "error");
    return;
  }
  if (flowsHost !== host || flowId !== id) return;
  flowNewCount = 0;
  flowDupeCount = 0;
  logAddLine("— replaying run " + runId + (record && record.dry_run ? " (dry run)" : "") +
    " —", "info");
  for (const step of (record && record.steps) || []) {
    logAddStep(step);
    countDedupe(step);
  }
  logAddResult(record || {});
  applyStats(record && record.stats);
}

// --- artifacts: the flow's all-time ledger (dedupe ACROSS runs) ---------------
async function loadArtifacts(host, id) {
  const list = flEl("flow-artifacts");
  const summary = flEl("flow-artifacts-summary");
  if (!list) return;
  let data;
  try {
    const res = await fetch(flowUrl(host, id) + "/artifacts");
    data = await res.json();
  } catch (err) {
    if (flowsHost === host) {
      list.textContent = "";
      list.appendChild(flEmpty("Could not load artifacts."));
    }
    return;
  }
  if (flowsHost !== host || flowId !== id) return;

  list.textContent = "";
  const rows = (data && Array.isArray(data.artifacts)) ? data.artifacts : [];
  const stats = (data && data.stats) || {};   // server nests the ledger stats under `stats`
  if (summary) {
    summary.textContent = rows.length
      ? (stats.count != null ? stats.count : rows.length) + " files · " +
        flBytes(stats.bytes || 0) + (stats.root ? "  ·  " + stats.root : "")
      : "";
  }
  if (rows.length === 0) {
    list.appendChild(flEmpty("No files downloaded yet — a run with “Allow download” fills this."));
    return;
  }
  for (const a of rows) list.appendChild(buildArtifactRow(a));
}

function buildArtifactRow(a) {
  const li = document.createElement("li");
  li.className = "flow-artifact";

  const name = document.createElement("span");
  name.className = "flow-artifact-name";
  name.textContent = a.name || "(file)";        // server string — textContent only
  li.appendChild(name);

  const size = document.createElement("span");
  size.className = "flow-artifact-size";
  size.textContent = flBytes(a.size);
  li.appendChild(size);

  const when = document.createElement("span");
  when.className = "flow-artifact-when";
  when.textContent = flWhen(a.seen_at);
  li.appendChild(when);

  const sha = document.createElement("span");
  sha.className = "flow-artifact-sha";
  sha.textContent = a.sha256 ? String(a.sha256).slice(0, 12) : "";
  sha.title = a.sha256 ? String(a.sha256) : "";
  li.appendChild(sha);

  return li;
}

// --- wiring (flows.js loads exactly once, so top-level listeners are safe) ----
(function wireFlows() {
  // The JSON textarea is a first-class EDITOR of the shared doc, not a display of it: a
  // keystroke that leaves valid JSON behind flows straight into setDoc(…, "json"), which
  // repaints the canvas. A SYNTAX error stays local (the banner below says so) — there is
  // nothing to sync yet, and round-tripping half-typed JSON would just flicker the canvas.
  const editor = flEl("flow-editor-text");
  if (editor) {
    editor.addEventListener("input", () => {
      let doc;
      try {
        doc = JSON.parse(editor.value);
      } catch (err) {
        scheduleValidate();          // keeps the existing local "not valid JSON" banner
        return;
      }
      setDoc(doc, "json");           // setDoc schedules the validation itself
    });
  }

  const nw = flEl("flow-new");
  if (nw) nw.addEventListener("click", newFlow);
  const save = flEl("flow-save");
  if (save) save.addEventListener("click", saveFlow);
  const del = flEl("flow-delete");
  if (del) del.addEventListener("click", deleteFlow);
  const run = flEl("flow-run");
  if (run) run.addEventListener("click", runFlow);
  const cancel = flEl("flow-cancel");
  if (cancel) cancel.addEventListener("click", cancelFlowRun);
})();
