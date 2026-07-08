"""Tests for chat_store — per-(host, session-id) persistence, against an isolated home.

Reuses the isolated_cache_home fixture (PINCHTAB_WEBGRAPH_HOME -> a tmp dir) so no test
touches a real ~/.pinchtab-webgraph, exactly like test_cache_store.py.
"""
import pytest

from pinchtab_webgraph.ui import chat_store


# --- id + path validation (path-traversal guard) -----------------------------

@pytest.mark.parametrize("bad", ["../evil", "a/b", "", "ABC", "g" * 32, "0" * 31,
                                 "0" * 33, "../../etc/passwd"])
def test_validate_session_id_rejects_unsafe(bad):
    with pytest.raises(ValueError):
        chat_store.validate_session_id(bad)


def test_new_session_id_is_valid():
    sid = chat_store.new_session_id()
    chat_store.validate_session_id(sid)  # must not raise
    assert len(sid) == 32


def test_session_path_rejects_bad_host(isolated_cache_home):
    with pytest.raises(ValueError):
        chat_store.session_path("../evil", chat_store.new_session_id())


@pytest.mark.parametrize("bad", [".", "..", "..."])
def test_host_sessions_dir_rejects_all_dots(isolated_cache_home, bad):
    # host_sessions_dir uses the BARE host as a directory segment (no ".json" suffix),
    # so an all-dots host like ".." would resolve to the parent dir and escape the
    # per-host quarantine. It must be rejected at the validate_host choke point.
    with pytest.raises(ValueError):
        chat_store.host_sessions_dir(bad)
    with pytest.raises(ValueError):
        chat_store.create(bad, backend="api")


def test_session_path_rejects_bad_id(isolated_cache_home):
    with pytest.raises(ValueError):
        chat_store.session_path("example.test", "../evil")


def test_session_path_shape(isolated_cache_home):
    sid = chat_store.new_session_id()
    p = chat_store.session_path("example.test", sid)
    assert p.endswith("/sessions/example.test/%s.json" % sid)
    assert str(isolated_cache_home) in p


# --- create / load / list ----------------------------------------------------

def test_create_persists_immediately_and_loads(isolated_cache_home):
    rec = chat_store.create("example.test", backend="api")
    assert rec["backend"] == "api"
    assert rec["wire_messages"] == []          # api backend seeds an empty wire list
    assert rec["title_locked"] is False
    loaded = chat_store.load("example.test", rec["id"])
    assert loaded is not None
    assert loaded["id"] == rec["id"]
    assert loaded["created_at"] == rec["created_at"]


def test_create_claude_code_has_null_wire_messages(isolated_cache_home):
    rec = chat_store.create("example.test", backend="claude_code")
    loaded = chat_store.load("example.test", rec["id"])
    assert loaded["wire_messages"] is None


def test_create_with_title_locks_it(isolated_cache_home):
    rec = chat_store.create("example.test", backend="api", title="My chat")
    assert rec["title"] == "My chat"
    assert rec["title_locked"] is True


def test_load_absent_returns_none(isolated_cache_home):
    assert chat_store.load("example.test", chat_store.new_session_id()) is None


def test_list_sessions_empty(isolated_cache_home):
    assert chat_store.list_sessions("example.test") == []


def test_list_sessions_sorted_updated_desc(isolated_cache_home):
    a = chat_store.create("example.test", backend="api")
    b = chat_store.create("example.test", backend="api")
    # bump b's updated_at so it sorts first
    b["title"] = None
    chat_store.append_display_frame(b, {"type": "user", "text": "hi"})
    chat_store.save(b)
    ids = [s["id"] for s in chat_store.list_sessions("example.test")]
    assert ids[0] == b["id"]
    assert set(ids) == {a["id"], b["id"]}


def test_summary_omits_heavy_fields(isolated_cache_home):
    rec = chat_store.create("example.test", backend="api")
    s = chat_store.summary(rec)
    assert set(s) == {"id", "host", "backend", "title", "created_at",
                      "updated_at", "message_count"}
    assert "transcript" not in s and "wire_messages" not in s
    assert "sdk_session_id" not in s


# --- too-many-sessions cap ---------------------------------------------------

def test_create_raises_at_cap(isolated_cache_home, monkeypatch):
    monkeypatch.setattr(chat_store, "MAX_SESSIONS_PER_HOST", 2)
    chat_store.create("example.test", backend="api")
    chat_store.create("example.test", backend="api")
    with pytest.raises(chat_store.TooManySessions):
        chat_store.create("example.test", backend="api")


# --- save: transcript merge + message_count ----------------------------------

def test_save_bumps_updated_and_message_count(isolated_cache_home):
    rec = chat_store.create("example.test", backend="api")
    chat_store.append_display_frame(rec, {"type": "user", "text": "hello"})
    chat_store.append_display_frame(rec, {"type": "text", "delta": "hi back"})
    chat_store.append_display_frame(rec, {"type": "done"})
    chat_store.save(rec)
    loaded = chat_store.load("example.test", rec["id"])
    assert loaded["message_count"] == 2       # user + assistant text
    assert loaded["updated_at"] >= loaded["created_at"]
    assert [e["type"] for e in loaded["transcript"]] == ["user", "text"]
    assert loaded["transcript"][1]["text"] == "hi back"


