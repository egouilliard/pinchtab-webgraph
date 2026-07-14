#!/usr/bin/env python3
"""
PERFORM a how-to: actually run the compiled PinchTab command block against a live bridge.

This is the opt-in execution half of the "path -> executable" layer. `commands.py` compiles
a how-to (shortest click-path + terminal action) into structured steps; `api.resolve_action`
turns a goal into that plan OFFLINE; this module drives the live browser to carry it out:
navigate the path, then download / upload / fill.

  pinchtab-webgraph perform --host app.example.com --goal "download the q3 report"
  pinchtab-webgraph perform --graph out/site.json --goal "create team" \
      --set "Name=Acme" --set "Plan=Pro" --allow-submit
  pinchtab-webgraph perform --host app.example.com --goal "upload a document" --file ./x.pdf

SAFE BY DEFAULT — the same guarantees as the printed block, now enforced at execution:
  - the click-path only NAVIGATES (never a write/destructive control — those aren't in it);
  - navigation + downloads (no user input) run automatically — downloading is the point;
  - a field that needs a VALUE is SKIPPED unless you supply one (`--set '<label>=<value>'`,
    or `--file <path>` for a file input) — so it never types placeholder junk;
  - a form's SUBMIT never runs unless you pass `--allow-submit`;
  - `--dry-run` prints exactly what WOULD run and touches nothing.

Resolution is offline (needs a crawled cache/graph); only execution needs the bridge, so
`--host`/`--graph` selects a graph exactly like `howto`/`query`. Performing a real download
needs a bridge whose config has `security.allowDownload = true` (the crawl bridge sets it
false — that is for discovery). A rejected download surfaces the bridge's error verbatim.
"""
import argparse
import json
import os
import subprocess
import sys

from . import api, commands

DEFAULT_SERVER = "http://localhost:9871"


def load_token(config_path=None):
    """Read the bridge token from crawl-config.json (or $PINCHTAB_CONFIG). None if absent."""
    path = config_path or os.environ.get("PINCHTAB_CONFIG", "crawl-config.json")
    try:
        return json.load(open(path))["server"]["token"]
    except Exception:
        return None


def _pt(argv, server, token, tab, timeout=90):
    """Run one `pinchtab` command. Mirrors recipe.py pt(): `pinchtab --server <s> <argv>`
    with the bridge token in the env. Returns (rc, stdout, stderr)."""
    cmd = ["pinchtab"]
    if server:
        cmd += ["--server", server]
    cmd += list(argv)
    env = dict(os.environ)
    if token:
        env["PINCHTAB_TOKEN"] = token
    # target the tab via the env var (documented: `--tab` default is env PINCHTAB_TAB), NOT
    # a flag — commands like `download` have no --tab flag and would error on it, but they
    # harmlessly ignore the env var.
    if tab:
        env["PINCHTAB_TAB"] = tab
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except FileNotFoundError:
        return 127, "", "the `pinchtab` CLI is not on PATH"
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


def _tab_is_missing(text):
    """Does this failure mean 'the tab I aimed at is gone'? The bridge answers a command aimed
    at a dead/stale tab with `Error 404: tab <id> not found`. That — and only that — is the
    failure a nav may retry in a new tab; anything else (a real 404, a blocked domain) must
    surface rather than be masked."""
    return "not found" in (text or "").lower()


def _printed_tab_id(out):
    """The tab id `nav --print-tab-id` prints (last non-empty line of stdout)."""
    lines = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
    return lines[-1] if lines else None


def resolve_tab(server, token, url=None, _run=_pt):
    """Return a live page tab id to drive (PinchTab's stored default tab id goes stale and
    then every command 404s with 'tab not found'). Reuse an existing page tab; if there is
    none (a freshly started bridge has NO tabs) open one AT `url` when the caller knows one —
    `nav <url> --new-tab --print-tab-id` is the only way to create a tab, and it needs a REAL
    url (the bridge rejects a blank-page url with `400 invalid url`). None when there is no
    tab and no url — which is survivable: execute_steps self-heals on its first `nav`."""
    rc, out, _ = _run(["tab", "--json"], server, token, None, timeout=15)
    if rc == 0 and out:
        try:
            tabs = json.loads(out)
            if isinstance(tabs, dict):
                tabs = tabs.get("tabs", [])
            pages = [t for t in tabs if t.get("type") == "page" and t.get("id")]
            if pages:
                active = [t for t in pages if t.get("status") == "active"] or pages
                return active[-1]["id"]
        except (ValueError, TypeError):
            pass
    if not url:
        return None
    rc, out, _ = _run(["nav", url, "--new-tab", "--print-tab-id"], server, token, None)
    return _printed_tab_id(out) if rc == 0 else None


