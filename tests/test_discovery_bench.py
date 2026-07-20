"""Machine-independent regression guard for the single-round-trip settle.

recipe.py's cold "how do I do X" discovery used to poll the control count with ONE
`pinchtab eval` subprocess PER poll (N spawns + Python sleeps per settle); folding
that poll loop into ONE `eval --await-promise` cut the bridge ROUND-TRIPS per run.
This test pins the reduction without depending on the wall clock: it drives recipe.py
against tests/fixtures/fake_pinchtab.py (a synthetic, app-agnostic bridge) with all
latency zeroed and asserts, per scenario, that the run FOUND the trigger, took the
SHORTEST click path, and stayed under a round-trip ceiling the single-round-trip
settle satisfies but the old multi-poll settle would blow past.

The ceilings carry margin over the measured numbers (EASY 16 / MEDIUM 28 / HARD 46),
so ordinary drift never trips them — only a regression back to per-poll settling
(which multiplies eval calls per state) would. stdlib only; no network, no login.
"""
import json
import os
import subprocess
import sys

import pytest

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "fake_pinchtab.py")

# (name, trigger_tab, start_path, expected shortest clicks, round-trip ceiling)
SCENARIOS = [
    ("EASY",   0, "/settings", 2, 20),
    ("MEDIUM", 2, "/settings", 2, 32),
    ("HARD",   2, "/",         3, 48),
]


@pytest.fixture
def fake_bridge(tmp_path):
    """A `pinchtab` executable (the fake bridge) on PATH, zero latency, fresh log."""
    exe = tmp_path / "pinchtab"
    exe.write_text(open(FIXTURE).read())
    exe.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = str(tmp_path) + os.pathsep + env.get("PATH", "")
    env.update(FAKEPT_EVAL_MS="0", FAKEPT_NAV_MS="0", FAKEPT_CLICK_MS="0",
               FAKEPT_SETTLE_POLLS="0", FAKEPT_BASE="https://synthetic.test")
    return env, tmp_path


@pytest.mark.parametrize("name, trigger_tab, start_path, clicks, max_round_trips",
                         SCENARIOS)
def test_discovery_finds_trigger_within_round_trip_budget(
        fake_bridge, name, trigger_tab, start_path, clicks, max_round_trips):
    env, tmp_path = fake_bridge
    log = tmp_path / ("calls-%s.log" % name)
    out = tmp_path / ("recipe-%s" % name)
    env = dict(env, FAKEPT_STATE=str(tmp_path / ("state-%s.json" % name)),
               FAKEPT_LOG=str(log), FAKEPT_TRIGGER_TAB=str(trigger_tab))
    r = subprocess.run(
        [sys.executable, "-m", "pinchtab_webgraph.recipe", "--goal", "add widget",
         "--start", "https://synthetic.test" + start_path, "--out", str(out)],
        env=env, capture_output=True, text=True, timeout=60)

    assert r.returncode == 0, "recipe.py failed:\n%s" % r.stderr
    assert "HOW TO" in r.stdout, "discovery did not report a how-to:\n%s" % r.stdout

    rec = json.loads((tmp_path / ("recipe-%s.json" % name)).read_text())
    assert rec["trigger"] == "Add Widget"
    assert rec["triggerKind"] == "form"
    # shortest (fewest-click) path — BFS guarantees the first match is shortest
    assert rec["shortestClicks"] == clicks

    # bridge round-trips = one log line per `pinchtab` invocation
    round_trips = sum(1 for _ in open(log))
    assert round_trips <= max_round_trips, (
        "%s took %d round-trips (ceiling %d) — the single-round-trip settle may have "
        "regressed to per-poll settling" % (name, round_trips, max_round_trips))


def test_settle_never_raises_on_bridge_timeout(monkeypatch):
    """settle() must swallow a subprocess timeout instead of propagating it.

    Folding the poll loop into ONE `eval --await-promise` put the whole wait behind a
    single hard kill timeout, where the old Python-side loop structurally could not
    raise. login.perform_login calls settle() with NO try/except of its own (and
    interaction_crawl reaches it at crawl setup, before the crawl loop's scaffolding),
    so a bridge hiccup must not take the process down."""
    from pinchtab_webgraph import recipe

    def timeout(args, server, timeout=60):
        raise subprocess.TimeoutExpired(cmd=["pinchtab"], timeout=timeout)

    monkeypatch.setattr(recipe, "pt", timeout)
    recipe.settle("http://localhost:9871")      # must return, not raise


def test_time_budget_stops_discovery_early(fake_bridge):
    """--time-budget caps worst-case discovery: a trigger 3 clicks deep, a slow bridge,
    and a budget far under the time it needs => discovery stops and says so, instead of
    exploring to --max-discover. Default (0) is off and is covered by the scenarios above."""
    env, tmp_path = fake_bridge
    log = tmp_path / "calls-budget.log"
    env = dict(env, FAKEPT_STATE=str(tmp_path / "state-budget.json"),
               FAKEPT_LOG=str(log), FAKEPT_TRIGGER_TAB="2",
               FAKEPT_NAV_MS="400", FAKEPT_CLICK_MS="400")   # slow enough to trip the cap
    r = subprocess.run(
        [sys.executable, "-m", "pinchtab_webgraph.recipe", "--goal", "add widget",
         "--start", "https://synthetic.test/", "--time-budget", "0.5",
         "--out", str(tmp_path / "recipe-budget")],
        env=env, capture_output=True, text=True, timeout=60)

    assert "time budget" in r.stderr, (
        "discovery did not report hitting the budget:\n%s" % r.stderr)
    # it stopped before reaching the 3-clicks-deep trigger, so it reports a miss (rc!=0)
    assert r.returncode != 0
    assert "Could not find" in r.stderr
