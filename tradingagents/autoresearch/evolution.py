"""Evolution engine: main orchestrator for autoresearch strategy discovery."""

import logging
import re
import signal
import statistics
import time
from datetime import datetime, timedelta
from typing import Callable, Optional

import pandas as pd

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
    # RSI thresholds
    r"RSI_14 > (\d+)": lambda sr, m: sr.rsi_14 > float(m.group(1)),
    r"RSI_14 < (\d+)": lambda sr, m: sr.rsi_14 < float(m.group(1)),
    r"RSI_14 between (\d+) and (\d+)": lambda sr, m: float(m.group(1)) <= sr.rsi_14 <= float(m.group(2)),
    # Price vs EMAs
    r"price > EMA_10": lambda sr, m: sr.close > sr.ema_10,
    r"price < EMA_10": lambda sr, m: sr.close < sr.ema_10,
    r"price > EMA_50": lambda sr, m: sr.close > sr.ema_50,
    r"price < EMA_50": lambda sr, m: sr.close < sr.ema_50,
    # EMA crossovers
    r"EMA_10 > EMA_50": lambda sr, m: sr.ema_10 > sr.ema_50,
    r"EMA_10 < EMA_50": lambda sr, m: sr.ema_10 < sr.ema_50,
    # MACD
    r"MACD > 0": lambda sr, m: sr.macd > 0,
    r"MACD < 0": lambda sr, m: sr.macd < 0,
    # Bollinger Bands
    r"bollinger > ([\d.]+)": lambda sr, m: sr.boll_position > float(m.group(1)),
    r"bollinger < ([\d.]+)": lambda sr, m: sr.boll_position < float(m.group(1)),
    # Volume
    r"volume_ratio > ([\d.]+)": lambda sr, m: sr.volume_ratio > float(m.group(1)),
    r"volume_ratio < ([\d.]+)": lambda sr, m: sr.volume_ratio < float(m.group(1)),
    # 52-week range position
    r"52w_position > ([\d.]+)": lambda sr, m: ((sr.close - sr.low_52w) / max(sr.high_52w - sr.low_52w, 0.01)) > float(m.group(1)),
    r"52w_position < ([\d.]+)": lambda sr, m: ((sr.close - sr.low_52w) / max(sr.high_52w - sr.low_52w, 0.01)) < float(m.group(1)),
    # Momentum
    r"change_14d > ([-\d.]+)": lambda sr, m: sr.change_14d > float(m.group(1)),
    r"change_14d < ([-\d.]+)": lambda sr, m: sr.change_14d < float(m.group(1)),
    r"change_30d > ([-\d.]+)": lambda sr, m: sr.change_30d > float(m.group(1)),
    r"change_30d < ([-\d.]+)": lambda sr, m: sr.change_30d < float(m.group(1)),
    # Pipeline signal
    r"BUY signal from pipeline": None,  # handled separately
    r"SELL signal from pipeline": None,
    r"HOLD signal from pipeline": None,
}


