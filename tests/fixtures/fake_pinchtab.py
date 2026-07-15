#!/usr/bin/env python3
"""A fake `pinchtab` CLI for deterministic, offline benchmarking of recipe.py.

It implements ONLY the subcommands recipe.py's discovery loop uses (nav, eval,
tab, click, press, health, screenshot, wait), backed by a tiny in-process
synthetic app whose state persists in a JSON file ($FAKEPT_STATE). Per-op render
latency is injected to model a real bridge, so wall-clock is representative;
every call is appended to $FAKEPT_LOG (one line per invocation), which is how a
benchmark counts bridge ROUND-TRIPS independent of the wall clock.

Nothing here is app-specific: the synthetic app speaks only generic
"Section N" / "Tab N" / "Add Widget" vocabulary.

Synthetic app (app-agnostic — generic sections/tabs/create button):
  /                 start page: N sibling section links + M data-list rows
  /settings         a section with a tab bar of M tabs
  /settings#tab=T   a tab; the create trigger "Add Widget" appears only while the
                    trigger tab (index $FAKEPT_TRIGGER_TAB) is active
Reaching the trigger is a section-link click plus tab clicks — a 1-to-3 click
path depending on the start page and which tab is the trigger tab.

recipe.py sends opaque JS blobs; this fake recognizes them by STRUCTURAL markers
(it never executes JS) and returns the CONTROLS_JS / FORM_JS shapes recipe reads:
  * single-call async settle (`eval --await-promise`) — modeled as an in-page wait
  * combined state read  ({href, controls: …})       — returns url + controls
  * bare `location.href`                              — returns the current url
  * FORM_JS (isDialog/submitButtons/fieldCount)       — returns the one-field form
The legacy per-poll count read (`…summary").length`) is also recognized so an
OLD (pre-single-round-trip) recipe.py can be profiled against the same fixture.

Env knobs (with defaults):
  FAKEPT_STATE   path to the JSON state file (required in practice)
  FAKEPT_LOG     path to the round-trip log (one line per call; optional)
  FAKEPT_NAV_MS      700   page load + render latency
  FAKEPT_CLICK_MS    400   SPA re-render latency after a click
  FAKEPT_EVAL_MS      25   DOM read latency
  FAKEPT_SETTLE_POLLS  2   in-page settle poll intervals (@100ms) before stable
  FAKEPT_SECTIONS      6   sibling section links on the start page
  FAKEPT_ROWS         12   repeated data-list rows on the start page
  FAKEPT_TABS          4   tabs in the /settings tab bar
  FAKEPT_TRIGGER_TAB   2   tab index that reveals the "Add Widget" trigger
  FAKEPT_BASE   https://synthetic.test   origin the synthetic app is served from
"""
import json
import os
import sys
import time

STATE = os.environ.get("FAKEPT_STATE", "/tmp/fakept_state.json")
LOG = os.environ.get("FAKEPT_LOG", "")
NAV_MS = float(os.environ.get("FAKEPT_NAV_MS", "700"))      # page load + render
CLICK_MS = float(os.environ.get("FAKEPT_CLICK_MS", "400"))  # SPA re-render after click
EVAL_MS = float(os.environ.get("FAKEPT_EVAL_MS", "25"))     # DOM read
# how many settle polls until the control count "stabilizes" (>0 => a settle burns
# that many poll intervals; a big number models a never-settling realtime app).
SETTLE_POLLS = int(os.environ.get("FAKEPT_SETTLE_POLLS", "2"))
N_SECTIONS = int(os.environ.get("FAKEPT_SECTIONS", "6"))
N_ROWS = int(os.environ.get("FAKEPT_ROWS", "12"))
N_TABS = int(os.environ.get("FAKEPT_TABS", "4"))
TRIGGER_TAB = int(os.environ.get("FAKEPT_TRIGGER_TAB", "2"))
BASE = os.environ.get("FAKEPT_BASE", "https://synthetic.test")


def load():
    try:
        return json.load(open(STATE))
    except Exception:
        return {"url": BASE + "/", "tab": None, "form_open": False, "poll": 0}


def save(s):
    json.dump(s, open(STATE, "w"))


def log(kind):
    if LOG:
        with open(LOG, "a") as f:
            f.write("%.6f %s\n" % (time.time(), kind))


