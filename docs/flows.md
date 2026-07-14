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
| CLI | [`pinchtab_webgraph/flow_cmd.py`](../pinchtab_webgraph/flow_cmd.py) — `pwg flow run \| validate \| schema` (`--jsonl` streams the run as JSON Lines). |
| Web UI | The **[Flows tab](#running-flows-from-the-web-ui)** — [`ui/flow_store.py`](../pinchtab_webgraph/ui/flow_store.py) (saved flows + run history on disk) and [`ui/flow_runner.py`](../pinchtab_webgraph/ui/flow_runner.py) (runs `flow_cmd --jsonl` as a subprocess and relays its frames). Opt-in: `PINCHTAB_WEBGRAPH_ENABLE_FLOWS=1`. |

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
- [Running flows from the web UI](#running-flows-from-the-web-ui)
  - [The tab](#the-tab)
  - [The safety model, made visible](#the-safety-model-made-visible)
  - [Storage layout and caps](#storage-layout-and-caps)
  - [Why a run is a subprocess](#why-a-run-is-a-subprocess)
  - [REST surface](#rest-surface)
  - [`GET /ws/flows/run` — the frame protocol](#get-wsflowsrun--the-frame-protocol)
  - [Caveat: the artifact scope diverges between CLI and UI](#caveat-the-artifact-scope-diverges-between-cli-and-ui)
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

**3. Write the document.** Start from the [example above](#a-flow-in-20-lines), or from the
committed, runnable one at
[`examples/flows/download-all-reports.json`](../examples/flows/download-all-reports.json) (the
same document the UI's **`+ New flow`** button seeds the editor with). Rules of thumb:

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

## Running flows from the web UI

The flow layer shipped CLI-only, which meant an automation platform you could not *see* your
automations in. The optional **[web UI](ui.md)** now carries a fourth tab — **Flows** — where a
host's saved automations are listed, written, validated, run, re-run, and audited.

```bash
PINCHTAB_WEBGRAPH_ENABLE_FLOWS=1 pinchtab-webgraph-ui        # 1 / true / yes / on
```

**Off by default**, exactly like [`PINCHTAB_WEBGRAPH_ENABLE_CRAWL`](ui.md#new-crawl-get-wscrawl-opt-in)
— and more warranted: a crawl *structurally never submits*, where a flow's `do{submit: true}` or
`upload` step **can write to the real site**. With the gate unset, `/ws/flows/run` refuses with a
`flow_unavailable` / `disabled` frame; the CRUD routes and the editor keep working, so you can
still author and validate a flow on a server that is not allowed to run one.

### The tab

```
┌───────────────┬──────────────────────────────────────────────────────────┐
│ Crawled       │ app.example.com   [Workspace][Graph][Explore][Flows]      │
│ graphs        ├──────────────────┬───────────────────────────────────────┤
│ ───────────   │  Flows           │  { "name": "download-all-reports",     │
│ app.example   │  ─────────────   │    "steps": [ … ] }        ← editor    │
│ …             │  ▸ download-all  │  ✓ ok · 4 steps · download             │
│               │    reports  ·3   │  ┌──────────────────────────────────┐  │
│               │  ▸ export-users  │  │ Run: [x] Dry run  [ ] Allow submit│  │
│               │                  │  │      [Run flow] [Cancel]  5 new · │  │
│               │  [+ New flow]    │  │                          0 dupe   │  │
│               │                  │  │  ✓ download  report-a.pdf  new    │  │
│               │                  │  └──────────────────────────────────┘  │
│               │                  │  Runs (history)   │  Artifacts (all-time)│
└───────────────┴──────────────────┴───────────────────────────────────────┘
```

- **Flow list** (left) — every flow saved under the selected host, with its step count and how
  many times it has run. **`+ New flow`** seeds the editor with a runnable starter document (a
  paginate + for_each + download flow, so the dedupe story is there from the first keystroke).
- **Editor** — a plain JSON textarea with a **live validator**: every keystroke (debounced) is
  `POST`ed to `/api/flows/validate`, which is [`flow.validate()`](#the-document-format) — pure, no
  browser, no graph. A typo'd `${itm.href}` turns the bar red and names both the variable *and its
  path in the document* (`steps[1].body[0].body[0].href`) before a browser is ever leased. **Save
  is disabled while the document is invalid.**
- **Resolvability warnings** (amber) — the one thing `flow.validate()` structurally *cannot* know:
  whether a step's `goal` names anything on the actual site. `{"op":"goto","goal":"reports"}` on a
  site whose only trigger is “Add Report” is a *perfectly valid document* that aborts the moment it
  runs. So the route ALSO re-resolves every `goto`/`do` goal through `api.resolve_action` against the
  host's cached graph (`flow_resolve.py`) and returns a `warnings` list: the step's `path` (same
  grammar, so the canvas paints the box amber), the message, and the **candidate labels the site
  really has** — *did you mean “Add Report”?*. These are **warnings, not errors**: the verdict stays
  `ok` and **Save stays enabled**, because a flow may legitimately be authored before the crawl. A
  host with no cache (or no `host` at all) simply yields no warnings.
- **Run panel** — built from the flow's own declared `inputs` (one field per input, typed and
  marked required) and its declared `capabilities`. See below.
- **Run log** — the streaming step feed, plus a live **`N new · M dupe`** counter fed by the
  `download` step frames. That counter *is* the product: re-running a flow and watching it report
  **0 new · 5 dupe** is what makes this a change detector rather than a dumb poller.
- **Runs (history)** — the flow's past runs, newest first. Clicking one replays its persisted step
  log into the same panel, so a finished run reads exactly like a live one.
- **Artifacts (all-time)** — the flow's cumulative [dedupe ledger](#the-dedupe-ledger)
  (`artifacts.list_artifacts()`): every distinct file it has *ever* fetched, with name, size, when,
  and sha256. A run record only knows what *it* fetched; the ledger is what "what does this
  automation have?" is actually asking.

### The safety model, made visible

The [capability model](#the-capability--safety-model) is not just enforced in the UI, it is
**rendered**:

| Control | Behaviour |
| --- | --- |
| **Dry run** | **Checked by default.** A dry run touches nothing — no browser command, no artifact directory — so it also neither leases the bridge nor vetoes a crawl. |
| **Allow download** | Checked by default (the one read-only capability), and **disabled unless the flow declares `allow_download`**. |
| **Allow submit** / **Allow upload** | **Unchecked**, and **disabled unless the flow's own `capabilities` block declares them.** |

So *"a write happens only if the flow **declares** it **and** the caller **grants** it"* is
something you can **see** rather than something you have to read. The server re-derives the
effective grant the same way (`declared AND granted`) before it builds the subprocess argv, and the
runner ANDs it again per step — a checkbox in a browser is a convenience, never the enforcement.

### Storage layout and caps

Saved flows and their run history live under the [cache/config home](ui.md#environment-variables)
(`$PINCHTAB_WEBGRAPH_HOME`, default `~/.pinchtab-webgraph`):

```
<home>/flows/<host>/<flow_id>.json                  the flow record  {id, host, created_at, updated_at, doc}
<home>/flows/<host>/<flow_id>/runs/<run_id>.json    one execution    {status, dry_run, cancelled, capabilities,
                                                                      inputs, stats, steps, artifacts, collected, …}
```

[`ui/flow_store.py`](../pinchtab_webgraph/ui/flow_store.py) mirrors
[`chat_store.py`](ui.md#on-disk-layout) exactly: stdlib-only, a per-host directory, **atomic
writes** (tmp + `os.replace`), and one validation choke-point. `<flow_id>` / `<run_id>` are uuid4
hex (`^[0-9a-f]{32}$`), so a raw id can never resolve outside its host's directory.

> **Two different "host"s — do not conflate them.** The **`<host>` path segment is a STORAGE
> PARTITION KEY** (which drawer the flow is filed in; validated by `cache_store.validate_host`; it
> has no runtime meaning). A flow document's own optional **`host` field is a RUNTIME NAVIGATION
> GUARD** — the runner refuses a `goto{url}` that leaves it. They may legitimately differ. Nothing
> in the store reads `doc["host"]`; nothing in the runner reads the partition key.

| Cap | Value | At the cap |
| --- | --- | --- |
| `MAX_FLOWS_PER_HOST` | **200** | **Hard reject** — `429 too_many_flows`. No silent eviction: a flow is *authored content*, and deleting one behind your back to make room would destroy work you wrote. |
| `MAX_RUNS_PER_FLOW` | **50** | **FIFO-evict** the oldest run. A run history is an *audit trail of a reusable automation*, not authored content — hard-rejecting run #51 would mean "this saved automation can never be run again", which is the wrong failure mode for the thing the feature exists to do. Losing the oldest audit line is the cheap failure. |
| `MAX_RUN_LOG_ENTRIES` | **2 000** | A run's step log is trimmed to its trailing 2 000 entries on save (a `for_each` inside a `paginate` can emit a lot). |
| `MAX_LIVE_FLOW_RUNS` | **1** | A second live run gets `too_many_sessions`. |

**A flow run and a live crawl refuse each other** (a cross-veto, in both directions): they drive the
same single-tenant PinchTab bridge — one bridge, one tab. A **dry** run is exempt: it opens no
browser, so it neither consumes the bridge nor blocks a crawl.

Deleting a flow **cascades**: its whole run history goes with it.

### Why a run is a subprocess

A run is executed by spawning `python -m pinchtab_webgraph.flow_cmd run <doc> --jsonl` and relaying
its JSONL frames — not by calling `runner.execute()` in-process. That is deliberate:

- **Cancel only works this way.** A flow can run for a long time (a `paginate` over 50 pages, each a
  real browser round-trip) and the user must be able to stop it. `runner.execute()` has **no
  cooperative-cancellation hook** — no callback, no flag it re-checks between steps — so an
  in-process design could never honour that click. The only cancellation primitive that actually
  works is **SIGTERM→SIGKILL on the process's own group** (`start_new_session=True`).
- **Crash isolation** on the thing holding a real, logged-in browser tab, for free.

This mirrors [`live_crawl.py`](ui.md#new-crawl-get-wscrawl-opt-in) exactly — same structure, same
`FlowRunUnavailable(reason, detail)` degradation instead of a crash, same process-group teardown.
User values never become a shell string: `build_run_argv` emits an argv **list** and the session
uses `create_subprocess_exec` (no shell), so a hostile input is an inert argv token.

The run's placeholder record is written to disk **before** anything is spawned, so a run that is
SIGKILLed — or that dies with the server — still leaves a discoverable record stuck at `"running"`
("we started this and never heard back") rather than vanishing.

### REST surface

CRUD + audit. All of it works with the env gate **off**; only `/ws/flows/run` is gated.

| Method · Path | Does |
| --- | --- |
| `POST /api/flows/validate` | `flow.validate()` on a posted document → `{"status":"ok", name, host, steps, capabilities, inputs, warnings}` or `{"status":"invalid", path, error}`. The editor's live check. `warnings` is the **resolvability** pass (`flow_resolve.py`): `[{"path":"steps[0]", "op":"goto", "goal":"reports", "match":null, "message":"no trigger matches “reports” in the example.test graph", "candidates":["Add Report"]}]` — advisory, still `ok`, still savable, and empty when the host has no cache. `POST`/`PUT` of a flow return it too. |
| `POST /api/flows/schema` | Stateless — the document's `inputs` as a JSON Schema (what the run form is built from). |
| `GET /api/hosts/{host}/flows` | `{"flows":[…]}` — the host's flow **summaries** (id, name, steps, capabilities, inputs, `run_count`, timestamps; **no** doc). |
| `POST /api/hosts/{host}/flows` | Create. Validates first. `429 too_many_flows` at the cap. |
| `GET /api/hosts/{host}/flows/{flow_id}` | The **full** record, `doc` included (the editor needs it). |
| `PUT /api/hosts/{host}/flows/{flow_id}` | Replace the document (re-validated). |
| `DELETE /api/hosts/{host}/flows/{flow_id}` | **Idempotent** — deleting an absent flow is a green `{"deleted": false}`, never a 404. Cascades to the run history. |
| `GET /api/hosts/{host}/flows/{flow_id}/schema` | The saved flow's `inputs` as a JSON Schema. |
| `GET /api/hosts/{host}/flows/{flow_id}/runs` | `{"runs":[…]}` — run summaries, newest `started_at` first. |
| `GET /api/hosts/{host}/flows/{flow_id}/runs/{run_id}` | The **full** run record: steps, artifacts, collected, stats. |
| `GET /api/hosts/{host}/flows/{flow_id}/artifacts` | `{"artifacts":[…], "stats":{scope, root, count, bytes}}` — the flow's **cumulative** ledger (scope = the flow id). |

**Status conventions.** A document that fails validation is a structured **miss**, not an HTTP
error: `200 + {"status":"invalid", path, error}` — the same shape `pwg flow validate` prints. A bad
**id token** is a different thing: a malformed *request*, rejected before any filesystem access
(`invalid_flow` / `invalid_run` → **400**, the twin of `invalid_session` on a chat id).

| Status | HTTP code |
| --- | --- |
| `invalid_flow` · `invalid_run` (bad id token) | `400` |
| `flow_not_found` · `run_not_found` | `404` |
| `too_many_flows` | `429` |
| `invalid` (the *document* was rejected) | **`200`** |

### `GET /ws/flows/run` — the frame protocol

`GET /ws/flows/run?host=<host>&flow_id=<id>` — the one route the env gate protects.

**Server → client**, in order: one **`flow`** bootstrap → per run: **`status`** → *N* × **`step`**
→ **`log`** (interleaved) → exactly one terminal **`result`**.

| Frame | Meaning |
| --- | --- |
| `{"type":"flow", …summary}` | The **leading** bootstrap frame, sent once on connect: the flow's `summary` (name, steps, `capabilities`, `inputs`, `run_count`, …). The client renders the run form from **this** — no second fetch. |
| `{"type":"status","state":"starting","host","flow_id","run_id","dry_run"}` | The subprocess launched. |
| `{"type":"step", …event}` | One [runner event](#run-records-and-events) — `{op, status, …}`, status ∈ `ok`/`new`/`dupe`/`triggered`/`skipped`/`dry-run`/`page`/`error`. `new`/`dupe` on a `download` step is what feeds the live counter. |
| `{"type":"log","line":<str>}` | Anything the subprocess printed that was **not** a JSON frame — a stray print, a warning, a traceback (stderr is prefixed `[stderr] `). Truncated to 500 chars. A line the UI never sees is a line nobody can debug. |
| `{"type":"result","run_id", …run record}` | **Exactly one** terminal frame per run: the full record (`status`, `steps`, `artifacts`, `collected`, `stats`, …). Sent **after** the run is persisted, so a client can immediately `GET` the run it was just told about. If the process died without printing one, the server synthesizes an honest `status:"error"` result carrying the steps it *did* relay. |
| `{"type":"error","status":"flow_unavailable","reason","detail"}` | The run can't start — `reason` ∈ `disabled` (the env gate is off) / `no_config` (`$PINCHTAB_CONFIG` unset or missing) / `bridge_unreachable`. The socket then closes. |
| `{"type":"error","status":"too_many_sessions","max":1}` | A crawl or another flow run already holds the bridge. The socket closes. |
| `{"type":"error","status":"invalid_input","detail","path"}` | A bad value for a declared input. **The socket stays open** — a bad input is one keystroke away from a good one, so fix the form and press Run again. |
| `{"type":"error","status":"invalid_flow"\|"flow_not_found"}` | Bad id token / no such flow; the socket closes. |

**Client → server:**

| Frame | Meaning |
| --- | --- |
| `{"type":"run","inputs":{…},"grant":{"allow_submit":…,"allow_upload":…,"allow_download":…},"dry_run":<bool>}` | Kick off a run. **Repeatable** — the socket survives a completed run, so Run-again is one click (the live-run counter is released in a `finally`, so back-to-back runs are accepted and a failed start can never wedge the bridge). |
| `{"type":"cancel"}` | SIGTERM→SIGKILL the run's process group. A **client disconnect is an implicit cancel** — and the partial run is still persisted. |

### Caveat: the artifact scope diverges between CLI and UI

The [dedupe ledger](#the-dedupe-ledger)'s scope is chosen differently by the two front ends:

| Front end | Default `--scope` |
| --- | --- |
| **CLI** (`pwg flow run`) | the flow's **sanitized `name`** (e.g. `download-all-invoices`) |
| **Web UI** (`/ws/flows/run`) | the flow's **stable id** (the uuid4 hex) |

The UI uses the id because a name can be renamed and two flows can share one — an id cannot, so two
saved flows can never poison each other's ledger. The consequence, stated plainly: **a flow authored
in the UI and then run from the CLI will not share a dedupe ledger** (the CLI would start a fresh
one under the flow's name and report every file as `new`). To make them share one, pass the flow id
explicitly:

```bash
pwg flow run ./my-flow.json --host app.example.com --scope <flow_id>
#   the flow id is the filename under ~/.pinchtab-webgraph/flows/<host>/<flow_id>.json
```

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

And these bite when you run flows from the **[web UI](#running-flows-from-the-web-ui)** (fuller
notes in [ui.md → Operational notes](ui.md#operational-notes-developing--e2e-testing-the-ui)):

8. **The UI can't run a flow at all unless `PINCHTAB_WEBGRAPH_ENABLE_FLOWS` is truthy** — the
   editor and every CRUD route work regardless, so "I saved it but Run does nothing" is almost
   always the missing gate. Launch with
   `PINCHTAB_WEBGRAPH_ENABLE_FLOWS=1 PINCHTAB_WEBGRAPH_ENABLE_CRAWL=1 portless run --name webgraph -- python3 -m pinchtab_webgraph.ui.server`
   (portless needs node 24; **in a git worktree only `portless run` is correct** — the flat
   `portless <name> <cmd>` form skips the worktree prefix and collides with the main checkout).
9. **To drive the UI itself with PinchTab (e2e), use the RAW `http://127.0.0.1:<port>/`.** The
   bridge's IDPI allowlist admits `127.0.0.1` / `localhost` but **not a `*.localhost`
   subdomain**, so the portless HTTPS URL is refused. That URL is for the human's browser.
10. **`pinchtab press Enter` does NOT activate a focused `<button>`** (verified: 0 click hits) —
    use `pinchtab type <sel> $'\n'`. It will silently no-op a UI e2e test otherwise.
11. **A flow authored in the UI and re-run from the CLI does not share its dedupe ledger** unless
    you pass `--scope <flow_id>` — see [the caveat
    above](#caveat-the-artifact-scope-diverges-between-cli-and-ui).

## Verified end-to-end

`tests/e2e/test_flow_paginate_download_e2e.py` runs the whole layer with **nothing mocked** — a
real PinchTab bridge, real headless Chrome, a real crawl of a 3-page fixture site: `goto{goal}`
resolves the reports page off the crawled graph, `paginate` walks all 3 pages, a nested `for_each`
downloads **5 real files** (`via="fetch"`) with **5 distinct sha256s**, and a second run with a
fresh `ArtifactStore` on the same ledger reports **0 new, 5 dupes**. It skips cleanly when no
bridge is reachable.

The document model, the VM, the browser primitives, the store and the CLI also have **175
browser-free unit tests** (`tests/test_flow.py`, `test_runner.py`, `test_browser.py`,
`test_artifacts.py`, `test_flow_cmd.py`) — the VM is exercised end-to-end against a `FakeBrowser`,
which is exactly what the port split buys.

The **UI** layer adds **63** more (`tests/test_flow_store.py`, `tests/test_ui_flow_runner.py`) plus
**16** flow-route tests in `tests/test_ui_server.py`. The Flows tab itself was also driven in a real
browser against a real bridge: author a flow → watch validation go red on a typo'd `${itm.href}` →
save → run live (3 pages paginated, **5 files downloaded, 5 new · 0 dupe**) → run again on the same
ledger (**0 new · 5 dupe**), with the crawl left un-wedged afterwards.

---

← Back to the **[documentation index](README.md)** · the **[main README](../README.md)** ·
related: **[Web UI](ui.md)** (the [Flows tab](ui.md#flows-view-opt-in)), **[`perform` live
test](perform-live-test.md)**, **[MCP server](mcp-server.md)**, **[authenticated
login](authenticated-login.md)**
