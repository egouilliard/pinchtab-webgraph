#!/usr/bin/env python3
"""
The STEP VM — interprets a flow document against a live browser.

This is the piece the toolkit was missing. `perform.py` executes a STRAIGHT LINE: nav, nav,
click, fill, submit. Every automation worth writing needs more than that — loop over the 20
download buttons on this page, then do it again for each of the 12 pages, and only keep the
files I haven't already got. Those are `for_each`, `paginate` and content-hash dedupe, and
they are what this module adds.

Design constraints, in priority order:

  1. **The browser is a port.** Every side effect goes through `browser.py`'s interface, so
     the entire VM — including loops, gating and dedupe — is unit-testable against a fake,
     and a hosted worker can hand it a leased, tenant-scoped session without the VM knowing.
  2. **Safe by default.** Writes (submit, upload) run only if the flow DECLARED the
     capability *and* the caller granted it at run time. Both must agree; either can veto.
  3. **Resolution is offline.** A `goal` step re-resolves through `api.resolve_action`
     against the crawled graph — no LLM, no live search. The graph is the compiler.
  4. **Everything is an event.** Each step emits a structured frame, so a CLI progress line,
     an SSE stream to a browser, and a persisted run record are all the same data.

Returns a run record; raises only on a malformed flow (that is `flow.validate`'s job).
"""
import fnmatch
import time
from urllib.parse import urlparse

from . import api, browser as browser_mod, commands, flow as flow_mod

MAX_TOTAL_STEPS = 10000        # runaway guard: a flow cannot execute more than this
DEFAULT_FOR_EACH_MAX = 200
DEFAULT_MAX_PAGES = 25
DEFAULT_WAIT_TIMEOUT_MS = 10000


class Run:
    """The mutable state of one flow execution: variables, results, the event log."""

    def __init__(self, flow, inputs, caps, emit=None, dry_run=False):
        self.flow = flow
        self.caps = caps
        self.dry_run = dry_run
        self._emit = emit
        self.vars = dict(inputs)
        self.vars["run"] = {"name": flow["name"], "host": flow.get("host")}
        self.steps = []
        self.artifacts = []
        self.collected = {}
        self.executed = 0
        self.started = time.time()
        self.aborted = None

    def emit(self, event):
        self.steps.append(event)
        if self._emit:
            self._emit(event)
        return event

    def record(self, op, status, **extra):
        return self.emit(dict(op=op, status=status, **extra))

    def scope(self, extra=None):
        return dict(self.vars, **(extra or {})) if extra else dict(self.vars)


def _fatal(exc):
    return isinstance(exc, browser_mod.BrowserError) and exc.step_fatal


def _host_of(url):
    try:
        return urlparse(url).hostname or ""
    except ValueError:
        return ""


