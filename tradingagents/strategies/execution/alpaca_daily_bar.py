"""Read-only, fail-closed historical SIP evidence for an exact US equity session.

Only this adapter's explicit SIP/raw request is trusted. Transient transport
failures use the common bounded request policy; malformed prices are terminal.
The adapter has no alternative-feed, trading or persistence path.

Provider contract:
https://docs.alpaca.markets/us/reference/stockbarsingle-1
https://docs.alpaca.markets/us/docs/market-data-faq
"""

from __future__ import annotations

import os
import re
import json
import hashlib
import time as clock_time
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Callable
from zoneinfo import ZoneInfo

import requests

from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
from tradingagents.strategies.data_sources.request_policy import (
    current_provider_deadline, provider_budget, provider_request,
    provider_timeout, read_bounded_response,
)
from tradingagents.strategies.execution.models import MarketBar
from tradingagents.strategies.orchestration.trading_calendar import session_close

SOURCE = "alpaca-sip-1d-raw"
FEED = "sip"
ADJUSTMENT = "raw"
TIMEFRAME = "1Day"
HISTORICAL_DELAY = timedelta(minutes=15)
REQUEST_TIMEOUT = (5.0, 20.0)  # connect/read, clipped to remaining budget
_ET = ZoneInfo("America/New_York")
_SYMBOL = re.compile(r"[A-Z][A-Z0-9.-]{0,15}\Z")
MAX_BATCH_SYMBOLS = 100
MAX_BATCH_PAGES = 128
MAX_BATCH_ROWS = 10000
MAX_BATCH_BYTES = 8 * 1024 * 1024
_DIAGNOSTIC_REASONS = frozenset({
    "timeout", "transport_error", "http_error", "provider_error", "invalid_response",
    "missing_data", "invalid_request", "missing_credentials", "session_not_ready",
})


class AlpacaBarFailure(str, Enum):
    INVALID_REQUEST = "invalid_request"
    MISSING_CREDENTIALS = "missing_credentials"
    SESSION_NOT_READY = "session_not_ready"
    TRANSPORT_ERROR = "transport_error"
    HTTP_ERROR = "http_error"
    INVALID_RESPONSE = "invalid_response"


@dataclass(frozen=True)
class AlpacaDailyBarResult:
    """Credential-free normalized evidence, with exactly one bar or failure."""

    bar: MarketBar | None
    failure: AlpacaBarFailure | None
    request_start: datetime | None = None
    request_end: datetime | None = None
    bar_timestamp: datetime | None = None
    feed: str = FEED
    adjustment: str = ADJUSTMENT
    timeframe: str = TIMEFRAME
    response_symbol: str | None = None
    row_count: int | None = None
    pagination_complete: bool = False
    # Optional new-acquisition diagnostics. Historical recovery records keep
    # their existing ten fields and validation_error strings unchanged.
    reason_code: str | None = None
    http_status: int | None = None
    attempts: int = 0
    eligibility_evidence: dict | None = None

    def __post_init__(self) -> None:
        if (self.bar is None) == (self.failure is None):
            raise ValueError("exactly one bar or failure is required")
        if self.reason_code is not None and self.reason_code not in _DIAGNOSTIC_REASONS:
            raise ValueError("invalid safe SIP diagnostic")
        if self.http_status is not None and (
            type(self.http_status) is not int or not 100 <= self.http_status <= 599
        ):
            raise ValueError("invalid safe SIP HTTP status")
        if type(self.attempts) is not int or not 0 <= self.attempts <= 5:
            raise ValueError("invalid safe SIP attempt count")


