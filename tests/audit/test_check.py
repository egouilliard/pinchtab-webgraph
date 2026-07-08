"""Browser-free unit tests for the audit scorer (tests/audit/check.py). Proves the
Phase-1 `dup_ratio` metric detects same-URL over-noding, and that goal scoring reads
api.howto correctly — no live crawl needed.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import check  # noqa: E402


def test_dup_ratio_detects_same_url_overnoding():
    # 3 states but only 2 distinct normalized URLs (the / and /a re-noded) -> dup_ratio 1
    graph = {"states": [
        {"id": "s0", "url": "https://x.test/"},
        {"id": "s1", "url": "https://x.test/a"},
        {"id": "s2", "url": "https://x.test/a"},          # duplicate of s1
    ]}
    assert check.dup_ratio(graph) == 1


def test_dup_ratio_zero_when_all_unique():
    graph = {"states": [
        {"id": "s0", "url": "https://x.test/"},
        {"id": "s1", "url": "https://x.test/a"},
        {"id": "s2", "url": "https://x.test/b"},
    ]}
    assert check.dup_ratio(graph) == 0


def test_dup_ratio_folds_tracking_params():
    # same page reached with tracking junk must NOT count as a distinct state
    graph = {"states": [
        {"id": "s0", "url": "https://x.test/p"},
        {"id": "s1", "url": "https://x.test/p?utm_source=ad"},
    ]}
    assert check.dup_ratio(graph) == 1


def test_score_goal_no_match_expectation(tmp_path):
    # a graph with no matching trigger; a goal that EXPECTS a refusal passes
    graph = {"meta": {"host": "x.test", "states": 1, "edges": 0, "triggers": 0},
             "states": [{"id": "s0", "url": "https://x.test/"}],
             "state_index": {}, "edges": [], "triggers": []}
    gp = tmp_path / "x.json"
    gp.write_text(json.dumps(graph))
    ok, _ = check.score_goal(str(gp), {"goal": "nonexistent thing",
                                       "expect": {"status": "no_match"}})
    assert ok is True
