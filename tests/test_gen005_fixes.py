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
