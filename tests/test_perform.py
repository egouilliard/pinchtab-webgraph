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
    assert not any(c[:2] == ["click", "--css"] and c[2] == "#save" for c in run.calls)


def test_form_runs_fields_with_values_and_submit_when_allowed():
    run = FakeRunner()
    perform.execute_plan(_FORM, _PATH, "https://app.test/home", allow_submit=True,
                         values={"Name": "Acme", "Plan": "Pro"}, _run=run)
    assert ["fill", "#n", "Acme"] in run.calls
    assert ["select", "#p", "Pro"] in run.calls
    # submit ran (allow_submit) — and carries --wait-nav (see below)
    assert ["click", "--css", "#save", "--wait-nav"] in run.calls


# --- regression: a click that NAVIGATES must not abort the run ------------------
# PinchTab's action guard 409s ("unexpected page navigation") on a click that moves the
# page — AFTER the click already succeeded. execute_steps aborts on rc != 0 for role
# nav/click, so a successful form submit that server-side-redirects used to be reported as
# a FAILED, aborted run. commands.py now emits every click with --wait-nav, which makes
# PinchTab wait for the navigation and return rc 0. Two things are asserted:
#   1. every click argv carries the flag (so the guard never fires), and
#   2. with the flag present the run completes — the submit is `ok`, nothing is `aborted`.

def test_every_click_step_carries_wait_nav():
    steps = commands.steps_for_trigger(_FORM, _PATH, "https://app.test/home",
                                       allow_submit=True)
    clicks = [s for s in steps if s["argv"] and s["argv"][0] == "click"]
    assert clicks                                     # trigger click + submit click
    for s in clicks:
        assert s["argv"][-1] == "--wait-nav"
        assert s["argv"].count("--wait-nav") == 1     # no double-flag
        # appending a trailing flag is index-safe: a click never substitutes into its argv
        assert s["value_index"] is None and not s["needs_input"]


class NavGuardRunner(FakeRunner):
    """A bridge whose action guard 409s on a click that navigates — UNLESS --wait-nav is
    passed (the real PinchTab behaviour). Any navigating click here is `#save`."""

    def __call__(self, argv, server, token, tab, timeout=90):
        self.calls.append(argv)
        if argv[0] == "click" and "#save" in argv and "--wait-nav" not in argv:
            return 1, "", "409: unexpected page navigation: /teams/new -> /teams/42"
        return 0, "ok", ""


def test_navigating_click_does_not_abort_the_run():
    run = NavGuardRunner()
    res = perform.execute_plan(_FORM, _PATH, "https://app.test/home", allow_submit=True,
                               values={"Name": "Acme", "Plan": "Pro"}, _run=run)
    assert not any(r["status"] == "aborted" for r in res)
    assert not any(r["status"] == "error" for r in res)
    submit = [r for r in res if r["role"] == "submit"][0]
    assert submit["status"] == "ok"


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


# --- regression: a FRESH (zero-tab) bridge -------------------------------------
# A bridge with no open tabs has nothing to adopt, and the ONLY way to make a tab is
# `nav <real url> --new-tab` (it rejects a blank-page url with 400). resolve_tab used to try
# exactly that blank url, get a 400, and return None — after which every step fell back to
# the bridge's STALE stored default tab and 404'd, so `perform` aborted on its first step.

class ZeroTabRunner:
    """No page tabs: any command aimed at a tab 404s until `nav --new-tab` makes one."""

    def __init__(self):
        self.calls = []
        self.tabs = set()

    def __call__(self, argv, server, token, tab, timeout=90):
        self.calls.append((list(argv), tab))
        if argv[0] == "tab":
            return 0, "[]", ""
        if argv[0] == "nav" and "--new-tab" in argv:
            self.tabs.add("fresh")
            return 0, "fresh", ""
        if tab not in self.tabs:
            return 1, "", "Error 404: tab E3618C01 not found"
        return 0, "ok", ""


