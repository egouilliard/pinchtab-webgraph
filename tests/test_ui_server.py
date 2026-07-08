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


# --- live pane: "Show Me How" locate -> located round-trip -------------------
#
# Mirrors test_ui_screencast.py's WS tests: a fake open_live_session yields a LiveSession
# whose cdp_ws answers a Runtime.evaluate (the locate probe) with a scripted rect. The
# REAL relay + dispatcher drive it, so a {type:"locate"} frame yields a {type:"located"}.

import asyncio  # noqa: E402
import json  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402

from pinchtab_webgraph.ui import server as ui_server  # noqa: E402
from pinchtab_webgraph.ui import screencast  # noqa: E402


class LocateReplyCDPWebSocket:
    """A CDP socket that answers any Runtime.evaluate send with a scripted rect reply.

    recv() blocks until send() queues something; Page.enable/startScreencast/ack queue
    nothing (the relay just idles), while a Runtime.evaluate (the locate probe) queues a
    matching-``id`` reply so the shared dispatcher resolves the request future.
    """

    def __init__(self, rect_value):
        self.sent = []
        self._q = asyncio.Queue()
        self._rect = rect_value
        self.closed = False

    async def send(self, s):
        self.sent.append(s)
        msg = json.loads(s)
        if msg.get("method") == "Runtime.evaluate":
            self._q.put_nowait(json.dumps(
                {"id": msg["id"], "result": {"result": {"value": json.dumps(self._rect)}}}))

    async def recv(self):
        return await self._q.get()

    async def close(self):
        self.closed = True


def test_ws_screencast_locate_returns_located_rect(monkeypatch):
    rect = {"found": True, "x": 12, "y": 34, "width": 56, "height": 78}

    @asynccontextmanager
    async def fake_open(host, *, bridge_url=None):
        yield screencast.LiveSession(
            cdp_ws=LocateReplyCDPWebSocket(rect),
            auth={"authenticated": False, "reason": "no_credential"})

    monkeypatch.setattr(ui_server.screencast, "open_live_session", fake_open)

    with client.websocket_connect("/ws/screencast?host=example.test") as ws:
        assert ws.receive_json()["type"] == "status"          # route auth status
        assert ws.receive_json() == {"type": "status", "state": "live",
                                     "width": None, "height": None}  # relay status(live)
        ws.send_json({"type": "locate", "stepId": "step-1",
                      "selector": "a.create", "label": "Create Role"})
        located = ws.receive_json()
        assert located["type"] == "located"
        assert located["stepId"] == "step-1"
        assert located["rect"] == {"x": 12.0, "y": 34.0, "width": 56.0, "height": 78.0}


def test_ws_screencast_locate_null_rect_when_not_found(monkeypatch):
    # A not-found probe yields a null rect (never an error frame).
    @asynccontextmanager
    async def fake_open(host, *, bridge_url=None):
        yield screencast.LiveSession(
            cdp_ws=LocateReplyCDPWebSocket({"found": False}),
            auth={"authenticated": False, "reason": None})

    monkeypatch.setattr(ui_server.screencast, "open_live_session", fake_open)

    with client.websocket_connect("/ws/screencast?host=example.test") as ws:
        ws.receive_json()  # route status
        ws.receive_json()  # relay status(live)
        ws.send_json({"type": "locate", "stepId": "s2", "selector": None, "label": "Nope"})
        located = ws.receive_json()
        assert located == {"type": "located", "stepId": "s2", "rect": None}


# --- chat sessions: REST CRUD + WS bootstrap / restore -----------------------
#
# The session store is exercised against an isolated home (PINCHTAB_WEBGRAPH_HOME -> a
# tmp dir) so no test touches a real ~/.pinchtab-webgraph. The WS restore test
# monkeypatches open_chat_session so it needs no ANTHROPIC key / MCP subprocess — the
# route's own load + bootstrap-frame logic is what's under test.

from types import SimpleNamespace  # noqa: E402

from pinchtab_webgraph.ui import chat_store  # noqa: E402

SESS_HOST = "sess.example.com"


