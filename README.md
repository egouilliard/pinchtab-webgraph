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
- [MCP server](#-mcp-server)
- [UTCP interface](#-utcp-interface)
- [How interaction crawling works](#-how-interaction-crawling-works)
- [Architecture](#️-architecture)
- [Graph shape](#-graph-shape)
- [Safety model](#-safety-model)
- [Authenticated apps (login)](#-authenticated-apps-login)
- [Importing into Neo4j](#-importing-into-neo4j-optional)
- [Roadmap](#️-roadmap)
- [Contributing](#-contributing)
- [Star History](#-star-history)
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
| `login.py` (`pinchtab-webgraph login`) | Open a persistent browser session and sign in to a host (credentials from the OS keyring) so subsequent crawls run authenticated. Needs the optional `login` extra (`keyring`). |
| `cache_cmd.py` (`pinchtab-webgraph cache`) | Inspect / manage the per-host interaction-graph caches `ask.py` writes back: `cache list`, `cache path <host>`, `cache show <host>`, `cache clear <host>` / `--all` (destructive, dry-run unless `--yes`). |
| `query_cmd.py` (`pinchtab-webgraph query`) | **Machine-readable** twin of `howto.py` / `paths.py`: runs the offline `api.*` queries (`graph_summary`, `howto`, `find_content`, `list_content`, `list_forms`, `link_paths`) and prints the result as JSON on stdout. Takes `--host` (cache) or `--graph` (path). The substrate the UTCP manual shells out to. |
| `utcp_manual.py` (`pinchtab-webgraph manual`) | Build / print / serve the [UTCP](https://www.utcp.io) tool-calling manual so external tool-callers can invoke the `query` (and live `crawl`/`ask`) surface by running the CLI directly — no wrapper server. `manual --out FILE` / `manual --serve`. |

## 🔌 MCP server

An optional [Model Context Protocol](https://modelcontextprotocol.io) server exposes
the same offline queries — plus two live browser-driven tools — to any MCP client
(Claude Desktop, Claude Code, …). It's a thin binding onto the `api.py` query surface,
so answers are identical. The base install stays **mcp-free**: the server lives behind
its own extra and console script, and nothing in the base package imports it.

```bash
pip install 'pinchtab-webgraph[mcp]'   # on Ubuntu/PEP-668: add --user --break-system-packages, or use a venv
```

```json
{ "mcpServers": { "pinchtab-webgraph": { "command": "pinchtab-webgraph-mcp" } } }
```

- **Offline tools** (`graph_summary`, `howto`, `find_content`, `list_content`,
  `list_forms`, `link_paths`) take either `host=` (cache routing) or `graph=` (a path);
  no browser, no network.
- **Resources** `graph://hosts`, `graph://{host}/summary`, `graph://{host}` browse the
  interaction-graph cache.
- **Live tools** `crawl` (replaces a host's cache) and `ask_howto` (cache-first,
  merges) need a running PinchTab bridge; offline tools don't. The crawler's
  restart/login shell hooks are **operator-only** (env/config), never tool parameters.

Full inventory, env vars, and `.mcp.json` example: **[docs/mcp-server.md](docs/mcp-server.md)**.

## 🔌 UTCP interface

Prefer to call the CLI directly, with **no server running**? `pinchtab-webgraph` also
ships a [UTCP](https://www.utcp.io) manual: a description of each tool's JSON-schema
inputs/outputs plus the exact `pwg …` command to run, with args injected as
`UTCP_ARG_<name>_UTCP_END`. A UTCP-aware caller runs the command itself — the same
`api.py` queries as MCP, no wrapper process. Manual generation is **pure stdlib**.

```bash
pwg manual                        # print the manual JSON
pwg manual --out utcp-manual.json # write it (a committed copy lives at repo root)
pwg manual --serve                # serve at /utcp + /.well-known/utcp (default :9872)

pwg query howto --host app.example.com --goal "create role"   # the substrate, prints JSON
```

The exposed surface is a deliberate **subset** — required core args only, `--host`
routing only — so every command string is placeholder-free. Full tool table, exit-code
convention, and endpoints: **[docs/utcp.md](docs/utcp.md)**.

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
- **OS-level sandbox (opt-in)** — run under [Claude Code's built-in sandbox](#-run-it-under-a-sandbox-recommended) to confine the crawler at the OS level: no keyring/SSH/cloud-cred reads, localhost-only egress, and no `sudo` escape. A ready posture ships in [`.claude/settings.json`](.claude/settings.json).

## 🔐 Authenticated apps (login)

Crawling a site that needs a login is **built in and opt-in** — two ways to authenticate,
then a sandbox to run the whole thing safely. Full reference, threat model, config, and
test steps: **[docs/authenticated-login.md](docs/authenticated-login.md)**.

### Two ways to log in (safest first)

1. **Log in by hand once (recommended, zero config).** Open the persistent bridge
   profile, sign in, and crawl — the session cookie lives in `.instance/` (gitignored)
   and the crawler reuses it. **Your password never touches this toolkit.** This is the
   safest path and needs nothing below.

2. **Automated login (opt-in), for unattended / long-running crawls.** When the bridge
   may restart mid-crawl or you're running on a schedule, enable keyring-backed login:

   ```bash
   pip install 'pinchtab-webgraph[login]'          # optional dependency, only for this
   cp login-config.example.json login-config.json  # gitignored — ROUTING only, no password
   keyring set pinchtab-webgraph you@example.com    # the password lives in the OS keyring
   interaction_crawl --start https://app.example.com/home --login-config login-config.json
   ```

   The password is read from the **OS keyring at runtime** — never from a file, the graph
   JSON, or logs (only its length is ever printed). `login-config.json` holds per-host
   routing (`url`, `username`, optional field selectors); login form fields are
   auto-detected from standard HTML (input `type` / `autocomplete` / DOM order), so most
   apps need no selectors. The same login is reused to re-authenticate after a bridge wedge.
   See **[docs/authenticated-login.md](docs/authenticated-login.md)** for the full config
   reference, security properties, limits (SSO/2FA are not automated), and test steps.

### 🧱 Run it under a sandbox (recommended)

On its own, keyring is only **at-rest** hygiene: any process running as your user — an AI
agent included, and certainly one with `sudo` — can read a keyring secret with one command.
The fix that actually confines the automation, **without a VM or container**, is to run the
crawler inside **[Claude Code's](https://claude.com/claude-code) built-in sandbox** (this
feature *requires* Claude Code). It uses OS primitives (`bubblewrap` on Linux, Seatbelt on
macOS) to enforce, at the OS level, what commands can read and which hosts they can reach —
and a sandboxed process runs in an unprivileged user namespace, so it **can't `sudo` out**.

This repo ships a locked-down posture in [`.claude/settings.json`](.claude/settings.json):
sandbox on, network egress limited to **localhost**, and reads **denied** for the OS keyring,
`~/.ssh`, cloud creds, and GitHub/npm tokens — so a compromised crawl can't read your secrets
or phone them home.

```bash
# Linux/WSL2 deps (macOS needs nothing extra):
sudo apt-get install bubblewrap socat
npm install -g @anthropic-ai/sandbox-runtime   # optional: seccomp unix-socket blocking
```

Then run `/sandbox` in Claude Code and crawl as usual. Add your target app's domain to
`.claude/settings.local.json` (gitignored) or approve it on the first prompt.

**Recommended safe combo:** the shipped default **denies the keyring**, so pair the sandbox
with **hand-login / session-reuse** (option 1) — the agent drives the authenticated session
but can't read your credentials at all. If you want automated keyring login, remove the
keyring deny locally and use a **dedicated bot account**.

> ⚠️ A strong risk-reducer, not a perfect wall: the proxy allow-lists by hostname without
> TLS inspection, localhost egress stays open (the crawler needs the local bridge), and a
> denylist is never exhaustive. Keep the allow-list tight and keep a **bot account** as your
> backstop. Full model: [threat model + sandbox setup](docs/authenticated-login.md#threat-model--read-this-before-trusting-keyring).

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

PRs welcome — see **[CONTRIBUTING.md](CONTRIBUTING.md)** for the full guide (branch model, commit conventions, PR checklist, issue reporting). The short version:

- Cut feature branches from **`dev`** and open PRs against it. `main`, `release`, and `hotfix` are protected — every PR into them needs a Code Owner review (see [`CODEOWNERS`](CODEOWNERS)); force-pushes and deletions are blocked.
- **The one hard rule: stay generic** — no hardcoded app routes, labels, or vocabulary in the crawler; structural heuristics only.
- Discovery stays **safe** (never submits) and **secrets stay out of git**. Please open an issue before a large refactor.

**Development.** The runtime is pure stdlib; tests use `pytest`, added via the `test` extra:

```bash
pip install -e '.[test]'   # editable install + pytest
pytest                     # runs the suite in tests/
```

## ⭐ Star History

<a href="https://star-history.com/#egouilliard/pinchtab-webgraph&Date">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=egouilliard/pinchtab-webgraph&type=Date&theme=dark" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=egouilliard/pinchtab-webgraph&type=Date" />
   <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=egouilliard/pinchtab-webgraph&type=Date" />
 </picture>
</a>

## 📄 License

Licensed under the [MIT License](LICENSE). Copyright © 2026 Edouard Gouilliard.
