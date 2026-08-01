"""Contract tests for authoritative XNYS market-data boundaries."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pandas as pd
import pytest

from tradingagents.strategies.execution.models import MarketBar
from tradingagents.strategies.execution.price_source import (
    BarValidationError,
    CorporateActionValidationError,
    YFinancePriceSource,
    validate_required_bars,
)
from tradingagents.strategies.orchestration.trading_calendar import (
    is_session,
    next_session,
    previous_session,
    session_close,
    session_open,
)


_SESSION = date(2026, 7, 31)
_AS_OF = datetime(2026, 7, 31, 22, tzinfo=timezone.utc)


def _bar(**overrides: object) -> MarketBar:
    values: dict[str, object] = {
        "ticker": "AAPL",
        "session": _SESSION,
        "open": Decimal("100"),
        "high": Decimal("103"),
        "low": Decimal("99"),
        "close": Decimal("102"),
        "source": "yfinance",
        "fetched_at": _AS_OF,
        "adjusted": False,
    }
    values.update(overrides)
    return MarketBar(**values)  # type: ignore[arg-type]


def test_xnys_weekend_holiday_and_early_close():
    assert next_session(date(2026, 7, 2)) == date(2026, 7, 6)
    assert not is_session(date(2026, 7, 3))
    assert previous_session(date(2026, 7, 4)) == date(2026, 7, 2)
    assert session_open(date(2026, 11, 27)).utcoffset() == timedelta(0)
    assert session_close(date(2026, 11, 27)).hour == 18


@patch("tradingagents.strategies.execution.price_source.yf.download")
def test_yfinance_end_is_inclusive_and_raw(mock_download):
    columns = pd.MultiIndex.from_product([["Open", "High", "Low", "Close"], ["AAPL"]])
    mock_download.return_value = pd.DataFrame(
        [[100, 103, 99, 102]],
        index=pd.DatetimeIndex(["2026-07-31"]),
        columns=columns,
    )
    source = YFinancePriceSource(now=lambda: _AS_OF)
    bars = source.get_daily_bars(["AAPL"], _SESSION, _SESSION)

    kwargs = mock_download.call_args.kwargs
    assert kwargs["end"] == "2026-08-01"
    assert kwargs["auto_adjust"] is False
    assert kwargs["actions"] is True
    assert kwargs["timeout"] == 30
    assert bars[("AAPL", _SESSION)].close == Decimal("102")
    assert bars[("AAPL", _SESSION)].adjusted is False


@patch("tradingagents.strategies.execution.price_source.yf.download")
def test_yfinance_rejects_terminal_session_absence(mock_download):
    columns = pd.MultiIndex.from_product([["Open", "High", "Low", "Close"], ["AAPL"]])
    mock_download.return_value = pd.DataFrame(
        [[100, 103, 99, 102]],
        index=pd.DatetimeIndex(["2026-07-30"]),
        columns=columns,
    )

    with pytest.raises(BarValidationError, match="missing AAPL/2026-07-31"):
        YFinancePriceSource(now=lambda: _AS_OF).get_daily_bars(
            ["AAPL"], _SESSION, _SESSION
        )


@patch("tradingagents.strategies.execution.price_source.yf.download")
def test_cached_raw_bars_retain_download_timestamp_and_fail_stale(mock_download):
    columns = pd.MultiIndex.from_product([["Open", "High", "Low", "Close"], ["AAPL"]])
    mock_download.return_value = pd.DataFrame(
        [[100, 103, 99, 102]],
        index=pd.DatetimeIndex(["2026-07-31"]),
        columns=columns,
    )
    clock = [_AS_OF]
    source = YFinancePriceSource(now=lambda: clock[0])

    source.get_daily_bars(["AAPL"], _SESSION, _SESSION)
    clock[0] += timedelta(hours=24, seconds=1)

    with pytest.raises(BarValidationError, match="stale AAPL/2026-07-31"):
        source.get_daily_bars(["AAPL"], _SESSION, _SESSION)
    assert mock_download.call_count == 1


@patch("tradingagents.strategies.execution.price_source.yf.download")
def test_single_ticker_flat_ohlc_response_is_accepted(mock_download):
    mock_download.return_value = pd.DataFrame(
        [[100, 103, 99, 102]],
        index=pd.DatetimeIndex(["2026-07-31"]),
        columns=["Open", "High", "Low", "Close"],
    )

    bars = YFinancePriceSource(now=lambda: _AS_OF).get_daily_bars(
        ["AAPL"], _SESSION, _SESSION
    )

    assert bars[("AAPL", _SESSION)].close == Decimal("102")


@patch("tradingagents.strategies.execution.price_source.yf.download")
def test_multi_ticker_flat_ohlc_response_fails_closed(mock_download):
    mock_download.return_value = pd.DataFrame(
        [[100, 103, 99, 102]],
        index=pd.DatetimeIndex(["2026-07-31"]),
        columns=["Open", "High", "Low", "Close"],
    )

    with pytest.raises(BarValidationError, match="ambiguous flat columns"):
        YFinancePriceSource(now=lambda: _AS_OF).get_daily_bars(
            ["AAPL", "MSFT"], _SESSION, _SESSION
        )


@patch("tradingagents.strategies.execution.price_source.yf.download")
def test_benchmark_closes_are_total_return_adjusted(mock_download):
    columns = pd.MultiIndex.from_product([["Close"], ["SPY", "BIL"]])
    mock_download.return_value = pd.DataFrame(
        [[550.0, 91.5]],
        index=pd.DatetimeIndex(["2026-07-31"]),
        columns=columns,
    )
    source = YFinancePriceSource(now=lambda: _AS_OF)
    closes = source.get_total_return_closes(["SPY", "BIL"], _SESSION, _SESSION)

    kwargs = mock_download.call_args.kwargs
    assert kwargs["end"] == "2026-08-01"
    assert kwargs["auto_adjust"] is True
    assert kwargs["actions"] is False
    observation = closes[("SPY", _SESSION)]
    assert observation.close == Decimal("550.0")
    assert observation.source == "yfinance-adjusted"
    assert observation.fetched_at == _AS_OF


@patch("tradingagents.strategies.execution.price_source.yf.download")
def test_multi_symbol_flat_adjusted_close_response_fails_closed(mock_download):
    mock_download.return_value = pd.DataFrame(
        [[550.0]],
        index=pd.DatetimeIndex(["2026-07-31"]),
        columns=["Close"],
    )

    with pytest.raises(BarValidationError, match="ambiguous flat columns"):
        YFinancePriceSource(now=lambda: _AS_OF).get_total_return_closes(
            ["SPY", "BIL"], _SESSION, _SESSION
        )


@pytest.mark.parametrize("close", [0, -1])
@patch("tradingagents.strategies.execution.price_source.yf.download")
def test_nonpositive_adjusted_benchmark_close_fails_closed(mock_download, close):
    columns = pd.MultiIndex.from_product([["Close"], ["SPY"]])
    mock_download.return_value = pd.DataFrame(
        [[close]],
        index=pd.DatetimeIndex(["2026-07-31"]),
        columns=columns,
    )

    with pytest.raises(BarValidationError, match="invalid SPY/2026-07-31 close"):
        YFinancePriceSource(now=lambda: _AS_OF).get_total_return_closes(
            ["SPY"], _SESSION, _SESSION
        )


@pytest.mark.parametrize(
    ("bar", "error"),
    [
        (_bar(adjusted=True), "adjusted"),
        (_bar(fetched_at=datetime(2026, 7, 31, 22)), "naive"),
        (_bar(fetched_at=_AS_OF + timedelta(seconds=1)), "future"),
        (_bar(fetched_at=_AS_OF - timedelta(hours=24, seconds=1)), "stale"),
        (_bar(ticker="MSFT"), "mismatched"),
        (_bar(close=Decimal("0")), "invalid"),
        (_bar(close=Decimal("NaN")), "invalid"),
        (_bar(open=Decimal("104")), "incoherent"),
    ],
)
def test_invalid_required_bars_fail_closed(bar: MarketBar, error: str):
    with pytest.raises(BarValidationError, match=error):
        validate_required_bars({("AAPL", _SESSION): bar}, {"AAPL"}, _SESSION, _AS_OF)


def test_missing_required_bar_fails_closed():
    with pytest.raises(BarValidationError, match="missing"):
        validate_required_bars({}, {"AAPL"}, _SESSION, _AS_OF)


@patch("tradingagents.strategies.execution.price_source.yf.download")
def test_corporate_actions_are_verified_and_stably_identified(mock_download):
    columns = pd.MultiIndex.from_product([["Stock Splits", "Dividends"], ["AAPL"]])
    mock_download.return_value = pd.DataFrame(
        [[2.0, 0.24]],
        index=pd.DatetimeIndex(["2026-07-31"]),
        columns=columns,
    )
    source = YFinancePriceSource(now=lambda: _AS_OF)

    first = source.get_corporate_actions(["AAPL"], _SESSION)
    second = source.get_corporate_actions(["AAPL"], _SESSION)

    assert [
        (action.action_type, action.ratio, action.cash_per_share) for action in first
    ] == [
        ("split", Decimal("2.0"), None),
        ("cash_dividend", None, Decimal("0.24")),
    ]
    assert all(action.verified for action in first)
    assert [action.action_id for action in first] == [
        action.action_id for action in second
    ]


@patch("tradingagents.strategies.execution.price_source.yf.download")
def test_unparseable_nonzero_corporate_action_fails_closed(mock_download):
    columns = pd.MultiIndex.from_product([["Stock Splits"], ["AAPL"]])
    mock_download.return_value = pd.DataFrame(
        [["not-a-ratio"]],
        index=pd.DatetimeIndex(["2026-07-31"]),
        columns=columns,
    )

    with pytest.raises(CorporateActionValidationError, match="AAPL/2026-07-31"):
        YFinancePriceSource(now=lambda: _AS_OF).get_corporate_actions(
            ["AAPL"], _SESSION
        )
