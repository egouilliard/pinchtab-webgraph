"""End-to-end (REAL browser): structural auto-detection of single-URL app-shell mode.

Drives the REAL `detect_single_url()` through a live PinchTab bridge + Chrome against two
static fixtures served on 127.0.0.1 — the only faithful test of the heuristic, since it
reads a real DOM, evaluates real JS, and performs real clicks:

  - single_url_fix/appshell.html    — a tablist that swaps the view IN PLACE while the URL
                                       never changes  → MUST detect single-URL (app-shell)
  - single_url_fix/multi/index.html — real <a href> nav that changes the URL PATH
                                       → MUST detect normal nav mode

The browser-free `tests/test_single_url_detect.py` covers the decision logic against a fake
bridge; this file proves the same logic holds against a real browser end-to-end.

It SKIPS cleanly (never fails) when the environment can't run it:
  - the `pinchtab` CLI isn't on PATH, or
  - no bridge is reachable at the server URL.
Point it at a bridge with PWG_E2E_SERVER (default http://localhost:9871). The bridge auth
token is read from $PINCHTAB_TOKEN, else from crawl-config.json at the repo root. Bring a
bridge up with scripts/start-crawl-browser.sh, then:
  python3 -m pytest tests/e2e/test_single_url_detect_e2e.py -q
"""
import json
import os
import shutil
import socket
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest

from pinchtab_webgraph import interaction_crawl as ic

E2E_DIR = Path(__file__).parent
FIX_DIR = E2E_DIR / "single_url_fix"
REPO_ROOT = E2E_DIR.parent.parent
SERVER = os.environ.get("PWG_E2E_SERVER", "http://localhost:9871")


def _reachable(url):
    p = urlparse(url)
    try:
        with socket.create_connection((p.hostname or "127.0.0.1", p.port or 80), timeout=1.5):
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
    reason="no pinchtab CLI / no reachable bridge at %s (start one with "
           "scripts/start-crawl-browser.sh)" % SERVER,
)


@pytest.fixture
def fixture_server():
    """Serve tests/e2e/single_url_fix/ over HTTP on an ephemeral 127.0.0.1 port."""
    handler = partial(SimpleHTTPRequestHandler, directory=str(FIX_DIR))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield "http://127.0.0.1:%d" % httpd.server_address[1]
    finally:
        httpd.shutdown()


@pytest.fixture(autouse=True)
def _token_env(monkeypatch):
    tok = _token()
    if tok:
        monkeypatch.setenv("PINCHTAB_TOKEN", tok)


def test_appshell_fixture_detected_as_single_url(fixture_server):
    # Views swap in place, URL never changes → app-shell → single-URL mode.
    url = "%s/appshell.html" % fixture_server
    ic.nav(url, SERVER)                       # open it live (the operator's starting view)
    assert ic.detect_single_url(SERVER, url) is True


def test_multipage_fixture_detected_as_nav_mode(fixture_server):
    # A nav link changes the URL path → URL-primary routing → normal nav mode.
    url = "%s/multi/index.html" % fixture_server
    ic.nav(url, SERVER)
    assert ic.detect_single_url(SERVER, url) is False
