"""Read-only, fail-closed historical SIP evidence for an exact US equity session.

Only this adapter's explicit SIP/raw request is trusted; it has no IEX, latest,
SDK-default, trading, retry, or persistence path. The result is evidence for a
caller to govern, not authorization to replace a primary provider automatically.

Provider contract:
https://docs.alpaca.markets/us/reference/stockbarsingle-1
https://docs.alpaca.markets/us/docs/market-data-faq
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Callable
from zoneinfo import ZoneInfo

import requests

from tradingagents.strategies.execution.models import MarketBar
from tradingagents.strategies.metrics.calendar import XNYSCalendar

SOURCE = "alpaca-sip-1d-raw"
FEED = "sip"
ADJUSTMENT = "raw"
TIMEFRAME = "1Day"
HISTORICAL_DELAY = timedelta(minutes=15)
REQUEST_TIMEOUT = (5.0, 20.0)  # connect/read; no retries or redirects
_ET = ZoneInfo("America/New_York")
_SYMBOL = re.compile(r"[A-Z][A-Z0-9.-]{0,15}\Z")


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

    def __post_init__(self) -> None:
        if (self.bar is None) == (self.failure is None):
            raise ValueError("exactly one bar or failure is required")


def _price(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("invalid price")
    result = Decimal(str(value))
    if not result.is_finite() or result <= 0:
        raise ValueError("invalid price")
    return result


class AlpacaHistoricalSIPSource:
    """One bounded historical request per symbol; credentials come from env."""

    def __init__(self, *, get: Callable | None = None) -> None:
        self._get = get or requests.get

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
            close_at = XNYSCalendar().session_close(session)
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
            response = self._get(
                f"https://data.alpaca.markets/v2/stocks/{ticker}/bars",
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
