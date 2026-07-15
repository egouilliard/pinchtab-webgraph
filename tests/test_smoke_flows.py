"""SMOKE suite for the Flows product — the "is it wired up?" tripwire.

Fast, browser-free, bridge-free. A CI run with only the `ui` extra installed exercises
the whole critical surface an owner depends on through the FastAPI TestClient plus a few
direct calls into flow.py / the MCP tool registry:

  1. the Flows REST surface is reachable and shaped right (op vocabulary, CRUD + run
     history round-trip, validate's ok/warnings verdict, the upload endpoint's happy path
     and traversal reject, and a `file` input published as a path string);
  2. the SAFETY invariants hold at the API/tool layer (the run WS is gated off without the
     env flag; the agent tool surface has propose_flow but NO save/run verb; the flow-mode
     allow-list is exactly OFFLINE ∪ {propose_flow});
  3. typed inputs behave (one of every declared type validates; a missing required file and
     an out-of-set enum are both REJECTED at the bind boundary).

This is deliberately NOT a re-run of every edge case in test_ui_server.py / test_mcp_server.py
/ test_flow.py — it imports the same helpers and asserts the same load-bearing contracts, so
a single fast file catches a regression that unwires the product. If it goes red, the Flows
feature is broken at a level a browser test would only find later and slower.
"""
import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from pinchtab_webgraph import flow as flow_mod
from pinchtab_webgraph.ui import chat
from pinchtab_webgraph.ui.server import app

client = TestClient(app)

FLOW_HOST = "flow.example.com"


def _flow_doc(name="smoke-flow", **extra):
    doc = {"name": name, "steps": [{"op": "goto", "url": "https://flow.example.com/x"}]}
    doc.update(extra)
    return doc


# --- 1. the Flows REST surface -----------------------------------------------

def test_op_schema_publishes_the_whole_14_op_vocabulary():
    # 12 leaf ops + 2 body ops = the 14 the canvas draws its forms from. Asserted against
    # flow.py's own tables so the route can never silently drift from the validator.
    r = client.get("/api/flows/op_schema")
    assert r.status_code == 200
    body = r.json()
    assert set(body["leaf_ops"]) == set(flow_mod.LEAF_OPS)
    assert set(body["body_ops"]) == set(flow_mod.BODY_OPS)
    assert len(body["leaf_ops"]) + len(body["body_ops"]) == 14
    assert body["capabilities"] == flow_mod.DEFAULT_CAPABILITIES
    assert body["write_ops"] == sorted(flow_mod.WRITE_OPS)


def test_flow_crud_and_run_history_round_trip(isolated_cache_home):
    # empty -> create -> get -> list -> (record a run) -> run history -> delete.
    assert client.get("/api/hosts/%s/flows" % FLOW_HOST).json()["flows"] == []

    r = client.post("/api/hosts/%s/flows" % FLOW_HOST,
                    json=_flow_doc(inputs={"since": {"type": "string"}}))
    assert r.status_code == 200 and r.json()["status"] == "ok"
    fid = r.json()["id"]

    assert client.get("/api/hosts/%s/flows/%s" % (FLOW_HOST, fid)).json()["doc"]["name"] \
        == "smoke-flow"
    assert [f["id"] for f in client.get("/api/hosts/%s/flows" % FLOW_HOST).json()["flows"]] \
        == [fid]

    # a recorded run shows up in the flow's history and cascades away with the flow.
    from pinchtab_webgraph.ui.server import flow_store
    rid = flow_store.new_run_id()
    flow_store.start_run(FLOW_HOST, fid, rid, dry_run=True, capabilities={}, inputs={})
    flow_store.finish_run(FLOW_HOST, fid, rid,
                          {"status": "ok", "steps": [{"op": "goto", "status": "ok"}],
                           "stats": {"steps_executed": 1}})
    runs = client.get("/api/hosts/%s/flows/%s/runs" % (FLOW_HOST, fid)).json()["runs"]
    assert len(runs) == 1 and runs[0]["id"] == rid and runs[0]["status"] == "ok"

    r = client.delete("/api/hosts/%s/flows/%s" % (FLOW_HOST, fid))
    assert r.status_code == 200 and r.json()["deleted"] is True
    assert client.get("/api/hosts/%s/flows/%s" % (FLOW_HOST, fid)).status_code == 404
    # idempotent: deleting an absent flow is a green false, never a 404.
    assert client.delete("/api/hosts/%s/flows/%s"
                         % (FLOW_HOST, fid)).json()["deleted"] is False


