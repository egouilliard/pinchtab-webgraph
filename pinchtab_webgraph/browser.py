#!/usr/bin/env python3
"""
The BROWSER PORT — the one seam between a flow and a live page.

`runner.py` executes a flow against this interface and nothing else. Two consequences that
are the whole point of the split:

  - **Testable.** `FakeBrowser` implements the same six methods, so the entire step VM —
    loops, pagination, dedupe, capability gating — is exercised in unit tests with no
    browser, no bridge, no network.
  - **Hostable.** A worker in a multi-tenant deployment leases a `PinchTabBrowser` bound to
    one tenant's bridge and hands it to the runner. The runner has no idea; it never reaches
    for a global, a cwd, or an env var.

It also owns the two GENERIC live-DOM primitives the VM needs and the rest of the toolkit
did not yet have (see generality.md — structural signals only, no app vocabulary):

  QUERY_JS     find every control matching a spec, CLASSIFIED (download / link / button).
               Unlike recipe.CONTROLS_JS this deliberately DOES descend into tables and
               grids — a per-row "Download" button is exactly the thing a bulk flow wants,
               and CONTROLS_JS skips row controls for crawl speed.
  PAGINATE_JS  find the "next page" control and say whether it is exhausted. Structural
               first (rel=next, aria-label, aria-disabled), UI-verb regex second — the same
               class of heuristic as crawl.DOWNLOAD_ACTIONS, which is already accepted as
               generic.
"""
import base64
import json
import os
import subprocess

DEFAULT_SERVER = "http://localhost:9871"

# Cap on an in-session fetch. The bytes cross the CDP boundary base64-encoded inside a JSON
# string, so a huge file is a memory hazard on BOTH sides — refuse rather than wedge.
MAX_FETCH_BYTES = 10 * 1024 * 1024

# Shared JS helpers: the stable-selector builder is lifted verbatim from recipe.CONTROLS_JS
# so a selector minted here is replayable by the same rules the crawler recorded under
# (framework-generated ids — Radix `:r5:` etc. — change every render and must be skipped).
_SEL_JS = r"""
  function cssEsc(s){return (window.CSS&&CSS.escape)?CSS.escape(s):s.replace(/[^a-zA-Z0-9_-]/g,'\\$&');}
  function stableId(id){ return !!id && id.indexOf(':')<0 &&
    !/^(radix|headlessui|react-aria|reach-|mui|chakra|rc[-_])/i.test(id); }
  function sel(el){ if(el.id && stableId(el.id)) return '#'+cssEsc(el.id);
    const parts=[]; let e=el;
    while(e&&e.nodeType===1&&e!==document.body){ let p=e.tagName.toLowerCase(); const par=e.parentElement;
      if(par){const s=Array.prototype.filter.call(par.children,c=>c.tagName===e.tagName);
        if(s.length>1)p+=':nth-of-type('+(s.indexOf(e)+1)+')';}
      parts.unshift(p); e=par; if(parts.length>9)break; }
    return parts.join('>'); }
  const norm = s => (s||'').replace(/\s+/g,' ').trim();
"""

# Download classification — mirrors crawl.py EXTRACT_JS (`dlDirect` + the DL_VERB regex) so
# a control the crawler recorded as a `download` node is the same control this finds live.
_DL_JS = r"""
  const DL_VERB=/\b(download|export|télécharger|telecharger|descargar|exportar)\b|下载|導出|导出/i;
  const DL_EXT=/\.(pdf|csv|tsv|xlsx?|docx?|pptx?|zip|gz|tar|rar|7z|json|xml|txt|rtf|ods|odt|odp|png|jpe?g|svg|ics|vcf|epub|mp3|mp4|wav|mov|bin|dmg|exe|apk|parquet|sql)(\?|#|$)/i;
  function dlDirect(a){ if(!a||!a.getAttribute) return false;
    if(a.hasAttribute('download')) return true;
    const h=a.getAttribute('href')||'';
    if(/^(blob:|data:)/i.test(h)) return true;
    try { return DL_EXT.test(new URL(a.href, location.href).pathname); } catch(e){ return DL_EXT.test(h); } }
"""

