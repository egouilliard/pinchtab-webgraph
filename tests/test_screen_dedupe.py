"""Screen dedupe — the fix for the sticky-query-param state explosion (2026-09-22).

The crawler keyed nav-mode states by URL only. An app that keeps UI state in the query
and leaves the previous tab's params in place when you switch tabs renders ONE screen
under many URLs, and every combination was crawled (and content-captured, and form-read)
again. These tests drive the REAL crawl loop (`interaction_crawl.main`) against an
in-process fake bridge serving such an app (tests/fixtures/sticky_site.py) and prove:

  * duplicate URLs of one screen collapse into one state (recorded as aliases),
  * genuinely different screens are NOT merged (same toolbar / different data pages,
    sub-tabs that change the view, a view with volatile content is never wrongly merged),
  * forms on aliased screens are not re-opened and their content isn't re-captured,
  * meta.dedupe reports what was skipped, and the hot-path guard fires when a crawl's
    state budget concentrates on one page,
  * `--no-screen-dedupe` restores the old URL-keyed behaviour (the before/after lever).
"""
import sys
from pathlib import Path

import pytest

from pinchtab_webgraph import interaction_crawl as ic

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
import sticky_site  # noqa: E402
from sticky_site import StickySite, run_crawl, BASE  # noqa: E402


def _settings_screens(graph):
    """The distinct (tab, sub/mode) SCREENS among the recorded /settings states."""
    from urllib.parse import urlsplit, parse_qsl
    out = set()
    for s in graph["states"]:
        u = urlsplit(s["url"])
        if u.path != "/settings":
            continue
        q = dict(parse_qsl(u.query))
        t = int(q.get("tab", 0))
        extra = q.get("sub", "0") if t == 2 else (q.get("mode", "0") if t == 3 else "")
        out.add((t, extra))
    return out


# ---- pure helpers -----------------------------------------------------------------------

def _c(text, href=None, role="tab"):
    return {"text": text, "href": href, "role": role, "tag": "button", "nav": True, "bulk": False}


def test_fingerprint_ignores_the_query_but_not_the_path():
    ctl = [_c("A"), _c("B")]
    scr = {"view": ["A"], "heads": ["A"], "data": ["r1"], "fields": []}
    a = ic.screen_fingerprint("https://x.test/p?tab=a", ctl, scr)
    b = ic.screen_fingerprint("https://x.test/p?tab=a&leftover=z", ctl, scr)
    c = ic.screen_fingerprint("https://x.test/q?tab=a", ctl, scr)
    assert a == b          # same screen under a sticky-param URL → same fingerprint
    assert a != c          # a different PATH is never the same screen


@pytest.mark.parametrize("field,other", [
    ("view", ["B"]), ("heads", ["Other heading"]), ("data", ["r2"]), ("fields", ["q=x"]),
])
def test_fingerprint_separates_screens_that_differ_anywhere(field, other):
    ctl = [_c("A"), _c("B")]
    base = {"view": ["A"], "heads": ["A"], "data": ["r1"], "fields": []}
    changed = dict(base, **{field: other})
    u = "https://x.test/p"
    assert ic.screen_fingerprint(u, ctl, base) != ic.screen_fingerprint(u, ctl, changed)


def test_fingerprint_separates_different_control_sets():
    scr = {"view": [], "heads": [], "data": [], "fields": []}
    u = "https://x.test/p"
    assert ic.screen_fingerprint(u, [_c("A")], scr) != ic.screen_fingerprint(u, [_c("A"), _c("B")], scr)


def test_fingerprint_needs_screen_info():
    # no SCREEN_JS result → no fingerprint → the crawler falls back to URL identity
    assert ic.screen_fingerprint("https://x.test/p", [_c("A")], None) is None


def test_echoed_query_pairs_in_hrefs_are_ignored_but_real_targets_kept():
    scr = {"view": [], "heads": [], "data": [], "fields": []}
    # an app that propagates its current query into every link
    u1, u2 = "https://x.test/p?tab=a", "https://x.test/p?tab=a&sub=z"
    c1 = [_c("Go", href="https://x.test/r?tab=a", role="")]
    c2 = [_c("Go", href="https://x.test/r?tab=a&sub=z", role="")]
    assert ic.screen_fingerprint(u1, c1, scr) == ic.screen_fingerprint(u2, c2, scr)
    # …but a link whose own target differs is a different control
    c3 = [_c("Go", href="https://x.test/r?tab=b", role="")]
    assert ic.screen_fingerprint(u1, c1, scr) != ic.screen_fingerprint(u1, c3, scr)


def test_query_diff_names_the_params_that_did_not_change_the_screen():
    assert ic.query_diff("https://x.test/p?tab=a&sub=z&mode=1", "https://x.test/p?tab=a") == ["mode", "sub"]
    assert ic.query_diff("https://x.test/p?tab=a", "https://x.test/p?tab=a") == []


