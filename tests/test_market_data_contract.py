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
    CandidateBarAttempt,
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


def _candidate_frame(aapl_close: object, msft_close: object) -> pd.DataFrame:
    columns = pd.MultiIndex.from_product(
        [["Open", "High", "Low", "Close"], ["AAPL", "MSFT"]]
    )
    return pd.DataFrame(
        [[100, 200, 103, 203, 99, 199, aapl_close, msft_close]],
        index=pd.DatetimeIndex([_SESSION.isoformat()]),
        columns=columns,
    )


@patch("tradingagents.strategies.execution.price_source.yf.download")
def test_candidate_bars_validate_initial_fetch_against_post_fetch_clock(mock_download):
    columns = pd.MultiIndex.from_product([["Open", "High", "Low", "Close"], ["AAPL"]])
    mock_download.return_value = pd.DataFrame(
        [[100, 103, 99, 102]],
        index=pd.DatetimeIndex([_SESSION.isoformat()]),
        columns=columns,
    )
    processed_at = _AS_OF
    clock = [
        processed_at + timedelta(seconds=1),
        processed_at + timedelta(seconds=2),
        processed_at + timedelta(seconds=3),
    ]
    source = YFinancePriceSource(now=lambda: clock.pop(0))

    resolution = source.resolve_candidate_daily_bars(
        ["AAPL"], _SESSION, processed_at, timedelta(hours=24)
    )

    assert mock_download.call_count == 1
    assert set(resolution.bars) == {("AAPL", _SESSION)}
    assert resolution.attempts[0].validation_error is None
    assert resolution.recovered_tickers == frozenset()


@patch("tradingagents.strategies.execution.price_source.yf.download")
def test_candidate_bars_validate_recovery_fetch_against_post_fetch_clock(mock_download):
    columns = pd.MultiIndex.from_product([["Open", "High", "Low", "Close"], ["AAPL"]])
    mock_download.return_value = pd.DataFrame(
        [[100, 103, 99, 102]],
        index=pd.DatetimeIndex([_SESSION.isoformat()]),
        columns=columns,
    )
    processed_at = _AS_OF
    clock = [
        datetime(2026, 7, 31, 19, tzinfo=timezone.utc),
        processed_at + timedelta(seconds=1),
        processed_at + timedelta(seconds=2),
        processed_at + timedelta(seconds=3),
    ]
    source = YFinancePriceSource(now=lambda: clock.pop(0))

    resolution = source.resolve_candidate_daily_bars(
        ["AAPL"], _SESSION, processed_at, timedelta(hours=24)
    )

    assert mock_download.call_count == 2
    assert set(resolution.bars) == {("AAPL", _SESSION)}
    assert resolution.attempts[0].validation_error == "pre-close AAPL/2026-07-31"
    assert resolution.attempts[1].validation_error is None
    assert resolution.recovered_tickers == frozenset({"AAPL"})


@patch("tradingagents.strategies.execution.price_source.yf.download")
def test_candidate_bars_refreshes_only_invalid_ticker_and_recovers_it(mock_download):
    mock_download.side_effect = [
        _candidate_frame(102, 204),
        _candidate_frame(202, 202),
    ]
    source = YFinancePriceSource(now=lambda: _AS_OF)

    resolution = source.resolve_candidate_daily_bars(
        ["AAPL", "MSFT"], _SESSION, _AS_OF, timedelta(hours=24)
    )

    assert mock_download.call_count == 2
    assert set(resolution.bars) == {("AAPL", _SESSION), ("MSFT", _SESSION)}
    assert [(attempt.ticker, attempt.attempt, attempt.validation_error) for attempt in resolution.attempts] == [
        ("AAPL", 1, None),
        ("MSFT", 1, "incoherent MSFT/2026-07-31"),
        ("MSFT", 2, None),
    ]
    assert resolution.recovered_tickers == frozenset({"MSFT"})
    assert resolution.quarantined_tickers == frozenset()


