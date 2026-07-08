#!/usr/bin/env python3
"""Live chat agent: Claude wired to the pinchtab-webgraph MCP server as tools.

This is the Phase-3 write side of the OPTIONAL web UI: a streaming conversational
agent that answers "how do I do X on this site?" by driving Claude (the Anthropic
API) with the project's OWN MCP tool surface (mcp_server.py) as tools. The offline
graph query tools become Claude's tools; the model plans a click-path and reads the
form for you, streaming text + tool activity back over the server's WebSocket route.

MIRRORS vault.py's discipline exactly:
  * stdlib-only at module scope — NO `anthropic`, NO `mcp` import at import time, so
    server.py can `from . import chat` while the base package stays a pure-stdlib
    install (verified by the base-import cleanliness check);
  * the heavy deps (`anthropic`, `mcp`) are imported LAZILY inside the functions
    that need them, and a missing package / missing key degrades to a structured
    ``ChatUnavailable(reason, detail)`` rather than a crash — the twin of
    ``vault.VaultUnavailable``.

TESTABILITY INVARIANT: the agent loop is pure and dependency-injected. The Anthropic
client and the MCP session ride in via a ``ChatState`` dataclass (never a module
global), and ``emit`` (the per-frame sink) is a plain injected async callable. So a
test passes fakes for all three — no real key, no real network, no real subprocess.
"""
import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

DEFAULT_MODEL = "claude-opus-4-8"
MODEL_ENV_VAR = "PINCHTAB_UI_MODEL"

# The OFFLINE, read-only MCP tools the chat agent may call. `crawl` and `ask_howto`
# are deliberately EXCLUDED: both are LIVE tools that shell out to the PinchTab
# browser bridge (a subprocess + network), so exposing them to an unauthenticated
# chat WebSocket would let a chat message launch a crawl / drive a browser. The chat
# stays strictly on the already-cached, offline graph — no side effects.
OFFLINE_TOOL_NAMES = frozenset({
    "graph_summary", "howto", "find_content", "list_content", "list_forms",
    "link_paths",
})


class ChatUnavailable(Exception):
    """The chat agent cannot run — a dep is absent, or no API key is configured.

    ``reason`` is one of {"no_api_key", "no_anthropic_package", "no_mcp_package"};
    ``detail`` is a human remedy hint. Callers turn this into a structured status
    frame, never a 500. Mirrors ``vault.VaultUnavailable``.
    """

    def __init__(self, reason, detail):
        super().__init__("%s: %s" % (reason, detail))
        self.reason = reason
        self.detail = detail


_NO_API_KEY_HINT = (
    "No ANTHROPIC_API_KEY is set. Export ANTHROPIC_API_KEY with a valid Anthropic "
    "API key before starting the UI to enable the chat agent; the offline REST API "
    "and the graph viewer work without it.")
_NO_ANTHROPIC_HINT = (
    "The optional 'anthropic' package is not installed. Install the UI extra "
    "(pip install 'pinchtab-webgraph[ui]') to enable the chat agent.")
_NO_MCP_HINT = (
    "The optional 'mcp' package is not installed. Install the MCP extra "
    "(pip install 'pinchtab-webgraph[mcp]') — the chat agent drives the "
    "pinchtab-webgraph MCP server as its tool backend.")


def resolve_model():
    """The model id to drive: the PINCHTAB_UI_MODEL override, else DEFAULT_MODEL."""
    return os.environ.get(MODEL_ENV_VAR) or DEFAULT_MODEL


def require_api_key():
    """Return the Anthropic API key, or raise ChatUnavailable("no_api_key").

    `anthropic.AsyncAnthropic()` does NOT raise on a missing key at construction —
    it defers to the first request — so we check the env ourselves up front to fail
    fast with a structured, actionable error before any network is attempted.
    """
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise ChatUnavailable("no_api_key", _NO_API_KEY_HINT)
    return key


