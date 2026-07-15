"""Tests for flow_store — per-(host, flow-id) flow persistence + run history.

Reuses the isolated_cache_home fixture (PINCHTAB_WEBGRAPH_HOME -> a tmp dir) so no test
touches a real ~/.pinchtab-webgraph, exactly like test_chat_store.py / test_cache_store.py.
"""
import json
import os

import pytest

from pinchtab_webgraph import flow as flow_mod
from pinchtab_webgraph.ui import flow_store

HOST = "example.test"


def _doc(name="my-flow", **extra):
    doc = {"name": name, "steps": [{"op": "goto", "url": "https://example.test/x"}]}
    doc.update(extra)
    return doc


# --- id + path validation (path-traversal guard) -----------------------------

@pytest.mark.parametrize("bad", ["../evil", "a/b", "", "ABC", "g" * 32, "0" * 31,
                                 "0" * 33, "../../etc/passwd", None])
def test_validate_flow_id_rejects_unsafe(bad):
    with pytest.raises(ValueError):
        flow_store.validate_flow_id(bad)


@pytest.mark.parametrize("bad", ["../evil", "a/b", "", "ZZZ", "0" * 31])
def test_validate_run_id_rejects_unsafe(bad):
    with pytest.raises(ValueError):
        flow_store.validate_run_id(bad)


def test_new_ids_are_valid():
    flow_store.validate_flow_id(flow_store.new_flow_id())   # must not raise
    flow_store.validate_run_id(flow_store.new_run_id())


def test_flow_path_rejects_bad_host(isolated_cache_home):
    with pytest.raises(ValueError):
        flow_store.flow_path("../evil", flow_store.new_flow_id())


@pytest.mark.parametrize("bad", [".", "..", "..."])
def test_host_flows_dir_rejects_all_dots(isolated_cache_home, bad):
    # host_flows_dir uses the BARE host as a directory segment, so an all-dots host like ".."
    # would escape the per-host quarantine. Rejected at the validate_host choke point.
    with pytest.raises(ValueError):
        flow_store.host_flows_dir(bad)
    with pytest.raises(ValueError):
        flow_store.create(bad, _doc())


def test_runs_dir_and_run_path_validate_ids(isolated_cache_home):
    with pytest.raises(ValueError):
        flow_store.runs_dir(HOST, "../x")
    fid = flow_store.new_flow_id()
    with pytest.raises(ValueError):
        flow_store.run_path(HOST, fid, "../x")


# --- atomic_write -------------------------------------------------------------

def test_atomic_write_creates_parents_and_leaves_no_tmp(tmp_path):
    path = tmp_path / "a" / "b" / "rec.json"
    flow_store.atomic_write(str(path), {"x": 1})
    assert json.loads(path.read_text()) == {"x": 1}
    assert not (tmp_path / "a" / "b" / "rec.json.tmp").exists()
    # overwrite in place
    flow_store.atomic_write(str(path), {"x": 2})
    assert json.loads(path.read_text()) == {"x": 2}


# --- CRUD ---------------------------------------------------------------------

def test_create_validates_first_and_propagates_flow_error(isolated_cache_home):
    # An invalid doc must raise FlowError (the ROUTE decides invalid-vs-500, not the store),
    # and must NOT leave a record behind.
    with pytest.raises(flow_mod.FlowError):
        flow_store.create(HOST, {"name": "x"})            # no steps
    with pytest.raises(flow_mod.FlowError):
        flow_store.create(HOST, {"name": "x", "steps": [{"op": "nope"}]})
    assert flow_store.list_flows(HOST) == []


def test_create_load_round_trip(isolated_cache_home):
    rec = flow_store.create(HOST, _doc())
    flow_store.validate_flow_id(rec["id"])
    assert rec["host"] == HOST and rec["doc"]["name"] == "my-flow"
    assert os.path.exists(flow_store.flow_path(HOST, rec["id"]))

    loaded = flow_store.load(HOST, rec["id"])
    assert loaded["id"] == rec["id"] and loaded["doc"] == rec["doc"]
    assert loaded["created_at"] == rec["created_at"]


def test_load_absent_is_none(isolated_cache_home):
    assert flow_store.load(HOST, flow_store.new_flow_id()) is None


def test_summary_omits_the_doc_and_carries_the_run_form(isolated_cache_home):
    doc = _doc(inputs={"since": {"type": "string", "required": True}},
               capabilities={"allow_submit": True})
    doc["steps"] = [{"op": "do", "goal": "x", "submit": True}]
    rec = flow_store.create(HOST, doc)
    s = flow_store.summary(rec)
    assert "doc" not in s
    assert s["name"] == "my-flow" and s["steps"] == 1
    assert s["capabilities"]["allow_submit"] is True
    assert s["inputs"] == {"since": {"type": "string", "required": True}}
    assert s["run_count"] == 0


