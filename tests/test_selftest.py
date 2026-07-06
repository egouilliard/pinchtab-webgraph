"""Tests for pinchtab_webgraph.selftest — the pure self-test/report/issue core.

Only the I/O-free functions are exercised here (evaluate_scenario / build_report /
render_html / render_issue); the interactive loop and `gh` subprocess are thin
wrappers over these. Goals are checked against the hand-authored interaction
fixture (provably correct against the real howto algorithm).
"""
import sys
import types

import pytest

from pinchtab_webgraph import selftest


# --- evaluate_scenario -------------------------------------------------------

def test_evaluate_found(sample_interaction_graph_path):
    rec = selftest.evaluate_scenario(sample_interaction_graph_path, "create role")
    assert rec["status"] == "ok"
    assert selftest.scenario_found(rec) is True
    assert rec["clicks"] == 3
    assert rec["trigger"] == "Create Role"
    assert rec["steps"]  # non-empty path
    assert rec["form_field_count"] == 1
    # a fresh scenario is unjudged until the human rates it
    assert rec["verdict"] == selftest.UNRATED
    assert rec["note"] == ""


def test_evaluate_unreachable_is_a_finding(sample_interaction_graph_path):
    rec = selftest.evaluate_scenario(sample_interaction_graph_path, "add widget")
    assert rec["status"] == "unreachable"
    assert selftest.scenario_found(rec) is False
    assert rec["clicks"] is None
    assert rec["steps"] == []
    assert "Add Widget" in rec["candidates"]


def test_evaluate_no_match(sample_interaction_graph_path):
    rec = selftest.evaluate_scenario(sample_interaction_graph_path, "create nonexistent")
    assert rec["status"] == "no_match"
    assert selftest.scenario_found(rec) is False


# --- build_report ------------------------------------------------------------

def _records():
    return [
        {"goal": "create role", "status": "ok", "clicks": 3, "trigger": "Create Role",
         "steps": ["Go to /dashboard", "Click “Team”", "Click the “Create Role” button"],
         "form_field_count": 1, "candidates": [], "verdict": selftest.PASS, "note": ""},
        {"goal": "add widget", "status": "unreachable", "clicks": None, "trigger": None,
         "steps": [], "form_field_count": None, "candidates": ["Add Widget"],
         "verdict": selftest.FAIL, "note": "should be reachable from the dashboard"},
        {"goal": "add report", "status": "ok", "clicks": 2, "trigger": "Add Report",
         "steps": ["Go to /dashboard", "Click the “Add Report” button"],
         "form_field_count": None, "candidates": [], "verdict": selftest.UNRATED, "note": ""},
    ]


def test_build_report_totals():
    rep = selftest.build_report("example.test", "https://example.test/dashboard",
                                "g.json", {"states": 5, "edges": 3, "triggers": 3},
                                _records(), "2026-07-03 10:00:00")
    assert rep["totals"] == {"total": 3, "pass": 1, "fail": 1, "unrated": 1}
    assert rep["host"] == "example.test"
    assert len(rep["scenarios"]) == 3


# --- render_html -------------------------------------------------------------

def test_render_html_is_complete_and_safe():
    rep = selftest.build_report("example.test", "https://example.test/dashboard",
                                "g.json", {"states": 5, "edges": 3, "triggers": 3},
                                _records(), "2026-07-03 10:00:00")
    out = selftest.render_html(rep)
    assert out.lstrip().startswith("<!doctype html>")
    # all placeholder tokens were substituted
    assert "{{" not in out and "}}" not in out
    assert "example.test" in out
    assert "create role" in out
    assert "should be reachable from the dashboard" in out  # the fail note is rendered
    # verdict + capture badges present
    assert "PASS" in out and "FAIL" in out and "UNRATED" in out


def test_render_html_escapes_user_input():
    recs = [{"goal": "<script>alert(1)</script>", "status": "no_match", "clicks": None,
             "trigger": None, "steps": [], "form_field_count": None, "candidates": [],
             "verdict": selftest.FAIL, "note": "x & y <b>bold</b>"}]
    out = selftest.render_html(selftest.build_report("h", None, "g", {}, recs, "now"))
    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;" in out
    assert "x &amp; y &lt;b&gt;bold&lt;/b&gt;" in out


# --- render_issue ------------------------------------------------------------

def test_render_issue_highlights_fails():
    rep = selftest.build_report("example.test", "https://example.test/dashboard",
                                "g.json", {"states": 5, "edges": 3, "triggers": 3},
                                _records(), "2026-07-03 10:00:00")
    title, body = selftest.render_issue(rep)
    assert title == "[self-test] example.test — 1/3 scenarios failed"
    assert "## Failing scenarios" in body
    assert "### add widget" in body
    assert "should be reachable from the dashboard" in body
    # passing scenarios are summarised in a collapsible section
    assert "passing scenarios" in body
    assert "create role" in body


def test_render_issue_no_fails():
    recs = [{"goal": "create role", "status": "ok", "clicks": 3, "trigger": "Create Role",
             "steps": [], "form_field_count": 1, "candidates": [],
             "verdict": selftest.PASS, "note": ""}]
    rep = selftest.build_report("h", None, "g", {}, recs, "now")
    title, body = selftest.render_issue(rep)
    assert title == "[self-test] h — 0/1 scenarios failed"
    assert "No failing scenarios" in body


