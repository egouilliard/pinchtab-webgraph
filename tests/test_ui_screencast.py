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


# --- CdpDispatcher: shared send/recv id-space + request/reply -----------------

class QueueCDPWebSocket:
    """A CDP socket whose recv() blocks on an asyncio.Queue the test feeds.

    Lets a test run relay_screencast concurrently with dispatcher.request() and deliver
    a scripted reply (or a socket-death exception) at a controlled moment.
    """

    def __init__(self):
        self.sent = []
        self._queue = asyncio.Queue()
        self.closed = False

    async def send(self, s):
        self.sent.append(s)

    async def recv(self):
        item = await self._queue.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self):
        self.closed = True

    def feed(self, item):
        self._queue.put_nowait(item)


def test_relay_wraps_raw_ws_in_dispatcher_backcompat():
    # A raw duck-typed cdp_ws (not a dispatcher) must still work unchanged.
    ws = FakeCDPWebSocket([_screencast_frame_msg()])
    frames = []

    async def emit(f):
        frames.append(f)

    asyncio.run(screencast.relay_screencast(ws, emit=emit))
    assert [f["type"] for f in frames] == ["status", "frame", "stopped"]


def test_dispatcher_request_resolved_by_matching_id_not_emitted_as_frame():
    async def go():
        ws = QueueCDPWebSocket()
        dispatcher = screencast.CdpDispatcher(ws)
        frames = []

        async def emit(f):
            frames.append(f)

        relay = asyncio.create_task(screencast.relay_screencast(dispatcher, emit=emit))
        await asyncio.sleep(0)  # let relay send enable/start and reach recv()

        req = asyncio.create_task(
            dispatcher.request("Runtime.evaluate", {"expression": "1"}, timeout=2.0))
        await asyncio.sleep(0)  # let request allocate its id + send

        rid = json.loads(ws.sent[-1])["id"]
        reply = {"id": rid, "result": {"result": {"value": "42"}}}
        ws.feed(json.dumps(reply))
        got = await asyncio.wait_for(req, timeout=2.0)

        ws.feed(StopAsyncIteration())  # end the relay
        await asyncio.wait_for(relay, timeout=2.0)
        return got, frames

    got, frames = asyncio.run(go())
    # the reply resolved the request future...
    assert got == {"id": got["id"], "result": {"result": {"value": "42"}}}
    # ...and was consumed by _resolve, NOT emitted as a screencast frame.
    assert "frame" not in [f["type"] for f in frames]


def test_dispatcher_request_returns_none_when_socket_dies():
    async def go():
        ws = QueueCDPWebSocket()
        dispatcher = screencast.CdpDispatcher(ws)

        async def emit(_f):
            pass

        relay = asyncio.create_task(screencast.relay_screencast(dispatcher, emit=emit))
        await asyncio.sleep(0)
        req = asyncio.create_task(dispatcher.request("X", {}, timeout=5.0))
        await asyncio.sleep(0)
        # socket dies with NO reply — fail_all must resolve the pending future to None
        # (well before the 5s timeout), so request() never hangs.
        ws.feed(StopAsyncIteration())
        got = await asyncio.wait_for(req, timeout=2.0)
        await asyncio.wait_for(relay, timeout=2.0)
        return got

    assert asyncio.run(go()) is None


def test_dispatcher_fail_all_resolves_pending_to_none():
    async def go():
        dispatcher = screencast.CdpDispatcher(FakeCDPWebSocket([]))
        i = dispatcher.alloc_id()
        fut = asyncio.get_event_loop().create_future()
        dispatcher._pending[i] = fut
        dispatcher.fail_all()
        return await fut

    assert asyncio.run(go()) is None


# --- build_locate_expression / build_locate_command: FIXED, injection-safe ----

def test_build_locate_expression_embeds_selector_and_label_as_json_literals():
    # A hostile value must be embedded ONLY as a json.dumps-escaped string literal —
    # never concatenated as executable code.
    hostile = '"; alert(1); const x = "'
    expr = screencast.build_locate_expression(hostile, hostile)
    # embedded as an escaped JSON string literal (present verbatim for both slots)...
    assert json.dumps(hostile) in expr
    # escaping actually happened (the double-quote became \")...
    assert '\\"' in expr
    # ...and the NAIVE, injectable embedding (payload wrapped in raw, unescaped quotes)
    # is NOT present — the string can't be broken out of.
    naive = '"' + hostile + '"'
    assert naive not in expr
    # the FIXED allow-list of interactive elements is baked in (structural, generic).
    assert "[role=button]" in expr and "[role=menuitem]" in expr


