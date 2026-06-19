#!/usr/bin/env bash
# Daily refresh: crawl → Claude extract → KG ingest, for every ticker in config.
# A failing ticker never blocks the others. Single-instance via flock.
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

log "start daily_update: ${#TICKERS[@]} tickers"
ok=0; fail=0
for t in "${TICKERS[@]}"; do
  log "── $t ──"
  if PYTHONPATH="$HERE/src" "$PY" -m cpkg.pipeline --ticker "$t" >>"$LOG" 2>&1; then
    log "$t OK"; ok=$((ok+1))
  else
    log "$t FAILED (continuing)"; fail=$((fail+1))
  fi
done
log "done: ok=$ok fail=$fail"