def test_list_flows_sorted_newest_first(isolated_cache_home):
    a = flow_store.create(HOST, _doc("a"))
    b = flow_store.create(HOST, _doc("b"))
    # force distinct updated_at ordering
    flow_store.atomic_write(flow_store.flow_path(HOST, a["id"]),
                            dict(flow_store.load(HOST, a["id"]),
                                 updated_at="2030-01-01T00:00:00Z"))
    names = [f["id"] for f in flow_store.list_flows(HOST)]
    assert names == [a["id"], b["id"]]


def test_list_flows_skips_a_corrupt_record(isolated_cache_home):
    good = flow_store.create(HOST, _doc("good"))
    bad = os.path.join(flow_store.host_flows_dir(HOST), "%s.json" % ("f" * 32))
    with open(bad, "w") as fh:
        fh.write("{ not json")
    listed = flow_store.list_flows(HOST)
    assert [f["id"] for f in listed] == [good["id"]]


def test_update_revalidates_and_preserves_identity(isolated_cache_home):
    rec = flow_store.create(HOST, _doc("v1"))
    updated = flow_store.update(HOST, rec["id"], _doc("v2"))
    assert updated["id"] == rec["id"]                       # id is NOT caller-supplied
    assert updated["created_at"] == rec["created_at"]       # created_at is preserved
    assert updated["doc"]["name"] == "v2"
    assert flow_store.load(HOST, rec["id"])["doc"]["name"] == "v2"

    with pytest.raises(flow_mod.FlowError):
        flow_store.update(HOST, rec["id"], {"name": "v3"})  # no steps
    assert flow_store.load(HOST, rec["id"])["doc"]["name"] == "v2"   # unchanged on reject


def test_update_absent_is_none(isolated_cache_home):
    assert flow_store.update(HOST, flow_store.new_flow_id(), _doc()) is None


def test_delete_is_idempotent_and_cascades_runs(isolated_cache_home):
    rec = flow_store.create(HOST, _doc())
    fid = rec["id"]
    rid = flow_store.new_run_id()
    flow_store.start_run(HOST, fid, rid, dry_run=False, capabilities={}, inputs={})
    assert os.path.exists(flow_store.run_path(HOST, fid, rid))

    assert flow_store.delete(HOST, fid) is True
    assert not os.path.exists(flow_store.flow_path(HOST, fid))
    # CASCADE: the run history went with the flow (no orphaned runs/ dir).
    assert not os.path.exists(os.path.join(flow_store.host_flows_dir(HOST), fid))
    assert flow_store.delete(HOST, fid) is False            # idempotent


# --- caps: flows HARD-REJECT, runs FIFO-EVICT --------------------------------

def test_flows_hard_reject_at_the_cap(isolated_cache_home, monkeypatch):
    monkeypatch.setattr(flow_store, "MAX_FLOWS_PER_HOST", 3)
    for i in range(3):
        flow_store.create(HOST, _doc("f%d" % i))
    with pytest.raises(flow_store.TooManyFlows):
        flow_store.create(HOST, _doc("one-too-many"))
    # NO silent eviction — every authored flow is still there.
    assert len(flow_store.list_flows(HOST)) == 3


def test_runs_fifo_evict_at_the_cap(isolated_cache_home, monkeypatch):
    # DELIBERATE DIVERGENCE from the flow cap: a run history is an audit trail of a REUSABLE
    # automation, so at the cap we drop the OLDEST run rather than refusing to run at all.
    monkeypatch.setattr(flow_store, "MAX_RUNS_PER_FLOW", 3)
    rec = flow_store.create(HOST, _doc())
    fid = rec["id"]
    ids = []
    for i in range(5):
        rid = flow_store.new_run_id()
        ids.append(rid)
        flow_store.start_run(HOST, fid, rid, dry_run=True, capabilities={}, inputs={})
        # started_at is written by start_run at ~the same instant; force a distinct order.
        path = flow_store.run_path(HOST, fid, rid)
        flow_store.atomic_write(path, dict(flow_store.load_run(HOST, fid, rid),
                                           started_at="2026-01-0%d T00:00:00Z" % (i + 1)))
        flow_store.finish_run(HOST, fid, rid, {"status": "ok", "steps": []})

    kept = {r["id"] for r in flow_store.list_runs(HOST, fid)}
    assert kept == set(ids[2:])                    # the two OLDEST were evicted
    assert len(kept) == 3
    # the run we just finished is never the one evicted to make room for itself.
    assert ids[-1] in kept


