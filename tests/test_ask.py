"""Tests for pinchtab_webgraph.ask — the --all-hosts cross-host content surface.

ask.main() reads argv and calls sys.exit(), so each run is driven in-process by
patching sys.argv and catching SystemExit. The two_hosts_cache_home fixture sets
PINCHTAB_WEBGRAPH_HOME, which ask reads (via cache_store) at call time — so the
cross-host enumeration sees both example.test and shop.test with no browser.
"""
import os

import pytest

from pinchtab_webgraph import ask, cache_store


def _break_cache(host="broken.test"):
    """Write an unparseable cache file so the resilience path is exercised for real."""
    with open(os.path.join(cache_store.caches_dir(), "%s.json" % host), "w") as f:
        f.write("{ not json")


def _run(monkeypatch, capsys, argv):
    monkeypatch.setattr("sys.argv", ["pwg ask"] + argv)
    with pytest.raises(SystemExit) as exc:
        ask.main()
    out = capsys.readouterr().out
    return exc.value.code, out


def test_all_hosts_find(monkeypatch, capsys, two_hosts_cache_home):
    code, out = _run(monkeypatch, capsys, ["--find", "alice", "--all-hosts"])
    assert code == 0
    # both hosts are labeled in the merged, ranked output.
    assert "[example.test]" in out and "[shop.test]" in out


def test_all_hosts_find_miss(monkeypatch, capsys, two_hosts_cache_home):
    code, out = _run(monkeypatch, capsys, ["--find", "nothinghere", "--all-hosts"])
    assert code == 2


def test_all_hosts_list_content(monkeypatch, capsys, two_hosts_cache_home):
    code, out = _run(monkeypatch, capsys, ["--list-content", "--all-hosts"])
    assert code == 0
    assert "[example.test]" in out and "[shop.test]" in out


def test_all_hosts_requires_content_op(monkeypatch, capsys, two_hosts_cache_home):
    # --all-hosts with --goal is a usage error (exit 2).
    code, out = _run(monkeypatch, capsys,
                     ["--goal", "x", "--all-hosts", "--start", "https://example.test/"])
    assert code == 2


def test_all_hosts_rejects_graph(monkeypatch, capsys, two_hosts_cache_home,
                                 sample_interaction_graph_path):
    # --graph pins ONE explicit file, which contradicts spanning every host cache.
    code, _ = _run(monkeypatch, capsys,
                   ["--find", "alice", "--all-hosts", "--graph",
                    str(sample_interaction_graph_path)])
    assert code == 2


def test_start_required_without_all_hosts(monkeypatch, capsys, two_hosts_cache_home):
    # --start stays required for the single-host path (it is what routes the cache).
    code, _ = _run(monkeypatch, capsys, ["--find", "alice"])
    assert code == 2


def test_all_hosts_with_no_caches_exits(monkeypatch, capsys, isolated_cache_home):
    # Nothing crawled yet: fail with an actionable hint rather than an empty result.
    code, _ = _run(monkeypatch, capsys, ["--find", "alice", "--all-hosts"])
    assert code != 0


# --- no silent caps (regressions) --------------------------------------------

def test_all_hosts_find_limit_signals_truncation(monkeypatch, capsys,
                                                 two_hosts_cache_home):
    # REGRESSION: a --limit that drops whole views used to render silently, so the
    # header ("N items across M views") read as complete when only one view showed.
    code, out = _run(monkeypatch, capsys,
                     ["--find", "alice", "--all-hosts", "--limit", "1"])
    assert code == 0
    assert "raise --limit" in out


def test_all_hosts_find_unlimited_has_no_cap_notice(monkeypatch, capsys,
                                                    two_hosts_cache_home):
    # ... and the notice must NOT appear when nothing was actually capped.
    code, out = _run(monkeypatch, capsys, ["--find", "alice", "--all-hosts"])
    assert code == 0
    assert "raise --limit" not in out


def test_all_hosts_find_warns_on_unreadable_cache(monkeypatch, capsys,
                                                  two_hosts_cache_home):
    # REGRESSION: an unreadable cache was skipped silently on --find, understating
    # the result set while still counting the host as "searched".
    _break_cache()
    code, out = _run(monkeypatch, capsys, ["--find", "alice", "--all-hosts"])
    assert code == 0
    assert "broken.test" in out and "could not be read" in out
    # the good hosts still answer — one bad cache never fails the whole query.
    assert "[example.test]" in out and "[shop.test]" in out


def test_all_hosts_find_warns_on_unreadable_cache_on_miss(monkeypatch, capsys,
                                                          two_hosts_cache_home):
    # A miss with a corrupt cache must not read as an authoritative "nothing here".
    _break_cache()
    code, out = _run(monkeypatch, capsys, ["--find", "zzznope", "--all-hosts"])
    assert code == 2
    assert "could not be read" in out