def test_save_appends_new_entries_without_clobbering_concurrent_writer(isolated_cache_home):
    # Two "connections" load the same session; each adds a turn and saves. The second
    # save must NOT drop the first's turn — save re-reads disk and appends only its own.
    rec = chat_store.create("example.test", backend="api")
    a = chat_store.load("example.test", rec["id"])
    b = chat_store.load("example.test", rec["id"])

    chat_store.append_display_frame(a, {"type": "user", "text": "from A"})
    chat_store.save(a)

    chat_store.append_display_frame(b, {"type": "user", "text": "from B"})
    chat_store.save(b)

    loaded = chat_store.load("example.test", rec["id"])
    texts = [e["text"] for e in loaded["transcript"] if e["type"] == "user"]
    assert "from A" in texts and "from B" in texts


def test_save_trims_transcript_to_max(isolated_cache_home, monkeypatch):
    monkeypatch.setattr(chat_store, "MAX_TRANSCRIPT_ENTRIES", 3)
    rec = chat_store.create("example.test", backend="api")
    for i in range(6):
        chat_store.append_display_frame(rec, {"type": "user", "text": "m%d" % i})
    chat_store.save(rec)
    loaded = chat_store.load("example.test", rec["id"])
    assert len(loaded["transcript"]) == 3
    assert [e["text"] for e in loaded["transcript"]] == ["m3", "m4", "m5"]  # tail kept


# --- append_display_frame: fold semantics ------------------------------------

def test_append_accumulates_text_deltas_into_one_entry():
    rec = {"transcript": []}
    for d in ["Hel", "lo ", "world"]:
        chat_store.append_display_frame(rec, {"type": "text", "delta": d})
    assert len(rec["transcript"]) == 1
    assert rec["transcript"][0]["text"] == "Hello world"
    assert rec["transcript"][0]["role"] == "assistant"


def test_append_tool_use_flushes_text_run():
    rec = {"transcript": []}
    chat_store.append_display_frame(rec, {"type": "text", "delta": "let me check"})
    chat_store.append_display_frame(rec, {"type": "tool_use", "name": "howto"})
    chat_store.append_display_frame(rec, {"type": "text", "delta": "here"})
    assert [e["type"] for e in rec["transcript"]] == ["text", "tool_use", "text"]
    assert rec["transcript"][0]["text"] == "let me check"
    assert rec["transcript"][2]["text"] == "here"       # a NEW entry after the tool


def test_append_tool_result_and_tour_and_error_entries():
    rec = {"transcript": []}
    chat_store.append_display_frame(rec, {"type": "tool_result", "name": "howto",
                                          "status": "ok"})
    chat_store.append_display_frame(rec, {"type": "tour", "data": {"goal": "x"}})
    chat_store.append_display_frame(rec, {"type": "error", "status": "chat_error",
                                          "detail": "boom"})
    kinds = [e["type"] for e in rec["transcript"]]
    assert kinds == ["tool_result", "tour", "error"]
    assert rec["transcript"][1]["data"] == {"goal": "x"}
    assert rec["transcript"][2]["detail"] == "boom"


def test_append_done_appends_nothing():
    rec = {"transcript": [{"role": "assistant", "type": "text", "text": "hi"}]}
    chat_store.append_display_frame(rec, {"type": "done"})
    assert len(rec["transcript"]) == 1


def test_append_user_autotitles_once():
    rec = {"transcript": [], "title": None, "title_locked": False}
    chat_store.append_display_frame(rec, {"type": "user", "text": "  how do I add a CAE?  "})
    assert rec["title"] == "how do I add a CAE?"
    # a second user message does NOT overwrite the title
    chat_store.append_display_frame(rec, {"type": "user", "text": "and a role?"})
    assert rec["title"] == "how do I add a CAE?"


def test_append_user_respects_locked_title():
    rec = {"transcript": [], "title": "Kept", "title_locked": True}
    chat_store.append_display_frame(rec, {"type": "user", "text": "something else"})
    assert rec["title"] == "Kept"


# --- rename / delete ---------------------------------------------------------

def test_rename_sets_and_locks_title(isolated_cache_home):
    rec = chat_store.create("example.test", backend="api")
    out = chat_store.rename("example.test", rec["id"], "Renamed")
    assert out["title"] == "Renamed"
    assert out["title_locked"] is True
    loaded = chat_store.load("example.test", rec["id"])
    assert loaded["title"] == "Renamed" and loaded["title_locked"] is True


def test_rename_absent_returns_none(isolated_cache_home):
    assert chat_store.rename("example.test", chat_store.new_session_id(), "x") is None


def test_delete_present_then_idempotent(isolated_cache_home):
    rec = chat_store.create("example.test", backend="api")
    assert chat_store.delete("example.test", rec["id"]) is True
    assert chat_store.delete("example.test", rec["id"]) is False
    assert chat_store.load("example.test", rec["id"]) is None


# --- TranscriptSink: mirrors emitted frames into the transcript --------------

def test_transcript_sink_emits_and_folds():
    import asyncio
    rec = {"transcript": []}
    sent = []

    async def emit(f):
        sent.append(f)

    sink = chat_store.TranscriptSink(rec, emit)

    async def go():
        await sink({"type": "text", "delta": "hi"})
        await sink({"type": "done"})

    asyncio.run(go())
    assert sent == [{"type": "text", "delta": "hi"}, {"type": "done"}]  # forwarded
    assert rec["transcript"][0]["text"] == "hi"                # AND folded in
    assert len(rec["transcript"]) == 1                         # done adds no entry


def test_no_secret_leak_in_persisted_view(isolated_cache_home):
    # The in-memory save baseline (_disk_len) must never reach disk.
    rec = chat_store.create("example.test", backend="api")
    chat_store.save(rec)
    import json
    with open(chat_store.session_path("example.test", rec["id"])) as f:
        raw = json.load(f)
    assert "_disk_len" not in raw
