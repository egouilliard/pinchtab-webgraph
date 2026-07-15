#!/usr/bin/env bash
# Build an interaction-graph CACHE against the isolated crawl browser (9871).
# Start it first with scripts/start-crawl-browser.sh (and log in if the app needs it).
# Crawls once, thoroughly; never submits forms. Query the result offline with howto.py.
#
#   scripts/run-crawl-interactions.sh --start https://app/dashboard --out out/app-cache
#   python3 -m pinchtab_webgraph.howto out/app-cache.json --goal "create team"   # offline, milliseconds
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$DIR/.." && pwd)"
export PINCHTAB_CONFIG="$ROOT/crawl-config.json"
export PINCHTAB_TOKEN="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["server"]["token"])' "$ROOT/crawl-config.json")"
# run as a MODULE, not a file path — the package uses relative imports (`from . import recipe`),
# so executing the .py directly dies with "attempted relative import with no known parent package".
cd "$ROOT"
exec python3 -m pinchtab_webgraph.interaction_crawl "$@" --server http://localhost:9871
