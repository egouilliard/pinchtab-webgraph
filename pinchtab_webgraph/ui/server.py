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
import re
import shutil
import threading
import time
import uuid
import webbrowser

from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import (api, artifacts, cache_store, flow as flow_mod,
                flow_resolve, __version__)
from . import (chat, chat_backend, chat_store, flow_runner, flow_store, live_crawl,
               screencast, vault)

app = FastAPI(title="pinchtab-webgraph UI", version=__version__)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
VENDOR_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "vendor")


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
    "session_not_found": 404,
    "invalid_session": 400,
    "too_many_sessions": 429,
    "too_many_flows": 429,
    "flow_not_found": 404,
    "invalid_flow": 400,
    "run_not_found": 404,
    "invalid_run": 400,
    # NOTE: "invalid" (a FAILED FLOW-DOCUMENT VALIDATION) is deliberately NOT here. A flow
    # doc arrives as user-submitted JSON and failing its structural check is a structured
    # MISS, not a protocol error — so it is a 200 carrying {"status":"invalid","path","error"},
    # the exact shape `flow_cmd validate` prints and the same house convention as no_match /
    # no_path. The REQUEST was fine; the DOCUMENT wasn't, and the client renders that.
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


def _resolve_host_token(host):
    """Validate the host TOKEN only — unlike _resolve_host there is deliberately no
    filesystem-existence precondition, since "no credential yet" / "no session yet" is the
    normal state for the vault + session write paths. Returns None on success, else an
    invalid_host error dict. Shared by the vault routes and the session routes.
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
    err = _resolve_host_token(host)
    if err is not None:
        return _respond(err)
    r = vault.get_routing(host)
    if r is None:
        return _respond({"status": "no_credential_for_host", "host": host})
    return _respond(r)


@app.put("/api/vault/credentials/{host}")
def vault_put_credential(host: str, payload: dict = Body(...)):
    err = _resolve_host_token(host)
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
    err = _resolve_host_token(host)
    if err is not None:
        return _respond(err)
    return _respond(vault.delete_credential(host, delete_secret=delete_secret))


# --- chat session routes (Phase 4: persistent, named chats per host) ---------
#
# Multiple named chat sessions per host, persisted by chat_store. MIRRORS the vault
# routes' _resolve_host_token / _respond / structured-status style. The heavy transcript
# + wire history stay OUT of the list/summary responses (chips only need the summary);
# only the single-session GET returns the full record, minus the resume-only internals
# (wire_messages + sdk_session_id). Bad-id tokens are rejected as invalid_session (400)
# before any filesystem access, the twin of invalid_host on the host segment.

def _guard_session(host, session_id):
    """Shared host+id guard for the per-session routes: returns None on success, else a
    structured error dict (invalid_host / invalid_session). Blocks path traversal on the
    id segment before any chat_store call touches the filesystem."""
    err = _resolve_host_token(host)
    if err is not None:
        return err
    try:
        chat_store.validate_session_id(session_id)
    except ValueError:
        return {"status": "invalid_session", "session": session_id}
    return None


@app.get("/api/hosts/{host}/sessions")
def host_sessions(host: str, mode: str | None = Query(None)):
    """Every session for a host, optionally filtered to one ``mode`` — so the Chat tab
    lists workspace chats and the Flows tab lists flow chats from the same store."""
    err = _resolve_host_token(host)
    if err is not None:
        return _respond(err)
    return {"sessions": chat_store.list_sessions(host, mode=mode)}


def _mode(value):
    """Normalize a caller-supplied chat mode. Anything unknown FAILS CLOSED to the
    read-only "workspace" mode — an unrecognized token must never be the one that grants
    a tool (chat.effective_tool_names fails closed the same way)."""
    return value if value in chat.MODES else "workspace"


@app.post("/api/hosts/{host}/sessions")
def create_host_session(host: str, payload: dict | None = Body(default=None)):
    err = _resolve_host_token(host)
    if err is not None:
        return _respond(err)
    try:
        record = chat_store.create(host, backend=chat_backend.resolve_backend_name(),
                                   mode=_mode((payload or {}).get("mode")),
                                   title=(payload or {}).get("title"))
    except chat_store.TooManySessions:
        return _respond({"status": "too_many_sessions",
                         "max": chat_store.MAX_SESSIONS_PER_HOST})
    return _respond(chat_store.summary(record))


@app.get("/api/hosts/{host}/sessions/{session_id}")
def get_host_session(host: str, session_id: str):
    err = _guard_session(host, session_id)
    if err is not None:
        return _respond(err)
    rec = chat_store.load(host, session_id)
    if rec is None:
        return _respond({"status": "session_not_found", "session": session_id})
    # the full record MINUS the resume-only internals + the ephemeral in-memory baseline.
    return _respond({k: v for k, v in rec.items()
                     if k not in ("wire_messages", "sdk_session_id", "_disk_len")})


@app.patch("/api/hosts/{host}/sessions/{session_id}")
def rename_host_session(host: str, session_id: str, payload: dict | None = Body(default=None)):
    err = _guard_session(host, session_id)
    if err is not None:
        return _respond(err)
    rec = chat_store.rename(host, session_id, (payload or {}).get("title"))
    if rec is None:
        return _respond({"status": "session_not_found", "session": session_id})
    return _respond(chat_store.summary(rec))


@app.delete("/api/hosts/{host}/sessions/{session_id}")
def delete_host_session(host: str, session_id: str):
    err = _guard_session(host, session_id)
    if err is not None:
        return _respond(err)
    # idempotent — deleting an absent session is a green {"deleted": false}, never a 404.
    return {"status": "ok", "deleted": chat_store.delete(host, session_id)}


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
async def chat_ws(websocket: WebSocket, host: str = Query(...),
                  session: str | None = Query(None),
                  mode: str | None = Query(None)):
    """``mode`` ("workspace" | "flow") applies to a NEW session only. A RESUMED session
    ignores it entirely — the mode comes from the record, exactly as the backend does, so
    a workspace chat can never be re-opened with the flow tool attached."""
    await websocket.accept()
    try:
        cache_store.validate_host(host)
    except ValueError:
        await websocket.send_json({"type": "error", "status": "invalid_host",
                                   "host": host})
        await websocket.close(code=1008)
        return
    # Resolve the requested session (if any) BEFORE opening the backend. A bad id token is
    # invalid_session; an id that doesn't resolve (or belongs to another host) is
    # session_not_found — both close the socket rather than silently minting a new chat.
    if session is not None:
        try:
            chat_store.validate_session_id(session)
        except ValueError:
            await websocket.send_json({"type": "error", "status": "invalid_session"})
            await websocket.close(code=1008)
            return
        record = chat_store.load(host, session)
        if record is None or record.get("host") != host:
            await websocket.send_json({"type": "error", "status": "session_not_found",
                                       "session": session})
            await websocket.close(code=1008)
            return
    else:
        record = None
    try:
        # A resumed session passes mode=None: open_chat_session then reads the record's
        # own mode. Only a brand-new session takes the query param.
        async with chat_backend.open_chat_session(
                host, mode=(_mode(mode) if record is None else None),
                record=record) as session_obj:
            # Bootstrap FIRST: the SPA replays this frame's transcript to restore the log
            # (a brand-new chat carries an empty transcript). Carries the summary — which
            # includes the session's pinned `mode` — so the client learns the session id it
            # is now bound to and which UI it should render.
            await websocket.send_json({
                "type": "session", **chat_store.summary(session_obj.record),
                "transcript": session_obj.record.get("transcript", [])})
            while True:
                try:
                    msg = await websocket.receive_json()
                except WebSocketDisconnect:
                    return
                if msg.get("type") != "user_message":
                    continue
                await session_obj.handle(msg.get("text", ""),
                                         live_url=msg.get("live_url"),
                                         draft=msg.get("draft"),
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
# A separate cap on concurrently running crawl subprocesses (see /ws/crawl below).
app.state.live_crawls = 0
# …and on concurrently running FLOW subprocesses (see /ws/flows/run below). These two
# counters VETO EACH OTHER: a crawl and a live flow run both lease the SINGLE-TENANT PinchTab
# bridge's tab, so letting them overlap would have them fighting over the same browser —
# each other's navigations, each other's forms. One bridge, one driver.
app.state.live_flow_runs = 0


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


# --- live crawl WebSocket (Phase 3: crawl a URL from the UI, store the result) ---
#
# The SPA sidebar's "New crawl" form posts a URL; we launch
# `python -m pinchtab_webgraph.interaction_crawl` as a subprocess, stream its stderr
# progress out over this socket, and — when it finishes or is cancelled — atomically
# promote its written interaction-graph JSON into the cache so the new host appears in
# the sidebar and is immediately usable by the Graph view + chat. The user URL NEVER
# becomes a shell string (argv list + create_subprocess_exec inside live_crawl), and the
# whole feature is OFF unless PINCHTAB_WEBGRAPH_ENABLE_CRAWL is truthy — the default
# install must not launch a real browser click-through.

def _crawl_enabled():
    """Truthy PINCHTAB_WEBGRAPH_ENABLE_CRAWL gates the whole live-crawl feature."""
    return (os.environ.get("PINCHTAB_WEBGRAPH_ENABLE_CRAWL") or "").strip().lower() \
        in ("1", "true", "yes", "on")


@app.websocket("/ws/crawl")
async def crawl_ws(websocket: WebSocket, url: str = Query(...),
                   max_states: int | None = Query(None),
                   max_depth: int | None = Query(None)):
    await websocket.accept()

    # OFF by default: this route can make the server drive a REAL browser through the
    # whole target app, so it must be explicitly enabled.
    if not _crawl_enabled():
        await websocket.send_json({"type": "error", "status": "crawl_unavailable",
                                   "reason": "disabled",
                                   "detail": live_crawl._DISABLED_HINT})
        await websocket.close(code=1013)
        return

    # Validate the start URL -> host token (the same choke-point every cache path uses).
    try:
        host = live_crawl.parse_start_url(url)
    except ValueError as e:
        await websocket.send_json({"type": "error", "status": "invalid_url",
                                   "url": url, "detail": str(e)})
        await websocket.close(code=1008)
        return

    # Capacity — and the CROSS-VETO: a live flow run is already driving the single-tenant
    # bridge, so a crawl must not start on top of it (and vice versa, below).
    if (app.state.live_crawls >= live_crawl.MAX_LIVE_CRAWLS
            or app.state.live_flow_runs >= flow_runner.MAX_LIVE_FLOW_RUNS):
        await websocket.send_json({"type": "error", "status": "too_many_sessions",
                                   "max": live_crawl.MAX_LIVE_CRAWLS})
        await websocket.close(code=1013)
        return

    app.state.live_crawls += 1
    try:
        async with live_crawl.open_crawl_session(
                url, host=host, max_states=max_states, max_depth=max_depth) as session:
            await websocket.send_json({"type": "status", "state": "starting",
                                       "host": host, "start_url": url})

            # Progress streams OUT via a background task (crawler stderr -> frames). We
            # concurrently wait for EITHER the process to exit OR a client cancel /
            # disconnect; whichever happens first, we cancel the session (idempotent) and
            # send the ONE terminal frame from the promoted graph.
            pump = asyncio.create_task(
                live_crawl.pump_progress(session, emit=websocket.send_json))
            proc_wait = asyncio.create_task(session.process.wait())

            cancelled = False
            try:
                while True:
                    recv = asyncio.create_task(websocket.receive_json())
                    done, _pending = await asyncio.wait(
                        {recv, proc_wait}, return_when=asyncio.FIRST_COMPLETED)
                    if proc_wait in done:
                        recv.cancel()
                        break
                    # a client frame arrived first
                    try:
                        msg = recv.result()
                    except WebSocketDisconnect:
                        cancelled = True
                        break
                    except Exception:  # noqa: BLE001 — bad frame / closed socket
                        cancelled = True
                        break
                    if isinstance(msg, dict) and msg.get("type") == "cancel":
                        cancelled = True
                        break
            except WebSocketDisconnect:
                cancelled = True

            if not proc_wait.done():
                proc_wait.cancel()
            if cancelled:
                await live_crawl.cancel_session(session)
            # let the progress pump drain any buffered lines, then stop it.
            try:
                await asyncio.wait_for(pump, timeout=2.0)
            except Exception:  # noqa: BLE001
                pump.cancel()

            terminal = await live_crawl.finish_session(session, cancelled=cancelled)
            try:
                await websocket.send_json(terminal)
            except Exception:  # noqa: BLE001 — client already gone
                pass
    except live_crawl.CrawlUnavailable as e:
        await websocket.send_json({"type": "error", "status": "crawl_unavailable",
                                   "reason": e.reason, "detail": e.detail})
        await websocket.close(code=1013)
    except WebSocketDisconnect:
        return
    finally:
        app.state.live_crawls -= 1


# --- flow routes (saved automations + their run history, per host) -----------
#
# A flow is a DECLARATIVE document (flow.py) executed by the step VM (runner.py). These
# routes are its CRUD + audit surface; /ws/flows/run below is how one is actually executed.
#
# The `{host}` segment is a STORAGE PARTITION KEY (which host's drawer the flow is filed in),
# NOT the flow document's own optional `host` field — that one is a runtime navigation guard
# the runner enforces. flow_store's docstring spells the distinction out; do not conflate them.
#
# Validation status convention: a flow document that fails flow.validate is a structured MISS
# (200 + {"status":"invalid","path","error"}), matching `flow_cmd validate`'s exact output
# shape — see the note in _STATUS_CODE. A bad flow_id TOKEN is a different thing entirely
# (invalid_flow -> 400): that is a malformed REQUEST, and it is rejected before any
# filesystem access, the twin of invalid_session on a chat id.

def _guard_flow(host, flow_id):
    """Shared host+flow-id guard: None on success, else a structured error dict. Blocks path
    traversal on the id segment before flow_store touches the filesystem."""
    err = _resolve_host_token(host)
    if err is not None:
        return err
    try:
        flow_store.validate_flow_id(flow_id)
    except ValueError:
        return {"status": "invalid_flow", "flow": flow_id}
    return None


def _guard_run(host, flow_id, run_id):
    """The same guard, extended to the run-id segment (invalid_run -> 400)."""
    err = _guard_flow(host, flow_id)
    if err is not None:
        return err
    try:
        flow_store.validate_run_id(run_id)
    except ValueError:
        return {"status": "invalid_run", "run": run_id}
    return None


def _validated(doc):
    """flow.validate a caller-supplied document -> (ok_dict, None) | (None, invalid_dict).

    A thin (ok, err) SPLIT of flow.validate_report — which owns the verdict shape, so the
    CLI, this HTTP surface and the chat agent's `propose_flow` tool all answer the same
    question with the same words. The tuple form is kept because every caller here branches
    on it.

    Plus the ONE thing flow.validate_report structurally cannot know: whether the document's
    `goal`s RESOLVE against the host's crawled graph (flow_resolve). That check needs a graph
    off disk, which is why it lives here and not in the pure validator — but it is exactly the
    error the authoring loop most needs, so the HTTP surface (which the editor calls on every
    keystroke) is where it has to land."""
    report = flow_mod.validate_report(doc)
    if report["status"] != "ok":
        return None, report
    return dict(report, warnings=_resolvability_warnings(doc)), None


def _resolvability_warnings(doc):
    """Never let the resolvability check fail a request: it is ADVISORY. A missing/stale/
    corrupt cache yields no warnings, not a 500 — the document is still perfectly savable."""
    try:
        return flow_resolve.warnings_for_doc(doc)
    except Exception:                    # noqa: BLE001 — advisory only; see the docstring
        return []


@app.get("/api/flows/op_schema")
def flows_op_schema():
    """Stateless — the flow VOCABULARY itself, straight from flow.py's tables.

    The ONLY serialization of the op set anywhere: the canvas derives its edit forms from
    this (which keys an op takes, which are exclusive, which capability a write needs), so
    the editor cannot fall out of step with the validator. Nothing here is hand-listed —
    add an op to flow.LEAF_OPS and it appears in the UI.
    """
    return {
        "leaf_ops": {op: {k: list(v) for k, v in spec.items()}
                     for op, spec in flow_mod.LEAF_OPS.items()},
        "body_ops": {op: {k: list(v) for k, v in spec.items()}
                     for op, spec in flow_mod.BODY_OPS.items()},
        "capabilities": dict(flow_mod.DEFAULT_CAPABILITIES),
        "write_ops": sorted(flow_mod.WRITE_OPS),
        "body_vars": {op: sorted(v) for op, v in flow_mod.BODY_VARS.items()},
        "max_depth": flow_mod.MAX_DEPTH,
        "max_steps": flow_mod.MAX_STEPS,
    }


@app.post("/api/flows/validate")
def flows_validate(doc: dict = Body(...)):
    """The editor's "is this document legal?" check — nothing is stored, no browser is leased.

    Two verdicts in one answer, and the difference matters:
      * `status` — STRUCTURAL. invalid = the document is wrong; Save is blocked.
      * `warnings` — RESOLVABILITY. The doc is fine, but a `goal` doesn't match anything in
        the host's crawled graph, so the run WILL abort on that step. Advisory: `status` stays
        "ok" and the flow stays savable (the graph may simply be older than the flow), but the
        user must SEE it here rather than discover it mid-run. Each warning carries the step's
        `path` in flow.py's grammar, so the canvas lights up the exact box, plus the candidate
        labels the site really has.
    """
    ok, err = _validated(doc)
    return _respond(err if err is not None else ok)


@app.post("/api/flows/schema")
def flows_schema(doc: dict = Body(...)):
    """Stateless — the document's `inputs` as a JSON Schema (what the run form is built from)."""
    _ok, err = _validated(doc)
    if err is not None:
        return _respond(err)
    return _respond(flow_mod.json_schema(doc))


