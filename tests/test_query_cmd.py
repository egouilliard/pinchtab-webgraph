"""Tests for pinchtab_webgraph.query_cmd — the JSON-printing `query` subcommand.

query_cmd.main() reads sys.argv via argparse and prints json.dumps(result) to
stdout. Each op is driven by monkeypatching sys.argv and capturing stdout; the
parsed JSON is asserted against the SAME expected values test_api.py asserts, and
the exit-code convention is checked (0 for api-level results incl. structured
misses, 1 for resolver/env errors with JSON STILL on stdout, 2 for argparse usage
errors). Reuses the shared graph fixtures + populated_cache_home.
"""
import json

import pytest

from pinchtab_webgraph import query_cmd

HOST = "example.test"


def _run(monkeypatch, capsys, argv):
    """Run query_cmd.main() with argv (sans prog); return (exit_code, parsed_json | None)."""
    monkeypatch.setattr("sys.argv", ["pwg query"] + argv)
    code = query_cmd.main()
    out = capsys.readouterr().out
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError:
        parsed = None
    return code, parsed


# --- graph_summary -----------------------------------------------------------

def test_graph_summary_by_graph(monkeypatch, capsys, sample_interaction_graph_path):
    code, out = _run(monkeypatch, capsys,
                     ["graph_summary", "--graph", str(sample_interaction_graph_path)])
    assert code == 0
    assert out["graph_kind"] == "interaction"
    assert out["states"] == 5
    assert out["edges"] == 3
    assert out["triggers"] == 3
    assert out["meta"]["host"] == HOST


def test_graph_summary_by_host(monkeypatch, capsys, populated_cache_home):
    # --host routing must yield the SAME result as --graph on the same fixture.
    code, out = _run(monkeypatch, capsys, ["graph_summary", "--host", HOST])
    assert code == 0
    assert out["graph_kind"] == "interaction"
    assert out["states"] == 5
    assert out["triggers"] == 3


# --- howto -------------------------------------------------------------------

def test_howto_ok(monkeypatch, capsys, sample_interaction_graph_path):
    code, out = _run(monkeypatch, capsys,
                     ["howto", "--graph", str(sample_interaction_graph_path),
                      "--goal", "create role"])
    assert code == 0
    assert out["status"] == "ok"
    r = out["results"][0]
    assert r["trigger_label"] == "Create Role"
    assert r["clicks"] == 3
    assert r["form"]["fieldCount"] == 1


def test_howto_ok_by_host(monkeypatch, capsys, populated_cache_home):
    code, out = _run(monkeypatch, capsys,
                     ["howto", "--host", HOST, "--goal", "create role"])
    assert code == 0
    assert out["results"][0]["trigger_label"] == "Create Role"


def test_howto_match_regex(monkeypatch, capsys, sample_interaction_graph_path):
    code, out = _run(monkeypatch, capsys,
                     ["howto", "--graph", str(sample_interaction_graph_path),
                      "--match", "Report"])
    assert code == 0
    assert out["status"] == "ok"
    assert out["results"][0]["trigger_label"] == "Add Report"


def test_howto_unreachable_is_exit_0(monkeypatch, capsys, sample_interaction_graph_path):
    # a structured miss is exit 0 with JSON on stdout — it is NOT an error.
    code, out = _run(monkeypatch, capsys,
                     ["howto", "--graph", str(sample_interaction_graph_path),
                      "--goal", "add widget"])
    assert code == 0
    assert out["status"] == "unreachable"
    assert "Add Widget" in out["candidates"]


def test_howto_invalid_args_is_exit_0(monkeypatch, capsys, sample_interaction_graph_path):
    # neither --goal nor --match: api-level invalid_args (NOT a resolver error) -> exit 0.
    code, out = _run(monkeypatch, capsys,
                     ["howto", "--graph", str(sample_interaction_graph_path)])
    assert code == 0
    assert out["status"] == "invalid_args"


def test_all_flag_accepted(monkeypatch, capsys, sample_interaction_graph_path):
    code, out = _run(monkeypatch, capsys,
                     ["howto", "--graph", str(sample_interaction_graph_path),
                      "--goal", "create role", "--all"])
    assert code == 0
    assert out["status"] == "ok"


# --- find_content ------------------------------------------------------------

def test_find_content_hit(monkeypatch, capsys, sample_interaction_graph_path):
    code, out = _run(monkeypatch, capsys,
                     ["find_content", "--graph", str(sample_interaction_graph_path),
                      "--text", "Alice"])
    assert code == 0
    assert out["status"] == "ok"
    assert out["total_matches"] == 1
    assert out["views"][0]["items"][0]["text"] == "Alice Martin"


def test_find_content_miss_is_exit_0(monkeypatch, capsys, sample_interaction_graph_path):
    code, out = _run(monkeypatch, capsys,
                     ["find_content", "--graph", str(sample_interaction_graph_path),
                      "--text", "nothinghere"])
    assert code == 0
    assert out["status"] == "no_match"
    assert out["views"] == []


def test_find_content_missing_text_is_exit_2(monkeypatch, capsys,
                                             sample_interaction_graph_path):
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, capsys,
             ["find_content", "--graph", str(sample_interaction_graph_path)])
    assert exc.value.code == 2


# --- list_content ------------------------------------------------------------

def test_list_content(monkeypatch, capsys, sample_interaction_graph_path):
    code, out = _run(monkeypatch, capsys,
                     ["list_content", "--graph", str(sample_interaction_graph_path)])
    assert code == 0
    assert out["status"] == "ok"
    assert {v["view_label"] for v in out["views"]} == {"Team", "Reports"}


