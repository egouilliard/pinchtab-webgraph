"""Tests for cache_store's pure list/clear helpers, against an isolated cache home."""
import json

import pytest

from pinchtab_webgraph import cache_store


def test_list_hosts_empty(isolated_cache_home):
    assert cache_store.list_hosts() == []


# --- cache_path host validation (path-traversal guard) -----------------------

@pytest.mark.parametrize("bad", ["../evil", "a/b", "", "a\\b", "../../etc/passwd"])
def test_cache_path_rejects_unsafe_host(isolated_cache_home, bad):
    with pytest.raises(ValueError):
        cache_store.cache_path(bad)


def test_cache_path_accepts_real_host(isolated_cache_home):
    # a legitimate hostname (letters/digits/dots/hyphens) still resolves inside caches/
    p = cache_store.cache_path("go-staging.leyton.com")
    assert p.endswith("/caches/go-staging.leyton.com.json")
    assert str(isolated_cache_home) in p


def test_clear_traversal_raises_and_touches_nothing(isolated_cache_home):
    # A sentinel OUTSIDE caches/ that a traversal (`../victim`) would resolve to.
    victim = isolated_cache_home / "victim"
    victim.write_text("keep me")
    with pytest.raises(ValueError):
        cache_store.clear("../victim")
    # ValueError fired before any filesystem access → the sentinel is untouched.
    assert victim.exists()
    assert victim.read_text() == "keep me"


def test_list_hosts_sorted_and_excludes_tmp(isolated_cache_home):
    graph = {"meta": {}, "states": [], "state_index": {}, "edges": [], "triggers": []}
    cache_store.atomic_write("b.example.com", graph)
    cache_store.atomic_write("a.example.com", graph)
    # a stray .json.tmp (as atomic_write would leave mid-write) must be ignored
    (isolated_cache_home / "caches" / "c.example.com.json.tmp").write_text("{}")
    assert cache_store.list_hosts() == ["a.example.com", "b.example.com"]


def test_list_hosts_populated(populated_cache_home):
    assert cache_store.list_hosts() == ["example.test"]
    g = cache_store.load("example.test")
    assert g["meta"]["host"] == "example.test"


def test_clear_present(populated_cache_home):
    assert cache_store.clear("example.test") is True
    assert cache_store.list_hosts() == []


def test_clear_absent(isolated_cache_home):
    assert cache_store.clear("nope.example.com") is False


def test_clear_all(isolated_cache_home):
    graph = {"meta": {}, "states": [], "state_index": {}, "edges": [], "triggers": []}
    cache_store.atomic_write("x.example.com", graph)
    cache_store.atomic_write("y.example.com", graph)
    removed = cache_store.clear_all()
    assert removed == ["x.example.com", "y.example.com"]
    assert cache_store.list_hosts() == []


def test_clear_all_empty(isolated_cache_home):
    assert cache_store.clear_all() == []
