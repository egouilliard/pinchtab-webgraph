"""End-to-end smoke test for the /ws/crawl UI route (Phase 3: crawl from the web UI).

Drives the REAL /ws/crawl WebSocket, which spawns the REAL interaction crawler through a
live, isolated PinchTab bridge against a static 2-page HTML fixture (the existing
tests/e2e/upload_fixture.html <-> other.html cross-link), streams progress to a `done`
frame, and asserts the produced interaction-graph JSON was PROMOTED into the cache at
cache_store.cache_path(host).

It SKIPS cleanly (never fails) when the environment can't run it:
  - the `pinchtab` CLI isn't on PATH, or
  - no bridge is reachable at the server URL, or
  - PINCHTAB_WEBGRAPH_ENABLE_CRAWL is not set (the feature is off by default).
Point it at a bridge with PWG_E2E_SERVER (default http://localhost:9871). The bridge
auth token is read from $PINCHTAB_TOKEN, else from crawl-config.json at the repo root.
Bring a bridge up with scripts/start-crawl-browser.sh, then:
  PINCHTAB_WEBGRAPH_ENABLE_CRAWL=1 PINCHTAB_CONFIG=crawl-config.json \\
      python3 -m pytest tests/e2e/test_ui_crawl_e2e.py -q
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
    shutil.which("pinchtab") is None or not _reachable(SERVER)
    or not os.environ.get("PINCHTAB_WEBGRAPH_ENABLE_CRAWL"),
    reason="no pinchtab CLI / no reachable bridge at %s / crawl disabled "
           "(set PINCHTAB_WEBGRAPH_ENABLE_CRAWL=1 and start a bridge with "
           "scripts/start-crawl-browser.sh)" % SERVER,
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


def test_ui_crawl_promotes_graph(fixture_server, tmp_path, monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    # Isolate the cache under a tmp home so we never touch the real ~/.pinchtab-webgraph.
    monkeypatch.setenv("PINCHTAB_WEBGRAPH_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PINCHTAB_WEBGRAPH_BRIDGE", SERVER)
    tok = _token()
    if tok:
        monkeypatch.setenv("PINCHTAB_TOKEN", tok)
    cfg = REPO_ROOT / "crawl-config.json"
    if cfg.exists():
        monkeypatch.setenv("PINCHTAB_CONFIG", str(cfg))

    from pinchtab_webgraph import cache_store
    from pinchtab_webgraph.ui import server as ui_server

    start_url = "%s/upload_fixture.html" % fixture_server
    host = urlparse(start_url).hostname            # "127.0.0.1"

    client = TestClient(ui_server.app)
    q = "/ws/crawl?url=%s&max_states=5&max_depth=2" % start_url
    terminal = None
    with client.websocket_connect(q) as ws:
        first = ws.receive_json()
        assert first["type"] == "status" and first["state"] == "starting"
        assert first["host"] == host
        for _ in range(2000):                      # bounded read to a terminal frame
            f = ws.receive_json()
            if f["type"] in ("done", "cancelled", "error"):
                terminal = f
                break

    assert terminal is not None, "no terminal frame from /ws/crawl"
    assert terminal["type"] == "done", "crawl did not complete: %r" % terminal
    assert terminal["host"] == host
    assert terminal["states"] >= 1

    # the graph was promoted into the cache and is a valid interaction-graph JSON.
    cache_file = cache_store.cache_path(host)
    assert os.path.exists(cache_file), "graph was not promoted to %s" % cache_file
    graph = json.load(open(cache_file))
    assert graph["meta"]["host"] == host
    assert isinstance(graph["states"], list) and graph["states"]
    assert graph["meta"]["states"] == terminal["states"]
