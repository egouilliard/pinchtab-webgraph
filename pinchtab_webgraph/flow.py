#!/usr/bin/env python3
"""
The FLOW document model — a declarative, executable automation over a crawled site.

A flow is DATA, not code. It is a JSON document that `runner.py` interprets against a live
browser. That choice is load-bearing:

  - **Safe to schedule.** No arbitrary code executes, so a flow can be run by a worker, an
    HTTP request, or a cron tick without a sandbox. Every side effect a flow can have is
    declared up front in `capabilities` and enforced by the runner.
  - **Self-healable.** A step names its target semantically (`goal`, `match`) as well as
    structurally (`selector`). When the site changes, the graph is re-crawled and the step
    re-resolves — a flow survives a redesign that would break a recorded selector script.
  - **Introspectable.** `inputs` is a JSON-Schema-shaped declaration, so a saved flow can be
    exposed as a typed HTTP endpoint / MCP tool without anyone writing a wrapper.

Everything here is pure: parse, validate, substitute. No I/O, no browser, stdlib only — so
the model can be validated in an API handler before a browser is ever leased.

  {
    "name": "download-all-invoices",
    "host": "app.example.com",
    "inputs": {"since": {"type": "string", "required": false}},
    "capabilities": {"allow_download": true},
    "steps": [
      {"op": "goto", "goal": "invoices"},
      {"op": "paginate", "max_pages": 50, "body": [
        {"op": "for_each", "match": {"kind": "download"}, "as": "item", "body": [
          {"op": "download", "selector": "${item.selector}", "href": "${item.href}"}
        ]}
      ]}
    ]
  }
"""
import json
import re

SCHEMA_VERSION = 1

# --- the instruction set -------------------------------------------------------
# Each op maps to a method on the runner. `body` ops are the control flow — they are the
# whole reason this layer exists (perform.py can only run a straight line).

LEAF_OPS = {
    # `goto` accepts `match` as well as `goal`: the runner forwards it to api.resolve_action,
    # which resolves on a trigger-label regex ALONE — so a flow must be able to say so.
    "goto":     {"one_of": ("url", "goal", "match"), "opt": ("start", "match")},
    "do":       {"one_of": ("goal", "match"), "opt": ("set", "file", "submit", "start", "index")},
    "click":    {"one_of": ("selector", "text"), "opt": ()},
    "fill":     {"one_of": ("selector", "label"), "req": ("value",), "opt": ()},
    "select":   {"one_of": ("selector", "label"), "req": ("value",), "opt": ()},
    "check":    {"one_of": ("selector", "label"), "opt": ()},
    "upload":   {"one_of": ("selector",), "req": ("file",), "opt": ()},
    "download": {"one_of": ("href", "selector"), "opt": ("name", "dedupe")},
    "collect":  {"one_of": ("into",), "opt": ("kind",)},
    "wait":     {"one_of": ("ms", "selector", "text"), "opt": ("timeout_ms",)},
    "set":      {"one_of": ("var",), "req": ("value",), "opt": ()},
    "log":      {"one_of": ("message",), "opt": ()},
}

BODY_OPS = {
    "for_each": {"req": ("match",), "opt": ("as", "max")},
    "paginate": {"opt": ("max_pages", "until")},
}

OPS = set(LEAF_OPS) | set(BODY_OPS)

# Ops that can mutate the target site. The runner refuses these unless the flow's
# `capabilities` opts in — the same safe-by-default posture perform.py already enforces
# for a form submit, lifted to the flow level so a scheduled run can't surprise anyone.
WRITE_OPS = {"upload"}

MAX_DEPTH = 6          # nesting guard: a hand-written or LLM-authored flow can't run away
MAX_STEPS = 500        # total steps in the document


class FlowError(ValueError):
    """A flow document is malformed. Carries `path` — where in the doc the problem is."""

    def __init__(self, message, path=""):
        super().__init__("%s%s" % (("%s: " % path) if path else "", message))
        self.path = path
        self.message = message


# --- capabilities --------------------------------------------------------------

DEFAULT_CAPABILITIES = {
    "allow_submit": False,     # a form's SUBMIT (writes to the site)
    "allow_download": True,    # fetching files (read-only; the point of most flows)
    "allow_upload": False,     # sending files to the site (writes)
}


def capabilities(flow):
    """The flow's effective capability set (defaults + whatever it declares)."""
    caps = dict(DEFAULT_CAPABILITIES)
    caps.update(flow.get("capabilities") or {})
    return caps


