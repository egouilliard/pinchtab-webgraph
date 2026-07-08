"""Tests for pinchtab_webgraph.ui.live_crawl + the /ws/crawl WebSocket route.

Mirrors test_ui_screencast.py: the pure logic (parse_start_url, build_crawl_argv,
parse_progress_line, clamp_*, promote_result) is exercised directly, and the WS route
is driven with a SCRIPTED fake open_crawl_session (a FakeProcess double) — no real
subprocess, no real PinchTab bridge, no network. asyncio.run (NO pytest-asyncio in this
repo), TestClient(...).websocket_connect(...), monkeypatching the open_crawl_session
seam exactly as the screencast tests monkeypatch open_live_session.
"""
import asyncio
import json
import os
import signal
import sys
import threading
from contextlib import asynccontextmanager

import pytest

from pinchtab_webgraph import cache_store
from pinchtab_webgraph.ui import live_crawl


# --- parse_start_url ----------------------------------------------------------

def test_parse_start_url_accepts_http_and_https():
    assert live_crawl.parse_start_url("http://example.test/home") == "example.test"
    assert live_crawl.parse_start_url("https://app.example.com/x?y=1") == "app.example.com"


def test_parse_start_url_rejects_non_http_schemes():
    for bad in ("file:///etc/passwd", "javascript:alert(1)", "ftp://h/x", "data:text/html,x"):
        with pytest.raises(ValueError):
            live_crawl.parse_start_url(bad)


def test_parse_start_url_rejects_missing_host():
    for bad in ("http://", "https:///path", "not a url", ""):
        with pytest.raises(ValueError):
            live_crawl.parse_start_url(bad)


def test_parse_start_url_rejects_bad_host_token():
    # a hostname with a path separator / space can never resolve inside caches_dir().
    with pytest.raises(ValueError):
        live_crawl.parse_start_url("http://ex ample.test/")


# --- build_crawl_argv ---------------------------------------------------------

def test_build_crawl_argv_shape_and_atomic_tokens():
    argv = live_crawl.build_crawl_argv(
        "/usr/bin/python3", start_url="https://app.example.com/home",
        server_url="http://localhost:9871", config_path="/cfg/crawl-config.json",
        out_path="/staging/graph", max_states=60, max_depth=4)
    assert argv[0] == "/usr/bin/python3"
    assert argv[1:3] == ["-m", "pinchtab_webgraph.interaction_crawl"]
    # every token is a SEPARATE list element (never a shell string).
    i = argv.index("--start")
    assert argv[i + 1] == "https://app.example.com/home"
    assert "--server" in argv and argv[argv.index("--server") + 1] == "http://localhost:9871"
    assert argv[argv.index("--out") + 1] == "/staging/graph"
    assert argv[argv.index("--max-states") + 1] == "60"
    assert argv[argv.index("--max-depth") + 1] == "4"
    assert argv[argv.index("--checkpoint-every") + 1] == "5"
    # NOT a shell string: the URL is never concatenated with spaces into one token.
    assert not any(" " in tok and tok != "https://app.example.com/home" for tok in argv)
    assert all(isinstance(tok, str) for tok in argv)


# --- parse_progress_line (on LITERAL captured samples) ------------------------

def test_parse_progress_line_matches_and_extracts_ints():
    line = "· [12 states / 34 visits] depth 2 · https://app.example.com/home (7 controls, 3 items)"
    assert live_crawl.parse_progress_line(line) == {
        "type": "progress", "states": 12, "visits": 34, "depth": 2,
        "url": "https://app.example.com/home", "controls": 7}


def test_parse_progress_line_matches_without_items_tail():
    line = "· [1 states / 1 visits] depth 0 · https://app.example.com/ (5 controls)"
    assert live_crawl.parse_progress_line(line) == {
        "type": "progress", "states": 1, "visits": 1, "depth": 0,
        "url": "https://app.example.com/", "controls": 5}


