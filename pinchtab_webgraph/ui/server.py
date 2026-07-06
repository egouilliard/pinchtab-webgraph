#!/usr/bin/env python3
"""FastAPI app serving a minimal front-end + read-only REST over the offline queries.

This is the Phase-1 web binding onto `api.py` (the print-free, dict-returning query
surface) and `cache_store.py` (the per-host interaction-graph cache). It exposes the
SAME structured dicts the CLI / MCP surface returns, over HTTP, plus a static
placeholder UI. Everything is OFFLINE — a cached graph, no browser, no network.

Routing is by URL hostname ONLY — there is deliberately NO `graph=` filesystem-path
query param over HTTP (an unauthenticated open() of arbitrary paths). We lean on
`cache_store.cache_path`'s own `^[A-Za-z0-9._-]+$` guard to reject traversal; the
regex is NOT re-implemented here.

STRUCTURALLY fastapi-free base install: reached only via its own console script
`pinchtab-webgraph-ui`; nothing in the base package imports it (see mcp_server.py
for the same discipline with `mcp`).
"""
import argparse
import asyncio
import json
import os
import threading
import webbrowser

from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import api, cache_store, __version__
from . import chat_backend, screencast, vault

app = FastAPI(title="pinchtab-webgraph UI", version=__version__)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


# --- shared helpers ----------------------------------------------------------

# A structured status -> HTTP code map. Single source of truth: only the three
# resolver statuses get a non-200 code; every structured MISS (no_match,
# unreachable, empty, invalid_args, no_path, …) is a valid 200 answer with the
# status carried in the body — the same contract the CLI/MCP surface uses.
_STATUS_CODE = {
    "invalid_host": 400,
    "no_cache_for_host": 404,
    "invalid_graph": 422,
    "no_credential_for_host": 404,
    "vault_unavailable": 503,
    # NOTE: "invalid_args" is deliberately NOT here. It is an OVERLOADED status — a
    # 200 structured MISS for the read surface (howto with no goal/match), but a 400
    # for a vault PUT with a bad body. The vault PUT route maps it to 400 locally so
    # the read-side contract (test_howto_invalid_args_is_200) is preserved.
}


def _respond(result):
    """Wrap a structured api result in a JSONResponse with the mapped HTTP code."""
    code = _STATUS_CODE.get(result.get("status"), 200) if isinstance(result, dict) else 200
    return JSONResponse(result, status_code=code)


def _resolve_host(host):
    """Resolve a hostname to its cache-file path (host-only; no `graph=` escape hatch).

    # keep in sync with mcp_server.py:host_summary/host_graph and query_cmd.py:_resolve_graph
    Returns (path, None) on success, or (None, error_dict) with a `status` in
    {invalid_host, no_cache_for_host}.
    """
    try:
        path = cache_store.cache_path(host)
    except ValueError:
        return None, {"status": "invalid_host", "host": host}
    if not os.path.exists(path):
        return None, {"status": "no_cache_for_host", "host": host,
                      "caches_dir": cache_store.caches_dir()}
    return path, None


def _call(fn, path, **kwargs):
    """Call an api.* fn, mapping load/parse errors to an invalid_graph status.

    # keep in sync with mcp_server._call / query_cmd._call — cache_store.load and the
    # api resource funcs do NOT try/except, so a corrupt cache would otherwise 500;
    # this converts it to a clean 422.
    """
    try:
        return fn(path, **kwargs)
    except (OSError, ValueError, json.JSONDecodeError, KeyError) as e:
        return {"status": "invalid_graph", "path": path, "error": str(e)}


def _resolve_vault_host(host):
    """Validate the host TOKEN only — unlike _resolve_host there is deliberately no
    filesystem-existence precondition, since "no credential yet" is the normal state
    for the vault write path. Returns None on success, else an invalid_host error dict.
    """
    try:
        cache_store.validate_host(host)
    except ValueError:
        return {"status": "invalid_host", "host": host}
    return None


