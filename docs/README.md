# 📚 Documentation

Documentation for **[pinchtab-webgraph](../README.md)** — a deterministic toolkit that crawls any website into a navigation + content graph and answers how-to / where-is queries offline.

Start with the **[main README](../README.md)** for what the project is, install, the quickstart, and the tool inventory. The pages below go deeper on each topic.

## Guides

| Page | What it covers |
| --- | --- |
| **[Main README](../README.md)** | Overview, requirements, install, quickstart, the tools, [three ways to call it](../README.md#-three-ways-to-call-it), architecture, graph shape, safety model, roadmap. |
| **[Automation flows](flows.md)** | The declarative flow layer (`pwg flow run \| validate \| schema`): the [document format](flows.md#the-document-format) (every op — `goto`/`do`/`click`/`fill`/`download`/`collect` + the control-flow ops `for_each`/`paginate`), the [capability / safety model](flows.md#the-capability--safety-model), the [download strategy](flows.md#downloads-in-session-fetch-first-cli-fallback) and its constraints, the [dedupe ledger](flows.md#the-dedupe-ledger), an [authoring walkthrough](flows.md#authoring-a-flow--a-walkthrough), and the [gotchas](flows.md#gotchas). |
| **[`perform` live test](perform-live-test.md)** | The real-browser proof of `crawl → howto → perform`: the local test site, a downloads-enabled bridge, the SSRF caveat on `pinchtab download`, and the two bugs the live run caught. |
| **[MCP server](mcp-server.md)** | Run the Model Context Protocol server (`pinchtab-webgraph-mcp`): the `[mcp]` extra, `.mcp.json` registration, the tool + resource inventory, env vars, and the safety model for the live tools. |
| **[UTCP interface](utcp.md)** | The Universal Tool Calling Protocol manual: `pwg query` (JSON) + `pwg manual` / `--serve`, the 8 exposed tools, the deliberate scope subset, and the `query` exit-code convention. |
| **[Web UI](ui.md)** | The optional local web UI (`pinchtab-webgraph-ui`, `[ui]` extra): the Workspace/[Graph](ui.md#graph-view)/[Explore](ui.md#explore-view) view switcher + Ctrl/Cmd-K [command palette](ui.md#command-palette), the REST API + vault endpoints, the chat + screencast WebSockets, [persistent named chats](ui.md#chat-sessions), the opt-in [New crawl](ui.md#new-crawl-get-wscrawl-opt-in) endpoint, env vars, and the loopback-only security model. |
| **[Authenticated login](authenticated-login.md)** | Crawl sites behind a login safely: hand-login vs. keyring-backed automated login, where secrets live, the threat model, sandbox/bot-account isolation, and how to test it. |

## The three interfaces at a glance

The same crawl-once-query-offline capability is reachable three ways, all over one importable core (`pinchtab_webgraph.api`):

| Interface | Entry point | Guide | Runtime dep |
| --- | --- | --- | --- |
| **CLI** | `pwg` / `pinchtab-webgraph` | [main README](../README.md#️-the-tools) | none (pure stdlib) |
| **MCP server** | `pinchtab-webgraph-mcp` (stdio) | [MCP server](mcp-server.md) | `[mcp]` extra |
| **UTCP manual** | `pwg manual` (static manual / `--serve`) | [UTCP interface](utcp.md) | none to use (`[utcp]` only validates) |

## Reference & contributing

- **[`utcp-manual.json`](../utcp-manual.json)** — the committed static UTCP manual (regenerate with `pwg manual --out utcp-manual.json`).
- **[CONTRIBUTING.md](../CONTRIBUTING.md)** — [repository layout](../CONTRIBUTING.md#repository-layout) (where things live, the `out/` convention), branch model, Conventional Commits, the "stay generic" rule, safety, security, and how to open a PR.
- **[LICENSE](../LICENSE)** — MIT.

---

← Back to the **[main README](../README.md)**
