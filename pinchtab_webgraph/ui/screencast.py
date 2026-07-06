#!/usr/bin/env python3
"""Live browser pane: a CDP screencast of a headless Chrome streamed to the UI.

This is the Phase-4 write side of the OPTIONAL web UI: for a selected host we launch
a private headless Chrome, navigate it to the host's home page (best-effort logged in
through login.py), and relay Chrome's ``Page.screencastFrame`` events — base64 JPEG
frames — out over the server's WebSocket to the browser's right pane. Read-only from
the client's side: Phase 4 does NOT wire client->server input/resize (deferred).

MIRRORS chat.py's discipline exactly:
  * stdlib-only at module scope — NO `websockets` import at import time, so server.py
    can `from . import screencast` while the base package stays a pure-stdlib install
    (verified by the base-import cleanliness check);
  * the heavy dep (`websockets`) is imported LAZILY inside the function that needs it,
    and a missing package / missing binary / dead CDP degrades to a structured
    ``ScreencastUnavailable(reason, detail)`` rather than a crash — the twin of
    ``chat.ChatUnavailable``.

TESTABILITY INVARIANT: the relay loop is pure and dependency-injected. The CDP socket
rides in as a duck-typed ``{async send(str), async recv()->str}`` argument, and
``emit`` (the per-frame sink) is a plain injected async callable — so a test drives it
with a scripted fake socket, no real Chrome, no real network.

SECURITY INVARIANTS (must hold):
  * CDP NEVER binds non-loopback — build_chrome_argv NEVER adds
    ``--remote-debugging-address`` (Chrome's default remote-debugging bind is
    127.0.0.1 only). See the comment in build_chrome_argv.
  * The CDP url, the bridge url/token, and the debugging port NEVER appear in any
    frame sent to the client — only frame/status/error dicts leave relay_screencast.
  * attach_and_login is best-effort and wrapped so ANY failure degrades to
    unauthenticated; it NEVER raises and is never load-bearing.
  * terminate_chrome ALWAYS runs in open_live_session's finally — no orphan Chrome
    process, no leaked temp profile.
"""
import asyncio
import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import urllib.request
from contextlib import asynccontextmanager
from dataclasses import dataclass

MAX_LIVE_SESSIONS = 3


class ScreencastUnavailable(Exception):
    """The live browser pane cannot run — a dep/binary is absent, or CDP is dead.

    ``reason`` is one of {"no_websockets_package", "no_chrome_binary",
    "chrome_launch_failed", "cdp_unreachable", "no_page_target"}; ``detail`` is a
    human remedy hint. The WS route turns this into a structured status frame + close,
    never a 500. Mirrors ``chat.ChatUnavailable``.
    """

    def __init__(self, reason, detail):
        super().__init__("%s: %s" % (reason, detail))
        self.reason = reason
        self.detail = detail


_NO_WEBSOCKETS_HINT = (
    "The optional 'websockets' package is not installed. Install the UI extra "
    "(pip install 'pinchtab-webgraph[ui]') to enable the live browser pane.")
_NO_CHROME_HINT = (
    "No Chrome/Chromium binary was found on PATH. Install Google Chrome (or "
    "chromium) to enable the live browser pane; the offline graph + chat work "
    "without it.")
_LAUNCH_FAILED_HINT = (
    "Failed to launch the headless Chrome process for the live browser pane.")
_CDP_UNREACHABLE_HINT = (
    "Could not reach Chrome's DevTools (CDP) endpoint on 127.0.0.1. Chrome may have "
    "failed to start or exited early.")
_NO_PAGE_TARGET_HINT = (
    "Chrome exposed no 'page' DevTools target to screencast.")

_CHROME_CANDIDATES = ("google-chrome", "google-chrome-stable", "chromium",
                      "chromium-browser")


# --- pure ---------------------------------------------------------------------