# --- variable substitution -----------------------------------------------------
# `${a.b}` only. Deliberately NOT a template language and NOT an expression evaluator:
# a flow is executed by a scheduler on a machine with a logged-in browser session, so the
# document must never be able to compute. Lookup is a plain dotted path into the run's
# variable map; anything else is a validation error, not a silent empty string.

_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*)\}")


def variable_names(value):
    """Every `${...}` reference inside a value (recursing into dicts/lists)."""
    found = []
    if isinstance(value, str):
        found += _VAR_RE.findall(value)
    elif isinstance(value, dict):
        for v in value.values():
            found += variable_names(v)
    elif isinstance(value, list):
        for v in value:
            found += variable_names(v)
    return found


def _lookup(dotted, scope):
    cur = scope
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise FlowError("unknown variable ${%s}" % dotted)
        cur = cur[part]
    return cur


def substitute(value, scope):
    """Resolve every `${...}` in a value against `scope`. A string that is EXACTLY one
    reference resolves to the referent's native type (so `"${item.count}"` stays an int);
    a reference embedded in surrounding text interpolates as a string."""
    if isinstance(value, str):
        whole = _VAR_RE.fullmatch(value.strip())
        if whole:
            return _lookup(whole.group(1), scope)
        return _VAR_RE.sub(lambda m: str(_lookup(m.group(1), scope)), value)
    if isinstance(value, dict):
        return {k: substitute(v, scope) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, scope) for v in value]
    return value


# --- validation ----------------------------------------------------------------

def _validate_step(step, path, depth, counter):
    if not isinstance(step, dict):
        raise FlowError("a step must be an object", path)
    counter[0] += 1
    if counter[0] > MAX_STEPS:
        raise FlowError("flow has more than %d steps" % MAX_STEPS, path)
    if depth > MAX_DEPTH:
        raise FlowError("nested deeper than %d levels" % MAX_DEPTH, path)

    op = step.get("op")
    if op not in OPS:
        raise FlowError("unknown op %r (expected one of: %s)"
                        % (op, ", ".join(sorted(OPS))), path)

    spec = LEAF_OPS.get(op) or BODY_OPS[op]
    for key in spec.get("req", ()):
        if key not in step:
            raise FlowError("op %r requires %r" % (op, key), path)
    one_of = spec.get("one_of", ())
    if one_of and not any(k in step for k in one_of):
        raise FlowError("op %r requires one of: %s" % (op, ", ".join(one_of)), path)

    if op in BODY_OPS:
        body = step.get("body")
        if not isinstance(body, list) or not body:
            raise FlowError("op %r requires a non-empty `body` list" % op, path)
        for i, child in enumerate(body):
            _validate_step(child, "%s.body[%d]" % (path, i), depth + 1, counter)
    elif "body" in step:
        raise FlowError("op %r takes no `body`" % op, path)


def _validate_inputs(inputs):
    if not isinstance(inputs, dict):
        raise FlowError("`inputs` must be an object", "inputs")
    for name, spec in inputs.items():
        if not isinstance(spec, dict):
            raise FlowError("input %r must be an object" % name, "inputs")
        t = spec.get("type", "string")
        if t not in ("string", "number", "integer", "boolean"):
            raise FlowError("input %r has unsupported type %r" % (name, t), "inputs")


def validate(flow):
    """Raise FlowError if the document is malformed. Returns the flow unchanged.

    Validates STRUCTURE (ops, required keys, nesting, size) and REFERENCES (every `${var}`
    resolves to a declared input, a loop variable in scope, or a runtime built-in). It does
    NOT touch the browser or the graph — a flow can be rejected before anything is leased."""
    if not isinstance(flow, dict):
        raise FlowError("a flow must be a JSON object")
    for key in ("name", "steps"):
        if key not in flow:
            raise FlowError("missing required key %r" % key)
    if not isinstance(flow["name"], str) or not flow["name"].strip():
        raise FlowError("`name` must be a non-empty string", "name")
    steps = flow["steps"]
    if not isinstance(steps, list) or not steps:
        raise FlowError("`steps` must be a non-empty list", "steps")

    _validate_inputs(flow.get("inputs") or {})

    caps = flow.get("capabilities") or {}
    if not isinstance(caps, dict):
        raise FlowError("`capabilities` must be an object", "capabilities")
    for key in caps:
        if key not in DEFAULT_CAPABILITIES:
            raise FlowError("unknown capability %r (known: %s)"
                            % (key, ", ".join(sorted(DEFAULT_CAPABILITIES))), "capabilities")

    counter = [0]
    for i, step in enumerate(steps):
        _validate_step(step, "steps[%d]" % i, 1, counter)

    _check_references(flow)
    _check_capabilities(flow)
    return flow


