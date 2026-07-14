#!/usr/bin/env python3
"""MCP server exposing the offline graph queries as tools + resources.

This is the Model Context Protocol binding onto `api.py` (the print-free,
dict-returning query surface) and `cache_store.py` (the per-host interaction-graph
cache). A calling LLM gets:

  - six SYNC tools that answer OFFLINE from a cached graph (no browser, no network) —
    each accepts EITHER `host=` (route through the cache) OR `graph=` (an explicit
    graph-file path), and returns the same structured dict `api.*` returns, with a
    `status` field a caller can branch on;
  - three RESOURCES over the interaction-graph cache (host index, per-host summary,
    per-host raw graph);
  - two ASYNC live tools (`crawl`, `ask_howto`) that shell out to the crawler /
    the cache-first how-to resolver via the PinchTab browser bridge, streaming
    progress back through the MCP `Context`.

STRUCTURALLY mcp-free base install: this module is imported by NOTHING in the base
package (`__init__.py` / `cli.py` never touch it) — it is reached ONLY via its own
console script `pinchtab-webgraph-mcp`, so `pip install pinchtab-webgraph` (no
extras) never needs `mcp`.

Generic by construction: routing is by URL hostname, matching by label/URL. No
app/section vocabulary. Security: the crawler's restart/login shell hooks
(`--restart-cmd` / `--login-cmd` / `--login-config`, all run via shell=True) are
NEVER exposed as tool parameters — they are operator-only via env/config. The only
escape hatch, `extra_cli_args`, is forwarded argv-only through
`asyncio.create_subprocess_exec` (never a shell), so it cannot inject a shell.
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP, Context

from . import api, cache_store, flow as flow_mod

mcp = FastMCP("pinchtab-webgraph")

DEFAULT_SERVER = "http://localhost:9871"


# --- shared helpers ----------------------------------------------------------

def _resolve_graph(host=None, graph=None):
    """Resolve exactly one of host/graph to a graph-file path.

    Returns (path, None) on success, or (None, error_dict) where error_dict has a
    `status` in {invalid_args, invalid_host, no_cache_for_host}.
    """
    if (host is None) == (graph is None):
        return None, {"status": "invalid_args",
                      "detail": "pass exactly one of host= or graph=",
                      "host": host, "graph": graph}
    if host is not None:
        try:
            path = cache_store.cache_path(host)
        except ValueError:
            return None, {"status": "invalid_host", "host": host}
        if not os.path.exists(path):
            return None, {"status": "no_cache_for_host", "host": host,
                          "caches_dir": cache_store.caches_dir()}
        return path, None
    return graph, None


def _call(fn, host, graph, **kwargs):
    """Resolve host/graph then call an api.* fn, mapping load/parse errors to a status."""
    path, err = _resolve_graph(host, graph)
    if err is not None:
        return err
    try:
        return fn(path, **kwargs)
    except (OSError, ValueError, json.JSONDecodeError, KeyError) as e:
        return {"status": "invalid_graph", "path": path, "error": str(e)}


# --- sync tools (offline; answer from a cached graph) ------------------------

@mcp.tool()
def graph_summary(host: str | None = None, graph: str | None = None) -> dict:
    """Detect graph kind and return meta + element counts, from a cached graph.

    Pass EXACTLY ONE of `host` (routes through the cache) or `graph` (a graph-file
    path). Returns `graph_kind` in {interaction, link, unknown} plus `meta` and
    counts (interaction: states/edges/triggers; link: nodes/edges). On a resolver/
    load failure returns a `status` in
    {invalid_args, invalid_host, no_cache_for_host, invalid_graph}.
    """
    return _call(api.graph_summary, host, graph)


@mcp.tool()
def howto(host: str | None = None, graph: str | None = None, goal: str | None = None,
          start: str | None = None, match: str | None = None, all: bool = False) -> dict:
    """Shortest click-path(s) to a create-trigger matching `goal`/`match`, + its form.

    Pass EXACTLY ONE of `host` or `graph`, plus a `goal` (natural language) OR a
    `match` (regex over trigger labels). `start` optionally pins the start state;
    `all` returns every routed match, not just the shortest.
    `status` is one of:
      invalid_args   — neither goal nor match given (would broad-match every trigger),
      no_match       — no trigger label matched,
      unreachable    — matched a trigger but no click-path reaches its state,
      ok             — `results` holds the routed step list(s) + form.
    Resolver/load failures add: invalid_host, no_cache_for_host, invalid_graph.
    """
    return _call(api.howto, host, graph, goal=goal, start=start, match=match, all=all)


@mcp.tool()
def find_content(text: str, host: str | None = None, graph: str | None = None,
                 start: str | None = None, limit: int = 40) -> dict:
    """Search captured data collections for `text`; route each matching view.

    Pass EXACTLY ONE of `host` or `graph`. `start` optionally pins the start state
    for routing; `limit` caps returned items.
    `status` is one of:
      no_match — `text` appears in no captured collection,
      ok       — `views` holds each matching view + its click-path.
    Resolver/load failures add: invalid_args, invalid_host, no_cache_for_host,
    invalid_graph.
    """
    return _call(api.find_content, host, graph, text=text, start=start, limit=limit)


@mcp.tool()
def list_content(host: str | None = None, graph: str | None = None) -> dict:
    """Per-view inventory of captured data collections (kinds, counts, a sample).

    Pass EXACTLY ONE of `host` or `graph`.
    `status` is one of:
      empty — no view has any captured collection,
      ok    — `views` holds the per-view collection inventory.
    Resolver/load failures add: invalid_args, invalid_host, no_cache_for_host,
    invalid_graph.
    """
    return _call(api.list_content, host, graph)


@mcp.tool()
def list_forms(host: str | None = None, graph: str | None = None) -> dict:
    """Every create-form in the cache: label, host, click-depth, field count.

    Pass EXACTLY ONE of `host` or `graph`. On success returns `{meta, forms}` (no
    `status` key). A resolver/load failure returns a `status` in
    {invalid_args, invalid_host, no_cache_for_host, invalid_graph}.
    """
    return _call(api.list_forms, host, graph)


@mcp.tool()
def link_paths(frm: str, to: str, host: str | None = None, graph: str | None = None,
               structural: bool = False, all: bool = False, max_len: int = 5,
               max_paths: int = 50) -> dict:
    """Shortest / all click-paths between two pages of a crawled LINK graph.

    Pass EXACTLY ONE of `host` or `graph`. `frm`/`to` are matched by URL/title
    substring. `structural=True` drops global-nav (hub) edges; `all=True` also
    returns every path up to `max_len` (capped at `max_paths`).
    `status` is one of:
      not_found_from / not_found_to   — the needle matched no node,
      ambiguous_from / ambiguous_to   — the needle matched several (see `candidates`),
      no_path                         — no route between the two nodes,
      ok                              — `shortest` (+ `all_paths` when all=True).
    Resolver/load failures add: invalid_args, invalid_host, no_cache_for_host,
    invalid_graph.
    """
    return _call(api.link_paths, host, graph, frm=frm, to=to, structural=structural,
                 all=all, max_len=max_len, max_paths=max_paths)


# --- flow drafting (PURE: no disk, no browser, no subprocess) ----------------
#
# The flow-authoring agent's ONLY write-shaped verb — and it does not write. It exists
# so the model can HAND a candidate document to the UI: the chat layer intercepts every
# call and turns it into a `flow_draft` frame, so the canvas/JSON pane re-renders live
# as the conversation refines the draft. Deliberately there is NO save/update/delete/run
# flow tool anywhere in this module: the agent has no code path to persist or execute a
# flow, so the human's Save/Run buttons remain the ONLY authority. Keep it that way.

@mcp.tool()
def propose_flow(doc: dict, note: str | None = None) -> dict:
    """Validate a candidate flow document and hand it to the UI as the current draft.

    Performs NO disk write and NO browser action — it runs flow.validate_report(doc) and
    echoes `doc` back with the verdict. The chat layer intercepts every call and emits a
    `flow_draft` frame so the UI's canvas/JSON re-renders. Nothing here can save or execute
    a flow. Call it every time you want to show or update the draft — always the WHOLE
    document, never a diff. `note` = a short description of what changed.

    Returns `{"status":"ok","name","host","steps","capabilities","inputs","doc","note"}`
    or `{"status":"invalid","path","error","doc","note"}` — an invalid draft is still
    echoed back so you can see (and fix) exactly what you sent.
    """
    report = flow_mod.validate_report(doc)
    return {**report, "doc": doc, "note": note}


# --- resources (interaction-graph cache) -------------------------------------

@mcp.resource("graph://hosts")
def list_cached_hosts() -> dict:
    """Index of every host with a persisted interaction-graph cache + a cheap summary.

    Each host's summary is computed independently and wrapped in try/except so one
    corrupt cache never breaks the whole index (its entry carries an `error` instead).
    """
    hosts = []
    for h in cache_store.list_hosts():
        entry = {"host": h, "resource_uri": "graph://%s" % h,
                 "summary_uri": "graph://%s/summary" % h}
        try:
            entry["summary"] = api.graph_summary(cache_store.cache_path(h))
        except (OSError, ValueError, json.JSONDecodeError, KeyError) as e:
            entry["error"] = str(e)
        hosts.append(entry)
    return {"hosts": hosts, "caches_dir": cache_store.caches_dir()}


@mcp.resource("graph://{host}/summary")
def host_summary(host: str) -> dict:
    """Cheap summary (kind + meta + counts) for one host's cached graph."""
    try:
        path = cache_store.cache_path(host)
    except ValueError:
        return {"status": "invalid_host", "host": host}
    if not os.path.exists(path):
        return {"status": "no_cache_for_host", "host": host,
                "caches_dir": cache_store.caches_dir()}
    return api.graph_summary(path)


