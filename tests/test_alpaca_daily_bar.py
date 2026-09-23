"""Offline contract regressions, including observed 2026-09-22 SIP bars."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import Mock

import pytest
import requests

from tradingagents.strategies.execution.alpaca_daily_bar import (
    AlpacaBarFailure,
    AlpacaDailyBarResult,
    AlpacaHistoricalSIPSource,
    REQUEST_TIMEOUT,
    SOURCE,
)

SESSION = date(2026, 9, 22)
NOW = datetime(2026, 9, 22, 22, tzinfo=timezone.utc)
# Actual historical SIP/raw responses observed during the Sep 22 investigation.
PRICES = {
    "BRC": ("84.51", "84.51", "83.0675", "83.39"),
    "ICE": ("155.92", "155.92", "152.505", "152.93"),
}


def payload(ticker="BRC"):
    return {
        "symbol": ticker,
        "bars": [dict(t="2026-09-22T04:00:00Z", **dict(zip("ohlc", PRICES[ticker])))],
        "next_page_token": None,
    }


def source_for(body=None, *, status=200):
    response = Mock(status_code=status)
    response.json.return_value = payload() if body is None else body
    get = Mock(return_value=response)
    return AlpacaHistoricalSIPSource(get=get), get


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "test-key-do-not-persist")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret-do-not-persist")


@pytest.mark.parametrize("ticker", ["BRC", "ICE"])
def test_sep22_exact_historical_sip_raw_fixture(ticker):
    source, get = source_for(payload(ticker))
    result = source.fetch_daily_bar(ticker, SESSION, now=NOW)
    assert result.failure is None
    bar = result.bar
    assert (bar.open, bar.high, bar.low, bar.close) == tuple(map(Decimal, PRICES[ticker]))
    assert (bar.ticker, bar.session, bar.source, bar.adjusted, bar.fetched_at) == (
        ticker, SESSION, SOURCE, False, NOW
    )
    assert (result.response_symbol, result.row_count, result.pagination_complete) == (ticker, 1, True)
    assert (result.feed, result.adjustment, result.timeframe) == ("sip", "raw", "1Day")
    assert result.bar_timestamp == result.request_start
    assert result.request_end == NOW - timedelta(minutes=15)
    get.assert_called_once()
    args, kwargs = get.call_args
    assert args == (f"https://data.alpaca.markets/v2/stocks/{ticker}/bars",)
    assert kwargs["params"] == {
        "feed": "sip", "adjustment": "raw", "timeframe": "1Day", "limit": 2,
        "start": "2026-09-22T00:00:00-04:00", "end": "2026-09-22T21:45:00+00:00",
        "asof": "-", "currency": "USD", "sort": "asc",
    }
    assert kwargs["timeout"] == REQUEST_TIMEOUT
    assert kwargs["allow_redirects"] is False
    get.return_value.close.assert_called_once()
    assert "test-key" not in repr(result) and "test-secret" not in repr(result)


@pytest.mark.parametrize("missing", ["ALPACA_API_KEY", "ALPACA_SECRET_KEY"])
def test_missing_credentials_fail_before_network(monkeypatch, missing):
    monkeypatch.setenv(missing, " ")
    source, get = source_for()
    assert source.fetch_daily_bar("BRC", SESSION, now=NOW).failure == AlpacaBarFailure.MISSING_CREDENTIALS
    get.assert_not_called()


@pytest.mark.parametrize("now", [NOW.replace(hour=19), NOW.replace(hour=20, minute=14, second=59)])
def test_require_session_close_plus_fifteen_minutes(now):
    source, get = source_for()
    assert source.fetch_daily_bar("BRC", SESSION, now=now).failure == AlpacaBarFailure.SESSION_NOT_READY
    get.assert_not_called()


def test_exact_delay_boundary_and_older_session_end():
    source, _ = source_for()
    at_boundary = source.fetch_daily_bar("BRC", SESSION, now=NOW.replace(hour=20, minute=15))
    assert at_boundary.failure is None
    assert at_boundary.request_end == NOW.replace(hour=20)
    older = source.fetch_daily_bar("BRC", SESSION, now=NOW + timedelta(days=1))
    assert older.failure is None
    assert older.request_end.isoformat() == "2026-09-22T23:59:59.999999-04:00"


@pytest.mark.parametrize("ticker,session,now", [
    ("brc", SESSION, NOW), (" BRC", SESSION, NOW), ("BRC/../ICE", SESSION, NOW),
    ("BRC", date(2026, 9, 20), NOW), ("BRC", SESSION, NOW.replace(tzinfo=None)),
    ("BRC", "2026-09-22", NOW), ("BRC", NOW, NOW),
])
def test_invalid_scope_never_requests(ticker, session, now):
    source, get = source_for()
    assert source.fetch_daily_bar(ticker, session, now=now).failure == AlpacaBarFailure.INVALID_REQUEST
    get.assert_not_called()


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(symbol="ICE"),
    lambda p: p.update(next_page_token="more"),
    lambda p: p.update(next_page_token=""),
    lambda p: p.pop("next_page_token"),
    lambda p: p.update(bars=[]),
    lambda p: p["bars"].append(deepcopy(p["bars"][0])),
    lambda p: p.update(bars={"BRC": p["bars"]}),
    lambda p: p["bars"][0].update(t="2026-09-21T04:00:00Z"),
    lambda p: p["bars"][0].update(t="2026-09-22T13:30:00Z"),
    lambda p: p["bars"][0].update(t="2026-09-22T00:00:00"),
    lambda p: p["bars"][0].pop("t"),
    lambda p: p["bars"][0].pop("o"),
    lambda p: p["bars"][0].update(h="84.30500030517578"),  # original Yahoo BRC defect
    lambda p: p["bars"][0].update(l="84"),
])
def test_malformed_ambiguous_incoherent_evidence_is_rejected(mutation):
    body = payload()
    mutation(body)
    source, get = source_for(body)
    result = source.fetch_daily_bar("BRC", SESSION, now=NOW)
    assert result.bar is None and result.failure == AlpacaBarFailure.INVALID_RESPONSE
    assert not result.pagination_complete
    get.assert_called_once()  # no retry / alternative feed


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "0", "-1", None, True, [], {}])
@pytest.mark.parametrize("field", "ohlc")
def test_every_price_is_finite_positive_decimal(field, value):
    body = payload()
    body["bars"][0][field] = value
    source, _ = source_for(body)
    assert source.fetch_daily_bar("BRC", SESSION, now=NOW).failure == AlpacaBarFailure.INVALID_RESPONSE


@pytest.mark.parametrize("status", [301, 401, 403, 422, 429, 500])
def test_http_failures_do_not_fallback_or_parse_provider_body(status):
    source, get = source_for(status=status)
    result = source.fetch_daily_bar("BRC", SESSION, now=NOW)
    assert result.failure == AlpacaBarFailure.HTTP_ERROR
    get.assert_called_once()
    get.return_value.json.assert_not_called()


def test_transport_error_is_sanitized_and_has_no_fallback(caplog):
    get = Mock(side_effect=requests.Timeout("test-secret-do-not-persist"))
    result = AlpacaHistoricalSIPSource(get=get).fetch_daily_bar("BRC", SESSION, now=NOW)
    assert result.failure == AlpacaBarFailure.TRANSPORT_ERROR
    assert "test-secret" not in repr(result) + caplog.text
    get.assert_called_once()


def test_invalid_json_is_sanitized_and_closed():
    source, get = source_for()
    get.return_value.json.side_effect = ValueError("test-secret-do-not-persist")
    result = source.fetch_daily_bar("BRC", SESSION, now=NOW)
    assert result.failure == AlpacaBarFailure.INVALID_RESPONSE
    assert "test-secret" not in repr(result)
    get.return_value.close.assert_called_once()


def test_result_requires_exactly_one_bar_or_failure():
    with pytest.raises(ValueError):
        AlpacaDailyBarResult(None, None)