@patch("tradingagents.strategies.execution.price_source.yf.download")
def test_candidate_bars_retry_missing_initial_session_row_and_recovers(mock_download):
    columns = pd.MultiIndex.from_product([["Open", "High", "Low", "Close"], ["AAPL"]])
    mock_download.side_effect = [
        pd.DataFrame(index=pd.DatetimeIndex([]), columns=columns),
        pd.DataFrame(
            [[100, 103, 99, 102]],
            index=pd.DatetimeIndex([_SESSION.isoformat()]),
            columns=columns,
        ),
    ]
    source = YFinancePriceSource(now=lambda: _AS_OF)

    resolution = source.resolve_candidate_daily_bars(
        ["AAPL"], _SESSION, _AS_OF, timedelta(hours=24)
    )

    assert mock_download.call_count == 2
    assert resolution.bars[("AAPL", _SESSION)].close == Decimal("102")
    assert resolution.recovered_tickers == frozenset({"AAPL"})
    assert resolution.attempts[0].validation_error == "missing AAPL/2026-07-31"
    assert resolution.attempts[1].validation_error is None


@patch("tradingagents.strategies.execution.price_source.yf.download")
def test_candidate_bars_quarantines_only_persistently_invalid_ticker(mock_download):
    mock_download.side_effect = [
        _candidate_frame(102, 204),
        _candidate_frame(200, 204),
    ]
    source = YFinancePriceSource(now=lambda: _AS_OF)

    resolution = source.resolve_candidate_daily_bars(
        ["AAPL", "MSFT"], _SESSION, _AS_OF, timedelta(hours=24)
    )

    assert set(resolution.bars) == {("AAPL", _SESSION)}
    assert resolution.recovered_tickers == frozenset()
    assert resolution.quarantined_tickers == frozenset({"MSFT"})
    msft_attempts = [attempt for attempt in resolution.attempts if attempt.ticker == "MSFT"]
    assert msft_attempts == [
        CandidateBarAttempt(
            ticker="MSFT",
            session=_SESSION,
            attempt=1,
            source="yfinance",
            fetched_at=_AS_OF,
            open=Decimal("200"),
            high=Decimal("203"),
            low=Decimal("199"),
            close=Decimal("204"),
            validation_error="incoherent MSFT/2026-07-31",
        ),
        CandidateBarAttempt(
            ticker="MSFT",
            session=_SESSION,
            attempt=2,
            source="yfinance",
            fetched_at=_AS_OF,
            open=Decimal("200"),
            high=Decimal("203"),
            low=Decimal("199"),
            close=Decimal("204"),
            validation_error="incoherent MSFT/2026-07-31",
        ),
    ]


@patch("tradingagents.strategies.execution.price_source.yf.download")
def test_candidate_bars_refreshes_each_invalid_ticker_individually(mock_download):
    mock_download.side_effect = [
        _candidate_frame(104, 204),
        _candidate_frame(102, 202),
        _candidate_frame(102, 202),
    ]
    source = YFinancePriceSource(now=lambda: _AS_OF)

    resolution = source.resolve_candidate_daily_bars(
        ["AAPL", "MSFT"], _SESSION, _AS_OF, timedelta(hours=24)
    )

    assert set(resolution.bars) == {("AAPL", _SESSION), ("MSFT", _SESSION)}
    assert [call.args[0] for call in mock_download.call_args_list] == [
        ["AAPL", "MSFT"],
        ["AAPL"],
        ["MSFT"],
    ]


@patch("tradingagents.strategies.execution.price_source.yf.download")
def test_candidate_flat_multi_ticker_batch_retries_each_ticker_then_quarantines(
    mock_download,
):
    mock_download.side_effect = [
        pd.DataFrame(
            [[100, 103, 99, 102]],
            index=pd.DatetimeIndex([_SESSION.isoformat()]),
            columns=["Open", "High", "Low", "Close"],
        ),
        pd.DataFrame(
            [[100, 103, 99, 102]],
            index=pd.DatetimeIndex([_SESSION.isoformat()]),
            columns=["Open", "High", "Low", "Close"],
        ),
        pd.DataFrame(
            [[200, 203, 199, 204]],
            index=pd.DatetimeIndex([_SESSION.isoformat()]),
            columns=["Open", "High", "Low", "Close"],
        ),
    ]
    source = YFinancePriceSource(now=lambda: _AS_OF)

    resolution = source.resolve_candidate_daily_bars(
        ["AAPL", "MSFT"], _SESSION, _AS_OF, timedelta(hours=24)
    )

    assert [call.args[0] for call in mock_download.call_args_list] == [
        ["AAPL", "MSFT"],
        ["AAPL"],
        ["MSFT"],
    ]
    assert resolution.recovered_tickers == frozenset({"AAPL"})
    assert resolution.quarantined_tickers == frozenset({"MSFT"})
    assert set(resolution.bars) == {("AAPL", _SESSION)}
    assert [
        (attempt.ticker, attempt.attempt, attempt.validation_error)
        for attempt in resolution.attempts
    ] == [
        (
            "AAPL",
            1,
            "ambiguous flat columns for multiple requested tickers",
        ),
        (
            "MSFT",
            1,
            "ambiguous flat columns for multiple requested tickers",
        ),
        ("AAPL", 2, None),
        ("MSFT", 2, "incoherent MSFT/2026-07-31"),
    ]


