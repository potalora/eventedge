"""Fail-closed market-data adapters for execution, marking, and benchmarks."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Collection, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps
import json
import os
import time
from types import MappingProxyType
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from tradingagents.strategies.data_sources.yfinance_source import normalize_tickers
from tradingagents.strategies.execution.alpaca_daily_bar import (
    ADJUSTMENT as SIP_ADJUSTMENT,
    FEED as SIP_FEED,
    HISTORICAL_DELAY,
    MAX_BATCH_SYMBOLS,
    SOURCE as SIP_SOURCE,
    TIMEFRAME as SIP_TIMEFRAME,
    AlpacaBarFailure,
    AlpacaDailyBarResult,
    AlpacaHistoricalSIPSource,
)
from tradingagents.strategies.execution.ids import stable_id
from tradingagents.strategies.execution.models import CorporateAction, MarketBar
from tradingagents.strategies.orchestration.trading_calendar import (
    is_session,
    next_session,
    previous_session,
    session_close,
)


BOUNDED_TIMEOUT_SECONDS = 30
SIP_PRICING_VERSION = "raw-alpaca-sip-v1"
_ET = ZoneInfo("America/New_York")


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
    previous_session: date | None = None
    previous_close: Decimal | None = None

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
    eligibility_evidence: Mapping[str, dict] | None = None


@dataclass(frozen=True)
class GovernedDailyBarAttempt:
    """The exact initial daily evidence retained for one governed ticker."""

    ticker: str
    session: date
    source: str
    fetched_at: datetime
    raw_ohlc: Mapping[str, Decimal] | None
    validation_error: str | None

    def __post_init__(self) -> None:
        if self.raw_ohlc is not None:
            object.__setattr__(
                self, "raw_ohlc", MappingProxyType(dict(self.raw_ohlc))
            )


@dataclass(frozen=True)
class IntradayBarEvidence:
    """Retained evidence model for historical governed recovery payloads."""

    start: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    fetched_at: datetime


@dataclass(frozen=True)
class GovernedBarRecoveryEvidence:
    """Bounded same-provider evidence for one governed reconstruction attempt."""

    ticker: str
    session: date
    daily_attempt: GovernedDailyBarAttempt
    expected_starts: tuple[datetime, ...]
    observed_starts: tuple[datetime, ...]
    intraday_bars: tuple[IntradayBarEvidence, ...]
    reconstructed: MarketBar | None
    validation_error: str | None


@dataclass(frozen=True)
class GovernedDailyBarResolution:
    """Validated governed bars and normalized per-ticker failure evidence."""

    bars: Mapping[str, MarketBar]
    attempts: Mapping[str, GovernedDailyBarAttempt]
    recoveries: Mapping[str, GovernedBarRecoveryEvidence]
    failure_map: Mapping[str, str]

    def __post_init__(self) -> None:
        for field in ("bars", "attempts", "recoveries", "failure_map"):
            object.__setattr__(
                self, field, MappingProxyType(dict(getattr(self, field)))
            )


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

    def resolve_governed_daily_bars(
        self,
        tickers: Collection[str],
        session: date,
        *,
        processed_at: datetime,
        max_age: timedelta = timedelta(hours=24),
    ) -> GovernedDailyBarResolution: ...


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


def paired_adjusted_closes(
    closes: dict[tuple[str, date], AdjustedClose],
    symbols: tuple[str, ...],
    session: date,
) -> dict[tuple[str, date], AdjustedClose]:
    """Bind adjacent observations acquired together in one adjustment vintage."""
    prior = previous_session(session)
    pairs: dict[tuple[str, date], AdjustedClose] = {}
    for symbol in symbols:
        current = closes.get((symbol, session))
        previous = closes.get((symbol, prior))
        if current is None or previous is None:
            raise BarValidationError(
                f"missing paired benchmark {symbol}/{prior}/{session}"
            )
        if (
            current.symbol != symbol
            or current.session != session
            or previous.symbol != symbol
            or previous.session != prior
        ):
            raise BarValidationError(f"mismatched paired benchmark {symbol}/{session}")
        if (
            current.source != previous.source
            or current.fetched_at != previous.fetched_at
        ):
            raise BarValidationError(f"mixed-vintage benchmark {symbol}/{session}")
        if not previous.close.is_finite() or previous.close <= 0:
            raise BarValidationError(f"invalid paired benchmark {symbol}/{prior}")
        pairs[(symbol, session)] = replace(
            current, previous_session=prior, previous_close=previous.close
        )
    return pairs


def validate_benchmark_pairs(
    closes: dict[tuple[str, date], AdjustedClose],
    symbols: set[str],
    session: date,
) -> None:
    prior = previous_session(session)
    for symbol in symbols:
        current = closes[(symbol, session)]
        if (
            current.previous_session != prior
            or not isinstance(current.previous_close, Decimal)
            or not current.previous_close.is_finite()
            or current.previous_close <= 0
        ):
            raise BarValidationError(
                f"missing_or_invalid_same_vintage_benchmark_pair {symbol}/{session}"
            )


def _validate_range(
    tickers: list[str], start_session: date, end_session_inclusive: date
) -> None:
    if not tickers:
        raise ValueError("tickers must not be empty")
    if end_session_inclusive < start_session:
        raise ValueError("end_session_inclusive precedes start_session")


class YahooActionsBenchmarksSource:
    """Lean Yahoo Finance adapter for corporate actions and total-return benchmarks.

    Covers ONLY ``get_corporate_actions``, ``enrich_dividend_payment_terms``,
    and ``get_total_return_closes``. Raw daily execution bars are exclusively
    served by ``AlpacaSIPPriceSource``.
    """

    _RAW_CACHE_LIMIT = 8

    def __init__(
        self,
        now: Callable[[], datetime] | None = None,
        *,
        payment_terms_fetcher: Callable[..., dict] | None = None,
    ) -> None:
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._payment_terms_fetcher = payment_terms_fetcher
        self._raw_cache: OrderedDict[
            tuple[tuple[str, ...], date, date, bool], tuple[pd.DataFrame, datetime]
        ] = OrderedDict()

    def get_corporate_actions(
        self, tickers: list[str], session: date
    ) -> list[CorporateAction]:
        """Return verified split and cash-dividend terms for exactly *session*."""
        _validate_range(tickers, session, session)
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
        return self.enrich_dividend_payment_terms(actions)

    @staticmethod
    def _payment_terms_get(url: str, **options) -> dict:
        from tradingagents.strategies.runtime_deadline import bounded_transport

        result = bounded_transport(
            {"kind": "http_get", "url": url, **options}, options["timeout"]
        )
        if result.get("error") or result.get("status_code") != 200:
            return {}
        return json.loads(result["body"])

    def enrich_dividend_payment_terms(
        self, actions: list[CorporateAction] | tuple[CorporateAction, ...]
    ) -> list[CorporateAction]:
        """Enrich the payable date of an exact, unambiguous existing dividend.

        Alpaca filters process_date, not ex_date. Include upcoming processing
        dates, then join on ex_date and amount; actual observation time is kept.
        The existing action owns the economic amount; this source only adds a
        date. Missing optional currency is not asserted to be USD. An explicit
        foreign denomination, ambiguity, or unsupported terms remains unknown.
        """
        unknown = [
            a
            for a in actions
            if a.action_type == "cash_dividend" and a.payment_date is None
        ]
        key = os.environ.get("ALPACA_API_KEY", "")
        secret = os.environ.get("ALPACA_SECRET_KEY", "")
        if not unknown or not key or not secret:
            return list(actions)
        observed = self._now()
        params: dict[str, object] = {
            "symbols": ",".join(sorted({a.ticker for a in unknown})),
            "types": "cash_dividend",
            "start": (min(a.session for a in unknown) - timedelta(days=366)).isoformat(),
            "end": (observed.date() + timedelta(days=366)).isoformat(),
            "limit": 1000,
            "sort": "asc",
        }
        getter = self._payment_terms_fetcher or self._payment_terms_get
        rows: list[object] = []
        deadline = time.monotonic() + 15.0
        try:
            # No retry; incomplete pagination never authorizes cash.
            for _ in range(3):
                payload = getter(
                    "https://data.alpaca.markets/v1/corporate-actions",
                    params=dict(params),
                    headers={
                        "APCA-API-KEY-ID": key,
                        "APCA-API-SECRET-KEY": secret,
                    },
                    timeout=min(5.0, deadline - time.monotonic()),
                )
                if not isinstance(payload, dict) or not isinstance(
                    payload.get("corporate_actions"), dict
                ):
                    return list(actions)
                page = payload["corporate_actions"].get("cash_dividends", [])
                if not isinstance(page, list):
                    return list(actions)
                rows.extend(page)
                token = payload.get("next_page_token")
                if not token:
                    break
                params["page_token"] = token
            else:
                return list(actions)
        except (TimeoutError, OSError, RuntimeError, ValueError, KeyError, TypeError):
            return list(actions)
        observed = self._now()
        enriched: list[CorporateAction] = []
        for action in actions:
            candidates = [
                r
                for r in rows
                if isinstance(r, dict)
                and r.get("symbol") == action.ticker
                and r.get("ex_date") == action.session.isoformat()
            ]
            if action not in unknown or len(candidates) != 1:
                enriched.append(action)
                continue
            row = candidates[0]
            try:
                payable = date.fromisoformat(row["payable_date"])
                if (
                    payable < action.session
                    or Decimal(str(row["rate"])) != action.cash_per_share
                    or row.get("currency") not in (None, "", "USD")
                    or not row.get("id")
                    or row.get("due_bill_on_date")
                    or row.get("due_bill_off_date")
                    or row.get("sub_type")
                ):
                    raise ValueError("unsupported or mismatched payment terms")
                enriched.append(
                    replace(
                        action,
                        payment_date=payable,
                        payment_source="alpaca-corporate-actions-v1",
                        payment_reference=str(row["id"]),
                        payment_observed_at=observed,
                    )
                )
            except (ValueError, KeyError, TypeError, InvalidOperation):
                enriched.append(action)
        return enriched

    def get_total_return_closes(
        self,
        symbols: list[str],
        start_session: date,
        end_session_inclusive: date,
    ) -> dict[tuple[str, date], AdjustedClose]:
        """Fetch adjusted close-only data reserved for benchmark observations."""
        _validate_range(symbols, start_session, end_session_inclusive)
        frame = yf.download(
            normalize_tickers(symbols),
            start=start_session.isoformat(),
            end=(end_session_inclusive + timedelta(days=1)).isoformat(),
            auto_adjust=True,
            actions=False,
            progress=False,
            timeout=BOUNDED_TIMEOUT_SECONDS,
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
            timeout=BOUNDED_TIMEOUT_SECONDS,
        )
        cached = (frame, self._fetched_at())
        self._raw_cache[key] = cached
        if len(self._raw_cache) > self._RAW_CACHE_LIMIT:
            self._raw_cache.popitem(last=False)
        return cached

    def _fetched_at(self) -> datetime:
        fetched_at = self._now()
        if fetched_at.tzinfo is None:
            raise ValueError("now must return a timezone-aware datetime")
        return fetched_at.astimezone(timezone.utc)


# Backwards-compatible alias while callers drop YFinancePriceSource references;
# carries ONLY corporate actions, dividend payment terms, and adjusted benchmarks.
YFinancePriceSource = YahooActionsBenchmarksSource


class SIPPriceError(BarValidationError):
    """Typed, credential-free primary price failure with no alternative source."""

    def __init__(self, failures: Mapping[str, AlpacaBarFailure], session: date):
        self.failure_map = MappingProxyType(dict(failures))
        super().__init__(
            "; ".join(
                f"{failure.value} {ticker}/{session}"
                for ticker, failure in sorted(failures.items())
            )
        )


def _sip_acquisition(method):
    """Share one five-minute deadline across a requested batch of symbols."""

    @wraps(method)
    def acquire(*args, **kwargs):
        from tradingagents.strategies.data_sources.request_policy import (
            current_provider_deadline,
            provider_budget,
        )

        if current_provider_deadline("alpaca") is not None:
            return method(*args, **kwargs)
        with provider_budget("alpaca", time.monotonic() + 300):
            return method(*args, **kwargs)

    return acquire


class AlpacaSIPPriceSource:
    """Exact raw SIP execution prices, with separately declared Yahoo inputs.

    Yahoo supplies corporate actions and the established total-return-adjusted
    benchmark series via ``YahooActionsBenchmarksSource``. Raw SIP closes are
    never mislabeled as total returns. Research history/volatility/VIX remain
    separate pipeline dependencies. Successful exact-session bars are reused
    within this bounded source object; generation/session persistence remains
    the executor's immutable input bundle.
    """

    pricing_version = SIP_PRICING_VERSION
    yahoo_dependencies = ("corporate_actions", "total_return_adjusted_benchmarks")
    _CACHE_LIMIT = 2048

    def __init__(
        self,
        *,
        sip_source: AlpacaHistoricalSIPSource | None = None,
        actions_source: YahooActionsBenchmarksSource | PriceSource | None = None,
        research_source: YahooActionsBenchmarksSource | PriceSource | None = None,
        now: Callable[[], datetime] | None = None,
        capture_eligibility: bool = False,
    ) -> None:
        self._sip_source = sip_source or AlpacaHistoricalSIPSource()
        self._now = now or (lambda: datetime.now(timezone.utc))
        delegate = actions_source if actions_source is not None else research_source
        self._research_source = delegate or YahooActionsBenchmarksSource(now=self._now)
        self._bars: OrderedDict[tuple[str, date], AlpacaDailyBarResult] = OrderedDict()
        self._capture_eligibility = capture_eligibility

    @property
    def _actions_source(self) -> YahooActionsBenchmarksSource | PriceSource:
        return self._research_source

    @_actions_source.setter
    def _actions_source(self, value: YahooActionsBenchmarksSource | PriceSource) -> None:
        self._research_source = value

    def _fetch(
        self,
        ticker: str,
        session: date,
        processed_at: datetime,
        max_age: timedelta,
        *,
        supplied_result: AlpacaDailyBarResult | None = None,
        acquired_at: datetime | None = None,
    ) -> tuple[MarketBar | None, AlpacaBarFailure | None, datetime]:
        fetched_at = acquired_at if acquired_at is not None else self._now()
        if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
            raise BarValidationError("now must return a timezone-aware datetime")
        if processed_at.tzinfo is None or processed_at.utcoffset() is None:
            raise BarValidationError("processed_at must be timezone-aware")
        key = (ticker, session)
        result = self._bars.get(key)
        if result is not None and (
            result.bar is None or fetched_at - result.bar.fetched_at > max_age
        ):
            del self._bars[key]
            result = None
        if result is None:
            if supplied_result is not None:
                result = supplied_result
            else:
                try:
                    result = self._sip_source.fetch_daily_bar(
                        ticker, session, now=fetched_at
                    )
                except Exception:
                    return None, AlpacaBarFailure.TRANSPORT_ERROR, fetched_at
        if not isinstance(result, AlpacaDailyBarResult):
            return None, AlpacaBarFailure.INVALID_RESPONSE, fetched_at
        if result.failure is not None:
            reason = (
                result.failure
                if isinstance(result.failure, AlpacaBarFailure)
                else AlpacaBarFailure.INVALID_RESPONSE
            )
            return None, reason, fetched_at
        bar = result.bar
        try:
            assert bar is not None
            start = datetime.combine(session, datetime.min.time(), _ET)
            if (
                result.feed != SIP_FEED
                or result.adjustment != SIP_ADJUSTMENT
                or result.timeframe != SIP_TIMEFRAME
                or result.response_symbol != ticker
                or result.row_count != 1
                or result.pagination_complete is not True
                or result.request_start != start
                or result.bar_timestamp != start
                or result.request_end is None
                or result.request_end < start
                or result.request_end >= start + timedelta(days=1)
                or result.request_end > bar.fetched_at - HISTORICAL_DELAY
                or bar.source != SIP_SOURCE
                or bar.fetched_at < session_close(session) + HISTORICAL_DELAY
            ):
                raise BarValidationError("invalid SIP provenance")
            validate_required_bars(
                {key: bar}, {ticker}, session, max(processed_at, fetched_at), max_age
            )
        except (
            AssertionError,
            BarValidationError,
            TypeError,
            ValueError,
            AttributeError,
        ):
            return None, AlpacaBarFailure.INVALID_RESPONSE, fetched_at
        self._bars[key] = result
        self._bars.move_to_end(key)
        if len(self._bars) > self._CACHE_LIMIT:
            self._bars.popitem(last=False)
        return bar, None, bar.fetched_at

    @_sip_acquisition
    def get_daily_bars(
        self,
        tickers: list[str],
        start_session: date,
        end_session_inclusive: date,
        adjusted: bool = False,
    ) -> dict[tuple[str, date], MarketBar]:
        _validate_range(tickers, start_session, end_session_inclusive)
        if adjusted:
            raise BarValidationError("primary SIP policy permits raw daily bars only")
        if not is_session(start_session) or not is_session(end_session_inclusive):
            raise BarValidationError("daily range endpoints must be XNYS sessions")
        bars: dict[tuple[str, date], MarketBar] = {}
        session = start_session
        while session <= end_session_inclusive:
            failures: dict[str, AlpacaBarFailure] = {}
            for ticker in dict.fromkeys(tickers):
                bar, failure, _ = self._fetch(
                    ticker, session, self._now(), timedelta(hours=24)
                )
                if failure is not None:
                    failures[ticker] = failure
                else:
                    assert bar is not None
                    bars[(ticker, session)] = bar
            if failures:
                raise SIPPriceError(failures, session)
            if session == end_session_inclusive:
                break
            session = next_session(session)
        return bars

    @_sip_acquisition
    def resolve_candidate_daily_bars(
        self,
        tickers: list[str],
        session: date,
        processed_at: datetime,
        max_age: timedelta,
    ) -> CandidateBarResolution:
        bars: dict[tuple[str, date], MarketBar] = {}
        attempts: list[CandidateBarAttempt] = []
        failures: set[str] = set()
        eligibility_evidence: dict[str, dict] = {}
        symbols = list(dict.fromkeys(tickers))
        batch_fetch = getattr(self._sip_source, "fetch_daily_bars", None)
        # Keep scalar public/provider implementations compatible. Native batches
        # use only the documented exact-symbol endpoint, with no fallback.
        use_batch = (
            len(symbols) > 1 or self._capture_eligibility
        ) and callable(batch_fetch)
        for offset in range(0, len(symbols), MAX_BATCH_SYMBOLS):
            chunk = symbols[offset : offset + MAX_BATCH_SYMBOLS]
            batch_results = None
            acquired_at = None
            if use_batch:
                acquired_at = self._now()
                if acquired_at.tzinfo is None or acquired_at.utcoffset() is None:
                    raise BarValidationError("now must return a timezone-aware datetime")
                pending = [
                    ticker
                    for ticker in chunk
                    if (
                        (cached := self._bars.get((ticker, session))) is None
                        or cached.bar is None
                        or acquired_at - cached.bar.fetched_at > max_age
                    )
                ]
                if pending:
                    try:
                        batch_results = batch_fetch(
                            pending,
                            session,
                            now=acquired_at,
                            **(
                                {"capture_eligibility": True}
                                if self._capture_eligibility
                                else {}
                            ),
                        )
                    except Exception:
                        batch_results = {
                            ticker: AlpacaDailyBarResult(
                                None,
                                AlpacaBarFailure.TRANSPORT_ERROR,
                                reason_code="transport_error",
                            )
                            for ticker in pending
                        }
                    if not isinstance(batch_results, dict) or set(
                        batch_results
                    ) != set(pending):
                        batch_results = {
                            ticker: AlpacaDailyBarResult(
                                None,
                                AlpacaBarFailure.INVALID_RESPONSE,
                                reason_code="invalid_response",
                            )
                            for ticker in pending
                        }
            for ticker in chunk:
                result = (
                    batch_results.get(ticker) if batch_results is not None else None
                )
                if (
                    batch_results is not None
                    and ticker in batch_results
                    and not isinstance(result, AlpacaDailyBarResult)
                ):
                    result = AlpacaDailyBarResult(
                        None,
                        AlpacaBarFailure.INVALID_RESPONSE,
                        reason_code="invalid_response",
                    )
                bar, failure, fetched_at = self._fetch(
                    ticker,
                    session,
                    processed_at,
                    max_age,
                    supplied_result=result,
                    acquired_at=acquired_at,
                )
                if (
                    self._capture_eligibility
                    and result is not None
                    and result.eligibility_evidence is not None
                ):
                    eligibility_evidence[ticker] = result.eligibility_evidence
                error = f"{failure.value} {ticker}/{session}" if failure else None
                if failure and result is not None and result.reason_code is not None:
                    # New fixed diagnostics fit the historical evidence schema.
                    # Do not remap old strings: immutable replay derives their
                    # original coarse issue category from that exact prefix.
                    prefix = failure.value
                    if result.reason_code == "missing_data":
                        prefix = "missing"
                    elif failure in {
                        AlpacaBarFailure.TRANSPORT_ERROR,
                        AlpacaBarFailure.HTTP_ERROR,
                        AlpacaBarFailure.MISSING_CREDENTIALS,
                    }:
                        prefix = "provider_error"
                    elif failure == AlpacaBarFailure.SESSION_NOT_READY:
                        prefix = "pre-close"
                    details = (
                        f"failure={failure.value};reason={result.reason_code};"
                        f"attempts={result.attempts}"
                    )
                    if result.http_status is not None:
                        details += f";http_status={result.http_status}"
                    error = f"{prefix} {ticker}/{session} [{details}]"
                attempts.append(
                    CandidateBarAttempt(
                        ticker,
                        session,
                        1,
                        SIP_SOURCE,
                        fetched_at,
                        bar.open if bar else None,
                        bar.high if bar else None,
                        bar.low if bar else None,
                        bar.close if bar else None,
                        error,
                    )
                )
                if bar is not None:
                    bars[(ticker, session)] = bar
                else:
                    failures.add(ticker)
        return CandidateBarResolution(
            bars,
            tuple(attempts),
            frozenset(),
            frozenset(failures),
            eligibility_evidence if self._capture_eligibility else None,
        )

    def resolve_governed_daily_bars(
        self,
        tickers: Collection[str],
        session: date,
        *,
        processed_at: datetime,
        max_age: timedelta = timedelta(hours=24),
    ) -> GovernedDailyBarResolution:
        candidate = self.resolve_candidate_daily_bars(
            list(tickers), session, processed_at, max_age
        )
        attempts = {
            item.ticker: GovernedDailyBarAttempt(
                item.ticker,
                session,
                item.source,
                item.fetched_at,
                dict(
                    zip(
                        ("open", "high", "low", "close"),
                        (item.open, item.high, item.low, item.close),
                    )
                )
                if item.validation_error is None
                else None,
                item.validation_error,
            )
            for item in candidate.attempts
        }
        return GovernedDailyBarResolution(
            {ticker: bar for (ticker, _), bar in candidate.bars.items()},
            attempts,
            {},
            {
                ticker: f"invalid {ticker}/{session}"
                for ticker in candidate.quarantined_tickers
            },
        )

    def get_corporate_actions(
        self, tickers: list[str], session: date
    ) -> list[CorporateAction]:
        return self._actions_source.get_corporate_actions(tickers, session)

    def enrich_dividend_payment_terms(
        self, actions: list[CorporateAction] | tuple[CorporateAction, ...]
    ) -> list[CorporateAction]:
        enrich = getattr(self._actions_source, "enrich_dividend_payment_terms", None)
        return enrich(actions) if enrich else list(actions)

    def get_total_return_closes(
        self,
        symbols: list[str],
        start_session: date,
        end_session_inclusive: date,
    ) -> dict[tuple[str, date], AdjustedClose]:
        """Declared Yahoo adjusted series; raw SIP close is not a total return."""
        return self._actions_source.get_total_return_closes(
            symbols, start_session, end_session_inclusive
        )


def build_price_source(config: Mapping[str, object] | object) -> PriceSource:
    """Build the current raw SIP policy; alternate raw providers fail visibly."""
    if hasattr(config, "paper_ledger") and not isinstance(config, Mapping):
        version = getattr(config.paper_ledger, "pricing_version", SIP_PRICING_VERSION)
        capture_eligibility = False
    elif isinstance(config, Mapping):
        ar = config.get("autoresearch", {})
        ar_map = ar if isinstance(ar, Mapping) else {}
        pl = ar_map.get("paper_ledger", {})
        pl_map = pl if isinstance(pl, Mapping) else {}
        version = pl_map.get("pricing_version", SIP_PRICING_VERSION)
        capture_eligibility = (
            ar_map.get("new_entry_eligibility_policy")
            == "reference_session_activity_v1"
        )
    else:
        raise TypeError("config must be a mapping or GenerationConfig")
    if version == SIP_PRICING_VERSION:
        return AlpacaSIPPriceSource(capture_eligibility=capture_eligibility)
    raise ValueError("unsupported pricing_version")


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
    if value is None or pd.isna(value):
        raise CorporateActionValidationError(
            f"missing action evidence {ticker}/{session} {field}"
        )
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
