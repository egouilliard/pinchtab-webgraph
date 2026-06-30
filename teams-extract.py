#!/usr/bin/env python3
"""Teams content extractor — APP-SPECIFIC (like hard-bench.sh): Teams selectors are
allowed here, NOT in the generic tool code (recipe.py / crawl.py / interaction_crawl.py).

It demonstrates the single-URL drive primitives (no nav; JS-dispatch clicks) on a
single-URL app-shell and dumps, for MS Teams:
  - every left-rail VIEW's full control inventory (links / buttons / tabs / menuitems);
  - the global de-duplicated control union ("all the links, buttons and everything");
  - every CONVERSATION in Chat with its latest N messages (default 5).

SAFETY: it only ever CLICKS (a) rail views and (b) conversation rows. It NEVER clicks
create/destructive/stateful controls (it just records them). Opening a conversation
marks it READ — that is an accepted side effect of "pull all conversations".

Usage:
  PINCHTAB_TOKEN auto-loaded from --config.
  python3 teams-extract.py --server http://localhost:9881 --config teams-config.json \
          --start https://teams.cloud.microsoft/ [--messages 5] [--max-convos 0] \
          [--out teams-content]
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import recipe
from interaction_crawl import click_js

# ---- generic browser helpers (thin wrappers over recipe primitives) ----

def J(server, expr):
    """Eval `expr`, JSON-stringify in-page, parse here. Tolerates pinchtab's
    occasional double-encoding of string results."""
    out = recipe.pt(["eval", "JSON.stringify(%s)" % expr], server)[1]
    try:
        v = json.loads(out)
    except ValueError:
        return None
    if isinstance(v, str):          # pinchtab may double-encode; a genuine string value
        try:                        # (e.g. a chat title) is NOT re-parseable → return as-is
            return json.loads(v)
        except ValueError:
            return v
    return v


def controls_now(server):
    """Full control inventory of the current live view (generic CONTROLS_JS)."""
    return J(server, recipe.CONTROLS_JS) or []


# ---- Teams-specific DOM knowledge (allowed in this app harness only) ----

# Left-rail nav buttons carry an aria-label like "Chat (Ctrl+Shift+2)".
RAIL_JS = (r"""[...document.querySelectorAll('button,[role=tab]')]"""
           r""".map(e=>(e.getAttribute('aria-label')||'').trim())"""
           r""".filter(l=>/\(Ctrl\+Shift\+\d\)/.test(l))""")

# Conversation rows are LEAF treeitems (the list nests treeitems; group/section
# headers are non-leaf containers). A few fixed "quick view" rows aren't chats.
QUICKVIEWS = ("Copilot", "Mentions", "Drafts", "Favorites", "Quick views")

CONV_NAMES_JS = (r"""[...document.querySelectorAll('[role=treeitem]')]"""
                 r""".filter(e=>!e.querySelector('[role=treeitem]'))"""
                 r""".map(e=>(e.getAttribute('aria-label')||e.innerText||'')"""
                 r""".replace(/\s+/g,' ').trim()).filter(Boolean)""")

# Each chat-list row label packs name + date + latest-message preview, e.g.
# "Youssef BENNANI 6/25 You: ah top merci !". Split it WITHOUT opening the chat
# (zero side effects — nothing gets marked read).
import re
_ROW_RE = re.compile(r"^(?P<name>.*?)\s+(?P<date>\d{1,2}/\d{1,2}(?:/\d{2,4})?)\s+(?P<preview>.*)$")


def parse_row(label):
    m = _ROW_RE.match(label)
    if m:
        return {"name": m.group("name").strip(), "date": m.group("date"),
                "lastPreview": m.group("preview").strip(), "raw": label}
    return {"name": label, "date": None, "lastPreview": None, "raw": label}


def click_rail(server, label):
    return recipe.pt(["eval",
        "(()=>{const l=%s;const b=[...document.querySelectorAll('button,[role=tab]')]"
        ".find(e=>(e.getAttribute('aria-label')||'').trim()===l);"
        "if(!b)return'none';b.click();return'ok';})()" % json.dumps(label)], server)[1]


def open_conversation(server, raw_label):
    # best-effort: leaf treeitem whose label matches, synthetic .click (works for some
    # chat types; 1:1 person chats often don't activate this way — see --open-chats help)
    return recipe.pt(["eval",
        "(()=>{const n=%s;const L=[...document.querySelectorAll('[role=treeitem]')]"
        ".filter(e=>!e.querySelector('[role=treeitem]'));"
        "const t=L.find(e=>((e.getAttribute('aria-label')||e.innerText||'')"
        ".replace(/\\s+/g,' ').trim())===n);if(!t)return'notfound';"
        "t.scrollIntoView({block:'center'});t.click();return'ok';})()"
        % json.dumps(raw_label)], server)[1]


def goto_rail(server, prefix):
    return recipe.pt(["eval",
        "(()=>{const b=[...document.querySelectorAll('button,[role=tab]')]"
        ".find(e=>(e.getAttribute('aria-label')||'').startsWith(%s));if(!b)return'none';"
        "b.click();return'ok';})()" % json.dumps(prefix)], server)[1]


def expand_all_teams(server):
    # click every collapsed team group (aria-expanded=false), except the Quick-views group
    return recipe.pt(["eval",
        "(()=>{let n=0;[...document.querySelectorAll('[role=treeitem][aria-expanded=\"false\"]')]"
        ".forEach(g=>{if(!/quick views/i.test(g.getAttribute('aria-label')||'')){g.click();n++;}});"
        "return n;})()"], server)[1]


def list_channels(server):
    # Walk the Teams tree in order: a level-2 expandable row is a TEAM; the level>=3
    # leaf rows after it are its CHANNELS (channels, unlike 1:1 chats, activate on click).
    rows = J(server, r"""[...document.querySelectorAll('[role=treeitem]')].map(e=>({
      lvl:e.getAttribute('aria-level'), exp:e.getAttribute('aria-expanded'),
      label:(e.getAttribute('aria-label')||e.innerText||'').replace(/\s+/g,' ').trim()}))""") or []
    out, team = [], None
    for r in rows:
        lvl = int(r["lvl"]) if r["lvl"] else 0
        lab = r["label"]
        if r["exp"] is not None and lvl == 2:
            team = lab
        elif r["exp"] is None and lvl >= 3 and lab and not lab.startswith("See all"):
            out.append({"team": team, "channel": lab})
    return out


def open_treeitem(server, name):
    return recipe.pt(["eval",
        "(()=>{const t=[...document.querySelectorAll('[role=treeitem]')]"
        ".find(e=>((e.getAttribute('aria-label')||e.innerText||'').replace(/\\s+/g,' ').trim())"
        ".startsWith(%s));if(!t)return'notfound';t.scrollIntoView({block:'center'});"
        "t.click();return'ok';})()" % json.dumps(name)], server)[1]


def tabs_now(server):
    return J(server, r"""[...document.querySelectorAll('[role=tab]')]"""
                     r""".map(e=>(e.innerText||e.getAttribute('aria-label')||'').trim())"""
                     r""".filter(Boolean)""") or []


def last_messages(server, n):
    """Latest N messages of the open conversation: body text + epoch-ms id (data-mid)."""
    raw = J(server,
        r"""[...document.querySelectorAll('[data-tid="chat-pane-message"]')].slice(-%d)"""
        r""".map(e=>({mid:e.getAttribute('data-mid')||null,"""
        r"""text:(e.innerText||'').replace(/\s+/g,' ').trim().slice(0,800)}))""" % n) or []
    msgs = []
    for m in raw:
        ts = None
        try:
            ts = datetime.fromtimestamp(int(m["mid"]) / 1000.0).isoformat(timespec="minutes")
        except Exception:
            pass
        msgs.append({"time": ts, "text": m.get("text", "")})
    return msgs


def conv_title(server):
    return J(server, r"""(()=>{const t=document.querySelector('[data-tid="chat-title"]');"""
                     r"""return t?(t.innerText||'').replace(/\s+/g,' ').trim():null})()""")


def main():
    ap = argparse.ArgumentParser(description="Extract Teams views/controls + conversations")
    ap.add_argument("--server", default="http://localhost:9881")
    ap.add_argument("--config", default=os.path.join(os.path.dirname(__file__), "teams-config.json"))
    ap.add_argument("--start", default="https://teams.cloud.microsoft/")
    ap.add_argument("--messages", type=int, default=5, help="latest N messages per opened chat")
    ap.add_argument("--open-chats", type=int, default=0,
                    help="ALSO open up to N chats to pull --messages each (default 0 = list "
                         "previews only, no side effects). NOTE: opening marks a chat READ, and "
                         "Teams' 1:1 chat tree resists programmatic activation, so this is "
                         "best-effort and may fail per chat. For reliable full message history "
                         "use the Microsoft Graph API (/chats/{id}/messages).")
    ap.add_argument("--max-convos", type=int, default=0, help="cap conversations listed (0 = all)")
    ap.add_argument("--channels", dest="channels", action="store_true", default=True,
                    help="walk Teams → expand every team → open every channel and capture its "
                         "controls + tabs (default on; channels DO activate, unlike 1:1 chats)")
    ap.add_argument("--no-channels", dest="channels", action="store_false")
    ap.add_argument("--max-channels", type=int, default=0, help="cap channels opened (0 = all)")
    ap.add_argument("--out", default="teams-content")
    a = ap.parse_args()
    try:
        os.environ.setdefault("PINCHTAB_TOKEN", json.load(open(a.config))["server"]["token"])
    except Exception:
        pass
    S = a.server

    # sanity: live read must see controls (single-URL apps blank on nav, so we DON'T nav)
    live = controls_now(S)
    print("live controls: %d" % len(live), file=sys.stderr)
    if not live:
        print("ERROR: 0 controls on the live page — is the bridge signed in? (single-URL "
              "apps must already be loaded interactively; we never navigate)", file=sys.stderr)
        sys.exit(1)

    # ---- 1) walk every rail view, capture its full control inventory ----
    rail = J(S, RAIL_JS) or []
    print("rail views: %s" % ", ".join(r.split(" (")[0] for r in rail), file=sys.stderr)
    views, seen_ctrl, all_controls = [], set(), []
    for label in rail:
        if click_rail(S, label) != "ok":
            continue
        recipe.settle(S)
        ctrls = controls_now(S)
        views.append({"view": label.split(" (")[0], "railLabel": label,
                      "controlCount": len(ctrls), "controls": ctrls})
        for c in ctrls:
            k = (c.get("text"), c.get("selector"))
            if k not in seen_ctrl:
                seen_ctrl.add(k)
                all_controls.append(c)
        print("  · %-12s %d controls" % (label.split(' (')[0], len(ctrls)), file=sys.stderr)

    # ---- 2) Chat view → ALL conversations (safe: parsed from the list, NO opening) ----
    click_rail(S, next((r for r in rail if r.startswith("Chat ")), "Chat"))
    time.sleep(1); recipe.settle(S)
    labels = [n for n in (J(S, CONV_NAMES_JS) or [])
              if not any(n.startswith(q) for q in QUICKVIEWS)]
    labels = list(dict.fromkeys(labels))                # de-dupe, preserve order
    if a.max_convos:
        labels = labels[:a.max_convos]
    conversations = [parse_row(l) for l in labels]
    print("conversations listed: %d (latest-message preview, no side effects)"
          % len(conversations), file=sys.stderr)

    # ---- optional best-effort: open up to N chats for the latest N messages ----
    opened_ok = 0
    if a.open_chats:
        print("opening up to %d chats (best-effort; marks them read)…" % a.open_chats,
              file=sys.stderr)
        for conv in conversations[:a.open_chats]:
            if open_conversation(S, conv["raw"]) == "ok":
                time.sleep(1.2); recipe.settle(S)
                msgs = last_messages(S, a.messages)
                if msgs:
                    conv["messages"] = msgs
                    opened_ok += 1
            conv.setdefault("messages", [])
            print("  %-28s %d msgs" % (conv["name"][:28], len(conv.get("messages", []))),
                  file=sys.stderr)

    # ---- 3) Teams → expand every team, open every channel, capture controls + tabs ----
    channels = []
    if a.channels:
        goto_rail(S, "Teams (")
        time.sleep(1.5); recipe.settle(S)
        ne = expand_all_teams(S)
        time.sleep(1.5); recipe.settle(S)
        chans = list_channels(S)
        if a.max_channels:
            chans = chans[:a.max_channels]
        print("teams expanded: %s · channels to open: %d" % (ne, len(chans)), file=sys.stderr)
        for i, ch in enumerate(chans, 1):
            rec = {"team": ch["team"], "channel": ch["channel"],
                   "opened": False, "controlCount": 0, "tabs": []}
            if open_treeitem(S, ch["channel"]) == "ok":
                time.sleep(1.2); recipe.settle(S)
                cc = controls_now(S)
                rec.update(opened=True, controlCount=len(cc), tabs=tabs_now(S))
                for c in cc:                       # fold channel controls into the global union
                    k = (c.get("text"), c.get("selector"))
                    if k not in seen_ctrl:
                        seen_ctrl.add(k)
                        all_controls.append(c)
            channels.append(rec)
            print("  [%d/%d] %-16s / %-26s %d ctrls" % (i, len(chans),
                  (ch["team"] or "?")[:16], ch["channel"][:26], rec["controlCount"]), file=sys.stderr)

    out = {
        "extractedAt": datetime.now().isoformat(timespec="seconds"),
        "host": "teams.cloud.microsoft",
        "summary": {"views": len(views), "uniqueControls": len(all_controls),
                    "conversations": len(conversations),
                    "chatsOpenedForMessages": opened_ok,
                    "channels": len(channels),
                    "channelsOpened": sum(1 for c in channels if c["opened"])},
        "views": views,
        "allControls": all_controls,
        "conversations": conversations,
        "channels": channels,
    }
    path = os.path.join(os.path.dirname(__file__), a.out + ".json")
    json.dump(out, open(path, "w"), indent=2, ensure_ascii=False)
    print("\nWrote %s" % path, file=sys.stderr)
    print("  %d views · %d unique controls · %d conversations · %d/%d channels opened"
          % (out["summary"]["views"], out["summary"]["uniqueControls"],
             out["summary"]["conversations"], out["summary"]["channelsOpened"],
             out["summary"]["channels"]), file=sys.stderr)


if __name__ == "__main__":
    main()
