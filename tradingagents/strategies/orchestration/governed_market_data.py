"""Coordinate durable governed daily-bar recovery evidence."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from types import MappingProxyType

from tradingagents.strategies.data_sources.yfinance_source import normalize_tickers
from tradingagents.strategies.execution.models import MarketBar
from tradingagents.strategies.execution.price_source import (
    GovernedBarRecoveryEvidence,
    GovernedDailyBarResolution,
    PriceSource,
    validate_required_bars,
)
from tradingagents.strategies.metrics.models import (
    GOVERNED_BAR_RECOVERY_CONTRACT,
    GovernedBarRecoveryRecord,
)
from tradingagents.strategies.metrics.calendar import XNYSCalendar
from tradingagents.strategies.metrics.store import MetricStore


_MAX_TICKERS = 2_048
_MAX_COHORTS_PER_TICKER = 64
_MAX_TEXT = 4_096
_FAILURE_KINDS = frozenset({"missing", "incoherent", "invalid", "invalid_benchmark"})


def _immutable_mapping(values: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType(dict(values))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class GovernedRecoveryBinding:
    ticker: str
    recovery_id: str
    contract_version: str
    evidence_digest: str


@dataclass(frozen=True)
class GovernedInputResolution:
    bars: Mapping[str, MarketBar]
    recovery_bindings: Mapping[str, GovernedRecoveryBinding]
    recovery_summaries: tuple[Mapping[str, object], ...]
    failure_map: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "bars", MappingProxyType(dict(self.bars)))
        object.__setattr__(
            self,
            "recovery_bindings",
            MappingProxyType(dict(self.recovery_bindings)),
        )
        object.__setattr__(
            self,
            "recovery_summaries",
            tuple(_immutable_mapping(summary) for summary in self.recovery_summaries),
        )
        object.__setattr__(
            self, "failure_map", MappingProxyType(dict(self.failure_map))
        )


class GovernedMarketDataError(RuntimeError):
    """Bounded fail-closed coordinator error without raw provider details."""

    def __init__(self, failure_map: Mapping[str, str]) -> None:
        bounded = {
            str(ticker)[:_MAX_TEXT]: str(reason)[:_MAX_TEXT]
            for ticker, reason in sorted(failure_map.items())[:_MAX_TICKERS]
        }
        self.failure_map: Mapping[str, str] = MappingProxyType(bounded)
        super().__init__("governed market data resolution failed")


def _invalid_failure(ticker: str, session: date) -> str:
    return f"invalid {ticker}/{session.isoformat()}"


def _fail_closed(tickers: Collection[str], session: date) -> GovernedMarketDataError:
    return GovernedMarketDataError(
        {ticker: _invalid_failure(ticker, session) for ticker in sorted(tickers)}
    )


def _bounded_text(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_TEXT:
        raise ValueError(f"{label} is invalid")
    return value.strip()


def _canonical_inputs(
    tickers: Collection[str],
    cohort_ids_by_ticker: Mapping[str, Collection[str]],
) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]]]:
    if isinstance(tickers, (str, bytes)):
        raise ValueError("tickers are invalid")
    raw_tickers = tuple(tickers)
    if not raw_tickers or len(raw_tickers) > _MAX_TICKERS:
        raise ValueError("ticker count is invalid")
    canonical_tickers = tuple(
        _bounded_text(ticker, label="ticker").upper() for ticker in raw_tickers
    )
    if len(set(canonical_tickers)) != len(canonical_tickers):
        raise ValueError("ticker normalization collision")
    if len(set(normalize_tickers(list(canonical_tickers)))) != len(
        canonical_tickers
    ):
        raise ValueError("Yahoo ticker normalization collision")

    canonical_cohort_keys: dict[str, Collection[str]] = {}
    for raw_ticker, cohort_ids in cohort_ids_by_ticker.items():
        ticker = _bounded_text(raw_ticker, label="cohort ticker").upper()
        if ticker in canonical_cohort_keys:
            raise ValueError("cohort ticker normalization collision")
        canonical_cohort_keys[ticker] = cohort_ids
    if set(canonical_cohort_keys) != set(canonical_tickers):
        raise ValueError("cohort ticker scope is invalid")

    cohorts_by_ticker: dict[str, tuple[str, ...]] = {}
    for ticker in canonical_tickers:
        raw_cohorts = canonical_cohort_keys.get(ticker, ())
        if isinstance(raw_cohorts, (str, bytes)):
            raise ValueError("cohorts are invalid")
        cohorts = tuple(
            sorted(
                {
                    _bounded_text(cohort, label="cohort")
                    for cohort in raw_cohorts
                }
            )
        )
        if len(cohorts) > _MAX_COHORTS_PER_TICKER:
            raise ValueError("cohort count is invalid")
        if not cohorts:
            raise ValueError("cohort membership is empty")
        cohorts_by_ticker[ticker] = cohorts
    return tuple(sorted(canonical_tickers)), cohorts_by_ticker


def _decimal(value: object) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("governed recovery price is invalid") from error
    if not result.is_finite() or result <= 0:
        raise ValueError("governed recovery price is invalid")
    return result


def _timestamp(value: object) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("governed recovery timestamp is invalid") from error
    else:
        raise ValueError("governed recovery timestamp is invalid")
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("governed recovery timestamp is invalid")
    return result


def _bar_from_record(record: GovernedBarRecoveryRecord) -> MarketBar:
    validator = MetricStore.__new__(MetricStore)
    validator._calendar = XNYSCalendar()
    validator._validate_governed_bar_recovery(record)
    if not record.intraday_rows or record.final_validation_error is not None:
        raise ValueError("governed recovery is not accepted")
    close_at = XNYSCalendar().session_close(record.session)
    original_fetched_at = _timestamp(record.original_daily["fetched_at"])
    fetched_at = _timestamp(record.intraday_rows[0]["fetched_at"])
    if original_fetched_at < close_at or fetched_at < close_at:
        raise ValueError("governed recovery evidence predates session close")
    if any(
        _timestamp(row["fetched_at"]) != fetched_at
        or _timestamp(row["fetched_at"]) < close_at
        for row in record.intraday_rows
    ):
        raise ValueError("governed recovery timestamps are inconsistent")
    bar = MarketBar(
        ticker=record.ticker,
        session=record.session,
        open=_decimal(record.reconstructed_bar["open"]),
        high=_decimal(record.reconstructed_bar["high"]),
        low=_decimal(record.reconstructed_bar["low"]),
        close=_decimal(record.reconstructed_bar["close"]),
        source=str(record.reconstructed_bar["source"]),
        fetched_at=fetched_at,
        adjusted=False,
    )
    if (
        bar.source != "yfinance-60m-reconstruction"
        or bar.high < max(bar.open, bar.close)
        or bar.low > min(bar.open, bar.close)
        or bar.high < bar.low
    ):
        raise ValueError("governed recovery reconstructed bar is invalid")
    return bar


def _binding(record: GovernedBarRecoveryRecord) -> GovernedRecoveryBinding:
    return GovernedRecoveryBinding(
        ticker=record.ticker,
        recovery_id=record.recovery_id,
        contract_version=record.contract_version,
        evidence_digest=record.evidence_digest,
    )


def _summary(record: GovernedBarRecoveryRecord) -> Mapping[str, object]:
    return {
        "ticker": record.ticker,
        "session": record.session.isoformat(),
        "recovery_id": record.recovery_id,
        "contract_version": record.contract_version,
        "evidence_digest": record.evidence_digest,
        "affected_cohort_ids": record.affected_cohort_ids,
    }


def _record_from_recovery(
    *,
    recovery: GovernedBarRecoveryEvidence,
    epoch_id: str,
    affected_cohort_ids: tuple[str, ...],
) -> GovernedBarRecoveryRecord:
    attempt = recovery.daily_attempt
    reconstructed = recovery.reconstructed
    if (
        recovery.validation_error is not None
        or reconstructed is None
        or attempt.raw_ohlc is None
    ):
        raise ValueError("governed recovery is not accepted")
    return GovernedBarRecoveryRecord.create(
        contract_version=GOVERNED_BAR_RECOVERY_CONTRACT,
        epoch_id=epoch_id,
        session=recovery.session,
        ticker=recovery.ticker,
        original_daily={
            **attempt.raw_ohlc,
            "source": attempt.source,
            "fetched_at": attempt.fetched_at,
        },
        original_validation_error=attempt.validation_error,
        expected_starts=recovery.expected_starts,
        observed_starts=recovery.observed_starts,
        intraday_rows=tuple(
            {
                "start": row.start,
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "fetched_at": row.fetched_at,
            }
            for row in recovery.intraday_bars
        ),
        reconstructed_bar={
            "open": reconstructed.open,
            "high": reconstructed.high,
            "low": reconstructed.low,
            "close": reconstructed.close,
            "source": reconstructed.source,
        },
        final_validation_error=recovery.validation_error,
        affected_cohort_ids=affected_cohort_ids,
    )


def _is_normalized_failure(
    ticker: str, session: date, failure: object
) -> bool:
    if not isinstance(failure, str) or len(failure) > _MAX_TEXT:
        return False
    prefix, separator, scope = failure.partition(" ")
    return (
        bool(separator)
        and prefix in _FAILURE_KINDS
        and scope == f"{ticker}/{session.isoformat()}"
    )


def _validate_provider_resolution(
    resolution: GovernedDailyBarResolution,
    *,
    expected_tickers: tuple[str, ...],
    session: date,
    processed_at: datetime,
) -> None:
    expected = set(expected_tickers)
    bars = set(resolution.bars)
    attempts = set(resolution.attempts)
    recoveries = set(resolution.recoveries)
    failures = set(resolution.failure_map)
    close_at = XNYSCalendar().session_close(session)
    if (
        attempts != expected
        or not bars.issubset(expected)
        or not recoveries.issubset(expected)
        or not failures.issubset(expected)
        or bars & failures
        or bars | failures != expected
    ):
        raise ValueError("governed provider result scope is invalid")
    for ticker in expected_tickers:
        attempt = resolution.attempts[ticker]
        if attempt.ticker != ticker or attempt.session != session:
            raise ValueError("governed provider attempt scope is invalid")
        if ticker in failures and not _is_normalized_failure(
            ticker, session, resolution.failure_map[ticker]
        ):
            raise ValueError("governed provider failure is invalid")
        recovery = resolution.recoveries.get(ticker)
        bar = resolution.bars.get(ticker)
        if recovery is not None and (
            recovery.ticker != ticker
            or recovery.session != session
            or recovery.daily_attempt != attempt
        ):
            raise ValueError("governed provider recovery scope is invalid")
        accepted = recovery is not None and recovery.validation_error is None
        reconstructed = (
            bar is not None and bar.source == "yfinance-60m-reconstruction"
        )
        if accepted != reconstructed:
            raise ValueError("governed provider reconstruction is incoherent")
        if accepted and recovery is not None and recovery.reconstructed != bar:
            raise ValueError("governed provider reconstructed bar is unequal")
        if bar is not None and (
            bar.ticker != ticker or bar.session != session or bar.adjusted
        ):
            raise ValueError("governed provider bar scope is invalid")
        if bar is not None:
            validate_required_bars(
                {(ticker, session): bar}, {ticker}, session, processed_at
            )
            if bar.fetched_at < close_at:
                raise ValueError("governed provider bar predates session close")
        if recovery is None and bar is not None and (
            bar.source != "yfinance"
            or attempt.source != "yfinance"
            or attempt.validation_error is not None
            or attempt.fetched_at != bar.fetched_at
            or not _healthy_attempt_binds_bar(attempt.raw_ohlc, bar)
        ):
            raise ValueError("governed provider healthy bar source is invalid")


def _healthy_attempt_binds_bar(
    raw_ohlc: Mapping[str, Decimal] | None, bar: MarketBar
) -> bool:
    fields = ("open", "high", "low", "close")
    if raw_ohlc is None or set(raw_ohlc) != set(fields):
        return False
    values = tuple(raw_ohlc[field] for field in fields)
    return all(isinstance(value, Decimal) for value in values) and values == (
        bar.open,
        bar.high,
        bar.low,
        bar.close,
    )


def resolve_governed_bars(
    *,
    price_source: PriceSource,
    metric_store: MetricStore | None,
    epoch_id: str,
    session: date,
    tickers: Collection[str],
    cohort_ids_by_ticker: Mapping[str, Collection[str]],
    processed_at: datetime,
    persist: bool,
) -> GovernedInputResolution:
    """Resolve governed bars, reusing or persisting accepted recovery evidence."""
    input_failed = False
    try:
        canonical_epoch = _bounded_text(epoch_id, label="epoch")
        if not isinstance(session, date) or isinstance(session, datetime):
            raise ValueError("session is invalid")
        if processed_at.tzinfo is None or processed_at.utcoffset() is None:
            raise ValueError("processed_at is invalid")
        canonical_tickers, cohorts_by_ticker = _canonical_inputs(
            tickers, cohort_ids_by_ticker
        )
    except Exception:
        input_failed = True
    if input_failed:
        raise GovernedMarketDataError({})

    bars: dict[str, MarketBar] = {}
    records: dict[str, GovernedBarRecoveryRecord] = {}
    unresolved: list[str] = []
    for ticker in canonical_tickers:
        if metric_store is None:
            unresolved.append(ticker)
            continue
        load_failed = False
        try:
            record = metric_store.load_governed_bar_recovery(
                epoch_id=canonical_epoch, session=session, ticker=ticker
            )
        except Exception:
            load_failed = True
        if load_failed:
            raise _fail_closed((ticker,), session)
        if record is None:
            unresolved.append(ticker)
            continue
        if record.contract_version != GOVERNED_BAR_RECOVERY_CONTRACT:
            raise _fail_closed((ticker,), session)
        if (
            record.epoch_id != canonical_epoch
            or record.session != session
            or record.ticker != ticker
        ):
            raise _fail_closed((ticker,), session)
        if record.affected_cohort_ids != cohorts_by_ticker[ticker]:
            raise _fail_closed((ticker,), session)
        validation_failed = False
        try:
            bars[ticker] = _bar_from_record(record)
        except Exception:
            validation_failed = True
        if validation_failed:
            raise _fail_closed((ticker,), session)
        records[ticker] = record

    if unresolved:
        try:
            provider_resolution = price_source.resolve_governed_daily_bars(
                unresolved,
                session,
                processed_at=processed_at,
            )
            validation_at = max(processed_at, _utc_now())
        except Exception:
            failures = {
                ticker: _invalid_failure(ticker, session) for ticker in unresolved
            }
            return _result(bars, records, failures)
        provider_invariant_failed = False
        try:
            if not isinstance(provider_resolution, GovernedDailyBarResolution):
                raise ValueError("governed provider result is invalid")
            _validate_provider_resolution(
                provider_resolution,
                expected_tickers=tuple(unresolved),
                session=session,
                processed_at=validation_at,
            )
        except Exception:
            provider_invariant_failed = True
        if provider_invariant_failed:
            raise _fail_closed(unresolved, session)

        failures = dict(provider_resolution.failure_map)
        for ticker in unresolved:
            provider_bar = provider_resolution.bars.get(ticker)
            if provider_bar is None:
                continue
            recovery = provider_resolution.recoveries.get(ticker)
            if recovery is None:
                bars[ticker] = provider_bar
                continue
            persistence_failed = False
            try:
                record = _record_from_recovery(
                    recovery=recovery,
                    epoch_id=canonical_epoch,
                    affected_cohort_ids=cohorts_by_ticker[ticker],
                )
                if _bar_from_record(record) != provider_bar:
                    raise ValueError("governed provider bar does not bind to evidence")
                if persist:
                    if metric_store is None or getattr(
                        metric_store, "read_only", False
                    ):
                        raise ValueError("writable metric store is required")
                    metric_store.save_governed_bar_recovery(record)
            except Exception:
                persistence_failed = True
            if persistence_failed:
                raise _fail_closed((ticker,), session)
            records[ticker] = record
            bars[ticker] = provider_bar
    else:
        failures = {}

    return _result(bars, records, failures)


def _result(
    bars: Mapping[str, MarketBar],
    records: Mapping[str, GovernedBarRecoveryRecord],
    failure_map: Mapping[str, str],
) -> GovernedInputResolution:
    ordered_records = {ticker: records[ticker] for ticker in sorted(records)}
    return GovernedInputResolution(
        bars={ticker: bars[ticker] for ticker in sorted(bars)},
        recovery_bindings={
            ticker: _binding(record) for ticker, record in ordered_records.items()
        },
        recovery_summaries=tuple(
            _summary(record) for record in ordered_records.values()
        ),
        failure_map={ticker: failure_map[ticker] for ticker in sorted(failure_map)},
    )
