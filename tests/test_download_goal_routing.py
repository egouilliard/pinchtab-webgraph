"""A download goal must route to the download the NOUN names — not merely the first one.

Regression for a bug found by driving `perform` against a live site with two download
links on one page: `--goal "download the logo"` performed the *Q3 report* download.

The cause was vocabulary, in two places at once:
  - `download` was not in VERBS, so goal_needle's verb+noun regex could never match a
    download label, and every download goal fell through to the noun-only fallback;
  - `download` was not in GOAL_STOPWORDS, so it survived goal_nouns() as a *noun* — and
    the fallback ORs the nouns, so the shared word "download" matched EVERY download
    control. Both triggers then tied on path length and graph order silently decided.

The fix splits the two vocabularies: ACTION_VERBS (download/export) join VERBS to form
GOAL_VERBS for goal-matching only. VERBS itself must stay create-only, because
interaction_crawl's TRIGGER_RE keys on it to decide what to CLICK during a crawl — the
crawler records download controls and must never click them (a JS download can pop a
native save dialog). test_discovery_never_clicks_a_download_control pins that apart.
"""
import re

import pytest

from pinchtab_webgraph import api, interaction_crawl, recipe


@pytest.mark.parametrize("goal, expect_label, expect_file", [
    ("download the logo", "Download the logo", "logo.png"),
    ("download the q3 report", "Download the Q3 report", "q3-report.pdf"),
    # the verb is not load-bearing — the noun alone must still pick the right one
    ("logo", "Download the logo", "logo.png"),
    # a synonym in the same family routes the same way
    ("export the logo", "Download the logo", "logo.png"),
])
def test_download_goal_routes_by_noun(two_downloads_graph_path, goal, expect_label,
                                      expect_file):
    plan = api.resolve_action(two_downloads_graph_path, goal=goal)
    assert plan["status"] == "ok"
    assert plan["trigger_label"] == expect_label
    assert plan["action_kind"] == "download"
    assert plan["download_url"].endswith(expect_file)


def test_download_goal_does_not_also_match_the_other_download(two_downloads_graph_path):
    # the discriminating property: exactly ONE of the two same-page downloads is routed,
    # so there is no tie for graph order to break arbitrarily.
    plan = api.resolve_action(two_downloads_graph_path, goal="download the logo")
    assert plan["match_count"] == 1


def test_create_goals_still_route(two_downloads_graph_path):
    # the create-verb path must be untouched by the new vocabulary.
    for goal, label in [("create team", "Create team"), ("add document", "Add document")]:
        plan = api.resolve_action(two_downloads_graph_path, goal=goal)
        assert plan["status"] == "ok"
        assert plan["trigger_label"] == label
        assert plan["action_kind"] == "form"


def test_action_verbs_are_not_nouns():
    # a verb left in as a noun is what made the fallback match every download control.
    assert recipe.goal_nouns("download the logo") == ["logo"]
    assert recipe.goal_nouns("export the report") == ["report"]


def test_discovery_never_clicks_a_download_control():
    """VERBS drives what the CRAWLER clicks; it must not learn the download verbs."""
    assert not re.search(r"\bdownload\b", recipe.VERBS)
    assert not interaction_crawl.TRIGGER_RE.search("Download the Q3 report")
    # ...while the GOAL vocabulary does know them.
    assert re.search(r"\bdownload\b", recipe.GOAL_VERBS)
    assert re.search(recipe.goal_needle("download the logo"), "Download the logo", re.I)
