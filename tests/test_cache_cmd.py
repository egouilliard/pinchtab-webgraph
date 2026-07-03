"""Tests for the `cache` subcommand (cache_cmd.main) via an isolated cache home."""
import sys

import pytest

from pinchtab_webgraph import cache_cmd, cache_store


def run(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["cache", *args])
    return cache_cmd.main()


def test_list_empty(monkeypatch, isolated_cache_home, capsys):
    assert run(monkeypatch, "list") == 0
    assert "No caches" in capsys.readouterr().out


def test_list_populated(monkeypatch, populated_cache_home, capsys):
    assert run(monkeypatch, "list") == 0
    out = capsys.readouterr().out
    assert "example.test" in out
    assert "states" in out


def test_path(monkeypatch, isolated_cache_home, capsys):
    assert run(monkeypatch, "path", "example.test") == 0
    out = capsys.readouterr().out.strip()
    assert out.endswith("example.test.json")


def test_show_hit(monkeypatch, populated_cache_home, capsys):
    assert run(monkeypatch, "show", "example.test") == 0
    out = capsys.readouterr().out
    assert "example.test" in out


def test_show_miss(monkeypatch, isolated_cache_home, capsys):
    assert run(monkeypatch, "show", "absent.example.com") == 1
    assert "no cache" in capsys.readouterr().err


def test_clear_dry_run_keeps_file(monkeypatch, populated_cache_home, capsys):
    assert run(monkeypatch, "clear", "example.test") == 0
    assert "DRY RUN" in capsys.readouterr().out
    # dry run must NOT delete
    assert cache_store.list_hosts() == ["example.test"]


def test_clear_yes_deletes(monkeypatch, populated_cache_home, capsys):
    assert run(monkeypatch, "clear", "example.test", "--yes") == 0
    assert "Removed 1" in capsys.readouterr().out
    assert cache_store.list_hosts() == []


def test_clear_all_yes(monkeypatch, isolated_cache_home, capsys):
    graph = {"meta": {}, "states": [], "state_index": {}, "edges": [], "triggers": []}
    cache_store.atomic_write("a.example.com", graph)
    cache_store.atomic_write("b.example.com", graph)
    assert run(monkeypatch, "clear", "--all", "--yes") == 0
    assert cache_store.list_hosts() == []


def test_clear_needs_target(monkeypatch, isolated_cache_home):
    with pytest.raises(SystemExit):
        run(monkeypatch, "clear")