# QUERY_JS — the for_each primitive. Takes a spec {kind, label, selector, limit} and returns
# the matching controls with a stable selector each. `kind` is structural: 'download' uses
# the same classifier as the crawler; 'link'/'button' key off tag/role; omitted = anything
# actionable. `label` is a case-insensitive regex over the control's visible text.
QUERY_JS = r"""
(spec => {
  %(sel)s
  %(dl)s
  const want = (spec.kind||'').toLowerCase();
  const labelRe = spec.label ? new RegExp(spec.label, 'i') : null;
  const root = spec.selector ? document.querySelector(spec.selector) : document;
  if (!root) return [];
  const limit = spec.limit || 200;
  const out=[], seen=new Set();
  const CANDIDATES='a[href],button,[role=button],[role=link],[role=menuitem],summary,input[type=submit],input[type=button]';
  root.querySelectorAll(CANDIDATES).forEach(b=>{
    if(out.length>=limit) return;
    if(b.disabled || b.getAttribute('aria-disabled')==='true') return;
    const r=b.getBoundingClientRect(); if(r.width===0 && r.height===0) return;
    const text=norm(b.innerText || b.getAttribute('aria-label') || b.value || '').slice(0,120);
    const anchor = b.tagName==='A' ? b : (b.closest('a[href]') || b.querySelector('a[href]'));
    const direct = dlDirect(anchor);
    const isDl = direct || DL_VERB.test(text);
    const kind = isDl ? 'download' : (anchor || b.tagName==='A' ? 'link' : 'button');
    if(want && kind!==want) return;
    if(labelRe && !labelRe.test(text)) return;
    const s=sel(b); if(!s || seen.has(s)) return; seen.add(s);
    out.push({selector:s, text:text, kind:kind,
              href: direct && anchor ? anchor.href : null,
              dlKind: isDl ? (direct?'direct':'js') : null});
  });
  return out;
})(JSON.parse(%(spec)s))
"""

# PAGINATE_JS — the paginate primitive. Finds the control that advances to the next page and
# reports whether it is EXHAUSTED (disabled / aria-disabled / absent), which is what ends the
# loop. Structural signals are tried before the text regex, so a properly-marked-up paginator
# is found without any vocabulary at all.
_NEXT_JS = r"""
(() => {
  %(sel)s
  const NEXT_VERB=/^(next|next page|suivant|siguiente|weiter|próxima|proxima|›|»|→|>|>>)$/i;
  const NEXT_LABEL=/\b(next|suivant|siguiente|weiter|próxim|proxim)\b/i;
  function disabled(e){ return !!(e.disabled || e.getAttribute('aria-disabled')==='true' ||
    e.classList.contains('disabled') || (e.closest('li,[role=none]')||{classList:{contains:()=>false}}).classList.contains('disabled')); }
  // 1. structural: an explicit rel=next is unambiguous and needs no vocabulary.
  let el = document.querySelector('a[rel~=next],link[rel~=next],[rel=next]');
  // 2. structural: an aria-label naming the relationship.
  if(!el){ el = [...document.querySelectorAll('a,button,[role=button],[role=link]')]
    .find(e => NEXT_LABEL.test(e.getAttribute('aria-label')||'')); }
  // 3. visible text, scoped to a pagination-ish container first, then anywhere.
  if(!el){
    const scopes=[...document.querySelectorAll('[role=navigation],nav,[class*=pagin],[class*=Pagin]')];
    const pools=scopes.length?scopes:[document];
    for(const p of pools){
      el=[...p.querySelectorAll('a,button,[role=button],[role=link]')]
        .find(e => NEXT_VERB.test(norm(e.innerText)));
      if(el) break;
    }
  }
  if(!el) return {found:false, exhausted:true, selector:null};
  const r=el.getBoundingClientRect();
  const off = (r.width===0 && r.height===0);
  return {found:true, exhausted: disabled(el) || off, selector: sel(el),
          text: norm(el.innerText||el.getAttribute('aria-label')||'').slice(0,60)};
})()
"""


def query_js(spec):
    """The QUERY_JS expression for a match spec. The spec crosses into JS as a JSON string
    (never string-concatenated into the source) so a label regex can't break out."""
    return QUERY_JS % {"sel": _SEL_JS, "dl": _DL_JS, "spec": json.dumps(json.dumps(spec))}


def next_page_js():
    return _NEXT_JS % {"sel": _SEL_JS}