def test_resolve_tab_opens_the_first_tab_at_the_start_url():
    run = ZeroTabRunner()
    assert perform.resolve_tab("s", None, "https://app.test/home", _run=run) == "fresh"
    argv = run.calls[-1][0]
    assert argv[:2] == ["nav", "https://app.test/home"]
    assert "--new-tab" in argv and "--print-tab-id" in argv
    assert not any("about:blank" in " ".join(c[0]) for c in run.calls)


def test_resolve_tab_returns_none_with_no_tabs_and_no_url():
    run = ZeroTabRunner()
    assert perform.resolve_tab("s", None, _run=run) is None
    assert not any(c[0][0] == "nav" for c in run.calls)


def test_zero_tab_bridge_does_not_abort_the_run():
    """The shipped bug, end to end: with tab=None on a bridge with no tabs, the first nav
    404s ('tab not found'); execute_steps must re-nav in a new tab, pin it, and carry it
    through the REST of the plan — instead of aborting."""
    run = ZeroTabRunner()
    res = perform.execute_plan(_DL, _PATH, "https://app.test/home", server="s", tab=None,
                               _run=run)
    assert not any(r["status"] in ("error", "aborted") for r in res)
    assert [r["status"] for r in res] == ["ok", "ok", "ok"]
    # the first nav was retried at the SAME url with --new-tab, and every later call carried
    # the tab it printed.
    first, retry = run.calls[0], run.calls[1]
    assert first[0] == ["nav", "https://app.test/home"] and first[1] is None
    assert retry[0] == ["nav", "https://app.test/home", "--new-tab", "--print-tab-id"]
    assert [c[1] for c in run.calls[2:]] == ["fresh"] * len(run.calls[2:])
    assert run.calls[-1][0][:2] == ["download", "https://app.test/files/q3.pdf"]


def test_a_genuine_nav_failure_is_not_retried_in_a_new_tab():
    run = FakeRunner(fail_on="app.test/reports")     # fails with 'boom', not 'not found'
    res = perform.execute_plan(_DL, _PATH, "https://app.test/home", tab="t1", _run=run)
    assert res[-1]["status"] == "aborted"
    assert not any("--new-tab" in c for c in run.calls)


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


# --- checkbox: a `check` step has no value slot (regression) --------------------
# `--set 'Active=true'` used to crash with TypeError (argv[None]) the moment a form had a
# checkbox. A checkbox value is a BOOLEAN: truthy -> check it, falsy/absent -> leave it.

_CHECKBOX_FORM = {
    "kind": "form", "label": "Settings", "selector": "#open", "opensAt": None,
    "form": {"fieldCount": 1, "submitButtons": [], "submitSelector": None,
             "fields": [{"label": "Active", "type": "checkbox", "selector": "#active"}]}}


def test_checkbox_without_a_value_is_skipped():
    run = FakeRunner()
    res = perform.execute_plan(_CHECKBOX_FORM, [], "https://app.test/x", _run=run)
    check = [r for r in res if r["role"] == "check"][0]
    assert check["status"] == "skipped"
    assert not any(c[0] == "check" for c in run.calls)


def test_checkbox_with_a_truthy_value_is_checked():
    run = FakeRunner()
    res = perform.execute_plan(_CHECKBOX_FORM, [], "https://app.test/x",
                               values={"Active": "true"}, _run=run)
    check = [r for r in res if r["role"] == "check"][0]
    assert check["status"] == "ok"
    assert ["check", "#active"] in run.calls        # argv is UNCHANGED — no substitution


def test_checkbox_with_a_falsy_value_is_skipped_not_crashed():
    run = FakeRunner()
    res = perform.execute_plan(_CHECKBOX_FORM, [], "https://app.test/x",
                               values={"Active": "false"}, _run=run)
    check = [r for r in res if r["role"] == "check"][0]
    assert check["status"] == "skipped"
    assert "falsy" in check["reason"]
    assert not any(c[0] == "check" for c in run.calls)
