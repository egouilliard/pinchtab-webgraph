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

from .. import flow as flow_mod

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

# FLOW mode's ONE extra tool. Deliberately a SIBLING set, never folded into
# OFFLINE_TOOL_NAMES: the browsing fence above must stay exactly those 6 names, so the
# safety story is "flow mode ADDS one provably-pure tool" rather than "flow mode widens
# the fence". `propose_flow` performs no disk write, no browser action and no subprocess
# — it validates a document and hands it back (mcp_server.propose_flow). There is no
# flow save/run tool anywhere on the MCP surface, so the agent can only PROPOSE.
FLOW_TOOL_NAMES = frozenset({"propose_flow"})

# The chat modes. "workspace" = the original navigation assistant; "flow" = the flow
# author. A session's mode is fixed at creation and PINNED on resume (chat_backend),
# exactly like its backend — a workspace chat can never be talked into flow mode.
MODES = ("workspace", "flow")


def effective_tool_names(mode="workspace"):
    """The tool names a session in ``mode`` may call. The single safety predicate.

    The base offline fence is NEVER widened; flow mode only ADDS FLOW_TOOL_NAMES. Any
    unknown mode degrades to the base fence (fail closed).
    """
    if mode == "flow":
        return OFFLINE_TOOL_NAMES | FLOW_TOOL_NAMES
    return OFFLINE_TOOL_NAMES


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


def _op_line(op, spec, is_body):
    """One op's signature line, DERIVED from its flow.py spec (never hand-written)."""
    parts = []
    one_of = spec.get("one_of", ())
    if one_of:
        parts.append("exactly one of: %s" % " | ".join(one_of))
    if spec.get("req"):
        parts.append("required: %s" % ", ".join(spec["req"]))
    if spec.get("opt"):
        parts.append("optional: %s" % ", ".join(spec["opt"]))
    if is_body:
        parts.append("required: body (a non-empty list of steps)")
    return "  %-9s %s" % (op, "; ".join(parts) if parts else "(no keys)")


def op_reference():
    """The op table, GENERATED from flow.LEAF_OPS / BODY_OPS. Pure.

    The vocabulary has exactly one home (flow.py). Hand-copying it into a prompt string
    would give it a second, and the copy WOULD drift the first time an op grows a key —
    with the model then confidently emitting a document the validator rejects.
    """
    lines = ["LEAF OPS (a step is an object with an `op` key plus these):"]
    lines += [_op_line(op, spec, False) for op, spec in sorted(flow_mod.LEAF_OPS.items())]
    lines.append("BODY OPS (control flow — each takes a nested `body` list of steps):")
    lines += [_op_line(op, spec, True) for op, spec in sorted(flow_mod.BODY_OPS.items())]
    return "\n".join(lines)


