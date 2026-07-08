"""Phase 1 — deterministic state identity (nav mode URL-primary; single-URL keeps
the structural signature). Browser-free: exercises the pure module functions
`norm`, `nav_state_key`, `state_sig` directly (the crawl loop wires them together —
see the `sig = ...` assignment gated on `a.single_url`).

The bug being fixed: on content-dynamic pages the visible-control TEXTS jitter between
reads, so the old text-based `state_sig` minted a NEW state each visit of ONE url
(MDN 20 states = 5 unique urls). Nav-mode identity is now URL-primary, so jitter can't
mint duplicates — while single-URL app-shells (Teams) keep `state_sig` + ARIA view.
"""
from pinchtab_webgraph import interaction_crawl as ic


def _ctrl(text, nav=True, bulk=False):
    return {"text": text, "nav": nav, "bulk": bulk}


# --- nav-mode identity is URL-primary: control jitter no longer mints states ---------

def test_nav_state_key_ignores_control_jitter():
    # The exact scenario that over-noded MDN: same url, different visible controls per
    # read. Old text-based state_sig() diverges; nav_state_key() is stable.
    url = "https://developer.mozilla.org/en-US/"
    controls_read1 = [_ctrl("Home"), _ctrl("Learn"), _ctrl("Featured: Article A")]
    controls_read2 = [_ctrl("Home"), _ctrl("Learn"), _ctrl("Featured: Article B")]
    # state_sig (single-URL discriminator) DOES diverge on the jitter — that was the bug
    assert ic.state_sig(url, controls_read1) != ic.state_sig(url, controls_read2)
    # nav_state_key does NOT — one url == one state regardless of control churn
    assert ic.nav_state_key(url) == ic.nav_state_key(url)
    assert ic.nav_state_key(url).startswith("u::")


def test_nav_state_key_distinguishes_distinct_urls():
    a = ic.nav_state_key("https://site.test/products")
    b = ic.nav_state_key("https://site.test/cart")
    assert a != b


# --- norm(): fold generic tracking params, keep real content params -------------------

def test_norm_folds_tracking_params():
    clean = ic.norm("https://site.test/page?page=2")
    assert ic.norm("https://site.test/page?utm_source=x&utm_campaign=y&page=2") == clean
    assert ic.norm("https://site.test/page?gclid=abc&page=2") == clean
    assert ic.norm("https://site.test/page?fbclid=z&page=2") == clean


def test_norm_keeps_content_params_distinct():
    base = ic.norm("https://en.wikipedia.org/wiki/Foo")
    # a revision / pagination / query param is REAL content — must stay distinct
    assert ic.norm("https://en.wikipedia.org/wiki/Foo?oldid=5") != base
    assert ic.norm("https://site.test/list?page=2") != ic.norm("https://site.test/list?page=3")


def test_norm_is_order_stable_and_drops_fragment_and_slash():
    assert ic.norm("https://site.test/p?b=2&a=1") == ic.norm("https://site.test/p?a=1&b=2")
    assert ic.norm("https://site.test/p/") == ic.norm("https://site.test/p")
    assert ic.norm("https://site.test/p#section") == ic.norm("https://site.test/p")


# --- single-URL (Teams) discriminator is preserved: state_sig still varies -------------

def test_state_sig_still_distinguishes_views_for_single_url():
    url = "https://teams.example/"          # app-shell: url never changes across views
    controls = [_ctrl("Chat"), _ctrl("Calendar")]
    # different ARIA view markers => different states (the single-URL identity)
    assert ic.state_sig(url, controls, view=["chat-selected"]) != \
           ic.state_sig(url, controls, view=["calendar-selected"])


def test_state_sig_varies_with_control_set():
    url = "https://teams.example/"
    assert ic.state_sig(url, [_ctrl("Chat")]) != ic.state_sig(url, [_ctrl("Chat"), _ctrl("Files")])
