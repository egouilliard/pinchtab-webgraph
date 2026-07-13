"""Shared fixtures for the pinchtab-webgraph test suite.

Two flavours:
  - hand-authored graph fixtures (interaction + link), provably correct against the
    real algorithms — see tests/fixtures/.
  - an isolated cache home (PINCHTAB_WEBGRAPH_HOME pointed at a tmp dir) so the
    cache_store / cache_cmd tests never touch a real ~/.pinchtab-webgraph.
"""
import json
from pathlib import Path

import pytest

from pinchtab_webgraph import cache_store

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def sample_interaction_graph_path():
    return FIXTURES_DIR / "sample_interaction_graph.json"


@pytest.fixture
def sample_link_graph_path():
    return FIXTURES_DIR / "sample_link_graph.json"


@pytest.fixture
def two_downloads_graph_path():
    # Models a crawl of a site with TWO download links on the SAME state: the case where
    # only the goal's NOUN can pick the right one (both labels share "download", both are
    # one click from the root). See test_download_goal_routing.py.
    return FIXTURES_DIR / "two_downloads_graph.json"


@pytest.fixture
def linkedin_guest_graph_path():
    # Models the FIXED interaction crawl of linkedin.com's guest surface (issue #11):
    # a low-confidence create-VERB nav ("Find a NEW job", 0-field) plus two structurally
    # detected form-bearing states ("Sign in" -> /login, "Join now" -> /signup).
    return FIXTURES_DIR / "linkedin_guest_graph.json"


@pytest.fixture
def saas_generic_graph_path():
    # A DIFFERENT site archetype (generic SaaS, no brand): a 0-field create-VERB nav
    # ("New Releases"), a form-bearing sign-up, and a form-bearing contact page — proving
    # the same structural logic generalizes off LinkedIn.
    return FIXTURES_DIR / "saas_generic_graph.json"


@pytest.fixture
def spanish_app_graph_path():
    # Spanish-language site: exercises the ES create-VERBs ("nueva"/"añadir") and a
    # form-bearing "Iniciar sesión" whose label carries NO create-VERB — same code path.
    return FIXTURES_DIR / "spanish_app_graph.json"


@pytest.fixture
def isolated_cache_home(tmp_path, monkeypatch):
    """Point cache_store at an empty tmp home; return that home Path."""
    monkeypatch.setenv("PINCHTAB_WEBGRAPH_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def populated_cache_home(isolated_cache_home):
    """An isolated cache home with one host cache (example.test) written via cache_store."""
    graph = json.loads((FIXTURES_DIR / "sample_interaction_graph.json").read_text())
    cache_store.atomic_write("example.test", graph)
    return isolated_cache_home
