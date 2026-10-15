"""Tests for runner.py — the step VM.

The VM is exercised against a FakeBrowser implementing the full browser port (the same
injected-port idiom test_perform.py uses for the CLI), so loops, pagination, capability
gating and abort semantics are all covered with no bridge and no network.

The ONE thing that is NOT faked is the artifact store: the download tests run against a REAL
ArtifactStore(root=tmp_path), because the new-vs-dupe integration between the VM and the
content-hash ledger is exactly the behaviour worth proving.
"""
import json

import pytest

from pinchtab_webgraph import artifacts, browser as browser_mod, runner
from pinchtab_webgraph.browser import BrowserError


# --- the fake port -------------------------------------------------------------

class FakeBrowser:
    """Records every call; returns canned/scripted results. Implements the WHOLE port."""

    def __init__(self, *, query_results=None, next_pages=None, signatures=None,
                 content=None, fail=None, fetch_fails=False, download_fails=False,
                 evaluate_results=None, extract_results=None, visible_results=None):
        self.calls = []
        self.query_results = list(query_results or [])     # popped per query() call
        self.evaluate_results = list(evaluate_results or [])   # popped per evaluate() call
        self.extract_results = list(extract_results or [])  # popped per extract() call
        self.visible_results = list(visible_results or [])  # popped per element_visible() call
        self.next_pages = list(next_pages or [])           # popped per next_page() call
        self.signatures = list(signatures or [])           # popped per page_signature() call
        self._content = content or []
        self.fail = fail or {}                             # method -> BrowserError to raise
        self.fetch_fails = fetch_fails
        self.download_fails = download_fails
        self.saved = {}                                    # out_path -> bytes

    def _rec(self, name, *args):
        self.calls.append((name,) + args)
        if name in self.fail:
            raise self.fail[name]

    # mutating ops
    def nav(self, url):
        self._rec("nav", url)

    def open_tab(self, url):
        self._rec("open_tab", url)

    def close_opened_tabs(self):
        self._rec("close_opened_tabs")

    def click(self, selector):
        self._rec("click", selector)

    def fill(self, selector, value):
        self._rec("fill", selector, value)

    def select(self, selector, value):
        self._rec("select", selector, value)

    def check(self, selector):
        self._rec("check", selector)

    def click_js(self, selector):
        self._rec("click_js", selector)

    def focus(self, selector):
        self._rec("focus", selector)

    def press(self, key):
        self._rec("press", key)

    def keyboard_type(self, text):
        self._rec("keyboard_type", text)

    def upload(self, selector, path):
        self._rec("upload", selector, path)

    # downloads
    def fetch_bytes(self, url, timeout=180):
        self._rec("fetch_bytes", url)
        if self.fetch_fails:
            raise BrowserError("TypeError: Failed to fetch")
        return ("bytes-of:" + url).encode()

    def save_bytes(self, url, out_path, timeout=180):
        raw = self.fetch_bytes(url)                 # records the call + honours fetch_fails
        self.calls.append(("save_bytes", url, out_path))
        with open(out_path, "wb") as fh:
            fh.write(raw)
        self.saved[out_path] = raw
        return out_path

    def download(self, href, out_path):
        self._rec("download", href, out_path)
        if self.download_fails:
            raise BrowserError("400 unsafe URL: internal or blocked host")
        with open(out_path, "wb") as fh:
            fh.write(("cli-bytes-of:" + href).encode())
        return out_path

    # reads
    def evaluate(self, js, await_promise=False, timeout=None):
        # `upload` polls for its input to EXIST (an SPA has not rendered it when `goto`
        # returns) via a `document.querySelector(...)` probe. Default: the element is there.
        # Script `evaluate_results` to make it absent and exercise the timeout.
        self.calls.append(("evaluate", js))
        if self.evaluate_results:
            return self.evaluate_results.pop(0)
        return True

    def query(self, spec):
        self.calls.append(("query", spec))
        return self.query_results.pop(0) if self.query_results else []

    def element_visible(self, selector):
        # `wait {selector}`'s probe: is the element the selector NAMES present and visible?
        self.calls.append(("element_visible", selector))
        if "element_visible" in self.fail:
            raise self.fail["element_visible"]
        return self.visible_results.pop(0) if self.visible_results else False

    def extract(self, spec):
        # the port returns {matched, values}; default = the selector hit nothing.
        self.calls.append(("extract", spec))
        if "extract" in self.fail:
            raise self.fail["extract"]
        return (self.extract_results.pop(0) if self.extract_results
                else {"matched": 0, "values": []})

    def next_page(self):
        self.calls.append(("next_page",))
        return (self.next_pages.pop(0) if self.next_pages
                else {"found": False, "exhausted": True})

    def page_signature(self):
        self.calls.append(("page_signature",))
        return self.signatures.pop(0) if self.signatures else "sig"

    def content(self):
        self.calls.append(("content",))
        return self._content

    def url(self):
        self.calls.append(("url",))
        return "https://app.test/"


def _names(fb):
    return [c[0] for c in fb.calls]


MUTATING = {"nav", "open_tab", "click", "click_js", "fill", "select", "check", "upload", "download",
            "save_bytes", "fetch_bytes", "focus", "press", "keyboard_type"}


def _flow(steps, **kw):
    doc = {"name": "t", "steps": steps}
    doc.update(kw)
    return doc


def _run(fb, steps, *, store=None, graph_path=None, sleep=None, **kw):
    return runner.execute(_flow(steps, **kw.pop("flow_kw", {})), browser=fb, store=store,
                          graph_path=graph_path, sleep=sleep or (lambda s: None), **kw)


def _events(res, op=None, status=None):
    return [e for e in res["steps"]
            if (op is None or e["op"] == op) and (status is None or e["status"] == status)]


# --- basics --------------------------------------------------------------------

def test_goto_url_navigates_and_records():
    fb = FakeBrowser()
    res = _run(fb, [{"op": "goto", "url": "https://app.test/x"}])
    assert res["status"] == "ok"
    assert fb.calls == [("nav", "https://app.test/x")]
    assert _events(res, "goto", "ok")[0]["url"] == "https://app.test/x"


def test_a_url_that_leaves_the_declared_host_aborts():
    fb = FakeBrowser()
    res = _run(fb, [{"op": "goto", "url": "https://evil.test/x"}],
               flow_kw={"host": "app.test"})
    assert res["status"] == "aborted"
    assert "evil.test" in res["aborted"]
    assert fb.calls == []            # nothing was navigated


@pytest.mark.parametrize("url", [
    "https://evil.test\\@app.test/",     # Chrome: host evil.test; urlparse: host app.test
    "https:\\\\evil.test/x",             # no host for urlparse — used to SKIP the guard
    "javascript:alert(1)",
    "data:text/html,<script>1</script>",
    "https://app.test@evil.test/",       # userinfo hides the real host
    "https://app.test/ x",               # whitespace — parsers disagree
    "ftp://app.test/x",
])
def test_the_host_guard_refuses_urls_a_browser_could_read_differently(url):
    fb = FakeBrowser()
    res = _run(fb, [{"op": "goto", "url": url}], flow_kw={"host": "app.test"})
    assert res["status"] == "aborted"
    assert fb.calls == []            # nothing was navigated