def build_system_prompt(host):
    """The system prompt: a navigation assistant over the OFFLINE crawled graph.

    Pure (host in, string out). Tells the agent the single most load-bearing rule —
    always pass host="{host}" to every tool — and how to answer: a concrete
    click-path plus the form fields, never an invented path.
    """
    return (
        "You are a navigation assistant for the website \"%s\". You answer questions "
        "about how to accomplish tasks on that site by consulting an OFFLINE, "
        "previously-crawled navigation graph through your tools.\n"
        "\n"
        "RULES:\n"
        "1. Always pass host=\"%s\" to every tool call. Never invent a different "
        "host, and never pass a filesystem graph= path.\n"
        "2. Prefer the `howto` tool (for \"how do I do X\" / \"where do I create a "
        "Y\") and `find_content` (to locate captured data). Fall back to "
        "`list_forms`, `list_content`, `graph_summary`, and `link_paths` as needed.\n"
        "3. Answer with a concrete click-path: short, numbered steps the user "
        "follows in the UI, then list the relevant form fields when a form is "
        "involved.\n"
        "4. If a tool returns status \"no_match\", \"unreachable\", \"empty\", or "
        "\"no_cache_for_host\", say so plainly. NEVER invent a click-path or a "
        "control that the graph does not contain.\n"
        "5. Keep answers short and actionable."
        % (host, host)
    )


def _anthropic_tool_from_mcp(tool):
    """Convert one MCP `Tool` to an Anthropic tool spec (a near-passthrough).

    The only transform is the camelCase->snake_case key rename
    (`inputSchema` -> `input_schema`); name and description carry over verbatim.
    """
    return {
        "name": tool.name,
        "description": tool.description or "",
        "input_schema": tool.inputSchema,
    }


async def list_allowed_tools(mcp_session):
    """List the MCP server's tools, keep only the OFFLINE set, map to Anthropic specs."""
    listed = await mcp_session.list_tools()
    return [_anthropic_tool_from_mcp(t) for t in listed.tools
            if t.name in OFFLINE_TOOL_NAMES]


