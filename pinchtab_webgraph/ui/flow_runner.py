#!/usr/bin/env python3
"""Live flow run: spawn `flow_cmd` for a saved flow and stream its JSONL frames out.

This is the WRITE side of the flow layer in the OPTIONAL web UI: the SPA's "Run" button
sends inputs + a capability grant over `/ws/flows/run`, and this module launches
`python -m pinchtab_webgraph.flow_cmd run <doc> --jsonl` as a subprocess, relays every step
event it prints, and hands the terminal `result` payload back to the route to persist.

WHY A SUBPROCESS, not an in-process ``runner.execute`` call:

  * A flow can run for a LONG time (a `paginate` loop over 50 pages, each a real browser
    round-trip) and the user must be able to press Cancel. ``runner.execute`` has NO
    cooperative-cancellation hook — no callback, no flag it re-checks between steps — so an
    in-process design could never honour that click. The only cancellation primitive that
    actually works here is SIGTERM→SIGKILL on the process's own group, which is exactly what
    ``start_new_session=True`` + ``cancel_run_session`` give us.
  * A flow drives the single-tenant PinchTab bridge; crash isolation on the thing holding a
    real, logged-in browser tab is worth having for free.

MIRRORS live_crawl.py exactly — same three sections (pure / I-O / session lifecycle), same
degradation idiom (a structured ``FlowRunUnavailable(reason, detail)`` instead of a crash),
same process-group teardown, same "the finally ALWAYS cancels" discipline.

SECURITY INVARIANTS (must hold):
  * User values (inputs, the flow's own path) NEVER become a shell string — ``build_run_argv``
    emits an argv LIST and ``open_flow_run_session`` uses ``asyncio.create_subprocess_exec``
    (no shell). A hostile input is an inert argv token, never an executable statement.
  * SAFE BY DEFAULT: a write happens only if the flow DECLARES the capability *and* the
    caller GRANTS it. This module only carries the caller's grant onto the argv; the runner
    ANDs it with the document's declaration and either side can veto.
  * The whole feature is OFF unless ``PINCHTAB_WEBGRAPH_ENABLE_FLOWS`` is truthy (the route
    enforces it). A flow is MORE dangerous than a crawl: a crawl structurally never submits,
    where a flow's `do{submit:true}` / `upload` CAN write to the real site.
  * The bridge is the SAME one live_crawl uses (``server_url()`` reads the same env var).
    That identity is why the two features veto each other — one bridge, one tab.
"""
import asyncio
import json
import os
import shutil
import signal
import sys
import tempfile
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass

from .. import cache_store
from . import live_crawl

MAX_LIVE_FLOW_RUNS = 1

# flow_cmd's own --config default when the env var is unset. A DRY run never opens the file
# (it resolves no token and leases no tab), so the path only has to be an inert argv token.
DEFAULT_CONFIG_NAME = "crawl-config.json"


class FlowRunUnavailable(Exception):
    """A flow run cannot start — the feature is off, config is missing, or the PinchTab
    bridge is unreachable.

    ``reason`` is one of {"disabled", "no_config", "bridge_unreachable"}; ``detail`` is a
    human remedy hint. The WS route turns this into a structured error frame + close, never
    a 500. The exact twin of ``live_crawl.CrawlUnavailable``.
    """

    def __init__(self, reason, detail):
        super().__init__("%s: %s" % (reason, detail))
        self.reason = reason
        self.detail = detail


_DISABLED_HINT = (
    "Running flows is disabled. A flow drives a REAL browser and — when the flow declares "
    "the capability and you grant it — can SUBMIT forms and UPLOAD files to the live site; "
    "set PINCHTAB_WEBGRAPH_ENABLE_FLOWS=1 to enable it.")
_NO_CONFIG_HINT = (
    "No PinchTab config found. Set $PINCHTAB_CONFIG to the path of your crawl-config.json "
    "(it carries the bridge token the flow runner self-loads). A DRY RUN needs no config.")
_BRIDGE_UNREACHABLE_HINT = (
    "Could not reach the PinchTab bridge at %s. Start one (e.g. "
    "scripts/start-crawl-browser.sh) before running a flow — or use a dry run, which "
    "touches nothing.")


# --- pure ---------------------------------------------------------------------

