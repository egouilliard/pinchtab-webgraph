"""Tests for pinchtab_webgraph.api — the print-free, dict-returning query surface.

Each group hits a hit-path and a miss/error status, checked against the
hand-authored fixtures (which are provably correct against the real algorithms).
api functions take an explicit graph_path, so no cache isolation is needed here.
"""
from pinchtab_webgraph import api


# --- graph_summary -----------------------------------------------------------

def test_graph_summary_interaction(sample_interaction_graph_path):
    out = api.graph_summary(sample_interaction_graph_path)
    assert out["graph_kind"] == "interaction"
    assert out["states"] == 5
    assert out["edges"] == 3
    assert out["triggers"] == 3
    assert out["meta"]["host"] == "example.test"


def test_graph_summary_link(sample_link_graph_path):
    out = api.graph_summary(sample_link_graph_path)
    assert out["graph_kind"] == "link"
    assert out["nodes"] == 9
    assert out["edges"] == 11


# --- howto -------------------------------------------------------------------

def test_howto_ok(sample_interaction_graph_path):
    out = api.howto(sample_interaction_graph_path, goal="create role")
    assert out["status"] == "ok"
    assert len(out["results"]) == 1
    r = out["results"][0]
    assert r["trigger_label"] == "Create Role"
    assert r["state_id"] == "s2"
    assert r["clicks"] == 3  # 2 edges from root + the trigger click
    assert r["form"]["fieldCount"] == 1
    assert out["candidates"] == []


def test_howto_reports_opens_at(sample_interaction_graph_path):
    out = api.howto(sample_interaction_graph_path, goal="add report")
    assert out["status"] == "ok"
    r = out["results"][0]
    assert r["clicks"] == 2  # 1 edge from root + trigger click
    assert r["opens_at"] == "https://example.test/reports/new"


def test_howto_unreachable(sample_interaction_graph_path):
    out = api.howto(sample_interaction_graph_path, goal="add widget")
    assert out["status"] == "unreachable"
    assert out["results"] == []
    assert "Add Widget" in out["candidates"]


def test_howto_no_match(sample_interaction_graph_path):
    out = api.howto(sample_interaction_graph_path, goal="create nonexistent")
    assert out["status"] == "no_match"
    assert out["results"] == []


def test_howto_match_regex(sample_interaction_graph_path):
    out = api.howto(sample_interaction_graph_path, match="Report")
    assert out["status"] == "ok"
    assert out["results"][0]["trigger_label"] == "Add Report"


def test_howto_no_goal_no_match_is_invalid(sample_interaction_graph_path):
    # neither goal nor match → up-front guard; must NOT broad-match every trigger
    out = api.howto(sample_interaction_graph_path)
    assert out["status"] == "invalid_args"
    assert out["results"] == []
    assert out["candidates"] == []


# --- find_content ------------------------------------------------------------

def test_find_content_hit(sample_interaction_graph_path):
    out = api.find_content(sample_interaction_graph_path, "Alice")
    assert out["status"] == "ok"
    assert out["total_matches"] == 1
    assert out["views_matched"] == 1
    v = out["views"][0]
    assert v["view_label"] == "Team"
    assert v["reachable"] is True
    assert v["items"][0]["text"] == "Alice Martin"


def test_find_content_table(sample_interaction_graph_path):
    out = api.find_content(sample_interaction_graph_path, "Q1 Report")
    assert out["status"] == "ok"
    assert out["total_matches"] == 1
    assert out["views"][0]["view_label"] == "Reports"


def test_find_content_miss(sample_interaction_graph_path):
    out = api.find_content(sample_interaction_graph_path, "nothinghere")
    assert out["status"] == "no_match"
    assert out["total_matches"] == 0
    assert out["views"] == []


# --- list_content ------------------------------------------------------------

def test_list_content(sample_interaction_graph_path):
    out = api.list_content(sample_interaction_graph_path)
    assert out["status"] == "ok"
    labels = {v["view_label"] for v in out["views"]}
    assert labels == {"Team", "Reports"}
    reports = next(v for v in out["views"] if v["view_label"] == "Reports")
    assert reports["collections"][0]["kind"] == "table"
    assert reports["collections"][0]["count"] == 2


def test_list_content_empty(sample_link_graph_path):
    # a link graph has no `states`/`collections` → empty
    out = api.list_content(sample_link_graph_path)
    assert out["status"] == "empty"
    assert out["views"] == []


# --- list_forms --------------------------------------------------------------

def test_list_forms(sample_interaction_graph_path):
    out = api.list_forms(sample_interaction_graph_path)
    assert out["meta"]["host"] == "example.test"
    assert out["meta"]["triggers"] == 3
    labels = [f["label"] for f in out["forms"]]
    assert set(labels) == {"Create Role", "Add Report", "Add Widget"}
    # sorted by (state_url, label.lower()): /orphan < /reports < /team/roles
    assert labels == ["Add Widget", "Add Report", "Create Role"]
    cr = next(f for f in out["forms"] if f["label"] == "Create Role")
    assert cr["clicks"] == 3
    assert cr["field_count"] == 1


# --- link_paths --------------------------------------------------------------

def test_link_paths_shortest(sample_link_graph_path):
    out = api.link_paths(sample_link_graph_path, "home", "guide")
    assert out["status"] == "ok"
    assert out["shortest"]["clicks"] == 1  # home -> guide direct edge
    assert out["from"]["id"] == "home"
    assert out["to"]["id"] == "guide"


def test_link_paths_all(sample_link_graph_path):
    out = api.link_paths(sample_link_graph_path, "home", "guide", all=True)
    assert out["status"] == "ok"
    assert len(out["all_paths"]) == 2  # direct + via docs
    clicks = sorted(p["clicks"] for p in out["all_paths"])
    assert clicks == [1, 2]


def test_link_paths_structural_no_path(sample_link_graph_path):
    # every inbound edge to dashboard is a hub (glob) edge → structural has no path
    out = api.link_paths(sample_link_graph_path, "home", "dashboard", structural=True)
    assert out["status"] == "no_path"
    assert out["shortest"] is None


def test_link_paths_nonstructural_dashboard(sample_link_graph_path):
    out = api.link_paths(sample_link_graph_path, "home", "dashboard")
    assert out["status"] == "ok"
    assert out["shortest"]["clicks"] == 1


def test_link_paths_ambiguous_to(sample_link_graph_path):
    # "port" is a substring of both "reports" and "import-center"
    out = api.link_paths(sample_link_graph_path, "home", "port")
    assert out["status"] == "ambiguous_to"
    assert len(out["candidates"]) == 2


def test_link_paths_not_found_from(sample_link_graph_path):
    out = api.link_paths(sample_link_graph_path, "zzz", "guide")
    assert out["status"] == "not_found_from"
    assert out["candidates"] == []