# A page's identity, for the pagination no-progress guard: if clicking "next" leaves the
# content unchanged, the paginator is a decoy and the loop must stop rather than spin.
PAGE_SIG_JS = r"""
(() => {
  const norm = s => (s||'').replace(/\s+/g,' ').trim();
  const items=[...document.querySelectorAll('[role=row],[role=listitem],[role=treeitem],[role=option],tr,li')]
    .slice(0,40).map(e=>norm(e.innerText).slice(0,80));
  return location.href + '|' + items.length + '|' + items.join('§').slice(0,2000);
})()
"""


# FETCH_JS — download a file IN THE PAGE'S OWN CONTEXT.
#
# `pinchtab download` unconditionally refuses loopback/internal hosts (its SSRF guard) and
# otherwise requires the host to be in `security.downloadAllowedDomains`, so it cannot serve
# a flow running against a local app. Fetching from inside the page sidesteps that, and is
# strictly better anyway: the request inherits the page's SESSION COOKIES (the authenticated
# case) and hands us the real bytes, which is what content-hash dedupe needs.
#
# The body is chunked into 32K slices because String.fromCharCode.apply blows the JS stack on
# a large array. SAME-ORIGIN ONLY — a cross-origin href throws `TypeError: Failed to fetch`,
# which is why the runner keeps the CLI download as a fallback.
_FETCH_JS = r"""
fetch(%(url)s,{credentials:'include'})
  .then(r=>{if(!r.ok)throw new Error('HTTP '+r.status);return r.arrayBuffer()})
  .then(b=>{const u=new Uint8Array(b);let s='';const C=0x8000;
    for(let i=0;i<u.length;i+=C)s+=String.fromCharCode.apply(null,u.subarray(i,i+C));
    return btoa(s);})
"""


def fetch_js(url):
    """The FETCH_JS expression for a url. The url crosses into JS as a JSON literal (never
    string-concatenated) so it cannot break out of the expression."""
    return _FETCH_JS % {"url": json.dumps(url)}


class BrowserError(RuntimeError):
    """A browser command failed. `step_fatal` marks the failures that must abort a flow
    (a nav/click that didn't land means every later step is aimed at the wrong page)."""

    def __init__(self, message, *, step_fatal=False):
        super().__init__(message)
        self.step_fatal = step_fatal