def test_parse_progress_line_none_for_non_progress_lines():
    # banner / trigger / final-summary / warning lines are NOT progress frames.
    assert live_crawl.parse_progress_line(
        "Interaction crawl from https://app.example.com/home (max 60 states / 1200 visits, depth 4)") is None
    assert live_crawl.parse_progress_line("    ✓ trigger 'Create CAE' + form") is None
    assert live_crawl.parse_progress_line(
        "Wrote /p/graph.json: 12 states, 30 edges, 4 triggers  [stopped: frontier-exhausted]") is None
    assert live_crawl.parse_progress_line("") is None
    assert live_crawl.parse_progress_line("random log noise") is None


# --- clamp_max_states / clamp_max_depth ---------------------------------------

def test_clamp_max_states_bounds_and_default():
    assert live_crawl.clamp_max_states(None) == 60
    assert live_crawl.clamp_max_states("nope") == 60
    assert live_crawl.clamp_max_states(5) == 10          # below floor
    assert live_crawl.clamp_max_states(9999) == 500      # above ceiling
    assert live_crawl.clamp_max_states(120) == 120
    assert live_crawl.clamp_max_states("80") == 80


def test_clamp_max_depth_bounds_and_default():
    assert live_crawl.clamp_max_depth(None) == 4
    assert live_crawl.clamp_max_depth("nope") == 4
    assert live_crawl.clamp_max_depth(0) == 1            # below floor
    assert live_crawl.clamp_max_depth(99) == 8           # above ceiling
    assert live_crawl.clamp_max_depth(3) == 3


# --- promote_result -----------------------------------------------------------

def _valid_graph(host="example.test"):
    return {"meta": {"host": host, "states": 3, "edges": 2, "triggers": 1,
                     "complete": True, "stopped": "frontier-exhausted"},
            "states": [], "state_index": {}, "edges": [], "triggers": []}


def test_promote_result_moves_valid_staging_file(tmp_path, monkeypatch):
    monkeypatch.setenv("PINCHTAB_WEBGRAPH_HOME", str(tmp_path / "home"))
    staging = tmp_path / "staging.json"
    staging.write_text(json.dumps(_valid_graph()))

    meta = live_crawl.promote_result("example.test", str(staging))
    assert meta["states"] == 3 and meta["complete"] is True
    # moved onto the cache path AND gone from staging.
    assert os.path.exists(cache_store.cache_path("example.test"))
    assert not staging.exists()
    # the promoted file is the graph we wrote.
    assert cache_store.load("example.test")["meta"]["host"] == "example.test"


def test_promote_result_none_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("PINCHTAB_WEBGRAPH_HOME", str(tmp_path / "home"))
    assert live_crawl.promote_result("example.test", str(tmp_path / "nope.json")) is None


def test_promote_result_raises_on_corrupt(tmp_path, monkeypatch):
    monkeypatch.setenv("PINCHTAB_WEBGRAPH_HOME", str(tmp_path / "home"))
    staging = tmp_path / "staging.json"
    staging.write_text("{ not json")
    with pytest.raises((OSError, ValueError, json.JSONDecodeError, KeyError)):
        live_crawl.promote_result("example.test", str(staging))
    # a graph with no meta raises KeyError (in the standard tuple).
    staging.write_text(json.dumps({"states": []}))
    with pytest.raises((OSError, ValueError, json.JSONDecodeError, KeyError)):
        live_crawl.promote_result("example.test", str(staging))


# --- resolve_config_path ------------------------------------------------------

def test_resolve_config_path_missing_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("PINCHTAB_CONFIG", raising=False)
    with pytest.raises(live_crawl.CrawlUnavailable) as ei:
        live_crawl.resolve_config_path()
    assert ei.value.reason == "no_config"
    monkeypatch.setenv("PINCHTAB_CONFIG", str(tmp_path / "nope.json"))
    with pytest.raises(live_crawl.CrawlUnavailable) as ei2:
        live_crawl.resolve_config_path()
    assert ei2.value.reason == "no_config"


def test_resolve_config_path_present(monkeypatch, tmp_path):
    cfg = tmp_path / "crawl-config.json"
    cfg.write_text("{}")
    monkeypatch.setenv("PINCHTAB_CONFIG", str(cfg))
    assert live_crawl.resolve_config_path() == str(cfg)


# --- FakeProcess double (mirrors screencast's FakeCDPWebSocket discipline) -----

