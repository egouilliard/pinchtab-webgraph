#!/usr/bin/env python3
"""
Answer "how do I do X?" OFFLINE, in milliseconds, from an interaction-graph cache
built by interaction_crawl.py. No live browser, no discovery budget, no per-state
latency — just a BFS over the cached graph.

  python3 pinchtab_webgraph/howto.py out/interaction-graph.json --goal "create team"
  python3 pinchtab_webgraph/howto.py out/interaction-graph.json --goal "add template" --start https://app/x
  python3 pinchtab_webgraph/howto.py out/interaction-graph.json --list            # every create-form found

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

from . import recipe  # VERBS, for the same generic goal→trigger matching recipe.py uses


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
    # Match the goal's create-trigger label the same way recipe.py does: a create-VERB
    # adjacent to a goal noun, in either order. recipe.goal_needle() carries the
    # WORD-BOUNDARY + short-token/stopword filtering that stops false positives (a
    # short noun matching inside an unrelated word, or `in`/`a`/`to` matching anywhere).
    return re.compile(recipe.goal_needle(goal), re.I)


def form_field_count(trigger):
    """How many fields the trigger's captured form has (0 if none / not captured)."""
    form = trigger.get("form") or {}
    fc = form.get("fieldCount")
    return fc if fc is not None else len(form.get("fields") or [])


def form_confidence(trigger):
    """`high` when the trigger opens a real (non-empty) form, else `low`. A zero-field
    match is typically a nav control that merely shares a create-VERB with the goal
    ("Find a NEW job"), not a form the user wants — we de-prioritize those."""
    return "high" if form_field_count(trigger) > 0 else "low"


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
    ap.add_argument("--find", help="search the captured DATA (collections: table/list/tree/grid rows, "
                                   "files, messages, …) for text; shows what matched, which view it's in, "
                                   "and the shortest click-path to that view")
    ap.add_argument("--list-content", action="store_true",
                    help="inventory the data collections captured per view (kinds + item counts)")
    ap.add_argument("--limit", type=int, default=40, help="max content matches/items to show (default 40)")
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

    def item_text(it):
        return " ".join([it.get("t", "")] + (it.get("cells") or [])).strip()

    if a.list_content:
        any_c = False
        for s in graph["states"]:
            cols = s.get("collections") or []
            if not cols:
                continue
            any_c = True
            print("▸ %s  (%s)" % (s.get("label") or "(root)", s.get("url", "?")))
            for c in sorted(cols, key=lambda x: -x.get("count", 0)):
                sample = item_text(c["items"][0]) if c.get("items") else ""
                print("    [%-16s] %4d items   e.g. %s" % (c.get("kind"), c.get("count", 0), sample[:60]))
        if not any_c:
            print("No content captured in this graph — re-crawl with --capture-content.")
        return

    if a.find:
        adj = build_adj(graph)
        start_id = find_start_state(graph, a.start) or (graph["states"][0]["id"] if graph["states"] else None)
        q = a.find.lower()
        # group matches by the view (state) they live in
        by_state = {}
        total = 0
        for s in graph["states"]:
            for c in s.get("collections") or []:
                for it in c.get("items", []):
                    txt = item_text(it)
                    if q in txt.lower():
                        by_state.setdefault(s["id"], []).append((c.get("kind"), txt))
                        total += 1
        if not total:
            print("✗ No captured data matches %r." % a.find)
            print("  (searched every view's data collections; re-crawl with --capture-content if stale)")
            sys.exit(2)
        # route each view: shortest click-path from the start
        routed = []
        for sid, items in by_state.items():
            _, epath = bfs(adj, start_id, {sid})
            routed.append((len(epath) if epath is not None else 10 ** 6, epath, sid, items))
        routed.sort(key=lambda x: x[0])
        print("=== FOUND %d item(s) matching %r across %d view(s) ===" % (total, a.find, len(by_state)))
        shown = 0
        for dist, epath, sid, items in routed:
            st = states[sid]
            print("\n▸ %s  (%s)" % (st.get("label") or "(root)", st.get("url", "?")))
            if epath is None:
                print("   ⚠ no cached click-path from the start view")
            else:
                steps = ["Go to %s" % states[start_id]["url"]] + ["Click “%s”" % e["label"] for e in epath]
                print("   route (%d click%s): %s"
                      % (len(epath), "" if len(epath) == 1 else "s", "  →  ".join(steps)))
            for kind, txt in items[:a.limit - shown]:
                print("     • [%s] %s" % (kind, txt[:110]))
                shown += 1
            if shown >= a.limit:
                print("   … (%d shown; use --limit to see more)" % a.limit)
                break
        print("\n(answered offline from cache — 0 browser calls)")
        return

    if not a.goal and not a.match:
        sys.exit("Pass --goal \"...\" (or --match <regex>), --find \"...\", --list, or --list-content.")

    rx = re.compile(a.match, re.I) if a.match else goal_regex(a.goal)
    matches = [t for t in triggers if rx.search(t["label"])]
    if a.goal:
        # generic fallback (UNION, not just on empty): a trigger whose label shares a
        # goal noun as a whole word — catches form-bearing states (e.g. "Sign in",
        # "Join now") whose labels carry no create-VERB for the regex to hook onto.
        nouns = recipe.goal_nouns(a.goal)
        nrx = re.compile(recipe.noun_alt(nouns), re.I) if nouns else None
        if nrx:
            have = {id(t) for t in matches}
            matches += [t for t in triggers if id(t) not in have and nrx.search(t["label"])]
    if not matches:
        print("✗ No cached trigger matches %r." % (a.match or a.goal))
        print("  → cache miss: refresh with interaction_crawl.py, or run live:")
        print('     scripts/run-recipe.sh --goal "%s" --start <url>' % (a.goal or ""))
        sys.exit(2)

    adj = build_adj(graph)
    start_id = find_start_state(graph, a.start)
    if start_id is None:
        print("! --start %r not in cache; using the crawl root instead." % a.start, file=sys.stderr)
        start_id = find_start_state(graph, None)

    # route each matching trigger, tagging its confidence. A match whose form carries
    # NO fields (fieldCount 0 / no form) is LOW confidence — usually a nav control that
    # merely happened to share a create-VERB (e.g. "Find a NEW job"), not a real form.
    # We route by trigger (not by state) so confidence is per-trigger.
    dist_cache = {}
    routed = []
    for t in matches:
        sid = t.get("state")
        if sid is None:
            continue
        if sid not in dist_cache:
            dist_cache[sid] = bfs(adj, start_id, {sid})
        gid, epath = dist_cache[sid]
        if gid is None:
            continue
        routed.append((len(epath), epath, t, form_confidence(t)))
    if not routed:
        # trigger exists but unreachable from this start in the cached graph
        print("✗ Matching form(s) exist but no cached path from %s."
              % (a.start or "the crawl root"))
        print("  Triggers: %s" % ", ".join("“%s”" % t["label"] for t in matches))
        sys.exit(2)

    high = [r for r in routed if r[3] == "high"]
    if not high:
        # only zero-field / low-confidence matches survive — prefer no_match over
        # narrating a route the user probably didn't ask for.
        routed.sort(key=lambda x: x[0])
        print("✗ No confident match for %r." % (a.match or a.goal))
        print("  Low-confidence (zero-field) candidates: %s"
              % ", ".join("“%s”" % r[2]["label"] for r in routed))
        sys.exit(2)

    routed = high
    routed.sort(key=lambda x: x[0])
    show = routed if a.all else routed[:1]
    start_url = states[start_id]["url"]
    for dist, epath, t, _conf in show:
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
