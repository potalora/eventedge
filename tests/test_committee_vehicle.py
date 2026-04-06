"""Tests for portfolio committee vehicle selection and short book limits."""
from __future__ import annotations

from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee
from tradingagents.strategies.orchestration.cohort_orchestrator import PortfolioSizeProfile


class TestCommitteeVehicleSelection:
    def _make_committee(self, short_eligible=True, max_short_exposure=0.15):
        profile = PortfolioSizeProfile(
            name="test", total_capital=50_000, max_position_pct=0.10,
            min_position_value=2_500, max_positions=15,
            sector_concentration_cap=0.30, cash_reserve_pct=0.15,
            short_eligible=short_eligible,
            options_eligible=["covered_call"],
            max_short_exposure_pct=max_short_exposure,
            max_correlated_shorts=2,
        )
        config = {"autoresearch": {"paper_trade": {"portfolio_committee_enabled": False}}}
        return PortfolioCommittee(config, size_profile=profile)

    def test_short_signal_accepted_when_eligible(self):
        committee = self._make_committee(short_eligible=True)
        signals = [
            {"ticker": "AAPL", "direction": "short", "score": 0.8, "strategy": "litigation", "metadata": {}},
            {"ticker": "AAPL", "direction": "short", "score": 0.7, "strategy": "congressional_trades", "metadata": {}},
        ]
        recs = committee.synthesize(signals, total_capital=50_000)
        assert len(recs) >= 1
        assert recs[0].direction == "short"

    def test_short_signal_dropped_when_ineligible(self):
        committee = self._make_committee(short_eligible=False)
        signals = [
            {"ticker": "AAPL", "direction": "short", "score": 0.8, "strategy": "litigation", "metadata": {}},
            {"ticker": "AAPL", "direction": "short", "score": 0.7, "strategy": "congressional_trades", "metadata": {}},
        ]
        recs = committee.synthesize(signals, total_capital=50_000)
        short_recs = [r for r in recs if r.direction == "short"]
        assert len(short_recs) == 0

    def test_long_signals_unaffected_by_short_ineligibility(self):
        committee = self._make_committee(short_eligible=False)
        signals = [
            {"ticker": "MSFT", "direction": "long", "score": 0.8, "strategy": "earnings_call", "metadata": {}},
            {"ticker": "MSFT", "direction": "long", "score": 0.7, "strategy": "insider_activity", "metadata": {}},
        ]
        recs = committee.synthesize(signals, total_capital=50_000)
        assert len(recs) >= 1
        assert recs[0].direction == "long"

    def test_short_exposure_capped(self):
        committee = self._make_committee(short_eligible=True, max_short_exposure=0.15)
        signals = []
        for i, strat_pair in enumerate([
            ("litigation", "congressional_trades"),
            ("regulatory_pipeline", "supply_chain"),
        ]):
            for strat in strat_pair:
                signals.append({
                    "ticker": f"T{i}", "direction": "short", "score": 0.9,
                    "strategy": strat, "metadata": {},
                })
        recs = committee.synthesize(signals, total_capital=50_000)
        short_recs = [r for r in recs if r.direction == "short"]
        total_short_pct = sum(r.position_size_pct for r in short_recs)
        assert total_short_pct <= 0.15 + 0.001

    def test_no_profile_allows_all(self):
        """When no size_profile is set, shorts are allowed (backward compat)."""
        config = {"autoresearch": {"paper_trade": {"portfolio_committee_enabled": False}}}
        committee = PortfolioCommittee(config, size_profile=None)
        signals = [
            {"ticker": "AAPL", "direction": "short", "score": 0.8, "strategy": "litigation", "metadata": {}},
            {"ticker": "AAPL", "direction": "short", "score": 0.7, "strategy": "congressional_trades", "metadata": {}},
        ]
        recs = committee.synthesize(signals, total_capital=50_000)
        short_recs = [r for r in recs if r.direction == "short"]
        assert len(short_recs) >= 1
