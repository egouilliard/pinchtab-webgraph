#!/usr/bin/env python3
"""
PinchTab web-navigation graph crawler.

Crawls a website through PinchTab (real, JS-rendered browser), discovering
pages via <a href> links AND interactive widgets (buttons, tabs, menus,
accordions, SPA route changes) by actually clicking them. Builds a navigation
graph of nodes (pages + SPA states) and edges (links + actions) and writes:

  - <out>.json   : the graph data ({nodes, edges, meta})
  - <out>.html   : a self-contained Cytoscape.js viewer (double-click to open)

Design / safety:
  * Same-origin only by default (won't wander off the target site).
  * Destructive-looking actions (logout/delete/pay/submit/...) are SKIPPED by
    default and recorded as skipped edges. Use --allow-destructive to include.
  * Hard caps on pages, actions-per-state, and interaction depth prevent the
    classic SPA "state explosion".
  * Interaction exploration replays the action path from a fresh page load so
    each probe starts from a known state (refs aren't reused across reloads;
    stable CSS selectors are generated instead).

Requires a WORKING, ISOLATED PinchTab instance. Point at it with --server.
NEVER run this against a browser holding a live authenticated session you
care about (e.g. your monday.com tab) — it clicks things.

Usage:
  python3 pinchtab_webgraph/crawl.py https://example.com
  python3 pinchtab_webgraph/crawl.py https://app.example.com --server http://localhost:9871 \
      --max-pages 80 --interaction-depth 2 --out out/mygraph
"""
import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from urllib.parse import urlparse, urljoin, urldefrag, parse_qsl, urlencode, urlunparse

# Text on a control that means "clicking this might change/destroy state".
# Skipped by default; recorded as a skipped edge so you can see what was avoided.
DESTRUCTIVE = re.compile(
    r"\b(log\s*out|sign\s*out|logout|signout|delete|remove|destroy|trash|"
    r"pay|checkout|buy|purchase|order|subscribe|unsubscribe|deactivate|"
    r"disable|reset|wipe|clear|withdraw|transfer|send|submit|confirm|"
    r"approve|reject|publish|deploy|merge|archive|cancel|terminate)\b",
    re.I,
)

# Write/mutation verbs: controls that likely CREATE or MODIFY data rather than
# navigate. Skipped when --skip-writes is set (recommended on real apps, so the
# crawl maps navigation without spamming staging with junk records).
WRITE_ACTIONS = re.compile(
    r"\b(create|add|new|save|edit|update|upload|import|invite|generate|"
    r"duplicate|copy|rename|move|assign|enable|activate|send|share|export|"
    r"download|run|start|launch|apply)\b|^\+$|^＋$",
    re.I,
)

# Schemes / hrefs that are never pages to crawl.
SKIP_HREF = re.compile(r"^(mailto:|tel:|javascript:|data:|blob:|#|sms:|ftp:)", re.I)

# Common tracking params stripped during URL normalization.
TRACKING = re.compile(r"^(utm_|gclid$|fbclid$|mc_|ref$|ref_src$|_ga$)", re.I)


# --- JavaScript injected to read a page's interactive surface ----------------
# Returns {url, title, links:[{href,text}],
#          actions:[{selector,text,tag,nav,bulk,upload,accept}]}.
# `selector` is a stable CSS path so we can re-find the element after a reload.
# `upload` (bool) flags a file-upload affordance — a bare `input[type="file"]`, a
# file input hidden behind a styled <label>/button, or an inline-`ondrop` dropzone;
# `accept` carries that control's accepted file types (e.g. ".pdf,.docx", "image/*")
# or null. Upload affordances become a read-only "upload" graph node and are NEVER
# clicked (clicking a file input opens a native OS dialog the crawler can't dismiss).
EXTRACT_JS = r"""
(() => {
  function cssEsc(s){ return (window.CSS && CSS.escape) ? CSS.escape(s) : s.replace(/[^a-zA-Z0-9_-]/g,'\\$&'); }
  function sel(el){
    if (el.id) return '#' + cssEsc(el.id);
    const parts = [];
    let e = el;
    while (e && e.nodeType === 1 && e !== document.body && e !== document.documentElement) {
      let part = e.tagName.toLowerCase();
      const par = e.parentElement;
      if (par) {
        const sibs = Array.prototype.filter.call(par.children, c => c.tagName === e.tagName);
        if (sibs.length > 1) part += ':nth-of-type(' + (sibs.indexOf(e) + 1) + ')';
      }
      parts.unshift(part);
      e = par;
      if (parts.length > 8) break;
    }
    return parts.join('>');
  }
  function txt(el){
    let t = (el.innerText || el.value || el.getAttribute('aria-label') ||
             el.getAttribute('title') || el.getAttribute('alt') || '').trim();
    return t.replace(/\s+/g, ' ').slice(0, 90);
  }
  const out = { url: location.href, title: (document.title || '').slice(0,200), links: [], actions: [] };
  const seenL = new Set();
  document.querySelectorAll('a[href]').forEach(a => {
    const h = a.href;
    if (!h || seenL.has(h)) return;
    seenL.add(h);
    out.links.push({ href: h, text: txt(a) });
  });
  const seenA = new Set();
  const sels = 'button,[role="button"],[role="tab"],[role="menuitem"],summary,' +
               'input[type="submit"],input[type="button"],input[type="file"],' +
               '[onclick],[ondrop]';
  const navContainer = '[role="tablist"],[role="menu"],[role="menubar"],' +
                       '[role="navigation"],nav,aside,header';
  const bulkContainer = 'table,[role="grid"],[role="table"],[role="row"],' +
                        '[role="rowgroup"],tbody,thead,tr,td,th,[role="gridcell"],' +
                        '[role="cell"],[role="columnheader"],[role="listbox"],[role="option"]';
  document.querySelectorAll(sels).forEach(b => {
    // skip disabled / invisible
    if (b.disabled) return;
    const r = b.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return;
    const s = sel(b);
    if (!s || seenA.has(s)) return;
    seenA.add(s);
    const role = (b.getAttribute('role') || '').toLowerCase();
    const isNav = role === 'tab' || role === 'menuitem' ||
                  b.hasAttribute('aria-haspopup') || b.hasAttribute('aria-controls') ||
                  b.getAttribute('aria-expanded') !== null ||
                  !!b.closest(navContainer);
    const bulk = !!b.closest(bulkContainer);
    const _tag = b.tagName.toLowerCase();
    const _isFile = _tag === 'input' && (b.getAttribute('type')||'').toLowerCase() === 'file';
    const _nested = b.querySelector ? b.querySelector('input[type="file"]') : null;
    const _upload = _isFile || b.hasAttribute('ondrop') || !!_nested;
    const _accept = _isFile ? (b.getAttribute('accept') || null)
                  : (_nested ? (_nested.getAttribute('accept') || null) : null);
    out.actions.push({ selector: s, text: txt(b) || (_upload ? 'Upload file' : ''),
                       tag: _tag, nav: isNav, bulk: bulk,
                       upload: _upload, accept: _accept });
  });
  // file inputs are commonly hidden behind a styled <label>/button/dropzone the
  // scan above can't see — walk each up to its nearest clickable affordance so the
  // upload is still recorded, with its accepted file types. Never clicked (crawler
  // skips upload affordances), so this stays read-only.
  // NOTE (limitation): a dropzone whose drop handler is bound via addEventListener
  // (not an inline `ondrop` attribute) and that contains no file input can't be
  // detected from the DOM — the nested-file-input heuristic covers the common case.
  document.querySelectorAll('input[type="file"]').forEach(inp => {
    const acc = inp.getAttribute('accept') || null;
    // If the input itself was already captured (it was visible in the scan above),
    // upgrade THAT entry in place — don't also emit a coarser ancestor action for it.
    const sInp = sel(inp);
    if (sInp && seenA.has(sInp)) {
      const ex0 = out.actions.find(a => a.selector === sInp);
      if (ex0) { ex0.upload = true; if (!ex0.accept) ex0.accept = acc; }
      return;
    }
    // Otherwise the input is hidden — walk to its nearest clickable affordance (a
    // styled <label>/button/dropzone). `form` is excluded: a bare <form> is never a
    // meaningful upload click target.
    let aff = inp.closest('label,button,[role="button"],[ondrop]') || inp;
    const rr = aff.getBoundingClientRect();
    const target = (rr.width > 0 || rr.height > 0) ? aff : inp;
    const s2 = sel(target);
    if (!s2) return;
    if (seenA.has(s2)) {
      const ex = out.actions.find(a => a.selector === s2);
      if (ex) { ex.upload = true; if (!ex.accept) ex.accept = acc; }
      return;
    }
    seenA.add(s2);
    out.actions.push({ selector: s2, text: txt(target) || 'Upload file',
                       tag: target.tagName.toLowerCase(), nav: false,
                       bulk: !!(target.closest && target.closest(bulkContainer)),
                       upload: true, accept: acc });
  });
  return out;
})()
"""