def validate_report(doc):
    """validate() as a REPORT instead of an exception — pure, and it never raises.

    The one place a candidate document becomes a verdict:
      ok      -> {"status":"ok","name","host","steps","capabilities","inputs"}
      invalid -> {"status":"invalid","path","error"}

    The ok shape is exactly what `flow_cmd validate` prints and what the HTTP
    /api/flows/validate route returns, so the CLI, the API and the chat agent's
    `propose_flow` tool all answer the same question with the same words. Derived in
    one place on purpose — three hand-written copies of this dict WOULD drift.
    """
    try:
        validate(doc)
    except FlowError as e:
        return {"status": "invalid", "path": e.path, "error": e.message}
    except (TypeError, AttributeError) as e:   # a non-object doc (a list, a string, …)
        return {"status": "invalid", "path": "", "error": str(e)}
    return {"status": "ok", "name": doc["name"], "host": doc.get("host"),
            "steps": len(doc["steps"]), "capabilities": capabilities(doc),
            "inputs": sorted(doc.get("inputs") or {})}


# Variables the runner injects GLOBALLY; a flow may reference these anywhere without
# declaring them. `page` and `index` are deliberately NOT here — they only exist inside a
# `paginate` / `for_each` body, and the runner only ever puts them in scope there. Letting
# them validate anywhere would turn a typo into a RUNTIME FlowError, which the runner turns
# into an abort — i.e. a document that passes `flow validate` and then kills the run.
BUILTIN_VARS = {"run"}

# The variables each body op injects into its own body's scope (on top of its loop variable).
BODY_VARS = {"for_each": {"index"}, "paginate": {"page"}}


def _check_references(flow):
    """Every `${var}` must resolve to a declared input, a loop var in scope, a `set`/`collect`
    var, or a built-in. Catches the typo class of bug at save time rather than at 3am."""
    declared = set(flow.get("inputs") or {})

    def walk(steps, scope, path):
        scope = set(scope)
        for i, step in enumerate(steps):
            here = "%s[%d]" % (path, i)
            for ref in variable_names({k: v for k, v in step.items() if k != "body"}):
                root = ref.split(".")[0]
                if root not in scope:
                    raise FlowError(
                        "references ${%s} but %r is not a declared input, a loop variable "
                        "in scope, or a built-in" % (ref, root), here)
            if step["op"] == "set":
                scope.add(step["var"])
            elif step["op"] == "collect":
                # the runner writes the collection back to `run.vars[into]`, so a later
                # `${into}` is legal — validate() must agree with what the VM does.
                scope.add(step["into"])
            if step["op"] == "for_each":
                walk(step["body"],
                     scope | BODY_VARS["for_each"] | {step.get("as", "item")},
                     here + ".body")
            elif step["op"] == "paginate":
                walk(step["body"], scope | BODY_VARS["paginate"], here + ".body")

    walk(flow["steps"], declared | BUILTIN_VARS, "steps")


# --- graph-resolved targets ----------------------------------------------------
# validate() proves the document's SHAPE. It cannot prove that `{"op":"goto","goal":"reports"}`
# names anything on the actual site — that needs the crawled graph, which this module must
# never read (see the header: pure, stdlib-only, safe to call before a browser is leased).
#
# So the split is: flow.py enumerates WHICH steps resolve against the graph and WHERE they
# live (in the one path grammar this module owns — `steps[1].body[0]`), and flow_resolve.py
# does the graph read. That keeps `mcp_server.propose_flow` — which calls validate_report —
# provably free of disk I/O, while still letting the HTTP validator warn a human.

RESOLVING_OPS = ("goto", "do")     # the ops whose target the runner resolves via the graph


