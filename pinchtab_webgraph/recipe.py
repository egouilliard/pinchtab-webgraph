#!/usr/bin/env python3
"""
"How do I do X?" for a crawled web app, via PinchTab.

Given a goal (e.g. "add CAE", "create product"), this:
  1. locates the trigger control (a button/link whose text matches the goal),
  2. (optionally) shows the navigation path to the page that hosts it,
  3. CLICKS it to open the form/modal — but NEVER submits,
  4. introspects the form: every field, its type, whether it's required, options,
  5. screenshots the open form, then CANCELS (Escape) without saving,
  6. prints a step-by-step how-to + a JSON spec of the form.

Safe by design: it opens and reads a form, then cancels. It does not click
Save/Submit/Create-confirm, so it does not persist data. (On most apps opening
a "Create X" dialog creates nothing until you submit.)

  ./run-recipe.sh --goal "add item"  --page https://app.example.com/items/123
  ./run-recipe.sh --goal "create team" --start https://app.example.com/home
"""
import argparse
import heapq
import json
import os
import re
import subprocess
import sys
import time
from collections import deque
from urllib.parse import urlparse


def same_host(u, ref):
    def h(x):
        n = (urlparse(x).hostname or "").lower()
        return n[4:] if n.startswith("www.") else n
    return h(u) == h(ref)

# create-style verbs (EN + ES — covers bilingual apps; extend for other locales)
VERBS = r"create|add|new|start|crear|nuevo|nueva|añadir|anadir|agregar|generar"

# Tokens dropped from a goal before we match it against trigger labels: articles /
# prepositions, the create-VERBS themselves (they are matched via VERBS, not as a
# noun), and their ES equivalents. Short (≤2-char) tokens are ALSO dropped in
# goal_nouns() — they cause spurious substring hits ("in" inside "Find", "a"/"to"
# everywhere). Generic, no app/section vocabulary.
GOAL_STOPWORDS = {
    "a", "an", "the", "to", "of", "for", "in", "on", "at", "by", "with", "and",
    "or", "my", "me", "i", "do", "how", "some", "this", "that",
    "create", "add", "new", "make", "start", "generate",
    "crear", "nuevo", "nueva", "añadir", "anadir", "agregar", "generar",
    "un", "una", "el", "la", "los", "las", "de", "para",
}


def goal_nouns(goal):
    """Content tokens of a goal: lowercase word tokens minus GOAL_STOPWORDS and any
    ≤2-char token. These are the meaningful words a trigger label must share for a
    match — keying on them (not the raw goal) is what stops `in`/`a`/`to` and the
    generic create-VERBS from producing false positives. Generic, stdlib only."""
    return [w for w in re.findall(r"\w+", (goal or "").lower())
            if w not in GOAL_STOPWORDS and len(w) > 2]


def noun_alt(nouns):
    """Alternation that matches any goal noun as a WHOLE WORD, tolerating a trailing
    English plural (report→reports, box→boxes, cliente→clientes). The LEADING boundary
    blocks a short noun matching inside a longer word ("in" inside "Find"); the optional
    plural + TRAILING boundary blocks prefix matches ("sign" inside "signature") while
    still catching plurals. Lexical, not a stemmer — generic, no app vocabulary."""
    alt = "|".join(re.escape(n) for n in nouns)
    return r"\b(?:%s)(?:es|s)?\b" % alt


def goal_needle(goal, match=None):
    """The trigger-label regex for a live search: a create-VERB adjacent (≤30 chars)
    to a goal noun, in either order, with noun_alt()'s word-boundary/plural handling so
    a short noun can't match inside an unrelated word. Falls back to bare VERBS when the
    goal has no content nouns. `match` (an explicit user regex) overrides."""
    if match:
        return match
    nouns = goal_nouns(goal)
    if not nouns:
        return r"\b(?:%s)\b" % VERBS
    np = noun_alt(nouns)
    return r"(?:%s)\b.{0,30}%s|%s.{0,30}\b(?:%s)" % (VERBS, np, np, VERBS)

# --- stable CSS selector + form introspection, injected into the page ---------
TRIGGER_JS = r"""
(needle => {
  const re = new RegExp(needle, 'i');
  function cssEsc(s){return (window.CSS&&CSS.escape)?CSS.escape(s):s.replace(/[^a-zA-Z0-9_-]/g,'\\$&');}
  function sel(el){ if(el.id) return '#'+cssEsc(el.id);
    const parts=[]; let e=el;
    while(e&&e.nodeType===1&&e!==document.body){ let p=e.tagName.toLowerCase();
      const par=e.parentElement;
      if(par){const s=Array.prototype.filter.call(par.children,c=>c.tagName===e.tagName);
        if(s.length>1)p+=':nth-of-type('+(s.indexOf(e)+1)+')';}
      parts.unshift(p); e=par; if(parts.length>8)break; }
    return parts.join('>'); }
  const out=[];
  document.querySelectorAll('button,[role="button"],a[href],[role="menuitem"],input[type="submit"]').forEach(b=>{
    const t=(b.innerText||b.value||b.getAttribute('aria-label')||'').trim();
    const r=b.getBoundingClientRect(); if(r.width===0&&r.height===0) return;
    if(t && re.test(t)) out.push({selector:sel(b), text:t.replace(/\s+/g,' ').slice(0,70),
      tag:b.tagName.toLowerCase(), role:(b.getAttribute('role')||'')});
  });
  return out;
})("__NEEDLE__")
"""

