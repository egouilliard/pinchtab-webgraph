#!/usr/bin/env python3
"""
Interactive SELF-TEST → HTML report → (opt-in) GitHub issue — the self-improvement loop.

The idea: right after you crawl a site into a graph, you (the human who knows the
app) throw the HARDEST "how do I do X?" scenarios at the graph and judge whether the
answer is right. Every scenario that the graph gets wrong (missed a path, wrong
route, no form) is a concrete gap to fix — so the report is actionable FEEDBACK for
improving the crawler/graph, and can be filed as a GitHub issue with one keystroke.

Flow (`pwg test --start https://app.example.com/home`):
  1. resolve the graph (per-host cache by --start hostname, or explicit --graph),
  2. loop: you describe a hard goal → we answer it OFFLINE via api.howto (ms, no
     browser) → you say whether it's correct → if not, you say what's wrong,
  3. keep going until you're done (asked after each one),
  4. render a self-contained HTML report of every scenario + verdict + your notes,
  5. optionally (only if you pass --repo AND confirm) open it as a GitHub issue.

Design: the graph query is OFFLINE and read-only (api.howto never drives a browser
and never mutates the cache) — a "miss" IS a finding, not an error. The pure
functions (evaluate_scenario / build_report / render_html / render_issue) take no
I/O so they're unit-tested directly; only run_interactive() and _submit_issue() do
terminal / subprocess I/O.

Generic + stdlib only: the graph is routed by hostname; nothing here knows any
app's sections, labels, or vocabulary. Matches the repo's stay-generic rule.
"""
import argparse
import datetime
import html
import os
import shutil
import subprocess
import sys
from urllib.parse import urlparse

from . import api, cache_store


# ---------------------------------------------------------------------------
# Pure core — no terminal / network / subprocess I/O (directly unit-tested).
# ---------------------------------------------------------------------------

# verdicts a scenario can carry. "unrated" = ran but no human judged it (e.g. a
# non-interactive --goal seed with no TTY to ask on).
PASS, FAIL, UNRATED = "pass", "fail", "unrated"


def evaluate_scenario(graph_path, goal, start=None):
    """Run one how-to goal OFFLINE against the graph → a flat, report-ready record.

    Returns the record dict. `verdict` starts UNRATED and `note` empty; the caller
    (interactive loop) fills them in from the human's judgment. Never raises for a
    graph miss — the miss is captured in `status`/`candidates`.
    """
    r = api.howto(graph_path, goal=goal, start=start)
    rec = {
        "goal": goal,
        "status": r.get("status"),
        "start_url": r.get("start_url"),
        "clicks": None,
        "trigger": None,
        "steps": [],
        "form_field_count": None,
        "candidates": r.get("candidates") or [],
        "verdict": UNRATED,
        "note": "",
    }
    if r.get("status") == "ok" and r.get("results"):
        res = r["results"][0]
        rec["clicks"] = res.get("clicks")
        rec["trigger"] = res.get("trigger_label")
        rec["steps"] = res.get("steps") or []
        rec["form_field_count"] = (res.get("form") or {}).get("fieldCount")
    return rec


def scenario_found(rec):
    """True if the graph returned a concrete path for this scenario."""
    return rec.get("status") == "ok"


def build_report(host, start, graph_path, summary, records, generated_at):
    """Assemble the report data model (pure) from evaluated + judged scenarios."""
    totals = {"total": len(records), PASS: 0, FAIL: 0, UNRATED: 0}
    for rec in records:
        v = rec.get("verdict", UNRATED)
        totals[v] = totals.get(v, 0) + 1
    return {
        "host": host,
        "start_url": start,
        "graph_file": graph_path,
        "generated_at": generated_at,
        "summary": summary or {},
        "scenarios": list(records),
        "totals": totals,
    }


def _e(x):
    return html.escape("" if x is None else str(x))


