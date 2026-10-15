#!/usr/bin/env bash
# Run one of the MS Teams flows (examples/flows/teams/) against the Teams bridge; prints the
# run record as JSON (its "outputs" carry what the flow read back).
#
#   scripts/teams-flow.sh send-message --allow-submit to=someone@example.com 'message=Hello'
#   scripts/teams-flow.sh create-meeting --dry-run subject=Sync start=2026-10-16T15:00:00+02:00 end=2026-10-16T15:30:00+02:00
#
# Without --allow-submit a flow stops short of its Send/Save click (recorded as "skipped").
# Bridge config: $TEAMS_BRIDGE_CONFIG, default ./teams-config.json (gitignored, holds the token).
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$DIR/.." && pwd)"
FLOWS="$ROOT/examples/flows/teams"
[ $# -ge 1 ] || { sed -n '2,10p' "$0"; echo "flows: $(ls "$FLOWS" | sed 's/\.json$//' | tr '\n' ' ')"; exit 2; }
NAME="$1"; shift
FLOW="$FLOWS/$NAME.json"
[ -f "$FLOW" ] || { echo "no flow '$NAME' — have: $(ls "$FLOWS" | sed 's/\.json$//' | tr '\n' ' ')" >&2; exit 2; }
CFG="$(readlink -f "${TEAMS_BRIDGE_CONFIG:-$ROOT/teams-config.json}")"
[ -f "$CFG" ] || { echo "no bridge config at $CFG (set TEAMS_BRIDGE_CONFIG)" >&2; exit 2; }
PY=(env -u PYTHONHOME -u PYTHONPATH -u LD_LIBRARY_PATH -u LD_PRELOAD python3)
PORT="$("${PY[@]}" -c 'import json,sys; print(json.load(open(sys.argv[1]))["server"]["port"])' "$CFG")"

ARGS=()
for a in "$@"; do
  case "$a" in
    --*) ARGS+=("$a") ;;
    *=*) ARGS+=(--input "$a") ;;
    *) echo "expected NAME=VALUE or a --flag, got: $a" >&2; exit 2 ;;
  esac
done
cd "$ROOT"     # run the package from the repo root, not an older installed copy
exec "${PY[@]}" -m pinchtab_webgraph.cli flow run "$FLOW" \
  --server "http://localhost:$PORT" --config "$CFG" --json "${ARGS[@]}"
