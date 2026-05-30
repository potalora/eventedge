"""Tests for the short conviction gate.

A short clears the gate if 2+ strategies short the same ticker, OR a single
strategy shorts it with LLM conviction >= PortfolioCommittee.SHORT_CONVICTION_THRESHOLD
(0.6). Enforced at four layers: signal pre-filter, LLM system prompt rule,
LLM-output post-filter, and the rule-based fallback path. Long signals unaffected.
"""
from __future__ import annotations

import json
from unittest.mock import patch

from tradingagents.strategies.orchestration.cohort_orchestrator import PortfolioSizeProfile
from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee

THRESH = PortfolioCommittee.SHORT_CONVICTION_THRESHOLD  # 0.6


def _profile(short_eligible: bool = True) -> PortfolioSizeProfile:
    return PortfolioSizeProfile(
        name="test", total_capital=50_000, max_position_pct=0.10,
        min_position_value=2_500, max_positions=15,
        sector_concentration_cap=0.30, cash_reserve_pct=0.15,
        short_eligible=short_eligible,
        options_eligible=["covered_call"],
        max_short_exposure_pct=0.20,
        max_correlated_shorts=3,
    )


def _rule_based_committee(short_eligible: bool = True) -> PortfolioCommittee:
    config = {"autoresearch": {"paper_trade": {"portfolio_committee_enabled": False}}}
    return PortfolioCommittee(config, size_profile=_profile(short_eligible))


def _llm_committee(short_eligible: bool = True) -> PortfolioCommittee:
    config = {"autoresearch": {"paper_trade": {"portfolio_committee_enabled": True}}}
    return PortfolioCommittee(config, size_profile=_profile(short_eligible))


# ---------------------------------------------------------------------------
# Conviction helper / gate unit
# ---------------------------------------------------------------------------

class TestConvictionGateUnit:
    def test_single_high_conviction_passes(self):
        sigs = [{"ticker": "AAPL", "direction": "short", "score": 0.62,
                 "strategy": "supply_chain", "metadata": {}}]
        assert PortfolioCommittee._short_passes_gate(sigs) is True

    def test_single_low_conviction_fails(self):
        sigs = [{"ticker": "LCID", "direction": "short", "score": 0.55,
                 "strategy": "litigation", "metadata": {}}]
        assert PortfolioCommittee._short_passes_gate(sigs) is False

    def test_raw_rule_score_not_treated_as_conviction(self):
        # Congressional cluster score 12 is not on the 0-1 conviction scale.
        sigs = [{"ticker": "QCOM", "direction": "short", "score": 12.0,
                 "strategy": "congressional_trades", "metadata": {}}]
        assert PortfolioCommittee._short_passes_gate(sigs) is False

    def test_metadata_conviction_preferred(self):
        sigs = [{"ticker": "QCOM", "direction": "short", "score": 12.0,
                 "strategy": "congressional_trades",
                 "metadata": {"llm_analysis": {"conviction": 0.8}}}]
        assert PortfolioCommittee._short_passes_gate(sigs) is True

    def test_two_strategies_pass_regardless_of_conviction(self):
        sigs = [
            {"ticker": "TSLA", "direction": "short", "score": 0.1,
             "strategy": "litigation", "metadata": {}},
            {"ticker": "TSLA", "direction": "short", "score": 0.1,
             "strategy": "congressional_trades", "metadata": {}},
        ]
        assert PortfolioCommittee._short_passes_gate(sigs) is True


# ---------------------------------------------------------------------------
# Rule-based path
# ---------------------------------------------------------------------------

class TestRuleBasedShortGate:
    def test_single_strategy_low_conviction_short_rejected(self):
        committee = _rule_based_committee()
        signals = [{"ticker": "LCID", "direction": "short", "score": 0.55,
                    "strategy": "litigation", "metadata": {}}]
        recs = committee.synthesize(signals, total_capital=50_000)
        assert all(r.ticker != "LCID" for r in recs)

    def test_single_strategy_high_conviction_short_accepted(self):
        committee = _rule_based_committee()
        signals = [{"ticker": "AAPL", "direction": "short", "score": 0.7,
                    "strategy": "supply_chain", "metadata": {}}]
        recs = committee.synthesize(
            signals, total_capital=50_000,
            strategy_confidence={"supply_chain": 1.0},
        )
        assert any(r.ticker == "AAPL" and r.direction == "short" for r in recs), \
            f"high-conviction single-strategy short should pass, got {recs}"

    def test_two_strategy_short_accepted(self):
        committee = _rule_based_committee()
        signals = [
            {"ticker": "AAPL", "direction": "short", "score": 0.7,
             "strategy": "litigation", "metadata": {}},
            {"ticker": "AAPL", "direction": "short", "score": 0.6,
             "strategy": "congressional_trades", "metadata": {}},
        ]
        recs = committee.synthesize(signals, total_capital=50_000)
        short_recs = [r for r in recs if r.ticker == "AAPL"]
        assert len(short_recs) == 1 and short_recs[0].direction == "short"

    def test_single_strategy_long_accepted_at_threshold(self):
        committee = _rule_based_committee()
        signals = [{"ticker": "MSFT", "direction": "long", "score": 0.6,
                    "strategy": "earnings_call", "metadata": {}}]
        recs = committee.synthesize(
            signals, total_capital=50_000,
            strategy_confidence={"earnings_call": 1.0},
        )
        assert any(r.ticker == "MSFT" and r.direction == "long" for r in recs)

    def test_short_blocked_for_long_only_cohort_takes_precedence(self):
        committee = _rule_based_committee(short_eligible=False)
        signals = [
            {"ticker": "AAPL", "direction": "short", "score": 0.9,
             "strategy": "litigation", "metadata": {}},
            {"ticker": "AAPL", "direction": "short", "score": 0.7,
             "strategy": "congressional_trades", "metadata": {}},
        ]
        recs = committee.synthesize(signals, total_capital=50_000)
        assert all(r.direction != "short" for r in recs)


