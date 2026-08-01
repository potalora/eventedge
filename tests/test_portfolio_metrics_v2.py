from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from tradingagents.strategies.execution.models import (
    AccountSnapshot,
    BenchmarkObservation,
    Fill,
    SignalRecord,
)
from tradingagents.strategies.metrics.portfolio import (
    DatedReturn,
    annualized_sharpe,
    daily_net_returns,
    drawdowns,
    matched_benchmark_returns,
    matched_return,
    paired_comparison,
    portfolio_metrics,
    reconcile_costs,
    total_return,
)


UTC_NOON = datetime(2026, 8, 3, 12, tzinfo=UTC)
COHORT = "cohort-a"
EPOCH = "epoch-a"


def _snapshot(
    session: date,
    equity: str,
    *,
    cohort_id: str = COHORT,
    epoch_id: str = EPOCH,
    valid: bool = True,
    invalid_reason: str = "",
    gross_equity: str | None = None,
    costs: str = "0",
    gross_exposure: str = "50",
    net_exposure: str = "50",
    valuation_at: datetime | None = None,
) -> AccountSnapshot:
    value = Decimal(equity)
    total_cost = Decimal(costs)
    return AccountSnapshot(
        snapshot_id=f"{cohort_id}-{epoch_id}-{session.isoformat()}",
        cohort_id=cohort_id,
        epoch_id=epoch_id,
        session=session,
        valuation_at=valuation_at
        or UTC_NOON + timedelta(days=(session - date(2026, 8, 3)).days),
        cash=value - Decimal(gross_exposure),
        long_market_value=Decimal(gross_exposure),
        short_liability=Decimal("0"),
        gross_exposure=Decimal(gross_exposure),
        net_exposure=Decimal(net_exposure),
        margin_used=Decimal("0"),
        buying_power=value,
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        gross_equity=Decimal(gross_equity) if gross_equity else value + total_cost,
        slippage_cost=total_cost,
        commission_cost=Decimal("0"),
        other_fees=Decimal("0"),
        borrow_cost=Decimal("0"),
        financing_cost=Decimal("0"),
        dividend_cash=Decimal("0"),
        net_equity=value,
        high_water_mark=value,
        valid=valid,
        invalid_reason=invalid_reason,
    )


def _observation(
    session: date,
    symbol: str,
    close: str,
    *,
    cohort_id: str = COHORT,
    epoch_id: str = EPOCH,
    valid: bool = True,
    observed_at: datetime | None = None,
) -> BenchmarkObservation:
    return BenchmarkObservation(
        observation_id=f"{cohort_id}-{epoch_id}-{symbol}-{session.isoformat()}",
        cohort_id=cohort_id,
        epoch_id=epoch_id,
        session=session,
        symbol=symbol,
        close=Decimal(close),
        return_basis="total_return_adjusted",
        source="fixture",
        observed_at=observed_at or UTC_NOON,
        valid=valid,
        invalid_reason="" if valid else "missing_benchmark",
    )


def _signal(signal_id: str, event_key: str, *, epoch_id: str = EPOCH) -> SignalRecord:
    return SignalRecord(
        signal_id=signal_id,
        epoch_id=epoch_id,
        policy_id="policy",
        event_key=event_key,
        strategy="fixture",
        ticker="AAPL",
        direction="long",
        event_at=None,
        observed_at=UTC_NOON,
        reference_session=date(2026, 8, 3),
        reference_close=Decimal("100"),
        decision_at=UTC_NOON,
        evidence_hash="hash",
    )


def _fill(fill_id: str, intent_id: str, side: str) -> Fill:
    return Fill(
        fill_id,
        intent_id,
        side,
        date(2026, 8, 3),
        UTC_NOON,
        UTC_NOON,
        Decimal("100"),
        Decimal("100"),
        1,
        Decimal("0"),
        Decimal("0"),
        Decimal("0"),
    )