def _scenario_card_html(idx, rec):
    verdict = rec.get("verdict", UNRATED)
    found = scenario_found(rec)
    # A verdict badge (human's call) + a capture badge (did the graph find anything).
    badges = '<span class="badge v-%s">%s</span>' % (_e(verdict), _e(verdict.upper()))
    badges += ' <span class="badge %s">%s</span>' % (
        "cap-ok" if found else "cap-miss",
        "captured" if found else "not captured")

    parts = ['<article class="scn %s">' % ("ok" if verdict == PASS else
                                           "bad" if verdict == FAIL else "neu")]
    parts.append('<header><span class="num">#%d</span> <h3>%s</h3> %s</header>'
                 % (idx, _e(rec.get("goal")), badges))

    meta_bits = []
    if rec.get("clicks") is not None:
        meta_bits.append("%s clicks" % _e(rec["clicks"]))
    if rec.get("trigger"):
        meta_bits.append("trigger: <code>%s</code>" % _e(rec["trigger"]))
    if rec.get("form_field_count") is not None:
        meta_bits.append("%s form fields" % _e(rec["form_field_count"]))
    meta_bits.append("status: <code>%s</code>" % _e(rec.get("status")))
    parts.append('<p class="meta">%s</p>' % " · ".join(meta_bits))

    if rec.get("steps"):
        steps = "".join("<li>%s</li>" % _e(s) for s in rec["steps"])
        parts.append('<div class="steps"><div class="lbl">Path the graph returned</div>'
                     '<ol>%s</ol></div>' % steps)
    elif rec.get("candidates"):
        cands = ", ".join("<code>%s</code>" % _e(c) for c in rec["candidates"][:12])
        parts.append('<div class="steps"><div class="lbl">No path — nearest labels</div>'
                     '<p class="cands">%s</p></div>' % cands)
    else:
        parts.append('<div class="steps"><div class="lbl">No path found and no near matches</div></div>')

    if rec.get("note"):
        parts.append('<div class="note"><div class="lbl">What’s wrong / expected</div>'
                     '<p>%s</p></div>' % _e(rec["note"]))
    parts.append("</article>")
    return "".join(parts)


def render_html(report):
    """Render the report data model into a self-contained, theme-aware HTML string."""
    t = report["totals"]
    s = report.get("summary") or {}
    cards = "\n".join(_scenario_card_html(i + 1, r)
                      for i, r in enumerate(report.get("scenarios", [])))
    if not cards:
        cards = '<p class="empty">No scenarios were tested.</p>'
    tokens = {
        "TITLE": "Self-test report — %s" % (report.get("host") or "graph"),
        "HOST": report.get("host") or "(unknown host)",
        "START_URL": report.get("start_url") or "",
        "GENERATED_AT": report.get("generated_at") or "",
        "GRAPH_FILE": report.get("graph_file") or "",
        "N_TOTAL": str(t.get("total", 0)),
        "N_PASS": str(t.get(PASS, 0)),
        "N_FAIL": str(t.get(FAIL, 0)),
        "N_UNRATED": str(t.get(UNRATED, 0)),
        "N_STATES": str(s.get("states", "?")),
        "N_EDGES": str(s.get("edges", "?")),
        "N_TRIGGERS": str(s.get("triggers", "?")),
        "SCENARIOS": cards,
    }
    out = REPORT_TEMPLATE
    for k, v in tokens.items():
        out = out.replace("{{%s}}" % k, _e(v) if k != "SCENARIOS" else v)
    return out


def render_issue(report):
    """Render a (title, markdown-body) pair for `gh issue create` — tool feedback.

    Fails are the actionable content, so they get full detail; passes are summarised.
    """
    t = report["totals"]
    fails = [r for r in report.get("scenarios", []) if r.get("verdict") == FAIL]
    host = report.get("host") or "graph"
    title = "[self-test] %s — %d/%d scenarios failed" % (host, len(fails), t.get("total", 0))

    lines = [
        "**Self-test report** generated by `pwg test`.",
        "",
        "| host | scenarios | pass | fail | unrated |",
        "| --- | --- | --- | --- | --- |",
        "| `%s` | %d | %d | %d | %d |" % (host, t.get("total", 0), t.get(PASS, 0),
                                          t.get(FAIL, 0), t.get(UNRATED, 0)),
        "",
        "- start URL: `%s`" % (report.get("start_url") or "—"),
        "- graph: `%s`" % (report.get("graph_file") or "—"),
        "- generated: %s" % (report.get("generated_at") or "—"),
    ]
    s = report.get("summary") or {}
    if s:
        lines.append("- graph size: %s states · %s edges · %s triggers"
                     % (s.get("states", "?"), s.get("edges", "?"), s.get("triggers", "?")))

    if fails:
        lines += ["", "## Failing scenarios (gaps to fix)"]
        for r in fails:
            lines.append("")
            lines.append("### %s" % r.get("goal"))
            lines.append("- graph result: `%s`%s"
                         % (r.get("status"),
                            (" → %s clicks, trigger `%s`" % (r.get("clicks"), r.get("trigger")))
                            if scenario_found(r) else ""))
            if r.get("steps"):
                lines.append("- returned path: %s"
                             % " → ".join("`%s`" % st for st in r["steps"]))
            elif r.get("candidates"):
                lines.append("- nearest labels: %s"
                             % ", ".join("`%s`" % c for c in r["candidates"][:12]))
            if r.get("note"):
                lines.append("- **what's wrong / expected:** %s" % r["note"])
    else:
        lines += ["", "_No failing scenarios — the graph answered every tested goal correctly._"]

    passes = [r for r in report.get("scenarios", []) if r.get("verdict") == PASS]
    if passes:
        lines += ["", "<details><summary>%d passing scenarios</summary>" % len(passes), ""]
        for r in passes:
            lines.append("- %s — `%s`%s"
                         % (r.get("goal"), r.get("status"),
                            (" (%s clicks)" % r.get("clicks")) if scenario_found(r) else ""))
        lines += ["", "</details>"]
    return title, "\n".join(lines)


