#!/usr/bin/env bash
# Build an interaction-graph CACHE against the isolated crawl browser (9871).
# Start it first with ./start-crawl-browser.sh (and log in if the app needs it).
# Crawls once, thoroughly; never submits forms. Query the result offline with howto.py.
#
#   ./run-crawl-interactions.sh --start https://app/dashboard --out app-cache
#   python3 howto.py app-cache.json --goal "create team"     # offline, milliseconds
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PINCHTAB_CONFIG="$DIR/crawl-config.json"
export PINCHTAB_TOKEN="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["server"]["token"])' "$DIR/crawl-config.json")"
exec python3 "$DIR/interaction_crawl.py" "$@" --server http://localhost:9871