# --- file staging for `file` inputs -------------------------------------------
#
# A flow's `file` input carries an ABSOLUTE LOCAL PATH — that is what the `upload` op hands
# the bridge, which reads the file off this machine. A browser cannot produce such a path: a
# file <input> exposes only a name (`C:\fakepath\…`). So the UI UPLOADS the chosen bytes here,
# we stage them on disk, and the returned path is what the run frame's `inputs` map carries.
#
# RAW BODY, NOT MULTIPART — ON PURPOSE. FastAPI's UploadFile/Form require `python-multipart`,
# a dependency this package does not have and will not take on (the base install is
# pure-stdlib; even fastapi is an extra). A raw body needs nothing new and the browser can
# post a File object straight through: fetch(url, {method: "POST", body: fileObject}).
# Please do not "improve" this into multipart.
#
# This endpoint writes ATTACKER-SUPPLIED BYTES TO DISK from an UNAUTHENTICATED local UI, so it
# is treated accordingly: every upload lands in its OWN uuid4 directory (two uploads cannot
# collide or overwrite), the body is streamed to disk in chunks and hard-capped, and stale
# staging dirs are pruned as we go.
#
# TWO SEPARATE CONCERNS, DELIBERATELY SPLIT (they used to be one over-strict allowlist):
#   REJECT (400) — input that is not a filename at all: a path (a separator, or anything that
#     differs from its own basename), empty/whitespace-only, all-dots (the `.`/`..` subtlety
#     cache_store.validate_host also guards), or NUL/control bytes. That is the traversal guard,
#     and it is the ONLY thing that earns a 400.
#   SANITIZE — everything else. `Invoice Jan 2026.pdf` is an ordinary real file, not an attack;
#     a space is not a security boundary. Characters outside [A-Za-z0-9._-] become `_` in the
#     STORED name, while the ORIGINAL is echoed back for display. The stored name is cosmetic
#     anyway (the bridge renames uploads to `upload-N.<ext>` on the way into the page) — but it
#     must still be safe, and it is: sanitised, capped, and inside a fresh uuid4 dir.

