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

    def model_dump(self, mode=None):
        # match the real anthropic block: serialize_messages folds content blocks via
        # model_dump(mode="json") when a turn is persisted for resume.
        return {"type": self.type, "text": self.text}


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


def _fake_session_record(host):
    """A minimal in-memory session record — enough for chat_store.summary + the
    TranscriptSink/save path the real _ApiSession.handle drives."""
    return {"id": "0" * 32, "host": host, "backend": "api", "title": None,
            "title_locked": False, "created_at": "t", "updated_at": "t",
            "message_count": 0, "sdk_session_id": None, "transcript": [],
            "wire_messages": [], "_disk_len": 0}


def test_ws_chat_streams_scripted_frames(isolated_cache_home, monkeypatch):
    # A fake session whose real handle_user_message loop runs against injected fakes
    # and emits a scripted single-turn reply (text + done, no tool use). The leading
    # bootstrap `session` frame is consumed first. isolated_cache_home keeps the per-turn
    # chat_store.save writes off the real ~/.pinchtab-webgraph.
    stream = FakeStream(
        events=[FakeTextEvent("Hello there.")],
        final=FakeFinalMessage("end_turn", [FakeTextBlock("Hello there.")]))
    client = FakeAnthropic([stream])
    session = FakeMCPSession(result=FakeCallToolResult(structuredContent={"status": "ok"}))

    @asynccontextmanager
    async def fake_open(host, *, backend_name=None, mode=None, record=None):
        state = chat.ChatState(host=host, messages=[], mcp_session=session,
                               anthropic_client=client, tools=[])
        yield chat_backend._ApiSession(state=state,
                                       record=record or _fake_session_record(host),
                                       host=host)

    monkeypatch.setattr(ui_server.chat_backend, "open_chat_session", fake_open)

    with ws_client.websocket_connect("/ws/chat?host=example.test") as ws:
        boot = ws.receive_json()
        assert boot["type"] == "session"
        assert boot["transcript"] == []
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


# --- augment_with_location (live-position awareness) ---------------------------

def test_augment_with_location_none_returns_text_unchanged():
    assert chat.augment_with_location("hello", None) == "hello"
    assert chat.augment_with_location("hello", "") == "hello"


def test_augment_with_location_prefixes_url_and_start_instruction():
    out = chat.augment_with_location("how do I add a CAE?", "https://site.test/settings")
    assert "https://site.test/settings" in out
    assert 'start="https://site.test/settings"' in out
    assert out.endswith("how do I add a CAE?")   # original text preserved at the end


def test_handle_user_message_folds_live_url_into_the_turn():
    # handle_user_message should append a user turn whose content carries the live URL.
    async def fake_turn(state, *, emit):
        pass

    import asyncio
    from unittest import mock
    state = chat.ChatState(host="site.test")
    with mock.patch.object(chat, "run_conversation_turn", side_effect=fake_turn):
        asyncio.run(chat.handle_user_message(state, "where are stages?",
                                             emit=lambda f: None,
                                             live_url="https://site.test/settings"))
    assert state.messages[-1]["role"] == "user"
    assert "https://site.test/settings" in state.messages[-1]["content"]


# --- ToolMarkupFilter: strip leaked <function_calls> tool-call markup from text ------

def _run_filter(deltas):
    from pinchtab_webgraph.ui.chat import ToolMarkupFilter
    f = ToolMarkupFilter()
    out = "".join(f.feed(d) for d in deltas)
    return out + f.flush()


def test_tool_markup_filter_strips_single_delta_leak():
    leak = ('I\'ll list the forms.\n<function_calls>\n<invoke name="list_forms">\n'
            '<parameter name="host">go-staging.leyton.com</parameter>\n</invoke>\n</function_calls>')
    assert _run_filter([leak]) == "I'll list the forms.\n"


def test_tool_markup_filter_strips_across_streamed_deltas():
    leak = 'before<function_calls><invoke name="x"></invoke></function_calls>after'
    # fed one character at a time (worst-case streaming) the markup still vanishes
    assert _run_filter(list(leak)) == "beforeafter"


def test_tool_markup_filter_passes_normal_text():
    assert _run_filter(["Go to ", "Settings, ", "then click Add."]) == "Go to Settings, then click Add."


def test_tool_markup_filter_open_tag_split_on_boundary():
    assert _run_filter(["hi <function_", "calls>junk</function_calls> bye"]) == "hi  bye"


