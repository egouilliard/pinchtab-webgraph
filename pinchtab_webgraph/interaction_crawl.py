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

  scripts/run-crawl-interactions.sh https://app.example.com/home
  python3 pinchtab_webgraph/interaction_crawl.py --start https://app.example.com/home --out out/interaction-graph

For long crawls the headless bridge can WEDGE (nav/click time out though health
says ok). Pass your environment's bridge-relaunch and re-login commands to enable
auto-recovery — a wedge is then detected, the bridge restarted, and the in-memory
BFS resumed (partial output is still written if recovery gives up):

  python3 pinchtab_webgraph/interaction_crawl.py --start https://app.example.com/home \\
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
from urllib.parse import urlparse, urlsplit, urlunsplit, parse_qsl, urlencode

from . import recipe  # proven primitives — see module docstring
from .commands import is_direct_download
from .crawl import DOWNLOAD_ACTIONS

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


def serialize_trigger(t, state_id):
    """The trigger record written to the output graph. Carries the terminal-action
    descriptor (`kind`/`selector`/`href`/`accept`) the command compiler + `perform` read —
    a download trigger is useless downstream without it. Additive: defaults keep old
    create-form triggers valid (`kind`='form')."""
    return {"label": t["label"], "state": state_id,
            "path": t["path"], "form": t["form"], "opensAt": t["opensAt"],
            "kind": t.get("kind", "form"), "selector": t.get("selector"),
            "href": t.get("href"), "accept": t.get("accept")}

# STRUCTURAL form-bearing detection (Finding 2 of issue #11). A create-VERB in a
# control label is only ONE kind of trigger; many important forms — sign-in, sign-up,
# contact — carry no create-verb anywhere. So we ALSO treat a state as a trigger
# target when it structurally IS a form: it renders real input/select/textarea fields
# PLUS a submit-like control. Generic + structural — keys on form shape (fields +
# submit affordance), never on app/section vocabulary. Returns {fields, hasPassword,
# submit} so the caller can gate (≥2 fields, or ≥1 field + a password, + a submit).
FORM_BEARING_JS = r"""
(() => {
  const vis=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0;};
  const SKIP=new Set(['hidden','submit','button','image','reset','search']);
  const inputs=[...document.querySelectorAll('input,select,textarea')].filter(el=>{
    if(!vis(el)) return false;
    const t=(el.getAttribute('type')||'text').toLowerCase();
    return !SKIP.has(t);
  });
  const hasPw=inputs.some(el=>(el.getAttribute('type')||'').toLowerCase()==='password');
  const submit=[...document.querySelectorAll(
    'button,input[type=submit],[type=submit],[role=button]')].some(vis);
  return {fields:inputs.length, hasPassword:hasPw, submit:submit};
})()
"""

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
    # SINGLE-URL MODE ONLY. Normal nav mode uses nav_state_key(url) for a deterministic
    # URL-primary identity (see the sig assignment in the crawl loop); this structural
    # signature is reserved for app-shell SPAs where the URL never changes.
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


# Generic web-analytics TRACKING params (key-pattern denylist, NOT app/section vocabulary
# — the same "generic web-standard" class as the create-VERB list; see generality.md). A
# link decorated with tracking junk must dedup to the same state as its clean form.
_TRACKING_RE = re.compile(r"^utm_|^mc_|^(gclid|fbclid|_ga|igshid|ref|ref_src|yclid|msclkid)$", re.I)


def norm(u):
    # URL-normalization used for BOTH state identity (nav mode) and every URL dedup
    # (url_to_sig, enq_urls, cross-edge match, self-link skip) — so they all agree.
    # Drops #fragment + trailing '/', strips generic TRACKING params, and re-emits the
    # remaining query SORTED (stable order). Content/revision/pagination params (oldid,
    # page, section, q, …) are KEPT distinct — folding them would wrongly merge pages.
    parts = urlsplit(u.split("#")[0])
    if parts.query:
        kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                if not _TRACKING_RE.match(k)]
        query = urlencode(sorted(kept))
    else:
        query = ""
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), query, ""))


def nav_state_key(url):
    # Nav-mode state identity is URL-primary: one normalized URL == one state, so
    # control-count / feed-content jitter between reads can't mint duplicate states
    # (the over-noding bug — MDN 20 states = 5 URLs). The 'u::' namespace keeps a nav
    # key from ever colliding with a single-URL structural state_sig value. Known
    # limitation: hash-router SPAs ('/#/route') collapse to one state here since norm()
    # drops '#' (single-URL app-shells keep the structural state_sig instead).
    return "u::" + norm(url)


