"""EXTRACT_JS and the DOM click, run in a REAL headless Chrome (no bridge needed).

The runner/browser tests prove the VM and the argv; only a real engine can prove the DOM
semantics the extract op promises: trimmed innerText, form values, attributes, ABSOLUTE hrefs,
the case-insensitive pattern, and that a hostile selector stays inert text. A DOM click is
likewise only believable where `el.click()` really runs under an overlay.

One Chrome launch evaluates every case: each expression is embedded in a fixture page, and the
results come back through a body attribute (the same technique as test_form_js_scope.py).
"""
import base64
import html
import json
import re
import shutil
import subprocess

import pytest

from pinchtab_webgraph import browser as B

CHROME = shutil.which("google-chrome") or shutil.which("chromium")
pytestmark = pytest.mark.skipif(CHROME is None, reason="google-chrome not installed")

# <base> gives the page a real origin: a data: URL cannot resolve a relative href, and the
# extract op must hand back ABSOLUTE urls.
FIXTURE = """
<base href="https://app.example.com/dir/">
<h1>  Quarterly     plan </h1>
<ul>
  <li><a class="it" href="/m/1">First</a></li>
  <li><a class="it" href="m/2">Second</a></li>
  <li><a class="it">No href</a></li>
  <li><a class="it" href="https://other.example.org/x">Elsewhere</a></li>
</ul>
<input id="to" value="ana@example.com">
<input id="empty" value="">
<p class="note"></p>
<p class="note">  Hello   </p>
<a id="tag" href="#t" data-id="42">tag</a>
<button id="go" onclick="document.body.setAttribute('data-clicked', 'yes')">Go</button>
<div id="hidden" style="display:none">hidden</div>
<div id="zero" style="width:0;height:0;overflow:hidden"></div>
<div id="overlay" style="position:fixed;top:0;left:0;right:0;bottom:0"></div>
"""


def ex(selector, attr="text", all=False, pattern=None):
    return B.extract_js({"selector": selector, "attr": attr, "all": all, "pattern": pattern})


CASES = {
    "text_trimmed_first": ex("h1"),
    "text_all_skips_empty": ex(".note", all=True),
    "text_first_skips_empty": ex(".note"),
    "href_all_absolute": ex("a.it", "href", all=True),
    "href_first": ex("a.it", "href"),
    "href_pattern_ci": ex("a.it", "href", all=True, pattern="OTHER\\.EXAMPLE"),
    "href_pattern_none": ex("a.it", "href", all=True, pattern="^nothing"),
    "value": ex("#to", "value"),
    "value_empty": ex("#empty", "value"),
    "attr": ex("#tag", "data-id"),
    "attr_missing": ex("#tag", "data-nope"),
    "no_match": ex(".absent"),
    "bad_selector": ex("(("),
    "bad_pattern": ex("h1", pattern="("),
    "hostile_selector": ex("a\"); document.body.setAttribute('data-pwn','1'); (\""),
    "visible_button": B.element_visible_js("#go"),
    "visible_input": B.element_visible_js("#to"),
    "visible_display_none": B.element_visible_js("#hidden"),
    "visible_zero_box": B.element_visible_js("#zero"),
    "visible_missing": B.element_visible_js(".absent"),
    "visible_bad_selector": B.element_visible_js("(("),
    "old_probe_on_a_button": B.query_js({"selector": "#go", "limit": 1}),
    "old_probe_on_a_container": B.query_js({"selector": "ul", "limit": 1}),
    "click_under_overlay": B.dom_click_js("#go"),
    "click_missing": B.dom_click_js(".absent"),
    "click_bad_selector": B.dom_click_js("(("),
}


