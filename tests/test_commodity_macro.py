"""Tests for commodity_macro strategy and CFTCSource data source."""
from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock
import pandas as pd
import numpy as np


class TestCFTCSource:
    """Tests for CFTCSource data source."""

    def test_cftc_source_positioning(self):
        """Mock COT data -> correct percentiles and direction signals."""
        from tradingagents.strategies.data_sources.cftc_source import CFTCSource

        source = CFTCSource()

        # Build mock COT DataFrame with 52 weeks of data
        np.random.seed(42)
        n_weeks = 52
        dates = pd.date_range(end="2026-04-01", periods=n_weeks, freq="W")

        # Gold: high managed money net long -> should signal "short" (contrarian)
        gold_longs = np.linspace(100_000, 200_000, n_weeks)
        gold_shorts = np.full(n_weeks, 50_000)

        rows = []
        for i in range(n_weeks):
            rows.append({
                "Market and Exchange Names": "GOLD - COMEX",
                "As of Date in Form YYYY-MM-DD": dates[i].strftime("%Y-%m-%d"),
                "M_Money_Positions_Long_All": gold_longs[i],
                "M_Money_Positions_Short_All": gold_shorts[i],
            })

        mock_df = pd.DataFrame(rows)

        with patch.object(source, "_fetch_raw_report", return_value=mock_df):
            result = source.fetch({
                "method": "cot_positioning",
                "commodities": ["gold"],
                "lookback_weeks": 52,
            })

        assert "gold" in result
        gold = result["gold"]
        assert 0.0 <= gold["percentile"] <= 1.0
        assert gold["percentile"] > 0.8  # Near top of range
        assert gold["direction_signal"] == "short"  # Contrarian
        assert gold["net_position"] > 0

    def test_cftc_source_unavailable(self):
        """Graceful degradation when cot_reports not installed."""
        from tradingagents.strategies.data_sources.cftc_source import CFTCSource

        source = CFTCSource()
        with patch.dict("sys.modules", {"cot_reports": None}):
            with patch("builtins.__import__", side_effect=ImportError("no cot_reports")):
                assert source.is_available() is False