def shell_blanked(before_url, after_url):
    # The same URL-changed comparison inlined in capture_triggers today, lifted out as
    # the single-URL "did opening this trigger navigate away / blank the shell?"
    # predicate: True iff the URL changed (trailing slash ignored). None/empty after is
    # treated defensively as changed (unsafe — never read that DOM as a form).
    return (after_url or "").rstrip("/") != (before_url or "").rstrip("/")


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


# ---- crawl-start structural probe: is this a single-URL app-shell? -------------------
# GENERIC auto-detection of the mode --single-url gates, so the operator no longer has to
# know in advance. Purely structural (control counts + the URL + ARIA view markers) — NO
# app routes/labels/vocabulary. The defining property of an app-shell SPA (e.g. MS Teams)
# is that state transitions swap the view IN PLACE without changing the URL, whereas a
# normal multi-page site changes the URL. So we exercise a few representative nav controls
# on the live page and watch the URL: the first control that changes the URL PATH proves
# URL-primary routing (→ nav mode); a control that swaps the visible control set / active
# ARIA view while the URL path stays put proves an app-shell (→ single-URL mode).
# NON-DESTRUCTIVE by design — clicking a nav control is exactly what the crawl does anyway,
# and an app-shell's chrome is persistent, so a swapped view still exposes every top-level
# control. CONSERVATIVE: too little rendered, no observable transition, or any read error
# → False, which preserves today's nav-mode default (the explicit --single-url / the new
# --no-single-url force the mode either way and skip this probe entirely).
DETECT_MIN_CONTROLS = 8      # the live page must be substantially rendered to judge it
DETECT_MAX_PROBES = 3        # nav controls to try before defaulting to nav mode


def _ctrl_sig(controls):
    # Structural fingerprint of a view: the set of non-bulk control-label prefixes (the
    # same signal state_sig keys on) — lets us tell "the view swapped" from "nothing moved".
    return frozenset((c.get("text") or "")[:30] for c in controls if not c.get("bulk"))


def detect_single_url(server, start_url):
    """Structurally decide whether `start_url` is a single-URL app-shell SPA that must be
    crawled in single-URL mode. Returns True (app-shell) or False (normal nav mode). See
    the block comment above for the heuristic. Any failure → False (safe nav-mode default)."""
    def live():
        st = pt_json("({href:location.href, controls:%s})" % CONTROLS_JS, server)
        try:
            view = pt_json(VIEW_JS, server) or []
        except Exception:
            view = []
        return ((st.get("href") or "").strip().strip('"'),
                st.get("controls") or [], view)

    try:
        recipe.pin_tab(server, start_url)                # target the live app tab (0.10.0)
        u0, controls0, view0 = live()
    except Exception:
        return False
    if len(controls0) < DETECT_MIN_CONTROLS:
        return False                                     # nothing substantial rendered → nav
    base0, sig0, view0set = norm(u0), _ctrl_sig(controls0), set(view0)

    def is_link(c):                                      # a same-host http link that isn't a
        h = c.get("href") or ""                          # download — clicking it WOULD route
        return (h.startswith("http") and same_host(h, start_url)  # a normal multi-page site.
                and not is_direct_download(h))

    def kind(c):                                         # probe URL-changing links first: one
        if is_link(c):                                   # URL change is decisive for nav mode.
            return 0
        return 1 if c.get("role") in NAV_ROLES else 2
    cands = [c for c in controls0
             if c.get("selector") and not c.get("bulk")
             and not SKIP_NAV.search(c.get("text") or "")
             and not TRIGGER_RE.search(c.get("text") or "")   # never probe a create-trigger
             and (c.get("nav") or c.get("role") in NAV_ROLES or is_link(c))]
    cands.sort(key=kind)
    # the nav control that owns the operator's ORIGINAL view (its label is an active aria
    # marker) — used to click back after a probe so single-URL mode reads the --start view
    # as its root, not whatever the probe swapped into (single-URL mode never nav()s back).
    home_sel = next((c["selector"] for c in cands
                     if (c.get("text") or "")[:30] in view0set), None)
    for c in cands[:DETECT_MAX_PROBES]:
        try:
            click_js(c["selector"], server)              # JS-dispatch: occlusion-proof click
            settle(server)
            u1, controls1, view1 = live()
        except Exception:
            continue
        if norm(u1) != base0:
            return False                                 # URL path changed → URL-primary nav
        if _ctrl_sig(controls1) != sig0 or set(view1) != view0set:
            # App-shell. Best-effort restore of the original view so the crawl's root state
            # matches --start. If we can't identify the home control (no aria markers), we
            # stay put — the same mild drift as before, never worse; nav mode needs no
            # restore since its first materialize re-navs to start_url.
            if home_sel:
                try:
                    click_js(home_sel, server)
                    settle(server)
                except Exception:
                    pass
            return True                                  # view swapped in place → app-shell
    return False


