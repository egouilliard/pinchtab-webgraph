"""Tests for pinchtab_webgraph.ui.chat_claude_code — the Claude Code chat backend.

MOST tests here run WITHOUT the real Claude Agent SDK: the frame-mapping logic
dispatches on ``type(msg).__name__``, so a fake class named exactly ``StreamEvent`` /
``AssistantMessage`` / ``UserMessage`` / ``ResultMessage`` (and ``ToolUseBlock`` /
``ToolResultBlock`` / ``TextBlock``) exercises the mapper with no SDK, no CLI, no
subprocess. The lockdown-shape / deny-backstop tests DO need the real SDK and are
guarded with importorskip. The REAL end-to-end test is opt-in behind an env gate.
"""
import asyncio
import json
import os
import shutil
import sys

import pytest

from pinchtab_webgraph.ui import chat, chat_claude_code


# --- fakes (named to match the SDK classes the mapper dispatches on) ----------

class StreamEvent:
    def __init__(self, event):
        self.event = event


def _text_delta(text):
    return StreamEvent({"type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": text}})


class TextBlock:
    def __init__(self, text):
        self.text = text


class ToolUseBlock:
    def __init__(self, id, name, input):
        self.id = id
        self.name = name
        self.input = input


class ToolResultBlock:
    def __init__(self, tool_use_id, content=None, is_error=False):
        self.tool_use_id = tool_use_id
        self.content = content
        self.is_error = is_error


class AssistantMessage:
    def __init__(self, content):
        self.content = content


class UserMessage:
    def __init__(self, content):
        self.content = content


class ResultMessage:
    def __init__(self, is_error=False, result=None):
        self.is_error = is_error
        self.result = result


class SystemMessage:
    def __init__(self, subtype="init", data=None):
        self.subtype = subtype
        self.data = data or {}


class FakeClient:
    def __init__(self, messages):
        self._messages = messages
        self.queries = []

    async def query(self, text, session_id="default"):
        self.queries.append((text, session_id))

    async def receive_response(self):
        for m in self._messages:
            yield m


def _run(coro):
    return asyncio.run(coro)


# --- _is_allowed: THE safety predicate ---------------------------------------

def test_is_allowed_only_offline_tools():
    allowed = [chat_claude_code._qualified(n) for n in sorted(chat.OFFLINE_TOOL_NAMES)]
    # all 6 offline tools (qualified) are allowed
    for n in chat.OFFLINE_TOOL_NAMES:
        assert chat_claude_code._is_allowed(chat_claude_code._qualified(n), allowed)
    # the 2 LIVE tools are denied
    assert not chat_claude_code._is_allowed(chat_claude_code._qualified("crawl"), allowed)
    assert not chat_claude_code._is_allowed(chat_claude_code._qualified("ask_howto"), allowed)
    # built-ins are denied
    assert not chat_claude_code._is_allowed("Bash", allowed)
    assert not chat_claude_code._is_allowed("Write", allowed)
    # a bare (unqualified) offline name is denied — only the qualified form is allowed
    assert not chat_claude_code._is_allowed("howto", allowed)


# --- _qualified / _strip_prefix ----------------------------------------------

def test_qualified_and_strip_roundtrip():
    q = chat_claude_code._qualified("howto")
    assert q == "mcp__pinchtab-webgraph__howto"
    assert chat_claude_code._strip_prefix(q) == "howto"
    # a name without the prefix passes through unchanged
    assert chat_claude_code._strip_prefix("Bash") == "Bash"


# --- frame mapping: full scripted sequence -----------------------------------

def test_frame_mapping_full_sequence():
    qhowto = chat_claude_code._qualified("howto")
    messages = [
        _text_delta("Let me "),
        _text_delta("check. "),
        AssistantMessage([TextBlock("dropped — streamed via StreamEvent"),
                          ToolUseBlock("t1", qhowto, {"host": "h", "goal": "create role"})]),
        UserMessage([ToolResultBlock("t1", content="ok", is_error=False)]),
        _text_delta("Click Roles, then Create."),
        SystemMessage(),  # no-op
        ResultMessage(is_error=False, result=None),
    ]
    client = FakeClient(messages)
    frames = []

    async def emit(f):
        frames.append(f)

    _run(chat_claude_code.run_conversation_turn(client, "how?", emit=emit))

    assert [f["type"] for f in frames] == [
        "text", "text", "tool_use", "tool_result", "text", "done"]

    tu = next(f for f in frames if f["type"] == "tool_use")
    assert tu["name"] == "howto"  # qualifier stripped
    assert tu["input"] == {"host": "h", "goal": "create role"}

    tr = next(f for f in frames if f["type"] == "tool_result")
    assert tr["name"] == "howto"  # correlated by tool_use_id, stripped
    assert tr["status"] == "ok"

    # the user text was actually sent through query() with the default session id
    assert client.queries == [("how?", "default")]


