"""Tests for pinchtab_webgraph.ui.chat + the /ws/chat WebSocket route.

Guarded by importorskip("anthropic")/importorskip("mcp") so a base run without the
UI extra skips cleanly. EVERYTHING is exercised with a MOCKED Anthropic client and a
MOCKED MCP session injected via ChatState — NO test needs a real ANTHROPIC_API_KEY,
a real Anthropic network call, or a real MCP subprocess.

The load-bearing shapes (fakes below) match the real SDKs at the attribute level:
  * a stream is an async context manager that `async for`-yields text events and
    exposes `await get_final_message()` -> {stop_reason, content};
  * a CallToolResult exposes `structuredContent` + `content` (TextContent blocks);
  * an MCP Tool exposes `name`, `description`, `inputSchema`.
"""
import asyncio

import pytest

pytest.importorskip("anthropic")
pytest.importorskip("mcp")

from pinchtab_webgraph.ui import chat, chat_backend


# --- fakes -------------------------------------------------------------------

class FakeTextEvent:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class FakeTextBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class FakeToolUseBlock:
    def __init__(self, id, name, input):
        self.type = "tool_use"
        self.id = id
        self.name = name
        self.input = input


class FakeFinalMessage:
    def __init__(self, stop_reason, content):
        self.stop_reason = stop_reason
        self.content = content


class FakeStream:
    """Async CM that yields scripted text events, then a scripted final message."""

    def __init__(self, events, final):
        self._events = events
        self._final = final

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def __aiter__(self):
        for e in self._events:
            yield e

    async def get_final_message(self):
        return self._final


class FakeMessages:
    def __init__(self, streams):
        self._streams = list(streams)
        self.calls = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        return self._streams.pop(0)


class FakeAnthropic:
    def __init__(self, streams):
        self.messages = FakeMessages(streams)


class FakeContentBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class FakeCallToolResult:
    def __init__(self, structuredContent=None, content=None):
        self.structuredContent = structuredContent
        self.content = content or []
        self.isError = False


class FakeMCPSession:
    def __init__(self, result=None, raises=None, tools=None):
        self._result = result
        self._raises = raises
        self._tools = tools or []
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self._raises is not None:
            raise self._raises
        return self._result

    async def list_tools(self):
        return FakeListToolsResult(self._tools)


class FakeTool:
    def __init__(self, name, description, inputSchema):
        self.name = name
        self.description = description
        self.inputSchema = inputSchema


class FakeListToolsResult:
    def __init__(self, tools):
        self.tools = tools


# --- _anthropic_tool_from_mcp -------------------------------------------------

def test_anthropic_tool_from_mcp_renames_input_schema():
    tool = FakeTool("howto", "shortest click-path", {"type": "object", "properties": {}})
    spec = chat._anthropic_tool_from_mcp(tool)
    assert spec["name"] == "howto"
    assert spec["description"] == "shortest click-path"
    assert spec["input_schema"] == {"type": "object", "properties": {}}
    assert "inputSchema" not in spec


def test_anthropic_tool_from_mcp_none_description():
    tool = FakeTool("howto", None, {"type": "object"})
    assert chat._anthropic_tool_from_mcp(tool)["description"] == ""


# --- list_allowed_tools -------------------------------------------------------

def test_list_allowed_tools_filters_to_offline_set():
    tools = [
        FakeTool("graph_summary", "d", {"type": "object"}),
        FakeTool("howto", "d", {"type": "object"}),
        FakeTool("find_content", "d", {"type": "object"}),
        FakeTool("list_content", "d", {"type": "object"}),
        FakeTool("list_forms", "d", {"type": "object"}),
        FakeTool("link_paths", "d", {"type": "object"}),
        FakeTool("crawl", "LIVE", {"type": "object"}),       # must be dropped
        FakeTool("ask_howto", "LIVE", {"type": "object"}),   # must be dropped
    ]
    session = FakeMCPSession(tools=tools)
    allowed = asyncio.run(chat.list_allowed_tools(session))
    names = {t["name"] for t in allowed}
    assert names == set(chat.OFFLINE_TOOL_NAMES)
    assert "crawl" not in names
    assert "ask_howto" not in names