def build_run_argv(python_exe, *, flow_path, server_url, config_path, host=None,
                   graph_path=None, inputs=None, grant=None, dry_run=False, scope=None):
    """Assemble the argv LIST that runs one flow document as a subprocess.

    EVERY token is a separate list element — this is NEVER a shell string, so a user-supplied
    input value is an inert argv token, not an executable fragment.

    `--host` and `--graph` are mutually exclusive in flow_cmd, so an explicit graph path wins
    and the host is dropped. The grant maps onto flow_cmd's SAFE-BY-DEFAULT flags: submit and
    upload are opt-IN (``--allow-submit`` / ``--allow-upload`` only when granted), while
    download is on by default and must be explicitly WITHDRAWN (``--no-allow-download``) when
    the caller does not grant it. Pure."""
    grant = grant or {}
    argv = [
        python_exe, "-m", "pinchtab_webgraph.flow_cmd", "run", flow_path,
        "--jsonl",
        "--server", server_url,
        "--config", config_path,
    ]
    if graph_path:
        argv += ["--graph", graph_path]
    elif host:
        argv += ["--host", host]
    for name, value in (inputs or {}).items():
        if value is None:
            continue                       # an unset optional input is simply not passed
        argv += ["--input", "%s=%s" % (name, value)]
    if scope:
        argv += ["--scope", scope]
    if grant.get("allow_submit"):
        argv.append("--allow-submit")
    if grant.get("allow_upload"):
        argv.append("--allow-upload")
    if not grant.get("allow_download", True):
        argv.append("--no-allow-download")
    if dry_run:
        argv.append("--dry-run")
    return argv


def parse_frame_line(line):
    """Parse ONE line of the subprocess's JSONL stdout into a frame dict.

    ``flow_cmd --jsonl`` prints one JSON object per line ({"type":"step",…} then exactly one
    {"type":"result",…}). Anything that is NOT valid JSON — a stray print, a warning, a
    traceback line — becomes ``{"type":"log","line": …}`` (truncated to 500 chars) rather
    than being dropped: a line the UI never sees is a line that can't be debugged. Pure."""
    text = (line or "").strip()
    try:
        frame = json.loads(text)
    except ValueError:
        return {"type": "log", "line": (line or "")[:500]}
    if not isinstance(frame, dict) or "type" not in frame:
        return {"type": "log", "line": (line or "")[:500]}
    return frame


# --- I/O ----------------------------------------------------------------------

def resolve_config_path():
    """The PinchTab config path from $PINCHTAB_CONFIG, or FlowRunUnavailable("no_config").

    flow_cmd self-loads its bridge token from this config's ``server.token``. Same contract
    as live_crawl.resolve_config_path — the two features read the SAME env var because they
    drive the SAME bridge."""
    path = os.environ.get("PINCHTAB_CONFIG")
    if not path or not os.path.exists(path):
        raise FlowRunUnavailable("no_config", _NO_CONFIG_HINT)
    return path


def server_url():
    """The PinchTab bridge a flow run targets — the SAME one live_crawl uses.

    Deliberately reads live_crawl's env var and default rather than minting its own: a flow
    run and a crawl both lease the single-tenant bridge's tab, and that IDENTITY is precisely
    why the two features must veto each other's capacity slot."""
    return os.environ.get("PINCHTAB_WEBGRAPH_CRAWL_SERVER") or live_crawl.DEFAULT_CRAWL_SERVER


@contextmanager
def staged_flow_doc(doc):
    """Write a flow document to a temp file for the subprocess to read; always clean it up.

    flow_cmd's contract is a PATH (a flow is a file on disk), but the UI's flows live in
    flow_store records. The staging dir sits inside ``cache_store.home_dir()`` so it shares
    the store's filesystem and lifetime, and it is removed on every exit path."""
    staging_parent = os.path.join(cache_store.home_dir(), "flow-staging")
    os.makedirs(staging_parent, exist_ok=True)
    staging_dir = tempfile.mkdtemp(dir=staging_parent)
    path = os.path.join(staging_dir, "flow.json")
    try:
        with open(path, "w") as f:
            json.dump(doc, f)
        yield path
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


# --- session lifecycle --------------------------------------------------------

@dataclass
class FlowRunSession:
    """Everything the route needs to pump + finish one flow run."""
    process: object                # asyncio.subprocess.Process
    host: str
    flow_id: str
    run_id: str


