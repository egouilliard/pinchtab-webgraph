# UTCP interface

> **Docs:** [← README](../README.md) · [📚 Index](README.md) · [MCP server](mcp-server.md) · [Authenticated login](authenticated-login.md)

`pinchtab-webgraph` ships a [UTCP](https://www.utcp.io) (Universal Tool Calling
Protocol) manual that lets any UTCP-aware tool-caller invoke the offline graph
queries — and the two live browser-driven tools — by shelling out to the `pwg` CLI
directly, with **no wrapper server running**. The manual describes each tool's
JSON-schema inputs/outputs and the exact command string to run; the caller injects
arguments and runs the command itself.

Unlike the [MCP server](mcp-server.md) (a long-lived process a client speaks to),
UTCP is *just a description* of how to call a plain CLI. The two are complementary
bindings onto the same `api.py` query surface, so answers are identical.

## Prerequisites

Nothing extra is needed to **use** the interface: `pwg query …` and `pwg manual` are
part of the base install (pure stdlib). A UTCP client runs the manual's command
strings against the `pwg` console script on `PATH`.

```bash
pipx install git+https://github.com/egouilliard/pinchtab-webgraph   # installs `pwg`
```

The `[utcp]` extra is only needed to run the **validation test** that checks the
generated manual against the real UTCP pydantic model — it is not needed at runtime:

```bash
pip install 'pinchtab-webgraph[utcp]'
```

On Ubuntu / any PEP-668 "externally-managed" Python, either use a venv (recommended)
or add the escape flags:

```bash
pip install --user --break-system-packages 'pinchtab-webgraph[utcp]'
```

The **offline** query tools need only a populated cache. The **live** tools
(`crawl`, `ask`) additionally need a running PinchTab browser bridge — the same
bridge the CLI crawler uses (see the repo's Requirements).

## Getting the manual

Three ways, all equivalent:

```bash
pwg manual                       # print the manual JSON to stdout
pwg manual --out utcp-manual.json # write it to a file (no stdout)
pwg manual --serve               # serve it over HTTP (blocking; Ctrl-C to stop)
pwg manual --serve --port 9872   # default bind is 127.0.0.1:9872
```

A committed copy lives at [`utcp-manual.json`](../utcp-manual.json) in the repo root.
When served, the manual is available at **both** `/utcp` and `/.well-known/utcp`:

| Endpoint | Returns |
| --- | --- |
| `GET /utcp` | the manual JSON (`application/json`) |
| `GET /.well-known/utcp` | the same manual (the conventional discovery path) |
| anything else | `404` `{"error":"not found","hint":"GET /utcp"}` |

## Tools

The manual exposes **8 tools**. Each runs a `pwg …` command with arguments injected
via the literal token `UTCP_ARG_<name>_UTCP_END`.

| Tool | Kind | Inputs (all required) | Command | `status` values |
| --- | --- | --- | --- | --- |
| `graph_summary` | offline | `host` | `pwg query graph_summary --host …` | `graph_kind` ∈ interaction/link/unknown |
| `howto` | offline | `host`, `goal` | `pwg query howto --host … --goal …` | `ok` / `no_match` / `unreachable` / `invalid_args` |
| `find_content` | offline | `host`, `text` | `pwg query find_content --host … --text …` | `ok` / `no_match` |
| `list_content` | offline | `host` | `pwg query list_content --host …` | `ok` / `empty` |
| `list_forms` | offline | `host` | `pwg query list_forms --host …` | (no `status`; `{meta, forms}`) |
| `link_paths` | offline | `host`, `frm`, `to` | `pwg query link_paths --host … --from … --to …` | `ok` / `no_path` / `{not_found,ambiguous}_{from,to}` |
| `crawl` | live | `start` | `pwg crawl …` | narrated text (needs a running PinchTab bridge) |
| `ask` | live | `start`, `goal` | `pwg ask --start … --goal …` | narrated text (needs a running PinchTab bridge) |

## Scope: a deliberate subset

The UTCP surface is a **deliberate subset** of the full CLI, so every command string
is free of optional placeholders:

- **Required core args only** — optional flags (`--start`, `--match`, `--all`,
  `--limit`, `--structural`, `--max-len`, `--max-paths`) are dropped from the manual.
- **`--host` only** — the query tools route through the per-host cache; the manual
  never exposes the `--graph` path form (it would need an on-disk placeholder).

Callers that need those knobs run `pwg query …` directly with the extra flags — the
manual just advertises the common path.

## Exit codes (the `query` command)

`pwg query …` prints its result as JSON on **stdout** and follows this convention:

| Exit | Meaning |
| --- | --- |
| `0` | any api-level result, **including a structured miss** (`no_match`, `unreachable`, `empty`, `no_path`, `not_found_*`, `ambiguous_*`, `invalid_args`) — the JSON carries the `status`; the miss is not an error |
| `1` | a resolver / environment error (`invalid_host`, `no_cache_for_host`, `invalid_graph`) — the JSON is **still** printed to stdout, not stderr |
| `2` | an argparse usage error (missing required flag, or neither/both `--host`/`--graph`) |

The offline query tools never touch the network or a browser. The live tools
(`crawl`, `ask`) emit **narrated text** today (human-readable progress + result), not
structured JSON.

## Notes

- **Client-dependent JSON parsing.** Whether a UTCP client automatically
  `json.loads()` a tool's stdout (vs. handing back the raw string) is
  client-dependent. The `query` tools always emit valid JSON; the live `crawl`/`ask`
  tools emit narration.
- **Manual generation is pure-stdlib.** `build_manual()` imports only stdlib + the
  package version; nothing in the base install imports `utcp`. Only the gated
  validation test does (behind `pytest.importorskip("utcp")`).
- **Single source of truth for the version.** `manual_version` is the package
  `__version__`; regenerate `utcp-manual.json` (`pwg manual --out utcp-manual.json`)
  after a version bump — a test asserts the committed file stays in sync.
