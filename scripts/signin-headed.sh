#!/usr/bin/env bash
# Sign in BY HAND in a bridge's persistent profile, in a visible browser window.
#
#   scripts/signin-headed.sh <bridge-config.json> <url>
#   scripts/signin-headed.sh teams-config.json https://teams.cloud.microsoft/
#
# For sign-ins that need a human (MFA push / number matching, a captcha, "stay signed in?").
# Stops the bridge serving that config, restarts it HEADED on the SAME profile, opens <url>,
# waits for you to finish in the window, then restarts it in its configured mode. You type
# your credentials into the real login page — nothing is stored or seen by this toolkit.
#
# SIGNIN_NO_PROMPT=1 skips the wait (used to test the stop/start cycle unattended).
set -euo pipefail
[ $# -eq 2 ] || { sed -n '2,11p' "$0"; exit 2; }
CFG="$(readlink -f "$1")"; URL="$2"
PY=(env -u PYTHONHOME -u PYTHONPATH -u LD_LIBRARY_PATH -u LD_PRELOAD python3)
cfg() {   # cfg <dotted.key> [default] — read one value from the bridge config
  "${PY[@]}" -c 'import json,sys
v=json.load(open(sys.argv[1]))
for k in sys.argv[2].split("."):
    v=v.get(k) if isinstance(v,dict) else None
print(sys.argv[3] if v is None and len(sys.argv)>3 else v)' "$CFG" "$@"; }
PORT="$(cfg server.port)"
PROFILES="$(cfg profiles.baseDir)"
export PINCHTAB_TOKEN="$(cfg server.token)"
LOG="$(dirname "$CFG")/$(basename "$CFG" .json)-bridge.log"

listening() { ss -ltnH "sport = :$PORT" | grep -q .; }
wait_port() {   # $1 = up|down
  for _ in $(seq 1 60); do
    if [ "$1" = up ] && listening; then return 0; fi
    if [ "$1" = down ] && ! listening; then return 0; fi
    sleep 0.5
  done
  echo "bridge on :$PORT did not come $1" >&2; return 1
}
stop_bridge() {
  # by PID, never `pkill -f <pattern>`: a pattern matching this script's own cmdline kills it
  local pid; pid="$(ss -ltnpH "sport = :$PORT" | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2 || true)"
  [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
  wait_port down
  # the bridge's Chrome must release the profile lock before another can open it
  local chrome; chrome="$(pgrep -f -- "$PROFILES" || true)"
  [ -n "$chrome" ] && kill $chrome 2>/dev/null || true
  for _ in $(seq 1 30); do pgrep -f -- "$PROFILES" >/dev/null || return 0; sleep 0.5; done
  echo "Chrome still holds $PROFILES" >&2; return 1
}

echo "· stopping the bridge on :$PORT"
stop_bridge

TMP="$(mktemp --suffix=.json)"; chmod 600 "$TMP"
trap 'rm -f "$TMP"' EXIT
"${PY[@]}" -c 'import json,sys; c=json.load(open(sys.argv[1])); c.setdefault("instanceDefaults",{})["mode"]="headed"; json.dump(c,open(sys.argv[2],"w"),indent=2)' "$CFG" "$TMP"

echo "· starting it HEADED on the same profile"
PINCHTAB_CONFIG="$TMP" setsid pinchtab bridge --engine chrome >>"$LOG" 2>&1 </dev/null &
wait_port up
# the port answers before Chrome can take a tab ("create target: context canceled"), and
# pinchtab still exits 0 on that — so retry until the bridge actually lists a tab on the host
HOST="$(printf '%s' "$URL" | sed -E 's#^[a-z]+://([^/]+).*#\1#')"
for i in $(seq 1 15); do
  pinchtab --server "http://localhost:$PORT" nav "$URL" --new-tab >/dev/null 2>&1 || true
  sleep 2
  pinchtab --server "http://localhost:$PORT" tab --json 2>/dev/null | grep -q -- "$HOST" && break
  [ "$i" -eq 15 ] && { echo "could not open $URL in the headed browser" >&2; exit 1; }
done

if [ -n "${SIGNIN_NO_PROMPT:-}" ]; then
  echo "· headed check: $(pgrep -af -- "$PROFILES" | grep -c -- '--headless' || true) headless Chrome processes (want 0)"
else
  echo
  echo "  A browser window is open at $URL."
  echo "  Sign in there (email, password, MFA, 'Stay signed in? → Yes'), wait until the app loads,"
  read -rp "  then press Enter here to switch the bridge back… " _ </dev/tty
fi

echo "· restarting it in its configured mode"
stop_bridge
PINCHTAB_CONFIG="$CFG" setsid pinchtab bridge --engine chrome >>"$LOG" 2>&1 </dev/null &
wait_port up
pinchtab --server "http://localhost:$PORT" nav "$URL" >/dev/null 2>&1 || true
echo "✓ bridge back on :$PORT ($(cfg instanceDefaults.mode default))"