def _vault_fields(payload):
    """Pull the routing fields out of a PUT body. NEVER logs `payload` — it carries
    the plaintext password, which must travel no further than vault.set_credential."""
    return {
        "url": payload.get("url"),
        "username": payload.get("username"),
        "password": payload.get("password"),
        "userField": payload.get("userField"),
        "passField": payload.get("passField"),
        "submit": payload.get("submit"),
        "successUrl": payload.get("successUrl"),
        "keyringService": payload.get("keyringService"),
    }


# --- routes (all offline; answer from a cached graph) ------------------------

@app.get("/api/health")
def health():
    return {"status": "ok", "version": __version__}


@app.get("/api/hosts")
def hosts():
    """Index of every host with a persisted cache + a cheap summary.

    Each host's summary is computed independently inside its own try/except so one
    corrupt cache never breaks the whole index (its entry carries an `error`).
    # keep in sync with mcp_server.list_cached_hosts.
    """
    out = []
    for h in cache_store.list_hosts():
        entry = {"host": h,
                 "summary_url": "/api/hosts/%s/summary" % h,
                 "graph_url": "/api/hosts/%s/graph" % h,
                 "forms_url": "/api/hosts/%s/forms" % h,
                 "howto_url": "/api/hosts/%s/howto" % h,
                 "content_url": "/api/hosts/%s/content" % h}
        try:
            entry["summary"] = api.graph_summary(cache_store.cache_path(h))
        except (OSError, ValueError, json.JSONDecodeError, KeyError) as e:
            entry["error"] = str(e)
        out.append(entry)
    return {"hosts": out, "caches_dir": cache_store.caches_dir()}


@app.get("/api/hosts/{host}/summary")
def host_summary(host: str):
    path, err = _resolve_host(host)
    if err is not None:
        return _respond(err)
    return _respond(_call(api.graph_summary, path))


@app.get("/api/hosts/{host}/graph")
def host_graph(host: str):
    """The full raw interaction graph for one host (the large payload, on demand)."""
    path, err = _resolve_host(host)
    if err is not None:
        return _respond(err)
    # cache_store.load re-validates the host and re-reads the file; wrap it so a
    # corrupt cache returns invalid_graph (422), not a 500.
    return _respond(_call(lambda p: cache_store.load(host), path))


@app.get("/api/hosts/{host}/forms")
def host_forms(host: str):
    path, err = _resolve_host(host)
    if err is not None:
        return _respond(err)
    return _respond(_call(api.list_forms, path))


@app.get("/api/hosts/{host}/howto")
def host_howto(host: str, goal: str | None = None, start: str | None = None,
               match: str | None = None, all: bool = False):
    path, err = _resolve_host(host)
    if err is not None:
        return _respond(err)
    return _respond(_call(api.howto, path, goal=goal, start=start, match=match, all=all))


@app.get("/api/hosts/{host}/content")
def host_content(host: str):
    path, err = _resolve_host(host)
    if err is not None:
        return _respond(err)
    return _respond(_call(api.list_content, path))


@app.get("/api/hosts/{host}/content/search")
def host_content_search(host: str, text: str = Query(...), start: str | None = None,
                        limit: int = 40):
    path, err = _resolve_host(host)
    if err is not None:
        return _respond(err)
    return _respond(_call(api.find_content, path, text=text, start=start, limit=limit))


# --- vault routes (Phase 2: the credentials write side, feeds login.py) ------
#
# Password discipline: the plaintext secret enters ONLY via the PUT JSON body and
# leaves this process ONLY through vault.set_credential -> keyring.set_password. GET
# and DELETE never carry it; every response body is a masked, has_password-only view.

@app.get("/api/vault/status")
def vault_status():
    """Keyring backend health + where the routing file lives. Never non-200."""
    return {**vault.backend_status(), "config_path": vault.config_path()}


@app.get("/api/vault/credentials")
def vault_credentials():
    """Masked list of every stored credential (routing + has_password). Never non-200."""
    return vault.list_credentials()


@app.get("/api/vault/credentials/{host}")
def vault_get_credential(host: str):
    err = _resolve_vault_host(host)
    if err is not None:
        return _respond(err)
    r = vault.get_routing(host)
    if r is None:
        return _respond({"status": "no_credential_for_host", "host": host})
    return _respond(r)


