#!/usr/bin/env python3
"""Live crawl: spawn `interaction_crawl` for a URL and promote its graph into the cache.

This is the Phase-3 WRITE side of the OPTIONAL web UI: the SPA sidebar's "New crawl"
form posts a URL, and this module launches `python -m pinchtab_webgraph.interaction_crawl`
as a subprocess, streams its stderr progress out over the `/ws/crawl` WebSocket, and,
when the crawl finishes (or is cancelled/SIGTERM'd), ATOMICALLY moves the crawler's
written interaction-graph JSON into `cache_store.cache_path(host)` so it appears in the
sidebar and is immediately usable by the Graph view + chat.

MIRRORS screencast.py's discipline exactly:
  * stdlib-only at module scope — nothing here imports a heavy/optional dep at import
    time, so server.py can `from . import live_crawl` while the base package stays a
    pure-stdlib install;
  * a missing config / unreachable bridge / disabled feature degrades to a structured
    ``CrawlUnavailable(reason, detail)`` rather than a crash — the twin of
    ``ScreencastUnavailable``;
  * the subprocess runs in its OWN process group (``start_new_session=True``) so teardown
    can signal the WHOLE tree via ``killpg``; ``open_crawl_session``'s ``finally`` ALWAYS
    terminates it and removes its staging dir — no orphan process, no leaked temp files.

SECURITY INVARIANTS (must hold):
  * The user URL NEVER becomes a shell string — ``build_crawl_argv`` emits an argv LIST,
    and ``open_crawl_session`` uses ``asyncio.create_subprocess_exec`` (no shell). A
    hostile URL is an inert argv token, never an executable statement.
  * The crawl is OFF unless ``PINCHTAB_WEBGRAPH_ENABLE_CRAWL`` is truthy (the route
    enforces this) — the default install must not launch a real browser click-through.
  * The staging dir lives INSIDE ``cache_store.home_dir()`` so the final ``os.replace``
    onto the cache path is an atomic same-filesystem move, not a cross-device copy.
"""
import asyncio
import json
import os
import shutil
import signal
import socket
import sys
import tempfile
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urlparse

from .. import cache_store

DEFAULT_CRAWL_SERVER = "http://localhost:9871"
MAX_LIVE_CRAWLS = 1


class CrawlUnavailable(Exception):
    """A live crawl cannot start — the feature is off, config is missing, or the
    PinchTab bridge is unreachable.

    ``reason`` is one of {"disabled", "no_config", "bridge_unreachable"}; ``detail`` is
    a human remedy hint. The WS route turns this into a structured error frame + close,
    never a 500. Mirrors ``screencast.ScreencastUnavailable``.
    """

    def __init__(self, reason, detail):
        super().__init__("%s: %s" % (reason, detail))
        self.reason = reason
        self.detail = detail


_DISABLED_HINT = (
    "Live crawl is disabled. It makes the server launch a real browser that clicks "
    "through the whole target app; set PINCHTAB_WEBGRAPH_ENABLE_CRAWL=1 to enable it.")
_NO_CONFIG_HINT = (
    "No PinchTab crawl config found. Set $PINCHTAB_CONFIG to the path of your "
    "crawl-config.json (it carries the bridge token the crawler self-loads).")
_BRIDGE_UNREACHABLE_HINT = (
    "Could not reach the PinchTab bridge at %s. Start one (e.g. "
    "scripts/start-crawl-browser.sh) before launching a crawl.")


# --- pure ---------------------------------------------------------------------

def parse_start_url(url):
    """Validate a crawl start URL and return its host token. Raises ValueError on any
    rejection (bad scheme, no hostname, or a hostname the cache guard rejects).

    Accepts ONLY http/https with a non-empty hostname, then routes the hostname through
    ``cache_store.validate_host`` — the same choke-point every cache path uses, so a
    hostile ``host`` can never resolve OUTSIDE caches_dir(). Pure (no I/O)."""
    p = urlparse(url or "")
    if p.scheme not in ("http", "https"):
        raise ValueError("start URL must be http/https: %r" % (url,))
    host = p.hostname
    if not host:
        raise ValueError("start URL has no hostname: %r" % (url,))
    cache_store.validate_host(host)   # raises ValueError on a bad token
    return host


def clamp_max_states(v):
    """Clamp the crawl's --max-states into [10, 500]; default 60 when unset/invalid."""
    try:
        v = int(v)
    except (TypeError, ValueError):
        return 60
    return max(10, min(500, v))


def clamp_max_depth(v):
    """Clamp the crawl's --max-depth into [1, 8]; default 4 when unset/invalid."""
    try:
        v = int(v)
    except (TypeError, ValueError):
        return 4
    return max(1, min(8, v))


# interaction_crawl.py prints ONE progress line per visited state to stderr, exactly:
#   "· [%d states / %d visits] depth %d · %s (%d controls%s)"
# where the trailing %s is either "" or ", %d items". We extract the four counters +
# the URL; the optional ", N items" tail is ignored (it is not part of the frame).
PROGRESS_RE = re.compile(
    r"^· \[(?P<states>\d+) states / (?P<visits>\d+) visits\] "
    r"depth (?P<depth>\d+) · (?P<url>.*?) \((?P<controls>\d+) controls")


