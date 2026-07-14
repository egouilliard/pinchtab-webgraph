#!/usr/bin/env python3
"""Alternative chat backend: Claude driven through the local Claude Code CLI.

This is the Phase-6 SECOND chat backend for the OPTIONAL web UI. Where ``chat.py``
talks to the Anthropic API directly (needs ANTHROPIC_API_KEY), this backend drives
the user's LOCALLY-LOGGED-IN Claude Code via the Claude Agent SDK — no API key, the
account's own Claude Code session/credentials do the work. Both backends stream the
SAME frame protocol out over the WebSocket, so the SPA is untouched.

THE WHOLE POINT IS THE SAFETY LOCKDOWN. The Claude Agent SDK spawns a real ``claude``
subprocess that, left to its defaults, could run Bash/Write/Edit on THIS machine. So
``_build_options`` fences the agent down to ONLY the 6 offline pinchtab-webgraph graph
tools (the same set as ``chat.OFFLINE_TOOL_NAMES``), with a deny-by-default
``can_use_tool`` backstop. ``crawl`` and ``ask_howto`` (the two LIVE tools that shell
out to the PinchTab browser bridge) are stripped; every built-in tool is killed.

MIRRORS chat.py's discipline: stdlib-only at module scope (NO ``claude_agent_sdk``
import at import time, so server.py's ``from . import chat_backend`` — which imports
this — keeps the base package a pure-stdlib install). The heavy SDK is imported
LAZILY inside ``open_client``, and a missing package / missing CLI degrades to a
structured ``chat.ChatUnavailable(reason, detail)`` rather than a crash.
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
from contextlib import asynccontextmanager

# The MCP server name the offline graph tools are exposed under. The Claude Agent SDK
# surfaces MCP tools to the model as ``mcp__<server-name>__<toolname>``.
MCP_SERVER_NAME = "pinchtab-webgraph"

# Reused UNCHANGED from the API backend: the ChatUnavailable type, the offline tool
# name set, and the system prompt builder. Safe to import at module scope — chat.py is
# itself stdlib-only at import time.
from . import chat  # noqa: E402


_NO_CLAUDE_CLI_HINT = (
    "The 'claude' CLI is not on PATH. This backend drives your locally-logged-in "
    "Claude Code (no ANTHROPIC_API_KEY needed) — install the Claude Code CLI and log "
    "in (`claude`), or set ANTHROPIC_API_KEY / PINCHTAB_UI_CHAT_BACKEND=api to use the "
    "Anthropic-API backend instead.")
_NO_PACKAGE_HINT = (
    "The optional 'claude-agent-sdk' package is not installed. Install the "
    "claude-code UI extra (pip install 'pinchtab-webgraph[ui-claude-code]') to enable "
    "the Claude Code chat backend.")
_CLAUDE_CODE_DENY_MSG = (
    "This tool is not permitted. The pinchtab-webgraph chat agent may ONLY call the "
    "offline graph query tools.")


def _qualified(name):
    """Bare tool name -> the SDK's ``mcp__<server>__<name>`` qualified form."""
    return "mcp__%s__%s" % (MCP_SERVER_NAME, name)


def _strip_prefix(name):
    """Strip the ``mcp__<server>__`` qualifier so emitted frames carry bare names."""
    prefix = "mcp__%s__" % MCP_SERVER_NAME
    if name.startswith(prefix):
        return name[len(prefix):]
    return name


def _is_allowed(name, allowed):
    """PURE allow-list membership check (no SDK import) — the safety predicate.

    ``allowed`` is the collection of QUALIFIED offline tool names. A bare built-in
    like "Bash"/"Write", or a qualified LIVE tool (crawl/ask_howto), is NOT in it and
    is therefore denied by ``can_use_tool``. This is the single most important safety
    invariant, so it is kept pure and importable for a direct unit test.
    """
    return name in allowed


def require_claude_cli():
    """Raise ChatUnavailable("no_claude_cli") if the ``claude`` CLI is not on PATH.

    Fails fast WITHOUT importing the SDK — a base/[ui] install without the claude-code
    extra, or a machine with no Claude Code, degrades to a structured frame.
    """
    if shutil.which("claude") is None:
        raise chat.ChatUnavailable("no_claude_cli", _NO_CLAUDE_CLI_HINT)


