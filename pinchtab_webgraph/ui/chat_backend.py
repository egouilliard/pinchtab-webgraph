#!/usr/bin/env python3
"""Chat backend selector + backend-agnostic session dispatch.

Phase 6 ADDS a second chat backend (Claude Code, via the Claude Agent SDK) ALONGSIDE
the existing Anthropic-API backend (chat.py) — it does NOT swap it. This module is the
single seam server.py talks to: it resolves which backend to use, opens a session, and
returns a uniform ``session`` object with one ``async handle(text, *, emit)`` method so
the WebSocket route is backend-agnostic. Both backends emit the SAME frame protocol, so
the SPA is untouched.

Stdlib-only at module scope (both ``chat`` and ``chat_claude_code`` are themselves
stdlib-only at import time — the heavy deps are lazy inside them), so server.py's
``from . import chat_backend`` keeps the base package a pure-stdlib install.
"""
import os
import shutil
from contextlib import asynccontextmanager
from dataclasses import dataclass

from . import chat, chat_claude_code, chat_store

# Re-export so server.py imports ChatUnavailable from ONE place (chat_backend) — both
# backends raise the SAME type (chat_claude_code reuses chat.ChatUnavailable).
ChatUnavailable = chat.ChatUnavailable


def resolve_backend_name(*, env=None, has_api_key=None, claude_cli_available=None):
    """Pick the chat backend: "api" or "claude_code".

    Precedence:
      1. PINCHTAB_UI_CHAT_BACKEND in {"api","claude_code"} — an explicit override wins.
      2. ANTHROPIC_API_KEY set -> "api" (a configured key is the strongest signal).
      3. the ``claude`` CLI on PATH -> "claude_code" (logged-in Claude Code available).
      4. otherwise -> "api" (so the caller gets chat.py's structured no_api_key frame).

    The keyword params are injection seams for tests; when None they read the real
    environment / PATH.
    """
    if env is None:
        env = os.environ
    forced = env.get("PINCHTAB_UI_CHAT_BACKEND")
    if forced in ("api", "claude_code"):
        return forced
    if has_api_key is None:
        has_api_key = bool(env.get("ANTHROPIC_API_KEY"))
    if has_api_key:
        return "api"
    if claude_cli_available is None:
        claude_cli_available = shutil.which("claude") is not None
    if claude_cli_available:
        return "claude_code"
    return "api"


@asynccontextmanager
async def _open_api_session(host, *, record=None, mode="workspace"):
    """Yield a ready ChatState for the Anthropic-API backend (chat.py).

    This body is the verbatim logic moved out of server.py's old ``_open_chat_session``
    — key -> anthropic client -> MCP session -> tools — so the API backend behaviour is
    byte-identical. When ``record`` carries persisted ``wire_messages`` (a resumed
    session), the ChatState is SEEDED with them so the model recalls prior turns. ``mode``
    selects the prompt and the tool fence (chat.effective_tool_names). Raises
    chat.ChatUnavailable when a dep/key is missing.
    """
    chat.require_api_key()
    client = chat.build_anthropic_client()
    seed = chat.deserialize_messages((record or {}).get("wire_messages") or [])
    async with chat.mcp_client_session() as session:
        tools = await chat.list_allowed_tools(session, mode)
        yield chat.ChatState(host=host, messages=seed, mcp_session=session,
                             anthropic_client=client, tools=tools, mode=mode)


@dataclass
class _ApiSession:
    """Uniform session wrapper over the API backend's ChatState + its session record."""
    state: object
    record: dict = None
    host: str = None

    async def handle(self, text, *, emit, live_url=None, draft=None):
        # Record the user turn (never emitted — the client already showed it), wrap emit so
        # every streamed frame is folded into the transcript, run the turn, then persist
        # both the display transcript and the authoritative wire history for resume.
        chat_store.append_display_frame(self.record, {"type": "user", "text": text})
        sink = chat_store.TranscriptSink(self.record, emit)
        await chat.handle_user_message(self.state, text, emit=sink, live_url=live_url,
                                       draft=draft)
        self.record["wire_messages"] = chat.serialize_messages(self.state.messages)
        chat_store.save(self.record)


@dataclass
class _ClaudeCodeSession:
    """Uniform session wrapper over the Claude Code backend's SDK client + its record."""
    client: object
    record: dict = None
    host: str = None

    async def handle(self, text, *, emit, live_url=None, draft=None):
        chat_store.append_display_frame(self.record, {"type": "user", "text": text})
        sink = chat_store.TranscriptSink(self.record, emit)

        def on_sdk_session_id(sid):
            # Capture the SDK's session id for a FUTURE resume (v1 restores display only).
            if sid:
                self.record["sdk_session_id"] = sid

        await chat_claude_code.handle_user_message(
            self.client, text, emit=sink, live_url=live_url, draft=draft,
            on_sdk_session_id=on_sdk_session_id)
        chat_store.save(self.record)


@asynccontextmanager
async def open_chat_session(host, *, backend_name=None, mode=None, record=None):
    """Open the selected backend and yield a uniform session with ``handle()``.

    A brand-new chat (``record is None``) mints one via chat_store.create with the
    resolved (or overridden) backend and the requested ``mode``. A RESUMED chat carries its
    record — and BOTH the backend and the mode are PINNED to the record, never re-resolved:
    a session opened under the API backend never silently continues under Claude Code, and
    a WORKSPACE session can never be resumed into flow mode (which would hand it the
    `propose_flow` tool it was never granted). ``backend_name``/``mode`` therefore apply
    only to a brand-new chat.
    """
    if record is None:
        record = chat_store.create(host, backend=(backend_name or resolve_backend_name()),
                                   mode=(mode or "workspace"))
    name = record["backend"]
    # PINNED from the record — the query param is irrelevant once a session exists.
    session_mode = record.get("mode") or "workspace"

    if name == "claude_code":
        async with chat_claude_code.open_client(host, mode=session_mode) as client:
            yield _ClaudeCodeSession(client=client, record=record, host=host)
    else:
        async with _open_api_session(host, record=record, mode=session_mode) as state:
            yield _ApiSession(state=state, record=record, host=host)
