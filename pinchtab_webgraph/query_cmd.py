#!/usr/bin/env python3
"""
Query a cached / on-disk graph OFFLINE and print the result as JSON on stdout.

This is the machine-readable twin of `howto.py` / `paths.py`: it binds the same
print-free `api.*` query surface the MCP server binds, but as a plain argparse
subcommand whose ONLY output is `json.dumps(result, indent=2)`. It is the substrate
the UTCP manual's command strings shell out to.

  pinchtab-webgraph query graph_summary --host app.example.com
  pinchtab-webgraph query howto        --host app.example.com --goal "create role"
  pinchtab-webgraph query find_content --graph app.json --text "invoice"
  pinchtab-webgraph query list_content --host app.example.com
  pinchtab-webgraph query list_forms   --graph app.json
  pinchtab-webgraph query link_paths   --graph docs.json --from home --to guide

Every op takes EXACTLY ONE of `--host` (route through the per-host cache) or
`--graph` (an explicit graph-file path); that pair is a REQUIRED mutually-exclusive
group. Output is ALWAYS JSON on stdout, never stderr.

Exit codes:
  0  any api-level result, INCLUDING a structured miss (no_match / unreachable /
     empty / no_path / not_found_* / ambiguous_* / invalid_args) — the JSON carries
     the `status`; the miss is not an error.
  1  a resolver / environment error (invalid_host / no_cache_for_host /
     invalid_graph) — the JSON is STILL printed to stdout, not stderr.
  2  an argparse usage error (missing required flag, or neither/both --host/--graph).

Generic + stdlib only: routing is by hostname, nothing app-specific.
"""
import argparse
import json
import os
import sys

from . import api, cache_store

# Resolver / environment errors → exit 1. NOTE: `invalid_args` is deliberately NOT
# here — it is an api-level structured miss (howto with neither goal nor match) and
# must exit 0 with its JSON on stdout.
_RESOLVER_ERRORS = {"invalid_host", "no_cache_for_host", "invalid_graph"}


# keep in sync with mcp_server.py:_resolve_graph/_call
def _resolve_graph(host=None, graph=None):
    """Resolve exactly one of host/graph to a graph-file path.

    Returns (path, None) on success, or (None, error_dict) where error_dict has a
    `status` in {invalid_args, invalid_host, no_cache_for_host}.
    """
    if (host is None) == (graph is None):
        return None, {"status": "invalid_args",
                      "detail": "pass exactly one of --host or --graph",
                      "host": host, "graph": graph}
    if host is not None:
        try:
            path = cache_store.cache_path(host)
        except ValueError:
            return None, {"status": "invalid_host", "host": host}
        if not os.path.exists(path):
            return None, {"status": "no_cache_for_host", "host": host,
                          "caches_dir": cache_store.caches_dir()}
        return path, None
    return graph, None


# keep in sync with mcp_server.py:_resolve_graph/_call
def _call(fn, host, graph, **kwargs):
    """Resolve host/graph then call an api.* fn, mapping load/parse errors to a status."""
    path, err = _resolve_graph(host, graph)
    if err is not None:
        return err
    try:
        return fn(path, **kwargs)
    except (OSError, ValueError, json.JSONDecodeError, KeyError) as e:
        return {"status": "invalid_graph", "path": path, "error": str(e)}