@asynccontextmanager
async def mcp_client_session(python_exe=None):
    """Spawn the pinchtab-webgraph MCP server over stdio and yield an initialized session.

    Lazily imports `mcp` -> ChatUnavailable("no_mcp_package") on ImportError. Launches
    the server as `python -m pinchtab_webgraph.mcp_server` (matching mcp_server.py's
    own subprocess pattern — the module form, not the bare console-script name).
    """
    try:
        import sys
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
    except ImportError:
        raise ChatUnavailable("no_mcp_package", _NO_MCP_HINT)

    params = StdioServerParameters(
        command=python_exe or sys.executable,
        args=["-m", "pinchtab_webgraph.mcp_server"],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def build_anthropic_client():
    """Build an AsyncAnthropic client, requiring a key first.

    Lazily imports `anthropic` -> ChatUnavailable("no_anthropic_package") on
    ImportError, then require_api_key() -> ChatUnavailable("no_api_key") if unset.
    """
    try:
        import anthropic
    except ImportError:
        raise ChatUnavailable("no_anthropic_package", _NO_ANTHROPIC_HINT)
    require_api_key()
    return anthropic.AsyncAnthropic()


def _result_to_text(result):
    """Join the TextContent blocks of a CallToolResult into a single string."""
    parts = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(text)
    return "\n".join(parts)


async def run_tool(mcp_session, name, arguments):
    """Call an MCP tool and return a plain dict payload. NEVER raises.

    Prefers the structured result; falls back to parsing the first TextContent as
    JSON, else wraps the joined text. Any transport/tool exception becomes a
    structured {"status": "tool_error", ...} dict the model can read and recover from.
    """
    try:
        result = await mcp_session.call_tool(name, arguments)
    except Exception as e:  # noqa: BLE001 — never propagate into the stream loop
        return {"status": "tool_error", "detail": str(e)}

    # An MCP-level tool failure (the handler raised; FastMCP marks isError) comes back
    # as content-with-a-message, not JSON — surface it as tool_error so _tool_status,
    # the emitted frame, and the tool_result's is_error all agree it FAILED.
    if getattr(result, "isError", False):
        return {"status": "tool_error", "detail": _result_to_text(result) or "tool reported an error"}

    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured

    text = _result_to_text(result)
    if text:
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            return {"raw": text}
    return {"raw": text}


def _tool_status(payload):
    """"ok" unless the tool payload carries an error-ish status. Best-effort."""
    if isinstance(payload, dict):
        status = payload.get("status")
        if status in ("tool_error",) or (isinstance(status, str) and status.startswith("error")):
            return "error"
    return "ok"


def _extract_tour(payload):
    """Pull the "Show Me How" tour out of an OK `howto` payload, else None. Pure.

    Additive to the frame protocol: when the model runs `howto` and it resolves, the
    FIRST result carries a `tour` (the ordered nav/trigger/form highlight steps). We
    surface it as one structured `{"type":"tour"}` frame the SPA replays on the live
    pane. None unless the payload is a dict with status "ok" and a non-empty results
    list — a miss / error / result-less payload yields no tour frame.
    """
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return None
    results = payload.get("results") or []
    if not results:
        return None
    r = results[0]
    return {"goal": payload.get("goal"), "start_url": payload.get("start_url"),
            "trigger_label": r.get("trigger_label"), "steps": r.get("tour"),
            "form": r.get("form"), "opens_at": r.get("opens_at")}


# --- session persistence: wire-message (de)serialization ----------------------
#
# Phase 4 persists the API backend's ``ChatState.messages`` to disk so a reconnect can
# RESUME the conversation (the model recalls prior turns). These three pure functions are
# the seam chat_store/chat_backend use; they are the ONLY change to this module and touch
# nothing in the agent loop.

# Cap on any single serialized text / tool_result string, so one runaway tool payload
# can't bloat a session file unboundedly. Long strings are truncated with a marker.
MAX_WIRE_TEXT_CHARS = 20000


def _truncate(s):
    if isinstance(s, str) and len(s) > MAX_WIRE_TEXT_CHARS:
        return s[:MAX_WIRE_TEXT_CHARS] + "…[truncated]"
    return s


def _serialize_block(block):
    """One content block -> a JSON-safe dict. anthropic 0.85.0 blocks expose
    ``.model_dump(mode="json")``; a plain dict passes through. Long text/tool_result
    strings are truncated."""
    dump = getattr(block, "model_dump", None)
    if callable(dump):
        d = dump(mode="json")
    elif isinstance(block, dict):
        d = dict(block)
    else:
        d = block
    if isinstance(d, dict):
        if isinstance(d.get("text"), str):
            d["text"] = _truncate(d["text"])
        if isinstance(d.get("content"), str):
            d["content"] = _truncate(d["content"])
    return d


def serialize_messages(messages):
    """Serialize a ChatState.messages list to a JSON-safe, trimmed wire list.

    Each message's ``content`` is either a plain str (a genuine user turn) or a list of
    content blocks (assistant tool_use turns, tool_result turns) — the latter are mapped
    through ``.model_dump(mode="json")`` (or passed through if already dicts). The whole
    list is then trimmed to the trailing turns via trim_wire_messages so a long session
    never grows unboundedly.
    """
    out = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if isinstance(content, list):
            content = [_serialize_block(b) for b in content]
        elif isinstance(content, str):
            content = _truncate(content)
        out.append({"role": role, "content": content})
    # A trailing genuine user turn (role=user + plain-str content) with no assistant reply
    # after it is error-rollback residue (run_conversation_turn snapshots BEFORE the user
    # turn and restores on a mid-turn exception, but the caught path can still leave the
    # user message). Persisting it would make the NEXT resumed send two consecutive user
    # turns, which the Anthropic API rejects — drop it so a resumed session stays valid.
    if out and _is_new_user_turn(out[-1]):
        out.pop()
    return trim_wire_messages(out)


def deserialize_messages(data):
    """Defensive identity: accept a persisted wire list back as ChatState.messages.

    The wire list is already the exact ``{role, content}`` shape the Anthropic API and the
    agent loop consume, so this is a validated pass-through — a non-list, or a list with a
    non-dict element, degrades to an empty history rather than corrupting the next turn.
    """
    if not isinstance(data, list):
        return []
    return [m for m in data if isinstance(m, dict) and "role" in m]


def _is_new_user_turn(msg):
    """True for a GENUINE new user turn — role=="user" AND content is a plain str.

    A tool_result turn is ALSO role=="user" but carries a LIST of tool_result blocks; it
    must never be treated as a conversation boundary (splitting there would orphan the
    assistant tool_use turn it answers).
    """
    return msg.get("role") == "user" and isinstance(msg.get("content"), str)


def trim_wire_messages(messages, max_turns=100):
    """Keep only the trailing ``max_turns`` genuine user turns and everything after them.

    The history always resumes on a valid user-turn boundary — never on a dangling
    assistant tool_use turn (which the real API rejects on the next message). We find the
    index of the (max_turns)-th-from-last genuine user turn and slice from there; a
    tool_result "user" turn is NOT a boundary, so a tool_use/tool_result pair is never
    split across the cut.
    """
    user_idxs = [i for i, m in enumerate(messages) if _is_new_user_turn(m)]
    if len(user_idxs) <= max_turns:
        return messages
    cut = user_idxs[-max_turns]
    return messages[cut:]


@dataclass
class ChatState:
    """Injected agent context — the anthropic client + mcp session live HERE, not global."""
    host: str
    messages: list = field(default_factory=list)
    mcp_session: object = None
    anthropic_client: object = None
    tools: list = field(default_factory=list)


async def run_conversation_turn(state, *, emit, model=None, max_tokens=1024,
                                max_tool_iterations=8):
    """Drive the streaming + tool-use loop for the pending conversation turn.

    Streams Claude's reply, running MCP tools it requests and feeding results back
    until it stops asking for tools (bounded by `max_tool_iterations`). `emit` is an
    async callable taking one dict frame; the frame protocol is:
      {"type":"text","delta":<str>}                      per streamed text delta
      {"type":"tool_use","name":<str>,"input":<dict>}    before running a tool
      {"type":"tool_result","name":<str>,"status":"ok"|"error"}  after a tool
      {"type":"error","detail":<str>}                    on the iteration bound
      {"type":"done"}                                    exactly once at the end
    """
    client = state.anthropic_client
    the_model = model or resolve_model()
    system = build_system_prompt(state.host)

    # Snapshot the history length ONCE, at the pending user turn, BEFORE the tool loop
    # appends anything. If any stream call fails mid-turn, roll the WHOLE turn back to
    # here — dropping every assistant tool_use turn and its tool_result turn added this
    # turn. This guarantees history never ends on an assistant tool_use turn with no
    # matching tool_result (which the real API rejects on the NEXT message), and lets a
    # retry re-run the turn cleanly. (A per-iteration snapshot would leave the PRIOR
    # iteration's committed turns in place; the failure surfaces one iteration later.)
    snapshot = len(state.messages)
    for _iteration in range(max_tool_iterations):
        try:
            async with client.messages.stream(
                model=the_model,
                max_tokens=max_tokens,
                system=system,
                messages=state.messages,
                tools=state.tools,
            ) as stream:
                async for event in stream:
                    if getattr(event, "type", None) == "text":
                        await emit({"type": "text", "delta": event.text})
                final = await stream.get_final_message()
        except Exception:  # noqa: BLE001
            del state.messages[snapshot:]
            raise

        if getattr(final, "stop_reason", None) != "tool_use":
            # Natural end of turn: record the assistant message and finish.
            state.messages.append({"role": "assistant", "content": final.content})
            await emit({"type": "done"})
            return

        # Tool-use turn: run each requested tool, then loop the stream with results.
        state.messages.append({"role": "assistant", "content": final.content})
        tool_results = []
        for block in final.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            await emit({"type": "tool_use", "name": block.name, "input": block.input})
            payload = await run_tool(state.mcp_session, block.name, block.input)
            status = _tool_status(payload)
            await emit({"type": "tool_result", "name": block.name, "status": status})
            if block.name == "howto" and status == "ok":
                tour = _extract_tour(payload)
                if tour is not None:
                    await emit({"type": "tour", "data": tour})
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(payload),
                "is_error": status == "error",
            })
        state.messages.append({"role": "user", "content": tool_results})

    # Exhausted the iteration budget without a natural stop.
    await emit({"type": "error", "detail": "tool_iteration_limit_exceeded"})
    await emit({"type": "done"})