def _benchmarks(*sessions: tuple[date, str, str]) -> list[BenchmarkObservation]:
    return [
        observation
        for session, spy, bil in sessions
        for observation in (
            _observation(session, "SPY", spy),
            _observation(session, "BIL", bil),
        )
    ]


def test_total_return_uses_positive_finite_net_equity_endpoints() -> None:
    assert total_return([Decimal("100"), Decimal("110")]) == pytest.approx(0.1)
    with pytest.raises(ValueError, match="positive"):
        total_return([Decimal("0"), Decimal("110")])
    with pytest.raises(ValueError, match="finite"):
        total_return([Decimal("100"), Decimal("NaN")])


def test_known_drawdown_sequence_rejects_invalid_equity() -> None:
    assert drawdowns([100.0, 120.0, 90.0, 99.0]) == (0.0, 0.0, -0.25, -0.175)
    with pytest.raises(ValueError, match="positive finite"):
        drawdowns([100.0, float("inf")])


def test_sharpe_requires_thirty_actual_returns_and_zero_variance_is_none() -> None:
    assert annualized_sharpe([0.01] * 29) is None
    assert annualized_sharpe([0.01] * 30) is None
    value = annualized_sharpe([0.01, -0.01] * 15)
    assert value == pytest.approx(0.0, abs=1e-12)
    assert annualized_sharpe([0.01, -0.01] * 15, valid_sessions=31) == pytest.approx(
        0.0, abs=1e-12
    )
    with pytest.raises(ValueError, match="valid_sessions"):
        annualized_sharpe([0.01, -0.01] * 15, valid_sessions=30)


def test_daily_returns_require_same_cohort_epoch_and_consecutive_xnys_sessions() -> (
    None
):
    snapshots = [
        _snapshot(date(2026, 8, 3), "100"),
        _snapshot(date(2026, 8, 4), "110"),
        _snapshot(date(2026, 8, 5), "120", valid=False, invalid_reason="missing_mark"),
        _snapshot(date(2026, 8, 6), "130"),
        _snapshot(date(2026, 8, 7), "140", epoch_id="epoch-b"),
    ]
    returns = daily_net_returns(snapshots)
    assert returns == (DatedReturn(date(2026, 8, 4), pytest.approx(0.1)),)
    with pytest.raises(ValueError, match="one cohort"):
        daily_net_returns(
            [
                _snapshot(date(2026, 8, 3), "100"),
                _snapshot(date(2026, 8, 4), "110", cohort_id="cohort-b"),
            ]
        )


def test_daily_returns_do_not_bridge_missing_xnys_session_or_duplicate_session() -> (
    None
):
    assert (
        daily_net_returns(
            [
                _snapshot(date(2026, 8, 3), "100"),
                _snapshot(date(2026, 8, 5), "121"),
            ]
        )
        == ()
    )
    with pytest.raises(ValueError, match="duplicate snapshot"):
        daily_net_returns(
            [_snapshot(date(2026, 8, 3), "100"), _snapshot(date(2026, 8, 3), "101")]
        )


def test_matched_benchmark_uses_previous_session_exposure_and_target_scope() -> None:
    snapshots = [
        replace(
            _snapshot(
                date(2026, 8, 3),
                "100",
                gross_exposure="80",
                net_exposure="60",
            ),
            cash=Decimal("40"),
            long_market_value=Decimal("70"),
            short_liability=Decimal("10"),
        ),
        _snapshot(date(2026, 8, 4), "110"),
    ]
    observations = _benchmarks(
        (date(2026, 8, 3), "100", "100"), (date(2026, 8, 4), "102", "100.1")
    )
    observations.extend(
        [
            _observation(date(2026, 8, 4), "SPY", "999", cohort_id="foreign"),
            _observation(date(2026, 8, 4), "BIL", "999", epoch_id="foreign-epoch"),
        ]
    )
    assert matched_return(0.8, 0.6, 0.02, 0.001) == pytest.approx(0.0122)
    assert matched_benchmark_returns(snapshots, observations) == (
        DatedReturn(date(2026, 8, 4), pytest.approx(0.0122)),
    )


