#!/usr/bin/env python3
"""Unified CLI for pinchtab-webgraph.

`pinchtab-webgraph <subcommand> [args...]` (short alias: `pwg`). Each subcommand
forwards its args to the corresponding module's existing argparse `main()`.

Prerequisite: the external PinchTab CLI (`pinchtab`, v0.10.0+) must be installed
and a bridge running — this toolkit drives the browser through it. See the README.
"""
import importlib
import sys

from . import __version__

# subcommand -> (module, one-line help). Order = the natural workflow.
SUBS = {
    "crawl":     ("pinchtab_webgraph.interaction_crawl",
                  "crawl a site ONCE into a nav+content graph (states, actions, forms, data) — the main tool"),
    "login":     ("pinchtab_webgraph.login",
                  "open a persistent browser session and sign in to a host (keyring-backed) for authenticated crawls"),
    "howto":     ("pinchtab_webgraph.howto",
                  "query a graph OFFLINE: --goal <how-to> / --find <data> / --list-content"),
    "ask":       ("pinchtab_webgraph.ask",
                  "cache-first how-to: answer offline, else run live and write back"),
    "recipe":    ("pinchtab_webgraph.recipe",
                  "LIVE how-to finder — drive the running UI to a goal's trigger and read its form"),
    "perform":   ("pinchtab_webgraph.perform",
                  "PERFORM a how-to live — run the compiled pinchtab block (download/upload/fill); safe by default"),
    "linkcrawl": ("pinchtab_webgraph.crawl",
                  "page->page LINK graph + a self-contained Cytoscape HTML viewer"),
    "paths":     ("pinchtab_webgraph.paths",
                  "offline shortest / all click-paths over a crawled link graph"),
    "query":     ("pinchtab_webgraph.query_cmd",
                  "query a graph and print JSON — machine-readable howto/find/list/paths; the UTCP substrate"),
    "cache":     ("pinchtab_webgraph.cache_cmd",
                  "manage the per-host interaction-graph caches: list / path / show / clear"),
    "test":      ("pinchtab_webgraph.selftest",
                  "interactively self-test a crawled graph → HTML report → opt-in GitHub issue"),
    "manual":    ("pinchtab_webgraph.utcp_manual",
                  "print / serve the UTCP tool-calling manual for external tool-callers"),
}
# NOTE: `query` slots after `paths` (it is the offline query surface); `manual`
# is appended last (it advertises the query surface to external tool-callers).


def _help():
    print("pinchtab-webgraph %s — map ANY website into a nav+content graph, query it offline.\n"
          % __version__)
    print("usage: pinchtab-webgraph <command> [args...]   (alias: pwg)\n")
    print("commands:")
    for name, (_, doc) in SUBS.items():
        print("  %-10s %s" % (name, doc))
    print("\nRun 'pinchtab-webgraph <command> --help' for a command's options.")
    print("Requires the external `pinchtab` CLI (v0.10.0+) with a bridge running — see the README.")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        _help()
        return 0
    if argv[0] in ("-V", "--version", "version"):
        print("pinchtab-webgraph %s" % __version__)
        return 0
    sub = argv[0]
    if sub not in SUBS:
        sys.stderr.write("pinchtab-webgraph: unknown command %r\n\n" % sub)
        _help()
        return 2
    module = importlib.import_module(SUBS[sub][0])
    # each module's main() reads sys.argv via argparse; present a clean prog name
    sys.argv = ["pinchtab-webgraph " + sub] + argv[1:]
    return module.main() or 0


if __name__ == "__main__":
    sys.exit(main())
