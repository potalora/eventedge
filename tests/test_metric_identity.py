import hashlib
import json
from dataclasses import fields, replace
from datetime import UTC, date, datetime

import pytest

from tradingagents.strategies.metrics.identity import (
    deduplicate_signals,
    event_key,
    execution_id,
    signal_id,
)
from tradingagents.strategies.metrics.models import (
    LEGACY_SCHEMA_LABEL,
    METRIC_SCHEMA_VERSION,
    OUTCOME_WINDOWS,
    DeduplicationResult,
    MetricEpoch,
    OutcomeRecord,
    PairedComparison,
    PortfolioMetrics,
    SignalConflict,
    SignalMetricRecord,
    StrategyHealthRecord,
)


def _record(direction: str) -> SignalMetricRecord:
    event = event_key(
        source="courtlistener",
        source_event_id="docket-42",
        ticker="AAPL",
        event_at=datetime(2026, 7, 30, 14, tzinfo=UTC),
        evidence_hash="abc",
    )
    return SignalMetricRecord(
        event_key=event,
        signal_id=signal_id("epoch-1", "litigation", "30d", direction, event),
        epoch_id="epoch-1",
        policy_id="30d",
        strategy="litigation",
        ticker="AAPL",
        direction=direction,
        decision_at=datetime(2026, 7, 30, 20, tzinfo=UTC),
        reference_session=date(2026, 7, 30),
    )


def test_event_key_is_generation_independent_and_stable() -> None:
    args = dict(
        source="edgar",
        source_event_id="accession-1",
        ticker="MSFT",
        event_at=datetime(2026, 7, 29, 12, tzinfo=UTC),
        evidence_hash="hash-1",
    )
    assert event_key(**args) == event_key(**dict(reversed(list(args.items()))))


def test_dedup_is_order_independent() -> None:
    long = _record("long")
    result_a = deduplicate_signals([long, long])
    result_b = deduplicate_signals(list(reversed([long, long])))
    assert result_a.records == result_b.records == (long,)
    assert result_a.conflicts == ()


def test_direction_conflict_is_explicit() -> None:
    result = deduplicate_signals([_record("short"), _record("long")])
    assert result.records == ()
    assert len(result.conflicts) == 1
    assert result.conflicts[0].directions == ("long", "short")


@pytest.mark.parametrize(
    "record_type",
    [
        MetricEpoch,
        SignalMetricRecord,
        SignalConflict,
        DeduplicationResult,
        OutcomeRecord,
        StrategyHealthRecord,
        PortfolioMetrics,
        PairedComparison,
    ],
)
def test_metric_records_are_frozen_dataclasses(record_type: type[object]) -> None:
    assert record_type.__dataclass_params__.frozen is True


def test_metric_record_fields_and_schema_constants_are_exact() -> None:
    assert METRIC_SCHEMA_VERSION == 2
    assert LEGACY_SCHEMA_LABEL == "1_legacy_calendar_signed"
    assert OUTCOME_WINDOWS == (5, 10, 20, 30)
    assert tuple(field.name for field in fields(MetricEpoch)) == (
        "epoch_id",
        "generation_id",
        "generation_commit",
        "behavior_hash",
        "config_hash",
        "metric_schema_version",
        "execution_clock_version",
        "pricing_version",
        "cost_model_version",
        "start_session",
        "end_session",
        "status",
        "boundary_reason",
    )
    assert tuple(field.name for field in fields(SignalMetricRecord)) == (
        "event_key",
        "signal_id",
        "epoch_id",
        "policy_id",
        "strategy",
        "ticker",
        "direction",
        "decision_at",
        "reference_session",
    )
    assert tuple(field.name for field in fields(SignalConflict)) == (
        "epoch_id",
        "event_key",
        "strategy",
        "policy_id",
        "directions",
    )
    assert tuple(field.name for field in fields(DeduplicationResult)) == (
        "records",
        "conflicts",
    )
    assert tuple(field.name for field in fields(OutcomeRecord)) == (
        "outcome_id",
        "signal_id",
        "event_key",
        "epoch_id",
        "strategy",
        "policy_id",
        "ticker",
        "direction",
        "holding_sessions",
        "entry_session",
        "exit_session",
        "entry_price",
        "exit_price",
        "raw_return",
        "signed_return",
        "status",
        "invalid_reason",
    )
    assert tuple(field.name for field in fields(StrategyHealthRecord)) == (
        "health_id",
        "epoch_id",
        "session",
        "policy_id",
        "strategy",
        "status",
        "signal_count",
        "evidence",
    )
    assert tuple(field.name for field in fields(PortfolioMetrics)) == (
        "cohort_id",
        "epoch_id",
        "metric_schema_version",
        "start_session",
        "end_session",
        "valuation_at",
        "benchmark_at",
        "valid_sessions",
        "total_return",
        "gross_return",
        "matched_benchmark_return",
        "matched_excess_return",
        "annualized_daily_net_sharpe",
        "sharpe_return_count",
        "annualized_matched_information_ratio",
        "information_ratio_return_count",
        "max_drawdown",
        "long_weight",
        "short_weight",
        "gross_weight",
        "net_weight",
        "cash_weight",
        "cumulative_costs",
        "unique_catalysts",
        "strategy_decisions",
        "fills",
        "closed_trades",
        "missing_mark_count",
        "stale_mark_count",
    )
    assert tuple(field.name for field in fields(PairedComparison)) == (
        "candidate_epoch_id",
        "baseline_epoch_id",
        "common_sessions",
        "candidate_return",
        "baseline_return",
        "excess_return",
    )


