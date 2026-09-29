"""A pinned, live $PINCHTAB_TAB is kept; URL matching only replaces a stale one."""
import json

from pinchtab_webgraph import recipe


def _fake_pt(tabs):
    def pt(args, server, timeout=None):
        assert args[:2] == ["tab", "--json"]
        return 0, json.dumps(tabs), ""
    return pt


TABS = [{"id": "A", "type": "page", "url": "https://app/consultant/notifications", "status": "active"},
        {"id": "B", "type": "page", "url": "https://app/dashboard", "status": "background"}]


def test_a_live_pinned_tab_is_kept_even_when_another_matches_the_url(monkeypatch):
    monkeypatch.setattr(recipe, "pt", _fake_pt(TABS))
    monkeypatch.setenv("PINCHTAB_TAB", "A")          # redirected away from /dashboard
    assert recipe.pin_tab("srv", "https://app/dashboard") == "A"


def test_a_stale_pinned_tab_is_replaced_by_url(monkeypatch):
    monkeypatch.setattr(recipe, "pt", _fake_pt(TABS))
    monkeypatch.setenv("PINCHTAB_TAB", "GONE")
    assert recipe.pin_tab("srv", "https://app/dashboard") == "B"


def test_no_pin_resolves_by_url_as_before(monkeypatch):
    monkeypatch.setattr(recipe, "pt", _fake_pt(TABS))
    monkeypatch.delenv("PINCHTAB_TAB", raising=False)
    assert recipe.pin_tab("srv", "https://app/dashboard") == "B"
