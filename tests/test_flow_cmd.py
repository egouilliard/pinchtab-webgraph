"""Tests for flow_cmd.py — the `pwg flow validate|schema|run` subcommand.

Driven the way test_query_cmd.py drives its CLI: monkeypatched sys.argv + capsys, asserting
the printed JSON and the exit-code convention (0 ok · 1 rejected/errored · 2 usage/env). The
browser and the tab resolver are monkeypatched at the module boundary, so `run` exercises the
real runner + a real ArtifactStore without a bridge.
"""
import json
import os

import pytest

from pinchtab_webgraph import flow_cmd

from .test_runner import FakeBrowser        # the same full-port fake the VM tests use


def _write(tmp_path, doc, name="f.json"):
    p = tmp_path / name
    p.write_text(json.dumps(doc))
    return str(p)


def _cli(monkeypatch, capsys, argv):
    monkeypatch.setattr("sys.argv", ["pwg flow"] + argv)
    code = flow_cmd.main()
    out = capsys.readouterr()
    try:
        parsed = json.loads(out.out)
    except json.JSONDecodeError:
        parsed = None
    return code, parsed, out.out, out.err


@pytest.fixture
def fake_browser(monkeypatch):
    """Swap the live PinchTabBrowser for the runner's FakeBrowser."""
    made = {}

    def make(server, token, tab):
        made["args"] = (server, token, tab)
        fb = FakeBrowser(query_results=[[{"selector": "#a", "text": "A", "kind": "download",
                                          "href": "https://app.test/a.pdf"}]])
        made["browser"] = fb
        return fb

    monkeypatch.setattr(flow_cmd.browser_mod, "PinchTabBrowser", make)
    def resolve(server, token, url=None):
        made["resolve_url"] = url            # the flow's first literal goto url, if any
        return "tab1"

    monkeypatch.setattr(flow_cmd.browser_mod, "resolve_tab", resolve)
    monkeypatch.setattr(flow_cmd.perform, "load_token", lambda cfg: "TOK")
    return made


# --- validate ------------------------------------------------------------------

def test_validate_ok(monkeypatch, capsys, tmp_path):
    path = _write(tmp_path, {"name": "invoices", "host": "app.test",
                             "inputs": {"since": {"type": "string"}},
                             "capabilities": {"allow_submit": True},
                             "steps": [{"op": "goto", "url": "https://app.test/"}]})
    code, out, _, _ = _cli(monkeypatch, capsys, ["validate", path])
    assert code == 0
    assert out["status"] == "ok"
    assert out["name"] == "invoices"
    assert out["capabilities"]["allow_submit"] is True
    assert out["inputs"] == ["since"]


def test_validate_invalid_is_exit_1_with_json(monkeypatch, capsys, tmp_path):
    path = _write(tmp_path, {"name": "f", "steps": [{"op": "click"}]})
    code, out, _, _ = _cli(monkeypatch, capsys, ["validate", path])
    assert code == 1
    assert out["status"] == "invalid"
    assert out["path"] == "steps[0]"
    assert "requires one of" in out["error"]


def test_validate_missing_file_is_exit_1(monkeypatch, capsys, tmp_path):
    code, out, _, _ = _cli(monkeypatch, capsys,
                           ["validate", str(tmp_path / "nope.json")])
    assert code == 1
    assert out["status"] == "invalid"