MAX_UPLOAD_BYTES = 100 * 1024 * 1024      # 100 MB — over it: 413, and the partial is deleted
UPLOAD_CHUNK = 1024 * 1024                # streamed; the whole body is NEVER held in memory
UPLOAD_TTL_S = 7 * 24 * 60 * 60           # staged files are transient — prune after ~7 days
MAX_STORED_NAME = 120                     # so a pathological name cannot hit the FS name limit

# What may survive INTO THE STORED NAME. Anything else is replaced with `_` — never rejected.
_UPLOAD_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _uploads_dir():
    return os.path.join(cache_store.home_dir(), "uploads")


def _reject_unsafe_upload_name(name):
    """Raise ValueError if `name` is not a plain filename. Traversal only — NOT character
    purity: this guards the path, and `_stored_upload_name` handles the rest."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("a `name` is required")
    if "\x00" in name or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ValueError("name must not contain control characters")
    if "/" in name or "\\" in name or name != os.path.basename(name.replace("\\", "/")):
        raise ValueError("name must be a bare filename, not a path: %r" % name)
    if name.strip(".") == "":            # "." / ".." / "…" are traversal, not names
        raise ValueError("name must not be all dots: %r" % name)


def _stored_upload_name(name):
    """The on-disk basename for an already-accepted `name`: every character outside
    [A-Za-z0-9._-] becomes `_`, runs of `_` collapse, leading/trailing `_`/`.` are trimmed, the
    extension is preserved, and the whole thing is capped. Empty stem -> `upload`."""
    stem, ext = os.path.splitext(name)
    ext = _UPLOAD_UNSAFE_RE.sub("_", ext).strip("_")          # ".p df" -> ".p_df"; keep the dot
    if ext and not ext.startswith("."):
        ext = "." + ext
    stem = _UPLOAD_UNSAFE_RE.sub("_", stem)
    stem = re.sub(r"_+", "_", stem).strip("_.")
    if not stem:
        stem = "upload"
    ext = ext[:MAX_STORED_NAME - 1]                            # a pathological extension, too
    return stem[:max(1, MAX_STORED_NAME - len(ext))] + ext


def _safe_upload_name(name):
    """(original, stored) for a usable name, or ValueError for a traversing / non-name one."""
    _reject_unsafe_upload_name(name)
    return name, _stored_upload_name(name)


def _prune_uploads():
    """Delete staging dirs older than UPLOAD_TTL_S. Cheap housekeeping on each write — and it
    NEVER fails the request: a pruning error is not the caller's problem."""
    root = _uploads_dir()
    cutoff = time.time() - UPLOAD_TTL_S
    try:
        entries = os.listdir(root)
    except OSError:
        return
    for entry in entries:
        p = os.path.join(root, entry)
        try:
            if os.path.isdir(p) and os.path.getmtime(p) < cutoff:
                shutil.rmtree(p, ignore_errors=True)
        except OSError:                  # racing prune / vanished dir — nothing to do
            continue


