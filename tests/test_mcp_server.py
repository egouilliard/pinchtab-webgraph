"""Tests for pinchtab_webgraph.mcp_server — the MCP binding onto api + cache_store.

The `@mcp.tool()` / `@mcp.resource()` decorators return the wrapped function
UNCHANGED (verified against mcp 1.28.x), so the sync tools + resources are called
here as plain functions. Sync tools are exercised both via `graph=` (explicit path)
and via `host=` (cache routing, against populated_cache_home), asserting the SAME
expected values test_api.py asserts. The async live tools are driven through their
plain `_..._impl(...)` helpers with an injected fake `_subprocess_exec` — no bridge,
no real browser, no pytest-asyncio.
"""
import asyncio
import json
import os

import pytest

pytest.importorskip("mcp")

from pinchtab_webgraph import cache_store, mcp_server


HOST = "example.test"


# --- _resolve_graph error paths ----------------------------------------------

def test_resolve_graph_neither():
    path, err = mcp_server._resolve_graph()
    assert path is None
    assert err["status"] == "invalid_args"


def test_resolve_graph_both(sample_interaction_graph_path):
    path, err = mcp_server._resolve_graph(host=HOST, graph=str(sample_interaction_graph_path))
    assert path is None
    assert err["status"] == "invalid_args"


def test_resolve_graph_invalid_host():
    path, err = mcp_server._resolve_graph(host="../../etc/passwd")
    assert path is None
    assert err["status"] == "invalid_host"


def test_resolve_graph_no_cache_for_host(isolated_cache_home):
    path, err = mcp_server._resolve_graph(host="never-crawled.test")
    assert path is None
    assert err["status"] == "no_cache_for_host"
    assert err["host"] == "never-crawled.test"


def test_resolve_graph_host_ok(populated_cache_home):
    path, err = mcp_server._resolve_graph(host=HOST)
    assert err is None
    assert path.endswith("%s.json" % HOST)


