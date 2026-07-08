#!/usr/bin/env python3
"""
Storage + write-back merge for the interaction-graph cache.

The cache file is exactly the graph `interaction_crawl.py` emits and `howto.py`
queries OFFLINE — {meta, states, state_index, edges, triggers}. This module owns
the per-host file (caches/<host>.json) and, crucially, STITCHES a single live
discovery result (recipe.py's <out>.json) back into that graph so the next query
for the same goal answers from cache.

The stitch is the delicate part: it rebuilds a FAITHFUL LINEAR CHAIN of states
from the live result's structured path, so howto.py's BFS reconstructs the FULL
click-path (one "Click <label>" per edge) — never a collapsed self-loop.

Generic by construction: routing is by hostname, state matching by normalized
URL. No app/section vocabulary, no hardcoded routes or labels. Stdlib only.
"""
import datetime
import glob
import json
import os
import re
from urllib.parse import urlparse

# A safe hostname-shaped token: letters/digits/dots/hyphens (every real hostname),
# plus underscore for internal hosts. Crucially it admits NO path separators (`/`,
# `\`) and no `..`-as-a-segment escape, so a raw `host` can never resolve OUTSIDE
# caches_dir(). ask.py feeds urlparse(...).hostname (always [A-Za-z0-9.-]), which
# this always accepts — see cache_path().
_HOST_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def home_dir():
    return os.path.expanduser(os.environ.get("PINCHTAB_WEBGRAPH_HOME", "~/.pinchtab-webgraph"))


def caches_dir():
    return os.path.join(home_dir(), "caches")


def validate_host(host):
    # VALIDATE at the choke point: path/show/clear all route through here, so
    # rejecting a non-hostname token here blocks path-traversal for every caller
    # (e.g. `cache clear "../../etc/passwd"`) before any filesystem access.
    if not isinstance(host, str) or not _HOST_RE.match(host):
        raise ValueError("invalid cache host: %r" % host)
    # The regex accepts pure-dot tokens ("." / ".." / "..."), which are safe for
    # cache_path() (it appends ".json", so ".." becomes the filename "..json") but
    # NOT for callers that use the bare host as a directory segment (chat_store's
    # host_sessions_dir): there "." / ".." would resolve to the parent dir, escaping
    # the per-host quarantine. Reject any all-dots token at the shared choke point.
    if host.strip(".") == "":
        raise ValueError("invalid cache host: %r" % host)


def cache_path(host):
    validate_host(host)
    return os.path.join(caches_dir(), "%s.json" % host)


def load(host):
    p = cache_path(host)
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return json.load(f)


def list_hosts():
    """Hostnames with a persisted cache — the basenames of caches/<host>.json.

    Excludes the in-flight caches/<host>.json.tmp that atomic_write leaves mid-write.
    """
    out = []
    for p in glob.glob(os.path.join(caches_dir(), "*.json")):
        if p.endswith(".json.tmp"):
            continue
        out.append(os.path.basename(p)[:-len(".json")])
    return sorted(out)


def clear(host):
    """Remove one host's cache file. Returns True if a file was removed, else False."""
    p = cache_path(host)
    if os.path.exists(p):
        os.remove(p)
        return True
    return False


def clear_all():
    """Remove every host cache file. Returns the removed host names, sorted."""
    removed = [h for h in list_hosts() if clear(h)]
    return sorted(removed)


def _norm(u):
    return (u or "").rstrip("/").split("#")[0]


def _id_num(sid):
    # trailing numeric suffix of a state id ("s12" -> 12, "sl3" -> 3); 0 if none.
    digits = ""
    for ch in reversed(sid or ""):
        if ch.isdigit():
            digits = ch + digits
        else:
            break
    return int(digits) if digits else 0


def _max_state_num(graph):
    return max((_id_num(s.get("id", "")) for s in graph.get("states", [])), default=0)


def _next_live_id(graph):
    # "sl{n}", n = 1 + the largest numeric suffix across existing state ids. The
    # "sl" prefix marks states added LIVE (vs. "s" from a full crawl); the numeric
    # space is shared so ids stay globally unique.
    return "sl%d" % (_max_state_num(graph) + 1)


