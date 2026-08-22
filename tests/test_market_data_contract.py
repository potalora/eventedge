"""Contract tests for authoritative XNYS market-data boundaries."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from zoneinfo import ZoneInfo
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
_GOVERNED_SESSION = date(2026, 8, 10)
_GOVERNED_FETCHED_AT = datetime(2026, 8, 10, 22, 1, 31, tzinfo=timezone.utc)
_GOVERNED_PROCESSED_AT = datetime(2026, 8, 10, 22, 5, tzinfo=timezone.utc)
_ET = ZoneInfo("America/New_York")

_ESS_DAILY = {
    "Open": 286.2099914550781,
    "High": 285.82501220703125,
    "Low": 281.5299987792969,
    "Close": 283.2099914550781,
}

_ESS_60M_ROWS = (
    (286.2099914550781, 286.2099914550781, 284.3500061035156, 284.79998779296875),
    (284.79998779296875, 285.82501220703125, 284.4700012207031, 285.5),
    (285.5, 285.6000061035156, 283.8999938964844, 284.1000061035156),
    (284.1000061035156, 284.29998779296875, 282.75, 283.0),
    (283.0, 283.5, 282.1000061035156, 282.45001220703125),
    (282.45001220703125, 283.45001220703125, 281.5299987792969, 282.8999938964844),
    (282.8999938964844, 283.5, 282.70001220703125, 283.2099914550781),
)


def _daily_frame(
    values_by_ticker: dict[str, dict[str, object]],
    *,
    session: date = _GOVERNED_SESSION,
) -> pd.DataFrame:
    tickers = list(values_by_ticker)
    columns = pd.MultiIndex.from_product(
        [["Open", "High", "Low", "Close"], tickers]
    )
    row = [
        values_by_ticker[ticker][field]
        for field in ("Open", "High", "Low", "Close")
        for ticker in tickers
    ]
    return pd.DataFrame(
        [row], index=pd.DatetimeIndex([session.isoformat()]), columns=columns
    )


def _hourly_frame(
    *,
    starts: list[datetime] | None = None,
    rows: list[tuple[object, object, object, object]] | None = None,
    ticker: str | None = None,
) -> pd.DataFrame:
    starts = starts or [
        datetime(2026, 8, 10, hour, 30, tzinfo=_ET) for hour in range(9, 16)
    ]
    rows = rows or list(_ESS_60M_ROWS)
    columns: object = ["Open", "High", "Low", "Close"]
    if ticker is not None:
        columns = pd.MultiIndex.from_product(
            [["Open", "High", "Low", "Close"], [ticker]]
        )
    return pd.DataFrame(rows, index=pd.DatetimeIndex(starts), columns=columns)


def _resolve_ess(mock_download, hourly: pd.DataFrame):
    mock_download.side_effect = [_daily_frame({"ESS": _ESS_DAILY}), hourly]
    return YFinancePriceSource(
        now=lambda: _GOVERNED_FETCHED_AT
    ).resolve_governed_daily_bars(
        ["ESS"],
        _GOVERNED_SESSION,
        processed_at=_GOVERNED_PROCESSED_AT,
    )


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
def test_governed_resolver_reconstructs_exact_ess_incident(mock_download):
    invalid_daily = MarketBar(
        ticker="ESS",
        session=_GOVERNED_SESSION,
        open=Decimal("286.2099914550781"),
        high=Decimal("285.82501220703125"),
        low=Decimal("281.5299987792969"),
        close=Decimal("283.2099914550781"),
        source="yfinance",
        fetched_at=_GOVERNED_FETCHED_AT,
        adjusted=False,
    )
    with pytest.raises(BarValidationError, match="incoherent ESS/2026-08-10"):
        validate_required_bars(
            {("ESS", _GOVERNED_SESSION): invalid_daily},
            {"ESS"},
            _GOVERNED_SESSION,
            _GOVERNED_PROCESSED_AT,
        )

    resolution = _resolve_ess(mock_download, _hourly_frame())

    assert resolution.bars["ESS"] == MarketBar(
        ticker="ESS",
        session=_GOVERNED_SESSION,
        open=Decimal("286.2099914550781"),
        high=Decimal("286.2099914550781"),
        low=Decimal("281.5299987792969"),
        close=Decimal("283.2099914550781"),
        source="yfinance-60m-reconstruction",
        fetched_at=_GOVERNED_FETCHED_AT,
        adjusted=False,
    )
    assert resolution.bars["ESS"].adjusted is False
    assert resolution.failure_map == {}
    assert resolution.attempts["ESS"].validation_error == (
        "incoherent ESS/2026-08-10"
    )
    recovery = resolution.recoveries["ESS"]
    assert recovery.validation_error is None
    assert recovery.observed_starts == recovery.expected_starts
    assert len(recovery.intraday_bars) == 7
    intraday_kwargs = mock_download.call_args_list[1].kwargs
    assert mock_download.call_args_list[0].kwargs["auto_adjust"] is False
    assert intraday_kwargs == {
        "start": "2026-08-10",
        "end": "2026-08-11",
        "interval": "60m",
        "auto_adjust": False,
        "actions": False,
        "prepost": False,
        "repair": False,
        "threads": False,
        "progress": False,
        "timeout": 30,
    }


def _mutated_starts(kind: str) -> list[datetime]:
    starts = [
        datetime(2026, 8, 10, hour, 30, tzinfo=_ET) for hour in range(9, 16)
    ]
    if kind == "missing":
        return starts[:-1]
    if kind == "duplicate":
        return [*starts, starts[-1]]
    if kind == "shifted":
        starts[2] += timedelta(minutes=1)
        return starts
    if kind == "premarket":
        return [datetime(2026, 8, 10, 8, 30, tzinfo=_ET), *starts]
    if kind == "after-hours":
        return [*starts, datetime(2026, 8, 10, 16, 30, tzinfo=_ET)]
    raise AssertionError(kind)


@pytest.mark.parametrize(
    "kind", ["missing", "duplicate", "shifted", "premarket", "after-hours"]
)
@patch("tradingagents.strategies.execution.price_source.yf.download")
def test_governed_recovery_rejects_nonexact_interval_starts(
    mock_download, kind: str
):
    starts = _mutated_starts(kind)
    rows = [
        _ESS_60M_ROWS[index % len(_ESS_60M_ROWS)] for index in range(len(starts))
    ]

    resolution = _resolve_ess(
        mock_download, _hourly_frame(starts=starts, rows=rows)
    )

    assert resolution.bars == {}
    assert resolution.failure_map == {"ESS": "invalid ESS/2026-08-10"}
    assert resolution.recoveries["ESS"].validation_error is not None
    assert mock_download.call_count == 2


@patch("tradingagents.strategies.execution.price_source.yf.download")
def test_governed_recovery_uses_exact_early_close_schedule(mock_download):
    early_session = date(2026, 11, 27)
    fetched_at = datetime(2026, 11, 27, 20, tzinfo=timezone.utc)
    daily = {
        "Open": _ESS_DAILY["Open"],
        "High": _ESS_DAILY["High"],
        "Low": min(row[2] for row in _ESS_60M_ROWS[:4]),
        "Close": _ESS_60M_ROWS[3][3],
    }
    wrong_normal_day_starts = [
        datetime(2026, 11, 27, hour, 30, tzinfo=_ET) for hour in range(9, 16)
    ]
    mock_download.side_effect = [
        _daily_frame({"ESS": daily}, session=early_session),
        _hourly_frame(
            starts=wrong_normal_day_starts,
            rows=list(_ESS_60M_ROWS),
        ),
    ]

    resolution = YFinancePriceSource(now=lambda: fetched_at).resolve_governed_daily_bars(
        ["ESS"], early_session, processed_at=fetched_at
    )

    expected = tuple(
        datetime(2026, 11, 27, hour, 30, tzinfo=_ET) for hour in range(9, 13)
    )
    assert resolution.recoveries["ESS"].expected_starts == expected
    assert resolution.bars == {}


@pytest.mark.parametrize("failure", ["nonpositive", "nonfinite", "incoherent"])
@patch("tradingagents.strategies.execution.price_source.yf.download")
def test_governed_recovery_rejects_invalid_intraday_ohlc(
    mock_download, failure: str
):
    rows = [list(row) for row in _ESS_60M_ROWS]
    if failure == "nonpositive":
        rows[2][0] = 0
    elif failure == "nonfinite":
        rows[2][1] = float("nan")
    else:
        rows[2][3] = rows[2][1] + 1

    resolution = _resolve_ess(
        mock_download,
        _hourly_frame(rows=[tuple(row) for row in rows]),
    )

    assert resolution.bars == {}
    assert resolution.failure_map == {"ESS": "invalid ESS/2026-08-10"}
    assert resolution.recoveries["ESS"].reconstructed is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("Open", 286.0),
        ("Close", 283.0),
        ("Low", 281.0),
    ],
)
@patch("tradingagents.strategies.execution.price_source.yf.download")
def test_governed_recovery_requires_daily_agreement(
    mock_download, field: str, value: float
):
    daily = {**_ESS_DAILY, field: value}
    mock_download.side_effect = [_daily_frame({"ESS": daily}), _hourly_frame()]

    resolution = YFinancePriceSource(
        now=lambda: _GOVERNED_FETCHED_AT
    ).resolve_governed_daily_bars(
        ["ESS"],
        _GOVERNED_SESSION,
        processed_at=_GOVERNED_PROCESSED_AT,
    )

    assert resolution.bars == {}
    assert resolution.failure_map == {"ESS": "invalid ESS/2026-08-10"}


@patch("tradingagents.strategies.execution.price_source.yf.download")
def test_governed_recovery_rejects_both_broken_daily_extremes(mock_download):
    daily = {
        **_ESS_DAILY,
        "High": 282.0,
        "Low": 287.0,
    }
    mock_download.side_effect = [_daily_frame({"ESS": daily}), _hourly_frame()]

    resolution = YFinancePriceSource(
        now=lambda: _GOVERNED_FETCHED_AT
    ).resolve_governed_daily_bars(
        ["ESS"],
        _GOVERNED_SESSION,
        processed_at=_GOVERNED_PROCESSED_AT,
    )

    assert resolution.bars == {}
    assert resolution.failure_map == {"ESS": "invalid ESS/2026-08-10"}


@pytest.mark.parametrize(
    "case",
    ["missing", "nonpositive", "nonfinite", "stale", "pre-close", "wrong-session"],
)
@patch("tradingagents.strategies.execution.price_source.yf.download")
def test_governed_daily_noncoherence_failures_never_fetch_intraday(
    mock_download, case: str
):
    daily = dict(_ESS_DAILY)
    daily["High"] = 286.5
    frame_session = _GOVERNED_SESSION
    now = _GOVERNED_FETCHED_AT
    if case == "missing":
        frame = _daily_frame({"ESS": daily}).iloc[0:0]
    elif case == "nonpositive":
        daily["Close"] = 0
        frame = _daily_frame({"ESS": daily})
    elif case == "nonfinite":
        daily["Close"] = float("nan")
        frame = _daily_frame({"ESS": daily})
    elif case == "stale":
        now -= timedelta(days=2)
        frame = _daily_frame({"ESS": daily})
    elif case == "pre-close":
        now = datetime(2026, 8, 10, 19, tzinfo=timezone.utc)
        frame = _daily_frame({"ESS": daily})
    else:
        frame_session -= timedelta(days=1)
        frame = _daily_frame({"ESS": daily}, session=frame_session)
    mock_download.return_value = frame

    resolution = YFinancePriceSource(now=lambda: now).resolve_governed_daily_bars(
        ["ESS"],
        _GOVERNED_SESSION,
        processed_at=_GOVERNED_PROCESSED_AT,
    )

    assert resolution.bars == {}
    assert resolution.recoveries == {}
    assert mock_download.call_count == 1
    assert resolution.failure_map["ESS"].split()[0] in {"missing", "invalid"}


@patch("tradingagents.strategies.execution.price_source.yf.download")
def test_governed_preclose_incoherent_daily_never_fetches_intraday(mock_download):
    before_close = datetime(2026, 8, 10, 19, 59, tzinfo=timezone.utc)
    after_close = datetime(2026, 8, 10, 20, 0, 1, tzinfo=timezone.utc)
    clock = [before_close, after_close]
    mock_download.return_value = _daily_frame({"ESS": _ESS_DAILY})

    resolution = YFinancePriceSource(
        now=lambda: clock.pop(0)
    ).resolve_governed_daily_bars(
        ["ESS"],
        _GOVERNED_SESSION,
        processed_at=before_close - timedelta(minutes=1),
    )

    assert resolution.bars == {}
    assert resolution.recoveries == {}
    assert resolution.failure_map == {"ESS": "invalid ESS/2026-08-10"}
    assert mock_download.call_count == 1


@patch("tradingagents.strategies.execution.price_source.yf.download")
def test_governed_ambiguous_batch_identity_never_fetches_intraday(mock_download):
    mock_download.return_value = pd.DataFrame(
        [[100, 103, 99, 102]],
        index=pd.DatetimeIndex([_GOVERNED_SESSION.isoformat()]),
        columns=["Open", "High", "Low", "Close"],
    )

    resolution = YFinancePriceSource(
        now=lambda: _GOVERNED_FETCHED_AT
    ).resolve_governed_daily_bars(
        ["ESS", "IBM"],
        _GOVERNED_SESSION,
        processed_at=_GOVERNED_PROCESSED_AT,
    )

    assert resolution.failure_map == {
        "ESS": "invalid ESS/2026-08-10",
        "IBM": "invalid IBM/2026-08-10",
    }
    assert mock_download.call_count == 1


@pytest.mark.parametrize("provenance", ["alias-conflict", "duplicate", "absent"])
@patch("tradingagents.strategies.execution.price_source.yf.download")
def test_governed_daily_multiindex_requires_exact_alias_provenance(
    mock_download, provenance: str
):
    ticker = "BRK/B"
    if provenance == "alias-conflict":
        frame = _daily_frame({ticker: _ESS_DAILY, "BRK-B": _ESS_DAILY})
    elif provenance == "duplicate":
        columns = pd.MultiIndex.from_tuples(
            [
                (field, ticker)
                for field in ("Open", "High", "Low", "Close")
                for _ in range(2)
            ]
        )
        values = [
            value
            for field in ("Open", "High", "Low", "Close")
            for value in (_ESS_DAILY[field], _ESS_DAILY[field])
        ]
        frame = pd.DataFrame(
            [values],
            index=pd.DatetimeIndex([_GOVERNED_SESSION.isoformat()]),
            columns=columns,
        )
    else:
        frame = _daily_frame({"OTHER": _ESS_DAILY})
    mock_download.return_value = frame

    resolution = YFinancePriceSource(
        now=lambda: _GOVERNED_FETCHED_AT
    ).resolve_governed_daily_bars(
        [ticker],
        _GOVERNED_SESSION,
        processed_at=_GOVERNED_PROCESSED_AT,
    )

    assert resolution.bars == {}
    assert resolution.recoveries == {}
    assert resolution.failure_map == {ticker: f"invalid {ticker}/2026-08-10"}
    assert mock_download.call_count == 1


@pytest.mark.parametrize("bad_index", ["not-a-date", object()])
@patch("tradingagents.strategies.execution.price_source.yf.download")
def test_governed_malformed_daily_index_is_normalized(
    mock_download, bad_index: object
):
    frame = _daily_frame({"ESS": _ESS_DAILY})
    frame.index = pd.Index([bad_index])
    mock_download.return_value = frame

    resolution = YFinancePriceSource(
        now=lambda: _GOVERNED_FETCHED_AT
    ).resolve_governed_daily_bars(
        ["ESS"],
        _GOVERNED_SESSION,
        processed_at=_GOVERNED_PROCESSED_AT,
    )

    assert resolution.bars == {}
    assert resolution.failure_map == {"ESS": "invalid ESS/2026-08-10"}
    assert mock_download.call_count == 1


@patch("tradingagents.strategies.execution.price_source.yf.download")
def test_governed_daily_extra_wrong_session_row_is_invalid(mock_download):
    frame = pd.concat(
        [
            _daily_frame({"ESS": _ESS_DAILY}),
            _daily_frame(
                {"ESS": {**_ESS_DAILY, "High": 286.5}},
                session=_GOVERNED_SESSION - timedelta(days=1),
            ),
        ]
    )
    mock_download.return_value = frame

    resolution = YFinancePriceSource(
        now=lambda: _GOVERNED_FETCHED_AT
    ).resolve_governed_daily_bars(
        ["ESS"],
        _GOVERNED_SESSION,
        processed_at=_GOVERNED_PROCESSED_AT,
    )

    assert resolution.bars == {}
    assert resolution.failure_map == {"ESS": "invalid ESS/2026-08-10"}
    assert mock_download.call_count == 1


@patch("tradingagents.strategies.execution.price_source.yf.download")
def test_governed_resolver_preserves_healthy_batch_ticker(mock_download):
    ibm_values = {"Open": 250, "High": 253, "Low": 249, "Close": 252}
    daily_frame = _daily_frame({"ESS": _ESS_DAILY, "IBM": ibm_values})
    mock_download.side_effect = [daily_frame, _hourly_frame()]
    source = YFinancePriceSource(now=lambda: _GOVERNED_FETCHED_AT)

    resolution = source.resolve_governed_daily_bars(
        ["ESS", "IBM"],
        _GOVERNED_SESSION,
        processed_at=_GOVERNED_PROCESSED_AT,
    )

    expected_ibm = MarketBar(
        ticker="IBM",
        session=_GOVERNED_SESSION,
        open=Decimal("250"),
        high=Decimal("253"),
        low=Decimal("249"),
        close=Decimal("252"),
        source="yfinance",
        fetched_at=_GOVERNED_FETCHED_AT,
        adjusted=False,
    )
    assert resolution.bars["IBM"] == expected_ibm
    assert resolution.attempts["IBM"].validation_error is None
    assert [call.args[0] for call in mock_download.call_args_list] == [
        ["ESS", "IBM"],
        "ESS",
    ]


@patch("tradingagents.strategies.execution.price_source.yf.download")
def test_governed_provider_exception_is_normalized_and_attempted_once(mock_download):
    mock_download.side_effect = [
        _daily_frame({"ESS": _ESS_DAILY}),
        RuntimeError("secret provider detail"),
    ]

    resolution = YFinancePriceSource(
        now=lambda: _GOVERNED_FETCHED_AT
    ).resolve_governed_daily_bars(
        ["ESS"],
        _GOVERNED_SESSION,
        processed_at=_GOVERNED_PROCESSED_AT,
    )

    assert resolution.failure_map == {"ESS": "invalid ESS/2026-08-10"}
    assert "secret provider detail" not in (
        resolution.recoveries["ESS"].validation_error or ""
    )
    assert mock_download.call_count == 2


@patch("tradingagents.strategies.execution.price_source.yf.download")
def test_governed_initial_provider_failure_uses_detection_timestamp(mock_download):
    processed_at = datetime(2026, 8, 10, 20, tzinfo=timezone.utc)
    detected_at = datetime(2026, 8, 10, 20, 0, 5, tzinfo=timezone.utc)
    mock_download.side_effect = RuntimeError("provider unavailable")

    resolution = YFinancePriceSource(now=lambda: detected_at).resolve_governed_daily_bars(
        ["ESS"], _GOVERNED_SESSION, processed_at=processed_at
    )

    assert resolution.attempts["ESS"].fetched_at == detected_at
    assert resolution.attempts["ESS"].fetched_at != processed_at
    assert resolution.failure_map == {"ESS": "invalid ESS/2026-08-10"}


@patch("tradingagents.strategies.execution.price_source.yf.download")
def test_governed_resolution_evidence_maps_are_deeply_immutable(mock_download):
    resolution = _resolve_ess(mock_download, _hourly_frame())
    attempt = resolution.attempts["ESS"]
    assert attempt.raw_ohlc is not None

    mutations = (
        (resolution.bars, "OTHER", resolution.bars["ESS"]),
        (resolution.attempts, "OTHER", attempt),
        (resolution.recoveries, "OTHER", resolution.recoveries["ESS"]),
        (resolution.failure_map, "OTHER", "invalid OTHER/2026-08-10"),
        (attempt.raw_ohlc, "open", Decimal("1")),
    )
    for mapping, key, value in mutations:
        with pytest.raises(TypeError):
            mapping[key] = value  # type: ignore[index]


@patch("tradingagents.strategies.execution.price_source.yf.download")
def test_governed_bars_validate_against_post_fetch_clock(mock_download):
    healthy = {**_ESS_DAILY, "High": 286.5}
    mock_download.return_value = _daily_frame({"ESS": healthy})
    processed_at = _GOVERNED_FETCHED_AT
    clock = [
        processed_at + timedelta(seconds=1),
        processed_at + timedelta(seconds=2),
    ]

    resolution = YFinancePriceSource(
        now=lambda: clock.pop(0)
    ).resolve_governed_daily_bars(
        ["ESS"], _GOVERNED_SESSION, processed_at=processed_at
    )

    assert set(resolution.bars) == {"ESS"}
    assert resolution.failure_map == {}
    assert mock_download.call_count == 1


@pytest.mark.parametrize("stage", ["daily", "intraday"])
@patch("tradingagents.strategies.execution.price_source.yf.download")
def test_governed_none_provider_result_is_normalized(mock_download, stage: str):
    if stage == "daily":
        mock_download.return_value = None
    else:
        mock_download.side_effect = [_daily_frame({"ESS": _ESS_DAILY}), None]

    resolution = YFinancePriceSource(
        now=lambda: _GOVERNED_FETCHED_AT
    ).resolve_governed_daily_bars(
        ["ESS"],
        _GOVERNED_SESSION,
        processed_at=_GOVERNED_PROCESSED_AT,
    )

    assert resolution.bars == {}
    assert resolution.failure_map == {"ESS": "invalid ESS/2026-08-10"}
    assert mock_download.call_count == (1 if stage == "daily" else 2)


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
def test_candidate_provider_failures_are_bounded_and_retried_per_ticker(mock_download):
    """A failed batch and retry cannot leak text or abort later candidates."""
    good_ncl = _daily_frame(
        {"NCL": {"Open": 20, "High": 22, "Low": 19, "Close": 21}},
        session=_SESSION,
    )
    good_zkh = _daily_frame(
        {"ZKH": {"Open": 30, "High": 33, "Low": 29, "Close": 32}},
        session=_SESSION,
    )
    mock_download.side_effect = [
        RuntimeError("batch provider secret: request-123"),
        good_ncl,
        RuntimeError("UI provider secret: token-456"),
        good_zkh,
    ]
    source = YFinancePriceSource(now=lambda: _AS_OF)

    resolution = source.resolve_candidate_daily_bars(
        ["NCL", "UI", "ZKH"], _SESSION, _AS_OF, timedelta(hours=24)
    )

    assert [call.args[0] for call in mock_download.call_args_list] == [
        ["NCL", "UI", "ZKH"],
        ["NCL"],
        ["UI"],
        ["ZKH"],
    ]
    assert set(resolution.bars) == {("NCL", _SESSION), ("ZKH", _SESSION)}
    assert resolution.recovered_tickers == frozenset({"NCL", "ZKH"})
    assert resolution.quarantined_tickers == frozenset({"UI"})
    assert [
        (attempt.ticker, attempt.attempt, attempt.validation_error)
        for attempt in resolution.attempts
    ] == [
        ("NCL", 1, "provider_error NCL/2026-07-31"),
        ("UI", 1, "provider_error UI/2026-07-31"),
        ("ZKH", 1, "provider_error ZKH/2026-07-31"),
        ("NCL", 2, None),
        ("UI", 2, "provider_error UI/2026-07-31"),
        ("ZKH", 2, None),
    ]
    serialized = json.dumps(
        [asdict(attempt) for attempt in resolution.attempts], default=str
    )
    assert "provider secret" not in serialized
    assert "request-123" not in serialized
    assert "token-456" not in serialized


@patch("tradingagents.strategies.execution.price_source.yf.download")
@pytest.mark.parametrize("error_type", (TypeError, ValueError))
def test_candidate_bar_programmer_error_is_not_normalized(
    mock_download, monkeypatch, error_type
):
    mock_download.return_value = _daily_frame(
        {"NCL": {"Open": 20, "High": 22, "Low": 19, "Close": 21}},
        session=_SESSION,
    )
    source = YFinancePriceSource(now=lambda: _AS_OF)

    def invariant_failure(*_args, **_kwargs):
        raise error_type("candidate parser invariant failed")

    monkeypatch.setattr(source, "_candidate_bar_attempt", invariant_failure)

    with pytest.raises(error_type, match="candidate parser invariant failed"):
        source.resolve_candidate_daily_bars(
            ["NCL"], _SESSION, _AS_OF, timedelta(hours=24)
        )


@patch("tradingagents.strategies.execution.price_source.yf.download")
def test_candidate_malformed_provider_result_is_bounded_invalid_data(mock_download):
    mock_download.side_effect = [None, None]
    source = YFinancePriceSource(now=lambda: _AS_OF)

    resolution = source.resolve_candidate_daily_bars(
        ["NCL"], _SESSION, _AS_OF, timedelta(hours=24)
    )

    assert mock_download.call_count == 2
    assert resolution.bars == {}
    assert resolution.quarantined_tickers == frozenset({"NCL"})
    assert [attempt.validation_error for attempt in resolution.attempts] == [
        "invalid_data NCL/2026-07-31",
        "invalid_data NCL/2026-07-31",
    ]


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
