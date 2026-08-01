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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tradingagents.strategies.metrics.models import (
        CriticalGapMarker,
        StrategyHealthRecord,
    )

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
        name
        for name, result in results.items()
        if isinstance(result, dict)
        and (
            result.get("error")
            or result.get("valid") is False
            or bool(result.get("invalid_reason"))
        )
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
        generation_id: str,
        generation_commit: str,
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
        from tradingagents.strategies.metrics.store import MetricStore
        from tradingagents.strategies.orchestration.metric_epoch_context import (
            CohortSemanticPolicy,
            build_epoch_context,
        )
        from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger
        from tradingagents.strategies.state.state import StateManager
        from tradingagents.strategies.modules import get_paper_trade_strategies

        if not isinstance(generation_id, str) or not generation_id.strip():
            raise ValueError("generation_id must be non-empty text")
        if not isinstance(generation_commit, str) or not generation_commit.strip():
            raise ValueError("generation_commit must be non-empty text")
        model_keys = (
            "llm_provider",
            "deep_think_llm",
            "quick_think_llm",
            "cache_model",
            "live_model",
            "strategist_model",
            "cro_model",
            "autoresearch_model",
        )
        ar_config = base_config.get("autoresearch", {})
        models = {
            key: (base_config.get(key) if index < 3 else ar_config.get(key))
            for index, key in enumerate(model_keys)
        }
        for key, value in models.items():
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"model {key} must be non-empty text")

        self.cohorts: list[dict[str, Any]] = []
        if base_config.get("execution", {}).get("mode", "paper") != "paper":
            raise ValueError("CohortOrchestrator is paper-only")
        strategies = get_paper_trade_strategies()
        strategy_names = tuple(
            getattr(strategy, "name", None) for strategy in strategies
        )
        if any(
            not isinstance(name, str) or not name.strip() for name in strategy_names
        ):
            raise ValueError("strategy names must be non-empty text")
        if len(set(strategy_names)) != len(strategy_names):
            raise ValueError("duplicate strategy name")
        self._active_strategy_names = frozenset(strategy_names)
        cohort_names = [cfg.name for cfg in cohort_configs]
        if any(not isinstance(name, str) or not name.strip() for name in cohort_names):
            raise ValueError("cohort names must be non-empty text")
        if len(set(cohort_names)) != len(cohort_names):
            raise ValueError("duplicate cohort name")
        configured_policy_id = (
            base_config.get("autoresearch", {}).get("paper_ledger", {}).get("policy_id")
        )
        policy_ids: dict[str, str] = {}
        for cfg in cohort_configs:
            for label, value in (
                ("horizon", cfg.horizon),
                ("size_profile", cfg.size_profile),
            ):
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"cohort {label} must be non-empty text")
            if not isinstance(cfg.use_llm, bool):
                raise ValueError("cohort use_llm must be boolean")
            policy_id = (
                configured_policy_id
                if configured_policy_id is not None
                else f"foundation-{cfg.horizon}"
            )
            if not isinstance(policy_id, str) or not policy_id.strip():
                raise ValueError("policy_id must be non-empty text")
            policy_ids[cfg.name] = policy_id.strip()
        metric_store = MetricStore(
            Path(base_config.get("autoresearch", {}).get("state_dir", "data/state"))
            / "metrics_v2.sqlite3"
        )
        self._metric_store = metric_store

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
            executor = SessionExecutor(ledger, cohort_config, metric_store=metric_store)
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

        cohort_policies = tuple(
            CohortSemanticPolicy(
                name=cohort["config"].name,
                horizon=cohort["config"].horizon,
                size_profile=cohort["config"].size_profile,
                policy_id=policy_ids[cohort["config"].name],
                use_llm=cohort["config"].use_llm,
                learning_enabled=cohort["config"].learning_enabled,
                execution_policy=cohort["executor"].semantic_policy_document(),
            )
            for cohort in self.cohorts
        )
        self._metric_epoch_context = build_epoch_context(
            generation_id=generation_id,
            generation_commit=generation_commit,
            models=models,
            strategies=strategy_names,
            cohort_policies=cohort_policies,
        )

        self._base_config = base_config
        self._price_source = price_source or YFinancePriceSource()
        self._epoch_id: str | None = None
        self._after_gap_blocker = lambda marker: None
        self._after_gap_marker = lambda marker: None
        self._after_gap_p0_invalidation = lambda marker: None
        self._after_gap_metric_invalidation = lambda marker: None

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

    def _policy_id_for_horizon(self, horizon: str) -> str:
        configured_policy_id = (
            self._base_config.get("autoresearch", {})
            .get("paper_ledger", {})
            .get("policy_id")
        )
        return str(configured_policy_id or f"foundation-{horizon}")

    def _screen_for_horizon(
        self,
        data: dict,
        trading_date: str,
        horizon: str,
    ) -> tuple[list[dict], dict, list[StrategyHealthRecord]]:
        """Screen all strategies with horizon-specific params."""
        first_engine = self.cohorts[0]["engine"]
        policy_id = self._policy_id_for_horizon(horizon)
        if self._epoch_id is None:
            raise RuntimeError("metric epoch must exist before strategy screening")
        return first_engine.screen_and_enrich(
            trading_date,
            data,
            horizon=horizon,
            epoch_id=self._epoch_id,
            policy_id=policy_id,
        )

    def _persist_horizon_health(
        self,
        health: list[StrategyHealthRecord],
        session: date,
        policy_id: str,
    ) -> bool:
        """Persist one shared horizon's evidence and enforce full coverage."""
        for record in health:
            self._metric_store.save_strategy_health(record)
        if (
            len(health) == len(self._active_strategy_names) == 12
            and {record.strategy for record in health} == self._active_strategy_names
            and all(record.epoch_id == self._epoch_id for record in health)
            and all(record.session == session for record in health)
            and all(record.policy_id == policy_id for record in health)
        ):
            return True

        from tradingagents.strategies.metrics.epochs import EpochManager

        EpochManager(self._metric_store).invalidate_current(
            session, "unclassified_strategy_silence"
        )
        return False

    def _bound_critical_gap_cohorts(
        self, marker: CriticalGapMarker
    ) -> list[dict[str, Any]]:
        """Resolve every original cohort to its exact opaque ledger binding."""
        if marker.detail_status == "legacy_unbound" or not marker.affected_cohorts:
            raise ValueError("critical gap cohort binding is missing")
        current: dict[str, tuple[str, dict[str, Any]]] = {}
        seen_bindings: set[str] = set()
        for cohort in self.cohorts:
            name = cohort["config"].name
            binding = cohort["ledger"].recovery_binding_id()
            if name in current or binding in seen_bindings:
                raise ValueError("critical gap cohort binding is duplicated")
            if cohort["ledger"].cohort_id != name:
                raise ValueError("critical gap cohort binding is incompatible")
            current[name] = (binding, cohort)
            seen_bindings.add(binding)
        resolved: list[dict[str, Any]] = []
        for name, expected_binding in marker.affected_cohorts.items():
            bound = current.get(name)
            if bound is None:
                raise ValueError(f"critical gap cohort binding is missing for {name}")
            actual_binding, cohort = bound
            if actual_binding != expected_binding:
                raise ValueError(
                    f"critical gap cohort binding is incompatible for {name}"
                )
            resolved.append(cohort)
        return resolved

    def _invalidate_gap_epoch_after_boundary_error(
        self,
        epoch_id: str,
        gap_session: date,
        error: Exception,
    ) -> None:
        """Close the exact epoch while retaining the boundary error as primary."""
        try:
            if self.cohorts:
                self.cohorts[0]["executor"].invalidate_metric_epoch(
                    gap_session,
                    epoch_id=epoch_id,
                )
            else:
                self._metric_store.invalidate_epoch(
                    epoch_id,
                    gap_session,
                    "critical_market_data_gap",
                )
        except Exception as invalidation_error:
            raise error from invalidation_error

    def _build_critical_gap_detail_marker(
        self,
        minimal_marker: CriticalGapMarker,
        session: date,
        processed_at: datetime,
        raw_bars: dict,
        corporate_action_errors: dict[str, Any] | None,
    ) -> CriticalGapMarker:
        """Build provider-derived recovery detail after the blocker is durable."""
        cohort_invalid_reasons: dict[str, dict[str, str]] = {}
        for cohort in self.cohorts:
            executor = cohort["executor"]
            due = executor.due_outcome_signals(session, self._epoch_id)
            if not due:
                continue
            _, invalid_reasons = executor.validated_outcome_bars(
                session, self._epoch_id, raw_bars, processed_at
            )
            for signal, _ in due:
                invalid_reasons.setdefault(signal.ticker, "critical_market_data_gap")
            cohort_invalid_reasons[cohort["config"].name] = dict(
                sorted(invalid_reasons.items())
            )
        corporate_action_rejections: dict[str, dict[str, object]] = {}
        for cohort in self.cohorts:
            name = cohort["config"].name
            action_error = (corporate_action_errors or {}).get(name)
            if action_error is None:
                continue
            actions = sorted(
                action_error.actions,
                key=lambda action: (
                    action.action_id,
                    action.ticker,
                    action.session.isoformat(),
                    action.action_type,
                    str(action.ratio),
                    str(action.cash_per_share),
                    action.source,
                    action.fetched_at.isoformat(),
                    action.verified,
                ),
            )
            corporate_action_rejections[name] = {
                "actions": [
                    {
                        "action_id": action.action_id,
                        "ticker": action.ticker,
                        "session": action.session.isoformat(),
                        "action_type": action.action_type,
                        "ratio": (
                            format(action.ratio, "f")
                            if action.ratio is not None
                            else None
                        ),
                        "cash_per_share": (
                            format(action.cash_per_share, "f")
                            if action.cash_per_share is not None
                            else None
                        ),
                        "source": action.source,
                        "fetched_at": action.fetched_at.isoformat(),
                        "verified": action.verified,
                    }
                    for action in actions
                ],
                "governed_tickers": list(
                    cohort["executor"].required_tickers(session, self._epoch_id)
                ),
                "errors": sorted(set(action_error.errors)),
            }
        return replace(
            minimal_marker,
            cohort_invalid_reasons=dict(sorted(cohort_invalid_reasons.items())),
            detail_status="ready",
            corporate_action_rejections=dict(
                sorted(corporate_action_rejections.items())
            ),
        )

    def _stop_for_critical_market_data_gap(
        self,
        session: date,
        processed_at: datetime,
        results: dict[str, Any],
        raw_bars: dict,
        reason: str,
        *,
        corporate_action_errors: dict[str, Any] | None = None,
        original_error: Exception | None = None,
    ) -> dict[str, Any]:
        """Start and complete one durable cross-database gap transition."""
        from tradingagents.strategies.execution.ids import stable_id
        from tradingagents.strategies.metrics.models import CriticalGapMarker

        if self._epoch_id is None:
            raise RuntimeError("metric epoch is not initialized")
        epoch_id = self._epoch_id
        store = self._metric_store
        try:
            affected_cohorts = {
                cohort["config"].name: cohort["ledger"].recovery_binding_id()
                for cohort in self.cohorts
            }
            minimal_marker = CriticalGapMarker(
                marker_id=stable_id("critical_gap_marker", epoch_id, session),
                epoch_id=epoch_id,
                gap_session=session,
                reason="critical_market_data_gap",
                cohort_invalid_reasons={},
                status="pending",
                affected_cohorts=dict(sorted(affected_cohorts.items())),
                detail_status="minimal",
                corporate_action_rejections={},
            )
            minimal_marker = store.begin_critical_gap(minimal_marker)
        except Exception as marker_error:
            self._invalidate_gap_epoch_after_boundary_error(
                epoch_id, session, marker_error
            )
            if original_error is not None:
                raise original_error from marker_error
            raise
        try:
            self._after_gap_blocker(minimal_marker)
            marker = self._build_critical_gap_detail_marker(
                minimal_marker,
                session,
                processed_at,
                raw_bars,
                corporate_action_errors,
            )
            marker = store.attach_critical_gap_details(marker)
        except Exception as detail_error:
            self._invalidate_gap_epoch_after_boundary_error(
                epoch_id, session, detail_error
            )
            if original_error is not None:
                raise original_error from detail_error
            raise
        self._after_gap_marker(marker)
        return self._complete_pending_critical_gap(
            marker,
            processed_at,
            results,
            result_reason=reason,
            original_error=original_error,
        )

    def _complete_pending_critical_gap(
        self,
        marker: CriticalGapMarker,
        processed_at: datetime,
        results: dict[str, Any] | None = None,
        *,
        result_reason: str | None = None,
        original_error: Exception | None = None,
    ) -> dict[str, Any]:
        """Idempotently finish outcomes, P0 invalidation, epoch close, and marker."""
        from tradingagents.strategies.execution.models import CorporateAction
        from tradingagents.strategies.orchestration.session_executor import PHASES

        results = results if results is not None else {}
        self._epoch_id = marker.epoch_id
        try:
            affected_cohorts = self._bound_critical_gap_cohorts(marker)
            if marker.detail_status != "ready":
                raise ValueError("critical gap recovery detail is not ready")
        except Exception as boundary_error:
            self._invalidate_gap_epoch_after_boundary_error(
                marker.epoch_id, marker.gap_session, boundary_error
            )
            raise
        committed = {
            cohort["config"].name
            for cohort in affected_cohorts
            if cohort["ledger"].read_snapshots(
                marker.gap_session,
                marker.gap_session,
                epoch_id=marker.epoch_id,
                valid_only=True,
            )
            and all(
                cohort["ledger"].phase_completed(marker.gap_session, phase)
                for phase in PHASES
            )
        }
        recovery_error: Exception | None = None
        audit_error: Exception | None = None
        try:
            for cohort in affected_cohorts:
                executor = cohort["executor"]
                invalid_reasons = marker.cohort_invalid_reasons.get(
                    cohort["config"].name, {}
                )
                if invalid_reasons:
                    executor.record_due_invalid_outcomes(
                        marker.gap_session,
                        marker.epoch_id,
                        invalid_reasons,
                        preserve_existing=True,
                    )
        except Exception as error:
            recovery_error = error
        for cohort in affected_cohorts:
            name = cohort["config"].name
            rejection = marker.corporate_action_rejections.get(name)
            if rejection is None:
                continue
            try:
                actions = tuple(
                    CorporateAction(
                        str(action["action_id"]),
                        str(action["ticker"]),
                        date.fromisoformat(str(action["session"])),
                        str(action["action_type"]),
                        (
                            Decimal(str(action["ratio"]))
                            if action["ratio"] is not None
                            else None
                        ),
                        (
                            Decimal(str(action["cash_per_share"]))
                            if action["cash_per_share"] is not None
                            else None
                        ),
                        str(action["source"]),
                        datetime.fromisoformat(str(action["fetched_at"])),
                        bool(action["verified"]),
                    )
                    for action in rejection["actions"]
                )
                cohort["ledger"].reject_corporate_action_batch(
                    marker.gap_session,
                    actions,
                    tuple(str(value) for value in rejection["governed_tickers"]),
                    tuple(str(value) for value in rejection["errors"]),
                    processed_at,
                    preserve_committed_session=name in committed,
                )
            except Exception as error:
                if audit_error is None:
                    audit_error = error
        for cohort in affected_cohorts:
            name = cohort["config"].name
            if name in committed:
                results.setdefault(
                    name,
                    {
                        "error": True,
                        "invalid_reason": result_reason or marker.reason,
                    },
                )
                continue
            ledger = cohort["ledger"]
            if not ledger.session_invalid_reason(marker.gap_session):
                ledger.invalidate_session_and_cancel_due(
                    marker.gap_session, marker.reason, processed_at
                )
            results[name] = {
                "error": True,
                "invalid_reason": ledger.session_invalid_reason(marker.gap_session)
                or result_reason
                or marker.reason,
            }
        affected_names = set(marker.affected_cohorts)
        for cohort in self.cohorts:
            name = cohort["config"].name
            if name not in affected_names:
                results.setdefault(
                    name,
                    {"error": True, "invalid_reason": marker.reason},
                )
        self._after_gap_p0_invalidation(marker)
        self.cohorts[0]["executor"].invalidate_metric_epoch(
            marker.gap_session,
            epoch_id=marker.epoch_id,
        )
        self._after_gap_metric_invalidation(marker)
        if recovery_error is None and audit_error is None:
            self._metric_store.complete_critical_gap(marker.marker_id)
        if original_error is not None:
            raise original_error
        if recovery_error is not None:
            raise recovery_error
        if audit_error is not None:
            raise audit_error
        return results

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
        pending_gap = self._metric_store.pending_critical_gap()
        if pending_gap is not None:
            if session < pending_gap.gap_session:
                raise ValueError(
                    f"{session} precedes pending critical gap {pending_gap.gap_session}"
                )
            if not self.cohorts:
                boundary_error = ValueError(
                    "critical gap cohort binding is missing for every affected cohort"
                )
                self._invalidate_gap_epoch_after_boundary_error(
                    pending_gap.epoch_id,
                    pending_gap.gap_session,
                    boundary_error,
                )
                raise boundary_error
            recovered = self._complete_pending_critical_gap(
                pending_gap, processed_at, results
            )
            if session == pending_gap.gap_session:
                return recovered
        if not self.cohorts:
            return results
        metric_epoch = self.cohorts[0]["executor"].ensure_metric_epoch(
            self._metric_epoch_context, session
        )
        self._epoch_id = metric_epoch.epoch_id
        if metric_epoch.status == "invalid":
            for cohort in self.cohorts:
                reason = cohort["ledger"].session_invalid_reason(session)
                results[cohort["config"].name] = {
                    "error": True,
                    "invalid_reason": reason or metric_epoch.boundary_reason,
                }
            return results

        complete_replays: dict[str, Any] = {}
        completed_cohorts: list[dict[str, Any]] = []
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
                    return self._stop_for_critical_market_data_gap(
                        session,
                        processed_at,
                        results,
                        {},
                        str(error),
                        original_error=error,
                    )
            if len(snapshots) == 1 and phases_complete and staging_complete:
                completed_cohorts.append(cohort)
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
        for cohort in completed_cohorts:
            name = cohort["config"].name
            executor = cohort["executor"]
            if not executor.due_outcome_signals(session, self._epoch_id):
                continue
            try:
                executor.record_due_outcomes(
                    session,
                    self._epoch_id,
                    executor.persisted_input_bundle(session).bars,
                )
            except Exception as error:
                results[name] = {"error": True, "invalid_reason": str(error)}
                return self._stop_for_critical_market_data_gap(
                    session,
                    processed_at,
                    results,
                    {},
                    str(error),
                    original_error=error,
                )
        if not execution_needed and not stage_only:
            return results

        valid_cohorts: list[dict[str, Any]] = list(stage_only)
        fresh_execution: list[dict[str, Any]] = []
        bundle = None
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
                    return self._stop_for_critical_market_data_gap(
                        session,
                        processed_at,
                        results,
                        {},
                        str(error),
                        original_error=error,
                    )
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
                        for ticker in cohort["executor"].required_tickers(
                            session, self._epoch_id
                        )
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
                corporate_errors = {}
                for cohort in fresh_execution:
                    reason = "invalid corporate action batch: " + "; ".join(
                        sorted(set(error.errors))
                    )
                    results[cohort["config"].name] = {
                        "error": True,
                        "invalid_reason": reason,
                    }
                    corporate_errors[cohort["config"].name] = error
                return self._stop_for_critical_market_data_gap(
                    session,
                    processed_at,
                    results,
                    bundle.bars,
                    "critical_market_data_gap",
                    corporate_action_errors=corporate_errors,
                )
            except Exception as error:
                reason = f"shared session input fetch failed: {error}"
                for cohort in fresh_execution:
                    results[cohort["config"].name] = {
                        "error": True,
                        "invalid_reason": reason,
                    }
                return self._stop_for_critical_market_data_gap(
                    session,
                    processed_at,
                    results,
                    {},
                    reason,
                )

            if bundle is not None:
                critical_gap = False
                for cohort in fresh_execution:
                    executor = cohort["executor"]
                    if not executor.due_outcome_signals(session, self._epoch_id):
                        continue
                    valid_bars, invalid_reasons = executor.validated_outcome_bars(
                        session,
                        self._epoch_id,
                        bundle.bars,
                        processed_at,
                    )
                    if not invalid_reasons:
                        continue
                    reason = "market data validation failed: " + "; ".join(
                        f"{ticker} {invalid_reasons[ticker]}"
                        for ticker in sorted(invalid_reasons)
                    )
                    results[cohort["config"].name] = {
                        "error": True,
                        "invalid_reason": reason,
                    }
                    critical_gap = True
                if critical_gap:
                    return self._stop_for_critical_market_data_gap(
                        session,
                        processed_at,
                        results,
                        bundle.bars,
                        "critical_market_data_gap",
                    )
                preflight_gap = False
                preflight_corporate_errors = {}
                for cohort in fresh_execution:
                    name = cohort["config"].name
                    executor = cohort["executor"]
                    required = executor.required_tickers(session, self._epoch_id)
                    try:
                        executor.validate_execution_input_bundle(
                            session,
                            self._epoch_id,
                            bundle.for_tickers(required),
                            processed_at,
                        )
                    except CorporateActionBatchError as error:
                        reason = "invalid corporate action batch: " + "; ".join(
                            sorted(set(error.errors))
                        )
                        results[name] = {
                            "error": True,
                            "invalid_reason": reason,
                        }
                        preflight_corporate_errors[name] = error
                        preflight_gap = True
                    except Exception as error:
                        reason = f"market data validation failed: {error}"
                        results[name] = {
                            "error": True,
                            "invalid_reason": reason,
                        }
                        preflight_gap = True
                if preflight_gap:
                    return self._stop_for_critical_market_data_gap(
                        session,
                        processed_at,
                        results,
                        bundle.bars,
                        "critical_market_data_gap",
                        corporate_action_errors=preflight_corporate_errors,
                    )
                execution_bundle_gap = False
                for cohort in fresh_execution:
                    name = cohort["config"].name
                    try:
                        required = cohort["executor"].required_tickers(
                            session, self._epoch_id
                        )
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
                        if lifecycle.invalid_reason.startswith(
                            "market data validation failed:"
                        ):
                            execution_bundle_gap = True
                        continue
                    cohort["marked_account"] = lifecycle.snapshot
                    valid_cohorts.append(cohort)
                if execution_bundle_gap:
                    return self._stop_for_critical_market_data_gap(
                        session,
                        processed_at,
                        results,
                        bundle.bars,
                        "critical_market_data_gap",
                    )

        if not valid_cohorts:
            return results

        outcome_ready: list[dict[str, Any]] = []
        for cohort in valid_cohorts:
            name = cohort["config"].name
            executor = cohort["executor"]
            if not executor.due_outcome_signals(session, self._epoch_id):
                outcome_ready.append(cohort)
                continue
            try:
                executor.record_due_outcomes(
                    session,
                    self._epoch_id,
                    executor.persisted_input_bundle(session).bars,
                )
            except Exception as error:
                results[name] = {"error": True, "invalid_reason": str(error)}
                return self._stop_for_critical_market_data_gap(
                    session,
                    processed_at,
                    results,
                    {},
                    str(error),
                    original_error=error,
                )
            outcome_ready.append(cohort)
        valid_cohorts = outcome_ready
        if not valid_cohorts:
            return results

        first_engine = self.cohorts[0]["engine"]
        lookback_start = (
            datetime.strptime(trading_date, "%Y-%m-%d") - timedelta(days=90)
        ).strftime("%Y-%m-%d")
        shared_data = first_engine._fetch_all_data(lookback_start, trading_date)
        logger.info("Shared data fetched: %s", list(shared_data.keys()))

        horizons = sorted({cohort["config"].horizon for cohort in valid_cohorts})
        horizon_signals: dict[
            str, tuple[list[dict], dict, list[StrategyHealthRecord]]
        ] = {}
        for horizon in horizons:
            signals, regime, health = self._screen_for_horizon(
                shared_data, trading_date, horizon
            )
            if not self._persist_horizon_health(
                health, session, self._policy_id_for_horizon(horizon)
            ):
                for cohort in valid_cohorts:
                    results[cohort["config"].name] = {
                        "error": True,
                        "invalid_reason": "unclassified_strategy_silence",
                    }
                return results
            horizon_signals[horizon] = (signals, regime, health)
            logger.info("Horizon %s: %d signals", horizon, len(signals))

        all_signals = [
            signal for signals, _, _ in horizon_signals.values() for signal in signals
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
            return self._stop_for_critical_market_data_gap(
                session,
                processed_at,
                results,
                bundle.bars if bundle is not None else {},
                "critical_market_data_gap",
                original_error=error,
            )

        for cohort in valid_cohorts:
            cfg = cohort["config"]
            name = cfg.name
            signals, regime, _health = horizon_signals[cfg.horizon]
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
