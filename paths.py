#!/usr/bin/env python3
"""
Query navigation paths in a crawl graph (the <out>.json from crawl.py).

  # shortest click-path between two pages (matched by URL substring):
  python3 paths.py leytongo-full-links.json --from /admin/users --to solviverde/documents

  # ignore the ubiquitous sidebar/global-nav links (show the "content" route):
  python3 paths.py graph.json --from dashboard --to bonoverde/workflow --structural

  # enumerate all simple paths up to N hops:
  python3 paths.py graph.json --from A --to B --all --max-len 4

Edges are DIRECTED (you follow a link from source to target). "Shortest" =
fewest clicks (BFS). On a site whose sidebar links every page to every section,
most shortest paths are 1-2 hops *because of* that global nav — use --structural
to exclude those hub links and see the longer, more meaningful route.
"""
import argparse
import json
import sys
from collections import deque, defaultdict


def load(path):
    g = json.load(open(path))
    nodes = {n["id"]: n for n in g["nodes"]}
    pages = g.get("meta", {}).get("pages", len(nodes))
    # classify "global nav" edges: into a target most pages link to (same rule
    # the viewer uses to de-clutter).
    indeg = defaultdict(int)
    for e in g["edges"]:
        indeg[e["target"]] += 1
    hub = max(8, pages * 0.4)
    for e in g["edges"]:
        e["glob"] = indeg[e["target"]] >= hub
    return g, nodes


def resolve(nodes, needle):
    """Find a node id by exact id or URL/title substring; error if ambiguous."""
    if needle in nodes:
        return needle
    hits = [nid for nid, n in nodes.items()
            if needle.lower() in (n.get("url") or nid).lower()
            or needle.lower() in (n.get("title") or "").lower()]
    if not hits:
        sys.exit("no node matches %r" % needle)
    if len(hits) > 1:
        # prefer an exact path endpoint match if present
        exact = [h for h in hits if (nodes[h].get("url") or h).rstrip("/").endswith(needle.rstrip("/"))]
        if len(exact) == 1:
            return exact[0]
        sys.exit("ambiguous %r — matches %d nodes:\n  %s%s" % (
            needle, len(hits), "\n  ".join(hits[:10]),
            "\n  …" if len(hits) > 10 else ""))
    return hits[0]


def build_adj(g, structural):
    adj = defaultdict(list)  # src -> list of (dst, label, kind)
    for e in g["edges"]:
        if structural and e.get("glob"):
            continue
        adj[e["source"]].append((e["target"], e.get("label", ""), e.get("kind", "link")))
    return adj


def shortest(adj, src, dst):
    if src == dst:
        return [src], []
    prev = {src: None}
    via = {}
    q = deque([src])
    while q:
        u = q.popleft()
        if u == dst:
            break
        for v, label, kind in adj.get(u, []):
            if v not in prev:
                prev[v] = u
                via[v] = (label, kind)
                q.append(v)
    if dst not in prev:
        return None, None
    path, edges = [], []
    cur = dst
    while cur is not None:
        path.append(cur)
        if prev[cur] is not None:
            edges.append(via[cur])
        cur = prev[cur]
    path.reverse()
    edges.reverse()
    return path, edges


def all_paths(adj, src, dst, max_len, max_paths):
    out = []
    stack = [(src, [src])]
    while stack and len(out) < max_paths:
        node, path = stack.pop()
        if node == dst and len(path) > 1:
            out.append(path)
            continue
        if len(path) - 1 >= max_len:
            continue
        for v, _l, _k in adj.get(node, []):
            if v not in path:
                stack.append((v, path + [v]))
    return out


def label_of(nodes, nid):
    n = nodes.get(nid, {})
    return n.get("url") or n.get("title") or nid


def main():
    ap = argparse.ArgumentParser(description="Query paths in a crawl graph")
    ap.add_argument("graph", help="the <out>.json from crawl.py")
    ap.add_argument("--from", dest="src", required=True, help="source (URL/title substring or id)")
    ap.add_argument("--to", dest="dst", required=True, help="target (URL/title substring or id)")
    ap.add_argument("--structural", action="store_true",
                    help="exclude global-nav (sidebar/topbar) edges from pathfinding")
    ap.add_argument("--all", action="store_true", help="enumerate simple paths, not just shortest")
    ap.add_argument("--max-len", type=int, default=5, help="max hops for --all (default 5)")
    ap.add_argument("--max-paths", type=int, default=50, help="cap for --all (default 50)")
    a = ap.parse_args()

    g, nodes = load(a.graph)
    src, dst = resolve(nodes, a.src), resolve(nodes, a.dst)
    adj = build_adj(g, a.structural)
    mode = "structural (no global nav)" if a.structural else "all edges (incl. global nav)"
    print("from: %s\n  to: %s\nmode: %s\n" % (label_of(nodes, src), label_of(nodes, dst), mode))

    path, edges = shortest(adj, src, dst)
    if not path:
        print("NO PATH (%s)." % ("even via global nav" if not a.structural
                                 else "try without --structural"))
    else:
        print("SHORTEST: %d click(s)" % len(edges))
        print("   " + label_of(nodes, path[0]))
        for (label, kind), nid in zip(edges, path[1:]):
            arrow = "==>" if kind == "action" else "-->"
            print("   %s [%s] %s" % (arrow, (label or "").strip()[:40], label_of(nodes, nid)))

    if a.all:
        ps = all_paths(adj, src, dst, a.max_len, a.max_paths)
        print("\nALL SIMPLE PATHS (<=%d hops, cap %d): found %d%s"
              % (a.max_len, a.max_paths, len(ps), " (capped)" if len(ps) == a.max_paths else ""))
        ps.sort(key=len)
        for p in ps:
            print("  (%d) %s" % (len(p) - 1, "  ->  ".join(
                (nodes[x].get("url") or x).replace("https://", "") for x in p)))


if __name__ == "__main__":
    main()
