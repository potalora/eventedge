"""Fail-closed market-data adapters for execution, marking, and benchmarks."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable, Protocol

import pandas as pd
import yfinance as yf

from tradingagents.strategies.data_sources.yfinance_source import normalize_tickers
from tradingagents.strategies.execution.ids import stable_id
from tradingagents.strategies.execution.models import CorporateAction, MarketBar
from tradingagents.strategies.orchestration.trading_calendar import session_close


class BarValidationError(ValueError):
    """Raised when data cannot safely be used for execution or valuation."""


class CorporateActionValidationError(ValueError):
    """Raised when a nonzero corporate action cannot be verified."""


@dataclass(frozen=True)
class AdjustedClose:
    """One provenance-bearing total-return-adjusted benchmark close."""

    symbol: str
    session: date
    close: Decimal
    source: str
    fetched_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.close, Decimal):
            raise TypeError("close must be Decimal")


@dataclass(frozen=True)
class CandidateBarAttempt:
    """One candidate-bar retrieval, including any validation failure."""

    ticker: str
    session: date
    attempt: int
    source: str
    fetched_at: datetime
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    validation_error: str | None


@dataclass(frozen=True)
class CandidateBarResolution:
    """Validated candidate bars plus the evidence for one recovery retry."""

    bars: dict[tuple[str, date], MarketBar]
    attempts: tuple[CandidateBarAttempt, ...]
    recovered_tickers: frozenset[str]
    quarantined_tickers: frozenset[str]


class PriceSource(Protocol):
    """Inclusive daily market-data boundary for the authoritative ledger."""

    def get_daily_bars(
        self,
        tickers: list[str],
        start_session: date,
        end_session_inclusive: date,
        adjusted: bool = False,
    ) -> dict[tuple[str, date], MarketBar]: ...

    def get_corporate_actions(
        self, tickers: list[str], session: date
    ) -> list[CorporateAction]: ...

    def get_total_return_closes(
        self,
        symbols: list[str],
        start_session: date,
        end_session_inclusive: date,
    ) -> dict[tuple[str, date], AdjustedClose]: ...

    def resolve_candidate_daily_bars(
        self,
        tickers: list[str],
        session: date,
        processed_at: datetime,
        max_age: timedelta,
    ) -> CandidateBarResolution: ...


def validate_required_bars(
    bars: dict[tuple[str, date], MarketBar],
    tickers: set[str],
    session: date,
    as_of: datetime,
    max_fetch_age: timedelta = timedelta(hours=24),
) -> None:
    """Reject missing, unsafe, or non-raw bars before any ledger mutation."""
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise BarValidationError("as_of must be timezone-aware")
    errors: list[str] = []
    for ticker in sorted(tickers):
        bar = bars.get((ticker, session))
        if bar is None:
            errors.append(f"missing {ticker}/{session}")
            continue
        if bar.ticker != ticker or bar.session != session:
            errors.append(f"mismatched {ticker}/{session}")
        values = (bar.open, bar.high, bar.low, bar.close)
        if bar.adjusted:
            errors.append(f"adjusted {ticker}/{session}")
        if not bar.source:
            errors.append(f"missing source {ticker}/{session}")
        if any(not value.is_finite() or value <= 0 for value in values):
            errors.append(f"invalid {ticker}/{session}")
        if bar.fetched_at.tzinfo is None or bar.fetched_at.utcoffset() is None:
            errors.append(f"naive {ticker}/{session}")
        elif bar.fetched_at > as_of:
            errors.append(f"future {ticker}/{session}")
        elif as_of - bar.fetched_at > max_fetch_age:
            errors.append(f"stale {ticker}/{session}")
        if all(value.is_finite() for value in values) and not (
            bar.low <= bar.open <= bar.high and bar.low <= bar.close <= bar.high
        ):
            errors.append(f"incoherent {ticker}/{session}")
    if errors:
        raise BarValidationError("; ".join(errors))


def validate_adjusted_closes(
    closes: dict[tuple[str, date], AdjustedClose],
    symbols: set[str],
    session: date,
    as_of: datetime,
    max_fetch_age: timedelta = timedelta(hours=24),
) -> None:
    """Reject missing or time-unsafe adjusted benchmark observations."""
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise BarValidationError("as_of must be timezone-aware")
    errors: list[str] = []
    for symbol in sorted(symbols):
        observation = closes.get((symbol, session))
        if observation is None:
            errors.append(f"missing {symbol}/{session}")
            continue
        if observation.symbol != symbol or observation.session != session:
            errors.append(f"mismatched {symbol}/{session}")
        if not observation.close.is_finite() or observation.close <= 0:
            errors.append(f"invalid {symbol}/{session}")
        if not observation.source:
            errors.append(f"missing source {symbol}/{session}")
        if (
            observation.fetched_at.tzinfo is None
            or observation.fetched_at.utcoffset() is None
        ):
            errors.append(f"naive {symbol}/{session}")
        elif observation.fetched_at > as_of:
            errors.append(f"future {symbol}/{session}")
        elif as_of - observation.fetched_at > max_fetch_age:
            errors.append(f"stale {symbol}/{session}")
    if errors:
        raise BarValidationError("; ".join(errors))


class YFinancePriceSource:
    """yfinance adapter with raw execution bars and separate adjusted benchmarks.

    The bounded raw-frame cache only deduplicates an immediately repeated
    price/action request. It never stores adjusted benchmark data or grows
    without limit.
    """

    _RAW_CACHE_LIMIT = 8

    def __init__(self, now: Callable[[], datetime] | None = None) -> None:
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._raw_cache: OrderedDict[
            tuple[tuple[str, ...], date, date, bool], tuple[pd.DataFrame, datetime]
        ] = OrderedDict()

    def get_daily_bars(
        self,
        tickers: list[str],
        start_session: date,
        end_session_inclusive: date,
        adjusted: bool = False,
    ) -> dict[tuple[str, date], MarketBar]:
        """Fetch raw daily OHLC bars through an inclusive terminal session."""
        self._validate_range(tickers, start_session, end_session_inclusive)
        frame, fetched_at = self._raw_frame(
            tickers, start_session, end_session_inclusive, adjusted=adjusted
        )
        bars: dict[tuple[str, date], MarketBar] = {}
        normalized = normalize_tickers(tickers)
        _require_flat_frame_provenance(frame, normalized)
        for original, yf_ticker in zip(tickers, normalized):
            for timestamp in frame.index:
                session = _session_date(timestamp)
                values = tuple(
                    _decimal_bar_value(
                        _frame_value(frame, field, yf_ticker, original, timestamp),
                        original,
                        session,
                        field.lower(),
                    )
                    for field in ("Open", "High", "Low", "Close")
                )
                bars[(original, session)] = MarketBar(
                    ticker=original,
                    session=session,
                    open=values[0],
                    high=values[1],
                    low=values[2],
                    close=values[3],
                    source="yfinance",
                    fetched_at=fetched_at,
                    adjusted=adjusted,
                )
        validate_required_bars(
            bars, set(tickers), end_session_inclusive, self._fetched_at()
        )
        return bars

    def resolve_candidate_daily_bars(
        self,
        tickers: list[str],
        session: date,
        processed_at: datetime,
        max_age: timedelta,
    ) -> CandidateBarResolution:
        """Return valid candidate bars, retrying only initially invalid tickers once."""
        self._validate_range(tickers, session, session)
        frame, fetched_at = self._raw_frame(tickers, session, session, adjusted=False)
        attempts: list[CandidateBarAttempt] = []
        bars: dict[tuple[str, date], MarketBar] = {}
        invalid_tickers: list[str] = []
        for ticker in tickers:
            bar, attempt = self._candidate_bar_attempt(
                frame, ticker, tickers, session, fetched_at, 1, processed_at, max_age
            )
            attempts.append(attempt)
            if attempt.validation_error is not None:
                invalid_tickers.append(ticker)
            else:
                assert bar is not None
                bars[(ticker, session)] = bar

        recovered_tickers: set[str] = set()
        quarantined_tickers: set[str] = set()
        if invalid_tickers:
            for ticker in invalid_tickers:
                refreshed = self.refresh_daily_bars([ticker], session, session)
                bar = refreshed.bars.get((ticker, session))
                raw_attempt = refreshed.attempts[0]
                attempt = (
                    raw_attempt
                    if raw_attempt.validation_error is not None
                    else self._candidate_attempt_from_bar(
                        ticker, session, 2, bar, processed_at, max_age
                    )
                )
                attempts.append(attempt)
                if attempt.validation_error is None and bar is not None:
                    bars[(ticker, session)] = bar
                    recovered_tickers.add(ticker)
                else:
                    quarantined_tickers.add(ticker)
        return CandidateBarResolution(
            bars=bars,
            attempts=tuple(attempts),
            recovered_tickers=frozenset(recovered_tickers),
            quarantined_tickers=frozenset(quarantined_tickers),
        )

    def refresh_daily_bars(
        self,
        tickers: list[str],
        start_session: date,
        end_session_inclusive: date,
    ) -> CandidateBarResolution:
        """Fetch raw daily bars without consulting or updating the raw-frame cache."""
        self._validate_range(tickers, start_session, end_session_inclusive)
        frame = yf.download(
            normalize_tickers(tickers),
            start=start_session.isoformat(),
            end=(end_session_inclusive + timedelta(days=1)).isoformat(),
            auto_adjust=False,
            actions=True,
            progress=False,
            timeout=30,
        )
        fetched_at = self._fetched_at()
        bars: dict[tuple[str, date], MarketBar] = {}
        attempts: list[CandidateBarAttempt] = []
        for ticker in tickers:
            bar, attempt = self._candidate_bar_attempt(
                frame,
                ticker,
                tickers,
                end_session_inclusive,
                fetched_at,
                2,
                fetched_at,
                timedelta.max,
            )
            attempts.append(attempt)
            if bar is not None:
                bars[(ticker, end_session_inclusive)] = bar
        return CandidateBarResolution(
            bars=bars,
            attempts=tuple(attempts),
            recovered_tickers=frozenset(),
            quarantined_tickers=frozenset(),
        )

    def _candidate_bar_attempt(
        self,
        frame: pd.DataFrame,
        ticker: str,
        requested_tickers: list[str],
        session: date,
        fetched_at: datetime,
        attempt: int,
        processed_at: datetime,
        max_age: timedelta,
    ) -> tuple[MarketBar | None, CandidateBarAttempt]:
        _require_flat_frame_provenance(frame, normalize_tickers(requested_tickers))
        normalized = normalize_tickers([ticker])[0]
        values: list[Decimal | None] = []
        error: str | None = None
        for field in ("Open", "High", "Low", "Close"):
            try:
                raw = _frame_value(frame, field, normalized, ticker, pd.Timestamp(session))
                values.append(_decimal_bar_value(raw, ticker, session, field.lower()))
            except BarValidationError as exc:
                values.append(None)
                error = str(exc)
                break
        values.extend([None] * (4 - len(values)))
        if error is not None:
            return None, CandidateBarAttempt(
                ticker=ticker,
                session=session,
                attempt=attempt,
                source="yfinance",
                fetched_at=fetched_at,
                open=values[0],
                high=values[1],
                low=values[2],
                close=values[3],
                validation_error=error,
            )
        bar = MarketBar(
            ticker=ticker,
            session=session,
            open=values[0],  # type: ignore[arg-type]
            high=values[1],  # type: ignore[arg-type]
            low=values[2],  # type: ignore[arg-type]
            close=values[3],  # type: ignore[arg-type]
            source="yfinance",
            fetched_at=fetched_at,
            adjusted=False,
        )
        return bar, self._candidate_attempt_from_bar(
            ticker, session, attempt, bar, processed_at, max_age, error
        )

    @staticmethod
    def _candidate_attempt_from_bar(
        ticker: str,
        session: date,
        attempt: int,
        bar: MarketBar | None,
        processed_at: datetime,
        max_age: timedelta,
        conversion_error: str | None = None,
    ) -> CandidateBarAttempt:
        error = conversion_error
        if bar is None and error is None:
            error = f"missing {ticker}/{session}"
        if bar is not None and error is None:
            try:
                validate_required_bars(
                    {(ticker, session): bar}, {ticker}, session, processed_at, max_age
                )
            except BarValidationError as exc:
                error = str(exc)
        if bar is not None and error is None and bar.fetched_at < session_close(session):
            error = f"pre-close {ticker}/{session}"
        return CandidateBarAttempt(
            ticker=ticker,
            session=session,
            attempt=attempt,
            source=bar.source if bar is not None else "yfinance",
            fetched_at=bar.fetched_at if bar is not None else processed_at,
            open=bar.open if bar is not None else None,
            high=bar.high if bar is not None else None,
            low=bar.low if bar is not None else None,
            close=bar.close if bar is not None else None,
            validation_error=error,
        )

    def get_corporate_actions(
        self, tickers: list[str], session: date
    ) -> list[CorporateAction]:
        """Return verified split and cash-dividend terms for exactly *session*."""
        self._validate_range(tickers, session, session)
        frame, fetched_at = self._raw_frame(tickers, session, session, adjusted=False)
        actions: list[CorporateAction] = []
        normalized = normalize_tickers(tickers)
        _require_flat_frame_provenance(frame, normalized)
        for original, yf_ticker in zip(tickers, normalized):
            for field, action_type, target in (
                ("Stock Splits", "split", "ratio"),
                ("Dividends", "cash_dividend", "cash_per_share"),
            ):
                value = _frame_value(
                    frame, field, yf_ticker, original, pd.Timestamp(session)
                )
                action_value = _optional_action_decimal(value, original, session, field)
                if action_value is None:
                    continue
                if action_value <= 0:
                    raise CorporateActionValidationError(
                        f"invalid {original}/{session} {field}"
                    )
                ratio = action_value if target == "ratio" else None
                cash_per_share = action_value if target == "cash_per_share" else None
                actions.append(
                    CorporateAction(
                        action_id=stable_id(
                            "corporate_action",
                            original,
                            session,
                            action_type,
                            action_value,
                            "yfinance",
                        ),
                        ticker=original,
                        session=session,
                        action_type=action_type,
                        ratio=ratio,
                        cash_per_share=cash_per_share,
                        source="yfinance",
                        fetched_at=fetched_at,
                        verified=True,
                    )
                )
        return actions

    def get_total_return_closes(
        self,
        symbols: list[str],
        start_session: date,
        end_session_inclusive: date,
    ) -> dict[tuple[str, date], AdjustedClose]:
        """Fetch adjusted close-only data reserved for benchmark observations."""
        self._validate_range(symbols, start_session, end_session_inclusive)
        frame = yf.download(
            normalize_tickers(symbols),
            start=start_session.isoformat(),
            end=(end_session_inclusive + timedelta(days=1)).isoformat(),
            auto_adjust=True,
            actions=False,
            progress=False,
            timeout=30,
        )
        fetched_at = self._fetched_at()
        closes: dict[tuple[str, date], AdjustedClose] = {}
        normalized = normalize_tickers(symbols)
        _require_flat_frame_provenance(frame, normalized)
        for original, yf_ticker in zip(symbols, normalized):
            for timestamp in frame.index:
                session = _session_date(timestamp)
                close = _decimal_positive_price(
                    _frame_value(frame, "Close", yf_ticker, original, timestamp),
                    original,
                    session,
                    "close",
                )
                closes[(original, session)] = AdjustedClose(
                    original, session, close, "yfinance-adjusted", fetched_at
                )
        validate_adjusted_closes(
            closes, set(symbols), end_session_inclusive, self._fetched_at()
        )
        return closes

    def _raw_frame(
        self,
        tickers: list[str],
        start_session: date,
        end_session_inclusive: date,
        *,
        adjusted: bool,
    ) -> tuple[pd.DataFrame, datetime]:
        key = (tuple(tickers), start_session, end_session_inclusive, adjusted)
        cached = self._raw_cache.get(key)
        if cached is not None:
            self._raw_cache.move_to_end(key)
            return cached
        frame = yf.download(
            normalize_tickers(tickers),
            start=start_session.isoformat(),
            end=(end_session_inclusive + timedelta(days=1)).isoformat(),
            auto_adjust=adjusted,
            actions=True,
            progress=False,
            timeout=30,
        )
        cached = (frame, self._fetched_at())
        self._raw_cache[key] = cached
        if len(self._raw_cache) > self._RAW_CACHE_LIMIT:
            self._raw_cache.popitem(last=False)
        return cached

    @staticmethod
    def _validate_range(
        tickers: list[str], start_session: date, end_session_inclusive: date
    ) -> None:
        if not tickers:
            raise ValueError("tickers must not be empty")
        if end_session_inclusive < start_session:
            raise ValueError("end_session_inclusive precedes start_session")

    def _fetched_at(self) -> datetime:
        fetched_at = self._now()
        if fetched_at.tzinfo is None:
            raise ValueError("now must return a timezone-aware datetime")
        return fetched_at.astimezone(timezone.utc)


def _session_date(value: object) -> date:
    return pd.Timestamp(value).date()


def _frame_value(
    frame: pd.DataFrame,
    field: str,
    yf_ticker: str,
    original_ticker: str,
    index: object,
) -> object:
    if isinstance(frame.columns, pd.MultiIndex):
        for ticker in (yf_ticker, original_ticker):
            key = (field, ticker)
            if key in frame.columns:
                return frame.loc[index, key]
        return None
    if field in frame.columns:
        return frame.loc[index, field]
    return None


def _require_flat_frame_provenance(
    frame: pd.DataFrame, normalized_tickers: list[str]
) -> None:
    if (
        not isinstance(frame.columns, pd.MultiIndex)
        and len(set(normalized_tickers)) != 1
    ):
        raise BarValidationError(
            "ambiguous flat columns for multiple requested tickers"
        )


def _decimal_bar_value(
    value: object, ticker: str, session: date, field: str
) -> Decimal:
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise BarValidationError(f"invalid {ticker}/{session} {field}") from exc
    if not decimal_value.is_finite():
        raise BarValidationError(f"invalid {ticker}/{session} {field}")
    return decimal_value


def _decimal_positive_price(
    value: object, ticker: str, session: date, field: str
) -> Decimal:
    decimal_value = _decimal_bar_value(value, ticker, session, field)
    if decimal_value <= 0:
        raise BarValidationError(f"invalid {ticker}/{session} {field}")
    return decimal_value


def _optional_action_decimal(
    value: object, ticker: str, session: date, field: str
) -> Decimal | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise CorporateActionValidationError(
            f"invalid {ticker}/{session} {field}"
        ) from exc
    if not decimal_value.is_finite() or decimal_value == 0:
        if decimal_value == 0:
            return None
        raise CorporateActionValidationError(f"invalid {ticker}/{session} {field}")
    return decimal_value
