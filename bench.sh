#!/usr/bin/env bash
# Benchmark recipe.py across easy/medium/hard discovery scenarios.
cd ~/pinchtab-webgraph
export PINCHTAB_CONFIG=~/pinchtab-webgraph/crawl-config.json
LOGIN=~/.claude/skills/leytongo-testing-and-guides/scripts/leytongo-login

run () {
  local name="$1"; shift
  "$LOGIN" >/dev/null 2>&1
  local start end out states route found
  start=$(date +%s)
  out=$(timeout 300 ./run-recipe.sh "$@" 2>&1)
  end=$(date +%s)
  states=$(printf '%s\n' "$out" | grep -c '· \[')
  route=$(printf '%s\n' "$out" | grep -oE 'Shortest route — [0-9]+ click' | grep -oE '[0-9]+')
  printf '%s\n' "$out" | grep -q 'HOW TO' && found=FOUND || found=MISS
  echo "[$name] $found | $((end-start))s | states_explored=$states | path_clicks=${route:-NA}"
}

echo "=== recipe.py discovery benchmark ==="
# EASY: trigger is on the start page itself
run "EASY  (add cae @ dashboard)"        --goal "add cae"      --start https://go-staging.leyton.com/dashboard          --out bench-easy
# MEDIUM: trigger one tab-click from the start page
run "MEDIUM(add template @ settings)"    --goal "add template" --start https://go-staging.leyton.com/settings           --out bench-medium
# HARD: trigger multiple steps away, through a tab, from the dashboard
run "HARD  (add template @ dashboard)"   --goal "add template" --start https://go-staging.leyton.com/dashboard          --out bench-hard
echo "=== done ==="
