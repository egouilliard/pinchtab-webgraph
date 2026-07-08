"""Tests for pinchtab_webgraph.ui.chat_backend — backend selection + dispatch.

No real SDK/anthropic/mcp needed: selection is pure (injected params) and the dispatch
tests monkeypatch the two backends' open/handle entry points with fakes.
"""
import asyncio
from contextlib import asynccontextmanager

from pinchtab_webgraph.ui import chat_backend, chat_store


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

class _FakeState:
    """A ChatState stand-in exposing a .messages list for serialize_messages()."""
    def __init__(self):
        self.messages = []


def test_open_chat_session_claude_code_dispatch(isolated_cache_home, monkeypatch):
    fake_client = object()
    opened = []

    @asynccontextmanager
    async def fake_open_client(host, *, model=None):
        opened.append(host)
        yield fake_client

    recorded = {}

    async def fake_handle(client, text, *, emit, live_url=None, on_sdk_session_id=None):
        recorded["client"] = client
        recorded["text"] = text
        recorded["live_url"] = live_url
        recorded["has_sdk_cb"] = callable(on_sdk_session_id)
        if on_sdk_session_id:
            on_sdk_session_id("sdk-123")     # exercise the capture callback
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
            assert session.record["backend"] == "claude_code"
            await session.handle("hi there", emit=emit, live_url="https://example.test/y")
            return session.record

    record = _run(go())
    assert opened == ["example.test"]
    assert recorded["client"] is fake_client
    assert recorded["text"] == "hi there"
    assert recorded["live_url"] == "https://example.test/y"   # threaded through
    assert recorded["has_sdk_cb"] is True
    assert frames == [{"type": "done"}]
    assert record["sdk_session_id"] == "sdk-123"              # captured via callback
    # persisted: the user turn is in the transcript.
    reloaded = chat_store.load("example.test", record["id"])
    assert reloaded["sdk_session_id"] == "sdk-123"
    assert reloaded["wire_messages"] is None                  # display-only for cc
    assert [e["type"] for e in reloaded["transcript"]] == ["user"]


# --- open_chat_session: api dispatch -----------------------------------------

def test_open_chat_session_api_dispatch(isolated_cache_home, monkeypatch):
    fake_state = _FakeState()

    @asynccontextmanager
    async def fake_open_api(host, *, record=None):
        yield fake_state

    recorded = {}

    async def fake_handle(state, text, *, emit, live_url=None):
        recorded["state"] = state
        recorded["text"] = text
        recorded["live_url"] = live_url
        state.messages.append({"role": "user", "content": text})
        await emit({"type": "text", "delta": "ok"})
        # a real turn ends with the assistant reply — without it the trailing bare user
        # turn is (correctly) dropped from wire_messages as error residue.
        state.messages.append({"role": "assistant",
                               "content": [{"type": "text", "text": "ok"}]})
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
            return session.record

    record = _run(go())
    assert recorded["state"] is fake_state
    assert recorded["text"] == "hey"
    assert recorded["live_url"] == "https://example.test/x"   # threaded through
    assert frames == [{"type": "text", "delta": "ok"}, {"type": "done"}]
    # persisted per turn: wire_messages seeded + transcript folded.
    reloaded = chat_store.load("example.test", record["id"])
    assert reloaded["wire_messages"] == [
        {"role": "user", "content": "hey"},
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}]
    assert [e["type"] for e in reloaded["transcript"]] == ["user", "text"]


# --- backend PINNED on resume (never re-resolved) ----------------------------

def test_open_chat_session_pins_backend_from_record(isolated_cache_home, monkeypatch):
    # A record created under "api" must resume under "api" EVEN IF resolve_backend_name
    # would now pick claude_code. open_chat_session must read record["backend"], not resolve.
    monkeypatch.setattr(chat_backend, "resolve_backend_name",
                        lambda *a, **k: "claude_code")
    record = chat_store.create("example.test", backend="api")

    @asynccontextmanager
    async def fake_open_api(host, *, record=None):
        yield _FakeState()

    monkeypatch.setattr(chat_backend, "_open_api_session", fake_open_api)

    async def go():
        async with chat_backend.open_chat_session(
                "example.test", record=record) as session:
            return session

    session = _run(go())
    assert isinstance(session, chat_backend._ApiSession)     # api, NOT claude_code


# --- ChatUnavailable re-export -----------------------------------------------

def test_chat_unavailable_reexport_is_chat_type():
    from pinchtab_webgraph.ui import chat
    assert chat_backend.ChatUnavailable is chat.ChatUnavailable
