"""Parallel paper portfolio runner for a 16-cohort horizon x size matrix.

Runs paper portfolios across 4 investment horizons x 4 portfolio sizes
with shared data fetching.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def count_failed_cohorts(results: dict) -> tuple[int, int, list[str]]:
    """Count cohorts that errored in a ``run_daily`` results dict.

    A cohort is considered failed when its result is a mapping carrying a truthy
    ``"error"`` (cohort_orchestrator records ``{"error": True}`` for any cohort
    whose execution or staging lifecycle raised). Used to surface masked failures —
    a run where cohorts errored must never be recorded as a clean success.

    Returns ``(n_failed, n_total, sorted_failed_names)``.
    """
    failed = sorted(
        name for name, r in results.items() if isinstance(r, dict) and r.get("error")
    )
    return len(failed), len(results), failed


# ---------------------------------------------------------------------------
# Lookup tables
# ---------------------------------------------------------------------------


@dataclass
class PortfolioSizeProfile:
    """Position-sizing and concentration parameters for a portfolio tier."""

    name: str  # "5k", "10k", "50k", "100k"
    total_capital: float  # e.g. 5000.0
    max_position_pct: float  # max single-position weight
    min_position_value: float  # floor for position value
    max_positions: int  # max concurrent positions
    sector_concentration_cap: float  # max weight in one sector
    cash_reserve_pct: float  # cash held back from allocation

    # Short selling eligibility
    short_eligible: bool = False
    max_short_exposure_pct: float = 0.0  # max total short exposure as % of capital
    max_single_short_pct: float = 0.05  # max single short position as % of capital
    margin_cash_buffer_pct: float = 0.0  # cash buffer required for margin
    max_correlated_shorts: int = 0  # max simultaneous correlated short positions

    # Options eligibility
    options_eligible: list[str] = field(default_factory=list)  # e.g. ["covered_call"]
    max_options_premium_pct: float = 0.0  # max options premium spend as % of capital

    # Commodity eligibility
    commodity_eligible: bool = False
    max_commodity_allocation_pct: float = 0.0
    commodity_instruments: list[str] = field(default_factory=list)


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
        # Commodities
        commodity_eligible=True,
        max_commodity_allocation_pct=0.10,
        commodity_instruments=["GLD", "SLV", "PDBC"],
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
        # Commodities
        commodity_eligible=True,
        max_commodity_allocation_pct=0.10,
        commodity_instruments=["GLD", "SLV", "PDBC", "COPX", "XLE", "USO", "UNG"],
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
        # Commodities
        commodity_eligible=True,
        max_commodity_allocation_pct=0.10,
        commodity_instruments=["GLD", "SLV", "PDBC", "COPX", "XLE", "USO", "UNG"],
    ),
}

HORIZON_PARAMS: dict[str, dict] = {
    "30d": {
        "hold_days_default": 25,
        "hold_days_range": (20, 45),
        "signal_decay_window": (5, 10),
        "commodity_eligible": False,
    },
    "3m": {
        "hold_days_default": 90,
        "hold_days_range": (60, 120),
        "signal_decay_window": (15, 30),
        "commodity_eligible": True,
        "commodity_signal_decay_window": (7, 21),
    },
    "6m": {
        "hold_days_default": 180,
        "hold_days_range": (120, 210),
        "signal_decay_window": (30, 60),
        "commodity_eligible": True,
        "commodity_signal_decay_window": (14, 45),
    },
    "1y": {
        "hold_days_default": 300,
        "hold_days_range": (250, 365),
        "signal_decay_window": (60, 120),
        "commodity_eligible": True,
        "commodity_signal_decay_window": (30, 90),
        "commodity_instruments_override": ["GLD", "SLV"],
    },
}


# ---------------------------------------------------------------------------
# Cohort configuration
# ---------------------------------------------------------------------------


@dataclass
class CohortConfig:
    """Configuration for a single cohort."""

    name: str  # "horizon_30d_size_5k"
    state_dir: str  # Unique per cohort
    horizon: str  # "30d", "3m", "6m", "1y"
    size_profile: str  # "5k", "10k", "50k", "100k"
    use_llm: bool = True
    adaptive_confidence: bool = False  # dormant
    learning_enabled: bool = False  # dormant


class CohortOrchestrator:
    """Run paper portfolios in parallel with shared data fetch."""

    def __init__(
        self,
        cohort_configs: list[CohortConfig],
        base_config: dict,
        *,
        price_source: Any | None = None,
    ):
        """
        Args:
            cohort_configs: List of CohortConfig (one per cohort).
            base_config: Base config dict (DEFAULT_CONFIG with env vars applied).
                         Per-cohort state_dir overrides are applied automatically.
        """
        from tradingagents.strategies.orchestration.multi_strategy_engine import (
            MultiStrategyEngine,
        )
        from tradingagents.strategies.orchestration.session_executor import (
            SessionExecutor,
        )
        from tradingagents.strategies.execution.price_source import YFinancePriceSource
        from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger
        from tradingagents.strategies.state.state import StateManager
        from tradingagents.strategies.modules import get_paper_trade_strategies

        self.cohorts: list[dict[str, Any]] = []
        if base_config.get("execution", {}).get("mode", "paper") != "paper":
            raise ValueError("CohortOrchestrator is paper-only")
        strategies = get_paper_trade_strategies()

        for cfg in cohort_configs:
            cfg = replace(cfg, adaptive_confidence=False, learning_enabled=False)
            cohort_config = copy.deepcopy(base_config)
            cohort_config.setdefault("autoresearch", {})["state_dir"] = cfg.state_dir
            cohort_config["autoresearch"]["horizon"] = cfg.horizon
            profile = SIZE_PROFILES.get(cfg.size_profile)
            if profile:
                cohort_config.setdefault("autoresearch", {})["total_capital"] = (
                    profile.total_capital
                )
                risk = cohort_config["autoresearch"].setdefault("risk_gate", {})
                risk.update(
                    {
                        "max_positions": profile.max_positions,
                        "max_position_pct": profile.max_position_pct,
                        "min_position_value": profile.min_position_value,
                        "cash_reserve_pct": profile.cash_reserve_pct,
                        "long_only": not profile.short_eligible,
                    }
                )

            ledger = PortfolioLedger(
                Path(cfg.state_dir) / "portfolio.db",
                cfg.name,
                Decimal(str(profile.total_capital if profile else 5000)),
                paper_ledger_config=cohort_config["autoresearch"].get("paper_ledger"),
                short_selling_config=cohort_config["autoresearch"].get("short_selling"),
            )

            state = StateManager(cfg.state_dir)
            engine = MultiStrategyEngine(
                config=cohort_config,
                strategies=strategies,
                state_manager=state,
                use_llm=cfg.use_llm,
                adaptive_confidence=False,
                ledger=ledger,
            )
            executor = SessionExecutor(ledger, cohort_config)
            self.cohorts.append(
                {
                    "config": cfg,
                    "engine": engine,
                    "state": state,
                    "size_profile": SIZE_PROFILES.get(cfg.size_profile),
                    "ledger": ledger,
                    "executor": executor,
                }
            )

        self._base_config = base_config
        self._price_source = price_source or YFinancePriceSource()
        self._epoch_id = str(
            base_config.get("autoresearch", {})
            .get("paper_ledger", {})
            .get("epoch_id", "foundation-v1")
        )

        # OpenBB availability check — warn loudly if unavailable
        first_engine = self.cohorts[0]["engine"] if self.cohorts else None
        openbb_source = first_engine.registry.get("openbb") if first_engine else None
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
        self,
        data: dict,
        trading_date: str,
        horizon: str,
    ) -> tuple[list[dict], dict]:
        """Screen all strategies with horizon-specific params."""
        first_engine = self.cohorts[0]["engine"]
        return first_engine.screen_and_enrich(trading_date, data, horizon=horizon)

    def run_daily(self, trading_date: str | None = None) -> dict[str, Any]:
        """Execute/mark every cohort, then share four screens and stage intents."""
        from tradingagents.strategies.orchestration.session_executor import (
            CorporateActionBatchError,
            PHASES,
            SessionExecutor,
            ensure_reference_bars,
        )
        from tradingagents.strategies.orchestration.trading_calendar import (
            is_session,
            session_close,
        )

        if not trading_date:
            trading_date = datetime.now().strftime("%Y-%m-%d")
        session = date.fromisoformat(trading_date)
        processed_at = datetime.now(timezone.utc)
        if not is_session(session):
            raise ValueError(f"{session} is not an XNYS session")
        if processed_at < session_close(session):
            raise ValueError("daily cohort run cannot precede the exact XNYS close")

        logger.info("=== Cohort daily run: %s ===", trading_date)
        results: dict[str, Any] = {}

        complete_replays: dict[str, Any] = {}
        stage_only: list[dict[str, Any]] = []
        execution_needed: list[dict[str, Any]] = []
        for cohort in self.cohorts:
            ledger = cohort["ledger"]
            invalid_reason = ledger.session_invalid_reason(session)
            if invalid_reason:
                results[cohort["config"].name] = {
                    "error": True,
                    "invalid_reason": invalid_reason,
                }
                continue
            snapshots = ledger.read_snapshots(
                session, session, epoch_id=self._epoch_id, valid_only=True
            )
            horizon = cohort["config"].horizon
            policy_id = str(
                cohort["engine"]
                .ar_config.get("paper_ledger", {})
                .get("policy_id", f"foundation-{horizon}")
            )
            phases_complete = all(
                ledger.phase_completed(session, phase) for phase in PHASES
            )
            staging_complete = ledger.staging_completed(
                session, self._epoch_id, policy_id
            )
            if len(snapshots) == 1 and phases_complete:
                try:
                    cohort["executor"].validate_bound_context(session, self._epoch_id)
                except Exception as error:
                    results[cohort["config"].name] = {
                        "error": True,
                        "invalid_reason": str(error),
                    }
                    continue
            if len(snapshots) == 1 and phases_complete and staging_complete:
                fills = ledger.read_fills(session, session)
                complete_replays[cohort["config"].name] = {
                    "signals": [
                        signal.__dict__
                        for signal in ledger.read_signals(
                            session,
                            session,
                            epoch_id=self._epoch_id,
                            policy_id=policy_id,
                        )
                    ],
                    "recommendations": [],
                    "intents_staged": [],
                    "cutoff_late": [],
                    "regime": {},
                    "account": snapshots[0].__dict__,
                    "trades_opened": [
                        fill.fill_id for fill in fills if fill.side in {"buy", "short"}
                    ],
                    "trades_closed": [
                        fill.fill_id for fill in fills if fill.side in {"sell", "cover"}
                    ],
                    "replayed": True,
                    "error": False,
                }
            elif len(snapshots) == 1 and phases_complete:
                cohort["marked_account"] = snapshots[0]
                stage_only.append(cohort)
            else:
                execution_needed.append(cohort)
        results.update(complete_replays)
        if not execution_needed and not stage_only:
            return results

        valid_cohorts: list[dict[str, Any]] = list(stage_only)
        fresh_execution: list[dict[str, Any]] = []
        if execution_needed:
            for cohort in execution_needed:
                name = cohort["config"].name
                try:
                    context = cohort["ledger"].session_execution_context(session)
                    if context is None:
                        fresh_execution.append(cohort)
                        continue
                    persisted = cohort["executor"].persisted_input_bundle(session)
                    persisted_borrow = cohort["executor"].persisted_borrow_rates(
                        session
                    )
                    lifecycle = cohort["executor"].execute_open_and_mark(
                        session,
                        self._epoch_id,
                        persisted,
                        persisted_borrow,
                        processed_at,
                    )
                except Exception as error:
                    logger.error("Cohort %s stored resume failed", name, exc_info=True)
                    results[name] = {"error": True, "invalid_reason": str(error)}
                    continue
                if not lifecycle.valid or lifecycle.snapshot is None:
                    results[name] = {
                        "error": True,
                        "invalid_reason": lifecycle.invalid_reason,
                    }
                    continue
                cohort["marked_account"] = lifecycle.snapshot
                valid_cohorts.append(cohort)

        if fresh_execution:
            required_tickers = tuple(
                sorted(
                    {
                        ticker
                        for cohort in fresh_execution
                        for ticker in cohort["executor"].required_tickers(session)
                    }
                )
            )
            benchmark_symbols = fresh_execution[0]["executor"].benchmark_symbols
            try:
                bundle = SessionExecutor.fetch_input_bundle(
                    session,
                    required_tickers,
                    self._price_source,
                    benchmark_symbols,
                )
                SessionExecutor.validate_shared_action_response(
                    bundle.actions, bundle.tickers, session
                )
                processed_at = datetime.now(timezone.utc)
            except CorporateActionBatchError as error:
                for cohort in fresh_execution:
                    required = cohort["executor"].required_tickers(session)
                    reason = cohort["ledger"].reject_corporate_action_batch(
                        session,
                        error.actions,
                        required,
                        error.errors,
                        processed_at,
                    )
                    results[cohort["config"].name] = {
                        "error": True,
                        "invalid_reason": reason,
                    }
                bundle = None
            except Exception as error:
                reason = f"shared session input fetch failed: {error}"
                for cohort in fresh_execution:
                    cohort["ledger"].invalidate_session_and_cancel_due(
                        session, reason, processed_at
                    )
                    results[cohort["config"].name] = {
                        "error": True,
                        "invalid_reason": reason,
                    }
                if not valid_cohorts:
                    return results
                bundle = None

            if bundle is not None:
                for cohort in fresh_execution:
                    name = cohort["config"].name
                    try:
                        required = cohort["executor"].required_tickers(session)
                        lifecycle = cohort["executor"].execute_open_and_mark(
                            session,
                            self._epoch_id,
                            bundle.for_tickers(required),
                            {},
                            processed_at,
                        )
                    except Exception as error:
                        logger.error("Cohort %s execution failed", name, exc_info=True)
                        results[name] = {"error": True, "invalid_reason": str(error)}
                        continue
                    if not lifecycle.valid or lifecycle.snapshot is None:
                        results[name] = {
                            "error": True,
                            "invalid_reason": lifecycle.invalid_reason,
                        }
                        continue
                    cohort["marked_account"] = lifecycle.snapshot
                    valid_cohorts.append(cohort)

        if not valid_cohorts:
            return results

        first_engine = self.cohorts[0]["engine"]
        lookback_start = (
            datetime.strptime(trading_date, "%Y-%m-%d") - timedelta(days=90)
        ).strftime("%Y-%m-%d")
        shared_data = first_engine._fetch_all_data(lookback_start, trading_date)
        logger.info("Shared data fetched: %s", list(shared_data.keys()))

        horizons = sorted({cohort["config"].horizon for cohort in valid_cohorts})
        horizon_signals: dict[str, tuple[list[dict], dict]] = {}
        for horizon in horizons:
            signals, regime = self._screen_for_horizon(
                shared_data, trading_date, horizon
            )
            horizon_signals[horizon] = (signals, regime)
            logger.info("Horizon %s: %d signals", horizon, len(signals))

        all_signals = [
            signal for signals, _ in horizon_signals.values() for signal in signals
        ]
        enrichment = self._fetch_openbb_enrichment(all_signals)
        reference_tickers = {
            str(signal.get("ticker", "")).strip().upper()
            for signal in all_signals
            if signal.get("ticker")
        }
        reference_tickers.update(
            str(position["ticker"])
            for cohort in valid_cohorts
            for position in cohort["ledger"].open_positions()
        )
        try:
            shared_data["_execution_reference_bars"] = ensure_reference_bars(
                self._price_source,
                reference_tickers,
                session,
                processed_at,
                timedelta(
                    hours=float(
                        self._base_config.get("autoresearch", {})
                        .get("paper_ledger", {})
                        .get("bar_max_age_hours", 24)
                    )
                ),
            )
        except Exception as error:
            reason = f"candidate reference-bar validation failed: {error}"
            for cohort in valid_cohorts:
                results[cohort["config"].name] = {
                    "error": True,
                    "invalid_reason": reason,
                }
            return results

        for cohort in valid_cohorts:
            cfg = cohort["config"]
            name = cfg.name
            signals, regime = horizon_signals[cfg.horizon]
            try:
                staged = cohort["engine"].screen_and_stage(
                    trading_date=trading_date,
                    data=shared_data,
                    shared_signals=signals,
                    shared_regime=regime,
                    enrichment=enrichment,
                    size_profile=cohort.get("size_profile"),
                    marked_account=cohort["marked_account"],
                )
                fills = cohort["ledger"].read_fills(session, session)
                staged["trades_opened"] = [
                    fill.fill_id for fill in fills if fill.side in {"buy", "short"}
                ]
                staged["trades_closed"] = [
                    fill.fill_id for fill in fills if fill.side in {"sell", "cover"}
                ]
                staged["error"] = False
                results[name] = staged
            except Exception as error:
                logger.error("Cohort %s staging failed", name, exc_info=True)
                results[name] = {"error": True, "invalid_reason": str(error)}

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
            result = openbb_source.fetch(
                {"method": "equity_short_interest", "ticker": ticker}
            )
            if "error" not in result:
                short_interest[ticker] = result
        if short_interest:
            enrichment["short_interest"] = short_interest

        # Fetch Fama-French factors (once, not per ticker)
        factors = openbb_source.fetch({"method": "factors_fama_french"})
        if "error" not in factors:
            enrichment["factors"] = factors.get("factors", {})

        # Fetch commodity futures curves for commodity signals
        from tradingagents.strategies.modules.commodity_macro import (
            ETF_TO_FUTURES_UNDERLYING,
        )

        commodity_tickers = [t for t in tickers if t in ETF_TO_FUTURES_UNDERLYING]
        if commodity_tickers:
            curves = {}
            for ticker in commodity_tickers:
                underlying = ETF_TO_FUTURES_UNDERLYING.get(ticker)
                if underlying is None:
                    continue
                result = openbb_source.fetch(
                    {
                        "method": "commodity_futures_curve",
                        "symbol": underlying,
                    }
                )
                if "error" not in result:
                    curves[underlying] = result
            if curves:
                enrichment["commodity_futures_curves"] = curves

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
    base_state_dir = base_config.get("autoresearch", {}).get("state_dir", "data/state")
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
