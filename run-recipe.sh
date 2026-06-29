#!/usr/bin/env bash
# Generate a how-to for an action against the isolated crawl browser (9871).
# Start it first with ./start-crawl-browser.sh (and log in if the app needs it).
#
#   ./run-recipe.sh --goal "add cae" --page https://app/caes/some-project
#   ./run-recipe.sh --goal "create team" --graph leytongo-full-links.json --start https://app/dashboard
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PINCHTAB_CONFIG="$DIR/crawl-config.json"
export PINCHTAB_TOKEN="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["server"]["token"])' "$DIR/crawl-config.json")"
exec python3 "$DIR/recipe.py" "$@" --server http://localhost:9871