def test_hot_paths_flags_a_page_that_holds_most_states():
    urls = ["https://x.test/p?v=%d" % i for i in range(50)] + ["https://x.test/q", "https://x.test/r"]
    rows = ic.hot_paths(urls, {"https://x.test/p": 200})
    top = rows[0]
    assert top[0] == "https://x.test/p" and top[1] == 50 and top[2] == 200 and top[3] is True
    assert all(not r[3] for r in rows[1:])
    # a small crawl never warns, however concentrated
    assert not any(r[3] for r in ic.hot_paths(["https://x.test/p?v=%d" % i for i in range(5)]))


# ---- the real crawl loop against the sticky-param fixture ------------------------------

ARGS = ("--max-depth", "4", "--max-states", "300", "--max-visits", "1800")


def test_sticky_params_explode_without_dedupe(monkeypatch, tmp_path):
    # the reproduction: URL-keyed identity mints a state per param combination
    site = StickySite()
    g = run_crawl(monkeypatch, tmp_path, site, *ARGS, "--no-screen-dedupe")
    settings = [s for s in g["states"] if "/settings" in s["url"]]
    screens = _settings_screens(g)
    assert len(settings) > 2 * len(screens)          # many URLs for the same screens
    assert g["meta"]["dedupe"]["enabled"] is False
    assert g["meta"]["dedupe"]["screenAliases"] == 0


def test_dedupe_collapses_sticky_urls_into_one_state_per_screen(monkeypatch, tmp_path):
    site = StickySite()
    g = run_crawl(monkeypatch, tmp_path, site, *ARGS)
    settings = [s for s in g["states"] if "/settings" in s["url"]]
    screens = _settings_screens(g)
    # 8 tabs, tab 2 has 3 sub-tabs, tab 3 has 3 modes → 8 - 2 + 3 + 3 = 12 real screens
    assert len(screens) == 12
    assert len(settings) == len(screens)              # exactly one state per real screen
    d = g["meta"]["dedupe"]
    assert d["enabled"] is True and d["screenAliases"] > 0
    assert sum(d["aliasesPerState"].values()) == d["screenAliases"]
    # the aliases are resolvable: state_index maps an alias URL key to its canonical id
    ids = {s["id"] for s in g["states"]}
    alias_keys = [k for k in g["state_index"] if k not in {"u::" + ic.norm(s["url"]) for s in g["states"]}]
    assert alias_keys and all(g["state_index"][k] in ids for k in alias_keys)
    # inert params were learned (diagnostic) for the settings page
    assert set(d["inertParams"].get(BASE + "/settings", {})) & {"sub", "mode", "tab"}
    assert g["meta"]["complete"] is True


def test_same_toolbar_different_data_pages_are_not_merged(monkeypatch, tmp_path):
    site = StickySite(n_pages=3)
    g = run_crawl(monkeypatch, tmp_path, site, *ARGS)
    pages = sorted(s["url"] for s in g["states"] if "/records" in s["url"])
    assert pages == [BASE + "/records?page=%d" % p for p in (1, 2, 3)]


def test_aliased_screens_do_not_reopen_forms_or_recapture_content(monkeypatch, tmp_path):
    before = StickySite()
    gb = run_crawl(monkeypatch, tmp_path, before, *ARGS, "--no-screen-dedupe")
    after = StickySite()
    tmp2 = tmp_path / "after"
    tmp2.mkdir()
    ga = run_crawl(monkeypatch, tmp2, after, *ARGS)
    # every real form is still read, each exactly once
    labels_b = {t["label"] for t in gb["triggers"] if t["form"]}
    labels_a = {t["label"] for t in ga["triggers"] if t["form"]}
    assert labels_a == labels_b and labels_a
    assert len(after.forms_opened) == len(labels_a)
    assert ga["meta"]["dedupe"]["formReads"] <= gb["meta"]["dedupe"]["formReads"]
    # content capture runs once per real state, not once per URL variant
    assert ga["meta"]["dedupe"]["contentCaptures"] == len(ga["states"])
    assert ga["meta"]["dedupe"]["contentCaptures"] < gb["meta"]["dedupe"]["contentCaptures"]
    # and the whole crawl is much cheaper
    assert after.cost() < 0.5 * before.cost()


def test_same_real_screens_found_with_and_without_dedupe(monkeypatch, tmp_path):
    gb = run_crawl(monkeypatch, tmp_path, StickySite(), *ARGS, "--no-screen-dedupe")
    tmp2 = tmp_path / "after"
    tmp2.mkdir()
    ga = run_crawl(monkeypatch, tmp2, StickySite(), *ARGS)
    assert _settings_screens(ga) == _settings_screens(gb)
    paths = lambda g: {s["url"].split("?")[0] for s in g["states"]}  # noqa: E731
    assert paths(ga) == paths(gb)


def test_volatile_content_is_never_merged_and_trips_the_hot_path_guard(monkeypatch, tmp_path, capsys):
    # A ticking clock inside the data makes every read differ → the fingerprint must NOT
    # merge (under-merge is the safe failure). The explosion then remains, and the guard
    # has to make it LOUD instead of silent.
    site = StickySite(n_tabs=12, volatile=True)
    g = run_crawl(monkeypatch, tmp_path, site, *ARGS, "--chrome-evidence", "0")
    assert g["meta"]["dedupe"]["screenAliases"] == 0
    assert g["meta"]["dedupe"]["warnings"]
    assert any(h["hot"] and h["page"] == BASE + "/settings" for h in g["meta"]["dedupe"]["hotPaths"])
    assert "HOT PATH" in capsys.readouterr().err