def build_flow_system_prompt(host):
    """The FLOW-AUTHOR system prompt — a SIBLING of build_system_prompt, not a branch.

    Same read-only graph tools, a different job: draft a flow DOCUMENT (flow.py's model)
    for "%s" by grounding every step in the REAL crawled graph, and hand it to the human
    via `propose_flow`. Kept separate so the navigation prompt (and its tests) are
    untouched, and so the two prompts can diverge without either growing conditionals.

    Everything site-agnostic in it — the op table, the capability names, the loop vars —
    is GENERATED from flow.py's tables, so the prompt cannot drift from the validator.
    """
    caps = ", ".join("%s (default %s)" % (k, str(v).lower())
                     for k, v in sorted(flow_mod.DEFAULT_CAPABILITIES.items()))
    write_ops = ", ".join(sorted(flow_mod.WRITE_OPS)) or "(none)"
    loop_vars = "; ".join(
        "`%s` body injects ${%s}" % (op, "}, ${".join(sorted(vs)))
        for op, vs in sorted(flow_mod.BODY_VARS.items()))
    return (
        "You are a FLOW AUTHOR for the website \"%(host)s\". A flow is a declarative JSON "
        "document that a runner later executes against a real browser on this site. You "
        "draft it WITH the human, in conversation.\n"
        "\n"
        "AUTHORITY — read this twice:\n"
        "You can only PROPOSE. Call `propose_flow(doc, note)` every time you want to show "
        "or change the draft — always resend the ENTIRE document. You cannot save or run a "
        "flow; only the human can, via the Save/Run buttons. Ground every step in the REAL "
        "crawled graph — use `howto`/`find_content`/`list_forms`/`graph_summary` to find the "
        "actual trigger and click-path BEFORE proposing. Never invent a selector or a "
        "trigger label.\n"
        "\n"
        "TOOLS: always pass host=\"%(host)s\" to every graph tool. Never pass a filesystem "
        "graph= path. `propose_flow` takes no host — the document carries its own.\n"
        "\n"
        "GOTO GOALS — the one thing the validator CANNOT check for you:\n"
        "A `goto` step's `goal` is resolved against the graph AT RUN TIME by the same "
        "path-finder `howto` uses. `propose_flow` only checks the document's SHAPE — it "
        "will happily call a flow \"valid\" whose `goal` matches nothing, and the run then "
        "aborts instantly with \"could not resolve '<goal>' against the graph: no_match\". "
        "So: for EVERY `goto` you write, first call `howto(host=\"%(host)s\", goal=...)` and "
        "use a goal that actually RESOLVES — normally the real trigger label you found in "
        "the graph (e.g. the exact text of the link or button). If the draft you are editing "
        "already contains a `goto` you did not yourself verify — including the placeholder in "
        "a new/seed document — verify it before you propose, and replace it if it does not "
        "resolve. A \"valid\" verdict is NOT evidence that the goal resolves.\n"
        "\n"
        "THE DOCUMENT:\n"
        "  {\"name\": <non-empty string>, \"host\": \"%(host)s\", \"inputs\": {...}, "
        "\"capabilities\": {...}, \"steps\": [ ...steps... ]}\n"
        "`steps` is a non-empty list. Max nesting depth %(max_depth)d, max %(max_steps)d "
        "steps in total.\n"
        "\n"
        "%(ops)s\n"
        "\n"
        "CONTROL FLOW:\n"
        "  `for_each` repeats its body once per element the `match` selects (e.g. "
        "{\"kind\": \"download\"}); the element is bound to the variable named by `as` "
        "(default `item`).\n"
        "  `paginate` repeats its body page by page until `max_pages` / `until`.\n"
        "  Loop variables exist ONLY inside the body that injects them: %(loop_vars)s. "
        "`${run}` is available everywhere.\n"
        "\n"
        "SUBSTITUTION: `${a.b}` is a LITERAL dotted lookup into the run's variables — NOT "
        "an expression language. No arithmetic, no calls, no conditionals. Every `${x}` "
        "must resolve to a declared input, a loop variable in scope, a `set`/`collect` "
        "variable, or `${run}` — otherwise the document is invalid.\n"
        "\n"
        "CAPABILITIES: a write must be DECLARED. Known capabilities: %(caps)s. The write "
        "ops are: %(write_ops)s (each needs its allow_* capability set to true), and a `do` "
        "step with submit=true needs capabilities.allow_submit=true. Never set a capability "
        "the human did not ask for — if a step needs one, say so and ask.\n"
        "\n"
        "WORKFLOW: understand the goal -> query the graph to find the real pages, triggers "
        "and form fields -> `propose_flow` the whole draft with a one-line `note` -> read "
        "back the verdict. If it comes back \"invalid\", fix the reported `path`/`error` and "
        "propose again. Then explain the draft in two or three plain sentences and stop — "
        "the human decides whether to Save and Run it."
        % {"host": host, "ops": op_reference(), "caps": caps, "write_ops": write_ops,
           "loop_vars": loop_vars, "max_depth": flow_mod.MAX_DEPTH,
           "max_steps": flow_mod.MAX_STEPS}
    )


def system_prompt_for(host, mode="workspace"):
    """The system prompt for a session's mode. The ONE place the two prompts are chosen
    between, so both backends pick identically."""
    if mode == "flow":
        return build_flow_system_prompt(host)
    return build_system_prompt(host)


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


