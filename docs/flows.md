# 🔁 Automation flows

`crawl → howto → perform` answers *"how do I do X?"* and then does it **once, in a straight
line**: navigate, click, fill, submit. Every real automation needs more than that — *download
all 20 PDFs on this page, then do it again for each of the 12 pages, and only keep the files I
don't already have.* That is what the **flow layer** adds.

A flow is a **JSON document**, not a script. It is executed by a **step VM**
([`runner.py`](../pinchtab_webgraph/runner.py)) against a live browser. That choice is
load-bearing:

- **Safe to schedule.** No arbitrary code runs, so a cron tick, a worker, or an HTTP handler can
  execute a flow without a sandbox. Every side effect a flow can have is declared up front in
  `capabilities` and enforced by the runner.
- **Self-healing.** A step names its target **semantically** (`goal` / `match`) as well as
  structurally (`selector`), so it re-resolves against a re-crawled graph instead of snapping on
  a stale selector after a redesign.
- **Introspectable.** `inputs` derives a JSON Schema, so a saved flow can become a typed HTTP
  endpoint or an MCP tool with **no hand-written wrapper**.

| | |
| --- | --- |
| Document model | [`pinchtab_webgraph/flow.py`](../pinchtab_webgraph/flow.py) — ops, `validate()`, `substitute()`, `bind_inputs()`, `json_schema()`, `capabilities()`. Pure: no I/O, no browser. |
| Step VM | [`pinchtab_webgraph/runner.py`](../pinchtab_webgraph/runner.py) — the interpreter. Browser, artifact store and sleep are **injectable ports**. |
| Browser port | [`pinchtab_webgraph/browser.py`](../pinchtab_webgraph/browser.py) — `PinchTabBrowser` + the live-DOM primitives (`query`, `next_page`, `page_signature`, `fetch_bytes`/`save_bytes`). |
| Artifact store | [`pinchtab_webgraph/artifacts.py`](../pinchtab_webgraph/artifacts.py) — content-addressed store + the persistent dedupe ledger. |
| CLI | [`pinchtab_webgraph/flow_cmd.py`](../pinchtab_webgraph/flow_cmd.py) — `pwg flow run \| validate \| schema`. |

## Contents