@pytest.mark.parametrize("host, url", [
    ("app.test", "HTTPS://APP.test/x"),          # host match is case-insensitive
    ("*.app.test", "https://eu.app.test/x"),
    ("App.Test", "https://app.test/x"),
])
def test_the_host_guard_still_admits_its_own_host(host, url):
    fb = FakeBrowser()
    res = _run(fb, [{"op": "goto", "url": url}], flow_kw={"host": host})
    assert res["status"] == "ok"
    assert fb.calls == [("nav", url)]


def test_a_flow_without_a_host_is_not_guarded():
    fb = FakeBrowser()
    res = _run(fb, [{"op": "goto", "url": "https://anywhere.test/x"}])
    assert res["status"] == "ok"


def test_inputs_substitute_into_steps():
    fb = FakeBrowser()
    res = runner.execute(
        _flow([{"op": "fill", "selector": "#d", "value": "${since}"}],
              inputs={"since": {"type": "string"}}),
        browser=fb, inputs={"since": "2026-01-01"}, sleep=lambda s: None)
    assert ("fill", "#d", "2026-01-01") in fb.calls
    assert res["status"] == "ok"


def test_set_log_and_collect_flow_data_through_vars():
    fb = FakeBrowser(content=[{"kind": "table", "items": [{"a": 1}, {"a": 2}]},
                              {"kind": "list", "items": [{"a": 3}]}])
    res = _run(fb, [{"op": "set", "var": "n", "value": 2},
                    {"op": "log", "message": "n is ${n}"},
                    {"op": "collect", "into": "rows", "kind": "table"}])
    assert _events(res, "log")[0]["message"] == "n is 2"
    assert res["collected"]["rows"] == [{"a": 1}, {"a": 2}]     # `kind` filtered the list one


def test_wait_ms_sleeps_without_touching_the_browser():
    fb, slept = FakeBrowser(), []
    res = _run(fb, [{"op": "wait", "ms": 250}], sleep=slept.append)
    assert slept == [0.25]
    assert fb.calls == []
    assert _events(res, "wait", "ok")


def test_click_by_text_resolves_through_query():
    fb = FakeBrowser(query_results=[[{"selector": "#found", "text": "Go"}]])
    _run(fb, [{"op": "click", "text": "Go"}])
    assert ("click", "#found") in fb.calls


# --- capability AND-ing ---------------------------------------------------------

def test_upload_needs_the_flow_to_declare_AND_the_caller_to_grant():
    step = {"op": "upload", "selector": "#f", "file": "/x.pdf"}
    # flow declares, caller denies -> skipped
    fb = FakeBrowser()
    res = _run(fb, [step], flow_kw={"capabilities": {"allow_upload": True}},
               grant={"allow_upload": False})
    assert _events(res, "upload", "skipped")
    assert fb.calls == []
    # flow declares, caller grants -> runs
    fb = FakeBrowser()
    _run(fb, [step], flow_kw={"capabilities": {"allow_upload": True}},
         grant={"allow_upload": True})
    assert ("upload", "#f", "/x.pdf") in fb.calls


def test_a_flow_that_does_not_declare_upload_is_rejected_at_validate():
    # the flow-side veto happens in validate(), before a browser is ever touched
    fb = FakeBrowser()
    with pytest.raises(Exception):
        _run(fb, [{"op": "upload", "selector": "#f", "file": "/x.pdf"}],
             grant={"allow_upload": True})


def test_download_capability_can_be_withdrawn_by_the_caller():
    fb = FakeBrowser()
    res = _run(fb, [{"op": "download", "href": "https://app.test/a.pdf"}],
               grant={"allow_download": False})
    assert _events(res, "download", "skipped")
    assert fb.calls == []


def test_submit_is_gated_both_ways(two_downloads_graph_path):
    step = {"op": "do", "goal": "create team", "submit": True}
    fb = FakeBrowser()
    res = _run(fb, [step], graph_path=str(two_downloads_graph_path),
               flow_kw={"capabilities": {"allow_submit": True}}, grant={"allow_submit": False})
    assert _events(res, "do", "skipped")
    assert fb.calls == []


# --- upload: waiting for the file input to mount -------------------------------------

_UPLOAD = {"op": "upload", "selector": "#drop input[type=file]", "file": "/x.pdf"}
_UPLOAD_KW = dict(flow_kw={"capabilities": {"allow_upload": True}},
                  grant={"allow_upload": True})


def _evaluated(fb):
    return [c[1] for c in fb.calls if c[0] == "evaluate"]


def _fake_clock(monkeypatch, start=1000.0):
    """Freeze runner's clock and let the INJECTED sleep advance it: the poll loop's deadline
    is then exercised for real, deterministically, without a test that waits 10 seconds."""
    now = [start]
    monkeypatch.setattr(runner.time, "time", lambda: now[0])
    return now


def test_upload_polls_until_the_spa_mounts_its_input_then_uploads():
    # the dropzone is mounted AFTER the load event: the first two probes miss, the third hits.
    fb, slept = FakeBrowser(evaluate_results=[False, False, True]), []
    res = _run(fb, [_UPLOAD], sleep=slept.append, **_UPLOAD_KW)
    assert res["status"] == "ok"
    assert len(_evaluated(fb)) == 3                    # it POLLED — one probe is not enough
    assert slept == [0.2, 0.2]                         # …and paused between probes
    assert ("upload", "#drop input[type=file]", "/x.pdf") in fb.calls
    assert _events(res, "upload", "ok")[0]["file"] == "/x.pdf"


def test_upload_aborts_when_the_input_never_appears_and_never_uploads(monkeypatch):
    # the deadline is real (the injected sleep drives the clock), so this proves the timeout
    # branch rather than just an exhausted script — and it costs no wall-clock time.
    now = _fake_clock(monkeypatch)
    fb = FakeBrowser(evaluate_results=[False] * 500)
    res = _run(fb, [_UPLOAD], sleep=lambda s: now.__setitem__(0, now[0] + s), **_UPLOAD_KW)

    assert res["status"] == "aborted"
    assert "#drop input[type=file]" in res["aborted"]        # the message NAMES the selector
    assert "10000ms" in res["aborted"]
    assert not any(c[0] == "upload" for c in fb.calls)       # the upload never raced the DOM
    # bounded: ~10s of VIRTUAL time at 0.2s a poll, not 500 probes and not 10s of real waiting
    assert 45 <= len(_evaluated(fb)) <= 55
    assert fb.evaluate_results                               # it stopped on the deadline


@pytest.mark.parametrize("selector", [
    """input[name="a'b\\"c"]""",          # both quote flavours: naive concatenation breaks out
    r"input[data-p=\"x\"]",              # a backslash: naive escaping mangles it
])
def test_the_selector_crosses_into_js_as_a_json_string_not_concatenated(selector):
    # a selector can come from an AI-authored flow, so the probe must not be assembled by
    # gluing it into the JS source. json.dumps is the guard; this is its regression lock.
    fb = FakeBrowser()
    res = _run(fb, [dict(_UPLOAD, selector=selector)], **_UPLOAD_KW)
    assert res["status"] == "ok"
    js = _evaluated(fb)[0]
    assert json.dumps(selector) in js            # embedded ENCODED…
    assert selector not in js                    # …and never spliced in raw
    assert js.count("document.querySelector") == 1   # no injected second statement/call


# --- for_each --------------------------------------------------------------------

