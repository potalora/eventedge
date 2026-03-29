# tests/test_options_data.py
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime

from tradingagents.dataflows.options_data import (
    get_options_chain,
    get_options_greeks,
    get_put_call_ratio,
)


class TestGetOptionsChain:
    @patch("tradingagents.dataflows.options_data.yf.Ticker")
    def test_returns_formatted_chain(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker.options = ("2026-04-18", "2026-05-16", "2026-06-20")

        import pandas as pd
        calls_df = pd.DataFrame({
            "strike": [70.0, 75.0, 80.0],
            "lastPrice": [5.0, 2.5, 1.0],
            "bid": [4.8, 2.3, 0.9],
            "ask": [5.2, 2.7, 1.1],
            "volume": [1000, 2000, 500],
            "openInterest": [5000, 8000, 3000],
            "impliedVolatility": [0.35, 0.32, 0.30],
        })
        puts_df = pd.DataFrame({
            "strike": [70.0, 75.0, 80.0],
            "lastPrice": [1.0, 2.5, 5.0],
            "bid": [0.9, 2.3, 4.8],
            "ask": [1.1, 2.7, 5.2],
            "volume": [800, 1500, 600],
            "openInterest": [4000, 7000, 2000],
            "impliedVolatility": [0.33, 0.31, 0.29],
        })
        mock_chain = MagicMock()
        mock_chain.calls = calls_df
        mock_chain.puts = puts_df
        mock_ticker.option_chain.return_value = mock_chain
        mock_ticker_cls.return_value = mock_ticker

        result = get_options_chain("SOFI", "2026-04-01")
        assert "strike" in result.lower() or "Strike" in result
        assert "70.0" in result or "70" in result
        assert "call" in result.lower() or "Call" in result

    @patch("tradingagents.dataflows.options_data.yf.Ticker")
    def test_handles_no_options(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker.options = ()
        mock_ticker_cls.return_value = mock_ticker

        result = get_options_chain("FAKE", "2026-04-01")
        assert "no options" in result.lower()


class TestGetOptionsGreeks:
    @patch("tradingagents.dataflows.options_data.yf.Ticker")
    def test_returns_greeks(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker.info = {"regularMarketPrice": 75.0}
        mock_ticker_cls.return_value = mock_ticker

        result = get_options_greeks("SOFI", "2026-06-20", 80.0, "call")
        assert "delta" in result.lower()

    @patch("tradingagents.dataflows.options_data.yf.Ticker")
    def test_handles_missing_price(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker.info = {}
        mock_ticker_cls.return_value = mock_ticker

        result = get_options_greeks("FAKE", "2026-06-20", 80.0, "call")
        assert "error" in result.lower() or "unavailable" in result.lower()


class TestGetPutCallRatio:
    @patch("tradingagents.dataflows.options_data.yf.Ticker")
    def test_returns_ratio(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker.options = ("2026-04-18",)

        import pandas as pd
        calls_df = pd.DataFrame({"openInterest": [5000, 8000]})
        puts_df = pd.DataFrame({"openInterest": [4000, 7000]})
        mock_chain = MagicMock()
        mock_chain.calls = calls_df
        mock_chain.puts = puts_df
        mock_ticker.option_chain.return_value = mock_chain
        mock_ticker_cls.return_value = mock_ticker

        result = get_put_call_ratio("SOFI")
        assert "put/call" in result.lower() or "ratio" in result.lower()