class Runner:
    def __init__(self, browser, *, graph_path=None, store=None, sleep=time.sleep):
        self.browser = browser
        self.graph_path = graph_path
        self.store = store
        self.sleep = sleep          # injectable so tests don't actually wait

    # --- entry point ------------------------------------------------------------

    def run(self, flow, inputs=None, *, emit=None, dry_run=False, grant=None):
        """Execute a validated flow.

        `grant` is the CALLER's capability grant. The effective capability is the AND of what
        the flow declares and what the caller allows — a flow that declares allow_submit is
        still not permitted to submit unless the person/scheduler triggering it says so too.
        That is what makes it safe to expose a flow as an HTTP endpoint."""
        flow_mod.validate(flow)
        bound = flow_mod.bind_inputs(flow, inputs)
        declared = flow_mod.capabilities(flow)
        grant = flow_mod.DEFAULT_CAPABILITIES if grant is None else dict(
            flow_mod.DEFAULT_CAPABILITIES, **grant)
        caps = {k: bool(declared.get(k) and grant.get(k)) for k in flow_mod.DEFAULT_CAPABILITIES}

        run = Run(flow, bound, caps, emit=emit, dry_run=dry_run)
        run.record("run", "started", flow=flow["name"], host=flow.get("host"),
                   dry_run=dry_run, capabilities=caps)
        try:
            self._exec_steps(flow["steps"], run)
        except _Abort as stop:
            run.aborted = str(stop)

        status = "aborted" if run.aborted else (
            "error" if any(s.get("status") == "error" for s in run.steps) else "ok")
        summary = {
            "status": status,
            "flow": flow["name"],
            "dry_run": dry_run,
            "aborted": run.aborted,
            "duration_s": round(time.time() - run.started, 2),
            "steps": run.steps,
            "artifacts": run.artifacts,
            "collected": run.collected,
            "stats": {
                "steps_executed": run.executed,
                "artifacts_new": sum(1 for a in run.artifacts if a.get("status") == "new"),
                "artifacts_dupe": sum(1 for a in run.artifacts if a.get("status") == "dupe"),
            },
        }
        run.record("run", status, stats=summary["stats"])
        return summary

    # --- the interpreter --------------------------------------------------------

    def _exec_steps(self, steps, run, extra_scope=None):
        for step in steps:
            run.executed += 1
            if run.executed > MAX_TOTAL_STEPS:
                raise _Abort("exceeded %d executed steps — runaway flow" % MAX_TOTAL_STEPS)
            self._exec_step(step, run, extra_scope)

    def _exec_step(self, step, run, extra_scope=None):
        op = step["op"]
        scope = run.scope(extra_scope)
        try:
            resolved = flow_mod.substitute(
                {k: v for k, v in step.items() if k != "body"}, scope)
        except flow_mod.FlowError as exc:
            run.record(op, "error", error=str(exc))
            raise _Abort(str(exc))

        handler = getattr(self, "_op_" + op)
        try:
            handler(step, resolved, run, extra_scope)
        except _Abort:
            raise
        except browser_mod.BrowserError as exc:
            run.record(op, "error", error=str(exc), **_target(resolved))
            if _fatal(exc):
                raise _Abort("a %s step failed; the page is not where later steps expect"
                             % op)
        except Exception as exc:                              # noqa: BLE001 - reported, not swallowed
            run.record(op, "error", error="%s: %s" % (type(exc).__name__, exc),
                       **_target(resolved))

    # --- navigation -------------------------------------------------------------

    def _op_goto(self, step, r, run, _extra):
        if r.get("url"):
            self._guard_host(r["url"], run)
            if run.dry_run:
                return run.record("goto", "dry-run", url=r["url"])
            self.browser.nav(r["url"])
            return run.record("goto", "ok", url=r["url"])

        plan = self._resolve(r.get("goal"), r, run)
        if run.dry_run:
            return run.record("goto", "dry-run", goal=r.get("goal"),
                              target=plan["trigger_label"], clicks=len(plan["path_steps"]))
        # navigate the path only — do NOT click the trigger. `goto` positions; `do` acts.
        self._walk(plan["start_url"], plan["path_steps"])
        run.record("goto", "ok", goal=r.get("goal"), target=plan["trigger_label"],
                   clicks=len(plan["path_steps"]))

    def _walk(self, start_url, path_steps):
        for cstep in commands._path_steps(start_url, path_steps):
            argv = cstep["argv"]
            if not argv:
                continue
            if argv[0] == "nav":
                self.browser.nav(argv[1])
            elif argv[0] == "click":
                self.browser.click(argv[2])

    def _resolve(self, goal, r, run):
        if not self.graph_path:
            raise _Abort("this flow resolves a goal (%r) but no graph was supplied — crawl "
                         "the host first, or use an explicit url/selector" % goal)
        plan = api.resolve_action(self.graph_path, goal=goal, start=r.get("start"),
                                  match=r.get("match"), index=r.get("index", 0))
        if plan.get("status") != "ok":
            raise _Abort("could not resolve %r against the graph: %s%s"
                         % (goal or r.get("match"), plan.get("status"),
                            (" (candidates: %s)" % ", ".join(plan["candidates"][:5]))
                            if plan.get("candidates") else ""))
        return plan

    def _guard_host(self, url, run):
        """A flow declares its `host`; a url step that leaves it is refused. Without this a
        saved flow is an open redirect the moment it takes a `${input}` in a url."""
        host = run.flow.get("host")
        target = _host_of(url)
        if host and target and not (target == host or fnmatch.fnmatch(target, host)):
            raise _Abort("step navigates to %r but the flow declares host %r" % (target, host))

    # --- direct element ops ------------------------------------------------------

    def _op_click(self, step, r, run, _extra):
        sel = r.get("selector") or self._selector_for_text(r.get("text"), run)
        if run.dry_run:
            return run.record("click", "dry-run", selector=sel, text=r.get("text"))
        self.browser.click(sel)
        run.record("click", "ok", selector=sel, text=r.get("text"))

    def _selector_for_text(self, text, run):
        if run.dry_run:
            return "<by text: %s>" % text
        hits = self.browser.query({"label": _literal_re(text), "limit": 1})
        if not hits:
            raise _Abort("no control found matching text %r" % text)
        return hits[0]["selector"]

    def _op_fill(self, step, r, run, _extra):
        sel = self._field_selector(r, run)
        if run.dry_run:
            return run.record("fill", "dry-run", selector=sel, value=r["value"])
        self.browser.fill(sel, r["value"])
        run.record("fill", "ok", selector=sel, label=r.get("label"))

    def _op_select(self, step, r, run, _extra):
        sel = self._field_selector(r, run)
        if run.dry_run:
            return run.record("select", "dry-run", selector=sel, value=r["value"])
        self.browser.select(sel, r["value"])
        run.record("select", "ok", selector=sel, label=r.get("label"))

    def _op_check(self, step, r, run, _extra):
        sel = self._field_selector(r, run)
        if run.dry_run:
            return run.record("check", "dry-run", selector=sel)
        self.browser.check(sel)
        run.record("check", "ok", selector=sel, label=r.get("label"))

    def _op_upload(self, step, r, run, _extra):
        if not run.caps["allow_upload"]:
            return run.record("upload", "skipped",
                              reason="upload not permitted (capabilities.allow_upload)")
        if run.dry_run:
            return run.record("upload", "dry-run", selector=r["selector"], file=r["file"])
        self.browser.upload(r["selector"], r["file"])
        run.record("upload", "ok", selector=r["selector"], file=r["file"])

    def _field_selector(self, r, run):
        if r.get("selector"):
            return r["selector"]
        return self._selector_for_text(r.get("label"), run)

    # --- download ----------------------------------------------------------------

    def _op_download(self, step, r, run, _extra):
        if not run.caps["allow_download"]:
            return run.record("download", "skipped",
                              reason="download not permitted (capabilities.allow_download)")
        href, sel = r.get("href"), r.get("selector")
        name = r.get("name") or commands.suggest_filename(href=href, label=r.get("label"))
        dedupe = r.get("dedupe", "content-hash") != "none"

        if run.dry_run:
            return run.record("download", "dry-run", href=href, selector=sel, name=name)

        if href:
            if not self.store:
                via = self._fetch_file(href, name)
                return run.record("download", "ok", href=href, name=name, via=via)
            staged = self.store.staging_path(name)
            via = self._fetch_file(href, staged)
            rec = self.store.accept(staged, name=name, source=href, dedupe=dedupe)
            run.artifacts.append(rec)
            return run.record("download", rec["status"], href=href, name=name, via=via,
                              sha256=rec["sha256"], path=rec.get("path"), size=rec.get("size"))

        # A JS-triggered download (no href): the page fabricates the file, and the browser
        # session captures it wherever its download dir points. We can click it, but we
        # cannot hash what we never touched — so it is reported honestly as `triggered`,
        # NOT as a deduped artifact. Direct-href downloads are the ones the store can vouch for.
        self.browser.click(sel)
        run.record("download", "triggered", selector=sel,
                   note="JS-triggered — captured by the browser session, not hashed here")

    def _fetch_file(self, href, out_path):
        """Get a file to `out_path`, and say HOW. Two strategies, in this order:

          1. `save_bytes` — fetch inside the page. Inherits the session's cookies (so an
             authenticated app works), is not subject to the CLI's SSRF/allowlist refusal (so
             a local app works), and hands us the real bytes to hash. Same-origin only.
          2. `download`  — the pinchtab CLI. The fallback for a CROSS-ORIGIN href (a CDN
             link), which the in-page fetch cannot read.

        If both fail the BrowserError propagates and `_exec_step` records the step as error."""
        try:
            self.browser.save_bytes(href, out_path)
            return "fetch"
        except browser_mod.BrowserError:
            self.browser.download(href, out_path)
            return "cli"

    # --- form action (a whole how-to as one step) ---------------------------------

    def _op_do(self, step, r, run, _extra):
        plan = self._resolve(r.get("goal"), r, run)
        want_submit = bool(r.get("submit"))
        if want_submit and not run.caps["allow_submit"]:
            return run.record("do", "skipped", goal=r.get("goal"),
                              reason="submit not permitted (capabilities.allow_submit)")

        cmd_steps = commands.steps_for_trigger(
            plan["trigger"], plan["path_steps"], plan["start_url"],
            allow_submit=want_submit and run.caps["allow_submit"])
        values = {str(k).lower(): v for k, v in (r.get("set") or {}).items()}

        if run.dry_run:
            return run.record("do", "dry-run", goal=r.get("goal"),
                              target=plan["trigger_label"], action_kind=plan["action_kind"],
                              commands=commands.render(cmd_steps))

        filled, skipped = [], []
        for cs in cmd_steps:
            outcome = self._exec_command_step(cs, values, r.get("file"), run)
            if outcome == "skipped":
                skipped.append(cs.get("label"))
            elif outcome and cs.get("label"):
                filled.append(cs["label"])
        run.record("do", "ok", goal=r.get("goal"), target=plan["trigger_label"],
                   action_kind=plan["action_kind"], filled=filled,
                   skipped=[s for s in skipped if s], submitted=want_submit)

    def _exec_command_step(self, cs, values, upload_file, run):
        """Execute one of commands.py's structured steps through the browser port.

        Reuses the exact same step list the printed command block and `perform` use, so what
        a how-to SHOWS you and what a flow RUNS can never drift apart."""
        argv = cs["argv"]
        if argv is None:                      # note/blank — display only
            return None
        if cs.get("disabled"):                # the gated submit
            return "skipped"
        role = cs.get("role")
        if role == "download" and not run.caps["allow_download"]:
            return "skipped"          # `do` reuses this block, so it must honour the grant too

        if cs.get("needs_input"):
            if role == "upload":
                if not upload_file or not run.caps["allow_upload"]:
                    return "skipped"
                argv = list(argv)
                argv[cs["value_index"]] = upload_file
            else:
                val = values.get((cs.get("label") or "").lower())
                if val is None:
                    return "skipped"          # never type placeholder junk into a real form
                if cs["value_index"] is None:
                    # a checkbox: `check <sel>` has no value slot, so the supplied value is a
                    # BOOLEAN — check when truthy, leave alone when falsy. Substituting would
                    # be argv[None] → TypeError.
                    if not commands.is_truthy(val):
                        return "skipped"
                else:
                    argv = list(argv)
                    argv[cs["value_index"]] = str(val)

        head = argv[0]
        if head == "nav":
            self.browser.nav(argv[1])
        elif head == "click":
            self.browser.click(argv[2])
        elif head == "fill":
            self.browser.fill(argv[1], argv[2])
        elif head == "select":
            self.browser.select(argv[1], argv[2])
        elif head == "check":
            self.browser.check(argv[1])
        elif head == "upload":
            self.browser.upload(argv[3], argv[1])
        elif head == "download":
            self.browser.download(argv[1], argv[3] if len(argv) > 3 else argv[1])
        else:
            return None
        return "ok"

    # --- control flow: the reason this module exists -------------------------------

    def _op_for_each(self, step, r, run, extra):
        spec = dict(r["match"])
        spec.setdefault("limit", r.get("max") or DEFAULT_FOR_EACH_MAX)
        var = step.get("as", "item")

        if run.dry_run:
            run.record("for_each", "dry-run", match=spec,
                       note="body previewed once with a placeholder item")
            placeholder = {"selector": "<item.selector>", "text": "<item.text>",
                           "href": "<item.href>", "kind": spec.get("kind") or "control",
                           "index": 0}
            return self._exec_steps(step["body"], run,
                                    dict(extra or {}, **{var: placeholder, "index": 0}))

        items = self.browser.query(spec)
        run.record("for_each", "ok", match=spec, found=len(items))
        if not items:
            return

        for i, item in enumerate(items[:spec["limit"]]):
            item = dict(item, index=i)
            scope = dict(extra or {}, **{var: item, "index": i})
            self._exec_steps(step["body"], run, scope)

    def _op_paginate(self, step, r, run, extra):
        max_pages = int(r.get("max_pages") or DEFAULT_MAX_PAGES)

        if run.dry_run:
            run.record("paginate", "dry-run", max_pages=max_pages,
                       note="body previewed once (page 1)")
            return self._exec_steps(step["body"], run, dict(extra or {}, page=1))

        seen_sigs = set()
        for page in range(1, max_pages + 1):
            run.record("paginate", "page", page=page, max_pages=max_pages)
            self._exec_steps(step["body"], run, dict(extra or {}, page=page))

            nxt = self.browser.next_page()
            if not nxt.get("found") or nxt.get("exhausted"):
                run.record("paginate", "ok", pages=page,
                           reason="exhausted" if nxt.get("found") else "no paginator found")
                return

            # No-progress guard: a paginator that doesn't change the content is a decoy (or
            # we're on the last page and "next" was never disabled). Without this the loop
            # burns its whole page budget re-downloading page 1.
            sig = self.browser.page_signature()
            if sig in seen_sigs:
                run.record("paginate", "ok", pages=page,
                           reason="content stopped changing — stopping rather than re-reading")
                return
            seen_sigs.add(sig)

            self.browser.click(nxt["selector"])
            self._settle()

        run.record("paginate", "ok", pages=max_pages, reason="hit max_pages")

    def _settle(self, ms=400):
        self.sleep(ms / 1000.0)

    # --- data + misc -----------------------------------------------------------------

    def _op_collect(self, step, r, run, _extra):
        into = r["into"]
        if run.dry_run:
            return run.record("collect", "dry-run", into=into)
        collections = self.browser.content()
        if r.get("kind"):
            collections = [c for c in collections if c.get("kind") == r["kind"]]
        bucket = run.collected.setdefault(into, [])
        for coll in collections:
            bucket.extend(coll.get("items") or [])
        run.vars[into] = bucket
        run.record("collect", "ok", into=into,
                   collections=len(collections), items=sum(
                       len(c.get("items") or []) for c in collections))

    def _op_wait(self, step, r, run, _extra):
        if r.get("ms") is not None:
            if run.dry_run:
                return run.record("wait", "dry-run", ms=r["ms"])
            self.sleep(float(r["ms"]) / 1000.0)
            return run.record("wait", "ok", ms=r["ms"])

        target = r.get("selector") or r.get("text")
        timeout_ms = int(r.get("timeout_ms") or DEFAULT_WAIT_TIMEOUT_MS)
        if run.dry_run:
            return run.record("wait", "dry-run", target=target, timeout_ms=timeout_ms)

        spec = ({"selector": r["selector"], "limit": 1} if r.get("selector")
                else {"label": _literal_re(r["text"]), "limit": 1})
        deadline = time.time() + timeout_ms / 1000.0
        while time.time() < deadline:
            if self.browser.query(spec):
                return run.record("wait", "ok", target=target)
            self.sleep(0.2)
        run.record("wait", "error", target=target,
                   error="still not present after %dms" % timeout_ms)

    def _op_set(self, step, r, run, _extra):
        run.vars[r["var"]] = r["value"]
        run.record("set", "ok", var=r["var"], value=r["value"])

    def _op_log(self, step, r, run, _extra):
        run.record("log", "ok", message=str(r["message"]))


class _Abort(Exception):
    """Stop the flow. Raised for conditions where continuing would act on the wrong page."""


def _literal_re(text):
    """A regex that matches this exact text literally (QUERY_JS takes a regex for `label`)."""
    import re
    return re.escape(str(text or ""))


def _target(resolved):
    """The bits of a step worth putting on an error event."""
    keep = ("selector", "href", "url", "goal", "label", "text")
    return {k: resolved[k] for k in keep if resolved.get(k)}


# --- convenience ------------------------------------------------------------------

def execute(flow, *, browser, graph_path=None, store=None, inputs=None, emit=None,
            dry_run=False, grant=None, sleep=time.sleep):
    """Run a flow. The one call a CLI, a worker, or an HTTP handler needs."""
    return Runner(browser, graph_path=graph_path, store=store, sleep=sleep).run(
        flow, inputs=inputs, emit=emit, dry_run=dry_run, grant=grant)
