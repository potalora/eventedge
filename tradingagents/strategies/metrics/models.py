from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Literal, Mapping

Direction = Literal["long", "short", "neutral"]
OutcomeStatus = Literal["pending", "valid", "invalid"]
EpochStatus = Literal["open", "closed", "invalid"]
HealthStatus = Literal[
    "signals", "legitimate_no_event", "data_failure", "strategy_defect"
]
CriticalGapStatus = Literal["pending", "completed"]
CriticalGapDetailStatus = Literal["minimal", "ready", "legacy_unbound"]
CandidateBarRecoveryOutcome = Literal["accepted", "recovered", "quarantined"]

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
class CriticalGapMarker:
    """Bounded shared recovery intent for a cross-database critical gap."""

    marker_id: str
    epoch_id: str
    gap_session: date
    reason: str
    cohort_invalid_reasons: dict[str, dict[str, str]]
    status: CriticalGapStatus
    affected_cohorts: dict[str, str] = field(default_factory=dict)
    detail_status: CriticalGapDetailStatus = "legacy_unbound"
    corporate_action_rejections: dict[str, dict[str, object]] = field(
        default_factory=dict
    )


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
class CandidateBarRecoveryRecord:
    """Immutable bounded audit evidence for one candidate-bar recovery outcome."""

    recovery_id: str
    epoch_id: str
    session: date
    ticker: str
    outcome: CandidateBarRecoveryOutcome
    attempts: tuple[dict[str, object], ...]
    signal_identities: tuple[dict[str, str], ...]


def _canonical_timestamp(value: object) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("governed bar recovery timestamp must be timezone-aware")
        return value.isoformat()
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("governed bar recovery timestamp is invalid") from error
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("governed bar recovery timestamp must be timezone-aware")
        return parsed.isoformat()
    raise ValueError("governed bar recovery timestamp is invalid")


