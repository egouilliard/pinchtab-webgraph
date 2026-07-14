#!/usr/bin/env python3
"""Disk persistence for FLOWS and their RUN HISTORY — saved automations, per host.

`flow.py` gave us an executable document and `flow_cmd.py` a CLI to run it, but a flow
that lives in a file on someone's laptop is not an automation anyone can USE. This is the
store that turns a flow document into a durable, listable, re-runnable object the web UI
can show, edit, run, and audit:

  ~/.pinchtab-webgraph/flows/<host>/<flow_id>.json                 the flow record (the doc)
  ~/.pinchtab-webgraph/flows/<host>/<flow_id>/runs/<run_id>.json   one execution's record

TWO DIFFERENT "host"s — do not conflate them:

  * the `<host>` PATH SEGMENT here is a STORAGE PARTITION KEY. It is what the UI routes by
    (the sidebar's selected host), it is validated by ``cache_store.validate_host``, and it
    decides only WHERE the record lives on disk. It has no runtime meaning.
  * a flow document's OWN optional ``flow["host"]`` field is a RUNTIME NAVIGATION GUARD —
    ``runner._guard_host`` refuses to navigate a step off that host mid-run. It is part of
    the document's semantics, may be absent, and may legitimately differ from the partition
    the user filed the flow under.

Nothing here reads ``flow["host"]``; nothing in the runner reads the partition key.

MIRRORS chat_store.py exactly: stdlib-only, a per-host directory under ``home_dir()``,
atomic writes (tmp + os.replace), and a single id/host validation choke-point that rejects
path traversal before any filesystem access. Reuses ``cache_store.validate_host`` and
``cache_store.home_dir`` rather than re-implementing them.
"""
import datetime
import glob
import json
import os
import re
import shutil
import uuid

from .. import cache_store, flow as flow_mod

# A flow id / run id is a uuid4 hex (32 lowercase hex chars) — no separators, no dots, so a
# raw id can never resolve OUTSIDE its host's flow directory (the twin of
# chat_store._SESSION_ID_RE). validate_flow_id / validate_run_id are the choke-points every
# path routes through.
_FLOW_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# Hard caps.
#
# FLOWS are HARD-REJECTED at the per-host cap (TooManyFlows, no silent eviction) — exactly
# like chat_store's sessions. A flow is authored content; deleting one behind the user's
# back to make room for another would destroy work they wrote.
MAX_FLOWS_PER_HOST = 200

# RUNS deliberately DIVERGE: at the cap we FIFO-EVICT the oldest run instead of rejecting
# the new one. A run history is an AUDIT TRAIL of a REUSABLE automation, not authored
# content. Hard-rejecting run #51 the way we hard-reject flow #201 would mean "this saved
# automation can never be run again" — an unacceptable failure mode for the thing the whole
# feature exists to do. Losing the oldest audit line is the cheap failure; losing the ability
# to run is the expensive one.
MAX_RUNS_PER_FLOW = 50

# A persisted run's `steps` log is trimmed to its trailing N entries (a paginate-over-500-
# pages run can emit thousands). The trail is for a human reading back what happened, and
# the tail is the part that explains how it ended.
MAX_RUN_LOG_ENTRIES = 2000


class TooManyFlows(Exception):
    """The per-host flow cap is reached. Callers turn this into a 429, never a 500."""


def _now_iso():
    """An ISO-8601 UTC timestamp with a trailing Z (e.g. 2026-07-13T09:12:00.123456Z)."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


# --- paths (every one routes through a validation choke-point) ----------------

def flows_dir():
    return os.path.join(cache_store.home_dir(), "flows")


def host_flows_dir(host):
    """The directory holding one host's flow records. Validates the host token."""
    cache_store.validate_host(host)
    return os.path.join(flows_dir(), host)


def validate_flow_id(flow_id):
    # VALIDATE at the choke point: flow_path/load/update/delete/runs_dir all route through
    # here, so rejecting a non-uuid token blocks path traversal for every caller before any
    # filesystem access (mirrors cache_store.validate_host / chat_store.validate_session_id).
    if not isinstance(flow_id, str) or not _FLOW_ID_RE.match(flow_id):
        raise ValueError("invalid flow id: %r" % (flow_id,))


def new_flow_id():
    return uuid.uuid4().hex


def flow_path(host, flow_id):
    validate_flow_id(flow_id)
    return os.path.join(host_flows_dir(host), "%s.json" % flow_id)


def runs_dir(host, flow_id):
    """The per-flow run directory — a SIBLING of <flow_id>.json, named <flow_id>/runs."""
    validate_flow_id(flow_id)
    return os.path.join(host_flows_dir(host), flow_id, "runs")


def validate_run_id(run_id):
    if not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id):
        raise ValueError("invalid run id: %r" % (run_id,))


