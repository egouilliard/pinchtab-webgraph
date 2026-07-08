# MCP server

> **Docs:** [← README](../README.md) · [📚 Index](README.md) · [UTCP interface](utcp.md) · [Web UI](ui.md) · [Authenticated login](authenticated-login.md)

`pinchtab-webgraph` ships an optional [Model Context Protocol](https://modelcontextprotocol.io)
server that exposes the offline graph queries — and two live browser-driven tools —
to any MCP client (Claude Desktop, Claude Code, etc.). It is a thin binding onto the
same `api.py` query surface the CLI uses, so answers are identical.

The base install stays **mcp-free**: the server lives behind its own extra and its own
console script (`pinchtab-webgraph-mcp`), and nothing in the base package imports it.
`pip install pinchtab-webgraph` never pulls in `mcp`.

## Prerequisites

```bash
# install with the mcp extra
pip install 'pinchtab-webgraph[mcp]'
```

On Ubuntu / any PEP-668 "externally-managed" Python, either use a venv (recommended)
or add the escape flags:

```bash
pip install --user --break-system-packages 'pinchtab-webgraph[mcp]'
```

The **offline** tools and resources need only a populated cache. The **live** tools
(`crawl`, `ask_howto`) additionally need a running PinchTab browser bridge — the same
bridge the CLI crawler uses (see the repo's `scripts/start-crawl-browser.sh` / Requirements).

## Register the server

`.mcp.json` (Claude Code) or your client's server config:

```json
{
  "mcpServers": {
    "pinchtab-webgraph": {
      "command": "pinchtab-webgraph-mcp"
    }
  }
}
```

## Tools

Every offline tool takes **exactly one** of `host=` (route through the per-host cache)
or `graph=` (an explicit graph-file path). All return the structured dict `api.*`
returns, with a `status` field to branch on.

| Tool | Kind | What it does | `status` values |
| --- | --- | --- | --- |
| `graph_summary` | offline | Graph kind + meta + element counts | `graph_kind` ∈ interaction/link/unknown; resolver errors |
| `howto` | offline | Shortest click-path(s) to a create-trigger + its form | `ok` / `no_match` / `unreachable` / `invalid_args` |
| `find_content` | offline | Search captured data collections for text; route each match | `ok` / `no_match` |
| `list_content` | offline | Per-view inventory of captured collections | `ok` / `empty` |
| `list_forms` | offline | Every create-form: label, host, depth, field count | (no `status`; `{meta, forms}`) |
| `link_paths` | offline | Shortest / all paths between two pages of a link graph | `ok` / `no_path` / `{not_found,ambiguous}_{from,to}` |
| `crawl` | live | Crawl a site into its per-host cache (**REPLACES** it) | `ok` / `timeout` / `partial` / `failed` / bridge errors |
| `ask_howto` | live | Cache-first how-to; runs the browser only on a miss (**MERGES**) | underlying `howto` status + `cache_state` ∈ hit/updated/live_failed |

All offline tools may also return a resolver/load status:
`invalid_args`, `invalid_host`, `no_cache_for_host`, `invalid_graph`.

A down bridge surfaces as `bridge_unavailable` (CLI not on PATH),
`bridge_unreachable` (no answer / timeout), or `bridge_no_token` (auth not configured).

## Resources

Over the interaction-graph cache:

| URI | Returns |
| --- | --- |
| `graph://hosts` | Index of every cached host + a cheap per-host summary |
| `graph://{host}/summary` | Kind + meta + counts for one host |
| `graph://{host}` | The full raw interaction graph (the large payload, on demand) |

`crawl` returns a `resource_uri: graph://{host}` pointer rather than the graph body —
fetch the graph on demand via that resource.

## Environment variables

| Var | Effect |
| --- | --- |
| `PINCHTAB_WEBGRAPH_HOME` | Root of the cache dir (`$HOME/.pinchtab-webgraph` by default) |
| `PINCHTAB_CONFIG` | Crawl config JSON (bridge token etc.) for the live tools |
| `PINCHTAB_TOKEN` | Bridge auth token for the live tools |

## Notes & safety

- **Operator-only hooks.** The crawler's restart/login shell hooks
  (`--restart-cmd` / `--login-cmd` / `--login-config`) run via a shell and are
  **not** exposed as tool parameters. Configure them out-of-band (env/config) if you
  need them. The only escape hatch, `crawl`'s `extra_cli_args`, is forwarded
  argv-only (never through a shell).
- **`crawl` replaces, `ask_howto` merges.** `crawl` overwrites a host's cache
  wholesale; `ask_howto` stitches a single live discovery into the existing cache.
- **`ask_howto` verify timeout caveat.** `ask.py` has no SIGTERM flush, so a
  timed-out `verify` run may lose its live write-back (the cache is left unchanged).
- Offline tools never touch the network or a browser; only `crawl` and `ask_howto`
  do, and both preflight the bridge before launching anything.