class _FakeStderr:
    """An async line reader backed by a list; readline() yields bytes then b'' (EOF)."""

    def __init__(self, lines):
        self._lines = list(lines)

    async def readline(self):
        if self._lines:
            return self._lines.pop(0)
        return b""


class FakeProcess:
    """A stand-in for asyncio.subprocess.Process with controllable exit + recorded signals.

    ``exits_on_its_own`` pre-sets the returncode so ``wait()`` returns immediately (the
    happy path). Otherwise ``wait()`` polls until a signal arrives via ``_killpg`` /
    ``send_signal`` (the cancel / disconnect paths). ``terminated`` is a threading.Event
    so a test can observe teardown from outside the app's event loop.
    """

    def __init__(self, stderr_lines=(), returncode=0, pid=424242, exits_on_its_own=False):
        self.stderr = _FakeStderr(stderr_lines)
        self.pid = pid
        self.signals = []
        self.terminated = threading.Event()
        self._returncode = returncode if exits_on_its_own else None

    @property
    def returncode(self):
        return self._returncode

    async def wait(self):
        while self._returncode is None:
            await asyncio.sleep(0.01)
        return self._returncode

    def _killpg(self, sig):
        self.signals.append(sig)
        if self._returncode is None:
            self._returncode = -sig
        self.terminated.set()

    def send_signal(self, sig):
        self._killpg(sig)


