"""An in-process fake PinchTab bridge serving a synthetic app that keeps UI state in
STICKY query params — the defect behind the 2026-09-22 screen-dedupe fix.

The app (generic vocabulary only — nothing here is modelled on a real product's names):

  /home                 chrome links: Settings, Records, Other
  /settings?tab=&sub=&mode=
                        a tab bar of N_TABS role=tab buttons. Clicking a tab sets
                        `tab=` and KEEPS every other param already in the URL (sticky).
                        Tab 2 has a sub-tab bar ("Sub j" → `sub=j`), tab 3 a mode bar
                        ("Mode j" → `mode=j`); those params only matter while their tab
                        is showing. Odd tabs carry an "Add Item <i>" create-trigger that
                        opens a dialog form. `/settings` with no tab shows tab 0.
  /records?page=p       a list with the SAME toolbar on every page but different rows,
                        plus a "Next" link — genuinely different screens that must NOT be
                        merged by any dedupe.
  /other                a plain page.

Use `install(monkeypatch, site)` to route the crawler's `pt()` calls into `site`.
`site.calls` counts every bridge operation by kind; `site.cost()` turns that into a
modelled wall-clock (seconds) with per-operation latencies in the ballpark of a real
bridge driving a heavy SPA, so before/after comparisons are meaningful without a browser.
"""
import json
from urllib.parse import urlsplit, parse_qsl, urlencode

BASE = "https://sticky.test"

# modelled latency per bridge op (seconds) — a real nav+render of a heavy SPA ≈ 2-3s,
# a click re-render ≈ 1s, an in-page settle wait ≈ 0.5s, a DOM read eval ≈ 0.1s
LATENCY = {"nav": 2.5, "click": 1.0, "settle": 0.5, "eval": 0.1, "press": 0.2, "tab": 0.05}


def _ctl(sel, text, role="", tag="button", href=None, nav=True):
    return {"selector": sel, "text": text, "tag": tag, "role": role, "href": href,
            "nav": nav, "bulk": False}


FORM = {"title": "New item", "isDialog": True,
        "fields": [{"label": "Name", "type": "text", "required": True, "options": None,
                    "value": None, "accept": None, "selector": "#name", "placeholder": ""}],
        "submitButtons": ["Save"], "submitSelector": "#save", "fieldCount": 1}


