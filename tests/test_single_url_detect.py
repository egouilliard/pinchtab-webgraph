"""Auto-detection of single-URL app-shell mode (roadmap #1) — the structural probe that
lets the crawler pick single-URL vs normal nav mode without the operator passing a flag.

Browser-free: `detect_single_url` reaches the bridge only through the module seams
`pt_json` / `click_js` / `settle` / `recipe.pin_tab`, so a scripted fake bridge exercises
the whole decision. The heuristic is GENERIC (control counts + URL + aria-view only), and
these scenarios use neutral labels — no app vocabulary — to keep it that way.
"""
import pinchtab_webgraph.interaction_crawl as ic


class _FakeBridge:
    """Scripted bridge: a list of view `steps`, each {href, controls, view}. `live()`
    reads the current step; each `click_js` advances one step (until the last), modelling
    a state transition. That lets one fake drive both the URL-change and in-place-swap
    branches of the detector."""

    def __init__(self, steps, routes=None):
        self.steps = steps
        self.routes = routes or {}                    # selector -> target step index
        self.i = 0
        self.clicks = 0
        self.last_selector = None

    def pt_json(self, js, server):
        step = self.steps[self.i]
        if "location.href" in js:                     # the (href, controls) read
            return {"href": step["href"], "controls": step["controls"]}
        return step.get("view", [])                   # the VIEW_JS read

    def click_js(self, selector, server):
        self.clicks += 1
        self.last_selector = selector
        if selector in self.routes:                   # selector-routed transition...
            self.i = self.routes[selector]
        elif self.i < len(self.steps) - 1:            # ...else advance one step
            self.i += 1

    def install(self, monkeypatch):
        monkeypatch.setattr(ic, "pt_json", self.pt_json)
        monkeypatch.setattr(ic, "click_js", self.click_js)
        monkeypatch.setattr(ic, "settle", lambda *a, **k: None)
        monkeypatch.setattr(ic.recipe, "pin_tab", lambda *a, **k: None)


def _tab(text, i):
    return {"text": text, "role": "tab", "selector": "#t%d" % i, "nav": False}


def _link(text, href, i):
    return {"text": text, "href": href, "nav": True, "selector": "#a%d" % i}


def _shell(labels, href="https://app.test/", view=None):
    return {"href": href, "controls": [_tab(t, i) for i, t in enumerate(labels)],
            "view": view or []}


# neutral top-level chrome — 8+ controls so the page reads as "substantially rendered"
_CHROME = ["Home", "Chat", "Calendar", "Files", "Teams", "Activity", "Calls", "Help"]


def test_app_shell_view_swaps_without_url_change_is_single_url(monkeypatch):
    # An app-shell (Teams-style): clicking a tab swaps the visible control set while the
    # URL stays put → single-URL mode.
    steps = [
        _shell(_CHROME, view=["Home"]),
        _shell(_CHROME + ["Compose box", "Message list"], view=["Chat"]),
    ]
    _FakeBridge(steps).install(monkeypatch)
    assert ic.detect_single_url("srv", "https://app.test/") is True


def test_app_shell_restores_origin_view_after_probe(monkeypatch):
    # After deciding app-shell, the detector must click BACK to the operator's original
    # view so single-URL mode reads --start as its root (it never nav()s back). Here the
    # first probe clicks the already-active Home tab (#t0, a no-op → routes to origin), the
    # second clicks Chat (#t1 → swaps), then the restore clicks the home control (#t0) to
    # return to the origin view.
    steps = [
        _shell(_CHROME, view=["Home"]),                          # origin: Home active
        _shell(_CHROME + ["Compose box"], view=["Chat"]),        # probed-into: Chat
    ]
    bridge = _FakeBridge(steps, routes={"#t0": 0})               # #t0 (Home) → origin
    bridge.install(monkeypatch)
    assert ic.detect_single_url("srv", "https://app.test/") is True
    assert bridge.i == 0                                          # restored to origin view
    assert bridge.last_selector == "#t0"                         # via the home control


def test_hash_router_only_changes_fragment_is_single_url(monkeypatch):
    # Fragment-only routing (#/home → #/settings) normalizes to one URL, so nav mode would
    # collapse it; the in-place view swap makes the detector pick single-URL (the improvement
    # called out in the README trade-off).
    steps = [
        _shell(_CHROME, href="https://app.test/#/home", view=["Home"]),
        _shell(_CHROME + ["Preference toggle"], href="https://app.test/#/settings",
               view=["Settings"]),
    ]
    _FakeBridge(steps).install(monkeypatch)
    assert ic.detect_single_url("srv", "https://app.test/") is True


def test_normal_multipage_url_path_changes_is_nav_mode(monkeypatch):
    # A same-host link that changes the URL PATH proves URL-primary routing → nav mode.
    # The link sorts ahead of the tabs (URL-changing candidates are probed first).
    controls = [_link("Products", "https://app.test/products", 0)] + \
               [_tab(t, i) for i, t in enumerate(_CHROME)]
    steps = [
        {"href": "https://app.test/", "controls": controls, "view": ["Home"]},
        {"href": "https://app.test/products", "controls": controls, "view": ["Products"]},
    ]
    _FakeBridge(steps).install(monkeypatch)
    assert ic.detect_single_url("srv", "https://app.test/") is False


def test_too_few_controls_defaults_to_nav_mode(monkeypatch):
    # Nothing substantial rendered live (< DETECT_MIN_CONTROLS) → conservative nav default,
    # and we never click anything.
    bridge = _FakeBridge([_shell(["Home", "Chat", "Files"])])
    bridge.install(monkeypatch)
    assert ic.detect_single_url("srv", "https://app.test/") is False
    assert bridge.clicks == 0


def test_no_observable_transition_defaults_to_nav_mode(monkeypatch):
    # Every probe click is a no-op (URL, controls and view all unchanged) → cannot confirm
    # an app-shell → nav default. It did try (clicked up to the probe cap).
    bridge = _FakeBridge([_shell(_CHROME, view=["Home"])])
    bridge.install(monkeypatch)
    assert ic.detect_single_url("srv", "https://app.test/") is False
    assert bridge.clicks == ic.DETECT_MAX_PROBES


def test_read_failure_defaults_to_nav_mode(monkeypatch):
    # Any bridge read error → False (never wedges the crawl on a probe).
    def boom(*a, **k):
        raise RuntimeError("bridge down")
    monkeypatch.setattr(ic, "pt_json", boom)
    monkeypatch.setattr(ic.recipe, "pin_tab", lambda *a, **k: None)
    assert ic.detect_single_url("srv", "https://app.test/") is False
