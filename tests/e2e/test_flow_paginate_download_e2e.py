"""End-to-end test for the automation/flow layer: paginate → download → content-hash dedupe.

Nothing in the exercised path is mocked. This test:

  1. serves the 4-page fixture site (flow_index -> flow_page1 -> flow_page2 -> flow_page3)
     over HTTP on 127.0.0.1, exactly like tests/e2e/test_crawl_upload_e2e.py does;
  2. runs the REAL interaction crawler against it through a live PinchTab bridge + real
     headless Chrome, producing a real interaction graph;
  3. drives the REAL step VM (`runner.execute`) in-process with a REAL `PinchTabBrowser` and
     a REAL `ArtifactStore`: `goto{goal}` resolves the "reports" page off the crawled graph
     and walks to it, then `paginate` walks all 3 pages, and a nested `for_each` downloads
     every `kind: download` control it finds — 5 files, 5 distinct sha256s, really on disk;
  4. RE-RUNS the identical flow against a SECOND ArtifactStore instance on the same
     root+scope (i.e. the next scheduled tick, fresh process state) and asserts the ledger
     persisted: 0 new, 5 dupes. That is the whole point of the polling use case.

Note the bytes come from the IN-SESSION fetch (`browser.save_bytes`), not `pinchtab
download`: the CLI's SSRF guard unconditionally refuses loopback hosts, so a local fixture
can only be fetched from inside the page. That is the same path an authenticated real-world
app takes (it inherits the session's cookies), and `runner._op_download` tries it first.

It SKIPS cleanly (never fails) when the environment can't run it:
  - the `pinchtab` CLI isn't on PATH, or
  - no bridge is reachable at the server URL.
Point it at a bridge with PWG_E2E_SERVER (default http://localhost:9871). The bridge auth
token is read from $PINCHTAB_TOKEN, else from crawl-config.json at the repo root (both
gitignored). The bridge config MUST set instanceDefaults.blockImages/blockMedia to false —
they silently break the in-session fetch by file extension.
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

from pinchtab_webgraph import artifacts, browser as browser_mod, runner

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


def _crawl(start_url, out_base):
    """Run the real interaction crawler as a subprocess; return the graph JSON path."""
    env = dict(os.environ)
    tok = _token()
    if tok:
        env["PINCHTAB_TOKEN"] = tok
    cfg = REPO_ROOT / "crawl-config.json"
    if cfg.exists():
        env.setdefault("PINCHTAB_CONFIG", str(cfg))

    r = subprocess.run(
        [sys.executable, "-m", "pinchtab_webgraph.interaction_crawl",
         "--start", start_url, "--server", SERVER, "--out", str(out_base),
         "--max-states", "12", "--max-depth", "3", "--no-capture-form-states"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=300,
    )
    assert r.returncode == 0, "interaction crawl failed:\n%s\n%s" % (r.stdout, r.stderr)
    path = "%s.json" % out_base
    assert os.path.exists(path), "crawler produced no graph at %s" % path
    return path


def _flow(host):
    """goto the reports page off the crawled graph, then paginate + download everything."""
    return {
        "name": "e2e-download-reports",
        "host": host,
        "capabilities": {"allow_download": True},
        "steps": [
            {"op": "goto", "goal": "download report a"},
            {"op": "paginate", "max_pages": 5, "body": [
                {"op": "for_each", "match": {"kind": "download"}, "as": "item", "body": [
                    {"op": "download", "href": "${item.href}", "name": "${item.text}.pdf"},
                ]},
            ]},
        ],
    }


def _pages_paginated(result):
    return [e for e in result["steps"] if e["op"] == "paginate" and e["status"] == "page"]


def test_flow_paginates_downloads_and_dedupes(fixture_server, tmp_path):
    start_url = "%s/flow_index.html" % fixture_server
    host = urlparse(start_url).hostname

    graph_path = _crawl(start_url, tmp_path / "flowgraph")

    token = _token()
    tab = browser_mod.resolve_tab(SERVER, token)
    assert tab, "could not resolve a live tab on the bridge"
    live = browser_mod.PinchTabBrowser(SERVER, token, tab)

    flow = _flow(host)
    store_root = tmp_path / "store"

    # --- RUN 1: everything is new -------------------------------------------------
    store1 = artifacts.ArtifactStore(scope="e2e-reports", root=str(store_root))
    r1 = runner.execute(flow, browser=live, graph_path=graph_path, store=store1)

    assert r1["status"] == "ok", json.dumps(r1["steps"], indent=2)
    assert len(_pages_paginated(r1)) == 3, "expected 3 paginated pages: %s" % _pages_paginated(r1)

    done = [e for e in r1["steps"] if e["op"] == "paginate" and e["status"] == "ok"]
    assert done and done[-1]["pages"] == 3 and done[-1]["reason"] == "no paginator found"

    assert r1["stats"]["artifacts_new"] == 5, r1["stats"]
    assert r1["stats"]["artifacts_dupe"] == 0, r1["stats"]

    shas = {a["sha256"] for a in r1["artifacts"]}
    assert len(shas) == 5, "expected 5 DISTINCT content hashes, got %s" % sorted(shas)

    # every download really came from the in-session fetch, and every file is really on disk
    downloads = [e for e in r1["steps"] if e["op"] == "download" and e["status"] == "new"]
    assert len(downloads) == 5
    assert {e["via"] for e in downloads} == {"fetch"}
    assert {e["name"] for e in downloads} == {
        "Download report %s.pdf" % L for L in "ABCDE"}

    for art in r1["artifacts"]:
        assert os.path.exists(art["path"]), art
        assert os.path.getsize(art["path"]) == art["size"] > 0
        assert artifacts.hash_file(art["path"]) == art["sha256"]

    # the bytes on disk are byte-identical to the files the fixture server served
    served = {artifacts.hash_file(str(E2E_DIR / "flow_files" / ("report-%s.pdf" % L)))
              for L in "abcde"}
    assert shas == served

    # --- RUN 2: the dedupe proof (a fresh store instance = the next scheduled tick) ---
    store2 = artifacts.ArtifactStore(scope="e2e-reports", root=str(store_root))
    assert store2.stats()["count"] == 5, "ledger did not persist across store instances"

    r2 = runner.execute(flow, browser=live, graph_path=graph_path, store=store2)

    assert r2["status"] == "ok", json.dumps(r2["steps"], indent=2)
    assert len(_pages_paginated(r2)) == 3
    assert r2["stats"]["artifacts_new"] == 0, r2["stats"]
    assert r2["stats"]["artifacts_dupe"] == 5, r2["stats"]
    assert {a["sha256"] for a in r2["artifacts"]} == shas

    # a dupe points at the ALREADY-STORED bytes and leaves no second copy behind
    for art in r2["artifacts"]:
        assert art["status"] == "dupe"
        assert os.path.exists(art["path"])
    stored = list((store_root / "files").iterdir())
    assert len(stored) == 5, "dedupe wrote extra copies: %s" % [p.name for p in stored]
    staging = store_root / "staging"
    assert not staging.exists() or not list(staging.iterdir()), "staging left behind"
