#!/usr/bin/env python3
"""
Run / validate a declarative automation FLOW — the CLI over `flow.py` + `runner.py`.

A flow is a JSON document (see flow.py) executed by the step VM against a live PinchTab
browser. This module is the thin edge: parse args, resolve the graph, build the ports
(browser, artifact store), hand them to `runner.execute`, print what happened.

  pinchtab-webgraph flow validate ./invoices.json
  pinchtab-webgraph flow schema   ./invoices.json          # inputs as JSON Schema
  pinchtab-webgraph flow run      ./invoices.json --host app.example.com --dry-run
  pinchtab-webgraph flow run      ./invoices.json --graph out/app.json \
      --input since=2026-01-01 --allow-submit

SAFE BY DEFAULT — a write happens only when the flow DECLARES the capability *and* this CLI
GRANTS it (`--allow-submit` / `--allow-upload`); `--no-allow-download` withdraws the one
capability that is on by default. `--dry-run` prints exactly what would run and touches
nothing (no browser command, no artifact directory).

`--host`/`--graph` are OPTIONAL here (unlike `perform`): a flow made of explicit `goto{url}`
steps needs no crawled graph at all. It is needed the moment a step resolves a `goal`, and
the runner says so plainly if it is missing.

Exit codes: 0 the run finished ok · 1 a step errored / the flow was rejected · 2 a usage or
environment error (bad --host, no cache).
"""
import argparse
import json
import os
import sys

from . import artifacts, browser as browser_mod, flow as flow_mod, perform, runner

DEFAULT_SERVER = "http://localhost:9871"

# The same marks perform.py prints, so one run of a how-to and one run of a flow look alike.
_MARKS = {"ok": "✓", "new": "✓", "triggered": "✓", "dupe": "=", "dry-run": "·",
          "page": "·", "started": "▸", "skipped": "–", "error": "✗", "aborted": "✗"}
# The fields worth showing on a progress line, in the order a human reads them.
_DETAIL = ("flow", "goal", "match", "url", "target", "name", "href", "selector", "label",
           "value", "into", "message", "page", "pages", "found", "via", "size", "filled",
           "note", "reason", "error")


def _describe(event):
    """One progress line for an emitted event. Structural — no per-op special-casing."""
    bits = []
    for key in _DETAIL:
        val = event.get(key)
        if val in (None, "", [], {}):
            continue
        if isinstance(val, (list, dict)):
            val = json.dumps(val)
        bits.append("%s=%s" % (key, val))
    mark = _MARKS.get(event.get("status"), " ")
    return "  %s %-9s %s" % (mark, event["op"], " ".join(bits))


def _default_scope(name):
    """The artifact scope a flow gets when the caller doesn't pick one: its own name, so two
    flows never poison each other's dedupe ledger. Sanitized because a flow name is free text
    and a scope is a directory segment (artifacts.validate_scope is the real gate)."""
    safe = "".join(c if (c.isalnum() or c in "._-") else "-" for c in (name or ""))
    return safe.strip("-.") or "default"


def _resolve_graph(a):
    """(graph_path, error). Mirrors perform.main()'s resolution — but a flow may legitimately
    have NO graph (only explicit urls/selectors), so `None, None` is a valid answer."""
    if a.graph:
        return a.graph, None
    if not a.host:
        return None, None
    from . import cache_store
    try:
        path = cache_store.cache_path(a.host)
    except ValueError:
        return None, "invalid --host %r" % a.host
    if not os.path.exists(path):
        return None, ("no cache for %s yet — crawl it first "
                      "(pinchtab-webgraph crawl / ask)." % a.host)
    return path, None