def _apply_out_dir(argv, out_dir):
    """Rewrite a download step's `-o <name>` to live under out_dir (client-side path)."""
    if not out_dir or "-o" not in argv:
        return argv
    argv = list(argv)
    i = argv.index("-o")
    if i + 1 < len(argv):
        argv[i + 1] = os.path.join(out_dir, os.path.basename(argv[i + 1]))
    return argv


def execute_steps(steps, *, server=DEFAULT_SERVER, token=None, tab=None,
                  values=None, upload_file=None, out_dir=None, dry_run=False,
                  _run=_pt):
    """Execute a structured step list (from `commands.steps_for_trigger`). Returns a list
    of per-step result dicts: {line, status, ...}. `status` is one of run / ok / error /
    skipped / dry-run / aborted. `_run` is injectable for tests."""
    values = {k.lower(): v for k, v in (values or {}).items()}
    out = []
    for s in steps:
        if s["argv"] is None:               # note / blank / section header — display only
            continue
        rendered = commands.render_step(s)
        role = s.get("role")
        if s.get("disabled"):               # a gated step (form submit without --allow-submit)
            out.append({"line": rendered, "role": role, "status": "skipped",
                        "reason": "gated (submit) — pass --allow-submit to run it"})
            continue
        argv = list(s["argv"])
        if s.get("needs_input"):
            if role == "upload":
                if not upload_file:
                    out.append({"line": rendered, "role": role, "status": "skipped",
                                "reason": "needs a file — pass --file <path>"})
                    continue
                argv[s["value_index"]] = upload_file
            else:
                val = values.get((s.get("label") or "").lower())
                if val is None:
                    out.append({"line": rendered, "role": role, "status": "skipped",
                                "reason": "needs a value — pass --set %r"
                                          % ("%s=<value>" % (s.get("label") or "field"))})
                    continue
                if s["value_index"] is None:
                    # a checkbox: `check <sel>` has no value slot. The value is a BOOLEAN —
                    # run the command when truthy, skip it when falsy. (Substituting here
                    # would be argv[None] → TypeError.)
                    if not commands.is_truthy(val):
                        out.append({"line": rendered, "role": role, "status": "skipped",
                                    "reason": "value %r is falsy — leaving it unchecked" % val})
                        continue
                else:
                    argv[s["value_index"]] = val
        if role == "download":
            argv = _apply_out_dir(argv, out_dir)
        shown = commands.render_step({**s, "argv": argv})
        if dry_run:
            out.append({"line": shown, "role": role, "status": "dry-run"})
            continue
        rc, sout, serr = _run(argv, server, token, tab)
        if rc != 0 and role == "nav" and _tab_is_missing(serr + sout):
            # SELF-HEAL a missing tab. `tab` is None (nothing to adopt on a fresh bridge, so
            # the bridge fell back to its STALE stored default) or its tab has since closed —
            # either way every later step would 404 the same way. Re-nav to the SAME url in a
            # new tab and pin the id it prints, so the rest of the plan lands on a live tab.
            # Only a "tab not found" failure is retried; a genuine nav error still errors.
            rc, sout, serr = _run(argv + ["--new-tab", "--print-tab-id"], server, token, None)
            if rc == 0:
                tab = _printed_tab_id(sout) or tab
        rec = {"line": shown, "role": role, "status": "ok" if rc == 0 else "error", "rc": rc}
        if rc != 0:
            rec["stderr"] = (serr or sout)[:300]
        out.append(rec)
        if rc != 0 and role in ("nav", "click"):
            # a broken navigation step means later steps won't land — stop cleanly.
            out.append({"status": "aborted",
                        "reason": "a navigation step failed; not running the rest"})
            break
    return out


def execute_plan(trigger, path_steps, start_url, *, allow_submit=False, **kw):
    """Build the steps for a resolved plan and execute them. `kw` → execute_steps."""
    steps = commands.steps_for_trigger(trigger, path_steps, start_url,
                                       allow_submit=allow_submit)
    return execute_steps(steps, **kw)