@app.post("/api/flows/uploads")
async def flows_upload(request: Request, name: str = Query(...)):
    """Stage a file for a flow's `file` input; return the absolute path the run frame carries.

    POST /api/flows/uploads?name=<filename>   body = the raw file bytes
      -> 200 {"status":"ok","path","name","stored_name","size"}
             `name` is the ORIGINAL the user chose (what the UI shows); `stored_name` is the
             sanitised basename actually on disk. They differ for e.g. "Invoice Jan 2026.pdf".
      -> 400 {"status":"invalid_name","name","detail"}   — traversal / non-name input ONLY
      -> 413 {"status":"too_large","max_bytes"}
    """
    try:
        original, stored = _safe_upload_name(name)
    except ValueError as e:
        # NOTHING has been created at this point — the uuid dir is minted only after the
        # name is known good, so a rejected name writes nowhere at all.
        return JSONResponse({"status": "invalid_name", "name": name, "detail": str(e)},
                            status_code=400)

    _prune_uploads()
    staging = os.path.join(_uploads_dir(), uuid.uuid4().hex)
    dest = os.path.join(staging, stored)

    size = 0
    too_large = False
    try:
        os.makedirs(staging, exist_ok=True)
        with open(dest, "wb") as fh:
            # STREAMED: the cap is enforced against the running total as chunks arrive, so an
            # oversize body is abandoned mid-flight — it is never fully read into memory, and
            # never fully written to disk.
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    too_large = True
                    break
                fh.write(chunk)
    except OSError as e:
        shutil.rmtree(staging, ignore_errors=True)
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)

    if too_large:
        shutil.rmtree(staging, ignore_errors=True)      # the partial file goes with it
        return JSONResponse({"status": "too_large", "max_bytes": MAX_UPLOAD_BYTES},
                            status_code=413)
    return {"status": "ok", "path": dest, "name": original, "stored_name": stored, "size": size}


