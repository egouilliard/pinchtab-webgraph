"""End-to-end smoke test for file-upload affordance extraction.

Unlike the browser-free structural/behavioral tests in tests/test_crawl_extract.py,
this drives the REAL crawler through a live, isolated PinchTab bridge against a
static HTML fixture (tests/e2e/upload_fixture.html) and asserts the produced graph
records every upload affordance — a bare <input type="file">, a file input hidden
behind a styled <label>, and an ondrop dropzone — with the accepted file types, and
that none of them were clicked (read-only guarantee: skipped=True edges).

It SKIPS cleanly (never fails) when the environment can't run it:
  - the `pinchtab` CLI isn't on PATH, or
  - no bridge is reachable at the server URL.
Point it at a bridge with PWG_E2E_SERVER (default http://localhost:9871). The bridge
auth token is read from $PINCHTAB_TOKEN, else from crawl-config.json at the repo root
(both gitignored). Bring a bridge up with scripts/start-crawl-browser.sh.
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


def test_crawl_extracts_upload_affordances(fixture_server, tmp_path):
    out = tmp_path / "upgraph"
    env = dict(os.environ)
    tok = _token()
    if tok:
        env["PINCHTAB_TOKEN"] = tok
    cfg = REPO_ROOT / "crawl-config.json"
    if cfg.exists():
        env.setdefault("PINCHTAB_CONFIG", str(cfg))

    r = subprocess.run(
        [sys.executable, str(REPO_ROOT / "pinchtab_webgraph" / "crawl.py"),
         "%s/upload_fixture.html" % fixture_server,
         "--server", SERVER, "--interaction-depth", "1",
         "--max-pages", "5", "--out", str(out)],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=180,
    )
    assert r.returncode == 0, "crawl failed:\n%s\n%s" % (r.stdout, r.stderr)

    graph = json.load(open("%s.json" % out))
    uploads = [n for n in graph["nodes"] if n.get("type") == "upload"]

    # EXACTLY four affordances, one node each — a visible <input> inside a <form>
    # (section d) must NOT also emit a coarse <form> node (duplicate-node regression).
    assert graph["meta"].get("uploads", 0) == 4, graph["meta"]
    assert len(uploads) == 4

    accepts = {n.get("accept", "") for n in uploads}
    assert ".pdf,.docx" in accepts          # bare visible <input type=file accept>
    assert "image/*" in accepts             # file input hidden behind a styled <label>
    assert "" in accepts                    # ondrop dropzone (no file-type whitelist)
    assert ".csv" in accepts                # visible <input> inside a <form>

    # accepted types surface in the human-facing node title
    pdf_node = next(n for n in uploads if n.get("accept") == ".pdf,.docx")
    assert ".pdf,.docx" in pdf_node["title"]

    # READ-ONLY: every upload affordance was recorded but never clicked.
    up_ids = {n["id"] for n in uploads}
    up_edges = [e for e in graph["edges"] if e["target"] in up_ids]
    assert up_edges, "no action edges point at the upload nodes"
    assert all(e.get("skipped") is True for e in up_edges)
    assert all(e["kind"] == "action" for e in up_edges)
