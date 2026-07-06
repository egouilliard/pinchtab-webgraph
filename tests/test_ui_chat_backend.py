"""Tests for pinchtab_webgraph.ui.chat_backend — backend selection + dispatch.

No real SDK/anthropic/mcp needed: selection is pure (injected params) and the dispatch
tests monkeypatch the two backends' open/handle entry points with fakes.
"""
import asyncio
from contextlib import asynccontextmanager

from pinchtab_webgraph.ui import chat_backend


def _run(coro):
    return asyncio.run(coro)


# --- resolve_backend_name: every branch --------------------------------------

def test_resolve_forced_api_overrides_signals():
    assert chat_backend.resolve_backend_name(
        env={"PINCHTAB_UI_CHAT_BACKEND": "api"},
        has_api_key=False, claude_cli_available=True) == "api"


def test_resolve_forced_claude_code_overrides_signals():
    assert chat_backend.resolve_backend_name(
        env={"PINCHTAB_UI_CHAT_BACKEND": "claude_code"},
        has_api_key=True, claude_cli_available=False) == "claude_code"


def test_resolve_invalid_force_falls_through():
    # a bogus override value is ignored; the api-key signal then wins
    assert chat_backend.resolve_backend_name(
        env={"PINCHTAB_UI_CHAT_BACKEND": "bogus"},
        has_api_key=True) == "api"


def test_resolve_api_key_wins_over_cli():
    assert chat_backend.resolve_backend_name(
        env={}, has_api_key=True, claude_cli_available=True) == "api"


def test_resolve_cli_when_no_key():
    assert chat_backend.resolve_backend_name(
        env={}, has_api_key=False, claude_cli_available=True) == "claude_code"


def test_resolve_neither_defaults_to_api():
    assert chat_backend.resolve_backend_name(
        env={}, has_api_key=False, claude_cli_available=False) == "api"


def test_resolve_reads_api_key_from_env_when_not_injected():
    assert chat_backend.resolve_backend_name(
        env={"ANTHROPIC_API_KEY": "sk-x"}, claude_cli_available=False) == "api"


# --- open_chat_session: claude_code dispatch ---------------------------------

def test_open_chat_session_claude_code_dispatch(monkeypatch):
    fake_client = object()
    opened = []

    @asynccontextmanager
    async def fake_open_client(host, *, model=None):
        opened.append(host)
        yield fake_client

    recorded = {}

    async def fake_handle(client, text, *, emit, live_url=None):
        recorded["client"] = client
        recorded["text"] = text
        recorded["live_url"] = live_url
        await emit({"type": "done"})

    monkeypatch.setattr(chat_backend.chat_claude_code, "open_client", fake_open_client)
    monkeypatch.setattr(chat_backend.chat_claude_code, "handle_user_message", fake_handle)

    frames = []

    async def emit(f):
        frames.append(f)

    async def go():
        async with chat_backend.open_chat_session(
                "example.test", backend_name="claude_code") as session:
            assert isinstance(session, chat_backend._ClaudeCodeSession)
            await session.handle("hi there", emit=emit, live_url="https://example.test/y")

    _run(go())
    assert opened == ["example.test"]
    assert recorded["client"] is fake_client
    assert recorded["text"] == "hi there"
    assert recorded["live_url"] == "https://example.test/y"   # threaded through
    assert frames == [{"type": "done"}]


# --- open_chat_session: api dispatch -----------------------------------------

def test_open_chat_session_api_dispatch(monkeypatch):
    fake_state = object()

    @asynccontextmanager
    async def fake_open_api(host):
        yield fake_state

    recorded = {}

    async def fake_handle(state, text, *, emit, live_url=None):
        recorded["state"] = state
        recorded["text"] = text
        recorded["live_url"] = live_url
        await emit({"type": "done"})

    monkeypatch.setattr(chat_backend, "_open_api_session", fake_open_api)
    monkeypatch.setattr(chat_backend.chat, "handle_user_message", fake_handle)

    frames = []

    async def emit(f):
        frames.append(f)

    async def go():
        async with chat_backend.open_chat_session(
                "example.test", backend_name="api") as session:
            assert isinstance(session, chat_backend._ApiSession)
            await session.handle("hey", emit=emit, live_url="https://example.test/x")

    _run(go())
    assert recorded["state"] is fake_state
    assert recorded["text"] == "hey"
    assert recorded["live_url"] == "https://example.test/x"   # threaded through
    assert frames == [{"type": "done"}]


# --- ChatUnavailable re-export -----------------------------------------------

def test_chat_unavailable_reexport_is_chat_type():
    from pinchtab_webgraph.ui import chat
    assert chat_backend.ChatUnavailable is chat.ChatUnavailable
