"""Tests for browser.py — the port between a flow and a live page.

PinchTabBrowser is driven through an INJECTED `_run` (the same idiom test_perform.py uses for
the CLI), so every assertion here is about the exact argv we send and the exact bytes/JSON we
get back — no bridge, no browser, no network.
"""
import base64
import json

import pytest

from pinchtab_webgraph import browser as B


class FakeRun:
    """Stands in for the `pinchtab` CLI: records argv, returns a scripted (rc, out, err)."""

    def __init__(self, out="", rc=0, err="", script=None):
        self.calls = []
        self.timeouts = []
        self.out, self.rc, self.err = out, rc, err
        self.script = script or {}      # argv[0] -> (rc, out, err)

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        self.timeouts.append(timeout)
        if argv[0] in self.script:
            return self.script[argv[0]]
        return self.rc, self.out, self.err


def _browser(**kw):
    run = FakeRun(**kw)
    return B.PinchTabBrowser("http://s", "tok", "tab1", _run=run), run


# --- argv contract ------------------------------------------------------------

def test_each_method_sends_the_exact_argv():
    b, run = _browser()
    b.nav("https://x.test/a")
    b.click("#c")
    b.fill("#f", "v")
    b.select("#s", "opt")
    b.check("#k")
    b.upload("#u", "/tmp/x.pdf")
    b.download("https://x.test/f.pdf", "out.pdf")
    assert run.calls == [
        ["nav", "https://x.test/a"],
        # --wait-nav: a click that navigates otherwise 409s ("unexpected page navigation")
        # after the page has already moved — which would abort a paginating flow.
        ["click", "--css", "#c", "--wait-nav"],
        ["fill", "#f", "v"],
        ["select", "#s", "opt"],
        ["check", "#k"],
        ["upload", "/tmp/x.pdf", "-s", "#u"],
        ["download", "https://x.test/f.pdf", "-o", "out.pdf"],
    ]


def test_download_returns_its_out_path_and_gets_a_long_timeout():
    b, run = _browser()
    assert b.download("https://x/f.pdf", "out.pdf") == "out.pdf"
    assert run.timeouts[-1] >= 180


def test_fill_and_select_stringify_a_non_string_value():
    b, run = _browser()
    b.fill("#f", 7)
    b.select("#s", 2)
    assert run.calls == [["fill", "#f", "7"], ["select", "#s", "2"]]


def test_subprocess_argv_carries_the_server_and_env():
    seen = {}

    def fake_run(cmd, capture_output, text, timeout, env):
        seen["cmd"] = cmd
        seen["env"] = env

        class R:
            returncode, stdout, stderr = 0, "ok", ""
        return R()

    import subprocess
    real = subprocess.run
    subprocess.run = fake_run
    try:
        B.PinchTabBrowser("http://bridge", "T0K", "tab9").nav("https://x/")
    finally:
        subprocess.run = real
    assert seen["cmd"][:4] == ["pinchtab", "--server", "http://bridge", "nav"]
    assert seen["env"]["PINCHTAB_TOKEN"] == "T0K"
    assert seen["env"]["PINCHTAB_TAB"] == "tab9"   # the tab goes in via the ENV, never a flag


# --- errors -------------------------------------------------------------------

@pytest.mark.parametrize("argv, fatal", [
    (["nav", "https://x/"], True),
    (["click", "--css", "#c"], True),
    (["fill", "#f", "v"], False),
    (["download", "https://x/f", "-o", "f"], False),
    (["eval", "1"], False),
])
def test_step_fatal_is_true_only_for_nav_and_click(argv, fatal):
    b, _ = _browser(rc=1, err="boom")
    with pytest.raises(B.BrowserError) as exc:
        b.run(argv)
    assert exc.value.step_fatal is fatal
    assert "boom" in str(exc.value)


# --- evaluate: ONE decode, every type ------------------------------------------
# regression: `pinchtab eval` prints a string result UNQUOTED, and because we always wrap the
# expression in JSON.stringify(...) stdout is ALWAYS valid JSON. A second json.loads therefore
# blew up on EVERY string result — including location.href, which silently disabled the
# pagination no-progress guard.

@pytest.mark.parametrize("value", [
    {"a": 1, "b": [2]}, [1, 2, 3], 3, 1.5, True, False, None, "hello", "",
    '{"looks":"like json"}',            # a string that would double-decode into a dict
])
def test_evaluate_single_decodes_every_type(value):
    b, run = _browser(out=json.dumps(value))    # what `JSON.stringify(expr)` puts on stdout
    assert b.evaluate("expr") == value
    assert run.calls == [["eval", "JSON.stringify(expr)"]]


def test_evaluate_empty_output_is_none():
    b, _ = _browser(out="")
    assert b.evaluate("expr") is None


def test_evaluate_bad_output_raises():
    b, _ = _browser(out="not json at all")
    with pytest.raises(B.BrowserError, match="bad eval output"):
        b.evaluate("expr")


def test_url_and_page_signature_survive_a_string_result():
    b, _ = _browser(out=json.dumps("https://x.test/p?page=2"))
    assert b.url() == "https://x.test/p?page=2"
    assert b.page_signature() == "https://x.test/p?page=2"