def test_build_locate_expression_handles_none_inputs():
    # None selector/label must not crash and must still produce a valid expression.
    expr = screencast.build_locate_expression(None, None)
    assert "document.querySelectorAll" in expr


def test_build_locate_command_shape():
    cmd = screencast.build_locate_command("a.foo", "Click me")
    assert cmd["method"] == "Runtime.evaluate"
    assert cmd["params"]["returnByValue"] is True
    assert cmd["params"]["awaitPromise"] is False
    assert cmd["params"]["expression"] == screencast.build_locate_expression("a.foo", "Click me")


# --- _extract_locate_rect -----------------------------------------------------

def test_extract_locate_rect_found_stringified():
    result = {"result": {"value": json.dumps(
        {"found": True, "x": 1.5, "y": 2, "width": 10, "height": 20})}}
    assert screencast._extract_locate_rect(result) == {
        "x": 1.5, "y": 2.0, "width": 10.0, "height": 20.0}


def test_extract_locate_rect_found_dict_value():
    result = {"result": {"value": {"found": True, "x": 0, "y": 0, "width": 1, "height": 1}}}
    assert screencast._extract_locate_rect(result) == {
        "x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}


def test_extract_locate_rect_none_cases():
    assert screencast._extract_locate_rect(
        {"result": {"value": json.dumps({"found": False})}}) is None
    assert screencast._extract_locate_rect(None) is None
    assert screencast._extract_locate_rect({}) is None
    assert screencast._extract_locate_rect({"result": {}}) is None
    assert screencast._extract_locate_rect({"result": {"value": "not json"}}) is None


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


# --- top_frame_url + relay location frames (live-position awareness) ----------

def _frame_navigated_msg(url="https://site.test/settings", parent_id=None, frame_id="MAIN"):
    frame = {"url": url, "id": frame_id}
    if parent_id is not None:
        frame["parentId"] = parent_id
    return json.dumps({"method": "Page.frameNavigated", "params": {"frame": frame}})


def _within_doc_msg(url="https://site.test/settings?tab=team", frame_id="MAIN"):
    return json.dumps({"method": "Page.navigatedWithinDocument",
                       "params": {"frameId": frame_id, "url": url}})


def test_top_frame_url_returns_main_frame_url():
    msg = json.loads(_frame_navigated_msg("https://site.test/x"))
    assert screencast.top_frame_url(msg) == "https://site.test/x"


def test_top_frame_url_ignores_subframe():
    msg = json.loads(_frame_navigated_msg("https://site.test/iframe", parent_id="F1"))
    assert screencast.top_frame_url(msg) is None


def test_top_frame_url_none_for_other_messages():
    assert screencast.top_frame_url(json.loads(_screencast_frame_msg())) is None
    assert screencast.top_frame_url({"method": "Page.frameNavigated"}) is None  # no frame
    assert screencast.top_frame_url({"method": "Page.frameNavigated",
                                     "params": {"frame": {}}}) is None          # no url
    assert screencast.top_frame_url("not a dict") is None
    # malformed (non-dict) params / frame must not raise, just return None.
    assert screencast.top_frame_url({"method": "Page.frameNavigated",
                                     "params": "oops"}) is None
    assert screencast.top_frame_url({"method": "Page.frameNavigated",
                                     "params": {"frame": "oops"}}) is None


def test_relay_emits_location_on_top_frame_navigation():
    ws = FakeCDPWebSocket([_frame_navigated_msg("https://site.test/settings"),
                           _screencast_frame_msg()])
    frames = []

    async def emit(f):
        frames.append(f)

    asyncio.run(screencast.relay_screencast(ws, emit=emit))

    locs = [f for f in frames if f["type"] == "location"]
    assert locs and locs[0]["url"] == "https://site.test/settings"
    # the screencast frame still flows normally after the location frame.
    assert any(f["type"] == "frame" for f in frames)


# --- SPA soft-navigation tracking (Page.navigatedWithinDocument) --------------

def test_top_frame_id_returns_main_frame_id():
    assert screencast.top_frame_id(json.loads(_frame_navigated_msg(frame_id="F0"))) == "F0"
    # subframe / non-frameNavigated -> None
    assert screencast.top_frame_id(json.loads(_frame_navigated_msg(parent_id="P"))) is None
    assert screencast.top_frame_id(json.loads(_screencast_frame_msg())) is None


def test_navigated_within_document_url_main_frame_match():
    msg = json.loads(_within_doc_msg("https://site.test/settings?tab=team", frame_id="MAIN"))
    assert screencast.navigated_within_document_url(msg, "MAIN") == "https://site.test/settings?tab=team"


def test_navigated_within_document_url_ignores_subframe():
    msg = json.loads(_within_doc_msg("https://site.test/x", frame_id="SUB"))
    assert screencast.navigated_within_document_url(msg, "MAIN") is None


def test_navigated_within_document_url_accepts_when_main_unknown():
    # before any hard load, main_frame_id is None -> accept best-effort.
    msg = json.loads(_within_doc_msg("https://site.test/y", frame_id="ANY"))
    assert screencast.navigated_within_document_url(msg, None) == "https://site.test/y"


def test_navigated_within_document_url_none_for_other_messages():
    assert screencast.navigated_within_document_url(json.loads(_screencast_frame_msg()), "MAIN") is None
    assert screencast.navigated_within_document_url({"method": "Page.navigatedWithinDocument",
                                                     "params": "oops"}, None) is None
    assert screencast.navigated_within_document_url("not a dict", None) is None


def test_relay_emits_location_on_spa_soft_navigation():
    # hard load (learns main frame id) then a pushState soft-nav on the SAME frame.
    ws = FakeCDPWebSocket([
        _frame_navigated_msg("https://site.test/", frame_id="MAIN"),
        _within_doc_msg("https://site.test/settings?tab=team", frame_id="MAIN"),
        _screencast_frame_msg(),
    ])
    frames = []
    asyncio.run(screencast.relay_screencast(ws, emit=lambda f: frames.append(f) or _aw()))
    urls = [f["url"] for f in frames if f["type"] == "location"]
    assert urls == ["https://site.test/", "https://site.test/settings?tab=team"]


def test_relay_ignores_subframe_soft_navigation():
    ws = FakeCDPWebSocket([
        _frame_navigated_msg("https://site.test/", frame_id="MAIN"),
        _within_doc_msg("https://ads.test/frame", frame_id="SUBFRAME"),  # subframe -> ignored
    ])
    frames = []
    asyncio.run(screencast.relay_screencast(ws, emit=lambda f: frames.append(f) or _aw()))
    urls = [f["url"] for f in frames if f["type"] == "location"]
    assert urls == ["https://site.test/"]   # only the hard load, not the subframe soft-nav


async def _aw():
    return None


# --- bootstrap the main frame id + initial position from Page.getFrameTree ----

def test_frame_tree_main_frame_extracts_frame():
    reply = {"id": 3, "result": {"frameTree": {"frame": {"id": "MAIN", "url": "https://s/"}}}}
    assert screencast.frame_tree_main_frame(reply) == {"id": "MAIN", "url": "https://s/"}
    # malformed / missing shapes -> None, never raises.
    assert screencast.frame_tree_main_frame({"id": 3, "result": "oops"}) is None
    assert screencast.frame_tree_main_frame({"id": 3, "result": {"frameTree": {}}}) is None
    assert screencast.frame_tree_main_frame("not a dict") is None


def test_relay_bootstraps_position_and_scopes_soft_navs_from_frame_tree():
    # relay sends Page.enable(1), Page.startScreencast(2), Page.getFrameTree(3); the reply
    # with id==3 seeds the initial position + main frame id, so a subsequent SUBFRAME
    # soft-nav is filtered while a MAIN-frame soft-nav is emitted.
    ws = FakeCDPWebSocket([
        json.dumps({"id": 3, "result": {"frameTree": {"frame": {"id": "MAIN",
                                                                 "url": "https://site.test/dash"}}}}),
        _within_doc_msg("https://ads.test/x", frame_id="SUB"),                     # filtered
        _within_doc_msg("https://site.test/settings?tab=team", frame_id="MAIN"),   # emitted
    ])
    frames = []
    asyncio.run(screencast.relay_screencast(ws, emit=lambda f: frames.append(f) or _aw2()))
    urls = [f["url"] for f in frames if f["type"] == "location"]
    assert urls == ["https://site.test/dash", "https://site.test/settings?tab=team"]


async def _aw2():
    return None