@app.get("/api/hosts/{host}/flows")
def host_flows(host: str):
    err = _resolve_host_token(host)
    if err is not None:
        return _respond(err)
    return {"flows": flow_store.list_flows(host)}


@app.post("/api/hosts/{host}/flows")
def create_host_flow(host: str, doc: dict = Body(...)):
    err = _resolve_host_token(host)
    if err is not None:
        return _respond(err)
    try:
        record = flow_store.create(host, doc)
    except flow_mod.FlowError as e:
        # 200 + the structured miss — the request was fine, the DOCUMENT wasn't.
        return _respond({"status": "invalid", "path": e.path, "error": e.message})
    except flow_store.TooManyFlows:
        return _respond({"status": "too_many_flows", "max": flow_store.MAX_FLOWS_PER_HOST})
    # SAVED, and still `ok` — a resolvability warning is advisory, never a blocker (the flow
    # may be authored before the crawl). It rides along so the editor can say so.
    return _respond({"status": "ok", **flow_store.summary(record),
                     "warnings": _resolvability_warnings(doc)})


@app.get("/api/hosts/{host}/flows/{flow_id}")
def get_host_flow(host: str, flow_id: str):
    err = _guard_flow(host, flow_id)
    if err is not None:
        return _respond(err)
    record = flow_store.load(host, flow_id)
    if record is None:
        return _respond({"status": "flow_not_found", "flow": flow_id})
    return _respond(record)          # the FULL record, doc included (the editor needs it)


