#!/usr/bin/env bash
# Deterministic, OFFLINE discovery benchmark for recipe.py.
#
# Unlike scripts/bench.sh (which drives the real bridge against LeytonGo and needs
# a login + network), this runs recipe.py against tests/fixtures/fake_pinchtab.py —
# a synthetic, app-agnostic bridge with realistic injected latencies. It measures
# cold-discovery wall-clock AND bridge round-trips for three scenarios, prints an
# aligned table, and writes a machine-readable report to out/bench/ (gitignored).
#
#   scripts/bench-discovery.sh                 # run + print table + write report
#   scripts/bench-discovery.sh --budget-check  # ALSO exit non-zero if EASY/MEDIUM ≥ 10s
#
# Scenarios (realistic latencies: NAV 700ms / CLICK 400ms / EVAL 25ms, 2 settle polls):
#   EASY   — start /settings, trigger on the default tab      (1 tab click)
#   MEDIUM — start /settings, trigger on tab 2                 (1 tab click)
#   HARD   — start /,        section + trigger on tab 2        (section + tab click)
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$DIR/.." && pwd)"
cd "$ROOT"

BUDGET_CHECK=0
[ "${1:-}" = "--budget-check" ] && BUDGET_CHECK=1
BUDGET_S=10          # the "<10s cold discovery" claim we guard for EASY + MEDIUM

FAKE="$ROOT/tests/fixtures/fake_pinchtab.py"
mkdir -p out/bench
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# a `pinchtab` on PATH that IS the fake bridge (its python shebang runs it directly —
# one fork per call, matching the real CLI's ~0.07s spawn cost the profiling measured).
cp "$FAKE" "$WORK/pinchtab"
chmod +x "$WORK/pinchtab"
export PATH="$WORK:$PATH"

# realistic bridge latencies (the fake's own defaults, pinned here for clarity)
export FAKEPT_NAV_MS=700 FAKEPT_CLICK_MS=400 FAKEPT_EVAL_MS=25 FAKEPT_SETTLE_POLLS=2

TS="$(date -u +%Y%m%dT%H%M%SZ)"
REPORT="out/bench/discovery-$TS.json"
FAIL=0

printf '=== recipe.py OFFLINE discovery benchmark (fake bridge) ===\n'
printf '%-8s %-6s %8s %8s %8s\n' SCENARIO FOUND WALL_S CLICKS ROUNDTR
json_rows=""

run () {
  local name="$1" trig_tab="$2" start="$3"
  local state="$WORK/state.json" log="$WORK/calls.log"
  rm -f "$state" "$log"
  local out start_s end_s wall found clicks rt
  start_s=$(date +%s.%N)
  out=$(FAKEPT_STATE="$state" FAKEPT_LOG="$log" FAKEPT_TRIGGER_TAB="$trig_tab" \
        python3 -m pinchtab_webgraph.recipe --goal "add widget" \
        --start "$start" --out "$WORK/recipe-$name" 2>&1)
  end_s=$(date +%s.%N)
  wall=$(awk "BEGIN{printf \"%.2f\", $end_s-$start_s}")
  printf '%s\n' "$out" | grep -q 'HOW TO' && found=FOUND || found=MISS
  clicks=$(printf '%s\n' "$out" | grep -oE 'route — [0-9]+ click' | grep -oE '[0-9]+')
  rt=$([ -f "$log" ] && wc -l < "$log" | tr -d ' ' || echo 0)
  printf '%-8s %-6s %8s %8s %8s\n' "$name" "$found" "$wall" "${clicks:-NA}" "$rt"
  [ "$found" = FOUND ] || FAIL=1
  # budget guard applies to EASY + MEDIUM (the shallow, common cold-miss cases)
  if [ "$BUDGET_CHECK" = 1 ] && { [ "$name" = EASY ] || [ "$name" = MEDIUM ]; }; then
    awk "BEGIN{exit !($wall >= $BUDGET_S)}" && { FAIL=1
      echo "  ! $name wall ${wall}s exceeds ${BUDGET_S}s budget" >&2; }
  fi
  json_rows="$json_rows${json_rows:+,}
    {\"scenario\":\"$name\",\"found\":\"$found\",\"wall_s\":$wall,\"clicks\":${clicks:-null},\"round_trips\":$rt,\"start\":\"$start\",\"trigger_tab\":$trig_tab}"
}

run EASY   0 https://synthetic.test/settings
run MEDIUM 2 https://synthetic.test/settings
run HARD   2 https://synthetic.test/

cat > "$REPORT" <<EOF
{
  "generated_utc": "$TS",
  "budget_seconds": $BUDGET_S,
  "latency_ms": {"nav": $FAKEPT_NAV_MS, "click": $FAKEPT_CLICK_MS, "eval": $FAKEPT_EVAL_MS, "settle_polls": $FAKEPT_SETTLE_POLLS},
  "scenarios": [$json_rows
  ]
}
EOF
echo "=== report: $REPORT ==="

exit "$FAIL"
