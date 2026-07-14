// pinchtab-webgraph web UI — the Flows CANVAS.
//
// A flow doc is a NESTED SEQUENCE, not a free-floating node graph, so the canvas is a
// VERTICAL FLOW DIAGRAM WITH CONTAINERS: boxes top-to-bottom joined by connectors, and a
// body op (`for_each` / `paginate`) renders as a container that visibly WRAPS its child
// steps. Containment is the whole point — a picture that hid it would be a lie about the
// document.
//
// This file is PURE DOM + PATH LOGIC. It opens no socket and makes no fetch: it is handed
// a doc + the op schema and hands back a NEW doc through `onMutate`. flows.js owns the
// network, the validation and the three-way sync.
//
// TWO INVARIANTS THIS FILE EXISTS TO KEEP:
//
//   1. `data-path` uses flow.py's path grammar EXACTLY — `steps[1].body[0].body[0]`. That
//      is what makes flow.validate()'s error path (which is reported in the same grammar)
//      addressable: highlightPath() can point at the offending BOX, not just print a path.
//
//   2. Every per-op form is DERIVED from GET /api/flows/op_schema (the real LEAF_OPS /
//      BODY_OPS tables from flow.py). There is deliberately NO op list in this file — a
//      second copy of the DSL would drift the first time an op is added server-side.
//      The only per-KEY knowledge here is a rendering hint (is this key a number? an
//      object? a flag?) with a text-input fallback, so an unknown key still edits fine.
//
// SAFETY: step values (name/href/selector/message/…) are attacker-controlled when a flow
// targets a hostile site, and MODEL-controlled now that an agent writes them. Everything
// is createElement + textContent — NEVER innerHTML — and an href is never rendered as a
// live <a> (clicking one would navigate the SPA away).
"use strict";

// The mutators are pure w.r.t. the doc, so a `wrapInLoop` called without a schema (the
// signature the orchestrator froze) can still seed the wrapper: remember the last schema
// the canvas rendered with.
let canvasSchema = null;

// Which nodes have their edit form open, by path. Kept ACROSS repaints (every mutation
// repaints the whole canvas) so editing a step doesn't slam the form shut under you.
const canvasOpenForms = new Set();

// --- path grammar (must match flow.py's `steps[1].body[0]` exactly) ----------

// "steps[1].body[0]" -> [["steps",1],["body",0]]
function parsePath(path) {
  const out = [];
  if (!path) return out;
  const re = /([A-Za-z_][A-Za-z0-9_]*)\[(\d+)\]/g;
  let m;
  while ((m = re.exec(String(path))) !== null) out.push([m[1], Number(m[2])]);
  return out;
}

// The step (or the doc, for "") a path addresses. Returns undefined when it doesn't resolve.
function getAt(doc, path) {
  let cur = doc;
  for (const seg of parsePath(path)) {
    if (!cur || typeof cur !== "object") return undefined;
    const arr = cur[seg[0]];
    if (!Array.isArray(arr)) return undefined;
    cur = arr[seg[1]];
  }
  return cur;
}

function cloneDoc(doc) {
  // A flow doc is JSON by definition, so a round trip is a faithful deep copy — and it
  // also strips anything non-JSON an agent might have slipped in.
  try { return JSON.parse(JSON.stringify(doc)); } catch (e) { return doc; }
}

// An ARRAY path — "steps" or "steps[1].body" — resolved to the live array (or null).
function getArrayAt(doc, arrayPath) {
  const m = String(arrayPath).match(/^(.*)\.([A-Za-z_][A-Za-z0-9_]*)$/);
  const owner = m ? getAt(doc, m[1]) : doc;
  const key = m ? m[2] : String(arrayPath);
  if (!owner || typeof owner !== "object") return null;
  return Array.isArray(owner[key]) ? owner[key] : null;
}

// "steps[1].body[0]" -> {arrayPath: "steps[1].body", index: 0}
function splitStepPath(stepPath) {
  const m = String(stepPath).match(/^(.*)\[(\d+)\]$/);
  if (!m) return null;
  return { arrayPath: m[1], index: Number(m[2]) };
}

// --- the op schema (the ONLY source of truth for what a step may contain) -----
function opSpec(schema, op) {
  if (!schema) return null;
  const leaf = schema.leaf_ops || {};
  const body = schema.body_ops || {};
  return leaf[op] || body[op] || null;
}

