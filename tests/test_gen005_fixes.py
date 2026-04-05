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

    def test_retry_on_timeout(self):
        from tradingagents.strategies.data_sources.noaa_source import NOAASource

        source = NOAASource(token="test-token")
        with patch("requests.get") as mock_get, patch("time.sleep"):
            mock_get.side_effect = [
                requests.exceptions.Timeout("timeout"),
                MagicMock(status_code=200, json=lambda: {"results": [{"value": 1}]}),
            ]
            result = source._api_get("/data", {"datasetid": "GHCND"})
            assert result is not None
            assert mock_get.call_count == 2

    def test_retry_on_500(self):
        from tradingagents.strategies.data_sources.noaa_source import NOAASource

        source = NOAASource(token="test-token")
        with patch("requests.get") as mock_get, patch("time.sleep"):
            mock_resp_500 = MagicMock(status_code=500)
            mock_resp_ok = MagicMock(status_code=200, json=lambda: {"results": []})
            mock_get.side_effect = [mock_resp_500, mock_resp_ok]
            result = source._api_get("/data", {"datasetid": "GHCND"})
            assert result is not None
            assert mock_get.call_count == 2

    def test_max_retries_exhausted(self):
        from tradingagents.strategies.data_sources.noaa_source import NOAASource

        source = NOAASource(token="test-token")
        with patch("requests.get") as mock_get, patch("time.sleep"):
            mock_get.side_effect = requests.exceptions.Timeout("timeout")
            result = source._api_get("/data", {"datasetid": "GHCND"})
            assert result is None
            assert mock_get.call_count == 4  # 1 initial + 3 retries

    def test_timeout_increased_to_60s(self):
        from tradingagents.strategies.data_sources.noaa_source import NOAASource

        source = NOAASource(token="test-token")
        with patch("requests.get") as mock_get, patch("time.sleep"):
            mock_get.return_value = MagicMock(status_code=200, json=lambda: {"results": []})
            source._api_get("/data", {"datasetid": "GHCND"})
            _, kwargs = mock_get.call_args
            assert kwargs["timeout"] == 60


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
            assert mock_get.call_count == 4  # 1 initial + 3 retries


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