@app.put("/api/hosts/{host}/flows/{flow_id}")
def update_host_flow(host: str, flow_id: str, doc: dict = Body(...)):
    err = _guard_flow(host, flow_id)
    if err is not None:
        return _respond(err)
    try:
        record = flow_store.update(host, flow_id, doc)
    except flow_mod.FlowError as e:
        return _respond({"status": "invalid", "path": e.path, "error": e.message})
    if record is None:
        return _respond({"status": "flow_not_found", "flow": flow_id})
    return _respond({"status": "ok", **flow_store.summary(record),
                     "warnings": _resolvability_warnings(doc)})


@app.delete("/api/hosts/{host}/flows/{flow_id}")
def delete_host_flow(host: str, flow_id: str):
    err = _guard_flow(host, flow_id)
    if err is not None:
        return _respond(err)
    # idempotent — deleting an absent flow is a green {"deleted": false}, never a 404. The
    # delete CASCADES: the flow's whole run history goes with it.
    return {"status": "ok", "deleted": flow_store.delete(host, flow_id)}


@app.get("/api/hosts/{host}/flows/{flow_id}/schema")
def host_flow_schema(host: str, flow_id: str):
    err = _guard_flow(host, flow_id)
    if err is not None:
        return _respond(err)
    record = flow_store.load(host, flow_id)
    if record is None:
        return _respond({"status": "flow_not_found", "flow": flow_id})
    return _respond(flow_mod.json_schema(record["doc"]))


@app.get("/api/hosts/{host}/flows/{flow_id}/runs")
def host_flow_runs(host: str, flow_id: str):
    err = _guard_flow(host, flow_id)
    if err is not None:
        return _respond(err)
    if flow_store.load(host, flow_id) is None:
        return _respond({"status": "flow_not_found", "flow": flow_id})
    return {"runs": flow_store.list_runs(host, flow_id)}


@app.get("/api/hosts/{host}/flows/{flow_id}/runs/{run_id}")
def host_flow_run(host: str, flow_id: str, run_id: str):
    err = _guard_run(host, flow_id, run_id)
    if err is not None:
        return _respond(err)
    record = flow_store.load_run(host, flow_id, run_id)
    if record is None:
        return _respond({"status": "run_not_found", "run": run_id})
    return _respond(record)          # the FULL run record: steps, artifacts, collected


@app.get("/api/hosts/{host}/flows/{flow_id}/artifacts")
def host_flow_artifacts(host: str, flow_id: str):
    """The flow's CUMULATIVE artifact ledger — every distinct file it has ever fetched.

    The scope is the flow_id (a uuid4 hex, hence a legal artifact scope), which is also what
    the run WS passes as `--scope`, so two flows never poison each other's dedupe ledger."""
    err = _guard_flow(host, flow_id)
    if err is not None:
        return _respond(err)
    if flow_store.load(host, flow_id) is None:
        return _respond({"status": "flow_not_found", "flow": flow_id})
    store = artifacts.ArtifactStore(scope=flow_id)
    return {"artifacts": store.list_artifacts(), "stats": store.stats()}


# --- flow run WebSocket (execute a saved flow, stream every step) --------------
#
# The SPA's "Run" button sends inputs + a capability grant; we launch
# `python -m pinchtab_webgraph.flow_cmd run <doc> --jsonl` as a subprocess (see
# flow_runner.py for WHY a subprocess: SIGTERM on its process group is the ONLY cancellation
# primitive the flow layer has) and relay its JSONL frames out over this socket.
#
# OFF unless PINCHTAB_WEBGRAPH_ENABLE_FLOWS is truthy. This is the most dangerous route in
# the server: unlike a crawl — which structurally NEVER submits — a flow's `do{submit:true}`
# or `upload` step CAN write to the real site, whenever the document declares the capability
# AND the caller grants it. Both must agree; either vetoes.

