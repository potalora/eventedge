from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

Direction = Literal["long", "short", "neutral"]
OutcomeStatus = Literal["pending", "valid", "invalid"]
EpochStatus = Literal["open", "closed", "invalid"]
HealthStatus = Literal[
    "signals", "legitimate_no_event", "data_failure", "strategy_defect"
]

METRIC_SCHEMA_VERSION = 2
LEGACY_SCHEMA_LABEL = "1_legacy_calendar_signed"
OUTCOME_WINDOWS = (5, 10, 20, 30)


@dataclass(frozen=True)
class MetricEpoch:
    epoch_id: str
    generation_id: str
    generation_commit: str
    behavior_hash: str
    config_hash: str
    metric_schema_version: int
    execution_clock_version: str
    pricing_version: str
    cost_model_version: str
    start_session: date
    end_session: date | None
    status: EpochStatus
    boundary_reason: str


@dataclass(frozen=True)
class SignalMetricRecord:
    event_key: str
    signal_id: str
    epoch_id: str
    policy_id: str
    strategy: str
    ticker: str
    direction: Direction
    decision_at: datetime
    reference_session: date


@dataclass(frozen=True)
class SignalConflict:
    epoch_id: str
    event_key: str
    strategy: str
    policy_id: str
    directions: tuple[str, ...]


@dataclass(frozen=True)
class DeduplicationResult:
    records: tuple[SignalMetricRecord, ...]
    conflicts: tuple[SignalConflict, ...]


@dataclass(frozen=True)
class OutcomeRecord:
    outcome_id: str
    signal_id: str
    event_key: str
    epoch_id: str
    strategy: str
    policy_id: str
    ticker: str
    direction: Direction
    holding_sessions: int
    entry_session: date
    exit_session: date
    entry_price: Decimal | None
    exit_price: Decimal | None
    raw_return: Decimal | None
    signed_return: Decimal | None
    status: OutcomeStatus
    invalid_reason: str


@dataclass(frozen=True)
class StrategyHealthRecord:
    health_id: str
    epoch_id: str
    session: date
    policy_id: str
    strategy: str
    status: HealthStatus
    signal_count: int
    evidence: dict[str, object]


@dataclass(frozen=True)
class PortfolioMetrics:
    cohort_id: str
    epoch_id: str
    metric_schema_version: int
    start_session: date
    end_session: date
    valuation_at: datetime
    benchmark_at: datetime
    valid_sessions: int
    total_return: float
    gross_return: float
    matched_benchmark_return: float
    matched_excess_return: float
    annualized_daily_net_sharpe: float | None
    sharpe_return_count: int
    annualized_matched_information_ratio: float | None
    information_ratio_return_count: int
    max_drawdown: float
    long_weight: float
    short_weight: float
    gross_weight: float
    net_weight: float
    cash_weight: float
    cumulative_costs: dict[str, float]
    unique_catalysts: int
    strategy_decisions: int
    fills: int
    closed_trades: int
    missing_mark_count: int
    stale_mark_count: int


@dataclass(frozen=True)
class PairedComparison:
    candidate_epoch_id: str
    baseline_epoch_id: str
    common_sessions: tuple[date, ...]
    candidate_return: float
    baseline_return: float
    excess_return: float