def resolve_model():
    """The model to drive: the PINCHTAB_UI_CLAUDE_CODE_MODEL override, else None.

    None = the account's Claude Code default. Deliberately NOT chat.resolve_model()'s
    ``claude-opus-4-8`` default, which is an API model id, not necessarily a valid
    Claude Code CLI alias — passing it could break a session that would otherwise
    just use the account default.
    """
    return os.environ.get("PINCHTAB_UI_CLAUDE_CODE_MODEL") or None


def _build_options(claude_agent_sdk, host, *, model=None, cwd=None, mode="workspace"):
    """Build the FULLY-LOCKED-DOWN ClaudeAgentOptions — the safety fence.

    Every field here is load-bearing for safety; ``test_build_options_lockdown_shape``
    asserts each one and must fail if any is weakened:
      * tools=[]                  -> kill ALL built-in tools (Bash/Write/Edit/...).
                                     THIS is the real fence — allowed_tools only
                                     AUTO-APPROVES, it does not restrict availability.
      * mcp_servers={...stdio...} -> the ONLY tool source: our own MCP server, spawned
                                     as `python -m pinchtab_webgraph.mcp_server`.
      * strict_mcp_config=True    -> ignore any project .mcp.json.
      * allowed_tools=<mode set>  -> auto-approve exactly chat.effective_tool_names(mode):
                                     the 6 offline graph tools, plus — in flow mode only —
                                     the PURE `propose_flow` (no disk, no browser, no
                                     subprocess; the MCP surface has no save/run tool at
                                     all, so the agent still cannot persist or execute).
      * disallowed_tools=<2 live> -> strip crawl/ask_howto (they drive a live browser).
      * setting_sources=[]        -> no ~/.claude / CLAUDE.md / settings leak in.
      * permission_mode="default" -> so can_use_tool IS consulted (NOT bypassPermissions).
      * can_use_tool=<closure>    -> deny-by-default backstop (allow iff allow-listed).
      * system_prompt=<mode>      -> the same prompt chat.py drives this mode with.
      * include_partial_messages  -> StreamEvent text deltas for streaming.
      * model / cwd               -> account default unless overridden / an isolated,
                                     per-session temp cwd removed on close.
    """
    allowed = [_qualified(n) for n in sorted(chat.effective_tool_names(mode))]

    async def can_use_tool(tool_name, tool_input, context):
        if _is_allowed(tool_name, allowed):
            return claude_agent_sdk.PermissionResultAllow()
        return claude_agent_sdk.PermissionResultDeny(message=_CLAUDE_CODE_DENY_MSG)

    # The MCP server is spawned as `python -m pinchtab_webgraph.mcp_server` from an
    # ISOLATED temp cwd (below), so `-m` can only find the package via sys.path — NOT
    # the cwd. Pin PYTHONPATH to this package's parent so the import works regardless of
    # how (or whether) the package is installed. Without this, a broken/dangling editable
    # install (e.g. pointing at a pruned worktree) silently crashes the MCP server → the
    # model gets NO tools and narrates its tool calls as text instead of calling them.
    _pkg_parent = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    _mcp_env = dict(os.environ)
    _mcp_env["PYTHONPATH"] = os.pathsep.join(
        p for p in (_pkg_parent, os.environ.get("PYTHONPATH")) if p)

    return claude_agent_sdk.ClaudeAgentOptions(
        tools=[],
        mcp_servers={
            MCP_SERVER_NAME: {
                "type": "stdio",
                "command": sys.executable,
                "args": ["-m", "pinchtab_webgraph.mcp_server"],
                "env": _mcp_env,
            }
        },
        strict_mcp_config=True,
        allowed_tools=allowed,
        disallowed_tools=[_qualified("crawl"), _qualified("ask_howto")],
        setting_sources=[],
        permission_mode="default",
        can_use_tool=can_use_tool,
        system_prompt=chat.system_prompt_for(host, mode),
        include_partial_messages=True,
        model=model,
        cwd=cwd,
    )