def discover_page_target(json_list_payload, *, prefer_url=None):
    """Pick a screencastable 'page' target from a CDP /json/list payload. No I/O.

    Keeps only entries with type=="page". If ``prefer_url`` is given, prefers the
    first page whose url startswith it; otherwise returns the first page. None when
    the payload contains no page target.
    """
    pages = [t for t in (json_list_payload or [])
             if isinstance(t, dict) and t.get("type") == "page"]
    if not pages:
        return None
    if prefer_url:
        for t in pages:
            url = t.get("url") or ""
            if url.startswith(prefer_url):
                return t
    return pages[0]


def home_url_for(host):
    """The https home page URL for a host token (host already validated upstream)."""
    return "https://%s/" % host


async def relay_screencast(cdp_ws, *, emit, fmt="jpeg", quality=70, max_width=1600,
                           max_height=1000, every_nth_frame=1):
    """Drive Chrome's screencast over ``cdp_ws`` and stream frames out through ``emit``.

    ``cdp_ws`` is a duck-typed CDP client exposing ``async send(str)`` and
    ``async recv()->str``. ``emit`` is an async callable taking one dict frame — the
    ONLY output side effect (mirrors chat.run_conversation_turn). The frame protocol:
      {"type":"status","state":"live","width":<int|None>,"height":<int|None>}
      {"type":"frame","data":<base64 str>,"metadata":<dict>}   per screencast frame
      {"type":"stopped"}                                       once, when CDP ends

    The CDP url / port are NEVER emitted — only frame/status dicts leave this loop.
    """
    next_id = 1

    async def _cmd(method, params=None):
        nonlocal next_id
        msg = {"id": next_id, "method": method}
        if params is not None:
            msg["params"] = params
        next_id += 1
        await cdp_ws.send(json.dumps(msg))

    await _cmd("Page.enable")
    await _cmd("Page.startScreencast", {
        "format": fmt,
        "quality": quality,
        "maxWidth": max_width,
        "maxHeight": max_height,
        "everyNthFrame": every_nth_frame,
    })
    # width/height are unknown until the first frame's metadata arrives.
    await emit({"type": "status", "state": "live", "width": None, "height": None})

    while True:
        try:
            raw = await cdp_ws.recv()
        except Exception:  # noqa: BLE001
            # ConnectionClosed (a websockets Exception subclass), StopAsyncIteration,
            # a closed OS socket, or an exhausted scripted fake all end the stream.
            break
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if msg.get("method") != "Page.screencastFrame":
            continue
        params = msg.get("params") or {}
        session_id = params.get("sessionId")
        # ACK IMMEDIATELY on receipt — this bounds Chrome's in-flight frame memory;
        # never gate the ack on `emit` (a slow client must not stall Chrome).
        if session_id is not None:
            try:
                await _cmd("Page.screencastFrameAck", {"sessionId": session_id})
            except Exception:  # noqa: BLE001 — socket died between recv and ack
                break
        await emit({"type": "frame", "data": params.get("data"),
                    "metadata": params.get("metadata")})

    await emit({"type": "stopped"})


# --- interactive input: client event -> CDP Input.* command -------------------

_CDP_MOUSE_TYPE = {"mousemoved": "mouseMoved", "mousepressed": "mousePressed",
                   "mousereleased": "mouseReleased"}
_CDP_BUTTON = {0: "left", 1: "middle", 2: "right"}


