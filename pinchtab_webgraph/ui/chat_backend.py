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

from . import chat, chat_claude_code

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
async def _open_api_session(host):
    """Yield a ready ChatState for the Anthropic-API backend (chat.py).

    This body is the verbatim logic moved out of server.py's old ``_open_chat_session``
    — key -> anthropic client -> MCP session -> tools — so the API backend behaviour is
    byte-identical. Raises chat.ChatUnavailable when a dep/key is missing.
    """
    chat.require_api_key()
    client = chat.build_anthropic_client()
    async with chat.mcp_client_session() as session:
        tools = await chat.list_allowed_tools(session)
        yield chat.ChatState(host=host, messages=[], mcp_session=session,
                             anthropic_client=client, tools=tools)


@dataclass
class _ApiSession:
    """Uniform session wrapper over the API backend's ChatState."""
    state: object

    async def handle(self, text, *, emit, live_url=None):
        await chat.handle_user_message(self.state, text, emit=emit, live_url=live_url)


@dataclass
class _ClaudeCodeSession:
    """Uniform session wrapper over the Claude Code backend's SDK client."""
    client: object

    async def handle(self, text, *, emit, live_url=None):
        await chat_claude_code.handle_user_message(self.client, text, emit=emit,
                                                   live_url=live_url)


@asynccontextmanager
async def open_chat_session(host, *, backend_name=None):
    """Open the selected backend and yield a uniform session with ``handle()``.

    ``backend_name`` overrides selection (tests pass it explicitly); when None the
    backend is resolved from the environment via ``resolve_backend_name``.
    """
    name = backend_name or resolve_backend_name()
    if name == "claude_code":
        async with chat_claude_code.open_client(host) as client:
            yield _ClaudeCodeSession(client)
    else:
        async with _open_api_session(host) as state:
            yield _ApiSession(state)