async def _await_mcp_ready(client, *, attempts=50, delay=0.2):
    """Best-effort wait until the stdio MCP server finishes its handshake.

    A freshly-connected stdio MCP server is ``pending`` for a moment. If the FIRST
    user turn races that handshake, the offline graph tools are not yet in the model's
    tool list, so the model can only narrate ("I'll look that up...") instead of
    actually calling ``howto``. Poll ``get_mcp_status`` until our server reaches a
    settled state (connected / failed / disabled / needs-auth), bounded to ~10s so a
    missing or unresponsive status endpoint never hangs the session (any error just
    proceeds — the turn still works, it just may miss tools on the very first message).
    """
    get_status = getattr(client, "get_mcp_status", None)
    if get_status is None:
        # This SDK build can't report MCP readiness. Rather than proceed instantly (the
        # first turn would then race an EMPTY tool list, and the model emits its tool
        # call as raw TEXT), give the stdio handshake a bounded moment to settle.
        await asyncio.sleep(1.0)
        return
    for _ in range(attempts):
        try:
            status = await get_status()
        except Exception:  # noqa: BLE001 — status endpoint erroring: brief settle, don't hang
            await asyncio.sleep(1.0)
            return
        servers = (status or {}).get("mcpServers") or []
        ours = next((s for s in servers if s.get("name") == MCP_SERVER_NAME), None)
        if ours is None:
            return
        if ours.get("status") in ("connected", "failed", "needs-auth", "disabled"):
            return
        await asyncio.sleep(delay)


@asynccontextmanager
async def open_client(host, *, model=None, mode="workspace"):
    """Yield a connected, locked-down ClaudeSDKClient. NEVER leaks a stray subprocess.

    Fails fast on a missing CLI (no SDK import), then lazily imports the SDK -> a
    structured ChatUnavailable on ImportError. The client runs in a fresh per-session
    temp cwd, removed on close. ``mode`` selects the prompt + the tool fence and is fixed
    for the life of the client. The SDK terminates the child ``claude`` process itself
    on __aexit__ (disconnect -> transport.close(): graceful wait -> SIGTERM -> SIGKILL,
    plus an atexit backstop), so no explicit process teardown is needed here.
    """
    require_claude_cli()
    try:
        import claude_agent_sdk
    except ImportError:
        raise chat.ChatUnavailable("no_claude_code_package", _NO_PACKAGE_HINT)

    if model is None:
        model = resolve_model()

    cwd = tempfile.mkdtemp(prefix="pinchtab-webgraph-cc-")
    try:
        options = _build_options(claude_agent_sdk, host, model=model, cwd=cwd, mode=mode)
        client = claude_agent_sdk.ClaudeSDKClient(options)
        try:
            async with client:
                await _await_mcp_ready(client)
                yield client
        except claude_agent_sdk.CLINotFoundError:
            raise chat.ChatUnavailable("no_claude_cli", _NO_CLAUDE_CLI_HINT)
        except claude_agent_sdk.ClaudeSDKError as e:
            raise chat.ChatUnavailable("claude_code_startup_error", str(e))
    finally:
        shutil.rmtree(cwd, ignore_errors=True)