# --- require_api_key ----------------------------------------------------------

def test_require_api_key_raises_when_unset(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(chat.ChatUnavailable) as ei:
        chat.require_api_key()
    assert ei.value.reason == "no_api_key"


def test_require_api_key_returns_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")
    assert chat.require_api_key() == "sk-test-123"


# --- resolve_model ------------------------------------------------------------

def test_resolve_model_default(monkeypatch):
    monkeypatch.delenv(chat.MODEL_ENV_VAR, raising=False)
    assert chat.resolve_model() == chat.DEFAULT_MODEL


def test_resolve_model_env_override(monkeypatch):
    monkeypatch.setenv(chat.MODEL_ENV_VAR, "claude-sonnet-test")
    assert chat.resolve_model() == "claude-sonnet-test"


# --- build_system_prompt ------------------------------------------------------

def test_build_system_prompt_pins_host():
    prompt = chat.build_system_prompt("app.example.com")
    assert "app.example.com" in prompt
    assert 'host="app.example.com"' in prompt


# --- run_tool -----------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


def test_run_tool_structured_content_path():
    result = FakeCallToolResult(structuredContent={"status": "ok", "results": [1]})
    session = FakeMCPSession(result=result)
    payload = _run(chat.run_tool(session, "howto", {"host": "h", "goal": "g"}))
    assert payload == {"status": "ok", "results": [1]}
    assert session.calls == [("howto", {"host": "h", "goal": "g"})]


def test_run_tool_text_json_path():
    result = FakeCallToolResult(
        structuredContent=None,
        content=[FakeContentBlock('{"status": "no_match"}')])
    session = FakeMCPSession(result=result)
    payload = _run(chat.run_tool(session, "howto", {"host": "h"}))
    assert payload == {"status": "no_match"}


def test_run_tool_raw_text_when_not_json():
    result = FakeCallToolResult(
        structuredContent=None, content=[FakeContentBlock("plain text")])
    session = FakeMCPSession(result=result)
    payload = _run(chat.run_tool(session, "howto", {"host": "h"}))
    assert payload == {"raw": "plain text"}


def test_run_tool_exception_path():
    session = FakeMCPSession(raises=RuntimeError("transport boom"))
    payload = _run(chat.run_tool(session, "howto", {"host": "h"}))
    assert payload["status"] == "tool_error"
    assert "transport boom" in payload["detail"]


def test_run_tool_iserror_becomes_tool_error():
    # An MCP-level failure (isError=True, non-JSON error text) must surface as
    # tool_error — NOT be swallowed into a green {"raw": ...} "ok" payload.
    result = FakeCallToolResult(
        structuredContent=None, content=[FakeContentBlock("boom: handler raised")])
    result.isError = True
    session = FakeMCPSession(result=result)
    payload = _run(chat.run_tool(session, "howto", {"host": "h"}))
    assert payload["status"] == "tool_error"
    assert "boom: handler raised" in payload["detail"]
    # and the loop's status classifier agrees it FAILED.
    assert chat._tool_status(payload) == "error"


# --- _extract_tour: pure "Show Me How" tour extraction ------------------------

def _howto_ok_payload():
    return {"status": "ok", "goal": "create role",
            "start_url": "https://example.test/dashboard",
            "results": [{"trigger_label": "Create Role",
                         "opens_at": "https://example.test/team/roles",
                         "form": {"fieldCount": 1},
                         "tour": [{"kind": "nav", "label": "Team", "selector": "a", "href": None},
                                  {"kind": "trigger", "label": "Create Role",
                                   "selector": None, "href": None},
                                  {"kind": "form"}]}]}


def test_extract_tour_from_ok_payload():
    tour = chat._extract_tour(_howto_ok_payload())
    assert tour["goal"] == "create role"
    assert tour["start_url"] == "https://example.test/dashboard"
    assert tour["trigger_label"] == "Create Role"
    assert tour["opens_at"] == "https://example.test/team/roles"
    assert tour["form"] == {"fieldCount": 1}
    assert [s["kind"] for s in tour["steps"]] == ["nav", "trigger", "form"]


def test_extract_tour_none_when_not_ok():
    assert chat._extract_tour({"status": "no_match", "results": []}) is None
    assert chat._extract_tour({"status": "ok", "results": []}) is None
    assert chat._extract_tour("not a dict") is None
    assert chat._extract_tour({"status": "ok"}) is None


# --- run_conversation_turn: full stream + tool-use loop -----------------------

def test_run_conversation_turn_streams_and_runs_tool():
    # turn 1: some text, then a tool_use block (stop_reason "tool_use").
    stream1 = FakeStream(
        events=[FakeTextEvent("Let me "), FakeTextEvent("check. ")],
        final=FakeFinalMessage(
            "tool_use",
            [FakeToolUseBlock("t1", "howto", {"host": "example.test", "goal": "create role"})]))
    # turn 2: final text (stop_reason "end_turn").
    stream2 = FakeStream(
        events=[FakeTextEvent("Click Roles, then Create.")],
        final=FakeFinalMessage("end_turn", [FakeTextBlock("Click Roles, then Create.")]))

    client = FakeAnthropic([stream1, stream2])
    tool_result = FakeCallToolResult(
        structuredContent={"status": "ok", "results": [{"trigger_label": "Create Role"}]})
    session = FakeMCPSession(result=tool_result)

    state = chat.ChatState(host="example.test", messages=[{"role": "user", "content": "how?"}],
                           mcp_session=session, anthropic_client=client, tools=[])

    frames = []

    async def emit(frame):
        frames.append(frame)

    _run(chat.run_conversation_turn(state, emit=emit))

    types = [f["type"] for f in frames]
    # ordered: text..., tool_use(howto), tool_result(ok), tour, text..., done
    # (howto returned status:"ok" with a non-empty results list, so a tour frame is
    # emitted right after the tool_result — see _extract_tour.)
    assert types == ["text", "text", "tool_use", "tool_result", "tour", "text", "done"]

    tool_use = next(f for f in frames if f["type"] == "tool_use")
    assert tool_use["name"] == "howto"
    assert tool_use["input"] == {"host": "example.test", "goal": "create role"}

    tool_res = next(f for f in frames if f["type"] == "tool_result")
    assert tool_res["name"] == "howto"
    assert tool_res["status"] == "ok"

    # the tool_result turn was appended to state.messages.
    user_tool_turns = [m for m in state.messages
                       if m["role"] == "user" and isinstance(m["content"], list)]
    assert len(user_tool_turns) == 1
    block = user_tool_turns[0]["content"][0]
    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "t1"
    assert block["is_error"] is False
    # the tool was actually invoked with the model-provided args.
    assert session.calls == [("howto", {"host": "example.test", "goal": "create role"})]


def test_run_conversation_turn_emits_tour_frame_after_howto():
    # A howto tool call that returns status:"ok" with a tour -> a {"type":"tour"} frame
    # emitted immediately AFTER the tool_result frame.
    stream1 = FakeStream(
        events=[],
        final=FakeFinalMessage(
            "tool_use",
            [FakeToolUseBlock("t1", "howto", {"host": "example.test", "goal": "create role"})]))
    stream2 = FakeStream(
        events=[FakeTextEvent("Click Team, then Create Role.")],
        final=FakeFinalMessage("end_turn", [FakeTextBlock("done")]))
    client = FakeAnthropic([stream1, stream2])
    session = FakeMCPSession(result=FakeCallToolResult(structuredContent=_howto_ok_payload()))
    state = chat.ChatState(host="example.test", messages=[{"role": "user", "content": "how?"}],
                           mcp_session=session, anthropic_client=client, tools=[])
    frames = []

    async def emit(frame):
        frames.append(frame)

    _run(chat.run_conversation_turn(state, emit=emit))

    types = [f["type"] for f in frames]
    assert types == ["tool_use", "tool_result", "tour", "text", "done"]
    # the tour frame carries the extracted tour, keyed on the first result.
    tour = next(f for f in frames if f["type"] == "tour")["data"]
    assert tour["trigger_label"] == "Create Role"
    assert [s["kind"] for s in tour["steps"]] == ["nav", "trigger", "form"]


def test_run_conversation_turn_no_tour_frame_when_not_ok():
    # a howto miss (status != ok / empty results) must NOT emit a tour frame.
    stream1 = FakeStream(
        events=[],
        final=FakeFinalMessage(
            "tool_use", [FakeToolUseBlock("t1", "howto", {"host": "h", "goal": "x"})]))
    stream2 = FakeStream(
        events=[FakeTextEvent("No path.")],
        final=FakeFinalMessage("end_turn", [FakeTextBlock("No path.")]))
    client = FakeAnthropic([stream1, stream2])
    session = FakeMCPSession(
        result=FakeCallToolResult(structuredContent={"status": "no_match", "results": []}))
    state = chat.ChatState(host="h", messages=[{"role": "user", "content": "how?"}],
                           mcp_session=session, anthropic_client=client, tools=[])
    frames = []

    async def emit(frame):
        frames.append(frame)

    _run(chat.run_conversation_turn(state, emit=emit))
    assert "tour" not in [f["type"] for f in frames]


def test_run_conversation_turn_iteration_limit():
    # every stream asks for a tool -> the loop hits its bound and emits the error frame.
    def make_stream():
        return FakeStream(
            events=[],
            final=FakeFinalMessage(
                "tool_use", [FakeToolUseBlock("t", "howto", {"host": "h"})]))

    client = FakeAnthropic([make_stream() for _ in range(3)])
    session = FakeMCPSession(result=FakeCallToolResult(structuredContent={"status": "ok"}))
    state = chat.ChatState(host="h", messages=[{"role": "user", "content": "x"}],
                           mcp_session=session, anthropic_client=client, tools=[])
    frames = []

    async def emit(frame):
        frames.append(frame)

    _run(chat.run_conversation_turn(state, emit=emit, max_tool_iterations=3))
    assert frames[-2] == {"type": "error", "detail": "tool_iteration_limit_exceeded"}
    assert frames[-1] == {"type": "done"}


def test_run_conversation_turn_aggregates_multiple_tool_uses():
    # TWO tool_use blocks in one assistant turn -> ONE follow-up user turn carrying
    # BOTH tool_results, each keyed by its own tool_use_id.
    stream1 = FakeStream(
        events=[],
        final=FakeFinalMessage("tool_use", [
            FakeToolUseBlock("t1", "howto", {"host": "h", "goal": "a"}),
            FakeToolUseBlock("t2", "list_forms", {"host": "h"})]))
    stream2 = FakeStream(
        events=[FakeTextEvent("done")],
        final=FakeFinalMessage("end_turn", [FakeTextBlock("done")]))
    client = FakeAnthropic([stream1, stream2])
    session = FakeMCPSession(result=FakeCallToolResult(structuredContent={"status": "ok"}))
    state = chat.ChatState(host="h", messages=[{"role": "user", "content": "x"}],
                           mcp_session=session, anthropic_client=client, tools=[])

    async def emit(_frame):
        pass

    _run(chat.run_conversation_turn(state, emit=emit))
    user_tool_turns = [m for m in state.messages
                       if m["role"] == "user" and isinstance(m["content"], list)]
    assert len(user_tool_turns) == 1
    ids = [b["tool_use_id"] for b in user_tool_turns[0]["content"]]
    assert ids == ["t1", "t2"]
    assert len(session.calls) == 2


def test_run_conversation_turn_rolls_back_dangling_tooluse_on_restream_failure():
    # turn 1 asks for a tool (appends assistant tool_use turn + tool_result turn);
    # turn 2's stream RAISES. The loop must roll BOTH appended turns back off
    # state.messages, so no assistant tool_use turn is left without its tool_result
    # (which the real API would 400 on for the NEXT user message).
    class BoomStream:
        async def __aenter__(self):
            raise RuntimeError("stream 2 down")

        async def __aexit__(self, *a):
            return False

    stream1 = FakeStream(
        events=[],
        final=FakeFinalMessage("tool_use", [FakeToolUseBlock("t1", "howto", {"host": "h"})]))
    client = FakeAnthropic([stream1, BoomStream()])
    session = FakeMCPSession(result=FakeCallToolResult(structuredContent={"status": "ok"}))
    state = chat.ChatState(host="h", messages=[{"role": "user", "content": "how?"}],
                           mcp_session=session, anthropic_client=client, tools=[])
    frames = []

    async def emit(frame):
        frames.append(frame)

    # handle_user_message swallows the raised error into one error frame.
    _run(chat.handle_user_message(state, "how?", emit=emit))
    assert frames[-1]["type"] == "error"
    # history is clean: the initial user turns only, no dangling assistant tool_use turn.
    assert all(not (m["role"] == "assistant") for m in state.messages)
    assert state.messages[-1]["role"] == "user"
    assert isinstance(state.messages[-1]["content"], str)


# --- handle_user_message: never raises ---------------------------------------

def test_handle_user_message_reports_error_frame():
    class BoomMessages:
        def stream(self, **kwargs):
            raise RuntimeError("api down")

    class BoomClient:
        messages = BoomMessages()

    state = chat.ChatState(host="h", messages=[], mcp_session=FakeMCPSession(),
                           anthropic_client=BoomClient(), tools=[])
    frames = []

    async def emit(frame):
        frames.append(frame)

    _run(chat.handle_user_message(state, "hello", emit=emit))
    assert state.messages[0] == {"role": "user", "content": "hello"}
    assert frames[-1]["type"] == "error"
    assert frames[-1]["status"] == "chat_error"
    assert "api down" in frames[-1]["detail"]


# --- WebSocket route ----------------------------------------------------------

fastapi = pytest.importorskip("fastapi")
from contextlib import asynccontextmanager  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from pinchtab_webgraph.ui import server as ui_server  # noqa: E402

ws_client = TestClient(ui_server.app)


def test_ws_chat_streams_scripted_frames(monkeypatch):
    # A fake session whose real handle_user_message loop runs against injected fakes
    # and emits a scripted single-turn reply (text + done, no tool use).
    stream = FakeStream(
        events=[FakeTextEvent("Hello there.")],
        final=FakeFinalMessage("end_turn", [FakeTextBlock("Hello there.")]))
    client = FakeAnthropic([stream])
    session = FakeMCPSession(result=FakeCallToolResult(structuredContent={"status": "ok"}))

    @asynccontextmanager
    async def fake_open(host):
        state = chat.ChatState(host=host, messages=[], mcp_session=session,
                               anthropic_client=client, tools=[])
        yield chat_backend._ApiSession(state)

    monkeypatch.setattr(ui_server.chat_backend, "open_chat_session", fake_open)

    with ws_client.websocket_connect("/ws/chat?host=example.test") as ws:
        ws.send_json({"type": "user_message", "text": "hi"})
        f1 = ws.receive_json()
        f2 = ws.receive_json()
        assert f1 == {"type": "text", "delta": "Hello there."}
        assert f2 == {"type": "done"}


def test_ws_chat_invalid_host_error_and_close():
    with ws_client.websocket_connect("/ws/chat?host=bad%20host") as ws:
        frame = ws.receive_json()
        assert frame["type"] == "error"
        assert frame["status"] == "invalid_host"
