#!/usr/bin/env python3
"""Run autoresearch paper trading engine.

Usage:
    python scripts/run_generation.py                    # paper trading (default)
    python scripts/run_generation.py --phase paper      # same as above
    python scripts/run_generation.py --phase learning   # run learning loop
    python scripts/run_generation.py --use-llm          # enable LLM enrichment
    python scripts/run_generation.py --reset             # clear state and run fresh
"""
from __future__ import annotations

# NOTE: For 2-cohort paper trading trials, use scripts/run_cohorts.py instead.
# This script runs a single engine instance (legacy behavior).

import argparse
import logging
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("run_generation")


def on_event(kind: str, **data):
    """Simple event handler that prints progress."""
    if kind == "phase":
        phase = data.get("phase", "")
        status = data.get("status", "")
        print(f"  [{phase}] {status}")
    elif kind == "strategy_start":
        name = data.get("name", "?")
        track = data.get("track", "?")
        print(f"  Running {track} strategy: {name}")
    elif kind == "strategy_done":
        name = data.get("name", "?")
        n = data.get("num_results", data.get("num_signals", 0))
        print(f"    -> {name}: {n} results")


def print_phase_result(result: dict, phase: str, elapsed: float):
    """Print results from a --phase command."""
    print(f"\n{'='*60}")
    print(f"  {phase.upper()} PHASE RESULTS")
    print(f"{'='*60}")
    print(f"  Total time: {elapsed:.1f}s")

    if phase == "paper":
        signals = result.get("signals", [])
        recs = result.get("recommendations", [])
        opened = result.get("trades_opened", [])
        closed = result.get("trades_closed", [])
        regime = result.get("regime", {})
        account = result.get("account", {})
        print(f"  Regime: {regime.get('overall_regime', '?')}")
        print(f"  Signals: {len(signals)}, Recommendations: {len(recs)}")
        print(f"  Trades opened: {len(opened)}, closed: {len(closed)}")
        if account:
            print(f"  Portfolio value: ${account.get('portfolio_value', 0):,.2f}")
            print(f"  Cash: ${account.get('cash', 0):,.2f}")
        for s in signals[:10]:
            print(f"    {s.get('strategy', '?')}: {s.get('ticker', '?')} "
                  f"{s.get('direction', '?')} score={s.get('score', 0):.2f}")
        for r in recs[:5]:
            ticker = r.get("ticker", "?")
            direction = r.get("direction", "?")
            conf = r.get("confidence", 0)
            print(f"    REC: {ticker} {direction} conf={conf:.2f} "
                  f"size={r.get('position_size_pct', 0):.1%}")

    elif phase == "learning":
        print(f"  Triggered: {result.get('triggered', False)}")
        print(f"  Strategies evaluated: {result.get('strategies_evaluated', 0)}")
        scores = result.get("scores", {})
        if scores:
            for name, score in sorted(scores.items(), key=lambda x: -x[1]):
                tc = result.get("trade_counts", {}).get(name, 0)
                print(f"    {name}: score={score:.4f} trades={tc}")
        weights = result.get("paper_weights", {})
        if weights:
            print(f"\n  Paper Weights:")
            for name in sorted(weights, key=lambda k: -weights[k]):
                print(f"    {name}: {weights[name]:.4f}")


def main():
    parser = argparse.ArgumentParser(description="Run autoresearch paper trading")
    parser.add_argument(
        "--phase",
        choices=["paper", "learning"],
        default="paper",
        help="'paper' runs trading loop (default), 'learning' runs learning loop.",
    )
    parser.add_argument(
        "--use-llm",
        action="store_true",
        help="Use LLM for signal enrichment (costs ~$0.01/run)",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Clear all state before running",
    )
    parser.add_argument(
        "--trading-date",
        default=None,
        help="Override trading date (default: today)",
    )
    args = parser.parse_args()

    import os
    from dotenv import load_dotenv
    load_dotenv()

    from tradingagents.autoresearch.multi_strategy_engine import MultiStrategyEngine
    from tradingagents.autoresearch.state import StateManager
    from tradingagents.autoresearch.strategies import get_paper_trade_strategies
    from tradingagents.default_config import DEFAULT_CONFIG

    # Populate API keys from environment
    ar = DEFAULT_CONFIG.setdefault("autoresearch", {})
    ar["fred_api_key"] = os.environ.get("FRED_API_KEY", ar.get("fred_api_key", ""))
    ar["finnhub_api_key"] = os.environ.get("FINNHUB_API_KEY", ar.get("finnhub_api_key", ""))
    ar["regulations_api_key"] = os.environ.get("REGULATIONS_API_KEY", ar.get("regulations_api_key", ""))
    ar["courtlistener_token"] = os.environ.get("COURTLISTENER_TOKEN", ar.get("courtlistener_token", ""))

    strategies = get_paper_trade_strategies()

    print(f"Active strategies ({len(strategies)}):")
    for s in strategies:
        print(f"  [{s.track}] {s.name}")

    # State manager
    ar_config = DEFAULT_CONFIG.get("autoresearch", {})
    state = StateManager(ar_config.get("state_dir", "data/state"))
    if args.reset:
        state.reset()
        print("  State cleared.")

    engine = MultiStrategyEngine(
        config=DEFAULT_CONFIG,
        strategies=strategies,
        state_manager=state,
        on_event=on_event,
        use_llm=args.use_llm,
    )

    start = time.time()

    if args.phase == "paper":
        result = engine.run_paper_trade_phase(trading_date=args.trading_date)
    elif args.phase == "learning":
        result = engine.run_learning_loop()

    elapsed = time.time() - start
    print_phase_result(result, args.phase, elapsed)


if __name__ == "__main__":
    main()