def test_sessions_crud_round_trip(isolated_cache_home):
    # empty to start
    r = client.get("/api/hosts/%s/sessions" % SESS_HOST)
    assert r.status_code == 200
    assert r.json()["sessions"] == []

    # create
    r = client.post("/api/hosts/%s/sessions" % SESS_HOST, json={"title": "my chat"})
    assert r.status_code == 200
    created = r.json()
    sid = created["id"]
    assert created["title"] == "my chat"
    assert created["host"] == SESS_HOST
    assert "transcript" not in created  # summary only

    # list shows it
    r = client.get("/api/hosts/%s/sessions" % SESS_HOST)
    assert sid in {s["id"] for s in r.json()["sessions"]}

    # get the full record — WITHOUT the resume-only internals
    r = client.get("/api/hosts/%s/sessions/%s" % (SESS_HOST, sid))
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == sid
    assert "transcript" in body
    assert "wire_messages" not in body and "sdk_session_id" not in body

    # rename -> summary reflects it
    r = client.patch("/api/hosts/%s/sessions/%s" % (SESS_HOST, sid),
                     json={"title": "renamed"})
    assert r.status_code == 200
    assert r.json()["title"] == "renamed"

    # delete -> deleted True, then gone (404), then idempotent False
    r = client.delete("/api/hosts/%s/sessions/%s" % (SESS_HOST, sid))
    assert r.status_code == 200 and r.json()["deleted"] is True
    r = client.get("/api/hosts/%s/sessions/%s" % (SESS_HOST, sid))
    assert r.status_code == 404 and r.json()["status"] == "session_not_found"
    r = client.delete("/api/hosts/%s/sessions/%s" % (SESS_HOST, sid))
    assert r.status_code == 200 and r.json()["deleted"] is False


def test_sessions_too_many_returns_429(isolated_cache_home):
    for _ in range(chat_store.MAX_SESSIONS_PER_HOST):
        assert client.post("/api/hosts/%s/sessions" % SESS_HOST).status_code == 200
    r = client.post("/api/hosts/%s/sessions" % SESS_HOST)
    assert r.status_code == 429
    body = r.json()
    assert body["status"] == "too_many_sessions"
    assert body["max"] == chat_store.MAX_SESSIONS_PER_HOST


def test_session_not_found_on_get_and_patch(isolated_cache_home):
    sid = "a" * 32  # a valid-shaped but non-existent id
    r = client.get("/api/hosts/%s/sessions/%s" % (SESS_HOST, sid))
    assert r.status_code == 404 and r.json()["status"] == "session_not_found"
    r = client.patch("/api/hosts/%s/sessions/%s" % (SESS_HOST, sid), json={"title": "x"})
    assert r.status_code == 404 and r.json()["status"] == "session_not_found"


def test_invalid_session_id_on_get(isolated_cache_home):
    r = client.get("/api/hosts/%s/sessions/%s" % (SESS_HOST, "NOT-HEX"))
    assert r.status_code == 400 and r.json()["status"] == "invalid_session"


def test_invalid_session_id_on_ws(isolated_cache_home):
    with client.websocket_connect(
            "/ws/chat?host=%s&session=%s" % (SESS_HOST, "NOT-HEX")) as ws:
        frame = ws.receive_json()
        assert frame["type"] == "error"
        assert frame["status"] == "invalid_session"


def test_ws_chat_restores_transcript_on_bootstrap(isolated_cache_home, monkeypatch):
    # Pre-seed a session with a transcript, then connect with ?session=ID and assert the
    # leading bootstrap `session` frame carries that transcript back verbatim.
    rec = chat_store.create(SESS_HOST, backend="api", title="restored chat")
    rec["transcript"] = [
        {"role": "user", "type": "user", "text": "how do I add a role?", "ts": "t"},
        {"role": "assistant", "type": "text", "text": "Go to Team.", "ts": "t"}]
    chat_store.save(rec)
    sid = rec["id"]

    @asynccontextmanager
    async def fake_open(host, *, backend_name=None, record=None):
        # the route loads the record from disk and passes it in; echo it back.
        yield SimpleNamespace(record=record)

    monkeypatch.setattr(ui_server.chat_backend, "open_chat_session", fake_open)

    with client.websocket_connect(
            "/ws/chat?host=%s&session=%s" % (SESS_HOST, sid)) as ws:
        boot = ws.receive_json()
        assert boot["type"] == "session"
        assert boot["id"] == sid
        assert boot["title"] == "restored chat"
        texts = [e.get("text") for e in boot["transcript"]]
        assert "how do I add a role?" in texts and "Go to Team." in texts


