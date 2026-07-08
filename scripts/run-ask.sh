#!/usr/bin/env bash
# Cache-first how-to against the isolated crawl browser (9871): query the cache,
# fall back to live discovery on a miss, and write the result back into the cache.
# Start the browser first with scripts/start-crawl-browser.sh (and log in if needed).
#
#   scripts/run-ask.sh --goal "add item" --start https://app/dashboard
#   scripts/run-ask.sh --goal "create team" --start https://app/dashboard --verify
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$DIR/.." && pwd)"
export PINCHTAB_CONFIG="$ROOT/crawl-config.json"
export PINCHTAB_TOKEN="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["server"]["token"])' "$ROOT/crawl-config.json")"
exec python3 "$ROOT/pinchtab_webgraph/ask.py" "$@" --server http://localhost:9871