def stitch(live_rec, graph):
    """Build (new_states, new_edges, new_trigger_record) from one live result.

    A FAITHFUL LINEAR CHAIN: start state, then one DISTINCT next state per step in
    live_rec["pathStructured"], so howto.py's BFS walks the whole path. Click-only
    (href=null) steps are NEVER collapsed into self-loops — each advances to its own
    synthetic state — otherwise the reconstructed how-to would drop those clicks.
    """
    start_url = live_rec["start"]
    steps = live_rec.get("pathStructured") or []
    trigger_page = live_rec.get("triggerPage")

    # existing states indexed by normalized URL (first wins) — for prefix reuse.
    # by_url_label additionally keys on (url, label) so a click-only step can reuse the
    # SAME synthetic state a prior run created, instead of minting a fresh one each run.
    by_url = {}
    by_url_label = {}
    for s in graph.get("states", []):
        by_url.setdefault(_norm(s["url"]), s)
        by_url_label.setdefault((_norm(s["url"]), (s.get("label") or "").lower()), s)

    new_states = []
    counter = [_max_state_num(graph)]            # live id allocator (increments per synth)

    def synth(url, depth, label):
        counter[0] += 1
        st = {"id": "sl%d" % counter[0], "url": _norm(url), "label": label, "depth": depth}
        new_states.append(st)
        return st

    new_ids = set()                              # ids of states we just synthesized
    used_ids = set()                             # ids already on THIS chain (avoid cycles)

    # start node: reuse an existing state with the same URL, else synthesize one.
    s0 = by_url.get(_norm(start_url))
    if s0 is not None:
        start_state = s0
    else:
        start_state = synth(start_url, 0, urlparse(start_url).path or "/")
        new_ids.add(start_state["id"])
    used_ids.add(start_state["id"])

    new_edges = []
    prev = start_state
    depth = prev.get("depth", 0)
    for step in steps:
        href = step.get("href")
        label = step.get("label")
        selector = step.get("selector")
        depth += 1
        if href:
            nurl = _norm(href)
            cand = by_url.get(nurl)
            if cand is not None and cand["id"] not in used_ids:
                nxt = cand                       # reuse an existing state for this URL
            else:
                nxt = synth(href, depth, label)  # synthesize (new URL, or would cycle)
                new_ids.add(nxt["id"])
            kind = "link"
        else:
            # click-only (tab/menu): reuse a prior run's state with the same (url, label)
            # if one exists (so repeated live runs don't mint a fresh sl{n} every time),
            # else a DISTINCT state inheriting the previous URL.
            cand = by_url_label.get((_norm(prev["url"]), (label or "").lower()))
            if cand is not None and cand["id"] not in used_ids:
                nxt = cand
            else:
                nxt = synth(prev["url"], depth, label)
                new_ids.add(nxt["id"])
            kind = "click"
        new_edges.append({"from": prev["id"], "to": nxt["id"], "label": label,
                          "selector": selector, "kind": kind})
        used_ids.add(nxt["id"])
        prev = nxt

    # The trigger sits at the page the chain ends on. A terminal click-only step
    # (e.g. a tab that updates only the URL's query, exposing no href) left the
    # synthetic state carrying the PREVIOUS URL — promote it to triggerPage. If the
    # last state is one we reused from the graph (can't mutate) and its URL still
    # differs, append one distinct triggerPage state + its edge.
    if trigger_page and _norm(prev["url"]) != _norm(trigger_page):
        if prev["id"] in new_ids:
            prev["url"] = _norm(trigger_page)
        else:
            depth += 1
            tp = synth(trigger_page, depth, urlparse(trigger_page).path or "/")
            new_ids.add(tp["id"])
            # label with the URL path, NOT the trigger text — otherwise howto.py would
            # emit "Click <trigger>" as a path step AND again as the final action.
            new_edges.append({"from": prev["id"], "to": tp["id"],
                              "label": urlparse(trigger_page).path or "/",
                              "selector": live_rec.get("triggerSelector"), "kind": "link"})
            prev = tp

    new_trigger = {"label": live_rec["trigger"], "state": prev["id"],
                   "path": live_rec.get("pathStructured") or [],
                   "form": live_rec.get("form"), "opensAt": live_rec.get("opensAt"),
                   "cachedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                   "cacheSource": "live"}
    return new_states, new_edges, new_trigger


def atomic_write(host, graph):
    # ATOMIC: write to caches/<host>.json.tmp then os.replace onto the target, so a
    # reader never sees a half-written file. (Concurrent WRITERS would still need
    # fcntl.flock around the read-modify-write — not implemented here.)
    os.makedirs(caches_dir(), exist_ok=True)
    target = cache_path(host)
    tmp = target + ".tmp"
    with open(tmp, "w") as f:
        json.dump(graph, f, indent=2)
    os.replace(tmp, target)


def _trigger_sig(label, path):
    # Identity of a cached how-to: its trigger label + the sequence of click labels.
    # Distinguishes distinct same-label triggers (different paths) while letting a
    # re-run of the SAME goal refresh its own entry.
    return ((label or "").lower(),
            tuple((s.get("label") or "").lower() for s in (path or [])))


def merge(host, live_rec, now_iso):
    """Stitch a live result into host's cache (creating it if absent) and persist.

    A re-run for the SAME trigger+path (a re-miss or --verify self-heal) UPDATES the
    cached entry IN PLACE — it does not re-stitch states, so repeated live runs cannot
    grow the cache unboundedly."""
    graph = load(host) or {"meta": {"host": host, "states": 0, "edges": 0, "triggers": 0},
                           "states": [], "state_index": {}, "edges": [], "triggers": []}
    graph.setdefault("states", [])
    graph.setdefault("state_index", {})
    graph.setdefault("edges", [])
    graph.setdefault("triggers", [])

    # Idempotent refresh: a trigger with the same label + path-label signature is
    # already cached → update it in place and skip the stitch (no new sl{n} states).
    sig = _trigger_sig(live_rec.get("trigger"), live_rec.get("pathStructured"))
    for t in graph["triggers"]:
        if _trigger_sig(t.get("label"), t.get("path")) == sig:
            t["path"] = live_rec.get("pathStructured") or []
            t["form"] = live_rec.get("form")
            t["opensAt"] = live_rec.get("opensAt")
            t["cachedAt"] = now_iso
            t["cacheSource"] = "live"
            t.pop("staleWarning", None)
            meta = graph.setdefault("meta", {})
            meta["host"] = host
            meta["states"], meta["edges"], meta["triggers"] = (
                len(graph["states"]), len(graph["edges"]), len(graph["triggers"]))
            meta["lastLiveUpdate"] = now_iso
            meta["liveHits"] = meta.get("liveHits", 0) + 1
            atomic_write(host, graph)
            return {"states": meta["states"], "edges": meta["edges"], "triggers": meta["triggers"]}

    new_states, new_edges, new_trigger = stitch(live_rec, graph)

    # states: every entry in new_states is a freshly-synthesized node with a distinct
    # id (stitch reused existing states in place rather than re-adding them), so we
    # keep them all — even when a URL duplicates an existing state, the chain needs
    # its own ids. state_index is keyed by URL here (live additions); howto.py reads
    # the states list directly, so this only feeds graph tooling.
    have_ids = {s["id"] for s in graph["states"]}
    for st in new_states:
        if st["id"] in have_ids:
            continue
        graph["states"].append(st)
        graph["state_index"][_norm(st["url"])] = st["id"]
        have_ids.add(st["id"])

    # edges: dedup by (from, to, selector).
    eseen = {(e.get("from"), e.get("to"), e.get("selector")) for e in graph["edges"]}
    for e in new_edges:
        k = (e.get("from"), e.get("to"), e.get("selector"))
        if k in eseen:
            continue
        eseen.add(k)
        graph["edges"].append(e)

    # No existing trigger matched the signature (checked above) — this is genuinely new.
    graph["triggers"].append(new_trigger)

    meta = graph.setdefault("meta", {})
    meta["host"] = host
    meta["states"] = len(graph["states"])
    meta["edges"] = len(graph["edges"])
    meta["triggers"] = len(graph["triggers"])
    meta["lastLiveUpdate"] = now_iso
    meta["liveHits"] = meta.get("liveHits", 0) + 1

    atomic_write(host, graph)
    return {"states": meta["states"], "edges": meta["edges"], "triggers": meta["triggers"]}