def recover_bridge(server, restart_cmd, login_cmd, attempt):
    # Mirror scripts/hard-bench.sh's ensure_browser(): kill the stale bridge by its PORT
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
    ap.add_argument("--config", default=os.environ.get("PINCHTAB_CONFIG", "crawl-config.json"))
    ap.add_argument("--out", default="out/interaction-graph")
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
    ap.add_argument("--single-url", dest="single_url", action="store_true", default=None,
                    help="FORCE single-URL app-shell mode: NEVER navigate (a programmatic nav "
                         "blanks such SPAs, e.g. MS Teams) — read the current live page and "
                         "drive it with JS-dispatch clicks; key states by active aria-view. "
                         "Form-open triggers ARE read here via in-place JS-dispatch clicks "
                         "with an automatic shell-blank guard (never a real navigation); a "
                         "trigger that can't open without navigating is recorded with "
                         "form: null and the crawl recovers in place. "
                         "Omit BOTH single-url flags to auto-detect this structurally at start.")
    ap.add_argument("--no-single-url", dest="single_url", action="store_false", default=None,
                    help="FORCE normal nav mode: skip the app-shell auto-detection probe. The "
                         "escape hatch for when detection guesses wrong on a normal site.")
    ap.add_argument("--read-forms", dest="read_forms", action="store_true", default=True,
                    help="open+read each create form (default on)")
    ap.add_argument("--no-read-forms", dest="read_forms", action="store_false",
                    help="record triggers but skip opening their forms (faster, no form specs)")
    ap.add_argument("--capture-form-states", dest="capture_form_states",
                    action="store_true", default=True,
                    help="ALSO register a state that structurally IS a form (input/select/"
                         "textarea fields + a submit control) as a trigger target, even "
                         "when no control carries a create-VERB — so sign-in / sign-up / "
                         "contact forms are reachable via howto (default ON, generic).")
    ap.add_argument("--no-capture-form-states", dest="capture_form_states",
                    action="store_false",
                    help="only register create-VERB-labeled triggers (legacy behavior)")
    ap.add_argument("--restart-cmd", default="",
                    help="shell command to relaunch the bridge after a wedge "
                         "(empty = no relaunch, just kill the stale PID)")
    ap.add_argument("--login-cmd", default="",
                    help="shell command to re-authenticate after restart (empty = none)")
    ap.add_argument("--login-config", default="",
                    help="OPT-IN automated login (default off): path to a gitignored "
                         "per-host routing file. The password is read from the OS keyring "
                         "at runtime, NEVER from this file — see pinchtab_webgraph/login.py. "
                         "Empty = log in by hand in the bridge profile instead. Also becomes "
                         "the wedge-recovery re-auth unless --login-cmd is set.")
    ap.add_argument("--max-restarts", type=int, default=3,
                    help="max wedge-recovery attempts before writing partial output (default 3)")
    ap.add_argument("--probe-timeout", type=int, default=12,
                    help="seconds for the wedge-detection probe (default 12)")
    ap.add_argument("--render-ms", type=int, default=recipe.RENDER_MS)
    ap.add_argument("--settle-poll", type=float, default=recipe.SETTLE_POLL)
    ap.add_argument("--settle-delay", type=float, default=recipe.SETTLE_DELAY)
    a = ap.parse_args()
    os.makedirs(os.path.dirname(os.path.abspath(a.out)) or ".", exist_ok=True)

    recipe.RENDER_MS, recipe.SETTLE_POLL, recipe.SETTLE_DELAY = a.render_ms, a.settle_poll, a.settle_delay
    try:
        os.environ.setdefault("PINCHTAB_TOKEN", json.load(open(a.config))["server"]["token"])
    except Exception:
        pass

    start_url = a.start

    # OPT-IN automated login (off unless --login-config is given). Establishes an
    # authenticated session BEFORE crawling (and before we probe the shell below); the
    # password is read from the OS keyring at runtime, never from disk here — see login.py.
    if a.login_config:
        import shlex
        from . import login as _login
        _login.ensure_logged_in(a.login_config, start_url, a.server)
        if not a.login_cmd:            # reuse the same login for wedge-recovery re-auth
            a.login_cmd = ("%s -m pinchtab_webgraph.login --config %s --server %s "
                           "--host %s --pinchtab-config %s") % (
                shlex.quote(sys.executable), shlex.quote(a.login_config),
                shlex.quote(a.server), shlex.quote(urlparse(start_url).hostname or ""),
                shlex.quote(a.config))

    # Resolve single-URL app-shell mode. --single-url / --no-single-url force it either way
    # (a.single_url is True/False); otherwise it's None → auto-detect structurally against
    # the live shell (must run AFTER login so an authenticated shell is what we probe).
    if a.single_url is None:
        a.single_url = detect_single_url(a.server, start_url)
        print("· single-URL app-shell mode: %s (auto-detected)"
              % ("on" if a.single_url else "off"), file=sys.stderr, flush=True)
    else:
        print("· single-URL app-shell mode: %s (explicit)"
              % ("on" if a.single_url else "off"), file=sys.stderr, flush=True)

    # PinchTab 0.10.0 targets $PINCHTAB_TAB (a stored default that goes stale → "tab not
    # found" on every command). Pin it to a live tab up-front — critical for single-URL
    # mode, which READS the current page before any nav() would pin it. (Auto-detect
    # already pinned it above; re-pinning is cheap and keeps the explicit path correct.)
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
    form_state_urls = set()  # norm(url) already registered as a form-bearing trigger (dedup)
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
        shell_dead = False                                   # single-url: shell unrecoverable
        for c in cand:
            lab = c["text"].strip()
            key = lab.lower()
            if key in seen_labels:
                continue
            seen_labels.add(key)
            rec = {"label": lab, "state": sig,
                   "path": [{"label": x["label"], "selector": x["selector"],
                             "href": x.get("href")} for x in path],
                   "form": None, "opensAt": None,
                   "kind": "form", "selector": c["selector"], "href": None, "accept": None}
            cached = forms_by_label.get(key)
            if cached is not None:                           # same trigger label seen before:
                rec["form"], rec["opensAt"] = cached         # reuse its form, don't re-open
                triggers.append(rec)
                continue
            if a.single_url and shell_dead:                  # shell couldn't be recovered:
                triggers.append(rec)                         # record the trigger, form=None,
                continue                                     # don't poison the label cache
            if a.read_forms:
                if not a.single_url and need_remat:          # normal mode: re-anchor BEFORE
                    materialize(path)                        # reading 'before' — a prior full-
                    need_remat = False                       # page form left us off-position
                before = pt(["eval", "location.href"], a.server)[1].strip().strip('"')
                if not a.single_url:
                    try:
                        pt(["click", c["selector"]], a.server, timeout=30)
                        settle(a.server)
                        after = pt(["eval", "location.href"], a.server)[1].strip().strip('"')
                        form = pt_json(FORM_JS, a.server)
                        rec["form"] = form
                        if after.rstrip("/") != before.rstrip("/"):
                            rec["opensAt"] = after            # full-page form: must re-nav next
                            need_remat = True
                        else:
                            close_modal()                    # modal: just Escape (NEVER submit)
                    except Exception as e:
                        print("  ! form read failed @ %r (%s)" % (lab, str(e)[:60]), file=sys.stderr)
                        close_modal()
                        need_remat = True
                else:
                    # single-url: JS-dispatch click (no real nav). Read the form ONLY if the
                    # shell didn't blank; recovery is immediate (materialize re-anchors us).
                    try:
                        click_js(c["selector"], a.server)
                        settle(a.server)
                        after = pt(["eval", "location.href"], a.server)[1].strip().strip('"')
                        if shell_blanked(before, after):
                            # opening navigated away / blanked the shell — do NOT read that
                            # DOM as a form; leave form/opensAt None and recover in place.
                            print("  ! shell blanked opening %r (single-url) — recovering" % lab,
                                  file=sys.stderr)
                            if not materialize(path):
                                shell_dead = True
                                print("  ! could not recover shell position — skipping "
                                      "remaining triggers this state", file=sys.stderr)
                        else:
                            rec["form"] = pt_json(FORM_JS, a.server)   # safe in-place open
                            close_modal()                    # modal: just Escape (NEVER submit)
                    except Exception as e:
                        print("  ! single-url form read failed @ %r (%s)" % (lab, str(e)[:60]),
                              file=sys.stderr)
                        if not materialize(path):
                            shell_dead = True
                            print("  ! could not recover shell position — skipping "
                                  "remaining triggers this state", file=sys.stderr)
                forms_by_label[key] = (rec["form"], rec["opensAt"])   # cache (incl. failures)
            triggers.append(rec)
            print("    ✓ trigger %r%s" % (lab, " + form" if rec["form"] else ""),
                  file=sys.stderr)

        # download/export controls are a distinct terminal action, recorded but NEVER
        # clicked (a JS download can pop a native save dialog; a direct link would fetch
        # the file). We keep the selector + (for a direct link) the file URL, no form —
        # the command compiler turns these into a `pinchtab download`/`click`.
        for c in controls:
            t = (c.get("text") or "").strip()
            if not t or c.get("bulk"):
                continue
            direct = is_direct_download(c.get("href"))
            if not (direct or DOWNLOAD_ACTIONS.search(t)):
                continue
            key = t.lower()
            if key in seen_labels:
                continue
            seen_labels.add(key)
            triggers.append({"label": t, "state": sig,
                             "path": [{"label": x["label"], "selector": x["selector"],
                                       "href": x.get("href")} for x in path],
                             "form": None, "opensAt": None, "kind": "download",
                             "selector": c["selector"],
                             "href": c.get("href") if direct else None, "accept": None})
            print("    ✓ download %r%s" % (t, " (direct)" if direct else ""), file=sys.stderr)

    # ---- Finding 2: register a state that structurally IS a form (fields + submit)
    #      as a trigger target, even with no create-VERB anywhere. The "trigger" is the
    #      nav control that OPENED this form (it lives on the PARENT state) — same shape
    #      as a create-trigger whose click opens a form: howto routes to the parent,
    #      then "Click <label>" lands on the form. MUST run BEFORE capture_triggers,
    #      while the browser is cleanly materialized at this state (capture_triggers may
    #      navigate away opening full-page create forms). Reached-by-nav only: a pure
    #      root/inline form is skipped (its /login route, if crawled, is the clean answer). --
    def capture_form_state(path, sig, psig, pact):
        if not a.capture_form_states or not path or pact is None or psig is None:
            return
        try:
            fb = pt_json(FORM_BEARING_JS, a.server)
        except Exception:
            return
        fields = fb.get("fields", 0) if isinstance(fb, dict) else 0
        # a real form: ≥2 fields, or ≥1 field guarded by a password (auth form) — plus a
        # submit affordance. One search box or a lone filter input won't qualify.
        if not (fb and fb.get("submit") and
                (fields >= 2 or (fields >= 1 and fb.get("hasPassword")))):
            return
        cur = pt(["eval", "location.href"], a.server)[1].strip().strip('"')
        nurl = norm(cur)
        if nurl in form_state_urls:
            return                                           # this form already registered
        if any(norm(t.get("opensAt") or "") == nurl for t in triggers if t.get("opensAt")):
            return                                           # a create-trigger already opens it
        form = None
        if a.read_forms:
            try:
                form = pt_json(FORM_JS, a.server)            # the form IS the page — no click
            except Exception as e:
                print("  ! form-state read failed @ %s (%s)" % (nurl, str(e)[:50]),
                      file=sys.stderr)
        # label = what the user clicked to get here ("Sign in"/"Join now"); fall back to
        # the form title / first submit button when the nav label is empty.
        label = (pact.get("label") or "").strip()
        if not label and form:
            label = (form.get("title") or
                     (form.get("submitButtons") or [""])[0] or "").strip()
        if not label:
            return
        form_state_urls.add(nurl)
        triggers.append({"label": label, "state": psig,        # trigger lives on the parent
                         "path": [{"label": x["label"], "selector": x["selector"],
                                   "href": x.get("href")} for x in path[:-1]],
                         "form": form, "opensAt": cur,
                         "kind": "form", "selector": pact.get("selector"),
                         "href": None, "accept": None})
        print("    ✓ form-state trigger %r @ %s (%d fields)"
              % (label, nurl, (form or {}).get("fieldCount", fields)), file=sys.stderr)

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
    path_out = os.path.abspath("%s.json" % a.out)
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
            "triggers": [serialize_trigger(
                t, states[t["state"]]["id"] if t["state"] in states else None)
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
        # State identity: URL-primary in normal nav mode (one normalized URL == one
        # state → same-URL re-materializations collapse at the `if sig in states` dedup
        # below, killing over-noding); structural signature + ARIA view ONLY for
        # single-URL app-shells (Teams etc.) where the URL never changes.
        sig = state_sig(cur, controls, view) if a.single_url else nav_state_key(cur)

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

        capture_form_state(path, sig, psig, pact)   # BEFORE capture_triggers (clean DOM)
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