def run_pt(args, server, timeout=90):
    """Run a pinchtab CLI command, return (rc, stdout, stderr)."""
    cmd = ["pinchtab"]
    if server:
        cmd += ["--server", server]
    cmd += args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


def pt_eval_json(js, server):
    """Evaluate JS that returns a value; JSON.stringify it and parse robustly."""
    wrapped = "JSON.stringify(%s)" % js.strip()
    rc, out, err = run_pt(["eval", wrapped], server)
    if rc != 0:
        raise RuntimeError("eval failed: %s" % (err or out))
    out = out.strip()
    # pinchtab may print the JSON directly, or wrap a returned string in quotes.
    for attempt in (out,):
        try:
            v = json.loads(attempt)
            # If it parsed to a string, it was double-encoded -> parse again.
            return json.loads(v) if isinstance(v, str) else v
        except (ValueError, TypeError):
            pass
    raise RuntimeError("could not parse eval output: %r" % out[:200])


def normalize_url(url, strip_tracking=True):
    """Canonicalize a URL: drop fragment, sort/strip query, tidy trailing slash."""
    url, _frag = urldefrag(url)
    p = urlparse(url)
    q = parse_qsl(p.query, keep_blank_values=True)
    if strip_tracking:
        q = [(k, v) for k, v in q if not TRACKING.match(k)]
    q.sort()
    path = p.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return urlunparse((p.scheme, p.netloc.lower(), path, "", urlencode(q), ""))


def host_key(url, include_subdomains):
    h = (urlparse(url).hostname or "").lower()
    if h.startswith("www."):
        h = h[4:]
    if include_subdomains:
        parts = h.split(".")
        h = ".".join(parts[-2:]) if len(parts) >= 2 else h
    return h


def state_sig(info):
    """A short hash of a page's interactive structure (for SPA-state dedup)."""
    basis = normalize_url(info["url"]) + "|"
    basis += "|".join(sorted(l["href"] for l in info["links"]))
    basis += "||"
    basis += "|".join(sorted(a["selector"] + ">" + a["text"] for a in info["actions"]))
    return hashlib.sha1(basis.encode("utf-8", "replace")).hexdigest()[:10]