def build_input_command(frame):
    """Map ONE client input frame to a CDP ``{method, params}`` (no id). Pure.

    Returns None for anything unrecognized (so a bad/unknown frame is a silent no-op,
    never a crash). Coordinates arrive already translated to viewport CSS pixels by the
    front-end. This is the only place raw client input becomes a browser action, so it
    is deliberately a tight allow-list of Input.* methods — no eval, no navigation.
    """
    if not isinstance(frame, dict):
        return None
    kind = frame.get("kind")
    if kind in _CDP_MOUSE_TYPE:
        p = {"type": _CDP_MOUSE_TYPE[kind],
             "x": float(frame.get("x", 0)), "y": float(frame.get("y", 0)),
             "button": _CDP_BUTTON.get(frame.get("button", 0), "left"),
             "buttons": int(frame.get("buttons", 0))}
        if kind != "mousemoved":
            p["clickCount"] = int(frame.get("clickCount", 1))
        return {"method": "Input.dispatchMouseEvent", "params": p}
    if kind == "wheel":
        return {"method": "Input.dispatchMouseEvent",
                "params": {"type": "mouseWheel",
                           "x": float(frame.get("x", 0)), "y": float(frame.get("y", 0)),
                           "deltaX": float(frame.get("dx", 0)),
                           "deltaY": float(frame.get("dy", 0))}}
    if kind == "text":
        text = frame.get("text", "")
        return {"method": "Input.insertText", "params": {"text": str(text)}} if text else None
    if kind in ("keydown", "keyup"):
        return {"method": "Input.dispatchKeyEvent",
                "params": {"type": "keyDown" if kind == "keydown" else "keyUp",
                           "key": str(frame.get("key", "")),
                           "code": str(frame.get("code", "")),
                           "windowsVirtualKeyCode": int(frame.get("keyCode", 0) or 0)}}
    return None


# --- CDP / HTTP glue (lazy imports; raise ScreencastUnavailable) --------------

def fetch_json(port, path="/json/list", timeout=5.0):
    """GET http://127.0.0.1:<port><path> and parse the JSON body (stdlib urllib).

    ScreencastUnavailable("cdp_unreachable") on ANY error (connection refused, bad
    JSON, timeout) — the CDP endpoint is loopback-only by construction.
    """
    url = "http://127.0.0.1:%d%s" % (port, path)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        raise ScreencastUnavailable("cdp_unreachable",
                                    "%s (%s)" % (_CDP_UNREACHABLE_HINT, str(e)[:80]))


async def open_cdp_websocket(ws_url):
    """Connect to a CDP webSocketDebuggerUrl and return the connected client.

    Lazily imports `websockets` -> ScreencastUnavailable("no_websockets_package") on
    ImportError. Returns a connected websockets client (async send/recv).
    """
    try:
        import websockets
        import websockets.asyncio.client as ws_client
    except ImportError:
        raise ScreencastUnavailable("no_websockets_package", _NO_WEBSOCKETS_HINT)
    # max_size=None: screencast frames (base64 JPEG) can exceed the default 1 MiB cap.
    return await ws_client.connect(ws_url, max_size=None)


# --- Chrome lifecycle ---------------------------------------------------------

def find_chrome_binary():
    """The first Chrome/Chromium executable on PATH, or None."""
    for name in _CHROME_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    return None


