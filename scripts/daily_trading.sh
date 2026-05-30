#!/usr/bin/env bash
# Run daily paper trading for all active generations.
# Called by launchd or cron on weekdays.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV_PYTHON="$REPO_ROOT/.venv/bin/python"
LOG_DIR="$REPO_ROOT/data/logs"
mkdir -p "$LOG_DIR"

TODAY=$(date +%Y-%m-%d)
DOW=$(date +%u)  # 1=Monday ... 7=Sunday

# Skip weekends (markets closed)
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

echo "=== Daily trading run: $TODAY ===" >> "$LOG_FILE"
"$VENV_PYTHON" "$REPO_ROOT/scripts/run_generations.py" run-daily --date "$TODAY" >> "$LOG_FILE" 2>&1

# Daily report is intentionally NOT generated here. Per project convention,
# Claude writes the report by reading JSON state from
# data/generations/gen_NNN/horizon_*/ into docs/reports/YYYY-MM-DD-genNNN-daily-report.md.
echo "=== Done (report written separately by Claude): $(date) ===" >> "$LOG_FILE"
