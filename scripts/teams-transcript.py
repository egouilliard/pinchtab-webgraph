#!/usr/bin/env python3
"""Fetch a Teams meeting's transcript document once it exists — polling, with a give-up.

    scripts/teams-transcript.py --subject "Quarterly review" --date 2026-10-13
    scripts/teams-transcript.py --subject "Weekly sync" --every 300 --tries 12 --format vtt

How: a recorded meeting's transcript is attached to its recording, a video in the
organizer's OneDrive. Inside a signed-in OneDrive tab (the Teams bridge profile) this
searches the drive for a video whose name starts with the meeting subject (and, with
--date, was recorded that day), lists the recording's transcripts and downloads one as
.docx or .vtt — all same-origin requests, so the page's own session authorises them. No
recording or no transcript yet → wait --every seconds and retry, up to --tries times.

Prints one JSON object. Exit codes: 0 downloaded · 3 gave up (not found after every try)
· 1 error (bridge, auth). Limits: only recordings in YOUR OneDrive (meetings you
organized and recorded); a transcript-only meeting (no recording) is not stored there.

Run from the repo root (it imports pinchtab_webgraph). Bridge config: --config or
$TEAMS_BRIDGE_CONFIG (default ./teams-config.json, gitignored, holds the token).
"""
import argparse
import json
import os
import re
import sys
import time
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pinchtab_webgraph import browser as browser_mod  # noqa: E402

FETCH_JSON_JS = r"""
(async (u) => {
  const r = await fetch(u, {credentials: 'include', headers: {Accept: 'application/json'}});
  if (!r.ok) return {error: 'HTTP ' + r.status};
  return await r.json();
})(%s)
"""


def fetch_json(b, path):
    return b.evaluate(FETCH_JSON_JS % json.dumps(path), await_promise=True, timeout=60) or {}


def find_recording(b, subject, date):
    q = quote("'" + subject.replace("'", "''") + "'")
    res = fetch_json(b, "/_api/v2.0/me/drive/root/search(q=%s)?$top=50" % q)
    if res.get("error"):
        raise RuntimeError("OneDrive search failed: %s" % res["error"])
    want = subject.lower()
    hits = []
    for it in res.get("value", []):
        name = it.get("name") or ""
        mime = (it.get("file") or {}).get("mimeType") or ""
        if not mime.startswith("video/") or not name.lower().startswith(want):
            continue
        created = it.get("createdDateTime") or ""
        if date and not (created.startswith(date) or date.replace("-", "") in name):
            continue
        drive = (it.get("parentReference") or {}).get("driveId")
        if drive and it.get("id"):
            hits.append({"name": name, "created": created, "drive": drive, "id": it["id"]})
    hits.sort(key=lambda h: h["created"], reverse=True)       # newest recording first
    return hits[0] if hits else None


def transcripts_path(rec):
    return "/_api/v2.1/drives/%s/items/%s/media/transcripts" % (rec["drive"], rec["id"])


def find_transcript(b, rec):
    res = fetch_json(b, transcripts_path(rec))
    if res.get("error"):
        return None                     # the item can exist before its transcript does
    items = res.get("value", [])
    items.sort(key=lambda t: (not t.get("isDefault"), t.get("languageTag") or ""))
    return items[0] if items else None


def safe_name(s):
    return re.sub(r"[^A-Za-z0-9._ -]+", "_", s).strip()[:120] or "transcript"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--subject", required=True, help="the meeting title, as it starts the recording's name")
    ap.add_argument("--date", default="", help="meeting day YYYY-MM-DD (recommended: picks the right occurrence)")
    ap.add_argument("--format", choices=("docx", "vtt"), default="docx")
    ap.add_argument("--out", default="transcripts", help="directory to save into")
    ap.add_argument("--every", type=float, default=300, help="seconds between tries (default 300)")
    ap.add_argument("--tries", type=int, default=12, help="give up after this many tries (default 12)")
    ap.add_argument("--onedrive", default=os.environ.get("TEAMS_ONEDRIVE", ""),
                    help="your OneDrive host root, e.g. https://<tenant>-my.sharepoint.com/ ($TEAMS_ONEDRIVE)")
    ap.add_argument("--config", default=os.environ.get("TEAMS_BRIDGE_CONFIG", "teams-config.json"))
    a = ap.parse_args(argv)
    if not a.onedrive:
        ap.error("--onedrive (or $TEAMS_ONEDRIVE) is required: https://<tenant>-my.sharepoint.com/")
    if a.date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", a.date):
        ap.error("--date must be YYYY-MM-DD")

    cfg = json.load(open(a.config))
    b = browser_mod.PinchTabBrowser("http://localhost:%s" % cfg["server"]["port"],
                                    cfg["server"]["token"], None, timeout=90)
    result = {"status": "not_found", "subject": a.subject, "date": a.date or None, "tries": 0}
    try:
        b.open_tab(a.onedrive)          # its own tab; closed below whatever happens
        time.sleep(8)                   # let SharePoint settle the session cookies
        for attempt in range(1, a.tries + 1):
            result["tries"] = attempt
            rec = find_recording(b, a.subject, a.date)
            tr = find_transcript(b, rec) if rec else None
            if tr:
                url = "%s/%s/streamContent?format=%s" % (transcripts_path(rec), tr["id"], a.format)
                os.makedirs(a.out, exist_ok=True)
                day = a.date or (rec["created"] or "")[:10]
                path = os.path.abspath(os.path.join(
                    a.out, "%s %s.%s" % (safe_name(a.subject), day, a.format)))
                b.save_bytes(url, path)
                result.update(status="downloaded", path=path, bytes=os.path.getsize(path),
                              recording=rec["name"], language=tr.get("languageTag"))
                break
            result["last"] = "no transcript on the recording yet" if rec else "no recording found yet"
            print("try %d/%d: %s" % (attempt, a.tries, result["last"]), file=sys.stderr)
            if attempt < a.tries:
                time.sleep(a.every)
        else:
            result["message"] = "gave up after %d tries: %s" % (a.tries, result.get("last"))
    except (browser_mod.BrowserError, RuntimeError, OSError, ValueError) as exc:
        result.update(status="error", error=str(exc))
    finally:
        b.close_opened_tabs()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return {"downloaded": 0, "not_found": 3}.get(result["status"], 1)


if __name__ == "__main__":
    sys.exit(main())
