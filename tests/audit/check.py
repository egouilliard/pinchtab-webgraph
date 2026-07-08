#!/usr/bin/env python3
"""Browser-free scorer for the 10-site regression audit (Phase 0).

Given a directory of crawled interaction-graph JSONs (one per site, named <name>.json)
and `sites.json`, it:
  1. computes the Phase-1 acceptance metric `dup_ratio` (# states that share a
     normalized URL — MUST be 0 in nav mode) + a base-path spread report;
  2. runs every site's hard-question goals through the OFFLINE query API
     (`pinchtab_webgraph.api.howto`, no browser) and scores them against `expect`;
  3. prints a per-site + aggregate table and exits non-zero per `--gate`.

`--gate dup`  (default): fail only if any site has dup_ratio > 0. This is the Phase-1
              gate; the goal scoreboard is INFORMATIONAL (it rises as later phases land).
`--gate full`: additionally fail if the goal score dropped below `--baseline`.

The graphs are produced by `scripts/site-audit.sh` (which needs a live pinchtab bridge);
this scorer itself is pure and unit-tested (see tests/audit/test_check.py).
"""
import argparse
import collections
import json
import os
import sys

# import the real project code (nav-mode URL normalization + the offline query API)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from pinchtab_webgraph import api                       # noqa: E402
from pinchtab_webgraph.interaction_crawl import norm    # noqa: E402


def dup_ratio(graph):
    """# of states beyond one-per-normalized-URL. 0 == no over-noding (the Phase-1 gate)."""
    states = graph.get("states", [])
    urls = [norm(s.get("url", "")) for s in states]
    return len(states) - len(set(urls))


def base_path_spread(graph):
    c = collections.Counter((s.get("url", "").split("?")[0]) for s in graph.get("states", []))
    return len(c)


def _first_result(res):
    return (res.get("results") or [None])[0]


def score_goal(graph_path, goal_spec, default_start=None):
    """Return (passed: bool, note: str) for one goal against api.howto (browser-free)."""
    exp = goal_spec.get("expect", {})
    want = exp.get("status", "ok")
    res = api.howto(graph_path, goal=goal_spec["goal"],
                    start=goal_spec.get("start") or default_start)
    got = res.get("status")
    # REFUSAL cases: we asked for a miss (a false-positive trap). ok when the tool declines.
    if want == "no_match":
        return (got in ("no_match", "unreachable"), "want no_match, got %s" % got)
    if got != "ok":
        return (False, "want ok, got %s" % got)
    r = _first_result(res)
    if r is None:
        return (False, "ok but no result")
    # optional structural checks
    uc = exp.get("url_contains")
    if uc and uc not in (r.get("state_url") or "") and uc not in (r.get("opens_at") or ""):
        return (False, "url lacks %r (got %s)" % (uc, r.get("state_url")))
    mf = exp.get("min_fields")
    if mf is not None:
        n = len(((r.get("form") or {}).get("fields")) or [])
        if n < mf:
            return (False, "fields %d < %d" % (n, mf))
    return (True, "ok")


def main():
    ap = argparse.ArgumentParser(description="Score the 10-site audit (browser-free).")
    ap.add_argument("--graphs", required=True, help="dir of crawled <name>.json graphs")
    ap.add_argument("--sites", default=os.path.join(os.path.dirname(__file__), "sites.json"))
    ap.add_argument("--gate", choices=("dup", "full"), default="dup")
    ap.add_argument("--baseline", type=int, default=0, help="--gate full: min goals to pass")
    args = ap.parse_args()

    sites = json.load(open(args.sites))["sites"]
    rows, total_goals, total_pass, dup_fail = [], 0, 0, False
    print("%-20s %8s %7s %8s   %s" % ("site", "st/uniq", "dup", "spread", "goals"))
    print("-" * 66)
    for site in sites:
        name = site["name"]
        gpath = os.path.join(args.graphs, name + ".json")
        if not os.path.exists(gpath):
            print("%-20s   %s" % (name, "MISSING GRAPH (crawl not run) — skipped"))
            continue
        graph = json.load(open(gpath))
        n_states = len(graph.get("states", []))
        dr = dup_ratio(graph)
        spread = base_path_spread(graph)
        if dr > 0:
            dup_fail = True
        gp = gt = 0
        notes = []
        for gs in site.get("goals", []):
            gt += 1
            ok, note = score_goal(gpath, gs, default_start=site.get("start_url"))
            gp += 1 if ok else 0
            if not ok:
                notes.append("  ✗ %-28s %s" % (gs["goal"][:28], note))
        total_goals += gt
        total_pass += gp
        flag = "  DUP>0!" if dr > 0 else ""
        print("%-20s %3d/%-4d %7d %8d   %d/%d%s" % (name, n_states, spread, dr, spread, gp, gt, flag))
        rows.append((name, notes))

    print("-" * 66)
    print("AGGREGATE goals: %d/%d   dup_ratio_ok: %s" %
          (total_pass, total_goals, "NO" if dup_fail else "yes"))
    for name, notes in rows:
        if notes:
            print("\n%s:" % name)
            print("\n".join(notes))

    if args.gate == "dup":
        sys.exit(1 if dup_fail else 0)
    # full gate: dup must be clean AND goal score >= baseline
    sys.exit(1 if (dup_fail or total_pass < args.baseline) else 0)


if __name__ == "__main__":
    main()