def _expected_id(kind: str, *parts: object) -> str:
    payload = json.dumps(
        [kind, *parts], sort_keys=True, separators=(",", ":"), default=str
    )
    return f"{kind}_{hashlib.sha256(payload.encode()).hexdigest()[:24]}"


def test_event_key_normalizes_only_planned_identity_fields() -> None:
    event_at = datetime(2026, 7, 29, 12, tzinfo=UTC)
    actual = event_key(
        source="  EDGAR ",
        source_event_id=" accession-1 ",
        ticker=" msft ",
        event_at=event_at,
        evidence_hash="hash-1",
    )
    expected = _expected_id(
        "event",
        "edgar",
        "accession-1",
        "MSFT",
        event_at.isoformat(),
        "hash-1",
    )
    assert actual == expected
    assert actual.startswith("event_")


@pytest.mark.parametrize("source_event_id", ["", "   "])
def test_event_key_rejects_empty_normalized_source_event_id(
    source_event_id: str,
) -> None:
    with pytest.raises(ValueError, match="source_event_id is required"):
        event_key("edgar", source_event_id, "MSFT", None, "hash-1")


def test_signal_id_changes_for_every_semantic_identity_part() -> None:
    parts = ["epoch-1", "litigation", "30d", "long", "event-1"]
    baseline = signal_id(*parts)
    assert baseline == _expected_id("signal", *parts)
    assert baseline.startswith("signal_")
    replacements = ["epoch-2", "filing", "3m", "short", "event-2"]
    for index, replacement in enumerate(replacements):
        changed = parts.copy()
        changed[index] = replacement
        assert signal_id(*changed) != baseline


def test_execution_id_changes_for_every_semantic_identity_part() -> None:
    parts = ["cohort-1", "signal-1", "fill-1"]
    baseline = execution_id(*parts)
    assert baseline == _expected_id("execution", *parts)
    assert baseline.startswith("execution_")
    for index, replacement in enumerate(["cohort-2", "signal-2", "fill-2"]):
        changed = parts.copy()
        changed[index] = replacement
        assert execution_id(*changed) != baseline


def _record_for(
    *, event: str, policy: str, direction: str, signal: str
) -> SignalMetricRecord:
    return replace(
        _record(direction),
        event_key=event,
        policy_id=policy,
        signal_id=signal,
    )


def test_deduplication_outputs_have_deterministic_ordering() -> None:
    accepted_a = _record_for(
        event="event-z", policy="30d", direction="long", signal="signal-z"
    )
    accepted_b = _record_for(
        event="event-a", policy="30d", direction="long", signal="signal-a"
    )
    conflict_a = [
        _record_for(
            event="event-c", policy="30d", direction=direction, signal=f"c-{direction}"
        )
        for direction in ("short", "long")
    ]
    conflict_b = [
        _record_for(
            event="event-b", policy="3m", direction=direction, signal=f"b-{direction}"
        )
        for direction in ("short", "long")
    ]
    records = [accepted_a, *conflict_a, accepted_b, *conflict_b]

    forward = deduplicate_signals(records)
    reverse = deduplicate_signals(reversed(records))

    assert forward == reverse
    assert tuple(row.signal_id for row in forward.records) == ("signal-a", "signal-z")
    assert tuple(
        (row.event_key, row.policy_id, row.directions) for row in forward.conflicts
    ) == (
        ("event-b", "3m", ("long", "short")),
        ("event-c", "30d", ("long", "short")),
    )


def test_reused_signal_id_with_unequal_payload_fails_closed() -> None:
    original = _record("long")
    unequal = replace(original, ticker="MSFT")
    with pytest.raises(ValueError, match="signal_id .* unequal payloads"):
        deduplicate_signals([original, unequal])