def test_benchmark_rejects_duplicate_invalid_prices_and_wrong_timestamp_scope() -> None:
    snapshots = [_snapshot(date(2026, 8, 3), "100"), _snapshot(date(2026, 8, 4), "101")]
    observations = _benchmarks(
        (date(2026, 8, 3), "100", "100"), (date(2026, 8, 4), "101", "100.1")
    )
    observations.append(_observation(date(2026, 8, 4), "SPY", "101"))
    with pytest.raises(ValueError, match="duplicate benchmark"):
        matched_benchmark_returns(snapshots, observations)
    bad = _benchmarks(
        (date(2026, 8, 3), "100", "100"), (date(2026, 8, 4), "0", "100.1")
    )
    with pytest.raises(ValueError, match="positive finite"):
        matched_benchmark_returns(snapshots, bad)
    invalid_duplicate = _benchmarks(
        (date(2026, 8, 3), "100", "100"), (date(2026, 8, 4), "101", "100.1")
    )
    invalid_duplicate.append(_observation(date(2026, 8, 4), "SPY", "101", valid=False))
    with pytest.raises(ValueError, match="duplicate benchmark"):
        matched_benchmark_returns(snapshots, invalid_duplicate)
    naive = _benchmarks(
        (date(2026, 8, 3), "100", "100"), (date(2026, 8, 4), "101", "100.1")
    )
    naive[0] = replace(naive[0], observed_at=datetime(2026, 8, 3, 12))
    with pytest.raises(ValueError, match="timezone-aware"):
        matched_benchmark_returns(snapshots, naive)


def test_benchmark_generator_has_same_result_as_list() -> None:
    snapshots = [
        _snapshot(date(2026, 8, 3), "100"),
        _snapshot(date(2026, 8, 4), "101"),
        _snapshot(date(2026, 8, 5), "102"),
    ]
    observations = _benchmarks(
        (date(2026, 8, 3), "100", "100"),
        (date(2026, 8, 4), "101", "100.1"),
        (date(2026, 8, 5), "102", "100.2"),
    )
    assert matched_benchmark_returns(
        snapshots, iter(observations)
    ) == matched_benchmark_returns(snapshots, observations)


def test_multi_epoch_benchmark_observations_are_indexed_once() -> None:
    class CountingObservation:
        def __init__(self, row: BenchmarkObservation) -> None:
            self.row = row
            self.scope_visits = 0

        @property
        def cohort_id(self) -> str:
            self.scope_visits += 1
            return self.row.cohort_id

        def __getattr__(self, name: str) -> object:
            return getattr(self.row, name)

    epoch_sessions = (
        ("epoch-0", date(2026, 8, 3), date(2026, 8, 4)),
        ("epoch-1", date(2026, 8, 5), date(2026, 8, 6)),
        ("epoch-2", date(2026, 8, 7), date(2026, 8, 10)),
    )
    snapshots = [
        _snapshot(session, equity, epoch_id=epoch_id)
        for epoch_id, previous, current in epoch_sessions
        for session, equity in ((previous, "100"), (current, "101"))
    ]
    observations = [
        CountingObservation(_observation(session, symbol, close, epoch_id=epoch_id))
        for epoch_id, previous, current in epoch_sessions
        for session, spy, bil in (
            (previous, "100", "100"),
            (current, "101", "100.1"),
        )
        for symbol, close in (("SPY", spy), ("BIL", bil))
    ]

    result = matched_benchmark_returns(snapshots, observations)  # type: ignore[arg-type]

    assert tuple(row.session for row in result) == (
        date(2026, 8, 4),
        date(2026, 8, 6),
        date(2026, 8, 10),
    )
    assert sum(row.scope_visits for row in observations) == len(observations)