class Crawler:
    def __init__(self, args):
        self.a = args
        self.server = args.server
        self.start = normalize_url(args.url, args.strip_tracking)
        self.base_host = host_key(self.start, args.include_subdomains)
        self.nodes = {}          # id -> node dict
        self.edges = []          # edge dicts
        self.edge_keys = set()   # dedup edges
        self.explored = set()    # state signatures fully explored
        self.page_seen = set()   # normalized page URLs queued/visited
        self.action_budget = args.max_actions
        self.auth_bounces = 0
        self.t0 = time.time()

    # --- low-level browser ops ---------------------------------------------
    def nav(self, url):
        # First nav opens a dedicated tab; a stale tab id in the CLI state file
        # (e.g. left by another instance) shows up as "tab ... not found" — in
        # that case retry forcing a fresh tab.
        extra = ["--new-tab"] if not getattr(self, "_tab_ready", False) else []
        rc, out, err = run_pt(["nav", url] + extra, self.server, timeout=self.a.nav_timeout)
        if rc != 0 and "not found" in (err + out).lower():
            rc, out, err = run_pt(["nav", url, "--new-tab"], self.server,
                                  timeout=self.a.nav_timeout)
        if rc != 0:
            raise RuntimeError("nav %s failed: %s" % (url, err or out))
        self._tab_ready = True
        self.settle()

    def click(self, selector):
        rc, out, err = run_pt(["click", selector, "--wait-nav"], self.server,
                              timeout=self.a.nav_timeout)
        # 409 (unexpected navigation) is treated as success per PinchTab docs.
        self.settle()
        return rc == 0 or "409" in (err + out)

    def settle(self):
        # SPAs (e.g. React apps) render after the initial response, and a loading
        # spinner can briefly satisfy a simple "has buttons" check before the real
        # content paints. So wait for the network to settle, then poll until the
        # interactive-element count STABILISES (two equal readings) before reading.
        run_pt(["wait", "--load", "networkidle"], self.server, timeout=12)
        deadline = time.time() + self.a.render_timeout / 1000.0
        last, stable = -1, 0
        while time.time() < deadline:
            rc, out, _ = run_pt(
                ["eval", 'document.querySelectorAll("a[href]").length+'
                         'document.querySelectorAll("button,[role=button]").length'],
                self.server, timeout=15)
            try:
                c = int(out.strip().strip('"'))
            except ValueError:
                c = 0
            if c > 3 and c == last:
                break  # rendered and steady
            last, stable = c, 0
            time.sleep(0.4)
        if self.a.delay:
            time.sleep(self.a.delay)

    def extract(self):
        return pt_eval_json(EXTRACT_JS, self.server)

    def materialize(self, entry_url, action_path):
        """Reach a state: load entry_url then replay each action selector."""
        self.nav(entry_url)
        for act in action_path:
            ok = self.click(act["selector"])
            if not ok:
                return False
        return True

    def cur_url(self):
        rc, out, err = run_pt(["eval", "location.href"], self.server, timeout=20)
        return out.strip().strip('"') if rc == 0 else ""

    def reset_to(self, entry_url, action_path, base_url, base_sig):
        """Cheaply return to the base state between probes: `back` if we navigated
        away, else `Escape` to dismiss a popover/modal. Full reload only if those
        don't restore the base interactive signature. Avoids reloading heavy pages
        for every probe (the slow path on data-grid views)."""
        cur = self.cur_url()
        if cur and normalize_url(cur) != normalize_url(base_url):
            self.nav(base_url)  # direct nav is safer than `back` (no /auth overshoot)
        else:
            run_pt(["press", "Escape"], self.server, timeout=15)
            if self.a.delay:
                time.sleep(self.a.delay)
        try:
            if state_sig(self.extract()) == base_sig:
                return True
        except Exception:
            pass
        try:  # fallback: full re-materialize
            if self.materialize(entry_url, action_path):
                return state_sig(self.extract()) == base_sig
        except Exception:
            pass
        return False

    # --- graph helpers ------------------------------------------------------
    def add_node(self, nid, **kw):
        if nid not in self.nodes:
            self.nodes[nid] = {"id": nid, **kw}
        else:
            self.nodes[nid].update({k: v for k, v in kw.items() if v})
        return nid

    def add_edge(self, src, dst, label, kind, skipped=False):
        key = (src, dst, label, kind)
        if key in self.edge_keys:
            return
        self.edge_keys.add(key)
        self.edges.append({"source": src, "target": dst, "label": label,
                           "kind": kind, "skipped": skipped})

    def page_id(self, url):
        return normalize_url(url, self.a.strip_tracking)

    # --- crawl --------------------------------------------------------------
    def same_site(self, url):
        return host_key(url, self.a.include_subdomains) == self.base_host

    def is_crawlable_link(self, href):
        if not href or SKIP_HREF.match(href):
            return False
        if not href.lower().startswith(("http://", "https://")):
            return False
        return self.same_site(href)

    def _on_auth(self, info, url):
        """True if we landed on the logged-out path when we didn't request it."""
        return bool(self.a.auth_path) and self.a.auth_path in (info.get("url") or "") \
            and self.a.auth_path not in url

    def run(self):
        queue = [self.start]
        self.page_seen.add(self.start)
        while queue:
            if len(self.nodes) >= self.a.max_pages:
                self.log("reached --max-pages cap (%d)" % self.a.max_pages)
                break
            url = queue.pop(0)
            self.log("PAGE %d/%d  %s" % (len(self.nodes) + 1, self.a.max_pages, url))
            try:
                self.nav(url)
                info = self.extract()
            except Exception as e:
                self.log("  ! skip (%s)" % e)
                self.add_node(self.page_id(url), url=url, title="(error)",
                              type="page", error=str(e)[:200])
                continue
            # If logged out and a relogin command is configured, recover once.
            if self._on_auth(info, url) and self.a.relogin_cmd:
                self.log("  redirected to auth — running relogin command")
                try:
                    subprocess.run(shlex.split(self.a.relogin_cmd), capture_output=True,
                                   timeout=180)
                    self.nav(url)
                    info = self.extract()
                except Exception as e:
                    self.log("  ! relogin failed (%s)" % e)
            # Auth-loss guard: if pages keep redirecting to the logged-out path,
            # the session likely died — stop before polluting the graph.
            if self._on_auth(info, url):
                self.auth_bounces += 1
                self.log("  ! redirected to auth (%d) — possible session loss"
                         % self.auth_bounces)
                if self.auth_bounces >= 3:
                    self.log("STOP: repeated auth redirects — session lost. "
                             "Re-login (or pass --relogin-cmd) and re-run.")
                    break
                continue
            else:
                self.auth_bounces = 0
            sid = self.page_id(url)
            self.add_node(sid, url=info["url"], title=info["title"] or url, type="page")
            self.explore_state(sid, url, [], info, depth=0, queue=queue)
        return self.finish()

    def explore_state(self, node_id, entry_url, action_path, info, depth, queue):
        sig = state_sig(info)
        if sig in self.explored:
            return
        self.explored.add(sig)

        # 1) Link edges (page -> page). Cheap and safe.
        for link in info["links"]:
            href = link["href"]
            if not self.is_crawlable_link(href):
                continue
            tgt = self.page_id(href)
            self.add_node(tgt, url=href, title=link["text"] or href, type="page")
            self.add_edge(node_id, tgt, link["text"] or "(link)", "link")
            if tgt not in self.page_seen and len(self.page_seen) < self.a.max_pages:
                self.page_seen.add(tgt)
                queue.append(href)

        # 2) Interaction edges (click controls). Bounded & guarded.
        if depth >= self.a.interaction_depth:
            return
        candidates = info["actions"]
        if self.a.nav_only:  # only real nav controls; drop bulk table/grid buttons
            candidates = [a for a in candidates if a.get("nav") and not a.get("bulk")]
        actions = candidates[: self.a.max_actions_per_state]
        if len(candidates) > len(actions):
            self.log("  (capped actions: %d -> %d)" % (len(candidates), len(actions)))
        base_url = info["url"]
        base_sig = sig
        probed = 0
        for act in actions:
            if self.action_budget <= 0:
                self.log("  reached global --max-actions budget")
                return
            label = act["text"] or "(%s)" % act["tag"]
            txt = act["text"] or ""
            if act.get("upload"):
                accept = act.get("accept")
                up_id = node_id + "##upload:" + hashlib.sha1(
                    (act["selector"] + label).encode()).hexdigest()[:6]
                title = "⬆ " + label + ((" [" + accept + "]") if accept else "")
                self.add_node(up_id, url="", title=title, type="upload",
                              reason="upload", accept=accept or "")
                self.add_edge(node_id, up_id, label, "action", skipped=True)
                continue
            is_destructive = DESTRUCTIVE.search(txt) and not self.a.allow_destructive
            is_write = self.a.skip_writes and WRITE_ACTIONS.search(txt)
            if is_destructive or is_write:
                # record what we deliberately did not click
                reason = "destructive" if is_destructive else "write"
                skip_id = node_id + "##skipped:" + hashlib.sha1(
                    (act["selector"] + label).encode()).hexdigest()[:6]
                self.add_node(skip_id, url="", title="⚠ " + label,
                              type="skipped", reason=reason)
                self.add_edge(node_id, skip_id, label, "action", skipped=True)
                continue

            # We're already at base on the first probe; reset cheaply after that.
            if probed > 0 and not self.reset_to(entry_url, action_path, base_url, base_sig):
                self.log("  ! could not restore base state; stopping probes here")
                break
            probed += 1
            self.action_budget -= 1
            try:
                self.click(act["selector"])
                after = self.extract()
            except Exception as e:
                self.log("  ! action '%s' failed (%s)" % (label[:40], e))
                continue

            after_url = after["url"]
            if self.same_site(after_url) and \
               normalize_url(after_url) != normalize_url(base_url) and \
               not SKIP_HREF.match(after_url):
                # The click navigated to a different page.
                tgt = self.page_id(after_url)
                self.add_node(tgt, url=after_url, title=after["title"] or after_url,
                              type="page")
                self.add_edge(node_id, tgt, label, "action")
                if tgt not in self.page_seen and len(self.page_seen) < self.a.max_pages:
                    self.page_seen.add(tgt)
                    queue.append(after_url)
                continue

            after_sig = state_sig(after)
            if after_sig == base_sig:
                continue  # no-op click, ignore
            # A same-URL DOM change => an SPA / modal state node.
            state_node = self.page_id(entry_url) + "#state:" + after_sig
            self.add_node(state_node, url=after_url,
                          title=(after["title"] or "state") + " ▸ " + label[:40],
                          type="state")
            self.add_edge(node_id, state_node, label, "action")
            # Recurse into the new state (bounded by interaction-depth).
            self.explore_state(state_node, entry_url, action_path + [act],
                               after, depth + 1, queue)

    # --- output -------------------------------------------------------------
    def log(self, msg):
        if not self.a.quiet:
            print("[%5.1fs] %s" % (time.time() - self.t0, msg), file=sys.stderr)

    def finish(self):
        meta = {
            "start": self.start,
            "host": self.base_host,
            "pages": sum(1 for n in self.nodes.values() if n["type"] == "page"),
            "states": sum(1 for n in self.nodes.values() if n["type"] == "state"),
            "skipped": sum(1 for n in self.nodes.values() if n["type"] == "skipped"),
            "uploads": sum(1 for n in self.nodes.values() if n["type"] == "upload"),
            "edges": len(self.edges),
            "interaction_depth": self.a.interaction_depth,
            "allow_destructive": self.a.allow_destructive,
            "elapsed_sec": round(time.time() - self.t0, 1),
        }
        graph = {"nodes": list(self.nodes.values()), "edges": self.edges, "meta": meta}
        json_path = self.a.out + ".json"
        with open(json_path, "w") as f:
            json.dump(graph, f, indent=2)
        html_path = self.a.out + ".html"
        with open(html_path, "w") as f:
            f.write(render_html(graph))
        self.log("DONE  %d nodes (%d pages, %d states, %d skipped), %d edges"
                 % (len(self.nodes), meta["pages"], meta["states"],
                    meta["skipped"], meta["edges"]))
        print("\nWrote:\n  %s\n  %s  <- open this in a browser" % (json_path, html_path))
        return graph


