"""Tests for TradeRecommendation vehicle field."""
from __future__ import annotations

from tradingagents.strategies.trading.portfolio_committee import TradeRecommendation
from tradingagents.strategies.modules.base import OptionSpec


class TestTradeRecommendationVehicle:
    def test_default_vehicle_equity(self):
        rec = TradeRecommendation(
            ticker="AAPL", direction="long",
            position_size_pct=0.05, confidence=0.8, rationale="test",
        )
        assert rec.vehicle == "equity"
        assert rec.option_spec is None

    def test_covered_call_recommendation(self):
        spec = OptionSpec(strategy="covered_call", expiry_target_days=30, strike_offset_pct=0.05, max_premium_pct=0.03)
        rec = TradeRecommendation(
            ticker="AAPL", direction="long",
            position_size_pct=0.05, confidence=0.8, rationale="overlay",
            vehicle="option", option_spec=spec,
        )
        assert rec.vehicle == "option"
        assert rec.option_spec.strategy == "covered_call"

    def test_backward_compat(self):
        """Existing code creating TradeRecommendation without vehicle still works."""
        rec = TradeRecommendation(
            ticker="MSFT", direction="short",
            position_size_pct=0.08, confidence=0.9, rationale="bearish",
            contributing_strategies=["litigation", "congressional_trades"],
            regime_alignment="aligned",
        )
        assert rec.vehicle == "equity"
        assert rec.option_spec is None
