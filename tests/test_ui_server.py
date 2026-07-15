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


# --- content (list + search) -------------------------------------------------

def test_content(populated_cache_home):
    r = client.get("/api/hosts/%s/content" % HOST)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    labels = {v["view_label"] for v in body["views"]}
    assert "Team" in labels and "Reports" in labels
    team = next(v for v in body["views"] if v["view_label"] == "Team")
    assert team["collections"][0]["kind"] == "list"
    assert team["collections"][0]["count"] == 1


def test_content_empty(isolated_cache_home):
    # A graph whose states carry no data collections resolves to status "empty".
    from pathlib import Path
    import json as _json
    from pinchtab_webgraph import cache_store
    fixtures = Path(__file__).parent / "fixtures"
    graph = _json.loads((fixtures / "sample_interaction_graph.json").read_text())
    for s in graph["states"]:
        s.pop("collections", None)
    cache_store.atomic_write("nocontent.test", graph)
    r = client.get("/api/hosts/nocontent.test/content")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "empty"
    assert body["views"] == []


def test_content_search_ok(populated_cache_home):
    r = client.get("/api/hosts/%s/content/search" % HOST, params={"text": "Alice"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["total_matches"] == 1
    assert body["views_matched"] == 1
    view = body["views"][0]
    assert view["view_label"] == "Team"
    assert view["reachable"] is True
    assert view["distance_clicks"] == 1
    assert view["items"][0]["text"] == "Alice Martin"
    assert view["truncated"] is False


def test_content_search_no_match(populated_cache_home):
    r = client.get("/api/hosts/%s/content/search" % HOST,
                   params={"text": "zzzznomatch"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "no_match"
    assert body["total_matches"] == 0
    assert body["views"] == []


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
import os  # noqa: E402
import pathlib  # noqa: E402
import time  # noqa: E402
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
    async def fake_open(host, *, backend_name=None, mode=None, record=None):
        # the route loads the record from disk and passes it in; echo it back.
        yield SimpleNamespace(record=record)

    monkeypatch.setattr(ui_server.chat_backend, "open_chat_session", fake_open)

    with client.websocket_connect(
            "/ws/chat?host=%s&session=%s" % (SESS_HOST, sid)) as ws:
        boot = ws.receive_json()
        assert boot["type"] == "session"
        assert boot["id"] == sid
        assert boot["title"] == "restored chat"
        assert boot["mode"] == "workspace"
        texts = [e.get("text") for e in boot["transcript"]]
        assert "how do I add a role?" in texts and "Go to Team." in texts


def test_session_routes_reject_invalid_host(isolated_cache_home):
    r = client.get("/api/hosts/%s/sessions" % "bad%20host")
    assert r.status_code == 400 and r.json()["status"] == "invalid_host"


# --- chat MODE: created new, PINNED on resume, filterable --------------------

def test_create_session_with_flow_mode_and_list_filter(isolated_cache_home):
    r = client.post("/api/hosts/%s/sessions" % SESS_HOST, json={"mode": "flow"})
    assert r.status_code == 200 and r.json()["mode"] == "flow"
    fid = r.json()["id"]

    r = client.post("/api/hosts/%s/sessions" % SESS_HOST, json={})
    assert r.json()["mode"] == "workspace"                  # the default
    wid = r.json()["id"]

    # an unknown mode token FAILS CLOSED to workspace — it must never grant a tool.
    r = client.post("/api/hosts/%s/sessions" % SESS_HOST, json={"mode": "wide-open"})
    assert r.json()["mode"] == "workspace"

    listed = client.get("/api/hosts/%s/sessions?mode=flow" % SESS_HOST).json()["sessions"]
    assert [s["id"] for s in listed] == [fid]
    ws_ids = {s["id"] for s in
              client.get("/api/hosts/%s/sessions?mode=workspace" % SESS_HOST)
              .json()["sessions"]}
    assert wid in ws_ids and fid not in ws_ids
    # unfiltered: everything
    assert len(client.get("/api/hosts/%s/sessions" % SESS_HOST).json()["sessions"]) == 3


def test_ws_chat_new_session_takes_the_mode_query_param(isolated_cache_home, monkeypatch):
    seen = {}

    @asynccontextmanager
    async def fake_open(host, *, backend_name=None, mode=None, record=None):
        seen["mode"] = mode
        rec = chat_store.create(host, backend="api", mode=mode or "workspace")
        yield SimpleNamespace(record=rec)

    monkeypatch.setattr(ui_server.chat_backend, "open_chat_session", fake_open)

    with client.websocket_connect("/ws/chat?host=%s&mode=flow" % SESS_HOST) as ws:
        boot = ws.receive_json()
        assert boot["type"] == "session" and boot["mode"] == "flow"
    assert seen["mode"] == "flow"


def test_ws_chat_resumed_session_ignores_the_mode_query_param(isolated_cache_home,
                                                              monkeypatch):
    # THE SAFETY REGRESSION at the route level: reconnecting to an EXISTING workspace
    # session with ?mode=flow must NOT pass a mode through — the mode comes from the
    # record, mirroring the backend pin. Otherwise a stale session id + a crafted query
    # would hand a read-only chat the propose_flow tool.
    rec = chat_store.create(SESS_HOST, backend="api")           # workspace
    seen = {}

    @asynccontextmanager
    async def fake_open(host, *, backend_name=None, mode=None, record=None):
        seen["mode"] = mode
        seen["record_mode"] = (record or {}).get("mode")
        yield SimpleNamespace(record=record)

    monkeypatch.setattr(ui_server.chat_backend, "open_chat_session", fake_open)

    with client.websocket_connect(
            "/ws/chat?host=%s&session=%s&mode=flow" % (SESS_HOST, rec["id"])) as ws:
        boot = ws.receive_json()
        assert boot["mode"] == "workspace"                      # NOT flow
    assert seen["mode"] is None                                 # the param never travelled
    assert seen["record_mode"] == "workspace"


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
    "view-tabs", "tab-workspace", "tab-graph", "tab-explore",         # view switcher
    "graph-view", "graph-canvas", "graph-detail", "graph-search", "graph-status",  # graph view
    "explore-view", "explore-tab-search", "explore-search-input",     # explore: search
    "explore-search-results",
    "explore-tab-forms", "explore-goal-input", "explore-goal-result", # explore: forms
    "explore-forms-list",
    "explore-tab-content", "explore-content-list",                    # explore: content
    "cmdk-modal", "cmdk-open", "cmdk-input", "cmdk-results",          # command palette
    "theme-toggle",                                                   # light/dark toggle
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


# --- Phase 5 explore view + command palette: static assets -------------------
#
# Unlike graph.js (lazy — 785KB of Cytoscape deps), explore.js has NO vendor deps and is
# loaded EAGERLY via a <script> right after app.js (it calls app.js globals), so the
# inverse of test_graph_js_lazy_not_eager holds: its <script> tag IS present in the HTML.

def test_explore_js_served():
    r = client.get("/explore.js")
    assert r.status_code == 200


def test_explore_css_served():
    r = client.get("/explore.css")
    assert r.status_code == 200


def test_theme_toggle_wired():
    # The manual light/dark toggle needs three load-bearing pieces the SPA_IDS check
    # (which only covers the button id) can't see: the pre-paint inline script that
    # applies the saved theme, and the explicit data-theme CSS override.
    html = client.get("/").text
    assert 'localStorage.getItem("pwg-theme")' in html          # pre-paint anti-flash script
    assert 'data-theme' in html
    css = client.get("/style.css").text
    assert ':root[data-theme="dark"]' in css                    # explicit dark override wins
    assert ':root:not([data-theme])' in css                     # OS default only when unset


def test_explore_js_eager_not_lazy():
    # explore.js is eager: its <script> tag must be present in the index HTML (the inverse
    # of graph.js, which app.js injects dynamically only on the first Graph-tab switch).
    html = client.get("/").text
    assert 'src="explore.js"' in html


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


# --- flow routes: CRUD + validation + run history + artifacts ----------------
#
# The flow store is exercised against an isolated home (PINCHTAB_WEBGRAPH_HOME -> a tmp dir)
# via the shared isolated_cache_home fixture. The KEY contract asserted here is the status
# convention: a flow document that fails validation is a structured MISS — 200 with
# {"status":"invalid"} in the BODY, NOT a 400 — while a malformed flow_id TOKEN is a real
# protocol error (400). See the note in server._STATUS_CODE.

FLOW_HOST = "flow.example.com"


def _flow_doc(name="my-flow", **extra):
    doc = {"name": name, "steps": [{"op": "goto", "url": "https://flow.example.com/x"}]}
    doc.update(extra)
    return doc


def test_flows_crud_round_trip(isolated_cache_home):
    # empty to start
    r = client.get("/api/hosts/%s/flows" % FLOW_HOST)
    assert r.status_code == 200 and r.json()["flows"] == []

    # create
    r = client.post("/api/hosts/%s/flows" % FLOW_HOST,
                    json=_flow_doc(inputs={"since": {"type": "string"}}))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["name"] == "my-flow"
    fid = body["id"]
    assert "doc" not in body                     # the summary omits the full document

    # list
    r = client.get("/api/hosts/%s/flows" % FLOW_HOST)
    assert [f["id"] for f in r.json()["flows"]] == [fid]

    # get (the FULL record, doc included — the editor needs it)
    r = client.get("/api/hosts/%s/flows/%s" % (FLOW_HOST, fid))
    assert r.status_code == 200
    assert r.json()["doc"]["name"] == "my-flow"

    # schema (derived from the doc's `inputs`)
    r = client.get("/api/hosts/%s/flows/%s/schema" % (FLOW_HOST, fid))
    assert r.status_code == 200
    assert r.json() == {"type": "object", "additionalProperties": False,
                        "properties": {"since": {"type": "string"}}}

    # update (full replace, re-validated)
    r = client.put("/api/hosts/%s/flows/%s" % (FLOW_HOST, fid), json=_flow_doc("renamed"))
    assert r.status_code == 200 and r.json()["name"] == "renamed"
    assert client.get("/api/hosts/%s/flows/%s" % (FLOW_HOST, fid)).json()["doc"]["name"] \
        == "renamed"

    # delete (idempotent)
    r = client.delete("/api/hosts/%s/flows/%s" % (FLOW_HOST, fid))
    assert r.status_code == 200 and r.json()["deleted"] is True
    assert client.get("/api/hosts/%s/flows/%s"
                      % (FLOW_HOST, fid)).status_code == 404
    r = client.delete("/api/hosts/%s/flows/%s" % (FLOW_HOST, fid))
    assert r.status_code == 200 and r.json()["deleted"] is False


def test_flow_create_invalid_doc_is_200_with_status_in_body(isolated_cache_home):
    # THE house convention: a structural miss on user-submitted JSON is a 200 answer that
    # SAYS it is invalid — the same shape `flow_cmd validate` prints. NOT a 400.
    r = client.post("/api/hosts/%s/flows" % FLOW_HOST,
                    json={"name": "x", "steps": []})          # an empty step list
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "invalid"
    assert body["path"] == "steps" and "steps" in body["error"]
    assert client.get("/api/hosts/%s/flows" % FLOW_HOST).json()["flows"] == []


def test_flow_update_invalid_doc_is_200_and_does_not_clobber(isolated_cache_home):
    fid = client.post("/api/hosts/%s/flows" % FLOW_HOST, json=_flow_doc("v1")).json()["id"]
    r = client.put("/api/hosts/%s/flows/%s" % (FLOW_HOST, fid),
                   json={"name": "v2", "steps": [{"op": "nope"}]})
    assert r.status_code == 200 and r.json()["status"] == "invalid"
    assert client.get("/api/hosts/%s/flows/%s"
                      % (FLOW_HOST, fid)).json()["doc"]["name"] == "v1"


def test_flow_capability_declaration_is_enforced_at_save(isolated_cache_home):
    # SAFE BY DEFAULT: a step that submits, in a doc that doesn't declare allow_submit, is
    # rejected at SAVE time — a scheduled run can never half-execute it.
    r = client.post("/api/hosts/%s/flows" % FLOW_HOST,
                    json={"name": "writer",
                          "steps": [{"op": "do", "goal": "create", "submit": True}]})
    assert r.status_code == 200 and r.json()["status"] == "invalid"
    assert "allow_submit" in r.json()["error"]


def test_flows_too_many_returns_429(isolated_cache_home, monkeypatch):
    monkeypatch.setattr(ui_server.flow_store, "MAX_FLOWS_PER_HOST", 2)
    for i in range(2):
        assert client.post("/api/hosts/%s/flows" % FLOW_HOST,
                           json=_flow_doc("f%d" % i)).status_code == 200
    r = client.post("/api/hosts/%s/flows" % FLOW_HOST, json=_flow_doc("f3"))
    assert r.status_code == 429
    assert r.json()["status"] == "too_many_flows" and r.json()["max"] == 2


def test_flow_not_found_and_bad_ids(isolated_cache_home):
    missing = "0" * 32
    for url in ("/api/hosts/%s/flows/%s" % (FLOW_HOST, missing),
                "/api/hosts/%s/flows/%s/schema" % (FLOW_HOST, missing),
                "/api/hosts/%s/flows/%s/runs" % (FLOW_HOST, missing),
                "/api/hosts/%s/flows/%s/artifacts" % (FLOW_HOST, missing)):
        r = client.get(url)
        assert r.status_code == 404 and r.json()["status"] == "flow_not_found"

    # a malformed id TOKEN is a protocol error (400), rejected before any filesystem access.
    r = client.get("/api/hosts/%s/flows/%s" % (FLOW_HOST, "NOT-HEX"))
    assert r.status_code == 400 and r.json()["status"] == "invalid_flow"
    fid = client.post("/api/hosts/%s/flows" % FLOW_HOST, json=_flow_doc()).json()["id"]
    r = client.get("/api/hosts/%s/flows/%s/runs/%s" % (FLOW_HOST, fid, "../../etc/passwd"))
    assert r.status_code in (400, 404)      # starlette may 404 a path-traversal segment


def test_flow_routes_reject_invalid_host(isolated_cache_home):
    r = client.get("/api/hosts/%s/flows" % "bad host")
    assert r.status_code == 400 and r.json()["status"] == "invalid_host"
    r = client.post("/api/hosts/%s/flows" % "bad host", json=_flow_doc())
    assert r.status_code == 400 and r.json()["status"] == "invalid_host"


def test_flow_runs_routes(isolated_cache_home):
    fid = client.post("/api/hosts/%s/flows" % FLOW_HOST, json=_flow_doc()).json()["id"]
    assert client.get("/api/hosts/%s/flows/%s/runs" % (FLOW_HOST, fid)).json()["runs"] == []

    rid = ui_server.flow_store.new_run_id()
    ui_server.flow_store.start_run(FLOW_HOST, fid, rid, dry_run=True, capabilities={},
                                   inputs={})
    ui_server.flow_store.finish_run(FLOW_HOST, fid, rid,
                                    {"status": "ok",
                                     "steps": [{"op": "goto", "status": "ok"}],
                                     "stats": {"steps_executed": 1}})

    r = client.get("/api/hosts/%s/flows/%s/runs" % (FLOW_HOST, fid))
    runs = r.json()["runs"]
    assert len(runs) == 1 and runs[0]["id"] == rid and runs[0]["status"] == "ok"
    assert "steps" not in runs[0]                # summaries omit the heavy payloads

    r = client.get("/api/hosts/%s/flows/%s/runs/%s" % (FLOW_HOST, fid, rid))
    assert r.status_code == 200 and r.json()["steps"] == [{"op": "goto", "status": "ok"}]

    r = client.get("/api/hosts/%s/flows/%s/runs/%s" % (FLOW_HOST, fid, "0" * 32))
    assert r.status_code == 404 and r.json()["status"] == "run_not_found"
    r = client.get("/api/hosts/%s/flows/%s/runs/%s" % (FLOW_HOST, fid, "NOTHEX"))
    assert r.status_code == 400 and r.json()["status"] == "invalid_run"

    # the run history CASCADES away with the flow.
    client.delete("/api/hosts/%s/flows/%s" % (FLOW_HOST, fid))
    assert client.get("/api/hosts/%s/flows/%s/runs"
                      % (FLOW_HOST, fid)).status_code == 404


def test_flow_artifacts_route(isolated_cache_home):
    from pinchtab_webgraph import artifacts as artifacts_mod
    fid = client.post("/api/hosts/%s/flows" % FLOW_HOST, json=_flow_doc()).json()["id"]

    # the scope is the flow_id — the same one the run WS passes as --scope.
    store = artifacts_mod.ArtifactStore(scope=fid)
    staged = store.staging_path("report.pdf")
    with open(staged, "wb") as fh:
        fh.write(b"pdf-bytes")
    store.accept(staged, name="report.pdf", source="https://flow.example.com/r.pdf")

    r = client.get("/api/hosts/%s/flows/%s/artifacts" % (FLOW_HOST, fid))
    assert r.status_code == 200
    body = r.json()
    assert [a["name"] for a in body["artifacts"]] == ["report.pdf"]
    assert body["stats"]["count"] == 1 and body["stats"]["scope"] == fid


# --- stateless flow validate / schema ----------------------------------------

def test_flows_validate_stateless():
    r = client.post("/api/flows/validate", json=_flow_doc("v", host="h.test"))
    assert r.status_code == 200
    body = r.json()
    assert body == {"status": "ok", "name": "v", "host": "h.test", "steps": 1,
                    "capabilities": {"allow_submit": False, "allow_download": True,
                                     "allow_upload": False},
                    "inputs": [],
                    "warnings": []}             # h.test has no cache -> nothing to resolve

    r = client.post("/api/flows/validate", json={"name": "x", "steps": []})
    assert r.status_code == 200                 # a structural miss stays a 200
    assert r.json()["status"] == "invalid" and r.json()["path"] == "steps"


# --- resolvability warnings (the goal that validates green and aborts at run time) ---

def _goal_doc(goal, host=HOST):
    return {"name": "g", "host": host,
            "steps": [{"op": "goto", "url": "https://%s/x" % host},
                      {"op": "paginate", "max_pages": 2,
                       "body": [{"op": "do", "goal": goal}]}]}


def test_flows_validate_warns_when_a_goal_does_not_resolve(populated_cache_home):
    # THE papercut: structurally perfect, and the run WILL abort on `reports` (the crawled
    # example.test graph only has “Add Report”). Still 200, still `ok`, still SAVABLE — but
    # the warning names the step and the control the site really has.
    r = client.post("/api/flows/validate", json=_goal_doc("reports"))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"                        # NOT an error — advisory only
    assert len(body["warnings"]) == 1
    w = body["warnings"][0]
    assert w["path"] == "steps[1].body[0]"              # the NESTED path — flow.py's grammar
    assert w["op"] == "do" and w["goal"] == "reports"
    assert "reports" in w["message"] and HOST in w["message"]
    assert w["candidates"] == ["Add Report"]

    # the same document with the goal the graph actually answers: no warnings at all.
    ok = client.post("/api/flows/validate", json=_goal_doc("report")).json()
    assert ok["status"] == "ok" and ok["warnings"] == []


def test_flows_validate_never_warns_for_an_uncrawled_host(isolated_cache_home):
    # A flow may be authored BEFORE the crawl. "Not crawled" must never become "not valid".
    body = client.post("/api/flows/validate", json=_goal_doc("reports")).json()
    assert body["status"] == "ok" and body["warnings"] == []


def test_a_flow_with_a_warning_still_saves(populated_cache_home):
    # Savable is the point: the warning rides along on the create response, and the document
    # is really persisted (a warning is not a blocker).
    r = client.post("/api/hosts/%s/flows" % HOST, json=_goal_doc("reports"))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert [w["path"] for w in body["warnings"]] == ["steps[1].body[0]"]
    fid = body["id"]
    assert client.get("/api/hosts/%s/flows/%s" % (HOST, fid)).json()["doc"]["name"] == "g"

    # …and an UPDATE that fixes the goal drops the warning.
    r = client.put("/api/hosts/%s/flows/%s" % (HOST, fid), json=_goal_doc("report"))
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert r.json()["warnings"] == []


def test_flows_op_schema_is_the_real_tables():
    # The ONLY serialization of the op vocabulary — the canvas builds its edit forms from
    # it. Asserted against flow.py's tables themselves, so the route CANNOT drift from the
    # validator (add an op and this keeps passing; hand-list one and it fails).
    from pinchtab_webgraph import flow as flow_mod

    r = client.get("/api/flows/op_schema")
    assert r.status_code == 200
    body = r.json()

    assert set(body["leaf_ops"]) == set(flow_mod.LEAF_OPS)
    assert set(body["body_ops"]) == set(flow_mod.BODY_OPS)
    for op, spec in flow_mod.LEAF_OPS.items():
        assert body["leaf_ops"][op] == {k: list(v) for k, v in spec.items()}
    for op, spec in flow_mod.BODY_OPS.items():
        assert body["body_ops"][op] == {k: list(v) for k, v in spec.items()}
    assert body["capabilities"] == flow_mod.DEFAULT_CAPABILITIES
    assert body["write_ops"] == sorted(flow_mod.WRITE_OPS)
    assert body["body_vars"] == {op: sorted(v) for op, v in flow_mod.BODY_VARS.items()}
    assert body["max_depth"] == flow_mod.MAX_DEPTH
    assert body["max_steps"] == flow_mod.MAX_STEPS


def test_flows_schema_stateless():
    r = client.post("/api/flows/schema",
                    json=_flow_doc(inputs={"since": {"type": "string", "required": True},
                                           "n": {"type": "integer", "default": 5}}))
    assert r.status_code == 200
    assert r.json() == {"type": "object", "additionalProperties": False,
                        "required": ["since"],
                        "properties": {"since": {"type": "string"},
                                       "n": {"type": "integer", "default": 5}}}

    r = client.post("/api/flows/schema", json={"steps": []})
    assert r.status_code == 200 and r.json()["status"] == "invalid"


# --- the flow-run WS gate + the crawl<->flow cross-veto -----------------------

def test_ws_flow_run_disabled_by_default(isolated_cache_home, monkeypatch):
    monkeypatch.delenv("PINCHTAB_WEBGRAPH_ENABLE_FLOWS", raising=False)
    fid = client.post("/api/hosts/%s/flows" % FLOW_HOST, json=_flow_doc()).json()["id"]
    with client.websocket_connect(
            "/ws/flows/run?host=%s&flow_id=%s" % (FLOW_HOST, fid)) as ws:
        f = ws.receive_json()
        assert f["type"] == "error" and f["status"] == "flow_unavailable"
        assert f["reason"] == "disabled"


def test_ws_flow_run_rejects_bad_flow_id_and_missing_flow(isolated_cache_home, monkeypatch):
    monkeypatch.setenv("PINCHTAB_WEBGRAPH_ENABLE_FLOWS", "1")
    with client.websocket_connect(
            "/ws/flows/run?host=%s&flow_id=NOTHEX" % FLOW_HOST) as ws:
        assert ws.receive_json()["status"] == "invalid_flow"
    with client.websocket_connect(
            "/ws/flows/run?host=%s&flow_id=%s" % (FLOW_HOST, "0" * 32)) as ws:
        assert ws.receive_json()["status"] == "flow_not_found"


def test_crawl_is_vetoed_by_a_live_flow_run(monkeypatch):
    # ONE single-tenant bridge, ONE driver: a live flow run must lock a crawl out.
    monkeypatch.setenv("PINCHTAB_WEBGRAPH_ENABLE_CRAWL", "1")
    ui_server.app.state.live_flow_runs = 1
    try:
        with client.websocket_connect("/ws/crawl?url=http://example.test/") as ws:
            f = ws.receive_json()
            assert f["type"] == "error" and f["status"] == "too_many_sessions"
    finally:
        ui_server.app.state.live_flow_runs = 0


def test_live_flow_run_is_vetoed_by_a_crawl(isolated_cache_home, monkeypatch):
    # …and symmetrically: a crawl in flight locks a LIVE (non-dry) flow run out.
    monkeypatch.setenv("PINCHTAB_WEBGRAPH_ENABLE_FLOWS", "1")
    fid = client.post("/api/hosts/%s/flows" % FLOW_HOST, json=_flow_doc()).json()["id"]
    ui_server.app.state.live_crawls = 1
    try:
        with client.websocket_connect(
                "/ws/flows/run?host=%s&flow_id=%s" % (FLOW_HOST, fid)) as ws:
            assert ws.receive_json()["type"] == "flow"
            ws.send_json({"type": "run", "inputs": {}, "dry_run": False})
            f = ws.receive_json()
            assert f["type"] == "error" and f["status"] == "too_many_sessions"
    finally:
        ui_server.app.state.live_crawls = 0


def test_dry_run_is_not_vetoed_by_a_crawl(isolated_cache_home, monkeypatch):
    # A DRY run touches no browser at all, so it neither needs the bridge nor waits for it.
    monkeypatch.setenv("PINCHTAB_WEBGRAPH_ENABLE_FLOWS", "1")
    fid = client.post("/api/hosts/%s/flows" % FLOW_HOST, json=_flow_doc()).json()["id"]

    from contextlib import asynccontextmanager as _acm
    from tests.test_ui_flow_runner import FakeProcess

    proc = FakeProcess(stdout_lines=[b'{"type":"result","status":"ok","steps":[]}\n'],
                       exits_on_its_own=True)

    @_acm
    async def fake(*, flow_path, host, flow_id, run_id, **_kw):
        yield ui_server.flow_runner.FlowRunSession(process=proc, host=host, flow_id=flow_id,
                                                   run_id=run_id)

    monkeypatch.setattr(ui_server.flow_runner, "open_flow_run_session", fake)
    ui_server.app.state.live_crawls = 1
    try:
        with client.websocket_connect(
                "/ws/flows/run?host=%s&flow_id=%s" % (FLOW_HOST, fid)) as ws:
            assert ws.receive_json()["type"] == "flow"
            ws.send_json({"type": "run", "inputs": {}, "dry_run": True})
            assert ws.receive_json()["type"] == "status"
            assert ws.receive_json()["type"] == "result"
    finally:
        ui_server.app.state.live_crawls = 0


def test_live_flow_run_counter_survives_a_start_run_failure(isolated_cache_home, monkeypatch):
    # flow_store.start_run atomic_writes to disk, so it CAN raise (disk full, read-only fs).
    # If that raise escapes the try that owns the decrementing finally, the live-run count
    # leaks and — MAX_LIVE_FLOW_RUNS being 1, plus the cross-veto — every later flow run AND
    # every later crawl is refused with too_many_sessions until the process restarts.
    monkeypatch.setenv("PINCHTAB_WEBGRAPH_ENABLE_FLOWS", "1")
    fid = client.post("/api/hosts/%s/flows" % FLOW_HOST, json=_flow_doc()).json()["id"]

    from contextlib import asynccontextmanager as _acm
    from tests.test_ui_flow_runner import FakeProcess

    real_start_run = ui_server.flow_store.start_run
    calls = {"n": 0}

    def flaky_start_run(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("No space left on device")   # the FIRST run's placeholder write
        return real_start_run(*args, **kwargs)

    monkeypatch.setattr(ui_server.flow_store, "start_run", flaky_start_run)

    proc = FakeProcess(stdout_lines=[b'{"type":"result","status":"ok","steps":[]}\n'],
                       exits_on_its_own=True)

    @_acm
    async def fake(*, flow_path, host, flow_id, run_id, **_kw):
        yield ui_server.flow_runner.FlowRunSession(process=proc, host=host, flow_id=flow_id,
                                                   run_id=run_id)

    monkeypatch.setattr(ui_server.flow_runner, "open_flow_run_session", fake)

    ui_server.app.state.live_flow_runs = 0
    with pytest.raises(OSError):
        with client.websocket_connect(
                "/ws/flows/run?host=%s&flow_id=%s" % (FLOW_HOST, fid)) as ws:
            assert ws.receive_json()["type"] == "flow"
            ws.send_json({"type": "run", "inputs": {}, "dry_run": False})
            ws.receive_json()          # the socket dies with the server-side OSError
    assert ui_server.app.state.live_flow_runs == 0     # released, not leaked

    # …and the very next LIVE run is served, not refused: the bridge was never really busy.
    with client.websocket_connect(
            "/ws/flows/run?host=%s&flow_id=%s" % (FLOW_HOST, fid)) as ws:
        assert ws.receive_json()["type"] == "flow"
        ws.send_json({"type": "run", "inputs": {}, "dry_run": False})
        f = ws.receive_json()
        assert f["type"] == "status", f              # NOT {"status": "too_many_sessions"}
        assert ws.receive_json()["type"] == "result"
    assert ui_server.app.state.live_flow_runs == 0


# --- POST /api/flows/uploads — staging a file for a `file` input ---------------
#
# The browser cannot hand the server a local path, so it POSTs the RAW BYTES here and the
# server stages them; the returned absolute path is what the run frame's `inputs` map carries.
# This endpoint writes attacker-supplied bytes to disk from an unauthenticated local UI, so
# the traversal guard and the size cap are the load-bearing tests.

def _uploads_root(home):
    return home / "uploads"


def test_upload_stages_the_bytes_and_returns_the_path(isolated_cache_home):
    r = client.post("/api/flows/uploads?name=invoice.pdf", content=b"%PDF-1.4 hello")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["name"] == "invoice.pdf"
    assert body["size"] == len(b"%PDF-1.4 hello")

    staged = pathlib.Path(body["path"])
    assert staged.is_absolute() and staged.read_bytes() == b"%PDF-1.4 hello"
    # …under <HOME>/uploads/<uuid4hex>/<name>, never anywhere else.
    assert staged.parent.parent == _uploads_root(isolated_cache_home)
    assert len(staged.parent.name) == 32 and staged.name == "invoice.pdf"


def test_two_uploads_of_the_same_name_do_not_collide(isolated_cache_home):
    a = client.post("/api/flows/uploads?name=doc.pdf", content=b"AAA").json()
    b = client.post("/api/flows/uploads?name=doc.pdf", content=b"BBBB").json()
    assert a["path"] != b["path"]                     # a fresh uuid dir per upload
    assert pathlib.Path(a["path"]).read_bytes() == b"AAA"     # …so neither overwrote the other
    assert pathlib.Path(b["path"]).read_bytes() == b"BBBB"


# The name has TWO separate jobs, and the endpoint treats them separately: a path is REJECTED
# (that is the traversal guard), while an ordinary filename that merely contains characters we
# would rather not put on disk is SANITISED. `Invoice Jan 2026.pdf` is a real file, not an
# attack — the owner would hit it on their first upload — so a space must not be a 400.

@pytest.mark.parametrize("name", ["../../x.pdf", "/etc/passwd", "a/b.pdf", "..\\..\\x.pdf",
                                  "..", ".", "", "   "])
def test_upload_rejects_a_traversing_or_illegal_name(isolated_cache_home, name):
    r = client.post("/api/flows/uploads", params={"name": name}, content=b"pwned")
    assert r.status_code == 400
    assert r.json()["status"] == "invalid_name"
    # NOTHING was written — not inside the staging dir, and not outside it either.
    assert not _uploads_root(isolated_cache_home).exists()
    assert not (isolated_cache_home / "x").exists()
    assert not (isolated_cache_home / "x.pdf").exists()


@pytest.mark.parametrize("name,stored", [
    ("Invoice Jan 2026.pdf", "Invoice_Jan_2026.pdf"),   # a space is not an attack
    ("report (final).pdf", "report_final.pdf"),         # …nor are parentheses
    ("rapport-été.pdf", "rapport-_t.pdf"),              # …nor an accent
    ("rm -rf; ls & echo.txt", "rm_-rf_ls_echo.txt"),    # shell metacharacters: inert on disk
])
def test_upload_sanitises_an_ordinary_name_instead_of_rejecting_it(isolated_cache_home,
                                                                   name, stored):
    r = client.post("/api/flows/uploads", params={"name": name}, content=b"%PDF-1.4 hi")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["name"] == name                  # the ORIGINAL, for the UI to show
    assert body["stored_name"] == stored         # …and the sanitised one that is on disk

    staged = pathlib.Path(body["path"])
    assert staged.name == stored and staged.read_bytes() == b"%PDF-1.4 hi"
    # …and it really is INSIDE the staging root: no escape, whatever the name contained.
    root = str(_uploads_root(isolated_cache_home).resolve()) + os.sep
    assert os.path.realpath(str(staged)).startswith(root)


def test_upload_caps_a_pathological_name_but_keeps_the_extension(isolated_cache_home):
    body = client.post("/api/flows/uploads", params={"name": "A" * 500 + ".pdf"},
                       content=b"x").json()
    stored = body["stored_name"]
    assert len(stored) <= ui_server.MAX_STORED_NAME     # the filesystem limit is never hit
    assert stored.endswith(".pdf") and stored.startswith("AAAA")
    assert body["name"] == "A" * 500 + ".pdf"          # the original is still echoed in full
    assert pathlib.Path(body["path"]).name == stored


def test_two_originals_that_sanitise_alike_still_do_not_collide(isolated_cache_home):
    a = client.post("/api/flows/uploads", params={"name": "my report.pdf"},
                    content=b"AAA").json()
    b = client.post("/api/flows/uploads", params={"name": "my;report.pdf"},
                    content=b"BBBB").json()
    assert a["stored_name"] == b["stored_name"] == "my_report.pdf"   # same name on disk…
    assert a["path"] != b["path"]                                    # …different uuid4 dirs
    assert pathlib.Path(a["path"]).read_bytes() == b"AAA"            # …so neither overwrote
    assert pathlib.Path(b["path"]).read_bytes() == b"BBBB"


def test_upload_over_the_cap_is_413_and_deletes_the_partial(isolated_cache_home, monkeypatch):
    monkeypatch.setattr(ui_server, "MAX_UPLOAD_BYTES", 16)
    r = client.post("/api/flows/uploads?name=big.bin", content=b"x" * 64)
    assert r.status_code == 413
    body = r.json()
    assert body["status"] == "too_large" and body["max_bytes"] == 16
    # the partial file (and its whole staging dir) is gone — no half-written turd on disk.
    assert list(_uploads_root(isolated_cache_home).glob("*/*")) == []


def test_upload_prunes_stale_staging_dirs(isolated_cache_home, monkeypatch):
    stale = _uploads_root(isolated_cache_home) / ("0" * 32)
    stale.mkdir(parents=True)
    (stale / "old.pdf").write_bytes(b"old")
    old = time.time() - (ui_server.UPLOAD_TTL_S + 60)
    os.utime(stale, (old, old))

    fresh = client.post("/api/flows/uploads?name=new.pdf", content=b"new").json()
    assert not stale.exists()                          # pruned on the way past…
    assert pathlib.Path(fresh["path"]).exists()        # …and the new upload is untouched


def test_upload_prune_failure_never_fails_the_request(isolated_cache_home):
    # Pruning is housekeeping, not the caller's problem: make listdir(uploads) itself raise
    # (uploads exists but is a FILE, so the makedirs below fails too) and assert the request
    # still gets a clean, structured answer instead of a 500 stack trace.
    _uploads_root(isolated_cache_home).write_bytes(b"not a directory")
    r = client.post("/api/flows/uploads?name=ok.pdf", content=b"z")
    assert r.status_code == 500 and r.json()["status"] == "error"   # not an unhandled crash

    _uploads_root(isolated_cache_home).unlink()
    assert client.post("/api/flows/uploads?name=ok.pdf",
                       content=b"z").json()["status"] == "ok"


# --- a `file` input end-to-end: schema, then the run WS ------------------------

def _file_flow_doc():
    return _flow_doc(inputs={"file": {"type": "file", "required": True}},
                     capabilities={"allow_upload": True},
                     steps=[{"op": "upload", "selector": "#f", "file": "${file}"}])


def test_flow_schema_route_publishes_a_file_input_as_a_path_string(isolated_cache_home):
    fid = client.post("/api/hosts/%s/flows" % FLOW_HOST, json=_file_flow_doc()).json()["id"]
    r = client.get("/api/hosts/%s/flows/%s/schema" % (FLOW_HOST, fid))
    assert r.status_code == 200
    assert r.json()["properties"]["file"] == {"type": "string", "format": "path"}


def test_ws_run_with_a_missing_file_is_invalid_input_and_keeps_the_socket_open(
        isolated_cache_home, monkeypatch):
    # The whole point of validating the path at bind time: the user sees a readable error
    # INLINE and can just pick another file — the socket stays open for the next Run.
    monkeypatch.setenv("PINCHTAB_WEBGRAPH_ENABLE_FLOWS", "1")
    fid = client.post("/api/hosts/%s/flows" % FLOW_HOST, json=_file_flow_doc()).json()["id"]
    with client.websocket_connect(
            "/ws/flows/run?host=%s&flow_id=%s" % (FLOW_HOST, fid)) as ws:
        assert ws.receive_json()["type"] == "flow"
        ws.send_json({"type": "run", "inputs": {"file": "/no/such/file.pdf"},
                      "dry_run": True})
        f = ws.receive_json()
        assert f["type"] == "error" and f["status"] == "invalid_input"
        assert "no such file" in f["detail"] and "/no/such/file.pdf" in f["detail"]
        # STILL OPEN — the missing input is not a fatal protocol error.
        ws.send_json({"type": "run", "inputs": {}, "dry_run": True})
        assert ws.receive_json()["status"] == "invalid_input"


def test_ws_run_accepts_a_staged_upload_path(isolated_cache_home, monkeypatch):
    # The contract joined up: POST the bytes -> get a staged path -> pass it as the file input.
    monkeypatch.setenv("PINCHTAB_WEBGRAPH_ENABLE_FLOWS", "1")
    staged = client.post("/api/flows/uploads?name=invoice.pdf", content=b"%PDF").json()["path"]
    fid = client.post("/api/hosts/%s/flows" % FLOW_HOST, json=_file_flow_doc()).json()["id"]

    from contextlib import asynccontextmanager as _acm
    from tests.test_ui_flow_runner import FakeProcess

    proc = FakeProcess(stdout_lines=[b'{"type":"result","status":"ok","steps":[]}\n'],
                       exits_on_its_own=True)
    seen = {}

    @_acm
    async def fake(*, flow_path, host, flow_id, run_id, inputs=None, **_kw):
        seen["inputs"] = inputs
        yield ui_server.flow_runner.FlowRunSession(process=proc, host=host, flow_id=flow_id,
                                                   run_id=run_id)

    monkeypatch.setattr(ui_server.flow_runner, "open_flow_run_session", fake)
    with client.websocket_connect(
            "/ws/flows/run?host=%s&flow_id=%s" % (FLOW_HOST, fid)) as ws:
        assert ws.receive_json()["type"] == "flow"
        ws.send_json({"type": "run", "inputs": {"file": staged}, "dry_run": True})
        assert ws.receive_json()["type"] == "status"
        assert ws.receive_json()["type"] == "result"
    assert seen["inputs"] == {"file": staged}       # the staged path reached the runner