def test_reconcile_costs_rejects_mismatch() -> None:
    with pytest.raises(ValueError, match="does not reconcile"):
        reconcile_costs(
            _snapshot(date(2026, 8, 3), "100", gross_equity="101", costs="2")
        )
    with pytest.raises(ValueError, match="positive finite"):
        reconcile_costs(_snapshot(date(2026, 8, 3), "100", gross_equity="NaN"))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cash", Decimal("NaN")),
        ("cash", Decimal("Infinity")),
        ("long_market_value", Decimal("NaN")),
        ("long_market_value", Decimal("Infinity")),
        ("long_market_value", Decimal("-1")),
        ("short_liability", Decimal("NaN")),
        ("short_liability", Decimal("Infinity")),
        ("short_liability", Decimal("-1")),
        ("gross_exposure", Decimal("NaN")),
        ("gross_exposure", Decimal("Infinity")),
        ("gross_exposure", Decimal("-1")),
        ("net_exposure", Decimal("NaN")),
        ("net_exposure", Decimal("Infinity")),
    ],
)
def test_portfolio_metrics_rejects_nonfinite_or_negative_historical_account_fields(
    field: str, value: Decimal
) -> None:
    snapshots = [
        _snapshot(date(2026, 8, 3), "100"),
        _snapshot(date(2026, 8, 4), "101"),
        _snapshot(date(2026, 8, 5), "102"),
    ]
    snapshots[1] = replace(snapshots[1], **{field: value})
    observations = _benchmarks(
        (date(2026, 8, 3), "100", "100"),
        (date(2026, 8, 4), "101", "100.1"),
        (date(2026, 8, 5), "102", "100.2"),
    )

    with pytest.raises(ValueError):
        portfolio_metrics(
            cohort_id=COHORT,
            epoch_id=EPOCH,
            snapshots=snapshots,
            benchmark_observations=observations,
            signals=[],
            fills=[],
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"cash": Decimal("52")}, "net equity does not reconcile"),
        ({"gross_exposure": Decimal("49")}, "gross exposure does not reconcile"),
        ({"net_exposure": Decimal("49")}, "net exposure does not reconcile"),
    ],
)
def test_every_metric_path_rejects_exact_decimal_identity_corruption(
    changes: dict[str, Decimal], message: str
) -> None:
    snapshots = [
        _snapshot(date(2026, 8, 3), "100"),
        replace(_snapshot(date(2026, 8, 4), "101"), **changes),
        _snapshot(date(2026, 8, 5), "102"),
    ]
    observations = _benchmarks(
        (date(2026, 8, 3), "100", "100"),
        (date(2026, 8, 4), "101", "100.1"),
        (date(2026, 8, 5), "102", "100.2"),
    )

    with pytest.raises(ValueError, match=message):
        daily_net_returns(snapshots)
    with pytest.raises(ValueError, match=message):
        matched_benchmark_returns(snapshots, observations)
    with pytest.raises(ValueError, match=message):
        portfolio_metrics(
            cohort_id=COHORT,
            epoch_id=EPOCH,
            snapshots=snapshots,
            benchmark_observations=observations,
            signals=[],
            fills=[],
        )


def test_matched_benchmark_rejects_latest_snapshot_weight_corruption() -> None:
    snapshots = [
        _snapshot(date(2026, 8, 3), "100"),
        replace(_snapshot(date(2026, 8, 4), "101"), net_exposure=Decimal("99")),
    ]
    observations = _benchmarks(
        (date(2026, 8, 3), "100", "100"),
        (date(2026, 8, 4), "101", "100.1"),
    )

    with pytest.raises(ValueError, match="net exposure does not reconcile"):
        matched_benchmark_returns(snapshots, observations)


