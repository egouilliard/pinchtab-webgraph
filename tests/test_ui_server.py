"""Tests for pinchtab_webgraph.ui.server — the FastAPI binding onto api + cache_store.

Guarded by importorskip("fastapi") so a base test run without the `ui` extra skips
cleanly (mirrors how test_mcp_server.py guards `mcp`). Routes are exercised through
a FastAPI TestClient against the module `app`, reusing the shared cache fixtures
(populated_cache_home seeds caches/example.test.json). Both the HTTP status code and
the body status/keys are asserted — only the three resolver statuses get non-200
codes; every structured miss stays 200.
"""
import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from pinchtab_webgraph.ui.server import app

client = TestClient(app)

HOST = "example.test"


# --- health ------------------------------------------------------------------

def test_health():
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "version" in body


# --- hosts index -------------------------------------------------------------

def test_hosts_lists_seeded_host(populated_cache_home):
    r = client.get("/api/hosts")
    assert r.status_code == 200
    body = r.json()
    hosts = {h["host"] for h in body["hosts"]}
    assert HOST in hosts
    entry = next(h for h in body["hosts"] if h["host"] == HOST)
    assert entry["summary"]["graph_kind"] == "interaction"
    assert entry["howto_url"] == "/api/hosts/%s/howto" % HOST
    assert "caches_dir" in body


# --- summary -----------------------------------------------------------------

def test_summary(populated_cache_home):
    r = client.get("/api/hosts/%s/summary" % HOST)
    assert r.status_code == 200
    body = r.json()
    assert body["graph_kind"] == "interaction"
    assert body["states"] == 5


# --- forms -------------------------------------------------------------------

def test_forms(populated_cache_home):
    r = client.get("/api/hosts/%s/forms" % HOST)
    assert r.status_code == 200
    body = r.json()
    assert body["meta"]["host"] == HOST
    assert {f["label"] for f in body["forms"]} == {"Create Role", "Add Report", "Add Widget"}


# --- howto -------------------------------------------------------------------

def test_howto_ok(populated_cache_home):
    r = client.get("/api/hosts/%s/howto" % HOST, params={"goal": "create role"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["results"][0]["trigger_label"] == "Create Role"


def test_howto_invalid_args_is_200(populated_cache_home):
    # no goal/match -> invalid_args, but that's a structured miss, still HTTP 200.
    r = client.get("/api/hosts/%s/howto" % HOST)
    assert r.status_code == 200
    assert r.json()["status"] == "invalid_args"


# --- graph -------------------------------------------------------------------

def test_graph_raw(populated_cache_home):
    r = client.get("/api/hosts/%s/graph" % HOST)
    assert r.status_code == 200
    body = r.json()
    assert "states" in body and "triggers" in body
    assert len(body["states"]) == 5


# --- resolver error codes ----------------------------------------------------

def test_invalid_host_400(populated_cache_home):
    # A single-segment host that fails cache_path's ^[A-Za-z0-9._-]+$ guard (a space)
    # reaches the resolver and is rejected as invalid_host.
    r = client.get("/api/hosts/%s/summary" % "bad%20host")
    assert r.status_code == 400
    assert r.json()["status"] == "invalid_host"


def test_encoded_slash_traversal_is_rejected(populated_cache_home):
    # An encoded-slash traversal attempt never matches the single-segment {host}
    # route, so it is safely rejected (404) before reaching the filesystem — never
    # a 200 leak or a 500 traceback.
    r = client.get("/api/hosts/%s/summary" % "..%2Fetc")
    assert r.status_code in (400, 404)
    assert r.status_code != 500


def test_unknown_host_404(isolated_cache_home):
    r = client.get("/api/hosts/never-crawled.test/summary")
    assert r.status_code == 404
    assert r.json()["status"] == "no_cache_for_host"
