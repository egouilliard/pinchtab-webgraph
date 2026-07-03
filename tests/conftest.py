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