def _price(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("invalid price")
    result = Decimal(str(value))
    if not result.is_finite() or result <= 0:
        raise ValueError("invalid price")
    return result


def _validate_activity(row: dict) -> None:
    """Require actual stock-bar activity, without a minimum liquidity rule.

    Alpaca's stock aggregation contract increments volume and trade count
    together and emits bars only with nonzero OHLC and volume. A flat price
    may be valid; zero-activity provider rows cannot establish this session's
    traded execution/mark price. No VWAP or arbitrary size threshold is used.
    https://docs.alpaca.markets/us/docs/market-data-faq#how-are-bars-aggregated
    """
    _price(row["v"])
    if type(row["n"]) is not int or row["n"] <= 0:
        raise ValueError("invalid stock bar activity")


class AlpacaHistoricalSIPSource:
    """Validated historical observations; credentials come from env."""

    def __init__(self, *, get: Callable | None = None) -> None:
        self._get = get or requests.get

    def fetch_daily_bars(
        self, tickers: list[str], session: date, *, now: datetime | None = None,
        capture_eligibility: bool = False,
    ) -> dict[str, AlpacaDailyBarResult]:
        """Collect an exact-symbol SIP batch before publishing any observations.

        The documented multi-stock endpoint can omit symbols with no data.
        Pagination must complete globally; malformed known-symbol rows fail
        only that symbol. Unknown identities and incomplete pagination invalidate
        the whole batch. Every page consumes the same acquisition deadline.
        https://docs.alpaca.markets/us/reference/stockbars
        """
        if current_provider_deadline("alpaca") is None:
            with provider_budget("alpaca", clock_time.monotonic() + 300):
                return self.fetch_daily_bars(tickers, session, now=now,
                                             capture_eligibility=capture_eligibility)
        symbols = list(dict.fromkeys(tickers))
        fetched_at = now if now is not None else datetime.now(timezone.utc)
        start = end = None
        eligibility_pages = []

        def fail_all(reason, reason_code, *, http_status=None, attempts=0):
            evidence = None
            if capture_eligibility and isinstance(session, date) and isinstance(fetched_at, datetime):
                evidence = {'source': SOURCE, 'session': session.isoformat(),
                            'symbols': symbols, 'observed_at': fetched_at.isoformat(),
                            'complete': False, 'pages': eligibility_pages}
            return {symbol: AlpacaDailyBarResult(
                None, reason, start, end, reason_code=reason_code,
                http_status=http_status, attempts=attempts, eligibility_evidence=evidence,
            ) for symbol in symbols}

        try:
            if (
                not symbols or len(symbols) > MAX_BATCH_SYMBOLS
                or any(not isinstance(symbol, str) or _SYMBOL.fullmatch(symbol) is None for symbol in symbols)
                or type(session) is not date
                or not isinstance(fetched_at, datetime) or fetched_at.tzinfo is None
                or fetched_at.utcoffset() is None
            ):
                raise ValueError("invalid request")
            close_at = session_close(session)
            start = datetime.combine(session, time.min, _ET)
            next_midnight = datetime.combine(session + timedelta(days=1), time.min, _ET)
            end = min(fetched_at - HISTORICAL_DELAY, next_midnight - timedelta(microseconds=1))
        except (ValueError, TypeError, OverflowError):
            return fail_all(AlpacaBarFailure.INVALID_REQUEST, "invalid_request")
        if fetched_at < close_at + HISTORICAL_DELAY:
            return fail_all(AlpacaBarFailure.SESSION_NOT_READY, "session_not_ready")
        key = os.environ.get("ALPACA_API_KEY", "").strip()
        secret = os.environ.get("ALPACA_SECRET_KEY", "").strip()
        if not key or not secret:
            return fail_all(AlpacaBarFailure.MISSING_CREDENTIALS, "missing_credentials")

        params = {
            "symbols": ",".join(symbols), "feed": FEED, "adjustment": ADJUSTMENT,
            "timeframe": TIMEFRAME, "start": start.isoformat(), "end": end.isoformat(),
            "asof": "-", "currency": "USD", "sort": "asc", "limit": 201,
        }
        rows = {symbol: [] for symbol in symbols}
        invalid_symbols = set()
        seen_tokens = set()
        token = None
        total_bytes = total_rows = 0

        def unique_fields(pairs):
            result = {}
            for field, value in pairs:
                if field in result:
                    raise ValueError("duplicate response field")
                result[field] = value
            return result

        for _ in range(MAX_BATCH_PAGES):
            response = None
            try:
                response = provider_request(
                    "alpaca", "GET", "https://data.alpaca.markets/v2/stocks/bars",
                    operation="raw_daily_bars", transport=self._get,
                    params={**params, **({"page_token": token} if token is not None else {})},
                    headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
                    timeout=REQUEST_TIMEOUT, allow_redirects=False, stream=True,
                )
                provider_timeout("alpaca")
                if response.status_code != 200:
                    return fail_all(AlpacaBarFailure.HTTP_ERROR, "http_error", http_status=response.status_code)
                content = read_bounded_response(
                    response, provider="alpaca", max_bytes=MAX_BATCH_BYTES - total_bytes,
                )
                total_bytes += len(content)
                if capture_eligibility:
                    eligibility_pages.append({
                        'url': 'https://data.alpaca.markets/v2/stocks/bars',
                        'params': {**params, **({'page_token': token} if token is not None else {})},
                        'status': response.status_code, 'body': content.decode('utf-8'),
                        'sha256': hashlib.sha256(content).hexdigest(),
                    })
                provider_timeout("alpaca")
                payload = json.loads(content, parse_float=Decimal, object_pairs_hook=unique_fields)
                provider_timeout("alpaca")
                if (
                    not isinstance(payload, dict) or not isinstance(payload.get("bars"), dict)
                    or "next_page_token" not in payload
                    or any(symbol not in rows for symbol in payload["bars"])
                ):
                    raise ValueError("invalid batch envelope")
                next_token = payload["next_page_token"]
                if next_token is not None and (
                    not isinstance(next_token, str) or not next_token or len(next_token) > 1024
                    or next_token in seen_tokens
                ):
                    raise ValueError("invalid pagination")
                for symbol, page_rows in payload["bars"].items():
                    if not isinstance(page_rows, list):
                        invalid_symbols.add(symbol)
                        continue
                    total_rows += len(page_rows)
                    if total_rows > MAX_BATCH_ROWS:
                        raise ValueError("batch row limit")
                    # More than one row is already a per-symbol failure. Keep
                    # only enough rows to prove that fact, not full vendor data.
                    rows[symbol].extend(page_rows[:max(0, 2 - len(rows[symbol]))])
                if next_token is None:
                    break
                seen_tokens.add(next_token)
                token = next_token
            except SourceFetchError as error:
                if error.reason_code == "invalid_response":
                    return fail_all(AlpacaBarFailure.INVALID_RESPONSE, "invalid_response")
                return fail_all(
                    AlpacaBarFailure.HTTP_ERROR if error.http_status is not None else AlpacaBarFailure.TRANSPORT_ERROR,
                    error.reason_code, http_status=error.http_status, attempts=error.attempts,
                )
            except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
                return fail_all(AlpacaBarFailure.INVALID_RESPONSE, "invalid_response")
            except Exception:
                return fail_all(AlpacaBarFailure.TRANSPORT_ERROR, "transport_error")
            finally:
                if response is not None:
                    try:
                        response.close()
                    except Exception:
                        pass  # Cleanup cannot replace validated data or leak provider text.
        else:
            return fail_all(AlpacaBarFailure.INVALID_RESPONSE, "invalid_response")

        try:
            provider_timeout("alpaca")
        except SourceFetchError:
            return fail_all(AlpacaBarFailure.TRANSPORT_ERROR, "timeout")
        results = {}
        for symbol in symbols:
            symbol_rows = rows[symbol]
            if symbol not in invalid_symbols and not symbol_rows:
                results[symbol] = AlpacaDailyBarResult(
                    None, AlpacaBarFailure.INVALID_RESPONSE, start, end,
                    response_symbol=symbol, row_count=0, pagination_complete=True, reason_code="missing_data",
                )
                continue
            try:
                if symbol in invalid_symbols or len(symbol_rows) != 1:
                    raise ValueError("invalid symbol rows")
                row = symbol_rows[0]
                if not isinstance(row, dict) or not isinstance(row.get("t"), str):
                    raise ValueError("invalid row")
                stamp = datetime.fromisoformat(row["t"].replace("Z", "+00:00"))
                if stamp.tzinfo is None or stamp.utcoffset() is None or stamp != start:
                    raise ValueError("invalid timestamp")
                _validate_activity(row)
                op, high, low, close = (_price(row[field]) for field in ("o", "h", "l", "c"))
                if high < max(op, close) or low > min(op, close) or high < low:
                    raise ValueError("incoherent prices")
                results[symbol] = AlpacaDailyBarResult(
                    MarketBar(symbol, session, op, high, low, close, SOURCE, fetched_at, False),
                    None, start, end, stamp, response_symbol=symbol, row_count=1, pagination_complete=True,
                )
            except (ValueError, TypeError, KeyError, InvalidOperation, OverflowError):
                results[symbol] = AlpacaDailyBarResult(
                    None, AlpacaBarFailure.INVALID_RESPONSE, start, end, reason_code="invalid_response",
                )
        if capture_eligibility:
            evidence = {'source': SOURCE, 'session': session.isoformat(),
                        'symbols': symbols, 'observed_at': fetched_at.isoformat(),
                        'complete': True, 'pages': eligibility_pages}
            results = {symbol: replace(result, eligibility_evidence=evidence)
                       if result.failure is not None else result
                       for symbol, result in results.items()}
        try:
            provider_timeout("alpaca")
        except SourceFetchError:
            return fail_all(AlpacaBarFailure.TRANSPORT_ERROR, "timeout")
        return results

    def fetch_daily_bar(
        self, ticker: str, session: date, *, now: datetime | None = None
    ) -> AlpacaDailyBarResult:
        """Fetch after XNYS close + 15m, with an end at least 15m in the past.

        ``now`` is a deterministic clock seam. Older sessions end immediately
        before next New York midnight; today's request ends at now minus 15m.
        The single-symbol response must name the exact symbol and contain one
        bar timestamped at the session's New York midnight, with no next page.
        """
        fetched_at = now if now is not None else datetime.now(timezone.utc)
        try:
            if (
                not isinstance(ticker, str)
                or _SYMBOL.fullmatch(ticker) is None
                or type(session) is not date
                or not isinstance(fetched_at, datetime)
                or fetched_at.tzinfo is None
                or fetched_at.utcoffset() is None
            ):
                raise ValueError("invalid request")
            close_at = session_close(session)
            start = datetime.combine(session, time.min, _ET)
            next_midnight = datetime.combine(session + timedelta(days=1), time.min, _ET)
            end = min(
                fetched_at - HISTORICAL_DELAY,
                next_midnight - timedelta(microseconds=1),
            )
        except (ValueError, TypeError, OverflowError):
            return AlpacaDailyBarResult(None, AlpacaBarFailure.INVALID_REQUEST)

        def fail(reason: AlpacaBarFailure) -> AlpacaDailyBarResult:
            return AlpacaDailyBarResult(None, reason, start, end)

        if fetched_at < close_at + HISTORICAL_DELAY:
            return fail(AlpacaBarFailure.SESSION_NOT_READY)
        key = os.environ.get("ALPACA_API_KEY", "").strip()
        secret = os.environ.get("ALPACA_SECRET_KEY", "").strip()
        if not key or not secret:
            return fail(AlpacaBarFailure.MISSING_CREDENTIALS)
        try:
            response = provider_request(
                "alpaca", "GET",
                f"https://data.alpaca.markets/v2/stocks/{ticker}/bars",
                operation="raw_daily_bar", transport=self._get,
                params={
                    "feed": FEED,
                    "adjustment": ADJUSTMENT,
                    "timeframe": TIMEFRAME,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "asof": "-",  # disable automatic historical symbol remapping
                    "currency": "USD",
                    "sort": "asc",
                    "limit": 2,  # expose unexpected extra rows; never truncate to one
                },
                headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
                timeout=REQUEST_TIMEOUT,
                allow_redirects=False,
            )
        except SourceFetchError as error:
            return fail(AlpacaBarFailure.HTTP_ERROR if error.http_status is not None
                        else AlpacaBarFailure.TRANSPORT_ERROR)
        except Exception:
            # Provider exceptions may contain URLs, headers or body text. Never
            # retain, interpolate, log or chain them into persisted evidence.
            return fail(AlpacaBarFailure.TRANSPORT_ERROR)
        try:
            if response.status_code != 200:
                return fail(AlpacaBarFailure.HTTP_ERROR)
            payload = response.json(parse_float=Decimal)
            if (
                not isinstance(payload, dict)
                or payload.get("symbol") != ticker
                or "next_page_token" not in payload
                or payload["next_page_token"] is not None
                or not isinstance(payload.get("bars"), list)
                or len(payload["bars"]) != 1
            ):
                return fail(AlpacaBarFailure.INVALID_RESPONSE)
            row = payload["bars"][0]
            if not isinstance(row, dict) or not isinstance(row.get("t"), str):
                return fail(AlpacaBarFailure.INVALID_RESPONSE)
            stamp = datetime.fromisoformat(row["t"].replace("Z", "+00:00"))
            if stamp.tzinfo is None or stamp.utcoffset() is None or stamp != start:
                return fail(AlpacaBarFailure.INVALID_RESPONSE)
            _validate_activity(row)
            op, high, low, close = (_price(row[key]) for key in ("o", "h", "l", "c"))
            if high < max(op, close) or low > min(op, close) or high < low:
                return fail(AlpacaBarFailure.INVALID_RESPONSE)
            bar = MarketBar(
                ticker, session, op, high, low, close, SOURCE, fetched_at, False
            )
            return AlpacaDailyBarResult(
                bar,
                None,
                start,
                end,
                stamp,
                response_symbol=ticker,
                row_count=1,
                pagination_complete=True,
            )
        except (KeyError, ValueError, TypeError, InvalidOperation, OverflowError):
            return fail(AlpacaBarFailure.INVALID_RESPONSE)
        finally:
            response.close()