def test_session_routes_reject_invalid_host(isolated_cache_home):
    r = client.get("/api/hosts/%s/sessions" % "bad%20host")
    assert r.status_code == 400 and r.json()["status"] == "invalid_host"


# --- SPA static page: index.html <-> app.js element-id contract --------------
#
# The SPA is vanilla HTML/JS with no build step, so nothing enforces that the IDs
# app.js reaches for actually exist in index.html. These two guards do: if index.html
# is served, and if every ID the controller depends on is present, the two files stay
# in sync. We do NOT execute the JS — just assert the shared ID surface + that app.js
# references those same IDs.

# The element IDs the SPA controller (app.js) depends on — the panes, the two
# sockets' targets, and the vault modal. Keep in sync with app.js's `el(...)` calls.
SPA_IDS = [
    "caches-dir", "hosts",                        # sidebar: crawled-graphs list
    "host-header", "host-name", "host-kind", "host-counts", "panes",  # host header
    "view-tabs", "tab-workspace", "tab-graph",                        # view switcher
    "graph-view", "graph-canvas", "graph-detail", "graph-search", "graph-status",  # graph view
    "chat-form", "chat-input", "chat-log", "chat-status",             # chat pane
    "live-view", "live-status",                                       # live pane
    "vault-modal", "vault-open", "vault-close", "vault-status",       # vault modal
    "creds", "cred-form", "cred-host", "cred-password", "cred-msg",
]


def test_index_served_with_spa_element_ids():
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    html = r.text
    for _id in SPA_IDS:
        assert ('id="%s"' % _id) in html, "index.html is missing id=%r" % _id


def test_app_js_references_the_same_ids():
    r = client.get("/app.js")
    assert r.status_code == 200
    js = r.text
    # The controller resolves elements by id string literal; each SPA id must appear.
    for _id in SPA_IDS:
        assert ('"%s"' % _id) in js, "app.js never references id=%r" % _id


# --- Phase 2 graph view: /vendor mount, lazy graph.js, adapter contract ------
#
# The Graph view reuses the SAME 6 vendored Cytoscape libs crawl.py inlines, served from
# a NEW /vendor StaticFiles mount registered BEFORE the catch-all "/" mount (else "/"
# would shadow it). graph.js is injected lazily by app.js (never an eager <script>).

def test_vendor_mounted_all_six_files():
    from pinchtab_webgraph import crawl
    for name in crawl._VENDOR_FILES:
        r = client.get("/vendor/%s" % name)
        assert r.status_code == 200, "missing /vendor/%s" % name
        assert len(r.text) > 3000, "/vendor/%s looks truncated" % name


def test_vendor_path_traversal_rejected():
    # A traversal attempt out of the vendor dir must never resolve to server.py (or any
    # source) — StaticFiles rejects it with a 403/404, never a 200 leak.
    # Percent-encode the dot-segments so httpx does NOT normalize `..` away client-side
    # (a bare "/vendor/../server.py" is collapsed to "/server.py" before it ever reaches
    # the app, which would test nothing) — this drives the raw `..` into StaticFiles.
    r = client.get("/vendor/%2e%2e/server.py")
    assert r.status_code in (403, 404)
    assert r.status_code != 200
    assert "app.mount" not in r.text


def test_graph_js_lazy_not_eager():
    # graph.js is served, but the index HTML must NOT eager-load it via a <script> tag —
    # app.js injects it (and the vendor libs) dynamically on the first Graph-tab switch.
    r = client.get("/graph.js")
    assert r.status_code == 200
    html = client.get("/").text
    assert 'src="graph.js"' not in html
    assert 'src="/graph.js"' not in html


def test_graph_css_served():
    r = client.get("/graph.css")
    assert r.status_code == 200


def test_graph_raw_edge_shape_locks_adapter_contract(populated_cache_home):
    # The client-side adapter (graph.js adaptInteractionGraph) maps {from,to,...} edges to
    # Cytoscape {source,target,...}. This locks the RAW server shape it consumes: edges carry
    # from/to/label/kind and deliberately NOT source/target (regression guard for the contract).
    r = client.get("/api/hosts/%s/graph" % HOST)
    assert r.status_code == 200
    edges = r.json()["edges"]
    assert edges, "fixture should have at least one edge"
    for e in edges:
        assert "from" in e and "to" in e and "label" in e and "kind" in e
        assert "source" not in e and "target" not in e
