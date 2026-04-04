"""Tests for the enhanced WeatherAg strategy.

Covers: expanded tickers, year-round operation, gate logic,
LLM metadata bundling, seasonal ticker filtering, graceful degradation.
"""
from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from tradingagents.strategies.modules.weather_ag import (
    WeatherAgStrategy,
    AG_TICKERS_FULL,
    AG_TICKERS_WINTER,
)
from tradingagents.strategies.modules.base import Candidate


@pytest.fixture()
def strategy():
    return WeatherAgStrategy()


@pytest.fixture()
def price_data():
    """Build mock price DataFrames for ag tickers with upward trend."""
    dates = pd.bdate_range("2025-03-01", periods=60)
    prices = {}
    for ticker in AG_TICKERS_FULL.values():
        df = pd.DataFrame(
            {"Close": [100 + i * 0.5 for i in range(60)]},
            index=dates,
        )
        prices[ticker] = df
    return prices


@pytest.fixture()
def flat_price_data():
    """Build mock price DataFrames with no momentum."""
    dates = pd.bdate_range("2025-03-01", periods=60)
    prices = {}
    for ticker in AG_TICKERS_FULL.values():
        prices[ticker] = pd.DataFrame({"Close": [100.0] * 60}, index=dates)
    return prices


def _make_data(
    prices: dict,
    noaa: dict | None = None,
    drought: dict | None = None,
    usda: dict | None = None,
) -> dict:
    """Build a data dict matching engine output."""
    data: dict = {"yfinance": {"prices": prices}}
    if noaa is not None:
        data["noaa"] = noaa
    if drought is not None:
        data["drought_monitor"] = drought
    if usda is not None:
        data["usda"] = usda
    return data


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------

class TestProtocol:
    def test_name(self, strategy):
        assert strategy.name == "weather_ag"

    def test_track(self, strategy):
        assert strategy.track == "paper_trade"

    def test_data_sources(self, strategy):
        assert "yfinance" in strategy.data_sources
        assert "noaa" in strategy.data_sources
        assert "usda" in strategy.data_sources
        assert "drought_monitor" in strategy.data_sources
        assert "openbb" in strategy.data_sources

    def test_param_space_has_new_params(self, strategy):
        space = strategy.get_param_space()
        assert "drought_min_score" in space
        assert "crop_decline_threshold" in space

    def test_default_params_has_new_params(self, strategy):
        defaults = strategy.get_default_params()
        assert "drought_min_score" in defaults
        assert "crop_decline_threshold" in defaults
        assert "season_start_month" not in defaults
        assert "season_end_month" not in defaults

    def test_screen_empty_data(self, strategy):
        result = strategy.screen({}, "2025-06-15", strategy.get_default_params())
        assert result == []

    def test_check_exit_returns_tuple(self, strategy):
        should_exit, reason = strategy.check_exit("DBA", 100, 105, 5, strategy.get_default_params(), {})
        assert isinstance(should_exit, bool)
        assert isinstance(reason, str)


# ---------------------------------------------------------------------------
# Expanded tickers
# ---------------------------------------------------------------------------

class TestTickerUniverse:
    def test_full_universe_has_expected_tickers(self):
        assert len(AG_TICKERS_FULL) >= 16

    def test_full_includes_etfs_and_stocks(self):
        assert "DBA" in AG_TICKERS_FULL.values()
        assert "ADM" in AG_TICKERS_FULL.values()
        assert "DE" in AG_TICKERS_FULL.values()
        assert "SOYB" in AG_TICKERS_FULL.values()

    def test_winter_subset_excludes_corn_soy(self):
        assert "corn" not in AG_TICKERS_WINTER
        assert "soyb" not in AG_TICKERS_WINTER
        assert "weat" in AG_TICKERS_WINTER
        assert "dba" in AG_TICKERS_WINTER


# ---------------------------------------------------------------------------
# Year-round operation
# ---------------------------------------------------------------------------

