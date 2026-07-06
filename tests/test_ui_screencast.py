"""Tests for pinchtab_webgraph.ui.screencast + the /ws/screencast WebSocket route.

Guarded by importorskip("websockets") so a base run without the UI extra skips
cleanly. The pure logic (discover_page_target, relay_screencast, build_chrome_argv)
and the best-effort no-op branches (attach_and_login) are exercised with SCRIPTED
fakes — no real Chrome, no real bridge, no network. ONE test (guarded on the presence
of google-chrome) launches a REAL headless Chrome end-to-end and proves a real JPEG
frame flows through the relay.

Layout mirrors test_ui_chat.py: importorskip, asyncio.run (NO pytest-asyncio in this
repo), TestClient(...).websocket_connect(...), monkeypatching the open_live_session
seam.
"""
import asyncio
import base64
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from contextlib import asynccontextmanager

import pytest

pytest.importorskip("websockets")

from pinchtab_webgraph.ui import screencast


# --- fakes -------------------------------------------------------------------

class FakeCDPWebSocket:
    """A scripted CDP socket: recv() yields queued strings, then raises `raise_exc`.

    Duck-types the {async send(str), async recv()->str, async close()} surface
    relay_screencast + open_live_session depend on.
    """

    def __init__(self, incoming, raise_exc=None):
        self.sent = []
        self._incoming = list(incoming)
        self._raise = raise_exc or StopAsyncIteration()
        self.closed = False

    async def send(self, s):
        self.sent.append(s)

    async def recv(self):
        if self._incoming:
            return self._incoming.pop(0)
        raise self._raise

    async def close(self):
        self.closed = True


class FakeConnectionClosed(Exception):
    """Stand-in for websockets.ConnectionClosed (a plain Exception subclass)."""


def _screencast_frame_msg(data="BASE64DATA", session_id="sess-1", width=800, height=600):
    return json.dumps({
        "method": "Page.screencastFrame",
        "params": {"data": data,
                   "metadata": {"deviceWidth": width, "deviceHeight": height},
                   "sessionId": session_id},
    })


# --- discover_page_target -----------------------------------------------------

def test_discover_page_target_picks_page_over_others():
    targets = [
        {"type": "background_page", "url": "chrome://x", "webSocketDebuggerUrl": "w0"},
        {"type": "page", "url": "https://example.test/", "webSocketDebuggerUrl": "w1"},
    ]
    t = screencast.discover_page_target(targets)
    assert t["webSocketDebuggerUrl"] == "w1"


def test_discover_page_target_prefers_url_prefix():
    targets = [
        {"type": "page", "url": "https://other.test/", "webSocketDebuggerUrl": "w1"},
        {"type": "page", "url": "https://example.test/home", "webSocketDebuggerUrl": "w2"},
    ]
    t = screencast.discover_page_target(targets, prefer_url="https://example.test/")
    assert t["webSocketDebuggerUrl"] == "w2"


def test_discover_page_target_falls_back_to_first_page():
    targets = [
        {"type": "page", "url": "https://a.test/", "webSocketDebuggerUrl": "w1"},
        {"type": "page", "url": "https://b.test/", "webSocketDebuggerUrl": "w2"},
    ]
    # prefer_url matches nothing -> first page.
    t = screencast.discover_page_target(targets, prefer_url="https://z.test/")
    assert t["webSocketDebuggerUrl"] == "w1"


def test_discover_page_target_none_when_no_page():
    targets = [{"type": "worker", "url": "x", "webSocketDebuggerUrl": "w0"}]
    assert screencast.discover_page_target(targets) is None
    assert screencast.discover_page_target([]) is None
    assert screencast.discover_page_target(None) is None


def test_home_url_for():
    assert screencast.home_url_for("example.test") == "https://example.test/"


# --- relay_screencast ---------------------------------------------------------

def test_relay_sends_enable_start_and_acks_frame():
    ws = FakeCDPWebSocket([_screencast_frame_msg()])
    frames = []

    async def emit(f):
        frames.append(f)

    asyncio.run(screencast.relay_screencast(ws, emit=emit))

    sent = [json.loads(s) for s in ws.sent]
    methods = [m["method"] for m in sent]
    # Page.enable then Page.startScreencast are the first two commands.
    assert methods[0] == "Page.enable"
    assert methods[1] == "Page.startScreencast"
    start_params = sent[1]["params"]
    assert start_params["format"] == "jpeg"
    assert start_params["quality"] == 70
    assert start_params["maxWidth"] == 1600
    assert start_params["everyNthFrame"] == 1
    # ids are incrementing.
    assert sent[0]["id"] == 1 and sent[1]["id"] == 2

    # the frame was ack'd with the matching sessionId.
    acks = [m for m in sent if m["method"] == "Page.screencastFrameAck"]
    assert acks and acks[0]["params"]["sessionId"] == "sess-1"

    # emitted status(live) -> frame -> stopped.
    types = [f["type"] for f in frames]
    assert types == ["status", "frame", "stopped"]
    assert frames[0]["state"] == "live"
    assert frames[1]["data"] == "BASE64DATA"
    assert frames[1]["metadata"]["deviceWidth"] == 800


