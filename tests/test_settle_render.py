"""The in-page settle waits for a RENDERED page, not for the first quiet moment.

Runs recipe._SETTLE_JS itself in Node against a fake `document` whose control and
loading-indicator counts follow a time schedule — the shape a real SPA renders in:
shell, then a partial page, then the rest. The old rule (two equal reads one poll
apart, count > 3) returned on the first plateau; on a live app that meant states read
and clicked — by positional selector — on a half-drawn page, which recorded a sidebar
link as leading nowhere and lost every screen behind it.
"""
import json
import shutil
import subprocess

import pytest

from pinchtab_webgraph import recipe

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="needs node to run the in-page JS")

HARNESS = r"""
const schedule = %s;          // the page from each t onwards (shape below)
const t0 = Date.now();
const now = () => { const t = Date.now() - t0; let cur = schedule[0];
                    for (const s of schedule) if (t >= s[0]) cur = s; return cur; };
const box = { getBoundingClientRect: () => ({ width: 10, height: 10 }) };
// [t, controls, pending(skeleton/aria-busy), activity(spinner)] — activity optional
global.document = { querySelectorAll: (sel) => Array(
  sel.includes('aria-busy') ? now()[2] : sel.includes('animate-spin') ? (now()[3] || 0) : now()[1]
).fill(box) };
const started = Date.now();
Promise.resolve(%s).then((n) => console.log(JSON.stringify({ n, ms: Date.now() - started })));
"""


def run_settle(schedule, render_ms=4000, poll_ms=20, delay_ms=0, stable_ms=300):
    js = recipe._SETTLE_JS % (render_ms, poll_ms, delay_ms, stable_ms)
    out = subprocess.run([NODE, "-e", HARNESS % (json.dumps(schedule), js)],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_does_not_stop_on_a_short_plateau():
    # 36 controls for 250ms, then the page's own content arrives: 49.
    r = run_settle([[0, 0, 0], [100, 36, 0], [350, 49, 0]])
    assert r["n"] == 49
    assert r["ms"] >= 350 + 300


def test_a_blank_page_is_never_settled_early():
    # Nothing has rendered yet: waiting is right, up to the cap.
    r = run_settle([[0, 0, 0]], render_ms=600)
    assert r["n"] == 0
    assert r["ms"] >= 600


def test_skeletons_being_replaced_hold_the_settle():
    # The control count holds still while skeleton placeholders are swapped out.
    r = run_settle([[0, 40, 6], [500, 40, 0]])
    assert r["ms"] >= 500 + 300


def test_a_permanent_spinner_does_not_block():
    # One spinner that never goes away is part of the page, not a loading state.
    r = run_settle([[0, 0, 0, 1], [100, 30, 0, 1]], render_ms=4000)
    assert r["n"] == 30
    assert r["ms"] < 1500


def test_a_spinner_going_away_is_a_change():
    # Gone before the window elapsed: the window restarts from that change.
    r = run_settle([[0, 30, 0, 1], [200, 30, 0, 0]], render_ms=4000)
    assert r["ms"] >= 200 + 300


def test_a_settled_page_returns_after_the_window_not_the_cap():
    r = run_settle([[0, 12, 0]], render_ms=4000)
    assert r["n"] == 12
    assert 300 <= r["ms"] < 1000


def test_settle_passes_the_stable_window(monkeypatch):
    seen = {}

    def fake_pt(args, server, timeout=None):
        seen["js"], seen["timeout"] = args[-1], timeout
        return 0, "1", ""

    monkeypatch.setattr(recipe, "pt", fake_pt)
    monkeypatch.setattr(recipe, "SETTLE_STABLE_MS", 1234)
    recipe.settle("http://localhost:9871")
    assert seen["js"].rstrip().endswith("1234)")
    assert seen["timeout"] >= recipe.RENDER_MS / 1000.0
