#!/usr/bin/env bash
# Launch an ISOLATED PinchTab browser dedicated to crawling, on port 9871.
# Separate config + profile from your daemon (9867) and your monday attach.
# Headless, ad/image/media-blocked, JS-eval enabled, no auth (localhost only).
#
#   ./start-crawl-browser.sh          # run in its own terminal; leave it open
#   python3 crawl.py https://site.com --server http://localhost:9871
#
# Stop it with Ctrl-C in this terminal (or: pkill -f 'pinchtab bridge').
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PINCHTAB_CONFIG="$DIR/crawl-config.json"
mkdir -p "$DIR/.instance/state" "$DIR/.instance/profiles"
echo "Starting isolated crawl browser on http://localhost:9871"
echo "Config: $PINCHTAB_CONFIG"
exec pinchtab bridge --engine chrome
