"""End-to-end test for the automation/flow UPLOAD path: goto → wait-for-input → upload.

The twin of test_flow_paginate_download_e2e.py, for the OTHER dangerous capability. Nothing
in the exercised path is mocked. This test:

  1. serves tests/e2e/upload_fixture.html over HTTP on 127.0.0.1 (a real file <input>, exactly
     the shape the doc-approval app exposes — a bare `#doc` picker);
  2. drives the REAL step VM (`runner.execute`) in-process with a REAL `PinchTabBrowser` and a
     REAL `ArtifactStore`: `goto` loads the page, then the `upload` op waits for the input to
     exist and sets a REAL local file on it through the live bridge;
  3. asserts the run reaches `ok`, the upload step recorded `ok`, and — reading the DOM back
     through the same live browser — the file input actually received exactly one file.

It is CRAWL-FREE: `upload` targets a CSS `selector`, so no interaction graph / goal resolution
is needed (graph_path stays None). `allow_upload` is granted on the flow; without it the op is
a safe no-op (`skipped`), which is the whole point of the capability.

It SKIPS cleanly (never fails) when the environment can't run it — the `pinchtab` CLI isn't on
PATH, or no bridge is reachable at the server URL — mirroring the download e2e. Point it at a
bridge with PWG_E2E_SERVER (default http://localhost:9871); the token is read from
$PINCHTAB_TOKEN, else from the gitignored crawl-config.json. The bridge's config MUST allow
uploads (security.allowUpload / an upload allowlist admitting 127.0.0.1).

To run against a DIFFERENT upload page (e.g. the real doc-approval SPA, which is not a stable
CI fixture), set PWG_UPLOAD_E2E_URL and optionally PWG_UPLOAD_E2E_SELECTOR (default `#doc`) —
then the local fixture server is not started and the flow drives that URL instead.
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

from pinchtab_webgraph import artifacts, browser as browser_mod, runner

E2E_DIR = Path(__file__).parent
REPO_ROOT = E2E_DIR.parent.parent
SERVER = os.environ.get("PWG_E2E_SERVER", "http://localhost:9871")

# Optional override: point at a real upload page instead of the bundled fixture.
OVERRIDE_URL = os.environ.get("PWG_UPLOAD_E2E_URL")
UPLOAD_SELECTOR = os.environ.get("PWG_UPLOAD_E2E_SELECTOR", "#doc")


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
def upload_page():
    """The URL of a page carrying a file <input>. Either an override (a real app) or the
    bundled fixture served over HTTP on an ephemeral 127.0.0.1 port."""
    if OVERRIDE_URL:
        yield OVERRIDE_URL
        return
    handler = partial(SimpleHTTPRequestHandler, directory=str(E2E_DIR))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield "http://127.0.0.1:%d/upload_fixture.html" % httpd.server_address[1]
    finally:
        httpd.shutdown()


def _upload_flow(url):
    return {
        "name": "e2e-upload-document",
        "host": urlparse(url).hostname,
        "capabilities": {"allow_upload": True},
        "steps": [
            {"op": "goto", "url": url},
            {"op": "upload", "selector": UPLOAD_SELECTOR, "file": "${file}"},
        ],
        "inputs": {"file": {"type": "file", "required": True}},
    }


def test_flow_uploads_a_real_file_to_a_file_input(upload_page, tmp_path):
    # a real file on disk for the picker to receive.
    doc = tmp_path / "Invoice Jan 2026 (final).pdf"
    doc.write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n")

    token = _token()
    tab = browser_mod.resolve_tab(SERVER, token)
    assert tab, "could not resolve a live tab on the bridge"
    live = browser_mod.PinchTabBrowser(SERVER, token, tab)

    flow = _upload_flow(upload_page)
    store = artifacts.ArtifactStore(scope="e2e-upload", root=str(tmp_path / "store"))

    # The effective capability is declared AND granted; upload is off by default, so the run
    # must be GRANTED allow_upload (the UI's "Allow upload" toggle) for the op to fire.
    result = runner.execute(flow, browser=live, graph_path=None, store=store,
                            inputs={"file": str(doc)}, grant={"allow_upload": True})

    assert result["status"] == "ok", json.dumps(result["steps"], indent=2)
    ups = [e for e in result["steps"] if e["op"] == "upload"]
    assert ups and ups[-1]["status"] == "ok", ups
    assert ups[-1]["file"] == str(doc)

    # read the DOM back through the SAME live browser: the input really holds one file, and
    # its extension survived the bridge's `upload-N.<ext>` rename.
    got = live.evaluate(
        "(() => { const i = document.querySelector(%s);"
        " return i && i.files ? {n: i.files.length,"
        " name: i.files.length ? i.files[0].name : null} : {n: -1}; })()"
        % json.dumps(UPLOAD_SELECTOR))
    assert got["n"] == 1, "the file input did not receive exactly one file: %r" % got
    assert str(got["name"]).lower().endswith(".pdf"), got


def test_upload_is_a_safe_noop_when_the_run_withholds_the_grant(upload_page, tmp_path):
    # SAFE BY DEFAULT: the flow DECLARES allow_upload (it must, or it would not even validate),
    # but the effective capability is declared AND granted — so a run that WITHHOLDS the grant
    # (the UI's "Allow upload" toggle left off) makes the very same op refuse to touch the page:
    # it records `skipped`, and the run still ends `ok`. This is the capability doing its job.
    doc = tmp_path / "x.pdf"
    doc.write_bytes(b"%PDF-1.4\n%%EOF\n")

    token = _token()
    tab = browser_mod.resolve_tab(SERVER, token)
    live = browser_mod.PinchTabBrowser(SERVER, token, tab)

    flow = _upload_flow(upload_page)                # declares allow_upload=True
    result = runner.execute(flow, browser=live, graph_path=None,
                            store=artifacts.ArtifactStore(scope="e2e-upload-noop",
                                                          root=str(tmp_path / "s")),
                            inputs={"file": str(doc)},
                            grant={"allow_upload": False})   # …but the run declines it
    assert result["status"] == "ok", json.dumps(result["steps"], indent=2)
    ups = [e for e in result["steps"] if e["op"] == "upload"]
    assert ups and ups[-1]["status"] == "skipped", ups