FORM_JS = r"""
(() => {
  const NOISE=/ask me anything|search\.\.\.|^search$|type a message|chat/i;
  const SAVE=/save|create|add|submit|confirm|next|finish|crear|guardar|a[nñ]adir|agregar|siguiente|continuar/i;
  function vis(el){const r=el.getBoundingClientRect();return r.width>0&&r.height>0;}
  function label(el){
    let l=el.getAttribute('aria-label'); if(l) return l;
    if(el.id){const lf=document.querySelector('label[for="'+(window.CSS?CSS.escape(el.id):el.id)+'"]'); if(lf) return lf.innerText;}
    const wl=el.closest('label'); if(wl) return wl.innerText;
    const lb=el.getAttribute('aria-labelledby'); if(lb){const e=document.getElementById(lb); if(e) return e.innerText;}
    let g=el.closest('div,fieldset,section');          // field-group: nearest container w/ a label
    for(let i=0;i<4&&g;i++){const c=g.querySelector('label,legend');
      if(c&&!c.contains(el)&&c.innerText.trim()) return c.innerText; g=g.parentElement;}
    let p=el.previousElementSibling;                   // or a short preceding text node
    while(p){const t=(p.innerText||'').trim(); if(t&&t.length<40) return t; p=p.previousElementSibling;}
    return el.getAttribute('placeholder')||el.getAttribute('name')||'';
  }
  function commonAncestor(els){if(!els.length)return document.body;let a=els[0];
    els.slice(1).forEach(e=>{while(a&&!a.contains(e))a=a.parentElement;});return a||document.body;}
  // include custom button-based widgets (Radix-style dropdowns/toggles), not just native inputs
  const FIELD_SEL='input,select,textarea,[role="combobox"],[role="listbox"],[role="switch"],'
    +'[role="checkbox"],[role="radio"],[role="radiogroup"],[aria-haspopup="listbox"],'
    +'[aria-haspopup="menu"],[contenteditable="true"]';
  function typeOf(el){
    const tag=el.tagName.toLowerCase();
    if(tag==='input') return el.getAttribute('type')||'text';
    if(tag==='select') return 'select';
    if(tag==='textarea') return 'textarea';
    const role=(el.getAttribute('role')||'').toLowerCase();
    const hp=(el.getAttribute('aria-haspopup')||'').toLowerCase();
    if(role==='combobox'||role==='listbox'||hp==='listbox') return 'dropdown';
    if(role==='radiogroup') return 'radiogroup';
    if(role==='switch') return 'toggle';
    if(role==='checkbox') return 'checkbox';
    if(role==='radio') return 'radio';
    if(el.isContentEditable) return 'text';
    return 'control';
  }
  const dialog=[...document.querySelectorAll('[role="dialog"],[aria-modal="true"],dialog[open]')].filter(vis).pop();
  const scope=dialog||document.querySelector('form')||document;
  const raw=[...scope.querySelectorAll(FIELD_SEL)].filter(el=>{
    if(!vis(el)) return false;
    const type=typeOf(el);
    if(['hidden','submit','button','image'].includes(type)) return false;
    const lab=(label(el)||'')+' '+(el.getAttribute('placeholder')||'');
    return !NOISE.test(lab);                            // drop chat/search page furniture
  });
  const root=dialog||(scope!==document?scope:commonAncestor(raw));
  const fields=[]; const seen=new Set();
  raw.forEach(el=>{
    const type=typeOf(el);
    let lab=(label(el)||'').replace(/\s+/g,' ').trim();
    const required=el.required||el.getAttribute('aria-required')==='true'||/\*/.test(lab);
    lab=lab.replace(/\s*\*\s*/g,' ').trim().slice(0,80);
    const key=lab+'|'+type; if(seen.has(key)) return; seen.add(key);
    let options=null, value=null;
    if(el.tagName.toLowerCase()==='select') options=[...el.options].map(o=>o.text.trim()).filter(Boolean).slice(0,25);
    if(type==='dropdown'||type==='toggle'||type==='radiogroup') value=(el.innerText||'').replace(/\s+/g,' ').trim().slice(0,40);
    const accept=(el.tagName.toLowerCase()==='input' && (el.getAttribute('type')||'').toLowerCase()==='file') ? (el.getAttribute('accept')||null) : null;
    fields.push({label:lab, type, required:!!required, options, value, accept, placeholder:(el.getAttribute('placeholder')||'').slice(0,60)});
  });
  const h=(root.querySelector&&root.querySelector('h1,h2,h3,[role="heading"]'));
  let submit=[...(root.querySelectorAll?root.querySelectorAll('button,[type="submit"],[role="button"]'):[])]
    .map(b=>(b.innerText||b.value||'').replace(/\s+/g,' ').trim()).filter(Boolean).filter(t=>!NOISE.test(t));
  const saves=submit.filter(t=>SAVE.test(t)); if(saves.length) submit=saves;
  return {title:((h&&h.innerText)||document.title||'').replace(/\s+/g,' ').trim().slice(0,120),
          isDialog: !!dialog,
          fields, submitButtons:[...new Set(submit)].slice(0,6), fieldCount:fields.length};
})()
"""

