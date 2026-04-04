"""Paper-trading-first multi-strategy engine.

Screens event-driven strategies for signals, synthesizes through a
portfolio committee, gates through risk controls, and executes via
PaperBroker or AlpacaBroker.
"""
from __future__ import annotations

import logging
import statistics
from datetime import datetime, timedelta
from typing import Any, Callable

import pandas as pd

from tradingagents.autoresearch.data_sources.registry import (
    DataSourceRegistry,
    build_default_registry,
)
from tradingagents.autoresearch.state import StateManager
from tradingagents.autoresearch.strategies import get_paper_trade_strategies
from tradingagents.autoresearch.strategies.base import Candidate

logger = logging.getLogger(__name__)


class MultiStrategyEngine:
    """Paper-trading-first strategy engine.

    Screens event-driven strategies for signals, synthesizes through
    a portfolio committee, gates through risk controls, and executes
    via PaperBroker or AlpacaBroker. Weights evolve through a
    conservative learning loop based on realized trade outcomes.
    """

    def __init__(
        self,
        config: dict | None = None,
        strategies: list | None = None,
        registry: DataSourceRegistry | None = None,
        state_manager: StateManager | None = None,
        on_event: Callable | None = None,
        use_llm: bool = False,
        adaptive_confidence: bool = False,
    ):
        self.config = config or {}
        self.ar_config = self.config.get("autoresearch", {})

        # Load strategies (paper-trade only)
        self.paper_trade_strategies = strategies or get_paper_trade_strategies()

        # Data source registry
        self.registry = registry or build_default_registry(self.ar_config)

        # State
        self.state = state_manager or StateManager(
            self.ar_config.get("state_dir", "data/state")
        )

        # Event callback
        self._on_event = on_event or (lambda kind, **kw: None)

        # LLM analyzer for paper-trade signal enrichment
        self._analyzer = None
        if use_llm:
            from tradingagents.autoresearch.llm_analyzer import LLMAnalyzer
            self._analyzer = LLMAnalyzer(self.config)

        # Price cache: ticker -> DataFrame
        self._price_cache: dict[str, pd.DataFrame] = {}

        # Adaptive confidence: journal-derived (True) or fixed 0.5 (False)
        self._adaptive_confidence = adaptive_confidence

        # Signal journal (shared across methods)
        from tradingagents.autoresearch.signal_journal import SignalJournal
        self._journal = SignalJournal(self.ar_config.get("state_dir", "data/state"))

    def _emit(self, kind: str, **data: Any) -> None:
        self._on_event(kind, **data)

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def screen_and_enrich(
        self,
        trading_date: str,
        data: dict,
    ) -> tuple[list[dict], dict]:
        """Run strategy screening and LLM enrichment (steps 1-2).

        Returns enriched, deduped signals and regime model. These can be
        shared across cohorts so LLM non-determinism doesn't confound results.
        """
        regime_model = self._build_regime_model(data)
        self.state.save_regime_snapshot(regime_model)

        all_signals: list[dict] = []
        for strategy in self.paper_trade_strategies:
            self._emit("strategy_start", name=strategy.name, track="paper_trade")
            params = strategy.get_default_params()
            candidates = strategy.screen(data, trading_date, params)
            if candidates:
                candidates = self._enrich_with_llm(candidates, strategy.name, regime_context=regime_model)
            for c in candidates:
                all_signals.append({
                    "ticker": c.ticker,
                    "direction": c.direction,
                    "score": c.score,
                    "strategy": strategy.name,
                    "metadata": c.metadata,
                })
            self._emit("strategy_done", name=strategy.name, num_signals=len(candidates))

        # Dedup and resolve conflicting signals
        seen: dict[tuple[str, str, str], dict] = {}
        for signal in all_signals:
            key = (signal["strategy"], signal["ticker"], signal["direction"])
            if key not in seen or signal["score"] > seen[key]["score"]:
                seen[key] = signal
        to_remove: set[tuple[str, str, str]] = set()
        for (strat, ticker, direction) in seen:
            opposite = "short" if direction == "long" else "long"
            opp_key = (strat, ticker, opposite)
            if opp_key in seen:
                to_remove.add((strat, ticker, direction))
                to_remove.add(opp_key)
        for key in to_remove:
            seen.pop(key, None)
        deduped_signals = [s for s in seen.values() if s.get("ticker", "").strip()]

        # Filter blocked tickers
        blocked = set(t.upper() for t in self.ar_config.get("blocked_tickers", []))
        if blocked:
            before = len(deduped_signals)
            deduped_signals = [s for s in deduped_signals if s["ticker"].upper() not in blocked]
            removed = before - len(deduped_signals)
            if removed:
                logger.info("Blocked %d signals for tickers: %s", removed, blocked)

        return deduped_signals, regime_model

    def run_paper_trade_phase(
        self,
        trading_date: str | None = None,
        data: dict | None = None,
        shared_signals: list[dict] | None = None,
        shared_regime: dict | None = None,
        enrichment: dict | None = None,
    ) -> dict:
        """Paper trading loop: screen → committee → risk gate → execute.

        Signals flow through:
        1. Strategy screen (deterministic filtering)
        2. LLM enrichment (classification, not prediction)
        3. Portfolio committee (synthesis, sizing)
        4. Risk gate (hard limits, position sizing)
        5. Execution (PaperBroker or AlpacaBroker)
        6. Signal journal (all signals logged, outcomes tracked)

        Args:
            trading_date: Date to trade (default: today).
            data: Pre-fetched data dict. If provided, skips data fetch
                  (used by CohortOrchestrator to share data across cohorts).
            shared_signals: Pre-computed enriched signals from screen_and_enrich().
                           If provided, skips steps 1-2 (used by orchestrator).
            shared_regime: Pre-computed regime model from screen_and_enrich().
        """
        if not trading_date:
            trading_date = datetime.now().strftime("%Y-%m-%d")

        lookback_start = (datetime.strptime(trading_date, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")

        # Use shared signals if provided (from orchestrator), otherwise run locally
        if shared_signals is not None:
            import copy
            deduped_signals = copy.deepcopy(shared_signals)
            regime_model = shared_regime or {}
            self.state.save_regime_snapshot(regime_model)
        else:
            if data is None:
                data = self._fetch_all_data(lookback_start, trading_date)
            deduped_signals, regime_model = self.screen_and_enrich(trading_date, data)

        # Compute strategy confidence (this IS cohort-specific)
        strategy_confidence: dict[str, float] = {}
        strategies_in_signals = {s["strategy"] for s in deduped_signals}
        for strat_name in strategies_in_signals:
            if self._adaptive_confidence:
                strategy_confidence[strat_name] = self._compute_strategy_confidence(strat_name)
            else:
                strategy_confidence[strat_name] = 0.5

        # ------------------------------------------------------------------
        # 3. Fetch prices for signal tickers
        # ------------------------------------------------------------------
        missing_tickers = sorted({
            s["ticker"] for s in deduped_signals
            if s["ticker"] not in self._price_cache
        })
        if missing_tickers:
            self._fetch_missing_prices(missing_tickers, lookback_start, trading_date)

        # ------------------------------------------------------------------
        # 4. Portfolio committee synthesis → sized recommendations
        # ------------------------------------------------------------------
        from tradingagents.autoresearch.execution_bridge import ExecutionBridge
        from tradingagents.autoresearch.paper_trader import PaperTrader
        from tradingagents.autoresearch.portfolio_committee import PortfolioCommittee
        from tradingagents.autoresearch.signal_journal import JournalEntry

        bridge = ExecutionBridge(self.config)
        bridge.risk_gate.reset_daily(trading_date)
        bridge.risk_gate.update_high_water_mark()

        # Reconstruct broker state from persistent trades
        open_trades_for_broker = self.state.load_paper_trades(status="open")
        if open_trades_for_broker and hasattr(bridge.broker, "reconstruct_from_trades"):
            bridge.broker.reconstruct_from_trades(open_trades_for_broker)
            logger.info(
                "Reconstructed broker: cash=$%.2f, %d positions",
                bridge.broker.cash, len(bridge.broker.positions),
            )

        committee = PortfolioCommittee(self.config)
        recommendations = committee.synthesize(
            signals=deduped_signals,
            regime_context=regime_model,
            strategy_confidence=strategy_confidence,
            current_positions=bridge.get_positions(),
            total_capital=bridge.get_account().portfolio_value,
            enrichment=enrichment,
        )

        # ------------------------------------------------------------------
        # 5. Execute through risk gate (sized, gated trades)
        # ------------------------------------------------------------------
        trader = PaperTrader(self.state)
        trades_opened = []
        traded_tickers: set[str] = set()
        open_trades = self.state.load_paper_trades(status="open")

        # Idempotency: skip tickers already traded today
        already_traded_today = {
            t["ticker"] for t in open_trades
            if t.get("entry_date") == trading_date
        }

        for rec in recommendations:
            if rec.ticker in already_traded_today or rec.ticker in traded_tickers:
                continue

            # Get current price
            ticker_prices = self._price_cache.get(rec.ticker)
            if ticker_prices is None:
                continue
            try:
                current_price = float(ticker_prices["Close"].iloc[-1])
            except (KeyError, IndexError):
                continue
            if current_price <= 0:
                continue

            primary_strategy = rec.contributing_strategies[0] if rec.contributing_strategies else ""

            # Execute through bridge (size → gate → broker)
            result = bridge.execute_recommendation(
                ticker=rec.ticker,
                direction=rec.direction,
                position_size_pct=rec.position_size_pct,
                confidence=rec.confidence,
                strategy=primary_strategy,
                current_price=current_price,
                open_trades=open_trades,
            )

            if result and result.status == "filled":
                # Record in PaperTrader for journal/state tracking
                trade_id = trader.open_trade(
                    strategy=primary_strategy,
                    ticker=rec.ticker,
                    direction=rec.direction,
                    entry_price=result.filled_price,
                    entry_date=trading_date,
                    shares=result.filled_qty,
                    position_value=result.filled_qty * result.filled_price,
                )
                trades_opened.append(trade_id)
                traded_tickers.add(rec.ticker)

        # ------------------------------------------------------------------
        # 6. Log all signals to journal (traded and untraded)
        # ------------------------------------------------------------------
        regime_label = regime_model.get("overall_regime", "") if regime_model else ""
        journal_entries = []
        for signal in deduped_signals:
            ticker_prices = self._price_cache.get(signal["ticker"])
            price = 0.0
            if ticker_prices is not None and not ticker_prices.empty:
                try:
                    price = float(ticker_prices["Close"].iloc[-1])
                except (KeyError, IndexError):
                    pass

            was_traded = signal["ticker"] in traded_tickers
            llm_analysis = signal.get("metadata", {}).get("llm_analysis", {}) if isinstance(signal.get("metadata"), dict) else {}

            # Track which prompt version generated this signal
            prompt_version = ""
            if self._analyzer:
                from tradingagents.autoresearch.prompt_optimizer import PromptOptimizer
                po = PromptOptimizer(self.ar_config.get("state_dir", "data/state"), self._analyzer)
                prompt_version = po.get_prompt_version(signal["strategy"])

            journal_entries.append(JournalEntry(
                timestamp=trading_date,
                strategy=signal["strategy"],
                ticker=signal["ticker"],
                direction=signal["direction"],
                score=signal["score"],
                llm_conviction=llm_analysis.get("conviction", llm_analysis.get("score", 0.0)),
                regime=regime_label,
                traded=was_traded,
                entry_price=price,
                prompt_version=prompt_version,
            ))

        if journal_entries:
            self._journal.log_signals(journal_entries)
            logger.info("Signal journal: logged %d signals (%d traded)", len(journal_entries), len(trades_opened))

        # Convergence detection
        convergence = self._journal.get_convergence_signals(trading_date, min_strategies=2)
        if convergence:
            logger.info("Convergence signals: %s", [(c["ticker"], c["direction"], c["strategies"]) for c in convergence])

        # Back-fill outcomes for past entries
        outcomes_filled = self._journal.fill_outcomes(self._price_cache, trading_date)
        if outcomes_filled:
            logger.info("Signal journal: filled outcomes for %d past entries", outcomes_filled)

        # ------------------------------------------------------------------
        # 7. Check exits on open positions (strategy rules + stop loss)
        # ------------------------------------------------------------------
        trades_closed = []
        open_trades = self.state.load_paper_trades(status="open")

        # Force-close positions hitting global stop loss
        force_close_ids = bridge.risk_gate.enforce_stop_losses(open_trades, self._price_cache)
        for trade_id in force_close_ids:
            trade = next((t for t in open_trades if t.get("trade_id") == trade_id), None)
            if trade:
                ticker_prices = self._price_cache.get(trade["ticker"])
                if ticker_prices is not None and not ticker_prices.empty:
                    try:
                        exit_price = float(ticker_prices["Close"].iloc[-1])
                        trader.close_trade(trade_id, exit_price=exit_price, exit_date=trading_date, exit_reason="stop_loss")
                        trades_closed.append(trade_id)
                        # Track daily losses for risk gate
                        pnl = (exit_price - trade.get("entry_price", 0)) * trade.get("shares", 1)
                        if pnl < 0:
                            bridge.risk_gate.record_daily_loss(abs(pnl))
                    except (KeyError, IndexError):
                        pass

        # Check strategy-specific exit rules
        still_open = [t for t in open_trades if t.get("trade_id") not in set(force_close_ids)]
        for trade in still_open:
            ticker = trade.get("ticker", "")
            strat_name = trade.get("strategy", "")
            strategy = next((s for s in self.paper_trade_strategies if s.name == strat_name), None)
            if not strategy:
                continue

            ticker_prices = self._price_cache.get(ticker)
            if ticker_prices is None or ticker_prices.empty:
                continue

            try:
                current_price = float(ticker_prices["Close"].iloc[-1])
            except (KeyError, IndexError):
                continue

            entry_price = trade.get("entry_price", 0)
            entry_date = trade.get("entry_date", trading_date)
            holding_days = (pd.Timestamp(trading_date) - pd.Timestamp(entry_date)).days

            params = strategy.get_default_params()
            should_exit, reason = strategy.check_exit(
                ticker=ticker,
                entry_price=entry_price,
                current_price=current_price,
                holding_days=holding_days,
                params=params,
                data=data,
            )
            if should_exit:
                trader.close_trade(trade["trade_id"], exit_price=current_price, exit_date=trading_date, exit_reason=reason)
                trades_closed.append(trade["trade_id"])

        return {
            "signals": deduped_signals,
            "recommendations": [r.__dict__ if hasattr(r, '__dict__') else r for r in recommendations],
            "regime": regime_model,
            "trades_opened": trades_opened,
            "trades_closed": trades_closed,
            "account": bridge.get_account().__dict__,
        }

    def run_learning_loop(self) -> dict:
        """Phase 2 learning loop: Evaluate strategy performance and optimize prompts."""
        if not self._should_trigger_learning_loop():
            return {"triggered": False, "strategies_evaluated": 0}

        scores: dict[str, float] = {}
        trade_counts: dict[str, int] = {}

        for strategy in self.paper_trade_strategies:
            trades = self.state.load_paper_trades(strategy=strategy.name, status="closed")
            trade_counts[strategy.name] = len(trades)

            if not trades:
                scores[strategy.name] = 0.0
                continue

            # Compute Sharpe from PnL (with fallback for trades closed before pnl field existed)
            pnls = []
            for t in trades:
                p = t.get("pnl")
                if p is not None:
                    pnls.append(p)
                else:
                    entry = t.get("entry_price", 0)
                    exit_ = t.get("exit_price", 0)
                    if entry > 0 and exit_ > 0:
                        raw = (exit_ - entry) / entry
                        if t.get("direction") == "short":
                            raw = -raw
                        pnls.append(raw * entry * t.get("shares", 1))
            if len(pnls) > 1:
                mean_pnl = statistics.mean(pnls)
                std_pnl = statistics.stdev(pnls)
                scores[strategy.name] = mean_pnl / std_pnl if std_pnl > 0 else 0.0
            elif pnls:
                scores[strategy.name] = pnls[0]
            else:
                scores[strategy.name] = 0.0

        # ------------------------------------------------------------------
        # Prompt optimization (Atlas-GIC inspired)
        # ------------------------------------------------------------------
        prompt_optimization_result: dict[str, Any] = {}
        if self._analyzer:
            from tradingagents.autoresearch.prompt_optimizer import PromptOptimizer

            state_dir = self.ar_config.get("state_dir", "data/state")
            optimizer = PromptOptimizer(state_dir, self._analyzer)

            # Check active trial first
            trial_id, trial = optimizer.get_active_trial()
            if trial_id:
                decision = optimizer.check_trial(trial_id, self._journal)
                if decision in ("keep", "revert"):
                    optimizer.commit_or_revert(trial_id, decision)
                    prompt_optimization_result["trial_completed"] = {
                        "trial_id": trial_id, "decision": decision,
                    }
                else:
                    prompt_optimization_result["trial_ongoing"] = trial_id
            else:
                # No active trial — evaluate and potentially start one
                prompt_scores = optimizer.evaluate_prompts(self._journal)
                worst = optimizer.identify_worst_prompt(prompt_scores)
                if worst:
                    current_prompt = self._analyzer.get_prompt(worst)
                    failures = self._journal.get_high_conviction_failures(worst, limit=10)
                    if failures:
                        new_prompt = optimizer.propose_modification(worst, current_prompt, failures)
                        if new_prompt != current_prompt:
                            new_trial_id = optimizer.start_trial(worst, new_prompt)
                            prompt_optimization_result["trial_started"] = {
                                "trial_id": new_trial_id, "strategy": worst,
                            }
                prompt_optimization_result["prompt_scores"] = {
                    k: {"hit_rate": v["hit_rate"], "n_signals": v["n_signals"]}
                    for k, v in prompt_scores.items()
                }

        # Update learning loop state
        ll_state = self.state.load_learning_loop_state()
        ll_state["last_run"] = datetime.now().isoformat()
        ll_state["strategies_evaluated"] = list(scores.keys())
        self.state.save_learning_loop_state(ll_state)

        return {
            "triggered": True,
            "strategies_evaluated": len(scores),
            "scores": scores,
            "trade_counts": trade_counts,
            "prompt_optimization": prompt_optimization_result,
        }

    # ------------------------------------------------------------------
    # Strategy confidence
    # ------------------------------------------------------------------

    def _compute_strategy_confidence(self, strategy_name: str) -> float:
        """Compute confidence from signal journal hit rates.

        Maps hit_rate [0.3, 0.7] → confidence [0.2, 0.9].
        Returns 0.5 (neutral) if fewer than 10 signals with outcomes.
        """
        entries = self._journal.get_entries(strategy=strategy_name)
        with_outcomes = [e for e in entries if e.get("return_5d") is not None]

        if len(with_outcomes) < 10:
            return 0.5  # neutral until proven

        hits = sum(
            1 for e in with_outcomes
            if (e["direction"] == "long" and e["return_5d"] > 0)
            or (e["direction"] == "short" and e["return_5d"] < 0)
        )
        hit_rate = hits / len(with_outcomes)

        # Linear map: 30% hit rate → 0.2 confidence, 70% → 0.9
        return max(0.2, min(0.9, (hit_rate - 0.3) / 0.4 * 0.7 + 0.2))

    # ------------------------------------------------------------------
    # Regime model helpers
    # ------------------------------------------------------------------

    def _build_regime_model(self, data: dict) -> dict:
        """Build regime model from available data (VIX, credit spreads, yield curve)."""
        vix_data = data.get("yfinance", {}).get("vix")
        vix_level = 0.0
        if vix_data is not None and not vix_data.empty:
            vix_level = float(vix_data["Close"].iloc[-1])

        fred = data.get("fred", {})
        hy_spread = fred.get("hy_spread")
        credit_bps = 0.0
        if hy_spread is not None and hasattr(hy_spread, "iloc") and len(hy_spread) > 0:
            credit_bps = float(hy_spread.iloc[-1]) * 100 if not pd.isna(hy_spread.iloc[-1]) else 0.0

        yield_curve = fred.get("yield_curve")
        yc_slope = 0.0
        if yield_curve is not None and hasattr(yield_curve, "iloc") and len(yield_curve) > 0:
            yc_slope = float(yield_curve.iloc[-1]) if not pd.isna(yield_curve.iloc[-1]) else 0.0

        overall = self._classify_regime(vix_level, credit_bps, yc_slope)

        return {
            "vix_level": vix_level,
            "vix_regime": "crisis" if vix_level > 35 else "elevated" if vix_level > 25 else "normal" if vix_level > 15 else "low",
            "credit_spread_bps": credit_bps,
            "credit_regime": "crisis" if credit_bps > 600 else "stressed" if credit_bps > 400 else "normal",
            "yield_curve_slope": yc_slope,
            "yield_regime": "inverted" if yc_slope < -0.2 else "flat" if yc_slope < 0.5 else "normal" if yc_slope < 1.5 else "steep",
            "overall_regime": overall,
            "timestamp": datetime.now().isoformat(),
            "thresholds": {
                "vix": {"low": 15, "elevated": 25, "crisis": 35},
                "credit_bps": {"stressed": 400, "crisis": 600},
                "yield_curve": {"inverted": -0.2, "flat": 0.5, "steep": 1.5},
            },
        }

    def _classify_regime(self, vix: float, credit_bps: float, yc_slope: float) -> str:
        """Classify overall market regime."""
        crisis_signals = 0
        if vix > 35:
            crisis_signals += 1
        if credit_bps > 600:
            crisis_signals += 1
        if yc_slope < -0.2:
            crisis_signals += 1

        if crisis_signals >= 2:
            return "crisis"
        if vix > 25 or credit_bps > 400:
            return "stressed"
        if vix < 15 and credit_bps < 300:
            return "benign"
        return "normal"

    def _should_trigger_learning_loop(self) -> bool:
        """Check if learning loop should fire."""
        pt_config = self.ar_config.get("paper_trade", {})
        ll_state = self.state.load_learning_loop_state()

        # Calendar check
        last_run = ll_state.get("last_run")
        calendar_days = pt_config.get("learning_loop_calendar_days", 30)
        if last_run:
            last_dt = datetime.fromisoformat(last_run)
            if (datetime.now() - last_dt).days >= calendar_days:
                return True
        else:
            # Never run before — trigger if we have any completed trades
            trades = self.state.load_paper_trades(status="closed")
            if trades:
                return True

        # Trade count check
        min_strategies = pt_config.get("learning_loop_min_strategies", 5)
        min_trades = pt_config.get("min_trades_for_evaluation", 20)
        qualifying = 0
        for s in self.paper_trade_strategies:
            trades = self.state.load_paper_trades(strategy=s.name, status="closed")
            if len(trades) >= min_trades:
                qualifying += 1

        return qualifying >= min_strategies

    # ------------------------------------------------------------------
    # Price helpers
    # ------------------------------------------------------------------

    def _fetch_missing_prices(
        self, tickers: list[str], start_date: str, end_date: str,
    ) -> None:
        """Fetch prices for tickers not already in cache."""
        from tradingagents.autoresearch.data_sources.yfinance_source import YFinanceSource

        source = self.registry.get("yfinance")
        if not isinstance(source, YFinanceSource):
            return

        logger.info("Fetching prices for %d signal tickers: %s", len(tickers), tickers)
        extra_df = source.fetch_prices(tickers, start_date, end_date)
        if not extra_df.empty and isinstance(extra_df.columns, pd.MultiIndex):
            for ticker in tickers:
                try:
                    ticker_df = extra_df.xs(ticker, level=1, axis=1)
                    if not ticker_df.empty:
                        self._price_cache[ticker] = ticker_df
                except (KeyError, ValueError):
                    pass
        elif not extra_df.empty and len(tickers) == 1:
            self._price_cache[tickers[0]] = extra_df

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------

    def _fetch_all_data(self, start_date: str, end_date: str) -> dict[str, Any]:
        """Fetch all data needed by active strategies.

        Returns nested dict: {source_name: {data_type: data}}.
        """
        self._emit("phase", phase="data_fetch", status="starting")
        data: dict[str, Any] = {}

        # Collect which sources are needed
        needed_sources: set[str] = set()
        for s in self.paper_trade_strategies:
            needed_sources.update(s.data_sources)

        available = set(self.registry.available_sources())
        logger.info("Needed sources: %s, available: %s", needed_sources, available)

        # Fetch yfinance data (VIX + core market data for regime model)
        if "yfinance" in needed_sources and "yfinance" in available:
            data["yfinance"] = self._fetch_yfinance_data(start_date, end_date)

        # Fetch API-key sources in parallel (I/O bound, no dependency on each other)
        from concurrent.futures import ThreadPoolExecutor, as_completed

        api_fetches: dict[str, tuple] = {}
        if "finnhub" in needed_sources and "finnhub" in available:
            api_fetches["finnhub"] = (self._fetch_finnhub_data, ())
        if "regulations" in needed_sources and "regulations" in available:
            api_fetches["regulations"] = (self._fetch_regulations_data, ())
        if "courtlistener" in needed_sources and "courtlistener" in available:
            api_fetches["courtlistener"] = (self._fetch_courtlistener_data, ())
        if "fred" in needed_sources and "fred" in available:
            api_fetches["fred"] = (self._fetch_fred_data, (start_date, end_date))
        if "congress" in needed_sources and "congress" in available:
            api_fetches["congress"] = (self._fetch_congress_data, ())
        if "noaa" in needed_sources and "noaa" in available:
            api_fetches["noaa"] = (self._fetch_noaa_data, (end_date,))
        if "usda" in needed_sources and "usda" in available:
            api_fetches["usda"] = (self._fetch_usda_data, (end_date,))
        if "drought_monitor" in needed_sources and "drought_monitor" in available:
            api_fetches["drought_monitor"] = (self._fetch_drought_data, (end_date,))

        # Also fetch EDGAR events for paper-trade strategies
        if "edgar" in needed_sources and "edgar" in available:
            api_fetches["edgar"] = (self._fetch_edgar_events, ())

        if api_fetches:
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = {
                    pool.submit(fn, *args): name
                    for name, (fn, args) in api_fetches.items()
                }
                for future in as_completed(futures):
                    name = futures[future]
                    try:
                        data[name] = future.result()
                    except Exception:
                        logger.error("Failed to fetch %s", name, exc_info=True)
                        data[name] = {}

        self._emit("phase", phase="data_fetch", status="done")
        return data

    def _fetch_finnhub_data(self) -> dict[str, Any]:
        """Fetch Finnhub data for earnings calls and supply chain strategies."""
        source = self.registry.get("finnhub")
        if source is None:
            return {}

        result: dict[str, Any] = {}

        # Earnings calendar: who reported recently? (P1/P2)
        date_to = datetime.now().strftime("%Y-%m-%d")
        date_from = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        earnings = source.fetch_recent_earnings(date_from, date_to)
        if earnings:
            # Collect news around earnings dates for top reporters (proxy for transcripts)
            transcripts = []
            for e in earnings[:10]:  # Top 10 to limit API calls
                symbol = e.get("symbol", "")
                edate = e.get("date", "")
                if not symbol or not edate:
                    continue
                news = source.fetch_earnings_news(symbol, edate)
                if news:
                    # Build a pseudo-transcript from earnings news
                    news_text = "\n".join(
                        f"[{n.get('source', 'Unknown')}]: {n.get('headline', '')} — {n.get('summary', '')}"
                        for n in news[:5]
                    )
                    transcripts.append({
                        "symbol": symbol,
                        "year": e.get("year"),
                        "quarter": e.get("quarter"),
                        "transcript_text": news_text,
                        "eps_actual": e.get("epsActual"),
                        "eps_estimate": e.get("epsEstimate"),
                        "revenue_actual": e.get("revenueActual"),
                        "revenue_estimate": e.get("revenueEstimate"),
                    })
            result["transcripts"] = transcripts

        # Company news for supply chain disruption detection (P6)
        sc_symbols = ["AAPL", "TSLA", "NVDA", "AMZN", "BA", "CAT", "DE"]
        all_news = []
        for symbol in sc_symbols:
            news = source.fetch_company_news(symbol, date_from, date_to)
            for article in news:
                article["symbol"] = symbol
            all_news.extend(news)
        if all_news:
            result["disruption_news"] = all_news

        # Supply chain / peer relationships
        chains: dict[str, list[str]] = {}
        for symbol in sc_symbols:
            peers = source.fetch_supply_chain(symbol)
            if peers:
                chains[symbol] = [p["ticker"] for p in peers]
        if chains:
            result["supply_chains"] = chains

        logger.info(
            "Finnhub fetch: %d earnings, %d news, %d chains",
            len(result.get("transcripts", [])),
            len(result.get("disruption_news", [])),
            len(result.get("supply_chains", {})),
        )
        return result

    def _fetch_regulations_data(self) -> dict[str, Any]:
        """Fetch regulations.gov data for regulatory pipeline strategy."""
        from tradingagents.autoresearch.event_monitor import EventMonitor

        monitor = EventMonitor(self.registry)
        result: dict[str, Any] = {}

        rules = monitor.poll_proposed_rules(
            agencies=["SEC", "EPA", "FDA", "FTC", "DOL", "CFPB"],
            days_back=14,
        )
        if rules:
            result["proposed_rules"] = rules

        logger.info("Regulations.gov fetch: %d proposed rules", len(rules))
        return result

    def _fetch_edgar_events(self) -> dict[str, Any]:
        """Fetch EDGAR events for paper-trade strategies (P3, P4, P7, P8, P9)."""
        from tradingagents.autoresearch.event_monitor import EventMonitor

        monitor = EventMonitor(self.registry)
        result: dict[str, Any] = {}

        # Filings for P3 (filing changes), P9 (exec comp)
        filings = monitor.poll_edgar_filings(
            form_types=["10-K", "10-Q", "DEF 14A"],
            days_back=14,
        )
        if filings:
            result["filings"] = filings

        # Form 4 for P4 (insider combo), P7 (10b5-1)
        # Poll for major tickers
        tickers = ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "JPM"]
        form4 = monitor.poll_form4_filings(tickers, days_back=14)
        if form4:
            result["form4"] = form4

        # 13D for B6 (activist)
        filings_13d = monitor.poll_13d_filings(days_back=14)
        if filings_13d:
            result["activist_13d"] = filings_13d

        logger.info(
            "EDGAR fetch: %d filings, %d form4 tickers, %d 13D",
            len(result.get("filings", [])),
            len(result.get("form4", {})),
            len(result.get("activist_13d", [])),
        )
        return result

    def _fetch_courtlistener_data(self) -> dict[str, Any]:
        """Fetch CourtListener data for litigation strategy."""
        from tradingagents.autoresearch.event_monitor import EventMonitor

        monitor = EventMonitor(self.registry)
        result: dict[str, Any] = {}

        # Search for securities-related cases
        for query in ["securities class action", "SEC enforcement", "antitrust"]:
            dockets = monitor.poll_court_dockets(query=query, days_back=14)
            existing = result.get("dockets", [])
            existing.extend(dockets)
            result["dockets"] = existing

        logger.info(
            "CourtListener fetch: %d dockets",
            len(result.get("dockets", [])),
        )
        return result

    def _fetch_fred_data(self, start_date: str, end_date: str) -> dict[str, Any]:
        """Fetch FRED credit spreads and economic indicators."""
        source = self.registry.get("fred")
        if source is None:
            return {}

        result: dict[str, Any] = {}

        # Credit spreads for regime model
        try:
            spreads = source.fetch_credit_spreads(start_date, end_date)
            result.update(spreads)  # Keys are FRED series IDs (BAMLH0A0HYM2, BAMLC0A4CBBB)
        except Exception:
            logger.error("Failed to fetch FRED credit spreads", exc_info=True)

        # Economic indicators for regime model
        try:
            indicators = source.fetch_economic_indicators(start_date, end_date)
            result.update(indicators)  # Keys are FRED series IDs (UNRATE, PAYEMS, etc.)
        except Exception:
            logger.error("Failed to fetch FRED economic indicators", exc_info=True)

        # Map friendly names for strategies that use them
        from tradingagents.autoresearch.data_sources.fred_source import SERIES_MAP
        for friendly_name, series_id in SERIES_MAP.items():
            if series_id in result:
                result[friendly_name] = result[series_id]

        logger.info("FRED fetch: %d series loaded", len(result))
        return result

    def _fetch_congress_data(self) -> dict[str, Any]:
        """Fetch recent congressional stock trades."""
        source = self.registry.get("congress")
        if source is None:
            return {}

        result: dict[str, Any] = {}
        try:
            trades = source.get_recent_trades(days_back=30)
            result["recent_trades"] = trades
            logger.info("Congress fetch: %d recent trades", len(trades))
        except Exception:
            logger.error("Failed to fetch congressional trades", exc_info=True)

        return result

    def _fetch_noaa_data(self, trading_date: str) -> dict[str, Any]:
        """Fetch NOAA weather anomaly summary for Corn Belt ag regions."""
        source = self.registry.get("noaa")
        if source is None:
            return {}

        try:
            return source.fetch_ag_weather_summary(trading_date, lookback_days=30)
        except Exception:
            logger.error("Failed to fetch NOAA weather data", exc_info=True)
            return {}

    def _fetch_usda_data(self, trading_date: str) -> dict[str, Any]:
        """Fetch USDA crop condition data for corn, soybeans, and wheat."""
        source = self.registry.get("usda")
        if source is None:
            return {}

        try:
            from datetime import datetime
            year = datetime.strptime(trading_date, "%Y-%m-%d").year
            crop_progress = {}
            for commodity in ("CORN", "SOYBEANS", "WHEAT"):
                weeks = source.fetch_crop_progress(commodity, year)
                if weeks:
                    crop_progress[commodity] = weeks
            return {"crop_progress": crop_progress}
        except Exception:
            logger.error("Failed to fetch USDA data", exc_info=True)
            return {}

    def _fetch_drought_data(self, trading_date: str) -> dict[str, Any]:
        """Fetch Drought Monitor severity and composite score."""
        source = self.registry.get("drought_monitor")
        if source is None:
            return {}

        try:
            from datetime import datetime, timedelta
            end = trading_date
            start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")
            severity = source.fetch_drought_severity(start=start, end=end)
            composite = source.fetch_composite_score(date=trading_date)
            return {"composite_score": composite, "states": severity}
        except Exception:
            logger.error("Failed to fetch Drought Monitor data", exc_info=True)
            return {}

    def _fetch_yfinance_data(self, start_date: str, end_date: str) -> dict[str, Any]:
        """Fetch all yfinance data needed by strategies."""
        from tradingagents.autoresearch.data_sources.yfinance_source import YFinanceSource

        source = self.registry.get("yfinance")
        if not isinstance(source, YFinanceSource):
            logger.warning("yfinance source not available")
            return {}

        result: dict[str, Any] = {}

        # Core market tickers for regime model and general context
        # Includes ag ETFs for weather_ag strategy
        core_tickers = ["SPY", "SHY", "TLT", "DBA", "WEAT", "CORN", "MOO", "SOYB", "ADM", "BG", "CTVA", "DE", "FMC"]

        logger.info("Fetching prices for %d core tickers", len(core_tickers))
        prices_df = source.fetch_prices(core_tickers, start_date, end_date)

        # Split into per-ticker DataFrames
        prices: dict[str, pd.DataFrame] = {}
        if not prices_df.empty and isinstance(prices_df.columns, pd.MultiIndex):
            for ticker in core_tickers:
                try:
                    ticker_df = prices_df.xs(ticker, level=1, axis=1)
                    if not ticker_df.empty:
                        prices[ticker] = ticker_df
                except (KeyError, ValueError):
                    logger.debug("No data for %s in batch download", ticker)
        elif not prices_df.empty and len(core_tickers) == 1:
            prices[core_tickers[0]] = prices_df

        result["prices"] = prices
        self._price_cache.update(prices)

        # Fetch VIX for regime model
        vix_df = source.fetch_vix(start_date, end_date)
        if not vix_df.empty:
            result["vix"] = vix_df

        return result

    # ------------------------------------------------------------------
    # LLM enrichment
    # ------------------------------------------------------------------

    def _enrich_with_llm(
        self, candidates: list[Candidate], strategy_name: str,
        regime_context: dict | None = None,
    ) -> list[Candidate]:
        """Run LLM analysis on candidates that have needs_llm_analysis=True."""
        enriched = []
        for c in candidates:
            if not c.metadata.get("needs_llm_analysis"):
                enriched.append(c)
                continue

            analysis_type = c.metadata.get("analysis_type", "")
            llm_result = {}

            try:
                if analysis_type == "earnings_call":
                    llm_result = self._analyzer.analyze_earnings_call(
                        c.metadata.get("analysis_text", c.metadata.get("transcript_text", "")),
                        c.ticker,
                        regime_context=regime_context,
                        text_source=c.metadata.get("text_source", "earnings_news"),
                    )
                elif analysis_type == "regulation":
                    llm_result = self._analyzer.analyze_regulation(
                        c.metadata.get("title", ""),
                        c.metadata.get("summary", ""),
                        c.metadata.get("agency_id", ""),
                        regime_context=regime_context,
                    )
                elif analysis_type == "supply_chain":
                    llm_result = self._analyzer.analyze_supply_chain(
                        c.metadata.get("headline", ""),
                        c.metadata.get("summary", ""),
                        c.ticker,
                        c.metadata.get("affected_peers", []),
                        regime_context=regime_context,
                    )
                elif analysis_type == "litigation":
                    llm_result = self._analyzer.analyze_litigation(
                        c.metadata.get("case_name", ""),
                        c.metadata.get("nature_of_suit", ""),
                        c.metadata.get("cause", ""),
                        c.metadata.get("court", ""),
                        regime_context=regime_context,
                    )
                elif analysis_type == "insider_activity":
                    cluster_type = c.metadata.get("cluster_type", "")
                    if cluster_type == "buy_cluster":
                        llm_result = self._analyzer.analyze_insider_context(
                            c.metadata.get("filings", []),
                            c.ticker,
                            regime_context=regime_context,
                        )
                    elif cluster_type == "sell_pattern":
                        llm_result = self._analyzer.analyze_10b5_1_plan(
                            c.metadata.get("filings", []),
                            c.ticker,
                            regime_context=regime_context,
                        )
                elif analysis_type == "filing_change":
                    llm_result = self._analyzer.analyze_filing_change(
                        c.metadata.get("current_text", ""),
                        c.metadata.get("prior_text", ""),
                        c.ticker,
                        regime_context=regime_context,
                    )
                elif analysis_type == "exec_comp":
                    llm_result = self._analyzer.analyze_exec_comp(
                        c.metadata.get("proxy_text", ""),
                        c.ticker,
                        regime_context=regime_context,
                    )
                elif analysis_type == "ag_weather":
                    llm_result = self._analyzer.analyze_ag_weather(
                        ticker=c.ticker,
                        commodity_name=c.metadata.get("commodity", c.ticker),
                        ag_context={
                            "drought_score": c.metadata.get("drought_score", 0),
                            "drought_states": c.metadata.get("drought_states", {}),
                            "noaa_data": c.metadata.get("noaa_data", {}),
                            "usda_data": c.metadata.get("usda_data", {}),
                        },
                        trailing_return=c.metadata.get("trailing_return", 0),
                        hold_days=21,
                        regime_context=regime_context,
                    )
            except Exception:
                logger.error("LLM analysis failed for %s/%s", strategy_name, c.ticker, exc_info=True)

            if llm_result:
                # Update candidate with LLM results
                c.direction = llm_result.get("direction", c.direction)
                c.score = llm_result.get("conviction", llm_result.get("score", c.score))
                c.metadata["llm_analysis"] = llm_result
                # Resolve ticker if LLM provided one
                if not c.ticker and llm_result.get("defendant_ticker"):
                    c.ticker = llm_result["defendant_ticker"]
                if not c.ticker and llm_result.get("affected_tickers"):
                    c.ticker = llm_result["affected_tickers"][0]

                # Validate LLM-resolved ticker against SEC data
                if c.ticker:
                    edgar = self.registry.get("edgar")
                    if edgar and hasattr(edgar, "validate_ticker"):
                        if not edgar.validate_ticker(c.ticker):
                            logger.warning(
                                "LLM returned invalid ticker %s for %s, dropping",
                                c.ticker, strategy_name,
                            )
                            c.ticker = ""

            enriched.append(c)

        return enriched

