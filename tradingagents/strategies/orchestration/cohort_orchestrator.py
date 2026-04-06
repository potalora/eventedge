"""Parallel paper portfolio runner for a 16-cohort horizon x size matrix.

Runs paper portfolios across 4 investment horizons x 4 portfolio sizes
with shared data fetching.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lookup tables
# ---------------------------------------------------------------------------

@dataclass
class PortfolioSizeProfile:
    """Position-sizing and concentration parameters for a portfolio tier."""

    name: str                          # "5k", "10k", "50k", "100k"
    total_capital: float               # e.g. 5000.0
    max_position_pct: float            # max single-position weight
    min_position_value: float          # floor for position value
    max_positions: int                 # max concurrent positions
    sector_concentration_cap: float    # max weight in one sector
    cash_reserve_pct: float            # cash held back from allocation

    # Short selling eligibility
    short_eligible: bool = False
    max_short_exposure_pct: float = 0.0   # max total short exposure as % of capital
    max_single_short_pct: float = 0.05    # max single short position as % of capital
    margin_cash_buffer_pct: float = 0.0   # cash buffer required for margin
    max_correlated_shorts: int = 0        # max simultaneous correlated short positions

    # Options eligibility
    options_eligible: list[str] = field(default_factory=list)  # e.g. ["covered_call"]
    max_options_premium_pct: float = 0.0  # max options premium spend as % of capital


SIZE_PROFILES: dict[str, PortfolioSizeProfile] = {
    "5k": PortfolioSizeProfile(
        name="5k",
        total_capital=5_000.0,
        max_position_pct=0.25,
        min_position_value=100.0,
        max_positions=5,
        sector_concentration_cap=0.50,
        cash_reserve_pct=0.10,
    ),
    "10k": PortfolioSizeProfile(
        name="10k",
        total_capital=10_000.0,
        max_position_pct=0.20,
        min_position_value=250.0,
        max_positions=8,
        sector_concentration_cap=0.40,
        cash_reserve_pct=0.10,
        # Options: covered calls only, no short selling
        options_eligible=["covered_call"],
        max_options_premium_pct=0.05,
    ),
    "50k": PortfolioSizeProfile(
        name="50k",
        total_capital=50_000.0,
        max_position_pct=0.10,
        min_position_value=1_000.0,
        max_positions=15,
        sector_concentration_cap=0.30,
        cash_reserve_pct=0.15,
        # Short selling eligible
        short_eligible=True,
        max_short_exposure_pct=0.15,
        max_single_short_pct=0.05,
        margin_cash_buffer_pct=0.20,
        max_correlated_shorts=2,
        # Options: covered calls
        options_eligible=["covered_call"],
        max_options_premium_pct=0.05,
    ),
    "100k": PortfolioSizeProfile(
        name="100k",
        total_capital=100_000.0,
        max_position_pct=0.08,
        min_position_value=2_000.0,
        max_positions=20,
        sector_concentration_cap=0.25,
        cash_reserve_pct=0.15,
        # Short selling eligible
        short_eligible=True,
        max_short_exposure_pct=0.20,
        max_single_short_pct=0.05,
        margin_cash_buffer_pct=0.15,
        max_correlated_shorts=4,
        # Options: covered calls
        options_eligible=["covered_call"],
        max_options_premium_pct=0.08,
    ),
}

HORIZON_PARAMS: dict[str, dict] = {
    "30d": {
        "hold_days_default": 25,
        "hold_days_range": (20, 45),
        "signal_decay_window": (5, 10),
    },
    "3m": {
        "hold_days_default": 90,
        "hold_days_range": (60, 120),
        "signal_decay_window": (15, 30),
    },
    "6m": {
        "hold_days_default": 180,
        "hold_days_range": (120, 210),
        "signal_decay_window": (30, 60),
    },
    "1y": {
        "hold_days_default": 300,
        "hold_days_range": (250, 365),
        "signal_decay_window": (60, 120),
    },
}


# ---------------------------------------------------------------------------
# Cohort configuration
# ---------------------------------------------------------------------------

@dataclass
class CohortConfig:
    """Configuration for a single cohort."""

    name: str                           # "horizon_30d_size_5k"
    state_dir: str                      # Unique per cohort
    horizon: str                        # "30d", "3m", "6m", "1y"
    size_profile: str                   # "5k", "10k", "50k", "100k"
    use_llm: bool = True
    adaptive_confidence: bool = False   # dormant
    learning_enabled: bool = False      # dormant


class CohortOrchestrator:
    """Run paper portfolios in parallel with shared data fetch."""

    def __init__(self, cohort_configs: list[CohortConfig], base_config: dict):
        """
        Args:
            cohort_configs: List of CohortConfig (one per cohort).
            base_config: Base config dict (DEFAULT_CONFIG with env vars applied).
                         Per-cohort state_dir overrides are applied automatically.
        """
        from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
        from tradingagents.strategies.state.state import StateManager
        from tradingagents.strategies.modules import get_paper_trade_strategies

        self.cohorts: list[dict[str, Any]] = []
        strategies = get_paper_trade_strategies()

        for cfg in cohort_configs:
            cohort_config = copy.deepcopy(base_config)
            cohort_config.setdefault("autoresearch", {})["state_dir"] = cfg.state_dir
            profile = SIZE_PROFILES.get(cfg.size_profile)
            if profile:
                cohort_config.setdefault("autoresearch", {})["total_capital"] = profile.total_capital

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
                "size_profile": SIZE_PROFILES.get(cfg.size_profile),
            })

        self._base_config = base_config

        # OpenBB availability check — warn loudly if unavailable
        first_engine = self.cohorts[0]["engine"] if self.cohorts else None
        openbb_source = (
            first_engine.registry.get("openbb") if first_engine else None
        )
        if openbb_source is not None and openbb_source.is_available():
            self.openbb_degraded = False
            logger.info("OpenBB: available — sector enforcement and enrichment active")
        else:
            self.openbb_degraded = True
            logger.warning(
                "OpenBB: UNAVAILABLE — sector enforcement disabled, enrichment skipped. "
                "Install with: pip install -e '[.openbb]' and set FMP_API_KEY"
            )

    def _screen_for_horizon(
        self, data: dict, trading_date: str, horizon: str,
    ) -> tuple[list[dict], dict]:
        """Screen all strategies with horizon-specific params."""
        first_engine = self.cohorts[0]["engine"]
        return first_engine.screen_and_enrich(trading_date, data, horizon=horizon)

    def run_daily(self, trading_date: str | None = None) -> dict[str, Any]:
        """Run all cohorts with shared data fetch and per-horizon screening.

        1. Fetch data ONCE.
        2. Screen once per horizon (4 passes).
        3. OpenBB enrichment once (deduped tickers across horizons).
        4. Dispatch to all 16 cohorts.
        """
        if not trading_date:
            trading_date = datetime.now().strftime("%Y-%m-%d")

        logger.info("=== Cohort daily run: %s ===", trading_date)

        # Fetch data once
        first_engine = self.cohorts[0]["engine"]
        lookback_start = (
            datetime.strptime(trading_date, "%Y-%m-%d") - timedelta(days=90)
        ).strftime("%Y-%m-%d")
        shared_data = first_engine._fetch_all_data(lookback_start, trading_date)
        logger.info("Shared data fetched: %s", list(shared_data.keys()))

        # Screen once per horizon (4 passes, cached)
        horizons = sorted({c["config"].horizon for c in self.cohorts})
        horizon_signals: dict[str, tuple[list[dict], dict]] = {}
        for horizon in horizons:
            signals, regime = self._screen_for_horizon(shared_data, trading_date, horizon)
            horizon_signals[horizon] = (signals, regime)
            logger.info("Horizon %s: %d signals", horizon, len(signals))

        # OpenBB enrichment once (dedupe tickers across all horizons)
        all_signals = []
        for signals, _ in horizon_signals.values():
            all_signals.extend(signals)
        enrichment = self._fetch_openbb_enrichment(all_signals)

        # Dispatch to all cohorts
        results: dict[str, Any] = {}
        for cohort in self.cohorts:
            cfg = cohort["config"]
            name = cfg.name
            logger.info("--- Running cohort: %s ---", name)

            signals, regime = horizon_signals[cfg.horizon]

            try:
                result = cohort["engine"].run_paper_trade_phase(
                    trading_date=trading_date,
                    shared_signals=signals,
                    shared_regime=regime,
                    enrichment=enrichment,
                    size_profile=cohort.get("size_profile"),
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
    """Build the 16-cohort horizon x size matrix.

    Produces one cohort for each combination of 4 horizons x 4 portfolio sizes.
    All cohorts start with adaptive_confidence=False and learning_enabled=False.
    """
    base_state_dir = base_config.get("autoresearch", {}).get(
        "state_dir", "data/state"
    )
    horizons = ["30d", "3m", "6m", "1y"]
    sizes = ["5k", "10k", "50k", "100k"]
    cohorts: list[CohortConfig] = []
    for h in horizons:
        for s in sizes:
            name = f"horizon_{h}_size_{s}"
            cohorts.append(
                CohortConfig(
                    name=name,
                    state_dir=f"{base_state_dir}/{name}",
                    horizon=h,
                    size_profile=s,
                )
            )
    return cohorts