class TestYearRound:
    def test_growing_season_returns_candidates(self, strategy, price_data):
        data = _make_data(
            price_data,
            drought={"composite_score": 1.5, "states": {}},
        )
        result = strategy.screen(data, "2025-05-20", strategy.get_default_params())
        assert isinstance(result, list)
        assert len(result) > 0

    def test_winter_returns_candidates_with_drought(self, strategy, price_data):
        data = _make_data(
            price_data,
            drought={"composite_score": 1.5, "states": {}},
        )
        result = strategy.screen(data, "2025-01-15", strategy.get_default_params())
        assert isinstance(result, list)
        # Should only have winter-eligible tickers
        winter_tickers = {AG_TICKERS_FULL[k] for k in AG_TICKERS_WINTER}
        for c in result:
            assert c.ticker in winter_tickers

    def test_winter_excludes_summer_only_tickers(self, strategy, price_data):
        data = _make_data(
            price_data,
            drought={"composite_score": 2.0, "states": {}},
        )
        result = strategy.screen(data, "2025-12-15", strategy.get_default_params())
        summer_only = {"CORN", "SOYB", "CTVA", "FMC", "DE"}
        for c in result:
            assert c.ticker not in summer_only


# ---------------------------------------------------------------------------
# Gate logic
# ---------------------------------------------------------------------------

class TestGateLogic:
    def test_no_signals_when_nothing_interesting(self, strategy, flat_price_data):
        """All data present but below thresholds → empty."""
        data = _make_data(
            flat_price_data,
            noaa={"heat_stress_days": 0, "precip_deficit_pct": 0, "frost_events": 0},
            drought={"composite_score": 0.1, "states": {}},
            usda={"crop_progress": {"CORN": []}},
        )
        result = strategy.screen(data, "2025-05-20", strategy.get_default_params())
        assert result == []

    def test_drought_gate_triggers(self, strategy, price_data):
        data = _make_data(
            price_data,
            drought={"composite_score": 1.5, "states": {}},
        )
        result = strategy.screen(data, "2025-06-15", strategy.get_default_params())
        assert len(result) > 0

    def test_noaa_heat_gate_triggers(self, strategy, price_data):
        data = _make_data(
            price_data,
            noaa={"heat_stress_days": 10, "precip_deficit_pct": 0, "frost_events": 0},
        )
        result = strategy.screen(data, "2025-07-15", strategy.get_default_params())
        assert len(result) > 0

    def test_noaa_precip_gate_triggers(self, strategy, price_data):
        data = _make_data(
            price_data,
            noaa={"heat_stress_days": 0, "precip_deficit_pct": -35, "frost_events": 0},
        )
        result = strategy.screen(data, "2025-07-15", strategy.get_default_params())
        assert len(result) > 0

    def test_noaa_frost_gate_triggers(self, strategy, price_data):
        data = _make_data(
            price_data,
            noaa={"heat_stress_days": 0, "precip_deficit_pct": 0, "frost_events": 2},
        )
        result = strategy.screen(data, "2025-04-20", strategy.get_default_params())
        assert len(result) > 0

    def test_momentum_gate_triggers(self, strategy):
        """High momentum alone should trigger gate."""
        dates = pd.bdate_range("2025-06-01", periods=30)
        prices = {}
        for ticker in AG_TICKERS_FULL.values():
            prices[ticker] = pd.DataFrame(
                {"Close": [100 + i * 1.0 for i in range(30)]},
                index=dates,
            )
        data = _make_data(prices)
        result = strategy.screen(data, "2025-07-10", strategy.get_default_params())
        assert len(result) > 0

    def test_usda_crop_decline_gate_triggers(self, strategy, price_data):
        usda = {"crop_progress": {"CORN": [
            {"week_ending": "2025-06-08", "good_pct": 50, "excellent_pct": 20},
            {"week_ending": "2025-06-15", "good_pct": 45, "excellent_pct": 17},
        ]}}
        data = _make_data(price_data, usda=usda)
        result = strategy.screen(data, "2025-06-15", strategy.get_default_params())
        assert len(result) > 0

    def test_max_3_candidates(self, strategy, price_data):
        data = _make_data(
            price_data,
            drought={"composite_score": 2.0, "states": {}},
        )
        result = strategy.screen(data, "2025-06-15", strategy.get_default_params())
        assert len(result) <= 3