@app.put("/api/vault/credentials/{host}")
def vault_put_credential(host: str, payload: dict = Body(...)):
    err = _resolve_vault_host(host)
    if err is not None:
        return _respond(err)
    try:
        result = vault.set_credential(host, **_vault_fields(payload))
        return _respond({"status": "ok", **result})
    except ValueError as e:
        # 400 mapped locally (not via _STATUS_CODE) — see the note on invalid_args there.
        return JSONResponse({"status": "invalid_args", "detail": str(e)},
                            status_code=400)
    except vault.VaultUnavailable as e:
        return _respond({"status": "vault_unavailable", "reason": e.reason,
                         "detail": e.detail})


@app.delete("/api/vault/credentials/{host}")
def vault_delete_credential(host: str, delete_secret: bool = True):
    err = _resolve_vault_host(host)
    if err is not None:
        return _respond(err)
    return _respond(vault.delete_credential(host, delete_secret=delete_secret))


# --- chat WebSocket (Phase 3/6: Claude wired to the offline MCP tools) --------
#
# The chat agent drives Claude with the pinchtab-webgraph MCP server's OFFLINE,
# read-only tools (crawl/ask_howto are deliberately excluded — see
# chat.OFFLINE_TOOL_NAMES). TWO backends live behind chat_backend.open_chat_session:
# the Anthropic-API backend (chat.py) and the Claude Code backend (chat_claude_code.py,
# via the Claude Agent SDK). Both emit the SAME frame protocol, so this route and the
# SPA are backend-agnostic. Everything heavy (anthropic/mcp/claude_agent_sdk) is lazy
# inside those modules, so a base install without those extras degrades to a structured
# ChatUnavailable frame rather than a crash.

@app.websocket("/ws/chat")
async def chat_ws(websocket: WebSocket, host: str = Query(...)):
    await websocket.accept()
    try:
        cache_store.validate_host(host)
    except ValueError:
        await websocket.send_json({"type": "error", "status": "invalid_host",
                                   "host": host})
        await websocket.close(code=1008)
        return
    try:
        async with chat_backend.open_chat_session(host) as session:
            while True:
                try:
                    msg = await websocket.receive_json()
                except WebSocketDisconnect:
                    return
                if msg.get("type") != "user_message":
                    continue
                await session.handle(msg.get("text", ""),
                                     emit=websocket.send_json)
    except chat_backend.ChatUnavailable as e:
        await websocket.send_json({"type": "error", "status": "chat_unavailable",
                                   "reason": e.reason, "detail": e.detail})
        await websocket.close(code=1013)
    except WebSocketDisconnect:
        return


# --- live browser pane WebSocket (Phase 4: a CDP screencast of headless Chrome) --
#
# For the selected host we launch a PRIVATE headless Chrome, navigate it to the
# host's home page (best-effort logged in via login.py through the PinchTab bridge),
# and relay Chrome's screencast frames (base64 JPEG) out over this socket. Everything
# heavy (websockets) + every Chrome/CDP failure is lazy/structured inside
# screencast.py, so a base install without those extras / without Chrome degrades to
# a structured ScreencastUnavailable frame rather than a crash. The CDP endpoint is
# loopback-only by construction (see screencast.build_chrome_argv).

# A process-wide cap on concurrently launched Chrome instances. Guarded by app.state
# so it is per-app (a fresh TestClient app starts at 0) and reset cleanly in finally.
app.state.live_sessions = 0