@mcp.resource("graph://{host}")
def host_graph(host: str) -> dict:
    """The full raw interaction graph for one host (the large payload, on demand)."""
    try:
        graph = cache_store.load(host)
    except ValueError:
        return {"status": "invalid_host", "host": host}
    if graph is None:
        return {"status": "no_cache_for_host", "host": host,
                "caches_dir": cache_store.caches_dir()}
    return graph


# --- live subprocess plumbing ------------------------------------------------

# Per-state progress line the crawler prints to stderr:
#   "· [12 states / 40 visits] depth 3 · <url> (7 controls, 3 items)"
_PROGRESS_RE = re.compile(r"\[(\d+) states / (\d+) visits\]")


def _parse_progress(line):
    """Return (states, visits) from a crawler progress line, or None."""
    m = _PROGRESS_RE.search(line)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


# Failure markers the `pinchtab health` CLI prints to stdout/stderr while STILL
# exiting 0 when the bridge is unreachable — so rc==0 alone can't be trusted.
_BRIDGE_DOWN_MARKERS = (
    "request failed", "connection refused", "econnrefused", "dial tcp",
    "could not connect", "no such host", "connect: connection",
)


def _bridge_health(server):
    """Sync preflight: is the PinchTab bridge reachable? None if healthy, else an error.

    # keep in sync with recipe.py pt() — same `pinchtab --server <server> <cmd>` shape.
    Distinct structured errors let a caller tell the failure modes apart:
      bridge_unavailable  — the `pinchtab` CLI is not on PATH,
      bridge_unreachable  — the bridge did not answer `health` (nonzero / timeout),
      bridge_no_token     — reachable but reports missing/invalid auth (soft).
    """
    if shutil.which("pinchtab") is None:
        return {"status": "bridge_unavailable", "reason": "pinchtab_not_on_path",
                "detail": "the `pinchtab` CLI is not on PATH; install it to run live tools"}
    try:
        r = subprocess.run(["pinchtab", "--server", server, "health"],
                           capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        return {"status": "bridge_unavailable", "reason": "pinchtab_not_on_path",
                "detail": "the `pinchtab` CLI is not on PATH; install it to run live tools"}
    except subprocess.TimeoutExpired:
        return {"status": "bridge_unreachable", "reason": "health_timeout", "server": server}
    low = ((r.stderr or "") + (r.stdout or "")).lower()
    if r.returncode != 0:
        if "token" in low or "auth" in low or "unauthor" in low:
            return {"status": "bridge_no_token", "reason": "no_token_configured",
                    "server": server}
        return {"status": "bridge_unreachable", "reason": "health_failed", "server": server,
                "detail": ((r.stderr or r.stdout) or "").strip()[:200]}
    # rc==0 is NOT sufficient: the `pinchtab health` CLI exits 0 even when the
    # bridge is down, printing the failure (e.g. "connection refused") to
    # stdout/stderr. Inspect the output so a dead bridge is caught by the
    # preflight instead of letting a live tool run against nothing.
    if any(m in low for m in _BRIDGE_DOWN_MARKERS):
        return {"status": "bridge_unreachable", "reason": "health_no_connect", "server": server,
                "detail": ((r.stderr or r.stdout) or "").strip()[:200]}
    return None


async def _pump(stream, on_line):
    """Relay every line of an async byte stream to on_line (decoded, rstripped)."""
    if stream is None:
        return
    while True:
        raw = await stream.readline()
        if not raw:
            break
        text = raw.decode(errors="replace").rstrip()
        if on_line is not None:
            await on_line(text)


async def _run_relayed(argv, on_line, timeout_seconds, _subprocess_exec, want_stdout=False):
    """Launch argv, relay stderr (+ optionally stdout) to on_line, wait with a timeout.

    Returns (proc, timed_out). On timeout the process is terminated (then killed if it
    ignores the signal) so a partial, atomically-flushed graph is still readable.
    """
    proc = await _subprocess_exec(
        *argv,
        stdout=(subprocess.PIPE if want_stdout else subprocess.DEVNULL),
        stderr=subprocess.PIPE,
    )
    pumps = [asyncio.ensure_future(_pump(proc.stderr, on_line))]
    if want_stdout:
        pumps.append(asyncio.ensure_future(_pump(proc.stdout, on_line)))
    timed_out = False
    try:
        await asyncio.wait_for(proc.wait(), timeout_seconds)
    except (asyncio.TimeoutError, TimeoutError):
        timed_out = True
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), 30)
        except (asyncio.TimeoutError, TimeoutError):
            proc.kill()
            try:
                await proc.wait()
            except ProcessLookupError:
                pass
    finally:
        for p in pumps:
            try:
                await p
            except Exception:
                pass
    return proc, timed_out


