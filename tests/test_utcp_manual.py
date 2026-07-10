"""Tests for pinchtab_webgraph.utcp_manual — the UTCP manual builder + subcommand.

Manual generation is pure-stdlib, so most tests need no extra. The final test
validates the manual against the REAL utcp model, guarded by importorskip so the
base suite runs without the [utcp] extra installed. The `--serve` smoke test is
guarded so it skips where listening sockets are unavailable.
"""
import json
import re
import threading
from pathlib import Path
from urllib.request import urlopen

import pytest

from pinchtab_webgraph import __version__, utcp_manual

REPO_ROOT = Path(__file__).resolve().parent.parent
COMMITTED_MANUAL = REPO_ROOT / "utcp-manual.json"

_TOKEN_RE = re.compile(r"UTCP_ARG_([A-Za-z0-9_]+?)_UTCP_END")


# --- shape -------------------------------------------------------------------

def test_build_manual_shape():
    m = utcp_manual.build_manual()
    # top keys EXACTLY {utcp_version, manual_version, tools} — no `info`.
    assert set(m.keys()) == {"utcp_version", "manual_version", "tools"}
    assert m["utcp_version"] == "1.1"
    assert m["manual_version"] == __version__
    assert len(m["tools"]) == 9

    for t in m["tools"]:
        assert set(t.keys()) == {"name", "description", "inputs", "outputs",
                                 "tags", "tool_call_template"}
        tct = t["tool_call_template"]
        assert tct["name"] == t["name"]                 # the call template carries its own name
        assert tct["call_template_type"] == "cli"
        assert len(tct["commands"]) == 1
        step = tct["commands"][0]
        assert step["append_to_final_output"] is True
        assert isinstance(step["command"], str)

        command = step["command"]
        required = set(t["inputs"]["required"])
        tokens = set(_TOKEN_RE.findall(command))
        # every required input has a matching UTCP_ARG_<name>_UTCP_END token ...
        assert required == tokens, (t["name"], required, tokens)
        # ... and no token exists for a name that is not a required input.
        for name in tokens:
            assert name in required


def test_tool_names_are_clean_and_expected():
    names = [t["name"] for t in utcp_manual.build_manual()["tools"]]
    assert names == ["graph_summary", "howto", "find_content", "list_content",
                     "list_forms", "link_paths", "crawl", "ask", "perform"]
    # clean, unprefixed tool names (namespaced by the manual, not the tool name).
    assert all("pwg" not in n and "query" not in n for n in names)


def test_link_paths_uses_frm_token():
    m = utcp_manual.build_manual()
    lp = next(t for t in m["tools"] if t["name"] == "link_paths")
    cmd = lp["tool_call_template"]["commands"][0]["command"]
    assert "UTCP_ARG_frm_UTCP_END" in cmd
    assert "--from UTCP_ARG_frm_UTCP_END" in cmd
    assert "--to UTCP_ARG_to_UTCP_END" in cmd
    assert "--host UTCP_ARG_host_UTCP_END" in cmd


def test_every_arg_token_is_a_flag_value():
    # Every target subcommand consumes its args as flags (no positionals), so
    # each UTCP_ARG_<name>_UTCP_END token MUST be the value of a preceding
    # `--flag` — never a bare positional. Guards against the class of bug where
    # e.g. `pwg crawl UTCP_ARG_start_UTCP_END` is emitted but the crawler
    # requires `--start` (an unfillable command from a UTCP client).
    for tool in utcp_manual.build_manual()["tools"]:
        cmd = tool["tool_call_template"]["commands"][0]["command"]
        tokens = cmd.split()
        for i, tok in enumerate(tokens):
            if _TOKEN_RE.fullmatch(tok):
                assert i > 0 and tokens[i - 1].startswith("--"), (
                    "%s: token %r is not preceded by a --flag in %r"
                    % (tool["name"], tok, cmd)
                )


# --- committed file in sync --------------------------------------------------

def test_manual_in_sync():
    # the committed utcp-manual.json must equal what build_manual() produces now.
    assert COMMITTED_MANUAL.exists(), "run: pwg manual --out utcp-manual.json"
    committed = json.loads(COMMITTED_MANUAL.read_text())
    assert committed == utcp_manual.build_manual()


# --- main() / --out ----------------------------------------------------------

def test_manual_stdout(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["pwg manual"])
    assert utcp_manual.main() == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed == utcp_manual.build_manual()


def test_manual_out_file(monkeypatch, capsys, tmp_path):
    out = tmp_path / "m.json"
    monkeypatch.setattr("sys.argv", ["pwg manual", "--out", str(out)])
    assert utcp_manual.main() == 0
    # --out writes the file and prints NOTHING to stdout.
    assert capsys.readouterr().out == ""
    assert json.loads(out.read_text()) == utcp_manual.build_manual()


# --- _route (no socket) ------------------------------------------------------

def test_route_utcp():
    code, body, ctype = utcp_manual._route("/utcp")
    assert code == 200
    assert ctype == "application/json"
    assert json.loads(body) == utcp_manual.build_manual()


def test_route_well_known():
    code, body, ctype = utcp_manual._route("/.well-known/utcp")
    assert code == 200
    assert json.loads(body) == utcp_manual.build_manual()


def test_route_404():
    code, body, ctype = utcp_manual._route("/nope")
    assert code == 404
    assert ctype == "application/json"
    assert json.loads(body)["error"] == "not found"


# --- serve smoke (guarded: skips where listening sockets are unavailable) ----

def test_serve_smoke():
    from http.server import ThreadingHTTPServer
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", 0), utcp_manual._Handler)
    except OSError:
        pytest.skip("listening sockets unavailable")
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        with urlopen("http://127.0.0.1:%d/utcp" % port, timeout=5) as resp:
            payload = json.loads(resp.read())
        assert payload == utcp_manual.build_manual()
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


# --- validates against the real utcp model (gated) ---------------------------

def test_manual_validates_against_utcp():
    pytest.importorskip("utcp")
    try:
        from utcp.data.utcp_manual import UtcpManual
    except ImportError as e:
        pytest.skip("utcp UtcpManual import path differs: %s" % e)
    # This MUST pass — the manual shape was verified against utcp 1.1.x.
    UtcpManual.model_validate(utcp_manual.build_manual())