function isBodyOp(schema, op) {
  return !!(schema && schema.body_ops && schema.body_ops[op]);
}

function isWriteOp(schema, op) {
  return !!(schema && Array.isArray(schema.write_ops) && schema.write_ops.indexOf(op) !== -1);
}

// The variables a step inside this container may reference: the schema's `body_vars` for
// the op, plus the loop's own alias (`as`, which flow.py defaults to "item"). Both are
// read from the doc/schema — nothing here knows what a `for_each` IS.
function bodyVars(schema, op, step) {
  const out = [];
  const declared = (schema && schema.body_vars && schema.body_vars[op]) || [];
  for (const v of declared) out.push(String(v));
  const spec = opSpec(schema, op) || {};
  const optKeys = spec.opt || [];
  if (optKeys.indexOf("as") !== -1) {
    const alias = (step && typeof step.as === "string" && step.as.trim()) ? step.as : "item";
    out.unshift(alias);
  }
  return out;
}

function allOps(schema) {
  if (!schema) return { leaf: [], body: [] };
  return {
    leaf: Object.keys(schema.leaf_ops || {}),
    body: Object.keys(schema.body_ops || {}),
  };
}

// A RENDERING hint, not a schema: which widget suits a key. The KEY SET always comes from
// op_schema — this only decides how to draw the box, and anything unlisted falls back to a
// text input, so a new key added server-side still edits fine (as a string).
const CANVAS_NUMBER_KEYS = ["ms", "timeout_ms", "max", "max_pages", "index"];
const CANVAS_OBJECT_KEYS = ["match", "set"];
const CANVAS_FLAG_KEYS = ["submit", "dedupe"];

function keyKind(key) {
  if (CANVAS_NUMBER_KEYS.indexOf(key) !== -1) return "number";
  if (CANVAS_OBJECT_KEYS.indexOf(key) !== -1) return "object";
  if (CANVAS_FLAG_KEYS.indexOf(key) !== -1) return "flag";
  return "text";
}

// The blank a freshly-added step carries for a required key: PRESENT (so the doc is
// structurally valid the moment it lands) but empty (so it reads as "fill me in").
function blankValue(key) {
  switch (keyKind(key)) {
    case "number": return 0;
    case "object": return {};
    case "flag": return false;
    default: return "";
  }
}

// A new step for `op`, seeded from the schema alone: the first `one_of` alternative + every
// required key. A body op gets an empty `body` — which flow.validate() rejects ("requires a
// non-empty body"), and that is DELIBERATE: the canvas immediately highlights the empty
// container, which is exactly the instruction "put a step in me".
function seedStep(op, schema) {
  const spec = opSpec(schema, op) || {};
  const step = { op: op };
  const oneOf = spec.one_of || [];
  if (oneOf.length) step[oneOf[0]] = blankValue(oneOf[0]);
  for (const key of (spec.req || [])) step[key] = blankValue(key);
  if (isBodyOp(schema, op)) step.body = [];
  return step;
}

// --- mutators: every one returns a NEW doc, none mutates in place -------------

function insertStep(doc, arrayPath, index, newStep) {
  const next = cloneDoc(doc);
  let arr = getArrayAt(next, arrayPath);
  if (!arr) {
    // The array doesn't exist yet (a doc with no `steps`, or a body op with no `body`) —
    // create it rather than dropping the insert on the floor.
    const m = String(arrayPath).match(/^(.*)\.([A-Za-z_][A-Za-z0-9_]*)$/);
    const owner = m ? getAt(next, m[1]) : next;
    const key = m ? m[2] : String(arrayPath);
    if (!owner || typeof owner !== "object") return next;
    owner[key] = [];
    arr = owner[key];
  }
  const at = (index == null) ? arr.length : Math.max(0, Math.min(arr.length, index));
  arr.splice(at, 0, newStep);
  return next;
}

function deleteStep(doc, stepPath) {
  const next = cloneDoc(doc);
  const loc = splitStepPath(stepPath);
  if (!loc) return next;
  const arr = getArrayAt(next, loc.arrayPath);
  if (!arr || loc.index < 0 || loc.index >= arr.length) return next;
  arr.splice(loc.index, 1);
  // An emptied `body` is left EMPTY on purpose: the validator will say so and the canvas
  // will highlight it. Silently deleting the parent container would throw away a step the
  // user never asked to lose.
  return next;
}