_ITEMS = [
    {"selector": "#a", "text": "Invoice A", "kind": "download", "href": "https://app.test/a.pdf"},
    {"selector": "#b", "text": "Invoice B", "kind": "download", "href": "https://app.test/b.pdf"},
]


def test_for_each_iterates_and_substitutes_the_item(tmp_path):
    fb = FakeBrowser(query_results=[_ITEMS])
    store = artifacts.ArtifactStore(scope="s", root=str(tmp_path))
    res = _run(fb, [{"op": "for_each", "match": {"kind": "download"}, "as": "item",
                     "body": [{"op": "download", "href": "${item.href}",
                               "name": "${item.text}.pdf"}]}], store=store)
    assert res["status"] == "ok"
    assert _events(res, "for_each", "ok")[0]["found"] == 2
    fetched = [c[1] for c in fb.calls if c[0] == "fetch_bytes"]
    assert fetched == ["https://app.test/a.pdf", "https://app.test/b.pdf"]
    assert [a["name"] for a in res["artifacts"]] == ["Invoice A.pdf", "Invoice B.pdf"]


def test_for_each_exposes_index_and_defaults_the_loop_var_to_item():
    fb = FakeBrowser(query_results=[_ITEMS])
    res = _run(fb, [{"op": "for_each", "match": {},
                     "body": [{"op": "log", "message": "${index}:${item.text}"}]}])
    assert [e["message"] for e in _events(res, "log")] == ["0:Invoice A", "1:Invoice B"]


def test_for_each_max_caps_both_the_query_and_the_iteration():
    fb = FakeBrowser(query_results=[_ITEMS])
    res = _run(fb, [{"op": "for_each", "match": {"kind": "download"}, "max": 1,
                     "body": [{"op": "log", "message": "${item.text}"}]}])
    spec = [c[1] for c in fb.calls if c[0] == "query"][0]
    assert spec["limit"] == 1                       # the cap is pushed down into the query…
    assert len(_events(res, "log")) == 1            # …and enforced on the results too


def test_for_each_with_no_hits_skips_its_body():
    fb = FakeBrowser(query_results=[[]])
    res = _run(fb, [{"op": "for_each", "match": {}, "body": [{"op": "click", "selector": "#x"}]}])
    assert _events(res, "for_each", "ok")[0]["found"] == 0
    assert not any(c[0] == "click" for c in fb.calls)


# --- paginate ----------------------------------------------------------------------

def test_paginate_walks_every_page_until_exhausted():
    fb = FakeBrowser(
        next_pages=[{"found": True, "exhausted": False, "selector": "#next"},
                    {"found": True, "exhausted": False, "selector": "#next"},
                    {"found": True, "exhausted": True, "selector": "#next"}],
        signatures=["p1", "p2"])
    res = _run(fb, [{"op": "paginate", "body": [{"op": "log", "message": "page ${page}"}]}])
    assert [e["message"] for e in _events(res, "log")] == ["page 1", "page 2", "page 3"]
    assert _events(res, "paginate", "ok")[0]["reason"] == "exhausted"
    assert [c[1] for c in fb.calls if c[0] == "click"] == ["#next", "#next"]


def test_paginate_stops_when_no_paginator_is_found():
    fb = FakeBrowser(next_pages=[{"found": False, "exhausted": True}])
    res = _run(fb, [{"op": "paginate", "body": [{"op": "log", "message": "${page}"}]}])
    ev = _events(res, "paginate", "ok")[0]
    assert ev["pages"] == 1 and ev["reason"] == "no paginator found"


def test_paginate_no_progress_guard_stops_a_decoy_paginator():
    # a "next" that never disables and never changes the content: without the guard the loop
    # burns its whole page budget re-reading page 1.
    fb = FakeBrowser(
        next_pages=[{"found": True, "exhausted": False, "selector": "#next"}] * 10,
        signatures=["same", "same", "same"])
    res = _run(fb, [{"op": "paginate", "max_pages": 10,
                     "body": [{"op": "log", "message": "${page}"}]}])
    ev = _events(res, "paginate", "ok")[0]
    assert ev["pages"] == 2                      # page 1 ran, page 2 ran, then it stopped
    assert "stopped changing" in ev["reason"]
    assert len(_events(res, "log")) == 2


def test_paginate_respects_max_pages():
    fb = FakeBrowser(
        next_pages=[{"found": True, "exhausted": False, "selector": "#next"}] * 10,
        signatures=["a", "b", "c", "d", "e"])
    res = _run(fb, [{"op": "paginate", "max_pages": 3,
                     "body": [{"op": "log", "message": "${page}"}]}])
    ev = _events(res, "paginate", "ok")[0]
    assert ev["pages"] == 3 and ev["reason"] == "hit max_pages"


def test_paginate_over_for_each_is_the_bulk_download_shape(tmp_path):
    store = artifacts.ArtifactStore(scope="s", root=str(tmp_path))
    page1 = [{"selector": "#a", "text": "A", "kind": "download", "href": "https://app.test/a.pdf"}]
    page2 = [{"selector": "#b", "text": "B", "kind": "download", "href": "https://app.test/b.pdf"}]
    fb = FakeBrowser(query_results=[page1, page2],
                     next_pages=[{"found": True, "exhausted": False, "selector": "#next"},
                                 {"found": True, "exhausted": True}],
                     signatures=["p1"])
    res = _run(fb, [{"op": "paginate", "body": [
        {"op": "for_each", "match": {"kind": "download"},
         "body": [{"op": "download", "href": "${item.href}", "name": "${item.text}.pdf"}]}]}],
        store=store)
    assert res["status"] == "ok"
    assert res["stats"]["artifacts_new"] == 2
    assert sorted(a["name"] for a in res["artifacts"]) == ["A.pdf", "B.pdf"]


# --- download: the store integration (a REAL ArtifactStore) -------------------------

def _store(tmp_path):
    return artifacts.ArtifactStore(scope="s", root=str(tmp_path / "store"))


def test_download_new_then_dupe_against_a_real_store(tmp_path):
    store = _store(tmp_path)
    step = {"op": "download", "href": "https://app.test/q3.pdf", "name": "q3.pdf"}

    fb = FakeBrowser()
    first = _run(fb, [step], store=store)
    rec = _events(first, "download")[0]
    assert rec["status"] == "new" and rec["via"] == "fetch"
    assert open(rec["path"], "rb").read() == b"bytes-of:https://app.test/q3.pdf"

    # a LATER run (a fresh store on the same root — the dedupe-across-runs contract)
    later = _run(FakeBrowser(), [step], store=artifacts.ArtifactStore(scope="s", root=store.root))
    assert _events(later, "download")[0]["status"] == "dupe"
    assert later["stats"]["artifacts_dupe"] == 1 and later["stats"]["artifacts_new"] == 0


def test_download_dedupe_none_readmits(tmp_path):
    store = _store(tmp_path)
    step = {"op": "download", "href": "https://app.test/q3.pdf", "name": "q3.pdf",
            "dedupe": "none"}
    _run(FakeBrowser(), [step], store=store)
    res = _run(FakeBrowser(), [step], store=store)
    assert _events(res, "download")[0]["status"] == "new"


