#!/usr/bin/env bash
# Daily refresh: crawl → Claude extract → KG ingest, for every ticker in config.
# A failing ticker never blocks the others. Single-instance via flock.
#
# Cadence (cost optimization): Tier-1 runs EVERY day (deterministic, cheap, the
# MCP source of truth); Tier-2 (Graphiti, the costly LLM step) runs only once a
# week on TIER2_WEEKDAY — see the cadence block below. Schedule this script once
# a day; it decides Tier-1-only vs Tier-1+Tier-2 itself (no separate cron entry).
#
# ── Schedule (macOS launchd, recommended) ────────────────────────────────────
#   Create ~/Library/LaunchAgents/com.cpkg.daily.plist with ProgramArguments:
#     /bin/bash -lc '/abs/path/company-profile-kg/bin/daily_update.sh'
#   and a StartCalendarInterval (e.g. Hour=6 Minute=30), then:
#     launchctl load ~/Library/LaunchAgents/com.cpkg.daily.plist
#   NOTE: `claude` must already be logged in (run `claude` once interactively) so
#   headless extraction works under launchd/cron.
#
# ── Schedule (crontab alternative) ───────────────────────────────────────────
#   30 6 * * *  /bin/bash -lc '/abs/path/company-profile-kg/bin/daily_update.sh'
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

PY="${PYTHON:-$HERE/.venv/bin/python}"
[ -x "$PY" ] || PY="python3"
DAY="$(date +%F)"
LOG_DIR="$HERE/logs"; mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/$DAY.log"
LOCK="$HERE/.daily.lock"

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date -u +%FT%TZ) another daily_update is running — exit" | tee -a "$LOG"
  exit 0
fi

log() { echo "$(date -u +%FT%TZ) $*" | tee -a "$LOG"; }

# Read tickers from config/tickers.yaml (no yaml dep in bash — ask Python).
mapfile -t TICKERS < <(PYTHONPATH="$HERE/src" "$PY" -c \
  "from cpkg.config import load_tickers; [print(r['ticker']) for r in load_tickers()]")

# ── Cadence: Tier-1 daily, Tier-2 weekly ─────────────────────────────────────
# Tier-2 (Graphiti) is the costly LLM step (~90+ claude calls/ticker) and bank
# profiles barely change day to day — so refresh it only weekly to cut ~85% of
# its cost/quota. Tier-1 still runs every day.
#   TIER2_WEEKDAY : 1..7 (Mon..Sun, matches `date +%u`); default 7 (Sunday).
#                   "daily" → run Tier-2 every day; anything else → that weekday.
#   FORCE_TIER2=1 : run Tier-2 this invocation regardless of weekday.
TIER2_WEEKDAY="${TIER2_WEEKDAY:-7}"
DOW="$(date +%u)"
TIER2_ARG="--no-tier2"; MODE="tier1-only"
if [ "${FORCE_TIER2:-0}" = "1" ] || [ "$TIER2_WEEKDAY" = "daily" ] || [ "$DOW" = "$TIER2_WEEKDAY" ]; then
  TIER2_ARG=""; MODE="tier1+tier2"
fi

log "start daily_update: ${#TICKERS[@]} tickers (mode=$MODE, dow=$DOW, tier2_weekday=$TIER2_WEEKDAY)"
ok=0; fail=0
for t in "${TICKERS[@]}"; do
  log "── $t ($MODE) ──"
  if PYTHONPATH="$HERE/src" "$PY" -m cpkg.pipeline --ticker "$t" $TIER2_ARG >>"$LOG" 2>&1; then
    log "$t OK"; ok=$((ok+1))
  else
    log "$t FAILED (continuing)"; fail=$((fail+1))
  fi
done
log "done: ok=$ok fail=$fail (mode=$MODE)"