# ---------------------------------------------------------------------------
# LLM path
# ---------------------------------------------------------------------------

class TestLLMShortGate:
    @patch.object(PortfolioCommittee, "_get_client")
    @patch.object(PortfolioCommittee, "_call_llm")
    def test_pre_filter_drops_low_conviction_keeps_high(self, mock_call, mock_client):
        mock_client.return_value = object()
        captured = {}

        def fake_call(*, system, prompt, max_tokens):
            captured["prompt"] = prompt
            return "[]"

        mock_call.side_effect = fake_call
        committee = _llm_committee()
        signals = [
            # Single-strategy LOW conviction — dropped
            {"ticker": "LCID", "direction": "short", "score": 0.5,
             "strategy": "litigation", "metadata": {}},
            # Single-strategy HIGH conviction — kept
            {"ticker": "AAPL", "direction": "short", "score": 0.7,
             "strategy": "supply_chain", "metadata": {}},
            # Two-strategy short — kept
            {"ticker": "TSLA", "direction": "short", "score": 0.3,
             "strategy": "litigation", "metadata": {}},
            {"ticker": "TSLA", "direction": "short", "score": 0.3,
             "strategy": "congressional_trades", "metadata": {}},
            {"ticker": "MSFT", "direction": "long", "score": 0.8,
             "strategy": "earnings_call", "metadata": {}},
        ]
        committee.synthesize(signals, total_capital=50_000)
        prompt = captured.get("prompt", "")
        assert "LCID short" not in prompt, "low-conviction single short leaked into prompt"
        assert "AAPL short" in prompt, "high-conviction single short was wrongly filtered"
        assert "TSLA short" in prompt, "multi-strategy short was wrongly filtered"
        assert "MSFT long" in prompt

    @patch.object(PortfolioCommittee, "_get_client")
    @patch.object(PortfolioCommittee, "_call_llm")
    def test_post_filter_drops_low_confidence_single_short(self, mock_call, mock_client):
        mock_client.return_value = object()
        mock_call.return_value = json.dumps([
            {"ticker": "AAPL", "direction": "short", "position_size_pct": 0.05,
             "confidence": 0.5, "rationale": "weak single short",
             "contributing_strategies": ["litigation"], "regime_alignment": "neutral"},
            {"ticker": "MSFT", "direction": "long", "position_size_pct": 0.05,
             "confidence": 0.8, "rationale": "earnings beat",
             "contributing_strategies": ["earnings_call"], "regime_alignment": "neutral"},
        ])
        committee = _llm_committee()
        recs = committee.synthesize(
            signals=[{"ticker": "AAPL", "direction": "short", "score": 0.7,
                      "strategy": "supply_chain", "metadata": {}}],
            total_capital=50_000,
        )
        assert all(r.ticker != "AAPL" for r in recs), "low-confidence single short should be dropped"
        assert any(r.ticker == "MSFT" for r in recs)

    @patch.object(PortfolioCommittee, "_get_client")
    @patch.object(PortfolioCommittee, "_call_llm")
    def test_post_filter_keeps_high_confidence_single_short(self, mock_call, mock_client):
        mock_client.return_value = object()
        mock_call.return_value = json.dumps([
            {"ticker": "AAPL", "direction": "short", "position_size_pct": 0.05,
             "confidence": 0.8, "rationale": "strong single short",
             "contributing_strategies": ["supply_chain"], "regime_alignment": "neutral"},
        ])
        committee = _llm_committee()
        recs = committee.synthesize(
            signals=[{"ticker": "AAPL", "direction": "short", "score": 0.7,
                      "strategy": "supply_chain", "metadata": {}}],
            total_capital=50_000,
        )
        assert any(r.ticker == "AAPL" and r.direction == "short" for r in recs), \
            f"high-confidence single short should survive, got {recs}"

    @patch.object(PortfolioCommittee, "_get_client")
    @patch.object(PortfolioCommittee, "_call_llm")
    def test_long_unchanged(self, mock_call, mock_client):
        mock_client.return_value = object()
        mock_call.return_value = json.dumps([
            {"ticker": "MSFT", "direction": "long", "position_size_pct": 0.05,
             "confidence": 0.7, "rationale": "earnings beat",
             "contributing_strategies": ["earnings_call"], "regime_alignment": "neutral"},
        ])
        committee = _llm_committee()
        recs = committee.synthesize(
            signals=[{"ticker": "MSFT", "direction": "long", "score": 0.8,
                      "strategy": "earnings_call", "metadata": {}}],
            total_capital=50_000,
        )
        assert any(r.ticker == "MSFT" and r.direction == "long" for r in recs)