// dir: -1 / "up" moves earlier, +1 / "down" moves later. A move past either end is a no-op.
function moveStep(doc, stepPath, dir) {
  const next = cloneDoc(doc);
  const loc = splitStepPath(stepPath);
  if (!loc) return next;
  const arr = getArrayAt(next, loc.arrayPath);
  if (!arr) return next;
  const delta = (dir === "up") ? -1 : (dir === "down") ? 1 : Number(dir) || 0;
  const to = loc.index + delta;
  if (loc.index < 0 || loc.index >= arr.length || to < 0 || to >= arr.length) return next;
  const moved = arr.splice(loc.index, 1)[0];
  arr.splice(to, 0, moved);
  return next;
}

// `patch` is a plain key->value map. A key whose value is undefined/null is DELETED — that
// is how the form clears an optional key (an empty optional must not be sent as "", which
// the runner would read as a real, empty value).
function editStepField(doc, stepPath, patch) {
  const next = cloneDoc(doc);
  const step = getAt(next, stepPath);
  if (!step || typeof step !== "object" || !patch) return next;
  for (const key of Object.keys(patch)) {
    const v = patch[key];
    if (v === undefined || v === null) delete step[key];
    else step[key] = v;
  }
  return next;
}

// Wrap the step at `stepPath` in a fresh body op (`for_each` / `paginate` — whatever the
// schema calls them), with the step as the wrapper's only child.
function wrapInLoop(doc, stepPath, kind, schema) {
  const next = cloneDoc(doc);
  const loc = splitStepPath(stepPath);
  if (!loc) return next;
  const arr = getArrayAt(next, loc.arrayPath);
  if (!arr || loc.index < 0 || loc.index >= arr.length) return next;
  const wrapper = seedStep(kind, schema || canvasSchema);
  wrapper.body = [arr[loc.index]];
  arr[loc.index] = wrapper;
  return next;
}

// --- small DOM helpers (createElement + textContent only) --------------------
function fcEl(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = String(text);   // untrusted value — textContent only
  return node;
}

function fcButton(cls, text, title, onClick) {
  const b = document.createElement("button");
  b.type = "button";                       // never a form submit
  b.className = cls;
  b.textContent = text;                    // fixed UI glyph/label, not doc data
  if (title) b.title = title;
  if (onClick) b.addEventListener("click", onClick);
  return b;
}

// A step value, shrunk to one legible line. Objects are stringified (never rendered as
// markup), long strings truncated — and it all lands via textContent regardless.
function shortValue(v) {
  let s;
  if (typeof v === "string") s = '"' + v + '"';
  else if (v && typeof v === "object") { try { s = JSON.stringify(v); } catch (e) { s = "…"; } }
  else s = String(v);
  return s.length > 64 ? s.slice(0, 63) + "…" : s;
}

function stepSummary(step) {
  if (!step || typeof step !== "object") return "";
  const parts = [];
  for (const key of Object.keys(step)) {
    if (key === "op" || key === "body") continue;
    parts.push(key + ": " + shortValue(step[key]));
  }
  return parts.join("   ·   ");
}

// --- the per-op edit form (derived ONLY from op_schema) -----------------------

// One value widget for one key. `read()` returns {ok, value, error}; a `value` of undefined
// means "omit this key" (an untouched optional).
function buildValueInput(key, value, required) {
  const kind = keyKind(key);

  if (kind === "flag") {
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = value === true;
    return { el: input, read: () => ({ ok: true, value: input.checked ? true : (required ? false : undefined) }) };
  }

  if (kind === "object") {
    const ta = document.createElement("textarea");
    ta.className = "flow-field-json";
    ta.spellcheck = false;
    ta.rows = 2;
    if (value !== undefined) {
      try { ta.value = JSON.stringify(value); } catch (e) { ta.value = ""; }
    }
    ta.placeholder = '{"kind": "download"}';
    return {
      el: ta,
      read: () => {
        const raw = ta.value.trim();
        if (!raw) return { ok: true, value: required ? {} : undefined };
        try { return { ok: true, value: JSON.parse(raw) }; }
        catch (err) { return { ok: false, error: key + ": not valid JSON" }; }
      },
    };
  }

  if (kind === "number") {
    const input = document.createElement("input");
    input.type = "number";
    if (typeof value === "number") input.value = String(value);
    else if (typeof value === "string") input.value = value;   // a "${var}" — see below
    return {
      el: input,
      read: () => {
        const raw = input.value.trim();
        if (!raw) return { ok: true, value: required ? 0 : undefined };
        const n = Number(raw);
        if (isNaN(n)) return { ok: false, error: key + ": expected a number" };
        return { ok: true, value: n };
      },
    };
  }

  const input = document.createElement("input");
  input.type = "text";
  if (value !== undefined && value !== null && typeof value !== "object") {
    input.value = String(value);           // an input VALUE is inert (never parsed as markup)
  }
  input.placeholder = "${item.href} or a literal";
  return {
    el: input,
    read: () => {
      const raw = input.value;
      if (raw === "") return { ok: true, value: required ? "" : undefined };
      return { ok: true, value: raw };
    },
  };
}