def goal_targets(flow):
    """Every step the runner will RESOLVE AGAINST THE GRAPH, in document order.

    Pure: a walk of the document, no graph, no disk. Returns
    `[{"path","op","goal","match","start","index"}, …]` where `path` is this module's own
    grammar (`steps[1].body[0]`) — the same string validate() puts in a FlowError and the
    same string the canvas uses as a box's `data-path`.

    Skipped on purpose:
      * `goto` with a `url` — the runner short-circuits to the URL and never consults the graph.
      * a goal/match carrying `${…}` — it is only known at run time, so there is nothing to
        check now; warning about it would be noise, not signal.
    """
    out = []

    def walk(steps, path):
        if not isinstance(steps, list):
            return
        for i, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            here = "%s[%d]" % (path, i)
            op = step.get("op")
            if op in RESOLVING_OPS and not step.get("url"):
                goal = step.get("goal") if isinstance(step.get("goal"), str) else None
                match = step.get("match") if isinstance(step.get("match"), str) else None
                if (goal or match) and not _VAR_RE.search(" ".join(
                        x for x in (goal, match) if x)):
                    out.append({
                        "path": here, "op": op, "goal": goal, "match": match,
                        "start": step["start"] if isinstance(step.get("start"), str) else None,
                        "index": step["index"] if isinstance(step.get("index"), int) else 0,
                    })
            if op in BODY_OPS:
                walk(step.get("body"), here + ".body")

    walk(flow.get("steps") if isinstance(flow, dict) else None, "steps")
    return out


def _check_capabilities(flow):
    """A flow must DECLARE the writes it performs. Refusing at validation time (not at
    step time, halfway through) means a scheduled run never half-executes."""
    caps = capabilities(flow)
    used = set()

    def walk(steps):
        for step in steps:
            if step["op"] in WRITE_OPS:
                used.add(step["op"])
            if step["op"] == "do" and step.get("submit"):
                used.add("submit")
            for child in step.get("body") or []:
                walk([child])

    walk(flow["steps"])
    if "submit" in used and not caps["allow_submit"]:
        raise FlowError("a step submits a form, but capabilities.allow_submit is false")
    if "upload" in used and not caps["allow_upload"]:
        raise FlowError("a step uploads a file, but capabilities.allow_upload is false")


# --- input binding -------------------------------------------------------------

_COERCE = {"string": str, "number": float, "integer": int,
           "boolean": lambda v: v if isinstance(v, bool) else str(v).lower() == "true"}


def bind_inputs(flow, values):
    """Validate + coerce caller-supplied values against the flow's `inputs` declaration.
    This is the boundary an HTTP request body crosses, so it rejects unknown keys rather
    than silently ignoring them (a typo'd param must not run the flow with a default)."""
    spec = flow.get("inputs") or {}
    values = dict(values or {})
    unknown = set(values) - set(spec)
    if unknown:
        raise FlowError("unknown input(s): %s" % ", ".join(sorted(unknown)), "inputs")
    bound = {}
    for name, decl in spec.items():
        if name in values:
            try:
                bound[name] = _COERCE[decl.get("type", "string")](values[name])
            except (TypeError, ValueError):
                raise FlowError("input %r is not a valid %s"
                                % (name, decl.get("type", "string")), "inputs")
        elif "default" in decl:
            bound[name] = decl["default"]
        elif decl.get("required"):
            raise FlowError("missing required input %r" % name, "inputs")
        else:
            bound[name] = None
    return bound


def json_schema(flow):
    """The flow's `inputs` as a JSON Schema — this is what lets a saved flow become a typed
    HTTP endpoint and an MCP tool with no hand-written wrapper. The crawler already read the
    form's field specs, so the schema is derived, never authored."""
    spec = flow.get("inputs") or {}
    props, required = {}, []
    for name, decl in spec.items():
        p = {"type": decl.get("type", "string")}
        if decl.get("description"):
            p["description"] = decl["description"]
        if decl.get("enum"):
            p["enum"] = decl["enum"]
        if "default" in decl:
            p["default"] = decl["default"]
        props[name] = p
        if decl.get("required"):
            required.append(name)
    schema = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        schema["required"] = sorted(required)
    return schema


# --- loading -------------------------------------------------------------------

def load(path):
    """Read + validate a flow document from disk."""
    with open(path) as fh:
        try:
            flow = json.load(fh)
        except ValueError as exc:
            raise FlowError("not valid JSON: %s" % exc)
    return validate(flow)


def loads(text):
    try:
        flow = json.loads(text)
    except ValueError as exc:
        raise FlowError("not valid JSON: %s" % exc)
    return validate(flow)