# --- session persistence: serialize / deserialize / trim wire messages --------

class FakeBlock:
    """A stand-in for an anthropic 0.85.0 content block exposing model_dump(mode=...)."""

    def __init__(self, d):
        self._d = d

    def model_dump(self, mode=None):
        assert mode == "json"      # serialize_messages MUST pass mode="json"
        return dict(self._d)


def test_serialize_messages_maps_blocks_through_model_dump():
    messages = [
        {"role": "user", "content": "how do I add a role?"},
        {"role": "assistant", "content": [FakeBlock({"type": "text", "text": "sure"})]},
    ]
    out = chat.serialize_messages(messages)
    assert out[0] == {"role": "user", "content": "how do I add a role?"}
    assert out[1] == {"role": "assistant", "content": [{"type": "text", "text": "sure"}]}


def test_serialize_messages_passes_plain_dict_blocks_through():
    messages = [{"role": "user",
                 "content": [{"type": "tool_result", "tool_use_id": "t1",
                              "content": "{}", "is_error": False}]}]
    out = chat.serialize_messages(messages)
    assert out[0]["content"][0]["tool_use_id"] == "t1"


def test_serialize_messages_truncates_long_strings():
    big = "x" * (chat.MAX_WIRE_TEXT_CHARS + 500)
    # A completed turn (user + assistant reply) — the assistant turn keeps the user turn
    # from being treated as unanswered error-residue and dropped.
    messages = [{"role": "user", "content": big},
                {"role": "assistant", "content": [FakeBlock({"type": "text", "text": "ok"})]}]
    out = chat.serialize_messages(messages)
    assert out[0]["content"].endswith("…[truncated]")
    assert len(out[0]["content"]) < len(big)


def test_serialize_messages_drops_trailing_unanswered_user_turn():
    # An error mid-turn can leave a bare user message with no assistant reply. Persisting it
    # would make the next resumed send two consecutive user turns (Anthropic API 400) — it
    # must be dropped. A tool_result trailing "user" turn (a list) is NOT dropped.
    completed = [{"role": "user", "content": "q1"},
                 {"role": "assistant", "content": [FakeBlock({"type": "text", "text": "a1"})]}]
    residue = completed + [{"role": "user", "content": "q2-unanswered"}]
    out = chat.serialize_messages(residue)
    assert len(out) == 2 and out[-1]["role"] == "assistant"
    # a normal completed conversation is preserved intact
    assert len(chat.serialize_messages(completed)) == 2


def test_serialize_deserialize_round_trip():
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [FakeBlock({"type": "text", "text": "hello"})]},
    ]
    wire = chat.serialize_messages(messages)
    back = chat.deserialize_messages(wire)
    assert back == wire


def test_deserialize_messages_defensive_on_garbage():
    assert chat.deserialize_messages("not a list") == []
    assert chat.deserialize_messages([{"role": "user", "content": "ok"}, 42, "x"]) == \
        [{"role": "user", "content": "ok"}]


def test_trim_wire_messages_keeps_trailing_turns_and_never_splits_tool_pairs():
    # Build 5 user turns, each: user(str) -> assistant tool_use(list) -> tool_result(list).
    messages = []
    for i in range(5):
        messages.append({"role": "user", "content": "q%d" % i})
        messages.append({"role": "assistant",
                         "content": [{"type": "tool_use", "id": "t%d" % i, "name": "howto"}]})
        messages.append({"role": "user",
                         "content": [{"type": "tool_result", "tool_use_id": "t%d" % i}]})
    trimmed = chat.trim_wire_messages(messages, max_turns=2)
    # keeps the last 2 genuine user turns (q3, q4) and everything after -> 6 messages.
    assert len(trimmed) == 6
    # starts on a genuine user turn (a plain-str content), NEVER a tool_use/tool_result.
    assert trimmed[0]["role"] == "user" and isinstance(trimmed[0]["content"], str)
    assert trimmed[0]["content"] == "q3"
    # every assistant tool_use turn is immediately followed by its tool_result turn.
    for i, m in enumerate(trimmed):
        if m["role"] == "assistant" and isinstance(m["content"], list):
            nxt = trimmed[i + 1]
            assert nxt["role"] == "user" and isinstance(nxt["content"], list)
            assert nxt["content"][0]["type"] == "tool_result"


def test_trim_wire_messages_under_cap_is_identity():
    messages = [{"role": "user", "content": "only one"}]
    assert chat.trim_wire_messages(messages, max_turns=100) == messages