function buildFieldRow(key, value, required, readers) {
  const row = fcEl("div", "flow-field");
  const label = fcEl("span", "flow-field-label", key + (required ? " *" : ""));
  const widget = buildValueInput(key, value, required);
  const holder = fcEl("div", "flow-field-input");
  holder.appendChild(widget.el);
  row.appendChild(label);
  row.appendChild(holder);
  readers.push({ key: key, read: widget.read });
  return row;
}

// The labeled inputs for ONE op, built strictly from that op's `one_of` / `req` / `opt`.
// `onSubmit(patch)` receives the key->value patch (undefined = delete the key).
function buildStepForm(op, step, schema, onSubmit) {
  const form = document.createElement("form");
  form.className = "flow-node-form";
  form.autocomplete = "off";

  const spec = opSpec(schema, op);
  const readers = [];
  const err = fcEl("p", "flow-field-err", "");

  if (!spec) {
    form.appendChild(fcEl("p", "flow-field-err",
      "unknown op “" + op + "” — the server's op schema has no entry for it."));
    return form;
  }

  const oneOf = (spec.one_of || []).slice();
  const req = (spec.req || []).slice();
  // A key that is BOTH a one_of alternative and an opt (goto's `match`) is rendered once,
  // in the one_of picker — the JSON pane remains the way to set both at the same time.
  const opt = (spec.opt || []).filter(
    (k) => oneOf.indexOf(k) === -1 && req.indexOf(k) === -1);

  let oneOfSelect = null;
  if (oneOf.length) {
    const row = fcEl("div", "flow-field flow-field-oneof");
    row.appendChild(fcEl("span", "flow-field-label", "one of *"));
    const sel = document.createElement("select");
    for (const k of oneOf) {
      const o = document.createElement("option");
      o.value = k;
      o.textContent = k;                   // an op-schema key — textContent regardless
      sel.appendChild(o);
    }
    const held = oneOf.filter((k) => step && Object.prototype.hasOwnProperty.call(step, k));
    sel.value = held.length ? held[0] : oneOf[0];
    oneOfSelect = sel;

    const holder = fcEl("div", "flow-field-input");
    let widget = null;
    const mount = (key) => {
      holder.textContent = "";
      widget = buildValueInput(key, step ? step[key] : undefined, true);
      holder.appendChild(widget.el);
    };
    mount(sel.value);
    sel.addEventListener("change", () => mount(sel.value));

    row.appendChild(sel);
    row.appendChild(holder);
    form.appendChild(row);
    readers.push({ oneOf: true, read: () => widget.read(), keyOf: () => sel.value });
  }

  for (const key of req) {
    form.appendChild(buildFieldRow(key, step ? step[key] : undefined, true, readers));
  }
  for (const key of opt) {
    form.appendChild(buildFieldRow(key, step ? step[key] : undefined, false, readers));
  }
  if (!oneOf.length && !req.length && !opt.length) {
    form.appendChild(fcEl("p", "flow-field-hint", "This op takes no fields."));
  }

  form.appendChild(err);

  const actions = fcEl("div", "flow-field-actions");
  const save = document.createElement("button");
  save.type = "submit";
  save.className = "flow-node-save";
  save.textContent = "Apply";
  actions.appendChild(save);
  form.appendChild(actions);

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    err.textContent = "";
    const patch = {};
    for (const r of readers) {
      const got = r.read();
      if (!got.ok) { err.textContent = got.error || "invalid value"; return; }
      if (r.oneOf) {
        const chosen = r.keyOf();
        // the alternatives NOT chosen are dropped — the doc must say exactly one thing.
        for (const k of oneOf) if (k !== chosen) patch[k] = undefined;
        patch[chosen] = got.value;
      } else {
        patch[r.key] = got.value;
      }
    }
    if (typeof onSubmit === "function") onSubmit(patch);
  });

  return form;
}