def test_portfolio_metrics_fails_closed_on_invalid_or_missing_session_gap() -> None:
    snapshots = [
        _snapshot(date(2026, 8, 3), "100"),
        _snapshot(date(2026, 8, 4), "110"),
        _snapshot(date(2026, 8, 5), "105", valid=False, invalid_reason="missing_mark"),
        _snapshot(date(2026, 8, 6), "120"),
    ]
    with pytest.raises(ValueError, match="contiguous valid"):
        portfolio_metrics(
            cohort_id=COHORT,
            epoch_id=EPOCH,
            snapshots=snapshots,
            benchmark_observations=[],
            signals=[],
            fills=[],
        )
    with pytest.raises(ValueError, match="contiguous valid"):
        portfolio_metrics(
            cohort_id=COHORT,
            epoch_id=EPOCH,
            snapshots=[
                _snapshot(date(2026, 8, 3), "100"),
                _snapshot(date(2026, 8, 5), "110"),
            ],
            benchmark_observations=[],
            signals=[],
            fills=[],
        )


def test_portfolio_metrics_scopes_identity_and_counts_unique_records() -> None:
    snapshots = [_snapshot(date(2026, 8, 3), "100"), _snapshot(date(2026, 8, 4), "110")]
    observations = _benchmarks(
        (date(2026, 8, 3), "100", "100"), (date(2026, 8, 4), "102", "100.1")
    )
    result = portfolio_metrics(
        cohort_id=COHORT,
        epoch_id=EPOCH,
        snapshots=[*snapshots, _snapshot(date(2026, 8, 4), "999", cohort_id="foreign")],
        benchmark_observations=observations,
        signals=[
            _signal("one", "event"),
            _signal("one", "event"),
            _signal("two", "event-2"),
            _signal("other", "other", epoch_id="other"),
        ],
        fills=[
            _fill("fill", "intent", "buy"),
            _fill("fill", "intent", "buy"),
            _fill("close", "intent", "sell"),
        ],
    )
    assert result.total_return == pytest.approx(0.1)
    assert result.max_drawdown == 0.0
    assert result.unique_catalysts == 2
    assert result.strategy_decisions == 2
    assert result.fills == 2
    assert result.closed_trades == 1
    assert result.benchmark_at == UTC_NOON
    with pytest.raises(ValueError, match="conflicting signal_id"):
        portfolio_metrics(
            cohort_id=COHORT,
            epoch_id=EPOCH,
            snapshots=snapshots,
            benchmark_observations=observations,
            signals=[
                _signal("one", "event"),
                replace(_signal("one", "event"), event_key="other"),
            ],
            fills=[],
        )
    with pytest.raises(ValueError, match="conflicting fill_id"):
        portfolio_metrics(
            cohort_id=COHORT,
            epoch_id=EPOCH,
            snapshots=snapshots,
            benchmark_observations=observations,
            signals=[],
            fills=[
                _fill("fill", "intent", "buy"),
                replace(_fill("fill", "intent", "buy"), side="sell"),
            ],
        )


def test_portfolio_metrics_materializes_fills_generator_once() -> None:
    snapshots = [_snapshot(date(2026, 8, 3), "100"), _snapshot(date(2026, 8, 4), "110")]
    observations = _benchmarks(
        (date(2026, 8, 3), "100", "100"), (date(2026, 8, 4), "102", "100.1")
    )
    fills = [
        _fill("open", "open-intent", "buy"),
        _fill("close", "close-intent", "sell"),
    ]
    list_metrics = portfolio_metrics(
        cohort_id=COHORT,
        epoch_id=EPOCH,
        snapshots=snapshots,
        benchmark_observations=observations,
        signals=[],
        fills=fills,
    )
    generator_metrics = portfolio_metrics(
        cohort_id=COHORT,
        epoch_id=EPOCH,
        snapshots=snapshots,
        benchmark_observations=observations,
        signals=[],
        fills=iter(fills),
    )
    assert (
        (generator_metrics.fills, generator_metrics.closed_trades)
        == (list_metrics.fills, list_metrics.closed_trades)
        == (2, 1)
    )