- [A flow in 20 lines](#a-flow-in-20-lines)
- [The CLI](#the-cli)
- [The document format](#the-document-format)
  - [Top level](#top-level)
  - [The ops](#the-ops)
  - [Variables and `${…}` substitution](#variables-and--substitution)
  - [Inputs → JSON Schema](#inputs--json-schema)
- [The capability / safety model](#the-capability--safety-model)
- [Downloads: in-session fetch first, CLI fallback](#downloads-in-session-fetch-first-cli-fallback)
- [The dedupe ledger](#the-dedupe-ledger)
- [Authoring a flow — a walkthrough](#authoring-a-flow--a-walkthrough)
- [Run records and events](#run-records-and-events)
- [Gotchas](#gotchas)

## A flow in 20 lines

`invoices.json` — go to the invoices view, walk every page, download every download-classified
control, and keep only files whose bytes we have never seen:

```json
{
  "name": "download-all-invoices",
  "host": "app.example.com",
  "inputs": {
    "since": {"type": "string", "required": false, "description": "only invoices on/after this date"}
  },
  "capabilities": {"allow_download": true},
  "steps": [
    {"op": "goto", "goal": "invoices"},
    {"op": "paginate", "max_pages": 50, "body": [
      {"op": "for_each", "match": {"kind": "download"}, "as": "item", "body": [
        {"op": "download", "href": "${item.href}", "name": "${item.text}.pdf"}
      ]}
    ]},
    {"op": "log", "message": "done for ${since}"}
  ]
}
```

Nothing in it is app-specific. `goal: "invoices"` is resolved **offline** against the crawled
graph (the same `api.resolve_action` that powers `howto`/`perform`); `match: {"kind":
"download"}` is the **structural** download classifier applied to the live DOM; `paginate` finds
the next-page control structurally (`rel=next` / `aria-label` / `aria-disabled` first, a UI-verb
regex second).

## The CLI

```bash
pwg flow validate ./invoices.json      # structure + every ${var} + capability declarations
pwg flow schema   ./invoices.json      # the `inputs` block as a JSON Schema
pwg flow run      ./invoices.json --host app.example.com --dry-run
pwg flow run      ./invoices.json --host app.example.com --input since=2026-01-01
```

`validate` is pure — no browser, no graph, no network — so an API handler can reject a bad
document before a browser session is ever leased:

```console
$ pwg flow validate ./invoices.json
{
  "status": "ok",
  "name": "download-all-invoices",
  "host": "app.example.com",
  "steps": 3,
  "capabilities": {
    "allow_submit": false,
    "allow_download": true,
    "allow_upload": false
  },
  "inputs": [
    "since"
  ]
}

$ pwg flow schema ./invoices.json
{
  "type": "object",
  "properties": {
    "since": {
      "type": "string",
      "description": "only invoices on/after this date"
    }
  },
  "additionalProperties": false
}
```

`flow run` options:

| Flag | Meaning |
| --- | --- |
| `--host HOST` / `--graph FILE` | Where a `goal`/`match` step resolves. **Optional** (unlike `perform`): a flow built only from explicit `goto{url}` + `selector` steps needs no graph at all. The runner aborts with a plain message the moment a step *does* need one. |
| `--input NAME=VALUE` | Supply a declared input (repeatable). An undeclared name is **rejected**, not ignored — a typo'd param must never silently run the flow with a default. |
| `--allow-submit` / `--allow-upload` | The caller's grant for the two write capabilities. See [safety model](#the-capability--safety-model). |
| `--no-allow-download` | Withdraw the one capability that is on by default. |
| `--scope NAME` | The artifact scope / dedupe ledger. Defaults to the flow's (sanitized) name, so two flows never poison each other's ledger. |
| `--artifacts-root DIR` | Store artifacts under `DIR/<scope>` instead of `$PINCHTAB_WEBGRAPH_HOME/artifacts/<scope>`. |
| `--dry-run` | Print exactly what *would* run and touch **nothing** — no browser command, not even an artifact directory. |
| `--server` / `--config` | The PinchTab bridge (default `http://localhost:9871`) and the config file the bridge token is read from (default `crawl-config.json`, gitignored). |
| `--json` | Emit the full structured run record instead of progress lines. |

Exit codes: **0** the run finished ok · **1** a step errored / the document was rejected · **2** a
usage or environment error (bad `--host`, no cache).

## The document format

### Top level

| Key | Required | Meaning |
| --- | --- | --- |
| `name` | ✔ | Non-empty string. Also the default artifact scope. |
| `steps` | ✔ | Non-empty list of step objects. |
| `host` | | The host the flow belongs to. Two jobs: it is the default `--host` story for humans, and it is a **guard** — a `goto{url}` step that leaves this host is refused (`fnmatch`, so `*.example.com` works). Without it a saved flow taking a `${input}` in a url is an open redirect. |
| `inputs` | | `{name: {type, required, default, description, enum}}`. `type` ∈ `string` / `number` / `integer` / `boolean`. |
| `capabilities` | | `{allow_submit, allow_download, allow_upload}`. Defaults: `false` / **`true`** / `false`. |

Guards: **500** steps in the document, **6** levels of nesting, **10 000** executed steps at run
time (a `for_each` inside a `paginate` multiplies).

### The ops

Leaf ops — `one of` means at least one of those keys must be present.

| Op | Keys | What it does |
| --- | --- | --- |
| `goto` | one of `url` / `goal` / `match`; opt `start`, `match` | Position the browser. With `url`: navigate (host-guarded). With `goal`/`match`: resolve against the crawled graph and **walk the click-path** — it deliberately does **not** click the trigger. `goto` positions; `do` acts. |
| `do` | one of `goal` / `match`; opt `set`, `file`, `submit`, `start`, `index` | Run a whole how-to as one step: resolve it offline, then execute the *same* compiled command block `perform` runs (walk the path, open the form, fill the fields from `set`, upload `file`). `submit: true` needs the capability. `index` picks among ambiguous matches. |
| `click` | one of `selector` / `text` | Click a control. `text` is resolved live through the control query (exact, case-insensitive). |
| `fill` | one of `selector` / `label`; req `value` | Type into a field. |
| `select` | one of `selector` / `label`; req `value` | Choose an option. |
| `check` | one of `selector` / `label` | Tick a checkbox. |
| `upload` | `selector`; req `file` | Attach a file. **Write op** — needs `allow_upload`. |
| `download` | one of `href` / `selector`; opt `name`, `dedupe` | Fetch a file. With `href` the bytes are fetched, hashed and deduped (see below). With only a `selector` (a JS-triggered export) the control is clicked and the step is honestly reported as `triggered` — the browser session captures the file, so we cannot hash what we never touched. `dedupe: "none"` disables the ledger check for that step. |
| `collect` | `into`; opt `kind` | Extract the current view's data collections (the crawler's generic content extractor) into `run.collected[into]`, and bind `${into}` for later steps. `kind` filters (e.g. `table`). |
| `wait` | one of `ms` / `selector` / `text`; opt `timeout_ms` | Sleep, or poll until a control appears (default timeout 10 000 ms). |
| `set` | `var`; req `value` | Bind a variable. |
| `log` | `message` | Emit a message onto the event stream. |

Body ops — the control flow, and the reason this layer exists.

| Op | Keys | What it does |
| --- | --- | --- |
| `for_each` | req `match`; opt `as` (default `item`), `max` (default 200); req `body` | Query the live page for every control matching `match` and run `body` once per hit. In the body, `${item}` is `{selector, text, kind, href, dlKind, index}` and `${index}` is the 0-based position. |
| `paginate` | opt `max_pages` (default 25); req `body` | Run `body` on the current page, click "next", repeat. In the body `${page}` is the 1-based page number. Stops when the paginator is **absent**, **exhausted** (`disabled` / `aria-disabled`), `max_pages` is hit, or the **no-progress guard** fires — if the page signature stops changing, a "next" that doesn't advance is a decoy and the loop stops rather than re-downloading page 1 for its whole budget. |

`match` (used by `for_each` and the `goto`/`do` resolvers) is `{kind, label, selector, limit}`:

- `kind` — `download` (the same structural classifier the crawler uses: a `download` attribute, a
  file-extension path, a `blob:`/`data:` URL, or a download/export **verb** label), `link`, or
  `button`. Omit to match anything actionable.
- `label` — a case-insensitive **regex** over the control's visible text (or `aria-label`).
- `selector` — restrict the search to a subtree.
- `limit` — cap the hit count (default 200).

> The control query deliberately **descends into table rows and grids**, unlike the crawler's
> `recipe.CONTROLS_JS` (which skips row controls for crawl speed). A per-row "Download" button is
> exactly what a bulk flow wants.

### Variables and `${…}` substitution

`${a.b}` — a plain dotted lookup into the run's variable map. That is **all** it is: not a
template language, not an expression evaluator. A flow is executed by a scheduler on a machine
with a logged-in browser session, so the document must never be able to *compute*.

- A string that is **exactly one** reference resolves to the referent's native type
  (`"${item.index}"` stays an int); a reference embedded in text interpolates as a string
  (`"${item.text}.pdf"`).
- Every reference is checked at **validation time** against declared inputs, `set`/`collect`
  variables, loop variables in scope, and the built-in `${run}` (`{name, host}`). A typo is a
  rejected document, not a 3am abort.
- `${page}` and `${index}` exist **only inside** a `paginate` / `for_each` body — that is
  deliberate: allowing them everywhere would let a typo validate and then kill the run.

### Inputs → JSON Schema

`pwg flow schema` (and `flow.json_schema()`) turns the `inputs` block into a JSON Schema with
`additionalProperties: false`. `flow.bind_inputs()` is the boundary an HTTP request body would
cross: it coerces by declared type, applies `default`, enforces `required`, and **rejects unknown
keys**. Together these are what make a saved flow a typed endpoint / MCP tool with nothing
hand-written in between.

## The capability / safety model

Two independent gates. **The effective capability is the AND of what the flow declares and what
the caller grants — either side can veto.**

| Capability | Default | Flow declares | Caller grants |
| --- | --- | --- | --- |
| `allow_download` | **`true`** | `capabilities.allow_download` | on by default; `--no-allow-download` withdraws it |
| `allow_submit` | `false` | `capabilities.allow_submit` | `--allow-submit` |
| `allow_upload` | `false` | `capabilities.allow_upload` | `--allow-upload` |

Downloading is read-only and is the point of most flows, so it is the one capability on by
default. Submitting a form and uploading a file **write to the target site**, so both are off
unless *two* parties agree.

Enforcement happens in two places, on purpose:

1. **At validation.** `flow.validate()` refuses a document whose steps perform a write it did not
   declare — *"a step uploads a file, but capabilities.allow_upload is false"*. Rejecting up front
   (rather than at step time, halfway through) means a scheduled run can never **half-execute**.
2. **At run time.** The runner ANDs the declaration with the caller's grant and re-checks per
   step; a denied step is recorded as `skipped` with a reason, never silently dropped. The `do`
   op's compiled block honours the same grants — a `do` cannot smuggle a download or an upload
   past a withheld capability.

The pre-existing safety rules still hold underneath: the crawl never submits, a form field with
no supplied value is **skipped** rather than filled with placeholder junk, and `--dry-run`
touches nothing at all (no browser command, and no artifact directory — the store would `mkdir`).

## Downloads: in-session fetch first, CLI fallback

`_op_download` with an `href` tries **two strategies, in this order**:

1. **In-session fetch** (`browser.save_bytes` → `fetch(url, {credentials: 'include'})` evaluated
   *inside the page*). It inherits the page's **session cookies** (so an authenticated app just
   works), it is not subject to the CLI's SSRF/allowlist refusal (so a **local** app works), and
   it hands us the real bytes — which is what content-hash dedupe needs. The bytes cross the CDP
   boundary base64-encoded, capped at **10 MB**.
2. **`pinchtab download`** (the CLI) — the fallback, because the in-page fetch is **same-origin
   only**. A cross-origin href (a CDN link) throws `TypeError: Failed to fetch` and lands here.

The step reports which one ran as `via="fetch"` or `via="cli"`.

**Constraints that bite (all verified the hard way):**

- **`pinchtab download` can NEVER fetch a loopback/local URL.** Its SSRF guard refuses
  `127.0.0.1` / `localhost` / link-local **even when the host is allowlisted**. Local and e2e
  downloads only work via the in-session fetch path.
- **The in-session fetch is same-origin only** — the runner must already be navigated to the site
  (a `goto` before the `download`).
- **`blockImages` / `blockMedia: true` in the bridge config silently break the fetch by file
  extension.** A `.png` fetch fails while byte-identical content served as `.bin` succeeds. Set
  **both to `false`** in `instanceDefaults` for any bridge that runs download flows.
- A real CLI download also needs `security.allowDownload = true` **and** the host in
  `security.downloadAllowedDomains`. Note `security.allowedDomains` is a **top-level key under
  `security`** — not nested under `idpi`.

## The dedupe ledger

"Download the report every 10 seconds" is never really that. It is **"tell me when a *new* report
appears."** A flow that re-downloads the same PDF 8 640 times a day and calls each one a result is
useless.

So `ArtifactStore` is **content-addressed**: every accepted file is sha256'd, and a hash the store
has seen before is reported as a **`dupe`** — not saved again, not counted as a result. The staged
copy is removed and the record still points at the already-stored bytes, so a caller can reference
the file it re-found without a second copy on disk. Storing by hash also fixes the silent
corruption case where a site serves ten different files all called `export.pdf`.

```
$PINCHTAB_WEBGRAPH_HOME/            (default ~/.pinchtab-webgraph)
  artifacts/
    <scope>/
      ledger.jsonl                  append-only: one JSON line per accepted artifact
      files/<sha256>.<ext>          the bytes, content-addressed (never overwritten)
      staging/                      where a download lands before it is hashed
```

**The ledger persists across runs** — that is the entire polling use case: run *N* must know what
run *N−1* already fetched. The e2e test proves it: run 1 downloads 5 files (5 distinct sha256s,
`artifacts_new: 5`); a second run with a **fresh `ArtifactStore` instance** on the same root+scope
reports **0 new, 5 dupes**. A torn last line (from a killed run) is skipped, not fatal.

Scope defaults to the flow's name and is validated as a directory segment (allowlist, not an
escape) — the same treatment `cache_store` gives a host.

## Authoring a flow — a walkthrough

**1. Crawl the site once.** Flow `goal` steps resolve offline against the interaction graph, so
the graph has to exist:

```bash
pwg crawl https://app.example.com/dashboard --out out/app
#   or, to write it into the per-host cache that --host reads:
pwg ask --url https://app.example.com/dashboard --goal "invoices"
```

**2. Find the goal's real name.** Ask the graph what it can answer, so the flow's `goal` string
is one the resolver actually matches:

```bash
pwg howto out/app.json --goal "invoices"      # the click-path + what it lands on
```

**3. Write the document.** Start from the [example above](#a-flow-in-20-lines). Rules of thumb:

- Prefer `goal` / `match` over a hard-coded `selector` — that is what survives a redesign.
- `goto` to position, `for_each` to fan out over what's on the page, `paginate` to fan out over
  pages, `download` to take the bytes.
- Declare every input you intend to pass; declare every write capability you intend to use.

**4. Validate, then dry-run.** Validation is free and offline. A dry run previews a `for_each`
body **once with a placeholder item** and a `paginate` body **once for page 1**, so you see the
shape without touching the site:

```bash
pwg flow validate ./invoices.json
pwg flow run ./invoices.json --host app.example.com --dry-run
```

**5. Run it for real**, and look at the artifact directory the run prints:

```bash
pwg flow run ./invoices.json --host app.example.com --input since=2026-01-01
```
```
=== FLOW: DOWNLOAD-ALL-INVOICES ===  (live)
  ▸ run       flow=download-all-invoices
  ✓ goto      goal=invoices target=Invoices
  · paginate  page=1
  ✓ for_each  match={"kind": "download", "limit": 200} found=2
  ✓ download  name=Download report A.pdf via=fetch
  ...
--- ok: 14 steps, 5 new file(s), 0 duplicate(s), 6.1s
    artifacts: ~/.pinchtab-webgraph/artifacts/download-all-invoices
```

**6. Schedule it.** Re-running the same flow is the change detector: everything already seen comes
back as a `dupe`, and `stats.artifacts_new` is the answer to *"is there anything new?"*

## Run records and events

Every step emits a structured event (`{op, status, …}`), so a CLI progress line, an SSE stream to
a browser, and a persisted run record are all the same data. `flow run --json` prints the whole
record:

| Field | Meaning |
| --- | --- |
| `status` | `ok` · `error` (a step failed but the run continued) · `aborted` (a fatal step — a nav/click that didn't land means every later step is aimed at the wrong page) |
| `steps` | The event log, in order. Statuses: `ok` · `new` · `dupe` · `triggered` · `skipped` · `dry-run` · `page` · `error`. |
| `artifacts` | One record per accepted download: `{status, sha256, name, path, size, source}`. |
| `collected` | The `collect` buckets. |
| `stats` | `{steps_executed, artifacts_new, artifacts_dupe}`. |

A `skipped` step always carries a `reason` (usually a withheld capability or a form field with no
supplied value). Nothing is ever silently dropped.

## Gotchas

Every one of these cost real debugging time. These are the ones that bite in the flow layer:

1. **`pinchtab download` can never fetch a loopback/local URL** — the SSRF guard refuses
   `127.0.0.1` even when allowlisted. Local/e2e downloads go through the in-session fetch.
2. **The in-session fetch is same-origin only** — `goto` the site before you `download`.
3. **`blockImages` / `blockMedia: true` silently break the fetch by file extension.** Set both
   `false` in the bridge config for any download flow.
4. **`pinchtab eval` does not await promises without `--await-promise`** — without it an async
   expression returns `{}` with **rc=0**: a silent wrong answer. `browser.evaluate()` handles
   this; don't hand-roll a bridge call that doesn't.
5. **`pinchtab eval` prints strings unquoted** — always wrap the expression in
   `JSON.stringify(...)` and do a **single** `json.loads`. (A double-decode was a P0 bug.)
6. **`security.allowedDomains` is a top-level key under `security`**, not nested under `idpi`; a
   real CLI download also needs `security.downloadAllowedDomains`.
7. **A navigating click 409s under PinchTab's action guard *after* the click succeeded.** Every
   emitted click carries `--wait-nav` — without it, link-based paginators and path clicks abort a
   flow that in fact worked.

## Verified end-to-end

`tests/e2e/test_flow_paginate_download_e2e.py` runs the whole layer with **nothing mocked** — a
real PinchTab bridge, real headless Chrome, a real crawl of a 3-page fixture site: `goto{goal}`
resolves the reports page off the crawled graph, `paginate` walks all 3 pages, a nested `for_each`
downloads **5 real files** (`via="fetch"`) with **5 distinct sha256s**, and a second run with a
fresh `ArtifactStore` on the same ledger reports **0 new, 5 dupes**. It skips cleanly when no
bridge is reachable.

The document model, the VM, the browser primitives, the store and the CLI also have **171
browser-free unit tests** (`tests/test_flow.py`, `test_runner.py`, `test_browser.py`,
`test_artifacts.py`, `test_flow_cmd.py`) — the VM is exercised end-to-end against a `FakeBrowser`,
which is exactly what the port split buys.

---

← Back to the **[documentation index](README.md)** · the **[main README](../README.md)** ·
related: **[`perform` live test](perform-live-test.md)**, **[MCP server](mcp-server.md)**,
**[authenticated login](authenticated-login.md)**