# --- live tools --------------------------------------------------------------

async def _crawl_impl(start, *, max_states=None, max_visits=None, max_depth=None,
                      cross_host=False, max_cross_host=None, single_url=False,
                      capture_content=True, read_forms=True, server=DEFAULT_SERVER,
                      out_path=None, extra_cli_args=None, timeout_seconds=1800,
                      on_line=None, _subprocess_exec=asyncio.create_subprocess_exec):
    """Crawl `start` into an interaction-graph cache (subprocess + line relay).

    Preflights the bridge (never launches a subprocess when it's down), maps only the
    SAFE crawler flags to argv, appends `extra_cli_args` verbatim (argv-only), and on
    timeout still reads back the partial graph the crawler flushes on SIGTERM.
    Returns {status, output_path, resource_uri, ...} — NEVER the raw graph body.
    """
    host = urlparse(start).hostname
    if not host:
        return {"status": "invalid_args", "start": start,
                "detail": "start must be a full URL including a scheme, e.g. https://example.com"}

    berr = await asyncio.to_thread(_bridge_health, server)
    if berr is not None:
        return berr

    # `used_cache` decides whether the graph://{host} resource can resolve to what we
    # write (it reads the DEFAULT host cache): only when no custom out_path was given.
    used_cache = out_path is None
    out_path = out_path or cache_store.cache_path(host)
    out_stem = out_path[:-len(".json")] if out_path.endswith(".json") else out_path
    # The crawler ALWAYS writes "<stem>.json" (interaction_crawl passes "%s.json" % a.out),
    # so pin the canonical on-disk path once and use it everywhere downstream — no path
    # can diverge from what the crawler actually wrote (fixes a raw-stem open() mismatch).
    out_file = out_stem + ".json"
    os.makedirs(os.path.dirname(os.path.abspath(out_file)) or ".", exist_ok=True)

    argv = [sys.executable, "-m", "pinchtab_webgraph.interaction_crawl",
            "--start", start, "--out", out_stem, "--server", server]
    if max_states is not None:
        argv += ["--max-states", str(max_states)]
    if max_visits is not None:
        argv += ["--max-visits", str(max_visits)]
    if max_depth is not None:
        argv += ["--max-depth", str(max_depth)]
    if cross_host:
        argv += ["--cross-host"]
    if max_cross_host is not None:
        argv += ["--max-cross-host", str(max_cross_host)]
    if single_url:
        argv += ["--single-url"]
    if not capture_content:
        argv += ["--no-capture-content"]
    if not read_forms:
        argv += ["--no-read-forms"]
    if extra_cli_args:
        argv += list(extra_cli_args)

    proc, timed_out = await _run_relayed(argv, on_line, timeout_seconds, _subprocess_exec)

    try:
        with open(out_file) as f:
            graph = json.load(f)
    except (OSError, ValueError) as e:
        result = {"status": "failed", "output_path": out_file,
                  "returncode": proc.returncode, "timed_out": timed_out,
                  "detail": "crawl produced no readable graph (%s)" % e}
        if used_cache:
            result["resource_uri"] = "graph://%s" % host
        return result

    meta = graph.get("meta", {})
    try:
        summary = api.graph_summary(out_file)
    except (OSError, ValueError, KeyError) as e:
        summary = {"error": str(e)}
    if timed_out:
        status = "timeout"
    elif proc.returncode == 0:
        status = "ok"
    else:
        status = "partial"
    result = {"status": status, "output_path": out_file,
              "returncode": proc.returncode, "complete": bool(meta.get("complete")),
              "meta": meta, "summary": summary}
    # graph://{host} resolves to the DEFAULT host cache only; a custom out_path would
    # make it point at stale/absent data, so emit resource_uri only for the cache case.
    if used_cache:
        result["resource_uri"] = "graph://%s" % host
    return result


