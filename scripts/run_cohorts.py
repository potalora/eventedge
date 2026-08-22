#!/usr/bin/env python3
"""Run 16-cohort paper trading matrix (4 horizons × 4 portfolio sizes).

Usage:
    python scripts/run_cohorts.py --date 2026-04-05    # daily trading (LLM on by default)
    python scripts/run_cohorts.py --learning            # refused: production learning is disabled
    python scripts/run_cohorts.py --compare             # print comparison report
    python scripts/run_cohorts.py --reset               # refused: start a fresh generation
    python scripts/run_cohorts.py --date 2026-04-05 --no-llm  # without LLM enrichment
    python scripts/run_cohorts.py --date 2026-04-05 --preflight  # no-write integrity check
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import date

# Generation isolation: when run via GenerationManager with PYTHONPATH set to a
# worktree, the editable install's finder would still resolve `tradingagents` to
# the main repo. Inserting the worktree at sys.path[0] before any project imports
# ensures the frozen worktree code is loaded instead.
_worktree = os.environ.get("PYTHONPATH", "")
if _worktree and _worktree != sys.path[0]:
    sys.path.insert(0, _worktree)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("run_cohorts")


def _runtime_lock_context(*, exclusive: bool):
    from pathlib import Path

    from tradingagents.strategies.orchestration.runtime_lock import (
        RuntimeLockInvalid,
        canonical_runtime_lock_path,
        runtime_lock,
    )

    lock_path = canonical_runtime_lock_path(Path(__file__).resolve().parent)
    fd_value = os.environ.get("EVENTEDGE_RUNTIME_LOCK_FD")
    mode_value = os.environ.get("EVENTEDGE_RUNTIME_LOCK_MODE")
    if fd_value is None and mode_value is None:
        return runtime_lock(lock_path, exclusive=exclusive)
    if fd_value is None or mode_value not in {"shared", "exclusive"}:
        raise RuntimeLockInvalid("inherited runtime lock environment is invalid")
    try:
        inherited_fd = int(fd_value)
    except ValueError as error:
        raise RuntimeLockInvalid("inherited runtime lock environment is invalid") from error
    return runtime_lock(
        lock_path,
        exclusive=exclusive,
        inherited_fd=inherited_fd,
        inherited_exclusive=mode_value == "exclusive",
    )


def _exit_runtime_lock_error(error: Exception) -> None:
    from tradingagents.strategies.orchestration.runtime_lock import RuntimeLockBusy

    payload = {
        "success": False,
        "busy": isinstance(error, RuntimeLockBusy),
        "error": str(error)[:4_096],
    }
    print(json.dumps(payload, sort_keys=True), file=sys.stderr)
    raise SystemExit(1)


def _preflight_exit_status(
    report: object, mode: str, trading_date: str | None = None
) -> tuple[int, str]:
    """Return one bounded, mode-appropriate preflight outcome."""
    from tradingagents.strategies.orchestration.generation_manager import (
        normalize_preflight_report,
    )

    report_date = (
        trading_date
        if trading_date is not None
        else report.get("trading_date")
        if isinstance(report, dict)
        else None
    )
    if not isinstance(report_date, str):
        return 1, f"PREFLIGHT {mode.upper()} FAILED: malformed report"
    normalized = normalize_preflight_report(
        report, mode=mode, trading_date=report_date
    )
    if normalized is None:
        return 1, f"PREFLIGHT {mode.upper()} FAILED: malformed report"
    if mode in {"governed", "all"}:
        status = normalized["governed_probe_status"]
        failure_map = normalized["governed_failure_map"]
        recoveries = normalized["governed_bar_recoveries"]
        ready = (
            normalized["ok"] is True
            and status == "ready"
            and not failure_map
        )
        recovery_count = len(recoveries)
        if ready:
            return (
                0,
                f"PREFLIGHT {mode.upper()} READY: governed probe ready; "
                f"{recovery_count} recovery summary(s)",
            )
        bounded_status = str(status if isinstance(status, str) else "malformed")[:128]
        return 1, f"PREFLIGHT {mode.upper()} FAILED: governed status {bounded_status}"
    success = normalized["ok"] is True and normalized["screen_ok"] is True
    failure_count = normalized["screen_failure_count"]
    return (
        0 if success else 1,
        f"PREFLIGHT SCREEN {'OK' if success else 'FAILED'}: "
        f"{failure_count} failure(s)",
    )


def _cohort_run_exit_status(result: dict) -> tuple[int, str]:
    """Return the alerting exit outcome for cohort execution results.

    Candidate-data quarantine is a distinct degraded outcome: its cohort
    execution is valid, but its performance must not be reported as a clean run.
    Execution failures retain exit status 1; degraded runs use 2.
    """
    from tradingagents.strategies.orchestration.cohort_orchestrator import (
        aggregate_governed_reporting,
        count_degraded_cohorts,
        count_failed_cohorts,
    )

    n_failed, n_total, failed = count_failed_cohorts(result)
    n_degraded, _, degraded = count_degraded_cohorts(result)
    quarantined_tickers = sorted(
        {
            str(ticker)
            for name in degraded
            for ticker in result[name].get("candidate_bar_quarantines", [])
        }
    )
    recoveries, _ = aggregate_governed_reporting(result)
    recovered_tickers = sorted({str(summary["ticker"]) for summary in recoveries})
    if n_failed:
        message = f"ERROR: {n_failed}/{n_total} cohorts failed: {', '.join(failed)}"
        if n_degraded:
            message += (
                "; DEGRADED: "
                f"{n_degraded}/{n_total} cohorts degraded (execution valid): "
                f"{', '.join(degraded)}"
            )
            if quarantined_tickers:
                message += "; quarantined tickers: " + ", ".join(quarantined_tickers)
        return 1, message

    if n_degraded:
        message = (
            "DEGRADED: "
            f"{n_degraded}/{n_total} cohorts degraded (execution valid): "
            f"{', '.join(degraded)}"
        )
        if quarantined_tickers:
            message += "; quarantined tickers: " + ", ".join(quarantined_tickers)
        if recovered_tickers:
            message += "; recovered tickers: " + ", ".join(recovered_tickers)
        return 2, message
    return 0, ""


def _raise_fd_limit() -> None:
    """Raise the soft file-descriptor limit (launchd default is 256, too low
    for the 13-source fetch fan-out). Prefer the shared helper; fall back to a
    stdlib-only bump for frozen worktrees that predate tradingagents.sys_limits.
    """
    try:
        from tradingagents.sys_limits import raise_fd_limit

        raise_fd_limit()
        return
    except Exception:
        pass
    try:
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = 16384 if hard == resource.RLIM_INFINITY else min(16384, hard)
        if target > soft:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except Exception:
        pass


def main():
    _raise_fd_limit()
    parser = argparse.ArgumentParser(
        description="Run 16-cohort paper trading matrix",
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Trading date (YYYY-MM-DD). Defaults to today.",
    )
    parser.add_argument(
        "--learning",
        action="store_true",
        help="Refuse retired production learning (exit 2).",
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="Print cohort comparison report.",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Refuse destructive reset; start a fresh generation instead.",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Disable LLM enrichment (on by default).",
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help=(
            "Run the no-write integrity preflight (live fetch -> screen -> "
            "event-identity staging gates) instead of the daily cycle."
        ),
    )
    parser.add_argument(
        "--preflight-mode",
        choices=("all", "screen", "governed"),
        default=None,
        help="Preflight checks to run (default with --preflight: all)",
    )
    parser.add_argument(
        "--block-tickers",
        default="",
        help="Comma-separated tickers to exclude (compliance). Also reads BLOCKED_TICKERS env var.",
    )
    args = parser.parse_args()

    if args.learning:
        print(
            "Production learning is disabled; no generation state was changed.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if args.reset:
        parser.error(
            "reset is disabled for ledger-backed generation state; "
            "start a fresh generation instead"
        )
    if args.preflight and args.compare:
        parser.error("--preflight cannot be combined with --compare")
    if not args.preflight and args.preflight_mode is not None:
        parser.error("--preflight-mode requires --preflight")
    preflight_mode = args.preflight_mode or "all"

    # Bound for the checker; the flag exits above guarantee assignment
    # before any branch that reads it (preflight or daily run).
    exact_trading_date = ""
    if not (args.learning or args.compare or args.reset):
        requested = args.date or date.today().isoformat()
        try:
            trading_session = date.fromisoformat(requested)
        except ValueError:
            parser.error(f"invalid ISO trading date: {requested}")

        from tradingagents.strategies.orchestration.trading_calendar import is_session

        if not is_session(trading_session):
            parser.error(f"{requested} is not an XNYS session")
        exact_trading_date = trading_session.isoformat()

    generation_id = os.environ.get("EVENTEDGE_GENERATION_ID", "").strip()
    generation_commit = os.environ.get("EVENTEDGE_GENERATION_COMMIT", "").strip()
    if not generation_id or not generation_commit:
        parser.error(
            "EVENTEDGE_GENERATION_ID and EVENTEDGE_GENERATION_COMMIT are required"
        )

    from dotenv import load_dotenv

    load_dotenv()

    from tradingagents.strategies.orchestration.cohort_orchestrator import (
        CohortOrchestrator,
        build_default_cohorts,
    )
    from tradingagents.default_config import DEFAULT_CONFIG

    # Build config with env-var overrides
    config = dict(DEFAULT_CONFIG)
    config["autoresearch"] = dict(config.get("autoresearch", {}))

    # Allow generation manager to override state_dir via env var
    state_dir_override = os.environ.get("AUTORESEARCH_STATE_DIR")
    if state_dir_override:
        config["autoresearch"]["state_dir"] = state_dir_override

    # Load all API keys from environment
    for key in [
        "finnhub_api_key",
        "fred_api_key",
        "regulations_api_key",
        "courtlistener_token",
        "edgar_user_agent",
        "noaa_cdo_token",
        "usda_nass_api_key",
        "fmp_api_key",
    ]:
        env_val = os.environ.get(key.upper(), "")
        if env_val:
            config["autoresearch"][key] = env_val

    # Blocked tickers (compliance, conflict of interest)
    blocked = args.block_tickers or os.environ.get("BLOCKED_TICKERS", "")
    if blocked:
        tickers = [t.strip().upper() for t in blocked.split(",") if t.strip()]
        config["autoresearch"]["blocked_tickers"] = tickers
        logger.info("Blocked tickers: %s", tickers)

    # Preflight: no-write integrity check. Routed before cohort/ledger
    # construction so generation state is never opened.
    if args.preflight:
        from tradingagents.strategies.orchestration.preflight import run_preflight
        from tradingagents.strategies.orchestration.runtime_lock import (
            RuntimeLockBusy,
            RuntimeLockInvalid,
        )

        try:
            with _runtime_lock_context(exclusive=False):
                start = time.time()
                report = run_preflight(config, exact_trading_date, mode=preflight_mode)
                elapsed = time.time() - start
        except (RuntimeLockBusy, RuntimeLockInvalid) as error:
            _exit_runtime_lock_error(error)
        from tradingagents.strategies.orchestration.generation_manager import (
            normalize_preflight_report,
        )

        normalized_report = normalize_preflight_report(
            report,
            mode=preflight_mode,
            trading_date=exact_trading_date,
        )
        print(
            json.dumps(
                normalized_report
                if normalized_report is not None
                else {
                    "ok": False,
                    "preflight_mode": preflight_mode,
                    "error": "malformed preflight report",
                },
                indent=2,
                default=str,
            )
        )
        exit_code, message = _preflight_exit_status(
            report, preflight_mode, exact_trading_date
        )
        rendered = f"{message} ({exact_trading_date}, {elapsed:.1f}s)"
        if exit_code == 0:
            print(rendered)
            return
        print(rendered, file=sys.stderr)
        sys.exit(exit_code)

    def build_orchestrator():
        cohort_configs = build_default_cohorts(config)
        if args.no_llm:
            for cohort_config in cohort_configs:
                cohort_config.use_llm = False
        return CohortOrchestrator(
            cohort_configs,
            config,
            generation_id=generation_id,
            generation_commit=generation_commit,
        )

    # Route to the right action
    if args.compare:
        from tradingagents.strategies.metrics.service import MetricsService
        from tradingagents.strategies.orchestration.cohort_comparison import (
            CohortComparison,
        )

        orchestrator = build_orchestrator()
        ledgers = {
            cohort["config"].name: cohort["ledger"] for cohort in orchestrator.cohorts
        }
        generation_state_dir = config["autoresearch"]["state_dir"]
        service = MetricsService(
            generation_state_dir,
            ledgers,
            read_only=True,
        )
        print(
            json.dumps(
                CohortComparison(metrics_service=service).compare(),
                indent=2,
                default=str,
            )
        )
        return

    # Default: daily trading
    from tradingagents.strategies.orchestration.runtime_lock import (
        RuntimeLockBusy,
        RuntimeLockInvalid,
    )

    trading_date = exact_trading_date
    try:
        with _runtime_lock_context(exclusive=True):
            start = time.time()
            orchestrator = build_orchestrator()
            result = orchestrator.run_daily(trading_date)
            elapsed = time.time() - start
    except (RuntimeLockBusy, RuntimeLockInvalid) as error:
        _exit_runtime_lock_error(error)

    print(f"\nDaily trading completed for {trading_date} in {elapsed:.1f}s")
    from tradingagents.strategies.orchestration.run_outcome import (
        DAILY_RESULT_PREFIX,
        DAILY_RESULT_WIRE_VERSION,
    )

    envelope = {
        "wire_version": DAILY_RESULT_WIRE_VERSION,
        "cohort_results": result,
    }
    print(DAILY_RESULT_PREFIX + json.dumps(envelope, default=str))

    # Surface both execution failures and candidate-data quarantine via distinct
    # nonzero outcomes.  Quarantine keeps P0 execution valid but must never be
    # recorded as a clean performance run.
    try:
        exit_code, message = _cohort_run_exit_status(result)
    except Exception:
        failed = [
            k for k, v in result.items() if isinstance(v, dict) and v.get("error")
        ]
        exit_code = 1 if failed else 0
        message = (
            f"ERROR: {len(failed)}/{len(result)} cohorts failed: {', '.join(failed)}"
            if failed
            else ""
        )
    if exit_code:
        print(message, file=sys.stderr)
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
