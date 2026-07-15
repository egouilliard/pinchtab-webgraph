"""End-to-end smoke test for SAFE, no-nav form reading in --single-url app-shell mode.

Unlike the browser-free unit tests, this drives the REAL interaction crawler through a
live, isolated PinchTab bridge against a persistent app-shell SPA fixture
(tests/e2e/single_url_shell_fixture.html) and asserts the single-URL create-trigger
handling behaves exactly as designed:

  - "New Widget" opens an IN-PLACE [role=dialog] via a JS-dispatch click (no navigation),
    so its form is read: >=2 fields captured, the "Create" submit button captured, and
    opensAt stays None (single-URL never records a nav target).
  - "New Report" performs a REAL full-page navigation to a chrome-less page, blanking the
    shell; the shell-blank guard must fire, so it is recorded with form=None / opensAt=None
    and the crawler does NOT scrape the blanked destination page as a form.
  - Read-only guarantee: the form's submit button is only CAPTURED, never clicked (the
    crawler Escapes). No graph state is a post-submit landing (opensAt is None), and no
    edge is a submit/create action — every edge is a plain nav kind. The fixture also
    carries a #submitted marker that stays empty for a manual eyeball after a live run.

It SKIPS cleanly (never fails) when the environment can't run it:
  - the `pinchtab` CLI isn't on PATH, or
  - no bridge is reachable at the server URL.
Point it at a bridge with PWG_E2E_SERVER (default http://localhost:9871). The bridge
auth token is read from $PINCHTAB_TOKEN, else from crawl-config.json at the repo root
(both gitignored). Bring a bridge up with scripts/start-crawl-browser.sh, then:
  PINCHTAB_CONFIG=crawl-config.json python3 -m pytest \\
      tests/e2e/test_single_url_form_read_e2e.py -q

NOTE: the crawler is a package using relative imports, so it must run as a MODULE
(python3 -m pinchtab_webgraph.interaction_crawl), never as a bare file path.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest

E2E_DIR = Path(__file__).parent
REPO_ROOT = E2E_DIR.parent.parent
SERVER = os.environ.get("PWG_E2E_SERVER", "http://localhost:9871")


def _reachable(url):
    p = urlparse(url)
    host, port = p.hostname or "127.0.0.1", p.port or 80
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return True
    except OSError:
        return False


def _token():
    tok = os.environ.get("PINCHTAB_TOKEN")
    if tok:
        return tok
    cfg = REPO_ROOT / "crawl-config.json"
    if cfg.exists():
        try:
            return json.load(open(cfg))["server"]["token"]
        except (KeyError, ValueError):
            return None
    return None


pytestmark = pytest.mark.skipif(
    shutil.which("pinchtab") is None or not _reachable(SERVER),
    reason="no pinchtab CLI or no reachable bridge at %s "
           "(start one with scripts/start-crawl-browser.sh)" % SERVER,
)


@pytest.fixture
def fixture_server():
    """Serve tests/e2e/ over HTTP on an ephemeral 127.0.0.1 port."""
    handler = partial(SimpleHTTPRequestHandler, directory=str(E2E_DIR))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield "http://127.0.0.1:%d" % httpd.server_address[1]
    finally:
        httpd.shutdown()


def test_single_url_reads_inplace_form_and_guards_shell_blank(fixture_server, tmp_path):
    out = tmp_path / "singleurl"
    fixture_url = "%s/single_url_shell_fixture.html" % fixture_server
    env = dict(os.environ)
    tok = _token()
    if tok:
        env["PINCHTAB_TOKEN"] = tok
    cfg = REPO_ROOT / "crawl-config.json"
    if cfg.exists():
        env.setdefault("PINCHTAB_CONFIG", str(cfg))

    # --single-url calls recipe.pin_tab, which needs an ALREADY-OPEN tab (it reads the
    # live page before any nav would pin one). Open the fixture in a tab first.
    open_tab = subprocess.run(
        ["pinchtab", "--server", SERVER, "nav", fixture_url],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=60,
    )
    assert open_tab.returncode == 0, \
        "failed to open fixture tab:\n%s\n%s" % (open_tab.stdout, open_tab.stderr)

    # Run as a MODULE (the package uses relative imports); --single-url + --read-forms.
    r = subprocess.run(
        [sys.executable, "-m", "pinchtab_webgraph.interaction_crawl",
         "--start", fixture_url, "--server", SERVER,
         "--single-url", "--read-forms", "--out", str(out)],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=180,
    )
    assert r.returncode == 0, "crawl failed:\n%s\n%s" % (r.stdout, r.stderr)

    graph = json.load(open("%s.json" % out))
    triggers = graph["triggers"]

    def find(label):
        hits = [t for t in triggers if t["label"].strip().lower() == label.lower()]
        assert hits, "no trigger labelled %r; got %r" % (
            label, [t["label"] for t in triggers])
        return hits[0]

    # (a) "New Widget": in-place dialog read via JS-dispatch click — NEVER a navigation.
    widget = find("New Widget")
    form = widget.get("form")
    assert form is not None, "New Widget form was not read: %r" % widget
    nfields = form.get("fieldCount")
    if nfields is None:
        nfields = len(form.get("fields") or [])
    assert nfields >= 2, "expected >=2 form fields, got %r: %r" % (nfields, form)
    # single-URL never records a nav target for a trigger it opened in place
    assert widget.get("opensAt") is None, "opensAt should be None: %r" % widget
    # the submit button was CAPTURED (read), not clicked — read-only proof
    assert "Create" in (form.get("submitButtons") or []), \
        "expected 'Create' in submitButtons: %r" % form.get("submitButtons")

    # (b) "New Report": a REAL navigation blanks the shell — the guard must fire, leaving
    # form/opensAt None and NOT scraping the blanked destination page as a form.
    report = find("New Report")
    assert report.get("form") is None, \
        "New Report should have form=None (shell-blank guard); got %r" % report.get("form")
    assert report.get("opensAt") is None, \
        "New Report opensAt should be None; got %r" % report.get("opensAt")

    # READ-ONLY across the graph: no submit/create action ever became an edge or a landing
    # state. Every recorded edge is a plain navigation kind (tab/link/menu/row/external/
    # iframe) — none is an "action"/submit — and no trigger recorded a post-open landing.
    NAV_KINDS = {"tab", "link", "menu", "row", "external", "iframe"}
    for e in graph["edges"]:
        assert e["kind"] in NAV_KINDS, "unexpected non-nav edge (submission?): %r" % e
    assert all(t.get("opensAt") is None for t in triggers), \
        "some trigger recorded a landing state (a submission?): %r" % triggers