def _check_entry_rule(rule: str, screener_result: ScreenerResult,
                       pipeline_result: dict = None,
                       backtest_mode: bool = False) -> bool:
    """Check if a single entry rule is satisfied.

    In backtest_mode, pipeline signal rules default to True since we don't
    run the LLM pipeline during backtesting.
    """
    rule_lower = rule.lower().strip()

    # Pipeline signal rules
    if "signal from pipeline" in rule_lower:
        if backtest_mode or pipeline_result is None:
            return True  # no pipeline in backtest — let screener rules filter
        if "buy" in rule_lower:
            return pipeline_result.get("rating", "").upper() in ("BUY", "STRONG BUY")
        if "sell" in rule_lower:
            return pipeline_result.get("rating", "").upper() in ("SELL", "STRONG SELL")
        if "hold" in rule_lower:
            return pipeline_result.get("rating", "").upper() == "HOLD"
        return True

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
        self._screener_cache: dict[tuple[str, str], ScreenerResult | None] = {}
        self._forward_price_cache: dict[str, pd.DataFrame] = {}  # ticker -> full OHLCV df

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

        # Step 2b: Prefetch forward price data for trade simulation
        universe_tickers = [sr.ticker for sr in screener_results]
        earliest_test = windows[0].test_start if windows else start_date
        self._prefetch_forward_prices(universe_tickers, earliest_test, end_date)

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

            # 3b-pre: Batch-fetch historical screener data for all test dates
            all_test_dates = [w.test_start for w in windows]
            universe_tickers = [sr.ticker for sr in screener_results]
            self._prefetch_screener_data(universe_tickers, all_test_dates, regime)

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
        """Backtest a strategy across walk-forward windows using real prices.

        For each test date and ticker, checks entry rules against screener data.
        If entry triggers, simulates the trade against real forward prices
        from yfinance (no LLM calls).
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
                sr_fallback = next((s for s in screener_results if s.ticker == ticker), None)
                if sr_fallback is None:
                    continue

                sr = self._get_screener_for_date(ticker, window.test_start, sr_fallback)

                # Check entry rules (backtest mode — no pipeline)
                if self._check_entry_rules(strategy, sr, backtest_mode=True):
                    entry_price = sr.close

                    # Simulate trade with real forward prices
                    trade = self._simulate_trade(
                        strategy, ticker, window.test_start, entry_price,
                        window.test_end, sr.regime,
                    )
                    if trade is None:
                        continue

                    window_trades.append(trade)

                    # Record trade in DB
                    if strategy.id:
                        self.db.insert_strategy_trade(
                            strategy_id=strategy.id,
                            ticker=ticker,
                            trade_type="backtest",
                            entry_date=trade["entry_date"],
                            exit_date=trade["exit_date"],
                            instrument=strategy.instrument,
                            entry_price=trade["entry_price"],
                            exit_price=trade["exit_price"],
                            quantity=1,
                            pnl=trade["pnl"],
                            pnl_pct=trade["pnl_pct"],
                            holding_days=trade["holding_days"],
                            regime=trade["regime"],
                        )

            all_trades.extend(window_trades)

            # Compute window Sharpe
            if window_trades:
                returns = [t["pnl_pct"] for t in window_trades]
                mean_r = statistics.mean(returns)
                std_r = statistics.stdev(returns) if len(returns) > 1 else 1.0
                window_sharpe = mean_r / std_r if std_r > 0 else 0.0
                window_sharpes.append(window_sharpe)

        # Compute aggregate backtest results
        if all_trades:
            returns = [t["pnl_pct"] for t in all_trades]
            mean_r = statistics.mean(returns)
            std_r = statistics.stdev(returns) if len(returns) > 1 else 1.0

            winners = [t for t in all_trades if t["pnl_pct"] > 0]
            losers = [t for t in all_trades if t["pnl_pct"] < 0]

            gross_profit = sum(t["pnl_pct"] for t in winners) if winners else 0
            gross_loss = abs(sum(t["pnl_pct"] for t in losers)) if losers else 0.001

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

    def _simulate_trade(self, strategy: Strategy, ticker: str,
                        entry_date: str, entry_price: float,
                        window_end: str, regime: str) -> dict | None:
        """Simulate a trade using real forward prices.

        Walks daily prices from entry_date, checking exit rules each day:
        - Profit target: close >= entry * (1 + target%)
        - Stop loss: close <= entry * (1 - stop%)
        - Trailing stop: close <= high_water_mark * (1 - trail%)
        - Time horizon exceeded
        - Window end reached

        Returns trade dict or None if no price data available.
        """
        if ticker not in self._forward_price_cache:
            return None

        price_df = self._forward_price_cache[ticker]
        if price_df.empty:
            return None

        # Slice from entry_date forward
        try:
            entry_dt = pd.Timestamp(entry_date)
            window_end_dt = pd.Timestamp(window_end)
            forward = price_df[price_df.index >= entry_dt]
        except Exception:
            return None

        if forward.empty:
            return None

        # Parse exit rule parameters
        profit_target = None
        stop_loss = None
        trailing_stop = None
        for rule in strategy.exit_rules:
            rule_lower = rule.lower().strip()
            m = re.match(r"(\d+)%?\s*profit\s*target", rule_lower)
            if m:
                profit_target = float(m.group(1)) / 100.0
                continue
            m = re.match(r"(\d+)%?\s*stop\s*loss", rule_lower)
            if m:
                stop_loss = float(m.group(1)) / 100.0
                continue
            m = re.match(r"(\d+)%?\s*trailing\s*stop", rule_lower)
            if m:
                trailing_stop = float(m.group(1)) / 100.0
                continue

        is_short = strategy.instrument in ("stock_short", "put_option")
        high_water_mark = entry_price
        exit_price = entry_price
        exit_date = entry_date
        exit_reason = "window_end"
        holding_days = 0

        for i, (date_idx, row) in enumerate(forward.iterrows()):
            close = float(row["Close"])
            current_dt = pd.Timestamp(date_idx)
            holding_days = i  # trading days since entry

            # Update high water mark for trailing stop
            if not is_short:
                high_water_mark = max(high_water_mark, close)
            else:
                high_water_mark = min(high_water_mark, close)

            # Skip entry day (we enter at close, check exits starting next day)
            if i == 0:
                continue

            exit_price = close
            exit_date = current_dt.strftime("%Y-%m-%d")

            # Check profit target
            if profit_target is not None:
                if not is_short and close >= entry_price * (1 + profit_target):
                    exit_reason = "profit_target"
                    break
                if is_short and close <= entry_price * (1 - profit_target):
                    exit_reason = "profit_target"
                    break

            # Check stop loss
            if stop_loss is not None:
                if not is_short and close <= entry_price * (1 - stop_loss):
                    exit_reason = "stop_loss"
                    break
                if is_short and close >= entry_price * (1 + stop_loss):
                    exit_reason = "stop_loss"
                    break

            # Check trailing stop
            if trailing_stop is not None:
                if not is_short and close <= high_water_mark * (1 - trailing_stop):
                    exit_reason = "trailing_stop"
                    break
                if is_short and close >= high_water_mark * (1 + trailing_stop):
                    exit_reason = "trailing_stop"
                    break

            # Check time horizon
            if holding_days >= strategy.time_horizon_days:
                exit_reason = "time_horizon"
                break

            # Check window end
            if current_dt >= window_end_dt:
                exit_reason = "window_end"
                break

        # Compute PnL
        if is_short:
            pnl = entry_price - exit_price
            pnl_pct = (entry_price - exit_price) / entry_price if entry_price else 0
        else:
            pnl = exit_price - entry_price
            pnl_pct = (exit_price - entry_price) / entry_price if entry_price else 0

        return {
            "ticker": ticker,
            "entry_date": entry_date,
            "exit_date": exit_date,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "regime": regime,
            "holding_days": holding_days,
            "exit_reason": exit_reason,
        }

    def _check_entry_rules(self, strategy: Strategy,
                            screener_result: ScreenerResult,
                            pipeline_result: dict = None,
                            backtest_mode: bool = False) -> bool:
        """Check if all entry rules are satisfied."""
        if not strategy.entry_rules:
            return True
        return all(
            _check_entry_rule(rule, screener_result, pipeline_result,
                               backtest_mode=backtest_mode)
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

    def _prefetch_screener_data(self, tickers: list[str], dates: list[str],
                                regime: str) -> None:
        """Batch-fetch screener data for all (ticker, date) combos not yet cached."""
        for date in dates:
            # Skip if all tickers already cached for this date
            uncached = [t for t in tickers if (t, date) not in self._screener_cache]
            if not uncached:
                continue

            self._emit("step", generation=self._generation, step="screener_fetch",
                        message=f"Fetching historical data for {len(uncached)} tickers on {date}")
            results = self.screener.batch_fetch(uncached, date, regime)

            # Populate cache
            fetched_tickers = set()
            for sr in results:
                self._screener_cache[(sr.ticker, date)] = sr
                fetched_tickers.add(sr.ticker)

            # Mark missing tickers as None so we don't re-fetch
            for t in uncached:
                if t not in fetched_tickers:
                    self._screener_cache[(t, date)] = None

    def _get_screener_for_date(self, ticker: str, date: str,
                               fallback: ScreenerResult) -> ScreenerResult:
        """Get screener data for a ticker on a specific date, with caching.

        Falls back to the end-date screener result if historical fetch fails.
        """
        key = (ticker, date)
        if key in self._screener_cache:
            cached = self._screener_cache[key]
            return cached if cached is not None else fallback

        result = self.screener.fetch_ticker_data(ticker, date)
        if result is not None:
            # Inherit regime from fallback (regime is set at universe level)
            result.regime = fallback.regime
        self._screener_cache[key] = result
        return result if result is not None else fallback

    def _prefetch_forward_prices(self, tickers: list[str],
                                  earliest_date: str, end_date: str) -> None:
        """Bulk-fetch forward price data for all tickers across the full date range."""
        if self._forward_price_cache:
            return  # already fetched

        self._emit("phase", phase="forward_prices", status="starting",
                    message=f"Fetching forward prices for {len(tickers)} tickers...")
        price_data = self.screener.fetch_forward_prices(tickers, earliest_date, end_date)
        self._forward_price_cache.update(price_data)
        self._emit("phase", phase="forward_prices", status="done",
                    message=f"Forward prices cached for {len(price_data)} tickers")

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
        """Run holdout validation on top strategies using real prices.

        Flags strategies whose holdout Sharpe degrades significantly.
        """
        results = []
        for strat_dict in strategies:
            strategy = Strategy.from_db_dict(strat_dict)

            tickers_per = self.ar_config.get("tickers_per_strategy", 5)
            matching = [sr.ticker for sr in screener_results
                        if self.screener.apply_filters(sr, strategy.screener)][:tickers_per]

            if not matching:
                continue

            holdout_trades = []
            for ticker in matching:
                sr_fallback = next((s for s in screener_results if s.ticker == ticker), None)
                if sr_fallback is None:
                    continue
                sr = self._get_screener_for_date(ticker, holdout_start, sr_fallback)

                if self._check_entry_rules(strategy, sr, backtest_mode=True):
                    trade = self._simulate_trade(
                        strategy, ticker, holdout_start, sr.close,
                        holdout_end, sr.regime,
                    )
                    if trade is not None:
                        holdout_trades.append(trade)

            if holdout_trades:
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