def _flows_enabled():
    """Truthy PINCHTAB_WEBGRAPH_ENABLE_FLOWS gates the whole run-a-flow feature."""
    return (os.environ.get("PINCHTAB_WEBGRAPH_ENABLE_FLOWS") or "").strip().lower() \
        in ("1", "true", "yes", "on")


async def _execute_flow_run(websocket, *, record, host, flow_id, msg):
    """Run ONE flow execution end-to-end: spawn, relay, cancel, persist, send the terminal.

    Returns True if the socket should stay open for another kickoff, False if it was closed.
    """
    doc = record["doc"]
    dry_run = bool(msg.get("dry_run"))
    grant = msg.get("grant") or {}

    try:
        inputs = flow_mod.bind_inputs(doc, msg.get("inputs") or {})
    except flow_mod.FlowError as e:
        # A bad input is the user's next keystroke away from being a good one — report it and
        # KEEP THE SOCKET OPEN so they can just fix the form and press Run again.
        await websocket.send_json({"type": "error", "status": "invalid_input",
                                   "detail": e.message, "path": e.path})
        return True

    steps = []          # accumulated as we relay, so a hard-killed run still persists a trail
    gone = False        # the client vanished mid-run — persist, then stop touching the socket

    async def relay(frame):
        if frame.get("type") == "step":
            steps.append({k: v for k, v in frame.items() if k != "type"})
        elif frame.get("type") == "result":
            return      # held back: the ONE terminal frame is sent below, after persisting
        await websocket.send_json(frame)

    # A DRY run touches no browser, so it neither consumes the bridge nor vetoes a crawl.
    if not dry_run:
        if (app.state.live_crawls >= live_crawl.MAX_LIVE_CRAWLS
                or app.state.live_flow_runs >= flow_runner.MAX_LIVE_FLOW_RUNS):
            await websocket.send_json({"type": "error", "status": "too_many_sessions",
                                       "max": flow_runner.MAX_LIVE_FLOW_RUNS})
            await websocket.close(code=1013)
            return False
        # NOTHING that can raise may sit between this line and the `try` that owns the
        # decrementing `finally` — the pair must be provable. `flow_store.start_run` writes
        # to disk (and so can raise OSError), which is exactly why it lives INSIDE the try:
        # a leaked count would wedge every later flow run AND, via the cross-veto, every
        # later crawl until the process restarts. Mirrors /ws/crawl.
        app.state.live_flow_runs += 1
    try:
        run_id = flow_store.new_run_id()
        caps = {k: bool(flow_mod.capabilities(doc).get(k) and grant.get(k, v))
                for k, v in flow_mod.DEFAULT_CAPABILITIES.items()}
        # The placeholder is written BEFORE anything is spawned: a run that is SIGKILLed, or
        # that dies with the server, still leaves a discoverable record instead of vanishing.
        flow_store.start_run(host, flow_id, run_id, dry_run=dry_run, capabilities=caps,
                             inputs=inputs)

        with flow_runner.staged_flow_doc(doc) as flow_path:
            async with flow_runner.open_flow_run_session(
                    flow_path=flow_path, host=host, flow_id=flow_id, run_id=run_id,
                    inputs=inputs, grant=grant, dry_run=dry_run,
                    scope=flow_id) as session:
                await websocket.send_json({"type": "status", "state": "starting",
                                           "host": host, "flow_id": flow_id,
                                           "run_id": run_id, "dry_run": dry_run})

                # Frames stream OUT via a background task (subprocess JSONL -> relay). We
                # concurrently wait for EITHER the process to exit OR a client cancel /
                # disconnect; whichever happens first, we cancel the session (idempotent) and
                # send the ONE terminal frame.
                pump = asyncio.create_task(flow_runner.pump_frames(session, emit=relay))
                proc_wait = asyncio.create_task(session.process.wait())

                cancelled = False
                try:
                    while True:
                        recv = asyncio.create_task(websocket.receive_json())
                        done, _pending = await asyncio.wait(
                            {recv, proc_wait}, return_when=asyncio.FIRST_COMPLETED)
                        if proc_wait in done:
                            recv.cancel()
                            break
                        try:
                            client_msg = recv.result()
                        except WebSocketDisconnect:
                            cancelled = gone = True   # a disconnect mid-run IS a cancel
                            break
                        except Exception:  # noqa: BLE001 — bad frame / closed socket
                            cancelled = gone = True
                            break
                        if isinstance(client_msg, dict) and client_msg.get("type") == "cancel":
                            cancelled = True
                            break
                except WebSocketDisconnect:
                    cancelled = gone = True

                if not proc_wait.done():
                    proc_wait.cancel()
                if cancelled:
                    await flow_runner.cancel_run_session(session)
                # let the pump drain whatever the subprocess already wrote, then stop it.
                result = None
                try:
                    result = await asyncio.wait_for(pump, timeout=5.0)
                except Exception:  # noqa: BLE001 — pump wedged: keep what we already relayed
                    pump.cancel()
                rc = session.process.returncode
    except flow_runner.FlowRunUnavailable as e:
        await websocket.send_json({"type": "error", "status": "flow_unavailable",
                                   "reason": e.reason, "detail": e.detail})
        await websocket.close(code=1013)
        return False
    finally:
        if not dry_run:
            app.state.live_flow_runs -= 1

    if result is None:
        # HONEST rather than hanging: the process is gone and it never printed a result, so
        # say exactly that (the same discipline live_crawl.finish_session practices when a
        # crawl produced no graph). The steps we DID relay are kept — that trail is the only
        # evidence of what the flow managed to do before it died.
        result = {"status": "error", "flow": (doc.get("name")), "dry_run": dry_run,
                  "detail": "the flow process exited with no result (rc=%s)" % (rc,),
                  "steps": steps, "artifacts": [], "collected": {}, "stats": {}}
    else:
        result = {k: v for k, v in result.items() if k != "type"}

    # PERSIST BEFORE SENDING: the record must exist by the time the client can act on the
    # frame (e.g. immediately GET the run it was just told about).
    flow_store.finish_run(host, flow_id, run_id, result, cancelled=cancelled)
    if gone:
        return False    # nothing left to send to, and nothing left to receive from
    try:
        await websocket.send_json({"type": "result", "run_id": run_id, **result})
    except Exception:  # noqa: BLE001 — client already gone; the run is persisted regardless
        return False
    return True