def _tool_result_text(content):
    """Join a ToolResultBlock's ``content`` into plain text. Pure, never raises.

    Unlike the API backend (chat.py), the Claude Agent SDK delivers a tool result as
    the raw ToolResultBlock content — a str, or a list of ``{"type":"text","text":...}``
    blocks (the SDK's content-block shape), or None. Returns "" for anything else.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _parse_tool_payload(content):
    """Best-effort ``json.loads`` of a ToolResultBlock's text. None on any failure.

    The SAME `howto` payload the API backend reads as a dict arrives here as JSON TEXT,
    so we parse it back to a dict before reusing ``chat._extract_tour``. Any parse
    failure (non-JSON text, empty content) degrades to None — never a tour frame.
    """
    try:
        return json.loads(_tool_result_text(content))
    except (ValueError, TypeError):
        return None


async def run_conversation_turn(client, text, *, emit, on_sdk_session_id=None):
    """Drive one turn: send ``text``, stream the reply, map SDK messages -> frames.

    Emits the SAME frame protocol as chat.run_conversation_turn so the SPA is shared:
      {"type":"text","delta":<str>}                      per streamed text delta
      {"type":"tool_use","name":<str>,"input":<dict>}    when the model calls a tool
      {"type":"tool_result","name":<str>,"status":"ok"|"error"}  after a tool
      {"type":"error","detail":<str>}                    on a turn-level error
      {"type":"done"}                                    exactly once at the end

    Message dispatch is by class NAME (not isinstance) so the frame-mapping tests can
    feed fakes without importing the real SDK. Text is streamed ONLY via StreamEvent
    deltas; AssistantMessage TextBlocks are dropped to avoid emitting text twice.

    SESSION PERSISTENCE (Phase 4): v1 persists the DISPLAY transcript only for this
    backend — it does NOT resume the SDK conversation, so a restored Claude Code chat
    shows the earlier turns but the model won't recall them yet. As a forward step, when
    ``on_sdk_session_id`` is given it is called (best-effort) with the SDK's ``init``
    session id, which a future version can pass to ``client.query(..., resume=<id>)``.
    """
    await client.query(text, session_id="default")
    tool_names = {}  # tool_use_id -> qualified name, to label the matching result
    # Drop any tool-call markup the model streams as TEXT (tools-not-ready fallback) so
    # raw <function_calls> XML never renders as chat prose. Flushed before "done".
    text_filter = chat.ToolMarkupFilter()

    async for msg in client.receive_response():
        kind = type(msg).__name__

        if kind == "StreamEvent":
            event = getattr(msg, "event", None) or {}
            if event.get("type") == "content_block_delta":
                delta = event.get("delta") or {}
                if delta.get("type") == "text_delta":
                    safe = text_filter.feed(delta.get("text", ""))
                    if safe:
                        await emit({"type": "text", "delta": safe})
            continue

        if kind == "AssistantMessage":
            for block in getattr(msg, "content", None) or []:
                if type(block).__name__ == "ToolUseBlock":
                    tool_names[block.id] = block.name
                    await emit({"type": "tool_use",
                                "name": _strip_prefix(block.name),
                                "input": block.input})
            continue

        if kind == "UserMessage":
            content = getattr(msg, "content", None)
            if isinstance(content, list):
                for block in content:
                    if type(block).__name__ == "ToolResultBlock":
                        name = _strip_prefix(tool_names.get(block.tool_use_id, ""))
                        status = "error" if block.is_error else "ok"
                        await emit({"type": "tool_result", "name": name,
                                    "status": status})
                        if name == "howto" and status == "ok":
                            payload = _parse_tool_payload(block.content)
                            if payload is not None:
                                tour = chat._extract_tour(payload)
                                if tour is not None:
                                    await emit({"type": "tour", "data": tour})
                        if name == "propose_flow" and status == "ok":
                            # The SAME extractor the API backend uses (chat._extract_flow_draft)
                            # — one definition of the frame, so both backends can never drift.
                            payload = _parse_tool_payload(block.content)
                            if payload is not None:
                                draft = chat._extract_flow_draft(payload)
                                if draft is not None:
                                    await emit({"type": "flow_draft", **draft})
            continue

        if kind == "ResultMessage":
            tail = text_filter.flush()
            if tail:
                await emit({"type": "text", "delta": tail})
            if msg.is_error:
                await emit({"type": "error",
                            "detail": msg.result or "claude_code_turn_error"})
            await emit({"type": "done"})
            continue

        if kind == "SystemMessage":
            # The SDK's init SystemMessage carries the session id. Capture it (best-effort,
            # never raise) so the session record can store it for a future SDK resume.
            if on_sdk_session_id and getattr(msg, "subtype", None) == "init":
                try:
                    on_sdk_session_id((getattr(msg, "data", None) or {}).get("session_id"))
                except Exception:  # noqa: BLE001 — capture is best-effort, never break the turn
                    pass
            continue

        # SystemMessage task/hook subclasses and anything else: no-op.


async def handle_user_message(client, text, *, emit, live_url=None, draft=None,
                              on_sdk_session_id=None):
    """Run one turn. NEVER raises into the WebSocket; the client survives for reuse.

    Mirrors chat.handle_user_message: any ChatUnavailable / SDK / transport error is
    reported as a single structured {"type":"error", ...} frame. ``live_url`` (the live
    pane's current page) and ``draft`` (the flow document on screen) are folded in via the
    SHARED ``chat.augment_with_location`` / ``chat.augment_with_flow_draft`` so both
    backends learn the user's position and the live draft identically.
    ``on_sdk_session_id`` is threaded to run_conversation_turn to capture the SDK session
    id for a future resume.
    """
    prompt = chat.augment_with_flow_draft(
        chat.augment_with_location(text, live_url), draft)
    try:
        await run_conversation_turn(client, prompt, emit=emit,
                                    on_sdk_session_id=on_sdk_session_id)
    except chat.ChatUnavailable as e:
        await emit({"type": "error", "status": "chat_unavailable",
                    "reason": e.reason, "detail": e.detail})
    except Exception as e:  # noqa: BLE001 — SDK/transport errors never reach the WS
        await emit({"type": "error", "status": "chat_error", "detail": str(e)})
