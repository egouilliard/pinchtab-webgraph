#!/usr/bin/env python3
"""Disk persistence for chat sessions — multiple named chats per host.

Phase 4 of the OPTIONAL web UI turns the ephemeral in-memory chat history (chat.py's
``ChatState.messages``, discarded when the WebSocket drops) into a durable, browsable
set of NAMED sessions keyed by (host, session-id). Each host owns a directory of
``<id>.json`` records; a record carries BOTH:

  * ``transcript`` — the display-only fold of the emitted WS frames (user text +
    assistant text/tool/tour/error entries), replayed verbatim on reconnect so the
    chat log is restored for EVERY backend; and
  * ``wire_messages`` — the API backend's serialized Anthropic message list, so the
    Anthropic-API backend can RESUME the conversation (the model recalls prior turns).
    Null/absent for the Claude Code backend, which in v1 restores the transcript for
    DISPLAY only (the SDK session is fresh, so it won't recall earlier turns yet — we
    do capture its ``sdk_session_id`` for a future resume).

MIRRORS cache_store.py exactly: stdlib-only, a per-host directory under
``home_dir()``, atomic writes (tmp + os.replace), and a single host/id validation
choke-point that rejects path traversal before any filesystem access. Reuses
``cache_store.validate_host`` and ``cache_store.home_dir`` rather than re-implementing
them.
"""
import datetime
import glob
import json
import os
import re
import uuid

from .. import cache_store

# A session id is a uuid4 hex (32 lowercase hex chars) — no separators, no dots, so a
# raw id can never resolve OUTSIDE its host's session directory (the twin of
# cache_store._HOST_RE). validate_session_id is the choke-point every path routes through.
_SESSION_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# Hard caps. Sessions are rejected at the per-host cap (NO silent eviction — a full host
# raises TooManySessions), and each transcript is trimmed to its trailing MAX entries.
MAX_SESSIONS_PER_HOST = 50
MAX_TRANSCRIPT_ENTRIES = 500

# The number of leading chars of the first user message an auto-title is derived from.
_TITLE_MAX_CHARS = 60


class TooManySessions(Exception):
    """The per-host session cap is reached. Callers turn this into a 429, never a 500."""


def _now_iso():
    """An ISO-8601 UTC timestamp with a trailing Z (e.g. 2026-07-07T09:12:00.123456Z)."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def sessions_dir():
    return os.path.join(cache_store.home_dir(), "sessions")


def host_sessions_dir(host):
    """The directory holding one host's session records. Validates the host token."""
    cache_store.validate_host(host)
    return os.path.join(sessions_dir(), host)


def validate_session_id(session_id):
    # VALIDATE at the choke point: session_path/load/save/rename/delete all route through
    # here, so rejecting a non-uuid token blocks path traversal for every caller before
    # any filesystem access (mirrors cache_store.validate_host).
    if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
        raise ValueError("invalid session id: %r" % session_id)


def new_session_id():
    return uuid.uuid4().hex


def session_path(host, session_id):
    validate_session_id(session_id)
    return os.path.join(host_sessions_dir(host), "%s.json" % session_id)


def atomic_write(path, obj):
    # ATOMIC: write to <path>.tmp then os.replace onto the target, so a reader never sees
    # a half-written file (the same pattern as cache_store.atomic_write). Creates the
    # host session dir first.
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


# The canonical on-disk schema. Written field-by-field so no ephemeral in-memory key
# (e.g. the _disk_len save-baseline) ever leaks onto disk or into a response.
def _persisted(record):
    return {
        "id": record["id"],
        "host": record["host"],
        "backend": record["backend"],
        # "workspace" for every record written before flow mode existed — an old session
        # must keep resuming as the navigation assistant it was created as.
        "mode": record.get("mode") or "workspace",
        "title": record.get("title"),
        "title_locked": bool(record.get("title_locked")),
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
        "message_count": record.get("message_count", 0),
        "sdk_session_id": record.get("sdk_session_id"),
        "transcript": record.get("transcript") or [],
        "wire_messages": record.get("wire_messages"),
    }


