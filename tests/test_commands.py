"""Tests for commands.py — the path->pinchtab command compiler.

Pure/stdlib and browser-free: every function is deterministic and derives its output
from graph data, so these assert the exact emitted `pinchtab` lines. That is the
correctness contract — the whole point of the module is that the commands are right.
"""
import pytest

from pinchtab_webgraph import commands as cm


# --- shell quoting -------------------------------------------------------------

def test_shq_wraps_and_escapes_single_quotes():
    assert cm.shq("simple") == "'simple'"
    assert cm.shq("a b") == "'a b'"
    assert cm.shq("it's") == "'it'\\''s'"
    assert cm.shq(None) == "''"


# --- filename suggestion -------------------------------------------------------

def test_suggest_filename_prefers_url_basename():
    assert cm.suggest_filename(href="https://x.test/files/report.pdf") == "report.pdf"


def test_suggest_filename_from_label_and_accept():
    assert cm.suggest_filename(href=None, accept=".csv", label="Export Invoices") \
        == "export-invoices.csv"


def test_suggest_filename_accept_mime():
    assert cm.suggest_filename(href=None, accept="application/pdf", label="Doc") == "doc.pdf"


# --- path prefix ---------------------------------------------------------------

def test_nav_prefix_link_vs_click():
    steps = [
        {"label": "Reports", "selector": "#rep", "href": "https://x.test/reports"},
        {"label": "Exports tab", "selector": "div>button:nth-of-type(2)", "href": None},
    ]
    lines = cm.nav_prefix("https://x.test/home", steps)
    assert lines[0] == "pinchtab nav 'https://x.test/home'"
    assert lines[1] == "pinchtab nav 'https://x.test/reports'   # Reports"
    # --wait-nav on EVERY click (a tab click that navigates otherwise 409s AFTER it worked);
    # note the flag renders BARE — render_step only quotes non-flag args.
    assert lines[2] == ("pinchtab click --css 'div>button:nth-of-type(2)' --wait-nav"
                        "   # Exports tab")


def test_path_from_edges_resolves_link_dest_url():
    states = {"s2": {"url": "https://x.test/reports"}}
    epath = [
        {"label": "Reports", "selector": "#rep", "kind": "link", "to": "s2"},
        {"label": "Exports", "selector": "#ex", "kind": "click", "to": "s3"},
    ]
    steps = cm.path_from_edges(epath, states)
    assert steps[0] == {"label": "Reports", "selector": "#rep", "href": "https://x.test/reports"}
    assert steps[1] == {"label": "Exports", "selector": "#ex", "href": None}


# --- download terminal ---------------------------------------------------------

def test_download_terminal_direct_href():
    out = cm.download_terminal(href="https://x.test/a/invoice.pdf", label="Download")
    assert out == ["pinchtab download 'https://x.test/a/invoice.pdf' -o 'invoice.pdf'"]


def test_download_terminal_js_triggered():
    out = cm.download_terminal(href=None, selector="#export-btn", label="Export")
    assert out[0].startswith("pinchtab click --css '#export-btn'")


# --- upload terminal -----------------------------------------------------------

def test_upload_terminal_includes_selector_and_accept():
    out = cm.upload_terminal(selector="#file", accept=".pdf,.docx")
    assert out == ["pinchtab upload '<FILE>' -s '#file'   # accepts: .pdf,.docx"]


# --- form terminal -------------------------------------------------------------

def _form():
    return {
        "fields": [
            {"label": "Name", "type": "text", "required": True, "selector": "#name"},
            {"label": "Type", "type": "select", "options": ["A", "B"], "selector": "#type"},
            {"label": "Active", "type": "checkbox", "selector": "#active"},
            {"label": "Doc", "type": "file", "accept": ".pdf", "selector": "#doc"},
        ],
        "submitButtons": ["Save"],
        "submitSelector": "#save",
    }


def test_form_terminal_fills_by_type_and_comments_submit():
    lines = cm.form_terminal(_form(), trigger_selector="#open")
    assert lines[0] == "pinchtab click --css '#open' --wait-nav   # opens the form"
    assert "pinchtab fill '#name' '<name>'   # Name (required)" in lines
    assert "pinchtab select '#type' 'A'   # Type" in lines
    assert "pinchtab check '#active'   # Active" in lines
    assert any(l.startswith("pinchtab upload '<FILE>' -s '#doc'") for l in lines)
    # submit is COMMENTED OUT by default (safety) — and still carries --wait-nav, since a
    # human who uncomments it hits the same redirect-after-submit 409 otherwise.
    assert "# pinchtab click --css '#save' --wait-nav   # submit (uncomment to save)" in lines
    assert not any(l.startswith("pinchtab click --css '#save'") for l in lines)


def test_form_terminal_allow_submit_uncomments():
    lines = cm.form_terminal(_form(), trigger_selector="#open", allow_submit=True)
    assert "pinchtab click --css '#save' --wait-nav   # submit: Save" in lines


def test_field_without_selector_degrades_to_comment():
    lines = cm.form_terminal({"fields": [{"label": "Name", "type": "text"}]})
    assert any(l.startswith("# set 'Name'") for l in lines)


# --- for_trigger dispatch ------------------------------------------------------