_VENDOR_DIR = os.path.join(os.path.dirname(__file__), "vendor")
_VENDOR_FILES = ("cytoscape.min.js", "dagre.min.js", "cytoscape-dagre.min.js",
                 "layout-base.min.js", "cose-base.min.js", "cytoscape-fcose.min.js")


def _vendor_js():
    """Inline the six minified libs as self-contained <script> tags (no CDN).

    Each file's contents are escaped so a literal </script> (or any </) inside
    the minified JS can't break out of the tag; load order is significant."""
    out = []
    for name in _VENDOR_FILES:
        with open(os.path.join(_VENDOR_DIR, name), encoding="utf-8") as f:
            js = f.read().replace("</", "<\\/")
        out.append("<script>" + js + "</script>")
    return "\n".join(out)


def render_html(graph):
    # Escape </ so a crawled title containing </script> can't break the tag.
    data = json.dumps(graph).replace("</", "<\\/")
    return (HTML_TEMPLATE
            .replace("<!--__VENDOR_JS__-->", _vendor_js())
            .replace("/*__GRAPH__*/null", data))


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Navigation Graph</title>
<!--__VENDOR_JS__-->
<style>
:root{--bg:#eef2f7;--panel:#fff;--ink:#0f172a;--muted:#64748b;--border:#e2e8f0;--accent:#2563eb;}
*{box-sizing:border-box}
html,body{margin:0;height:100%;font:13px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;color:var(--ink);background:var(--bg)}
#app{display:flex;height:100%}
#cy{position:relative;flex:1;height:100%;background:radial-gradient(circle at 32% 18%,#ffffff 0,rgba(255,255,255,0) 55%),var(--bg)}
#cyload{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);display:none;z-index:5;padding:7px 14px;border:1px solid var(--border);background:var(--panel);color:var(--muted);border-radius:20px;font-size:12px;box-shadow:0 4px 14px rgba(15,23,42,.08)}
#cyload.on{display:block}
#side{width:344px;min-width:344px;height:100%;background:var(--panel);border-left:1px solid var(--border);display:flex;flex-direction:column;box-shadow:-6px 0 22px rgba(15,23,42,.05)}
.sec{padding:13px 16px;border-bottom:1px solid var(--border)}
h1{font-size:15px;margin:0 0 3px;display:flex;align-items:center;gap:8px}
.sub{color:var(--muted);font-size:11px;word-break:break-all}
.stats{display:grid;grid-template-columns:1fr 1fr;gap:6px 10px;margin-top:11px}
.stat{display:flex;justify-content:space-between;background:var(--bg);padding:6px 9px;border-radius:7px}
.stat span{color:var(--muted)}.stat b{font-variant-numeric:tabular-nums}
input[type=text]{width:100%;padding:8px 11px;border:1px solid var(--border);border-radius:8px;font:inherit;background:var(--bg)}
input[type=text]:focus{outline:none;border-color:var(--accent);background:#fff}
.btns{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px;align-items:center}
.seclabel{font-size:10.5px;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);margin-bottom:7px}
.chk{display:flex;align-items:center;gap:5px;font-size:11px;color:var(--muted);cursor:pointer}
button{font:inherit;font-size:12px;padding:6px 11px;border:1px solid var(--border);background:#fff;color:var(--ink);border-radius:7px;cursor:pointer;transition:.12s}
button:hover{border-color:var(--accent);color:var(--accent)}
button.on{background:var(--accent);color:#fff;border-color:var(--accent)}
.legend{display:flex;flex-direction:column;gap:6px}
.lg{display:flex;align-items:center;gap:9px;font-size:12px;cursor:pointer}
.lg.off{opacity:.32}
.sw{width:13px;height:13px;border-radius:4px;flex:none}
.detail{flex:1;overflow:auto;padding:14px 16px}
.detail .title{font-size:14px;font-weight:600;word-break:break-word}
.badge{display:inline-block;font-size:10px;padding:2px 8px;border-radius:20px;background:var(--bg);color:var(--muted);margin:5px 5px 0 0}
.url{display:block;margin:9px 0;font-size:11px;color:var(--accent);word-break:break-all;text-decoration:none}
.url:hover{text-decoration:underline}
.adj h4{margin:15px 0 6px;font-size:10.5px;text-transform:uppercase;letter-spacing:.5px;color:var(--muted)}
.adj a{display:block;padding:5px 8px;border-radius:6px;color:var(--ink);text-decoration:none;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;border-left:3px solid transparent;cursor:pointer}
.adj a:hover{background:var(--bg);border-left-color:var(--accent)}
.adj a .a-kind{float:right;font-size:9px;color:var(--muted);text-transform:uppercase}
.hint{color:var(--muted);font-size:12px}
.foot{padding:10px 16px;border-top:1px solid var(--border);color:var(--muted);font-size:11px}
</style>
</head>
<body>
<div id="app">
  <div id="cy"><div id="cyload">Laying out graph…</div></div>
  <aside id="side">
    <div class="sec">
      <h1>&#129517; Navigation Graph</h1>
      <div class="sub" id="host"></div>
      <div class="stats" id="stats"></div>
    </div>
    <div class="sec">
      <input id="q" type="text" placeholder="Search pages by URL or title…">
      <div class="btns">
        <button onclick="relayout('fcose')" id="b-fcose" class="on">Clusters</button>
        <button onclick="relayout('dagre')" id="b-dagre">Tree</button>
        <button onclick="relayout('fcose-proof')" id="b-fcose-proof">High quality</button>
        <button onclick="toggleGlobal()" id="b-global">Show global nav</button>
        <button onclick="cy.fit(null,40)">Fit</button>
        <button onclick="resetView()">Reset</button>
      </div>
    </div>
    <div class="sec">
      <div class="seclabel">Find path between pages</div>
      <input id="pa" list="nodelist" type="text" placeholder="From… (type a URL/title)">
      <input id="pb" list="nodelist" type="text" placeholder="To… (type a URL/title)" style="margin-top:6px">
      <datalist id="nodelist"></datalist>
      <div class="btns">
        <button onclick="findPath()" id="b-path">Shortest path</button>
        <label class="chk"><input type="checkbox" id="pg" checked> via global nav</label>
      </div>
    </div>
    <div class="sec"><div class="legend" id="legend"></div></div>
    <div class="detail" id="detail"><div class="hint">Click a node to see <b>what it links to</b> and <b>what links to it</b>. Hover to preview its connections. Click a legend swatch to hide/show a section.</div></div>
    <div class="foot" id="foot"></div>
  </aside>
</div>
<script>
const GRAPH = /*__GRAPH__*/null;
const PALETTE=['#2563eb','#0d9488','#d97706','#7c3aed','#db2777','#0891b2','#dc2626','#65a30d','#9333ea','#0369a1','#b45309','#0f766e'];
function pathSegs(u){try{return new URL(u).pathname.split('/').filter(Boolean);}catch(e){return [];}}
function sectionOf(u){const s=pathSegs(u);return s[0]||'home';}
function clusterOf(u){const s=pathSegs(u);if(!s.length)return 'home';
  if((s[0]==='caes'||s[0]==='cross-product')&&s[1])return s[0]+'/'+s[1];return s[0];}
function shortLabel(n){if(n.url){const s=pathSegs(n.url);return s.length?('/'+s.slice(-2).join('/')):'/';}
  return (n.title||'').replace(/^⚠ /,'').slice(0,24);}
const sections=[...new Set(GRAPH.nodes.filter(n=>n.url).map(n=>sectionOf(n.url)))].sort();
const sectionColor={};sections.forEach((s,i)=>sectionColor[s]=PALETTE[i%PALETTE.length]);
function nodeColor(n){if(n.type==='state')return '#9333ea';if(n.type==='skipped')return '#f59e0b';
  if(n.type==='upload')return '#0891b2';
  return sectionColor[sectionOf(n.url||'')]||'#64748b';}
const deg={};GRAPH.nodes.forEach(n=>deg[n.id]=0);
GRAPH.edges.forEach(e=>{deg[e.source]=(deg[e.source]||0)+1;deg[e.target]=(deg[e.target]||0)+1;});
// "global nav" edges: links INTO a target that most pages link to (sidebar/topbar).
// These are ~all the edges and collapse the layout into a blob, so hide by default.
const inDeg={};GRAPH.nodes.forEach(n=>inDeg[n.id]=0);
GRAPH.edges.forEach(e=>inDeg[e.target]=(inDeg[e.target]||0)+1);
const HUB=Math.max(8,(GRAPH.meta.pages||GRAPH.nodes.length)*0.4);
const els=[];const parents={};
GRAPH.nodes.forEach(n=>{const cl=n.url?clusterOf(n.url):(n.type==='state'?'states':(n.type==='upload'?'uploads':'skipped'));
  if(!parents['grp:'+cl])parents['grp:'+cl]={id:'grp:'+cl,label:cl,section:n.url?sectionOf(n.url):cl};});
Object.values(parents).forEach(p=>els.push({data:{id:p.id,label:p.label,isGroup:1,gcolor:sectionColor[p.section]||'#94a3b8'}}));
GRAPH.nodes.forEach(n=>{const cl=n.url?clusterOf(n.url):(n.type==='state'?'states':(n.type==='upload'?'uploads':'skipped'));
  els.push({data:{id:n.id,parent:'grp:'+cl,label:(n.title||n.url||n.id),short:shortLabel(n),
    url:n.url,type:n.type,color:nodeColor(n),deg:deg[n.id]||0,size:16+Math.min(34,Math.sqrt(deg[n.id]||1)*5)}});});
GRAPH.edges.forEach((e,i)=>els.push({data:{id:'e'+i,source:e.source,target:e.target,label:e.label,kind:e.kind,skipped:!!e.skipped,glob:((inDeg[e.target]||0)>=HUB)?1:0}}));
const cy=cytoscape({container:document.getElementById('cy'),elements:els,wheelSensitivity:.2,
  style:[
    {selector:'node[isGroup]',style:{'label':'data(label)','text-valign':'top','text-halign':'center','font-size':11,'font-weight':600,'color':'#475569','background-color':'data(gcolor)','background-opacity':0.06,'border-width':1.5,'border-color':'data(gcolor)','border-opacity':0.45,'shape':'round-rectangle','padding':14,'text-margin-y':-2}},
    {selector:'node[!isGroup]',style:{'label':'data(short)','color':'#334155','font-size':10,'text-wrap':'ellipsis','text-max-width':110,'text-margin-y':3,'min-zoomed-font-size':10,'background-color':'data(color)','width':'data(size)','height':'data(size)','border-width':1.5,'border-color':'#fff','text-valign':'bottom'}},
    {selector:'node.show-label',style:{'min-zoomed-font-size':0,'font-size':11,'color':'#0f172a','z-index':40,'text-background-color':'#fff','text-background-opacity':0.85,'text-background-padding':2}},
    {selector:'node[type="state"]',style:{'shape':'round-diamond'}},
    {selector:'node[type="skipped"]',style:{'shape':'triangle'}},
    {selector:'node[type="upload"]',style:{'shape':'tag'}},
    {selector:'edge',style:{'width':1,'line-color':'#94a3b8','line-opacity':0.12,'curve-style':'bezier','target-arrow-shape':'triangle','target-arrow-color':'#cbd5e1','arrow-scale':0.6}},
    {selector:'edge[kind="action"]',style:{'line-style':'dashed'}},
    {selector:'.dim',style:{'opacity':0.06}},
    {selector:'node.hl',style:{'border-width':3,'border-color':'#0f172a','z-index':30}},
    {selector:'edge.hl',style:{'line-opacity':0.92,'width':2.2,'line-color':'#2563eb','target-arrow-color':'#2563eb','z-index':25}},
    {selector:'edge.hl[kind="action"]',style:{'line-color':'#ea580c','target-arrow-color':'#ea580c'}},
    {selector:'.hidden',style:{'display':'none'}},
    {selector:'edge.ghide',style:{'display':'none'}},
    {selector:'node.path',style:{'border-width':3.5,'border-color':'#16a34a','z-index':50}},
    {selector:'edge.path',style:{'display':'element','line-opacity':1,'width':3.5,'line-color':'#16a34a','target-arrow-color':'#16a34a','line-style':'solid','z-index':50}},
  ]});
const LAYOUTS={
  fcose:{name:'fcose',quality:'default',animate:false,randomize:false,nodeDimensionsIncludeLabels:false,nodeRepulsion:14000,idealEdgeLength:75,nestingFactor:0.2,gravity:0.12,gravityCompound:1.4,gravityRangeCompound:2,packComponents:true,nodeSeparation:130,tile:true,componentSpacing:140},
  'fcose-proof':{name:'fcose',quality:'proof',animate:false,randomize:true,nodeDimensionsIncludeLabels:false,nodeRepulsion:14000,idealEdgeLength:75,nestingFactor:0.2,gravity:0.12,gravityCompound:1.4,gravityRangeCompound:2,packComponents:true,nodeSeparation:130,tile:true,componentSpacing:140},
  dagre:{name:'dagre',rankDir:'LR',nodeSep:16,rankSep:90,animate:false},
};
let hideGlobal=true;
function showLoad(){const e=document.getElementById('cyload');if(e)e.classList.add('on');}
function hideLoad(){const e=document.getElementById('cyload');if(e)e.classList.remove('on');}
function curLayout(){if(document.querySelector('#b-dagre.on'))return 'dagre';
  if(document.querySelector('#b-fcose-proof.on'))return 'fcose-proof';return 'fcose';}
function relayout(name){document.querySelectorAll('#b-fcose,#b-dagre,#b-fcose-proof').forEach(b=>b.classList.remove('on'));
  const b=document.getElementById('b-'+name);if(b)b.classList.add('on');
  // Yield a frame so the overlay paints before the synchronous layout blocks.
  showLoad();setTimeout(()=>{const lay=cy.elements(':visible').layout(LAYOUTS[name]||LAYOUTS.fcose);
    lay.one('layoutstop',hideLoad);
    try{lay.run();}catch(e){hideLoad();cy.elements(':visible').layout(LAYOUTS.dagre).run();}},16);}
function applyGlobal(){cy.edges().forEach(e=>e.toggleClass('ghide',hideGlobal&&!!e.data('glob')));}
function toggleGlobal(){hideGlobal=!hideGlobal;
  const b=document.getElementById('b-global');if(b){b.classList.toggle('on',!hideGlobal);
    b.textContent=hideGlobal?'Show global nav':'Hide global nav';}
  applyGlobal();relayout(curLayout());}
applyGlobal();
relayout('fcose');
const m=GRAPH.meta;
document.getElementById('host').textContent=m.host+'  ·  '+(m.start||'').replace(/^https?:\/\//,'');
function st(k,v){return '<div class="stat"><span>'+k+'</span><b>'+v+'</b></div>';}
document.getElementById('stats').innerHTML=st('Pages',m.pages)+st('States',m.states)+st('Skipped',m.skipped)+(m.uploads?st('Uploads',m.uploads):'')+st('Edges',m.edges)+st('Depth',m.interaction_depth)+st('Elapsed',m.elapsed_sec+'s');
document.getElementById('foot').textContent=GRAPH.nodes.length+' nodes · '+GRAPH.edges.length+' edges · drag to pan · scroll to zoom';
const legendEl=document.getElementById('legend');const hiddenKeys=new Set();
const legendItems=sections.map(s=>({key:s,color:sectionColor[s],label:s}));
legendItems.push({key:'__state',color:'#9333ea',label:'SPA / modal state'});
legendItems.push({key:'__skip',color:'#f59e0b',label:'skipped (write / destructive)'});
legendItems.push({key:'__upload',color:'#0891b2',label:'file upload'});
legendItems.forEach(it=>{const d=document.createElement('div');d.className='lg';
  d.innerHTML='<span class="sw" style="background:'+it.color+'"></span>'+it.label;
  d.onclick=()=>{if(hiddenKeys.has(it.key))hiddenKeys.delete(it.key);else hiddenKeys.add(it.key);
    d.classList.toggle('off');applyFilter();};legendEl.appendChild(d);});
function nodeMatchesLegend(n){const t=n.data('type');
  if(t==='state')return !hiddenKeys.has('__state');
  if(t==='skipped')return !hiddenKeys.has('__skip');
  if(t==='upload')return !hiddenKeys.has('__upload');
  return !hiddenKeys.has(sectionOf(n.data('url')||''));}
function applyFilter(){const q=document.getElementById('q').value.toLowerCase().trim();
  cy.batch(()=>{cy.nodes('[!isGroup]').forEach(n=>{const d=n.data();
    const okText=!q||(d.url||'').toLowerCase().includes(q)||(d.label||'').toLowerCase().includes(q);
    n.toggleClass('hidden',!(okText&&nodeMatchesLegend(n)));});});}
document.getElementById('q').addEventListener('input',applyFilter);
function focus(node){
  applyGlobal();                              // re-hide global edges from any prior focus
  cy.elements().removeClass('hl show-label').addClass('dim');
  const ce=node.connectedEdges();
  ce.removeClass('ghide');                     // reveal THIS node's links, even global ones
  node.closedNeighborhood().removeClass('dim');
  ce.removeClass('dim').addClass('hl');
  node.neighborhood('node[!isGroup]').removeClass('dim').addClass('show-label');
  node.addClass('hl show-label');node.parents().removeClass('dim');
  showDetail(node);}
function resetView(){cy.elements().removeClass('dim hl show-label path');applyGlobal();
  document.getElementById('q').value='';applyFilter();
  document.getElementById('detail').innerHTML='<div class="hint">Click a node to see <b>what it links to</b> and <b>what links to it</b>.</div>';cy.fit(null,40);}
// ---- path finding ----
const ADJ={};cy.edges().forEach(e=>{const d=e.data();(ADJ[d.source]=ADJ[d.source]||[]).push({to:d.target,eid:d.id,glob:!!d.glob});});
const dl=document.getElementById('nodelist');
cy.nodes('[!isGroup]').forEach(n=>{const o=document.createElement('option');o.value=n.data('url')||n.data('label');dl.appendChild(o);});
function resolveInput(v){v=(v||'').trim().toLowerCase();if(!v)return null;
  let exact=null;cy.nodes('[!isGroup]').forEach(n=>{if(exact)return;
    const u=(n.data('url')||'').toLowerCase(),l=(n.data('label')||'').toLowerCase();if(u===v||l===v)exact=n;});
  if(exact)return exact;
  const hits=cy.nodes('[!isGroup]').filter(n=>((n.data('url')||'')+' '+(n.data('label')||'')).toLowerCase().includes(v));
  return hits.length?hits[0]:null;}
function pathBFS(src,dst,useGlobal){if(src===dst)return{nodes:[src],edges:[]};
  const prev={},viaE={};prev[src]=null;const q=[src];let head=0;
  while(head<q.length){const u=q[head++];if(u===dst)break;
    (ADJ[u]||[]).forEach(x=>{if(!useGlobal&&x.glob)return;if(!(x.to in prev)){prev[x.to]=u;viaE[x.to]=x.eid;q.push(x.to);}});}
  if(!(dst in prev))return null;const nodes=[],edges=[];let c=dst;
  while(c!==null){nodes.unshift(c);if(prev[c]!==null)edges.unshift(viaE[c]);c=prev[c];}return{nodes,edges};}
function findPath(){const A=resolveInput(document.getElementById('pa').value),B=resolveInput(document.getElementById('pb').value);
  if(!A||!B){document.getElementById('detail').innerHTML='<div class="hint">Type a valid <b>From</b> and <b>To</b> page (pick from the suggestions).</div>';return;}
  const useG=document.getElementById('pg').checked;const r=pathBFS(A.id(),B.id(),useG);
  cy.elements().removeClass('dim hl show-label path');applyGlobal();
  if(!r){document.getElementById('detail').innerHTML='<div class="title">No path found</div><div class="hint">No directed click-route from “'+esc(A.data('label'))+'” to “'+esc(B.data('label'))+'”'+(useG?'.':' without global nav — try ticking “via global nav”.')+'</div>';return;}
  cy.elements().addClass('dim');const sel=cy.collection();
  r.edges.forEach(id=>{const e=cy.getElementById(id);e.removeClass('ghide dim').addClass('path');sel.merge(e);});
  r.nodes.forEach(id=>{const n=cy.getElementById(id);n.removeClass('dim').addClass('path show-label');n.parents().removeClass('dim');sel.merge(n);});
  let h='<div class="title">Shortest path · '+r.edges.length+' click'+(r.edges.length===1?'':'s')+'</div>';
  h+='<span class="badge">'+(useG?'via global nav':'structural only')+'</span><div class="adj" style="margin-top:8px">';
  r.nodes.forEach((id,i)=>{const n=cy.getElementById(id);
    h+='<a class="adj-link" data-id="'+esc(id)+'">'+(i>0?'<span class="a-kind">'+esc(edgeLabel(r.edges[i-1]))+'</span>':'')+(i+1)+'. '+esc(n.data('label')||id)+'</a>';});
  h+='</div>';document.getElementById('detail').innerHTML=h;
  cy.animate({fit:{eles:sel,padding:60}},{duration:300});}
function edgeLabel(eid){const e=cy.getElementById(eid);return e?(e.data('label')||e.data('kind')||''):'';}
cy.on('tap','node[!isGroup]',e=>focus(e.target));
cy.on('mouseover','node[!isGroup]',e=>{const n=e.target;n.addClass('show-label');
  n.neighborhood('node[!isGroup]').addClass('show-label');
  if(cy.$('node.hl').length===0)n.connectedEdges(':visible').addClass('hl');});
cy.on('mouseout','node[!isGroup]',e=>{const n=e.target;
  if(cy.$('node.hl').length===0){n.connectedEdges().removeClass('hl');
    cy.nodes('.show-label').not(cy.nodes('.hl')).removeClass('show-label');}});
cy.on('tap',e=>{if(e.target===cy)resetView();});
function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function adjList(edges,dir){if(!edges.length)return '<div class="hint">none</div>';
  const seen=new Set();let h='';
  edges.forEach(e=>{const other=dir==='target'?e.target():e.source();const od=other.data();
    if(seen.has(od.id))return;seen.add(od.id);
    h+='<a class="adj-link" data-id="'+esc(od.id)+'" title="'+esc(od.url||od.label)+'"><span class="a-kind">'+e.data('kind')+'</span>'+esc(od.label||od.id)+'</a>';});
  return h;}
function showDetail(node){const d=node.data();const out=node.outgoers('edge');const inc=node.incomers('edge');
  let html='<div class="title">'+esc(d.label)+'</div>';
  html+='<span class="badge">'+d.type+'</span><span class="badge">'+d.deg+' connections</span>';
  if(d.url)html+='<a class="url" href="'+esc(d.url)+'" target="_blank">'+esc(d.url)+'</a>';
  html+='<div class="adj"><h4>&#8594; Links to ('+out.length+')</h4>'+adjList(out,'target');
  html+='<h4>&#8592; Linked from ('+inc.length+')</h4>'+adjList(inc,'source')+'</div>';
  document.getElementById('detail').innerHTML=html;}
document.getElementById('detail').addEventListener('click',ev=>{const a=ev.target.closest('.adj-link');
  if(!a)return;ev.preventDefault();const n=cy.getElementById(a.dataset.id);
  if(n&&n.length){focus(n);cy.animate({center:{eles:n},zoom:Math.max(cy.zoom(),1)},{duration:300});}});
</script>
</body>
</html>
"""


def build_argparser():
    p = argparse.ArgumentParser(description="PinchTab web-navigation graph crawler")
    p.add_argument("url", help="start URL (e.g. https://example.com)")
    p.add_argument("--server", default=None,
                   help="PinchTab server URL of an ISOLATED instance (e.g. http://localhost:9871)")
    p.add_argument("--out", default="out/webgraph",
                   help="output basename (default: out/webgraph); parent dirs are created")
    p.add_argument("--max-pages", type=int, default=60, help="max pages/nodes (default 60)")
    p.add_argument("--interaction-depth", type=int, default=2,
                   help="how many clicks deep to explore widgets/SPA states (0 = links only)")
    p.add_argument("--max-actions-per-state", type=int, default=25,
                   help="cap on widgets probed per page/state (default 25)")
    p.add_argument("--max-actions", type=int, default=2000,
                   help="global budget of action clicks (default 2000)")
    p.add_argument("--allow-destructive", action="store_true",
                   help="also click logout/delete/pay/submit-style controls (DANGER)")
    p.add_argument("--skip-writes", action="store_true",
                   help="skip create/add/save/edit/invite/etc. controls (recommended on "
                        "real apps: maps navigation without creating/modifying data)")
    p.add_argument("--nav-only", action="store_true",
                   help="probe ONLY navigation controls (tabs, menus, role=tab/menuitem, "
                        "nav/header/aside buttons); skip bulk table/grid buttons. Much "
                        "faster + higher-signal interaction edges on real apps")
    p.add_argument("--auth-path", default=None,
                   help="path fragment meaning 'logged out' (e.g. /auth); if pages keep "
                        "landing here mid-crawl the session likely died and crawling stops")
    p.add_argument("--relogin-cmd", default=None,
                   help="command to run to re-authenticate when --auth-path is hit "
                        "(e.g. a login script); the crawl recovers and continues. Inherits "
                        "this process's env, so export PINCHTAB_CONFIG/TOKEN before running")
    p.add_argument("--include-subdomains", action="store_true",
                   help="treat *.domain.tld as same site")
    p.add_argument("--no-strip-tracking", dest="strip_tracking", action="store_false",
                   help="keep utm_/gclid/etc. query params (default: strip them)")
    p.add_argument("--delay", type=float, default=0.3,
                   help="seconds to wait after each nav/click for DOM settle (default 0.3)")
    p.add_argument("--nav-timeout", type=int, default=60, help="per-nav timeout seconds")
    p.add_argument("--render-timeout", type=int, default=10000,
                   help="ms to wait for SPA interactive content to render (default 10000)")
    p.add_argument("--solve", action="store_true", help="(reserved) attempt challenge solving")
    p.add_argument("--quiet", action="store_true")
    return p


def main():
    args = build_argparser().parse_args()
    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    if not args.url.lower().startswith(("http://", "https://")):
        args.url = "https://" + args.url
    print("Crawling %s  (depth=%d, max-pages=%d, destructive=%s)"
          % (args.url, args.interaction_depth, args.max_pages, args.allow_destructive),
          file=sys.stderr)
    Crawler(args).run()


if __name__ == "__main__":
    main()