# Enumerate every visible, actionable control on the current state: links AND
# nav controls (tabs/menu items/sidebar buttons). One DOM read per state — the
# heart of the deferred-expansion search.
CONTROLS_JS = r"""
(() => {
  function cssEsc(s){return (window.CSS&&CSS.escape)?CSS.escape(s):s.replace(/[^a-zA-Z0-9_-]/g,'\\$&');}
  // An id is only a STABLE selector if a framework didn't auto-generate it. Radix
  // (`radix-:r5:`), Headless UI, React-Aria etc. mint ids that change every render,
  // so a selector captured now breaks when a path is replayed later (the cache crawl
  // replays paths across states/time). Such ids contain ':' or a known prefix — skip
  // them and fall back to the structural nth-of-type path. Generic: keys on framework
  // id patterns, never app/section vocabulary.
  function stableId(id){ return !!id && id.indexOf(':')<0 &&
    !/^(radix|headlessui|react-aria|reach-|mui|chakra|rc[-_])/i.test(id); }
  function sel(el){ if(el.id && stableId(el.id)) return '#'+cssEsc(el.id);
    const parts=[]; let e=el;
    while(e&&e.nodeType===1&&e!==document.body){ let p=e.tagName.toLowerCase(); const par=e.parentElement;
      if(par){const s=Array.prototype.filter.call(par.children,c=>c.tagName===e.tagName);
        if(s.length>1)p+=':nth-of-type('+(s.indexOf(e)+1)+')';}
      parts.unshift(p); e=par; if(parts.length>9)break; }
    return parts.join('>'); }
  const navC='[role="tablist"],[role="menu"],[role="menubar"],[role="navigation"],nav,aside,header';
  const bulkC='table,[role="grid"],[role="row"],tbody,thead,tr,td,th,[role="gridcell"],[role="option"]';
  const out=[]; const seen=new Set();
  document.querySelectorAll('a[href],button,[role="button"],[role="tab"],[role="menuitem"],[role="link"],summary').forEach(b=>{
    if(out.length>=150) return;
    if(b.disabled) return;
    if(b.closest(bulkC)) return;          // skip table/grid row controls entirely (speed + noise)
    const r=b.getBoundingClientRect(); if(r.width===0&&r.height===0) return;
    const s=sel(b); if(!s||seen.has(s)) return; seen.add(s);
    const t=(b.innerText||b.getAttribute('aria-label')||'').replace(/\s+/g,' ').trim().slice(0,60);
    if(!t) return;
    const role=(b.getAttribute('role')||'').toLowerCase();
    const href=b.tagName.toLowerCase()==='a'?(b.href||null):null;
    const nav = role==='tab'||role==='menuitem'||role==='link'||
                b.hasAttribute('aria-haspopup')||b.hasAttribute('aria-controls')||!!b.closest(navC);
    out.push({selector:s, text:t, tag:b.tagName.toLowerCase(), role, href, nav, bulk:false});
  });
  return out;
})()
"""

# Controls we must NOT click while *exploring* (they mutate data or end the
# session). The target trigger is matched separately by the goal regex, so a
# "Create X" button is still findable — we just never click it to navigate.
SKIP_NAV = re.compile(
    r"\b(create|add|new|save|delete|remove|destroy|submit|confirm|pay|checkout|buy|"
    r"upload|import|invite|generate|duplicate|publish|deploy|archive|cancel|"
    r"log\s*out|sign\s*out|logout|crear|nuevo|nueva|a[nñ]adir|agregar|guardar|"
    r"eliminar|borrar|enviar|subir)\b", re.I)

# NOTE: a hardcoded EN/ES "section vocabulary" regex used to live here to
# prioritize "section" nav by matching app words. It was REMOVED — it biased toward
# apps that name their sections with those specific words. "Distinct-section"
# priority is now derived STRUCTURALLY in tier() below (an aria tab/menu, or a link
# that is not part of a repeated sibling-list) — no app- or language-specific vocab.