# ---------------------------------------------------------------------------
# Interactive driver + I/O (thin; delegates all logic to the pure core above).
# ---------------------------------------------------------------------------

def _prompt(msg):
    try:
        return input(msg).strip()
    except EOFError:
        return ""


def _ask_yes_no(msg, default=True):
    hint = "[Y/n]" if default else "[y/N]"
    while True:
        ans = _prompt("%s %s " % (msg, hint)).lower()
        if not ans:
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False


def _print_result(rec):
    print()
    if scenario_found(rec):
        print("  ✓ graph found a path (%s clicks):" % rec["clicks"])
        for st in rec["steps"]:
            print("      %s" % st)
        if rec.get("form_field_count") is not None:
            print("    form: %s fields at the trigger" % rec["form_field_count"])
        else:
            print("    (no form captured at the trigger)")
    else:
        print("  ✗ graph found NO path (status: %s)." % rec["status"])
        if rec.get("candidates"):
            print("    nearest trigger labels: %s"
                  % ", ".join(rec["candidates"][:12]))
    print()


def run_interactive(graph_path, host, start, summary, seeds, max_suggested=5):
    """Drive the human through the test loop; return the list of judged records."""
    records = []
    print("Self-test — %s" % (host or graph_path))
    if summary:
        print("Graph: %s states · %s edges · %s triggers"
              % (summary.get("states", "?"), summary.get("edges", "?"),
                 summary.get("triggers", "?")))
    print("Throw your %d hardest \"how do I do X?\" goals at the graph. "
          "Blank line finishes.\n" % max_suggested)

    n = 0
    queue = list(seeds or [])
    while True:
        n += 1
        if queue:
            goal = queue.pop(0)
            print("Scenario #%d (from --goal): %s" % (n, goal))
        else:
            goal = _prompt("Scenario #%d — describe a hard goal (blank to finish): " % n)
            if not goal:
                break
        rec = evaluate_scenario(graph_path, goal, start)
        _print_result(rec)
        correct = _ask_yes_no("Is that correct / what you expected?", default=scenario_found(rec))
        rec["verdict"] = PASS if correct else FAIL
        if not correct:
            rec["note"] = _prompt("  What's wrong / what did you expect? ")
        records.append(rec)
        if not queue and not _ask_yes_no("Test another scenario?", default=True):
            break
    return records


def _submit_issue(repo, title, body, assume_yes=False):
    """Preview, confirm (PUBLIC!), then `gh issue create -R repo`. Returns True on success."""
    if not shutil.which("gh"):
        print("! `gh` CLI not found — cannot open an issue. Install GitHub CLI or file it "
              "manually from the HTML report.", file=sys.stderr)
        return False
    print("\n--- GitHub issue preview (repo: %s) ---" % repo)
    print("Title: %s\n" % title)
    print(body)
    print("--- end preview ---")
    print("\n⚠  This posts to %s, which may be PUBLIC. The body can include target-app "
          "labels / URLs / form details — do not publish anything you can't disclose."
          % repo)
    if not assume_yes and not _ask_yes_no("Open this as a GitHub issue now?", default=False):
        print("Skipped issue creation.")
        return False
    proc = subprocess.run(["gh", "issue", "create", "-R", repo, "--title", title, "--body", body])
    return proc.returncode == 0