def summary(record):
    """The lightweight chip view of a session — OMITS transcript/wire_messages/sdk id."""
    return {
        "id": record["id"],
        "host": record["host"],
        "backend": record["backend"],
        "mode": record.get("mode") or "workspace",
        "title": record.get("title"),
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
        "message_count": record.get("message_count", 0),
    }


def _count_sessions(host):
    d = host_sessions_dir(host)
    return len([p for p in glob.glob(os.path.join(d, "*.json"))
                if not p.endswith(".json.tmp")])


def create(host, *, backend, title=None, mode="workspace"):
    """Mint a new session record, persist it immediately, and return it.

    Raises TooManySessions if the host is already at MAX_SESSIONS_PER_HOST (no silent
    eviction). The record is atomically written on creation so a fresh chat survives a
    reconnect even before its first message. ``mode`` ("workspace" | "flow") is written
    ONCE here and pinned on every resume — like ``backend``, it is never re-resolved.
    """
    cache_store.validate_host(host)
    if _count_sessions(host) >= MAX_SESSIONS_PER_HOST:
        raise TooManySessions(
            "host %r already has %d sessions" % (host, MAX_SESSIONS_PER_HOST))
    now = _now_iso()
    record = {
        "id": new_session_id(),
        "host": host,
        "backend": backend,
        "mode": mode or "workspace",
        "title": title,
        "title_locked": bool(title),
        "created_at": now,
        "updated_at": now,
        "message_count": 0,
        "sdk_session_id": None,
        "transcript": [],
        "wire_messages": [] if backend == "api" else None,
    }
    atomic_write(session_path(host, record["id"]), _persisted(record))
    record["_disk_len"] = 0
    return record


def load(host, session_id):
    """Load one session record (with an in-memory save baseline), or None if absent."""
    path = session_path(host, session_id)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        record = json.load(f)
    # in-memory only: how many transcript entries are already on disk, so save() appends
    # only THIS session's new entries instead of blindly overwriting a concurrent writer.
    record["_disk_len"] = len(record.get("transcript") or [])
    return record


def list_sessions(host, mode=None):
    """Summaries of every session for a host, newest (updated_at) first. Never raises.

    ``mode`` optionally filters to one mode ("workspace" | "flow") — the Chat tab lists
    workspace chats and the Flows tab lists flow chats, from the same store. A record with
    no ``mode`` (written before flow mode existed) counts as "workspace".
    """
    cache_store.validate_host(host)
    out = []
    for p in glob.glob(os.path.join(host_sessions_dir(host), "*.json")):
        if p.endswith(".json.tmp"):
            continue
        try:
            with open(p) as f:
                out.append(summary(json.load(f)))
        except (OSError, ValueError, KeyError):
            continue  # a corrupt record never breaks the whole list
    if mode is not None:
        out = [s for s in out if s.get("mode") == mode]
    out.sort(key=lambda s: s.get("updated_at") or "", reverse=True)
    return out


def save(record):
    """Persist a record after a turn: merge this turn's new transcript entries onto the
    freshest on-disk transcript, trim to MAX, recompute message_count, bump updated_at.

    The transcript is NOT blindly overwritten: we re-read the on-disk transcript and
    append only the entries this session added since its last save (record["_disk_len"]),
    so two connections to the same session can't clobber each other's turns. wire_messages
    IS the authoritative full history from the ChatState, so it is written as-is.
    """
    base = record.get("_disk_len", 0)
    new_entries = (record.get("transcript") or [])[base:]

    disk = load(record["host"], record["id"])
    disk_transcript = (disk.get("transcript") if disk else None) or []
    merged = disk_transcript + new_entries
    if len(merged) > MAX_TRANSCRIPT_ENTRIES:
        merged = merged[-MAX_TRANSCRIPT_ENTRIES:]

    record["transcript"] = merged
    record["_disk_len"] = len(merged)
    record["message_count"] = len(merged)
    record["updated_at"] = _now_iso()
    atomic_write(session_path(record["host"], record["id"]), _persisted(record))
    return record


