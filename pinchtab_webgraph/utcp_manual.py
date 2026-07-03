#!/usr/bin/env python3
"""
Build / print / serve the UTCP tool-calling manual for external tool-callers.

[UTCP](https://www.utcp.io) lets a tool-caller invoke a plain CLI directly, with no
wrapper protocol running: the manual describes each tool's JSON-schema inputs/outputs
and the exact `pwg …` command string to run, with arguments injected via the literal
token `UTCP_ARG_<name>_UTCP_END`. This module emits that manual — a pure-stdlib dict
that mirrors the `query` (and `crawl` / `ask`) command surface.

  pinchtab-webgraph manual                       # print the manual JSON to stdout
  pinchtab-webgraph manual --out utcp-manual.json # write it to a file (no stdout)
  pinchtab-webgraph manual --serve               # serve it at /utcp + /.well-known/utcp
  pinchtab-webgraph manual --serve --port 9872   # (default 127.0.0.1:9872)

The exposed tool surface is a deliberate SUBSET: only required core args (so every
command string is free of optional placeholders), and query tools route by `--host`
only. Manual generation is pure stdlib; the `[utcp]` extra is only for the
importorskip-gated test that validates this manual against the real UTCP model.

Generic + stdlib only: routing is by hostname, nothing app-specific.
"""
import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__

# The UTCP protocol version this manual targets (validated against utcp 1.1.x).
UTCP_VERSION = "1.1"

# Shared input descriptor: the one arg every offline query tool exposes.
_HOST_ARG = ("host", "hostname to route through the per-host cache (e.g. app.example.com)")


def _tool(name, description, tags, args, command, status_enum=None, extra_out=None):
    """Assemble one UTCP Tool dict (all `args` are REQUIRED inputs).

    `args` is a list of (arg_name, arg_description); `command` is the `pwg …` string
    with each arg injected as `UTCP_ARG_<arg_name>_UTCP_END`. The call template
    carries its OWN `name` (UTCP requires it) and one CommandStep object.
    """
    inputs = {
        "type": "object",
        "properties": {an: {"type": "string", "description": d} for an, d in args},
        "required": [an for an, _ in args],
    }
    out_props = {}
    if status_enum is not None:
        out_props["status"] = {"type": "string", "enum": list(status_enum)}
    if extra_out:
        out_props.update(extra_out)
    outputs = {"type": "object", "properties": out_props}
    tool_call_template = {
        "name": name,
        "call_template_type": "cli",
        "commands": [{"command": command, "append_to_final_output": True}],
    }
    return {
        "name": name,
        "description": description,
        "inputs": inputs,
        "outputs": outputs,
        "tags": tags,
        "tool_call_template": tool_call_template,
    }


# --- the 6 offline query tools (share the `_tool` code path) ------------------

_OFFLINE = " Answers OFFLINE from the per-host cache (no browser, no network)."

_QUERY_TOOLS = [
    dict(
        name="graph_summary",
        description="Detect a cached graph's kind and return its meta + element counts."
        + _OFFLINE + " Returns JSON with `graph_kind` in {interaction, link, unknown}.",
        tags=["offline", "graph", "summary"],
        args=[_HOST_ARG],
        command="pwg query graph_summary --host UTCP_ARG_host_UTCP_END",
        status_enum=None,
        extra_out={"graph_kind": {"type": "string",
                                  "enum": ["interaction", "link", "unknown"]},
                   "meta": {"type": "object"}},
    ),
    dict(
        name="howto",
        description="Shortest click-path(s) to a create-trigger matching a natural-language "
        "goal, plus the fields of the form it opens." + _OFFLINE
        + " Returns JSON with a `status` in {ok, no_match, unreachable, invalid_args}.",
        tags=["offline", "howto", "path"],
        args=[_HOST_ARG, ("goal", "natural-language goal, e.g. \"create role\"")],
        command="pwg query howto --host UTCP_ARG_host_UTCP_END --goal UTCP_ARG_goal_UTCP_END",
        status_enum=["ok", "no_match", "unreachable", "invalid_args"],
        extra_out={"results": {"type": "array"}, "candidates": {"type": "array"}},
    ),
    dict(
        name="find_content",
        description="Search every captured data collection for text and route each matching "
        "view (what matched, which view, the click-path to it)." + _OFFLINE
        + " Returns JSON with a `status` in {ok, no_match}.",
        tags=["offline", "content", "search"],
        args=[_HOST_ARG, ("text", "text to search for across captured collections")],
        command="pwg query find_content --host UTCP_ARG_host_UTCP_END --text UTCP_ARG_text_UTCP_END",
        status_enum=["ok", "no_match"],
        extra_out={"views": {"type": "array"}},
    ),
    dict(
        name="list_content",
        description="Per-view inventory of captured data collections (kinds, counts, a sample)."
        + _OFFLINE + " Returns JSON with a `status` in {ok, empty}.",
        tags=["offline", "content", "inventory"],
        args=[_HOST_ARG],
        command="pwg query list_content --host UTCP_ARG_host_UTCP_END",
        status_enum=["ok", "empty"],
        extra_out={"views": {"type": "array"}},
    ),
    dict(
        name="list_forms",
        description="Every create-form in the cache: label, host, click-depth, field count."
        + _OFFLINE + " Returns JSON `{meta, forms}` (no `status` key on success).",
        tags=["offline", "forms", "inventory"],
        args=[_HOST_ARG],
        command="pwg query list_forms --host UTCP_ARG_host_UTCP_END",
        status_enum=None,
        extra_out={"meta": {"type": "object"}, "forms": {"type": "array"}},
    ),
    dict(
        name="link_paths",
        description="Shortest / all click-paths between two pages of a crawled LINK graph, "
        "matched by URL/title substring." + _OFFLINE + " Returns JSON with a `status` in "
        "{ok, no_path, not_found_from, not_found_to, ambiguous_from, ambiguous_to}.",
        tags=["offline", "paths", "link-graph"],
        args=[_HOST_ARG, ("frm", "source page (URL / title substring)"),
              ("to", "target page (URL / title substring)")],
        command="pwg query link_paths --host UTCP_ARG_host_UTCP_END "
        "--from UTCP_ARG_frm_UTCP_END --to UTCP_ARG_to_UTCP_END",
        status_enum=["ok", "no_path", "not_found_from", "not_found_to",
                     "ambiguous_from", "ambiguous_to"],
        extra_out={"shortest": {"type": "object"}, "all_paths": {"type": "array"},
                   "candidates": {"type": "array"}},
    ),
]