def test_tool_result_text_and_parse_helpers():
    # str content passes through; list-of-text-blocks joins; anything else -> "".
    assert chat_claude_code._tool_result_text('{"a": 1}') == '{"a": 1}'
    assert chat_claude_code._tool_result_text(
        [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "a\nb"
    assert chat_claude_code._tool_result_text(None) == ""
    assert chat_claude_code._tool_result_text(123) == ""
    # best-effort json parse; non-JSON / empty -> None (never raises).
    assert chat_claude_code._parse_tool_payload('{"status": "ok"}') == {"status": "ok"}
    assert chat_claude_code._parse_tool_payload("not json") is None
    assert chat_claude_code._parse_tool_payload(None) is None


def test_frame_mapping_emits_tour_frame_for_howto_ok():
    # The SDK delivers the howto result as JSON TEXT in ToolResultBlock.content; an OK
    # payload with a tour must yield a {"type":"tour"} frame identical in shape to the
    # API backend's (chat._extract_tour).
    qhowto = chat_claude_code._qualified("howto")
    payload = {"status": "ok", "goal": "create role",
               "start_url": "https://example.test/dashboard",
               "results": [{"trigger_label": "Create Role",
                            "opens_at": "https://example.test/team/roles",
                            "form": {"fieldCount": 1},
                            "tour": [{"kind": "nav", "label": "Team", "selector": "a", "href": None},
                                     {"kind": "trigger", "label": "Create Role",
                                      "selector": None, "href": None},
                                     {"kind": "form"}]}]}
    messages = [
        AssistantMessage([ToolUseBlock("t1", qhowto, {"host": "h", "goal": "create role"})]),
        UserMessage([ToolResultBlock("t1", content=json.dumps(payload), is_error=False)]),
        ResultMessage(is_error=False),
    ]
    frames = []

    async def emit(f):
        frames.append(f)

    _run(chat_claude_code.run_conversation_turn(FakeClient(messages), "how?", emit=emit))

    assert [f["type"] for f in frames] == ["tool_use", "tool_result", "tour", "done"]
    tour = next(f for f in frames if f["type"] == "tour")["data"]
    # identical shape to the API backend's _extract_tour output.
    assert tour == chat._extract_tour(payload)
    assert tour["trigger_label"] == "Create Role"
    assert [s["kind"] for s in tour["steps"]] == ["nav", "trigger", "form"]


def test_frame_mapping_no_tour_frame_for_howto_miss():
    qhowto = chat_claude_code._qualified("howto")
    messages = [
        AssistantMessage([ToolUseBlock("t1", qhowto, {"host": "h"})]),
        UserMessage([ToolResultBlock(
            "t1", content=json.dumps({"status": "no_match", "results": []}))]),
        ResultMessage(is_error=False),
    ]
    frames = []

    async def emit(f):
        frames.append(f)

    _run(chat_claude_code.run_conversation_turn(FakeClient(messages), "x", emit=emit))
    assert "tour" not in [f["type"] for f in frames]


def test_frame_mapping_tool_result_error_status():
    qforms = chat_claude_code._qualified("list_forms")
    messages = [
        AssistantMessage([ToolUseBlock("t9", qforms, {"host": "h"})]),
        UserMessage([ToolResultBlock("t9", content="boom", is_error=True)]),
        ResultMessage(is_error=False),
    ]
    frames = []

    async def emit(f):
        frames.append(f)

    _run(chat_claude_code.run_conversation_turn(FakeClient(messages), "x", emit=emit))
    tr = next(f for f in frames if f["type"] == "tool_result")
    assert tr["name"] == "list_forms"
    assert tr["status"] == "error"


def test_frame_mapping_result_error_emits_error_then_done():
    frames = []

    async def emit(f):
        frames.append(f)

    _run(chat_claude_code.run_conversation_turn(
        FakeClient([ResultMessage(is_error=True, result="turn blew up")]), "x", emit=emit))
    assert frames == [{"type": "error", "detail": "turn blew up"},
                      {"type": "done"}]


def test_frame_mapping_result_error_default_detail():
    frames = []

    async def emit(f):
        frames.append(f)

    _run(chat_claude_code.run_conversation_turn(
        FakeClient([ResultMessage(is_error=True, result=None)]), "x", emit=emit))
    assert frames[0] == {"type": "error", "detail": "claude_code_turn_error"}
    assert frames[1] == {"type": "done"}


# --- handle_user_message: never raises ---------------------------------------

def test_handle_user_message_swallows_query_error():
    class BoomClient:
        async def query(self, text, session_id="default"):
            raise RuntimeError("cli transport down")

        async def receive_response(self):  # pragma: no cover - never reached
            if False:
                yield None

    frames = []

    async def emit(f):
        frames.append(f)

    _run(chat_claude_code.handle_user_message(BoomClient(), "hi", emit=emit))
    assert frames[-1]["type"] == "error"
    assert frames[-1]["status"] == "chat_error"
    assert "cli transport down" in frames[-1]["detail"]


# --- open_client: missing CLI / missing package degrade gracefully -----------

def test_open_client_missing_cli_without_importing_sdk(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.delitem(sys.modules, "claude_agent_sdk", raising=False)

    async def go():
        async with chat_claude_code.open_client("example.test"):
            pass  # pragma: no cover

    with pytest.raises(chat.ChatUnavailable) as ei:
        _run(go())
    assert ei.value.reason == "no_claude_cli"
    # require_claude_cli fails fast, BEFORE the SDK is ever imported
    assert "claude_agent_sdk" not in sys.modules


def test_open_client_missing_package(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)  # import -> ImportError

    async def go():
        async with chat_claude_code.open_client("example.test"):
            pass  # pragma: no cover

    with pytest.raises(chat.ChatUnavailable) as ei:
        _run(go())
    assert ei.value.reason == "no_claude_code_package"


# --- resolve_model ------------------------------------------------------------

def test_resolve_model_default_none(monkeypatch):
    monkeypatch.delenv("PINCHTAB_UI_CLAUDE_CODE_MODEL", raising=False)
    assert chat_claude_code.resolve_model() is None


def test_resolve_model_env_override(monkeypatch):
    monkeypatch.setenv("PINCHTAB_UI_CLAUDE_CODE_MODEL", "sonnet")
    assert chat_claude_code.resolve_model() == "sonnet"


# --- SDK-guarded: the lockdown IS the safety checklist -----------------------

def test_build_options_lockdown_shape():
    sdk = pytest.importorskip("claude_agent_sdk")
    opts = chat_claude_code._build_options(sdk, "example.test", model=None, cwd="/tmp/cc")

    # kill ALL built-in tools — the real fence
    assert opts.tools == []
    # no ~/.claude / CLAUDE.md / settings, and no project .mcp.json
    assert opts.setting_sources == []
    assert opts.strict_mcp_config is True
    # can_use_tool must be consulted (NOT bypassPermissions)
    assert opts.permission_mode == "default"
    # auto-approve EXACTLY the 6 offline tools (qualified)
    expected_allowed = [chat_claude_code._qualified(n)
                        for n in sorted(chat.OFFLINE_TOOL_NAMES)]
    assert opts.allowed_tools == expected_allowed
    assert len(expected_allowed) == 6
    # strip the 2 LIVE tools
    assert opts.disallowed_tools == [chat_claude_code._qualified("crawl"),
                                     chat_claude_code._qualified("ask_howto")]
    # exactly one stdio MCP server: our own
    assert set(opts.mcp_servers.keys()) == {"pinchtab-webgraph"}
    srv = opts.mcp_servers["pinchtab-webgraph"]
    assert srv["type"] == "stdio"
    assert srv["command"] == sys.executable
    assert srv["args"] == ["-m", "pinchtab_webgraph.mcp_server"]
    # the deny-by-default backstop is wired
    assert opts.can_use_tool is not None
    # the host-pinned offline prompt, reused verbatim from chat.py
    assert opts.system_prompt == chat.build_system_prompt("example.test")
    # streaming text deltas on; account-default model
    assert opts.include_partial_messages is True
    assert opts.model is None


def test_deny_unless_allowlisted():
    sdk = pytest.importorskip("claude_agent_sdk")
    cb = chat_claude_code._build_options(sdk, "h").can_use_tool

    allow = _run(cb(chat_claude_code._qualified("howto"), {}, None))
    assert isinstance(allow, sdk.PermissionResultAllow)

    for denied in ("Bash", "Write", chat_claude_code._qualified("crawl"),
                   chat_claude_code._qualified("ask_howto")):
        res = _run(cb(denied, {}, None))
        assert isinstance(res, sdk.PermissionResultDeny)


# --- REAL opt-in end-to-end (spawns the claude CLI; gated) -------------------

@pytest.mark.skipif(
    shutil.which("claude") is None
    or os.environ.get("PINCHTAB_RUN_REAL_CLAUDE_CODE") != "1",
    reason="real Claude Code E2E is opt-in: set PINCHTAB_RUN_REAL_CLAUDE_CODE=1 "
           "with the claude CLI installed and logged in")
def test_real_claude_code_end_to_end(populated_cache_home):
    pytest.importorskip("claude_agent_sdk")
    frames = []

    async def collect(f):
        frames.append(f)

    async def go():
        async with chat_claude_code.open_client("example.test") as client:
            await chat_claude_code.handle_user_message(
                client, "How do I create a role?", emit=collect)

    _run(go())

    assert frames, "expected streamed frames from the real Claude Code turn"
    assert frames[-1]["type"] == "done", frames
    # SAFETY: any tool the agent called must be one of the 6 offline graph tools —
    # never crawl/ask_howto, never a built-in like Bash/Write.
    for f in frames:
        if f["type"] == "tool_use":
            assert f["name"] in chat.OFFLINE_TOOL_NAMES, f
