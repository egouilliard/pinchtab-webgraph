#!/usr/bin/env python3
"""
Answer "how do I do X?" OFFLINE, in milliseconds, from an interaction-graph cache
built by interaction_crawl.py. No live browser, no discovery budget, no per-state
latency — just a BFS over the cached graph.

  python3 howto.py interaction-graph.json --goal "create team"
  python3 howto.py interaction-graph.json --goal "add template" --start https://app/x
  python3 howto.py interaction-graph.json --list            # every create-form found

Given a goal, it finds the create-trigger whose label matches the goal, then the
SHORTEST click-path to it (from --start if given, else the crawl root), and prints
the step-by-step route + the form spec captured at crawl time. Falls back to live
recipe.py only on a cache miss (suggested, not run).

Generic: the goal→trigger match reuses recipe.py's create-VERB + goal-noun regex;
no app/section vocabulary.
"""
import argparse
import json
import os
import re
import sys
from collections import deque
from urllib.parse import urlparse

import recipe  # VERBS, for the same generic goal→trigger matching recipe.py uses


def norm(u):
    return (u or "").rstrip("/").split("#")[0]


def build_adj(graph):
    adj = {}
    for e in graph.get("edges", []):
        if e.get("from") is None or e.get("to") is None:
            continue
        adj.setdefault(e["from"], []).append(e)
    return adj


def bfs(adj, start_id, goal_ids):
    """Shortest edge-path from start_id to ANY id in goal_ids. Returns (goal_id, [edges])."""
    if start_id in goal_ids:
        return start_id, []
    seen = {start_id}
    q = deque([(start_id, [])])
    while q:
        node, epath = q.popleft()
        for e in adj.get(node, []):
            nxt = e["to"]
            if nxt in seen:
                continue
            npath = epath + [e]
            if nxt in goal_ids:
                return nxt, npath
            seen.add(nxt)
            q.append((nxt, npath))
    return None, None


def goal_regex(goal):
    nouns = "|".join(w for w in goal.split() if w.lower() not in
                     ("a", "the", "create", "add", "new", "make", "to"))
    # match the goal's create-trigger label the same way recipe.py does
    if nouns:
        pat = r"(?:%s).{0,30}(?:%s)|(?:%s).{0,30}(?:%s)" % (recipe.VERBS, nouns, nouns, recipe.VERBS)
    else:
        pat = r"(?:%s)" % recipe.VERBS
    return re.compile(pat, re.I)


def find_start_state(graph, start_url):
    """Map a --start URL to the closest cached state id (exact path match preferred)."""
    if not start_url:
        # crawl root = the state with depth 0
        roots = [s for s in graph["states"] if s.get("depth") == 0]
        return roots[0]["id"] if roots else (graph["states"][0]["id"] if graph["states"] else None)
    want = norm(start_url)
    exact = [s for s in graph["states"] if norm(s["url"]) == want]
    if exact:
        return exact[0]["id"]
    # fall back to same path prefix (ignore query), else root
    pw = urlparse(want).path.rstrip("/")
    pref = [s for s in graph["states"] if urlparse(norm(s["url"])).path.rstrip("/") == pw]
    if pref:
        return pref[0]["id"]
    return None


def print_form(form):
    if not form:
        print("  (form spec not captured for this trigger)")
        return
    kind = "a dialog" if form.get("isDialog") else "a form"
    print("\nThis opens %s: “%s”" % (kind, form.get("title") or "(untitled)"))
    print("Fill in %d field(s):" % form.get("fieldCount", len(form.get("fields", []))))
    for f in form.get("fields", []):
        req = "  (required)" if f.get("required") else ""
        opt = ("  options: " + ", ".join(f["options"])) if f.get("options") else ""
        val = ("  default: " + f["value"]) if f.get("value") else ""
        ph = ("  e.g. " + f["placeholder"]) if f.get("placeholder") and not opt and not val else ""
        print("  • %-30s [%s]%s%s%s%s"
              % (f.get("label") or "(unlabeled)", f.get("type"), req, ph, val, opt))
    if form.get("submitButtons"):
        print("\nThen click to confirm: %s" % "  /  ".join("“%s”" % b for b in form["submitButtons"]))


