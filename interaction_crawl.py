#!/usr/bin/env python3
"""
Build an INTERACTION-GRAPH CACHE for a web app, via PinchTab.

Crawls the live UI ONCE, thoroughly (no per-query time pressure):
  - follows navigation controls (links + tabs/menus/sidebar), recording STATES
    (deduped by URL + visible-control signature) and ACTION EDGES between them,
  - for every CREATE-style trigger it encounters, opens the form, introspects its
    fields, and presses Escape — it NEVER submits/saves/deletes,
  - writes a single graph JSON that `howto.py` queries OFFLINE in milliseconds
    (shortest click-path to any goal's trigger + the form spec), from ANY start
    page, with no live browser and no discovery budget.

This is the cache that replaces live discovery: crawl once, query instantly.

GENERIC by construction: reuses recipe.py's structural heuristics only (ARIA
roles, repeated-sibling/data-list detection, URL-path section grouping). No app
routes/labels/section vocabulary. The only "vocabulary" is the create-VERB regex
(create/add/new/crear/…), which identifies a goal's trigger generically the same
way recipe.py already does — it is not app- or section-specific.

Safe by design: opens & reads forms, then Escapes. Opening a "Create X" dialog
persists nothing until submitted, and we never submit.

  ./run-crawl-interactions.sh https://app.example.com/home
  python3 interaction_crawl.py --start https://app.example.com/home --out interaction-graph

For long crawls the headless bridge can WEDGE (nav/click time out though health
says ok). Pass your environment's bridge-relaunch and re-login commands to enable
auto-recovery — a wedge is then detected, the bridge restarted, and the in-memory
BFS resumed (partial output is still written if recovery gives up):

  python3 interaction_crawl.py --start https://app.example.com/home \\
      --restart-cmd '<relaunch the bridge>' --login-cmd '<re-authenticate>'
"""
import argparse
import atexit
import heapq
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections import deque
from urllib.parse import urlparse

import recipe  # proven primitives — see module docstring

CONTROLS_JS = recipe.CONTROLS_JS
CONTENT_JS = recipe.CONTENT_JS
FORM_JS = recipe.FORM_JS
SKIP_NAV = recipe.SKIP_NAV
VERBS = recipe.VERBS
pt = recipe.pt
pt_json = recipe.pt_json
settle = recipe.settle
nav = recipe.nav
same_host = recipe.same_host

# A create-style trigger: a create-verb appears in the control's own label. Same
# generic signal recipe.py uses to find a goal's button; here we capture ALL of
# them. NOT destructive (no delete/remove) — we only ever open CREATE forms.
TRIGGER_RE = re.compile(r"\b(%s)\b" % VERBS, re.I)

NAV_ROLES = ("tab", "menuitem", "menuitemradio", "menuitemcheckbox")

# Single-URL app-shells (e.g. MS Teams) swap views in place WITHOUT changing the URL,
# so a programmatic nav() to the same URL blanks/re-inits the SPA (read returns 0
# controls). For these apps we NEVER navigate: we read the current live page and drive
# it by clicking. Two structural fixes that mode needs (both app-agnostic):
#  (1) JS-dispatch clicks — coordinate clicks get "occluded" by transient animation
#      overlays (a fade-out div sits on top), and pinchtab reports rc=0 even when the
#      click is swallowed. el.click() bypasses occlusion and is position-independent.
#  (2) a VIEW marker in the state signature — switching the active view often only
#      flips aria-selected/aria-pressed/aria-current on the persistent nav chrome
#      while the captured control set is unchanged, so without it all views collapse
#      to one state. ARIA-only signal, no app vocabulary.
VIEW_JS = (r"""[...document.querySelectorAll('[aria-selected="true"],"""
           r"""[aria-pressed="true"],[aria-current]:not([aria-current="false"])')]"""
           r""".map(e=>(e.getAttribute('aria-label')||e.innerText||'')"""
           r""".replace(/\s+/g,' ').trim().slice(0,30)).filter(Boolean)""")


def click_js(selector, server):
    sel = json.dumps(selector)
    rc, out, err = pt(["eval", "(()=>{const e=document.querySelector(%s);"
                       "if(!e)return 'missing';e.click();return 'ok';})()" % sel], server)
    if rc != 0:
        raise RuntimeError(err or out)
    if "missing" in (out or ""):
        raise RuntimeError("selector not present: %s" % selector[:60])