# CONTENT_JS — GENERIC content/data extractor (no app vocabulary). Where CONTROLS_JS
# captures the interactive chrome (buttons/links/tabs), this captures the DATA a view
# renders: tables, grids, trees, lists, feeds, and non-ARIA "repeated-sibling" clusters
# (the project's allowed data-list heuristic). Structural signals only — ARIA roles
# (grid/table/treegrid/tree/list/listbox/feed + row/treeitem/listitem/option/article)
# and the semantic tags table/tr/ul/ol/li. Validated identically on MS Teams (tree +
# messages), Hacker News (<table> rows), and GitHub trending (React article cards).
# Each collection → {kind, count, items:[{t, cells?, level?}]}. Item count capped per
# collection; virtualization (only-visible rows) is handled by scroll-loading in the caller.
CONTENT_JS = r"""
(() => {
  const norm = s => (s||'').replace(/\s+/g,' ').trim();
  const COLL = {grid:1,table:1,treegrid:1,tree:1,list:1,listbox:1,feed:1};
  const ITEM = '[role=row],[role=treeitem],[role=listitem],[role=option],[role=article],tr,li';
  const CLOSE = '[role=grid],[role=table],[role=treegrid],[role=tree],[role=list],[role=listbox],[role=feed],table,ul,ol';
  const roleOf = e => (e.getAttribute('role')||'').toLowerCase() ||
     (e.tagName==='TABLE'?'table':((e.tagName==='UL'||e.tagName==='OL')?'list':''));
  const out=[], containers=new Set();
  document.querySelectorAll('[role],table,ul,ol').forEach(c=>{
    if(out.length>=60) return;
    const role=roleOf(c); if(!COLL[role]) return;
    const items=[...c.querySelectorAll(ITEM)].filter(it=>{
      const a=it.parentElement&&it.parentElement.closest(CLOSE); return a===c; });
    if(items.length<2) return;
    containers.add(c);
    out.push({kind:'aria:'+role, count:items.length, items: items.slice(0,500).map(it=>{
      const cells=[...it.querySelectorAll('[role=gridcell],[role=cell],td')].map(x=>norm(x.innerText)).filter(Boolean);
      const lvl=it.getAttribute('aria-level'); const t=norm(it.getAttribute('aria-label')||it.innerText).slice(0,140);
      const o={t}; if(cells.length)o.cells=cells.slice(0,10); if(lvl)o.level=+lvl; return o; })});
  });
  // repeated-sibling clusters (no ARIA) — generic data lists (cards, rows-of-divs)
  const used=new Set();
  document.querySelectorAll('div,ul,ol,tbody,section,nav,main').forEach(p=>{
    if(out.length>=80) return; if(containers.has(p)) return;
    const kids=[...p.children]; if(kids.length<5) return;
    const byTag={}; kids.forEach(k=>byTag[k.tagName]=(byTag[k.tagName]||0)+1);
    const top=Object.entries(byTag).sort((a,b)=>b[1]-a[1])[0]; if(!top||top[1]<5) return;
    const grp=kids.filter(k=>k.tagName===top[0]&&norm(k.innerText)); if(grp.length<5) return;
    const anc=grp[0].closest(CLOSE); if(anc&&COLL[roleOf(anc)]) return;
    if(used.has(grp[0])) return; grp.forEach(g=>used.add(g));
    out.push({kind:'repeated:'+top[0].toLowerCase(), count:grp.length,
      items:grp.slice(0,500).map(k=>({t:norm(k.innerText).slice(0,140)}))});
  });
  return out;
})()
"""


def pt(args, server, timeout=60):
    cmd = ["pinchtab", "--server", server] + args
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def active_tab(server, url=None):
    # PinchTab 0.10.0 targets the tab in $PINCHTAB_TAB (a STORED default), NOT "the active
    # tab" — so once that stored id goes stale every command 404s ("tab not found"). Resolve
    # a live page tab (prefer one whose URL matches `url`, else the last active page) so the
    # caller can pin $PINCHTAB_TAB to it. Returns None if it can't tell.
    try:
        rc, out, _ = pt(["tab", "--json"], server, timeout=15)
        if rc != 0:
            return None
        tabs = json.loads(out)
        if isinstance(tabs, dict):
            tabs = tabs.get("tabs", [])
        pages = [t for t in tabs if t.get("type") == "page" and t.get("id")]
        if url:
            key = url.split("#")[0].rstrip("/")
            for t in pages:                                   # exact URL match
                if (t.get("url", "").split("#")[0].rstrip("/")) == key:
                    return t["id"]
            for t in pages:                                   # prefix match (SPA route drift)
                if key[:60] and key[:60] in t.get("url", ""):
                    return t["id"]
        act = [t for t in pages if t.get("status") == "active"] or pages
        return act[-1]["id"] if act else None
    except Exception:
        return None