// --- the op picker (the ONLY place a new step is born) -----------------------
// Built from op_schema: leaf ops and body ops in two labeled groups, so "this one wraps
// other steps" is visible at the moment of choosing.
function buildOpPicker(schema, onPick, onCancel) {
  const box = fcEl("div", "flow-op-picker");
  const sel = document.createElement("select");
  const ops = allOps(schema);

  const addGroup = (label, names) => {
    if (!names.length) return;
    const g = document.createElement("optgroup");
    g.label = label;
    for (const name of names.slice().sort()) {
      const o = document.createElement("option");
      o.value = name;
      o.textContent = name + (isWriteOp(schema, name) ? "  (writes)" : "");
      g.appendChild(o);
    }
    sel.appendChild(g);
  };
  addGroup("steps", ops.leaf);
  addGroup("containers (wrap other steps)", ops.body);

  if (!ops.leaf.length && !ops.body.length) {
    box.appendChild(fcEl("span", "flow-field-hint", "op schema unavailable"));
    return box;
  }

  box.appendChild(sel);
  box.appendChild(fcButton("flow-op-add", "Add", "Add this step", () => onPick(sel.value)));
  box.appendChild(fcButton("flow-op-cancel", "Cancel", "", () => onCancel && onCancel()));
  return box;
}

// --- rendering ---------------------------------------------------------------

// Every mutation goes through here: hand the NEW doc to the owner (flows.js — which
// updates the JSON pane and re-validates) and repaint the canvas from it. The canvas
// repaints ITSELF, which is precisely why setDoc(doc, "canvas") does not: a second render
// would be wasted work, and re-entrancy is how a sync loop starts.
function canvasMutate(ctx, nextDoc) {
  ctx.onMutate(nextDoc);
  renderCanvasFromDoc(ctx.container, nextDoc, ctx.opSchema, { onMutate: ctx.onMutate });
}

function buildToolbar(ctx, doc, step, path, op) {
  const bar = fcEl("div", "flow-node-toolbar");
  const loc = splitStepPath(path);

  bar.appendChild(fcButton("flow-node-btn", "✎", "Edit this step's fields", () => {
    if (canvasOpenForms.has(path)) canvasOpenForms.delete(path);
    else canvasOpenForms.add(path);
    renderCanvasFromDoc(ctx.container, doc, ctx.opSchema, { onMutate: ctx.onMutate });
  }));

  bar.appendChild(fcButton("flow-node-btn", "↑", "Move up", () => {
    if (!loc) return;
    canvasOpenForms.clear();      // the paths shift under a move — stale open-forms would lie
    canvasMutate(ctx, moveStep(doc, path, -1));
  }));
  bar.appendChild(fcButton("flow-node-btn", "↓", "Move down", () => {
    if (!loc) return;
    canvasOpenForms.clear();
    canvasMutate(ctx, moveStep(doc, path, 1));
  }));

  // insert-after: opens the same op picker the [+ add step] row uses, at index+1.
  bar.appendChild(fcButton("flow-node-btn", "+", "Insert a step after this one", (e) => {
    const node = e.target.closest(".flow-node");
    if (!node || !loc) return;
    const existing = node.querySelector(":scope > .flow-node-picker");
    if (existing) { existing.remove(); return; }
    const slot = fcEl("div", "flow-node-picker");
    slot.appendChild(buildOpPicker(ctx.opSchema, (op2) => {
      canvasOpenForms.clear();
      canvasMutate(ctx, insertStep(doc, loc.arrayPath, loc.index + 1,
        seedStep(op2, ctx.opSchema)));
    }, () => slot.remove()));
    const head = node.querySelector(":scope > .flow-node-head");
    if (head && head.nextSibling) node.insertBefore(slot, head.nextSibling);
    else node.appendChild(slot);
  }));

  // wrap: one button per BODY op, from the schema — never a hardcoded ["for_each", …].
  const bodyOps = allOps(ctx.opSchema).body;
  if (bodyOps.length && !isBodyOp(ctx.opSchema, op)) {
    bar.appendChild(fcButton("flow-node-btn", "⧉", "Wrap this step in a container", (e) => {
      const node = e.target.closest(".flow-node");
      if (!node) return;
      const existing = node.querySelector(":scope > .flow-node-wrap");
      if (existing) { existing.remove(); return; }
      const slot = fcEl("div", "flow-node-wrap");
      slot.appendChild(fcEl("span", "flow-field-hint", "wrap in:"));
      for (const kind of bodyOps.slice().sort()) {
        slot.appendChild(fcButton("flow-op-add", kind, "Wrap in " + kind, () => {
          canvasOpenForms.clear();
          canvasMutate(ctx, wrapInLoop(doc, path, kind, ctx.opSchema));
        }));
      }
      slot.appendChild(fcButton("flow-op-cancel", "Cancel", "", () => slot.remove()));
      const head = node.querySelector(":scope > .flow-node-head");
      if (head && head.nextSibling) node.insertBefore(slot, head.nextSibling);
      else node.appendChild(slot);
    }));
  }

  // delete: first click arms, a second confirms (the pattern flows.js + the chat chips use).
  const del = fcButton("flow-node-btn flow-node-del", "✕", "Delete this step", () => {
    if (del.classList.contains("confirm")) {
      canvasOpenForms.clear();
      canvasMutate(ctx, deleteStep(doc, path));
      return;
    }
    del.classList.add("confirm");
    del.textContent = "delete?";
    setTimeout(() => {
      del.classList.remove("confirm");
      del.textContent = "✕";
    }, 2000);
  });
  bar.appendChild(del);

  return bar;
}