def test_validate_bad_json_is_exit_1(monkeypatch, capsys, tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{ nope")
    code, out, _, _ = _cli(monkeypatch, capsys, ["validate", str(p)])
    assert code == 1
    assert "not valid JSON" in out["error"]


# --- schema --------------------------------------------------------------------

def test_schema_prints_a_json_schema(monkeypatch, capsys, tmp_path):
    path = _write(tmp_path, {"name": "f",
                             "inputs": {"since": {"type": "string", "required": True},
                                        "limit": {"type": "integer", "default": 10}},
                             "steps": [{"op": "log", "message": "x"}]})
    code, out, _, _ = _cli(monkeypatch, capsys, ["schema", path])
    assert code == 0
    assert out == {"type": "object", "additionalProperties": False,
                   "properties": {"since": {"type": "string"},
                                  "limit": {"type": "integer", "default": 10}},
                   "required": ["since"]}


def test_schema_of_an_invalid_flow_is_exit_1(monkeypatch, capsys, tmp_path):
    path = _write(tmp_path, {"name": "f", "steps": [{"op": "bogus"}]})
    code, out, _, _ = _cli(monkeypatch, capsys, ["schema", path])
    assert code == 1
    assert out["status"] == "invalid"


# --- run -----------------------------------------------------------------------

_DL_FLOW = {"name": "dl-flow", "steps": [
    {"op": "goto", "url": "https://app.test/invoices"},
    {"op": "for_each", "match": {"kind": "download"},
     "body": [{"op": "download", "href": "${item.href}", "name": "${item.text}.pdf"}]},
]}


def test_run_downloads_and_summarizes(monkeypatch, capsys, tmp_path, fake_browser):
    path = _write(tmp_path, _DL_FLOW)
    code, _, out, _ = _cli(monkeypatch, capsys,
                           ["run", path, "--artifacts-root", str(tmp_path / "art")])
    assert code == 0
    assert "=== FLOW: DL-FLOW ===" in out
    assert "✓ goto" in out and "download" in out
    assert "1 new file(s), 0 duplicate(s)" in out
    # the scope defaults to the FLOW NAME — that is where artifacts.py's promise is kept
    assert os.path.isdir(str(tmp_path / "art" / "dl-flow"))


def test_run_hands_the_first_goto_url_to_resolve_tab(monkeypatch, capsys, tmp_path,
                                                     fake_browser):
    # a fresh bridge has NO tab to adopt, and the only way to make one is to nav a REAL url —
    # so the flow's first literal goto url is passed along. Generic: no site knowledge.
    path = _write(tmp_path, _DL_FLOW)
    _cli(monkeypatch, capsys, ["run", path, "--artifacts-root", str(tmp_path / "art")])
    assert fake_browser["resolve_url"] == "https://app.test/invoices"


def test_run_passes_no_url_when_the_flow_does_not_open_with_a_literal_goto(
        monkeypatch, capsys, tmp_path, fake_browser):
    # a goal-based goto needs the graph and a templated url isn't interpolated yet — None is
    # the honest answer, and it is safe: browser.nav() self-heals a missing tab.
    path = _write(tmp_path, {"name": "f", "host": "app.test", "steps": [
        {"op": "goto", "goal": "invoices"}]})
    _cli(monkeypatch, capsys, ["run", path, "--artifacts-root", str(tmp_path / "art")])
    assert fake_browser["resolve_url"] is None


def test_run_json_prints_the_full_record(monkeypatch, capsys, tmp_path, fake_browser):
    path = _write(tmp_path, _DL_FLOW)
    code, out, _, _ = _cli(monkeypatch, capsys,
                           ["run", path, "--json", "--artifacts-root", str(tmp_path / "art")])
    assert code == 0
    assert out["status"] == "ok"
    assert out["stats"]["artifacts_new"] == 1
    assert out["artifacts"][0]["name"] == "A.pdf"


def test_run_dedupes_across_runs(monkeypatch, capsys, tmp_path, fake_browser):
    # the polling contract: run N must know what run N-1 already fetched (a fresh process,
    # a fresh store — only the on-disk ledger carries the knowledge).
    path = _write(tmp_path, _DL_FLOW)
    argv = ["run", path, "--json", "--artifacts-root", str(tmp_path / "art")]
    _cli(monkeypatch, capsys, argv)
    code, out, _, _ = _cli(monkeypatch, capsys, argv)
    assert code == 0
    assert out["stats"]["artifacts_new"] == 0
    assert out["stats"]["artifacts_dupe"] == 1


def test_run_explicit_scope_is_used(monkeypatch, capsys, tmp_path, fake_browser):
    path = _write(tmp_path, _DL_FLOW)
    _cli(monkeypatch, capsys, ["run", path, "--scope", "mine",
                               "--artifacts-root", str(tmp_path / "art")])
    assert os.path.isdir(str(tmp_path / "art" / "mine"))


def test_run_rejects_a_traversing_scope_with_exit_2(monkeypatch, capsys, tmp_path,
                                                    fake_browser):
    path = _write(tmp_path, _DL_FLOW)
    code, _, _, err = _cli(monkeypatch, capsys, ["run", path, "--scope", "../../etc"])
    assert code == 2
    assert "invalid artifact scope" in err


def test_run_dry_run_touches_nothing(monkeypatch, capsys, tmp_path, fake_browser):
    path = _write(tmp_path, _DL_FLOW)
    code, _, out, _ = _cli(monkeypatch, capsys,
                           ["run", path, "--dry-run",
                            "--artifacts-root", str(tmp_path / "art")])
    assert code == 0
    assert "WOULD run" in out
    assert fake_browser["browser"].calls == []
    assert not os.path.exists(str(tmp_path / "art"))     # no artifact dir was created
    assert fake_browser["args"][2] is None                # …and no tab was even resolved


def test_run_inputs_are_parsed_and_bound(monkeypatch, capsys, tmp_path, fake_browser):
    path = _write(tmp_path, {"name": "f", "inputs": {"n": {"type": "integer"}},
                             "steps": [{"op": "log", "message": "n=${n}"}]})
    code, out, _, _ = _cli(monkeypatch, capsys,
                           ["run", path, "--json", "--input", "n=7",
                            "--artifacts-root", str(tmp_path / "art")])
    assert code == 0
    assert [e for e in out["steps"] if e["op"] == "log"][0]["message"] == "n=7"


def test_run_unknown_input_is_exit_1(monkeypatch, capsys, tmp_path, fake_browser):
    path = _write(tmp_path, {"name": "f", "steps": [{"op": "log", "message": "x"}]})
    code, out, _, _ = _cli(monkeypatch, capsys, ["run", path, "--input", "nope=1"])
    assert code == 1
    assert "unknown input" in out["error"]


def test_run_file_input_binds_an_existing_path(monkeypatch, capsys, tmp_path, fake_browser):
    # `--input file=/abs/path` keeps working exactly as it did — a file input is still a
    # NAME=VALUE string on the CLI; only its validation is new.
    doc = {"name": "f", "inputs": {"file": {"type": "file", "required": True}},
           "steps": [{"op": "log", "message": "f=${file}"}]}
    path = _write(tmp_path, doc)
    real = tmp_path / "invoice.pdf"
    real.write_bytes(b"%PDF-1.4")
    code, out, _, _ = _cli(monkeypatch, capsys,
                           ["run", path, "--json", "--input", "file=%s" % real,
                            "--artifacts-root", str(tmp_path / "art")])
    assert code == 0
    assert [e for e in out["steps"] if e["op"] == "log"][0]["message"] == "f=%s" % real


def test_run_missing_file_input_is_a_clean_exit_1(monkeypatch, capsys, tmp_path, fake_browser):
    # A clean rejection — the printed JSON, exit 1 — NOT a traceback out of the runner.
    doc = {"name": "f", "inputs": {"file": {"type": "file", "required": True}},
           "steps": [{"op": "log", "message": "x"}]}
    path = _write(tmp_path, doc)
    missing = str(tmp_path / "gone.pdf")
    code, out, _, err = _cli(monkeypatch, capsys,
                             ["run", path, "--input", "file=%s" % missing])
    assert code == 1
    assert out["status"] == "invalid"
    assert "no such file" in out["error"] and missing in out["error"]
    assert "Traceback" not in err


def test_run_capability_flags_are_wired_into_the_grant(monkeypatch, capsys, tmp_path,
                                                       fake_browser):
    doc = {"name": "f", "capabilities": {"allow_upload": True},
           "steps": [{"op": "upload", "selector": "#f", "file": "/x.pdf"}]}
    path = _write(tmp_path, doc)
    root = ["--artifacts-root", str(tmp_path / "art")]
    # the flow declares it, but the caller did not grant it -> skipped
    code, out, _, _ = _cli(monkeypatch, capsys, ["run", path, "--json"] + root)
    assert code == 0
    assert [e for e in out["steps"] if e["op"] == "upload"][0]["status"] == "skipped"
    # with --allow-upload it runs
    code, out, _, _ = _cli(monkeypatch, capsys,
                           ["run", path, "--json", "--allow-upload"] + root)
    assert [e for e in out["steps"] if e["op"] == "upload"][0]["status"] == "ok"
    assert ("upload", "#f", "/x.pdf") in fake_browser["browser"].calls


def test_run_no_allow_download_withdraws_the_default_capability(monkeypatch, capsys,
                                                                tmp_path, fake_browser):
    path = _write(tmp_path, _DL_FLOW)
    code, out, _, _ = _cli(monkeypatch, capsys,
                           ["run", path, "--json", "--no-allow-download",
                            "--artifacts-root", str(tmp_path / "art")])
    assert code == 0
    assert out["stats"]["artifacts_new"] == 0
    assert [e for e in out["steps"] if e["op"] == "download"][0]["status"] == "skipped"


def test_run_that_does_not_finish_ok_is_exit_1(monkeypatch, capsys, tmp_path, fake_browser):
    # a goal step with no graph: the runner aborts, and the CLI must say so with exit 1
    path = _write(tmp_path, {"name": "f", "steps": [{"op": "goto", "goal": "invoices"}]})
    code, out, _, _ = _cli(monkeypatch, capsys,
                           ["run", path, "--json", "--artifacts-root", str(tmp_path / "art")])
    assert code == 1
    assert out["status"] == "aborted"
    assert "no graph was supplied" in out["aborted"]


def test_run_human_output_marks_an_abort(monkeypatch, capsys, tmp_path, fake_browser):
    path = _write(tmp_path, {"name": "f", "steps": [{"op": "goto", "goal": "invoices"}]})
    code, _, out, _ = _cli(monkeypatch, capsys,
                           ["run", path, "--artifacts-root", str(tmp_path / "art")])
    assert code == 1
    assert "✗ ABORTED" in out
    assert "--- aborted:" in out


# --- graph resolution ----------------------------------------------------------

def test_run_needs_no_graph_for_a_url_only_flow(monkeypatch, capsys, tmp_path, fake_browser):
    path = _write(tmp_path, {"name": "f", "steps": [{"op": "goto", "url": "https://app.test/"}]})
    code, _, _, _ = _cli(monkeypatch, capsys,
                         ["run", path, "--artifacts-root", str(tmp_path / "art")])
    assert code == 0


def test_run_with_host_resolves_a_goal(monkeypatch, capsys, tmp_path, fake_browser,
                                       populated_cache_home):
    path = _write(tmp_path, {"name": "f", "steps": [{"op": "goto", "goal": "create role"}]})
    code, out, _, _ = _cli(monkeypatch, capsys,
                           ["run", path, "--host", "example.test", "--json",
                            "--artifacts-root", str(tmp_path / "art")])
    assert code == 0
    assert [e for e in out["steps"] if e["op"] == "goto"][0]["target"] == "Create Role"


def test_run_with_an_uncrawled_host_is_exit_2(monkeypatch, capsys, tmp_path, fake_browser,
                                              isolated_cache_home):
    path = _write(tmp_path, _DL_FLOW)
    code, _, _, err = _cli(monkeypatch, capsys, ["run", path, "--host", "never.test"])
    assert code == 2
    assert "no cache for never.test" in err


def test_run_with_an_invalid_host_is_exit_2(monkeypatch, capsys, tmp_path, fake_browser,
                                            isolated_cache_home):
    path = _write(tmp_path, _DL_FLOW)
    code, _, _, err = _cli(monkeypatch, capsys, ["run", path, "--host", "../../etc/passwd"])
    assert code == 2
    assert "invalid --host" in err


def test_run_host_and_graph_are_mutually_exclusive(monkeypatch, capsys, tmp_path):
    path = _write(tmp_path, _DL_FLOW)
    with pytest.raises(SystemExit) as exc:
        _cli(monkeypatch, capsys, ["run", path, "--host", "a.test", "--graph", "g.json"])
    assert exc.value.code == 2


def test_no_op_is_a_usage_error(monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        _cli(monkeypatch, capsys, [])
    assert exc.value.code == 2


# --- the subcommand is registered ----------------------------------------------

def test_flow_is_registered_in_the_cli():
    from pinchtab_webgraph import cli
    assert cli.SUBS["flow"][0] == "pinchtab_webgraph.flow_cmd"


# --- --jsonl (the streaming wire format the web UI consumes) -------------------

def _jsonl(out):
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def test_jsonl_emits_one_step_per_event_then_exactly_one_result(monkeypatch, capsys,
                                                                tmp_path):
    doc = {"name": "streamer",
           "steps": [{"op": "log", "message": "one"}, {"op": "log", "message": "two"}]}
    code, _p, out, _err = _cli(monkeypatch, capsys,
                               ["run", _write(tmp_path, doc), "--jsonl", "--dry-run"])
    assert code == 0
    frames = _jsonl(out)
    assert all(f["type"] in ("step", "result") for f in frames)
    steps = [f for f in frames if f["type"] == "step"]
    results = [f for f in frames if f["type"] == "result"]
    assert len(results) == 1                     # EXACTLY ONE terminal frame…
    assert frames[-1]["type"] == "result"        # …and it is LAST
    assert any(s.get("message") == "two" for s in steps)
    assert results[0]["status"] == "ok" and results[0]["dry_run"] is True
    assert "steps" in results[0] and "stats" in results[0]
    # the human banner must never corrupt the machine-readable stream.
    assert "=== FLOW" not in out


def test_jsonl_suppresses_the_human_banner_and_summary(monkeypatch, capsys, tmp_path):
    doc = {"name": "quiet", "steps": [{"op": "log", "message": "x"}]}
    _code, _p, out, _err = _cli(monkeypatch, capsys,
                                ["run", _write(tmp_path, doc), "--jsonl", "--dry-run"])
    assert "--- ok:" not in out and "=== FLOW" not in out
    for line in out.splitlines():
        if line.strip():
            json.loads(line)                     # EVERY line is a JSON object


def test_jsonl_rejection_is_still_the_one_terminal_result_frame(monkeypatch, capsys,
                                                                tmp_path):
    # A doc that fails validation still produces exactly one `result` line, so a supervising
    # process never has to special-case "it died before it started".
    bad = _write(tmp_path, {"name": "x", "steps": []})
    code, _p, out, _err = _cli(monkeypatch, capsys, ["run", bad, "--jsonl", "--dry-run"])
    assert code == 1
    frames = _jsonl(out)
    assert len(frames) == 1
    assert frames[0]["type"] == "result" and frames[0]["status"] == "invalid"


def test_human_and_json_modes_are_unchanged(monkeypatch, capsys, tmp_path):
    doc = {"name": "plain", "steps": [{"op": "log", "message": "x"}]}
    path = _write(tmp_path, doc)
    _code, _p, out, _err = _cli(monkeypatch, capsys, ["run", path, "--dry-run"])
    assert "=== FLOW: PLAIN ===" in out                 # the banner still prints
    _code, parsed, _out, _err = _cli(monkeypatch, capsys, ["run", path, "--dry-run", "--json"])
    assert parsed["status"] == "ok" and "type" not in parsed   # --json is NOT wrapped