def test_for_trigger_download_kind():
    trig = {"kind": "download", "label": "Download PDF",
            "href": "https://x.test/f/doc.pdf"}
    steps = [{"label": "Reports", "selector": "#r", "href": "https://x.test/reports"}]
    lines = cm.for_trigger(trig, steps, "https://x.test/home")
    assert lines[0] == "pinchtab nav 'https://x.test/home'"
    assert "pinchtab download 'https://x.test/f/doc.pdf' -o 'doc.pdf'" in lines


def test_for_trigger_form_kind_default():
    trig = {"label": "Create team", "selector": "#new", "form": _form()}
    lines = cm.for_trigger(trig, [], "https://x.test/teams")
    assert lines[0] == "pinchtab nav 'https://x.test/teams'"
    assert any(l.startswith("pinchtab fill '#name'") for l in lines)
    assert any(l.startswith("# pinchtab click --css '#save'") for l in lines)


def test_is_direct_download():
    assert cm.is_direct_download("https://x.test/a/report.pdf")
    assert cm.is_direct_download("https://x.test/data.csv?v=2")
    assert cm.is_direct_download("blob:https://x.test/abc")
    assert not cm.is_direct_download("https://x.test/reports")
    assert not cm.is_direct_download(None)


# --- integration: api.howto surfaces commands + fixes download confidence ------

import json

from pinchtab_webgraph import api, cache_store

_GRAPH = {
    "meta": {"host": "app.test", "states": 2, "edges": 1, "triggers": 2},
    "states": [
        {"id": "s0", "url": "https://app.test/home", "label": "Home", "depth": 0},
        {"id": "s1", "url": "https://app.test/reports", "label": "Reports", "depth": 1},
    ],
    "state_index": {},
    "edges": [{"from": "s0", "to": "s1", "label": "Reports", "selector": "#nav",
               "kind": "link"}],
    "triggers": [
        {"label": "Export CSV", "state": "s1", "path": [], "form": None, "opensAt": None,
         "kind": "download", "selector": "#export", "href": None, "accept": None},
        {"label": "Download report", "state": "s1", "path": [], "form": None, "opensAt": None,
         "kind": "download", "selector": None,
         "href": "https://app.test/files/q3.pdf", "accept": None},
    ],
}


def _write_graph(tmp_path):
    p = tmp_path / "cache.json"
    p.write_text(json.dumps(_GRAPH))
    return str(p)


def test_api_howto_download_is_high_confidence(tmp_path):
    # a download trigger opens NO form (0 fields) — the confidence gate must NOT
    # suppress it. This is the fix that makes "how do I download X" answerable.
    out = api.howto(_write_graph(tmp_path), goal="export csv", start="https://app.test/home")
    assert out["status"] == "ok"
    r = out["results"][0]
    assert r["action_kind"] == "download"
    assert r["confidence"] == "high"
    assert "pinchtab click --css '#export'" in "\n".join(r["commands"])


def test_api_howto_direct_download_command(tmp_path):
    out = api.howto(_write_graph(tmp_path), goal="download report", start="https://app.test/home")
    r = out["results"][0]
    assert r["download_url"] == "https://app.test/files/q3.pdf"
    assert "pinchtab download 'https://app.test/files/q3.pdf' -o 'q3.pdf'" in "\n".join(r["commands"])


# --- cache_store propagates the terminal-action descriptor ---------------------

def test_cache_store_stitch_records_download_kind():
    live_rec = {
        "start": "https://app.test/home", "trigger": "Export CSV",
        "triggerPage": "https://app.test/reports",
        "pathStructured": [{"label": "Reports", "selector": "#nav",
                            "href": "https://app.test/reports"}],
        "form": None, "opensAt": None,
        "triggerKind": "download", "triggerSelector": "#export",
        "triggerHref": None, "triggerAccept": None,
    }
    graph = {"meta": {}, "states": [], "state_index": {}, "edges": [], "triggers": []}
    _states, _edges, trig = cache_store.stitch(live_rec, graph)
    assert trig["kind"] == "download"
    assert trig["selector"] == "#export"
    assert trig["href"] is None
    assert trig["label"] == "Export CSV"


# --- the checkbox step shape (a `check` argv has NO value slot) -----------------
# regression: `_field_step` emits a checkbox as needs_input=True with value_index=None, and
# BOTH executors used to do `argv[step["value_index"]] = val` -> argv[None] -> TypeError the
# moment anyone passed `--set 'Active=true'`. The step shape here is the source of truth.

def test_checkbox_step_needs_input_but_has_no_value_index():
    step = cm._field_step({"label": "Active", "type": "checkbox", "selector": "#active"})
    assert step["argv"] == ["check", "#active"]
    assert step["needs_input"] is True
    assert step["value_index"] is None            # nothing to substitute into
    assert step["label"] == "Active"


@pytest.mark.parametrize("value, expect", [
    (True, True), ("true", True), ("yes", True), ("1", True), ("on", True), ("x", True),
    (False, False), ("false", False), ("no", False), ("0", False), ("off", False),
    ("", False), ("  ", False), (None, False),
])
def test_is_truthy_reads_a_checkbox_value_as_a_boolean(value, expect):
    assert cm.is_truthy(value) is expect
