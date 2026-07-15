#!/usr/bin/env python3
"""Does this flow's `goal` actually name anything on the crawled site?

`flow.validate` proves a document's SHAPE (ops, required keys, `${var}` references,
declared capabilities) and nothing else — by design, because it must stay pure enough to run
before a browser or a graph is ever touched. The consequence, found by a real-browser e2e, is
the feature's sharpest papercut: a flow whose `goto` step says `{"goal": "reports"}` on a site
whose only trigger is “Add Report” validates GREEN, saves, runs, and only THEN aborts with
`could not resolve 'reports' against the graph: no_match`.

This module closes that gap. It is the ONE place that answers the question `validate` can't:

    flow.goal_targets(doc)   (pure: WHICH steps resolve, and WHERE)
        + api.resolve_action (the graph read — the SAME resolver runner.py runs)
        = warnings

They are WARNINGS, never errors. A flow may legitimately be authored before the host is
crawled, or against a stale graph, so "not crawled" must never become "not savable":

    * no `host`, or a host with no cache on disk -> NO warnings (and no error).
    * a `goal` that is `${…}`-substituted at run time -> not checkable now, so not warned.
    * anything else that does not resolve -> a warning carrying the exact step `path`
      (flow.py's grammar, so the canvas can light up the offending box) and, when we can
      find them, the CANDIDATE labels the site actually has — "did you mean “Add Report”?"
      is the whole value of the check.

Kept OUT of flow.py (which promises no I/O) and OUT of `mcp_server.propose_flow` (which is
provably pure — no disk, no browser, no subprocess, and a test poisons `open`/`os.replace`/
`subprocess` to keep it that way). The agent verifies goals with the `howto` tool; the human
gets these warnings from POST /api/flows/validate, which the editor calls on every change.
"""
import difflib
import json
import os
import re

from . import api, cache_store, flow as flow_mod

MAX_CANDIDATES = 5


def graph_path_for(doc):
    """The cached interaction graph for the flow's declared `host` — or None.

    None (rather than an exception) for every "we cannot check this" case: no host, a host
    token that isn't hostname-shaped, or a host that has never been crawled. The caller turns
    None into "no warnings", never into an error."""
    host = (doc or {}).get("host") if isinstance(doc, dict) else None
    if not isinstance(host, str) or not host.strip():
        return None
    try:
        path = cache_store.cache_path(host.strip())
    except ValueError:                    # not a hostname token — the CRUD routes already 400
        return None
    return path if os.path.exists(path) else None


def warnings_for_doc(doc):
    """Resolvability warnings for a flow document, against ITS host's cached graph.

    The entry point the HTTP validator calls. Empty list when there is nothing to check
    against — an uncrawled host is not a broken flow."""
    graph_path = graph_path_for(doc)
    if graph_path is None:
        return []
    return warnings_against_graph(doc, graph_path, host=(doc.get("host") or "").strip())


def warnings_against_graph(doc, graph_path, host=None):
    """The same check against an EXPLICIT graph path (what the tests drive, and what a future
    'validate against this crawl' surface would use)."""
    out = []
    for target in flow_mod.goal_targets(doc):
        warning = _check_target(target, graph_path, host)
        if warning is not None:
            out.append(warning)
    return out


def _check_target(target, graph_path, host):
    goal, match = target["goal"], target["match"]
    try:
        plan = api.resolve_action(graph_path, goal=goal, start=target["start"],
                                  match=match, index=target["index"])
    except (OSError, ValueError, KeyError, TypeError, re.error):
        # A malformed graph, or a `match` that isn't a legal regex. Neither is worth turning a
        # VALIDATION request into a 500: the run will surface it loudly, and the document
        # itself is still structurally fine. Silence beats a false alarm here.
        return None

    status = plan.get("status")
    if status in ("ok", "invalid_args"):        # invalid_args can't happen (goal_targets filters)
        return None

    needle = goal or match or ""
    where = "the %s graph" % host if host else "the crawled graph"
    if status == "unreachable":
        message = "“%s” matches a trigger, but no click-path in %s reaches it" % (needle, where)
    elif plan.get("detail"):                    # e.g. `index` out of range for the matches
        message = "%s (%s)" % (plan["detail"], where)
    else:
        message = "no trigger matches “%s” in %s" % (needle, where)

    candidates = [c for c in (plan.get("candidates") or []) if isinstance(c, str)]
    if not candidates:
        # resolve_action only returns candidates for `unreachable`. A plain no_match — the
        # common case, and the one the user most needs help with — gets them from the graph's
        # own trigger labels, which is exactly the "did you mean …?" the papercut wants.
        candidates = suggest(graph_path, needle)
    return {"path": target["path"], "op": target["op"], "goal": goal, "match": match,
            "message": message, "candidates": candidates[:MAX_CANDIDATES]}


# --- "did you mean …?" ---------------------------------------------------------
# Deliberately dumb and deterministic (no LLM, no fuzz beyond stdlib difflib): the goal is to
# name real controls that EXIST on the site, so a wrong-but-plausible suggestion is worse than
# none. Token overlap first (it catches the plural/singular miss — "reports" vs “Add Report”),
# a difflib near-match only as a fallback.

_STOP = {"the", "a", "an", "and", "for", "new", "add", "all", "any", "with", "from"}


def _stem(word):
    word = word.lower()
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


def _tokens(text):
    return {_stem(t) for t in re.findall(r"[A-Za-z0-9]+", text or "")
            if len(t) > 2 and t.lower() not in _STOP}


def _trigger_labels(graph_path):
    try:
        with open(os.path.expanduser(str(graph_path))) as fh:
            graph = json.load(fh)
    except (OSError, ValueError):
        return []
    labels = []
    for trigger in (graph.get("triggers") or []):
        label = trigger.get("label") if isinstance(trigger, dict) else None
        if isinstance(label, str) and label.strip() and label not in labels:
            labels.append(label)
    return labels


def suggest(graph_path, needle, limit=MAX_CANDIDATES):
    """Trigger labels the site actually has that plausibly answer `needle`."""
    labels = _trigger_labels(graph_path)
    if not labels:
        return []
    wanted = _tokens(needle)
    hits = [lab for lab in labels if wanted & _tokens(lab)] if wanted else []
    if not hits:
        lowered = {lab.lower(): lab for lab in labels}
        hits = [lowered[m] for m in difflib.get_close_matches(
            str(needle or "").lower(), list(lowered), n=limit, cutoff=0.6)]
    return hits[:limit]
