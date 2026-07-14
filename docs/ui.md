# Web UI

> **Docs:** [← README](../README.md) · [📚 Index](README.md) · [Automation flows](flows.md) · [MCP server](mcp-server.md) · [UTCP interface](utcp.md) · [Authenticated login](authenticated-login.md)

`pinchtab-webgraph` ships an **optional** local web UI: a small FastAPI app behind a
single console script, `pinchtab-webgraph-ui`. From one loopback-only browser app you can:

- **browse** any host's crawled **interaction graph** interactively (the [Graph view](#graph-view));
- **chat** — "how do I do X on this site?" — against the offline graph, across
  **multiple persistent named [chats](#chat-sessions)** per host, driving Claude through
  the project's own MCP tools;
- watch a **live headless-browser** pane and follow a [**"Show me how"** guided tour](#show-me-how-guided-tour)
  that highlights and clicks each step for you, stopping at the target form;
- **[crawl a brand-new URL](#new-crawl-get-wscrawl-opt-in)** and store it, right from the sidebar (opt-in);
- **[explore / search](#explore-view)** everything the crawl captured — full-text search,
  the create-form inventory + a goal path-finder, and the per-view content inventory —
  plus a Ctrl/Cmd-K [command palette](#command-palette) launcher over the whole UI;
- **author, run and re-run [automation flows](#flows-view-opt-in)** — the host's saved
  [declarative flows](flows.md), in a workbench of **three synchronized views**: an
  [**AI agent** you describe the automation to](flows.md#authoring-a-flow-with-the-ai-agent),
  an editable **visual canvas**, and the live-validating **JSON** — plus visible capability
  toggles, a streaming run log with a live `N new · M dupe` dedupe counter, run history, and
  the flow's all-time artifact ledger (opt-in);
- store per-host login credentials in a keyring-backed [vault](#vault-credentials).

Underneath it's a small FastAPI app that serves a read-only REST API over the offline
graph caches, the credentials vault, and the chat / screencast / crawl WebSockets.

Like the [MCP server](mcp-server.md) and the [UTCP manual](utcp.md), the UI is a thin
binding onto the same `api.py` query surface the CLI uses, so its answers are identical.
And like them, it stays out of the base install: everything lives behind the `[ui]`
extra and its own console script, and **nothing in the base package imports the `ui`
subpackage**. `pip install pinchtab-webgraph` never pulls in fastapi / anthropic / etc.

## The two-pane UX

Selecting a host in the sidebar opens two live WebSocket sessions for it at once:

```
┌───────────────┬───────────────────────────────────────────────┐
│  Crawled      │  app.example.com          interaction · 42 · 91│
│  graphs       ├──────────────────────┬────────────────────────┤
│  ───────────  │  Chat                │  Live browser          │
│  app.example  │  ──────────────────  │  ────────────────────  │
│  docs.example │  “how do I create a  │  ┌──────────────────┐  │
│  …            │   template?”         │  │  <img> ← JPEG     │  │
│               │  → numbered click-   │  │  screencast frames │  │
│  [Credentials]│    path + form spec  │  └──────────────────┘  │
└───────────────┴──────────────────────┴────────────────────────┘
```

- **Sidebar** — every host with a persisted crawl cache (from `/api/hosts`), plus a
  **Credentials** button that opens the vault modal.
- **Chat pane** (left) — a conversational "how do I do X on this site?" agent. It drives
  Claude with the project's **offline** MCP tools and streams the reply as it lands. A chip
  bar across its top holds **multiple named, persisted chats** per host — see
  [Chat sessions](#chat-sessions).
- **Live browser pane** (right) — a private headless Chrome, navigated to the host's home
  page, streamed frame-by-frame into an `<img>` via a CDP screencast.
- **Credentials modal** — the vault: store per-host login routing + a password (password
  goes to the OS keyring, never to disk) so the live pane can best-effort log itself in.

Every capability degrades independently: with no chat backend configured the chat pane
shows a `chat_unavailable` notice while the REST API and graph browsing keep working; with
no Chrome binary the live pane shows `screencast_unavailable`; with no keyring backend the
vault reports `vault_unavailable`.

## Graph view

The host header carries a **Workspace | Graph | Explore | Flows** view switcher. **Workspace**
is the two-pane chat + live-browser view above; **Graph** replaces it with an interactive
rendering of the selected host's **interaction graph** (below); **[Explore](#explore-view)**
replaces it with a read-only browser over the crawled cache (search / forms / content); and
**[Flows](#flows-view-opt-in)** replaces it with the host's saved automations (the only view
that can *write* to the target site, and the only one behind an opt-in gate). The first three
read the same offline cache the REST API and chat serve. Switching views only toggles
which pane is `hidden`; it **never** opens or closes the chat / live-browser WebSockets, so
flipping to Graph or Explore and back leaves both live sessions running untouched.

```
┌───────────────┬───────────────────────────────────────────────┐
│  Crawled      │  app.example.com   interaction · 45 · 662   [Workspace][Graph]
│  graphs       ├───────────────────────────────────┬───────────┤
│  ───────────  │  Search states by URL or label…   │  ● state  │
│  app.example  │  ┌─────────────────────────────┐  │  ◆ trigger│
│  docs.example │  │   ●───●   ◆ (opens a form)   │  │  ───────  │
│  …            │  │   │  ╲│                       │  │  detail:  │
│               │  │   ●   ●──●                    │  │  url/depth│
│  [Credentials]│  └─────────────────────────────┘  │ [Ask in…] │
└───────────────┴───────────────────────────────────┴───────────┘
```

**What the graph shows.** It renders the interaction-graph schema (`states` /
`edges{from,to}` / `triggers`) with a fixed visual language mirroring the [standalone
Cytoscape viewer](../README.md#-graph-shape):

- **States** — blue circles (`●`), one per crawled page / SPA view, **sized by out-degree**
  (a hub with many outbound edges is drawn larger). Labelled by the state's label or a
  trimmed URL path.
- **Form-triggers** — green diamonds (`◆`), one per create-trigger that "opens a form",
  linked from the state that surfaces them by a dotted green edge.
- **Edges** — `link` navigation edges are solid gray arrows; trigger edges are dotted green.
- A **legend** (state vs. trigger) sits in the detail rail.

**Search / filter.** The toolbar's *"Search states by URL or label…"* box filters the graph
live as you type — nodes whose label or URL don't contain the query are hidden (client-side,
no refetch).

**Focus + detail panel.** Clicking a node dims the rest of the graph and spotlights the
node's neighbourhood (an adjacency highlight), and fills the right-hand detail rail: for a
**state**, its URL and depth; for a **trigger**, the form title, field count, where it opens,
and its selector. The panel's **"Ask in chat"** button prefills the chat input with *"How do
I get to `<label>`?"* and focuses it — a one-click bridge from "I see this node" to "tell me
the click-path" (it only prefills the box; you still send it, and the chat socket is untouched).

**Fully offline.** The whole view is served from the cached interaction graph via
`GET /api/hosts/{host}/graph` — no browser, no network, no crawl. The status line reports the
rendered counts (e.g. `45 states · 53 triggers · 662 edges`).

**Interaction graphs only (v1).** The Graph view renders **interaction** graphs. A host whose
cache is a page→page **link graph** shows a message pointing you at that host's **standalone
`.html` viewer** (produced by `linkcrawl` / `crawl.py`) instead — the in-UI renderer is
interaction-graph-only for now. Structured-error and non-graph payloads are reported inline
rather than crashing the pane. Switching hosts always returns to the Workspace view and tears
down the prior render; the Graph tab is disabled for a host whose cache failed to load.

### `/vendor` mount + lazy loading

The Graph view reuses **Phase 1's vendored Cytoscape/fcose stack** — the exact same six
minified libs (`cytoscape`, `dagre`, `cytoscape-dagre`, `layout-base`, `cose-base`,
`cytoscape-fcose`) that `crawl.py` inlines into the standalone viewer. Rather than duplicate
that ~785KB under `static/`, the server adds a **`/vendor` static mount** that serves them
straight from `pinchtab_webgraph/vendor/`:

| Mount | Serves | Notes |
| --- | --- | --- |
| `/vendor/*` | `pinchtab_webgraph/vendor/*.min.js` (the 6 Cytoscape libs) | registered **before** the catch-all `/` mount so it isn't shadowed; path-traversal out of the vendor dir is rejected (403/404) |
| `/` | `pinchtab_webgraph/ui/static/` (`html=True`) | the SPA shell + `app.js` / `graph.js` / `explore.js` / `style.css` / `graph.css` / `explore.css`, registered **last** so it never shadows `/api/*` or `/vendor/*`. `explore.js` / `explore.css` load **eagerly** (no vendor deps); `graph.js` + the Cytoscape libs load lazily |

The libs **and** `graph.js` are **lazy-loaded on the first Graph-tab open** — injected
sequentially by `app.js` (core → layout deps → fcose extension → controller; the order is
load-bearing) and memoized, so a session that never opens the Graph view never pays the
Cytoscape download and the SPA shell stays light. A load failure is reported in the graph
status line and clears the memo so a later switch retries.

## Explore view

The third view tab, **Explore**, is a read-only browser over everything the crawl already
captured. It surfaces data the offline `api.py` surface has always computed but that no UI
exposed before — full-text content search, the create-form inventory, a goal path-finder,
and the per-view content inventory — behind **three sub-tabs** (`Search | Forms | Content`).
No new endpoints: it is a fresh UI over four pre-existing REST routes.

```
┌───────────────┬───────────────────────────────────────────────┐
│  Crawled      │  app.example.com   [Workspace][Graph][Explore]
│  graphs       ├───────────────────────────────────────────────┤
│  ───────────  │  [ Search ][ Forms ][ Content ]   12 matches…  │
│  app.example  │  ┌─────────────────────────────────────────┐  │
│  docs.example │  │ Search captured page data…      [Search] │  │
│  …            │  │  ● Roles  [reachable][2 clicks]          │  │
│               │  │    admin/roles                           │  │
│  [Credentials]│  │    1. Click “Settings”  2. Click “Roles” │  │
└───────────────┴───────────────────────────────────────────────┘
```

**Search sub-tab** → `GET /api/hosts/{host}/content/search?text=<q>&limit=40`. A text box
searches every crawled view's captured collections. Each matching view is a card showing a
**reachable / unreachable** badge, a **click-count** badge, the **click-path** to reach it
(one step per line), and the **matched items** (`kind` + text); a server-truncated view adds
a *"+ more"* marker. The status line reports `N matches · M views · showing K`. The `limit`
is fixed at 40, and an empty query is a guarded no-op (never the 422 an empty `text=` would
return). Results stay empty until you submit.

**Forms sub-tab** → the create-form inventory + a goal path-finder:

- **Create-form inventory** — `GET /api/hosts/{host}/forms`: one card per discovered
  create-form, showing its **label**, a **click-depth** badge, and a **field-count** badge.
  Each card carries a **"Show me how"** button. Clicking it runs
  `GET /api/hosts/{host}/howto?match=<label>` and, on an `ok` result, **switches to the
  Workspace view** and hands the first result's `tour` steps to the existing
  [`startTour` overlay](#show-me-how-guided-tour) — the same guided tour the chat offers,
  driven straight from a form row. A form with no reachable path shows an inline
  *"No reachable path found for this form."* note instead.
- **Goal path-finder** — a free-text *"How do I… (e.g. create a role)"* box →
  `GET /api/hosts/{host}/howto?goal=<goal>`. On a confident hit it lists the matching
  click-path(s) (each with its own **"Show me how"**); on a miss it additionally surfaces
  the server's **`candidates`** and the dimmed **`low_confidence`** matches — routing data
  the resolver always computed but that was previously invisible in any UI.

**Content sub-tab** → `GET /api/hosts/{host}/content`: the per-view inventory of captured
collections — one card per crawled view (label + URL), each listing its collections as
`kind × count` with a sample string.

**Show-me-how reuses the tour, verbatim.** `explore.js` never owns the live/tour sockets;
it just calls the `app.js` globals `setView("workspace")` and `startTour(...)`, so a form's
"Show me how" and a chat answer's "Show me how" drive the **same** live-pane overlay
([Show Me How guided tour](#show-me-how-guided-tour)) — it highlights each step, and **Next**
performs the real click, stopping at the target form without ever submitting it.

**Safety (same discipline as [graph.js](#graph-view)).** Every crawled string is rendered
with `createElement` + `textContent`, never `innerHTML`. **Crawled URLs are shown as a plain
`<span>`, never a live `<a href>`** — clicking one must never navigate the SPA away. Every
query param is `encodeURIComponent`-encoded, and a form **label is regex-escaped before it
becomes a `?match=`** pattern (so a label with regex metacharacters stays a literal match).

**Loaded eagerly.** Unlike the Graph view's lazy Cytoscape stack, `explore.js` has **no
vendor dependencies**, so `/explore.js` (script, after `app.js` which it depends on) and
`/explore.css` (stylesheet) are served **eagerly** from the `/` static mount — the Explore
tab is instant. Switching hosts calls `destroyExploreView()` first, so a stale host's search
results / forms / content never bleed into the next; Forms and Content are fetched once per
host and cached across sub-tab flips (Search refetches per submit).

### Command palette

A **Ctrl/Cmd-K** command palette (also reachable from the header button) is a keyboard
launcher over the UI's **existing** state and functions — it opens **no new fetches**, just
re-dispatches onto globals the SPA already exposes. It offers:

- **Jump view** — *Go to Workspace / Graph / Explore* (a view that's unavailable for the
  current host renders greyed with a *"unavailable for this host"* hint rather than hiding).
- **Switch host** — a dynamic *"Switch to `<host>`"* row per sidebar host (it replays that
  row's click).
- **New chat** (host-gated — shows *"pick a host first"* when none is selected).
- **New crawl** (focuses the sidebar [New-crawl](#new-crawl-get-wscrawl-opt-in) form; never
  host-gated, since crawling is how you get your first host).
- **Manage credentials** (opens the [vault](#vault-credentials) modal).
- **Free-text content search** — typing anything adds a *"Search content for `…`"* row that
  hands off into Explore's Search panel: it switches to the Explore view, selects the Search
  sub-tab, fills the box, and submits — one keystroke path from "anywhere" to a content search.

Typing **substring-filters** the action list; **↑/↓** move the highlight and **Enter** runs
the selected row (a disabled row is a no-op and explains why via its hint). Every row is
built with `createElement` + `textContent` (host names are untrusted crawled data), and the
Ctrl/Cmd-K shortcut fires even while a chat or crawl input is focused.

## Flows view (opt-in)

The fourth tab, **Flows**, is the front end for the [declarative automation flow
layer](flows.md) — the host's **saved automations**. It is a **workbench**, not a textarea:
**[describe the automation to an agent](flows.md#authoring-a-flow-with-the-ai-agent)**, or draw it
on a **visual canvas**, or type the **JSON** — three synchronized views of **one** document. Then
run it against a real browser, watch every step stream in, and re-run it to see the content-hash
dedupe do its job.

```bash
PINCHTAB_WEBGRAPH_ENABLE_FLOWS=1 pinchtab-webgraph-ui        # 1 / true / yes / on
```

**Off by default**, the same posture as [New crawl](#new-crawl-get-wscrawl-opt-in) — and more
warranted: a crawl *structurally never submits*, where a flow's `do{submit:true}` / `upload` step
**can write to the real site**. With the gate unset the editor, **the agent** and the CRUD routes
still work; only `/ws/flows/run` refuses (`flow_unavailable` / `disabled`). Authoring is always
safe; *running* is what is gated.

```
┌───────────────┬──────────────────────────────────────────────────────────┐
│ Crawled       │ app.example.com   [Workspace][Graph][Explore][Flows]      │
│ graphs        ├──────────┬────────────────┬──────────────────────────────┤
│ ───────────   │  Flows   │ Chat (mode=    │  ┌ canvas ─────────────────┐ │
│ app.example   │  ──────  │       flow)    │  │ goto  goal=Reports       │ │
│ …             │ ▸ downl- │ ──────────────  │  │ ┌ paginate ────────────┐ │ │
│               │   all-   │ “download every│  │ │ ┌ for_each ────────┐ │ │ │
│               │   reports│  report PDF    │  │ │ │ ▸ download        │ │ │ │
│               │   ·3     │  across all    │  │ │ └──────────────────┘ │ │ │
│               │ ▸ export-│  the pages”    │  │ └──────────────────────┘ │ │
│               │   users  │                │  └──────────────────────────┘ │
│  [Credentials]│          │ ▸ draft ✓ ok   │  { "name": …, "steps": […] }  │
│               │ [+ New   │   (chip)       │  ✓ valid · 2 steps  ← JSON    │
│               │   flow]  │                │  ┌──────────────────────────┐ │
│               │          │                │  │ [x] Dry run [ ] Allow sub│ │
│               │          │                │  │ [Run flow][Cancel] 5 new·│ │
│               │          │                │  └──────────────────────────┘ │
│               │          │                │  Runs (history) │ Artifacts   │
└───────────────┴──────────┴────────────────┴──────────────────────────────┘
```

- **Three synchronized views, one document.** The **chat agent** proposes; the **canvas** is
  clicked; the **JSON** is typed. Any of them mutates the doc, the other two re-render, and
  validation runs on every change. A validation error at `steps[1].body[0]` **highlights that
  canvas box** — the canvas's `data-path` uses `flow.py`'s path grammar verbatim. The full model
  is in **[flows.md → Authoring a flow with the AI
  agent](flows.md#authoring-a-flow-with-the-ai-agent)**; the UI-side wiring is
  [below](#the-flow-agent-and-the-mode-axis).
- **Live validator.** Every keystroke (debounced) is `POST`ed to `/api/flows/validate` — which is
  `flow.validate()`: pure, no browser, no graph. A typo'd `${itm.href}` is caught **as you type**,
  named together with its exact path in the document, and **Save stays disabled** until the document
  is legal. A bad document can never be saved, let alone scheduled.
- **Resolvability warnings (amber).** The same call re-resolves every `goto`/`do` **`goal`** against
  the host's *crawled graph*. A goal that matches nothing (`"reports"` on a site that only has
  “Add Report”) is a valid document that would abort at run time — so the banner turns amber
  (`valid — 3 steps · ⚠ 1 step may not resolve`), the offending canvas box goes amber
  (`.flow-node-warn`, distinct from the red `.flow-node-error`), and the box prints the message plus
  the *candidate labels the site actually has*. **Save stays enabled** — it is a warning, not a
  blocker (the flow may predate the crawl), and an uncrawled host produces no warnings at all.
- **The safety model is visible, not just enforced.** The **Allow-submit** / **Allow-upload**
  toggles are **disabled unless the flow document itself declares that capability**, and **Dry run
  is checked by default**. That makes *"a write happens only if the flow **declares** it **and** the
  caller **grants** it"* something you can see. The server re-derives the same AND before spawning,
  and the runner re-checks it per step — the checkbox is a convenience, never the enforcement.
- **The dedupe counter is the product.** `download` step frames arrive with status `new` or `dupe`,
  which the log folds into a live **`N new · M dupe`** counter. Re-running a flow and watching it
  report **0 new · 5 dupe** is what makes this a change detector rather than a poller.
- **History + an all-time ledger.** Past runs replay their persisted step log into the same panel;
  the **Artifacts** column is the flow's cumulative [dedupe ledger](flows.md#the-dedupe-ledger) —
  every distinct file it has *ever* fetched (name / size / when / sha256), which a single run record
  cannot answer.
- **A flow run and a live crawl refuse each other** (cross-veto, both directions): they drive the
  same single-tenant PinchTab bridge. A **dry** run is exempt — it opens no browser.

### The flow agent and the `mode` axis

The Flows tab's chat pane is **the same chat pane as the Workspace tab's** — the same
`createChatPane` factory in `app.js`, mounted a second time with flow-namespaced element ids. One
implementation, two mounts: a second copy of the pane would drift the first time the frame protocol
moved. (It did, once: extracting the factory briefly leaked flow chats into the Workspace tab's chip
bar — fixed by the `mode` filter below, and regression-tested.)

What separates them is **one axis: `mode`.**

| | `mode: "workspace"` (the default) | `mode: "flow"` |
| --- | --- | --- |
| **The job** | the navigation assistant — *"how do I do X on this site?"* | the **flow author** — *"download every report PDF across all the pages"* |
| **Tools** | the **6** offline graph tools (`chat.OFFLINE_TOOL_NAMES`) | those same **6**, **plus** `propose_flow` (`FLOW_TOOL_NAMES`) — 7 |
| **System prompt** | `build_system_prompt` | `build_flow_system_prompt` — a **sibling**, not a branch. Its op table, capability names and loop vars are **generated from `flow.py`'s tables**, so the prompt can't drift from the validator |
| **Extra per-turn payload** | `live_url` (the live pane's position) | `draft` (the live flow document) |
| **Extra frame** | `tour` (after an OK `howto`) | `flow_draft` (after a `propose_flow`) |
| **Where its chats are listed** | the Workspace chip bar | the Flows tab's chip bar |

**The fence is additive and fails closed.** `chat.effective_tool_names(mode)` is the single safety
predicate: flow mode **unions in** `{"propose_flow"}`; the 6-tool browsing fence is never widened;
and **any unknown mode degrades to the base 6** (so a typo'd or hostile mode token can never be the
thing that grants a tool). `server._mode()` normalizes the query param the same way.

**A session's mode is PINNED at creation** — written into its record by `chat_store.create` and read
back from the record on every resume, exactly like its `backend`. `open_chat_session` passes
`mode=None` for a resumed session, so **the query param is irrelevant once a session exists**: a
workspace chat **cannot be escalated** into flow mode by reconnecting with `?mode=flow` (and thereby
handed a tool it was never granted). A record written before flow mode existed counts as
`"workspace"`.

**And the agent can only PROPOSE.** `propose_flow` is a pure validate-and-echo — no disk, no
browser, no subprocess — and **no tool anywhere on the MCP surface reaches `flow_store` (save) or
`flow_runner` (run)**. Both facts are held by tests (`test_propose_flow_is_pure_no_disk_no_subprocess`
poisons `open`/`os.replace`/`subprocess`; `test_no_flow_save_or_run_tool_exists_anywhere` guards the
surface). **Save and Run are the human's, and only the human's.** See
[flows.md](flows.md#the-agent-can-only-propose--and-that-is-structural).

**The draft round-trips every turn.** The SPA ships the **live** document (`getDraft()`, read from
the editor's current state — never the agent's stale copy) as `user_message.draft`, and
`chat.augment_with_flow_draft` prefixes the turn with it. So the agent revises *the document on your
screen*, and **hand edits made between turns survive**. A **replayed** draft (out of a stored
transcript) is deliberately **never** auto-applied — it renders as a chip with an explicit **"Restore
this draft"** button, and a monotonic generation guard drops any superseded async writer, so
reopening an old chat can't clobber the flow you just opened.

### Flow modules (and what they mirror)

The tab is two new server modules, each a deliberate twin of an existing one:

| New module | Mirrors | What it does |
| --- | --- | --- |
| [`ui/flow_store.py`](../pinchtab_webgraph/ui/flow_store.py) | [`chat_store.py`](#on-disk-layout) | Saved flows + run records on disk: `<home>/flows/<host>/<flow_id>.json` and `…/<flow_id>/runs/<run_id>.json`. Stdlib-only, per-host directory, **atomic writes** (tmp + `os.replace`), one id/host validation choke-point (uuid4-hex ids, `cache_store.validate_host`). Caps: **200 flows/host** (hard reject, `429 too_many_flows` — a flow is authored content, never silently evicted) and **50 runs/flow** (**FIFO-evict** — an audit line is cheap to lose; the ability to run the automation is not). |
| [`ui/flow_runner.py`](../pinchtab_webgraph/ui/flow_runner.py) | [`live_crawl.py`](#new-crawl-get-wscrawl-opt-in) | Spawns `python -m pinchtab_webgraph.flow_cmd run <doc> --jsonl` as a **subprocess** and relays its JSONL frames. Same degradation idiom (`FlowRunUnavailable(reason, detail)` → a structured frame, never a 500), same process-group teardown, same argv-list-never-a-shell-string discipline. `MAX_LIVE_FLOW_RUNS = 1`. |

**Why a subprocess and not an in-process `runner.execute()`:** `runner.execute()` has **no
cooperative-cancellation hook** — no callback, no flag it re-checks between steps — so an in-process
design could never honour a **Cancel** click. The only cancellation primitive that works is
SIGTERM→SIGKILL on the process's own group (`start_new_session=True`). Crash isolation on the thing
holding a real, logged-in browser tab comes free with it.

> **The `<host>` in `<home>/flows/<host>/…` is a STORAGE PARTITION KEY** — which drawer the flow is
> filed in — and is *not* the flow document's own optional `host` field, which is a **runtime
> navigation guard** the runner enforces. They may legitimately differ. See
> [flows.md → Storage layout and caps](flows.md#storage-layout-and-caps).

### The flow REST + WS surface

Twelve REST routes (CRUD + audit; all usable with the env gate **off**) and one gated WebSocket.
The full table, the status-code contract, the frame protocol, and the artifact-scope caveat live in
**[flows.md → Running flows from the web UI](flows.md#running-flows-from-the-web-ui)**. In brief:

| Method · Path | Does |
| --- | --- |
| `POST /api/flows/validate` · `/api/flows/schema` | Stateless: validate a posted document (structural verdict **+ [resolvability `warnings`](flows.md#resolvability-warnings)**) / derive its `inputs` JSON Schema. |
| `GET /api/flows/op_schema` | Stateless: **the op vocabulary itself**, served from `flow.py`'s own `LEAF_OPS`/`BODY_OPS`. **Every** per-op canvas form is derived from it — there is no second copy of the DSL in JS to drift. |
| `GET` · `POST /api/hosts/{host}/flows` | List the host's flow summaries / create one. |
| `GET` · `PUT` · `DELETE /api/hosts/{host}/flows/{flow_id}` | The full record (doc included) / replace / delete (idempotent, cascades to runs). |
| `GET /api/hosts/{host}/flows/{flow_id}/schema` | The saved flow's `inputs` as a JSON Schema. |
| `GET /api/hosts/{host}/flows/{flow_id}/runs` · `/runs/{run_id}` | Run summaries (newest first) / one full run record. |
| `GET /api/hosts/{host}/flows/{flow_id}/artifacts` | The flow's cumulative artifact ledger + stats. |
| **`GET /ws/flows/run?host=&flow_id=`** | **Execute a saved flow.** Gated by `PINCHTAB_WEBGRAPH_ENABLE_FLOWS`. |

The `/ws/flows/run` wire format: a **`flow`** bootstrap frame (the client builds the run form from
it — no second fetch) → per run **`status`** → *N* × **`step`** → **`log`** (anything the subprocess
printed that wasn't a frame, stderr included) → **exactly one** terminal **`result`** (sent *after*
the run is persisted). The client sends `{"type":"run", inputs, grant, dry_run}` — **repeatable**,
so Run-again is one click — and `{"type":"cancel"}`; **a client disconnect is an implicit cancel**,
and the partial run is still persisted.

A document that fails validation is a structured **200 miss** (`{"status":"invalid", path, error}`,
the same shape `pwg flow validate` prints); a bad **id token** is a malformed request
(`invalid_flow` / `invalid_run` → **400**), rejected before any filesystem access.

## Chat backends

The chat pane has **two interchangeable backends**. Both stream the identical frame
protocol and are locked to the same six offline graph tools — they differ only in *how*
they reach Claude:

| Backend | Reaches Claude via | Auth | Needs |
| --- | --- | --- | --- |
| **`api`** (default when a key is present) | the **Anthropic API** directly | an `ANTHROPIC_API_KEY` | the `[ui]` extra (`anthropic`) |
| **`claude_code`** | your **locally-logged-in Claude Code**, driven through the Claude Agent SDK | **no API key** — it uses your Claude Code login (`~/.claude/.credentials.json`) | the `claude` CLI on `PATH` + the `[ui-claude-code]` extra |

The `claude_code` backend is the way to use the chat with a **Claude Code subscription and
no API key at all**: it spawns your logged-in `claude` as a subprocess (via the Claude
Agent SDK) and points it at the pinchtab-webgraph MCP server as its only tool source.

**Selection logic** (`chat_backend.resolve_backend_name`), first match wins:

1. `PINCHTAB_UI_CHAT_BACKEND` set to `api` or `claude_code` → that backend (explicit override).
2. `ANTHROPIC_API_KEY` set → `api` (a configured key is the strongest signal).
3. the `claude` CLI on `PATH` → `claude_code` (a logged-in Claude Code is available).
4. otherwise → `api` (so you get the actionable `no_api_key` notice).

**Model override:** the `api` backend defaults to `claude-opus-4-8` (override with
`PINCHTAB_UI_MODEL`); the `claude_code` backend defaults to **your account's Claude Code
default model** (override with `PINCHTAB_UI_CLAUDE_CODE_MODEL` — deliberately *not* the API
`claude-opus-4-8` alias, which is an API model id, not necessarily a valid CLI alias).

**Same safety posture, both backends.** The `claude_code` agent is fenced to the exact
same six offline tools as the `api` backend: every built-in tool (Bash / Write / Edit / …)
is removed (`tools=[]`), the two live tools (`crawl`, `ask_howto`) are stripped, no
`~/.claude` / `CLAUDE.md` / project settings are loaded (`setting_sources=[]`,
`strict_mcp_config=True`), and a deny-by-default `can_use_tool` backstop rejects anything
not on the allow-list. It can never run a shell command on the host or drive a live crawl.
See [Security model](#security-model).

## Chat sessions

Chat history is **persistent and multiple**. Each host owns a set of **named chats**;
the chat pane carries a **session chip bar** across its top, and every chat is durably
saved to disk keyed by `(host, session-id)` — so switching host, reloading the page, or
dropping the WebSocket no longer discards the conversation.

```
┌──────────────────────────────────────────────┐
│  Chat                                          │
│  ┌────────────┬────────────┬───────┐ ┌──────┐ │
│  │ Templates ×│ Billing  × │ Chat× │ │ + New│ │  ← session chip bar
│  └────────────┴────────────┴───────┘ └──────┘ │
│  ────────────────────────────────────────────  │
│  “how do I create a template?” → …             │
└──────────────────────────────────────────────┘
```

- **`+ New`** mints another chat for the host (`POST …/sessions`) and connects to it.
- **Click a chip** to switch: the SPA reconnects the chat socket to that session (closing
  the old one) and the chat log is restored from the reopened session's transcript.
- **Double-click a chip title** to rename it inline (`PATCH …/sessions/{id}`). An explicit
  title is *locked* so the auto-title never overwrites it.
- **The trailing `×`** deletes with a **two-click confirm** (first click arms it and
  auto-disarms after ~2s; a second click issues `DELETE …/sessions/{id}`).

A chat with no explicit title is **auto-titled** from its first user message (collapsed
whitespace, capped at 60 chars).

### On-disk layout

Records live under the [cache/config home](#environment-variables)
(`$PINCHTAB_WEBGRAPH_HOME`, default `$HOME/.pinchtab-webgraph`), one JSON file per chat:

```
<home>/sessions/<host>/<id>.json
```

`chat_store.py` mirrors `cache_store.py` exactly: stdlib-only, a per-host directory,
**atomic writes** (tmp file + `os.replace`, so a reader never sees a half-written record),
and a single validation choke-point. `<host>` is run through `cache_store.validate_host`;
`<id>` is a **uuid4 hex** (`^[0-9a-f]{32}$`) validated by `chat_store.validate_session_id`
— no separators or dots, so a raw id can never resolve outside its host's directory.

Each record carries `id`, `host`, `backend`, **`mode`**, `title` / `title_locked`, `created_at` /
`updated_at`, `message_count`, `sdk_session_id`, and **two** history fields.

**`mode`** (`workspace` | `flow`) is written once at creation and — like `backend` — **pinned** on
every resume, never re-resolved (see [The flow agent and the `mode`
axis](#the-flow-agent-and-the-mode-axis)). A record written before flow mode existed reads back as
`"workspace"`, so an old chat keeps resuming as the navigation assistant it was created as.

The history fields:

- **`transcript`** — the display-only fold of the emitted WS frames (user text +
  assistant `text` / `tool_use` / `tool_result` / `tour` / **`flow_draft`** / `error` entries;
  a `flow_draft` persists the **proposed document itself**, not just its note, so reopening a
  flow chat can restore the draft to the canvas). Replayed
  verbatim on reconnect, so the chat **log** is restored for **every** backend. A
  `TranscriptSink` wraps the route's `emit` and folds each frame into the record as it is
  sent, so both backends persist identically.
- **`wire_messages`** — the `api` backend's serialized Anthropic message list, so it can
  **resume** the conversation. `null` for the `claude_code` backend.

### Load-on-connect (the `session` bootstrap frame)

Reconnecting to an existing chat replays it. When `/ws/chat` opens with a
[`session=` param](#get-wschathosthost), the server resolves that record **before** opening
the backend and sends a **leading bootstrap frame** — the session summary plus its
transcript — as the very first frame, ahead of any turn:

```json
{"type":"session","id":"…","host":"…","backend":"api|claude_code",
 "title":"…","created_at":"…","updated_at":"…","message_count":N,
 "transcript":[ … ]}
```

The SPA learns the id it is now bound to from the summary and **replays** the transcript to
rebuild the log. A brand-new chat simply carries an empty `transcript`. Restored (untrusted)
content only ever travels the existing escaping paths — `textContent` for chips and plain
text, the HTML-escape-first `renderMarkdown` for assistant messages — never a raw
`innerHTML` sink.

### Restore: full continuation vs. display-only

The two [chat backends](#chat-backends) restore differently, and the backend is **pinned**
to whatever the chat was created with (it is never re-resolved on resume):

| Backend | On reopen | Recalls earlier turns? |
| --- | --- | --- |
| **`api`** | full **save + restore-to-continue** — `wire_messages` are rehydrated so the Anthropic-API conversation continues. Trimming is turn-boundary-aware (`trim_wire_messages` never splits a `tool_use`/`tool_result` pair, and drops a trailing unanswered user turn so a resumed session can't send two consecutive user turns → API 400). | **Yes** — the model continues the conversation. |
| **`claude_code`** | **display-only** in v1: the transcript replays so the log is restored, but the SDK session is fresh. The `sdk_session_id` is captured for a future resume. | **No** — flagged in the UI (see badge). |

Because a restored `claude_code` chat looks complete but the agent won't remember it, the
chat status line shows a **badge** when a non-empty `claude_code` transcript is restored:
*"restored view — this backend won't recall earlier turns yet"*.

### Limits

- **`MAX_SESSIONS_PER_HOST` = 50.** A host at the cap **rejects** a new chat (`POST` →
  **429 `too_many_sessions`**) — there is **no silent eviction** of an old chat.
- **`MAX_TRANSCRIPT_ENTRIES` = 500.** A transcript is trimmed to its trailing 500 entries
  on each save, so a very long chat's on-disk record stays bounded.

## Prerequisites & install

```bash
pip install 'pinchtab-webgraph[ui]'
```

On Ubuntu / any PEP-668 "externally-managed" Python, either use a venv (recommended) or
add the escape flags:

```bash
pip install --user --break-system-packages 'pinchtab-webgraph[ui]'
```

The `[ui]` extra pulls **fastapi**, **uvicorn** (`uvicorn[standard]`), **keyring**,
**anthropic**, **websockets**, and — self-referencing — the **`[mcp]` extra** (the chat
agent drives the pinchtab-webgraph MCP server as its tool backend).

To use the **Claude Code chat backend** instead of the Anthropic API (drive your local,
logged-in Claude Code with no API key), also install the separate `[ui-claude-code]` extra:

```bash
pip install 'pinchtab-webgraph[ui,ui-claude-code]'
```

It's a **separate** extra on purpose — the Claude Agent SDK it pulls (`claude-agent-sdk`,
~75MB) is *not* forced onto a plain `[ui]` install. It also needs the `claude` CLI
installed and logged in (`claude`). See [Chat backends](#chat-backends).

Beyond the extra, each of the three live capabilities needs one more thing at runtime;
none is required to run the server or use the offline REST API:

| Capability | Additionally needs |
| --- | --- |
| **Offline REST API + graph browsing** | only a populated cache (nothing else) |
| **Chat pane** | **either** an `ANTHROPIC_API_KEY` (the `api` backend) **or** a logged-in `claude` CLI + the `[ui-claude-code]` extra (the `claude_code` backend, no key). See [Chat backends](#chat-backends). |
| **Live browser pane** | a Chrome/Chromium binary on `PATH` (+ a running PinchTab bridge, via `PINCHTAB_WEBGRAPH_BRIDGE`, for automated login) |
| **Credentials vault** | a usable OS **keyring** backend (e.g. Secret Service, macOS Keychain, or `keyrings.alt` for a file backend) |

The live browser pane looks for `google-chrome`, `google-chrome-stable`, `chromium`, or
`chromium-browser` on `PATH`.

## Running

```bash
pinchtab-webgraph-ui                 # serve on http://127.0.0.1:8765/
pinchtab-webgraph-ui --open          # …and open it in a browser once it's up
pinchtab-webgraph-ui --port 9000     # pick a different port
```

| Flag | Default | Effect |
| --- | --- | --- |
| `--host` | `127.0.0.1` | bind address |
| `--port` | `$PORT` if set, else `8765` | bind port |
| `--open` | off | open the UI in a browser once the server is listening |

### portless-native

`--port` honours the `$PORT` environment variable, so the server runs cleanly through
[portless](https://www.npmjs.com/package/portless) — no port to remember, no `8765`
collision:

```bash
portless webgraph python -m pinchtab_webgraph.ui.server
# -> https://webgraph.localhost   (portless assigns a free $PORT and proxies to it)
```

`--host` stays `127.0.0.1` (portless proxies from the same machine), so the loopback-only
safety model below is unchanged.

**It binds loopback (`127.0.0.1`) by default, on purpose.** The vault WRITE endpoints,
the chat agent, and the live browser pane are **unauthenticated** — anyone who can reach
the port can store or delete keyring credentials, drive the chat agent (spending API
credits), and make the server launch local headless Chrome processes that best-effort
drive the credential-bearing PinchTab bridge to log in. Binding anything other than
`127.0.0.1` / `localhost` / `::1` prints a loud warning for exactly this reason. Keep it
on loopback and reach it from the same machine (or an SSH tunnel).

## REST API

The `/api/*` surface is the **same offline `api.py` query surface** as the CLI
(`pwg query`), the [MCP server](mcp-server.md), and the [UTCP manual](utcp.md) — the same
structured dicts, just over HTTP. Everything here is offline: a cached graph, no browser,
no network. Routing is **by URL hostname only** — there is deliberately no `graph=`
filesystem-path parameter over HTTP.

### Offline graph queries

| Method · Path | Wraps | Notes |
| --- | --- | --- |
| `GET /api/health` | — | `{status:"ok", version}` liveness probe |
| `GET /api/hosts` | `cache_store.list_hosts` + `api.graph_summary` | index of every cached host + a cheap per-host summary (+ `caches_dir`) |
| `GET /api/hosts/{host}/summary` | `api.graph_summary` | graph kind + meta + element counts |
| `GET /api/hosts/{host}/graph` | `cache_store.load` | the full raw interaction graph (the large payload, on demand) — also what the in-UI [Graph view](#graph-view) renders |
| `GET /api/hosts/{host}/forms` | `api.list_forms` | every create-form: label, host, depth, field count. Also drives the [Explore](#explore-view) Forms sub-tab |
| `GET /api/hosts/{host}/howto?goal=&start=&match=&all=` | `api.howto` | shortest click-path(s) to a create-trigger or download + its form; each result also carries additive `tour` (the [Show Me How](#show-me-how-guided-tour) highlight steps), `commands` (a runnable PinchTab command block reproducing the path + terminal action), `action_kind` (`form`/`download`), and `download_url` (for a direct download). Backs the chat agent, the [Explore](#explore-view) path-finder, and each form's "Show me how" |
| `GET /api/hosts/{host}/content` | `api.list_content` | per-view inventory of captured collections. Backs the [Explore](#explore-view) Content sub-tab |
| `GET /api/hosts/{host}/content/search?text=&start=&limit=` | `api.find_content` | search captured collections for text; `text` required, `limit` default 40. Backs the [Explore](#explore-view) Search sub-tab |

The [Explore view](#explore-view) (Phase 5) is the **first UI consumer** of these four
routes — `/content` and `/content/search` had no front end at all before it, and `/forms` /
`/howto` were previously reachable only through the chat agent.

### Vault (credentials)

The password write side (Phase 2). The plaintext secret enters **only** via the PUT JSON
body and leaves the process **only** through `keyring.set_password`; GET and DELETE never
carry it, and every response body is a masked, `has_password`-only view.

| Method · Path | Does | Notes |
| --- | --- | --- |
| `GET /api/vault/status` | keyring backend health + `config_path` | never non-200 |
| `GET /api/vault/credentials` | masked list of every stored credential | routing + `has_password` only |
| `GET /api/vault/credentials/{host}` | one host's masked routing | `no_credential_for_host` (404) if none |
| `PUT /api/vault/credentials/{host}` | store routing + password | body: `url`, `username`, `password` (required) + optional selector overrides; returns masked view |
| `DELETE /api/vault/credentials/{host}?delete_secret=true` | remove routing (and, by default, the keyring secret) | idempotent |

The PUT body's optional fields mirror what `login.py` understands: `userField`,
`passField`, `submit`, `successUrl`, `keyringService`. The routing is written to
`~/.pinchtab-webgraph/login-config.json` atomically at mode `0600`; the password goes to
the OS keyring under the exact `(service, username)` pair `login.py` reads back at crawl
time. See [Authenticated login](authenticated-login.md) for how that credential is used.

### Chat sessions

The CRUD surface behind the [session chip bar](#chat-sessions) — multiple named chats per
host, persisted by `chat_store`. All five routes run the same host/id validation
choke-point as the vault routes, so a bad `host` or `session_id` token is rejected before
any filesystem access.

| Method · Path | Does | Notes |
| --- | --- | --- |
| `GET /api/hosts/{host}/sessions?mode=` | list a host's chats | `{"sessions":[…]}`, lightweight **summaries** (no transcript / wire state), newest `updated_at` first. The optional **`mode=`** filter (`workspace` \| `flow`) is what keeps the two chip bars separate — the Workspace tab lists `workspace` chats and the [Flows tab](#the-flow-agent-and-the-mode-axis) lists `flow` chats, out of the same store |
| `POST /api/hosts/{host}/sessions` | create a chat | optional `{"title", "mode"}` body; returns the new summary. `mode` (`workspace` \| `flow`, **unknown fails closed to `workspace`**) is written **once** here and [pinned](#the-flow-agent-and-the-mode-axis) on every resume. **429 `too_many_sessions`** at `MAX_SESSIONS_PER_HOST` (50) |
| `GET /api/hosts/{host}/sessions/{id}` | one chat's full record | includes `transcript`, **minus** the resume-only internals (`wire_messages`, `sdk_session_id`). `session_not_found` (**404**) if absent |
| `PATCH /api/hosts/{host}/sessions/{id}` | rename | body `{"title"}`; locks the title. `session_not_found` (**404**) if absent |
| `DELETE /api/hosts/{host}/sessions/{id}` | delete | **idempotent** — deleting an absent chat is a green `{"deleted": false}`, never a 404 |

A bad `session_id` token (not a uuid4 hex) is `invalid_session` (**400**); a bad `host`
token is `invalid_host` (**400**).

### HTTP status contract

Only three resolver statuses map to a non-200 code; every structured **miss**
(`no_match`, `unreachable`, `empty`, `no_path`, `invalid_args` on the read surface, …) is
a valid **200** answer with the `status` carried in the body — the same contract the
CLI/MCP surface uses.

| Status | HTTP code |
| --- | --- |
| `invalid_host` | `400` |
| `invalid_session` | `400` |
| `no_cache_for_host` | `404` |
| `no_credential_for_host` | `404` |
| `session_not_found` | `404` |
| `invalid_graph` | `422` |
| `too_many_sessions` | `429` |
| `vault_unavailable` | `503` |
| any structured miss (`no_match`, `unreachable`, `empty`, `no_path`, …) | `200` |

`invalid_args` is deliberately overloaded: a **200** structured miss on the read surface
(e.g. `howto` with no goal/match), but a **400** on a vault `PUT` with a bad body.

## WebSockets

### `GET /ws/chat?host=<host>`

The chat agent: Claude wired to the pinchtab-webgraph MCP server (a stdio subprocess) as
tools. It exposes **only the OFFLINE, read-only** tools — `graph_summary`, `howto`,
`find_content`, `list_content`, `list_forms`, `link_paths`. The live `crawl` / `ask_howto`
tools are deliberately withheld, so a chat message can never launch a crawl or drive a
browser.

The route dispatches through the selected [chat backend](#chat-backends): the **`api`**
backend (Anthropic API, `ANTHROPIC_API_KEY`, model default `claude-opus-4-8` via
`PINCHTAB_UI_MODEL`) or the **`claude_code`** backend (your local, logged-in Claude Code
via the Claude Agent SDK — no key, model via `PINCHTAB_UI_CLAUDE_CODE_MODEL`). Both stream
the same frames below and enforce the same offline-only tool lockdown.

The optional **`session=<id>`** query param binds the socket to a persisted
[chat session](#chat-sessions). When present the server resolves that record first and
replays it via a leading `session` bootstrap frame (below); a bad id token closes the
socket with `invalid_session`, and an id that doesn't resolve (or belongs to another host)
closes it with `session_not_found`. When absent, the connection uses/mints the host's chat
without restoring a prior transcript.

The optional **`mode=`** query param (`workspace` | `flow`) selects the agent's job — the
navigation assistant, or the [flow author](#the-flow-agent-and-the-mode-axis). It applies to a
**brand-new** session only: a **resumed** session's mode is **pinned** from its own record, so a
workspace chat can never be re-opened with the `propose_flow` tool attached. An **unknown** value
fails closed to `workspace`.

**Client → server:**

| Frame | Meaning |
| --- | --- |
| `{"type":"user_message","text":<str>,"live_url":<str\|null>,"draft":<dict\|null>}` | a user turn. The optional `live_url` is the live pane's current page (tracked by the SPA from screencast `location` frames); when present it is folded into the turn so the agent calls `howto` with `start=<live_url>` and routes **from where the user is**, not the crawl root (see [Live-position awareness](#live-position-awareness)). The optional `draft` (**`mode=flow`** only) is the **live flow document** on the user's screen, folded into the turn by `chat.augment_with_flow_draft` so the agent edits **your** current doc — hand edits between turns survive. Any other `type` is ignored |

**Server → client:**

| Frame | Meaning |
| --- | --- |
| `{"type":"session",…summary,"transcript":[…]}` | the **leading** bootstrap frame, sent once on connect ahead of any turn: the [session](#chat-sessions) summary (`id`/`host`/`backend`/`title`/`created_at`/`updated_at`/`message_count`) + its `transcript`, which the SPA replays to restore the log. Empty transcript for a new chat |
| `{"type":"text","delta":<str>}` | a streamed text delta of the reply |
| `{"type":"tool_use","name":<str>,"input":<dict>}` | the agent is about to call a tool |
| `{"type":"tool_result","name":<str>,"status":"ok"\|"error"}` | a tool returned |
| `{"type":"tour","data":{"goal","start_url","trigger_label","opens_at","form","steps":[…]}}` | a **"Show Me How"** guided tour — emitted once after an OK `howto` tool result (from its FIRST result's `tour` field). `steps` is the ordered highlight list; the SPA replays it on the live pane. See [Show Me How guided tour](#show-me-how-guided-tour). |
| `{"type":"flow_draft","doc":{…},"status":"ok"\|"invalid","path","error","name","note"}` | **`mode=flow`** only — the exact **twin of `tour`**, emitted after a [`propose_flow`](flows.md#propose_flow-and-the-flow_draft-frame) tool result: the WHOLE candidate document plus the validator's verdict. The SPA re-renders its **canvas + JSON pane** from it, so the draft lands **live** mid-conversation. An **invalid** draft is still emitted and still rendered (with `path`/`error`, and the offending canvas box lit up) — a withheld bad draft teaches the user nothing. Persisted into the transcript, so reopening a flow chat restores its drafts (as **chips**, never auto-applied — see [the mode axis](#the-flow-agent-and-the-mode-axis)) |
| `{"type":"done"}` | end of the turn (exactly once) |
| `{"type":"error","detail":<str>}` | a per-turn error (e.g. tool-iteration limit) |
| `{"type":"error","status":"invalid_host",…}` | bad host token; the socket then closes |
| `{"type":"error","status":"invalid_session"}` / `{"…":"session_not_found","session":…}` | the `session=` id was malformed, or didn't resolve for this host; the socket then closes. See [Chat sessions](#chat-sessions) |
| `{"type":"error","status":"chat_unavailable","reason":…,"detail":…}` | key / dep / CLI missing — `api` backend: `no_api_key` / `no_anthropic_package` / `no_mcp_package`; `claude_code` backend: `no_claude_cli` / `no_claude_code_package` / `claude_code_startup_error` |

### `GET /ws/screencast?host=<host>`

The live browser pane (Phase 4): for the selected host the server launches a **private
headless Chrome** (`--remote-debugging-port`, loopback-only — never
`--remote-debugging-address`), navigates it to the host's home page, best-effort logs it
in via `login.py` + the vault **only if** a bridge is configured via
`PINCHTAB_WEBGRAPH_BRIDGE`, then relays Chrome's `Page.startScreencast` frames (base64
JPEG) out to an `<img>`. The socket is **bidirectional**: the client also sends `input`
frames (mouse / wheel / keyboard) and `locate` probes for the guided tour, which a single
`CdpDispatcher` turns into CDP commands on the one shared CDP socket (demuxing id-bearing
command replies from method-bearing screencast events). `MAX_LIVE_SESSIONS` (3) caps
concurrency.

**Client → server:**

| Frame | Meaning |
| --- | --- |
| `{"type":"input","kind":<str>,…}` | one input event, coords already in viewport CSS pixels; mapped to a CDP `Input.*` command via a tight allow-list (`kind`: `mousemoved` / `mousepressed` / `mousereleased` / `wheel` / `text` / `keydown` / `keyup`). Fire-and-forget; an unknown `kind` is a silent no-op |
| `{"type":"locate","stepId":<int>,"selector":<str\|null>,"label":<str>}` | resolve a tour step's element in the live page; the server replies with a `located` frame. Used by [Show Me How](#show-me-how-guided-tour) |

**Server → client:**

| Frame | Meaning |
| --- | --- |
| `{"type":"status","state":"live","authenticated":<bool\|None>,"reason":<str\|None>}` | session is up; whether best-effort login succeeded |
| `{"type":"status","state":"live","width":…,"height":…}` | screencast started (dimensions arrive with the first frame) |
| `{"type":"frame","data":<base64 str>,"metadata":<dict>}` | one screencast frame |
| `{"type":"location","url":<str>}` | the live page navigated (top frame only) — the SPA tracks it as the current position and sends it as `live_url` on the next chat message. See [Live-position awareness](#live-position-awareness) |
| `{"type":"located","stepId":<int>,"rect":{x,y,width,height}\|null}` | reply to a `locate`: the resolved element's rect in viewport CSS pixels, or `null` when not found (the overlay then falls back to "click it yourself" and keeps Next enabled). Best-effort — never an error |
| `{"type":"stopped"}` | the CDP stream ended |
| `{"type":"error","status":"invalid_host",…}` | bad host token; the socket closes |
| `{"type":"error","status":"too_many_sessions","max":3}` | at the `MAX_LIVE_SESSIONS` cap |
| `{"type":"error","status":"screencast_unavailable","reason":…,"detail":…}` | dep/binary missing or CDP dead (`no_websockets_package` / `no_chrome_binary` / `chrome_launch_failed` / `cdp_unreachable` / `no_page_target`) |

The CDP URL, the debugging port, and the bridge URL/token **never** appear in any frame —
only frame/status/error dicts leave the relay loop.

### Show Me How guided tour

When the chat answers a "how do I get to X" question, its reply carries a **"Show me How"**
button (from the `tour` frame above). Clicking it starts an onboarding overlay **on the
live browser pane**: step `1..n`, each highlighting exactly where to click on the live
preview, with **Prev / Next / Done**. **Next performs the real click** — it drives the live
browser forward via the existing `input` channel and moves the highlight to the next
target — until the user lands on the target form. The tour **stops there: it never
auto-submits the form**. That guarantee is structural — the terminal `form` step (and the
`trigger` step) carries no selector, so there is nothing for Next to click past the form.

Each step is resolved with a `locate` → `located` round-trip: the SPA sends the step's
`selector` + `label`, and the server resolves the element in the live headless Chrome via a
single CDP `Runtime.evaluate` — CSS selector first, then a case-insensitive text match of
`label` against a fixed interactive allow-list (`a, button, [role=button], [role=link],
[role=menuitem]`) — and replies with the element's `rect` in viewport CSS pixels (or `null`
when not found, which drops the step to a "click it yourself" fallback with Next still
enabled). The SPA's `rectToDisplay()` (the inverse of the pane's `liveCoords()`
`object-fit: contain` math) maps that rect to a highlight box over the displayed `<img>`.

The steps come from the `tour` field now on every `api.howto()` result (and the REST
`/api/hosts/{host}/howto` response): an ordered list derived from the same offline routing
edges as the text `steps` — zero+ `nav` steps (`{"kind":"nav","label","selector","href"}`,
one per routing edge) → one `trigger` step (`{"kind":"trigger","label","selector":null,
"href":null}`, the trigger's own click, resolved by label) → a terminal `{"kind":"form"}`
marker. It is purely **additive**: the existing `steps` / `form` / `opens_at` fields are
unchanged.

The locate expression is a **fixed, injection-safe JS template**
(`screencast.build_locate_expression`): `selector` and `label` are embedded only as
`json.dumps()`-escaped, length-capped string literals — never as executable code — so there
is no arbitrary client eval, and CDP stays loopback-only.

### Live-position awareness

The chat agent knows **where the live browser currently is**, so it routes from your
current page instead of always from the crawl root:

1. As the live pane's headless Chrome navigates (you click around, or a tour advances) its
   position is relayed out as `{"type":"location","url":…}` frames, main frame only
   (subframe/iframe navigations are ignored). BOTH navigation kinds are tracked:
   - hard loads / full page navigations → CDP `Page.frameNavigated` (`screencast.top_frame_url`);
   - **SPA soft navigations** (a React/Vue app switching tabs via `history.pushState` /
     `replaceState` / hash changes, which do NOT fire `frameNavigated`) → CDP
     `Page.navigatedWithinDocument` (`screencast.navigated_within_document_url`), scoped to
     the main frame via the id learned from the first hard load. Without this the tracked
     position would go stale the moment you switched tabs inside a single-page app.
2. The SPA stores the latest as `currentLiveUrl` (reset on host switch) and sends it as
   `live_url` on the next `user_message`.
3. The server threads `live_url` into the turn via `chat.augment_with_location` (shared by
   **both** chat backends): the message is prefixed with the current URL and an instruction
   to call `howto` with `start=<live_url>`, so the click-path and the **Show Me How** tour
   begin from where you are.

It is fully additive and degrades cleanly: when `live_url` is absent (the client didn't send
one, or the pane hasn't navigated yet) the agent routes from the crawl's default start, exactly
as before. The live URL is untrusted site data, but it only ever travels to the model as text —
it is never rendered as HTML.

### New crawl (`GET /ws/crawl`, opt-in)

`GET /ws/crawl?url=<url>&max_states=&max_depth=` — the one **WRITE** socket in the UI:
crawl a brand-new URL and store the result, all from
the sidebar — no CLI, no shell. The server spawns
`python -m pinchtab_webgraph.interaction_crawl` as a subprocess, streams its progress out
frame-by-frame, and — when it finishes (or is cancelled) — **atomically promotes** the
resulting interaction graph into the cache dir, so the new host appears in the sidebar and
is immediately usable by the [Graph view](#graph-view) and the [chat](#get-wschathosthost).

**Off by default.** This is the biggest capability in the UI: it makes the server drive a
**real browser** through the *entire* target app and open **every** "Create" form (to read
its fields — it **never submits**). So the route is gated behind an opt-in env var and
refuses with a structured frame unless it is set:

```bash
PINCHTAB_WEBGRAPH_ENABLE_CRAWL=1 pinchtab-webgraph-ui   # 1 / true / yes / on
```

Its target PinchTab bridge is its **own** env var, `PINCHTAB_WEBGRAPH_CRAWL_SERVER`
(default `http://localhost:9871`) — deliberately distinct from the [screencast pane's
`PINCHTAB_WEBGRAPH_BRIDGE`](#get-wsscreencasthosthost), so a crawl and the live pane can point
at different physical bridges. The crawler self-loads its bridge **token** from the config
at `$PINCHTAB_CONFIG`. Concurrency is capped at **one** crawl at a time
(`MAX_LIVE_CRAWLS`); a second attempt gets a `too_many_sessions` frame.

**The sidebar form.** A **"New crawl"** form sits above the host list:

- a `url` input (validated `http`/`https` + a hostname the cache guard accepts);
- an **Advanced (crawl limits)** disclosure with `max states` (clamped to **[10, 500]**,
  default **60**) and `max depth` (clamped to **[1, 8]**, default **4**);
- a **New crawl** submit + a **Cancel** button (shown only while a crawl runs);
- a permanent **safety note** — *"The crawler clicks through every page and opens every
  Create form to read it — it never submits anything."*;
- a live **progress log** that streams `status` / `progress` / `log` lines and the terminal
  result. Every server-sourced string is rendered with `textContent` (never `innerHTML`).

On a terminal `done` (or a `cancelled` that promoted a partial graph) the SPA re-fetches
`/api/hosts`, then **auto-selects** the freshly-stored host so its Graph view + chat open
immediately.

**Client → server:**

| Frame | Meaning |
| --- | --- |
| `{"type":"cancel"}` | request cancellation; the server SIGTERM→SIGKILLs the crawler's process group and still promotes whatever partial graph was written. Any other frame — or a socket disconnect — is treated the same as a cancel |

**Server → client:**

| Frame | Meaning |
| --- | --- |
| `{"type":"status","state":"starting","host":<str>,"start_url":<str>}` | the subprocess launched |
| `{"type":"progress","states":<int>,"visits":<int>,"depth":<int>,"url":<str>,"controls":<int>}` | one visited-state progress tick, parsed from the crawler's stderr |
| `{"type":"log","line":<str>}` | any non-progress crawler line (banner, `✓ trigger …`, warnings, the final `Wrote …`), truncated to 500 chars |
| `{"type":"done","host":<str>,"states":<int>,"edges":<int>,"triggers":<int>,"complete":<bool>,"stopped":<str>}` | the crawl finished and its graph was promoted. `complete` / `stopped` come from the **written graph's `meta`**, never the return code |
| `{"type":"cancelled","host":<str>,"promoted":<bool>,"states":…,"edges":…,"triggers":…,"complete":…,"stopped":…}` | cancelled / disconnected. `promoted` says whether a partial graph was stored; the counts are `null` when nothing was written |
| `{"type":"error","status":"invalid_url","url":<str>,"detail":…}` | bad scheme / no hostname / rejected host token; the socket closes |
| `{"type":"error","status":"crawl_unavailable","reason":…,"detail":…}` | the feature can't start — `reason` is `disabled` (the opt-in var isn't set), `no_config` (`$PINCHTAB_CONFIG` unset/missing), or `bridge_unreachable` (the crawl bridge didn't answer); the socket closes |
| `{"type":"error","status":"too_many_sessions","max":1}` | already at the `MAX_LIVE_CRAWLS` cap; the socket closes |
| `{"type":"error","status":"crawl_failed","host":<str>,"detail":…}` | the crawl produced no output graph, or its staging file was corrupt/partial (promotion raised) |

**Storage guarantee — temp-staging → atomic promote.** The crawler writes into a private
staging dir created **inside** `$PINCHTAB_WEBGRAPH_HOME`, and only a *promotable* result
(a graph whose `meta` loads) is `os.replace`d onto `cache_store.cache_path(host)` — an
**atomic, same-filesystem move**. A failed, empty, or corrupt crawl therefore **never
clobbers an existing good cache**; the staging dir is always torn down in a `finally`, so no
orphan process and no leaked temp files. Because a cancel still writes the crawler's partial
graph first (its own SIGTERM handler), a cancelled crawl can still promote what it captured.

## Environment variables

| Var | Effect |
| --- | --- |
| `PINCHTAB_WEBGRAPH_HOME` | Root of the cache + config dir (`$HOME/.pinchtab-webgraph` by default). The vault's `login-config.json` lives here too. |
| `ANTHROPIC_API_KEY` | Enables the `api` chat backend. When set it also *selects* the `api` backend by default. Absent → the UI falls back to the `claude_code` backend if a `claude` CLI is on `PATH`, else shows a `chat_unavailable` frame; the rest of the UI still works. |
| `PINCHTAB_UI_MODEL` | Override the `api`-backend chat model (default `claude-opus-4-8`). |
| `PINCHTAB_UI_CHAT_BACKEND` | Force the chat backend: `api` or `claude_code` (an explicit override wins over the auto-selection). See [Chat backends](#chat-backends). |
| `PINCHTAB_UI_CLAUDE_CODE_MODEL` | Override the `claude_code`-backend model. Default: your account's Claude Code default (deliberately *not* the API `claude-opus-4-8` alias). |
| `PINCHTAB_WEBGRAPH_BRIDGE` | PinchTab bridge URL for the live pane's best-effort automated login. Absent → the live pane still runs, just unauthenticated. |
| `PINCHTAB_WEBGRAPH_ENABLE_CRAWL` | **Opt-in gate** for the [New crawl](#new-crawl-get-wscrawl-opt-in) socket (`/ws/crawl`). Unset → **off**: `/ws/crawl` refuses with a `crawl_unavailable`/`disabled` frame. Set to a truthy value (`1` / `true` / `yes` / `on`) to allow crawling a URL from the UI. |
| `PINCHTAB_WEBGRAPH_CRAWL_SERVER` | PinchTab bridge URL the [New crawl](#new-crawl-get-wscrawl-opt-in) **and** the [flow-run](#flows-view-opt-in) subprocesses drive (default `http://localhost:9871`). Deliberately **distinct** from `PINCHTAB_WEBGRAPH_BRIDGE` so a crawl and the live pane can target different bridges. Both subprocesses self-load the bridge **token** from `$PINCHTAB_CONFIG`. A crawl and a flow run read the **same** var because they drive the **same** bridge — which is exactly why they [veto each other](#flows-view-opt-in). |
| `PINCHTAB_WEBGRAPH_ENABLE_FLOWS` | **Opt-in gate** for **running** a flow ([`/ws/flows/run`](#flows-view-opt-in)). Unset → **off**: the run socket refuses with a `flow_unavailable`/`disabled` frame, while the editor + the flow CRUD routes keep working. Truthy (`1` / `true` / `yes` / `on`) allows a saved flow to drive a real browser — and, if the document declares it *and* you grant it, to **submit / upload**. Strictly more dangerous than `PINCHTAB_WEBGRAPH_ENABLE_CRAWL`. |
| `PINCHTAB_CONFIG` | The bridge config file a [flow run](#flows-view-opt-in) / [crawl](#new-crawl-get-wscrawl-opt-in) subprocess reads its bridge **token** from (default `crawl-config.json`, gitignored — never commit it). A **live** flow run with it unset/missing fails fast (`flow_unavailable` / `no_config`); a **dry** run needs neither the config nor a reachable bridge, because `--dry-run` touches nothing. A live flow run also **preflights** the bridge and refuses with `bridge_unreachable` rather than spawning a process against nothing. |

## Security model

- **Loopback-only by default.** The vault write endpoints, the chat agent, and the live
  browser pane are unauthenticated. Bind `127.0.0.1` (the default) and reach the UI from
  the same machine. Binding elsewhere prints a warning — heed it.
- **CDP never leaves loopback.** The headless-Chrome DevTools endpoint is bound to
  `127.0.0.1` by construction: the launcher never adds `--remote-debugging-address` (the
  only flag that could expose CDP off-loopback), and a free loopback port is chosen per
  session. Chrome runs in its own process group and its temp profile is always torn down.
- **Passwords are write-only and keyring-backed.** The vault stores per-host **routing**
  in `login-config.json` (atomic, `0600`) and the **password in the OS keyring only** —
  never on disk, never in a response, log line, or exception. Every read surface returns a
  `has_password` boolean, not the secret. Note that keyring is **at-rest** hygiene only:
  any process running as your user can read a keyring secret. See the
  [authenticated-login threat model](authenticated-login.md#threat-model--read-this-before-trusting-keyring)
  for the full picture and the sandbox posture that actually confines the automation.
- **Chat is offline-tools-only — on both backends.** Whichever [chat backend](#chat-backends)
  runs, the agent is given only the six read-only offline MCP tools; `crawl` and `ask_howto`
  (which would drive a live browser) are withheld, so no chat message has side effects beyond
  reading the already-cached graph. The `claude_code` backend spawns a real `claude`
  subprocess that *could* otherwise run Bash/Write on the host, so it is fenced harder still:
  all built-in tools are removed (`tools=[]`), no `~/.claude` / `CLAUDE.md` / project settings
  are loaded, and a deny-by-default `can_use_tool` backstop rejects anything off the
  allow-list.
- **The flow agent can only PROPOSE — it can neither save nor run.** [`mode=flow`](#the-flow-agent-and-the-mode-axis)
  **adds exactly one** tool to that six-tool fence — `propose_flow` — and never widens it. That
  tool is a **pure validate-and-echo**: no disk write, no browser, no subprocess (a test poisons
  `open` / `os.replace` / `subprocess` to prove it). And **no tool anywhere on the MCP surface
  reaches `flow_store` (save) or `flow_runner` (run)** — also guarded by a test — so the human's
  **Save** and **Run** buttons remain the only authority, and the [capability
  model](flows.md#the-capability--safety-model) still applies to everything the agent drafts.
  `effective_tool_names()` **fails closed** on an unknown mode, and a session's mode is **pinned**
  from its record, so a workspace chat can't be escalated via `?mode=flow`.
- **Crawl-from-UI is the strongest capability — and the most fenced.** The
  [New crawl](#new-crawl-get-wscrawl-opt-in) socket (`/ws/crawl`) can
  make the server drive a **real browser** through the whole target app and open every
  Create form (it never submits). It is guarded four ways: **opt-in** — off unless
  `PINCHTAB_WEBGRAPH_ENABLE_CRAWL` is truthy; **loopback-only** — same unauthenticated,
  `127.0.0.1`-by-default posture as the rest of the UI (and it makes the non-loopback
  warning louder); **no shell / no argv injection** — the user URL is passed as an inert
  argv element to `create_subprocess_exec` (never a shell string), so a hostile URL can't
  become an executable statement; and **host-validated** — the start URL must be `http`/`https`
  with a hostname the `cache_store.validate_host` choke-point accepts, so a crawl can never
  resolve or write outside `caches_dir()`. Concurrency is capped at one, and the result is
  promoted by an **atomic same-filesystem move** so a failed crawl can't corrupt a good cache.
- **Running a flow is the only capability that can WRITE to the target site — so it is gated
  hardest.** A [flow](#flows-view-opt-in) can `do{submit: true}` or `upload` a file; a crawl
  structurally never submits. Five guards: **opt-in** — `/ws/flows/run` is dead unless
  `PINCHTAB_WEBGRAPH_ENABLE_FLOWS` is truthy; **declared AND granted** — a write runs only if
  the *document* declares the capability *and* the *caller* grants it, ANDed by the server and
  re-checked per step by the runner (either side vetoes); **dry-run by default** in the UI;
  **no shell / no argv injection** — inputs and paths are inert argv elements passed to
  `create_subprocess_exec`, never a shell string; and **loopback-only**, like the rest of the
  UI. The run is a **subprocess in its own process group**, so Cancel (and a client disconnect,
  which is an implicit cancel) can actually stop it, and a wedged browser step can't take the
  server down. Concurrency is 1, and a flow run and a crawl refuse each other.
- **Per-host path quarantine (defense-in-depth).** The cache, the
  [chat-session store](#chat-sessions) and the [flow store](#flows-view-opt-in) route every `host` through the shared
  `cache_store.validate_host` choke-point before touching the filesystem. That guard was
  hardened to also **reject all-dots tokens** (`"."`, `".."`, …): the host regex accepted
  them, harmless for `cache_path` (which appends `.json`) but not for `chat_store`, which
  uses the bare host as a **directory segment** — so `/ws/chat?host=..` would otherwise
  escape the per-host quarantine up into the home dir. Now rejected at the single choke
  point (with a regression test).
- **No endpoint authentication.** There is deliberately none — the design assumes a
  localhost-only deployment. Do not put this behind a public reverse proxy without adding
  your own auth in front.

## Notes & limitations

- **Chat needs a key *or* a local Claude Code.** With an `ANTHROPIC_API_KEY` the `api`
  backend runs; with no key but a logged-in `claude` CLI (+ the `[ui-claude-code]` extra)
  the `claude_code` backend runs with no key at all; with neither, the chat pane reports
  `chat_unavailable` and everything else keeps working. See [Chat backends](#chat-backends).
- **Live login needs a running bridge.** Without `PINCHTAB_WEBGRAPH_BRIDGE` (or a stored
  credential) the live pane still opens, just unauthenticated. Login is best-effort and
  never load-bearing: any failure degrades silently to an unauthenticated session.
- **The live pane is interactive.** Client→server input (mouse / wheel / keyboard) is
  wired, and the [Show Me How guided tour](#show-me-how-guided-tour) drives real clicks over
  the live page. Resize is still deferred.
- **One Chrome + one MCP subprocess per connection.** Each live-pane socket launches a
  private headless Chrome; each chat socket spawns a pinchtab-webgraph MCP stdio
  subprocess. `MAX_LIVE_SESSIONS` (3) caps concurrent Chrome instances.
- **Chats are persistent and multiple.** Each host owns a set of named chats saved to
  `<home>/sessions/<host>/<id>.json`, switched from the chat pane's chip bar and restored on
  reconnect. The `api` backend fully continues a reopened chat; the `claude_code` backend
  restores the transcript for **display only** in v1 (with a UI badge). See
  [Chat sessions](#chat-sessions).
- **Flows are opt-in and single-tenant.** Running a saved [flow](#flows-view-opt-in) needs
  `PINCHTAB_WEBGRAPH_ENABLE_FLOWS`; one run at a time, and a run and a crawl refuse each other
  (same bridge, same tab). **Authoring** — including the [AI
  agent](flows.md#authoring-a-flow-with-the-ai-agent) — validating, and browsing runs/artifacts
  all work with the gate **off**; only *running* is gated. The dedupe scope in the UI is the flow's
  **id**, which is *not* what the CLI defaults to — see the [artifact-scope
  caveat](flows.md#caveat-the-artifact-scope-diverges-between-cli-and-ui).
- **The flow agent needs a chat backend, like any other chat.** With neither an
  `ANTHROPIC_API_KEY` nor a logged-in `claude` CLI, the Flows tab's chat pane reports
  `chat_unavailable` and the canvas + JSON editor keep working — you can still author by hand.
- **Chat replies render as markdown.** The SPA renders the assistant's reply — bold,
  italics, headings, ordered/unordered lists, inline + block code, links, and GitHub-style
  **tables** — through a small HTML-escape-first renderer, so no model or crawled-site text
  can inject HTML.

## Troubleshooting

- **The chat replies with raw `<function_calls …>` or `Tool call: …` text and never lists
  results.** The model has **no tools** and is narrating the call. The `claude_code`
  backend's tools come from a `pinchtab-webgraph` MCP server it spawns as `python -m
  pinchtab_webgraph.mcp_server` from an isolated temp dir, so the package must be importable
  from *anywhere* — not just the repo. The usual cause is a **stale editable install** whose
  target was moved or pruned (e.g. a deleted git worktree): the subprocess then crashes with
  `ModuleNotFoundError` and the tools never load. Verify and fix:

  ```bash
  cd /tmp && python -c "import pinchtab_webgraph"   # must NOT raise
  pip install -e .                                  # re-point the editable install at this checkout
  ```

  Then **reload the chat page** — a fresh page spins up a new MCP subprocess. (The server
  also pins `PYTHONPATH` for the subprocess as a backstop, but a healthy import is the real
  fix.)
- **The chat pane says `chat_unavailable`.** No backend is configured — set an
  `ANTHROPIC_API_KEY` (the `api` backend) **or** install `[ui-claude-code]` and log in to the
  `claude` CLI (the `claude_code` backend, no key). See [Chat backends](#chat-backends).
- **The live browser pane stays blank.** No Chrome/Chromium on `PATH` — install one of
  `google-chrome`, `google-chrome-stable`, `chromium`, `chromium-browser`.
- **The Flows tab is there but Run does nothing / says `flow_unavailable`.** Read the frame's
  `reason`: `disabled` → `PINCHTAB_WEBGRAPH_ENABLE_FLOWS` isn't set (see below); `no_config` →
  `$PINCHTAB_CONFIG` doesn't point at a readable `crawl-config.json`; `bridge_unreachable` → no
  PinchTab bridge is listening (start one, e.g. `scripts/start-crawl-browser.sh`). A **dry run**
  needs none of the three and is the fastest way to confirm the rest of the tab works.

### Operational notes (developing / e2e-testing the UI)

Hard-won, and each one cost real time:

- **Launching the UI with every capability on** (flows *and* crawl), through
  [portless](https://www.npmjs.com/package/portless):

  ```bash
  nvm use 24                                     # portless needs node 24+
  PINCHTAB_WEBGRAPH_ENABLE_FLOWS=1 PINCHTAB_WEBGRAPH_ENABLE_CRAWL=1 \
    portless run --name webgraph -- python3 -m pinchtab_webgraph.ui.server
  #   -> https://webgraph.localhost   (in a git WORKTREE the branch is prefixed:
  #      https://<branch>.webgraph.localhost — read the real URL from `portless list`)
  ```

  **In a git worktree, only `portless run` is correct.** The flat form
  (`portless <name> <cmd>`) does **not** apply the worktree prefix and collides with the main
  checkout's route.
- **To drive the UI *itself* with PinchTab (e2e), use the RAW loopback URL, not the portless
  one.** The PinchTab bridge's IDPI allowlist admits `127.0.0.1` / `localhost` but **not a
  `*.localhost` subdomain**, so `https://webgraph.localhost` is refused. Point the automation at
  `http://127.0.0.1:<uvicorn-port>/` instead. (The portless HTTPS URL is for the *human's*
  browser.)
- **`pinchtab press Enter` does NOT activate a focused `<button>`.** Verified with a synthetic
  probe: **0 click hits**. Use `pinchtab type <sel> $'\n'` instead. This is a pinchtab
  limitation, and it will silently no-op your UI e2e test otherwise.
