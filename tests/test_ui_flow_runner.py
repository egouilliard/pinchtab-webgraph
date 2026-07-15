"""Tests for pinchtab_webgraph.ui.flow_runner + the /ws/flows/run WebSocket route.

Mirrors test_ui_live_crawl.py exactly: the pure logic (build_run_argv, parse_frame_line) is
exercised directly, and the WS route is driven with a SCRIPTED fake open_flow_run_session (a
FakeProcess double) — no real subprocess, no real PinchTab bridge, no network. asyncio.run
(NO pytest-asyncio in this repo), TestClient(...).websocket_connect(...), monkeypatching the
open_flow_run_session seam exactly as the crawl tests monkeypatch open_crawl_session.

The ONE exception is test_real_flow_cmd_jsonl_streams_incrementally, which spawns the REAL
`flow_cmd --jsonl` as a DRY RUN (so it needs no bridge and touches nothing). It is the only
thing that can prove `flush=True` actually flushes — a mock cannot.
"""
import asyncio
import json
import os
import signal
import sys
import threading
from contextlib import asynccontextmanager

import pytest

from pinchtab_webgraph.ui import flow_runner, flow_store

HOST = "example.test"


def _doc(name="my-flow", **extra):
    doc = {"name": name, "steps": [{"op": "goto", "url": "https://example.test/x"}]}
    doc.update(extra)
    return doc


# --- build_run_argv (pure) ----------------------------------------------------

def _argv(**kw):
    base = dict(flow_path="/f/flow.json", server_url="http://localhost:9871",
                config_path="/cfg/crawl-config.json")
    base.update(kw)
    return flow_runner.build_run_argv("/usr/bin/python3", **base)


def test_build_run_argv_shape_and_atomic_tokens():
    argv = _argv(host=HOST)
    assert argv[0] == "/usr/bin/python3"
    assert argv[1:5] == ["-m", "pinchtab_webgraph.flow_cmd", "run", "/f/flow.json"]
    assert "--jsonl" in argv
    assert argv[argv.index("--server") + 1] == "http://localhost:9871"
    assert argv[argv.index("--config") + 1] == "/cfg/crawl-config.json"
    assert argv[argv.index("--host") + 1] == HOST
    assert all(isinstance(tok, str) for tok in argv)


def test_build_run_argv_graph_wins_over_host():
    # --host / --graph are mutually exclusive in flow_cmd — an explicit graph drops the host.
    argv = _argv(host=HOST, graph_path="/g/graph.json")
    assert argv[argv.index("--graph") + 1] == "/g/graph.json"
    assert "--host" not in argv


def test_build_run_argv_neither_host_nor_graph():
    # a url-only flow legitimately needs no graph at all.
    argv = _argv()
    assert "--host" not in argv and "--graph" not in argv


def test_build_run_argv_repeats_input_and_skips_none():
    argv = _argv(inputs={"since": "2026-01-01", "limit": 10, "unset": None})
    pairs = [argv[i + 1] for i, tok in enumerate(argv) if tok == "--input"]
    assert sorted(pairs) == ["limit=10", "since=2026-01-01"]      # an unset input is omitted
    # a hostile value stays ONE inert argv token — never a shell fragment.
    argv2 = _argv(inputs={"x": "a; rm -rf /"})
    assert argv2[argv2.index("--input") + 1] == "x=a; rm -rf /"


def test_build_run_argv_grants_are_opt_in_download_is_opt_out():
    # no grant at all: safe by default — no --allow-*, and download (on by default) is kept.
    argv = _argv()
    assert "--allow-submit" not in argv and "--allow-upload" not in argv
    assert "--no-allow-download" not in argv

    argv = _argv(grant={"allow_submit": True, "allow_upload": True, "allow_download": True})
    assert "--allow-submit" in argv and "--allow-upload" in argv
    assert "--no-allow-download" not in argv

    # download must be explicitly WITHDRAWN when the caller does not grant it.
    argv = _argv(grant={"allow_download": False})
    assert "--no-allow-download" in argv
    assert "--allow-submit" not in argv and "--allow-upload" not in argv

    # a falsy submit/upload grant is not a grant.
    argv = _argv(grant={"allow_submit": False, "allow_upload": False})
    assert "--allow-submit" not in argv and "--allow-upload" not in argv


def test_build_run_argv_dry_run_and_scope():
    argv = _argv(dry_run=True, scope="abc123")
    assert "--dry-run" in argv
    assert argv[argv.index("--scope") + 1] == "abc123"
    assert "--dry-run" not in _argv(dry_run=False)
    assert "--scope" not in _argv()