# --- FLOW MODE: the tool fence, the prompt, the draft frame -------------------
#
# The safety shape: the base browsing fence (OFFLINE_TOOL_NAMES) is NEVER widened —
# flow mode only ADDS the one PURE tool (propose_flow), which cannot save or run.

def test_offline_fence_is_still_exactly_six_names():
    # A regression guard on the fence itself: flow mode must not have leaked into it.
    assert chat.OFFLINE_TOOL_NAMES == frozenset({
        "graph_summary", "howto", "find_content", "list_content", "list_forms",
        "link_paths"})
    assert len(chat.OFFLINE_TOOL_NAMES) == 6
    assert "propose_flow" not in chat.OFFLINE_TOOL_NAMES


def test_effective_tool_names_workspace_is_the_base_fence():
    assert chat.effective_tool_names("workspace") == chat.OFFLINE_TOOL_NAMES
    assert chat.effective_tool_names() == chat.OFFLINE_TOOL_NAMES


def test_effective_tool_names_flow_adds_exactly_propose_flow():
    assert chat.effective_tool_names("flow") == chat.OFFLINE_TOOL_NAMES | {"propose_flow"}
    assert len(chat.effective_tool_names("flow")) == 7


def test_effective_tool_names_unknown_mode_fails_closed():
    # an unrecognized token must never be the thing that grants a tool.
    assert chat.effective_tool_names("bogus") == chat.OFFLINE_TOOL_NAMES
    assert chat.effective_tool_names(None) == chat.OFFLINE_TOOL_NAMES


def test_list_allowed_tools_flow_mode_includes_propose_flow():
    tools = [FakeTool(n, "d", {"type": "object"}) for n in
             ("graph_summary", "howto", "find_content", "list_content", "list_forms",
              "link_paths", "propose_flow", "crawl", "ask_howto", "perform")]
    session = FakeMCPSession(tools=tools)

    workspace = {t["name"] for t in asyncio.run(chat.list_allowed_tools(session))}
    assert workspace == set(chat.OFFLINE_TOOL_NAMES)      # NO propose_flow in workspace

    flow_names = {t["name"] for t in asyncio.run(chat.list_allowed_tools(session, "flow"))}
    assert flow_names == set(chat.OFFLINE_TOOL_NAMES) | {"propose_flow"}
    # the LIVE tools are in NEITHER set.
    for live in ("crawl", "ask_howto", "perform"):
        assert live not in workspace and live not in flow_names


def test_flow_system_prompt_is_generated_from_the_flow_tables():
    from pinchtab_webgraph import flow as flow_mod
    prompt = chat.build_flow_system_prompt("site.test")
    assert "site.test" in prompt
    # every op the validator knows is named in the prompt — the reference is DERIVED, so
    # adding an op to flow.py cannot leave the prompt behind.
    for op in list(flow_mod.LEAF_OPS) + list(flow_mod.BODY_OPS):
        assert op in prompt
    for cap in flow_mod.DEFAULT_CAPABILITIES:
        assert cap in prompt
    assert str(flow_mod.MAX_STEPS) in prompt and str(flow_mod.MAX_DEPTH) in prompt
    # the authority rule + the grounding rule are stated explicitly.
    assert "only PROPOSE" in prompt
    assert "propose_flow" in prompt
    assert "Save/Run buttons" in prompt
    assert "Never invent a selector" in prompt


def test_system_prompt_for_picks_by_mode():
    assert chat.system_prompt_for("h", "workspace") == chat.build_system_prompt("h")
    assert chat.system_prompt_for("h") == chat.build_system_prompt("h")
    assert chat.system_prompt_for("h", "flow") == chat.build_flow_system_prompt("h")


# --- _extract_flow_draft (the exact twin of _extract_tour) --------------------

def _propose_ok_payload():
    doc = {"name": "invoices", "host": "x.test",
           "steps": [{"op": "goto", "url": "https://x.test/invoices"}]}
    return {"status": "ok", "name": "invoices", "host": "x.test", "steps": 1,
            "capabilities": {}, "inputs": [], "doc": doc, "note": "first draft"}


def test_extract_flow_draft_from_ok_payload():
    draft = chat._extract_flow_draft(_propose_ok_payload())
    assert draft["status"] == "ok"
    assert draft["name"] == "invoices"
    assert draft["note"] == "first draft"
    assert draft["doc"]["steps"][0]["op"] == "goto"


