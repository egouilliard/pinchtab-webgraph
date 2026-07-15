"""Browser-free unit tests for the single-URL form-reading capability: the pure
`shell_blanked` predicate (the shell-blank guard that decides whether opening a
trigger navigated away / blanked the app-shell) and `serialize_trigger`
passthrough on a single-URL-recovered trigger (form=None must stay None, never
coerced to {}). No bridge needed — these exercise the pure module functions.
"""
from pinchtab_webgraph import interaction_crawl as ic


# --- shell_blanked: did opening this trigger navigate away / blank the shell? ----------

def test_shell_blanked_same_url_is_safe():
    assert ic.shell_blanked("https://teams.example/app", "https://teams.example/app") is False


def test_shell_blanked_trailing_slash_only_is_safe():
    # a bare trailing-slash diff is not a real navigation
    assert ic.shell_blanked("https://teams.example/app", "https://teams.example/app/") is False


def test_shell_blanked_different_url_is_blanked():
    assert ic.shell_blanked("https://teams.example/app", "https://other.example/login") is True


def test_shell_blanked_empty_after_is_defensively_blanked():
    # None/empty after → treat as changed (unsafe: never read that DOM as a form)
    assert ic.shell_blanked("https://teams.example/app", None) is True
    assert ic.shell_blanked("https://teams.example/app", "") is True


# --- serialize_trigger: a single-URL-recovered trigger keeps form=None -----------------

def test_serialize_trigger_preserves_none_form_and_kind():
    t = {"label": "New X", "state": "sig", "path": [], "form": None,
         "opensAt": None, "kind": "form", "selector": "#x", "href": None, "accept": None}
    out = ic.serialize_trigger(t, "s7")
    assert out["form"] is None            # NOT coerced to {}
    assert out["kind"] == "form"
    assert out["state"] == "s7"           # state-id argument maps through
    assert out["selector"] == "#x"