function buildNode(ctx, doc, step, path) {
  const node = fcEl("div", "flow-node");
  node.dataset.path = path;                       // flow.py's grammar, EXACTLY

  const op = (step && typeof step === "object" && step.op != null) ? String(step.op) : "?";
  const container = isBodyOp(ctx.opSchema, op);
  if (container) node.classList.add("flow-node-container");

  const head = fcEl("div", "flow-node-head");
  head.appendChild(fcEl("span", "flow-node-op", op));   // model/user string — textContent
  if (isWriteOp(ctx.opSchema, op)) {
    head.appendChild(fcEl("span", "flow-node-write", "writes"));
  }
  head.appendChild(fcEl("span", "flow-node-summary", stepSummary(step)));
  head.appendChild(buildToolbar(ctx, doc, step, path, op));
  node.appendChild(head);

  if (canvasOpenForms.has(path)) {
    node.appendChild(buildStepForm(op, step, ctx.opSchema, (patch) => {
      canvasOpenForms.delete(path);
      canvasMutate(ctx, editStepField(doc, path, patch));
    }));
  }

  if (container) {
    const body = fcEl("div", "flow-node-body");
    // What a step INSIDE this container may refer to. Straight from op_schema's
    // `body_vars` (+ the loop's own alias) — the one thing you must know to write a
    // `${…}` here, so it is said where you'd write it rather than left to be guessed.
    const vars = bodyVars(ctx.opSchema, op, step);
    if (vars.length) {
      body.appendChild(fcEl("p", "flow-field-hint",
        "in scope: " + vars.map((v) => "${" + v + "}").join(", ")));
    }
    const kids = (step && Array.isArray(step.body)) ? step.body : [];
    body.appendChild(buildStepList(ctx, doc, kids, path + ".body"));
    node.appendChild(body);
  }

  return node;
}

// One vertical column of boxes + the connectors between them + the trailing [+ add step].
function buildStepList(ctx, doc, steps, arrayPath) {
  const list = fcEl("div", "flow-node-list");
  const rows = Array.isArray(steps) ? steps : [];

  rows.forEach((step, i) => {
    if (i > 0) list.appendChild(fcEl("div", "flow-connector"));
    list.appendChild(buildNode(ctx, doc, step, arrayPath + "[" + i + "]"));
  });

  if (rows.length === 0) {
    list.appendChild(fcEl("p", "flow-node-empty", "empty — add a step"));
  }

  const addRow = fcEl("div", "flow-add-row");
  addRow.appendChild(fcButton("flow-add-btn", "+ add step", "Append a step here", () => {
    const open = addRow.querySelector(".flow-op-picker");
    if (open) { open.remove(); return; }
    addRow.appendChild(buildOpPicker(ctx.opSchema, (op) => {
      canvasMutate(ctx, insertStep(doc, arrayPath, rows.length, seedStep(op, ctx.opSchema)));
    }, () => {
      const p = addRow.querySelector(".flow-op-picker");
      if (p) p.remove();
    }));
  }));
  list.appendChild(addRow);

  return list;
}

