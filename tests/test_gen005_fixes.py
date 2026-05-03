"""Tests for gen_005 fixes: ticker normalization, retry logic, date consistency."""
from __future__ import annotations

import pytest


class TestTickerNormalization:
    """BRK/B → BRK-B normalization at yfinance boundary."""

    def test_normalize_slash_to_dash(self):
        from tradingagents.strategies.data_sources.yfinance_source import normalize_ticker
        assert normalize_ticker("BRK/B") == "BRK-B"

    def test_normalize_no_slash(self):
        from tradingagents.strategies.data_sources.yfinance_source import normalize_ticker
        assert normalize_ticker("AAPL") == "AAPL"

    def test_normalize_multiple_slashes(self):
        from tradingagents.strategies.data_sources.yfinance_source import normalize_ticker
        assert normalize_ticker("BF/A") == "BF-A"

    def test_normalize_list(self):
        from tradingagents.strategies.data_sources.yfinance_source import normalize_tickers
        result = normalize_tickers(["AAPL", "BRK/B", "BF/A"])
        assert result == ["AAPL", "BRK-B", "BF-A"]

    def test_fetch_prices_normalizes(self):
        """fetch_prices should normalize tickers before calling yfinance."""
        from unittest.mock import patch, MagicMock
        import pandas as pd
        from tradingagents.strategies.data_sources.yfinance_source import YFinanceSource

        source = YFinanceSource()
        with patch("yfinance.download") as mock_dl:
            mock_dl.return_value = pd.DataFrame()
            source.fetch_prices(["BRK/B"], "2026-04-01", "2026-04-03")
            call_args = mock_dl.call_args
            assert "BRK-B" in call_args[0][0]
            assert "BRK/B" not in call_args[0][0]

    def test_fetch_prices_remaps_columns_back(self):
        """fetch_prices should remap BRK-B columns back to BRK/B in output."""
        import pandas as pd
        from unittest.mock import patch
        from tradingagents.strategies.data_sources.yfinance_source import YFinanceSource

        source = YFinanceSource()
        mock_df = pd.DataFrame(
            [[100.0, 200.0], [101.0, 201.0]],
            columns=pd.MultiIndex.from_tuples(
                [("Close", "BRK-B"), ("Close", "AAPL")],
                names=["Price", "Ticker"],
            ),
        )
        with patch("yfinance.download", return_value=mock_df):
            result = source.fetch_prices(["BRK/B", "AAPL"], "2026-04-01", "2026-04-03")

        assert ("Close", "BRK/B") in result.columns
        assert ("Close", "BRK-B") not in result.columns
        assert ("Close", "AAPL") in result.columns


from unittest.mock import patch, MagicMock
import requests


class TestNOAARetry:
    """NOAA CDO should retry on timeout/5xx with exponential backoff."""

    def _make_source(self):
        from tradingagents.strategies.data_sources.noaa_source import NOAASource
        source = NOAASource(token="test-token")
        source._session = MagicMock()
        return source

    def test_retry_on_timeout(self):
        source = self._make_source()
        with patch("time.sleep"):
            source._session.get.side_effect = [
                requests.exceptions.Timeout("timeout"),
                MagicMock(status_code=200, json=lambda: {"results": [{"value": 1}]}),
            ]
            result = source._api_get("/data", {"datasetid": "GHCND"})
            assert result is not None
            assert source._session.get.call_count == 2

    def test_retry_on_500(self):
        source = self._make_source()
        with patch("time.sleep"):
            mock_resp_500 = MagicMock(status_code=500)
            mock_resp_ok = MagicMock(status_code=200, json=lambda: {"results": []})
            source._session.get.side_effect = [mock_resp_500, mock_resp_ok]
            result = source._api_get("/data", {"datasetid": "GHCND"})
            assert result is not None
            assert source._session.get.call_count == 2

    def test_max_retries_exhausted(self):
        source = self._make_source()
        with patch("time.sleep"):
            source._session.get.side_effect = requests.exceptions.Timeout("timeout")
            result = source._api_get("/data", {"datasetid": "GHCND"})
            assert result is None
            assert source._session.get.call_count == 3  # 1 initial + 2 retries

    def test_timeout_uses_connect_and_read(self):
        source = self._make_source()
        with patch("time.sleep"):
            source._session.get.return_value = MagicMock(status_code=200, json=lambda: {"results": []})
            source._api_get("/data", {"datasetid": "GHCND"})
            _, kwargs = source._session.get.call_args
            # Tightened to fail fast on flaky NOAA fallback (was (10, 30)).
            assert kwargs["timeout"] == (5, 10)