class PinchTabBrowser:
    """The live implementation: drives a PinchTab bridge through its CLI.

    Reuses perform.py's proven invocation contract — the bridge token and the target tab go
    in via the ENV (`PINCHTAB_TOKEN` / `PINCHTAB_TAB`), never as flags, because commands like
    `download` have no `--tab` flag and error on it but harmlessly ignore the env var."""

    def __init__(self, server=DEFAULT_SERVER, token=None, tab=None, timeout=90, _run=None):
        self.server = server
        self.token = token
        self.tab = tab
        self.timeout = timeout
        self._run = _run or self._subprocess_run

    def _subprocess_run(self, argv, timeout):
        cmd = ["pinchtab"]
        if self.server:
            cmd += ["--server", self.server]
        cmd += list(argv)
        env = dict(os.environ)
        if self.token:
            env["PINCHTAB_TOKEN"] = self.token
        if self.tab:
            env["PINCHTAB_TAB"] = self.tab
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
            return r.returncode, r.stdout.strip(), r.stderr.strip()
        except FileNotFoundError:
            return 127, "", "the `pinchtab` CLI is not on PATH"
        except subprocess.TimeoutExpired:
            return 124, "", "timeout"

    def run(self, argv, timeout=None):
        rc, out, err = self._run(list(argv), timeout or self.timeout)
        if rc != 0:
            raise BrowserError(err or out or ("pinchtab %s failed (rc=%d)" % (argv[0], rc)),
                               step_fatal=argv[0] in ("nav", "click"))
        return out

    # --- the port ---------------------------------------------------------------

    def nav(self, url):
        self.run(["nav", url])

    def click(self, selector):
        """Click, and TOLERATE a navigation.

        `--wait-nav` is not an optimisation, it is required for correctness: PinchTab's
        action guard treats a click that changes the document as an *unexpected* navigation
        and fails it with `409 unexpected page navigation` — AFTER the page has already
        moved. Without the flag every link-based paginator ("Next" is an `<a href>`) and
        every path click a `goto` walks would abort the flow having in fact succeeded. With
        it, a click that navigates waits for the new document, and a click that does not
        (opening a modal) returns immediately."""
        self.run(["click", "--css", selector, "--wait-nav"])

    def fill(self, selector, value):
        self.run(["fill", selector, str(value)])

    def select(self, selector, value):
        self.run(["select", selector, str(value)])

    def check(self, selector):
        self.run(["check", selector])

    def upload(self, selector, path):
        self.run(["upload", path, "-s", selector])

    def download(self, href, out_path):
        self.run(["download", href, "-o", out_path], timeout=max(self.timeout, 180))
        return out_path

    def evaluate(self, js, await_promise=False, timeout=None):
        """Evaluate JS and return the parsed result.

        We ALWAYS wrap the expression in `JSON.stringify(...)`, so stdout is always valid
        JSON for every type (object/array/number/bool/null/string/"") — which means exactly
        ONE decode is right. (`pinchtab eval` prints a string result UNQUOTED, so a second
        `json.loads` would blow up on every string — including `location.href`, which is what
        the pagination no-progress guard reads.)

        `await_promise` is NOT optional for an async expression: without the flag the bridge
        returns `{}` with rc=0 — a silent wrong answer. The stringify wrap then has to go
        INSIDE the promise chain, because you cannot JSON.stringify a Promise."""
        if await_promise:
            argv = ["eval", "(%s).then(v=>JSON.stringify(v))" % js, "--await-promise"]
        else:
            argv = ["eval", "JSON.stringify(%s)" % js]
        out = (self.run(argv, timeout=timeout) or "").strip()
        if out == "":
            return None
        try:
            return json.loads(out)          # single decode — never double-decode
        except ValueError:
            raise BrowserError("bad eval output: %r" % out[:200])

    def fetch_bytes(self, url, timeout=180):
        """Fetch a URL's bytes in the PAGE's context (inherits session cookies) and return
        them. SAME-ORIGIN ONLY — the caller must already be navigated to the site; a
        cross-origin href raises (TypeError: Failed to fetch) and the caller should fall back
        to the CLI download. A 404 surfaces as `Error: HTTP 404` → BrowserError, as intended."""
        b64 = self.evaluate(fetch_js(url), await_promise=True,
                            timeout=max(self.timeout, timeout))
        if not isinstance(b64, str):
            raise BrowserError("fetch of %s returned %s, not base64 bytes"
                               % (url, type(b64).__name__))
        try:
            raw = base64.b64decode(b64)
        except (ValueError, TypeError):
            raise BrowserError("fetch of %s returned undecodable base64" % url)
        if len(raw) > MAX_FETCH_BYTES:
            raise BrowserError("fetch of %s is %d bytes — over the %d-byte limit"
                               % (url, len(raw), MAX_FETCH_BYTES))
        return raw

    def save_bytes(self, url, out_path, timeout=180):
        """`fetch_bytes` straight to disk. Returns out_path (so it reads like `download`)."""
        raw = self.fetch_bytes(url, timeout=timeout)
        parent = os.path.dirname(out_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(out_path, "wb") as fh:
            fh.write(raw)
        return out_path

    # --- derived helpers (the VM's vocabulary) ----------------------------------

    def query(self, spec):
        """Controls on the current page matching a spec → [{selector, text, kind, href}]."""
        return self.evaluate(query_js(spec)) or []

    def next_page(self):
        """{found, exhausted, selector, text} for the current page's paginator."""
        return self.evaluate(next_page_js()) or {"found": False, "exhausted": True}

    def page_signature(self):
        return self.evaluate(PAGE_SIG_JS) or ""

    def content(self):
        """The current view's data collections (reuses the crawler's generic extractor)."""
        from . import recipe
        return self.evaluate(recipe.CONTENT_JS) or []

    def url(self):
        return self.evaluate("location.href") or ""


def resolve_tab(server, token, _run=None):
    """Pin a live page tab. PinchTab stores a DEFAULT tab id which goes stale, after which
    every command 404s with 'tab not found' — so a run resolves one up front."""
    probe = PinchTabBrowser(server, token, None, timeout=15, _run=_run)
    try:
        tabs = json.loads(probe.run(["tab", "--json"], timeout=15) or "[]")
        if isinstance(tabs, dict):
            tabs = tabs.get("tabs", [])
        pages = [t for t in tabs if t.get("type") == "page" and t.get("id")]
        if pages:
            active = [t for t in pages if t.get("status") == "active"] or pages
            return active[-1]["id"]
    except (BrowserError, ValueError, TypeError):
        pass
    try:
        out = probe.run(["nav", "about:blank", "--new-tab", "--print-tab-id"])
        return out.strip().splitlines()[-1].strip() or None
    except BrowserError:
        return None
