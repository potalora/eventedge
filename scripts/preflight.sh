#!/usr/bin/env bash
# Standalone preflight integrity check for all active generations.
# Scheduled by trade-preflight.timer ahead of the daily run so integration
# defects (source payload shapes that fail staging, broken screens) surface
# while there is still time to fix and rerun the same session.
#
# Runs each generation's frozen worktree with --preflight: live shared
# fetch, per-horizon screens, and the event-identity staging gates — no
# state writes, no LLM, no trades.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV_PYTHON="${EVENTEDGE_PYTHON:-$REPO_ROOT/.venv/bin/python}"
LOG_DIR="${EVENTEDGE_LOG_DIR:-$REPO_ROOT/data/logs}"
mkdir -p "$LOG_DIR"

TODAY=$(date +%Y-%m-%d)
DOW=$(date +%u)  # 1=Monday ... 7=Sunday

if [ "$DOW" -ge 6 ]; then
    echo "$TODAY: Weekend, skipping preflight."
    exit 0
fi

# Load environment
if [ -f "$REPO_ROOT/.env" ]; then
    set -a
    source "$REPO_ROOT/.env"
    set +a
fi

LOG_FILE="$LOG_DIR/preflight_${TODAY}.log"

echo "=== Preflight check: $TODAY ===" >> "$LOG_FILE"
"$VENV_PYTHON" "$REPO_ROOT/scripts/run_generations.py" preflight --date "$TODAY" --preflight-mode screen >> "$LOG_FILE" 2>&1
rc=$?
echo "=== Preflight finished rc=$rc: $(date) ===" >> "$LOG_FILE"
exit $rc