# --- the 2 live tools (need a running PinchTab bridge) ------------------------

_LIVE_NARRATED = {"type": "string",
                  "description": "narrated text output (crawl/ask emit human-readable "
                                 "progress + result today, not structured JSON)"}

_LIVE_TOOLS = [
    dict(
        name="crawl",
        description="Crawl a site ONCE into its per-host interaction+content graph cache "
        "(states, action edges, forms, data). Needs a running PinchTab bridge; drives a "
        "real browser. Emits narrated progress.",
        tags=["live", "crawl", "browser"],
        args=[("start", "full start URL including a scheme, e.g. https://app.example.com/home")],
        command="pwg crawl --start UTCP_ARG_start_UTCP_END",
        status_enum=None,
        extra_out=None,
    ),
    dict(
        name="ask",
        description="Cache-first how-to: answer offline if cached, else run a live discovery "
        "through the browser and write the result back. Needs a running PinchTab bridge on a "
        "cache miss. Emits narrated text.",
        tags=["live", "howto", "browser"],
        args=[("start", "full start URL including a scheme, e.g. https://app.example.com/home"),
              ("goal", "natural-language goal, e.g. \"create role\"")],
        command="pwg ask --start UTCP_ARG_start_UTCP_END --goal UTCP_ARG_goal_UTCP_END",
        status_enum=None,
        extra_out=None,
    ),
]


def build_manual():
    """Return the UTCP manual as a plain dict.

    Shape is EXACTLY {utcp_version, manual_version, tools} — no `info` field.
    `manual_version` is the single-source-of-truth package `__version__`.
    """
    tools = []
    for d in _QUERY_TOOLS + _LIVE_TOOLS:
        out = _tool(d["name"], d["description"], d["tags"], d["args"], d["command"],
                    status_enum=d["status_enum"], extra_out=d["extra_out"])
        # live tools emit narrated text today, not a structured object.
        if d in _LIVE_TOOLS:
            out["outputs"] = dict(_LIVE_NARRATED)
        tools.append(out)
    return {"utcp_version": UTCP_VERSION, "manual_version": __version__, "tools": tools}


# --- HTTP serving (pure-routing seam is testable without a socket) ------------

def _route(path):
    """Map a GET path to (status_code, body_bytes, content_type). No socket involved."""
    clean = path.split("?", 1)[0]
    if clean in ("/utcp", "/.well-known/utcp"):
        return 200, json.dumps(build_manual()).encode(), "application/json"
    return 404, b'{"error":"not found","hint":"GET /utcp"}', "application/json"


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        code, body, ctype = _route(self.path)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # keep the server quiet; startup line is printed by _serve


def _serve(host, port):
    srv = ThreadingHTTPServer((host, port), _Handler)
    print("serving UTCP manual on http://%s:%d/utcp (also /.well-known/utcp) — Ctrl-C to stop"
          % (host, port))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping.")
    finally:
        srv.server_close()
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="Print / write / serve the UTCP tool-calling manual.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--out", metavar="FILE",
                      help="write the manual JSON to FILE (no stdout)")
    mode.add_argument("--serve", action="store_true",
                      help="serve the manual over HTTP (blocking) at /utcp + /.well-known/utcp")
    ap.add_argument("--host", default="127.0.0.1", help="--serve bind host (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=9872, help="--serve bind port (default 9872)")
    a = ap.parse_args()

    if a.serve:
        return _serve(a.host, a.port)

    text = json.dumps(build_manual(), indent=2)
    if a.out:
        with open(os.path.expanduser(a.out), "w") as f:
            f.write(text + "\n")
        return 0
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