def rename(host, session_id, title):
    """Set an explicit title (and lock it so auto-title never overwrites it). None if
    the session is absent."""
    record = load(host, session_id)
    if record is None:
        return None
    record["title"] = title
    record["title_locked"] = True
    record["updated_at"] = _now_iso()
    atomic_write(session_path(host, session_id), _persisted(record))
    return record


def delete(host, session_id):
    """Remove one session record. Idempotent — True if a file was removed, else False."""
    path = session_path(host, session_id)
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


def _derive_title(text):
    """The auto-title from a user message: collapsed whitespace, capped at _TITLE_MAX."""
    t = " ".join((text or "").split()).strip()
    if not t:
        return None
    return t[:_TITLE_MAX_CHARS]


def append_display_frame(record, frame):
    """Fold ONE emitted WS frame (or a synthetic user frame) into record["transcript"].

    Pure mutator. Streamed ``text`` deltas accumulate into the running last assistant
    text entry; a tool_use / tool_result / tour / error frame flushes that run and is
    appended as its own entry (a following text delta then starts a fresh entry); a
    ``done`` frame just closes the turn (no entry). A ``user`` frame appends the user's
    text and, when the title is still empty and not locked, auto-titles the session from
    it. Every entry carries a ``ts``. Unknown frame types are ignored.
    """
    transcript = record.setdefault("transcript", [])
    ftype = frame.get("type")
    ts = _now_iso()

    if ftype == "user":
        text = frame.get("text") or ""
        transcript.append({"role": "user", "type": "user", "text": text, "ts": ts})
        if not record.get("title") and not record.get("title_locked"):
            title = _derive_title(text)
            if title:
                record["title"] = title
        return

    if ftype == "text":
        delta = frame.get("delta") or ""
        if not delta:
            return
        last = transcript[-1] if transcript else None
        if last is not None and last.get("type") == "text" and last.get("role") == "assistant":
            last["text"] = (last.get("text") or "") + delta
        else:
            transcript.append({"role": "assistant", "type": "text",
                               "text": delta, "ts": ts})
        return

    if ftype == "tool_use":
        transcript.append({"role": "assistant", "type": "tool_use",
                           "name": frame.get("name"), "ts": ts})
        return

    if ftype == "tool_result":
        transcript.append({"role": "assistant", "type": "tool_result",
                           "name": frame.get("name"), "status": frame.get("status"),
                           "ts": ts})
        return

    if ftype == "tour":
        transcript.append({"role": "assistant", "type": "tour",
                           "data": frame.get("data"), "ts": ts})
        return

    if ftype == "flow_draft":
        # The proposed document is persisted IN the transcript (not just its note), so
        # reopening a flow chat restores the draft on the canvas — the twin of `tour`.
        transcript.append({"role": "assistant", "type": "flow_draft",
                           "doc": frame.get("doc"), "status": frame.get("status"),
                           "path": frame.get("path"), "error": frame.get("error"),
                           "name": frame.get("name"), "note": frame.get("note"),
                           "ts": ts})
        return

    if ftype == "error":
        transcript.append({"role": "assistant", "type": "error",
                           "status": frame.get("status"), "reason": frame.get("reason"),
                           "detail": frame.get("detail"), "ts": ts})
        return

    # "done" (and anything else): flush the running text run implicitly — no entry.


class TranscriptSink:
    """An ``emit`` wrapper that mirrors every emitted frame into a session transcript.

    The WS route passes a real ``emit`` (websocket.send_json); this wraps it so each
    frame is BOTH sent to the client AND folded into the record via append_display_frame.
    The user's own message is recorded separately (it is never emitted), so the transcript
    holds the full display log for restore.
    """

    def __init__(self, record, emit):
        self.record = record
        self._emit = emit

    async def __call__(self, frame):
        await self._emit(frame)
        append_display_frame(self.record, frame)
