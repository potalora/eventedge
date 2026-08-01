"""Fail-closed portfolio metrics over one contiguous XNYS metric window."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import math
import statistics
from typing import Iterable, Sequence

from tradingagents.strategies.execution.models import (
    AccountSnapshot,
    BenchmarkObservation,
    Fill,
    SignalRecord,
)

from .calendar import XNYSCalendar
from .models import METRIC_SCHEMA_VERSION, PairedComparison, PortfolioMetrics


@dataclass(frozen=True)
class DatedReturn:
    session: date
    value: float


def _finite(value: Decimal | float, *, name: str, positive: bool = False) -> float:
    numeric = float(value)
    if not math.isfinite(numeric) or (positive and numeric <= 0):
        qualifier = "positive finite" if positive else "finite"
        raise ValueError(f"{name} must be {qualifier}")
    return numeric


def _return(current: Decimal | float, previous: Decimal | float, *, name: str) -> float:
    return (
        _finite(current, name=name, positive=True)
        / _finite(previous, name=name, positive=True)
        - 1.0
    )


def total_return(values: Sequence[Decimal | float]) -> float:
    if len(values) < 2:
        raise ValueError("two positive-endpoint equity values are required")
    return _return(values[-1], values[0], name="equity endpoint")


def drawdowns(values: Sequence[Decimal | float]) -> tuple[float, ...]:
    peak = Decimal("0")
    result: list[float] = []
    for value in values:
        _finite(value, name="equity", positive=True)
        equity = value if isinstance(value, Decimal) else Decimal(str(value))
        peak = max(peak, equity)
        drawdown = float(equity / peak - Decimal("1"))
        if not math.isfinite(drawdown) or drawdown > 0.0 or drawdown < -1.0:
            raise ValueError("nonsensical drawdown")
        result.append(drawdown)
    return tuple(result)


def annualized_sharpe(
    excess_returns: Sequence[float], valid_sessions: int | None = None
) -> float | None:
    """Annualized daily Sharpe, based solely on actual common return rows."""
    values = [_finite(value, name="return") for value in excess_returns]
    if valid_sessions is not None and (
        not isinstance(valid_sessions, int)
        or isinstance(valid_sessions, bool)
        or valid_sessions <= 0
        or valid_sessions != len(values) + 1
    ):
        raise ValueError("valid_sessions must equal actual return count plus one")
    if len(values) < 30:
        return None
    deviation = statistics.stdev(values)
    if deviation == 0.0:
        return None
    return statistics.mean(values) / deviation * math.sqrt(252)


def matched_return(
    gross_weight: float,
    net_weight: float,
    spy_return: float,
    cash_return: float,
) -> float:
    gross = _finite(gross_weight, name="gross weight")
    net = _finite(net_weight, name="net weight")
    if gross < 0.0:
        raise ValueError("gross weight must be nonnegative")
    return net * _finite(spy_return, name="SPY return") + max(
        0.0, 1.0 - gross
    ) * _finite(cash_return, name="cash return")


def _ordered_snapshots(snapshots: Iterable[AccountSnapshot]) -> list[AccountSnapshot]:
    rows = sorted(snapshots, key=lambda row: (row.session, row.snapshot_id))
    cohorts = {row.cohort_id for row in rows}
    if len(cohorts) > 1:
        raise ValueError("daily returns require one cohort")
    seen: set[tuple[str, date]] = set()
    for row in rows:
        key = (row.epoch_id, row.session)
        if key in seen:
            raise ValueError(f"duplicate snapshot for {row.epoch_id}/{row.session}")
        seen.add(key)
    return rows


def _valid_snapshot(row: AccountSnapshot) -> None:
    for value, name in (
        (row.cash, "cash"),
        (row.long_market_value, "long market value"),
        (row.short_liability, "short liability"),
        (row.gross_exposure, "gross exposure"),
        (row.net_exposure, "net exposure"),
    ):
        if not value.is_finite():
            raise ValueError(f"{name} must be finite")
    for value, name in (
        (row.long_market_value, "long market value"),
        (row.short_liability, "short liability"),
        (row.gross_exposure, "gross exposure"),
    ):
        if value < 0:
            raise ValueError(f"{name} must be nonnegative")
    if row.net_equity != row.cash + row.long_market_value - row.short_liability:
        raise ValueError("net equity does not reconcile")
    if row.gross_exposure != row.long_market_value + row.short_liability:
        raise ValueError("gross exposure does not reconcile")
    if row.net_exposure != row.long_market_value - row.short_liability:
        raise ValueError("net exposure does not reconcile")
    _finite(row.net_equity, name="net equity", positive=True)
    _finite(row.gross_equity, name="gross equity", positive=True)
    _finite(row.gross_exposure, name="gross exposure")
    _finite(row.net_exposure, name="net exposure")


def daily_net_returns(
    snapshots: Iterable[AccountSnapshot], calendar: XNYSCalendar | None = None
) -> tuple[DatedReturn, ...]:
    session_calendar = calendar or XNYSCalendar()
    rows = _ordered_snapshots(snapshots)
    output: list[DatedReturn] = []
    for previous, current in zip(rows, rows[1:]):
        if not previous.valid or not current.valid:
            continue
        _valid_snapshot(previous)
        _valid_snapshot(current)
        if not session_calendar.is_session(
            previous.session
        ) or not session_calendar.is_session(current.session):
            raise ValueError("valid snapshots must use XNYS sessions")
        if previous.epoch_id != current.epoch_id:
            continue
        if session_calendar.next_session(previous.session) != current.session:
            continue
        output.append(
            DatedReturn(
                current.session,
                _return(current.net_equity, previous.net_equity, name="net equity"),
            )
        )
    return tuple(output)


BenchmarkMap = dict[tuple[str, date], BenchmarkObservation]
BenchmarkIndex = dict[str, BenchmarkMap]


def _benchmark_index(
    observations: Iterable[BenchmarkObservation],
    *,
    cohort_id: str,
    epoch_ids: set[str],
    calendar: XNYSCalendar,
) -> BenchmarkIndex:
    output: BenchmarkIndex = {epoch_id: {} for epoch_id in epoch_ids}
    for row in observations:
        if row.cohort_id != cohort_id or row.epoch_id not in epoch_ids:
            continue
        if row.symbol not in {"SPY", "BIL"}:
            continue
        if not calendar.is_session(row.session):
            raise ValueError("benchmark observations must use XNYS sessions")
        if row.observed_at.tzinfo is None or row.observed_at.utcoffset() is None:
            raise ValueError("benchmark observation timestamp must be timezone-aware")
        key = (row.symbol, row.session)
        epoch_rows = output[row.epoch_id]
        if key in epoch_rows:
            raise ValueError(
                f"duplicate benchmark observation for {row.symbol}/{row.session}"
            )
        if not row.valid:
            epoch_rows[key] = row
            continue
        if row.return_basis != "total_return_adjusted":
            raise ValueError("benchmark must be total_return_adjusted")
        _finite(row.close, name="benchmark close", positive=True)
        epoch_rows[key] = row
    return output


def matched_benchmark_returns(
    snapshots: Iterable[AccountSnapshot],
    observations: Iterable[BenchmarkObservation],
    calendar: XNYSCalendar | None = None,
) -> tuple[DatedReturn, ...]:
    session_calendar = calendar or XNYSCalendar()
    rows = _ordered_snapshots(snapshots)
    if not rows:
        return ()
    cohort_id = rows[0].cohort_id
    benchmark_by_epoch = _benchmark_index(
        observations,
        cohort_id=cohort_id,
        epoch_ids={row.epoch_id for row in rows},
        calendar=session_calendar,
    )
    return _matched_benchmark_returns(rows, benchmark_by_epoch, session_calendar)


def _matched_benchmark_returns(
    rows: Sequence[AccountSnapshot],
    benchmark_by_epoch: BenchmarkIndex,
    calendar: XNYSCalendar,
) -> tuple[DatedReturn, ...]:
    output: list[DatedReturn] = []
    for previous, current in zip(rows, rows[1:]):
        if (
            not previous.valid
            or not current.valid
            or previous.epoch_id != current.epoch_id
        ):
            continue
        _valid_snapshot(previous)
        _valid_snapshot(current)
        if not calendar.is_session(previous.session) or not calendar.is_session(
            current.session
        ):
            raise ValueError("valid snapshots must use XNYS sessions")
        if calendar.next_session(previous.session) != current.session:
            continue
        benchmarks = benchmark_by_epoch[previous.epoch_id]
        required = (
            ("SPY", previous.session),
            ("SPY", current.session),
            ("BIL", previous.session),
            ("BIL", current.session),
        )
        if not all(key in benchmarks and benchmarks[key].valid for key in required):
            continue
        spy_return = _return(
            benchmarks[("SPY", current.session)].close,
            benchmarks[("SPY", previous.session)].close,
            name="SPY close",
        )
        cash_return = _return(
            benchmarks[("BIL", current.session)].close,
            benchmarks[("BIL", previous.session)].close,
            name="BIL close",
        )
        equity = _finite(previous.net_equity, name="net equity", positive=True)
        output.append(
            DatedReturn(
                current.session,
                matched_return(
                    _finite(previous.gross_exposure, name="gross exposure") / equity,
                    _finite(previous.net_exposure, name="net exposure") / equity,
                    spy_return,
                    cash_return,
                ),
            )
        )
    return tuple(output)


def _cash_proxy_returns(
    rows: BenchmarkMap,
    *,
    calendar: XNYSCalendar,
) -> tuple[DatedReturn, ...]:
    sessions = sorted(
        session
        for symbol, session in rows
        if symbol == "BIL" and rows[(symbol, session)].valid
    )
    return tuple(
        DatedReturn(
            current,
            _return(
                rows[("BIL", current)].close,
                rows[("BIL", previous)].close,
                name="BIL close",
            ),
        )
        for previous, current in zip(sessions, sessions[1:])
        if calendar.next_session(previous) == current
    )


def reconcile_costs(snapshot: AccountSnapshot) -> None:
    for value in (
        snapshot.slippage_cost,
        snapshot.commission_cost,
        snapshot.other_fees,
        snapshot.borrow_cost,
        snapshot.financing_cost,
    ):
        numeric = float(value)
        if not math.isfinite(numeric) or numeric < 0.0:
            raise ValueError("cost must be finite nonnegative")
    _finite(snapshot.gross_equity, name="gross equity", positive=True)
    _finite(snapshot.net_equity, name="net equity", positive=True)
    costs = sum(
        (
            snapshot.slippage_cost,
            snapshot.commission_cost,
            snapshot.other_fees,
            snapshot.borrow_cost,
            snapshot.financing_cost,
        ),
        Decimal("0"),
    )
    if snapshot.gross_equity - costs != snapshot.net_equity:
        raise ValueError(
            f"snapshot {snapshot.snapshot_id} does not reconcile gross to net"
        )


def _compound(values: Sequence[float]) -> float:
    result = 1.0
    for value in values:
        result *= 1.0 + _finite(value, name="return")
    return result - 1.0


def _require_full_window(
    rows: Sequence[AccountSnapshot], calendar: XNYSCalendar
) -> None:
    if len(rows) < 2:
        raise ValueError("at least two valid snapshots are required")
    for row in rows:
        if not row.valid:
            raise ValueError(
                "portfolio metrics require a contiguous valid snapshot window"
            )
        _valid_snapshot(row)
        reconcile_costs(row)
        if row.valuation_at.tzinfo is None or row.valuation_at.utcoffset() is None:
            raise ValueError("snapshot valuation timestamp must be timezone-aware")
        if not calendar.is_session(row.session):
            raise ValueError("valid snapshots must use XNYS sessions")
    if any(
        calendar.next_session(previous.session) != current.session
        for previous, current in zip(rows, rows[1:])
    ):
        raise ValueError("portfolio metrics require a contiguous valid snapshot window")


def validate_snapshot_window(
    *,
    cohort_id: str,
    epoch_id: str,
    snapshots: Iterable[AccountSnapshot],
    calendar: XNYSCalendar | None = None,
) -> tuple[AccountSnapshot, ...]:
    """Materialize and validate one exact Task-5 portfolio snapshot window."""
    session_calendar = calendar or XNYSCalendar()
    rows = _ordered_snapshots(
        [
            row
            for row in snapshots
            if row.cohort_id == cohort_id and row.epoch_id == epoch_id
        ]
    )
    _require_full_window(rows, session_calendar)
    return tuple(rows)


def equal_weighted_scenario_return(values: Iterable[float]) -> float:
    """Return the equal-weighted result for exactly four dependent books."""
    rows = tuple(_finite(value, name="scenario return") for value in values)
    if len(rows) != 4:
        raise ValueError("the scenario panel requires exactly four returns")
    return statistics.fmean(rows)


def portfolio_metrics(
    *,
    cohort_id: str,
    epoch_id: str,
    snapshots: Iterable[AccountSnapshot],
    benchmark_observations: Iterable[BenchmarkObservation],
    signals: Iterable[SignalRecord],
    fills: Iterable[Fill],
) -> PortfolioMetrics:
    calendar = XNYSCalendar()
    rows = validate_snapshot_window(
        cohort_id=cohort_id,
        epoch_id=epoch_id,
        snapshots=snapshots,
        calendar=calendar,
    )
    benchmark_index = _benchmark_index(
        benchmark_observations,
        cohort_id=cohort_id,
        epoch_ids={epoch_id},
        calendar=calendar,
    )
    scoped_benchmarks = benchmark_index[epoch_id]
    book = {row.session: row.value for row in daily_net_returns(rows, calendar)}
    benchmark = {
        row.session: row.value
        for row in _matched_benchmark_returns(rows, benchmark_index, calendar)
    }
    cash = {
        row.session: row.value
        for row in _cash_proxy_returns(scoped_benchmarks, calendar=calendar)
    }
    return_sessions = tuple(row.session for row in rows[1:])
    if set(benchmark) != set(return_sessions) or not set(return_sessions) <= set(cash):
        raise ValueError("benchmarks must cover the complete contiguous metric window")
    common = return_sessions
    matched_excess = [book[session] - benchmark[session] for session in common]
    risk_free_excess = [book[session] - cash[session] for session in common]
    latest = rows[-1]
    latest_times = [
        row.observed_at
        for (symbol, session), row in scoped_benchmarks.items()
        if session == latest.session and symbol in {"SPY", "BIL"} and row.valid
    ]
    if len(latest_times) != 2:
        raise ValueError("benchmarks must cover the complete contiguous metric window")
    equity = _finite(latest.net_equity, name="net equity", positive=True)
    unique_signals: dict[str, SignalRecord] = {}
    for row in signals:
        if row.epoch_id != epoch_id:
            continue
        existing = unique_signals.get(row.signal_id)
        if existing is not None and existing != row:
            raise ValueError(f"conflicting signal_id {row.signal_id}")
        unique_signals[row.signal_id] = row
    unique_fills: dict[str, Fill] = {}
    for row in tuple(fills):
        existing = unique_fills.get(row.fill_id)
        if existing is not None and existing != row:
            raise ValueError(f"conflicting fill_id {row.fill_id}")
        unique_fills[row.fill_id] = row
    costs = {
        "slippage": _finite(latest.slippage_cost, name="slippage cost"),
        "commission": _finite(latest.commission_cost, name="commission cost"),
        "other_fees": _finite(latest.other_fees, name="other fees"),
        "borrow": _finite(latest.borrow_cost, name="borrow cost"),
        "financing": _finite(latest.financing_cost, name="financing cost"),
    }
    return PortfolioMetrics(
        cohort_id=cohort_id,
        epoch_id=epoch_id,
        metric_schema_version=METRIC_SCHEMA_VERSION,
        start_session=rows[0].session,
        end_session=latest.session,
        valuation_at=latest.valuation_at,
        benchmark_at=max(latest_times),
        valid_sessions=len(rows),
        total_return=total_return([rows[0].net_equity, latest.net_equity]),
        gross_return=total_return([rows[0].gross_equity, latest.gross_equity]),
        matched_benchmark_return=_compound([benchmark[session] for session in common]),
        matched_excess_return=_compound([book[session] for session in common])
        - _compound([benchmark[session] for session in common]),
        annualized_daily_net_sharpe=annualized_sharpe(risk_free_excess),
        sharpe_return_count=len(risk_free_excess),
        annualized_matched_information_ratio=annualized_sharpe(matched_excess),
        information_ratio_return_count=len(matched_excess),
        max_drawdown=min(drawdowns([row.net_equity for row in rows])),
        long_weight=_finite(latest.long_market_value, name="long market value")
        / equity,
        short_weight=_finite(latest.short_liability, name="short liability") / equity,
        gross_weight=_finite(latest.gross_exposure, name="gross exposure") / equity,
        net_weight=_finite(latest.net_exposure, name="net exposure") / equity,
        cash_weight=_finite(latest.cash, name="cash") / equity,
        cumulative_costs=costs,
        unique_catalysts=len({row.event_key for row in unique_signals.values()}),
        strategy_decisions=len(unique_signals),
        fills=len(unique_fills),
        closed_trades=len(
            {
                row.intent_id
                for row in unique_fills.values()
                if row.side in {"sell", "cover"}
            }
        ),
        missing_mark_count=0,
        stale_mark_count=0,
    )


def paired_comparison(
    *,
    candidate_epoch_id: str,
    baseline_epoch_id: str,
    candidate_returns: Iterable[DatedReturn],
    baseline_returns: Iterable[DatedReturn],
) -> PairedComparison:
    def index(rows: Iterable[DatedReturn]) -> dict[date, float]:
        mapped: dict[date, float] = {}
        for row in rows:
            if row.session in mapped:
                raise ValueError(f"duplicate return for {row.session}")
            value = _finite(row.value, name="return")
            if value <= -1.0:
                raise ValueError("return must be greater than -100%")
            mapped[row.session] = value
        return mapped

    candidate, baseline = index(candidate_returns), index(baseline_returns)
    common = tuple(sorted(set(candidate) & set(baseline)))
    if not common:
        raise ValueError("paired comparison requires at least one common session")
    calendar = XNYSCalendar()
    if not all(calendar.is_session(session) for session in common):
        raise ValueError("paired comparison requires XNYS sessions")
    if any(
        calendar.next_session(previous) != current
        for previous, current in zip(common, common[1:])
    ):
        raise ValueError("paired comparison requires contiguous common sessions")
    candidate_total = _compound([candidate[session] for session in common])
    baseline_total = _compound([baseline[session] for session in common])
    return PairedComparison(
        candidate_epoch_id,
        baseline_epoch_id,
        common,
        candidate_total,
        baseline_total,
        candidate_total - baseline_total,
    )
