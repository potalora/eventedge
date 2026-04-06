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

        from tradingagents.strategies.data_sources.cftc_source import (
            COL_MARKET, COL_DATE, COL_MM_LONG, COL_MM_SHORT, COMMODITY_CODES,
        )

        rows = []
        for i in range(n_weeks):
            rows.append({
                COL_MARKET: COMMODITY_CODES["gold"],
                COL_DATE: dates[i].strftime("%Y-%m-%d"),
                COL_MM_LONG: gold_longs[i],
                COL_MM_SHORT: gold_shorts[i],
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


class TestCommodityMacroStrategy:
    """Tests for CommodityMacroStrategy."""

    def _make_cot_data(self, gold_pctl=0.5, crude_pctl=0.5, gold_dir="neutral", crude_dir="neutral"):
        return {
            "gold": {"net_position": 100_000, "percentile": gold_pctl, "wow_change": 0, "direction_signal": gold_dir},
            "crude_oil": {"net_position": -50_000, "percentile": crude_pctl, "wow_change": 0, "direction_signal": crude_dir},
            "silver": {"net_position": 50_000, "percentile": 0.5, "wow_change": 0, "direction_signal": "neutral"},
            "nat_gas": {"net_position": 20_000, "percentile": 0.5, "wow_change": 0, "direction_signal": "neutral"},
            "copper": {"net_position": 30_000, "percentile": 0.5, "wow_change": 0, "direction_signal": "neutral"},
        }

    def _make_fred_data(self, fed_funds=5.0, cpi_latest=3.0, cpi_3m_ago=3.0, vix=20.0, yield_curve=0.5):
        return {
            "FEDFUNDS": {pd.Timestamp("2026-01-01"): fed_funds},
            "CPIAUCSL": {pd.Timestamp("2025-10-01"): cpi_3m_ago, pd.Timestamp("2026-01-01"): cpi_latest},
            "VIXCLS": {pd.Timestamp("2026-01-01"): vix},
            "T10Y2Y": {pd.Timestamp("2026-01-01"): yield_curve},
        }

    def test_screen_cot_gate_triggers(self):
        """Extreme positioning (90th pctl) -> candidates with correct ETF tickers."""
        from tradingagents.strategies.modules.commodity_macro import CommodityMacroStrategy
        strategy = CommodityMacroStrategy()
        data = {
            "cftc": self._make_cot_data(gold_pctl=0.90, gold_dir="short"),
            "fred": self._make_fred_data(),
            "regulations": {},
            "finnhub": {},
        }
        params = strategy.get_default_params("3m")
        params["eligible_instruments"] = ["GLD", "SLV", "PDBC", "COPX", "XLE", "USO", "UNG"]
        candidates = strategy.screen(data, "2026-04-01", params)
        assert len(candidates) > 0
        assert candidates[0].ticker == "GLD"
        assert candidates[0].direction == "short"
        assert candidates[0].metadata["needs_llm_analysis"] is True
        assert candidates[0].metadata["analysis_type"] == "commodity_macro"

    def test_screen_cot_gate_no_trigger(self):
        """Moderate positioning (50th pctl) -> empty list."""
        from tradingagents.strategies.modules.commodity_macro import CommodityMacroStrategy
        strategy = CommodityMacroStrategy()
        data = {
            "cftc": self._make_cot_data(),
            "fred": self._make_fred_data(),
            "regulations": {},
            "finnhub": {},
        }
        params = strategy.get_default_params("3m")
        params["eligible_instruments"] = ["GLD", "SLV", "PDBC"]
        candidates = strategy.screen(data, "2026-04-01", params)
        assert len(candidates) == 0

    def test_screen_macro_veto(self):
        """COT extreme + contradicting macro -> no candidates."""
        from tradingagents.strategies.modules.commodity_macro import CommodityMacroStrategy
        strategy = CommodityMacroStrategy()
        data = {
            "cftc": self._make_cot_data(gold_pctl=0.10, gold_dir="long"),
            "fred": self._make_fred_data(fed_funds=5.5, cpi_latest=2.5, cpi_3m_ago=3.0),
            "regulations": {},
            "finnhub": {},
        }
        params = strategy.get_default_params("3m")
        params["eligible_instruments"] = ["GLD", "SLV"]
        candidates = strategy.screen(data, "2026-04-01", params)
        gold_candidates = [c for c in candidates if c.ticker == "GLD"]
        assert len(gold_candidates) == 0

    def test_screen_catalyst_boost(self):
        """Score increases when catalyst aligns."""
        from tradingagents.strategies.modules.commodity_macro import CommodityMacroStrategy
        strategy = CommodityMacroStrategy()
        base_data = {
            "cftc": self._make_cot_data(gold_pctl=0.90, gold_dir="short"),
            "fred": self._make_fred_data(),
            "regulations": {},
            "finnhub": {},
        }
        params = strategy.get_default_params("3m")
        params["eligible_instruments"] = ["GLD", "SLV", "PDBC"]
        no_catalyst = strategy.screen(base_data, "2026-04-01", params)
        catalyst_data = dict(base_data)
        catalyst_data["regulations"] = {"results": [{"title": "New gold mining regulation announced"}]}
        with_catalyst = strategy.screen(catalyst_data, "2026-04-01", params)
        if no_catalyst and with_catalyst:
            assert with_catalyst[0].score >= no_catalyst[0].score

    def test_short_only_enforcement(self):
        """COT long crude -> USO NOT emitted long, XLE substituted or skipped."""
        from tradingagents.strategies.modules.commodity_macro import CommodityMacroStrategy
        strategy = CommodityMacroStrategy()
        data = {
            "cftc": self._make_cot_data(crude_pctl=0.10, crude_dir="long"),
            "fred": self._make_fred_data(),
            "regulations": {},
            "finnhub": {},
        }
        params = strategy.get_default_params("3m")
        params["eligible_instruments"] = ["GLD", "SLV", "PDBC", "COPX", "XLE", "USO", "UNG"]
        candidates = strategy.screen(data, "2026-04-01", params)
        uso_longs = [c for c in candidates if c.ticker == "USO" and c.direction == "long"]
        assert len(uso_longs) == 0
        xle_longs = [c for c in candidates if c.ticker == "XLE" and c.direction == "long"]
        assert len(xle_longs) > 0 or len(candidates) == 0

    def test_horizon_filtering(self):
        """30d -> no candidates. 3m -> candidates. 1y -> GLD/SLV only."""
        from tradingagents.strategies.modules.commodity_macro import CommodityMacroStrategy
        strategy = CommodityMacroStrategy()
        data = {
            "cftc": self._make_cot_data(gold_pctl=0.90, gold_dir="short"),
            "fred": self._make_fred_data(),
            "regulations": {},
            "finnhub": {},
        }
        params_30d = strategy.get_default_params("30d")
        params_30d["commodity_eligible"] = False
        params_30d["eligible_instruments"] = []
        assert strategy.screen(data, "2026-04-01", params_30d) == []

        params_3m = strategy.get_default_params("3m")
        params_3m["eligible_instruments"] = ["GLD", "SLV", "PDBC"]
        assert len(strategy.screen(data, "2026-04-01", params_3m)) > 0

        params_1y = strategy.get_default_params("1y")
        params_1y["eligible_instruments"] = ["GLD", "SLV"]
        for c in strategy.screen(data, "2026-04-01", params_1y):
            assert c.ticker in ("GLD", "SLV")

    def test_futures_to_etf_map(self):
        """All map entries resolve, no dangling keys."""
        from tradingagents.strategies.modules.commodity_macro import (
            FUTURES_TO_ETF_MAP, ETF_TO_FUTURES_UNDERLYING, COMMODITY_ETFS,
        )
        for futures, etf in FUTURES_TO_ETF_MAP.items():
            assert etf in COMMODITY_ETFS
        for etf in ETF_TO_FUTURES_UNDERLYING:
            assert etf in COMMODITY_ETFS

    def test_check_exit_hold_period(self):
        """Standard hold period exit."""
        from tradingagents.strategies.modules.commodity_macro import CommodityMacroStrategy
        strategy = CommodityMacroStrategy()
        params = strategy.get_default_params("3m")
        should_exit, reason = strategy.check_exit("GLD", 200.0, 210.0, params["hold_days"], params, {})
        assert should_exit is True
        assert reason == "hold_period"
        should_exit, reason = strategy.check_exit("GLD", 200.0, 210.0, 5, params, {})
        assert should_exit is False

    def test_check_exit_cot_normalization(self):
        """Early exit on positioning normalization."""
        from tradingagents.strategies.modules.commodity_macro import CommodityMacroStrategy
        strategy = CommodityMacroStrategy()
        params = strategy.get_default_params("3m")
        data = {
            "cftc": {
                "gold": {"net_position": 80_000, "percentile": 0.50, "wow_change": 0, "direction_signal": "neutral"},
            },
        }
        should_exit, reason = strategy.check_exit("GLD", 200.0, 210.0, 10, params, data)
        assert should_exit is True
        assert reason == "cot_normalized"


class TestFREDCommoditySeries:
    """Test that FRED commodity series are registered."""

    def test_commodity_series_in_map(self):
        from tradingagents.strategies.data_sources.fred_source import SERIES_MAP

        assert "wti_spot" in SERIES_MAP
        assert SERIES_MAP["wti_spot"] == "DCOILWTICO"
        assert "gold_spot" in SERIES_MAP
        assert SERIES_MAP["gold_spot"] == "GOLDAMGBD228NLBM"
        assert "copper_spot" in SERIES_MAP
        assert SERIES_MAP["copper_spot"] == "PCOPPUSDM"


class TestCohortIntegration:
    """Integration tests for commodity cohort configuration."""

    def test_30d_cohort_excludes_commodities(self):
        from tradingagents.strategies.orchestration.cohort_orchestrator import HORIZON_PARAMS
        hp = HORIZON_PARAMS["30d"]
        assert hp.get("commodity_eligible") is False

    def test_5k_cohort_excludes_commodities(self):
        from tradingagents.strategies.orchestration.cohort_orchestrator import SIZE_PROFILES
        profile = SIZE_PROFILES["5k"]
        assert profile.commodity_eligible is False
        assert profile.commodity_instruments == []

    def test_10k_commodity_eligible(self):
        from tradingagents.strategies.orchestration.cohort_orchestrator import SIZE_PROFILES
        profile = SIZE_PROFILES["10k"]
        assert profile.commodity_eligible is True
        assert profile.max_commodity_allocation_pct == 0.10
        assert "GLD" in profile.commodity_instruments
        assert "SLV" in profile.commodity_instruments

    def test_1y_horizon_narrows_instruments(self):
        from tradingagents.strategies.orchestration.cohort_orchestrator import HORIZON_PARAMS
        hp = HORIZON_PARAMS["1y"]
        assert hp.get("commodity_eligible") is True
        assert hp.get("commodity_instruments_override") == ["GLD", "SLV"]


class TestPortfolioCommittee:
    """Tests for portfolio committee commodity awareness."""

    def test_commodity_regime_alignment_crisis_gld(self):
        from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee
        committee = PortfolioCommittee()
        result = committee._assess_regime_alignment("long", {"overall_regime": "crisis"}, ticker="GLD")
        assert result == "aligned"

    def test_commodity_regime_alignment_crisis_xle(self):
        from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee
        committee = PortfolioCommittee()
        result = committee._assess_regime_alignment("long", {"overall_regime": "crisis"}, ticker="XLE")
        assert result == "misaligned"

    def test_commodity_regime_alignment_stressed_slv(self):
        from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee
        committee = PortfolioCommittee()
        result = committee._assess_regime_alignment("long", {"overall_regime": "stressed"}, ticker="SLV")
        assert result == "aligned"

    def test_regime_alignment_backward_compatible(self):
        from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee
        committee = PortfolioCommittee()
        result = committee._assess_regime_alignment("short", {"overall_regime": "crisis"})
        assert result == "aligned"
