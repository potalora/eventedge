#!/usr/bin/env bash
# Run daily paper trading for all active generations.
# Called by a scheduler Monday-Friday; Python validates the exact XNYS session.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV_PYTHON="${EVENTEDGE_PYTHON:-$REPO_ROOT/.venv/bin/python}"
LOG_DIR="${EVENTEDGE_LOG_DIR:-$REPO_ROOT/data/logs}"
mkdir -p "$LOG_DIR"

TODAY=$(date +%Y-%m-%d)
DOW=$(date +%u)  # 1=Monday ... 7=Sunday

# Avoid needless weekend invocations. Holidays still reach Python and are
# rejected because a weekday is not necessarily an XNYS trading session.
if [ "$DOW" -ge 6 ]; then
    echo "$TODAY: Weekend, skipping."
    exit 0
fi

# Load environment
if [ -f "$REPO_ROOT/.env" ]; then
    set -a
    source "$REPO_ROOT/.env"
    set +a
fi

LOG_FILE="$LOG_DIR/daily_${TODAY}.log"

# Prevent the Mac from sleeping mid-run. On battery the system enters
# "Maintenance Sleep" and suspends this process: that suspended the run for
# most of its window on 2026-06-15 (killed by the 3600s wall) and 2026-06-16
# (never finished, corrupting gen_001 state). caffeinate holds idle/system/disk
# sleep assertions for the lifetime of the run.
# CAVEAT: -s only blocks *system* sleep while on AC power; with the lid closed
# on battery the Mac still sleeps. Keep the machine plugged in during the run
# (or move to the always-on VPS) for reliable execution.
RUN_CMD=("$VENV_PYTHON" "$REPO_ROOT/scripts/run_generations.py" run-daily --date "$TODAY")

# The screen remains an integration warning. It cannot authorize P0 trading.
echo "=== Screen preflight: $TODAY ===" >> "$LOG_FILE"
if ! "$VENV_PYTHON" "$REPO_ROOT/scripts/run_generations.py" preflight --date "$TODAY" --preflight-mode screen >> "$LOG_FILE" 2>&1; then
    echo "SCREEN PREFLIGHT FAILED for $TODAY (continuing to governed gate)" >> "$LOG_FILE"
fi

# The after-close governed probe is the hard P0 gate. Python owns exact XNYS
# close readiness; shell time comparisons and fallback execution are forbidden.
echo "=== Governed preflight: $TODAY ===" >> "$LOG_FILE"
if ! "$VENV_PYTHON" "$REPO_ROOT/scripts/run_generations.py" preflight --date "$TODAY" --preflight-mode governed >> "$LOG_FILE" 2>&1; then
    echo "GOVERNED PREFLIGHT FAILED for $TODAY; daily run blocked" >> "$LOG_FILE"
    exit 1
fi

echo "=== Daily trading run: $TODAY ===" >> "$LOG_FILE"
if command -v caffeinate >/dev/null 2>&1; then
    caffeinate -ims "${RUN_CMD[@]}" >> "$LOG_FILE" 2>&1
else
    "${RUN_CMD[@]}" >> "$LOG_FILE" 2>&1
fi

# Daily report is intentionally NOT generated here. Per project convention,
# Codex writes the report from ledger-derived JSON projections under
# data/generations/gen_NNN/horizon_*/ into docs/reports/YYYY-MM-DD-genNNN-daily-report.md.
echo "=== Done (report written separately by Codex): $(date) ===" >> "$LOG_FILE"