# --- run lifecycle ------------------------------------------------------------

def test_start_run_persists_a_running_placeholder(isolated_cache_home):
    rec = flow_store.create(HOST, _doc())
    fid, rid = rec["id"], flow_store.new_run_id()
    started = flow_store.start_run(HOST, fid, rid, dry_run=True,
                                   capabilities={"allow_download": True},
                                   inputs={"since": "2026-01-01"})
    assert started["status"] == "running" and started["finished_at"] is None
    # DISCOVERABLE even if the process now crashes: the record is already on disk.
    on_disk = flow_store.load_run(HOST, fid, rid)
    assert on_disk["status"] == "running"
    assert on_disk["dry_run"] is True
    assert on_disk["inputs"] == {"since": "2026-01-01"}
    assert flow_store.summary(flow_store.load(HOST, fid))["run_count"] == 1


def test_finish_run_folds_the_result_and_trims_steps(isolated_cache_home, monkeypatch):
    monkeypatch.setattr(flow_store, "MAX_RUN_LOG_ENTRIES", 5)
    rec = flow_store.create(HOST, _doc())
    fid, rid = rec["id"], flow_store.new_run_id()
    flow_store.start_run(HOST, fid, rid, dry_run=False, capabilities={}, inputs={})

    result = {"status": "ok", "dry_run": False, "duration_s": 1.5, "aborted": None,
              "steps": [{"op": "log", "status": "ok", "i": i} for i in range(20)],
              "artifacts": [{"status": "new", "sha256": "abc"}],
              "collected": {"rows": [1, 2]},
              "stats": {"steps_executed": 20, "artifacts_new": 1, "artifacts_dupe": 0}}
    done = flow_store.finish_run(HOST, fid, rid, result)
    assert done["status"] == "ok" and done["finished_at"]
    assert done["cancelled"] is False
    assert len(done["steps"]) == 5                       # trimmed to the TRAILING N
    assert done["steps"][-1]["i"] == 19
    assert done["artifacts"] == [{"status": "new", "sha256": "abc"}]
    assert done["collected"] == {"rows": [1, 2]}

    reloaded = flow_store.load_run(HOST, fid, rid)
    assert reloaded["stats"]["steps_executed"] == 20


def test_finish_run_records_cancellation(isolated_cache_home):
    rec = flow_store.create(HOST, _doc())
    fid, rid = rec["id"], flow_store.new_run_id()
    flow_store.start_run(HOST, fid, rid, dry_run=False, capabilities={}, inputs={})
    done = flow_store.finish_run(HOST, fid, rid,
                                 {"status": "error", "detail": "no result (rc=-15)",
                                  "steps": [{"op": "goto", "status": "ok"}]},
                                 cancelled=True)
    assert done["cancelled"] is True and done["status"] == "error"
    assert done["detail"] == "no result (rc=-15)"
    # the partial trail survives a hard kill.
    assert len(flow_store.load_run(HOST, fid, rid)["steps"]) == 1


def test_list_runs_summaries_omit_heavy_payloads(isolated_cache_home):
    rec = flow_store.create(HOST, _doc())
    fid, rid = rec["id"], flow_store.new_run_id()
    flow_store.start_run(HOST, fid, rid, dry_run=False, capabilities={}, inputs={})
    flow_store.finish_run(HOST, fid, rid,
                          {"status": "ok", "steps": [{"op": "log", "status": "ok"}],
                           "artifacts": [{"status": "new"}], "collected": {"a": 1},
                           "stats": {"steps_executed": 1}})
    runs = flow_store.list_runs(HOST, fid)
    assert len(runs) == 1
    r = runs[0]
    assert r["id"] == rid and r["status"] == "ok"
    for heavy in ("steps", "artifacts", "collected"):
        assert heavy not in r
    assert r["stats"]["steps_executed"] == 1
    # the FULL record still has them.
    assert flow_store.load_run(HOST, fid, rid)["steps"]


def test_load_run_absent_is_none(isolated_cache_home):
    rec = flow_store.create(HOST, _doc())
    assert flow_store.load_run(HOST, rec["id"], flow_store.new_run_id()) is None


def test_list_runs_empty_for_a_flow_with_no_runs(isolated_cache_home):
    rec = flow_store.create(HOST, _doc())
    assert flow_store.list_runs(HOST, rec["id"]) == []