def _install_killpg(monkeypatch, proc):
    # Route live_crawl's process-group signalling at the FakeProcess (a fake pid has no
    # real group), so cancel_session's terminate is observable + never touches a real pg.
    monkeypatch.setattr(live_crawl.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(live_crawl.os, "killpg", lambda pgid, sig: proc._killpg(sig))


# --- WebSocket route ----------------------------------------------------------

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from pinchtab_webgraph.ui import server as ui_server  # noqa: E402

ws_client = TestClient(ui_server.app)


def _recv_until_terminal(ws):
    """Read frames until a terminal (done/cancelled/error) frame; return (frames, terminal)."""
    frames = []
    for _ in range(50):
        f = ws.receive_json()
        frames.append(f)
        if f["type"] in ("done", "cancelled") or (
                f["type"] == "error" and f.get("status") != "status"):
            return frames, f
    raise AssertionError("no terminal frame: %r" % frames)


def test_ws_crawl_disabled_by_default(monkeypatch):
    monkeypatch.delenv("PINCHTAB_WEBGRAPH_ENABLE_CRAWL", raising=False)
    with ws_client.websocket_connect("/ws/crawl?url=http://example.test/") as ws:
        f = ws.receive_json()
        assert f["type"] == "error"
        assert f["status"] == "crawl_unavailable"
        assert f["reason"] == "disabled"


def test_ws_crawl_invalid_url(monkeypatch):
    monkeypatch.setenv("PINCHTAB_WEBGRAPH_ENABLE_CRAWL", "1")
    with ws_client.websocket_connect("/ws/crawl?url=file:///etc/passwd") as ws:
        f = ws.receive_json()
        assert f["type"] == "error"
        assert f["status"] == "invalid_url"


def test_ws_crawl_too_many_sessions(monkeypatch):
    monkeypatch.setenv("PINCHTAB_WEBGRAPH_ENABLE_CRAWL", "1")
    ui_server.app.state.live_crawls = live_crawl.MAX_LIVE_CRAWLS
    try:
        with ws_client.websocket_connect("/ws/crawl?url=http://example.test/") as ws:
            f = ws.receive_json()
            assert f["type"] == "error"
            assert f["status"] == "too_many_sessions"
    finally:
        ui_server.app.state.live_crawls = 0


def test_ws_crawl_happy_path_promotes(monkeypatch, tmp_path):
    monkeypatch.setenv("PINCHTAB_WEBGRAPH_ENABLE_CRAWL", "1")
    monkeypatch.setenv("PINCHTAB_WEBGRAPH_HOME", str(tmp_path / "home"))

    staging = tmp_path / "graph.json"
    staging.write_text(json.dumps(_valid_graph("example.test")))

    proc = FakeProcess(stderr_lines=[
        b"Interaction crawl from https://example.test/ (max 60 states / 1200 visits, depth 4)\n",
        b"\xc2\xb7 [1 states / 1 visits] depth 0 \xc2\xb7 http://example.test/ (5 controls)\n",
        b"    \xe2\x9c\x93 trigger 'Create X' + form\n",
    ], exits_on_its_own=True)

    @asynccontextmanager
    async def fake_open(start_url, *, host, max_states=None, max_depth=None):
        yield live_crawl.CrawlSession(process=proc, staging_dir=str(tmp_path),
                                      staging_json_path=str(staging), host=host,
                                      start_url=start_url)

    monkeypatch.setattr(ui_server.live_crawl, "open_crawl_session", fake_open)

    with ws_client.websocket_connect("/ws/crawl?url=http://example.test/") as ws:
        first = ws.receive_json()
        assert first == {"type": "status", "state": "starting",
                         "host": "example.test", "start_url": "http://example.test/"}
        frames, terminal = _recv_until_terminal(ws)
        types = [f["type"] for f in frames]
        assert "progress" in types           # the "· [1 states …]" line parsed
        assert "log" in types                # the banner / trigger lines as log
        assert terminal["type"] == "done"
        assert terminal["host"] == "example.test"
        assert terminal["states"] == 3 and terminal["complete"] is True

    # the graph was promoted into the cache.
    assert os.path.exists(cache_store.cache_path("example.test"))


def test_ws_crawl_client_cancel_terminates(monkeypatch, tmp_path):
    monkeypatch.setenv("PINCHTAB_WEBGRAPH_ENABLE_CRAWL", "1")
    monkeypatch.setenv("PINCHTAB_WEBGRAPH_HOME", str(tmp_path / "home"))

    # a partial staging graph the crawler "wrote" before SIGTERM.
    staging = tmp_path / "graph.json"
    staging.write_text(json.dumps(_valid_graph("example.test")))

    proc = FakeProcess(stderr_lines=[], exits_on_its_own=False)   # wait() blocks until signalled
    _install_killpg(monkeypatch, proc)

    @asynccontextmanager
    async def fake_open(start_url, *, host, max_states=None, max_depth=None):
        yield live_crawl.CrawlSession(process=proc, staging_dir=str(tmp_path),
                                      staging_json_path=str(staging), host=host,
                                      start_url=start_url)

    monkeypatch.setattr(ui_server.live_crawl, "open_crawl_session", fake_open)

    with ws_client.websocket_connect("/ws/crawl?url=http://example.test/") as ws:
        assert ws.receive_json()["type"] == "status"
        ws.send_json({"type": "cancel"})
        _frames, terminal = _recv_until_terminal(ws)
        assert terminal["type"] == "cancelled"
        assert terminal["host"] == "example.test"

    # terminate ran: SIGTERM was delivered to the process group.
    assert proc.terminated.wait(2.0)
    assert signal.SIGTERM in proc.signals


def test_ws_crawl_disconnect_still_terminates(monkeypatch, tmp_path):
    monkeypatch.setenv("PINCHTAB_WEBGRAPH_ENABLE_CRAWL", "1")
    monkeypatch.setenv("PINCHTAB_WEBGRAPH_HOME", str(tmp_path / "home"))

    proc = FakeProcess(stderr_lines=[], exits_on_its_own=False)
    _install_killpg(monkeypatch, proc)

    @asynccontextmanager
    async def fake_open(start_url, *, host, max_states=None, max_depth=None):
        yield live_crawl.CrawlSession(process=proc, staging_dir=str(tmp_path),
                                      staging_json_path=str(tmp_path / "absent.json"),
                                      host=host, start_url=start_url)

    monkeypatch.setattr(ui_server.live_crawl, "open_crawl_session", fake_open)

    with ws_client.websocket_connect("/ws/crawl?url=http://example.test/") as ws:
        assert ws.receive_json()["type"] == "status"
        # abrupt disconnect (no cancel frame): the server must still terminate the process.

    assert proc.terminated.wait(2.0), "subprocess leaked after client disconnect"
    assert signal.SIGTERM in proc.signals


def test_ws_crawl_unavailable_maps_to_error(monkeypatch):
    monkeypatch.setenv("PINCHTAB_WEBGRAPH_ENABLE_CRAWL", "1")

    @asynccontextmanager
    async def boom(start_url, *, host, max_states=None, max_depth=None):
        raise live_crawl.CrawlUnavailable("bridge_unreachable", "no bridge")
        yield  # pragma: no cover

    monkeypatch.setattr(ui_server.live_crawl, "open_crawl_session", boom)
    with ws_client.websocket_connect("/ws/crawl?url=http://example.test/") as ws:
        f = ws.receive_json()
        assert f["type"] == "error"
        assert f["status"] == "crawl_unavailable"
        assert f["reason"] == "bridge_unreachable"


# --- pump_progress / cancel_session / finish_session (direct) ------------------

def test_pump_progress_emits_progress_and_log_then_stops():
    proc = FakeProcess(stderr_lines=[
        b"\xc2\xb7 [2 states / 3 visits] depth 1 \xc2\xb7 http://h/ (4 controls)\n",
        b"some log line\n",
    ], exits_on_its_own=True)
    session = live_crawl.CrawlSession(proc, "/d", "/d/g.json", "h", "http://h/")
    frames = []

    async def emit(f):
        frames.append(f)

    asyncio.run(live_crawl.pump_progress(session, emit=emit))
    assert frames[0]["type"] == "progress" and frames[0]["states"] == 2
    assert frames[1] == {"type": "log", "line": "some log line"}


def test_pump_progress_never_raises_when_emit_fails():
    proc = FakeProcess(stderr_lines=[b"line\n"], exits_on_its_own=True)
    session = live_crawl.CrawlSession(proc, "/d", "/d/g.json", "h", "http://h/")

    async def emit(_f):
        raise RuntimeError("client gone")

    # must return cleanly, never propagate.
    asyncio.run(live_crawl.pump_progress(session, emit=emit))


def test_finish_session_done_from_meta(tmp_path, monkeypatch):
    monkeypatch.setenv("PINCHTAB_WEBGRAPH_HOME", str(tmp_path / "home"))
    staging = tmp_path / "g.json"
    staging.write_text(json.dumps(_valid_graph("h.test")))
    proc = FakeProcess(exits_on_its_own=True)
    session = live_crawl.CrawlSession(proc, str(tmp_path), str(staging), "h.test", "http://h.test/")
    frame = asyncio.run(live_crawl.finish_session(session, cancelled=False))
    assert frame == {"type": "done", "host": "h.test", "states": 3, "edges": 2,
                     "triggers": 1, "complete": True, "stopped": "frontier-exhausted"}


def test_finish_session_crawl_failed_when_no_output(tmp_path, monkeypatch):
    monkeypatch.setenv("PINCHTAB_WEBGRAPH_HOME", str(tmp_path / "home"))
    proc = FakeProcess(exits_on_its_own=True)
    session = live_crawl.CrawlSession(proc, str(tmp_path), str(tmp_path / "absent.json"),
                                      "h.test", "http://h.test/")
    frame = asyncio.run(live_crawl.finish_session(session, cancelled=False))
    assert frame["type"] == "error" and frame["status"] == "crawl_failed"


def test_finish_session_cancelled_promoted(tmp_path, monkeypatch):
    monkeypatch.setenv("PINCHTAB_WEBGRAPH_HOME", str(tmp_path / "home"))
    staging = tmp_path / "g.json"
    staging.write_text(json.dumps(_valid_graph("h.test")))
    proc = FakeProcess(exits_on_its_own=True)
    session = live_crawl.CrawlSession(proc, str(tmp_path), str(staging), "h.test", "http://h.test/")
    frame = asyncio.run(live_crawl.finish_session(session, cancelled=True))
    assert frame["type"] == "cancelled" and frame["promoted"] is True
    assert frame["states"] == 3


def test_finish_session_cancelled_nothing_saved(tmp_path, monkeypatch):
    monkeypatch.setenv("PINCHTAB_WEBGRAPH_HOME", str(tmp_path / "home"))
    proc = FakeProcess(exits_on_its_own=True)
    session = live_crawl.CrawlSession(proc, str(tmp_path), str(tmp_path / "absent.json"),
                                      "h.test", "http://h.test/")
    frame = asyncio.run(live_crawl.finish_session(session, cancelled=True))
    assert frame["type"] == "cancelled" and frame["promoted"] is False
    assert frame["states"] is None
