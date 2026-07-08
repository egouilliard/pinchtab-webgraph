#!/usr/bin/env bash
# Benchmark several GENUINELY-HARD scenarios — all started from /dashboard, each
# requiring blind multi-step navigation (section + tab) to reach a create/add
# control that opens a real form. Every goal below is a 3-click path:
#   dashboard -> a section (Settings / Users & Roles) -> a tab -> the create button.
# (1–2 click cases like "add cae", "create view", "add organization type" are NOT
#  here — those triggers live on the dashboard or the default settings tab.)
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$DIR/.." && pwd)"
cd "$ROOT"
mkdir -p out/howto out/logs
export PINCHTAB_CONFIG="$ROOT/crawl-config.json"
LOGIN=~/.claude/skills/leytongo-testing-and-guides/scripts/leytongo-login
DASH=https://go-staging.leyton.com/dashboard

ensure_browser () {
  pinchtab health >/dev/null 2>&1 && return 0
  echo "  (bridge down — restarting)"
  local bp; bp=$(ss -ltnp 2>/dev/null | grep 9871 | grep -oP 'pid=\K[0-9]+' | head -1)
  [ -n "$bp" ] && kill "$bp" 2>/dev/null; sleep 2
  nohup setsid gnome-terminal -- bash -lc "'$DIR/start-crawl-browser.sh' 2>&1 | tee '$ROOT/out/logs/bridge.log'; exec bash" >/dev/null 2>&1 & disown
  for _ in $(seq 1 25); do sleep 1; pinchtab health >/dev/null 2>&1 && break; done
}

run () {
  local name="$1" goal="$2"
  ensure_browser
  "$LOGIN" >/dev/null 2>&1
  local s e out st cl f fc tr
  s=$(date +%s)
  out=$(timeout 120 "$DIR/run-recipe.sh" --goal "$goal" --start "$DASH" --max-discover 25 --out "out/howto/ht-$name" 2>&1)
  e=$(date +%s)
  st=$(printf '%s\n' "$out" | grep -c '· \[')
  cl=$(printf '%s\n' "$out" | grep -oE 'route — [0-9]+ click' | grep -oE '[0-9]+')
  fc=$(printf '%s\n' "$out" | grep -oE 'Fill in [0-9]+' | grep -oE '[0-9]+')
  tr=$(printf '%s\n' "$out" | grep -oE "FOUND '[^']+'" | head -1 | sed "s/FOUND //")
  printf '%s\n' "$out" | grep -q 'HOW TO' && f=FOUND || f=MISS
  printf '[%-22s] %-5s | %2ds | states=%-2s | clicks=%-2s | fields=%-3s | trigger=%-16s | %s\n' \
    "$name" "$f" "$((e-s))" "$st" "${cl:-NA}" "${fc:-NA}" "${tr:-NA}" "$goal"
}

echo "=== HARD scenarios (all 3-click, blind from /dashboard) ==="
run template     "add template"          # Settings -> Document Templates  -> Add Template
run team         "create team"           # Users&Roles -> Teams            -> Create Team
run ficha        "add ficha"             # Settings -> FICHA Types          -> Add FICHA Type
run stage        "add stage"             # Settings -> Stage Types          -> Add Stage Type
run organization "create organization"   # Settings/Orgs -> Organizations   -> Add Organization
run trigger      "add trigger template"  # Settings -> (a) Templates tab    -> create template
echo "=== done ==="
# NOTE: "add keyword" and "add plugin" are intentionally NOT here — see report:
#   * Plugins tab has no create action (fixed set of integrations to configure).
#   * Keyword Management has only per-ROW "Add" buttons inside the requirements
#     table (bulk controls, labeled just "Add") — no top-level create form.