def parse_progress_line(line):
    """Parse ONE crawler stderr line into a ``progress`` frame dict, or None.

    Matches the exact ``· [N states / M visits] depth D · <url> (C controls…)`` format
    and returns ``{"type":"progress","states","visits","depth","url","controls"}`` with
    the counters as ints. Non-matching lines (the banner, ``✓ trigger …``, the final
    ``Wrote …`` summary, warnings) return None so the caller emits them as ``log``. Pure."""
    m = PROGRESS_RE.match(line or "")
    if not m:
        return None
    return {"type": "progress",
            "states": int(m.group("states")),
            "visits": int(m.group("visits")),
            "depth": int(m.group("depth")),
            "url": m.group("url"),
            "controls": int(m.group("controls"))}


def build_crawl_argv(python_exe, *, start_url, server_url, config_path, out_path,
                     max_states, max_depth, checkpoint_every=5):
    """Assemble the argv LIST that launches the interaction crawler as a subprocess.

    EVERY token is a separate list element — this is NEVER a shell string, so the user
    ``start_url`` (and every other value) is an inert argv token, not an executable
    fragment. The crawler appends ``.json`` to ``out_path`` when it writes. Pure."""
    return [
        python_exe, "-m", "pinchtab_webgraph.interaction_crawl",
        "--start", start_url,
        "--server", server_url,
        "--config", config_path,
        "--out", out_path,
        "--max-states", str(max_states),
        "--max-depth", str(max_depth),
        "--checkpoint-every", str(checkpoint_every),
    ]


# --- I/O ----------------------------------------------------------------------

def resolve_config_path():
    """The PinchTab crawl config path from $PINCHTAB_CONFIG, or CrawlUnavailable.

    The crawler self-loads its bridge token from this config's ``server.token``. When
    $PINCHTAB_CONFIG is unset or points at a missing file, raise
    ``CrawlUnavailable("no_config", …)`` so the route degrades to a structured frame."""
    path = os.environ.get("PINCHTAB_CONFIG")
    if not path or not os.path.exists(path):
        raise CrawlUnavailable("no_config", _NO_CONFIG_HINT)
    return path


def bridge_reachable(server_url, timeout=1.5):
    """True if a TCP connection to the bridge's host:port succeeds within ``timeout``.

    Mirrors tests/e2e/test_crawl_upload_e2e.py:_reachable — a cheap socket probe so the
    route can fail fast with a structured ``bridge_unreachable`` instead of spawning a
    crawl that would immediately die."""
    p = urlparse(server_url)
    host, port = p.hostname or "127.0.0.1", p.port or 80
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def promote_result(host, staging_json_path):
    """ATOMICALLY move the crawler's staging graph onto the host's cache path.

    Returns None when the crawler wrote nothing (the file is absent). Otherwise loads +
    minimally validates the graph (``graph["meta"]`` must exist) — letting the standard
    ``(OSError, ValueError, json.JSONDecodeError, KeyError)`` tuple propagate on a
    corrupt/partial file — then ``os.replace``s it onto ``cache_store.cache_path(host)``
    (an atomic same-filesystem move, since the staging dir lives inside home_dir()).
    Returns the graph's ``meta`` so the caller can build the terminal frame."""
    if not os.path.exists(staging_json_path):
        return None
    with open(staging_json_path) as f:
        graph = json.load(f)
    meta = graph["meta"]                       # KeyError on a graph with no meta
    target = cache_store.cache_path(host)
    os.makedirs(os.path.dirname(target), exist_ok=True)   # first-ever crawl: no caches/ yet
    os.replace(staging_json_path, target)      # atomic same-filesystem move
    return meta


# --- session lifecycle --------------------------------------------------------

@dataclass
class CrawlSession:
    """Everything the route needs to pump + finish one crawl."""
    process: object                # asyncio.subprocess.Process
    staging_dir: str
    staging_json_path: str
    host: str
    start_url: str