class TestUSDARetry:
    """USDA NASS should retry on timeout/5xx."""

    @patch("time.sleep")
    def test_retry_on_timeout(self, mock_sleep):
        from tradingagents.strategies.data_sources.usda_source import USDASource

        source = USDASource(api_key="test-key")
        with patch("requests.get") as mock_get:
            mock_get.side_effect = [
                requests.exceptions.Timeout("timeout"),
                MagicMock(status_code=200, json=lambda: {"data": []}),
            ]
            result = source.fetch_crop_progress("CORN", 2026)
            assert result == []
            assert mock_get.call_count == 2

    @patch("time.sleep")
    def test_max_retries_exhausted(self, mock_sleep):
        from tradingagents.strategies.data_sources.usda_source import USDASource

        source = USDASource(api_key="test-key")
        with patch("requests.get") as mock_get:
            mock_get.side_effect = requests.exceptions.Timeout("timeout")
            result = source.fetch_crop_progress("CORN", 2026)
            assert result == []
            # 1 initial + 1 retry against the QuickStats API, then 1 ESMIS
            # landing fetch attempt (which also times out under the mock).
            assert mock_get.call_count == 3
            assert source._unavailable is True


class TestDroughtMonitorRetry:
    """Drought Monitor should retry on timeout/5xx."""

    @patch("time.sleep")
    def test_retry_on_timeout(self, mock_sleep):
        from tradingagents.strategies.data_sources.drought_monitor_source import DroughtMonitorSource

        source = DroughtMonitorSource()
        with patch("requests.get") as mock_get:
            mock_get.side_effect = [
                requests.exceptions.Timeout("timeout"),
                MagicMock(status_code=200, json=lambda: []),
            ]
            result = source.fetch_drought_severity(
                start="2026-03-27", end="2026-04-03"
            )
            assert result == {}
            assert mock_get.call_count == 2


# ---------------------------------------------------------------------------
# Task 4: 50k/100k min_position_value tuning
# ---------------------------------------------------------------------------

class TestMinPositionValue:
    """50k/100k min_position_value should be lowered for day-1 signals."""

    def test_50k_min_position_value(self):
        from tradingagents.strategies.orchestration.cohort_orchestrator import SIZE_PROFILES
        assert SIZE_PROFILES["50k"].min_position_value == 1_000.0

    def test_100k_min_position_value(self):
        from tradingagents.strategies.orchestration.cohort_orchestrator import SIZE_PROFILES
        assert SIZE_PROFILES["100k"].min_position_value == 2_000.0

    def test_5k_low_min(self):
        from tradingagents.strategies.orchestration.cohort_orchestrator import SIZE_PROFILES
        assert SIZE_PROFILES["5k"].min_position_value == 100.0

    def test_10k_low_min(self):
        from tradingagents.strategies.orchestration.cohort_orchestrator import SIZE_PROFILES
        assert SIZE_PROFILES["10k"].min_position_value == 250.0


# ---------------------------------------------------------------------------
# Task 5: Trading date consistency
# ---------------------------------------------------------------------------

from datetime import datetime, timedelta


class TestFinnhubDateConsistency:
    """Finnhub fetch should use trading_date, not datetime.now()."""

    def test_finnhub_uses_trading_date(self):
        from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
        import inspect
        sig = inspect.signature(MultiStrategyEngine._fetch_finnhub_data)
        assert "trading_date" in sig.parameters


class TestCongressDateConsistency:
    """Congress source should accept an as_of parameter."""

    def test_get_recent_trades_accepts_as_of(self):
        from tradingagents.strategies.data_sources.congress_source import CongressSource
        import inspect
        sig = inspect.signature(CongressSource.get_recent_trades)
        assert "as_of" in sig.parameters

    def test_get_recent_trades_uses_as_of(self):
        from tradingagents.strategies.data_sources.congress_source import CongressSource

        source = CongressSource()
        source._cache["all_trades"] = [
            {"tradeDate": "2026-03-15", "ticker": "AAPL", "type": "purchase",
             "transaction_date": "2026-03-15"},
            {"tradeDate": "2026-02-01", "ticker": "MSFT", "type": "purchase",
             "transaction_date": "2026-02-01"},
        ]
        recent = source.get_recent_trades(days_back=30, as_of="2026-04-03")
        tickers = [t["ticker"] for t in recent]
        assert "AAPL" in tickers
        assert "MSFT" not in tickers