def pick_free_port():
    """Ask the OS for a free loopback TCP port (bind to :0, read it back)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def build_chrome_argv(binary, port, user_data_dir, headless=True, url=None):
    """Assemble the headless-Chrome argv for a private CDP-enabled instance.

    HARD SECURITY INVARIANT: we NEVER add ``--remote-debugging-address``. Chrome's
    default remote-debugging bind address is 127.0.0.1 (loopback only); adding an
    address flag is the ONLY way to expose CDP off-loopback, so it must never appear
    here. The credential-bearing DevTools endpoint stays reachable from localhost only.
    """
    argv = [binary]
    if headless:
        argv.append("--headless=new")
    argv += [
        "--remote-debugging-port=%d" % port,
        "--user-data-dir=" + user_data_dir,
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-gpu",
        "--hide-scrollbars",
        "--window-size=1600,1000",   # a large viewport so the screencast fills a wide pane crisply
    ]
    if url:
        argv.append(url)
    return argv


async def launch_chrome(url, *, headless=True):
    """Launch a private headless Chrome navigated to ``url``. Returns (proc, port, dir).

    ScreencastUnavailable("no_chrome_binary") when no binary is found;
    ScreencastUnavailable("chrome_launch_failed") if Popen raises OSError. The caller
    (open_live_session) owns terminating the process + removing the profile dir.
    """
    binary = find_chrome_binary()
    if not binary:
        raise ScreencastUnavailable("no_chrome_binary", _NO_CHROME_HINT)
    port = pick_free_port()
    user_data_dir = tempfile.mkdtemp(prefix="pinchtab-webgraph-ui-chrome-")
    argv = build_chrome_argv(binary, port, user_data_dir, headless=headless, url=url)
    try:
        # start_new_session=True puts Chrome in its OWN process group, so teardown can
        # signal the WHOLE tree (browser + renderer/gpu/zygote children) via killpg —
        # killing only the parent PID would orphan children that keep the profile dir
        # busy and leak. See terminate_chrome.
        proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        shutil.rmtree(user_data_dir, ignore_errors=True)
        raise ScreencastUnavailable("chrome_launch_failed",
                                    "%s (%s)" % (_LAUNCH_FAILED_HINT, str(e)[:80]))
    return proc, port, user_data_dir


async def wait_for_cdp_ready(port, timeout=10.0):
    """Poll /json/version until CDP answers, or ScreencastUnavailable("cdp_unreachable")."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while True:
        try:
            # to_thread: fetch_json is a blocking urllib GET — never run it inline in
            # an async function or it stalls the whole event loop.
            await asyncio.to_thread(fetch_json, port, "/json/version", 2.0)
            return
        except ScreencastUnavailable:
            if loop.time() >= deadline:
                raise
            await asyncio.sleep(0.25)