@pytest.fixture(scope="module")
def results():
    calls = "\n".join("R[%s] = %s;" % (json.dumps(k), v) for k, v in CASES.items())
    page = ("<!doctype html><html><body>%s<script>const R = {};\n%s\n"
            "R.__clicked = document.body.getAttribute('data-clicked');\n"
            "R.__pwn = document.body.getAttribute('data-pwn');\n"
            "document.body.setAttribute('data-out', JSON.stringify(R));</script></body></html>"
            % (FIXTURE, calls))
    url = "data:text/html;base64," + base64.b64encode(page.encode()).decode()
    try:
        r = subprocess.run([CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
                            "--dump-dom", url], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        pytest.skip("headless Chrome could not run here: %s" % e)
    m = re.search(r'data-out="([^"]*)"', r.stdout)
    if not m:
        pytest.skip("headless Chrome produced no DOM here (sandboxed?)")
    return json.loads(html.unescape(m.group(1)))


def test_text_is_trimmed_innertext(results):
    assert results["text_trimmed_first"] == {"matched": 1, "values": ["Quarterly plan"]}


def test_an_element_with_no_text_is_skipped_not_returned_as_empty(results):
    assert results["text_all_skips_empty"] == {"matched": 2, "values": ["Hello"]}
    assert results["text_first_skips_empty"] == {"matched": 2, "values": ["Hello"]}


def test_href_is_absolute_and_a_missing_href_is_skipped(results):
    assert results["href_all_absolute"] == {"matched": 4, "values": [
        "https://app.example.com/m/1",             # /root-relative against <base>
        "https://app.example.com/dir/m/2",         # path-relative against <base>
        "https://other.example.org/x"]}            # already absolute: unchanged
    assert results["href_first"]["values"] == ["https://app.example.com/m/1"]


def test_pattern_is_a_case_insensitive_filter_and_non_matches_are_skipped(results):
    assert results["href_pattern_ci"]["values"] == ["https://other.example.org/x"]
    assert results["href_pattern_none"] == {"matched": 4, "values": []}


def test_value_reads_the_form_value(results):
    assert results["value"]["values"] == ["ana@example.com"]
    assert results["value_empty"] == {"matched": 1, "values": []}     # empty = no value


def test_any_other_attr_reads_that_attribute(results):
    assert results["attr"]["values"] == ["42"]
    assert results["attr_missing"] == {"matched": 1, "values": []}


def test_a_selector_that_matches_nothing_reports_matched_zero(results):
    assert results["no_match"] == {"matched": 0, "values": []}


def test_a_bad_selector_or_pattern_comes_back_as_an_error_not_an_exception(results):
    assert "error" in results["bad_selector"] and "values" not in results["bad_selector"]
    assert "error" in results["bad_pattern"]


def test_a_hostile_selector_is_inert_text(results):
    assert "error" in results["hostile_selector"]      # an invalid selector, nothing more…
    assert results["__pwn"] is None                    # …and it never executed as code


def test_dom_click_fires_even_under_a_covering_overlay(results):
    # a coordinate click at the button's centre would land on #overlay and be swallowed;
    # el.click() dispatches on the element itself.
    assert results["click_under_overlay"] == {"ok": True}
    assert results["__clicked"] == "yes"


def test_dom_click_reports_a_miss_or_a_bad_selector_instead_of_throwing(results):
    assert results["click_missing"] == {"ok": False, "reason": "no element matches selector"}
    assert results["click_bad_selector"]["ok"] is False
    assert results["click_bad_selector"]["reason"]


def test_wait_selector_probe_sees_the_element_itself_where_the_old_probe_could_not(results):
    # THE regression: QUERY_JS takes `selector` as a ROOT and returns actionable DESCENDANTS,
    # so for a button / an input it finds nothing — a wait built on it never succeeded.
    assert results["old_probe_on_a_button"] == []
    assert results["visible_button"] is True
    assert results["visible_input"] is True
    # …and the old probe only "worked" for a container that happened to hold links.
    assert len(results["old_probe_on_a_container"]) == 1


def test_wait_selector_probe_is_false_for_hidden_zero_size_and_absent_elements(results):
    assert results["visible_display_none"] is False
    assert results["visible_zero_box"] is False
    assert results["visible_missing"] is False


def test_wait_selector_probe_reports_a_bad_selector_as_an_error(results):
    assert "error" in results["visible_bad_selector"]
