#!/usr/bin/env bash
# Run the crawler against the isolated crawl browser (port 9871), passing the
# right auth token automatically. Start the browser first with:
#   ./start-crawl-browser.sh   (in its own terminal)
#
# Usage:
#   ./run-crawl.sh https://example.com [extra crawl.py flags...]
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PINCHTAB_CONFIG="$DIR/crawl-config.json"
export PINCHTAB_TOKEN="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["server"]["token"])' "$DIR/crawl-config.json")"
exec python3 "$DIR/crawl.py" "$@" --server http://localhost:9871
