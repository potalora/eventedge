"""Tests for covered call overlay generation."""
from __future__ import annotations

from unittest.mock import patch, MagicMock
from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee
from tradingagents.strategies.orchestration.cohort_orchestrator import PortfolioSizeProfile


class TestCoveredCallOverlay:
    def _make_committee(self, options_eligible=None):
        profile = PortfolioSizeProfile(
            name="50k", total_capital=50_000, max_position_pct=0.10,
            min_position_value=2_500, max_positions=15,
            sector_concentration_cap=0.30, cash_reserve_pct=0.15,
            options_eligible=options_eligible or ["covered_call"],
            max_options_premium_pct=0.05,
        )
        config = {"autoresearch": {"paper_trade": {"portfolio_committee_enabled": True}}}
        return PortfolioCommittee(config, size_profile=profile)

    def test_generate_overlay_candidates(self):
        committee = self._make_committee()
        positions = [
            {"ticker": "AAPL", "direction": "long", "entry_price": 150.0,
             "entry_date": "2026-03-01", "shares": 10},
        ]
        mock_llm_result = [
            {"ticker": "AAPL", "strike_offset_pct": 0.05, "expiry_days": 30,
             "rationale": "Low IV, sideways"}
        ]
        with patch.object(committee, "_llm_covered_call_overlay", return_value=mock_llm_result):
            overlays = committee.generate_covered_call_overlays(
                current_positions=positions,
                iv_data={"AAPL": {"iv_rank": 35, "iv": 0.30}},
                earnings_dates={"AAPL": 30},
                trading_date="2026-04-04",
            )
        assert len(overlays) == 1
        assert overlays[0].ticker == "AAPL"
        assert overlays[0].vehicle == "option"
        assert overlays[0].option_spec.strategy == "covered_call"
        assert overlays[0].option_spec.expiry_target_days == 30

    def test_no_overlay_when_not_eligible(self):
        profile = PortfolioSizeProfile(
            name="5k", total_capital=5_000, max_position_pct=0.25,
            min_position_value=500, max_positions=5,
            sector_concentration_cap=0.50, cash_reserve_pct=0.10,
            options_eligible=[],
        )
        config = {"autoresearch": {"paper_trade": {"portfolio_committee_enabled": False}}}
        committee = PortfolioCommittee(config, size_profile=profile)
        overlays = committee.generate_covered_call_overlays(
            current_positions=[{"ticker": "AAPL", "direction": "long", "entry_price": 150.0, "entry_date": "2026-03-01", "shares": 10}],
            iv_data={}, earnings_dates={}, trading_date="2026-04-04",
        )
        assert len(overlays) == 0

    def test_no_overlay_on_short_positions(self):
        committee = self._make_committee()
        positions = [
            {"ticker": "AAPL", "direction": "short", "entry_price": 150.0,
             "entry_date": "2026-03-01", "shares": 10},
        ]
        with patch.object(committee, "_llm_covered_call_overlay", return_value=[]) as mock:
            overlays = committee.generate_covered_call_overlays(
                current_positions=positions,
                iv_data={}, earnings_dates={}, trading_date="2026-04-04",
            )
        assert len(overlays) == 0

    def test_overlay_position_size_is_zero(self):
        """Overlay is not a new position — position_size_pct should be 0."""
        committee = self._make_committee()
        mock_result = [{"ticker": "AAPL", "strike_offset_pct": 0.05, "expiry_days": 30, "rationale": "test"}]
        with patch.object(committee, "_llm_covered_call_overlay", return_value=mock_result):
            overlays = committee.generate_covered_call_overlays(
                current_positions=[{"ticker": "AAPL", "direction": "long", "entry_price": 150.0, "entry_date": "2026-03-01", "shares": 10}],
                iv_data={}, earnings_dates={}, trading_date="2026-04-04",
            )
        assert overlays[0].position_size_pct == 0.0

    def test_llm_failure_returns_empty(self):
        committee = self._make_committee()
        with patch.object(committee, "_llm_covered_call_overlay", return_value=[]):
            overlays = committee.generate_covered_call_overlays(
                current_positions=[{"ticker": "AAPL", "direction": "long", "entry_price": 150.0, "entry_date": "2026-03-01", "shares": 10}],
                iv_data={}, earnings_dates={}, trading_date="2026-04-04",
            )
        assert len(overlays) == 0