@app.websocket("/ws/flows/run")
async def flow_run_ws(websocket: WebSocket, host: str = Query(...),
                      flow_id: str = Query(...)):
    await websocket.accept()

    if not _flows_enabled():
        await websocket.send_json({"type": "error", "status": "flow_unavailable",
                                   "reason": "disabled",
                                   "detail": flow_runner._DISABLED_HINT})
        await websocket.close(code=1013)
        return

    err = _guard_flow(host, flow_id)
    if err is not None:
        await websocket.send_json({"type": "error", **err})
        await websocket.close(code=1008)
        return

    record = flow_store.load(host, flow_id)
    if record is None:
        await websocket.send_json({"type": "error", "status": "flow_not_found",
                                   "flow": flow_id})
        await websocket.close(code=1008)
        return

    # Bootstrap: the client renders the run form (declared inputs + capabilities) from THIS
    # frame — no second fetch.
    await websocket.send_json({"type": "flow", **flow_store.summary(record)})

    try:
        while True:
            try:
                msg = await websocket.receive_json()
            except (WebSocketDisconnect, RuntimeError):
                return          # RuntimeError: the disconnect was already consumed below
            if not isinstance(msg, dict) or msg.get("type") != "run":
                continue
            if not await _execute_flow_run(websocket, record=record, host=host,
                                           flow_id=flow_id, msg=msg):
                return
    except WebSocketDisconnect:
        return


# Vendor mount — the 6 Cytoscape libs the graph view lazy-loads. Registered BEFORE
# the catch-all "/" mount because Starlette resolves mounts in REGISTRATION order: the
# "/" StaticFiles below matches every path, so a /vendor mount registered after it would
# be shadowed and never reached. It reuses pinchtab_webgraph/vendor/*.min.js (the same
# 785KB crawl.py inlines) rather than duplicating them under static/.
app.mount("/vendor", StaticFiles(directory=VENDOR_DIR), name="vendor")

# Static mount — registered LAST, AFTER every /api route AND the /vendor mount, so it
# never shadows the API or the vendor libs. `html=True` serves index.html at "/".
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
              "(/ws/chat — a read-only, offline-tool Claude agent), the live "
              "browser pane (/ws/screencast), AND — biggest of all — the live CRAWL "
              "endpoint (/ws/crawl) with NO authentication. Anyone who can reach this "
              "port can store or delete keyring credentials, drive the chat agent "
              "(spending API credits), launch local headless Chrome, and, worst of "
              "all, make the server drive a REAL browser that CLICKS THROUGH THE WHOLE "
              "TARGET APP and OPENS EVERY CREATE FORM (it never submits) against the "
              "credential-bearing PinchTab bridge — AND the flow-run endpoint "
              "(/ws/flows/run), which is MORE dangerous still: a crawl structurally never "
              "submits, but a FLOW's do{submit:true} / upload steps CAN WRITE TO THE REAL "
              "SITE whenever the saved flow declares the capability and the caller grants "
              "it. /ws/crawl and /ws/flows/run are off unless "
              "PINCHTAB_WEBGRAPH_ENABLE_CRAWL / PINCHTAB_WEBGRAPH_ENABLE_FLOWS are set, but "
              "this is the single strongest reason to keep --host 127.0.0.1."
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