# ---------------------------------------------------------------------------
# LLM metadata bundling
# ---------------------------------------------------------------------------

class TestLLMMetadata:
    def test_candidates_have_llm_flags(self, strategy, price_data):
        data = _make_data(
            price_data,
            drought={"composite_score": 1.5, "states": {"IA": {"D2": 30}}},
        )
        result = strategy.screen(data, "2025-06-15", strategy.get_default_params())
        assert len(result) > 0
        for c in result:
            assert c.metadata.get("needs_llm_analysis") is True
            assert c.metadata.get("analysis_type") == "ag_weather"

    def test_candidates_bundle_raw_data(self, strategy, price_data):
        drought_data = {"composite_score": 1.5, "states": {"IA": {"D2": 30}}}
        noaa_data = {"heat_stress_days": 8, "precip_deficit_pct": -30, "frost_events": 0, "avg_temp_anomaly_f": 3.5}
        data = _make_data(price_data, drought=drought_data, noaa=noaa_data)
        result = strategy.screen(data, "2025-06-15", strategy.get_default_params())
        assert len(result) > 0
        meta = result[0].metadata
        assert "drought_score" in meta
        assert meta["drought_score"] == 1.5
        assert "noaa_data" in meta
        assert meta["noaa_data"]["heat_stress_days"] == 8

    def test_candidates_have_trailing_return(self, strategy, price_data):
        data = _make_data(price_data, drought={"composite_score": 1.5, "states": {}})
        result = strategy.screen(data, "2025-06-15", strategy.get_default_params())
        assert len(result) > 0
        for c in result:
            assert "trailing_return" in c.metadata
            assert isinstance(c.metadata["trailing_return"], float)

    def test_initial_score_is_half(self, strategy, price_data):
        data = _make_data(price_data, drought={"composite_score": 1.5, "states": {}})
        result = strategy.screen(data, "2025-06-15", strategy.get_default_params())
        for c in result:
            assert c.score == 0.5

    def test_direction_is_long(self, strategy, price_data):
        data = _make_data(price_data, drought={"composite_score": 1.5, "states": {}})
        result = strategy.screen(data, "2025-06-15", strategy.get_default_params())
        for c in result:
            assert c.direction == "long"


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------

class TestGracefulDegradation:
    def test_works_with_only_momentum(self, strategy):
        """No NOAA, no USDA, no drought — momentum alone."""
        dates = pd.bdate_range("2025-06-01", periods=30)
        prices = {}
        for ticker in AG_TICKERS_FULL.values():
            prices[ticker] = pd.DataFrame(
                {"Close": [100 + i * 1.0 for i in range(30)]},
                index=dates,
            )
        data = _make_data(prices)
        result = strategy.screen(data, "2025-07-10", strategy.get_default_params())
        assert isinstance(result, list)

    def test_empty_prices_returns_empty(self, strategy):
        data = _make_data({}, drought={"composite_score": 2.0, "states": {}})
        result = strategy.screen(data, "2025-06-15", strategy.get_default_params())
        assert result == []

    def test_screen_with_no_data_returns_empty(self, strategy):
        result = strategy.screen({}, "2025-06-15", strategy.get_default_params())
        assert result == []


# ---------------------------------------------------------------------------
# Crop decline helper
# ---------------------------------------------------------------------------

