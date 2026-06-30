#!/usr/bin/env bash
# Relaunch the isolated crawl browser (bridge on 9871) in a fresh gnome-terminal.
# Used as interaction_crawl.py's --restart-cmd so a wedged bridge auto-recovers
# (the crawler kills the stale PID first, then runs this to relaunch, then polls
# health and re-logs-in via --login-cmd). Needs $DISPLAY (desktop session).
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
nohup setsid gnome-terminal --title="PinchTab Crawl Browser (9871)" -- \
  bash -lc "cd '$DIR' && ./start-crawl-browser.sh 2>&1 | tee bridge.log; exec bash" \
  >/dev/null 2>&1 &