def _signal_group(process, sig):
    """Send `sig` to the process's whole group (falls back to the single process).

    Chrome launches a tree of children (renderer/gpu/zygote); signalling the group
    reaches all of them so none are orphaned to keep the profile dir busy.
    """
    try:
        os.killpg(os.getpgid(process.pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.send_signal(sig)
        except Exception:  # noqa: BLE001
            pass


async def terminate_chrome(process, user_data_dir):
    """Stop Chrome's whole process group (SIGTERM -> SIGKILL) + delete its temp
    profile. NEVER raises."""
    if process is not None:
        try:
            if process.poll() is None:
                _signal_group(process, signal.SIGTERM)
                for _ in range(20):  # up to ~2s for a graceful exit
                    if process.poll() is not None:
                        break
                    await asyncio.sleep(0.1)
                if process.poll() is None:
                    # Headless Chrome routinely ignores SIGTERM; escalate to SIGKILL
                    # (uncatchable) across the group, then reap the leader. wait() cannot
                    # hang here — the kernel delivers SIGKILL unconditionally — so a
                    # bounded blocking wait guarantees poll() != None afterwards.
                    _signal_group(process, signal.SIGKILL)
                    try:
                        process.wait(timeout=5)
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001 — best-effort teardown, never propagate
            pass
    if user_data_dir:
        # Children may take a beat to release open files after the group SIGKILL; a
        # couple of short retries make the profile removal reliable (best-effort).
        for _ in range(5):
            shutil.rmtree(user_data_dir, ignore_errors=True)
            if not os.path.exists(user_data_dir):
                break
            await asyncio.sleep(0.1)


# --- best-effort auth (reuse login.py UNCHANGED) ------------------------------

def _post_attach(bridge_url, ws_dbg):
    """POST /instances/attach to the bridge; True on a 2xx. Sync — run via to_thread.

    The bridge requires ``Authorization: Bearer <token>`` on its API; send it from
    PINCHTAB_TOKEN when set (the same env var login.py/recipe.py read), else the attach
    would 401 and login would silently fall through to unauthenticated.
    """
    body = json.dumps({"name": "pinchtab-webgraph-ui",
                       "cdpUrl": ws_dbg}).encode("utf-8")
    req = urllib.request.Request(
        bridge_url.rstrip("/") + "/instances/attach", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    token = os.environ.get("PINCHTAB_TOKEN")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=10.0) as resp:
        return 200 <= resp.status < 300


async def attach_and_login(port, host, *, bridge_url, vault_get_routing=None):
    """Best-effort: attach the running Chrome to the PinchTab bridge + log it in.

    NEVER raises. Returns {"authenticated": bool, "reason": <str|None>}. Degrades
    silently to unauthenticated when there is no stored credential, no bridge, or any
    step fails (attach/login). Reuses login.ensure_logged_in UNCHANGED.
    """
    if vault_get_routing is None:
        from . import vault
        vault_get_routing = vault.get_routing

    try:
        routing = vault_get_routing(host)
    except Exception:  # noqa: BLE001
        routing = None
    if routing is None:
        return {"authenticated": False, "reason": "no_credential"}
    if not bridge_url:
        return {"authenticated": False, "reason": "no_bridge"}

    # 1. Attach the bridge to THIS Chrome's DevTools endpoint (loopback CDP url).
    # to_thread everywhere: fetch_json + the attach POST are blocking urllib calls.
    try:
        version = await asyncio.to_thread(fetch_json, port, "/json/version")
        ws_dbg = version["webSocketDebuggerUrl"]
        attached = await asyncio.to_thread(_post_attach, bridge_url, ws_dbg)
        if not attached:
            return {"authenticated": False, "reason": "attach_failed"}
    except Exception:  # noqa: BLE001
        return {"authenticated": False, "reason": "attach_failed"}

    # 2. Drive login.py against the just-attached instance (best-effort). Run the
    # blocking, subprocess-driven login flow in a THREAD so it never freezes the event
    # loop (it can take tens of seconds), and catch SystemExit as well as Exception:
    # login._get_password / perform_login raise SystemExit (a BaseException, NOT an
    # Exception) when the keyring has no password or no password field is detected, so
    # a bare `except Exception` would let it escape the WS route.
    try:
        from .. import login
        from . import vault
        ok = await asyncio.to_thread(
            login.ensure_logged_in, vault.config_path(), routing["url"],
            server=bridge_url)
        return {"authenticated": bool(ok), "reason": None}
    except (Exception, SystemExit):  # noqa: BLE001
        return {"authenticated": False, "reason": "login_failed"}


# --- the ONE integration entry point server.py wraps -------------------------

@dataclass
class LiveSession:
    """Everything a live pane needs: the CDP socket + the best-effort auth outcome."""
    cdp_ws: object
    auth: dict
    process: object = None
    user_data_dir: str = None


@asynccontextmanager
async def open_live_session(host, *, bridge_url=None):
    """Launch Chrome for ``host``, best-effort log in, and yield a ready LiveSession.

    Broken out as an @asynccontextmanager so tests can monkeypatch server.py's
    reference with a fake that yields a scripted LiveSession — no real Chrome, no
    network. Raises ScreencastUnavailable when a binary/CDP/page is missing (the WS
    route maps it to a structured close). terminate_chrome ALWAYS runs in `finally`,
    even on an early raise, so no orphan Chrome or leaked temp profile survives.
    """
    if find_chrome_binary() is None:
        raise ScreencastUnavailable("no_chrome_binary", _NO_CHROME_HINT)

    proc = None
    user_data_dir = None
    cdp_ws = None
    try:
        proc, port, user_data_dir = await launch_chrome(home_url_for(host))
        await wait_for_cdp_ready(port)
        auth = await attach_and_login(port, host, bridge_url=bridge_url)
        targets = await asyncio.to_thread(fetch_json, port, "/json/list")
        target = discover_page_target(targets, prefer_url=home_url_for(host))
        if target is None:
            raise ScreencastUnavailable("no_page_target", _NO_PAGE_TARGET_HINT)
        cdp_ws = await open_cdp_websocket(target["webSocketDebuggerUrl"])
        yield LiveSession(cdp_ws=cdp_ws, auth=auth, process=proc,
                          user_data_dir=user_data_dir)
    finally:
        if cdp_ws is not None:
            try:
                await cdp_ws.close()
            except Exception:  # noqa: BLE001 — best-effort socket close
                pass
        await terminate_chrome(proc, user_data_dir)
