#!/usr/bin/env python3
"""
OPTIONAL keyring-backed automated login for pinchtab-webgraph.

Opt-in by construction: nothing here runs unless you pass ``--login-config`` to
``interaction_crawl`` (or invoke this module directly). The ``keyring`` package is
an OPTIONAL dependency, imported lazily only when a login actually runs — so the
base install stays pure-stdlib (``pip install pinchtab-webgraph``). Enable this
feature with ``pip install 'pinchtab-webgraph[login]'``.

SAFEST OPTION FIRST: for a one-off crawl, just log in BY HAND once in the
persistent bridge profile (``.instance/``) and let the session cookie ride — the
password then never touches this toolkit at all. Use this module only when you
need UNATTENDED re-login (long crawls whose bridge restarts mid-run, scheduled
runs) without keeping a plaintext password around.

WHERE SECRETS LIVE — split so nothing secret is ever written to disk here:

  * ``login-config.json``  (GITIGNORED)  — per-host ROUTING only, NO password::

      {
        "app.example.com": {
          "url":        "https://app.example.com/login",
          "username":   "me@example.com",
          "userField":  "input[name=email]",       // optional (auto-detected)
          "passField":  "input[type=password]",    // optional (auto-detected)
          "submit":     "button[type=submit]",     // optional (auto-detected / Enter)
          "successUrl": "/dashboard",              // optional (substring proving success)
          "keyringService": "pinchtab-webgraph"    // optional (this default)
        }
      }

  * the PASSWORD  —  OS KEYRING ONLY, resolved at runtime, never on disk here::

      keyring set pinchtab-webgraph me@example.com

The password is masked in every log/error (only its length is ever shown); it is
passed straight to the browser's fill command and never reaches the graph JSON,
stdout, or the config file.
"""
import json
import os
import sys
import time
from urllib.parse import urlparse

from . import recipe  # proven pinchtab primitives

pt = recipe.pt
pt_json = recipe.pt_json
nav = recipe.nav
settle = recipe.settle

DEFAULT_SERVICE = "pinchtab-webgraph"


def _host(u):
    n = (urlparse(u).hostname or u or "").lower()
    return n[4:] if n.startswith("www.") else n


def load_config(path):
    with open(path) as f:
        return json.load(f)


def entry_for(cfg, url):
    """Per-host login entry matching ``url``'s hostname (or None)."""
    h = _host(url)
    if h in cfg:
        return cfg[h]
    for k, v in cfg.items():
        if _host(k) == h:
            return v
    return None


def _get_password(entry):
    """Resolve the password from the OS keyring ONLY. Lazily imports `keyring`
    so it stays an optional dependency; the secret is never logged or returned
    to any caller that prints it."""
    service = entry.get("keyringService", DEFAULT_SERVICE)
    user = entry.get("username", "")
    try:
        import keyring
    except ImportError:
        raise SystemExit(
            "Automated login needs the optional 'keyring' package (opt-in feature).\n"
            "  pip install 'pinchtab-webgraph[login]'   (or: pip install keyring)\n"
            "Then store the password once and re-run:\n"
            "  keyring set %s %s\n"
            "Or skip automated login and just log in by hand in the bridge profile."
            % (service, user or "<username>"))
    pw = keyring.get_password(service, user)
    if not pw:
        raise SystemExit(
            "No password in the OS keyring for service=%r username=%r.\n"
            "Store it once:  keyring set %s %s" % (service, user, service, user))
    return pw


# Structural field detection — standard HTML semantics only (input types +
# autocomplete tokens + DOM order), NO app-specific labels/vocabulary. Keeps the
# module generic per the project's hard rule.
DETECT_JS = r"""
(() => {
  function cssEsc(s){return (window.CSS&&CSS.escape)?CSS.escape(s):s.replace(/[^a-zA-Z0-9_-]/g,'\\$&');}
  function sel(el){ if(!el) return null; if(el.id) return '#'+cssEsc(el.id);
    const parts=[]; let e=el;
    while(e&&e.nodeType===1&&e!==document.body){ let p=e.tagName.toLowerCase();
      const par=e.parentElement;
      if(par){const s=Array.prototype.filter.call(par.children,c=>c.tagName===e.tagName);
        if(s.length>1)p+=':nth-of-type('+(s.indexOf(e)+1)+')';}
      parts.unshift(p); e=par; if(parts.length>8)break; }
    return parts.join('>'); }
  function vis(el){const r=el.getBoundingClientRect();return r.width>0&&r.height>0;}
  const pass=[...document.querySelectorAll('input[type=password]')].filter(vis)[0]||null;
  const scope=(pass&&pass.closest('form'))||document;
  let user=scope.querySelector('input[autocomplete=username]');
  if(!user||!vis(user)) user=[...scope.querySelectorAll('input[type=email]')].filter(vis)[0]||null;
  if(!user){ // the visible text/tel input that immediately PRECEDES the password field
    const cands=[...scope.querySelectorAll('input[type=text],input[type=tel],input:not([type])')].filter(vis);
    if(pass){ user=cands.filter(c=>c.compareDocumentPosition(pass)&Node.DOCUMENT_POSITION_FOLLOWING).pop()
                    ||cands[0]||null; }
    else user=cands[0]||null;
  }
  let submit=scope.querySelector('button[type=submit],input[type=submit]');
  if(!submit||!vis(submit)) submit=[...scope.querySelectorAll('button')].filter(vis)[0]||null;
  return {user:sel(user), pass:sel(pass), submit:sel(submit)};
})()
"""