def test_call_invalid_graph(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json")
    out = mcp_server.graph_summary(graph=str(bad))
    assert out["status"] == "invalid_graph"


# --- graph_summary -----------------------------------------------------------

def test_graph_summary_by_graph(sample_interaction_graph_path):
    out = mcp_server.graph_summary(graph=str(sample_interaction_graph_path))
    assert out["graph_kind"] == "interaction"
    assert out["states"] == 5
    assert out["edges"] == 3
    assert out["triggers"] == 3
    assert out["meta"]["host"] == HOST


def test_graph_summary_by_host(populated_cache_home):
    out = mcp_server.graph_summary(host=HOST)
    assert out["graph_kind"] == "interaction"
    assert out["states"] == 5
    assert out["triggers"] == 3


def test_graph_summary_link_by_graph(sample_link_graph_path):
    out = mcp_server.graph_summary(graph=str(sample_link_graph_path))
    assert out["graph_kind"] == "link"
    assert out["nodes"] == 9
    assert out["edges"] == 11


# --- howto -------------------------------------------------------------------

def test_howto_ok_by_graph(sample_interaction_graph_path):
    out = mcp_server.howto(graph=str(sample_interaction_graph_path), goal="create role")
    assert out["status"] == "ok"
    r = out["results"][0]
    assert r["trigger_label"] == "Create Role"
    assert r["clicks"] == 3
    assert r["form"]["fieldCount"] == 1


def test_howto_ok_by_host(populated_cache_home):
    out = mcp_server.howto(host=HOST, goal="create role")
    assert out["status"] == "ok"
    assert out["results"][0]["trigger_label"] == "Create Role"


def test_howto_unreachable_by_host(populated_cache_home):
    out = mcp_server.howto(host=HOST, goal="add widget")
    assert out["status"] == "unreachable"
    assert "Add Widget" in out["candidates"]


def test_howto_no_goal_no_match_by_graph(sample_interaction_graph_path):
    out = mcp_server.howto(graph=str(sample_interaction_graph_path))
    assert out["status"] == "invalid_args"


# --- find_content ------------------------------------------------------------

def test_find_content_hit_by_graph(sample_interaction_graph_path):
    out = mcp_server.find_content("Alice", graph=str(sample_interaction_graph_path))
    assert out["status"] == "ok"
    assert out["total_matches"] == 1
    assert out["views"][0]["items"][0]["text"] == "Alice Martin"


def test_find_content_hit_by_host(populated_cache_home):
    out = mcp_server.find_content("Alice", host=HOST)
    assert out["status"] == "ok"
    assert out["views"][0]["view_label"] == "Team"


def test_find_content_miss_by_host(populated_cache_home):
    out = mcp_server.find_content("nothinghere", host=HOST)
    assert out["status"] == "no_match"


# --- list_content ------------------------------------------------------------

def test_list_content_by_graph(sample_interaction_graph_path):
    out = mcp_server.list_content(graph=str(sample_interaction_graph_path))
    assert out["status"] == "ok"
    assert {v["view_label"] for v in out["views"]} == {"Team", "Reports"}


def test_list_content_by_host(populated_cache_home):
    out = mcp_server.list_content(host=HOST)
    assert out["status"] == "ok"


def test_list_content_empty_by_graph(sample_link_graph_path):
    out = mcp_server.list_content(graph=str(sample_link_graph_path))
    assert out["status"] == "empty"


# --- list_forms --------------------------------------------------------------

def test_list_forms_by_graph(sample_interaction_graph_path):
    out = mcp_server.list_forms(graph=str(sample_interaction_graph_path))
    assert out["meta"]["host"] == HOST
    assert out["meta"]["triggers"] == 3
    assert {f["label"] for f in out["forms"]} == {"Create Role", "Add Report", "Add Widget"}


def test_list_forms_by_host(populated_cache_home):
    out = mcp_server.list_forms(host=HOST)
    assert out["meta"]["host"] == HOST
    assert out["meta"]["triggers"] == 3


# --- link_paths --------------------------------------------------------------

def test_link_paths_shortest_by_graph(sample_link_graph_path):
    out = mcp_server.link_paths("home", "guide", graph=str(sample_link_graph_path))
    assert out["status"] == "ok"
    assert out["shortest"]["clicks"] == 1


def test_link_paths_by_host(sample_link_graph_path, isolated_cache_home):
    # seed a LINK graph under a host so link_paths routes via host=.
    graph = json.loads(sample_link_graph_path.read_text())
    cache_store.atomic_write("links.test", graph)
    out = mcp_server.link_paths("home", "guide", host="links.test")
    assert out["status"] == "ok"
    assert out["shortest"]["clicks"] == 1


def test_link_paths_ambiguous_by_graph(sample_link_graph_path):
    out = mcp_server.link_paths("home", "port", graph=str(sample_link_graph_path))
    assert out["status"] == "ambiguous_to"
    assert len(out["candidates"]) == 2


# --- resources ---------------------------------------------------------------

def test_resource_hosts_index(populated_cache_home):
    out = mcp_server.list_cached_hosts()
    hosts = {h["host"] for h in out["hosts"]}
    assert HOST in hosts
    entry = next(h for h in out["hosts"] if h["host"] == HOST)
    assert entry["resource_uri"] == "graph://%s" % HOST
    assert entry["summary"]["graph_kind"] == "interaction"


def test_resource_host_summary(populated_cache_home):
    out = mcp_server.host_summary(HOST)
    assert out["graph_kind"] == "interaction"
    assert out["states"] == 5


def test_resource_host_summary_missing(isolated_cache_home):
    out = mcp_server.host_summary("never.test")
    assert out["status"] == "no_cache_for_host"


def test_resource_host_summary_invalid(isolated_cache_home):
    out = mcp_server.host_summary("../etc/passwd")
    assert out["status"] == "invalid_host"


def test_resource_host_graph_full(populated_cache_home):
    out = mcp_server.host_graph(HOST)
    # the FULL raw graph, not a summary
    assert "states" in out and "triggers" in out
    assert len(out["states"]) == 5


def test_resource_host_graph_missing(isolated_cache_home):
    out = mcp_server.host_graph("never.test")
    assert out["status"] == "no_cache_for_host"


# --- async live tools (fake subprocess) --------------------------------------

class _FakeStream:
    """Async byte stream yielding canned lines then EOF."""

    def __init__(self, lines):
        self._lines = list(lines)

    async def readline(self):
        if self._lines:
            return self._lines.pop(0)
        return b""


class _FakeProc:
    """Minimal stand-in for asyncio subprocess: canned stderr, controllable wait."""

    def __init__(self, stderr_lines=(), stdout_lines=(), returncode=0,
                 completes=True, on_write=None):
        self.stderr = _FakeStream(stderr_lines)
        self.stdout = _FakeStream(stdout_lines)
        self.returncode = returncode
        self._completes = completes
        self._done = asyncio.Event()
        self.terminated = False
        self.killed = False
        self._on_write = on_write

    async def wait(self):
        if self._completes:
            if self._on_write is not None:
                self._on_write()
            return self.returncode
        await self._done.wait()   # blocks until terminate()/kill() releases it
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15
        self._done.set()

    def kill(self):
        self.killed = True
        self.returncode = -9
        self._done.set()


def _fake_exec_factory(proc, recorder):
    async def _factory(*argv, **kwargs):
        recorder["argv"] = argv
        recorder["kwargs"] = kwargs
        recorder["called"] = True
        return proc
    return _factory


def test_crawl_happy_path(sample_interaction_graph_path, tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "_bridge_health", lambda server: None)
    out_path = tmp_path / "crawled.json"
    graph = sample_interaction_graph_path.read_text()

    def _write():
        out_path.write_text(graph)

    proc = _FakeProc(
        stderr_lines=[b"! Interaction crawl from https://example.test/dashboard\n",
                      b"\xc2\xb7 [5 states / 9 visits] depth 2 . done\n"],
        returncode=0, completes=True, on_write=_write)
    rec = {}
    seen = []

    async def on_line(line):
        seen.append(line)

    result = asyncio.run(mcp_server._crawl_impl(
        "https://example.test/dashboard", out_path=str(out_path), on_line=on_line,
        max_visits=100, _subprocess_exec=_fake_exec_factory(proc, rec)))

    assert rec["called"] is True
    assert result["status"] == "ok"
    assert result["output_path"] == str(out_path)
    # custom out_path (Fix 2): graph://{host} would resolve to the DEFAULT cache, not
    # this file, so resource_uri is omitted for a custom-path crawl.
    assert "resource_uri" not in result
    assert result["summary"]["graph_kind"] == "interaction"
    assert len(seen) == 2   # both stderr lines relayed
    assert any("5 states / 9 visits" in s for s in seen)
    # SAFE flag mapping made it into argv; no shell-injection surface.
    assert "--max-visits" in rec["argv"] and "100" in rec["argv"]


def test_crawl_single_url_tristate_maps_to_argv(monkeypatch):
    # single_url is tri-state: None → auto-detect (no flag), True → --single-url,
    # False → --no-single-url (the force-off escape hatch). Backward-compatible: True
    # still forces app-shell mode exactly as before; the default just changed to auto.
    monkeypatch.setattr(mcp_server, "_bridge_health", lambda server: None)
    for val in (None, True, False):
        proc = _FakeProc(returncode=0, completes=True)
        rec = {}
        asyncio.run(mcp_server._crawl_impl(
            "https://example.test/", single_url=val,
            _subprocess_exec=_fake_exec_factory(proc, rec)))
        argv = list(rec["argv"])
        if val is None:
            assert "--single-url" not in argv and "--no-single-url" not in argv
        elif val is True:
            assert "--single-url" in argv and "--no-single-url" not in argv
        else:
            assert "--no-single-url" in argv and "--single-url" not in argv


def test_crawl_timeout_returns_partial(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "_bridge_health", lambda server: None)
    out_path = tmp_path / "partial.json"
    # pre-write a partial graph (what the crawler flushes on SIGTERM).
    out_path.write_text(json.dumps({
        "meta": {"host": "example.test", "complete": False, "stopped": "in-progress"},
        "states": [], "edges": [], "triggers": []}))
    proc = _FakeProc(stderr_lines=[], completes=False)   # never finishes on its own
    rec = {}

    result = asyncio.run(mcp_server._crawl_impl(
        "https://example.test/dashboard", out_path=str(out_path), timeout_seconds=0.05,
        _subprocess_exec=_fake_exec_factory(proc, rec)))

    assert proc.terminated is True
    assert result["status"] == "timeout"
    assert result["meta"]["complete"] is False
    assert result["complete"] is False


def test_crawl_bridge_down_no_subprocess(monkeypatch):
    monkeypatch.setattr(mcp_server, "_bridge_health",
                        lambda server: {"status": "bridge_unreachable", "reason": "health_failed"})
    rec = {"called": False}
    proc = _FakeProc()

    result = asyncio.run(mcp_server._crawl_impl(
        "https://example.test/dashboard",
        _subprocess_exec=_fake_exec_factory(proc, rec)))

    assert result["status"] == "bridge_unreachable"
    assert rec["called"] is False   # never launched the subprocess


def test_ask_howto_cache_hit_fast_path(populated_cache_home, monkeypatch):
    rec = {"called": False}
    proc = _FakeProc()
    # If the fast path is broken and it reaches the bridge, force a distinct failure.
    monkeypatch.setattr(mcp_server, "_bridge_health",
                        lambda server: {"status": "bridge_unavailable"})

    result = asyncio.run(mcp_server._ask_howto_impl(
        "https://example.test/dashboard", "create role",
        _subprocess_exec=_fake_exec_factory(proc, rec)))

    assert result["cache_state"] == "hit"
    assert result["status"] == "ok"
    assert result["results"][0]["trigger_label"] == "Create Role"
    assert rec["called"] is False   # no subprocess, no bridge preflight consequence


# --- crawl out_path round-trip (Fix 1/2) -------------------------------------

def test_crawl_custom_out_path_no_json_suffix(sample_interaction_graph_path, tmp_path,
                                              monkeypatch):
    """out_path without a .json suffix: the crawler writes <stem>.json, so the code
    must open that real file (Fix 1) and OMIT resource_uri for a custom path (Fix 2)."""
    monkeypatch.setattr(mcp_server, "_bridge_health", lambda server: None)
    out_stem = tmp_path / "mydata"                 # no .json — a documented param shape
    written = tmp_path / "mydata.json"             # what interaction_crawl actually writes
    graph = sample_interaction_graph_path.read_text()

    def _write():
        written.write_text(graph)

    proc = _FakeProc(stderr_lines=[b"! done\n"], returncode=0, completes=True,
                     on_write=_write)
    rec = {}

    result = asyncio.run(mcp_server._crawl_impl(
        "https://example.test/dashboard", out_path=str(out_stem),
        _subprocess_exec=_fake_exec_factory(proc, rec)))

    # argv carries the STEM; the returned output_path is the real .json the crawler wrote.
    assert "--out" in rec["argv"] and str(out_stem) in rec["argv"]
    assert result["status"] == "ok"
    assert result["output_path"] == str(written)
    assert result["summary"]["graph_kind"] == "interaction"
    assert "resource_uri" not in result          # custom path -> no host resource


def test_crawl_default_cache_has_resource_uri(sample_interaction_graph_path,
                                              isolated_cache_home, monkeypatch):
    """Default out_path (None) -> writes the host cache, so resource_uri IS present
    and output_path is that cache file (Fix 2, complement of the custom-path case)."""
    monkeypatch.setattr(mcp_server, "_bridge_health", lambda server: None)
    cache_file = cache_store.cache_path("example.test")
    graph = sample_interaction_graph_path.read_text()

    def _write():
        os.makedirs(os.path.dirname(cache_file), exist_ok=True)
        with open(cache_file, "w") as f:
            f.write(graph)

    proc = _FakeProc(stderr_lines=[b"! done\n"], returncode=0, completes=True,
                     on_write=_write)
    rec = {}

    result = asyncio.run(mcp_server._crawl_impl(
        "https://example.test/dashboard",          # out_path defaults to None
        _subprocess_exec=_fake_exec_factory(proc, rec)))

    assert result["status"] == "ok"
    assert result["output_path"] == cache_file
    assert result["resource_uri"] == "graph://example.test"


# --- _bridge_health real branches (Fix 4) ------------------------------------

class _FakeCompleted:
    """Stand-in for subprocess.CompletedProcess (only the fields _bridge_health reads)."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _patch_run(monkeypatch, *, result=None, exc=None, which="/usr/bin/pinchtab"):
    monkeypatch.setattr(mcp_server.shutil, "which", lambda name: which)

    def _fake_run(*args, **kwargs):
        if exc is not None:
            raise exc
        return result

    monkeypatch.setattr(mcp_server.subprocess, "run", _fake_run)


def test_bridge_health_pinchtab_not_on_path(monkeypatch):
    # on PATH per which(), but the exec raises FileNotFoundError -> bridge_unavailable.
    _patch_run(monkeypatch, exc=FileNotFoundError())
    out = mcp_server._bridge_health(mcp_server.DEFAULT_SERVER)
    assert out["status"] == "bridge_unavailable"
    assert out["reason"] == "pinchtab_not_on_path"


def test_bridge_health_no_token(monkeypatch):
    _patch_run(monkeypatch, result=_FakeCompleted(returncode=1, stderr="missing auth token"))
    out = mcp_server._bridge_health("http://localhost:9871")
    assert out["status"] == "bridge_no_token"
    assert out["reason"] == "no_token_configured"
    assert out["server"] == "http://localhost:9871"


def test_bridge_health_unreachable_nonzero(monkeypatch):
    _patch_run(monkeypatch, result=_FakeCompleted(returncode=2, stderr="connection refused"))
    out = mcp_server._bridge_health("http://localhost:9871")
    assert out["status"] == "bridge_unreachable"
    assert out["reason"] == "health_failed"
    assert out["detail"] == "connection refused"


def test_bridge_health_unreachable_timeout(monkeypatch):
    _patch_run(monkeypatch,
               exc=mcp_server.subprocess.TimeoutExpired(cmd=["pinchtab"], timeout=10))
    out = mcp_server._bridge_health("http://localhost:9871")
    assert out["status"] == "bridge_unreachable"
    assert out["reason"] == "health_timeout"


def test_bridge_health_healthy_returns_none(monkeypatch):
    _patch_run(monkeypatch, result=_FakeCompleted(returncode=0, stdout="ok"))
    assert mcp_server._bridge_health("http://localhost:9871") is None


def test_bridge_health_rc0_but_connection_refused(monkeypatch):
    # The real `pinchtab health` CLI exits 0 even when the bridge is DOWN,
    # printing the failure to stdout. rc==0 alone must NOT be read as healthy.
    _patch_run(monkeypatch, result=_FakeCompleted(
        returncode=0,
        stdout='Request failed: Get "http://localhost:9871/health": '
               "dial tcp 127.0.0.1:9871: connect: connection refused"))
    out = mcp_server._bridge_health("http://localhost:9871")
    assert out is not None, "a down bridge that exits 0 must still be caught"
    assert out["status"] == "bridge_unreachable"
    assert out["reason"] == "health_no_connect"


def test_bridge_health_which_missing(monkeypatch):
    # which() returns None -> the first guard fires before any subprocess.run.
    monkeypatch.setattr(mcp_server.shutil, "which", lambda name: None)
    out = mcp_server._bridge_health(mcp_server.DEFAULT_SERVER)
    assert out["status"] == "bridge_unavailable"
    assert out["reason"] == "pinchtab_not_on_path"


# --- ask_howto live / subprocess path (Fix 5) --------------------------------

def test_ask_howto_live_updated(sample_interaction_graph_path, isolated_cache_home,
                                monkeypatch):
    """Cache MISS -> live run whose write-back seeds the cache, so the post-live
    api.howto re-query returns ok -> cache_state == 'updated'."""
    monkeypatch.setattr(mcp_server, "_bridge_health", lambda server: None)
    cache_file = cache_store.cache_path("example.test")
    graph = json.loads(sample_interaction_graph_path.read_text())

    def _write():                                  # the fake subprocess writes the cache
        cache_store.atomic_write("example.test", graph)

    proc = _FakeProc(stdout_lines=[b"live discovery done\n"], returncode=0,
                     completes=True, on_write=_write)
    rec = {}

    result = asyncio.run(mcp_server._ask_howto_impl(
        "https://example.test/dashboard", "create role",
        _subprocess_exec=_fake_exec_factory(proc, rec)))

    assert rec["called"] is True                   # took the live path (cache was empty)
    assert result["cache_state"] == "updated"
    assert result["status"] == "ok"
    assert result["results"][0]["trigger_label"] == "Create Role"


def test_ask_howto_live_failed(isolated_cache_home, monkeypatch):
    """Cache MISS -> live run exits nonzero and writes nothing -> cache_state ==
    'live_failed' with the subprocess returncode surfaced."""
    monkeypatch.setattr(mcp_server, "_bridge_health", lambda server: None)
    proc = _FakeProc(stdout_lines=[b"boom\n"], returncode=1, completes=True)
    rec = {}

    result = asyncio.run(mcp_server._ask_howto_impl(
        "https://example.test/dashboard", "create role",
        _subprocess_exec=_fake_exec_factory(proc, rec)))

    assert rec["called"] is True
    assert result["cache_state"] == "live_failed"
    assert result["returncode"] == 1
    assert result["status"] == "no_cache_for_host"


# --- perform tool (resolve offline + dry-run: no bridge, no subprocess) -------

_PERFORM_GRAPH = {
    "meta": {"host": "app.test"},
    "states": [
        {"id": "s0", "url": "https://app.test/home", "depth": 0},
        {"id": "s1", "url": "https://app.test/reports", "depth": 1},
    ],
    "state_index": {},
    "edges": [{"from": "s0", "to": "s1", "label": "Reports", "selector": "#nav",
               "kind": "link"}],
    "triggers": [
        {"label": "Download report", "state": "s1", "path": [], "kind": "download",
         "selector": None, "href": "https://app.test/files/q3.pdf", "form": None},
    ],
}


def test_perform_dry_run_needs_no_bridge(tmp_path, monkeypatch):
    p = tmp_path / "g.json"
    p.write_text(json.dumps(_PERFORM_GRAPH))
    # If the tool wrongly reached the bridge, this would flip the result to live_failed.
    monkeypatch.setattr(mcp_server, "_bridge_health",
                        lambda server: {"status": "bridge_unavailable"})

    result = asyncio.run(mcp_server.perform(
        goal="download report", graph=str(p), start="https://app.test/home", dry_run=True))

    assert result["status"] == "ok"
    assert result["action_kind"] == "download"
    assert result["download_url"] == "https://app.test/files/q3.pdf"
    assert all(s["status"] == "dry-run" for s in result["steps"])
    assert any(s["line"].startswith("pinchtab download ") for s in result["steps"])


def test_perform_no_match_returns_before_bridge(tmp_path):
    p = tmp_path / "g.json"
    p.write_text(json.dumps(_PERFORM_GRAPH))
    result = asyncio.run(mcp_server.perform(goal="frobnicate", graph=str(p)))
    assert result["status"] == "no_match"


# --- propose_flow: the flow-authoring agent's ONLY verb (and it is PURE) ------

def _registered_tool_names():
    return sorted(t.name for t in asyncio.run(mcp_server.mcp.list_tools()))


def test_propose_flow_ok_echoes_doc_and_verdict():
    doc = {"name": "invoices", "host": "app.test",
           "steps": [{"op": "goto", "url": "https://app.test/invoices"}]}
    out = mcp_server.propose_flow(doc, note="first draft")
    assert out["status"] == "ok"
    assert out["name"] == "invoices" and out["steps"] == 1
    assert out["capabilities"] == {"allow_submit": False, "allow_download": True,
                                   "allow_upload": False}
    assert out["doc"] == doc              # the WHOLE document is echoed back
    assert out["note"] == "first draft"


def test_propose_flow_invalid_still_echoes_the_doc():
    # an invalid draft must still come back with the document + the reason, so the UI can
    # render the broken doc and the model can fix the reported path.
    out = mcp_server.propose_flow({"name": "x", "steps": [{"op": "nope"}]})
    assert out["status"] == "invalid"
    assert out["path"] == "steps[0]" and "nope" in out["error"]
    assert out["doc"] == {"name": "x", "steps": [{"op": "nope"}]}
    assert out["note"] is None


def test_propose_flow_is_pure_no_disk_no_subprocess(monkeypatch, isolated_cache_home):
    # SAFETY: propose_flow validates and echoes — it must touch NOTHING. Poison every I/O
    # primitive it could reach; a single filesystem write or subprocess spawn fails here.
    import builtins
    import subprocess as _sp

    def boom(*a, **k):
        raise AssertionError("propose_flow performed I/O: %r %r" % (a, k))

    monkeypatch.setattr(builtins, "open", boom)
    monkeypatch.setattr(os, "replace", boom)
    monkeypatch.setattr(os, "makedirs", boom)
    monkeypatch.setattr(os, "remove", boom)
    monkeypatch.setattr(_sp, "Popen", boom)
    monkeypatch.setattr(_sp, "run", boom)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)

    out = mcp_server.propose_flow(
        {"name": "f", "steps": [{"op": "goto", "url": "https://x.test/"}]}, note="n")
    assert out["status"] == "ok"

    # …and nothing landed in the (isolated) home dir.
    assert not any(os.scandir(str(isolated_cache_home)))


def test_no_flow_save_or_run_tool_exists_anywhere():
    # THE authority invariant: the MCP surface exposes exactly ONE flow verb, and it is
    # the pure proposer. There is deliberately no save/update/delete/run flow tool, so the
    # chat agent has NO code path to persist or execute a flow — only the human's Save/Run
    # buttons can. If a new flow tool is ever added, this test must be the thing that
    # stops it.
    names = _registered_tool_names()
    flow_tools = [n for n in names if "flow" in n]
    assert flow_tools == ["propose_flow"]
    for forbidden in ("save_flow", "create_flow", "update_flow", "delete_flow",
                      "run_flow", "flow_run", "execute_flow"):
        assert forbidden not in names
