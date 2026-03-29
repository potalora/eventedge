import os
import tempfile
import pytest
from unittest.mock import patch, MagicMock

from tradingagents.storage.db import Database
from tradingagents.dashboard.components.formatters import format_rating_badge, format_currency
from tradingagents.dashboard.components.charts import make_equity_curve_chart


class TestFormatters:
    def test_format_rating_badge_buy(self):
        result = format_rating_badge("BUY")
        assert "BUY" in result

    def test_format_rating_badge_sell(self):
        result = format_rating_badge("SELL")
        assert "SELL" in result

    def test_format_currency(self):
        assert format_currency(5000.0) == "$5,000.00"
        assert format_currency(-150.50) == "-$150.50"
        assert format_currency(0) == "$0.00"


class TestCharts:
    def test_make_equity_curve_chart_returns_figure(self):
        import pandas as pd
        df = pd.DataFrame({
            "date": pd.date_range("2025-09-01", periods=5, freq="W"),
            "portfolio_value": [5000, 5100, 5050, 5200, 5300],
        })
        fig = make_equity_curve_chart(df)
        assert fig is not None
        assert hasattr(fig, "data")

    def test_make_equity_curve_chart_empty_df(self):
        import pandas as pd
        df = pd.DataFrame(columns=["date", "portfolio_value"])
        fig = make_equity_curve_chart(df)
        assert fig is not None