def test_download_falls_back_to_the_cli_when_the_in_page_fetch_fails(tmp_path):
    # a CROSS-ORIGIN href: the in-page fetch throws TypeError, the CLI can still get it.
    store = _store(tmp_path)
    fb = FakeBrowser(fetch_fails=True)
    res = _run(fb, [{"op": "download", "href": "https://cdn.other/x.pdf", "name": "x.pdf"}],
               store=store)
    rec = _events(res, "download")[0]
    assert rec["status"] == "new" and rec["via"] == "cli"
    assert _names(fb) == ["fetch_bytes", "download"]        # fetch FIRST, cli as fallback
    assert open(rec["path"], "rb").read() == b"cli-bytes-of:https://cdn.other/x.pdf"


def test_download_errors_when_both_strategies_fail(tmp_path):
    fb = FakeBrowser(fetch_fails=True, download_fails=True)
    res = _run(fb, [{"op": "download", "href": "https://x.test/a.pdf"}], store=_store(tmp_path))
    assert res["status"] == "error"
    err = _events(res, "download", "error")[0]
    assert "unsafe URL" in err["error"]
    assert err["href"] == "https://x.test/a.pdf"


def test_js_triggered_download_is_clicked_and_never_hashed(tmp_path):
    fb = FakeBrowser()
    res = _run(fb, [{"op": "download", "selector": "#export"}], store=_store(tmp_path))
    ev = _events(res, "download")[0]
    assert ev["status"] == "triggered"
    assert fb.calls == [("click", "#export")]
    assert res["artifacts"] == []                 # honestly reported: we never saw the bytes