@app.websocket("/ws/screencast")
async def screencast_ws(websocket: WebSocket, host: str = Query(...)):
    await websocket.accept()
    try:
        cache_store.validate_host(host)
    except ValueError:
        await websocket.send_json({"type": "error", "status": "invalid_host",
                                   "host": host})
        await websocket.close(code=1008)
        return

    if app.state.live_sessions >= screencast.MAX_LIVE_SESSIONS:
        await websocket.send_json({"type": "error", "status": "too_many_sessions",
                                   "max": screencast.MAX_LIVE_SESSIONS})
        await websocket.close(code=1013)
        return

    bridge_url = os.environ.get("PINCHTAB_WEBGRAPH_BRIDGE")
    app.state.live_sessions += 1
    try:
        async with screencast.open_live_session(host, bridge_url=bridge_url) as live:
            await websocket.send_json({
                "type": "status", "state": "live",
                "authenticated": live.auth.get("authenticated"),
                "reason": live.auth.get("reason")})
            # Frames stream OUT via a background task; client input events (mouse/key)
            # stream IN on the same socket and are dispatched as CDP Input.* commands on
            # the SAME cdp_ws (websockets serializes concurrent sends). This is what makes
            # the pane a driveable live session, not a passive view.
            # ONE dispatcher owns the cdp_ws id-space, shared by the relay (frames/acks),
            # the input path (Input.* fire-and-forget), and the "Show Me How" locate probe
            # (Runtime.evaluate request/reply). The relay consumes locate replies before
            # they can be mistaken for screencast frames.
            dispatcher = screencast.CdpDispatcher(live.cdp_ws)
            relay = asyncio.create_task(
                screencast.relay_screencast(dispatcher, emit=websocket.send_json))
            try:
                while True:
                    if relay.done():
                        break
                    msg = await websocket.receive_json()
                    if msg.get("type") == "input":
                        cmd = screencast.build_input_command(msg)
                        if cmd is None:
                            continue
                        try:
                            await dispatcher.send_nowait(cmd["method"], cmd.get("params"))
                        except Exception:  # cdp socket died — end the session
                            break
                    elif msg.get("type") == "locate":
                        # Resolve a tour step's on-screen rect so the SPA can draw a
                        # highlight over the live pane. Best-effort: any failure yields a
                        # null rect, never an error.
                        cmd = screencast.build_locate_command(
                            msg.get("selector"), msg.get("label"))
                        try:
                            result = await dispatcher.request(
                                cmd["method"], cmd["params"], timeout=3.0)
                        except Exception:  # noqa: BLE001 — never break the session on locate
                            result = None
                        rect = screencast._extract_locate_rect(result)
                        await websocket.send_json({"type": "located",
                                                   "stepId": msg.get("stepId"),
                                                   "rect": rect})
                    else:
                        continue
            except WebSocketDisconnect:
                pass
            finally:
                relay.cancel()
                try:
                    await relay
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
    except screencast.ScreencastUnavailable as e:
        await websocket.send_json({"type": "error", "status": "screencast_unavailable",
                                   "reason": e.reason, "detail": e.detail})
        await websocket.close(code=1013)
    except WebSocketDisconnect:
        return
    finally:
        app.state.live_sessions -= 1


# Static mount — registered LAST, AFTER every /api route, so it never shadows the
# API. `html=True` serves index.html at "/".
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


def main():
    ap = argparse.ArgumentParser(
        description="Serve the offline pinchtab-webgraph web UI + read-only REST API.")
    ap.add_argument("--host", default="127.0.0.1", help="bind host (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT") or 8765),
                    help="bind port (default: $PORT if set, else 8765). Honouring $PORT "
                         "lets `portless <name> python -m pinchtab_webgraph.ui.server` "
                         "assign a free port and serve it at https://<name>.localhost.")
    ap.add_argument("--open", action="store_true",
                    help="open the UI in a browser once the server is up")
    a = ap.parse_args()

    if a.host not in ("127.0.0.1", "localhost", "::1"):
        import sys
        print("WARNING: binding %s exposes the vault WRITE endpoints "
              "(PUT/DELETE /api/vault/credentials), the chat agent "
              "(/ws/chat — a read-only, offline-tool Claude agent), AND the live "
              "browser pane (/ws/screencast) with NO authentication — anyone who can "
              "reach this port can store or delete keyring credentials, drive the "
              "chat agent (spending API credits), and, biggest of all, make the "
              "server LAUNCH LOCAL HEADLESS CHROME processes that best-effort drive "
              "the credential-bearing PinchTab bridge to log in. This is the single "
              "strongest reason to keep --host 127.0.0.1."
              % a.host, file=sys.stderr)

    import uvicorn
    if a.open:
        url = "http://%s:%d/" % (a.host, a.port)
        # fire after a short delay so the server is listening before the tab opens.
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    # NOT reload=True: uvicorn's reloader needs an import string, not an app object.
    uvicorn.run(app, host=a.host, port=a.port)


if __name__ == "__main__":
    main()
