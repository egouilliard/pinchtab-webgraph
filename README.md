# 🕸️ pinchtab-webgraph

***Turn any website into a queryable navigation + content graph, then answer "how do I do X?" as the shortest click-path — deterministically, with no LLM in the runtime.***

![License](https://img.shields.io/badge/license-MIT-3DA639?style=flat-square)
![Python](https://img.shields.io/badge/python-3.10+-3776AB?style=flat-square&logo=python&logoColor=white)
![No LLM at runtime](https://img.shields.io/badge/runtime-no%20LLM-6E56CF?style=flat-square)
![Browser automation](https://img.shields.io/badge/driver-PinchTab-FF6B00?style=flat-square)
![Graph viewer](https://img.shields.io/badge/viewer-Cytoscape.js-F7A81B?style=flat-square)

`pinchtab-webgraph` drives a **real, JavaScript-rendered browser** (through the [PinchTab](#-requirements) automation CLI) to map an entire web app — every page, SPA state, button, tab, menu, form, and data collection — into a structured **navigation + content graph**. It then answers questions *offline* against that graph in milliseconds: shortest click-path between two views, "how do I create a X" (with the target form's fields read straight from the live UI), and "where does this data live and how do I reach it".

The whole pipeline is **deterministic** — structural heuristics only (ARIA roles, repeated-sibling detection, URL grouping), no model in the loop, no per-run cost, reproducible output. It works on *any* site: there is no app-specific vocabulary anywhere in the crawler.

## ✨ Highlights

- **Graph from anything** — one crawl records each state's full control inventory (links / buttons / tabs / menus) **and** its data collections (tables, grids, trees, lists, feeds, virtualized/scroll-loaded content). A complete nav + content graph of any site, all from structural signals.
- **Offline "how-to" in milliseconds** — BFS over the crawled graph returns the shortest click-path to any action *plus* the fields of the form it opens, in ~60–130 ms with zero browser calls.
- **Safe by construction** — discovery opens and reads forms, then presses Escape. It never submits, saves, or deletes anything. Destructive-looking controls are skipped and recorded, not clicked.
- **Never loses progress** — atomic checkpoints every N states plus a SIGINT/SIGTERM handler; a crash, OOM, or Ctrl-C keeps the partial graph. `meta.stopped` always says *why* a crawl ended (complete vs. truncated) — no silent truncation.
- **Spans app boundaries** — `--cross-host` follows links and `iframe[src]` into other hosts as graph nodes, so an embedded/linked app becomes part of the same graph. `--single-url` drives app-shell SPAs (e.g. Teams-style apps that swap views without changing the URL).
- **Cache-first workflow** — `ask.py` answers from a per-host cache when it can, falls back to a live discovery on a miss, and writes the result back so the next ask is an offline hit.
- **No LLM in the runtime** — indexing and path-finding are pure Python + the PinchTab CLI. Predictable, reproducible, free to re-run.

## 📑 Table of Contents

- [Why a web-navigation graph?](#-why-a-web-navigation-graph)
- [Requirements](#-requirements)
- [Quickstart](#-quickstart)
- [The tools](#️-the-tools)
- [How interaction crawling works](#-how-interaction-crawling-works)
- [Architecture](#️-architecture)
- [Graph shape](#-graph-shape)
- [Safety model](#-safety-model)
- [Importing into Neo4j](#-importing-into-neo4j-optional)
- [Roadmap](#️-roadmap)
- [Contributing](#-contributing)
- [License](#-license)

## 🧠 Why a web-navigation graph?

Automating or documenting a web app usually means one of two brittle things: hand-writing selectors that rot on every redesign, or asking an LLM to "figure out the UI" live on every request (slow, non-deterministic, and expensive).

`pinchtab-webgraph` takes a different stance: **crawl the UI once into a graph, then query the graph.**

- **How-to guides & onboarding** — "how do I create a template / an invoice / a new team?" becomes a shortest-path query that returns the exact clicks *and* the form fields, in milliseconds.
- **Change detection & QA** — snapshot the full control + content graph, then diff two crawls to see what moved, appeared, or disappeared.
- **Site maps for humans and agents** — a structured, low-noise map of an app's real navigation, far cheaper than replaying a browser for every question an agent asks.
- **Content discovery** — `--find TEXT` searches every view's captured data (rows / files / messages / cards) and returns what matched, which view it's in, and the click-path to get there.

## 📦 Requirements

- **Python 3.10+** — the tools are pure Python, no third-party dependencies.
- **The [PinchTab](https://github.com/) browser-automation CLI** available on your `PATH` as `pinchtab`. Every tool drives the live browser through it; you run an **isolated** PinchTab bridge (own profile, own port) so a "click-everything" crawl never touches a browser holding a live session you care about.

## 📦 Install

Pure Python (stdlib only) — the one runtime prerequisite is the external [PinchTab](#-requirements) CLI.

```bash
# from GitHub (no PyPI account needed):
pipx install git+https://github.com/egouilliard/pinchtab-webgraph
#   or:  uv tool install git+https://github.com/egouilliard/pinchtab-webgraph
#   or:  pip install git+https://github.com/egouilliard/pinchtab-webgraph
```

This installs the **`pinchtab-webgraph`** command (short alias **`pwg`**) with subcommands
`crawl · howto · ask · recipe · linkcrawl · paths`. Run `pinchtab-webgraph --help` for the map.

## 🚀 Quickstart

```bash
# 1. Start the isolated crawl browser (own profile/port). Leave it running.
#    (a PinchTab bridge — see Requirements; a helper script lives in the repo)

# 2a. Full interaction + content graph of an app (the main tool):
pinchtab-webgraph crawl https://app.example.com/dashboard --out app     # (pwg crawl …)

# 2b. …or a page→page link graph + interactive Cytoscape viewer:
pinchtab-webgraph linkcrawl https://docs.example.com --interaction-depth 0 --out docs
xdg-open docs.html

# 3. Ask the graph, offline, in milliseconds:
pwg howto app.json --goal "create template"     # shortest click-path + form spec
pwg howto app.json --find "invoice"             # where does this data live + how to reach it
pwg howto app.json --list-content               # per-view data inventory
```

Graphs and screenshots are written to the current working directory. From a git checkout you can
also run any tool without installing: `python3 -m pinchtab_webgraph.cli crawl …`.

`run-*.sh` forward the bridge auth token automatically and point at the isolated browser. Copy `crawl-config.example.json` to `crawl-config.json` and set a real token (`openssl rand -hex 24`) before the first run — `crawl-config.json` is gitignored because it holds that token.

## 🛠️ The tools

| Tool | What it does |
| --- | --- |
| `interaction_crawl.py` / `run-crawl-interactions.sh <url>` | **The core.** Crawls the live UI once into an interaction graph: states + action edges + every create-trigger's form spec. Full **capture-all is the default** — control inventory *and* data collections per state. Atomic checkpoints (never loses progress), explicit truncation reasons in `meta.stopped`. Modes: `--single-url` (app-shell SPAs), `--cross-host` (follow links + iframes to other hosts). Safe: opens and reads forms, never submits. |
| `howto.py <graph.json>` | **Offline** BFS over a crawled graph → shortest click-path + form spec in ms, no browser. `--goal "…"` for actions; `--find TEXT` searches captured data → what matched, which view, and the path to it; `--list-content` = per-view data inventory. |
| `ask.py` / `run-ask.sh` | **Cache-first** entry point. Routes by host to a per-host cache, answers offline via `howto.py`; on a miss runs a live discovery, then writes the result back so the next ask is an offline hit. `--verify` re-checks live. |
| `recipe.py` / `run-recipe.sh` | **Live** how-to finder: priority-BFS over the running UI to a goal's trigger, opens the form, reads the fields, never submits. The live fallback for cache misses. |
| `crawl.py` / `run-crawl.sh <url>` | Page→page **link graph** → `<out>.json` + a self-contained Cytoscape.js `<out>.html` viewer. |
| `paths.py` | Offline shortest / all click-paths over a crawled link graph (`--from`, `--to`, `--structural`, `--all`). |

## 🔎 How interaction crawling works

For each state the crawler reads every link and clickable widget (stable structural CSS selectors, not framework-generated refs), plus the state's data collections. Then, for each non-skipped widget, it **re-materializes** the state (replay the click-path from a known start), clicks the widget, and classifies the result:

- **navigated** (URL changed) → a page edge; enqueue the new page.
- **DOM changed, same URL** → a new SPA/state node + edge, recursed into up to the interaction depth.
- **create-trigger** → the form/modal is opened, its fields are read (label / type / required / options / confirm button), then Escape — nothing is persisted.
- **no change** → ignored.

Re-materializing per probe keeps every click starting from a known state and avoids stale element references across reloads. State signatures fold in ARIA view markers so same-URL views don't collapse into one node.

## 🏗️ Architecture

```
   Any website              PinchTab (real browser)          Graph                 Query
 ┌──────────────┐      ┌─────────────────────────┐    ┌────────────────┐    ┌──────────────┐
 │ pages        │ ───► │ read controls + content  │──► │ states         │──► │ howto.py     │
 │ SPA states   │      │ click widgets            │    │ + action edges │    │  (offline    │
 │ forms        │      │ open forms (read-only)    │    │ + form specs   │    │   BFS, ms)   │
 │ tables/grids │      │ scroll virtualized data   │    │ + collections  │    │ ask.py cache │
 └──────────────┘      └─────────────────────────┘    └───────┬────────┘    │ paths.py     │
   structural signals    isolated bridge, safe             checkpointed      │ Cytoscape UI │
   only (ARIA, siblings) never submits/saves               (atomic write)    └──────────────┘
```

Everything runs locally against your own isolated browser bridge. The pipeline is deterministic — no LLM in the indexing or path-finding path — and every crawl flushes atomic checkpoints so a kill never loses work.

## 🧩 Graph shape

The JSON graph is `{ nodes, edges, meta }`:

- **Nodes** — **pages** (by normalized URL) and **SPA/modal states** (same URL, changed DOM). Cross-host mode adds `external` / `iframe` nodes. Each node can carry its **control inventory** and its **content collections**.
- **Edges** — **links** (navigation) and **actions/clicks**. Destructive-looking actions that were deliberately skipped are recorded as dashed edges so you can see what was avoided.
- **meta** — crawl parameters plus `meta.stopped`: `frontier-exhausted` (complete) vs. `hit-max-*` / `wedge` (truncated). Truncation is always explicit.

## 🛡️ Safety model

- **Same-origin by default** — the crawler won't wander off the target site unless you pass `--cross-host`.
- **Never mutates data** — discovery opens and reads forms, then Escapes. Create / save / delete / submit controls are skipped by default and recorded, not clicked. **Never run a "click everything" crawl in an authenticated session you care about** — that's exactly why the isolated bridge exists.
- **Hard caps** on states, actions-per-state, interaction depth, and a global action budget prevent the classic SPA state explosion.
- **Secrets stay out of git** — `crawl-config.json` (bridge token) and `.instance/` (live browser profile/session) are gitignored. Commit explicit source files only.

## 🗄️ Importing into Neo4j (optional)

The JSON maps directly to a property graph:

```cypher
// after: WITH the json loaded as $g
UNWIND $g.nodes AS n
  MERGE (p:Page {id:n.id}) SET p.url = n.url, p.title = n.title, p.type = n.type;
UNWIND $g.edges AS e
  MATCH (a:Page {id:e.source}), (b:Page {id:e.target})
  MERGE (a)-[r:NAV {label:e.label, kind:e.kind}]->(b);
```

## 🛣️ Roadmap

- Auto-detect single-URL app-shell mode (no `--single-url` flag).
- Form-reading inside single-URL apps (currently disabled there for safety).
- Sub-10s cold-start live discovery for cache misses.
- Richer content queries surfaced through `ask.py` (cross-host collections).

## 🤝 Contributing

PRs welcome. The repository uses protected branches:

- **`main`** — production-ready code. All changes land here via PR.
- **`release`** — release-candidate branch; stabilisation before tagging.
- **`hotfix`** — urgent fixes that need to skip the normal cycle.
- **`dev`** — day-to-day integration branch (unprotected).

Every PR into `main`, `release`, or `hotfix` requires a Code Owner review (see [`CODEOWNERS`](CODEOWNERS)), and force-pushes and deletions are blocked on those branches. The one hard rule for code: **stay generic** — no hardcoded app routes, labels, or vocabulary in the crawler; structural heuristics only. Please open an issue before a large refactor.

## 📄 License

Licensed under the [MIT License](LICENSE). Copyright © 2026 Edouard Gouilliard.
