#!/usr/bin/env bash
# Run daily paper trading for all active generations.
# Called by a scheduler Monday-Friday; Python validates the exact XNYS session.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV_PYTHON="${EVENTEDGE_PYTHON:-$REPO_ROOT/.venv/bin/python}"
LOG_DIR="${EVENTEDGE_LOG_DIR:-$REPO_ROOT/data/logs}"
mkdir -p "$LOG_DIR"

TODAY=$(TZ=America/New_York date +%Y-%m-%d)
DOW=$(TZ=America/New_York date +%u)  # 1=Monday ... 7=Sunday
PROSPECTIVE_ARGS=()
if [ "$#" -gt 0 ]; then
    if [ "$#" -ne 10 ] || [ "$1" != '--prospective-session' ] || [ "$3" != '--expected-generation' ] || \
       [ "$5" != '--expected-commit' ] || [ "$7" != '--expected-profile-sha256' ] || [ "$9" != '--expected-created-at' ]; then
        echo 'Invalid prospective launch arguments.' >&2
        exit 2
    fi
    PROSPECTIVE_ARGS=("$@")
    # The native gate verifies the real clock, exact frozen identity, matching
    # durable running intent and inherited exclusive lease before any fetch.
    "$VENV_PYTHON" "$REPO_ROOT/scripts/recover_runtime.py" --repo-root "$REPO_ROOT" \
        --validate-prospective-launch "${PROSPECTIVE_ARGS[@]}"
    TODAY="$2"  # Reference label only; the real clock/source vintage stays native.
    readonly VENV_PYTHON REPO_ROOT TODAY
    readonly -a PROSPECTIVE_ARGS
    prospective_environment_signature() {
        printf '%q\n' "${EVENTEDGE_RUNTIME_LOCK_FD-}" "${EVENTEDGE_RUNTIME_LOCK_MODE-}" \
            "${EVENTEDGE_PYTHON-}" "${EVENTEDGE_GENERATION_ID-}" "${EVENTEDGE_GENERATION_COMMIT-}" \
            "${EVENTEDGE_GENERATION_CONFIG_FILE-}" "${EVENTEDGE_GENERATION_CONFIG_SHA256-}"
    }
    PROSPECTIVE_ENVIRONMENT_BEFORE=$(prospective_environment_signature)
    readonly PROSPECTIVE_ENVIRONMENT_BEFORE
fi

# Avoid needless weekend invocations. Holidays still reach Python and are
# rejected because a weekday is not necessarily an XNYS trading session.
if [ "${#PROSPECTIVE_ARGS[@]}" -eq 0 ] && [ "$DOW" -ge 6 ]; then
    echo "$TODAY: Weekend, skipping."
    exit 0
fi

# Load environment
if [ -f "$REPO_ROOT/.env" ]; then
    set -a
    source "$REPO_ROOT/.env"
    set +a
fi
if [ "${#PROSPECTIVE_ARGS[@]}" -gt 0 ]; then
    if [ "$(prospective_environment_signature)" != "$PROSPECTIVE_ENVIRONMENT_BEFORE" ]; then
        echo 'Environment changed guarded prospective launch identity or lease.' >&2
        exit 2
    fi
    "$VENV_PYTHON" "$REPO_ROOT/scripts/recover_runtime.py" --repo-root "$REPO_ROOT" \
        --validate-prospective-launch "${PROSPECTIVE_ARGS[@]}"
fi

LOG_FILE="$LOG_DIR/daily_${TODAY}.log"

# Read the finalized evidence even when the governed gate or daily command fails.
# A reporting failure is visible without rewriting the ledger or worker outcome.
write_operational_report() {
    local run_status=$?
    trap - EXIT
    echo "=== Operational report: $TODAY ===" >> "$LOG_FILE"
    if "$VENV_PYTHON" "$REPO_ROOT/scripts/generate_operational_report.py" \
        --repo-root "$REPO_ROOT" --all-active --date "$TODAY" \
        --output-dir "$REPO_ROOT/docs/reports" >> "$LOG_FILE" 2>&1; then
        :
    else
        local report_status=$?
        echo "OPERATIONAL REPORT INCOMPLETE for $TODAY (exit $report_status)" >> "$LOG_FILE"
        if [ "$run_status" -eq 0 ]; then
            run_status=$report_status
        fi
    fi
    exit "$run_status"
}
trap write_operational_report EXIT

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

echo "=== Daily command completed: $(date) ===" >> "$LOG_FILE"