def _build_parser():
    ap = argparse.ArgumentParser(
        description="Run or validate a declarative automation FLOW document.")
    sub = ap.add_subparsers(dest="op", required=True, metavar="OP")

    p = sub.add_parser("validate", help="structurally validate a flow document (JSON out)")
    p.add_argument("path", help="path to the flow JSON document")

    p = sub.add_parser("schema", help="print the flow's `inputs` as a JSON Schema")
    p.add_argument("path", help="path to the flow JSON document")

    p = sub.add_parser("run", help="execute a flow against a live browser")
    p.add_argument("path", help="path to the flow JSON document")
    src = p.add_mutually_exclusive_group()      # NOT required: a url-only flow needs no graph
    src.add_argument("--host", help="resolve goals against this host's cache")
    src.add_argument("--graph", help="resolve goals against an explicit interaction-graph JSON")
    p.add_argument("--input", action="append", metavar="NAME=VALUE", default=[],
                   help="supply a declared input (repeatable)")
    p.add_argument("--allow-submit", action="store_true",
                   help="permit a form SUBMIT (the flow must declare it too)")
    p.add_argument("--allow-upload", action="store_true",
                   help="permit a file UPLOAD (the flow must declare it too)")
    p.add_argument("--no-allow-download", action="store_true",
                   help="withdraw the download capability (on by default)")
    p.add_argument("--scope", help="artifact scope / dedupe ledger (default: the flow's name)")
    p.add_argument("--artifacts-root", help="store artifacts under this directory instead")
    p.add_argument("--dry-run", action="store_true",
                   help="print exactly what WOULD run; touch nothing")
    p.add_argument("--server", default=DEFAULT_SERVER,
                   help="PinchTab bridge (default %(default)s)")
    p.add_argument("--config", default=os.environ.get("PINCHTAB_CONFIG", "crawl-config.json"),
                   help="crawl-config.json — read the bridge token from it")
    p.add_argument("--json", action="store_true", help="emit the full run record as JSON")
    p.add_argument("--jsonl", action="store_true",
                   help="STREAM the run as JSON Lines: one {\"type\":\"step\",…} object per "
                        "emitted event, then exactly one {\"type\":\"result\",…}. For a "
                        "supervising process (the web UI) that wants progress as it happens.")
    return ap


# --- ops -----------------------------------------------------------------------

def _op_validate(a):
    try:
        doc = flow_mod.load(a.path)
    except flow_mod.FlowError as exc:
        print(json.dumps({"status": "invalid", "path": exc.path, "error": exc.message},
                         indent=2))
        return 1
    except OSError as exc:
        print(json.dumps({"status": "invalid", "path": a.path, "error": str(exc)}, indent=2))
        return 1
    print(json.dumps({"status": "ok", "name": doc["name"], "host": doc.get("host"),
                      "steps": len(doc["steps"]),
                      "capabilities": flow_mod.capabilities(doc),
                      "inputs": sorted(doc.get("inputs") or {})}, indent=2))
    return 0


def _op_schema(a):
    try:
        doc = flow_mod.load(a.path)
    except flow_mod.FlowError as exc:
        print(json.dumps({"status": "invalid", "path": exc.path, "error": exc.message},
                         indent=2))
        return 1
    except OSError as exc:
        print(json.dumps({"status": "invalid", "path": a.path, "error": str(exc)}, indent=2))
        return 1
    print(json.dumps(flow_mod.json_schema(doc), indent=2))
    return 0


# --- streaming (--jsonl) -------------------------------------------------------
#
# One JSON object per line, flushed EVERY line. The flush is mandatory, not hygiene: stdout
# to a PIPE is block-buffered, so without it a supervising process (the web UI) would see
# nothing at all for the whole run and then a single burst at exit — which is precisely the
# opposite of streaming, and would make a 10-minute paginate loop look like a hang.

def _emit_jsonl(frame):
    print(json.dumps(frame), flush=True)


def _reject(a, path, error):
    """The ONE rejection printer. In --jsonl mode a rejection is still the run's single
    terminal `result` frame, so a supervisor never has to special-case "it died before it
    started" — it reads exactly one result line either way. Exit code is unchanged (1)."""
    if a.jsonl:
        _emit_jsonl({"type": "result", "status": "invalid", "path": path, "error": error})
    else:
        print(json.dumps({"status": "invalid", "path": path, "error": error}, indent=2))
    return 1


