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
keyring = pytest.importorskip("keyring")
import keyring.backend
import keyring.errors

from fastapi.testclient import TestClient

from pinchtab_webgraph.ui.server import app

client = TestClient(app)

HOST = "example.test"


class FakeKeyring(keyring.backend.KeyringBackend):
    """In-memory keyring backend so the vault HTTP tests never touch a real OS keyring."""
    priority = 1

    def __init__(self):
        super().__init__()
        self._store = {}

    def get_password(self, service, username):
        return self._store.get((service, username))

    def set_password(self, service, username, password):
        self._store[(service, username)] = password

    def delete_password(self, service, username):
        try:
            del self._store[(service, username)]
        except KeyError:
            raise keyring.errors.PasswordDeleteError("not found")


@pytest.fixture
def fake_keyring():
    keyring.set_keyring(FakeKeyring())


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


# --- vault: PUT / GET / DELETE round-trip ------------------------------------

VAULT_HOST = "vault.example.com"
SECRET = "http-round-trip-pw"


def _no_secret_anywhere(obj):
    """The literal password must not appear at ANY key/value in a response body."""
    if isinstance(obj, dict):
        return all(SECRET not in str(k) and _no_secret_anywhere(v)
                   for k, v in obj.items())
    if isinstance(obj, list):
        return all(_no_secret_anywhere(v) for v in obj)
    return SECRET not in str(obj)


def test_vault_status_never_non_200(isolated_cache_home, fake_keyring):
    r = client.get("/api/vault/status")
    assert r.status_code == 200
    body = r.json()
    assert "available" in body and "config_path" in body


def test_vault_put_get_delete_roundtrip(isolated_cache_home, fake_keyring):
    # PUT
    r = client.put("/api/vault/credentials/%s" % VAULT_HOST, json={
        "url": "https://vault.example.com/login", "username": "me@example.com",
        "password": SECRET})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["has_password"] is True
    assert _no_secret_anywhere(body)

    # list — masked, no secret
    r = client.get("/api/vault/credentials")
    assert r.status_code == 200
    body = r.json()
    assert VAULT_HOST in {c["host"] for c in body["credentials"]}
    assert _no_secret_anywhere(body)

    # GET one
    r = client.get("/api/vault/credentials/%s" % VAULT_HOST)
    assert r.status_code == 200
    body = r.json()
    assert body["host"] == VAULT_HOST
    assert body["has_password"] is True
    assert _no_secret_anywhere(body)

    # DELETE
    r = client.delete("/api/vault/credentials/%s" % VAULT_HOST)
    assert r.status_code == 200
    body = r.json()
    assert body["routing_removed"] is True
    assert _no_secret_anywhere(body)

    # gone now -> 404
    r = client.get("/api/vault/credentials/%s" % VAULT_HOST)
    assert r.status_code == 404
    assert r.json()["status"] == "no_credential_for_host"


def test_vault_invalid_args_400(isolated_cache_home, fake_keyring):
    # missing password -> invalid_args -> 400
    r = client.put("/api/vault/credentials/%s" % VAULT_HOST, json={
        "url": "https://vault.example.com/login", "username": "me@example.com"})
    assert r.status_code == 400
    assert r.json()["status"] == "invalid_args"


def test_vault_invalid_host_400(isolated_cache_home, fake_keyring):
    r = client.get("/api/vault/credentials/%s" % "bad%20host")
    assert r.status_code == 400
    assert r.json()["status"] == "invalid_host"


def test_vault_unavailable_503(isolated_cache_home):
    import keyring.backends.fail
    keyring.set_keyring(keyring.backends.fail.Keyring())
    r = client.put("/api/vault/credentials/%s" % VAULT_HOST, json={
        "url": "https://vault.example.com/login", "username": "me@example.com",
        "password": SECRET})
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "vault_unavailable"
    assert body["reason"] == "no_keyring_backend"
    assert _no_secret_anywhere(body)