@asynccontextmanager
async def open_crawl_session(start_url, *, host, max_states=None, max_depth=None):
    """Launch the crawler subprocess for ``start_url`` and yield a ready CrawlSession.

    Broken out as an @asynccontextmanager so tests can monkeypatch server.py's reference
    with a fake that yields a scripted CrawlSession — no real subprocess, no bridge.
    Raises ``CrawlUnavailable`` when config is missing or the bridge is down (the WS
    route maps it to a structured close). The subprocess runs in its OWN process group;
    the ``finally`` ALWAYS terminates it (idempotent) and removes the staging dir, even
    on an early raise — no orphan process, no leaked temp files.

    The staging dir is created INSIDE ``cache_store.home_dir()`` so promote_result's
    ``os.replace`` onto the cache path is an atomic same-filesystem move."""
    config_path = resolve_config_path()
    # The crawler's target bridge is deliberately its OWN env var, distinct from
    # screencast's PINCHTAB_WEBGRAPH_BRIDGE (they can be different physical bridges).
    server_url = os.environ.get("PINCHTAB_WEBGRAPH_CRAWL_SERVER") or DEFAULT_CRAWL_SERVER
    # bridge_reachable() is a blocking socket connect — run it off the event loop so a
    # down/slow bridge can't stall every other /ws/* and /api/* client (same discipline
    # as screencast.wait_for_cdp_ready threading its blocking urllib GET).
    if not await asyncio.to_thread(bridge_reachable, server_url):
        raise CrawlUnavailable("bridge_unreachable",
                               _BRIDGE_UNREACHABLE_HINT % server_url)

    staging_parent = os.path.join(cache_store.home_dir(), "crawl-staging")
    os.makedirs(staging_parent, exist_ok=True)
    staging_dir = tempfile.mkdtemp(dir=staging_parent)
    out_path = os.path.join(staging_dir, "graph")   # crawler appends ".json"

    argv = build_crawl_argv(
        sys.executable, start_url=start_url, server_url=server_url,
        config_path=config_path, out_path=out_path,
        max_states=clamp_max_states(max_states),
        max_depth=clamp_max_depth(max_depth))

    session = None
    try:
        process = await asyncio.create_subprocess_exec(
            *argv, cwd=staging_dir, env=os.environ.copy(),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            start_new_session=True)
        session = CrawlSession(process=process, staging_dir=staging_dir,
                               staging_json_path=out_path + ".json", host=host,
                               start_url=start_url)
        yield session
    finally:
        if session is not None:
            await cancel_session(session)               # idempotent — no-op if exited
        shutil.rmtree(staging_dir, ignore_errors=True)


async def pump_progress(session, *, emit):
    """Read the crawler's stderr line-by-line and emit progress/log frames.

    Each line becomes ``parse_progress_line(line)`` (a ``progress`` frame) or, when it
    doesn't match, a ``{"type":"log","line": …}`` frame (truncated to 500 chars).
    Returns cleanly on EOF. NEVER raises into the caller — a dead client (emit raising)
    or a broken stream just ends the pump."""
    stderr = session.process.stderr
    if stderr is None:
        return
    while True:
        try:
            raw = await stderr.readline()
        except Exception:  # noqa: BLE001 — a broken pipe / closed transport ends the pump
            return
        if not raw:
            return                                       # EOF
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line:
            continue
        frame = parse_progress_line(line) or {"type": "log", "line": line[:500]}
        try:
            await emit(frame)
        except Exception:  # noqa: BLE001 — client gone / send failed: stop, never raise
            return


def terminate_process_group(process, sig):
    """Send ``sig`` to the subprocess's whole group (falls back to the single process).

    The crawler + any child (a PinchTab helper) share a process group via
    ``start_new_session=True``, so signalling the group reaches all of them. Idempotent:
    a None / already-exited process is a no-op. NEVER raises."""
    if process is None or process.returncode is not None:
        return
    try:
        os.killpg(os.getpgid(process.pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.send_signal(sig)
        except Exception:  # noqa: BLE001
            pass


async def cancel_session(session, *, timeout=5.0):
    """Stop the crawl's process group (SIGTERM -> bounded wait -> SIGKILL). NEVER raises.

    Idempotent: if the process already exited this returns immediately. The crawler
    catches SIGTERM and atomically writes its PARTIAL graph before dying, so a cancelled
    crawl still leaves a promotable staging file."""
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


async def finish_session(session, *, cancelled):
    """Await the crawler's exit, promote its output, and build the ONE terminal frame.

    Reads ``meta.complete`` / ``meta.stopped`` from the WRITTEN graph — never inferred
    from the return code (interaction_crawl exits 0 for every stop except a wedge-gave-up).
    Promotion runs in a thread (blocking file I/O). Returns:
      * ``crawl_failed`` error frame if promotion raised (corrupt/partial file) OR, on a
        non-cancelled run, produced no output;
      * ``cancelled`` frame (with ``promoted`` + best-effort counts) when cancelled;
      * ``done`` frame (states/edges/triggers/complete/stopped from meta) otherwise."""
    process = session.process
    try:
        await process.wait()
    except Exception:  # noqa: BLE001 — already reaped / no transport
        pass

    try:
        meta = await asyncio.to_thread(promote_result, session.host,
                                       session.staging_json_path)
    except (OSError, ValueError, json.JSONDecodeError, KeyError) as e:
        return {"type": "error", "status": "crawl_failed", "host": session.host,
                "detail": str(e)[:200]}

    if cancelled:
        m = meta or {}
        return {"type": "cancelled", "host": session.host,
                "promoted": meta is not None,
                "states": m.get("states") if meta else None,
                "edges": m.get("edges") if meta else None,
                "triggers": m.get("triggers") if meta else None,
                "complete": m.get("complete") if meta else None,
                "stopped": m.get("stopped") if meta else None}

    if meta is None:
        return {"type": "error", "status": "crawl_failed", "host": session.host,
                "detail": "the crawl produced no output graph"}
    return {"type": "done", "host": session.host,
            "states": meta.get("states"), "edges": meta.get("edges"),
            "triggers": meta.get("triggers"), "complete": meta.get("complete"),
            "stopped": meta.get("stopped")}