async def list_allowed_tools(mcp_session, mode="workspace"):
    """List the MCP server's tools, keep only the ones ``mode`` permits, map to Anthropic
    specs. The filter is effective_tool_names(mode) — the base offline fence, plus
    `propose_flow` in flow mode. A LIVE tool (crawl/ask_howto) is in neither set."""
    listed = await mcp_session.list_tools()
    allowed = effective_tool_names(mode)
    return [_anthropic_tool_from_mcp(t) for t in listed.tools if t.name in allowed]


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


def _extract_flow_draft(payload):
    """Pull the proposed flow draft out of a `propose_flow` payload, else None. Pure.

    The exact twin of _extract_tour, for the flow-authoring mode: when the model calls
    `propose_flow`, the payload echoes the WHOLE candidate document plus the validator's
    verdict, and we surface it as one structured `{"type":"flow_draft"}` frame the SPA
    re-renders its canvas / JSON pane from. An INVALID draft still yields a frame (status
    "invalid" + path/error) — the human should see the broken document and the reason,
    not nothing. None only when there is no document to show at all.
    """
    if not isinstance(payload, dict):
        return None
    doc = payload.get("doc")
    if not isinstance(doc, dict):
        return None
    return {"doc": doc, "status": payload.get("status"), "path": payload.get("path"),
            "error": payload.get("error"), "name": payload.get("name"),
            "note": payload.get("note")}


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
    """Injected agent context — the anthropic client + mcp session live HERE, not global.

    ``mode`` ("workspace" | "flow") selects the system prompt and the tool fence; it is
    fixed for the life of the session (chat_backend pins it from the record on resume).
    """
    host: str
    messages: list = field(default_factory=list)
    mcp_session: object = None
    anthropic_client: object = None
    tools: list = field(default_factory=list)
    mode: str = "workspace"


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
    plus, additively, {"type":"tour","data":{...}} after an OK `howto` and
    {"type":"flow_draft", ...} after a `propose_flow` (flow mode).
    """
    client = state.anthropic_client
    the_model = model or resolve_model()
    system = system_prompt_for(state.host, state.mode)

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
            if block.name == "propose_flow" and status == "ok":
                draft = _extract_flow_draft(payload)
                if draft is not None:
                    await emit({"type": "flow_draft", **draft})
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


def augment_with_flow_draft(text, draft_doc):
    """Prefix the user's message with the CURRENT draft document (flow mode).

    The sibling of augment_with_location, and the reason the draft never forks: the UI —
    not the model — owns the live document (the human can hand-edit the JSON or the canvas
    between turns), so each turn re-grounds the model on what is ACTUALLY on screen instead
    of the stale copy in its own context. Pure + shared by BOTH chat backends; returns
    ``text`` unchanged when there is no draft yet. A non-serializable draft degrades to the
    bare text rather than breaking the turn.
    """
    if not draft_doc:
        return text
    try:
        body = json.dumps(draft_doc, indent=2)
    except (TypeError, ValueError):
        return text
    return ("[Current draft flow — this is the live document on the user's screen, "
            "including any edits they made by hand. Base every change on THIS document "
            "and resend it WHOLE via propose_flow:\n%s]\n\n%s" % (body, text))


async def handle_user_message(state, text, *, emit, live_url=None, draft=None):
    """Append a user message and run one conversation turn. NEVER raises into the WS.

    Any ChatUnavailable / anthropic error / mcp transport error is reported as a
    single {"type":"error", ...} frame so the WebSocket route never sees an exception.
    ``live_url`` (the live browser pane's current page, when known) and ``draft`` (the
    flow document currently on screen, in flow mode) are folded into the turn so the agent
    routes from where the user actually is and edits what they can actually see.
    """
    content = augment_with_flow_draft(augment_with_location(text, live_url), draft)
    state.messages.append({"role": "user", "content": content})
    try:
        await run_conversation_turn(state, emit=emit)
    except ChatUnavailable as e:
        await emit({"type": "error", "status": "chat_unavailable",
                    "reason": e.reason, "detail": e.detail})
    except Exception as e:  # noqa: BLE001 — anthropic/mcp/transport errors
        await emit({"type": "error", "status": "chat_error", "detail": str(e)})
