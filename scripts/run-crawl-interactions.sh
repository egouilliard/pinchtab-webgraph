#!/usr/bin/env bash
# Build an interaction-graph CACHE against the isolated crawl browser (9871).
# Start it first with scripts/start-crawl-browser.sh (and log in if the app needs it).
# Crawls once, thoroughly; never submits forms. Query the result offline with howto.py.
#
#   scripts/run-crawl-interactions.sh --start https://app/dashboard --out out/app-cache
#   python3 pinchtab_webgraph/howto.py out/app-cache.json --goal "create team"   # offline, milliseconds
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$DIR/.." && pwd)"
export PINCHTAB_CONFIG="$ROOT/crawl-config.json"
export PINCHTAB_TOKEN="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["server"]["token"])' "$ROOT/crawl-config.json")"
exec python3 "$ROOT/pinchtab_webgraph/interaction_crawl.py" "$@" --server http://localhost:9871