@mcp.tool()
async def crawl(start: str, max_states: int | None = None, max_visits: int | None = None,
                max_depth: int | None = None, cross_host: bool = False,
                max_cross_host: int | None = None, single_url: bool = False,
                capture_content: bool = True, read_forms: bool = True,
                server: str = DEFAULT_SERVER, out_path: str | None = None,
                extra_cli_args: list[str] | None = None, timeout_seconds: int = 1800,
                ctx: Context = None) -> dict:
    """Crawl a site into its per-host interaction-graph cache (REPLACES that cache).

    Needs a running PinchTab bridge (preflighted; a down bridge returns a structured
    error, never launches). Default output is the host's cache file (override with
    `out_path`). This REPLACES the host cache wholesale (unlike `ask_howto`, which
    MERGES a single discovery into it). Streams crawler progress via the MCP Context.
    Returns {status, output_path, resource_uri, meta, summary} — never the raw graph;
    fetch that on demand via the `graph://{host}` resource. `status` is one of
    {ok, timeout, partial, failed} or a bridge error
    {bridge_unavailable, bridge_unreachable, bridge_no_token} or invalid_args.

    NOTE: the crawler's restart/login shell hooks are operator-only (env/config), not
    tool parameters. `extra_cli_args` is forwarded argv-only (never through a shell).
    """
    async def on_line(line):
        if ctx is not None:
            await ctx.info(line)
            p = _parse_progress(line)
            if p is not None:
                _states, visits = p
                await ctx.report_progress(visits, max_visits, line)

    return await _crawl_impl(
        start, max_states=max_states, max_visits=max_visits, max_depth=max_depth,
        cross_host=cross_host, max_cross_host=max_cross_host, single_url=single_url,
        capture_content=capture_content, read_forms=read_forms, server=server,
        out_path=out_path, extra_cli_args=extra_cli_args, timeout_seconds=timeout_seconds,
        on_line=on_line)