def _canonical_value(value: object, *, key: str | None = None) -> object:
    if key in {"fetched_at", "start", "created_at"}:
        return _canonical_timestamp(value)
    if isinstance(value, datetime):
        return _canonical_timestamp(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("governed bar recovery decimal is not finite")
        return str(value)
    if isinstance(value, Mapping):
        return {
            str(item_key): _canonical_value(item_value, key=str(item_key))
            for item_key, item_value in sorted(value.items())
        }
    if isinstance(value, (tuple, list)):
        return tuple(_canonical_value(item) for item in value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("governed bar recovery float is not finite")
        return value
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    raise ValueError("governed bar recovery value is not canonical JSON")


def _deep_freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _deep_freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (tuple, list)):
        return tuple(_deep_freeze(item) for item in value)
    return value


def canonical_governed_recovery_json(payload: Mapping[str, object]) -> str:
    """Return the single canonical representation used for durable evidence."""
    return json.dumps(
        _canonical_value(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )


@dataclass(frozen=True)
class GovernedBarRecoveryRecord:
    """Immutable canonical evidence for one governed daily-bar reconstruction."""

    recovery_id: str
    contract_version: str
    evidence_digest: str
    epoch_id: str
    session: date
    ticker: str
    original_daily: Mapping[str, object]
    original_validation_error: str | None
    expected_starts: tuple[str, ...]
    observed_starts: tuple[str, ...]
    intraday_rows: tuple[Mapping[str, object], ...]
    reconstructed_bar: Mapping[str, object]
    final_validation_error: str | None
    affected_cohort_ids: tuple[str, ...]

    @classmethod
    def create(
        cls,
        *,
        contract_version: str,
        epoch_id: str,
        session: date,
        ticker: str,
        original_daily: Mapping[str, object],
        original_validation_error: str | None,
        expected_starts: tuple[object, ...] | list[object],
        observed_starts: tuple[object, ...] | list[object],
        intraday_rows: tuple[Mapping[str, object], ...]
        | list[Mapping[str, object]],
        reconstructed_bar: Mapping[str, object],
        final_validation_error: str | None,
        affected_cohort_ids: tuple[str, ...] | list[str],
        evidence_digest: str | None = None,
        recovery_id: str | None = None,
    ) -> "GovernedBarRecoveryRecord":
        if not isinstance(session, date) or isinstance(session, datetime):
            raise ValueError("governed bar recovery session is invalid")
        canonical_original = _canonical_value(original_daily)
        if not isinstance(canonical_original, dict):
            raise ValueError("governed bar recovery original daily evidence is invalid")
        fields: dict[str, object] = {
            "contract_version": contract_version,
            "epoch_id": epoch_id,
            "session": session,
            "ticker": ticker.upper(),
            "original_daily": canonical_original,
            "original_validation_error": original_validation_error,
            "expected_starts": tuple(_canonical_timestamp(item) for item in expected_starts),
            "observed_starts": tuple(_canonical_timestamp(item) for item in observed_starts),
            "intraday_rows": tuple(
                _canonical_value(item) for item in intraday_rows
            ),
            "reconstructed_bar": _canonical_value(reconstructed_bar),
            "final_validation_error": final_validation_error,
            "affected_cohort_ids": tuple(sorted(set(affected_cohort_ids))),
        }
        canonical_fields = _canonical_value(fields)
        if not isinstance(canonical_fields, dict):  # pragma: no cover - typed above
            raise ValueError("governed bar recovery evidence is invalid")
        digest = "sha256:" + hashlib.sha256(
            canonical_governed_recovery_json(canonical_fields).encode("utf-8")
        ).hexdigest()
        identity = {
            "contract_version": canonical_fields["contract_version"],
            "epoch_id": canonical_fields["epoch_id"],
            "session": canonical_fields["session"],
            "ticker": canonical_fields["ticker"],
            "evidence_digest": digest,
        }
        computed_recovery_id = "governed_bar_recovery:" + hashlib.sha256(
            canonical_governed_recovery_json(identity).encode("utf-8")
        ).hexdigest()
        if evidence_digest is not None and evidence_digest != digest:
            raise ValueError("governed bar recovery evidence digest does not match payload")
        if recovery_id is not None and recovery_id != computed_recovery_id:
            raise ValueError("governed bar recovery recovery id does not match payload")
        return cls(
            recovery_id=computed_recovery_id,
            contract_version=str(canonical_fields["contract_version"]),
            evidence_digest=digest,
            epoch_id=str(canonical_fields["epoch_id"]),
            session=session,
            ticker=str(canonical_fields["ticker"]),
            original_daily=_deep_freeze(canonical_fields["original_daily"]),
            original_validation_error=canonical_fields["original_validation_error"],
            expected_starts=tuple(canonical_fields["expected_starts"]),
            observed_starts=tuple(canonical_fields["observed_starts"]),
            intraday_rows=tuple(
                _deep_freeze(row) for row in canonical_fields["intraday_rows"]
            ),
            reconstructed_bar=_deep_freeze(canonical_fields["reconstructed_bar"]),
            final_validation_error=canonical_fields["final_validation_error"],
            affected_cohort_ids=tuple(canonical_fields["affected_cohort_ids"]),
        )

    def evidence_fields(self) -> dict[str, object]:
        """Return the complete evidence excluding its derived identifiers."""
        return {
            "contract_version": self.contract_version,
            "epoch_id": self.epoch_id,
            "session": self.session,
            "ticker": self.ticker,
            "original_daily": self.original_daily,
            "original_validation_error": self.original_validation_error,
            "expected_starts": self.expected_starts,
            "observed_starts": self.observed_starts,
            "intraday_rows": self.intraday_rows,
            "reconstructed_bar": self.reconstructed_bar,
            "final_validation_error": self.final_validation_error,
            "affected_cohort_ids": self.affected_cohort_ids,
        }

    def canonical_payload(self) -> str:
        return canonical_governed_recovery_json(
            {
                "recovery_id": self.recovery_id,
                "evidence_digest": self.evidence_digest,
                **self.evidence_fields(),
            }
        )

    def validate_integrity(self) -> None:
        """Fail closed when a record was constructed or mutated outside create()."""
        canonical = self.create(
            **self.evidence_fields(),
            evidence_digest=self.evidence_digest,
            recovery_id=self.recovery_id,
        )
        if (
            canonical != self
            or not isinstance(self.original_daily, MappingProxyType)
            or not isinstance(self.reconstructed_bar, MappingProxyType)
            or any(
                not isinstance(row, MappingProxyType) for row in self.intraday_rows
            )
        ):
            raise ValueError("governed bar recovery record is not canonical")


@dataclass(frozen=True)
class CandidateSignalIdentityBinding:
    """Immutable per-horizon signal identity universe for one staging session."""

    binding_id: str
    epoch_id: str
    session: date
    identities: tuple[dict[str, str], ...]


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