def pin_tab(server, url=None):
    """Point $PINCHTAB_TAB at a live tab so subsequent pt()/eval calls hit it (0.10.0)."""
    tid = active_tab(server, url)
    if tid:
        os.environ["PINCHTAB_TAB"] = tid
    return tid


def pt_json(js, server):
    rc, out, err = pt(["eval", "JSON.stringify(%s)" % js], server)
    if rc != 0:
        raise RuntimeError(err or out)
    try:
        v = json.loads(out)
        return json.loads(v) if isinstance(v, str) else v
    except ValueError:
        raise RuntimeError("bad eval output: %r" % out[:200])


# settle tunables — overridable from the CLI (see main()). Defaults tuned for
# this realtime SPA: with images/media blocked, the DOM stabilizes in <0.5s, so
# a tight poll + short trailing delay beats the old 0.4s/0.3s cadence by ~2x.
RENDER_MS = 3000      # hard cap on the render-stability poll
SETTLE_POLL = 0.1     # interval between control-count reads
SETTLE_DELAY = 0.1    # trailing settle after the count is stable
NETIDLE_MS = 0        # networkidle wait: 0 = skip it (this realtime app never idles,
                      # so it just burns ~0.7s/state; the render-poll handles readiness)


def settle(server, render_ms=None, delay=None):
    render_ms = RENDER_MS if render_ms is None else render_ms
    delay = SETTLE_DELAY if delay is None else delay
    if NETIDLE_MS > 0:    # optional networkidle wait (off by default — see above)
        pt(["wait", "--load", "networkidle", "--timeout", str(NETIDLE_MS)],
           server, timeout=max(3, NETIDLE_MS / 1000.0 + 2))
    deadline = time.time() + render_ms / 1000.0
    last = -1
    while time.time() < deadline:
        # count the SAME control families CONTROLS_JS enumerates — crucially incl.
        # role=tab/menuitem, else tab-heavy pages read 0 controls and the
        # poll stalls to its deadline.
        rc, out, _ = pt(["eval", 'document.querySelectorAll('
                                 '"a[href],button,[role=button],[role=tab],[role=menuitem],summary"'
                                 ').length'], server)
        try:
            c = int(out.strip().strip('"'))
        except ValueError:
            c = 0
        if c > 3 and c == last:
            break
        last = c
        time.sleep(SETTLE_POLL)
    time.sleep(delay)


def nav(url, server):
    rc, out, err = pt(["nav", url], server, timeout=60)
    if rc != 0 and "not found" in (err + out).lower():
        # stale/closed $PINCHTAB_TAB (0.10.0) OR genuinely no tab → open a fresh one
        pt(["nav", url, "--new-tab"], server, timeout=60)
    pin_tab(server, url)                          # pin the now-current tab for later commands
    settle(server)


def shortest_path(graph, start, page):
    """Optional: directed BFS over the graph to describe how to reach `page`."""
    if not graph:
        return None
    nodes = {n["id"]: n for n in graph["nodes"]}

    def norm(u):
        return u.rstrip("/").split("#")[0]

    ids = {norm(k): k for k in nodes}
    s, t = ids.get(norm(start)), ids.get(norm(page))
    if not s or not t:
        return None
    adj = {}
    for e in graph["edges"]:
        adj.setdefault(e["source"], []).append((e["target"], e.get("label", "")))
    prev, via = {s: None}, {}
    q = deque([s])
    while q:
        u = q.popleft()
        if u == t:
            break
        for v, lab in adj.get(u, []):
            if v not in prev:
                prev[v] = u
                via[v] = lab
                q.append(v)
    if t not in prev:
        return None
    steps, cur = [], t
    while prev[cur] is not None:
        steps.append((via[cur], nodes[cur].get("url") or cur))
        cur = prev[cur]
    steps.reverse()
    return steps


def find_trigger_pages(graph, needle_re):
    """Pages whose recorded (skipped) actions match the goal — good candidates."""
    import re
    rx = re.compile(needle_re, re.I)
    nodes = {n["id"]: n for n in graph["nodes"]}
    hits = []
    for e in graph["edges"]:
        if rx.search(e.get("label", "")):
            src = nodes.get(e["source"], {}).get("url") or e["source"]
            hits.append((e["label"], src))
    return hits


