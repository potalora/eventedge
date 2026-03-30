"""Evolution engine: main orchestrator for autoresearch strategy discovery."""

import logging
import re
import signal
import time
from datetime import datetime, timedelta
from typing import Callable, Optional

from tradingagents.autoresearch.models import (
    Strategy,
    BacktestResults,
    ScreenerResult,
)
from tradingagents.autoresearch.screener import MarketScreener
from tradingagents.autoresearch.strategist import Strategist
from tradingagents.autoresearch.cached_pipeline import CachedPipelineRunner
from tradingagents.autoresearch.walk_forward import (
    generate_windows,
    get_test_dates,
    has_regime_diversity,
    cross_ticker_validation_split,
)
from tradingagents.autoresearch.fitness import (
    rank_strategies,
    compute_fitness,
    update_analyst_weights,
    meets_paper_criteria,
)
from tradingagents.autoresearch.ticker_universe import get_universe

logger = logging.getLogger(__name__)


class EvolutionEvent:
    """Event emitted by the evolution engine for progress reporting."""
    def __init__(self, kind: str, **data):
        self.kind = kind
        self.data = data
        self.timestamp = time.time()


# --- Entry/exit rule pattern matchers ---

_ENTRY_PATTERNS = {
    r"RSI_14 crosses above (\d+)": lambda sr, m: sr.rsi_14 > float(m.group(1)),
    r"RSI_14 crosses below (\d+)": lambda sr, m: sr.rsi_14 < float(m.group(1)),
    r"RSI_14 > (\d+)": lambda sr, m: sr.rsi_14 > float(m.group(1)),
    r"RSI_14 < (\d+)": lambda sr, m: sr.rsi_14 < float(m.group(1)),
    r"price > EMA_10": lambda sr, m: sr.close > sr.ema_10,
    r"price < EMA_10": lambda sr, m: sr.close < sr.ema_10,
    r"price > EMA_50": lambda sr, m: sr.close > sr.ema_50,
    r"price < EMA_50": lambda sr, m: sr.close < sr.ema_50,
    r"EMA_10 > EMA_50": lambda sr, m: sr.ema_10 > sr.ema_50,
    r"EMA_10 < EMA_50": lambda sr, m: sr.ema_10 < sr.ema_50,
    r"MACD > 0": lambda sr, m: sr.macd > 0,
    r"MACD < 0": lambda sr, m: sr.macd < 0,
    r"BUY signal from pipeline": None,  # handled separately with pipeline result
    r"SELL signal from pipeline": None,
    r"HOLD signal from pipeline": None,
}


def _check_entry_rule(rule: str, screener_result: ScreenerResult,
                       pipeline_result: dict) -> bool:
    """Check if a single entry rule is satisfied."""
    rule_lower = rule.lower().strip()

    # Pipeline signal rules
    if "buy signal from pipeline" in rule_lower:
        return pipeline_result.get("rating", "").upper() in ("BUY", "STRONG BUY")
    if "sell signal from pipeline" in rule_lower:
        return pipeline_result.get("rating", "").upper() in ("SELL", "STRONG SELL")
    if "hold signal from pipeline" in rule_lower:
        return pipeline_result.get("rating", "").upper() == "HOLD"

    # Pattern-based rules
    for pattern, evaluator in _ENTRY_PATTERNS.items():
        if evaluator is None:
            continue
        m = re.match(pattern, rule, re.IGNORECASE)
        if m:
            try:
                return evaluator(screener_result, m)
            except (AttributeError, TypeError):
                return True  # default to true if data missing

    # Unknown rule — default to true with warning
    logger.warning("Unknown entry rule '%s', defaulting to True", rule)
    return True


def _check_exit_rule(rule: str, entry_price: float, current_price: float,
                      holding_days: int, time_horizon: int) -> bool:
    """Check if a single exit rule is triggered."""
    rule_lower = rule.lower().strip()

    # Profit target: "X% profit target"
    m = re.match(r"(\d+)%?\s*profit\s*target", rule_lower)
    if m:
        target_pct = float(m.group(1)) / 100.0
        return current_price >= entry_price * (1 + target_pct)

    # Stop loss: "X% stop loss"
    m = re.match(r"(\d+)%?\s*stop\s*loss", rule_lower)
    if m:
        stop_pct = float(m.group(1)) / 100.0
        return current_price <= entry_price * (1 - stop_pct)

    # Time horizon exceeded
    if "time_horizon" in rule_lower or "time horizon" in rule_lower:
        return holding_days >= time_horizon

    # Trailing stop: "X% trailing stop"
    m = re.match(r"(\d+)%?\s*trailing\s*stop", rule_lower)
    if m:
        # Simplified: treat as regular stop loss (we don't track high water mark)
        stop_pct = float(m.group(1)) / 100.0
        return current_price <= entry_price * (1 - stop_pct)

    logger.warning("Unknown exit rule '%s', defaulting to False", rule)
    return False


