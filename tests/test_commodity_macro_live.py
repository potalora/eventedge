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
        """The configured commodity spot series each return a non-empty series.

        Reads the IDs from SERIES_MAP (not hardcoded) so the test tracks whatever
        production actually fetches and can't drift if a series is replaced.
        """
        from tradingagents.strategies.data_sources.fred_source import (
            SERIES_MAP,
            FREDSource,
        )

        source = FREDSource()
        if not source.is_available():
            pytest.skip("fredapi not installed or no API key")

        series_ids = [SERIES_MAP[k] for k in ("wti_spot", "gold_spot", "copper_spot")]
        for sid in series_ids:
            data = source.fetch_series(sid, "2025-01-01", "2026-04-01")
            assert len(data) > 0, f"FRED series {sid} returned empty"