def test_hot_path_guard_fires_on_the_undeduped_explosion_but_not_after_the_fix(monkeypatch, tmp_path):
    # a real-app-sized settings page (20 tabs + sub-tabs): legitimately ~26 screens on one
    # path — that must NOT raise the alarm; the URL-keyed explosion of the same site must.
    shape = dict(n_tabs=20, n_sub=5, n_mode=3)
    gb = run_crawl(monkeypatch, tmp_path, StickySite(**shape), *ARGS, "--no-screen-dedupe")
    assert gb["meta"]["dedupe"]["warnings"]
    tmp2 = tmp_path / "after"
    tmp2.mkdir()
    ga = run_crawl(monkeypatch, tmp2, StickySite(**shape), *ARGS)
    assert ga["meta"]["dedupe"]["warnings"] == []
    assert len(ga["states"]) < len(gb["states"]) / 3


def test_chrome_and_select_value_jitter_do_not_split_a_screen(monkeypatch, tmp_path):
    # a sidebar entry that only renders on some reads + a select still showing
    # "Loading…": neither says which screen this is. Chrome is LEARNED (a control seen on
    # >= CHROME_MIN_PAGES different paths) and select-like controls count by role only.
    site = StickySite(chrome_jitter=True)
    g = run_crawl(monkeypatch, tmp_path, site, *ARGS)
    settings = [s for s in g["states"] if "/settings" in s["url"]]
    assert len(settings) == len(_settings_screens(g)) == 12


def test_chrome_learning_is_generic():
    # the pure pieces: a control's identity for chrome learning ignores its selector
    a = {"tag": "a", "role": "", "text": "Help", "selector": "nav>a:nth-of-type(3)"}
    b = dict(a, selector="nav>a:nth-of-type(4)")
    assert ic.control_key(a) == ic.control_key(b)
    scr = {"view": [], "heads": [], "data": [], "fields": []}
    u = "https://x.test/p"
    with_help = [_c("Tab"), dict(a, href=None, nav=True, bulk=False)]
    assert ic.screen_fingerprint(u, with_help, scr, frozenset({ic.control_key(a)})) == \
        ic.screen_fingerprint(u, [_c("Tab")], scr, frozenset({ic.control_key(a)}))
    # without the chrome evidence the extra control DOES separate the screens
    assert ic.screen_fingerprint(u, with_help, scr) != ic.screen_fingerprint(u, [_c("Tab")], scr)
    # a select's shown value is not identity, its presence is
    sel1 = [_c("Loading…", role="combobox")]
    sel2 = [_c("Owner A", role="combobox")]
    assert ic.screen_fingerprint(u, sel1, scr) == ic.screen_fingerprint(u, sel2, scr)
    assert ic.screen_fingerprint(u, sel1, scr) != ic.screen_fingerprint(u, [], scr)


def test_reader_without_screen_info_keeps_url_identity(monkeypatch, tmp_path):
    # e.g. an older fake/bridge that answers only {href, controls}: no aliasing at all
    site = StickySite(screen_info=False)
    g = run_crawl(monkeypatch, tmp_path, site, *ARGS, "--chrome-evidence", "0")
    assert g["meta"]["dedupe"]["screenAliases"] == 0


def test_chrome_prediction_skips_clicks_and_stays_correct(monkeypatch, tmp_path):
    no_pred = StickySite()
    g0 = run_crawl(monkeypatch, tmp_path, no_pred, *ARGS, "--chrome-evidence", "0")
    tmp2 = tmp_path / "pred"
    tmp2.mkdir()
    pred = StickySite()
    g1 = run_crawl(monkeypatch, tmp2, pred, *ARGS)
    assert g1["meta"]["dedupe"]["predictedEdges"] > 0
    assert pred.calls.get("click", 0) < no_pred.calls.get("click", 0)
    # same states either way — a prediction only skips re-discovering a known target
    assert sorted(s["url"] for s in g1["states"]) == sorted(s["url"] for s in g0["states"])
    # predicted edges are flagged and point at real states
    ids = {s["id"] for s in g1["states"]}
    pe = [e for e in g1["edges"] if e.get("predicted")]
    assert pe and all(e["to"] in ids and e["from"] in ids for e in pe)


def test_trigger_paths_replay_on_the_fixture(monkeypatch, tmp_path):
    # the recorded click-path of every trigger must actually reach it (materialize/recipe
    # replay stays valid: paths only ever run through canonical, really-visited states)
    site = StickySite()
    g = run_crawl(monkeypatch, tmp_path, site, *ARGS)
    for t in (t for t in g["triggers"] if t["kind"] == "form"):
        probe = StickySite()
        probe.url = g["meta"]["start"]
        for hop in t["path"]:
            if hop.get("href"):
                probe.url = hop["href"]
            else:
                assert probe.click(hop["selector"]), (t["label"], hop)
        assert any(c["selector"] == t["selector"] for c in probe.controls()), t["label"]