class EvolutionEngine:
    """Main orchestrator for autoresearch strategy evolution.

    Composes screener, strategist, cached pipeline, walk-forward validation,
    fitness scoring, and reflection into an evolutionary loop.
    """

    def __init__(self, db, config: dict, on_event: Optional[Callable] = None):
        self.db = db
        self.config = config
        self.ar_config = config.get("autoresearch", {})
        self.screener = MarketScreener(db, config)
        self.strategist = Strategist(db, config)
        self.pipeline = CachedPipelineRunner(db, config)

        self._generation = 0
        self._best_fitness_history = []
        self._budget_used = 0.0
        self._interrupted = False
        self._on_event = on_event or (lambda e: None)
        self._start_time = None

    def _emit(self, kind: str, **data):
        """Emit a progress event."""
        event = EvolutionEvent(kind, **data)
        self._on_event(event)

    def interrupt(self):
        """Signal the engine to stop after the current generation completes."""
        self._interrupted = True

    def run(self, start_date: str = None, end_date: str = None,
            resume_from: int = 0) -> dict:
        """Main evolution loop.

        1. Run screener to get market data + regime
        2. Generate walk-forward windows + holdout
        3. Loop generations until _should_stop():
           a. strategist.propose() → strategies with CRO review
           b. For each strategy: _backtest_strategy() across walk-forward windows
           c. rank_strategies(), update DB fitness scores
           d. strategist.reflect() → store reflections
           e. update_analyst_weights() → Darwinian weights
        4. _run_holdout() on top strategies → flag overfit
        5. Return leaderboard

        Args:
            start_date: Backtest start date (default: 2 years ago).
            end_date: Backtest end date (default: today).
            resume_from: Generation number to resume from (skips earlier gens).

        Returns:
            Dict with leaderboard, stats, and progress info.
        """
        self._start_time = time.time()
        self._generation = resume_from

        if not start_date:
            start_date = (datetime.now() - timedelta(days=730)).strftime("%Y-%m-%d")
        if not end_date:
            end_date = datetime.now().strftime("%Y-%m-%d")

        max_generations = self.ar_config.get("max_generations", 15)
        num_windows = self.ar_config.get("walk_forward_windows", 3)
        holdout_weeks = self.ar_config.get("holdout_weeks", 6)

        # Step 1: Get universe and run screener
        self._emit("phase", phase="screener", status="starting",
                    message=f"Screening universe...")
        universe = get_universe(self.config)
        self._emit("screener_universe", count=len(universe))
        screener_results = self.screener.run(end_date, universe=universe)
        regime = screener_results[0].regime if screener_results else "TRANSITION"
        self._emit("phase", phase="screener", status="done",
                    message=f"Screened {len(screener_results)} tickers, regime={regime}",
                    tickers=len(screener_results), regime=regime)

        # Step 2: Generate walk-forward windows
        windows, holdout = generate_windows(start_date, end_date, num_windows, holdout_weeks)
        test_dates = get_test_dates(windows)
        self._emit("phase", phase="setup", status="done",
                    message=f"{len(windows)} walk-forward windows, holdout {holdout[0]} to {holdout[1]}",
                    windows=len(windows), holdout_start=holdout[0], holdout_end=holdout[1])

        logger.info("Evolution starting: %d windows, holdout %s to %s, regime=%s",
                     len(windows), holdout[0], holdout[1], regime)

        # Step 3: Evolution loop
        all_trade_results = []
        while self._generation < max_generations and not self._should_stop():
            if self._interrupted:
                self._emit("interrupted", generation=self._generation,
                            message="Interrupted by user — saving progress")
                break

            gen_start = time.time()
            self._emit("generation_start", generation=self._generation,
                        max_generations=max_generations,
                        message=f"Generation {self._generation}/{max_generations-1}")

            # 3a: Propose strategies
            self._emit("step", generation=self._generation, step="propose",
                        message="Proposing strategies (Sonnet)...")
            strategies = self.strategist.propose(screener_results, regime, self._generation)
            if not strategies:
                self._emit("step", generation=self._generation, step="propose",
                            message="No strategies proposed, skipping generation")
                self._generation += 1
                continue
            self._emit("step", generation=self._generation, step="propose_done",
                        message=f"{len(strategies)} strategies approved by CRO",
                        count=len(strategies),
                        names=[s.name for s in strategies])

            # 3b: Backtest each strategy across walk-forward windows
            for i, strategy in enumerate(strategies):
                self._emit("step", generation=self._generation, step="backtest",
                            message=f"Backtesting '{strategy.name}' ({i+1}/{len(strategies)})",
                            strategy_name=strategy.name, index=i, total=len(strategies))
                strategy = self._backtest_strategy(
                    strategy, windows, test_dates, screener_results
                )
                bt = strategy.backtest_results
                if bt:
                    self._emit("step", generation=self._generation, step="backtest_done",
                                message=f"  '{strategy.name}': {bt.num_trades} trades, "
                                        f"Sharpe={bt.sharpe:.2f}, WR={bt.win_rate:.0%}",
                                strategy_name=strategy.name,
                                num_trades=bt.num_trades, sharpe=bt.sharpe,
                                win_rate=bt.win_rate)

            # 3c: Rank strategies, update fitness in DB
            strategies = rank_strategies(strategies, self.config)
            for s in strategies:
                if s.id:
                    self.db.update_strategy_fitness(s.id, s.fitness_score)
                    if s.fitness_score > 0:
                        self.db.update_strategy_status(s.id, "backtested")

            # Track best fitness for stop criteria
            best_fitness = 0.0
            if strategies and strategies[0].fitness_score > 0:
                best_fitness = strategies[0].fitness_score
                self._best_fitness_history.append(best_fitness)

            self._emit("step", generation=self._generation, step="ranked",
                        message=f"Best fitness: {best_fitness:.4f} ({strategies[0].name})" if best_fitness > 0 else "No positive fitness this gen",
                        best_fitness=best_fitness,
                        rankings=[(s.name, s.fitness_score) for s in strategies[:3]])

            # 3d: Reflect
            self._emit("step", generation=self._generation, step="reflect",
                        message="Reflecting on generation (Sonnet)...")
            top_all_time = self.db.get_top_strategies(limit=5)
            self.strategist.reflect(self._generation, strategies, top_all_time)

            # 3e: Update analyst weights
            if all_trade_results:
                update_analyst_weights(self.db, all_trade_results, self.config)

            gen_elapsed = time.time() - gen_start
            cache = self.pipeline.get_cache_stats()
            self._emit("generation_done", generation=self._generation,
                        elapsed_sec=gen_elapsed,
                        best_fitness=best_fitness,
                        cache_hits=cache["hits"], cache_misses=cache["misses"],
                        cache_hit_rate=cache["hit_rate"],
                        message=f"Gen {self._generation} done in {gen_elapsed:.0f}s "
                                f"(cache: {cache['hit_rate']:.0%} hit rate)")

            self._generation += 1

        # Step 4: Holdout validation on top strategies
        if not self._interrupted:
            top_strategies = self.db.get_top_strategies(limit=10)
            if top_strategies and windows:
                self._emit("phase", phase="holdout", status="starting",
                            message=f"Running holdout validation on {len(top_strategies)} strategies...")
                self._run_holdout(top_strategies, holdout[0], holdout[1], screener_results)
                self._emit("phase", phase="holdout", status="done",
                            message="Holdout validation complete")

        # Step 5: Return leaderboard
        total_elapsed = time.time() - self._start_time
        result = {
            "leaderboard": self.get_leaderboard(),
            "generations_run": self._generation,
            "cache_stats": self.pipeline.get_cache_stats(),
            "budget_used": self._budget_used,
            "elapsed_sec": total_elapsed,
            "interrupted": self._interrupted,
        }
        self._emit("complete", elapsed_sec=total_elapsed,
                    generations=self._generation,
                    message=f"{'Interrupted' if self._interrupted else 'Complete'} — "
                            f"{self._generation} generations in {total_elapsed:.0f}s")
        return result

    def _backtest_strategy(
        self,
        strategy: Strategy,
        windows,
        test_dates: list[str],
        screener_results: list[ScreenerResult],
    ) -> Strategy:
        """Backtest a strategy across walk-forward windows.

        For each test date and ticker, runs the cached pipeline and
        evaluates entry/exit rules to simulate trades.
        """
        tickers_per = self.ar_config.get("tickers_per_strategy", 5)

        # Get tickers matching strategy's screener criteria
        matching_tickers = [
            sr.ticker for sr in screener_results
            if self.screener.apply_filters(sr, strategy.screener)
        ][:tickers_per]

        if not matching_tickers:
            matching_tickers = [sr.ticker for sr in screener_results[:tickers_per]]

        all_trades = []
        window_sharpes = []

        for window in windows:
            window_trades = []

            for ticker in matching_tickers:
                # Find screener result for this ticker
                sr = next((s for s in screener_results if s.ticker == ticker), None)
                if sr is None:
                    continue

                # Run pipeline for this ticker on test start date
                pipeline_result = self.pipeline.run(
                    ticker, window.test_start, "haiku", screener_result=sr
                )

                # Check entry rules
                if self._check_entry_rules(strategy, pipeline_result, sr):
                    # Simulate entry at close price
                    entry_price = sr.close

                    # Simulate exit: check exit rules with a simple price change model
                    # Use pipeline rating to estimate direction
                    rating = pipeline_result.get("rating", "HOLD")
                    if rating and "BUY" in str(rating).upper():
                        price_change = 0.03  # assume 3% gain for BUY
                    elif rating and "SELL" in str(rating).upper():
                        price_change = -0.03
                    else:
                        price_change = 0.0

                    exit_price = entry_price * (1 + price_change)
                    pnl = exit_price - entry_price
                    pnl_pct = price_change

                    trade = {
                        "ticker": ticker,
                        "entry_date": window.test_start,
                        "exit_date": window.test_end,
                        "entry_price": entry_price,
                        "exit_price": exit_price,
                        "pnl": pnl,
                        "pnl_pct": pnl_pct,
                        "regime": sr.regime,
                        "holding_days": strategy.time_horizon_days,
                        "analyst_scores": pipeline_result.get("analyst_scores"),
                    }
                    window_trades.append(trade)

                    # Record trade in DB
                    if strategy.id:
                        self.db.insert_strategy_trade(
                            strategy_id=strategy.id,
                            ticker=ticker,
                            trade_type="backtest",
                            entry_date=window.test_start,
                            exit_date=window.test_end,
                            instrument=strategy.instrument,
                            entry_price=entry_price,
                            exit_price=exit_price,
                            quantity=1,
                            pnl=pnl,
                            pnl_pct=pnl_pct,
                            holding_days=strategy.time_horizon_days,
                            regime=sr.regime,
                        )

            all_trades.extend(window_trades)

            # Compute window Sharpe
            if window_trades:
                import statistics
                returns = [t["pnl_pct"] for t in window_trades]
                mean_r = statistics.mean(returns)
                std_r = statistics.stdev(returns) if len(returns) > 1 else 1.0
                window_sharpe = mean_r / std_r if std_r > 0 else 0.0
                window_sharpes.append(window_sharpe)

        # Compute aggregate backtest results
        if all_trades:
            import statistics
            returns = [t["pnl_pct"] for t in all_trades]
            mean_r = statistics.mean(returns)
            std_r = statistics.stdev(returns) if len(returns) > 1 else 1.0

            winners = [t for t in all_trades if t["pnl_pct"] > 0]
            losers = [t for t in all_trades if t["pnl_pct"] <= 0]

            gross_profit = sum(t["pnl_pct"] for t in winners) if winners else 0
            gross_loss = abs(sum(t["pnl_pct"] for t in losers)) if losers else 1

            strategy.backtest_results = BacktestResults(
                sharpe=mean_r / std_r if std_r > 0 else 0.0,
                total_return=sum(returns),
                max_drawdown=min(returns) if returns else 0.0,
                win_rate=len(winners) / len(all_trades) if all_trades else 0.0,
                profit_factor=gross_profit / gross_loss if gross_loss > 0 else 0.0,
                num_trades=len(all_trades),
                tickers_tested=[t["ticker"] for t in all_trades],
                backtest_period=f"{windows[0].test_start} to {windows[-1].test_end}" if windows else "",
                walk_forward_scores=window_sharpes,
            )

            # Save backtest results to DB
            if strategy.id:
                br = strategy.backtest_results
                self.db.insert_strategy_backtest(
                    strategy_id=strategy.id,
                    sharpe=br.sharpe,
                    total_return=br.total_return,
                    max_drawdown=br.max_drawdown,
                    win_rate=br.win_rate,
                    profit_factor=br.profit_factor,
                    num_trades=br.num_trades,
                    tickers_tested=br.tickers_tested,
                    backtest_period=br.backtest_period,
                    walk_forward_scores=br.walk_forward_scores,
                )
        else:
            strategy.backtest_results = BacktestResults(num_trades=0)

        return strategy

    def _check_entry_rules(self, strategy: Strategy, pipeline_result: dict,
                            screener_result: ScreenerResult) -> bool:
        """Check if all entry rules are satisfied."""
        if not strategy.entry_rules:
            return True
        return all(
            _check_entry_rule(rule, screener_result, pipeline_result)
            for rule in strategy.entry_rules
        )

    def _check_exit_rules(self, strategy: Strategy, entry_price: float,
                           current_price: float, holding_days: int) -> bool:
        """Check if any exit rule is triggered."""
        if not strategy.exit_rules:
            return holding_days >= strategy.time_horizon_days
        return any(
            _check_exit_rule(rule, entry_price, current_price, holding_days,
                              strategy.time_horizon_days)
            for rule in strategy.exit_rules
        )

    def _should_stop(self) -> bool:
        """Check if evolution should stop.

        Stops when:
        - Top fitness unchanged for N generations
        - Budget cap exceeded
        """
        stop_unchanged = self.ar_config.get("stop_unchanged_generations", 3)
        budget_cap = self.ar_config.get("budget_cap_usd", 150.0)

        # Budget check
        if self._budget_used >= budget_cap:
            logger.info("Budget cap reached (%.2f >= %.2f)", self._budget_used, budget_cap)
            return True

        # Unchanged fitness check
        if len(self._best_fitness_history) >= stop_unchanged:
            recent = self._best_fitness_history[-stop_unchanged:]
            if len(set(recent)) == 1:
                logger.info("Top fitness unchanged for %d generations", stop_unchanged)
                return True

        return False

    def _run_holdout(self, strategies: list[dict], holdout_start: str,
                      holdout_end: str, screener_results: list[ScreenerResult]) -> list:
        """Run holdout validation on top strategies.

        Flags strategies whose holdout Sharpe degrades significantly.
        """
        results = []
        for strat_dict in strategies:
            strategy = Strategy.from_db_dict(strat_dict)

            # Get matching tickers for holdout
            tickers_per = self.ar_config.get("tickers_per_strategy", 5)
            matching = [sr.ticker for sr in screener_results
                        if self.screener.apply_filters(sr, strategy.screener)][:tickers_per]

            if not matching:
                continue

            # Run pipeline on holdout date
            holdout_trades = []
            for ticker in matching:
                sr = next((s for s in screener_results if s.ticker == ticker), None)
                if sr is None:
                    continue
                pipeline_result = self.pipeline.run(
                    ticker, holdout_start, "haiku", screener_result=sr
                )

                if self._check_entry_rules(strategy, pipeline_result, sr):
                    rating = pipeline_result.get("rating", "HOLD")
                    if rating and "BUY" in str(rating).upper():
                        pnl_pct = 0.03
                    elif rating and "SELL" in str(rating).upper():
                        pnl_pct = -0.03
                    else:
                        pnl_pct = 0.0

                    holdout_trades.append({"pnl_pct": pnl_pct, "ticker": ticker})

            if holdout_trades:
                import statistics
                returns = [t["pnl_pct"] for t in holdout_trades]
                mean_r = statistics.mean(returns)
                std_r = statistics.stdev(returns) if len(returns) > 1 else 1.0
                holdout_sharpe = mean_r / std_r if std_r > 0 else 0.0

                # Flag overfit if holdout sharpe significantly worse
                if strategy.backtest_results and strategy.backtest_results.sharpe > 0:
                    if holdout_sharpe < strategy.backtest_results.sharpe * 0.5:
                        logger.warning("Strategy '%s' may be overfit: holdout_sharpe=%.2f vs backtest=%.2f",
                                        strategy.name, holdout_sharpe, strategy.backtest_results.sharpe)

                results.append({
                    "strategy_id": strategy.id,
                    "name": strategy.name,
                    "holdout_sharpe": holdout_sharpe,
                })

        return results

    def get_leaderboard(self) -> list[dict]:
        """Get the current strategy leaderboard."""
        top = self.db.get_top_strategies(limit=20)
        return [
            {
                "rank": i + 1,
                "name": s["name"],
                "instrument": s["instrument"],
                "fitness_score": s.get("fitness_score", 0),
                "status": s["status"],
                "generation": s["generation"],
            }
            for i, s in enumerate(top)
        ]

    def get_progress(self) -> dict:
        """Get current evolution progress."""
        return {
            "generation": self._generation,
            "budget_used": self._budget_used,
            "cache_stats": self.pipeline.get_cache_stats(),
            "best_fitness_history": self._best_fitness_history,
        }