_TM_OPEN = "<function_calls>"
_TM_CLOSE = "</function_calls>"


class ToolMarkupFilter:
    """Strip leaked tool-call markup from a streamed assistant TEXT stream.

    A model whose tools are not yet in its list (e.g. an MCP handshake that raced the
    first turn) can emit a tool call AS TEXT — the raw ``<function_calls><invoke …>``
    markup — instead of a structured tool_use. That must never render as chat prose. This
    stateful filter drops every ``<function_calls>…</function_calls>`` span across the
    incoming deltas, holding back only a short tail that could be a tag split across two
    deltas. Generic — no tool/app names, no regex on content. Pure/stdlib.
    """

    def __init__(self):
        self._buf = ""
        self._in = False           # inside a <function_calls>…</function_calls> span

    def feed(self, delta):
        """Consume one text delta; return the text safe to emit now ('' if none yet)."""
        self._buf += delta or ""
        out = []
        while True:
            if self._in:
                i = self._buf.find(_TM_CLOSE)
                if i == -1:
                    break
                self._buf = self._buf[i + len(_TM_CLOSE):]
                self._in = False
            else:
                i = self._buf.find(_TM_OPEN)
                if i == -1:
                    break
                out.append(self._buf[:i])
                self._buf = self._buf[i + len(_TM_OPEN):]
                self._in = True
        # Hold back ONLY a tail that is a partial prefix of the tag we're scanning for
        # (so normal text streams immediately; we buffer only near a possible split tag).
        tag = _TM_CLOSE if self._in else _TM_OPEN
        hold = self._partial_prefix_len(self._buf, tag)
        if self._in:
            # inside a dropped span: discard everything except a possible partial close tag
            self._buf = self._buf[len(self._buf) - hold:] if hold else ""
        else:
            if hold:
                out.append(self._buf[:len(self._buf) - hold])
                self._buf = self._buf[len(self._buf) - hold:]
            else:
                out.append(self._buf)
                self._buf = ""
        return "".join(out)

    @staticmethod
    def _partial_prefix_len(buf, tag):
        """Length of the longest suffix of ``buf`` that is a proper prefix of ``tag``.

        That suffix might be the start of a ``tag`` split across the next delta, so it is
        the only part worth holding back; everything before it is safe to emit now.
        """
        for n in range(min(len(buf), len(tag) - 1), 0, -1):
            if tag.startswith(buf[-n:]):
                return n
        return 0

    def flush(self):
        """End of stream: emit any retained safe text (nothing if mid-dropped-span)."""
        out = "" if self._in else self._buf
        self._buf = ""
        self._in = False
        return out