def _detect_fields(server):
    try:
        return pt_json(DETECT_JS, server) or {}
    except Exception as e:
        print("  [login] field auto-detect failed (%s)" % str(e)[:60], file=sys.stderr)
        return {}


def verify(entry, server):
    """Heuristic success check: we reached ``successUrl`` (if given), or we left
    the login page AND no password field is visible any more."""
    try:
        st = pt_json(
            "({href:location.href,"
            " pw:[...document.querySelectorAll('input[type=password]')]"
            "    .some(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0;})})",
            server)
    except Exception:
        return False
    href = st.get("href") or ""
    want = entry.get("successUrl")
    if want:
        return want in href
    def norm(u):
        return u.split("#")[0].split("?")[0].rstrip("/")
    return norm(href) != norm(entry.get("url", "")) and not st.get("pw")


def perform_login(entry, server, verbose=True):
    """Navigate to the login page, fill credentials (password from the keyring),
    submit, and verify. Returns True on confirmed success. Never prints the
    password — only its length."""
    def log(m):
        if verbose:
            print("  [login] " + m, file=sys.stderr)

    url = entry["url"]
    user = entry.get("username", "")
    pw = _get_password(entry)
    log("navigating to %s  (user=%s, pw=<%d chars>)" % (url, user or "-", len(pw)))
    # Cold-start-safe navigation: a fresh bridge has no tabs and $PINCHTAB_TAB may
    # hold a stale default id, so a plain nav() can leave evals hitting no tab. Adopt
    # a live tab if one exists, else force a new tab and pin it before reading.
    if recipe.pin_tab(server):
        nav(url, server)
    else:
        pt(["nav", url, "--new-tab"], server, timeout=60)
        recipe.pin_tab(server, url)
        settle(server)

    det = _detect_fields(server)
    user_sel = entry.get("userField") or det.get("user")
    pass_sel = entry.get("passField") or det.get("pass")
    submit_sel = entry.get("submit") or det.get("submit")
    if not pass_sel:
        raise SystemExit("[login] no password field found on %s — set "
                         '"passField" in the login config.' % url)

    if user_sel and user:
        pt(["fill", user_sel, user], server, timeout=20)
    else:
        log("no username field/value — filling password only")
    pt(["fill", pass_sel, pw], server, timeout=20)   # pw -> browser only, never logged

    if submit_sel:
        pt(["click", "--css", submit_sel], server, timeout=30)
    else:
        pt(["press", "Enter"], server, timeout=30)    # submit by Enter if no button found
    settle(server)
    time.sleep(1.0)                                    # let the post-auth redirect land

    ok = verify(entry, server)
    log("login %s" % ("OK" if ok else "could NOT be confirmed (check selectors/successUrl)"))
    return ok


def ensure_logged_in(config_path, url, server, verbose=True):
    """Opt-in entry point used by the crawler. Logs in if the config has an entry
    for ``url``'s host; returns None (no-op) when there is no matching entry, so
    the crawl proceeds on whatever session the bridge already holds."""
    cfg = load_config(config_path)
    entry = entry_for(cfg, url)
    if not entry:
        if verbose:
            print("  [login] no login-config entry for host %s — skipping "
                  "(crawling the bridge's existing session)" % _host(url), file=sys.stderr)
        return None
    return perform_login(entry, server, verbose=verbose)


def main():
    """CLI: also used as the wedge-recovery `--login-cmd` so a restarted bridge
    re-authenticates the same way. Reads the bridge token from the pinchtab config."""
    import argparse
    ap = argparse.ArgumentParser(
        description="Optional keyring-backed login for pinchtab-webgraph (opt-in).")
    ap.add_argument("--config", default="login-config.json",
                    help="gitignored per-host routing file (default login-config.json)")
    ap.add_argument("--server", default="http://localhost:9871")
    ap.add_argument("--host", default="",
                    help="host key or URL to log in (default: the sole entry if the file has one)")
    ap.add_argument("--pinchtab-config",
                    default=os.environ.get("PINCHTAB_CONFIG", "crawl-config.json"),
                    help="bridge config to read the auth token from")
    a = ap.parse_args()
    try:
        os.environ.setdefault(
            "PINCHTAB_TOKEN", json.load(open(a.pinchtab_config))["server"]["token"])
    except Exception:
        pass
    cfg = load_config(a.config)
    if a.host:
        entry = entry_for(cfg, a.host) or cfg.get(a.host)
    elif len(cfg) == 1:
        entry = next(iter(cfg.values()))
    else:
        raise SystemExit("multiple hosts in %s — pass --host <host>" % a.config)
    if not entry:
        raise SystemExit("no login entry for host %r in %s" % (a.host, a.config))
    sys.exit(0 if perform_login(entry, a.server) else 1)


if __name__ == "__main__":
    main()