def _parse_set(pairs):
    values = {}
    for p in pairs or []:
        if "=" not in p:
            sys.exit("--set expects '<field label>=<value>', got %r" % p)
        k, v = p.split("=", 1)
        values[k.strip()] = v
    return values


def main():
    ap = argparse.ArgumentParser(
        description="Perform a how-to: run the compiled PinchTab command block live")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--host", help="resolve against this host's cache (caches/<host>.json)")
    src.add_argument("--graph", help="resolve against an explicit interaction-graph JSON")
    ap.add_argument("--goal", help='what to do, e.g. "download the q3 report" / "create team"')
    ap.add_argument("--match", help="regex for the trigger label (overrides --goal matching)")
    ap.add_argument("--start", help="start URL for the path (default: the crawl root)")
    ap.add_argument("--index", type=int, default=0,
                    help="which routed match to run when several tie (0 = shortest)")
    ap.add_argument("--set", action="append", metavar="LABEL=VALUE", default=[],
                    help="fill a form field by its label (repeatable)")
    ap.add_argument("--file", help="file path for an upload step")
    ap.add_argument("--out-dir", help="directory to save downloads into (default: cwd)")
    ap.add_argument("--allow-submit", action="store_true",
                    help="also run a form's SUBMIT (off by default — never auto-saves)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print exactly what WOULD run; touch nothing")
    ap.add_argument("--server", default=DEFAULT_SERVER, help="PinchTab bridge (default %(default)s)")
    ap.add_argument("--config", default=os.environ.get("PINCHTAB_CONFIG", "crawl-config.json"),
                    help="crawl-config.json — read the bridge token from it")
    ap.add_argument("--json", action="store_true", help="emit the result as JSON")
    a = ap.parse_args()

    if not (a.goal or a.match):
        ap.error("pass --goal or --match")

    # locate the graph exactly like howto/query
    if a.graph:
        graph_path = a.graph
    else:
        from . import cache_store
        try:
            graph_path = cache_store.cache_path(a.host)
        except ValueError:
            sys.exit("invalid --host %r" % a.host)
        if not os.path.exists(graph_path):
            sys.exit("no cache for %s yet — crawl it first (pinchtab-webgraph crawl / ask)."
                     % a.host)

    plan = api.resolve_action(graph_path, goal=a.goal, start=a.start, match=a.match,
                              index=a.index)
    if plan.get("status") != "ok":
        if a.json:
            print(json.dumps(plan, indent=2))
        else:
            print("✗ could not resolve %r: %s" % (a.match or a.goal, plan.get("status")),
                  file=sys.stderr)
            if plan.get("candidates"):
                print("  candidates: %s" % ", ".join(plan["candidates"]), file=sys.stderr)
        return 2

    token = load_token(a.config)
    values = _parse_set(a.set)
    # pin a live tab so the nav/click steps don't 404 on PinchTab's stale default tab. The
    # plan's start_url is what a fresh (zero-tab) bridge opens its first tab at.
    tab = None if a.dry_run else resolve_tab(a.server, token, plan["start_url"])
    results = execute_plan(plan["trigger"], plan["path_steps"], plan["start_url"],
                           allow_submit=a.allow_submit, server=a.server, token=token, tab=tab,
                           values=values, upload_file=a.file, out_dir=a.out_dir,
                           dry_run=a.dry_run)

    payload = {"status": "ok", "goal": a.goal, "trigger": plan["trigger_label"],
               "action_kind": plan["action_kind"], "download_url": plan.get("download_url"),
               "dry_run": a.dry_run, "steps": results}
    if a.json:
        print(json.dumps(payload, indent=2))
    else:
        verb = "WOULD run" if a.dry_run else "ran"
        print("=== PERFORM: %s ===  (%s, %s)"
              % ((a.goal or a.match).upper(), plan["action_kind"], verb))
        for r in results:
            if r["status"] == "aborted":
                print("  ✗ ABORTED — %s" % r["reason"])
                continue
            mark = {"ok": "✓", "dry-run": "·", "skipped": "–", "error": "✗"}.get(r["status"], " ")
            print("  %s %s" % (mark, r["line"]))
            if r.get("reason"):
                print("       ↳ %s" % r["reason"])
            if r.get("stderr"):
                print("       ↳ error: %s" % r["stderr"])
    # nonzero exit if any step errored (so scripts/CI can tell)
    return 1 if any(r.get("status") == "error" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
