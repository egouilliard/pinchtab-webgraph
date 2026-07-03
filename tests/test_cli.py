"""Tests for the unified CLI dispatch (cli.main)."""
import pytest

from pinchtab_webgraph import cli


def test_no_args_shows_help(capsys):
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert "commands:" in out
    # both new subcommands are advertised
    assert "login" in out
    assert "cache" in out


def test_unknown_subcommand_returns_2(capsys):
    assert cli.main(["definitely-not-a-command"]) == 2


@pytest.mark.parametrize("name", list(cli.SUBS))
def test_subcommand_help_exits_zero(name):
    # each subcommand forwards --help to its module's argparse, which exits 0
    with pytest.raises(SystemExit) as exc:
        cli.main([name, "--help"])
    assert exc.value.code == 0


def test_new_subcommands_registered():
    assert "login" in cli.SUBS
    assert "cache" in cli.SUBS
    assert cli.SUBS["cache"][0] == "pinchtab_webgraph.cache_cmd"
    assert cli.SUBS["login"][0] == "pinchtab_webgraph.login"


def test_ask_rejects_start_without_scheme(capsys):
    # `--start` without a scheme -> urlparse().hostname is None; ask must reject it
    # with a clean usage error (exit 2) rather than let cache_path() raise ValueError.
    with pytest.raises(SystemExit) as exc:
        cli.main(["ask", "--start", "example.com/page", "--goal", "add item"])
    assert exc.value.code == 2
    assert "scheme" in capsys.readouterr().err
