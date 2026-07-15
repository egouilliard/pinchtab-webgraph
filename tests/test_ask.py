"""Tests for pinchtab_webgraph.ask — the --all-hosts cross-host content surface.

ask.main() reads argv and calls sys.exit(), so each run is driven in-process by
patching sys.argv and catching SystemExit. The two_hosts_cache_home fixture sets
PINCHTAB_WEBGRAPH_HOME, which ask reads (via cache_store) at call time — so the
cross-host enumeration sees both example.test and shop.test with no browser.
"""
import pytest

from pinchtab_webgraph import ask


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