def new_run_id():
    return uuid.uuid4().hex


def run_path(host, flow_id, run_id):
    validate_run_id(run_id)
    return os.path.join(runs_dir(host, flow_id), "%s.json" % run_id)


def atomic_write(path, obj):
    # ATOMIC: write to <path>.tmp then os.replace onto the target, so a reader never sees a
    # half-written file (the same pattern as cache_store.atomic_write / chat_store).
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def _read_json(path):
    with open(path) as f:
        return json.load(f)


def _json_files(directory):
    return [p for p in glob.glob(os.path.join(directory, "*.json"))
            if not p.endswith(".json.tmp")]


# --- flow records --------------------------------------------------------------

# The canonical on-disk schema, written field-by-field so no caller-supplied extra key can
# smuggle itself into the record (or back out into a response).
def _persisted(record):
    return {
        "id": record["id"],
        "host": record["host"],
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
        "doc": record["doc"],
    }


def count_runs(host, flow_id):
    """How many runs this flow has on disk. Cheap (a glob), so summaries can carry it."""
    return len(_json_files(runs_dir(host, flow_id)))


def summary(record):
    """The lightweight card view of a flow — OMITS the full doc.

    Carries what a UI needs to render the flow's chip AND its run form (the declared inputs
    and the capabilities the doc asks for), so listing never forces a second fetch."""
    doc = record.get("doc") or {}
    return {
        "id": record["id"],
        "host": record["host"],
        "name": doc.get("name"),
        "steps": len(doc.get("steps") or []),
        "capabilities": flow_mod.capabilities(doc),
        "inputs": doc.get("inputs") or {},
        "run_count": count_runs(record["host"], record["id"]),
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
    }


def _count_flows(host):
    return len(_json_files(host_flows_dir(host)))


def create(host, doc):
    """Validate a flow document, persist it under `host`, and return the new record.

    ``flow_mod.validate`` runs FIRST and its FlowError is deliberately allowed to PROPAGATE:
    the store does not get to decide whether a bad document is a 400, a 422, or a structured
    200 — the ROUTE does (the house convention makes a validation miss a 200 with the status
    in the body). Raises TooManyFlows at the per-host cap (no silent eviction)."""
    cache_store.validate_host(host)
    flow_mod.validate(doc)
    if _count_flows(host) >= MAX_FLOWS_PER_HOST:
        raise TooManyFlows("host %r already has %d flows" % (host, MAX_FLOWS_PER_HOST))
    now = _now_iso()
    record = {"id": new_flow_id(), "host": host, "created_at": now, "updated_at": now,
              "doc": doc}
    atomic_write(flow_path(host, record["id"]), _persisted(record))
    return record


def load(host, flow_id):
    """Load one flow record (id/host/created_at/updated_at/doc), or None if absent."""
    path = flow_path(host, flow_id)
    if not os.path.exists(path):
        return None
    return _read_json(path)


def list_flows(host):
    """Summaries of every flow for a host, newest (updated_at) first. Never raises."""
    cache_store.validate_host(host)
    out = []
    for p in _json_files(host_flows_dir(host)):
        try:
            out.append(summary(_read_json(p)))
        except (OSError, ValueError, KeyError):
            continue  # a corrupt record never breaks the whole list
    out.sort(key=lambda s: s.get("updated_at") or "", reverse=True)
    return out


def update(host, flow_id, doc):
    """Full replace of a flow's document. Returns the record, or None if it doesn't exist.

    Re-validates (a PUT is exactly as untrusted as a POST — FlowError propagates to the
    route) and PRESERVES the record's identity: `id` and `created_at` are never rewritten by
    a caller-supplied body."""
    existing = load(host, flow_id)
    if existing is None:
        return None
    flow_mod.validate(doc)
    record = {"id": existing["id"], "host": host,
              "created_at": existing["created_at"], "updated_at": _now_iso(),
              "doc": doc}
    atomic_write(flow_path(host, flow_id), _persisted(record))
    return record


def delete(host, flow_id):
    """Remove a flow AND CASCADE to its run history. Idempotent — True if anything went.

    The cascade is the point: a run record is meaningless without the flow it ran, and an
    orphaned runs/ directory would silently keep counting against nothing."""
    path = flow_path(host, flow_id)
    existed = os.path.exists(path)
    if existed:
        os.remove(path)
    # rmtree the <flow_id>/ dir (holding runs/) even when the record file was already gone —
    # a half-deleted flow must not leave its audit trail behind.
    shutil.rmtree(os.path.join(host_flows_dir(host), flow_id), ignore_errors=True)
    return existed


# --- run records ---------------------------------------------------------------