def _resolve_graph(start, graph):
    """(path, host, error_msg). Exactly one routing source; host is for labelling."""
    if graph:
        host = None
        try:
            summ = api.graph_summary(graph)
            host = (summ.get("meta") or {}).get("host")
        except Exception:
            pass
        if not host and start:
            host = urlparse(start).hostname
        return graph, host, None
    if not start:
        return None, None, "pass --start <url> (routes the per-host cache) or --graph <file>"
    host = urlparse(start).hostname
    if not host:
        return None, None, "--start must be a full URL including a scheme, e.g. https://app.example.com"
    try:
        path = cache_store.cache_path(host)
    except ValueError:
        return None, None, "invalid host derived from --start: %r" % host
    if not os.path.exists(path):
        return None, host, ("no cache for %s yet — crawl it first:\n"
                            "  pwg crawl --start %s --capture-content" % (host, start))
    return path, host, None


def main():
    ap = argparse.ArgumentParser(
        description="Interactively self-test a crawled graph, write an HTML report, "
                    "and optionally file it as a GitHub issue.")
    ap.add_argument("--start", help="start URL — routes the per-host cache AND pins the how-to start state")
    ap.add_argument("--graph", help="explicit graph file (bypasses host routing)")
    ap.add_argument("--goal", action="append", default=[],
                    help="pre-seed a scenario goal (repeatable); runs before interactive prompts")
    ap.add_argument("--out", help="HTML report path (default: test-report-<host>-<timestamp>.html)")
    ap.add_argument("--repo", help="OWNER/NAME to enable filing the report as a GitHub issue (opt-in)")
    ap.add_argument("--yes", action="store_true",
                    help="skip the interactive confirmation before creating the GitHub issue")
    ap.add_argument("--max-suggested", type=int, default=5,
                    help="how many scenarios to suggest testing (a hint only; default 5)")
    a = ap.parse_args()

    graph_path, host, err = _resolve_graph(a.start, a.graph)
    if err:
        sys.stderr.write(err + "\n")
        return 1

    try:
        summary = api.graph_summary(graph_path)
        summary = {"states": summary.get("states"), "edges": summary.get("edges"),
                   "triggers": summary.get("triggers")}
    except Exception as e:
        sys.stderr.write("could not read graph %s: %s\n" % (graph_path, e))
        return 1

    interactive = sys.stdin.isatty()
    if interactive:
        records = run_interactive(graph_path, host, a.start, summary, a.goal, a.max_suggested)
    else:
        # No TTY: run any seeded goals unattended (verdict stays UNRATED — nobody to ask).
        if not a.goal:
            sys.stderr.write("not a TTY and no --goal given — nothing to test. "
                             "Pass --goal, or run in an interactive terminal.\n")
            return 1
        records = [evaluate_scenario(graph_path, g, a.start) for g in a.goal]

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    report = build_report(host, a.start, graph_path, summary, records, now)

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = a.out or ("test-report-%s-%s.html" % (host or "graph", stamp))
    with open(out, "w") as f:
        f.write(render_html(report))
    t = report["totals"]
    print("\nReport written: %s  (%d tested — %d pass / %d fail / %d unrated)"
          % (out, t["total"], t[PASS], t[FAIL], t[UNRATED]))

    if a.repo:
        title, body = render_issue(report)
        if interactive or a.yes:
            _submit_issue(a.repo, title, body, assume_yes=a.yes)
        else:
            print("(--repo given but no TTY and no --yes — skipping issue; "
                  "the report is on disk.)")
    elif t[FAIL] and interactive:
        print("Tip: re-run with --repo OWNER/NAME to file these %d gaps as a GitHub issue."
              % t[FAIL])
    return 0