async def _ask_howto_impl(start, goal, *, verify=False, server=DEFAULT_SERVER,
                          timeout_seconds=300, on_line=None,
                          _subprocess_exec=asyncio.create_subprocess_exec):
    """Cache-first how-to: answer offline if cached, else run live and re-query.

    Adds `cache_state` in {hit, updated, live_failed}: a cache HIT (and not verify)
    returns immediately with NO subprocess/bridge; otherwise the bridge is preflighted,
    `ask.py` runs live (cache-first→live→write-back), and the updated cache is re-read
    in-process for the structured result.
    """
    host = urlparse(start).hostname
    if not host:
        return {"status": "invalid_args", "start": start,
                "detail": "start must be a full URL including a scheme, e.g. https://example.com"}

    cache_file = cache_store.cache_path(host)
    cache_exists = os.path.exists(cache_file)

    # Fast path: an in-cache answer, unless the caller forced a live re-check.
    if cache_exists and not verify:
        res = api.howto(cache_file, goal=goal, start=start)
        if res.get("status") == "ok":
            out = dict(res)
            out["cache_state"] = "hit"
            return out

    # Live: preflight the bridge, then shell out to ask.py.
    berr = await asyncio.to_thread(_bridge_health, server)
    if berr is not None:
        out = dict(berr)
        out["cache_state"] = "live_failed"
        return out

    argv = [sys.executable, "-m", "pinchtab_webgraph.ask",
            "--goal", goal, "--start", start, "--server", server]
    if verify:
        argv += ["--verify"]

    proc, timed_out = await _run_relayed(argv, on_line, timeout_seconds, _subprocess_exec,
                                         want_stdout=True)

    res = api.howto(cache_file, goal=goal, start=start) if os.path.exists(cache_file) else None
    if res is not None and res.get("status") == "ok":
        out = dict(res)
        out["cache_state"] = "updated"
        return out

    out = dict(res) if res is not None else {"status": "no_cache_for_host", "host": host}
    out["cache_state"] = "live_failed"
    out["returncode"] = proc.returncode
    if timed_out:
        out["timed_out"] = True
    return out