def _persisted_run(record):
    return {
        "id": record["id"],
        "flow_id": record["flow_id"],
        "host": record["host"],
        "status": record["status"],
        "dry_run": bool(record.get("dry_run")),
        "cancelled": bool(record.get("cancelled")),
        "capabilities": record.get("capabilities") or {},
        "inputs": record.get("inputs") or {},
        "started_at": record["started_at"],
        "finished_at": record.get("finished_at"),
        "duration_s": record.get("duration_s"),
        "detail": record.get("detail"),
        "aborted": record.get("aborted"),
        "stats": record.get("stats") or {},
        "steps": record.get("steps") or [],
        "artifacts": record.get("artifacts") or [],
        "collected": record.get("collected") or {},
    }


def run_summary(record):
    """The chip view of a run — OMITS steps/artifacts/collected (the heavy payloads)."""
    return {k: v for k, v in _persisted_run(record).items()
            if k not in ("steps", "artifacts", "collected")}


def start_run(host, flow_id, run_id, *, dry_run, capabilities, inputs):
    """Persist a "running" PLACEHOLDER for a run, immediately, before anything is spawned.

    Written up front on purpose: if the flow process crashes, is SIGKILLed, or the whole
    server dies mid-run, the run is still DISCOVERABLE — a record stuck at "running" tells
    the truth ("we started this and never heard back"), where no record at all would silently
    lose the fact that a real browser did real things."""
    validate_run_id(run_id)
    record = {"id": run_id, "flow_id": flow_id, "host": host,
              "status": "running", "dry_run": bool(dry_run), "cancelled": False,
              "capabilities": capabilities or {}, "inputs": inputs or {},
              "started_at": _now_iso(), "finished_at": None, "duration_s": None,
              "detail": None, "aborted": None,
              "stats": {}, "steps": [], "artifacts": [], "collected": {}}
    atomic_write(run_path(host, flow_id, run_id), _persisted_run(record))
    return record


def _evict_old_runs(host, flow_id):
    """FIFO-evict the oldest runs over MAX_RUNS_PER_FLOW. See the constant's rationale."""
    paths = _json_files(runs_dir(host, flow_id))
    if len(paths) <= MAX_RUNS_PER_FLOW:
        return []
    dated = []
    for p in paths:
        try:
            dated.append((_read_json(p).get("started_at") or "", p))
        except (OSError, ValueError):
            dated.append(("", p))     # a corrupt run sorts oldest — it is the first to go
    dated.sort()
    evicted = []
    for _started, p in dated[:len(dated) - MAX_RUNS_PER_FLOW]:
        try:
            os.remove(p)
            evicted.append(p)
        except OSError:
            continue
    return evicted


def finish_run(host, flow_id, run_id, result, *, cancelled=False):
    """Fold a terminal runner result onto the run's placeholder and persist it.

    ``result`` is the runner's own run record (status/steps/artifacts/collected/stats/…) —
    or the SYNTHESIZED stand-in the route builds when the process died without printing one.
    The step log is trimmed to its trailing MAX_RUN_LOG_ENTRIES, the record is written, and
    ONLY THEN is the FIFO eviction run — so the run we just finished is never the one evicted
    to make room for itself."""
    record = load_run(host, flow_id, run_id) or {
        "id": run_id, "flow_id": flow_id, "host": host, "status": "running",
        "dry_run": False, "cancelled": False, "capabilities": {}, "inputs": {},
        "started_at": _now_iso()}
    result = dict(result or {})

    steps = result.get("steps") or []
    if len(steps) > MAX_RUN_LOG_ENTRIES:
        steps = steps[-MAX_RUN_LOG_ENTRIES:]

    record.update({
        "status": result.get("status") or "error",
        "cancelled": bool(cancelled),
        "finished_at": _now_iso(),
        "duration_s": result.get("duration_s"),
        "detail": result.get("detail"),
        "aborted": result.get("aborted"),
        "stats": result.get("stats") or {},
        "steps": steps,
        "artifacts": result.get("artifacts") or [],
        "collected": result.get("collected") or {},
    })
    if "dry_run" in result:
        record["dry_run"] = bool(result["dry_run"])

    atomic_write(run_path(host, flow_id, run_id), _persisted_run(record))
    _evict_old_runs(host, flow_id)
    return record


def list_runs(host, flow_id):
    """Summaries of every run of a flow, newest (started_at) first. Never raises."""
    validate_flow_id(flow_id)
    out = []
    for p in _json_files(runs_dir(host, flow_id)):
        try:
            out.append(run_summary(_read_json(p)))
        except (OSError, ValueError, KeyError):
            continue  # a corrupt record never breaks the whole list
    out.sort(key=lambda r: r.get("started_at") or "", reverse=True)
    return out


def load_run(host, flow_id, run_id):
    """The FULL run record (including steps/artifacts/collected), or None if absent."""
    path = run_path(host, flow_id, run_id)
    if not os.path.exists(path):
        return None
    return _read_json(path)
