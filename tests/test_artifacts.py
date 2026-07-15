"""Tests for artifacts.py — the content-addressed store + persistent dedupe ledger.

The load-bearing contract is dedupe ACROSS RUNS: a second ArtifactStore on the same
root/scope must recognise bytes the first one accepted, or "poll every 10s" re-downloads the
same PDF forever. That is the ledger-persistence test.
"""
import json
import os

import pytest

from pinchtab_webgraph import artifacts


def _store(tmp_path, scope="s"):
    return artifacts.ArtifactStore(scope=scope, root=str(tmp_path / scope))


def _stage(store, name, data):
    path = store.staging_path(name)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


# --- accept: new vs dupe -----------------------------------------------------

def test_accept_new_stores_the_bytes_content_addressed(tmp_path):
    st = _store(tmp_path)
    rec = st.accept(_stage(st, "q3.pdf", b"hello"), name="q3.pdf", source="http://x/q3.pdf")
    assert rec["status"] == "new"
    assert rec["size"] == 5
    assert rec["source"] == "http://x/q3.pdf"
    assert os.path.basename(rec["path"]) == rec["sha256"] + ".pdf"   # hashed, ext preserved
    assert open(rec["path"], "rb").read() == b"hello"


def test_accept_same_bytes_twice_is_a_dupe(tmp_path):
    st = _store(tmp_path)
    first = st.accept(_stage(st, "a.pdf", b"same"), name="a.pdf")
    staged = _stage(st, "b.pdf", b"same")
    second = st.accept(staged, name="b.pdf")
    assert second["status"] == "dupe"
    assert second["sha256"] == first["sha256"]
    assert second["path"] == first["path"]        # points at the ALREADY-stored bytes
    assert not os.path.exists(staged)             # the redundant staged copy is gone


def test_different_bytes_same_name_are_both_kept(tmp_path):
    # the silent-corruption case: a site serving ten different files all called export.pdf
    st = _store(tmp_path)
    a = st.accept(_stage(st, "export.pdf", b"one"), name="export.pdf")
    b = st.accept(_stage(st, "export.pdf", b"two"), name="export.pdf")
    assert a["status"] == b["status"] == "new"
    assert a["path"] != b["path"]


def test_dedupe_off_readmits_the_same_bytes(tmp_path):
    st = _store(tmp_path)
    st.accept(_stage(st, "a.pdf", b"x"), name="a.pdf")
    again = st.accept(_stage(st, "a.pdf", b"x"), name="a.pdf", dedupe=False)
    assert again["status"] == "new"


def test_has_and_stats(tmp_path):
    st = _store(tmp_path)
    rec = st.accept(_stage(st, "a.pdf", b"abc"), name="a.pdf")
    assert st.has(rec["sha256"])
    assert st.stats() == {"scope": "s", "root": st.root, "count": 1, "bytes": 3}


# --- the dedupe-across-runs contract -----------------------------------------

def test_ledger_persists_across_a_second_store_instance(tmp_path):
    first = _store(tmp_path)
    rec = first.accept(_stage(first, "a.pdf", b"payload"), name="a.pdf")

    second = artifacts.ArtifactStore(scope="s", root=first.root)   # a LATER run
    assert second.has(rec["sha256"])
    dupe = second.accept(_stage(second, "a.pdf", b"payload"), name="a.pdf")
    assert dupe["status"] == "dupe"
    assert dupe["first_seen"] == rec["seen_at"]

    # and genuinely NEW bytes in the later run are still admitted
    assert second.accept(_stage(second, "b.pdf", b"other"), name="b.pdf")["status"] == "new"


def test_a_separate_scope_has_its_own_ledger(tmp_path):
    a = _store(tmp_path, "alpha")
    rec = a.accept(_stage(a, "x.pdf", b"shared"), name="x.pdf")
    b = _store(tmp_path, "beta")
    assert not b.has(rec["sha256"])
    # same bytes → `new` for the other scope, and the stored file is not overwritten
    assert b.accept(_stage(b, "x.pdf", b"shared"), name="x.pdf")["status"] == "new"


def test_a_torn_ledger_line_is_skipped_not_fatal(tmp_path):
    st = _store(tmp_path)
    rec = st.accept(_stage(st, "a.pdf", b"x"), name="a.pdf")
    with open(st.ledger_path, "a") as fh:
        fh.write('{"sha256": "half\n\n')          # a run killed mid-write
    reopened = artifacts.ArtifactStore(scope="s", root=st.root)
    assert reopened.has(rec["sha256"])


def test_ledger_is_append_only_jsonl(tmp_path):
    st = _store(tmp_path)
    st.accept(_stage(st, "a.pdf", b"1"), name="a.pdf")
    st.accept(_stage(st, "b.pdf", b"2"), name="b.pdf")
    lines = [json.loads(x) for x in open(st.ledger_path) if x.strip()]
    assert [r["name"] for r in lines] == ["a.pdf", "b.pdf"]


# --- scope validation (same class of bug as cache_store.validate_host) --------

@pytest.mark.parametrize("bad", ["..", ".", "../../etc", "a/b", "  ", "a b", "x/../y"])
def test_validate_scope_rejects_traversal_and_junk(bad):
    with pytest.raises(ValueError, match="invalid artifact scope"):
        artifacts.validate_scope(bad)


def test_validate_scope_accepts_a_flow_name():
    assert artifacts.validate_scope("download-all_invoices.v2") == "download-all_invoices.v2"


def test_validate_scope_defaults():
    assert artifacts.validate_scope(None) == "default"


def test_store_rejects_a_traversing_scope(tmp_path):
    with pytest.raises(ValueError):
        artifacts.ArtifactStore(scope="../../etc", root=str(tmp_path))


# --- home_dir ----------------------------------------------------------------

def test_home_dir_expands_a_tilde_in_the_env_var(monkeypatch):
    # regression: an env-var path was NOT expanduser'd, so PINCHTAB_WEBGRAPH_HOME=~/x made a
    # directory literally named `~`. Must match cache_store.home_dir() exactly.
    monkeypatch.setenv("PINCHTAB_WEBGRAPH_HOME", "~/somewhere")
    assert artifacts.home_dir() == os.path.expanduser("~/somewhere")
    assert "~" not in artifacts.artifacts_dir()


def test_home_dir_default(monkeypatch):
    monkeypatch.delenv("PINCHTAB_WEBGRAPH_HOME", raising=False)
    assert artifacts.home_dir() == os.path.expanduser("~/.pinchtab-webgraph")


def test_store_root_defaults_under_the_home(monkeypatch, tmp_path):
    monkeypatch.setenv("PINCHTAB_WEBGRAPH_HOME", str(tmp_path))
    st = artifacts.ArtifactStore(scope="flowname")
    assert st.root == str(tmp_path / "artifacts" / "flowname")
    assert os.path.isdir(st.files_dir)