# --- parse_frame_line (pure) --------------------------------------------------

def test_parse_frame_line_valid_json_frames():
    assert flow_runner.parse_frame_line('{"type":"step","op":"goto","status":"ok"}') == {
        "type": "step", "op": "goto", "status": "ok"}
    assert flow_runner.parse_frame_line('{"type":"result","status":"ok"}')["type"] == "result"


def test_parse_frame_line_garbage_becomes_a_log_frame():
    # a line the UI never sees is a line nobody can debug — never dropped silently.
    assert flow_runner.parse_frame_line("Traceback (most recent call last):") == {
        "type": "log", "line": "Traceback (most recent call last):"}
    assert flow_runner.parse_frame_line("{ not json") == {"type": "log", "line": "{ not json"}
    # valid JSON that is not a typed frame object is still a log line, not a bogus frame.
    assert flow_runner.parse_frame_line("[1, 2]")["type"] == "log"
    assert flow_runner.parse_frame_line('{"status":"invalid"}')["type"] == "log"
    assert flow_runner.parse_frame_line("x" * 900)["line"] == "x" * 500     # truncated


# --- server_url / resolve_config_path -----------------------------------------

def test_server_url_is_the_same_bridge_crawl_uses(monkeypatch):
    from pinchtab_webgraph.ui import live_crawl
    monkeypatch.delenv("PINCHTAB_WEBGRAPH_CRAWL_SERVER", raising=False)
    assert flow_runner.server_url() == live_crawl.DEFAULT_CRAWL_SERVER
    monkeypatch.setenv("PINCHTAB_WEBGRAPH_CRAWL_SERVER", "http://bridge:1234")
    assert flow_runner.server_url() == "http://bridge:1234"


def test_resolve_config_path_missing_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("PINCHTAB_CONFIG", raising=False)
    with pytest.raises(flow_runner.FlowRunUnavailable) as ei:
        flow_runner.resolve_config_path()
    assert ei.value.reason == "no_config"
    monkeypatch.setenv("PINCHTAB_CONFIG", str(tmp_path / "nope.json"))
    with pytest.raises(flow_runner.FlowRunUnavailable):
        flow_runner.resolve_config_path()


def test_resolve_config_path_present(monkeypatch, tmp_path):
    cfg = tmp_path / "crawl-config.json"
    cfg.write_text("{}")
    monkeypatch.setenv("PINCHTAB_CONFIG", str(cfg))
    assert flow_runner.resolve_config_path() == str(cfg)


def test_staged_flow_doc_writes_then_cleans_up(isolated_cache_home):
    with flow_runner.staged_flow_doc(_doc()) as path:
        assert json.loads(open(path).read())["name"] == "my-flow"
        staging_dir = os.path.dirname(path)
    assert not os.path.exists(staging_dir)          # always cleaned, on every exit path


# --- FakeProcess double (mirrors test_ui_live_crawl's FakeProcess) ------------

class _FakeStream:
    """An async line reader backed by a list; readline() yields bytes then b'' (EOF)."""

    def __init__(self, lines):
        self._lines = list(lines)

    async def readline(self):
        if self._lines:
            return self._lines.pop(0)
        return b""