@patch("tradingagents.strategies.execution.price_source.yf.download")
def test_candidate_bars_retry_pre_close_observation_and_recover(mock_download):
    columns = pd.MultiIndex.from_product([["Open", "High", "Low", "Close"], ["AAPL"]])
    mock_download.return_value = pd.DataFrame(
        [[100, 103, 99, 102]],
        index=pd.DatetimeIndex([_SESSION.isoformat()]),
        columns=columns,
    )
    clock = [
        datetime(2026, 7, 31, 19, tzinfo=timezone.utc),
        _AS_OF,
        _AS_OF,
        _AS_OF,
    ]
    source = YFinancePriceSource(now=lambda: clock.pop(0))

    resolution = source.resolve_candidate_daily_bars(
        ["AAPL"], _SESSION, _AS_OF, timedelta(hours=24)
    )

    assert mock_download.call_count == 2
    assert set(resolution.bars) == {("AAPL", _SESSION)}
    assert resolution.recovered_tickers == frozenset({"AAPL"})
    assert resolution.attempts[0].validation_error == "pre-close AAPL/2026-07-31"
    assert resolution.attempts[1].validation_error is None


@patch("tradingagents.strategies.execution.price_source.yf.download")
def test_candidate_bars_preserves_malformed_refresh_attempt_evidence(mock_download):
    columns = pd.MultiIndex.from_product([["Open", "High", "Low", "Close"], ["AAPL"]])
    mock_download.side_effect = [
        pd.DataFrame(
            [[100, 103, 99, 104]],
            index=pd.DatetimeIndex([_SESSION.isoformat()]),
            columns=columns,
        ),
        pd.DataFrame(
            [[100, 103, 99, "not-a-close"]],
            index=pd.DatetimeIndex([_SESSION.isoformat()]),
            columns=columns,
        ),
    ]
    source = YFinancePriceSource(now=lambda: _AS_OF)

    resolution = source.resolve_candidate_daily_bars(
        ["AAPL"], _SESSION, _AS_OF, timedelta(hours=24)
    )

    assert resolution.bars == {}
    assert resolution.quarantined_tickers == frozenset({"AAPL"})
    assert resolution.attempts[1] == CandidateBarAttempt(
        ticker="AAPL",
        session=_SESSION,
        attempt=2,
        source="yfinance",
        fetched_at=_AS_OF,
        open=Decimal("100"),
        high=Decimal("103"),
        low=Decimal("99"),
        close=None,
        validation_error="invalid AAPL/2026-07-31 close",
    )


@patch("tradingagents.strategies.execution.price_source.yf.download")
def test_refresh_daily_bars_bypasses_raw_frame_cache(mock_download):
    columns = pd.MultiIndex.from_product([["Open", "High", "Low", "Close"], ["AAPL"]])
    mock_download.return_value = pd.DataFrame(
        [[100, 103, 99, 102]],
        index=pd.DatetimeIndex([_SESSION.isoformat()]),
        columns=columns,
    )
    source = YFinancePriceSource(now=lambda: _AS_OF)

    source.get_daily_bars(["AAPL"], _SESSION, _SESSION)
    source.refresh_daily_bars(["AAPL"], _SESSION, _SESSION)

    assert mock_download.call_count == 2


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