def test_relay_stops_on_connection_closed():
    ws = FakeCDPWebSocket([], raise_exc=FakeConnectionClosed())
    frames = []

    async def emit(f):
        frames.append(f)

    asyncio.run(screencast.relay_screencast(ws, emit=emit))
    # no frames arrived: just status(live) then stopped.
    assert [f["type"] for f in frames] == ["status", "stopped"]


def test_relay_stops_on_stop_async_iteration():
    ws = FakeCDPWebSocket([], raise_exc=StopAsyncIteration())
    frames = []

    async def emit(f):
        frames.append(f)

    asyncio.run(screencast.relay_screencast(ws, emit=emit))
    assert [f["type"] for f in frames] == ["status", "stopped"]


# --- open_cdp_websocket: no websockets package --------------------------------

def test_open_cdp_websocket_no_package(monkeypatch):
    monkeypatch.setitem(sys.modules, "websockets", None)
    with pytest.raises(screencast.ScreencastUnavailable) as ei:
        asyncio.run(screencast.open_cdp_websocket("ws://127.0.0.1:1/devtools/page/x"))
    assert ei.value.reason == "no_websockets_package"


# --- attach_and_login: best-effort no-op branches -----------------------------

def test_attach_and_login_no_routing():
    def fake_routing(host):
        return None

    res = asyncio.run(screencast.attach_and_login(
        9222, "example.test", bridge_url="http://localhost:9871",
        vault_get_routing=fake_routing))
    assert res == {"authenticated": False, "reason": "no_credential"}


def test_attach_and_login_no_bridge():
    def fake_routing(host):
        return {"url": "https://example.test/login", "username": "u"}

    res = asyncio.run(screencast.attach_and_login(
        9222, "example.test", bridge_url=None, vault_get_routing=fake_routing))
    assert res == {"authenticated": False, "reason": "no_bridge"}


def test_attach_and_login_bridge_unreachable(monkeypatch):
    def fake_routing(host):
        return {"url": "https://example.test/login", "username": "u"}

    def boom(*a, **k):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    res = asyncio.run(screencast.attach_and_login(
        9222, "example.test", bridge_url="http://localhost:9871",
        vault_get_routing=fake_routing))
    assert res == {"authenticated": False, "reason": "attach_failed"}


def _fake_routing(host):
    return {"url": "https://example.test/login", "username": "u"}


def test_attach_and_login_success_reaches_login(monkeypatch):
    # attach succeeds and login.ensure_logged_in returns truthy -> authenticated.
    import pinchtab_webgraph.login as pwlogin
    monkeypatch.setattr(screencast, "fetch_json",
                        lambda *a, **k: {"webSocketDebuggerUrl": "ws://127.0.0.1/x"})
    monkeypatch.setattr(screencast, "_post_attach", lambda *a, **k: True)
    monkeypatch.setattr(pwlogin, "ensure_logged_in", lambda *a, **k: True)
    res = asyncio.run(screencast.attach_and_login(
        9222, "example.test", bridge_url="http://localhost:9871",
        vault_get_routing=_fake_routing))
    assert res == {"authenticated": True, "reason": None}


def test_attach_and_login_swallows_systemexit_from_login(monkeypatch):
    # login.py raises SystemExit (a BaseException, NOT Exception) when the keyring has
    # no password / no password field is detected. attach_and_login MUST catch it and
    # degrade to login_failed — never let it escape into the WS route.
    import pinchtab_webgraph.login as pwlogin

    def raise_systemexit(*a, **k):
        raise SystemExit("no password in keyring")

    monkeypatch.setattr(screencast, "fetch_json",
                        lambda *a, **k: {"webSocketDebuggerUrl": "ws://127.0.0.1/x"})
    monkeypatch.setattr(screencast, "_post_attach", lambda *a, **k: True)
    monkeypatch.setattr(pwlogin, "ensure_logged_in", raise_systemexit)
    res = asyncio.run(screencast.attach_and_login(
        9222, "example.test", bridge_url="http://localhost:9871",
        vault_get_routing=_fake_routing))
    assert res == {"authenticated": False, "reason": "login_failed"}


# --- find_chrome_binary / build_chrome_argv -----------------------------------

def test_build_chrome_argv_shape_and_no_remote_address():
    argv = screencast.build_chrome_argv(
        "/usr/bin/google-chrome", 12345, "/tmp/profile", url="https://example.test/")
    assert argv[0] == "/usr/bin/google-chrome"
    assert "--headless=new" in argv
    assert "--remote-debugging-port=12345" in argv
    assert "--user-data-dir=/tmp/profile" in argv
    assert "https://example.test/" in argv
    # HARD SECURITY INVARIANT: CDP must stay loopback-only — no address flag anywhere.
    assert not any(a.startswith("--remote-debugging-address") for a in argv)