def test_portfolio_metrics_accepts_benchmark_generator_and_rejects_naive_valuation() -> (
    None
):
    snapshots = [_snapshot(date(2026, 8, 3), "100"), _snapshot(date(2026, 8, 4), "110")]
    observations = _benchmarks(
        (date(2026, 8, 3), "100", "100"), (date(2026, 8, 4), "102", "100.1")
    )
    assert portfolio_metrics(
        cohort_id=COHORT,
        epoch_id=EPOCH,
        snapshots=snapshots,
        benchmark_observations=iter(observations),
        signals=[],
        fills=[],
    ).total_return == pytest.approx(0.1)
    with pytest.raises(ValueError, match="timezone-aware"):
        portfolio_metrics(
            cohort_id=COHORT,
            epoch_id=EPOCH,
            snapshots=[
                replace(snapshots[0], valuation_at=datetime(2026, 8, 3, 12)),
                snapshots[1],
            ],
            benchmark_observations=observations,
            signals=[],
            fills=[],
        )


def test_paired_comparison_uses_only_common_sessions_and_rejects_duplicates() -> None:
    result = paired_comparison(
        candidate_epoch_id="candidate",
        baseline_epoch_id="baseline",
        candidate_returns=[
            DatedReturn(date(2026, 8, 4), 0.1),
            DatedReturn(date(2026, 8, 5), 0.1),
            DatedReturn(date(2026, 8, 6), 0.9),
        ],
        baseline_returns=[
            DatedReturn(date(2026, 8, 4), 0.05),
            DatedReturn(date(2026, 8, 5), 0.05),
        ],
    )
    assert result.common_sessions == (date(2026, 8, 4), date(2026, 8, 5))
    assert result.candidate_return == pytest.approx(0.21)
    with pytest.raises(ValueError, match="duplicate return"):
        paired_comparison(
            candidate_epoch_id="candidate",
            baseline_epoch_id="baseline",
            candidate_returns=[
                DatedReturn(date(2026, 8, 4), 0.1),
                DatedReturn(date(2026, 8, 4), 0.2),
            ],
            baseline_returns=[],
        )
    with pytest.raises(ValueError, match="contiguous common"):
        paired_comparison(
            candidate_epoch_id="candidate",
            baseline_epoch_id="baseline",
            candidate_returns=[
                DatedReturn(date(2026, 8, 4), 0.1),
                DatedReturn(date(2026, 8, 5), 0.1),
                DatedReturn(date(2026, 8, 6), 0.1),
            ],
            baseline_returns=[
                DatedReturn(date(2026, 8, 4), 0.1),
                DatedReturn(date(2026, 8, 6), 0.1),
            ],
        )
    with pytest.raises(ValueError, match="at least one common"):
        paired_comparison(
            candidate_epoch_id="candidate",
            baseline_epoch_id="baseline",
            candidate_returns=[DatedReturn(date(2026, 8, 4), 0.1)],
            baseline_returns=[],
        )
    with pytest.raises(ValueError, match="XNYS"):
        paired_comparison(
            candidate_epoch_id="candidate",
            baseline_epoch_id="baseline",
            candidate_returns=[DatedReturn(date(2026, 8, 8), 0.1)],
            baseline_returns=[DatedReturn(date(2026, 8, 8), 0.1)],
        )
    with pytest.raises(ValueError, match="return"):
        paired_comparison(
            candidate_epoch_id="candidate",
            baseline_epoch_id="baseline",
            candidate_returns=[DatedReturn(date(2026, 8, 4), float("nan"))],
            baseline_returns=[DatedReturn(date(2026, 8, 4), 0.1)],
        )
    with pytest.raises(ValueError, match="greater than -100%"):
        paired_comparison(
            candidate_epoch_id="candidate",
            baseline_epoch_id="baseline",
            candidate_returns=[DatedReturn(date(2026, 8, 4), -1.0)],
            baseline_returns=[DatedReturn(date(2026, 8, 4), 0.1)],
        )