class TestUSASpendingDateConsistency:
    """USASpending should accept as_of parameter."""

    def test_get_recent_large_contracts_accepts_as_of(self):
        from tradingagents.strategies.data_sources.usaspending_source import USASpendingSource
        import inspect
        sig = inspect.signature(USASpendingSource.get_recent_large_contracts)
        assert "as_of" in sig.parameters


class TestUSASpendingFetch:
    """Engine should fetch USASpending data for govt_contracts strategy."""

    def test_usaspending_fetch_method_exists(self):
        from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
        assert hasattr(MultiStrategyEngine, "_fetch_usaspending_data")

    def test_fetch_usaspending_data_signature(self):
        from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
        import inspect
        sig = inspect.signature(MultiStrategyEngine._fetch_usaspending_data)
        assert "trading_date" in sig.parameters


# ---------------------------------------------------------------------------
# Task 7: resolve_trading_date() utility
# ---------------------------------------------------------------------------

class TestResolveTradingDate:
    """resolve_trading_date should roll back to last trading day.

    Note: 2026-04-03 is Good Friday — NYSE is closed. The preceding trading
    day is Thursday 2026-04-02. Tests are written accordingly.
    """

    def test_weekday_unchanged(self):
        """A normal trading Friday (not a holiday) stays unchanged."""
        from tradingagents.strategies.orchestration.trading_calendar import resolve_trading_date
        # 2026-04-10 is a Friday and not a holiday
        assert resolve_trading_date("2026-04-10") == "2026-04-10"

    def test_thursday_unchanged(self):
        """Thursday that is not a holiday stays unchanged."""
        from tradingagents.strategies.orchestration.trading_calendar import resolve_trading_date
        # 2026-04-02 is Thursday (the day before Good Friday)
        assert resolve_trading_date("2026-04-02") == "2026-04-02"

    def test_good_friday_rolls_back(self):
        """2026-04-03 is Good Friday (NYSE closed) → rolls back to Thursday 2026-04-02."""
        from tradingagents.strategies.orchestration.trading_calendar import resolve_trading_date
        assert resolve_trading_date("2026-04-03") == "2026-04-02"

    def test_saturday_rolls_to_thursday(self):
        """Saturday 2026-04-04 → Friday 2026-04-03 is a holiday → Thursday 2026-04-02."""
        from tradingagents.strategies.orchestration.trading_calendar import resolve_trading_date
        assert resolve_trading_date("2026-04-04") == "2026-04-02"

    def test_sunday_rolls_to_thursday(self):
        """Sunday 2026-04-05 → Friday 2026-04-03 is a holiday → Thursday 2026-04-02."""
        from tradingagents.strategies.orchestration.trading_calendar import resolve_trading_date
        assert resolve_trading_date("2026-04-05") == "2026-04-02"

    def test_monday_unchanged(self):
        """Monday that is not a holiday stays unchanged."""
        from tradingagents.strategies.orchestration.trading_calendar import resolve_trading_date
        assert resolve_trading_date("2026-04-06") == "2026-04-06"

    def test_no_date_returns_valid_weekday(self):
        """None returns today resolved to a trading day (Mon-Fri, not a holiday)."""
        from tradingagents.strategies.orchestration.trading_calendar import resolve_trading_date
        result = resolve_trading_date(None)
        assert len(result) == 10
        day = datetime.strptime(result, "%Y-%m-%d").weekday()
        assert day < 5  # Monday-Friday

    def test_us_holiday_new_year_rolls_back(self):
        """2026-01-01 (Thursday, New Year's Day) → 2025-12-31 (Wednesday)."""
        from tradingagents.strategies.orchestration.trading_calendar import resolve_trading_date
        result = resolve_trading_date("2026-01-01")
        assert result == "2025-12-31"

    def test_normal_friday_april_10(self):
        """2026-04-10 is a Friday with no holiday — passes through unchanged."""
        from tradingagents.strategies.orchestration.trading_calendar import resolve_trading_date
        assert resolve_trading_date("2026-04-10") == "2026-04-10"

    def test_returns_string(self):
        """Result must be a YYYY-MM-DD string."""
        from tradingagents.strategies.orchestration.trading_calendar import resolve_trading_date
        result = resolve_trading_date("2026-04-06")
        assert isinstance(result, str)
        assert len(result) == 10
        assert result[4] == "-" and result[7] == "-"