# --- _resolve_graph (routing / error branches) -------------------------------
# Mirrors tests/test_query_cmd.py's coverage of the analogous query_cmd._resolve_graph.

def test_resolve_graph_needs_a_source():
    path, host, err = selftest._resolve_graph(None, None)
    assert path is None and host is None
    assert "--start" in err and "--graph" in err


def test_resolve_graph_start_without_scheme_is_rejected():
    # urlparse("example.com/x").hostname is None → clean error, not a ValueError later
    path, host, err = selftest._resolve_graph("example.com/page", None)
    assert path is None and host is None
    assert "full URL" in err


def test_resolve_graph_no_cache_for_host(isolated_cache_home):
    path, host, err = selftest._resolve_graph("https://example.test/dashboard", None)
    assert path is None
    assert host == "example.test"          # host still surfaced for messaging
    assert "no cache for example.test" in err


def test_resolve_graph_host_from_graph_meta(sample_interaction_graph_path):
    path, host, err = selftest._resolve_graph(None, str(sample_interaction_graph_path))
    assert err is None
    assert path == str(sample_interaction_graph_path)
    assert host == "example.test"          # derived from graph meta.host, no --start


def test_resolve_graph_from_cache(populated_cache_home):
    path, host, err = selftest._resolve_graph("https://example.test/dashboard", None)
    assert err is None and host == "example.test"
    assert path.endswith("example.test.json")


# --- _submit_issue (opt-in / confirm gate) -----------------------------------
# Mirrors tests/test_cache_cmd.py's both-sides coverage of the destructive-confirm gate.

def _capture_gh(monkeypatch, gh_present=True, answer="n"):
    """Stub shutil.which / subprocess.run / input; return the calls list."""
    calls = []
    monkeypatch.setattr(selftest.shutil, "which",
                        lambda n: "/usr/bin/gh" if gh_present else None)
    monkeypatch.setattr(selftest.subprocess, "run",
                        lambda *a, **k: calls.append(a[0]) or types.SimpleNamespace(returncode=0))
    monkeypatch.setattr("builtins.input", lambda *a, **k: answer)
    return calls


def test_submit_issue_gh_absent(monkeypatch, capsys):
    calls = _capture_gh(monkeypatch, gh_present=False)
    assert selftest._submit_issue("owner/name", "t", "b") is False
    assert calls == []                     # never shelled out
    assert "gh" in capsys.readouterr().err


def test_submit_issue_declined_does_not_post(monkeypatch, capsys):
    calls = _capture_gh(monkeypatch, gh_present=True, answer="n")
    assert selftest._submit_issue("owner/name", "t", "b", assume_yes=False) is False
    assert calls == []                     # user said no → gh NOT invoked
    out = capsys.readouterr().out
    assert "PUBLIC" in out                 # the warning was shown before the gate


def test_submit_issue_yes_posts_once(monkeypatch):
    calls = _capture_gh(monkeypatch, gh_present=True)
    assert selftest._submit_issue("owner/name", "T", "B", assume_yes=True) is True
    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[:3] == ["gh", "issue", "create"]
    assert "-R" in cmd and "owner/name" in cmd


# --- main() TTY / --yes gating -----------------------------------------------

def _run_main(monkeypatch, argv, tty=False):
    monkeypatch.setattr(sys, "argv", ["test", *argv])
    monkeypatch.setattr(selftest.sys, "stdin", types.SimpleNamespace(isatty=lambda: tty))
    return selftest.main()


def test_main_non_tty_no_goal_errors(monkeypatch, sample_interaction_graph_path, capsys):
    rc = _run_main(monkeypatch, ["--graph", str(sample_interaction_graph_path)])
    assert rc == 1
    assert "no --goal" in capsys.readouterr().err


def test_main_non_tty_with_goals_writes_report(monkeypatch, sample_interaction_graph_path,
                                                tmp_path, capsys):
    out = tmp_path / "r.html"
    calls = _capture_gh(monkeypatch)           # in case anything tries to post
    rc = _run_main(monkeypatch, ["--graph", str(sample_interaction_graph_path),
                                 "--goal", "create role", "--goal", "add widget",
                                 "--out", str(out)])
    assert rc == 0
    assert out.exists()
    html = out.read_text()
    assert "create role" in html
    # seeds run unattended → verdicts are UNRATED, and nothing posts without --repo
    assert "UNRATED" in html
    assert calls == []


def test_main_repo_without_yes_does_not_post(monkeypatch, sample_interaction_graph_path,
                                             tmp_path, capsys):
    out = tmp_path / "r.html"
    calls = _capture_gh(monkeypatch)
    rc = _run_main(monkeypatch, ["--graph", str(sample_interaction_graph_path),
                                 "--goal", "add widget", "--repo", "owner/name",
                                 "--out", str(out)])
    assert rc == 0
    assert calls == []                         # no TTY + no --yes → must NOT post
    assert "skipping issue" in capsys.readouterr().out


def test_main_repo_with_yes_posts(monkeypatch, sample_interaction_graph_path,
                                  tmp_path, capsys):
    out = tmp_path / "r.html"
    calls = _capture_gh(monkeypatch)
    rc = _run_main(monkeypatch, ["--graph", str(sample_interaction_graph_path),
                                 "--goal", "add widget", "--repo", "owner/name",
                                 "--yes", "--out", str(out)])
    assert rc == 0
    assert len(calls) == 1                      # --yes bypasses the TTY confirm → posts
    assert calls[0][:3] == ["gh", "issue", "create"]