@asynccontextmanager
async def open_flow_run_session(*, flow_path, host, flow_id, run_id, graph_path=None,
                                inputs=None, grant=None, dry_run=False, scope=None):
    """Launch the flow_cmd subprocess and yield a ready FlowRunSession.

    Broken out as an @asynccontextmanager so tests can monkeypatch server.py's reference with
    a fake that yields a scripted FlowRunSession — no real subprocess, no bridge.

    A DRY RUN SKIPS THE BRIDGE PREFLIGHT ENTIRELY. That is not an optimisation: flow_cmd's
    own invariant is that `--dry-run` touches NOTHING — it resolves no tab, opens no artifact
    directory, sends no browser command — so demanding a live bridge (or even a config file)
    before letting someone preview what a flow WOULD do would be a lie about the risk.

    A LIVE run preflights the bridge with a cheap socket probe (off the event loop, so a slow
    bridge can't stall every other client) and raises ``FlowRunUnavailable`` rather than
    spawning a process against nothing.

    The subprocess runs in its OWN process group; the ``finally`` ALWAYS cancels it
    (idempotent) even on an early raise — no orphan process holding the bridge's tab."""
    url = server_url()
    if dry_run:
        # No token is read and no bridge is contacted, so an absent config is not an error;
        # the path is just an inert argv token flow_cmd will never open.
        config_path = os.environ.get("PINCHTAB_CONFIG") or DEFAULT_CONFIG_NAME
    else:
        config_path = resolve_config_path()
        # bridge_reachable() is a blocking socket connect — run it off the event loop (the
        # same discipline live_crawl.open_crawl_session uses).
        if not await asyncio.to_thread(live_crawl.bridge_reachable, url):
            raise FlowRunUnavailable("bridge_unreachable", _BRIDGE_UNREACHABLE_HINT % url)

    argv = build_run_argv(
        sys.executable, flow_path=flow_path, server_url=url, config_path=config_path,
        host=host, graph_path=graph_path, inputs=inputs, grant=grant, dry_run=dry_run,
        scope=scope)

    session = None
    try:
        process = await asyncio.create_subprocess_exec(
            *argv, env=os.environ.copy(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True)
        session = FlowRunSession(process=process, host=host, flow_id=flow_id, run_id=run_id)
        yield session
    finally:
        if session is not None:
            await cancel_run_session(session)           # idempotent — no-op if exited


async def _pump_stream(stream, *, emit, transform):
    """Read one subprocess stream line-by-line, emit a frame per line. NEVER raises."""
    if stream is None:
        return
    while True:
        try:
            raw = await stream.readline()
        except Exception:  # noqa: BLE001 — a broken pipe / closed transport ends the pump
            return
        if not raw:
            return                                       # EOF
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line:
            continue
        frame = transform(line)
        if frame is None:
            continue
        try:
            await emit(frame)
        except Exception:  # noqa: BLE001 — client gone / send failed: stop, never raise
            return


async def pump_frames(session, *, emit):
    """Relay the subprocess's stdout JSONL frames (and its stderr) to ``emit``.

    stdout: every line becomes ``parse_frame_line(line)`` — a ``step`` frame, the terminal
    ``result`` frame, or a ``log`` frame for anything that wasn't JSON. EVERY frame is
    emitted, including the terminal one; the caller decides what to forward to the client.

    stderr: relayed as ``{"type":"log","line":"[stderr] …"}``. This is not decoration — if
    the flow process dies on a traceback, that traceback is the ONLY explanation anyone will
    get, and it must reach the user rather than vanish into a closed pipe.

    Returns the LAST ``result`` payload seen, or None if the process exited without printing
    one (the route synthesizes an honest error result in that case). NEVER raises."""
    seen = {"result": None}

    async def on_stdout(frame):
        if frame.get("type") == "result":
            seen["result"] = frame
        await emit(frame)

    await asyncio.gather(
        _pump_stream(session.process.stdout, emit=on_stdout, transform=parse_frame_line),
        _pump_stream(session.process.stderr, emit=emit,
                     transform=lambda line: {"type": "log",
                                             "line": ("[stderr] " + line)[:500]}),
    )
    return seen["result"]


def terminate_process_group(process, sig):
    """Send ``sig`` to the subprocess's whole group (falls back to the single process).

    flow_cmd + any child (a PinchTab helper) share a process group via
    ``start_new_session=True``, so signalling the group reaches all of them. Idempotent: a
    None / already-exited process is a no-op. NEVER raises."""
    if process is None or process.returncode is not None:
        return
    try:
        os.killpg(os.getpgid(process.pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.send_signal(sig)
        except Exception:  # noqa: BLE001
            pass


async def cancel_run_session(session, *, timeout=5.0):
    """Stop the flow's process group (SIGTERM -> bounded wait -> SIGKILL). NEVER raises.

    Idempotent: if the process already exited this returns immediately. This is the ONLY
    cancellation primitive the flow layer has — ``runner.execute`` never checks a flag — so a
    Cancel click, a client disconnect, and an unwind all land here."""
    process = session.process
    if process is None or process.returncode is not None:
        return
    terminate_process_group(process, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout)
        return
    except asyncio.TimeoutError:
        pass
    except Exception:  # noqa: BLE001 — best-effort teardown
        return
    # SIGTERM ignored/slow — escalate to SIGKILL (uncatchable) and reap the leader.
    terminate_process_group(process, signal.SIGKILL)
    try:
        await asyncio.wait_for(process.wait(), timeout)
    except Exception:  # noqa: BLE001
        pass
