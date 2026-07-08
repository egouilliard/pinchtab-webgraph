#!/usr/bin/env bash
# Run the crawler against the isolated crawl browser (port 9871), passing the
# right auth token automatically. Start the browser first with:
#   scripts/start-crawl-browser.sh   (in its own terminal)
#
# Usage:
#   scripts/run-crawl.sh https://example.com [extra crawl.py flags...]
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$DIR/.." && pwd)"
export PINCHTAB_CONFIG="$ROOT/crawl-config.json"
export PINCHTAB_TOKEN="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["server"]["token"])' "$ROOT/crawl-config.json")"
exec python3 "$ROOT/pinchtab_webgraph/crawl.py" "$@" --server http://localhost:9871