class TestCropDecline:
    def test_no_data_returns_zero(self):
        assert WeatherAgStrategy._check_crop_decline({}) == 0.0

    def test_no_crop_progress_returns_zero(self):
        assert WeatherAgStrategy._check_crop_decline({"crop_progress": {}}) == 0.0

    def test_single_week_returns_zero(self):
        usda = {"crop_progress": {"CORN": [
            {"good_pct": 50, "excellent_pct": 20},
        ]}}
        assert WeatherAgStrategy._check_crop_decline(usda) == 0.0

    def test_computes_decline(self):
        usda = {"crop_progress": {"CORN": [
            {"good_pct": 50, "excellent_pct": 20},
            {"good_pct": 45, "excellent_pct": 17},
        ]}}
        assert WeatherAgStrategy._check_crop_decline(usda) == 8  # 70 - 62 = 8

    def test_max_across_commodities(self):
        usda = {"crop_progress": {
            "CORN": [
                {"good_pct": 50, "excellent_pct": 20},
                {"good_pct": 48, "excellent_pct": 19},  # decline = 3
            ],
            "WHEAT": [
                {"good_pct": 40, "excellent_pct": 15},
                {"good_pct": 35, "excellent_pct": 10},  # decline = 10
            ],
        }}
        assert WeatherAgStrategy._check_crop_decline(usda) == 10


# ---------------------------------------------------------------------------
# LLM Analyzer integration
# ---------------------------------------------------------------------------

class TestLLMAnalyzerIntegration:
    def test_ag_weather_prompt_exists(self):
        from tradingagents.strategies.learning.llm_analyzer import _DEFAULT_PROMPTS
        assert "ag_weather" in _DEFAULT_PROMPTS

    def test_analyze_ag_weather_returns_dict(self):
        from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer
        analyzer = LLMAnalyzer()
        mock_response = '{"direction": "long", "score": 0.7, "reasoning": "Drought conditions severe"}'
        with patch.object(analyzer, "_call_llm", return_value=mock_response):
            result = analyzer.analyze_ag_weather(
                ticker="DBA",
                commodity_name="Invesco DB Agriculture Fund",
                ag_context={
                    "drought_score": 1.5,
                    "noaa_data": {"heat_stress_days": 8},
                    "usda_data": {},
                },
                trailing_return=0.03,
                hold_days=21,
            )
        assert result["direction"] == "long"
        assert result["score"] == 0.7


# ---------------------------------------------------------------------------
# Gen 004: loosened gates, expanded universe, 30-day horizon
# ---------------------------------------------------------------------------

class TestGen004:
    def test_default_params_aligned_to_30d_horizon(self):
        strategy = WeatherAgStrategy()
        params = strategy.get_default_params()
        assert 20 <= params["hold_days"] <= 30, "hold_days should target ~25 days"
        assert params["drought_min_score"] == 0.3, "drought gate should be loose"
        assert params["heat_stress_threshold"] == 2, "heat gate should be loose"
        assert params["precip_deficit_threshold"] == -10, "precip gate should be loose"

    def test_expanded_ticker_universe(self):
        """Verify curated expansion adds food/bev and fertilizer names."""
        tickers = set(AG_TICKERS_FULL.values())
        # Food/beverage
        assert "PEP" in tickers
        assert "KO" in tickers
        assert "GIS" in tickers
        assert "MDLZ" in tickers
        # Fertilizer
        assert "MOS" in tickers
        assert "NTR" in tickers
        assert len(tickers) >= 16, f"Expected >=16 curated tickers, got {len(tickers)}"

    def test_winter_subset_expanded(self):
        """Winter subset should include food/bev names."""
        assert "pep" in AG_TICKERS_WINTER
        assert "ko" in AG_TICKERS_WINTER

    def test_param_space_hold_days_floor(self):
        """Hold days range should have 20-day floor for 30-day horizon."""
        strategy = WeatherAgStrategy()
        space = strategy.get_param_space()
        assert space["hold_days"][0] >= 20, "hold_days floor should be >= 20"
