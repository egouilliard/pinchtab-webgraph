#!/usr/bin/env bash
# ============================================================================
# 10-site regression audit runner (Phase 0).  Crawls every public site in
# tests/audit/sites.json into a graph, then scores them browser-free via
# tests/audit/check.py.  This is the living gate for the extraction-quality
# improvement plan: dup_ratio==0 (Phase 1) today, rising to 50/50 by Phase 6.
#
# ⚠ MUST run on the HOST with a reachable pinchtab bridge — NOT inside the agent
#   Bash sandbox (its localhost is isolated; a host-bound bridge is unreachable).
#   Starting the bridge from an agent needs run_in_background + sandbox-off.
# ⚠ Per site it WIPES the Chrome profile AND pinchtab stateDir to defeat the
#   tab-restore cross-crawl contamination (see references/gotchas.md). Never
#   `pkill -f` a bridge (self-kills the shell, exit 144) — kill by port pid.
#
# Requires: pinchtab >=0.10.0 on PATH; crawl-config.audit.json (copy the
# .example, set a token). Usage:  scripts/site-audit.sh [--gate dup|full]
# ============================================================================
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="${PWG_AUDIT_CONFIG:-$ROOT/crawl-config.audit.json}"
PORT="${PWG_AUDIT_PORT:-9872}"
GATE="${1:---gate}"; GATE_VAL="${2:-dup}"
GRAPHS="$ROOT/.audit-graphs"; mkdir -p "$GRAPHS"
export PYTHONPATH="$ROOT"

if [ ! -f "$CONFIG" ]; then
  echo "ERROR: $CONFIG not found. Copy crawl-config.audit.example.json → crawl-config.audit.json and set a token." >&2
  exit 2
fi
export PINCHTAB_CONFIG="$CONFIG"
PINCHTAB_TOKEN="$(python3 -c "import json;print(json.load(open('$CONFIG'))['server']['token'])")"
export PINCHTAB_TOKEN
STATE_DIR="$(python3 -c "import json;print(json.load(open('$CONFIG'))['server']['stateDir'])")"
PROFILE_DIR="$(python3 -c "import json;print(json.load(open('$CONFIG'))['profiles']['baseDir'])")"

kill_bridge() {                                   # by PORT pid only — never pkill -f
  local bpid
  bpid="$(ss -ltnp 2>/dev/null | grep ":$PORT " | grep -oP 'pid=\K[0-9]+' | head -1)"
  [ -n "$bpid" ] && kill "$bpid" 2>/dev/null
  sleep 2
}
start_bridge() {                                  # fresh headless bridge, wait for health
  ( pinchtab bridge --engine chrome >/dev/null 2>&1 & )
  for _ in $(seq 1 25); do sleep 1; pinchtab health >/dev/null 2>&1 && return 0; done
  echo "ERROR: bridge did not become healthy on :$PORT" >&2; return 1
}
wipe_state() {                                    # defeat stateDir tab-restore contamination
  rm -rf "$STATE_DIR" "$PROFILE_DIR" 2>/dev/null
  mkdir -p "$STATE_DIR" "$PROFILE_DIR"
}

# --- crawl each site into $GRAPHS/<name>.json --------------------------------
python3 -c "import json;[print(s['name'],s['start_url'],s['max_states'],s['max_depth']) for s in json.load(open('$ROOT/tests/audit/sites.json'))['sites']]" \
| while read -r NAME URL MAXST MAXD; do
    echo "=== crawl $NAME ($URL) ==="
    kill_bridge; wipe_state
    start_bridge || { echo "skip $NAME (no bridge)"; continue; }
    timeout 300 python3 -m pinchtab_webgraph.cli crawl \
      --start "$URL" --server "http://127.0.0.1:$PORT" --config "$CONFIG" \
      --out "$GRAPHS/$NAME.json" --max-depth "$MAXD" --max-states "$MAXST" \
      --max-visits $((MAXST * 3)) --max-restarts 0 2>&1 | tail -2
    # cli appends .json → normalize the filename to <name>.json for the scorer
    [ -f "$GRAPHS/$NAME.json.json" ] && mv -f "$GRAPHS/$NAME.json.json" "$GRAPHS/$NAME.json"
  done
kill_bridge

# --- score browser-free ------------------------------------------------------
echo; echo "=== SCORING ==="
python3 "$ROOT/tests/audit/check.py" --graphs "$GRAPHS" --sites "$ROOT/tests/audit/sites.json" "$GATE" "$GATE_VAL"
