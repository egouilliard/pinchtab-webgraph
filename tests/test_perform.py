"""Tests for perform.py (the executor) + api.resolve_action.

The executor is exercised with an INJECTED runner (`_run`) so no browser/bridge is
needed — we assert which steps run, which are skipped/gated, value substitution, the
download out-dir rewrite, and abort-on-navigation-failure. resolve_action is checked
against a hand-built graph.
"""
import json

from pinchtab_webgraph import api, commands, perform


# --- a fake pinchtab runner: records argv, returns a scripted rc ---------------

class FakeRunner:
    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on          # a substring of render → that call returns rc 1

    def __call__(self, argv, server, token, tab, timeout=90):
        self.calls.append(argv)
        if self.fail_on and self.fail_on in " ".join(argv):
            return 1, "", "boom"
        return 0, "ok", ""


_DL = {"kind": "download", "label": "Download report",
       "href": "https://app.test/files/q3.pdf", "selector": None}
_FORM = {"kind": "form", "label": "Create team", "selector": "#new-team", "opensAt": None,
         "form": {"fieldCount": 2, "submitButtons": ["Create"], "submitSelector": "#save",
                  "fields": [
                      {"label": "Name", "type": "text", "required": True, "selector": "#n"},
                      {"label": "Plan", "type": "select", "options": ["Free", "Pro"],
                       "selector": "#p"},
                  ]}}
_PATH = [{"label": "Reports", "selector": "#nav", "href": "https://app.test/reports"}]


def test_download_runs_nav_then_download():
    run = FakeRunner()
    res = perform.execute_plan(_DL, _PATH, "https://app.test/home", server="s", _run=run)
    assert [r["status"] for r in res] == ["ok", "ok", "ok"]
    # nav home, nav reports, download
    assert run.calls[0][0] == "nav"
    assert run.calls[-1][:2] == ["download", "https://app.test/files/q3.pdf"]


def test_download_out_dir_rewrites_output():
    run = FakeRunner()
    perform.execute_plan(_DL, _PATH, "https://app.test/home", out_dir="/tmp/dl", _run=run)
    dl = run.calls[-1]
    assert dl[dl.index("-o") + 1] == "/tmp/dl/q3.pdf"


def test_form_skips_fields_without_values_and_gates_submit():
    run = FakeRunner()
    res = perform.execute_plan(_FORM, _PATH, "https://app.test/home", _run=run)
    ran = [r for r in res if r["status"] == "ok"]
    skipped = [r for r in res if r["status"] == "skipped"]
    # only nav/nav/open-form ran; both fields skipped (need values), submit gated
    assert len(ran) == 3
    assert {r["role"] for r in skipped} == {"fill", "select", "submit"}
    # crucially: no fill/select/submit was ever sent to the browser
    assert not any(c[0] in ("fill", "select") for c in run.calls)
    assert not any(c == ["click", "--css", "#save"] for c in run.calls)


def test_form_runs_fields_with_values_and_submit_when_allowed():
    run = FakeRunner()
    perform.execute_plan(_FORM, _PATH, "https://app.test/home", allow_submit=True,
                         values={"Name": "Acme", "Plan": "Pro"}, _run=run)
    assert ["fill", "#n", "Acme"] in run.calls
    assert ["select", "#p", "Pro"] in run.calls
    assert ["click", "--css", "#save"] in run.calls   # submit ran (allow_submit)


def test_upload_field_needs_file():
    run = FakeRunner()
    trig = {"kind": "form", "label": "x", "selector": "#o", "opensAt": None,
            "form": {"fields": [{"label": "Doc", "type": "file", "accept": ".pdf",
                                 "selector": "#d"}], "submitButtons": [], "fieldCount": 1}}
    # without --file → skipped
    res = perform.execute_plan(trig, [], "https://app.test/x", _run=run)
    assert any(r["role"] == "upload" and r["status"] == "skipped" for r in res)
    # with --file → runs with the real path
    run2 = FakeRunner()
    perform.execute_plan(trig, [], "https://app.test/x", upload_file="/x.pdf", _run=run2)
    assert ["upload", "/x.pdf", "-s", "#d"] in run2.calls


def test_aborts_when_a_navigation_step_fails():
    run = FakeRunner(fail_on="app.test/reports")
    res = perform.execute_plan(_DL, _PATH, "https://app.test/home", _run=run)
    assert res[-1]["status"] == "aborted"
    # the download step was never attempted after the nav failure
    assert not any(c[0] == "download" for c in run.calls)


def test_dry_run_touches_nothing():
    run = FakeRunner()
    res = perform.execute_plan(_DL, _PATH, "https://app.test/home", dry_run=True, _run=run)
    assert all(r["status"] == "dry-run" for r in res)
    assert run.calls == []


# --- resolve_action ------------------------------------------------------------

_GRAPH = {
    "meta": {"host": "app.test"},
    "states": [
        {"id": "s0", "url": "https://app.test/home", "depth": 0},
        {"id": "s1", "url": "https://app.test/reports", "depth": 1},
    ],
    "state_index": {},
    "edges": [{"from": "s0", "to": "s1", "label": "Reports", "selector": "#nav",
               "kind": "link"}],
    "triggers": [
        {"label": "Download report", "state": "s1", "path": [], "kind": "download",
         "selector": None, "href": "https://app.test/files/q3.pdf", "form": None},
    ],
}


def test_resolve_action_returns_executable_plan(tmp_path):
    p = tmp_path / "g.json"
    p.write_text(json.dumps(_GRAPH))
    plan = api.resolve_action(str(p), goal="download report", start="https://app.test/home")
    assert plan["status"] == "ok"
    assert plan["action_kind"] == "download"
    assert plan["download_url"] == "https://app.test/files/q3.pdf"
    assert plan["trigger"]["label"] == "Download report"
    assert plan["path_steps"][0]["href"] == "https://app.test/reports"
    # the plan is directly runnable
    run = FakeRunner()
    res = perform.execute_plan(plan["trigger"], plan["path_steps"], plan["start_url"],
                               _run=run)
    assert any(c[0] == "download" for c in run.calls)
    assert all(r["status"] == "ok" for r in res)


def test_resolve_action_no_match(tmp_path):
    p = tmp_path / "g.json"
    p.write_text(json.dumps(_GRAPH))
    assert api.resolve_action(str(p), goal="frobnicate widget")["status"] == "no_match"


# --- regression: the interaction-crawl output must keep the terminal-action fields ---
# (a real crawl once dropped kind/selector/href here, so downloads resolved as
# zero-field low-confidence and every download how-to failed — caught in live testing.)

def test_interaction_crawl_serializes_download_trigger_fields():
    from pinchtab_webgraph import interaction_crawl
    dl = {"label": "Download Q3 report", "state": "sig1", "path": [], "form": None,
          "opensAt": None, "kind": "download", "selector": "#dl",
          "href": "http://x/q3.pdf", "accept": None}
    rec = interaction_crawl.serialize_trigger(dl, "s1")
    assert rec["kind"] == "download"
    assert rec["selector"] == "#dl"
    assert rec["href"] == "http://x/q3.pdf"
    assert rec["state"] == "s1"
    # a create-form trigger with no explicit kind still serializes as a form
    form = {"label": "Create team", "state": "sig2", "path": [], "form": {"fieldCount": 1},
            "opensAt": None}
    assert interaction_crawl.serialize_trigger(form, "s2")["kind"] == "form"