def test_extract_flow_draft_invalid_still_yields_a_frame():
    # an INVALID draft still carries the doc + path/error — the human must see the broken
    # document and the reason, not nothing.
    draft = chat._extract_flow_draft(
        {"status": "invalid", "path": "steps[0]", "error": "unknown op 'nope'",
         "doc": {"name": "x", "steps": [{"op": "nope"}]}, "note": None})
    assert draft["status"] == "invalid"
    assert draft["path"] == "steps[0]" and "nope" in draft["error"]
    assert draft["doc"] == {"name": "x", "steps": [{"op": "nope"}]}


def test_extract_flow_draft_none_without_a_document():
    assert chat._extract_flow_draft({"status": "ok"}) is None
    assert chat._extract_flow_draft({"status": "ok", "doc": "not a dict"}) is None
    assert chat._extract_flow_draft("not a dict") is None
    assert chat._extract_flow_draft(None) is None


def test_run_conversation_turn_emits_flow_draft_frame():
    payload = _propose_ok_payload()
    stream1 = FakeStream(
        events=[],
        final=FakeFinalMessage(
            "tool_use", [FakeToolUseBlock("t1", "propose_flow", {"doc": payload["doc"]})]))
    stream2 = FakeStream(events=[FakeTextEvent("Here is the draft.")],
                         final=FakeFinalMessage("end_turn", [FakeTextBlock("done")]))
    client = FakeAnthropic([stream1, stream2])
    session = FakeMCPSession(result=FakeCallToolResult(structuredContent=payload))
    state = chat.ChatState(host="x.test", messages=[{"role": "user", "content": "build it"}],
                           mcp_session=session, anthropic_client=client, tools=[],
                           mode="flow")
    frames = []

    async def emit(frame):
        frames.append(frame)

    _run(chat.run_conversation_turn(state, emit=emit))

    assert [f["type"] for f in frames] == ["tool_use", "tool_result", "flow_draft",
                                           "text", "done"]
    draft = next(f for f in frames if f["type"] == "flow_draft")
    # THE FROZEN CONTRACT: a FLAT frame (not nested under "data").
    assert draft == {"type": "flow_draft", **chat._extract_flow_draft(payload)}
    assert draft["doc"] == payload["doc"]


def test_run_conversation_turn_flow_mode_uses_the_flow_prompt():
    stream = FakeStream(events=[], final=FakeFinalMessage("end_turn", [FakeTextBlock("x")]))
    client = FakeAnthropic([stream])
    state = chat.ChatState(host="x.test", messages=[{"role": "user", "content": "hi"}],
                           mcp_session=FakeMCPSession(), anthropic_client=client, tools=[],
                           mode="flow")

    async def emit(frame):
        pass

    _run(chat.run_conversation_turn(state, emit=emit))
    assert client.messages.calls[0]["system"] == chat.build_flow_system_prompt("x.test")
    assert client.messages.calls[0]["system"] != chat.build_system_prompt("x.test")


# --- augment_with_flow_draft (the sibling of augment_with_location) -----------

def test_augment_with_flow_draft_none_returns_text_unchanged():
    assert chat.augment_with_flow_draft("hello", None) == "hello"
    assert chat.augment_with_flow_draft("hello", {}) == "hello"


def test_augment_with_flow_draft_prefixes_the_live_document():
    doc = {"name": "invoices", "steps": [{"op": "goto", "url": "https://x.test/i"}]}
    out = chat.augment_with_flow_draft("add pagination", doc)
    assert '"name": "invoices"' in out                 # the CURRENT doc is inlined
    assert "propose_flow" in out                       # …and resent WHOLE
    assert out.endswith("add pagination")


def test_handle_user_message_folds_both_live_url_and_draft():
    async def fake_turn(state, *, emit):
        pass

    from unittest import mock
    doc = {"name": "f", "steps": []}
    state = chat.ChatState(host="site.test", mode="flow")
    with mock.patch.object(chat, "run_conversation_turn", side_effect=fake_turn):
        asyncio.run(chat.handle_user_message(state, "rename it",
                                             emit=lambda f: None,
                                             live_url="https://site.test/x",
                                             draft=doc))
    content = state.messages[-1]["content"]
    assert "https://site.test/x" in content            # augment_with_location
    assert '"name": "f"' in content                    # augment_with_flow_draft
    assert content.endswith("rename it")
