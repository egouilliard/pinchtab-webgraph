#!/usr/bin/env python3
"""
The unified, cache-first "how do I do X?" entry point.

Makes the default workflow query-cache → on-miss-run-live → write-result-back, so
repeat questions for the same host answer OFFLINE in milliseconds and the cache
fills itself in on the first miss:

  1. route by hostname → caches/<host>.json,
  2. if a cache exists, answer from it via howto.py (0 browser calls),
  3. on a miss (or with --verify), run live discovery via recipe.py against the
     crawl browser, then stitch the result back into the cache (cache_store.merge).

This script makes NO direct browser calls itself — it only shells out to howto.py
(offline) and recipe.py (live). Generic: routing is by URL hostname, nothing
app/section-specific.

--find / --list-content also accept --all-hosts to search EVERY per-host cache
offline in one shot, merging results ranked + labeled by origin host.

  scripts/run-ask.sh --goal "add item"   --start https://app.example.com/home
  scripts/run-ask.sh --goal "create team" --start https://app/dashboard --verify
  scripts/run-ask.sh --find "invoice" --all-hosts    # every cached host, offline
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import tempfile
from urllib.parse import urlparse

from . import cache_store

DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.environ.get("PINCHTAB_CONFIG", "crawl-config.json")


def _goal_nouns(goal):
    return [w for w in (goal or "").lower().split()
            if w not in ("a", "the", "create", "add", "new", "make", "to")]


def _mark_stale(host, goal):
    # --verify could not confirm a cached answer live: flag the matching trigger(s)
    # so a reader knows the answer may be out of date — but NEVER delete it (a failed
    # live check is more often a wedged browser than a real UI change).
    graph = cache_store.load(host)
    if not graph:
        return
    nouns = _goal_nouns(goal)
    hit = False
    for t in graph.get("triggers", []):
        lab = (t.get("label") or "").lower()
        if any(n in lab for n in nouns):
            t["staleWarning"] = True
            hit = True
    if hit:
        cache_store.atomic_write(host, graph)


def _warn_errored(hosts):
    # An unreadable cache contributes nothing; saying so keeps a partial answer from
    # reading as a complete one (a silent skip would understate the real result set).
    if hosts:
        print("\n⚠ %d cache(s) could not be read and were skipped: %s"
              % (len(hosts), ", ".join(hosts)))


def _print_find_hosts(res):
    q = res["query"]
    n_hosts = len(res["hosts_matched"])
    if res["status"] != "ok":
        print("✗ No captured data matches %r across %d host cache(s)."
              % (q, len(res["hosts_searched"])))
        print("  (searched every host's data collections; re-crawl with --capture-content if stale)")
        _warn_errored(res.get("hosts_errored"))
        return
    print("=== FOUND %d item(s) matching %r across %d host(s), %d view(s) ==="
          % (res["total_matches"], q, n_hosts, res["views_matched"]))
    for v in res["views"]:
        print("\n▸ [%s] %s  (%s)" % (v["host"], v.get("view_label") or "(root)",
                                          v.get("view_url") or "?"))
        if not v.get("reachable"):
            print("   ⚠ no cached click-path from this host's root")
        elif v.get("steps"):
            print("   route (%d click%s): %s"
                  % (v["distance_clicks"], "" if v["distance_clicks"] == 1 else "s",
                     "  →  ".join(v["steps"])))
        for it in v["items"]:
            print("     • [%s] %s" % (it.get("kind"), (it.get("text") or "")[:110]))
    # NO SILENT CAPS: say so when --limit dropped items or whole views, otherwise the
    # header ("N items across M views") reads as complete when it isn't.
    if res.get("views_omitted") or any(v.get("truncated") for v in res["views"]):
        print("\n   … showing %d of %d item(s) across %d of %d view(s) — raise --limit to see more"
              % (res["shown"], res["total_matches"], len(res["views"]), res["views_matched"]))
    _warn_errored(res.get("hosts_errored"))
    print("\n(answered offline from %d host cache(s) — 0 browser calls)"
          % len(res["hosts_searched"]))


def _print_list_hosts(res):
    if res["status"] != "ok":
        print("No content captured across %d host cache(s) — re-crawl with --capture-content."
              % len(res["hosts"]))
        return
    for h in res["hosts"]:
        if h["status"] == "error":
            print("▸ [%s]  ⚠ unreadable cache: %s" % (h["host"], h.get("error")))
            continue
        if h["status"] != "ok":
            continue
        print("▸ [%s]" % h["host"])
        for v in h["views"]:
            print("    %s  (%s)" % (v.get("view_label") or "(root)", v.get("view_url") or "?"))
            for c in v["collections"]:
                print("      [%-16s] %4d items   e.g. %s"
                      % (c.get("kind"), c.get("count", 0), (c.get("sample") or "")[:60]))


def main():
    ap = argparse.ArgumentParser(description="Cache-first how-to: query cache, fall back to live, write back")
    ap.add_argument("--goal", help='what to do, e.g. "add item" / "create team"')
    ap.add_argument("--find", help="search the captured DATA/content (offline, cache-only — no live)")
    ap.add_argument("--list-content", action="store_true",
                    help="inventory the captured data collections per view (offline)")
    ap.add_argument("--limit", type=int, help="max content matches to show (with --find)")
    ap.add_argument("--start", help="start URL (routes the cache by hostname; optional with --all-hosts)")
    ap.add_argument("--all-hosts", action="store_true",
                    help="search EVERY per-host cache (with --find/--list-content); offline, ranked+labeled by origin host")
    ap.add_argument("--verify", action="store_true",
                    help="print the cached answer, then re-run live and refresh the cache")
    ap.add_argument("--server", default="http://localhost:9871")
    ap.add_argument("--graph", help="explicit cache file (bypasses host routing)")
    ap.add_argument("--out", help="basename for the live result JSON (default: a temp file)")
    # unknown args are forwarded ONLY to the live recipe.py run (e.g. --max-discover,
    # --max-depth, --match) — howto.py never sees them.
    a, extra = ap.parse_known_args()

    if not (a.goal or a.find or a.list_content):
        ap.error("pass --goal, --find, or --list-content")

    if a.all_hosts and not (a.find or a.list_content):
        ap.error("--all-hosts only applies to --find / --list-content")
    if a.all_hosts and a.graph:
        ap.error("--all-hosts searches every host cache; it cannot be combined with --graph")
    if not a.all_hosts and not a.start:
        ap.error("--start is required (or pass --all-hosts for a cross-host content query)")

    # CROSS-HOST content query: search EVERY per-host cache offline, rank+label by origin.
    # (Content has no live equivalent, so this stays cache-only like the single-host --find.)
    if a.all_hosts:
        from . import api
        host_paths = [(h, cache_store.cache_path(h)) for h in cache_store.list_hosts()]
        if not host_paths:
            sys.exit("No host caches yet — crawl a site first: "
                     "pinchtab-webgraph crawl --start <url> --capture-content")
        if a.find:
            res = api.find_content_hosts(host_paths, a.find, limit=a.limit or 40)
            _print_find_hosts(res)
            sys.exit(0 if res["status"] == "ok" else 2)
        res = api.list_content_hosts(host_paths)   # a.list_content
        _print_list_hosts(res)
        sys.exit(0)

    host = urlparse(a.start).hostname
    if not a.graph and not host:
        ap.error("--start must be a full URL including a scheme, e.g. https://example.com")
    cache_file = a.graph or cache_store.cache_path(host)
    cache_exists = os.path.exists(cache_file)
    howto_py = os.path.join(DIR, "howto.py")
    recipe_py = os.path.join(DIR, "recipe.py")

    # CONTENT queries (--find / --list-content) are answered from the cache ONLY — there is
    # no live equivalent (recipe.py finds action-paths, not data), so no fallback/write-back.
    if a.find or a.list_content:
        if not cache_exists:
            sys.exit("No cache for %s yet — crawl it first: interaction_crawl.py "
                     "--start %s --capture-content" % (host, a.start))
        cmd = [sys.executable, "-m", "pinchtab_webgraph.howto", cache_file]
        if a.find:
            cmd += ["--find", a.find, "--start", a.start]
        if a.list_content:
            cmd += ["--list-content"]
        if a.limit:
            cmd += ["--limit", str(a.limit)]
        sys.exit(subprocess.run(cmd).returncode)

    # 1) CACHE-FIRST: answer offline unless the user forced a live re-check.
    if cache_exists and not a.verify:
        rc = subprocess.run([sys.executable, "-m", "pinchtab_webgraph.howto", cache_file,
                             "--goal", a.goal, "--start", a.start]).returncode
        if rc == 0:
            return                       # cache hit — done, 0 browser calls
        if rc != 2:
            sys.exit(rc)                 # real error (not a miss) — propagate
        # rc == 2 → cache miss; fall through to live discovery
    elif cache_exists and a.verify:
        subprocess.run([sys.executable, "-m", "pinchtab_webgraph.howto", cache_file,
                        "--goal", a.goal, "--start", a.start])
        print("--- verifying live ---", file=sys.stderr)

    # 2) LIVE: recipe.py drives the crawl browser. Pass the same token/config
    #    scripts/run-recipe.sh exports, so a direct `python3 pinchtab_webgraph/ask.py` works too.
    env = dict(os.environ)
    try:
        env["PINCHTAB_TOKEN"] = json.load(open(CONFIG))["server"]["token"]
        env["PINCHTAB_CONFIG"] = CONFIG
    except Exception:
        pass

    # recipe.py writes <out>.json to the CURRENT WORKING DIR (basename; we read it back
    # own dir), so --out is a basename, not a path. Use a unique temp basename when
    # the caller didn't pick one, and clean it up after we read it back.
    if a.out:
        out_base, cleanup = a.out, False
    else:
        out_base = os.path.basename(tempfile.mktemp(prefix=".ask-", dir=tempfile.gettempdir()))
        cleanup = True
    result_json = os.path.abspath(out_base + ".json")

    rc = subprocess.run([sys.executable, "-m", "pinchtab_webgraph.recipe", "--goal", a.goal, "--start", a.start,
                         "--out", out_base, "--server", a.server] + extra, env=env).returncode
    if rc != 0:
        # Do NOT touch the cache on a live failure.
        if a.verify and cache_exists:
            print("! Live check failed — cached answer may be stale", file=sys.stderr)
            _mark_stale(host, a.goal)
        else:
            print("Live discovery failed — is the crawl browser running? "
                  "See runbook.md step 1.", file=sys.stderr)
        sys.exit(rc)

    # 3) WRITE-BACK: stitch the live result into the cache so the next ask hits.
    try:
        with open(result_json) as f:
            live_rec = json.load(f)
        counts = cache_store.merge(host, live_rec,
                                   datetime.datetime.now(datetime.timezone.utc).isoformat())
        print("Cache updated: %s.json (%d states, %d edges, %d triggers)."
              % (host, counts["states"], counts["edges"], counts["triggers"]), file=sys.stderr)
    finally:
        if cleanup:
            try:
                os.remove(result_json)
            except OSError:
                pass


if __name__ == "__main__":
    main()
