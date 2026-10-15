"""FORM_JS scope selection, run in a REAL headless Chrome (no bridge needed).

Regression: FORM_JS scoped to `document.querySelector('form')` — the FIRST form in the
DOM. On app shells (MS Teams) that is the header `<form role="search">`, so every
view-swap form read returned just the search input (or nothing). The opened form must
be found structurally: a visible dialog, else the visible non-search <form> with the most
fields, else the document — and an ARIA search landmark is never part of it.

Each case embeds FORM_JS in a fixture page, loads it as a data: URL with
`--dump-dom`, and reads the result back from a body attribute.
"""
import base64
import html
import json
import re
import shutil
import subprocess

import pytest

from pinchtab_webgraph import recipe

CHROME = shutil.which("google-chrome") or shutil.which("chromium")
pytestmark = pytest.mark.skipif(CHROME is None, reason="google-chrome not installed")

SEARCH_HEADER = ('<header><form role="search">'
                 '<input type="search" aria-label="Search everything"></form></header>')


def run_form_js(body):
    page = ("<!doctype html><html><body>%s<script>"
            "document.body.setAttribute('data-out', JSON.stringify(%s));"
            "</script></body></html>") % (body, recipe.FORM_JS.strip())
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


def labels(form):
    return [f["label"] for f in form["fields"]]


def test_view_swap_form_ignores_header_search_form():
    out = run_form_js(SEARCH_HEADER + """
      <main><h2>New item</h2>
        <label for="t">Title</label><input id="t" type="text">
        <label for="d">Description</label><textarea id="d"></textarea>
        <button>Save</button></main>""")
    assert labels(out) == ["Title", "Description"]
    assert out["submitButtons"] == ["Save"]


def test_real_form_wins_over_first_search_form():
    out = run_form_js(SEARCH_HEADER + """
      <form><label for="n">Name</label><input id="n" type="text">
        <label for="e">Email</label><input id="e" type="email">
        <button type="submit">Create</button></form>""")
    assert labels(out) == ["Name", "Email"]
    assert out["submitButtons"] == ["Create"]


def test_dialog_still_takes_precedence():
    out = run_form_js(SEARCH_HEADER + """
      <form><label for="x">Page field</label><input id="x" type="text"></form>
      <div role="dialog"><h2>Add stage</h2>
        <label for="s">Stage name</label><input id="s" type="text">
        <button>Add</button></div>""")
    assert out["isDialog"] is True
    assert labels(out) == ["Stage name"]


def test_search_landmark_fields_never_counted_in_document_scope():
    out = run_form_js("""<div role="search"><input type="text" aria-label="Find people"></div>
      <main><label for="m">Meeting title</label><input id="m" type="text"></main>""")
    assert labels(out) == ["Meeting title"]