def _build_parser():
    # Shared by every op: the required host/graph choice + a forward-compat --json.
    parent = argparse.ArgumentParser(add_help=False)
    grp = parent.add_mutually_exclusive_group(required=True)
    grp.add_argument("--host", help="route through the per-host interaction-graph cache")
    grp.add_argument("--graph", help="explicit graph-file path")
    parent.add_argument("--json", action="store_true",
                        help="emit JSON (accepted no-op — output is ALWAYS JSON; "
                             "kept explicit/forward-compat)")

    ap = argparse.ArgumentParser(
        description="Query a graph OFFLINE and print the result as JSON on stdout.")
    sub = ap.add_subparsers(dest="op", required=True, metavar="OP")

    sub.add_parser("graph_summary", parents=[parent],
                   help="graph kind + meta + element counts")

    p = sub.add_parser("howto", parents=[parent],
                       help="shortest click-path(s) to a create-trigger + its form")
    p.add_argument("--goal", help="natural-language goal (e.g. \"create role\")")
    p.add_argument("--start", help="pin the start state (URL / label substring)")
    p.add_argument("--match", help="regex over trigger labels (instead of --goal)")
    p.add_argument("--all", action="store_true",
                   help="return every routed match, not just the shortest")

    p = sub.add_parser("find_content", parents=[parent],
                       help="search captured data collections for text; route each match")
    p.add_argument("--text", required=True, help="text to search for (required)")
    p.add_argument("--start", help="pin the start state for routing")
    p.add_argument("--limit", type=int, default=40, help="cap returned items (default 40)")

    sub.add_parser("list_content", parents=[parent],
                   help="per-view inventory of captured collections")

    p = sub.add_parser("find_content_hosts",
                       help="cross-host content search across ALL cached hosts")
    p.add_argument("--text", required=True, help="text to search for (required)")
    p.add_argument("--limit", type=int, default=40, help="cap merged items (default 40)")

    sub.add_parser("list_content_hosts",
                   help="cross-host collection inventory across ALL cached hosts")

    sub.add_parser("list_forms", parents=[parent],
                   help="every create-form: label, host, click-depth, field count")

    p = sub.add_parser("link_paths", parents=[parent],
                       help="shortest / all click-paths between two pages of a link graph")
    p.add_argument("--from", dest="frm", required=True,
                   help="source page (URL / title substring, required)")
    p.add_argument("--to", required=True, help="target page (URL / title substring, required)")
    p.add_argument("--structural", action="store_true",
                   help="drop global-nav (hub) edges")
    p.add_argument("--all", action="store_true",
                   help="also return every path up to --max-len")
    p.add_argument("--max-len", type=int, default=5, help="max path length for --all (default 5)")
    p.add_argument("--max-paths", type=int, default=50,
                   help="cap on returned paths for --all (default 50)")
    return ap


def _dispatch(a):
    op = a.op
    # Cross-host ops enumerate every cached host; they take neither --host nor --graph
    # (those attrs don't exist on their subparsers), so handle them BEFORE reading them.
    if op == "find_content_hosts":
        hp = [(h, cache_store.cache_path(h)) for h in cache_store.list_hosts()]
        return api.find_content_hosts(hp, text=a.text, limit=a.limit)
    if op == "list_content_hosts":
        hp = [(h, cache_store.cache_path(h)) for h in cache_store.list_hosts()]
        return api.list_content_hosts(hp)
    host, graph = a.host, a.graph
    if op == "graph_summary":
        return _call(api.graph_summary, host, graph)
    if op == "howto":
        return _call(api.howto, host, graph, goal=a.goal, start=a.start,
                     match=a.match, all=a.all)
    if op == "find_content":
        return _call(api.find_content, host, graph, text=a.text, start=a.start,
                     limit=a.limit)
    if op == "list_content":
        return _call(api.list_content, host, graph)
    if op == "list_forms":
        return _call(api.list_forms, host, graph)
    # link_paths
    return _call(api.link_paths, host, graph, frm=a.frm, to=a.to,
                 structural=a.structural, all=a.all, max_len=a.max_len,
                 max_paths=a.max_paths)


def main():
    # Zero-arg: argparse reads sys.argv (a usage error exits 2 via SystemExit).
    a = _build_parser().parse_args()
    result = _dispatch(a)
    print(json.dumps(result, indent=2))
    # A resolver/env error still prints its JSON to stdout, but exits nonzero.
    return 1 if result.get("status") in _RESOLVER_ERRORS else 0


if __name__ == "__main__":
    sys.exit(main() or 0)