# ---------------------------------------------------------------------------
# Report template — embedded as a string (the repo gitignores *.html, and the
# rest of the toolkit likewise ships self-contained HTML as string constants).
# Theme-aware (respects the OS light/dark preference). Zero external assets.
# ---------------------------------------------------------------------------
REPORT_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{TITLE}}</title>
<style>
  :root {
    --bg:#f6f7f9; --card:#fff; --ink:#1a1d21; --mut:#5b6570; --line:#e3e6ea;
    --ok:#1a7f37; --okbg:#e7f5ec; --bad:#c0392b; --badbg:#fceceb;
    --neu:#8a6d1a; --neubg:#fbf3da; --accent:#3b5bdb; --code:#eef0f3;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg:#14171a; --card:#1c2024; --ink:#e6e9ec; --mut:#9aa4ae; --line:#2c3238;
      --ok:#4ac26b; --okbg:#132a1b; --bad:#f0776a; --badbg:#2c1816;
      --neu:#d9b74a; --neubg:#2a2413; --accent:#7aa2ff; --code:#262b31;
    }
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
    font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
  .wrap { max-width:900px; margin:0 auto; padding:32px 20px 64px; }
  header.top h1 { margin:0 0 4px; font-size:22px; }
  header.top .sub { color:var(--mut); font-size:13px; word-break:break-all; }
  .cards { display:flex; flex-wrap:wrap; gap:12px; margin:22px 0 8px; }
  .stat { background:var(--card); border:1px solid var(--line); border-radius:10px;
    padding:12px 16px; min-width:96px; }
  .stat .n { font-size:24px; font-weight:700; }
  .stat .k { color:var(--mut); font-size:12px; text-transform:uppercase; letter-spacing:.04em; }
  .stat.pass .n { color:var(--ok); } .stat.fail .n { color:var(--bad); }
  .stat.unrated .n { color:var(--neu); }
  h2 { font-size:14px; text-transform:uppercase; letter-spacing:.05em; color:var(--mut);
    margin:30px 0 12px; }
  .scn { background:var(--card); border:1px solid var(--line); border-left-width:4px;
    border-radius:10px; padding:16px 18px; margin:0 0 14px; }
  .scn.ok { border-left-color:var(--ok); } .scn.bad { border-left-color:var(--bad); }
  .scn.neu { border-left-color:var(--neu); }
  .scn header { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
  .scn header h3 { margin:0; font-size:16px; }
  .scn .num { color:var(--mut); font-variant-numeric:tabular-nums; font-weight:600; }
  .badge { font-size:11px; font-weight:700; letter-spacing:.03em; padding:2px 8px;
    border-radius:20px; }
  .badge.v-pass { background:var(--okbg); color:var(--ok); }
  .badge.v-fail { background:var(--badbg); color:var(--bad); }
  .badge.v-unrated { background:var(--neubg); color:var(--neu); }
  .badge.cap-ok { background:var(--code); color:var(--mut); }
  .badge.cap-miss { background:var(--code); color:var(--mut); }
  .meta { color:var(--mut); font-size:13px; margin:8px 0 0; }
  .steps, .note { margin-top:12px; }
  .lbl { font-size:12px; text-transform:uppercase; letter-spacing:.04em; color:var(--mut);
    margin-bottom:4px; }
  .steps ol { margin:0; padding-left:22px; }
  .steps li { margin:2px 0; }
  .note p { margin:0; background:var(--badbg); border-radius:8px; padding:10px 12px; }
  .cands { margin:0; }
  code { background:var(--code); padding:1px 6px; border-radius:5px; font-size:12.5px;
    font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
  .empty { color:var(--mut); }
  footer { margin-top:36px; color:var(--mut); font-size:12px; border-top:1px solid var(--line);
    padding-top:14px; }
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <h1>Self-test report</h1>
    <div class="sub"><code>{{HOST}}</code> · start <code>{{START_URL}}</code> · {{GENERATED_AT}}</div>
  </header>

  <div class="cards">
    <div class="stat"><div class="n">{{N_TOTAL}}</div><div class="k">tested</div></div>
    <div class="stat pass"><div class="n">{{N_PASS}}</div><div class="k">pass</div></div>
    <div class="stat fail"><div class="n">{{N_FAIL}}</div><div class="k">fail</div></div>
    <div class="stat unrated"><div class="n">{{N_UNRATED}}</div><div class="k">unrated</div></div>
  </div>
  <div class="cards">
    <div class="stat"><div class="n">{{N_STATES}}</div><div class="k">states</div></div>
    <div class="stat"><div class="n">{{N_EDGES}}</div><div class="k">edges</div></div>
    <div class="stat"><div class="n">{{N_TRIGGERS}}</div><div class="k">triggers</div></div>
  </div>

  <h2>Scenarios</h2>
  {{SCENARIOS}}

  <footer>
    Graph: <code>{{GRAPH_FILE}}</code><br>
    Generated by <code>pwg test</code> — deterministic offline self-test of the crawled navigation graph.
  </footer>
</div>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main() or 0)
