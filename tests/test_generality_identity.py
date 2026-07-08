"""Generality guard for the Phase-1 tracking-param denylist (see generality.md): the
denylist must be GENERIC web-standard vocabulary only — never app/section/product
tokens. This mirrors the generality.md audit grep as an executable test.
"""
from pinchtab_webgraph import interaction_crawl as ic

# App/section/product tokens that must NEVER appear in the identity denylist. If a fix
# ever tries to fold a content param by name (e.g. MediaWiki `oldid`, a LeytonGo route),
# that is a generality violation AND a correctness bug (it merges distinct pages).
FORBIDDEN = ["oldid", "centralauth", "leyton", "dashboard", "section", "wpform",
             "wiki", "product_details", "brand"]


def test_tracking_denylist_has_no_app_vocabulary():
    pat = ic._TRACKING_RE.pattern.lower()
    for tok in FORBIDDEN:
        assert tok not in pat, "denylist leaked app-specific token %r" % tok


def test_tracking_denylist_covers_common_generic_params():
    # sanity: it DOES fold the standard web-analytics params
    for junk in ("utm_source", "utm_campaign", "gclid", "fbclid", "mc_cid", "msclkid"):
        assert ic._TRACKING_RE.match(junk), "expected %r to be treated as tracking" % junk
    # ...and does NOT touch real content params
    for real in ("page", "oldid", "q", "id", "section", "tab"):
        assert not ic._TRACKING_RE.match(real), "%r wrongly treated as tracking" % real
