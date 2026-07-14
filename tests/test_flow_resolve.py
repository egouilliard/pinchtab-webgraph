"""Tests for flow_resolve — "does this flow's `goal` name anything on the crawled site?"

The gap this closes: flow.validate proves SHAPE only, so a `goto` whose goal doesn't resolve
validates GREEN, saves, runs, and only then aborts. These warnings are the authoring-time
answer — and they are WARNINGS: an uncrawled host must never become an error.

Driven against the shared interaction fixture (host example.test), whose triggers are
“Create Role”, “Add Report” (both reachable) and “Add Widget” (matched but UNREACHABLE).
Note the papercut itself is in the fixture: goal "report" resolves, the plural "reports"
does NOT — which is exactly the miss a human writes.
"""
import json

import pytest

from pinchtab_webgraph import cache_store, flow, flow_resolve


@pytest.fixture
def graph(sample_interaction_graph_path):
    return str(sample_interaction_graph_path)


def _doc(*steps, **extra):
    doc = {"name": "f", "host": "example.test", "steps": list(steps)}
    doc.update(extra)
    return flow.validate(doc)          # every doc here is STRUCTURALLY valid, on purpose


# --- the pure walker (flow.goal_targets) --------------------------------------

def test_goal_targets_skips_url_gotos_and_runtime_vars():
    doc = _doc({"op": "goto", "url": "https://example.test/x"},
               {"op": "goto", "goal": "reports"},
               {"op": "do", "goal": "${name}"},
               inputs={"name": {"type": "string"}})
    # a url `goto` never consults the graph, and a `${…}` goal is only known at run time.
    assert [t["path"] for t in flow.goal_targets(doc)] == ["steps[1]"]


def test_goal_targets_uses_flow_pys_nested_path_grammar():
    doc = _doc({"op": "goto", "goal": "report"},
               {"op": "paginate", "body": [
                   {"op": "for_each", "match": {"kind": "download"}, "as": "item", "body": [
                       {"op": "do", "goal": "widgets"},
                   ]},
               ]})
    assert [t["path"] for t in flow.goal_targets(doc)] == [
        "steps[0]", "steps[1].body[0].body[0]"]
    # for_each's `match` is an OBJECT selector, not a graph goal — it must not be walked.
    assert [t["op"] for t in flow.goal_targets(doc)] == ["goto", "do"]


# --- the check ----------------------------------------------------------------

def test_a_goal_that_resolves_produces_no_warning(graph):
    doc = _doc({"op": "goto", "goal": "report"})
    assert flow_resolve.warnings_against_graph(doc, graph, host="example.test") == []


def test_a_goal_that_does_not_resolve_warns_with_path_and_candidates(graph):
    doc = _doc({"op": "goto", "goal": "reports"})
    warns = flow_resolve.warnings_against_graph(doc, graph, host="example.test")
    assert len(warns) == 1
    w = warns[0]
    assert w["path"] == "steps[0]" and w["op"] == "goto" and w["goal"] == "reports"
    assert "reports" in w["message"] and "example.test" in w["message"]
    # the whole value of the check: name the control the site ACTUALLY has.
    assert w["candidates"] == ["Add Report"]


def test_a_do_step_is_checked_too(graph):
    doc = _doc({"op": "do", "goal": "invoices"})
    warns = flow_resolve.warnings_against_graph(doc, graph, host="example.test")
    assert len(warns) == 1 and warns[0]["path"] == "steps[0]" and warns[0]["op"] == "do"


def test_a_match_regex_is_checked_too(graph):
    doc = _doc({"op": "goto", "match": "Nothing Like This"})
    warns = flow_resolve.warnings_against_graph(doc, graph, host="example.test")
    assert len(warns) == 1 and warns[0]["match"] == "Nothing Like This"
    assert warns[0]["goal"] is None

    # …and a `match` that DOES hit a trigger label is silent.
    assert flow_resolve.warnings_against_graph(
        _doc({"op": "goto", "match": "Add Report"}), graph, host="example.test") == []


def test_a_nested_step_warns_at_its_nested_path(graph):
    doc = _doc({"op": "goto", "goal": "report"},
               {"op": "paginate", "max_pages": 2, "body": [
                   {"op": "do", "goal": "reports"},
               ]})
    warns = flow_resolve.warnings_against_graph(doc, graph, host="example.test")
    # the path is the NESTED one — that is what makes the canvas able to light up the box.
    assert [w["path"] for w in warns] == ["steps[1].body[0]"]


def test_a_matched_but_unreachable_trigger_warns_with_resolvers_candidates(graph):
    # "Add Widget" exists but sits on an orphan state — resolve_action says `unreachable`
    # and hands back the candidate labels itself.
    doc = _doc({"op": "goto", "goal": "widget"})
    warns = flow_resolve.warnings_against_graph(doc, graph, host="example.test")
    assert len(warns) == 1
    assert warns[0]["candidates"] == ["Add Widget"]
    assert "reaches it" in warns[0]["message"]


def test_a_corrupt_graph_is_silent_not_an_exception(tmp_path):
    bad = tmp_path / "broken.json"
    bad.write_text("{not json")
    doc = _doc({"op": "goto", "goal": "reports"})
    assert flow_resolve.warnings_against_graph(doc, str(bad), host="example.test") == []


# --- "not crawled" is never an error ------------------------------------------

def test_no_host_and_no_cache_mean_no_warnings(isolated_cache_home):
    # a flow may legitimately be authored BEFORE the crawl, or target only url-gotos.
    no_host = flow.validate({"name": "f", "steps": [{"op": "goto", "goal": "reports"}]})
    assert flow_resolve.graph_path_for(no_host) is None
    assert flow_resolve.warnings_for_doc(no_host) == []

    uncrawled = _doc({"op": "goto", "goal": "reports"})          # host example.test, no cache
    assert flow_resolve.graph_path_for(uncrawled) is None
    assert flow_resolve.warnings_for_doc(uncrawled) == []


def test_a_non_hostname_host_token_is_silent(isolated_cache_home):
    doc = {"name": "f", "host": "../../etc/passwd", "steps": [{"op": "goto", "goal": "x"}]}
    assert flow_resolve.graph_path_for(doc) is None      # never leaves caches_dir()
    assert flow_resolve.warnings_for_doc(doc) == []


def test_warnings_for_doc_reads_the_hosts_cache(populated_cache_home):
    # the end-to-end shape: doc["host"] -> cache_store.cache_path -> the graph.
    doc = _doc({"op": "goto", "goal": "reports"})
    warns = flow_resolve.warnings_for_doc(doc)
    assert [w["path"] for w in warns] == ["steps[0]"]
    assert warns[0]["candidates"] == ["Add Report"]

    # and the same doc against a host whose cache SAYS the goal resolves is silent.
    assert flow_resolve.warnings_for_doc(_doc({"op": "goto", "goal": "report"})) == []


def test_a_stale_cache_is_re_read_not_memoized(populated_cache_home):
    # a warning must follow the GRAPH, not a cached verdict: re-crawl, and it goes away.
    doc = _doc({"op": "goto", "goal": "reports"})
    assert len(flow_resolve.warnings_for_doc(doc)) == 1

    with open(cache_store.cache_path("example.test")) as fh:
        graph = json.load(fh)
    for t in graph["triggers"]:
        if t["label"] == "Add Report":
            t["label"] = "Add Reports"           # the site renamed the button
    cache_store.atomic_write("example.test", graph)
    assert flow_resolve.warnings_for_doc(doc) == []