# Scroll the last item of the page's LARGEST collection into view (generic — used to
# defeat list VIRTUALIZATION, where only the on-screen rows exist in the DOM).
SCROLL_DOMINANT = r"""(()=>{
  const CLOSE='[role=grid],[role=table],[role=treegrid],[role=tree],[role=list],[role=listbox],[role=feed],table,ul,ol';
  const ITEM='[role=row],[role=treeitem],[role=listitem],[role=option],[role=article],tr,li';
  let best=null,bn=0;
  document.querySelectorAll(CLOSE).forEach(c=>{const n=c.querySelectorAll(ITEM).length;if(n>bn){bn=n;best=c;}});
  if(!best)return 0;
  const it=best.querySelectorAll(ITEM); if(it.length)it[it.length-1].scrollIntoView({block:'end'});
  return it.length;
})()"""


def capture_collections(server, max_rounds=50):
    """GENERIC content capture: read the view's data collections (CONTENT_JS) and, for
    the dominant (largest) one, scroll-load through virtualization, accumulating unique
    items until the count stops growing. Returns the collection list (dominant expanded)."""
    def read():
        try:
            return pt_json(CONTENT_JS, server) or []
        except Exception:
            return []
    cols = read()
    if not cols:
        return []
    dominant = max(cols, key=lambda c: c.get("count", 0)).get("kind")

    def key(it):
        return (it.get("t", ""), " | ".join(it.get("cells", [])), it.get("level"))

    merged, order = {}, []
    last, stable = -1, 0
    for _ in range(max_rounds):
        for c in read():
            if c.get("kind") == dominant:
                for it in c.get("items", []):
                    k = key(it)
                    if k not in merged:
                        merged[k] = it
                        order.append(k)
        if len(order) == last:
            stable += 1
            if stable >= 3:                                  # no growth after 3 scrolls → done
                break
        else:
            stable, last = 0, len(order)
        try:                                                 # a wedge mid-scroll must not throw
            pt(["eval", SCROLL_DOMINANT], server)            # out of here unguarded — break and
            settle(server)                                   # keep what we accumulated so far.
        except Exception:
            break
    final = read() or cols
    out = []
    for c in final:
        if c.get("kind") == dominant and order:
            out.append({"kind": c["kind"], "count": len(order),
                        "items": [merged[k] for k in order]})
        else:
            out.append(c)
    return out


def state_sig(url, controls, view=None):
    # Signature on the STRUCTURALLY meaningful controls only (nav + create-triggers),
    # not volatile page content (counts, notifications, timestamps) — so the same
    # logical page/tab dedups to one state instead of many cosmetic variants.
    key = sorted({(c.get("text") or "")[:30] for c in controls
                  if not c.get("bulk") and
                  (c.get("nav") or TRIGGER_RE.search(c.get("text") or ""))})
    base = url.split("#")[0] + "||" + "|".join(key)[:2000]
    if view:  # single-URL mode: the active aria-view distinguishes same-chrome views
        base += "||view=" + "|".join(sorted(set(view)))[:300]
    return base


def section_key(u):
    p = urlparse(u)
    seg = [s for s in p.path.split("/") if s]
    base = seg[0] if seg else "root"
    return base + ("?" + p.query if p.query else "")


def norm(u):
    # Same URL-normalization the comparisons elsewhere use (drop #fragment, trailing
    # /). Keys the global url->state dedup so each distinct URL is visited once.
    return u.split("#")[0].rstrip("/")


def probe_bridge(server, start_url, timeout, single_url=False):
    # Distinguish a WEDGED bridge from a merely-bad path/selector. Don't use nav()
    # (it hardcodes timeout=60) — probe directly with a short timeout. Both calls
    # rc==0 → bridge ALIVE (the earlier failure was a bad path → skip). Any
    # Exception (incl. subprocess.TimeoutExpired) or rc!=0 → WEDGED (→ recover).
    # In single_url mode we must NOT nav (it blanks the app-shell SPA) — a plain
    # eval is enough to tell a live bridge from a wedged one.
    try:
        if not single_url:
            if pt(["nav", start_url], server, timeout=timeout)[0] != 0:
                return False
        if pt(["eval", "location.href"], server, timeout=timeout)[0] != 0:
            return False
        return True
    except Exception:
        return False


