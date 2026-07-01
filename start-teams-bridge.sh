#!/usr/bin/env bash
# Launch the isolated Teams PinchTab bridge on port 9881 using teams-config.json
# (its own profile .instance-teams — persists the interactive Teams sign-in).
# Headed vs headless is set by "instanceDefaults.mode" in teams-config.json.
#   ./start-teams-bridge.sh     # run in its own terminal; leave it open
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PINCHTAB_CONFIG="$DIR/teams-config.json"
mkdir -p "$DIR/.instance-teams/state" "$DIR/.instance-teams/profiles"
echo "Starting Teams bridge on http://localhost:9881 (mode from teams-config.json)"
exec pinchtab bridge --engine chrome