def test_validate_returns_ok_and_a_warnings_array(populated_cache_home):
    # A structurally perfect doc is `ok`; when a `goal` cannot resolve against the crawled
    # host graph the same answer stays `ok` (savable) but carries an advisory warning that
    # names the offending step — the exact contract the editor's amber banner is built on.
    HOST = "example.test"                                  # the seeded, crawled host
    good = client.post("/api/flows/validate", json=_flow_doc("g", host=HOST))
    assert good.status_code == 200
    assert good.json()["status"] == "ok" and good.json()["warnings"] == []

    doc = {"name": "g", "host": HOST,
           "steps": [{"op": "goto", "url": "https://%s/x" % HOST},
                     {"op": "paginate", "max_pages": 2,
                      "body": [{"op": "do", "goal": "reports"}]}]}
    warned = client.post("/api/flows/validate", json=doc).json()
    assert warned["status"] == "ok"                        # advisory only — NOT invalid
    assert len(warned["warnings"]) == 1
    w = warned["warnings"][0]
    assert w["op"] == "do" and w["goal"] == "reports"
    assert "candidates" in w                               # the field the UI lists suggestions from

    # a structural miss, by contrast, is invalid (still a 200 with status in the body).
    bad = client.post("/api/flows/validate", json={"name": "x", "steps": []})
    assert bad.status_code == 200 and bad.json()["status"] == "invalid"


def test_upload_endpoint_happy_path_and_traversal_reject(isolated_cache_home):
    ok = client.post("/api/flows/uploads?name=invoice.pdf", content=b"%PDF-1.4 hi")
    assert ok.status_code == 200
    body = ok.json()
    assert body["status"] == "ok" and body["name"] == "invoice.pdf"
    import pathlib
    assert pathlib.Path(body["path"]).is_absolute()
    assert pathlib.Path(body["path"]).read_bytes() == b"%PDF-1.4 hi"

    # a path (traversal) is the ONE thing that earns a 400, and it writes nothing.
    bad = client.post("/api/flows/uploads", params={"name": "../../etc/passwd"},
                      content=b"pwned")
    assert bad.status_code == 400 and bad.json()["status"] == "invalid_name"


def test_flow_schema_route_maps_a_file_input_to_a_path_string(isolated_cache_home):
    doc = _flow_doc(inputs={"file": {"type": "file", "required": True}},
                    capabilities={"allow_upload": True},
                    steps=[{"op": "upload", "selector": "#f", "file": "${file}"}])
    fid = client.post("/api/hosts/%s/flows" % FLOW_HOST, json=doc).json()["id"]
    r = client.get("/api/hosts/%s/flows/%s/schema" % (FLOW_HOST, fid))
    assert r.status_code == 200
    # a file input's VALUE is a local path — never `{"type":"file"}` (not valid JSON Schema).
    assert r.json()["properties"]["file"] == {"type": "string", "format": "path"}


# --- 2. the safety invariants ------------------------------------------------

def test_run_ws_is_gated_off_without_the_env_flag(isolated_cache_home, monkeypatch):
    # The most dangerous route in the app: OFF unless PINCHTAB_WEBGRAPH_ENABLE_FLOWS is set.
    monkeypatch.delenv("PINCHTAB_WEBGRAPH_ENABLE_FLOWS", raising=False)
    fid = client.post("/api/hosts/%s/flows" % FLOW_HOST, json=_flow_doc()).json()["id"]
    with client.websocket_connect(
            "/ws/flows/run?host=%s&flow_id=%s" % (FLOW_HOST, fid)) as ws:
        f = ws.receive_json()
        assert f["type"] == "error" and f["status"] == "flow_unavailable"
        assert f["reason"] == "disabled"