def recover_bridge(server, restart_cmd, login_cmd, attempt):
    # Mirror hard-bench.sh's ensure_browser(): kill the stale bridge by its PORT
    # pid (NEVER pkill -f — that self-kills this process, exit 144; see gotchas.md),
    # relaunch, poll health, re-login. Returns True if the bridge is healthy again.
    print("  ! WEDGE detected (attempt %d) — killing bridge + restarting…" % attempt,
          file=sys.stderr)
    time.sleep(5 * attempt)                                  # backoff, grows per attempt
    port = (urlparse(server).port or 9871)                   # derive from --server (was hardcoded 9871)
    subprocess.run("BPID=$(ss -ltnp 2>/dev/null | grep ':%d ' | grep -oP 'pid=\\K[0-9]+' "
                   "| head -1); [ -n \"$BPID\" ] && kill \"$BPID\"" % port, shell=True)
    time.sleep(2)
    if restart_cmd:
        subprocess.run(restart_cmd, shell=True)
    up = False
    for _ in range(25):
        time.sleep(1)
        try:
            if pt(["health"], server, timeout=3)[0] == 0:
                up = True
                break
        except Exception:
            pass
    if not up:
        print("  ! bridge did not come up after restart %d — giving up" % attempt,
              file=sys.stderr)
        return False
    if login_cmd:
        r = subprocess.run(login_cmd, shell=True)
        if r.returncode != 0:
            print("  ! login_cmd exited %d — bridge may not be authenticated"
                  % r.returncode, file=sys.stderr)
            return False
    print("  ! bridge up — resuming crawl", file=sys.stderr)
    return True


