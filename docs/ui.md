# Web UI

> **Docs:** [← README](../README.md) · [📚 Index](README.md) · [MCP server](mcp-server.md) · [UTCP interface](utcp.md) · [Authenticated login](authenticated-login.md)

`pinchtab-webgraph` ships an **optional** local web UI: a small FastAPI app that serves
a read-only REST API over the offline graph caches, a credentials vault, a live chat
agent (Claude wired to the project's own MCP tools), and a live headless-browser pane —
behind a single console script, `pinchtab-webgraph-ui`.

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
  Claude with the project's **offline** MCP tools and streams the reply as it lands.
- **Live browser pane** (right) — a private headless Chrome, navigated to the host's home
  page, streamed frame-by-frame into an `<img>` via a CDP screencast.
- **Credentials modal** — the vault: store per-host login routing + a password (password
  goes to the OS keyring, never to disk) so the live pane can best-effort log itself in.

Every capability degrades independently: with no chat backend configured the chat pane
shows a `chat_unavailable` notice while the REST API and graph browsing keep working; with
no Chrome binary the live pane shows `screencast_unavailable`; with no keyring backend the
vault reports `vault_unavailable`.

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
| `--port` | `8765` | bind port |
| `--open` | off | open the UI in a browser once the server is listening |

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
| `GET /api/hosts/{host}/graph` | `cache_store.load` | the full raw interaction graph (the large payload, on demand) |
| `GET /api/hosts/{host}/forms` | `api.list_forms` | every create-form: label, host, depth, field count |
| `GET /api/hosts/{host}/howto?goal=&start=&match=&all=` | `api.howto` | shortest click-path(s) to a create-trigger + its form; each result also carries an additive `tour` field (the [Show Me How](#show-me-how-guided-tour) highlight steps) |
| `GET /api/hosts/{host}/content` | `api.list_content` | per-view inventory of captured collections |
| `GET /api/hosts/{host}/content/search?text=&start=&limit=` | `api.find_content` | search captured collections for text; `text` required, `limit` default 40 |

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

### HTTP status contract

Only three resolver statuses map to a non-200 code; every structured **miss**
(`no_match`, `unreachable`, `empty`, `no_path`, `invalid_args` on the read surface, …) is
a valid **200** answer with the `status` carried in the body — the same contract the
CLI/MCP surface uses.

| Status | HTTP code |
| --- | --- |
| `invalid_host` | `400` |
| `no_cache_for_host` | `404` |
| `no_credential_for_host` | `404` |
| `invalid_graph` | `422` |
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

**Client → server:**

| Frame | Meaning |
| --- | --- |
| `{"type":"user_message","text":<str>,"live_url":<str\|null>}` | a user turn; the optional `live_url` is the live pane's current page (tracked by the SPA from screencast `location` frames). When present it is folded into the turn so the agent calls `howto` with `start=<live_url>` and routes **from where the user is**, not the crawl root. See [Live-position awareness](#live-position-awareness). Any other `type` is ignored |

**Server → client:**

| Frame | Meaning |
| --- | --- |
| `{"type":"text","delta":<str>}` | a streamed text delta of the reply |
| `{"type":"tool_use","name":<str>,"input":<dict>}` | the agent is about to call a tool |
| `{"type":"tool_result","name":<str>,"status":"ok"\|"error"}` | a tool returned |
| `{"type":"tour","data":{"goal","start_url","trigger_label","opens_at","form","steps":[…]}}` | a **"Show Me How"** guided tour — emitted once after an OK `howto` tool result (from its FIRST result's `tour` field). `steps` is the ordered highlight list; the SPA replays it on the live pane. See [Show Me How guided tour](#show-me-how-guided-tour). |
| `{"type":"done"}` | end of the turn (exactly once) |
| `{"type":"error","detail":<str>}` | a per-turn error (e.g. tool-iteration limit) |
| `{"type":"error","status":"invalid_host",…}` | bad host token; the socket then closes |
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

## Environment variables

| Var | Effect |
| --- | --- |
| `PINCHTAB_WEBGRAPH_HOME` | Root of the cache + config dir (`$HOME/.pinchtab-webgraph` by default). The vault's `login-config.json` lives here too. |
| `ANTHROPIC_API_KEY` | Enables the `api` chat backend. When set it also *selects* the `api` backend by default. Absent → the UI falls back to the `claude_code` backend if a `claude` CLI is on `PATH`, else shows a `chat_unavailable` frame; the rest of the UI still works. |
| `PINCHTAB_UI_MODEL` | Override the `api`-backend chat model (default `claude-opus-4-8`). |
| `PINCHTAB_UI_CHAT_BACKEND` | Force the chat backend: `api` or `claude_code` (an explicit override wins over the auto-selection). See [Chat backends](#chat-backends). |
| `PINCHTAB_UI_CLAUDE_CODE_MODEL` | Override the `claude_code`-backend model. Default: your account's Claude Code default (deliberately *not* the API `claude-opus-4-8` alias). |
| `PINCHTAB_WEBGRAPH_BRIDGE` | PinchTab bridge URL for the live pane's best-effort automated login. Absent → the live pane still runs, just unauthenticated. |

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
  six-tool allow-list.
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