def test_mcp_surface_has_propose_flow_but_no_save_or_run_verb():
    # THE authority invariant, checked against the REAL registered tool list: the agent can
    # PROPOSE a flow (pure, echoes a draft) but has no code path to persist or execute one —
    # only the human's Save/Run buttons can. A new save/run flow tool must trip this.
    pytest.importorskip("mcp")
    import asyncio
    from pinchtab_webgraph import mcp_server
    names = sorted(t.name for t in asyncio.run(mcp_server.mcp.list_tools()))
    assert [n for n in names if "flow" in n] == ["propose_flow"]
    for forbidden in ("save_flow", "create_flow", "update_flow", "delete_flow",
                      "run_flow", "flow_run", "execute_flow"):
        assert forbidden not in names


def test_flow_mode_allowlist_is_exactly_offline_plus_propose_flow():
    # The lockdown, asserted at the tool-surface layer (no SDK needed): a flow-mode agent
    # gets the 6 read-only OFFLINE tools plus the single propose_flow verb — nothing else —
    # and a workspace-mode agent does NOT get propose_flow.
    assert chat.effective_tool_names("flow") == chat.OFFLINE_TOOL_NAMES | {"propose_flow"}
    assert chat.effective_tool_names("workspace") == set(chat.OFFLINE_TOOL_NAMES)
    assert "propose_flow" not in chat.effective_tool_names("workspace")
    assert len(chat.effective_tool_names("flow")) == 7


# --- 3. typed inputs ---------------------------------------------------------

def _typed_inputs_doc():
    return {"name": "typed", "host": "h.test",
            "inputs": {"s": {"type": "string"}, "n": {"type": "number"},
                       "i": {"type": "integer"}, "b": {"type": "boolean"},
                       "f": {"type": "file"},
                       "e": {"type": "string", "enum": ["x", "y"]}},
            "steps": [{"op": "goto", "url": "https://h.test/"}]}


def test_a_flow_with_one_input_of_every_type_validates():
    doc = _typed_inputs_doc()
    # the whole document validates (raises nothing) …
    flow_mod.validate(doc)
    # … and the stateless validate route agrees, echoing every declared input.
    body = client.post("/api/flows/validate", json=doc).json()
    assert body["status"] == "ok"
    assert set(body["inputs"]) == {"s", "n", "i", "b", "f", "e"}


def test_bind_inputs_rejects_a_missing_required_file():
    doc = {"name": "u", "inputs": {"file": {"type": "file", "required": True}},
           "capabilities": {"allow_upload": True},
           "steps": [{"op": "upload", "selector": "#f", "file": "${file}"}]}
    # nothing supplied for a REQUIRED file → rejected at the bind boundary, not mid-run.
    with pytest.raises(flow_mod.FlowError) as ei:
        flow_mod.bind_inputs(doc, {})
    assert ei.value.path == "inputs"

    # …and a path that does not name a real file is rejected too (readable, actionable).
    with pytest.raises(flow_mod.FlowError) as ei2:
        flow_mod.bind_inputs(doc, {"file": "/no/such/file.pdf"})
    assert "no such file" in str(ei2.value)


def test_bind_inputs_rejects_an_enum_value_outside_the_set():
    doc = {"name": "e", "inputs": {"choice": {"type": "string", "enum": ["x", "y"]}},
           "steps": [{"op": "goto", "url": "https://h.test/"}]}
    # an in-set value binds fine …
    assert flow_mod.bind_inputs(doc, {"choice": "x"}) == {"choice": "x"}
    # … and a value outside the constrained choice is rejected, not passed through.
    with pytest.raises(flow_mod.FlowError) as ei:
        flow_mod.bind_inputs(doc, {"choice": "z"})
    assert ei.value.path == "inputs" and "one of" in str(ei.value)