def main():
    ap = argparse.ArgumentParser(description="Crawl a web app into an interaction-graph cache")
    ap.add_argument("--start", required=True, help="start URL (the crawl root)")
    ap.add_argument("--server", default="http://localhost:9871")
    ap.add_argument("--config", default=os.path.expanduser("~/code/pinchtab-webgraph/crawl-config.json"))
    ap.add_argument("--out", default="interaction-graph")
    ap.add_argument("--max-states", type=int, default=500,
                    help="hard cap on distinct states to record (default 500 — raised so a "
                         "full-capture run isn't truncated; lower it to bound a huge app)")
    ap.add_argument("--max-visits", type=int, default=1200,
                    help="hard cap on materializations, incl. revisits (default 1200; click-only "
                         "tabs/menus can't be URL-deduped so visits run several× states — this "
                         "ceiling is deliberately high so breadth isn't starved. See the 'stopped' "
                         "field in the output: 'frontier-exhausted' = truly complete, a cap = truncated)")
    ap.add_argument("--max-depth", type=int, default=5,
                    help="max click-depth to explore (default 5 — covers deep nested forms)")
    ap.add_argument("--max-per-section", type=int, default=2,
                    help="max distinct pages explored per URL section, e.g. /items/* (default 2)")
    ap.add_argument("--data-list-min", type=int, default=3,
                    help="N sibling links sharing a section root = a repeated data list")
    ap.add_argument("--rows-per-list", type=int, default=1,
                    help="descend into N representative rows of each data list to reach "
                         "per-row nested forms (default 1; 0 = skip lists entirely)")
    ap.add_argument("--cross-host", dest="cross_host", action="store_true", default=False,
                    help="follow links AND iframe srcs to OTHER hosts (embedded/linked apps, "
                         "e.g. an in-Teams SharePoint doc library) as graph nodes — so the graph "
                         "spans app boundaries. Off by default (a same-host crawl is the norm).")
    ap.add_argument("--max-cross-host", type=int, default=25,
                    help="cap on distinct other-host nodes followed (default 25)")
    # Full capture is ON BY DEFAULT: one bare run extracts EVERYTHING — the full
    # control inventory (links/buttons/tabs/menus) of every state AND each state's data
    # collections — so "run the script" means "capture it all". --no-* opts out for speed.
    ap.add_argument("--dump-controls", dest="dump_controls", action="store_true", default=True,
                    help="store the FULL control inventory (links/buttons/tabs/menus) of every "
                         "state in the output, not just create-triggers (default ON)")
    ap.add_argument("--no-dump-controls", dest="dump_controls", action="store_false",
                    help="record only create-triggers per state, not the full control inventory")
    ap.add_argument("--capture-content", dest="capture_content", action="store_true", default=True,
                    help="ALSO capture each state's DATA collections (tables/grids/trees/lists/"
                         "feeds + repeated-sibling clusters) via CONTENT_JS, scroll-loading through "
                         "virtualization — generic, structural, no app vocabulary. Turns the nav "
                         "graph into a full content graph of ANY site (default ON).")
    ap.add_argument("--no-capture-content", dest="capture_content", action="store_false",
                    help="skip data-collection capture (faster; nav+controls+forms only)")
    ap.add_argument("--checkpoint-every", type=int, default=10,
                    help="flush the graph to disk every N new states (default 10) so a crash or "
                         "kill never loses progress; the crawl also flushes on SIGINT/SIGTERM")
    ap.add_argument("--single-url", dest="single_url", action="store_true", default=False,
                    help="single-URL app-shell mode: NEVER navigate (a programmatic nav "
                         "blanks such SPAs, e.g. MS Teams) — read the current live page and "
                         "drive it with JS-dispatch clicks; key states by active aria-view. "
                         "Use --no-read-forms with this (form-open in this mode is a follow-up).")
    ap.add_argument("--read-forms", dest="read_forms", action="store_true", default=True,
                    help="open+read each create form (default on)")
    ap.add_argument("--no-read-forms", dest="read_forms", action="store_false",
                    help="record triggers but skip opening their forms (faster, no form specs)")
    ap.add_argument("--restart-cmd", default="",
                    help="shell command to relaunch the bridge after a wedge "
                         "(empty = no relaunch, just kill the stale PID)")
    ap.add_argument("--login-cmd", default="",
                    help="shell command to re-authenticate after restart (empty = none)")
    ap.add_argument("--max-restarts", type=int, default=3,
                    help="max wedge-recovery attempts before writing partial output (default 3)")
    ap.add_argument("--probe-timeout", type=int, default=12,
                    help="seconds for the wedge-detection probe (default 12)")
    ap.add_argument("--render-ms", type=int, default=recipe.RENDER_MS)
    ap.add_argument("--settle-poll", type=float, default=recipe.SETTLE_POLL)
    ap.add_argument("--settle-delay", type=float, default=recipe.SETTLE_DELAY)
    a = ap.parse_args()

    recipe.RENDER_MS, recipe.SETTLE_POLL, recipe.SETTLE_DELAY = a.render_ms, a.settle_poll, a.settle_delay
    try:
        os.environ.setdefault("PINCHTAB_TOKEN", json.load(open(a.config))["server"]["token"])
    except Exception:
        pass

    start_url = a.start

    # PinchTab 0.10.0 targets $PINCHTAB_TAB (a stored default that goes stale → "tab not
    # found" on every command). Pin it to a live tab up-front — critical for --single-url,
    # which READS the current page before any nav() would pin it.
    if a.single_url:
        recipe.pin_tab(a.server, start_url)

    # ---- browser position tracking + materialization (prefix-reuse, like recipe) ----
    mat_state = {"path": None}

    def materialize(path):
        try:
            if a.single_url:
                # No nav (it blanks the shell). Replay the click-path from the CURRENT
                # live position: path[0] is a child of the root state = persistent shell
                # chrome, present in every view, so it re-anchors us from wherever we are;
                # each subsequent click was discovered present after the previous one.
                # EXCEPTION: an href action is a cross-host/iframe HOP to a real URL — nav
                # to it (leaving the shell is fine; we're capturing that other app).
                for act in path:
                    if act.get("href"):
                        nav(act["href"], a.server)
                    else:
                        click_js(act["selector"], a.server)
                        settle(a.server)
                mat_state["path"] = path
                return True
            idx = max((i for i, act in enumerate(path) if act.get("href")), default=-1)
            cur = mat_state["path"]
            cidx = (max((i for i, act in enumerate(cur) if act.get("href")), default=-1)
                    if cur is not None else -2)
            reuse = (cur is not None and idx >= 0 and cidx >= 0 and idx < len(path) - 1
                     and path[idx]["href"] == cur[cidx]["href"])
            if reuse:
                rest = path[idx + 1:]
            elif idx == -1:
                nav(start_url, a.server)
                rest = path
            else:
                nav(path[idx]["href"], a.server)
                rest = path[idx + 1:]
            for act in rest:
                if act.get("href"):
                    nav(act["href"], a.server)
                else:
                    pt(["click", act["selector"]], a.server, timeout=20)
                    settle(a.server)
            mat_state["path"] = path
            return True
        except Exception as e:
            mat_state["path"] = None
            print("  ! materialize failed (%s)" % str(e)[:80], file=sys.stderr)
            return False

    def read_state():
        st = pt_json("({href:location.href, controls:%s})" % CONTROLS_JS, a.server)
        cur = (st.get("href") or "").strip().strip('"')
        view = None
        if a.single_url:
            try:
                view = pt_json(VIEW_JS, a.server)
            except Exception:
                view = None
        return cur, (st.get("controls") or []), view

    # ---- graph accumulators ----
    states = {}            # sig -> {id, url, label, depth}
    edges = []             # {from: sig, to: sig, label, selector, kind}
    triggers = []          # {label, state: sig, path: [actions], form, opensAt}
    forms_by_label = {}    # trigger-label -> (form, opensAt): read each unique form ONCE
    order = [0]

    def register(sig, url, label, depth):
        if sig not in states:
            states[sig] = {"id": "s%d" % order[0], "url": url.split("#")[0],
                           "label": label, "depth": depth}
            order[0] += 1
        return states[sig]

    def label_for(url, path):
        return path[-1]["label"] if path else (urlparse(url).path or "/")

    def close_modal():
        # Close an opened create-dialog WITHOUT submitting. Escape, settle, and if a
        # dialog is somehow still open, Escape once more (prevents a stuck modal from
        # contaminating the next trigger's form read).
        pt(["press", "Escape"], a.server)
        settle(a.server)
        try:
            n = pt_json('document.querySelectorAll('
                        '\'[role="dialog"],[aria-modal="true"],dialog[open]\').length', a.server)
            if int(str(n)) > 0:
                pt(["press", "Escape"], a.server)
                settle(a.server)
        except Exception:
            pass

    # ---- read every create trigger at the current (already materialized) state ----
    def capture_triggers(path, sig, controls):
        cand = [c for c in controls
                if c.get("text") and TRIGGER_RE.search(c["text"]) and not c.get("bulk")]
        # rank like recipe.score: real <button> first, shorter label first
        def score(c):
            tag, role = c.get("tag"), c.get("role")
            base = 3 if tag == "button" else (2 if role == "button" or tag == "input" else 1)
            return (base, -len(c.get("text") or ""))
        cand.sort(key=score, reverse=True)
        seen_labels = set()
        need_remat = False                                   # only re-nav when needed
        for c in cand:
            lab = c["text"].strip()
            key = lab.lower()
            if key in seen_labels:
                continue
            seen_labels.add(key)
            rec = {"label": lab, "state": sig,
                   "path": [{"label": x["label"], "selector": x["selector"],
                             "href": x.get("href")} for x in path],
                   "form": None, "opensAt": None}
            cached = forms_by_label.get(key)
            if cached is not None:                           # same trigger label seen before:
                rec["form"], rec["opensAt"] = cached         # reuse its form, don't re-open
                triggers.append(rec)
                continue
            if a.read_forms:
                try:
                    if need_remat:                           # previous trigger navigated away
                        materialize(path)
                        need_remat = False
                    before = pt(["eval", "location.href"], a.server)[1].strip().strip('"')
                    pt(["click", c["selector"]], a.server, timeout=30)
                    settle(a.server)
                    after = pt(["eval", "location.href"], a.server)[1].strip().strip('"')
                    form = pt_json(FORM_JS, a.server)
                    rec["form"] = form
                    if after.rstrip("/") != before.rstrip("/"):
                        rec["opensAt"] = after                # full-page form: must re-nav next
                        need_remat = True
                    else:
                        close_modal()                        # modal: just Escape (NEVER submit)
                except Exception as e:
                    print("  ! form read failed @ %r (%s)" % (lab, str(e)[:60]), file=sys.stderr)
                    close_modal()
                    need_remat = True
                forms_by_label[key] = (rec["form"], rec["opensAt"])   # cache (incl. failures)
            triggers.append(rec)
            print("    ✓ trigger %r%s" % (lab, " + form" if rec["form"] else ""),
                  file=sys.stderr)

    # ---- nav children of a state (what we enqueue to explore) ----
    def nav_children(cur, controls, iframes=()):
        sib = {}
        for c in controls:
            h = c.get("href")
            if h and h.startswith("http") and same_host(h, start_url):
                sib[section_key(h.split("#")[0])] = sib.get(section_key(h.split("#")[0]), 0) + 1
        datalist = {k for k, n in sib.items() if n >= a.data_list_min}

        def xhost_ok(u):
            # can we follow a control/iframe to another host? only with --cross-host, bounded
            if same_host(u, start_url):
                return True
            if not a.cross_host or xhost["n"] >= a.max_cross_host:
                return False
            xhost["n"] += 1
            return True

        kids, enq_links, enq_clicks, rows_taken = [], set(), set(), {}
        for c in controls:
            txt = c.get("text") or ""
            if c.get("bulk") or SKIP_NAV.search(txt):
                continue                       # never explore via create/destructive controls
            href = c.get("href")
            isnav = bool(c.get("nav"))
            if href:
                u = href.split("#")[0]
                if not u.startswith("http"):
                    continue
                cross = not same_host(u, start_url)
                if cross and not xhost_ok(u):
                    continue                   # cross-host link, budget off/exhausted → skip
                if u.rstrip("/") == cur.split("#")[0].rstrip("/") or u in enq_links:
                    continue
                if cross:                      # another app/host — a distinct node, not a data row
                    enq_links.add(u)
                    kids.append({"label": txt or u, "selector": c["selector"], "href": u,
                                 "kind": "external"})
                    continue
                sk = section_key(u)
                if sk in datalist:
                    # Descend into up to --rows-per-list REPRESENTATIVE rows of a data
                    # list (instead of skipping it), so per-row nested create-forms
                    # (e.g. a specific team → Members → Add Member) become reachable.
                    # A one-time crawl can afford one row; live per-query never can.
                    if rows_taken.get(sk, 0) >= a.rows_per_list:
                        continue
                    rows_taken[sk] = rows_taken.get(sk, 0) + 1
                    enq_links.add(u)
                    kids.append({"label": txt or u, "selector": c["selector"], "href": u,
                                 "kind": "row"})
                    continue
                enq_links.add(u)
                kids.append({"label": txt or u, "selector": c["selector"], "href": u,
                             "kind": "link"})
            elif isnav:
                key = txt.strip().lower() or ("@" + c["selector"])
                if key in enq_clicks:
                    continue
                enq_clicks.add(key)
                kind = "tab" if c.get("role") in NAV_ROLES else "menu"
                kids.append({"label": txt, "selector": c["selector"], "href": None,
                             "kind": kind})
        # EMBEDDED apps: follow each iframe's real src as a node (cross-host aware). This is
        # how the graph spans embeds like an in-Teams SharePoint doc library — pinchtab's
        # `frame` can't attach to cross-origin OOP iframes, but the src IS a real URL we nav to.
        for src in iframes:
            u = src.split("#")[0]
            if not u.startswith("http") or u in enq_links:
                continue
            if not xhost_ok(u):
                continue
            enq_links.add(u)
            kids.append({"label": "⧉ " + (urlparse(u).hostname or u), "selector": None,
                         "href": u, "kind": "iframe"})
        return kids

    # ---- persistence: build the graph + write it ATOMICALLY (temp then os.replace, so a
    # file is never half-written). Called at every checkpoint, on normal completion, AND
    # from the signal handler below — so a crash, OOM, 2-min kill or Ctrl-C NEVER loses the
    # crawl (the exact failure mode that lost a 50-state run before). ----
    path_out = os.path.expanduser("~/code/pinchtab-webgraph/%s.json" % a.out)
    persist_flags = {"final_done": False}

    def persist(final=False, reason="in-progress"):
        out = {
            "meta": {"start": start_url, "host": urlparse(start_url).hostname,
                     "states": len(states), "edges": len(edges), "triggers": len(triggers),
                     "max_depth": a.max_depth, "tool": "interaction_crawl.py",
                     "complete": bool(final), "stopped": reason},
            "states": list(states.values()),
            "state_index": {sig: st["id"] for sig, st in states.items()},
            "edges": [{"from": states[e["from"]]["id"] if e["from"] in states else None,
                       "to": states[e["to"]]["id"] if e["to"] in states else None,
                       "label": e["label"], "selector": e["selector"], "kind": e["kind"]}
                      for e in edges if e["from"] in states],
            "triggers": [{"label": t["label"],
                          "state": states[t["state"]]["id"] if t["state"] in states else None,
                          "path": t["path"], "form": t["form"], "opensAt": t["opensAt"]}
                         for t in triggers],
        }
        tmp = path_out + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(out, fh, indent=2)
        os.replace(tmp, path_out)                    # atomic swap — reader never sees a partial file
        if final:
            persist_flags["final_done"] = True
        return len(out["edges"])

    # Flush-on-exit: even an external SIGTERM (2-min Bash limit) / SIGINT (Ctrl-C) writes the
    # partial graph before dying, marked complete=false. atexit covers uncaught exceptions;
    # it's a no-op once a final write happened (never clobbers complete=true with false).
    dying = {"sig": False}
    def on_signal(signum, frame):
        if dying["sig"]:
            os._exit(130)
        dying["sig"] = True
        print("\n  ! signal %d — flushing partial graph before exit" % signum, file=sys.stderr)
        try:
            persist(final=False)
        finally:
            os._exit(130)
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    atexit.register(lambda: None if persist_flags["final_done"] else persist(final=False))

    # ---- BFS over states, recording the graph ----
    # A priority min-heap keyed (depth, tier, seq): depth is primary so it stays a
    # true BFS (all depth-d items drain before depth-(d+1)); within a depth, the
    # STRUCTURAL tier (link/tab < row < menu) orders breadth-first nav ahead of deep
    # data-list row descents, so sibling tabs aren't starved by row-by-row dives.
    # Heap entries: (depth, tier, seq, (path, parent_sig, action_taken)).
    # external/iframe explored LAST within a depth: following a cross-host hop leaves the
    # (single-URL) shell, which can strand not-yet-materialized in-shell siblings — do them first.
    TIER = {"link": 1, "tab": 1, "row": 2, "menu": 3, "external": 4, "iframe": 4}
    queue = []
    seq = [0]
    def enqueue(item, depth, tier):
        heapq.heappush(queue, (depth, tier, seq[0], item))
        seq[0] += 1
    enqueue(([], None, None), 0, 0)
    retry_queue = deque()              # wedge-recovery re-tries jump the heap (FIFO)
    enq = {(): None}                   # path-tuple dedup (legacy guard, still cheap)
    url_to_sig = {}                    # norm(url) -> state sig: global URL dedup
    enq_urls = set()                   # norm(url) already enqueued-or-visited (any parent)
    visited_click_keys = set()         # (parent_sig, label.lower()) for click-only children
    xhost = {"n": 0}                    # count of distinct other-host nodes followed (--cross-host)
    visits = 0
    restart_attempts = 0
    gave_up = False
    sec_counts = {}
    print("Interaction crawl from %s (max %d states / %d visits, depth %d)"
          % (start_url, a.max_states, a.max_visits, a.max_depth), file=sys.stderr)

    while (queue or retry_queue) and len(states) < a.max_states and visits < a.max_visits:
        if retry_queue:                                    # recovered items first, in order
            path, psig, pact = retry_queue.popleft()
        else:
            _, _, _, (path, psig, pact) = heapq.heappop(queue)
        # Process this state's browser I/O (materialize + DOM read) under one guard:
        # a wedged bridge often passes materialize() (nav/settle swallow errors) and
        # only surfaces at read_state(), so BOTH must route to the same recovery.
        cur = controls = view = None
        ok = materialize(path)
        if ok:
            try:
                cur, controls, view = read_state()
            except Exception as e:
                print("  ! read_state failed (%s)" % str(e)[:70], file=sys.stderr)
                ok = False
        if not ok:
            mat_state["path"] = None                       # probe navigates; invalidate cursor
            if probe_bridge(a.server, start_url, a.probe_timeout, a.single_url):
                continue                                   # bridge alive → bad path → skip
            if restart_attempts >= a.max_restarts:
                print("  ! max restarts (%d) reached — writing partial output"
                      % a.max_restarts, file=sys.stderr)
                gave_up = True
                break                                      # fall through to persist
            restart_attempts += 1
            if recover_bridge(a.server, a.restart_cmd, a.login_cmd, restart_attempts):
                mat_state["path"] = None
                retry_queue.appendleft((path, psig, pact))  # retry this item next
            continue
        visits += 1
        restart_attempts = 0                               # progress clears the consecutive-wedge tally
        sig = state_sig(cur, controls, view)

        # record the edge that brought us here (even to an already-known state)
        if psig is not None and pact is not None:
            edges.append({"from": psig, "to": sig, "label": pact["label"],
                          "selector": pact["selector"], "kind": pact["kind"]})

        if sig in states:
            continue                            # already expanded this state
        node = register(sig, cur, label_for(cur, path), len(path))
        if a.dump_controls:                     # full per-state control inventory (generic)
            node["controls"] = [{"text": c.get("text"), "role": c.get("role"),
                                 "tag": c.get("tag"), "href": c.get("href"),
                                 "nav": c.get("nav"), "selector": c.get("selector")}
                                for c in controls]
        if a.capture_content:                   # data collections (tables/grids/trees/lists)
            try:
                node["collections"] = capture_collections(a.server)
            except Exception as e:              # a wedge here must not kill the crawl: keep
                node["collections"] = []        # the nav/controls we already have and move on
                mat_state["path"] = None        # cursor may be dirty after a failed scroll
                print("  ! content capture failed (%s) — nav+controls only"
                      % str(e)[:50], file=sys.stderr)
        url_to_sig[norm(cur)] = sig             # this URL now resolves to a known state
        ncol = sum(c.get("count", 0) for c in node.get("collections", []))
        print("· [%d states / %d visits] depth %d · %s (%d controls%s)"
              % (len(states), visits, len(path), cur, len(controls),
                 (", %d items" % ncol) if a.capture_content else ""), file=sys.stderr)

        capture_triggers(path, sig, controls)

        if a.checkpoint_every > 0 and len(states) % a.checkpoint_every == 0:
            persist()                           # incremental flush — never lose progress

        if len(path) >= a.max_depth:
            continue
        iframes = []
        if a.cross_host:                        # only pay the extra read when following embeds
            try:
                iframes = pt_json("[...document.querySelectorAll('iframe[src]')]"
                                  ".map(f=>f.src).filter(s=>/^https?:/.test(s))", a.server) or []
            except Exception:
                iframes = []
        for ch in nav_children(cur, controls, iframes):
            href = ch.get("href")
            if href:
                n = norm(href)
                if n in url_to_sig:
                    # already a recorded state: capture the cross-edge here (it's the
                    # only place we'd see it, since we won't re-visit) and don't enqueue
                    edges.append({"from": sig, "to": url_to_sig[n], "label": ch["label"],
                                  "selector": ch["selector"], "kind": ch["kind"]})
                    continue
                if n in enq_urls:
                    continue                    # pending from another parent — visit once
                enq_urls.add(n)                 # claim it before the per-section cap below
                sk = section_key(href)
                if sec_counts.get(sk, 0) >= a.max_per_section:
                    continue
                sec_counts[sk] = sec_counts.get(sk, 0) + 1
            else:
                ck = (sig, ch["label"].lower())  # click-only tab/menu: dedup per parent
                if ck in visited_click_keys:
                    continue
                visited_click_keys.add(ck)
            cpath = path + [ch]
            k = tuple((x["selector"], x.get("href")) for x in cpath)
            if k in enq:
                continue
            enq[k] = sig
            tier = TIER.get(ch["kind"], 1)
            # cross-host/iframe hops leave the page: in SINGLE-URL that strands not-yet-
            # materialized in-shell siblings → do them LAST; in normal (nav) mode each nav
            # is independent, so explore embeds/other-hosts alongside links (tier 1).
            if ch["kind"] in ("external", "iframe"):
                tier = 4 if a.single_url else 1
            enqueue((cpath, sig, ch), len(cpath), tier)

    # ---- why did the crawl stop? Be EXPLICIT — a silently truncated crawl reads as
    # "captured everything" when it didn't. Only a drained frontier means full coverage. ----
    if gave_up:
        reason = "wedge-gave-up"
    elif not queue and not retry_queue:
        reason = "frontier-exhausted"                     # nothing left to explore = complete
    elif len(states) >= a.max_states:
        reason = "hit-max-states(%d)" % a.max_states
    elif visits >= a.max_visits:
        reason = "hit-max-visits(%d)" % a.max_visits
    else:
        reason = "stopped"
    complete = (reason == "frontier-exhausted")

    # ---- final persist ---- (complete=true only when the whole frontier was drained)
    nedges = persist(final=complete, reason=reason)
    persist_flags["final_done"] = True                    # this write is authoritative — stop
    #                                                       atexit clobbering the real 'stopped'
    note = ("" if complete else
            "  ⚠ TRUNCATED (%s) — %d nav paths still queued; raise the cap for fuller coverage"
            % (reason, len(queue) + len(retry_queue)))
    print("\nWrote %s: %d states, %d edges, %d triggers  [stopped: %s]%s"
          % (path_out, len(states), nedges, len(triggers), reason, note), file=sys.stderr)

    if gave_up:                                            # gave up mid-crawl after a wedge:
        sys.exit(1)                                        # output written, but signal incomplete


if __name__ == "__main__":
    main()