def test_list_content_empty(monkeypatch, capsys, sample_link_graph_path):
    code, out = _run(monkeypatch, capsys,
                     ["list_content", "--graph", str(sample_link_graph_path)])
    assert code == 0
    assert out["status"] == "empty"


# --- find_content_hosts / list_content_hosts (cross-host, all cached hosts) --

def test_find_content_hosts(monkeypatch, capsys, two_hosts_cache_home):
    code, out = _run(monkeypatch, capsys, ["find_content_hosts", "--text", "alice"])
    assert code == 0
    assert out["status"] == "ok"
    assert set(out["hosts_matched"]) == {"example.test", "shop.test"}
    assert all("host" in v for v in out["views"])


def test_find_content_hosts_missing_text_is_exit_2(monkeypatch, capsys,
                                                   two_hosts_cache_home):
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, capsys, ["find_content_hosts"])
    assert exc.value.code == 2


def test_list_content_hosts(monkeypatch, capsys, two_hosts_cache_home):
    code, out = _run(monkeypatch, capsys, ["list_content_hosts"])
    assert code == 0
    assert out["status"] == "ok"
    assert set(out["hosts_with_content"]) == {"example.test", "shop.test"}


# --- list_forms --------------------------------------------------------------

def test_list_forms_keeps_no_status_shape(monkeypatch, capsys,
                                          sample_interaction_graph_path):
    code, out = _run(monkeypatch, capsys,
                     ["list_forms", "--graph", str(sample_interaction_graph_path)])
    assert code == 0
    # list_forms is the one op with a {meta, forms} shape and NO status key.
    assert "status" not in out
    assert out["meta"]["host"] == HOST
    assert out["meta"]["triggers"] == 3
    assert {f["label"] for f in out["forms"]} == {"Create Role", "Add Report", "Add Widget"}


def test_list_forms_by_host(monkeypatch, capsys, populated_cache_home):
    code, out = _run(monkeypatch, capsys, ["list_forms", "--host", HOST])
    assert code == 0
    assert out["meta"]["host"] == HOST


# --- link_paths --------------------------------------------------------------

def test_link_paths_shortest(monkeypatch, capsys, sample_link_graph_path):
    code, out = _run(monkeypatch, capsys,
                     ["link_paths", "--graph", str(sample_link_graph_path),
                      "--from", "home", "--to", "guide"])
    assert code == 0
    assert out["status"] == "ok"
    assert out["shortest"]["clicks"] == 1
    assert out["from"]["id"] == "home"
    assert out["to"]["id"] == "guide"


def test_link_paths_all(monkeypatch, capsys, sample_link_graph_path):
    code, out = _run(monkeypatch, capsys,
                     ["link_paths", "--graph", str(sample_link_graph_path),
                      "--from", "home", "--to", "guide", "--all"])
    assert code == 0
    assert sorted(p["clicks"] for p in out["all_paths"]) == [1, 2]


def test_link_paths_ambiguous_is_exit_0(monkeypatch, capsys, sample_link_graph_path):
    code, out = _run(monkeypatch, capsys,
                     ["link_paths", "--graph", str(sample_link_graph_path),
                      "--from", "home", "--to", "port"])
    assert code == 0
    assert out["status"] == "ambiguous_to"
    assert len(out["candidates"]) == 2


def test_link_paths_missing_from_is_exit_2(monkeypatch, capsys, sample_link_graph_path):
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, capsys,
             ["link_paths", "--graph", str(sample_link_graph_path), "--to", "guide"])
    assert exc.value.code == 2


def test_link_paths_missing_to_is_exit_2(monkeypatch, capsys, sample_link_graph_path):
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, capsys,
             ["link_paths", "--graph", str(sample_link_graph_path), "--from", "home"])
    assert exc.value.code == 2


# --- resolver / env errors -> exit 1 (JSON STILL on stdout) ------------------

def test_no_cache_for_host_is_exit_1(monkeypatch, capsys, isolated_cache_home):
    code, out = _run(monkeypatch, capsys, ["graph_summary", "--host", "never-crawled.test"])
    assert code == 1
    assert out["status"] == "no_cache_for_host"      # JSON printed to stdout, not stderr


def test_invalid_host_is_exit_1(monkeypatch, capsys, isolated_cache_home):
    code, out = _run(monkeypatch, capsys, ["graph_summary", "--host", "../../etc/passwd"])
    assert code == 1
    assert out["status"] == "invalid_host"


def test_invalid_graph_is_exit_1(monkeypatch, capsys, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json")
    code, out = _run(monkeypatch, capsys, ["graph_summary", "--graph", str(bad)])
    assert code == 1
    assert out["status"] == "invalid_graph"


# --- argparse usage errors -> exit 2 -----------------------------------------

def test_neither_host_nor_graph_is_exit_2(monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, capsys, ["graph_summary"])
    assert exc.value.code == 2


def test_both_host_and_graph_is_exit_2(monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, capsys, ["graph_summary", "--host", "x", "--graph", "y"])
    assert exc.value.code == 2


# --- --json is an accepted no-op ---------------------------------------------

def test_json_flag_is_accepted_noop(monkeypatch, capsys, sample_interaction_graph_path):
    code, out = _run(monkeypatch, capsys,
                     ["graph_summary", "--graph", str(sample_interaction_graph_path),
                      "--json"])
    assert code == 0
    assert out["graph_kind"] == "interaction"
