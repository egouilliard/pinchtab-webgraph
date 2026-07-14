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
- **Runnable answers, not just directions** — every how-to also compiles the click-path + terminal action into a copy-pasteable **PinchTab command block** (`pinchtab nav … → click … → download/upload/fill …`). So "how do I download the Q3 report" returns both the route *and* a `pinchtab download <url> -o q3.pdf` you can run. Form answers become a `fill`/`select`/`check` script with the submit line left **commented out** (safety). See [Runnable command blocks](#️-runnable-command-blocks).
- **Declarative automation flows** — a how-to answers "how do I do X" and `perform` does it *once, in a straight line*. A **flow** is the layer above: a JSON **document** — not a script — executed by a step VM with `for_each` / `paginate` / `collect`, so "download all 20 PDFs on this page, then do it again for each of the 12 pages, and only keep the files I don't already have" is 12 lines of JSON. No code executes, so a scheduler or an HTTP handler can safely run one; every download is content-hashed against a **persistent dedupe ledger**, which turns a dumb poller into a change detector. See [Automation flows](#-automation-flows).
- **Download / export discovery** — download and export affordances become read-only `download` nodes: a link that resolves to a file (a `download` attribute, a file-extension path, or a `blob:`/`data:` URL → tagged with the file URL) or a button whose label is a download/export verb (JS-triggered). Like uploads, they are **never clicked** during the crawl — detected, recorded, and turned into a `pinchtab download`/`click` command on demand.
- **File-upload discovery** — the crawl also finds where you can upload a document. File inputs (including ones hidden behind a styled `<label>`/button) and `ondrop` dropzones become read-only `upload` nodes tagged with the file types they accept (e.g. `.pdf,.docx`, `image/*`), so "how do I upload a … ?" is answerable — and the crawler never clicks them (that would pop a native OS file dialog).
- **Safe by construction** — discovery opens and reads forms, then presses Escape. It never submits, saves, or deletes anything. Destructive-looking controls are skipped and recorded, not clicked.
- **Never loses progress** — atomic checkpoints every N states plus a SIGINT/SIGTERM handler; a crash, OOM, or Ctrl-C keeps the partial graph. `meta.stopped` always says *why* a crawl ended (complete vs. truncated) — no silent truncation.
- **Spans app boundaries** — `--cross-host` follows links and `iframe[src]` into other hosts as graph nodes, so an embedded/linked app becomes part of the same graph. `--single-url` drives app-shell SPAs (e.g. Teams-style apps that swap views without changing the URL).
- **Cache-first workflow** — `ask.py` answers from a per-host cache when it can, falls back to a live discovery on a miss, and writes the result back so the next ask is an offline hit.
- **No LLM in the runtime** — indexing and path-finding are pure Python + the PinchTab CLI. Predictable, reproducible, free to re-run.
- **Three ways to call it** — the exact same graph queries are reachable from a full **CLI**, a **Model Context Protocol (MCP)** server, and a **Universal Tool Calling Protocol (UTCP)** manual — all over one shared, importable core API. See [Three ways to call it](#-three-ways-to-call-it).

## 📑 Table of Contents

- [Why a web-navigation graph?](#-why-a-web-navigation-graph)
- [Requirements](#-requirements)
- [Quickstart](#-quickstart)
- [The tools](#️-the-tools)
- [Runnable command blocks](#️-runnable-command-blocks)
- [Automation flows](#-automation-flows)
- [Self-test & report](#-self-test--report)
- [Regression audit (10 public sites)](#-regression-audit-10-public-sites)
- [Three ways to call it](#-three-ways-to-call-it)
- [MCP server](#-mcp-server)
- [UTCP interface](#-utcp-interface)
- [Documentation](#-documentation)
- [How interaction crawling works](#-how-interaction-crawling-works)
- [Architecture](#️-architecture)
- [Graph shape](#-graph-shape)
- [Safety model](#️-safety-model)
- [Authenticated apps (login)](#-authenticated-apps-login)
- [Importing into Neo4j](#️-importing-into-neo4j-optional)
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
`crawl · howto · ask · recipe · linkcrawl · paths · test`. Run `pinchtab-webgraph --help` for the map.

## 🚀 Quickstart

```bash
# 1. Start the isolated crawl browser (own profile/port). Leave it running.
#    (a PinchTab bridge — see Requirements; a helper script lives in the repo)

# 2a. Full interaction + content graph of an app (the main tool):
pinchtab-webgraph crawl https://app.example.com/dashboard --out out/app     # (pwg crawl …)

# 2b. …or a page→page link graph + interactive Cytoscape viewer:
pinchtab-webgraph linkcrawl https://docs.example.com --interaction-depth 0 --out out/docs
xdg-open out/docs.html

# 3. Ask the graph, offline, in milliseconds:
pwg howto out/app.json --goal "create template"     # shortest click-path + form spec
pwg howto out/app.json --find "invoice"             # where does this data live + how to reach it
pwg howto out/app.json --list-content               # per-view data inventory
```

Graphs and screenshots default to the gitignored `out/` directory (e.g. `out/webgraph.json`, `out/recipe.png`); pass `--out <path>` to change it — parent dirs are created for you. From a git checkout you can also run any tool without installing: `python3 -m pinchtab_webgraph.cli crawl …`.

The `scripts/run-*.sh` wrappers forward the bridge auth token automatically and point at the isolated browser. Copy `crawl-config.example.json` to `crawl-config.json` and set a real token (`openssl rand -hex 24`) before the first run — `crawl-config.json` is gitignored because it holds that token.

## 🛠️ The tools

| Tool | What it does |
| --- | --- |
| `interaction_crawl.py` / `scripts/run-crawl-interactions.sh <url>` | **The core.** Crawls the live UI once into an interaction graph: states + action edges + every create-trigger's form spec. Full **capture-all is the default** — control inventory *and* data collections per state. Atomic checkpoints (never loses progress), explicit truncation reasons in `meta.stopped`. Modes: `--single-url` (app-shell SPAs), `--cross-host` (follow links + iframes to other hosts). Safe: opens and reads forms, never submits. |
| `howto.py <graph.json>` | **Offline** BFS over a crawled graph → shortest click-path + form spec in ms, no browser. `--goal "…"` for actions; `--find TEXT` searches captured data → what matched, which view, and the path to it; `--list-content` = per-view data inventory. |
| `ask.py` / `scripts/run-ask.sh` | **Cache-first** entry point. Routes by host to a per-host cache, answers offline via `howto.py`; on a miss runs a live discovery, then writes the result back so the next ask is an offline hit. `--verify` re-checks live. |
| `recipe.py` / `scripts/run-recipe.sh` | **Live** how-to finder: priority-BFS over the running UI to a goal's trigger, opens the form, reads the fields, never submits. The live fallback for cache misses. |
| `perform.py` (`pinchtab-webgraph perform`) | **PERFORM** a how-to: resolve it OFFLINE (from a crawled cache/graph), then RUN the compiled block live through the bridge — navigate the path, then download / upload / fill. Safe by default: navigation + downloads run; a form field with no supplied value is skipped (`--set 'Label=value'`, `--file <path>`); the submit runs only with `--allow-submit`; `--dry-run` previews. See [Runnable command blocks](#️-runnable-command-blocks). |
| `flow_cmd.py` (`pinchtab-webgraph flow`) | **Run a declarative automation FLOW** — a JSON document (not a script) executed by a step VM: `goto` / `do` / `click` / `fill` / `download` / `collect` plus the control-flow ops **`for_each`** and **`paginate`**. Downloads are content-hashed against a **persistent dedupe ledger** (a re-run reports `dupe`, not a result). Safe by default: a write runs only if the flow *declares* the capability **and** the caller *grants* it. `flow run \| validate \| schema`. See [Automation flows](#-automation-flows) and [`docs/flows.md`](docs/flows.md). |
| `crawl.py` / `scripts/run-crawl.sh <url>` | Page→page **link graph** → `<out>.json` + a self-contained Cytoscape.js `<out>.html` viewer. |
| `paths.py` | Offline shortest / all click-paths over a crawled link graph (`--from`, `--to`, `--structural`, `--all`). |
| `login.py` (`pinchtab-webgraph login`) | Open a persistent browser session and sign in to a host (credentials from the OS keyring) so subsequent crawls run authenticated. Needs the optional `login` extra (`keyring`). |
| `cache_cmd.py` (`pinchtab-webgraph cache`) | Inspect / manage the per-host interaction-graph caches `ask.py` writes back: `cache list`, `cache path <host>`, `cache show <host>`, `cache clear <host>` / `--all` (destructive, dry-run unless `--yes`). |
| `query_cmd.py` (`pinchtab-webgraph query`) | **Machine-readable** twin of `howto.py` / `paths.py`: runs the offline `api.*` queries (`graph_summary`, `howto`, `find_content`, `list_content`, `list_forms`, `link_paths`) and prints the result as JSON on stdout. Takes `--host` (cache) or `--graph` (path). The substrate the UTCP manual shells out to. |
| `utcp_manual.py` (`pinchtab-webgraph manual`) | Build / print / serve the [UTCP](https://www.utcp.io) tool-calling manual so external tool-callers can invoke the `query` (and live `crawl`/`ask`) surface by running the CLI directly — no wrapper server. `manual --out FILE` / `manual --serve`. |
| `selftest.py` (`pinchtab-webgraph test`) | **Self-improvement loop.** Interactively throw your hardest "how do I do X?" goals at a crawled graph — each is answered **offline** via `api.howto`, you judge whether it's right, and every miss/wrong answer becomes a captured gap. Writes a self-contained HTML report; with `--repo OWNER/NAME` it can (after you confirm) file the report as a GitHub issue. See [Self-test & report](#-self-test--report). |
| `commands.py` | The deterministic **path → executable** compiler shared by every how-to surface. Turns a click-path + terminal action into a runnable `pinchtab` command block (`nav`/`click`/`download`/`upload`/`fill`/`select`/`check`). Pure, stdlib-only, no browser. See [Runnable command blocks](#️-runnable-command-blocks). |

## ▶️ Runnable command blocks

Every how-to answer — from `howto.py`, `recipe.py`, `ask.py`, and the `api`/MCP/UTCP surfaces — now carries a `commands` block: the shortest click-path **and** its terminal action, compiled into a copy-pasteable sequence of [PinchTab](#-requirements) CLI commands that reproduces it. The route tells a human what to click; the command block lets an agent (or you) *run* it.

The terminal action is chosen structurally from the graph — no LLM, no app-specific vocabulary:

| Action kind | How it's recognized | Emitted command |
| --- | --- | --- |
| **download** (direct) | a link/anchor that resolves to a file (`download` attr, file-extension path, or `blob:`/`data:` URL) | `pinchtab download '<url>' -o '<file>'` |
| **download** (JS) | a control whose label is a download/export verb | `pinchtab click --css '<selector>' --wait-nav` — the browser session captures the file |
| **upload** | a file input / styled `<label>` / `ondrop` dropzone | `pinchtab upload '<FILE>' -s '<selector>'` (accepted types annotated) |
| **form** | a create-style trigger that opens a form | one `fill`/`select`/`check` per field (placeholder values); the **submit line is commented out** |

```console
$ pinchtab-webgraph query --host app.example.com howto --goal "download the q3 report"   # (or howto.py / ask.py)

=== HOW TO: DOWNLOAD THE Q3 REPORT ===
Shortest route — 2 clicks:
  1. Go to https://app.example.com/home
  2. Click "Reports"
  3. Download via "Download report"
This downloads a file: https://app.example.com/files/q3-report.pdf

Run it with PinchTab:
  pinchtab nav 'https://app.example.com/home'
  pinchtab nav 'https://app.example.com/reports'   # Reports

  # --- download ---
  pinchtab download 'https://app.example.com/files/q3-report.pdf' -o 'q3-report.pdf'
```

**Safe by construction.** The click-path only ever navigates — it never traverses a write/destructive control (those are `skipped` nodes with no selector recorded). Download and upload affordances are detected but **never clicked during the crawl** (a JS download can pop a native OS save dialog; a file input opens a native picker). And the form terminal fills fields but leaves the submit **commented out** unless you deliberately uncomment it. Nothing a compiled block does mutates data on its own.

The structured surfaces (`api.howto`, MCP, UTCP) return the same thing under `commands`, plus `action_kind` (`form` / `download`) and, for a direct download, `download_url`.

### …and run it — `perform` (the opt-in)

The block above is copy-pasteable, but you don't have to copy-paste it. `pinchtab-webgraph perform` **resolves the how-to offline and then runs the compiled block through the bridge** — so "download the Q3 report" is one command end-to-end:

```console
$ pinchtab-webgraph perform --host app.example.com --goal "download the q3 report"
=== PERFORM: DOWNLOAD THE Q3 REPORT ===  (download, ran)
  ✓ pinchtab nav 'https://app.example.com/home'
  ✓ pinchtab nav 'https://app.example.com/reports'   # Reports
  ✓ pinchtab download 'https://app.example.com/files/q3-report.pdf' -o 'q3-report.pdf'
```

The **same safety rules are enforced at execution**, not just in the printed text:

- **navigation + downloads run** (downloading is the point); `--out-dir <dir>` chooses where the file lands.
- a **form field with no value is skipped**, never filled with placeholder junk — pass real values with `--set "Name=Acme" --set "Plan=Pro"`, and a file with `--file ./doc.pdf`.
- a form's **submit never runs** unless you add `--allow-submit`.
- `--dry-run` prints exactly what *would* run and touches nothing; `--json` emits a structured per-step result (run / skipped / error).

Resolution is offline, so `perform` needs a crawled cache/graph (`--host <h>` or `--graph <file>`) — crawl or `ask` the site first. Only execution needs the bridge. The same capability is exposed as the MCP `perform` tool and the UTCP `perform` manual entry.

**Bridge requirements for the two download kinds:**

- **JS-triggered downloads** (a `click` on an export button) work whenever the bridge config has `security.allowDownload = true`; the file lands in the browser profile's download directory.
- **Direct downloads** (`pinchtab download <url>`) also need `allowDownload = true`, but note PinchTab's `download` performs a **server-side fetch guarded against SSRF** — it refuses `internal or blocked host` URLs (e.g. `localhost`/`127.0.0.1`/link-local). That only affects fetching *internal* hosts; a normal `https://app.example.com/…/file.pdf` is fine. (The crawl bridge ships `allowDownload = false` on purpose — that config is for read-only discovery — so point `perform` at a bridge that enables it.) A rejected download surfaces the bridge's error verbatim.

> **Verified end-to-end** against a real site crawled through the live browser: `crawl → howto → perform` navigates the path and the browser actually writes the file to disk (JS-export path), and the form path fills + submits with real values. See the walkthrough in [`docs/perform-live-test.md`](docs/perform-live-test.md).

## 🔁 Automation flows

`perform` runs a **straight line**: nav, click, fill, submit — one how-to, once. Every real automation needs more: *download all 20 PDFs on this page, then do it again for each of the 12 pages, and only keep the files I don't already have.* That is a **flow**.

A flow is a **JSON document, not a script**, executed by a step VM. That is load-bearing: no arbitrary code runs, so a scheduler or an HTTP handler can safely execute one; a step names its target **semantically** (`goal` / `match`) as well as structurally, so it re-resolves against a re-crawled graph instead of snapping on a stale selector; and `inputs` derives a **JSON Schema**, so a saved flow can become a typed endpoint / MCP tool with no hand-written wrapper.

```json
{
  "name": "download-all-invoices",
  "host": "app.example.com",
  "inputs": { "since": { "type": "string", "required": false } },
  "capabilities": { "allow_download": true },
  "steps": [
    { "op": "goto", "goal": "invoices" },
    { "op": "paginate", "max_pages": 50, "body": [
      { "op": "for_each", "match": { "kind": "download" }, "as": "item", "body": [
        { "op": "download", "href": "${item.href}", "name": "${item.text}.pdf" }
      ]}
    ]}
  ]
}
```

```bash
pwg flow validate ./invoices.json                                  # structure + every ${var} + capabilities
pwg flow schema   ./invoices.json                                  # the `inputs` block as a JSON Schema
pwg flow run      ./invoices.json --host app.example.com --dry-run # print what WOULD run; touch nothing
pwg flow run      ./invoices.json --host app.example.com --input since=2026-01-01
```
```console
=== FLOW: DOWNLOAD-ALL-INVOICES ===  (live)
  ▸ run       flow=download-all-invoices
  ✓ goto      goal=invoices target=Invoices
  · paginate  page=1
  ✓ for_each  match={"kind": "download", "limit": 200} found=2
  ✓ download  name=Download report A.pdf via=fetch
  …
--- ok: 14 steps, 5 new file(s), 0 duplicate(s), 6.1s
```

Nothing in the document is app-specific. `goal: "invoices"` resolves **offline** against the crawled graph (the same resolver `howto`/`perform` use); `match: {"kind": "download"}` is the crawler's structural download classifier applied to the live DOM; `paginate` finds the next-page control structurally (`rel=next` / `aria-label` / `aria-disabled` first, a UI-verb regex second) and stops when it's exhausted — or when the page stops changing.

**Safe by default, twice over.** The effective capability is the **AND** of what the flow *declares* and what the caller *grants* — either side vetoes. Downloading is read-only and on by default (`--no-allow-download` withdraws it); a form **submit** and a file **upload** write to the site, so both need `capabilities.allow_submit`/`allow_upload` in the document **and** `--allow-submit`/`--allow-upload` on the command. A document that performs a write it didn't declare is rejected at *validation* time — so a scheduled run can never half-execute.

**Re-running a flow is a change detector.** "Download the report every 10s" is really "*tell me when a NEW report appears*", so every downloaded file is sha256'd into a content-addressed store with a **dedupe ledger that persists across runs**: a file whose bytes were seen before comes back as a `dupe`, not a result. Downloads take the **in-session fetch** path first (a `fetch()` inside the page — it inherits the session's cookies, so authenticated apps just work), falling back to the `pinchtab download` CLI for cross-origin hrefs.

> **Verified end-to-end** with nothing mocked (real bridge, real headless Chrome): crawl a 3-page fixture site → `paginate` all 3 pages → download **5 real files** (`via="fetch"`, 5 distinct sha256s) → re-run on the same ledger → **0 new, 5 dupes**.

**…and from the browser.** The optional [web UI](#-three-ways-to-call-it) carries a **Flows tab**: the host's saved automations, a JSON editor that validates as you type (a typo'd `${var}` is caught, with its path in the document, before a browser is ever leased), a run panel where the **safety model is visible** — the Allow-submit / Allow-upload toggles are *disabled unless the flow itself declares that capability*, and dry-run is checked by default — a streaming run log with a live **`N new · M dupe`** counter, run history, and the flow's all-time artifact ledger. Running a flow is **opt-in**, because unlike a crawl a flow *can* write to the site:

```bash
PINCHTAB_WEBGRAPH_ENABLE_FLOWS=1 pinchtab-webgraph-ui        # then open the Flows tab
```

Full format (every op and its args), the capability model, the download constraints, the ledger, an authoring walkthrough, and the [web-UI surface](docs/flows.md#running-flows-from-the-web-ui) (storage layout, caps, REST + WS frames, the subprocess/cancel design): **[`docs/flows.md`](docs/flows.md)**.

## 🧪 Self-test & report

Right after you crawl a site, sanity-check that the graph actually answers the questions you care about — and turn every gap into feedback:

```bash
# Interactive: describe your hardest goals, judge each answer, keep going until done.
pwg test --start https://app.example.com/dashboard

#   Scenario #1 — describe a hard goal (blank to finish): create a team
#     ✓ graph found a path (3 clicks): … / form: 4 fields at the trigger
#   Is that correct / what you expected? [Y/n] y
#   Test another scenario? [Y/n] y
#   …blank line finishes → writes test-report-app.example.com-<ts>.html

# Non-interactive (CI / scripting): seed goals, get the HTML report unattended.
pwg test --graph app.json --goal "create role" --goal "add invoice" --out report.html

# Opt-in issue: only with an explicit --repo, and only after you confirm the (public!) preview.
pwg test --start https://app.example.com/dashboard --repo egouilliard/pinchtab-webgraph
```

Each scenario is answered **offline** against the graph (no browser, deterministic) — a "miss" *is* the finding: it means the crawl didn't capture that path. The report groups scenarios by your verdict (pass / fail / unrated) with the returned click-path, form field count, and your "what's wrong" notes. **The report can contain target-app labels, URLs and form details** — issue creation is therefore off by default, requires you to name `--repo`, and shows the full body plus a PUBLIC-repo warning before posting.

## 🎯 Regression audit (10 public sites)

A repeatable extraction-quality gate over ten structurally different public sites (Hacker News, books/quotes.toscrape, python.org, getbootstrap, MDN, gov.uk, Wikipedia, stripe, automationexercise). It crawls each, then scores it **browser-free**, so a change to the crawler or the query layer is measured the same way every time.

```bash
# Copy the audit config and set a token, then run on the host (bridge reachable):
cp crawl-config.audit.example.json crawl-config.audit.json   # set server.token
scripts/site-audit.sh                 # crawl all 10 + score  (--gate dup, default)

# Score already-crawled graphs without a browser:
python3 tests/audit/check.py --graphs .audit-graphs
```

The runner wipes the Chrome profile **and** the pinchtab `stateDir` per site (pinchtab restores open tabs from `stateDir` across restarts — otherwise a prior crawl's tabs leak into the next graph) and crawls with `--max-restarts 0`. `tests/audit/check.py` reports two things per site from `tests/audit/sites.json`:

- **`dup_ratio`** — states that share a normalized URL. **0 means no over-noding** (the deterministic-identity guarantee above); it is the hard gate (`--gate dup`).
- the **50 hard-question goals** (5 per site) run through the offline `howto` API — an informational scoreboard that rises as the crawler improves.

The scorer is unit-tested (`tests/audit/test_check.py`), and the identity guarantee has its own browser-free guards in `tests/test_state_identity.py`.

## 🔌 Three ways to call it

The same crawl-once-query-offline capability is reachable through three interfaces, all layered over one importable core (`pinchtab_webgraph.api` — typed, print-free functions that return structured dicts). Pick whichever fits your consumer; they all resolve to the exact same graph queries, so their answers never disagree.

| Interface | For | How | Extra dep |
| --- | --- | --- | --- |
| **CLI** | humans, shell/CI scripts | `pwg query howto --host app.example.com --goal "create a team"` → JSON on stdout (or the human-readable `pwg howto …`). `pwg --help` lists every subcommand. | none (pure stdlib) |
| **MCP server** | LLM agents / MCP hosts (Claude, IDEs) | `pinchtab-webgraph-mcp` over stdio — 6 offline query tools, 2 live tools (`crawl`, `ask_howto`) with streamed progress, and `graph://…` resources. See [MCP server](#-mcp-server). | `pip install 'pinchtab-webgraph[mcp]'` |
| **UTCP manual** | any UTCP-aware tool-caller | a static [UTCP](https://www.utcp.io) manual (`pwg manual`, `--out`, or `--serve`) whose `cli` call templates invoke `pwg` directly — no wrapper server in the call path. See [UTCP interface](#-utcp-interface). | none to use (`[utcp]` only validates it) |

For a point-and-click front end there's also an **optional local web UI** — a browser app with a **Workspace | Graph | Explore | Flows** view switcher: a Workspace of a "how do I…" chat agent + a live headless-browser pane, an interactive **Graph view** that renders the crawled interaction graph (states as blue circles, form-triggers as green diamonds) right in the browser, and an **Explore view** to search / browse everything the crawl captured — plus a read-only REST API over the same queries, behind the `pinchtab-webgraph-ui` script and the `[ui]` extra:

| Interface | For | How | Extra dep |
| --- | --- | --- | --- |
| **Web UI** | humans, at a browser | `pinchtab-webgraph-ui` serves a loopback-only two-pane SPA + `/api/*` REST over the offline graph. See [docs/ui.md](docs/ui.md). | `pip install 'pinchtab-webgraph[ui]'` |

The chat pane can reach Claude two ways: the **Anthropic API** (`ANTHROPIC_API_KEY`, the default when a key is set) or your **locally-logged-in Claude Code** with no API key (add the separate `[ui-claude-code]` extra + a logged-in `claude` CLI). Both are locked to the same six offline graph tools. See [Chat backends](docs/ui.md#chat-backends).

Chats are **persistent and multiple**: the chat pane's chip bar holds several **named chats per host**, each saved to disk (`<home>/sessions/<host>/<id>.json`) and restored on reconnect — new / switch / rename / delete right from the bar. The `api` backend continues a reopened chat; the `claude_code` backend restores it for display only in v1. See [Chat sessions](docs/ui.md#chat-sessions).

A "how do I get to X" chat answer also offers a **"Show me How"** button: a guided tour that highlights each step directly on the live browser pane and, on **Next**, performs the real click to advance — stopping at the target form without ever submitting it. See [Show Me How guided tour](docs/ui.md#show-me-how-guided-tour).

The **Graph view** renders the host's cached interaction graph entirely offline (via `GET /api/hosts/{host}/graph`), with search, an adjacency-highlight on node click, a detail panel, and an "Ask in chat" button that prefills the chat with the click-path question. Its Cytoscape libraries are lazy-loaded on first open so the SPA stays light. See [Graph view](docs/ui.md#graph-view).

The UI can also **crawl a new URL and store it**: a sidebar **"New crawl"** form spawns the interaction crawler over a WebSocket, streams live progress, and atomically promotes the resulting graph into the cache so the new host appears in the sidebar and is instantly usable by the Graph view + chat. It is **opt-in** (off unless `PINCHTAB_WEBGRAPH_ENABLE_CRAWL` is set) because a crawl drives a real browser through the whole target app and opens every Create form (it never submits). See [New crawl](docs/ui.md#new-crawl-get-wscrawl-opt-in).

The **Explore view** is a read-only browser over everything the crawl captured, in three sub-tabs: **Search** (full-text search of captured page data, each hit showing reachable/click-count badges, the click-path, and the matched items), **Forms** (the create-form inventory + a free-text goal path-finder — each form has a **"Show me how"** button that reuses the live guided tour), and **Content** (the per-view inventory of captured collections). A **Ctrl/Cmd-K command palette** launches over the whole UI — switch host, jump view, new chat / new crawl, manage credentials, and a free-text "search content for …" hand-off into Explore. See [Explore view](docs/ui.md#explore-view).

The **Flows view** is the UI for the [automation flow layer](#-automation-flows): the host's saved flows, a JSON editor that validates as you type, a run panel where the **safety model is visible** (Allow-submit / Allow-upload are *disabled unless the flow declares that capability*; dry-run is on by default), a streaming run log with a live **`N new · M dupe`** dedupe counter, run history, and the flow's all-time artifact ledger. Each run is a **subprocess**, which is what makes **Cancel** work at all. **Opt-in** (`PINCHTAB_WEBGRAPH_ENABLE_FLOWS=1`) because — unlike a crawl, which never submits — a flow *can* write to the site. See [Flows view](docs/ui.md#flows-view-opt-in).

Only the **base install** (`pip install pinchtab-webgraph`, pure stdlib) is needed for the CLI and the UTCP manual; the MCP server and the web UI each live behind an optional extra (`[mcp]` / `[ui]`) so the base package stays dependency-free.

> On externally-managed Python (Debian/Ubuntu, PEP 668) install the extras into a venv, or use `pip install --user --break-system-packages 'pinchtab-webgraph[mcp]'`.

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

## 📚 Documentation

Deep-dive guides live in **[`docs/`](docs/README.md)** — start at the **[documentation index](docs/README.md)**, which links everything below:

| Guide | What it covers |
| --- | --- |
| **[Automation flows](docs/flows.md)** | The flow document format (every op + its args), the [capability / safety model](docs/flows.md#the-capability--safety-model), the [download strategy](docs/flows.md#downloads-in-session-fetch-first-cli-fallback) (in-session fetch first, CLI fallback) and its constraints, the [dedupe ledger](docs/flows.md#the-dedupe-ledger), an [authoring walkthrough](docs/flows.md#authoring-a-flow--a-walkthrough), [running flows from the web UI](docs/flows.md#running-flows-from-the-web-ui) (storage, caps, REST + WS frames, the subprocess/cancel design, the artifact-scope caveat), and the [gotchas](docs/flows.md#gotchas). |
| **[`perform` live test](docs/perform-live-test.md)** | The real-browser proof of `crawl → howto → perform`: a local test site, a downloads-enabled bridge, and the two bugs the live run caught. |
| **[MCP server](docs/mcp-server.md)** | Run `pinchtab-webgraph-mcp`: the `[mcp]` extra, `.mcp.json` registration, the tool + resource inventory, env vars, and the live-tool safety model. |
| **[UTCP interface](docs/utcp.md)** | The `pwg query` JSON surface + the `pwg manual` / `--serve` UTCP manual, the 8 tools, the scope subset, and the exit-code convention. |
| **[Web UI](docs/ui.md)** | The optional local web UI (`pinchtab-webgraph-ui`, `[ui]` extra): the Workspace/[Graph](docs/ui.md#graph-view)/[Explore](docs/ui.md#explore-view)/[Flows](docs/ui.md#flows-view-opt-in) view switcher + [command palette](docs/ui.md#command-palette), the REST API + vault endpoints, the chat + screencast WebSockets, [persistent named chats](docs/ui.md#chat-sessions), the opt-in [New crawl](docs/ui.md#new-crawl-get-wscrawl-opt-in) + [flow-run](docs/ui.md#flows-view-opt-in) endpoints, env vars, the loopback-only security model, and the [operational notes](docs/ui.md#operational-notes-developing--e2e-testing-the-ui) for driving the UI itself. |
| **[Authenticated login](docs/authenticated-login.md)** | Crawl behind a login safely: hand-login vs. keyring automation, the threat model, sandbox/bot-account isolation, and how to test it. |
| **[Contributing](CONTRIBUTING.md)** | Branch model, Conventional Commits, the stay-generic rule, safety, security, and PRs. |

## 🔎 How interaction crawling works

For each state the crawler reads every link and clickable widget (stable structural CSS selectors, not framework-generated refs), plus the state's data collections. The clickable set spans `button` / `[role="button"]` / tabs / menu items / `summary` / `[onclick]` **and** upload affordances — `input[type="file"]` (including a file input hidden behind a styled `<label>`/button) and `[ondrop]` dropzones — each carrying its `accept` attribute (accepted file types). Then, for each non-skipped widget, it **re-materializes** the state (replay the click-path from a known start), clicks the widget, and classifies the result:

- **navigated** (URL changed) → a page edge; enqueue the new page.
- **DOM changed, same URL** → a new SPA/state node + edge, recursed into up to the interaction depth.
- **create-trigger** → the form/modal is opened, its fields are read (label / type / required / options / accepted file types / confirm button), then Escape — nothing is persisted.
- **upload affordance** → recorded as a read-only `upload` node (with its `accept` file types) and a skipped action edge, but **never clicked** — clicking a file input opens a native OS file dialog the crawler can't dismiss, so uploads are documented, not activated.
- **no change** → ignored.

> A dropzone whose drop handler is attached via `addEventListener` (not an inline `ondrop` attribute) and that wraps no file input can't be seen from the DOM; the nested-file-input heuristic covers the common case.

Two kinds of state become **trigger targets** for the offline `howto` query: a control whose label carries a create-verb (`create` / `add` / `new` / …), **and** a state that *structurally is a form* — it renders real `input`/`select`/`textarea` fields plus a submit control — even when nothing on it carries a create-verb. That second, structural signal (`--capture-form-states`, on by default) is how sign-in / sign-up / contact pages become answerable (e.g. "how do I sign in" → `/login` with its email + password form), and it's fully generic — form shape, no app vocabulary. On the query side, a matched trigger whose form has **no fields** is treated as low-confidence and `howto` prefers `no_match` over surfacing it, so a nav link that merely shares a verb (say "Find a **new** job" for "post a job") is never returned as a confident match.

Re-materializing per probe keeps every click starting from a known state and avoids stale element references across reloads. **State identity is URL-primary in normal navigation** — one normalized URL (with `#fragment` and generic tracking params like `utm_*`/`gclid` stripped, remaining query kept) is exactly one state, so control-count / feed-content jitter between reads can never mint duplicate states for the same page. Single-URL app-shells (e.g. MS Teams), whose URL never changes as views swap in place, instead key on a structural signature that folds in ARIA view markers (`--single-url`) so same-shell views stay distinct. (Trade-off: hash-router SPAs that route only via `#/…` collapse to one state in nav mode.)

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

- **Nodes** — **pages** (by normalized URL) and **SPA/modal states** (same URL, changed DOM). Cross-host mode adds `external` / `iframe` nodes. File-upload affordances become a distinct `upload` node carrying an `accept` field (the accepted file types); download/export affordances become a distinct `download` node carrying `dlKind` (`direct`/`js`) and, for a direct download, `dlHref` (the file URL). Each node can carry its **control inventory** and its **content collections**.
- **Edges** — **links** (navigation) and **actions/clicks**. Destructive-looking actions that were deliberately skipped — and upload/download affordances, which are recorded but never clicked — are stored as dashed (skipped) edges so you can see what was avoided.
- **meta** — crawl parameters plus `meta.stopped`: `frontier-exhausted` (complete) vs. `hit-max-*` / `wedge` (truncated). Truncation is always explicit. Additive `meta.uploads` / `meta.downloads` count the upload / download affordances found.
- **Viewer** — the Cytoscape HTML viewer renders `upload` nodes distinctly (cyan, "tag" shape) and `download` nodes distinctly (violet, "vee" shape, a "download / export" legend entry) alongside the "SPA / modal state" and "skipped" legends; the `Uploads` / `Downloads` stats are guarded, so older graphs without them still render. The viewer is **truly self-contained and offline** — its six Cytoscape/layout libraries are vendored inline (no CDN, no network), so it opens and lays out with nothing but a browser. It uses a fast `fcose` layout by default (a big graph of ~2,500 edges lays out in well under a second) with a **High quality** button for an on-demand higher-fidelity relayout, and shows a "Laying out graph…" indicator while a layout runs.

## 🛡️ Safety model

- **Same-origin by default** — the crawler won't wander off the target site unless you pass `--cross-host`.
- **Never mutates data** — discovery opens and reads forms, then Escapes. Create / save / delete / submit controls are skipped by default and recorded, not clicked. **File-upload and download/export affordances are recorded but never clicked** (a file input opens a native OS picker; a JS download can pop a native save dialog the crawler can't dismiss), so the read-only contract holds. The runnable [command blocks](#️-runnable-command-blocks) inherit this: their path only navigates, downloads/uploads are separate explicit commands, and a form's submit line ships commented out. **Never run a "click everything" crawl in an authenticated session you care about** — that's exactly why the isolated bridge exists.
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