class StickySite:
    def __init__(self, n_tabs=8, n_sub=3, n_mode=3, n_pages=3, volatile=False,
                 screen_info=True, chrome_jitter=False):
        self.n_tabs, self.n_sub, self.n_mode, self.n_pages = n_tabs, n_sub, n_mode, n_pages
        self.volatile = volatile          # a ticking clock inside the view's data
        self.screen_info = screen_info    # False = a reader that returns no `screen`
        # render-timing jitter seen on a real app: a sidebar entry that is only there on
        # some reads, and a select whose shown VALUE is sometimes still "Loading…"
        self.chrome_jitter = chrome_jitter
        self.reads = 0
        self.url = BASE + "/home"
        self.modal = False
        self.tick = 0
        self.calls = {}
        self.forms_opened = []            # (url, trigger selector) per real form open

    # ---- app model ----------------------------------------------------------------
    def _q(self):
        return dict(parse_qsl(urlsplit(self.url).query))

    def _path(self):
        return urlsplit(self.url).path

    def _set_q(self, **kw):
        q = self._q()
        q.update({k: str(v) for k, v in kw.items()})
        self.url = BASE + self._path() + "?" + urlencode(q)   # keeps OTHER params: sticky

    def _tab(self):
        return int(self._q().get("tab", 0))

    def controls(self):
        chrome = [_ctl("nav>a:nth-of-type(1)", "Home", tag="a", href=BASE + "/home"),
                  _ctl("nav>a:nth-of-type(2)", "Settings", tag="a", href=BASE + "/settings"),
                  _ctl("nav>a:nth-of-type(3)", "Records", tag="a", href=BASE + "/records?page=1"),
                  _ctl("nav>a:nth-of-type(4)", "Other", tag="a", href=BASE + "/other")]
        p = self._path()
        out = list(chrome)
        if self.chrome_jitter:
            self.reads += 1
            if self.reads % 3:
                out.append(_ctl("nav>button", "Help"))
        if p == "/settings":
            t = self._tab()
            out += [_ctl("#tab%d" % i, "Tab %d" % i, role="tab") for i in range(self.n_tabs)]
            if self.chrome_jitter:
                out.append(_ctl("#owner", "Loading…" if self.reads % 2 else "Owner A",
                                role="combobox"))
            if t == 2:
                out += [_ctl("#sub%d" % j, "Sub %d" % j, role="tab") for j in range(self.n_sub)]
            if t == 3:
                out += [_ctl("#mode%d" % j, "Mode %d" % j, role="tab") for j in range(self.n_mode)]
            if t % 2 == 1:
                out.append(_ctl("#add%d" % t, "Add Item %d" % t, nav=False))
        elif p == "/records":
            page = int(self._q().get("page", 1))
            out += [_ctl("#filter", "Filter", nav=False)]           # same toolbar on every page
            if page < self.n_pages:
                out.append(_ctl("#next", "Next", tag="a",
                                href=BASE + "/records?page=%d" % (page + 1)))
        return out

    def screen(self):
        p = self._path()
        view, heads, data = [], [], []
        if p == "/settings":
            t = self._tab()
            view.append("Tab %d" % t)
            heads.append("Tab %d" % t)
            data = ["row %d-%d" % (t, k) for k in range(3)]
            if t == 2:
                s = int(self._q().get("sub", 0))
                view.append("Sub %d" % s)
                data += ["sub-row %d-%d" % (s, k) for k in range(2)]
            if t == 3:
                m = int(self._q().get("mode", 0))
                view.append("Mode %d" % m)
                data += ["mode-row %d-%d" % (m, k) for k in range(2)]
        elif p == "/records":
            page = int(self._q().get("page", 1))
            heads.append("Records")
            data = ["Record %d-%d" % (page, k) for k in range(5)]   # different DATA per page
        else:
            heads.append(p.strip("/") or "home")
        if self.volatile:
            self.tick += 1
            data = data + ["updated %d seconds ago" % self.tick]
        return {"view": view, "heads": heads, "data": data, "fields": []}

    def collections(self):
        items = [{"t": t} for t in self.screen()["data"]]
        return [{"kind": "aria:list", "count": len(items), "items": items}] if items else []

    def click(self, sel):
        p = self._path()
        if sel.startswith("nav>a"):
            href = next(c["href"] for c in self.controls() if c["selector"] == sel)
            self.url = href
        elif p == "/settings" and sel.startswith("#tab"):
            self._set_q(tab=int(sel[4:]))
        elif p == "/settings" and sel.startswith("#sub"):
            self._set_q(sub=int(sel[4:]))
        elif p == "/settings" and sel.startswith("#mode"):
            self._set_q(mode=int(sel[5:]))
        elif sel.startswith("#add"):
            self.modal = True
            self.forms_opened.append((self.url, sel))
        elif sel == "#next":
            self.url = next(c["href"] for c in self.controls() if c["selector"] == sel)
        else:
            return False
        return True

    # ---- bridge protocol ----------------------------------------------------------
    def _count(self, kind):
        self.calls[kind] = self.calls.get(kind, 0) + 1

    def cost(self):
        return round(sum(LATENCY.get(k, 0.1) * n for k, n in self.calls.items()), 1)

    def pt(self, args, server=None, timeout=60):
        cmd = args[0]
        if cmd == "tab":
            self._count("tab")
            return 0, json.dumps([{"id": "T1", "type": "page", "status": "active",
                                   "url": self.url}]), ""
        if cmd == "health":
            return 0, "ok", ""
        if cmd == "nav":
            self._count("nav")
            self.url, self.modal = args[1], False
            if self._path() == "/settings" and not urlsplit(self.url).query:
                pass                                   # bare /settings shows tab 0
            return 0, "ok", ""
        if cmd == "click":
            self._count("click")
            self.click(args[1])
            return 0, "ok", ""
        if cmd == "press":
            self._count("press")
            self.modal = False
            return 0, "ok", ""
        if cmd == "eval":
            if "--await-promise" in args:
                self._count("settle")
                return 0, "5", ""
            self._count("eval")
            js = args[-1]
            return 0, json.dumps(self.eval(js)), ""
        return 0, "ok", ""

    def eval(self, js):
        if "e.click()" in js:                          # click_js
            sel = json.loads(js.split("document.querySelector(", 1)[1].split(");", 1)[0])
            self._count("click")
            return "ok" if self.click(sel) else "missing"
        if "controls:" in js:
            st = {"href": self.url, "controls": self.controls()}
            if "screen:" in js and self.screen_info:
                st["screen"] = self.screen()
            return st
        if js.strip() == "location.href":
            return self.url
        if "submitButtons" in js:                      # FORM_JS
            return FORM if self.modal else {"fields": [], "fieldCount": 0}
        if "hasPassword" in js:                        # FORM_BEARING_JS
            return {"fields": 0, "hasPassword": False, "submit": True}
        if "repeated:" in js:                          # CONTENT_JS
            return self.collections()
        if "scrollIntoView" in js:
            return 0
        if "aria-modal" in js:                         # close_modal's open-dialog count
            return 1 if self.modal else 0
        return ""


def install(monkeypatch, site):
    """Route every bridge call of the crawler (and the recipe helpers it reuses) into
    `site`. Both modules bind `pt` at import, so both names are patched."""
    from pinchtab_webgraph import interaction_crawl as ic, recipe
    monkeypatch.setattr(recipe, "pt", site.pt)
    monkeypatch.setattr(ic, "pt", site.pt)
    return site


def run_crawl(monkeypatch, tmp_path, site, *extra, start=BASE + "/home"):
    """Run interaction_crawl.main() against `site`; return the written graph."""
    import sys
    from pinchtab_webgraph import interaction_crawl as ic
    install(monkeypatch, site)
    out = tmp_path / "graph"
    monkeypatch.setattr(sys, "argv", ["interaction_crawl", "--start", start,
                                      "--out", str(out), "--no-single-url",
                                      "--config", str(tmp_path / "none.json"), *extra])
    ic.main()
    return json.loads((tmp_path / "graph.json").read_text())