def main():
    ap = argparse.ArgumentParser(description="Offline how-to over an interaction-graph cache")
    ap.add_argument("graph", help="interaction-graph JSON from interaction_crawl.py")
    ap.add_argument("--goal", help='what to do, e.g. "create team" / "add template"')
    ap.add_argument("--start", help="start URL (default: the crawl root)")
    ap.add_argument("--match", help="regex for the trigger label (overrides --goal matching)")
    ap.add_argument("--list", action="store_true", help="list every create-form in the cache")
    ap.add_argument("--all", action="store_true", help="show ALL matching triggers, not just the shortest")
    a = ap.parse_args()

    graph = json.load(open(os.path.expanduser(a.graph)))
    states = {s["id"]: s for s in graph["states"]}
    triggers = graph.get("triggers", [])

    if a.list:
        print("Cache: %s — %d states, %d edges, %d create-forms\n"
              % (graph["meta"].get("host"), graph["meta"]["states"],
                 graph["meta"]["edges"], len(triggers)))
        for t in sorted(triggers, key=lambda x: (x.get("state") or "", x["label"].lower())):
            st = states.get(t.get("state"), {})
            depth = len(t.get("path", []))
            ff = (t.get("form") or {}).get("fieldCount")
            print("  %-28s  @ %-45s  %d-click%s%s"
                  % ("“%s”" % t["label"], st.get("url", "?"), depth + 1,
                     "" if depth + 1 == 1 else "s",
                     ("  (%d fields)" % ff) if ff is not None else ""))
        return

    if not a.goal and not a.match:
        sys.exit("Pass --goal \"...\" (or --match <regex>), or --list.")

    rx = re.compile(a.match, re.I) if a.match else goal_regex(a.goal)
    matches = [t for t in triggers if rx.search(t["label"])]
    if not matches:
        # generic fallback: any create-verb trigger whose label shares a goal noun
        if a.goal:
            nouns = [w for w in a.goal.lower().split()
                     if w not in ("a", "the", "create", "add", "new", "make", "to")]
            matches = [t for t in triggers
                       if any(n in t["label"].lower() for n in nouns)]
    if not matches:
        print("✗ No cached trigger matches %r." % (a.match or a.goal))
        print("  → cache miss: refresh with interaction_crawl.py, or run live:")
        print('     ./run-recipe.sh --goal "%s" --start <url>' % (a.goal or ""))
        sys.exit(2)

    adj = build_adj(graph)
    start_id = find_start_state(graph, a.start)
    if start_id is None:
        print("! --start %r not in cache; using the crawl root instead." % a.start, file=sys.stderr)
        start_id = find_start_state(graph, None)

    # for each matching trigger, find the shortest path from start to its state
    goal_states = {}
    for t in matches:
        goal_states.setdefault(t["state"], []).append(t)

    routed = []
    for sid, ts in goal_states.items():
        if sid is None:
            continue
        gid, epath = bfs(adj, start_id, {sid})
        if gid is None:
            continue
        routed.append((len(epath), epath, ts))
    if not routed:
        # trigger exists but unreachable from this start in the cached graph
        print("✗ Matching form(s) exist but no cached path from %s."
              % (a.start or "the crawl root"))
        print("  Triggers: %s" % ", ".join("“%s”" % t["label"] for t in matches))
        sys.exit(2)

    routed.sort(key=lambda x: x[0])
    show = routed if a.all else routed[:1]
    start_url = states[start_id]["url"]
    for dist, epath, ts in show:
        t = ts[0]
        steps = ["Go to %s" % start_url]
        steps += ["Click “%s”" % e["label"] for e in epath]
        steps.append("Click the “%s” button" % t["label"])
        print("\n=== HOW TO: %s ===\n" % (a.goal or t["label"]).upper())
        print("Shortest route — %d click%s:" % (len(steps) - 1, "" if len(steps) - 1 == 1 else "s"))
        for i, s in enumerate(steps, 1):
            print("  %d. %s" % (i, s))
        if t.get("opensAt"):
            print("     → opens %s" % t["opensAt"])
        print_form(t.get("form"))
        print("\n(answered offline from cache — 0 browser calls)")


if __name__ == "__main__":
    main()
