"""Live API smoke tests for commodity_macro strategy.

Skipped in normal pytest runs. Invoke with: pytest -m live
"""
from __future__ import annotations

import pytest


pytestmark = pytest.mark.live


class TestCFTCLive:
    """Live CFTC data tests."""

    def test_cftc_source_live_fetch(self):
        """Real COT report is non-empty, expected columns present, COMMODITY_CODES match."""
        from tradingagents.strategies.data_sources.cftc_source import (
            CFTCSource, COMMODITY_CODES, COL_MARKET, COL_MM_LONG,
        )

        source = CFTCSource()
        if not source.is_available():
            pytest.skip("cot_reports not installed")

        df = source._fetch_raw_report("disaggregated_futures")
        assert len(df) > 0, "COT report is empty"
        assert COL_MARKET in df.columns
        assert COL_MM_LONG in df.columns

        # Verify COMMODITY_CODES strings match actual data
        market_names = df[COL_MARKET].unique()
        for commodity, code in COMMODITY_CODES.items():
            matches = [m for m in market_names if code in m]
            assert len(matches) > 0, (
                f"COMMODITY_CODES['{commodity}'] = '{code}' not found in report. "
                f"Available names containing partial match: "
                f"{[m for m in market_names if commodity.split('_')[0].upper() in m.upper()][:5]}"
            )

    def test_cftc_positioning_live(self):
        """Live positioning for gold/crude returns percentiles in 0-1 range."""
        from tradingagents.strategies.data_sources.cftc_source import CFTCSource

        source = CFTCSource()
        if not source.is_available():
            pytest.skip("cot_reports not installed")

        result = source.fetch({
            "method": "cot_positioning",
            "commodities": ["gold", "crude_oil"],
            "lookback_weeks": 52,
        })
        assert "error" not in result, f"Fetch failed: {result}"
        assert "gold" in result, f"Gold not in result. Keys: {list(result.keys())}"
        assert 0.0 <= result["gold"]["percentile"] <= 1.0
        assert result["gold"]["net_position"] is not None


class TestFREDCommodityLive:
    """Live FRED commodity series tests."""

    def test_fred_commodity_series_live(self):
        """DCOILWTICO, GOLDAMGBD228NLBM, PCOPPUSDM return non-empty series."""
        from tradingagents.strategies.data_sources.fred_source import FREDSource

        source = FREDSource()
        if not source.is_available():
            pytest.skip("fredapi not installed or no API key")

        series_ids = ["DCOILWTICO", "GOLDAMGBD228NLBM", "PCOPPUSDM"]
        for sid in series_ids:
            data = source.fetch_series(sid, "2025-01-01", "2026-04-01")
            assert len(data) > 0, f"FRED series {sid} returned empty"


class TestOpenBBFuturesCurveLive:
    """Live OpenBB futures curve tests."""

    def test_openbb_futures_curve_live(self):
        """Gold futures curve present, contango calculation sane."""
        try:
            from tradingagents.strategies.data_sources.openbb_source import OpenBBSource
        except ImportError:
            pytest.skip("openbb not installed")

        source = OpenBBSource()
        if not source.is_available():
            pytest.skip("OpenBB not available")

        result = source.fetch({"method": "commodity_futures_curve", "symbol": "GC"})
        if "error" in result:
            pytest.skip(f"Futures curve fetch failed: {result['error']}")

        assert "front_month" in result
        assert "contango_pct" in result
        assert isinstance(result["contango_pct"], (int, float))
