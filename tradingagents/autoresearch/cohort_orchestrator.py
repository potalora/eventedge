"""Parallel paper portfolio runner for a 2-cohort paper trading trial.

Runs 2 independent $5K paper portfolios with shared data fetching.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class CohortConfig:
    """Configuration for a single cohort."""

    name: str                           # "control" or "adaptive"
    state_dir: str                      # Unique per cohort
    adaptive_confidence: bool = False   # False for control, True for adaptive
    learning_enabled: bool = False      # False for control, True for adaptive
    use_llm: bool = True               # LLM enrichment for both


class CohortOrchestrator:
    """Run 2 paper portfolios in parallel with shared data fetch."""

    def __init__(self, cohort_configs: list[CohortConfig], base_config: dict):
        """
        Args:
            cohort_configs: List of CohortConfig (one per cohort).
            base_config: Base config dict (DEFAULT_CONFIG with env vars applied).
                         Per-cohort state_dir overrides are applied automatically.
        """
        from tradingagents.autoresearch.multi_strategy_engine import MultiStrategyEngine
        from tradingagents.autoresearch.state import StateManager
        from tradingagents.autoresearch.strategies import get_paper_trade_strategies

        self.cohorts: list[dict[str, Any]] = []
        strategies = get_paper_trade_strategies()

        for cfg in cohort_configs:
            cohort_config = copy.deepcopy(base_config)
            cohort_config.setdefault("autoresearch", {})["state_dir"] = cfg.state_dir

            state = StateManager(cfg.state_dir)
            engine = MultiStrategyEngine(
                config=cohort_config,
                strategies=strategies,
                state_manager=state,
                use_llm=cfg.use_llm,
                adaptive_confidence=cfg.adaptive_confidence,
            )
            self.cohorts.append({
                "config": cfg,
                "engine": engine,
                "state": state,
            })

        self._base_config = base_config

    def run_daily(self, trading_date: str | None = None) -> dict[str, Any]:
        """Run all cohorts for a trading day with shared data fetch.

        1. Fetch data ONCE using first engine.
        2. Run each cohort with shared data.
        3. Log comparison summary.

        Returns:
            {cohort_name: result_dict}
        """
        if not trading_date:
            trading_date = datetime.now().strftime("%Y-%m-%d")

        logger.info("=== Cohort daily run: %s ===", trading_date)

        # Fetch data once
        first_engine = self.cohorts[0]["engine"]
        lookback_start = (
            datetime.strptime(trading_date, "%Y-%m-%d") - timedelta(days=7)
        ).strftime("%Y-%m-%d")
        shared_data = first_engine._fetch_all_data(lookback_start, trading_date)
        logger.info("Shared data fetched: %s", list(shared_data.keys()))

        # Screen + LLM enrich once (shared across cohorts)
        shared_signals, shared_regime = first_engine.screen_and_enrich(
            trading_date, shared_data,
        )
        logger.info("Shared signals: %d enriched signals", len(shared_signals))

        # Fetch OpenBB enrichment for signal tickers
        enrichment = self._fetch_openbb_enrichment(shared_signals)

        # Run each cohort with shared signals (only confidence + execution differs)
        results: dict[str, Any] = {}
        for cohort in self.cohorts:
            name = cohort["config"].name
            logger.info("--- Running cohort: %s ---", name)
            try:
                result = cohort["engine"].run_paper_trade_phase(
                    trading_date=trading_date,
                    shared_signals=shared_signals,
                    shared_regime=shared_regime,
                    enrichment=enrichment,
                )
                results[name] = result
                n_signals = len(result.get("signals", []))
                n_trades = len(result.get("trades_opened", []))
                account = result.get("account", {})
                logger.info(
                    "Cohort %s: %d signals, %d trades, portfolio=$%.0f",
                    name, n_signals, n_trades, account.get("portfolio_value", 0),
                )
            except Exception:
                logger.error("Cohort %s failed", name, exc_info=True)
                results[name] = {"error": True}

        return results

    def _fetch_openbb_enrichment(self, signals: list[dict]) -> dict:
        """Fetch OpenBB data to enrich portfolio committee decisions.

        Returns dict with profiles, short_interest, factors for signal tickers.
        """
        enrichment: dict[str, Any] = {}

        tickers = list({s.get("ticker", "") for s in signals if s.get("ticker")})
        if not tickers:
            return enrichment

        first_engine = self.cohorts[0]["engine"]
        registry = getattr(first_engine, "registry", None)
        if registry is None:
            return enrichment

        openbb_source = registry.get("openbb")
        if openbb_source is None or not openbb_source.is_available():
            return enrichment

        # Fetch profiles for all tickers
        profiles = {}
        for ticker in tickers:
            result = openbb_source.fetch({"method": "equity_profile", "ticker": ticker})
            if "error" not in result:
                profiles[ticker] = result
        if profiles:
            enrichment["profiles"] = profiles

        # Fetch short interest for all tickers
        short_interest = {}
        for ticker in tickers:
            result = openbb_source.fetch({"method": "equity_short_interest", "ticker": ticker})
            if "error" not in result:
                short_interest[ticker] = result
        if short_interest:
            enrichment["short_interest"] = short_interest

        # Fetch Fama-French factors (once, not per ticker)
        factors = openbb_source.fetch({"method": "factors_fama_french"})
        if "error" not in factors:
            enrichment["factors"] = factors.get("factors", {})

        return enrichment

    def run_learning(self) -> dict[str, Any]:
        """Run learning loop for cohorts that have it enabled.

        Returns:
            {cohort_name: learning_result}
        """
        results: dict[str, Any] = {}
        for cohort in self.cohorts:
            cfg = cohort["config"]
            if not cfg.learning_enabled:
                results[cfg.name] = {"skipped": True, "reason": "learning_disabled"}
                continue

            logger.info("--- Learning loop: %s ---", cfg.name)
            try:
                result = cohort["engine"].run_learning_loop()
                results[cfg.name] = result
            except Exception:
                logger.error("Learning loop failed for %s", cfg.name, exc_info=True)
                results[cfg.name] = {"error": True}

        return results

    def reset(self) -> None:
        """Reset all cohort state (for testing/fresh start)."""
        for cohort in self.cohorts:
            cohort["state"].reset()
            logger.info("Reset state for cohort: %s", cohort["config"].name)


def build_default_cohorts(base_config: dict) -> list[CohortConfig]:
    """Build the standard 2-cohort configuration.

    Cohort A (control): Fixed confidence (0.5), no learning.
    Cohort B (adaptive): Journal-derived confidence, prompt optimization, weekly learning.
    """
    base_state_dir = base_config.get("autoresearch", {}).get(
        "state_dir", "data/state"
    )
    return [
        CohortConfig(
            name="control",
            state_dir=f"{base_state_dir}/control",
            adaptive_confidence=False,
            learning_enabled=False,
            use_llm=True,
        ),
        CohortConfig(
            name="adaptive",
            state_dir=f"{base_state_dir}/adaptive",
            adaptive_confidence=True,
            learning_enabled=True,
            use_llm=True,
        ),
    ]