def augment_with_location(text, live_url):
    """Prefix the user's message with the live browser's current URL when known.

    This is how the agent learns "where the user is": the client tracks the live pane's
    position (from screencast `location` frames) and sends it with each message; we fold
    it into the turn so the agent calls `howto` with start=<live_url> and the steps +
    tour begin from the current page, not the crawl root. Pure + shared by BOTH chat
    backends; returns `text` unchanged when live_url is falsy. Live-site text is
    untrusted, but this only ever travels to the model (never rendered as HTML).
    """
    if not live_url:
        return text
    return ("[Context: the live browser pane is currently on %s. If I ask how to reach "
            "or do something, call the howto tool with start=\"%s\" so the click-path "
            "and tour start from this page.]\n\n%s" % (live_url, live_url, text))


async def handle_user_message(state, text, *, emit, live_url=None):
    """Append a user message and run one conversation turn. NEVER raises into the WS.

    Any ChatUnavailable / anthropic error / mcp transport error is reported as a
    single {"type":"error", ...} frame so the WebSocket route never sees an exception.
    ``live_url`` (the live browser pane's current page, when known) is folded into the
    turn so the agent routes from where the user actually is.
    """
    state.messages.append({"role": "user", "content": augment_with_location(text, live_url)})
    try:
        await run_conversation_turn(state, emit=emit)
    except ChatUnavailable as e:
        await emit({"type": "error", "status": "chat_unavailable",
                    "reason": e.reason, "detail": e.detail})
    except Exception as e:  # noqa: BLE001 — anthropic/mcp/transport errors
        await emit({"type": "error", "status": "chat_error", "detail": str(e)})
