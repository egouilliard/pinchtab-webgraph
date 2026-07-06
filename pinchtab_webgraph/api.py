#!/usr/bin/env python3
"""Print-free, dict-returning wrappers over the offline graph queries.

Every function here returns a STRUCTURED dict and NEVER prints or calls sys.exit —
a `status` field replaces the CLI's exit codes as the miss-signal. This is the
programmatic surface the CLI `main()`s narrate to a terminal; it's the seam a
future MCP/UTCP server binds to.

The graph logic is NOT re-implemented — we reuse `howto_graph.build_adj/bfs/goal_regex/
find_start_state/norm` and `paths.load/build_adj/shortest/all_paths/label_of/
resolve`. Only the small grouping/sort/limit glue is duplicated, each such site
flagged with a `keep in sync with <module>` comment.

`howto` and `paths` are imported QUALIFIED on purpose: both export `build_adj`
with INCOMPATIBLE signatures, so a flat `from .howto import build_adj` would
silently shadow one. Always call `howto_graph.build_adj` / `paths.build_adj`.
"""
import json
import os

# `howto` is imported under an alias because this module's PUBLIC function is itself
# named `howto` (a `def howto` would otherwise shadow the module). The alias keeps
# access QUALIFIED — `howto_graph.build_adj` / `paths.build_adj` — which is the point:
# both modules export `build_adj` with INCOMPATIBLE signatures, so a flat
# `from .howto import build_adj` would silently shadow one.
from . import howto as howto_graph, paths


def _load_interaction_graph(graph_path: str | os.PathLike) -> dict:
    # No error handling on purpose: a bad path / malformed JSON propagates to the
    # caller, who owns how to surface it.
    return json.load(open(os.path.expanduser(str(graph_path))))


def graph_summary(graph_path: str | os.PathLike) -> dict:
    """Detect graph kind (interaction vs link) and return meta + element counts."""
    graph = _load_interaction_graph(graph_path)
    meta = graph.get("meta", {})
    # interaction-graph = states/triggers; link-graph = nodes.
    if "states" in graph or "triggers" in graph:
        return {
            "graph_kind": "interaction",
            "meta": meta,
            "states": len(graph.get("states", [])),
            "edges": len(graph.get("edges", [])),
            "triggers": len(graph.get("triggers", [])),
        }
    if "nodes" in graph:
        return {
            "graph_kind": "link",
            "meta": meta,
            "nodes": len(graph.get("nodes", [])),
            "edges": len(graph.get("edges", [])),
        }
    return {"graph_kind": "unknown", "meta": meta}