// The entry point. Re-callable: it always repaints the whole canvas from `doc`.
function renderCanvasFromDoc(container, doc, opSchema, options) {
  if (!container) return;
  if (opSchema) canvasSchema = opSchema;
  const ctx = {
    container: container,
    opSchema: opSchema || canvasSchema,
    onMutate: (options && options.onMutate) || function () {},
  };

  container.textContent = "";
  if (!doc || typeof doc !== "object" || Array.isArray(doc)) {
    container.appendChild(fcEl("p", "flow-node-empty",
      "No flow open — pick one on the left, or “+ New flow”."));
    return;
  }
  container.appendChild(buildStepList(ctx, doc, doc.steps, "steps"));
}

// flow.validate() reports its error path in the SAME grammar this canvas uses for
// data-path — so an invalid doc points at a BOX. When the path is deeper than a step (e.g.
// a bad value inside one), the nearest enclosing box is highlighted instead of nothing.
function highlightPath(container, path) {
  if (!container) return;
  for (const node of container.querySelectorAll(".flow-node-error")) {
    node.classList.remove("flow-node-error");
  }
  if (!path) return;
  const target = String(path);
  let best = null;
  let bestLen = -1;
  for (const node of container.querySelectorAll(".flow-node")) {
    const p = node.dataset.path || "";
    if (p === target) { best = node; bestLen = p.length; break; }
    // a PREFIX only counts on a segment boundary — "steps[1]" must not claim "steps[10]".
    if (target.indexOf(p) === 0 && target.charAt(p.length) === "." && p.length > bestLen) {
      best = node;
      bestLen = p.length;
    }
  }
  if (!best) return;
  best.classList.add("flow-node-error");
  if (best.scrollIntoView) best.scrollIntoView({ block: "nearest" });
}

// The box whose data-path IS this path (exact only — a warning always names a whole step).
function nodeAtPath(container, path) {
  if (!container || !path) return null;
  const target = String(path);
  for (const node of container.querySelectorAll(".flow-node")) {
    if ((node.dataset.path || "") === target) return node;
  }
  return null;
}

// RESOLVABILITY warnings (POST /api/flows/validate -> `warnings`): the doc is legal, but a
// `goal` names nothing in the crawled graph, so the RUN would abort on that step. Amber, and
// deliberately NOT the red .flow-node-error — one is "this document is wrong", the other is
// "this document is fine but the site disagrees", and the user fixes them differently.
//
// The message + candidates are printed ON the box: the whole value is "did you mean “Add
// Report”?" said where the mistake is, not in a banner the user has to translate into a
// bracket-count. Every string here is server data (candidates come from the CRAWLED SITE) —
// createElement + textContent only, never innerHTML.
function markWarnings(container, warnings) {
  if (!container) return;
  for (const node of container.querySelectorAll(".flow-node-warn")) {
    node.classList.remove("flow-node-warn");
  }
  for (const note of container.querySelectorAll(".flow-node-warning")) note.remove();
  if (!Array.isArray(warnings) || warnings.length === 0) return;

  for (const w of warnings) {
    if (!w || typeof w !== "object") continue;
    const node = nodeAtPath(container, w.path);
    if (!node) continue;                       // a path we can't address: the banner still says it
    node.classList.add("flow-node-warn");

    const note = fcEl("div", "flow-node-warning");
    note.appendChild(fcEl("span", "flow-node-warning-icon", "⚠"));   // fixed glyph
    note.appendChild(fcEl("span", "flow-node-warning-msg",
      String(w.message || "this step may not resolve against the crawled graph")));

    const cands = Array.isArray(w.candidates) ? w.candidates.filter((c) => typeof c === "string") : [];
    if (cands.length) {
      const did = fcEl("div", "flow-node-warning-cands");
      did.appendChild(fcEl("span", "flow-node-warning-lead", "did you mean:"));
      for (const c of cands) did.appendChild(fcEl("span", "flow-node-cand", c));
      note.appendChild(did);
    }
    // after the head (and any open edit form), before a container's body — the warning reads
    // as part of THIS step, not as a caption on the step below it.
    const head = node.querySelector(".flow-node-head");
    if (head && head.nextSibling) node.insertBefore(note, head.nextSibling);
    else node.appendChild(note);
  }
}
