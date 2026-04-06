#!/usr/bin/env python3
"""Run 16-cohort paper trading matrix (4 horizons × 4 portfolio sizes).

Usage:
    python scripts/run_cohorts.py --date 2026-04-05    # daily trading (LLM on by default)
    python scripts/run_cohorts.py --learning            # learning loop (adaptive only)
    python scripts/run_cohorts.py --compare             # print comparison report
    python scripts/run_cohorts.py --reset               # clear all cohort state
    python scripts/run_cohorts.py --date 2026-04-05 --no-llm  # without LLM enrichment
"""
from __future__ import annotations

# Generation isolation: when run via GenerationManager with PYTHONPATH set to a
# worktree, the editable install's finder would still resolve `tradingagents` to
# the main repo. Inserting the worktree at sys.path[0] before any project imports
# ensures the frozen worktree code is loaded instead.
import os
import sys
_worktree = os.environ.get("PYTHONPATH", "")
if _worktree and _worktree != sys.path[0]:
    sys.path.insert(0, _worktree)

import argparse
import json
import logging
import time
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("run_cohorts")


def main():
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
        help="Run learning loop (adaptive cohort only).",
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="Print cohort comparison report.",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Reset all cohort state.",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Disable LLM enrichment (on by default).",
    )
    parser.add_argument(
        "--block-tickers",
        default="",
        help="Comma-separated tickers to exclude (compliance). Also reads BLOCKED_TICKERS env var.",
    )
    args = parser.parse_args()

    from dotenv import load_dotenv

    load_dotenv()

    from tradingagents.strategies.orchestration.cohort_comparison import CohortComparison
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

    # Build cohort configs
    cohort_configs = build_default_cohorts(config)

    # Disable LLM only if explicitly requested
    if args.no_llm:
        for cc in cohort_configs:
            cc.use_llm = False

    orchestrator = CohortOrchestrator(cohort_configs, config)

    # Route to the right action
    if args.compare:
        state_dirs = {cc.name: cc.state_dir for cc in cohort_configs}
        comparison = CohortComparison(state_dirs)
        print(comparison.format_report())
        return

    if args.reset:
        orchestrator.reset()
        print("All cohort state has been cleared.")
        return

    if args.learning:
        start = time.time()
        result = orchestrator.run_learning()
        elapsed = time.time() - start
        print(f"\nLearning loop completed in {elapsed:.1f}s")
        print(json.dumps(result, indent=2, default=str))
        return

    # Default: daily trading
    trading_date = args.date or datetime.now().strftime("%Y-%m-%d")
    start = time.time()
    result = orchestrator.run_daily(trading_date)
    elapsed = time.time() - start

    print(f"\nDaily trading completed for {trading_date} in {elapsed:.1f}s")
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