def test_download_without_a_store_still_fetches(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fb = FakeBrowser()
    res = _run(fb, [{"op": "download", "href": "https://app.test/q3.pdf"}])
    assert _events(res, "download", "ok")[0]["via"] == "fetch"
    assert open(str(tmp_path / "q3.pdf"), "rb").read().startswith(b"bytes-of:")


# --- goal resolution against a real crawled graph ------------------------------------

def test_goto_goal_walks_the_path_but_does_not_click_the_trigger(two_downloads_graph_path):
    fb = FakeBrowser()
    res = _run(fb, [{"op": "goto", "goal": "download the q3 report"}],
               graph_path=str(two_downloads_graph_path))
    assert res["status"] == "ok"
    # nav to the start, then nav the routing edge — and NOTHING else (goto positions; do acts)
    assert _names(fb) == ["nav", "nav"]
    assert fb.calls[-1][1] == "http://acme.test/reports.html"
    assert _events(res, "goto", "ok")[0]["target"] == "Download the Q3 report"


def test_goto_match_resolves_by_label_regex(two_downloads_graph_path):
    fb = FakeBrowser()
    res = _run(fb, [{"op": "goto", "match": "the logo"}],
               graph_path=str(two_downloads_graph_path))
    assert _events(res, "goto", "ok")[0]["target"] == "Download the logo"


def test_do_goal_runs_the_whole_compiled_block(two_downloads_graph_path, tmp_path,
                                               monkeypatch):
    monkeypatch.chdir(tmp_path)          # the compiled block's `-o` is a relative path
    fb = FakeBrowser()
    res = _run(fb, [{"op": "do", "goal": "download the q3 report"}],
               graph_path=str(two_downloads_graph_path))
    assert res["status"] == "ok"
    # nav start, nav reports, then the terminal download of the direct href
    assert ("download", "http://acme.test/files/q3-report.pdf",
            "q3-report.pdf") in fb.calls


def test_do_goal_honours_a_withdrawn_download_capability(two_downloads_graph_path, tmp_path,
                                                         monkeypatch):
    # the `do` path reuses the compiled block, and that block's terminal step is a DOWNLOAD.
    # Withdrawing the capability must veto it exactly as it vetoes the direct `download` op —
    # otherwise `--no-allow-download` silently writes the file anyway.
    monkeypatch.chdir(tmp_path)
    fb = FakeBrowser()
    res = _run(fb, [{"op": "do", "goal": "download the q3 report"}],
               graph_path=str(two_downloads_graph_path), grant={"allow_download": False})
    assert res["status"] == "ok"
    assert not (MUTATING & {"download", "save_bytes", "fetch_bytes"} & set(_names(fb)))
    assert _names(fb) == ["nav", "nav"]              # positioned, but the file was never taken
    assert list(tmp_path.iterdir()) == []            # …and nothing was written to disk
    assert _events(res, "do", "ok")[0]["filled"] == []


def test_do_form_fills_supplied_values_and_never_submits_by_default(two_downloads_graph_path):
    fb = FakeBrowser()
    res = _run(fb, [{"op": "do", "goal": "create team",
                     "set": {"Team name": "Acme"}}],       # matched case-insensitively
               graph_path=str(two_downloads_graph_path))
    ev = _events(res, "do", "ok")[0]
    assert ev["submitted"] is False
    assert ("fill", "#name", "Acme") in fb.calls
    assert ev["filled"] == ["Team name"]
    assert "Plan" in ev["skipped"]                          # no value supplied → never typed
    assert not any(c == ("click", "#save") for c in fb.calls)


def test_a_goal_step_without_a_graph_aborts_clearly():
    fb = FakeBrowser()
    res = _run(fb, [{"op": "goto", "goal": "anything"}])
    assert res["status"] == "aborted"
    assert "no graph was supplied" in res["aborted"]


def test_an_unresolvable_goal_aborts(two_downloads_graph_path):
    fb = FakeBrowser()
    res = _run(fb, [{"op": "goto", "goal": "frobnicate the widget"}],
               graph_path=str(two_downloads_graph_path))
    assert res["status"] == "aborted"
    assert "no_match" in res["aborted"]


# --- dry run ---------------------------------------------------------------------

def test_dry_run_makes_zero_mutating_browser_calls(two_downloads_graph_path, tmp_path):
    fb = FakeBrowser(query_results=[_ITEMS])
    res = _run(fb, [
        {"op": "goto", "goal": "download the q3 report"},
        {"op": "paginate", "max_pages": 3, "body": [
            {"op": "for_each", "match": {"kind": "download"}, "body": [
                {"op": "download", "href": "${item.href}"},
                {"op": "click", "selector": "#x"},
            ]}]},
        {"op": "fill", "selector": "#f", "value": "v"},
    ], graph_path=str(two_downloads_graph_path), store=_store(tmp_path), dry_run=True)
    assert res["status"] == "ok"
    assert res["dry_run"] is True
    assert not (set(_names(fb)) & MUTATING)
    assert all(e["status"] in ("dry-run", "started", "ok") for e in res["steps"])
    # the body is still previewed once, with a placeholder item
    assert _events(res, "download", "dry-run")[0]["href"] == "<item.href>"


# --- failure semantics -------------------------------------------------------------

def test_a_fatal_nav_failure_aborts_the_whole_run():
    fb = FakeBrowser(fail={"nav": BrowserError("tab not found", step_fatal=True)})
    res = _run(fb, [{"op": "goto", "url": "https://app.test/a"},
                    {"op": "click", "selector": "#never"}])
    assert res["status"] == "aborted"
    assert not any(c[0] == "click" for c in fb.calls)      # later steps never ran
    assert _events(res, "goto", "error")


def test_a_nonfatal_failure_records_an_error_and_keeps_going():
    fb = FakeBrowser(fail={"fill": BrowserError("no such element")})
    res = _run(fb, [{"op": "fill", "selector": "#f", "value": "v"},
                    {"op": "log", "message": "still here"}])
    assert res["status"] == "error"
    assert _events(res, "fill", "error")[0]["selector"] == "#f"
    assert _events(res, "log", "ok")                       # the run continued


def test_runaway_guard_aborts(monkeypatch):
    monkeypatch.setattr(runner, "MAX_TOTAL_STEPS", 3)
    fb = FakeBrowser(query_results=[[{"selector": "#a", "text": "x"}] * 10])
    res = _run(fb, [{"op": "for_each", "match": {},
                     "body": [{"op": "log", "message": "${item.text}"}]}])
    assert res["status"] == "aborted"
    assert "runaway" in res["aborted"]


def test_wait_for_a_selector_that_never_appears_errors():
    fb, slept = FakeBrowser(), []
    res = _run(fb, [{"op": "wait", "selector": "#gone", "timeout_ms": 1}], sleep=slept.append)
    assert _events(res, "wait", "error")[0]["target"] == "#gone"
    assert res["status"] == "error"


def test_wait_selector_succeeds_when_the_named_element_itself_is_visible():
    # regression: `wait {selector}` used to poll browser.query, which treats the selector as a
    # ROOT and returns actionable DESCENDANTS — so waiting on a button or input (no actionable
    # children) could never succeed. The probe is now the element itself, via element_visible.
    fb, slept = FakeBrowser(visible_results=[False, False, True]), []
    res = _run(fb, [{"op": "wait", "selector": "#some-button"}], sleep=slept.append)
    assert res["status"] == "ok"
    assert fb.calls == [("element_visible", "#some-button")] * 3      # polled, same selector
    assert slept == [0.2, 0.2]
    assert _events(res, "wait", "ok")[0]["target"] == "#some-button"
    assert not any(c[0] == "query" for c in fb.calls)                 # never the descendant query


def test_wait_selector_does_not_depend_on_the_descendant_query():
    # even a page whose control query would return NOTHING satisfies a selector wait.
    fb = FakeBrowser(query_results=[[]] * 10, visible_results=[True])
    res = _run(fb, [{"op": "wait", "selector": "#some-input"}])
    assert res["status"] == "ok" and fb.calls == [("element_visible", "#some-input")]


def test_wait_text_still_polls_the_control_query_by_label():
    fb, slept = FakeBrowser(query_results=[[], [{"selector": "#s", "text": "Send"}]]), []
    res = _run(fb, [{"op": "wait", "text": "Send"}], sleep=slept.append)
    assert res["status"] == "ok" and slept == [0.2]
    assert [c[0] for c in fb.calls] == ["query", "query"]
    assert fb.calls[0][1] == {"label": "Send", "limit": 1}      # literal label, unchanged
    assert "element_visible" not in _names(fb)


def test_wait_selector_with_a_bad_selector_fails_at_once_instead_of_timing_out():
    fb = FakeBrowser(fail={"element_visible": BrowserError("bad selector '((': invalid")})
    res = _run(fb, [{"op": "wait", "selector": "(("}])
    assert _events(res, "wait", "error")[0]["error"].startswith("BrowserError") is False
    assert "bad selector" in _events(res, "wait", "error")[0]["error"]
    assert [c[0] for c in fb.calls] == ["element_visible"]       # one probe, no 10s poll


def test_wait_selector_dry_run_probes_nothing():
    fb = FakeBrowser()
    res = _run(fb, [{"op": "wait", "selector": "#b"}], dry_run=True)
    assert fb.calls == [] and _events(res, "wait", "dry-run")[0]["target"] == "#b"


# --- the run record ------------------------------------------------------------------

def test_run_record_shape():
    fb = FakeBrowser()
    res = _run(fb, [{"op": "log", "message": "hi"}])
    assert set(res) >= {"status", "flow", "dry_run", "aborted", "duration_s", "steps",
                        "artifacts", "collected", "stats"}
    assert res["steps"][0] == {"op": "run", "status": "started", "flow": "t", "host": None,
                               "dry_run": False,
                               "capabilities": {"allow_submit": False, "allow_download": True,
                                                "allow_upload": False}}
    assert res["steps"][-1]["status"] == "ok"


def test_emit_streams_every_event_live():
    seen = []
    fb = FakeBrowser()
    res = runner.execute(_flow([{"op": "log", "message": "x"}]), browser=fb,
                         emit=seen.append, sleep=lambda s: None)
    assert seen == res["steps"]
    assert json.dumps(seen)          # every event is JSON-serializable (SSE / a run record)


# --- goto query: deep links that carry free text ------------------------------------

def test_goto_query_is_percent_encoded_and_appended():
    fb = FakeBrowser()
    res = _run(fb, [{"op": "goto", "url": "https://app.test/l",
                     "query": {"subject": "Q3 review & plan", "who": "Zoë"}}])
    final = "https://app.test/l?subject=Q3%20review%20%26%20plan&who=Zo%C3%AB"
    assert fb.calls == [("nav", final)]
    assert _events(res, "goto", "ok")[0]["url"] == final       # the event carries the FINAL url


def test_goto_query_values_are_substituted_first_then_encoded():
    # the input holds raw text full of URL metacharacters; encoding happens AFTER the ${} lookup,
    # so the text arrives as ONE intact parameter instead of splitting the link.
    fb = FakeBrowser()
    msg = "hi there & bye=1 #now"
    runner.execute(
        _flow([{"op": "goto", "url": "https://app.test/l", "query": {"m": "${msg}", "n": "${n}"}}],
              inputs={"msg": {"type": "string"}, "n": {"type": "integer"}}),
        browser=fb, inputs={"msg": msg, "n": 7}, sleep=lambda s: None)
    assert fb.calls == [("nav", "https://app.test/l?m=hi%20there%20%26%20bye%3D1%20%23now&n=7")]


def test_goto_query_appends_with_an_ampersand_when_the_url_has_a_query():
    fb = FakeBrowser()
    _run(fb, [{"op": "goto", "url": "https://app.test/l?a=1", "query": {"b": "x y"}}])
    assert fb.calls == [("nav", "https://app.test/l?a=1&b=x%20y")]


def test_goto_url_without_query_is_navigated_untouched():
    fb = FakeBrowser()
    _run(fb, [{"op": "goto", "url": "https://app.test/l?a=b c"}])
    assert fb.calls == [("nav", "https://app.test/l?a=b c")]


def test_goto_query_without_a_url_is_rejected_before_anything_runs():
    fb = FakeBrowser()
    with pytest.raises(Exception, match="only together with `url`"):
        _run(fb, [{"op": "goto", "goal": "invoices", "query": {"q": "x"}}])
    assert fb.calls == []


def test_goto_query_host_guard_checks_the_final_url(monkeypatch):
    seen = []
    real = runner.Runner._guard_host
    monkeypatch.setattr(runner.Runner, "_guard_host",
                        lambda self, url, run: (seen.append(url), real(self, url, run))[1])
    fb = FakeBrowser()
    _run(fb, [{"op": "goto", "url": "https://app.test/l", "query": {"q": "a b"}}],
         flow_kw={"host": "app.test"})
    assert seen == ["https://app.test/l?q=a%20b"]            # the guard saw the url as navigated
    assert fb.calls == [("nav", "https://app.test/l?q=a%20b")]


def test_goto_query_cannot_smuggle_a_different_host():
    # a value that LOOKS like a host change stays inert text after encoding…
    fb = FakeBrowser()
    res = _run(fb, [{"op": "goto", "url": "https://app.test/l",
                     "query": {"next": "https://evil.test/x@evil.test"}}],
               flow_kw={"host": "app.test"})
    assert res["status"] == "ok"
    assert fb.calls[0][1].startswith("https://app.test/l?next=https%3A%2F%2Fevil.test")
    # …and a url that really leaves the declared host is still refused, query or no query.
    fb = FakeBrowser()
    res = _run(fb, [{"op": "goto", "url": "https://evil.test/l", "query": {"q": "x"}}],
               flow_kw={"host": "app.test"})
    assert res["status"] == "aborted" and "evil.test" in res["aborted"]
    assert fb.calls == []


def test_goto_query_value_that_resolved_to_nothing_aborts_without_navigating():
    # an optional input that was not supplied binds None — a literal `None` in a link would be
    # a silent wrong answer, so the step refuses.
    fb = FakeBrowser()
    res = runner.execute(
        _flow([{"op": "goto", "url": "https://app.test/l", "query": {"q": "${q}"}}],
              inputs={"q": {"type": "string"}}),
        browser=fb, inputs={}, sleep=lambda s: None)
    assert res["status"] == "aborted"
    assert "query param 'q' has no value" in res["aborted"]
    assert fb.calls == []


def test_goto_query_dry_run_shows_the_final_url_and_navigates_nowhere():
    fb = FakeBrowser()
    res = _run(fb, [{"op": "goto", "url": "https://app.test/l", "query": {"q": "a b"}}],
               dry_run=True)
    assert _events(res, "goto", "dry-run")[0]["url"] == "https://app.test/l?q=a%20b"
    assert fb.calls == []


# --- type: focus, then REAL keystrokes ------------------------------------------------

def test_type_focuses_the_field_then_sends_keystrokes_not_a_value_set():
    fb = FakeBrowser()
    res = _run(fb, [{"op": "type", "selector": "#to", "value": "ana"}])
    assert fb.calls == [("focus", "#to"), ("keyboard_type", "ana")]    # in this order…
    assert "fill" not in _names(fb)                                    # …and never `fill`
    assert _events(res, "type", "ok")[0]["selector"] == "#to"


def test_type_resolves_a_label_the_way_fill_does():
    fb = FakeBrowser(query_results=[[{"selector": "#found", "text": "To"}]])
    _run(fb, [{"op": "type", "label": "To", "value": "ana"}])
    assert fb.calls[0][0] == "query" and fb.calls[0][1]["label"] == "To"
    assert fb.calls[1:] == [("focus", "#found"), ("keyboard_type", "ana")]


def test_type_substitutes_its_value():
    # (the port stringifies a non-string, exactly as it does for `fill` — see test_browser)
    fb = FakeBrowser()
    runner.execute(_flow([{"op": "type", "selector": "#n", "value": "id-${n}"}],
                         inputs={"n": {"type": "integer"}}),
                   browser=fb, inputs={"n": 42}, sleep=lambda s: None)
    assert fb.calls == [("focus", "#n"), ("keyboard_type", "id-42")]


def test_type_dry_run_touches_nothing():
    fb = FakeBrowser()
    res = _run(fb, [{"op": "type", "selector": "#to", "value": "ana"}], dry_run=True)
    assert fb.calls == []
    assert _events(res, "type", "dry-run")[0]["value"] == "ana"


def test_type_failure_is_a_recorded_error_and_the_run_continues():
    fb = FakeBrowser(fail={"focus": BrowserError("no such element")})
    res = _run(fb, [{"op": "type", "selector": "#to", "value": "ana"},
                    {"op": "log", "message": "still here"}])
    assert _events(res, "type", "error")[0]["selector"] == "#to"
    assert "keyboard_type" not in _names(fb)               # never typed into the wrong place
    assert _events(res, "log", "ok")


# --- press ------------------------------------------------------------------------------

def test_press_sends_the_key_to_the_focused_element():
    fb = FakeBrowser()
    res = _run(fb, [{"op": "press", "key": "Escape"}])
    assert fb.calls == [("press", "Escape")]
    assert _events(res, "press", "ok")[0]["key"] == "Escape"


def test_press_with_a_selector_focuses_it_first():
    fb = FakeBrowser()
    _run(fb, [{"op": "press", "key": "ArrowDown", "selector": "#combo"}])
    assert fb.calls == [("focus", "#combo"), ("press", "ArrowDown")]


def test_press_failed_focus_means_the_key_is_never_sent():
    fb = FakeBrowser(fail={"focus": BrowserError("no such element")})
    res = _run(fb, [{"op": "press", "key": "Enter", "selector": "#gone"}])
    assert "press" not in _names(fb)
    assert _events(res, "press", "error")


def test_press_rejects_a_combination_at_validate_time():
    fb = FakeBrowser()
    with pytest.raises(Exception, match="ONE key name"):
        _run(fb, [{"op": "press", "key": "Control+a"}])
    assert fb.calls == []


def test_press_rechecks_a_key_that_only_resolves_at_run_time():
    fb = FakeBrowser()
    res = runner.execute(_flow([{"op": "press", "key": "${k}"}],
                               inputs={"k": {"type": "string"}}),
                         browser=fb, inputs={"k": "Control+a"}, sleep=lambda s: None)
    assert res["status"] == "aborted" and "ONE key name" in res["aborted"]
    assert fb.calls == []


def test_press_dry_run_touches_nothing():
    fb = FakeBrowser()
    res = _run(fb, [{"op": "press", "key": "Enter", "selector": "#b"}], dry_run=True)
    assert fb.calls == []
    assert _events(res, "press", "dry-run")[0]["key"] == "Enter"


# --- extract ------------------------------------------------------------------------------

def _ex(*values, matched=None):
    """What the port's extract() returns: {matched, values}."""
    return {"matched": len(values) if matched is None else matched, "values": list(values)}


def test_extract_binds_the_first_value_for_later_steps_and_into_outputs():
    fb = FakeBrowser(extract_results=[_ex("Quarterly plan")])
    res = _run(fb, [{"op": "extract", "into": "title", "selector": "h1"},
                    {"op": "log", "message": "got ${title}"}])
    assert res["status"] == "ok"
    assert _events(res, "log")[0]["message"] == "got Quarterly plan"      # ${into} is bound
    assert res["outputs"] == {"title": "Quarterly plan"}                 # …and recorded
    ev = _events(res, "extract", "ok")[0]
    assert ev["into"] == "title" and ev["value"] == "Quarterly plan" and ev["count"] == 1
    # the spec crossed the port with the documented defaults
    assert fb.calls == [("extract", {"selector": "h1", "attr": "text", "all": False,
                                     "pattern": None})]


def test_extract_without_all_keeps_only_the_first_match():
    fb = FakeBrowser(extract_results=[_ex("a", "b", "c")])
    res = _run(fb, [{"op": "extract", "into": "x", "selector": "li"}])
    assert res["outputs"] == {"x": "a"}


def test_extract_all_binds_a_list_of_every_match():
    fb = FakeBrowser(extract_results=[_ex("a", "b", "c")])
    res = _run(fb, [{"op": "extract", "into": "xs", "selector": "li", "all": True},
                    {"op": "log", "message": "${xs}"}])
    assert res["outputs"] == {"xs": ["a", "b", "c"]}
    assert fb.calls[0][1]["all"] is True
    ev = _events(res, "extract", "ok")[0]
    assert ev["count"] == 3 and "value" not in ev       # the list lives in outputs, not the log


def test_extract_passes_attr_and_pattern_to_the_page():
    fb = FakeBrowser(extract_results=[_ex("https://app.test/m/42")])
    res = _run(fb, [{"op": "extract", "into": "link", "selector": "a.item", "attr": "href",
                     "pattern": "/m/\\d+$"}])
    assert fb.calls == [("extract", {"selector": "a.item", "attr": "href", "all": False,
                                     "pattern": "/m/\\d+$"})]
    assert res["outputs"] == {"link": "https://app.test/m/42"}


def test_an_extracted_href_can_feed_a_goto_through_the_host_guard():
    fb = FakeBrowser(extract_results=[_ex("https://app.test/m/42")])
    _run(fb, [{"op": "extract", "into": "link", "selector": "a", "attr": "href"},
              {"op": "goto", "url": "${link}"}], flow_kw={"host": "app.test"})
    assert fb.calls[-1] == ("nav", "https://app.test/m/42")
    fb = FakeBrowser(extract_results=[_ex("https://evil.test/m/42")])
    res = _run(fb, [{"op": "extract", "into": "link", "selector": "a", "attr": "href"},
                    {"op": "goto", "url": "${link}"}], flow_kw={"host": "app.test"})
    assert res["status"] == "aborted"            # a scraped link is no more trusted than an input
    assert not any(c[0] == "nav" for c in fb.calls)


def test_extract_with_nothing_found_is_an_error_by_default_and_binds_nothing():
    fb = FakeBrowser(extract_results=[_ex(matched=0)])
    res = _run(fb, [{"op": "extract", "into": "title", "selector": "h1.missing"},
                    {"op": "log", "message": "after"}])
    ev = _events(res, "extract", "error")[0]
    assert ev["into"] == "title" and ev["selector"] == "h1.missing"
    assert "no element matches selector 'h1.missing'" in ev["error"]
    assert res["status"] == "error"
    assert res["outputs"] == {}
    assert _events(res, "log", "ok")              # a step error, like `wait`: the run goes on


def test_extract_error_says_when_elements_matched_but_none_fitted():
    fb = FakeBrowser(extract_results=[_ex(matched=4)])
    res = _run(fb, [{"op": "extract", "into": "id", "selector": "a", "attr": "href",
                     "pattern": "^https://x"}])
    err = _events(res, "extract", "error")[0]["error"]
    assert "4 element(s) matched" in err and "usable href" in err and "/^https://x/i" in err


def test_a_later_step_that_needs_an_unbound_extract_aborts_loudly():
    fb = FakeBrowser(extract_results=[_ex(matched=0)])
    res = _run(fb, [{"op": "extract", "into": "link", "selector": "a", "attr": "href"},
                    {"op": "goto", "url": "${link}"}])
    assert res["status"] == "aborted" and "unknown variable ${link}" in res["aborted"]
    assert not any(c[0] == "nav" for c in fb.calls)      # never navigated to a guess


@pytest.mark.parametrize("extra", [{}, {"all": True}])
def test_extract_not_required_binds_null_instead_of_failing(extra):
    fb = FakeBrowser(extract_results=[_ex(matched=0)])
    res = _run(fb, [dict({"op": "extract", "into": "maybe", "selector": "p", "required": False},
                         **extra),
                    {"op": "set", "var": "seen", "value": "${maybe}"}])
    assert res["status"] == "ok"
    assert res["outputs"] == {"maybe": None}       # null, for a list-valued extract too
    assert _events(res, "extract", "ok")[0]["count"] == 0


def test_extract_required_true_is_the_default_and_false_must_be_explicit():
    fb = FakeBrowser(extract_results=[_ex(matched=0)])
    res = _run(fb, [{"op": "extract", "into": "t", "selector": "p", "required": True}])
    assert _events(res, "extract", "error")


def test_extract_page_error_is_a_recorded_step_error_and_the_run_continues():
    fb = FakeBrowser(fail={"extract": BrowserError("extract failed: invalid selector")})
    res = _run(fb, [{"op": "extract", "into": "t", "selector": "(("},
                    {"op": "log", "message": "after"}])
    assert "invalid selector" in _events(res, "extract", "error")[0]["error"]
    assert _events(res, "log", "ok") and res["outputs"] == {}


def test_extract_selector_is_substituted_per_loop_item():
    fb = FakeBrowser(query_results=[_ITEMS], extract_results=[_ex("A"), _ex("B")])
    res = _run(fb, [{"op": "for_each", "match": {}, "body": [
        {"op": "extract", "into": "row", "selector": "${item.selector} td"}]}])
    assert [c[1]["selector"] for c in fb.calls if c[0] == "extract"] == ["#a td", "#b td"]
    assert res["outputs"] == {"row": "B"}        # last write wins, like any variable


def test_extract_dry_run_reads_nothing_and_previews_later_steps():
    fb = FakeBrowser()
    res = _run(fb, [{"op": "extract", "into": "link", "selector": "a", "attr": "href"},
                    {"op": "goto", "url": "https://app.test/m?u=${link}"}], dry_run=True)
    assert res["status"] == "ok"                 # ${link} resolves to a placeholder, no abort
    assert fb.calls == []
    assert _events(res, "extract", "dry-run")[0]["into"] == "link"
    assert res["outputs"] == {}                  # nothing was read, so nothing is an output
    assert _events(res, "goto", "dry-run")[0]["url"] == "https://app.test/m?u=<link>"


def test_a_run_without_extracts_has_empty_outputs():
    res = _run(FakeBrowser(), [{"op": "log", "message": "x"}])
    assert res["outputs"] == {}


# --- submit-gated click / press ----------------------------------------------------------------

_SUBMIT_CAPS = {"capabilities": {"allow_submit": True}}


@pytest.mark.parametrize("step", [
    {"op": "click", "selector": "#send", "submit": True},
    {"op": "press", "key": "Enter", "selector": "#box", "submit": True},
])
def test_submit_marked_steps_are_skipped_with_a_reason_without_the_callers_grant(step):
    fb = FakeBrowser()
    res = _run(fb, [step, {"op": "log", "message": "after"}], flow_kw=_SUBMIT_CAPS,
               grant={"allow_submit": False})
    ev = _events(res, step["op"], "skipped")[0]
    assert ev["submit"] is True and "allow_submit" in ev["reason"]
    assert fb.calls == []                         # not clicked, not focused, not pressed
    assert _events(res, "log", "ok")              # recorded, never silently dropped
    assert res["status"] == "ok"


@pytest.mark.parametrize("step", [
    {"op": "click", "selector": "#send", "submit": True},
    {"op": "press", "key": "Enter", "submit": True},
])
def test_submit_marked_steps_default_to_withheld(step):
    # no grant argument at all = the safe default: the caller has granted nothing.
    fb = FakeBrowser()
    res = _run(fb, [step], flow_kw=_SUBMIT_CAPS)
    assert _events(res, step["op"], "skipped") and fb.calls == []


def test_submit_marked_click_runs_when_declared_and_granted():
    fb = FakeBrowser()
    res = _run(fb, [{"op": "click", "selector": "#send", "submit": True}],
               flow_kw=_SUBMIT_CAPS, grant={"allow_submit": True})
    assert fb.calls == [("click", "#send")]
    assert _events(res, "click", "ok")[0]["submit"] is True


def test_submit_marked_press_runs_when_declared_and_granted():
    fb = FakeBrowser()
    res = _run(fb, [{"op": "press", "key": "Enter", "selector": "#box", "submit": True}],
               flow_kw=_SUBMIT_CAPS, grant={"allow_submit": True})
    assert fb.calls == [("focus", "#box"), ("press", "Enter")]
    assert _events(res, "press", "ok")[0]["submit"] is True


def test_an_unmarked_click_and_press_need_no_grant():
    fb = FakeBrowser()
    res = _run(fb, [{"op": "click", "selector": "#a"}, {"op": "press", "key": "Tab"}])
    assert fb.calls == [("click", "#a"), ("press", "Tab")]
    assert not _events(res, status="skipped")


def test_a_gated_click_by_text_does_not_even_query_the_page():
    fb = FakeBrowser(query_results=[[{"selector": "#x", "text": "Send"}]])
    res = _run(fb, [{"op": "click", "text": "Send", "submit": True}], flow_kw=_SUBMIT_CAPS)
    assert fb.calls == []                         # not even the by-text control lookup
    assert _events(res, "click", "skipped")[0]["text"] == "Send"


def test_a_flow_that_does_not_declare_allow_submit_is_rejected_at_validate():
    # the flow-side veto, before a browser is ever touched — even if the caller would grant it.
    fb = FakeBrowser()
    for step in ({"op": "click", "selector": "#s", "submit": True},
                 {"op": "press", "key": "Enter", "submit": True}):
        with pytest.raises(Exception, match="allow_submit"):
            _run(fb, [step], grant={"allow_submit": True})
    assert fb.calls == []


@pytest.mark.parametrize("step", [
    {"op": "click", "selector": "#send", "submit": True},
    {"op": "press", "key": "Enter", "submit": True},
])
def test_dry_run_shows_a_submit_step_as_a_gated_write_without_touching_the_page(step):
    # same grant a live run would get: withheld -> shown as skipped (the gate), no browser call
    fb = FakeBrowser()
    res = _run(fb, [step], flow_kw=_SUBMIT_CAPS, dry_run=True)
    assert _events(res, step["op"], "skipped")[0]["reason"].startswith("submit not permitted")
    assert fb.calls == []
    # granted -> shown as a dry-run WRITE (submit=True) that would run
    fb = FakeBrowser()
    res = _run(fb, [step], flow_kw=_SUBMIT_CAPS, grant={"allow_submit": True}, dry_run=True)
    assert _events(res, step["op"], "dry-run")[0]["submit"] is True
    assert not (set(_names(fb)) & MUTATING)


# --- click js: a DOM click for a click an overlay would swallow ---------------------------------

def test_click_js_dispatches_in_the_page_and_not_through_the_cli_click():
    fb = FakeBrowser()
    res = _run(fb, [{"op": "click", "selector": "#go", "js": True}])
    assert fb.calls == [("click_js", "#go")]
    assert "click" not in _names(fb)
    assert _events(res, "click", "ok")[0]["js"] is True


def test_click_without_js_is_unchanged():
    fb = FakeBrowser()
    res = _run(fb, [{"op": "click", "selector": "#go", "js": False}])
    assert fb.calls == [("click", "#go")]
    assert "js" not in _events(res, "click", "ok")[0]


def test_click_js_by_text_resolves_the_selector_then_clicks_in_page():
    fb = FakeBrowser(query_results=[[{"selector": "#found", "text": "Go"}]])
    _run(fb, [{"op": "click", "text": "Go", "js": True}])
    assert fb.calls[-1] == ("click_js", "#found")


def test_click_js_that_matches_nothing_is_step_fatal_like_a_failed_click():
    fb = FakeBrowser(fail={"click_js": BrowserError("js click failed: no element matches "
                                                    "selector '#gone'", step_fatal=True)})
    res = _run(fb, [{"op": "click", "selector": "#gone", "js": True},
                    {"op": "log", "message": "never"}])
    assert res["status"] == "aborted"
    assert _events(res, "click", "error")
    assert not _events(res, "log")                # later steps never ran on the wrong page


def test_click_js_combines_with_submit_gating():
    step = {"op": "click", "selector": "#send", "submit": True, "js": True}
    fb = FakeBrowser()
    res = _run(fb, [step], flow_kw=_SUBMIT_CAPS, grant={"allow_submit": False})
    assert fb.calls == [] and _events(res, "click", "skipped")        # gate unchanged
    fb = FakeBrowser()
    res = _run(fb, [step], flow_kw=_SUBMIT_CAPS, grant={"allow_submit": True})
    assert fb.calls == [("click_js", "#send")]
    ev = _events(res, "click", "ok")[0]
    assert ev["submit"] is True and ev["js"] is True


def test_click_js_dry_run_touches_nothing():
    fb = FakeBrowser()
    res = _run(fb, [{"op": "click", "selector": "#go", "js": True}], dry_run=True)
    assert fb.calls == []
    assert _events(res, "click", "dry-run")[0]["js"] is True


# --- goto {new_tab} ----------------------------------------------------------------

def test_goto_new_tab_opens_a_fresh_tab_and_the_run_closes_it():
    fb = FakeBrowser()
    res = _run(fb, [{"op": "goto", "url": "https://app.test/l/x", "new_tab": True},
                    {"op": "click", "selector": "#b"}], flow_kw={"host": "app.test"})
    assert res["status"] == "ok"
    assert fb.calls == [("open_tab", "https://app.test/l/x"), ("click", "#b"),
                        ("close_opened_tabs",)]
    assert _events(res, "goto", "ok")[0]["new_tab"] is True


def test_opened_tabs_are_closed_even_when_the_run_aborts():
    fb = FakeBrowser(fail={"click": BrowserError("gone", step_fatal=True)})
    res = _run(fb, [{"op": "goto", "url": "https://app.test/", "new_tab": True},
                    {"op": "click", "selector": "#b"}])
    assert res["status"] == "aborted"
    assert fb.calls[-1] == ("close_opened_tabs",)


def test_goto_new_tab_is_host_guarded_and_dry_run_touches_nothing():
    fb = FakeBrowser()
    res = _run(fb, [{"op": "goto", "url": "https://evil.test/", "new_tab": True}],
               flow_kw={"host": "app.test"})
    assert res["status"] == "aborted" and ("open_tab", "https://evil.test/") not in fb.calls
    fb = FakeBrowser()
    res = runner.execute(_flow([{"op": "goto", "url": "https://app.test/", "new_tab": True}]),
                         browser=fb, dry_run=True, sleep=lambda s: None)
    assert fb.calls == []


@pytest.mark.parametrize("step, msg", [
    ({"op": "goto", "goal": "x", "new_tab": True}, "only together with `url`"),
    ({"op": "goto", "url": "https://a.test/", "new_tab": "yes"}, "true or false"),
])
def test_goto_new_tab_validation(step, msg):
    from pinchtab_webgraph import flow as flow_mod
    with pytest.raises(flow_mod.FlowError) as exc:
        flow_mod.validate(_flow([step]))
    assert msg in str(exc.value)