@mcp.tool()
async def ask_howto(start: str, goal: str, verify: bool = False,
                    server: str = DEFAULT_SERVER, timeout_seconds: int = 300,
                    ctx: Context = None) -> dict:
    """Answer "how do I do X?" cache-first, running the browser only on a miss.

    Tries the host cache in-process first: a HIT (and not `verify`) returns instantly
    with NO browser/bridge. Otherwise preflights the bridge and runs live discovery,
    MERGING the single result back into the host cache (unlike `crawl`, which replaces
    it). `verify=True` always re-checks live. Adds `cache_state` in
    {hit, updated, live_failed} on top of the underlying `howto` status
    {ok, no_match, unreachable, invalid_args}; a down bridge returns
    {bridge_unavailable, bridge_unreachable, bridge_no_token} + cache_state=live_failed.

    CAVEAT: a timed-out `verify` run may lose the live write-back — `ask.py` has no
    SIGTERM flush, so a terminate() mid-run leaves the cache unchanged.
    """
    async def on_line(line):
        if ctx is not None:
            await ctx.info(line)

    return await _ask_howto_impl(start, goal, verify=verify, server=server,
                                 timeout_seconds=timeout_seconds, on_line=on_line)


@mcp.tool()
async def perform(goal: str | None = None, host: str | None = None,
                  graph: str | None = None, start: str | None = None,
                  match: str | None = None, index: int = 0,
                  values: dict | None = None, upload_file: str | None = None,
                  out_dir: str | None = None, allow_submit: bool = False,
                  dry_run: bool = False, server: str = DEFAULT_SERVER,
                  ctx: Context = None) -> dict:
    """PERFORM a how-to: resolve it offline, then RUN the compiled PinchTab block live.

    Pass EXACTLY ONE of `host`/`graph` (the crawled graph to resolve against) plus a
    `goal` or `match`. Resolution is offline; execution drives the bridge. Safe by
    default: navigation + downloads run; a form field with no supplied value is SKIPPED
    (`values` maps field-label → value, `upload_file` feeds a file input); the SUBMIT
    runs only with `allow_submit=True`. `dry_run=True` returns exactly what WOULD run
    and never touches the bridge. `status`:
      ok            — `steps` holds each step's run/skipped/error result,
      no_match / unreachable / invalid_args — resolution failed (no execution),
      bridge_* / cache_state=live_failed — the bridge was down (non-dry-run only).
    """
    from . import perform as perform_mod

    path, err = _resolve_graph(host, graph)
    if err is not None:
        return err
    if not (goal or match):
        return {"status": "invalid_args", "detail": "pass a goal or a match"}
    try:
        plan = api.resolve_action(path, goal=goal, start=start, match=match, index=index)
    except (OSError, ValueError, json.JSONDecodeError, KeyError) as e:
        return {"status": "invalid_graph", "path": path, "error": str(e)}
    if plan.get("status") != "ok":
        return plan

    # dry-run needs no bridge — compile + report without executing.
    if not dry_run:
        berr = await asyncio.to_thread(_bridge_health, server)
        if berr is not None:
            out = dict(berr)
            out["cache_state"] = "live_failed"
            return out

    token = perform_mod.load_token()
    tab = None if dry_run else await asyncio.to_thread(perform_mod.resolve_tab, server, token,
                                                       plan["start_url"])
    steps = await asyncio.to_thread(
        perform_mod.execute_plan, plan["trigger"], plan["path_steps"], plan["start_url"],
        allow_submit=allow_submit, server=server, token=token, tab=tab, values=values,
        upload_file=upload_file, out_dir=out_dir, dry_run=dry_run)
    return {"status": "ok", "goal": goal, "trigger": plan["trigger_label"],
            "action_kind": plan["action_kind"], "download_url": plan.get("download_url"),
            "dry_run": dry_run, "steps": steps}


def main():
    mcp.run()


if __name__ == "__main__":
    main()