class FakeProcess:
    """A stand-in for asyncio.subprocess.Process with controllable exit + recorded signals.

    ``exits_on_its_own`` pre-sets the returncode so ``wait()`` returns immediately (the happy
    path). Otherwise ``wait()`` polls until a signal arrives via ``_killpg`` / ``send_signal``
    (the cancel / disconnect paths). ``terminated`` is a threading.Event so a test can observe
    teardown from outside the app's event loop.
    """

    def __init__(self, stdout_lines=(), stderr_lines=(), returncode=0, pid=424242,
                 exits_on_its_own=False):
        self.stdout = _FakeStream(stdout_lines)
        self.stderr = _FakeStream(stderr_lines)
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
    # Route flow_runner's process-group signalling at the FakeProcess (a fake pid has no real
    # group), so cancel_run_session's terminate is observable + never touches a real pg.
    monkeypatch.setattr(flow_runner.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(flow_runner.os, "killpg", lambda pgid, sig: proc._killpg(sig))


def _session(proc, host=HOST, flow_id="a" * 32, run_id="b" * 32):
    return flow_runner.FlowRunSession(process=proc, host=host, flow_id=flow_id, run_id=run_id)


# --- pump_frames (direct) -----------------------------------------------------

def test_pump_frames_relays_steps_stderr_and_returns_the_result():
    proc = FakeProcess(
        stdout_lines=[b'{"type":"step","op":"goto","status":"ok"}\n',
                      b'not json at all\n',
                      b'{"type":"result","status":"ok","steps":[]}\n'],
        stderr_lines=[b'Traceback (most recent call last):\n'],
        exits_on_its_own=True)
    frames = []

    async def emit(f):
        frames.append(f)

    result = asyncio.run(flow_runner.pump_frames(_session(proc), emit=emit))
    assert result == {"type": "result", "status": "ok", "steps": []}
    kinds = [f["type"] for f in frames]
    assert kinds.count("step") == 1
    assert kinds.count("result") == 1          # the terminal frame IS emitted too
    logs = [f["line"] for f in frames if f["type"] == "log"]
    assert "not json at all" in logs
    # a crash traceback MUST be visible.
    assert "[stderr] Traceback (most recent call last):" in logs


def test_pump_frames_returns_none_when_no_result_printed():
    proc = FakeProcess(stdout_lines=[b'{"type":"step","op":"goto","status":"ok"}\n'],
                       exits_on_its_own=True)

    async def emit(_f):
        pass

    assert asyncio.run(flow_runner.pump_frames(_session(proc), emit=emit)) is None


def test_pump_frames_never_raises_when_emit_fails():
    proc = FakeProcess(stdout_lines=[b'{"type":"step"}\n'], stderr_lines=[b'x\n'],
                       exits_on_its_own=True)

    async def emit(_f):
        raise RuntimeError("client gone")

    asyncio.run(flow_runner.pump_frames(_session(proc), emit=emit))   # must not propagate


def test_cancel_run_session_sigterms_the_group(monkeypatch):
    proc = FakeProcess(exits_on_its_own=False)
    _install_killpg(monkeypatch, proc)
    asyncio.run(flow_runner.cancel_run_session(_session(proc), timeout=1.0))
    assert signal.SIGTERM in proc.signals
    # idempotent — a second cancel on an exited process is a no-op.
    asyncio.run(flow_runner.cancel_run_session(_session(proc), timeout=1.0))
    assert proc.signals == [signal.SIGTERM]


def test_terminate_process_group_is_a_noop_on_exited():
    proc = FakeProcess(exits_on_its_own=True)
    flow_runner.terminate_process_group(proc, signal.SIGTERM)
    flow_runner.terminate_process_group(None, signal.SIGTERM)
    assert proc.signals == []


# --- WebSocket route (scripted fake subprocess) -------------------------------

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from pinchtab_webgraph.ui import server as ui_server  # noqa: E402

ws_client = TestClient(ui_server.app)


def _fake_open(proc):
    @asynccontextmanager
    async def fake(*, flow_path, host, flow_id, run_id, graph_path=None, inputs=None,
                   grant=None, dry_run=False, scope=None):
        fake.calls.append({"flow_path": flow_path, "inputs": inputs, "grant": grant,
                           "dry_run": dry_run, "scope": scope, "host": host})
        yield flow_runner.FlowRunSession(process=proc, host=host, flow_id=flow_id,
                                         run_id=run_id)
    fake.calls = []
    return fake


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("PINCHTAB_WEBGRAPH_ENABLE_FLOWS", "1")


@pytest.fixture
def saved_flow(isolated_cache_home):
    doc = _doc("stream-me", inputs={"since": {"type": "string"}})
    return flow_store.create(HOST, doc)


def _url(flow_id, host=HOST):
    return "/ws/flows/run?host=%s&flow_id=%s" % (host, flow_id)


def test_ws_flow_run_disabled_by_default(monkeypatch, saved_flow):
    monkeypatch.delenv("PINCHTAB_WEBGRAPH_ENABLE_FLOWS", raising=False)
    with ws_client.websocket_connect(_url(saved_flow["id"])) as ws:
        f = ws.receive_json()
        assert f["type"] == "error" and f["status"] == "flow_unavailable"
        assert f["reason"] == "disabled"


def test_ws_flow_run_happy_path(enabled, saved_flow, monkeypatch):
    proc = FakeProcess(
        stdout_lines=[b'{"type":"step","op":"run","status":"started"}\n',
                      b'{"type":"step","op":"goto","status":"ok","url":"https://x/"}\n',
                      b'{"type":"result","status":"ok","flow":"stream-me","dry_run":true,'
                      b'"steps":[{"op":"goto","status":"ok"}],"artifacts":[],'
                      b'"collected":{},"stats":{"steps_executed":1}}\n'],
        exits_on_its_own=True)
    fake = _fake_open(proc)
    monkeypatch.setattr(ui_server.flow_runner, "open_flow_run_session", fake)

    fid = saved_flow["id"]
    with ws_client.websocket_connect(_url(fid)) as ws:
        boot = ws.receive_json()
        assert boot["type"] == "flow" and boot["id"] == fid
        assert boot["name"] == "stream-me"
        assert "since" in boot["inputs"]           # the run form renders from THIS frame

        ws.send_json({"type": "run", "inputs": {"since": "2026-01-01"}, "dry_run": True,
                      "grant": {"allow_download": True}})
        status = ws.receive_json()
        assert status == {"type": "status", "state": "starting", "host": HOST,
                          "flow_id": fid, "run_id": status["run_id"], "dry_run": True}
        run_id = status["run_id"]

        frames = [ws.receive_json() for _ in range(3)]
        assert [f["type"] for f in frames] == ["step", "step", "result"]
        terminal = frames[-1]
        assert terminal["run_id"] == run_id and terminal["status"] == "ok"
        # EXACTLY ONE result frame (the pump's copy was held back, not double-sent).
        assert terminal["stats"]["steps_executed"] == 1

    # the subprocess got the bound inputs + the flow_id as its artifact scope.
    assert fake.calls[0]["inputs"] == {"since": "2026-01-01"}
    assert fake.calls[0]["scope"] == fid
    assert fake.calls[0]["dry_run"] is True
    # persisted BEFORE it was sent: the record exists and carries the terminal payload.
    rec = flow_store.load_run(HOST, fid, run_id)
    assert rec["status"] == "ok" and rec["cancelled"] is False


def test_ws_flow_run_synthesizes_a_result_when_the_process_dies_silently(
        enabled, saved_flow, monkeypatch):
    # stdout closes with NO result line and a traceback on stderr — the honest answer is an
    # error result naming the return code, not a hang.
    proc = FakeProcess(
        stdout_lines=[b'{"type":"step","op":"goto","status":"ok"}\n'],
        stderr_lines=[b'RuntimeError: boom\n'],
        returncode=1, exits_on_its_own=True)
    monkeypatch.setattr(ui_server.flow_runner, "open_flow_run_session", _fake_open(proc))

    fid = saved_flow["id"]
    with ws_client.websocket_connect(_url(fid)) as ws:
        ws.receive_json()                                  # flow bootstrap
        ws.send_json({"type": "run", "inputs": {}, "dry_run": True})
        status = ws.receive_json()
        run_id = status["run_id"]
        frames = []
        for _ in range(20):
            f = ws.receive_json()
            frames.append(f)
            if f["type"] == "result":
                break
        terminal = frames[-1]
        assert terminal["type"] == "result" and terminal["status"] == "error"
        assert "exited with no result (rc=1)" in terminal["detail"]
        # the trail we DID relay is preserved.
        assert terminal["steps"] == [{"op": "goto", "status": "ok"}]
        # the stderr line reached the client.
        assert any(f["type"] == "log" and "[stderr] RuntimeError: boom" in f["line"]
                   for f in frames)

    rec = flow_store.load_run(HOST, fid, run_id)
    assert rec["status"] == "error" and len(rec["steps"]) == 1


def test_ws_flow_run_cancel_terminates_the_process_group(enabled, saved_flow, monkeypatch):
    proc = FakeProcess(stdout_lines=[], exits_on_its_own=False)   # never exits on its own
    _install_killpg(monkeypatch, proc)
    monkeypatch.setattr(ui_server.flow_runner, "open_flow_run_session", _fake_open(proc))

    fid = saved_flow["id"]
    with ws_client.websocket_connect(_url(fid)) as ws:
        ws.receive_json()
        ws.send_json({"type": "run", "inputs": {}, "dry_run": True})
        assert ws.receive_json()["type"] == "status"
        ws.send_json({"type": "cancel"})
        terminal = ws.receive_json()
        assert terminal["type"] == "result" and terminal["status"] == "error"

    assert proc.terminated.wait(2.0)
    assert signal.SIGTERM in proc.signals
    runs = flow_store.list_runs(HOST, fid)
    assert runs[0]["cancelled"] is True        # a cancelled run is recorded AS cancelled


def test_ws_flow_run_disconnect_still_terminates(enabled, saved_flow, monkeypatch):
    proc = FakeProcess(stdout_lines=[], exits_on_its_own=False)
    _install_killpg(monkeypatch, proc)
    monkeypatch.setattr(ui_server.flow_runner, "open_flow_run_session", _fake_open(proc))

    with ws_client.websocket_connect(_url(saved_flow["id"])) as ws:
        ws.receive_json()
        ws.send_json({"type": "run", "inputs": {}, "dry_run": True})
        assert ws.receive_json()["type"] == "status"
        # abrupt disconnect mid-run: an implicit cancel. The process must NOT leak.

    assert proc.terminated.wait(2.0), "flow subprocess leaked after client disconnect"
    assert signal.SIGTERM in proc.signals


def test_ws_flow_run_bad_input_keeps_the_socket_open(enabled, isolated_cache_home,
                                                     monkeypatch):
    rec = flow_store.create(HOST, _doc("needs-since",
                                       inputs={"since": {"type": "string",
                                                         "required": True}}))
    proc = FakeProcess(stdout_lines=[b'{"type":"result","status":"ok","steps":[]}\n'],
                       exits_on_its_own=True)
    monkeypatch.setattr(ui_server.flow_runner, "open_flow_run_session", _fake_open(proc))

    with ws_client.websocket_connect(_url(rec["id"])) as ws:
        ws.receive_json()
        ws.send_json({"type": "run", "inputs": {"nope": 1}, "dry_run": True})
        f = ws.receive_json()
        assert f["type"] == "error" and f["status"] == "invalid_input"
        # the socket STAYS OPEN — the user just fixes the form and presses Run again.
        ws.send_json({"type": "run", "inputs": {"since": "2026-01-01"}, "dry_run": True})
        assert ws.receive_json()["type"] == "status"
        assert ws.receive_json()["type"] == "result"


def test_ws_flow_run_unavailable_maps_to_error(enabled, saved_flow, monkeypatch):
    @asynccontextmanager
    async def boom(**_kw):
        raise flow_runner.FlowRunUnavailable("bridge_unreachable", "no bridge")
        yield  # pragma: no cover

    monkeypatch.setattr(ui_server.flow_runner, "open_flow_run_session", boom)
    with ws_client.websocket_connect(_url(saved_flow["id"])) as ws:
        ws.receive_json()
        ws.send_json({"type": "run", "inputs": {}, "dry_run": False})
        f = ws.receive_json()
        assert f["type"] == "error" and f["status"] == "flow_unavailable"
        assert f["reason"] == "bridge_unreachable"


# --- the REAL subprocess: the one thing a mock cannot prove -------------------

def test_real_flow_cmd_jsonl_streams_incrementally(tmp_path, monkeypatch):
    """Spawn the REAL `flow_cmd run --jsonl --dry-run` and assert frames arrive AS THEY ARE
    PRODUCED, not in one burst at exit.

    A dry run touches nothing — no bridge, no tab, no artifact dir — so this needs no
    infrastructure. It exists because stdout to a PIPE is BLOCK-BUFFERED: without the
    per-line `flush=True` in flow_cmd, every frame would sit in a 4-8KB buffer and the UI
    would see a 10-minute hang followed by an avalanche. The only way to prove the flush is
    to read a real pipe before the process has exited.
    """
    monkeypatch.setenv("PINCHTAB_WEBGRAPH_HOME", str(tmp_path / "home"))
    doc = {"name": "streamer",
           "steps": [{"op": "log", "message": "one"},
                     {"op": "log", "message": "two"},
                     {"op": "log", "message": "three"}]}
    path = tmp_path / "flow.json"
    path.write_text(json.dumps(doc))

    argv = flow_runner.build_run_argv(
        sys.executable, flow_path=str(path), server_url="http://localhost:9871",
        config_path=str(tmp_path / "absent-config.json"), dry_run=True)
    assert "--dry-run" in argv and "--jsonl" in argv

    async def drive():
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True)
        session = flow_runner.FlowRunSession(process=proc, host=HOST, flow_id="a" * 32,
                                             run_id="b" * 32)
        frames, early = [], []

        async def emit(frame):
            frames.append(frame)
            # INCREMENTAL: the process is still alive when this frame reaches us.
            early.append(proc.returncode is None)

        result = await flow_runner.pump_frames(session, emit=emit)
        await proc.wait()
        return frames, early, result, proc.returncode

    frames, early, result, rc = asyncio.run(drive())

    assert rc == 0
    steps = [f for f in frames if f["type"] == "step"]
    results = [f for f in frames if f["type"] == "result"]
    # every declared step (+ the runner's own run/started + run/ok bookends) streamed.
    assert len(steps) >= 3
    assert any(s.get("message") == "two" for s in steps)
    # EXACTLY ONE result frame.
    assert len(results) == 1
    assert result is results[0]
    assert result["status"] == "ok" and result["dry_run"] is True
    # …and it arrived while the process was still running -> the flush is real.
    assert early[0] is True, "the first frame only arrived after exit — flush=True is broken"