def howto(
    graph_path: str | os.PathLike,
    goal: str | None = None,
    start: str | None = None,
    match: str | None = None,
    all: bool = False,
) -> dict:
    """Shortest click-path(s) to a create-trigger matching `goal`/`match`, + its form."""
    import re

    # keep in sync with howto.py:main() L206-207 — without a goal OR a match, the
    # CLI exits up front. Here we mirror that guard: goal_regex("") would otherwise
    # match EVERY create-verb trigger, so return a uniform miss instead.
    if not goal and not match:
        return {"status": "invalid_args", "goal": goal, "match_pattern": None,
                "start_url": None, "results": [], "candidates": []}

    graph = _load_interaction_graph(graph_path)
    states = {s["id"]: s for s in graph["states"]}
    triggers = graph.get("triggers", [])
    pattern = match or (goal or "")

    # keep in sync with howto.py:main() — goal/match trigger selection. The regex hooks
    # a create-VERB adjacent to a goal noun; the fallback UNION adds any trigger whose
    # label shares a goal noun as a WHOLE WORD (so form-bearing states like "Sign in" /
    # "Join now", which carry no create-VERB, are still matchable). Both paths are
    # word-boundaried + stopword/short-token filtered → no `in`-inside-"Find" hits.
    rx = re.compile(match, re.I) if match else howto_graph.goal_regex(goal or "")
    matches = [t for t in triggers if rx.search(t["label"])]
    if goal:
        from . import recipe
        nouns = recipe.goal_nouns(goal)
        if nouns:
            nrx = re.compile(recipe.noun_alt(nouns), re.I)
            have = {id(t) for t in matches}
            matches += [t for t in triggers if id(t) not in have and nrx.search(t["label"])]
    if not matches:
        return {"status": "no_match", "goal": goal, "match_pattern": pattern,
                "start_url": None, "results": [], "candidates": [], "low_confidence": []}

    # keep in sync with howto.py:main() — start-state resolution
    adj = howto_graph.build_adj(graph)
    start_id = howto_graph.find_start_state(graph, start)
    if start_id is None:
        start_id = howto_graph.find_start_state(graph, None)
    start_url = states[start_id]["url"] if start_id in states else None

    # keep in sync with howto.py:main() — route each trigger, tag confidence. A match
    # whose form has NO fields is LOW confidence (a nav control that merely shares a
    # create-VERB, not a real form); route by trigger so confidence is per-trigger.
    def _result(epath, t):
        steps = (["Go to %s" % start_url]
                 + ["Click “%s”" % e["label"] for e in epath]
                 + ["Click the “%s” button" % t["label"]])
        st = states.get(t["state"], {})
        return {"trigger_label": t["label"], "state_id": t["state"],
                "state_url": st.get("url"), "clicks": len(steps) - 1, "steps": steps,
                "opens_at": t.get("opensAt"), "form": t.get("form"),
                "confidence": howto_graph.form_confidence(t)}

    dist_cache: dict = {}
    routed = []
    for t in matches:
        sid = t.get("state")
        if sid is None:
            continue
        if sid not in dist_cache:
            dist_cache[sid] = howto_graph.bfs(adj, start_id, {sid})
        gid, epath = dist_cache[sid]
        if gid is None:
            continue
        routed.append((len(epath), epath, t, howto_graph.form_confidence(t)))
    if not routed:
        return {"status": "unreachable", "goal": goal, "match_pattern": pattern,
                "start_url": start_url, "results": [],
                "candidates": [t["label"] for t in matches], "low_confidence": []}

    routed.sort(key=lambda x: x[0])
    low = [_result(ep, t) for _d, ep, t, c in routed if c == "low"]
    high = [(d, ep, t) for d, ep, t, c in routed if c == "high"]
    if not high:
        # only zero-field / low-confidence matches — prefer no_match over surfacing a
        # route the user probably didn't ask for (the false-positive guard). The
        # flagged candidates are still returned under `low_confidence`.
        return {"status": "no_match", "goal": goal, "match_pattern": pattern,
                "start_url": start_url, "results": [], "candidates": [],
                "low_confidence": low}

    show = high if all else high[:1]
    results = [_result(ep, t) for _d, ep, t in show]
    return {"status": "ok", "goal": goal, "match_pattern": pattern,
            "start_url": start_url, "results": results, "candidates": [],
            "low_confidence": low}


def _item_text(it: dict) -> str:
    # keep in sync with howto.py:main() L144-145 — item_text()
    return " ".join([it.get("t", "")] + (it.get("cells") or [])).strip()


def find_content(
    graph_path: str | os.PathLike,
    text: str,
    start: str | None = None,
    limit: int = 40,
) -> dict:
    """Search captured data collections for `text`; route each matching view."""
    graph = _load_interaction_graph(graph_path)
    states = {s["id"]: s for s in graph["states"]}

    # keep in sync with howto.py:main() L163-185 — collect + route matches
    adj = howto_graph.build_adj(graph)
    start_id = howto_graph.find_start_state(graph, start) or (
        graph["states"][0]["id"] if graph["states"] else None)
    q = text.lower()
    by_state: dict = {}
    total = 0
    for s in graph["states"]:
        for c in s.get("collections") or []:
            for it in c.get("items", []):
                txt = _item_text(it)
                if q in txt.lower():
                    by_state.setdefault(s["id"], []).append((c.get("kind"), txt))
                    total += 1
    start_url = states[start_id]["url"] if start_id in states else None
    if not total:
        return {"status": "no_match", "query": text, "start_url": start_url,
                "total_matches": 0, "views_matched": 0, "views": [], "shown": 0}

    routed = []
    for sid, items in by_state.items():
        _, epath = howto_graph.bfs(adj, start_id, {sid})
        routed.append((len(epath) if epath is not None else 10 ** 6, epath, sid, items))
    routed.sort(key=lambda x: x[0])

    views = []
    shown = 0
    for _dist, epath, sid, items in routed:
        st = states[sid]
        take = items[:limit - shown]
        reachable = epath is not None
        if reachable:
            steps = ["Go to %s" % states[start_id]["url"]] + \
                    ["Click “%s”" % e["label"] for e in epath]
        else:
            steps = None
        views.append({
            "view_label": st.get("label") or "(root)",
            "view_url": st.get("url"),
            "reachable": reachable,
            "distance_clicks": len(epath) if reachable else None,
            "steps": steps,
            "items": [{"kind": k, "text": txt} for k, txt in take],
            "truncated": len(take) < len(items),
        })
        shown += len(take)
        if shown >= limit:
            break
    return {"status": "ok", "query": text, "start_url": start_url,
            "total_matches": total, "views_matched": len(by_state),
            "views": views, "shown": shown}


