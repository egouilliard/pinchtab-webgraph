#!/usr/bin/env python3
"""
Inspect and manage the per-host interaction-graph caches that ask.py writes back.

  pinchtab-webgraph cache list                 # every cached host + a one-line summary
  pinchtab-webgraph cache path <host>          # print the cache file path (even if absent)
  pinchtab-webgraph cache show <host>          # pretty-print that host's meta
  pinchtab-webgraph cache clear <host>         # DRY RUN — shows what would be removed
  pinchtab-webgraph cache clear <host> --yes   # actually remove it
  pinchtab-webgraph cache clear --all --yes    # remove every cache

`clear` is DESTRUCTIVE, so it defaults to a dry run and only deletes with --yes.
`--home DIR` overrides PINCHTAB_WEBGRAPH_HOME, e.g. to point at a repo-local
`./caches/` (pass the directory that CONTAINS `caches/`).

Generic + stdlib only: routing is by hostname, nothing app-specific.
"""
import argparse
import json
import os
import sys

from . import cache_store


def _meta_summary(host):
    g = cache_store.load(host)
    if not g:
        return "(no cache / unreadable)"
    m = g.get("meta", {})
    last = ("  last live: %s" % m["lastLiveUpdate"]) if m.get("lastLiveUpdate") else ""
    return "%d states, %d edges, %d triggers%s" % (
        m.get("states", 0), m.get("edges", 0), m.get("triggers", 0), last)


def main():
    ap = argparse.ArgumentParser(description="Manage per-host interaction-graph caches")
    ap.add_argument("action", choices=["list", "path", "show", "clear"],
                    help="list hosts / print a host's cache path / show its meta / clear (delete)")
    ap.add_argument("host", nargs="?", help="host for path / show / clear")
    ap.add_argument("--all", action="store_true", help="clear: target EVERY cached host")
    ap.add_argument("--yes", action="store_true",
                    help="clear: actually delete (clear is a dry run without this)")
    ap.add_argument("--home", help="override PINCHTAB_WEBGRAPH_HOME (dir that contains caches/)")
    a = ap.parse_args()

    if a.home:
        os.environ["PINCHTAB_WEBGRAPH_HOME"] = os.path.expanduser(a.home)

    # cache_store.cache_path() raises ValueError on a non-hostname token (e.g. a
    # path-traversal attempt like "../../etc/passwd") — surface it as a clean
    # error + nonzero exit instead of leaking a traceback.
    try:
        return _dispatch(a)
    except ValueError as e:
        sys.stderr.write("error: %s\n" % e)
        return 2


def _dispatch(a):
    if a.action == "list":
        hosts = cache_store.list_hosts()
        if not hosts:
            print("No caches in %s" % cache_store.caches_dir())
            return 0
        print("Caches in %s:\n" % cache_store.caches_dir())
        for h in hosts:
            print("  %-40s  %s" % (h, _meta_summary(h)))
        return 0

    if a.action == "path":
        if not a.host:
            sys.exit("cache path: needs a <host>")
        print(cache_store.cache_path(a.host))
        return 0

    if a.action == "show":
        if not a.host:
            sys.exit("cache show: needs a <host>")
        g = cache_store.load(a.host)
        if not g:
            sys.stderr.write("no cache for host %r (looked in %s)\n"
                             % (a.host, cache_store.caches_dir()))
            return 1
        print(json.dumps(g.get("meta", {}), indent=2))
        return 0

    # clear — DESTRUCTIVE, dry-run unless --yes
    if a.all:
        targets = cache_store.list_hosts()
    elif a.host:
        targets = [a.host]
    else:
        sys.exit("cache clear: pass a <host> or --all")

    if not targets:
        print("No caches to clear.")
        return 0

    if not a.yes:
        print("DRY RUN — would remove %d cache(s) (pass --yes to delete):" % len(targets))
        for h in targets:
            exists = os.path.exists(cache_store.cache_path(h))
            print("  %s%s" % (h, "" if exists else "  (no file — nothing to remove)"))
        return 0

    removed = [h for h in targets if cache_store.clear(h)]
    print("Removed %d cache(s): %s" % (len(removed), ", ".join(sorted(removed)) or "(none)"))
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