def test_evaluate_await_promise_flags_and_wraps_inside_the_chain():
    b, run = _browser(out=json.dumps("v"))
    assert b.evaluate("thing()", await_promise=True) == "v"
    argv = run.calls[0]
    assert argv[0] == "eval"
    assert "--await-promise" in argv          # without it the bridge returns {} with rc=0
    # the stringify must be INSIDE the chain — you cannot JSON.stringify a Promise
    assert argv[1] == "(thing()).then(v=>JSON.stringify(v))"
    assert not argv[1].startswith("JSON.stringify(")


# --- fetch_bytes / save_bytes --------------------------------------------------

def test_fetch_bytes_decodes_base64_and_awaits_the_promise():
    payload = b"%PDF-1.4 binary \x00\xff bytes"
    b, run = _browser(out=json.dumps(base64.b64encode(payload).decode()))
    assert b.fetch_bytes("https://x.test/a b.pdf") == payload
    argv = run.calls[0]
    assert "--await-promise" in argv
    assert '"https://x.test/a b.pdf"' in argv[1]      # url injected as a JSON literal
    assert "credentials:'include'" in argv[1]         # inherits the page's session cookies


def test_fetch_js_json_escapes_a_hostile_url():
    js = B.fetch_js("https://x/'+alert(1)+'\"")
    assert "alert(1)" in js                            # present, but…
    assert js.count("fetch(\"") == 1                    # …inside a JSON string literal
    assert '\\"' in js


def test_fetch_bytes_enforces_the_size_cap(monkeypatch):
    monkeypatch.setattr(B, "MAX_FETCH_BYTES", 8)
    b, _ = _browser(out=json.dumps(base64.b64encode(b"x" * 9).decode()))
    with pytest.raises(B.BrowserError, match="over the 8-byte limit"):
        b.fetch_bytes("https://x/f.pdf")


def test_fetch_bytes_rejects_a_non_string_result():
    b, _ = _browser(out=json.dumps({"oops": 1}))
    with pytest.raises(B.BrowserError, match="not base64"):
        b.fetch_bytes("https://x/f.pdf")


def test_fetch_bytes_surfaces_an_http_error_as_browsererror():
    # a 404 throws inside the page → rc != 0 → BrowserError (what the runner falls back on)
    b, _ = _browser(rc=1, err="Error: HTTP 404")
    with pytest.raises(B.BrowserError, match="404"):
        b.fetch_bytes("https://x/missing.pdf")


def test_save_bytes_writes_the_file_and_returns_the_path(tmp_path):
    b, _ = _browser(out=json.dumps(base64.b64encode(b"hello").decode()))
    out = str(tmp_path / "nested" / "f.pdf")
    assert b.save_bytes("https://x/f.pdf", out) == out
    assert open(out, "rb").read() == b"hello"      # parent dir created for us


# --- the generic JS primitives -------------------------------------------------

def test_query_js_json_escapes_a_hostile_label_regex():
    spec = {"label": "a\"); alert(1); //", "kind": "download"}
    js = B.query_js(spec)
    # the spec crosses into JS as a JSON *string literal* handed to JSON.parse — never
    # concatenated into the source, so a label regex cannot break out of the expression.
    assert "JSON.parse(" in js
    assert json.dumps(json.dumps(spec)) in js
    assert 'alert(1); //"' not in js          # the raw, unescaped form never appears


def test_query_and_next_page_return_safe_defaults_on_null():
    b, _ = _browser(out="null")
    assert b.query({}) == []
    assert b.next_page() == {"found": False, "exhausted": True}
    assert b.page_signature() == ""


def test_query_passes_the_spec_through_and_parses_hits():
    hits = [{"selector": "#a", "text": "Download", "kind": "download", "href": "http://x/a.pdf"}]
    b, run = _browser(out=json.dumps(hits))
    assert b.query({"kind": "download"}) == hits
    assert run.calls[0][0] == "eval"


def test_next_page_js_is_structural_first():
    js = B.next_page_js()
    assert "rel~=next" in js               # structural signals before any vocabulary
    assert "aria-disabled" in js
    assert js.index("rel~=next") < js.index("NEXT_VERB.test")


# --- resolve_tab ---------------------------------------------------------------

def test_resolve_tab_prefers_the_active_page_tab():
    run = FakeRun(script={"tab": (0, json.dumps([
        {"id": "worker", "type": "worker"},
        {"id": "t1", "type": "page", "status": "idle"},
        {"id": "t2", "type": "page", "status": "active"},
    ]), "")})
    assert B.resolve_tab("http://s", "tok", _run=run) == "t2"


def test_resolve_tab_falls_back_to_the_last_page_when_none_is_active():
    run = FakeRun(script={"tab": (0, json.dumps({"tabs": [
        {"id": "t1", "type": "page"}, {"id": "t2", "type": "page"}]}), "")})
    assert B.resolve_tab("http://s", "tok", _run=run) == "t2"


def test_resolve_tab_opens_a_new_tab_when_there_are_none():
    run = FakeRun(script={"tab": (0, "[]", ""), "nav": (0, "opened\nnewtab", "")})
    assert B.resolve_tab("http://s", "tok", _run=run) == "newtab"
    assert run.calls[-1][:2] == ["nav", "about:blank"]
    assert "--print-tab-id" in run.calls[-1]


def test_resolve_tab_returns_none_when_the_bridge_is_unreachable():
    run = FakeRun(rc=127, err="the `pinchtab` CLI is not on PATH")
    assert B.resolve_tab("http://s", "tok", _run=run) is None