def controls_for(s):
    """Return the CONTROLS_JS shape (list of control dicts) for the current state."""
    url = s["url"]
    path = url.split(BASE)[-1].split("#")[0]
    out = []
    if path in ("/", ""):
        # start page: sibling section links + a repeated data list (rows)
        for i in range(N_SECTIONS):
            name = "Settings" if i == 0 else "Section %d" % i
            href = BASE + ("/settings" if i == 0 else "/section%d" % i)
            out.append({"selector": "a#sec%d" % i, "text": name, "tag": "a",
                        "role": "", "href": href, "nav": True, "bulk": False})
        for r in range(N_ROWS):   # data list — should be pruned by data-list-min
            out.append({"selector": "a#row%d" % r, "text": "Item %d" % r, "tag": "a",
                        "role": "", "href": BASE + "/items/%d" % r, "nav": False,
                        "bulk": False})
    elif path == "/settings":
        # a tab bar; each tab is a role=tab click (no href)
        for t in range(N_TABS):
            out.append({"selector": "button#tab%d" % t, "text": "Tab %d" % t,
                        "tag": "button", "role": "tab", "href": None, "nav": True,
                        "bulk": False})
        # the create trigger only appears once the trigger tab is active
        if s.get("tab") == TRIGGER_TAB:
            out.append({"selector": "button#addwidget", "text": "Add Widget",
                        "tag": "button", "role": "button", "href": None,
                        "nav": False, "bulk": False})
    else:
        # generic section page: a dead-end link (no create form here)
        out.append({"selector": "a#back", "text": "Overview", "tag": "a",
                    "role": "", "href": BASE + path, "nav": True, "bulk": False})
    return out


# the FORM_JS shape recipe reads after clicking the trigger — a one-field dialog.
FORM = {"title": "Add Widget", "isDialog": True,
        "fields": [{"label": "Name", "type": "text", "required": True,
                    "options": None, "value": None, "accept": None,
                    "selector": "#name", "placeholder": ""}],
        "submitButtons": ["Create"], "submitSelector": "#create", "fieldCount": 1}


def do_eval(js, s):
    """Map a recipe JS blob to its expected string output, by structural markers."""
    # single-call async settle (`eval --await-promise`): one eval that waits
    # IN-PAGE for stability. Model the in-page wait (SETTLE_POLLS poll intervals
    # @100ms + a trailing settle) as wall-clock, then report a stable count.
    if "async" in js and "await new Promise" in js and "querySelectorAll(sel)" in js:
        time.sleep((SETTLE_POLLS + 1) * 0.1)
        s["poll"] = 0
        return str(5 + SETTLE_POLLS)
    # legacy per-poll count read (old recipe.py's settle): the DOM "settles" after
    # SETTLE_POLLS polls — the count changes until then, so the poller keeps going.
    if 'summary").length' in js or "summary').length" in js.replace('"', "'"):
        s["poll"] = s.get("poll", 0) + 1
        if s["poll"] <= SETTLE_POLLS:
            return str(5 + s["poll"])       # changing => not yet stable
        return str(5 + SETTLE_POLLS)        # stable
    # bare location.href
    if js.strip() in ("location.href", '"location.href"'):
        return json.dumps(s["url"])
    # combined state read: JSON.stringify({href, controls: …})
    if "controls:" in js:
        s["poll"] = 0
        return json.dumps(json.dumps({"href": s["url"], "controls": controls_for(s)}))
    # FORM_JS: JSON.stringify(<form introspection>)
    if "isDialog" in js or "submitButtons" in js or "fieldCount" in js:
        return json.dumps(json.dumps(FORM))
    return json.dumps("")


def main():
    # strip --server X (leading or interspersed); recipe passes it on every call.
    raw = sys.argv[1:]
    args = []
    i = 0
    while i < len(raw):
        if raw[i] == "--server":
            i += 2
            continue
        args.append(raw[i])
        i += 1
    if not args:
        sys.exit(0)
    cmd = args[0]
    s = load()
    log(cmd)
    if cmd == "health":
        print("ok")
        return
    if cmd == "tab":
        # active_tab() expects a JSON list of tabs
        print(json.dumps([{"id": "T1", "type": "page", "status": "active",
                           "url": s["url"]}]))
        return
    if cmd == "nav":
        time.sleep(NAV_MS / 1000.0)
        s["url"] = args[1]
        s["tab"] = None
        s["form_open"] = False
        s["poll"] = 0
        save(s)
        print("ok")
        return
    if cmd == "click":
        time.sleep(CLICK_MS / 1000.0)
        sel = args[1]
        if sel.startswith("button#tab"):
            s["tab"] = int(sel.replace("button#tab", ""))
        elif sel == "button#addwidget":
            s["form_open"] = True
        s["poll"] = 0
        save(s)
        print("ok")
        return
    if cmd == "press":
        s["form_open"] = False
        save(s)
        print("ok")
        return
    if cmd == "screenshot":
        print("ok")
        return
    if cmd == "wait":
        print("ok")
        return
    if cmd == "eval":
        time.sleep(EVAL_MS / 1000.0)
        # the eval expression is the first non-flag arg after `eval`
        expr = next((a for a in args[1:] if not a.startswith("--")), "")
        print(do_eval(expr, s))
        save(s)
        return
    print("ok")


if __name__ == "__main__":
    main()