def main():
    global RENDER_MS, SETTLE_POLL, SETTLE_DELAY
    ap = argparse.ArgumentParser(description="Generate a how-to for an action in a web app")
    ap.add_argument("--goal", required=True, help='what to do, e.g. "add cae" / "create team"')
    ap.add_argument("--page", help="page URL that has the trigger button (skip auto-locate)")
    ap.add_argument("--start", default=None, help="start URL for the navigation path (needs --graph)")
    ap.add_argument("--graph", help="a crawl <out>.json, to locate the button + show the path")
    ap.add_argument("--match", help="regex for the trigger text (default: verbs + goal nouns)")
    ap.add_argument("--server", default="http://localhost:9871")
    ap.add_argument("--config", default=os.environ.get("PINCHTAB_CONFIG", "crawl-config.json"))
    ap.add_argument("--max-discover", type=int, default=30,
                    help="max states to explore when auto-locating the trigger (default 30)")
    ap.add_argument("--max-depth", type=int, default=6,
                    help="max click-depth of the path to search (default 6)")
    ap.add_argument("--max-links", type=int, default=3,
                    help="max content (non-nav) links to follow per state (default 3)")
    ap.add_argument("--max-per-section", type=int, default=1,
                    help="max pages to explore per site section, e.g. /items/* (default 1)")
    ap.add_argument("--max-nav", type=int, default=0,
                    help="cap on low-value GENERIC nav controls per state (toolbar "
                         "combos/filters); goal-relevant + distinct-section nav are "
                         "never capped, so create-forms (in sections/tabs) stay "
                         "reachable. Default 0 — raise it to explore generic menus too.")
    ap.add_argument("--data-list-min", type=int, default=3,
                    help="N sibling links sharing a section root = a repeated data "
                         "list (e.g. /items/*); skip exploring it entirely (default 3)")
    ap.add_argument("--render-ms", type=int, default=RENDER_MS,
                    help="cap on the per-state render-stability poll (default %d)" % RENDER_MS)
    ap.add_argument("--settle-poll", type=float, default=SETTLE_POLL,
                    help="interval between control-count reads while settling (default %.2f)" % SETTLE_POLL)
    ap.add_argument("--settle-delay", type=float, default=SETTLE_DELAY,
                    help="trailing settle delay after the DOM is stable (default %.2f)" % SETTLE_DELAY)
    ap.add_argument("--screenshot", action="store_true",
                    help="also save a PNG of the opened form (off by default for speed)")
    ap.add_argument("--out", default="recipe")
    a = ap.parse_args()

    # apply settle tunables globally (settle() reads these module-level knobs)
    RENDER_MS, SETTLE_POLL, SETTLE_DELAY = a.render_ms, a.settle_poll, a.settle_delay

    # auth token for the isolated instance
    try:
        os.environ.setdefault("PINCHTAB_TOKEN", json.load(open(a.config))["server"]["token"])
    except Exception:
        pass

    noun_list = goal_nouns(a.goal)
    needle = goal_needle(a.goal, a.match)
    graph = json.load(open(a.graph)) if a.graph else None

    # 1) DISCOVER via interaction-aware BREADTH-FIRST search over the live UI:
    #    explore links AND nav controls (tabs/menus/sidebar), recording the
    #    click-path. BFS => the first match is the SHORTEST (fewest-click) route.
    #    Efficiency: one DOM read per state (deferred expansion — children aren't
    #    clicked until popped), state-dedup, and fast materialization (jump to the
    #    last link target, replay only trailing clicks).
    import re as _re
    needle_re = _re.compile(needle, _re.I)
    goal_re = _re.compile(noun_alt(noun_list) if noun_list else ".", _re.I)

    start_url = a.start or a.page
    if not start_url:
        sys.exit("Pass --start <url> (where to begin) or --page <url>.")

    def score(c):  # rank trigger candidates: real <button> > role=button/input > link
        tag, role = c.get("tag"), c.get("role")
        base = 3 if tag == "button" else (2 if role == "button" or tag == "input" else 1)
        return (base, -len(c.get("text") or ""))

    mat_state = {"path": None}              # path the browser is currently materialized at
    def materialize(path):
        try:
            idx = max((i for i, act in enumerate(path) if act.get("href")), default=-1)
            cur_path = mat_state["path"]
            cidx = (max((i for i, act in enumerate(cur_path) if act.get("href")), default=-1)
                    if cur_path is not None else -2)
            # PREFIX-REUSE: if we're already on the same section page (same last-link
            # href) and this path only appends trailing tab/menu clicks, skip the
            # nav+settle and just (re)click the target tab — tabs are idempotent
            # siblings, so the click lands the right state regardless of the active
            # tab. Saves a full page-load per depth-2 sibling explored back-to-back.
            reuse = (cur_path is not None and idx >= 0 and cidx >= 0
                     and idx < len(path) - 1
                     and path[idx]["href"] == cur_path[cidx]["href"])
            if reuse:
                rest = path[idx + 1:]
            elif idx == -1:                 # no link in path: start fresh, replay clicks
                nav(start_url, a.server)
                rest = path
            else:                           # jump to last link, replay only later clicks
                nav(path[idx]["href"], a.server)
                rest = path[idx + 1:]
            for act in rest:
                if act.get("href"):
                    nav(act["href"], a.server)
                else:
                    # plain click (NO --wait-nav): tab/menu clicks don't navigate, so
                    # --wait-nav would hang ~30s; settle() handles SPA re-render.
                    pt(["click", act["selector"]], a.server, timeout=20)
                    settle(a.server)
            mat_state["path"] = path
            return True
        except Exception as e:
            mat_state["path"] = None        # browser is in an unknown state now
            print("  ! materialize failed (%s)" % str(e)[:80], file=sys.stderr)
            return False

    def state_sig(url, controls):
        labels = sorted({(c.get("text") or "")[:30] for c in controls if not c.get("bulk")})
        return url.split("#")[0] + "||" + "|".join(labels)[:3000]

    def section_key(u):                     # group repetitive instances of one section
        p = urlparse(u)
        seg = [s for s in p.path.split("/") if s]
        base = seg[0] if seg else "root"
        return base + ("?" + p.query if p.query else "")  # keep ?tab=… features distinct

    trigger = page = None
    path_actions = []
    # priority frontier: (depth, goal-relevance, nav-first, seq, path). Popping by
    # depth keeps the result SHORTEST; within a depth, goal-relevant + nav controls
    # go first GLOBALLY (not just per-parent) so we beeline to the target.
    seq = 0
    frontier = [(0, 1, 1, seq, [])]
    seen_states, enq_links, enq_clicks = set(), set(), set()
    section_counts = {}
    budget = a.max_discover
    while frontier and budget > 0 and trigger is None:
        _depth, _rel, _nav, _s, path = heapq.heappop(frontier)
        if not materialize(path):
            continue
        budget -= 1
        # one DOM round-trip per state: read URL + controls together
        st = pt_json("({href:location.href, controls:%s})" % CONTROLS_JS, a.server)
        cur = (st.get("href") or "").strip().strip('"')
        controls = st.get("controls") or []
        sig = state_sig(cur, controls)
        if sig in seen_states:
            continue
        seen_states.add(sig)
        print("· [%d/%d] depth %d · %s (%d controls)"
              % (a.max_discover - budget, a.max_discover, len(path), cur, len(controls)),
              file=sys.stderr)
        cands = [c for c in controls
                 if c.get("text") and needle_re.search(c["text"]) and not c.get("bulk")]
        if cands:                           # trigger is here — shortest path found
            cands.sort(key=score, reverse=True)
            trigger, page, path_actions = cands[0], cur, path
            break
        if len(path) >= a.max_depth:
            continue
        # Identify the repeated DATA LIST on this state: many sibling links that
        # share a URL path-segment root (e.g. ~15 /items/<id> rows). These are data
        # instances, not features — visiting even one costs a heavy DOM read and
        # never hosts a create-form we want. Skip the whole list. (Genuine distinct
        # sections have few siblings and survive.)
        sib = {}
        for c in controls:
            h = c.get("href")
            if h and h.startswith("http") and same_host(h, start_url):
                sib[section_key(h.split("#")[0])] = sib.get(section_key(h.split("#")[0]), 0) + 1
        datalist = {k for k, n in sib.items() if n >= a.data_list_min}

        kids = []                           # enqueue navigational controls only
        for c in controls:
            txt = c.get("text") or ""
            if c.get("bulk") or SKIP_NAV.search(txt):
                continue                    # never navigate via write/destructive controls
            href = c.get("href")
            isnav = bool(c.get("nav"))
            if href:
                u = href.split("#")[0]
                if not (u.startswith("http") and same_host(u, start_url)) or u in enq_links:
                    continue
                if u.rstrip("/") == cur.split("#")[0].rstrip("/"):
                    continue                # self-link to the current page — no new state
                sk = section_key(u)
                if sk in datalist:          # skip the repeated data/project list
                    continue
                if section_counts.get(sk, 0) >= a.max_per_section:
                    continue                # cap repetitive instances of one section
                section_counts[sk] = section_counts.get(sk, 0) + 1
                enq_links.add(u)
                kids.append({"selector": c["selector"], "label": txt or u, "href": u,
                             "nav": isnav, "role": (c.get("role") or "")})
            elif isnav:
                # Global tab/menu dedup BY LABEL: a same-labelled tab/menu item
                # usually leads to the same place no matter which parent section you
                # reached it from (e.g. two sidebar entries into one SPA share a tab
                # bar), so enqueue it once. Avoids re-materializing the identical
                # tab-state through a second parent (the dominant duplicate cost).
                key = txt.strip().lower() or ("@" + c["selector"])
                if key in enq_clicks:
                    continue
                enq_clicks.add(key)
                kids.append({"selector": c["selector"], "label": txt, "href": None,
                             "nav": True, "role": (c.get("role") or "")})
        # Rank kids into relevance tiers, then PRUNE the long tail. Strict BFS pops
        # every shallower state before any deeper one, so state count is governed by
        # fan-out — not ordering. Tiers are fully STRUCTURAL (no app/word vocabulary):
        #   tier 0 = the user's goal noun appears in the label (generic, --goal-driven)
        #   tier 1 = EXPLICIT navigation: a link to a distinct section (repeated data
        #            lists are already skipped above), or an aria tab/menuitem
        #   tier 2 = a generic nav control (button/combobox/haspopup toggle with no
        #            href and no nav role) — toolbar/view widgets, rarely a route to a
        #            create-form, so capped by --max-nav (raise it to explore them).
        NAV_ROLES = ("tab", "menuitem", "menuitemradio", "menuitemcheckbox")
        def tier(k):
            if goal_re.search(k["label"] or ""):
                return 0
            if k.get("href") or k.get("role") in NAV_ROLES:
                return 1
            return 2
        navk = [k for k in kids if k["nav"]]
        content = [k for k in kids if not k["nav"]]
        navk.sort(key=lambda k: tier(k))
        keep_nav, tail = [], 0
        for k in navk:
            if tier(k) <= 1:
                keep_nav.append(k)
            elif tail < a.max_nav:
                keep_nav.append(k)
                tail += 1
        def navrank(k):                     # within a (depth,tier): order by structure
            if k.get("role") in NAV_ROLES:
                return 0                    # tab/menu click — stays in this section (forms live here)
            if k["nav"] and k.get("href"):
                return 1                    # link — jumps to a different section
            if k["nav"]:
                return 2                    # generic nav control (toolbar/menu toggle)
            return 3                        # content link
        for k in keep_nav + content[:a.max_links]:
            seq += 1
            heapq.heappush(frontier, (len(path) + 1, tier(k), navrank(k), seq, path + [k]))
    if trigger is None:
        sys.exit("Could not find a control matching %r within %d explored states from %s"
                 % (needle, a.max_discover, start_url))
    print("· FOUND %r after %d step(s) on %s" % (trigger["text"], len(path_actions), page),
          file=sys.stderr)
    # we are already materialized at the trigger's state (we broke right after finding it)

    # 3) open the form (DO NOT submit). The trigger may open a modal OR navigate
    #    to a form page — handle both.
    before = pt(["eval", "location.href"], a.server)[1].strip().strip('"')
    pt(["click", trigger["selector"]], a.server, timeout=30)  # 409 = navigation, fine
    settle(a.server)
    after = pt(["eval", "location.href"], a.server)[1].strip().strip('"')
    navigated = after.rstrip("/") != before.rstrip("/")
    form = pt_json(FORM_JS, a.server)
    if a.screenshot:                        # optional — off by default (saves a round-trip)
        pt(["screenshot", "-o", os.path.abspath("%s.png" % a.out)], a.server)

    # 4) cancel without saving (Escape closes a modal; on a form page it's a no-op)
    pt(["press", "Escape"], a.server)
    form["opensAt"] = after if navigated else None

    # 5) emit how-to — the recorded click-path is exactly what a chatbot narrates
    steps = ["Go to %s" % start_url]
    steps += ["Click “%s”" % act["label"] for act in path_actions]
    steps.append("Click the “%s” button" % trigger["text"])
    rec = {"goal": a.goal, "start": start_url, "triggerPage": page, "trigger": trigger["text"],
           "shortestClicks": len(path_actions) + 1, "steps": steps,
           "opensAt": form.get("opensAt"), "form": form,
           # structured path (additive) so the cache write-back can stitch states+edges
           # from a live result — one entry per click; href is null for tab/menu clicks.
           "pathStructured": [{"label": act["label"], "selector": act["selector"],
                               "href": act.get("href")} for act in path_actions],
           "triggerSelector": trigger["selector"]}
    json.dump(rec, open(os.path.abspath("%s.json" % a.out), "w"), indent=2)

    print("\n=== HOW TO: %s ===\n" % a.goal.upper())
    print("Shortest route — %d click%s:" % (len(steps) - 1, "" if len(steps) - 1 == 1 else "s"))
    for i, s in enumerate(steps, 1):
        print("  %d. %s" % (i, s))
    if form.get("opensAt"):
        print("     → opens %s" % form["opensAt"])
    print("\nThis opens%s: “%s”" % (" a dialog" if form.get("isDialog") else " a form", form["title"]))
    print("Fill in %d field(s):" % form["fieldCount"])
    for f in form["fields"]:
        req = "  (required)" if f["required"] else ""
        opt = ("  options: " + ", ".join(f["options"])) if f.get("options") else ""
        val = ("  default: " + f["value"]) if f.get("value") else ""
        acc = ("  accepts: " + f["accept"]) if f.get("accept") else ""
        ph = ("  e.g. " + f["placeholder"]) if f["placeholder"] and not opt and not val else ""
        print("  • %-30s [%s]%s%s%s%s%s" % (f["label"] or "(unlabeled)", f["type"], acc, req, ph, val, opt))
    if form.get("submitButtons"):
        print("\nThen click to confirm: %s" % "  /  ".join("“%s”" % b for b in form["submitButtons"]))
    if a.screenshot:
        print("\nScreenshot: %s.png" % a.out, end="")
    print("\nspec: %s.json" % a.out)


if __name__ == "__main__":
    main()