def test_build_chrome_argv_headless_false_omits_flag():
    argv = screencast.build_chrome_argv(
        "/usr/bin/google-chrome", 1, "/tmp/p", headless=False)
    assert "--headless=new" not in argv


def test_find_chrome_binary_uses_which(monkeypatch):
    calls = []

    def fake_which(name):
        calls.append(name)
        return "/usr/bin/" + name if name == "google-chrome" else None

    monkeypatch.setattr(shutil, "which", fake_which)
    assert screencast.find_chrome_binary() == "/usr/bin/google-chrome"


def test_find_chrome_binary_none(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert screencast.find_chrome_binary() is None


# --- WebSocket route ----------------------------------------------------------

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from pinchtab_webgraph.ui import server as ui_server  # noqa: E402

ws_client = TestClient(ui_server.app)


def test_ws_screencast_streams_scripted_frames(monkeypatch):
    # A fake open_live_session yields a LiveSession whose cdp_ws scripts one frame;
    # the REAL relay_screencast drives it, so the route emits status/frame/stopped.
    @asynccontextmanager
    async def fake_open(host, *, bridge_url=None):
        ws = FakeCDPWebSocket([_screencast_frame_msg(data="ABCD")])
        yield screencast.LiveSession(
            cdp_ws=ws, auth={"authenticated": False, "reason": "no_credential"})

    monkeypatch.setattr(ui_server.screencast, "open_live_session", fake_open)

    with ws_client.websocket_connect("/ws/screencast?host=example.test") as ws:
        f1 = ws.receive_json()  # route's own status frame (auth outcome)
        assert f1["type"] == "status"
        assert f1["authenticated"] is False
        assert f1["reason"] == "no_credential"
        f2 = ws.receive_json()  # relay's status(live)
        assert f2 == {"type": "status", "state": "live", "width": None, "height": None}
        f3 = ws.receive_json()  # the frame
        assert f3["type"] == "frame"
        assert f3["data"] == "ABCD"
        f4 = ws.receive_json()  # stopped
        assert f4 == {"type": "stopped"}


def test_ws_screencast_invalid_host_error_and_close():
    with ws_client.websocket_connect("/ws/screencast?host=bad%20host") as ws:
        frame = ws.receive_json()
        assert frame["type"] == "error"
        assert frame["status"] == "invalid_host"


def test_ws_screencast_unavailable_maps_to_error(monkeypatch):
    @asynccontextmanager
    async def boom(host, *, bridge_url=None):
        raise screencast.ScreencastUnavailable("no_chrome_binary", "no chrome")
        yield  # pragma: no cover

    monkeypatch.setattr(ui_server.screencast, "open_live_session", boom)
    with ws_client.websocket_connect("/ws/screencast?host=example.test") as ws:
        frame = ws.receive_json()
        assert frame["type"] == "error"
        assert frame["status"] == "screencast_unavailable"
        assert frame["reason"] == "no_chrome_binary"


# --- REAL headless-Chrome integration ----------------------------------------

@pytest.mark.skipif(shutil.which("google-chrome") is None,
                    reason="google-chrome not installed")
def test_real_chrome_emits_jpeg_frame():
    """Launch a REAL headless Chrome painting a red page, screencast one frame.

    Proves the whole pipeline: launch -> wait_for_cdp_ready -> /json/list ->
    discover_page_target -> open_cdp_websocket -> relay_screencast -> a non-empty
    base64 JPEG frame -> terminate_chrome (process dead, temp profile removed).
    """
    data_url = "data:text/html,<body style='background:red'><h1>hi</h1></body>"

    class _Stop(Exception):
        pass

    async def run():
        proc = port = udir = None
        try:
            proc, port, udir = await screencast.launch_chrome(data_url)
            await screencast.wait_for_cdp_ready(port, timeout=10.0)
            targets = screencast.fetch_json(port, "/json/list")
            target = screencast.discover_page_target(targets)
            assert target is not None, "Chrome exposed no page target"
            cdp_ws = await screencast.open_cdp_websocket(target["webSocketDebuggerUrl"])

            frames = []

            async def emit(frame):
                frames.append(frame)
                if frame.get("type") == "frame":
                    raise _Stop()  # one frame is enough; stop the relay.

            try:
                await asyncio.wait_for(
                    screencast.relay_screencast(cdp_ws, emit=emit), timeout=15.0)
            except _Stop:
                pass
            await cdp_ws.close()

            frame_frames = [f for f in frames if f["type"] == "frame"]
            assert frame_frames, "no screencast frame was emitted"
            data = frame_frames[0]["data"]
            assert data, "screencast frame carried no data"
            decoded = base64.b64decode(data)
            assert len(decoded) > 0
            # JPEG magic bytes (screencast format defaults to jpeg).
            assert decoded[:2] == b"\xff\xd8"

            await screencast.terminate_chrome(proc, udir)
            assert proc.poll() is not None, "Chrome process still alive after terminate"
            assert not os.path.exists(udir), "temp profile dir not removed"
        finally:
            # Safety net: idempotent, never raises.
            await screencast.terminate_chrome(proc, udir)

    asyncio.run(run())