def list_content(graph_path: str | os.PathLike) -> dict:
    """Per-view inventory of captured data collections (kinds, counts, a sample)."""
    graph = _load_interaction_graph(graph_path)
    # keep in sync with howto.py:main() L147-160 — list-content inventory
    views = []
    for s in graph.get("states", []):
        cols = s.get("collections") or []
        if not cols:
            continue
        collections = []
        for c in sorted(cols, key=lambda x: -x.get("count", 0)):
            sample = _item_text(c["items"][0]) if c.get("items") else ""
            collections.append({"kind": c.get("kind"), "count": c.get("count", 0),
                                "sample": sample})
        views.append({"view_label": s.get("label") or "(root)",
                      "view_url": s.get("url"), "collections": collections})
    return {"status": "ok" if views else "empty", "views": views}


def list_forms(graph_path: str | os.PathLike) -> dict:
    """Every create-form in the cache: label, host, click-depth, field count."""
    graph = _load_interaction_graph(graph_path)
    states = {s["id"]: s for s in graph["states"]}
    triggers = graph.get("triggers", [])
    meta = graph.get("meta", {})
    # keep in sync with howto.py:main() L131-141 — list rendering
    forms = []
    for t in triggers:
        st = states.get(t.get("state"), {})
        depth = len(t.get("path", []))
        forms.append({
            "label": t["label"],
            "state_url": st.get("url"),
            "clicks": depth + 1,
            "field_count": (t.get("form") or {}).get("fieldCount"),
        })
    forms.sort(key=lambda f: (f["state_url"] or "", f["label"].lower()))
    return {
        "meta": {"host": meta.get("host"), "states": meta.get("states"),
                 "edges": meta.get("edges"), "triggers": len(triggers)},
        "forms": forms,
    }


def _node_info(nodes: dict, nid: str) -> dict:
    n = nodes.get(nid, {})
    return {"id": nid, "url": n.get("url"), "title": n.get("title")}


def _link_candidates(nodes: dict, needle: str) -> list[str]:
    # the same substring test resolve() uses, surfaced as candidate labels.
    low = needle.lower()
    return [paths.label_of(nodes, nid) for nid, n in nodes.items()
            if low in (n.get("url") or nid).lower() or low in (n.get("title") or "").lower()]


def link_paths(
    graph_path: str | os.PathLike,
    frm: str,
    to: str,
    structural: bool = False,
    all: bool = False,
    max_len: int = 5,
    max_paths: int = 50,
) -> dict:
    """Shortest / all click-paths between two pages of a crawled link graph."""
    g, nodes = paths.load(os.path.expanduser(str(graph_path)))
    mode = "structural (no global nav)" if structural else "all edges (incl. global nav)"

    # paths.resolve() calls sys.exit on no-match / ambiguous-match — translate that
    # into a status string + candidates instead of exiting.
    def _resolve(needle, side):
        try:
            return paths.resolve(nodes, needle), None
        except SystemExit as e:
            kind = "ambiguous" if "ambiguous" in str(e).lower() else "not_found"
            return None, {"status": "%s_%s" % (kind, side), "from": None, "to": None,
                          "mode": mode, "shortest": None, "all_paths": None,
                          "candidates": _link_candidates(nodes, needle)}

    src, err = _resolve(frm, "from")
    if err is not None:
        return err
    dst, err = _resolve(to, "to")
    if err is not None:
        return err

    # keep in sync with paths.py:main() L133-155 — build adj, shortest, all
    adj = paths.build_adj(g, structural)
    path, edges = paths.shortest(adj, src, dst)
    if not path:
        return {"status": "no_path", "from": _node_info(nodes, src),
                "to": _node_info(nodes, dst), "mode": mode,
                "shortest": None, "all_paths": None}
    shortest = {
        "clicks": len(edges),
        "steps": [{"label": label, "kind": kind, "to_url": paths.label_of(nodes, nid)}
                  for (label, kind), nid in zip(edges, path[1:])],
    }
    all_p = None
    if all:
        ps = paths.all_paths(adj, src, dst, max_len, max_paths)
        ps.sort(key=len)
        all_p = [{"clicks": len(p) - 1, "nodes": [paths.label_of(nodes, x) for x in p]}
                 for p in ps]
    return {"status": "ok", "from": _node_info(nodes, src),
            "to": _node_info(nodes, dst), "mode": mode,
            "shortest": shortest, "all_paths": all_p}
