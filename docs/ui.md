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

Every capability degrades independently: with no API key the chat pane shows a
`chat_unavailable` notice while the REST API and graph browsing keep working; with no
Chrome binary the live pane shows `screencast_unavailable`; with no keyring backend the
vault reports `vault_unavailable`.

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

Beyond the extra, each of the three live capabilities needs one more thing at runtime;
none is required to run the server or use the offline REST API:

| Capability | Additionally needs |
| --- | --- |
| **Offline REST API + graph browsing** | only a populated cache (nothing else) |
| **Chat pane** | an `ANTHROPIC_API_KEY` in the environment |
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
| `GET /api/hosts/{host}/howto?goal=&start=&match=&all=` | `api.howto` | shortest click-path(s) to a create-trigger + its form |
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

The chat agent (Phase 3): Claude (Anthropic API) wired to the pinchtab-webgraph MCP
server (a stdio subprocess) as tools. It exposes **only the OFFLINE, read-only** tools —
`graph_summary`, `howto`, `find_content`, `list_content`, `list_forms`, `link_paths`. The
live `crawl` / `ask_howto` tools are deliberately withheld, so a chat message can never
launch a crawl or drive a browser. Model default `claude-opus-4-8`, overridable via
`PINCHTAB_UI_MODEL`.

**Client → server:**

| Frame | Meaning |
| --- | --- |
| `{"type":"user_message","text":<str>}` | a user turn; any other `type` is ignored |

**Server → client:**

| Frame | Meaning |
| --- | --- |
| `{"type":"text","delta":<str>}` | a streamed text delta of the reply |
| `{"type":"tool_use","name":<str>,"input":<dict>}` | the agent is about to call a tool |
| `{"type":"tool_result","name":<str>,"status":"ok"\|"error"}` | a tool returned |
| `{"type":"done"}` | end of the turn (exactly once) |
| `{"type":"error","detail":<str>}` | a per-turn error (e.g. tool-iteration limit) |
| `{"type":"error","status":"invalid_host",…}` | bad host token; the socket then closes |
| `{"type":"error","status":"chat_unavailable","reason":…,"detail":…}` | no key / dep missing (`no_api_key` / `no_anthropic_package` / `no_mcp_package`); the socket closes |

### `GET /ws/screencast?host=<host>`

The live browser pane (Phase 4): for the selected host the server launches a **private
headless Chrome** (`--remote-debugging-port`, loopback-only — never
`--remote-debugging-address`), navigates it to the host's home page, best-effort logs it
in via `login.py` + the vault **only if** a bridge is configured via
`PINCHTAB_WEBGRAPH_BRIDGE`, then relays Chrome's `Page.startScreencast` frames (base64
JPEG) out to an `<img>`. This direction is **read-only**: Phase 4 does not wire
client→server input or resize (deferred). `MAX_LIVE_SESSIONS` (3) caps concurrency.

**Server → client:**

| Frame | Meaning |
| --- | --- |
| `{"type":"status","state":"live","authenticated":<bool\|None>,"reason":<str\|None>}` | session is up; whether best-effort login succeeded |
| `{"type":"status","state":"live","width":…,"height":…}` | screencast started (dimensions arrive with the first frame) |
| `{"type":"frame","data":<base64 str>,"metadata":<dict>}` | one screencast frame |
| `{"type":"stopped"}` | the CDP stream ended |
| `{"type":"error","status":"invalid_host",…}` | bad host token; the socket closes |
| `{"type":"error","status":"too_many_sessions","max":3}` | at the `MAX_LIVE_SESSIONS` cap |
| `{"type":"error","status":"screencast_unavailable","reason":…,"detail":…}` | dep/binary missing or CDP dead (`no_websockets_package` / `no_chrome_binary` / `chrome_launch_failed` / `cdp_unreachable` / `no_page_target`) |

The CDP URL, the debugging port, and the bridge URL/token **never** appear in any frame —
only frame/status/error dicts leave the relay loop.

## Environment variables

| Var | Effect |
| --- | --- |
| `PINCHTAB_WEBGRAPH_HOME` | Root of the cache + config dir (`$HOME/.pinchtab-webgraph` by default). The vault's `login-config.json` lives here too. |
| `ANTHROPIC_API_KEY` | Enables the chat pane. Absent → a `chat_unavailable` frame; the rest of the UI still works. |
| `PINCHTAB_UI_MODEL` | Override the chat model (default `claude-opus-4-8`). |
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
- **Chat is offline-tools-only.** The chat agent is given only the six read-only offline
  MCP tools; `crawl` and `ask_howto` (which would drive a live browser) are withheld, so
  no chat message has side effects beyond reading the already-cached graph.
- **No endpoint authentication.** There is deliberately none — the design assumes a
  localhost-only deployment. Do not put this behind a public reverse proxy without adding
  your own auth in front.

## Notes & limitations

- **Chat needs a key.** No `ANTHROPIC_API_KEY` → the chat pane reports `chat_unavailable`;
  everything else keeps working.
- **Live login needs a running bridge.** Without `PINCHTAB_WEBGRAPH_BRIDGE` (or a stored
  credential) the live pane still opens, just unauthenticated. Login is best-effort and
  never load-bearing: any failure degrades silently to an unauthenticated session.
- **The live pane is view-only for now.** Client→server input and resize are deferred
  (Phase 4 relays frames one way).
- **One Chrome + one MCP subprocess per connection.** Each live-pane socket launches a
  private headless Chrome; each chat socket spawns a pinchtab-webgraph MCP stdio
  subprocess. `MAX_LIVE_SESSIONS` (3) caps concurrent Chrome instances.