def _first_goto_url(doc):
    """The flow's first literal `goto {url}`, if it opens with one — the url a fresh (zero-tab)
    bridge can open its first tab at, so the run's very first command already targets a live
    tab instead of paying for browser.nav()'s self-heal round-trip. Generic: no site knowledge,
    and None whenever the answer isn't cheap or certain — a templated url ({{...}}) is not
    interpolated yet, and a `goal`/`match` goto needs the graph. None is always safe."""
    for step in (doc.get("steps") or []):
        if not isinstance(step, dict):
            continue
        url = step.get("url") if step.get("op") == "goto" else None
        if isinstance(url, str) and url and "{{" not in url:
            return url
        return None            # only the FIRST step can position the first tab
    return None


def _op_run(a):
    try:
        doc = flow_mod.load(a.path)
    except flow_mod.FlowError as exc:
        return _reject(a, exc.path, exc.message)
    except OSError as exc:
        return _reject(a, a.path, str(exc))

    graph_path, err = _resolve_graph(a)
    if err:
        print(err, file=sys.stderr)
        return 2

    try:
        inputs = flow_mod.bind_inputs(doc, perform._parse_set(a.input))
    except flow_mod.FlowError as exc:
        return _reject(a, exc.path, exc.message)

    # The CALLER's grant. The runner ANDs it with what the flow declares — either side vetoes.
    grant = {"allow_submit": a.allow_submit, "allow_upload": a.allow_upload,
             "allow_download": not a.no_allow_download}

    token = perform.load_token(a.config)
    tab = None if a.dry_run else browser_mod.resolve_tab(a.server, token, _first_goto_url(doc))
    live = browser_mod.PinchTabBrowser(a.server, token, tab)

    # A dry run touches NOTHING — not even an artifact directory (the store would mkdir).
    store = None
    if not a.dry_run:
        scope = a.scope or _default_scope(doc["name"])
        try:
            root = (os.path.join(a.artifacts_root, artifacts.validate_scope(scope))
                    if a.artifacts_root else None)   # keep the ledger per-scope either way
            store = artifacts.ArtifactStore(scope=scope, root=root)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    if a.jsonl:
        emit = lambda ev: _emit_jsonl({"type": "step", **ev})  # noqa: E731
    elif a.json:
        emit = None
    else:
        emit = lambda ev: print(_describe(ev))                 # noqa: E731
    # the human banner would corrupt both machine-readable modes.
    if not a.json and not a.jsonl:
        print("=== FLOW: %s ===  (%s)"
              % (doc["name"].upper(), "WOULD run" if a.dry_run else "live"))

    result = runner.execute(doc, browser=live, graph_path=graph_path, store=store,
                            inputs=inputs, emit=emit, dry_run=a.dry_run, grant=grant)

    if a.jsonl:
        # EXACTLY ONE terminal frame, flushed — the supervisor's cue that the run is over.
        _emit_jsonl({"type": "result", **result})
    elif a.json:
        print(json.dumps(result, indent=2))
    else:
        stats = result["stats"]
        if result.get("aborted"):
            print("  ✗ ABORTED — %s" % result["aborted"])
        print("--- %s: %d steps, %d new file(s), %d duplicate(s), %ss"
              % (result["status"], stats["steps_executed"], stats["artifacts_new"],
                 stats["artifacts_dupe"], result["duration_s"]))
        if store:
            print("    artifacts: %s" % store.root)
    return 0 if result["status"] == "ok" else 1


def main():
    a = _build_parser().parse_args()
    if a.op == "validate":
        return _op_validate(a)
    if a.op == "schema":
        return _op_schema(a)
    return _op_run(a)


if __name__ == "__main__":
    sys.exit(main() or 0)
