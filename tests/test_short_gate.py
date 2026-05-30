"""Tests for the short conviction gate.

A short clears the gate if 2+ strategies short the same ticker, OR a single
strategy shorts it with LLM conviction >= PortfolioCommittee.SHORT_CONVICTION_THRESHOLD
(0.6). Conviction is the genuine LLM conviction (metadata.llm_analysis.conviction),
NOT the raw rule score. Enforced at four layers: signal pre-filter, LLM system
prompt rule, LLM-output post-filter, and the rule-based fallback path.
"""
from __future__ import annotations

import json
from unittest.mock import patch

from tradingagents.strategies.orchestration.cohort_orchestrator import PortfolioSizeProfile
from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee

THRESH = PortfolioCommittee.SHORT_CONVICTION_THRESHOLD  # 0.6


def _short(ticker, strategy, conviction, score=None):
    """A short signal whose LLM conviction lives in metadata.llm_analysis."""
    return {
        "ticker": ticker, "direction": "short",
        "score": conviction if score is None else score,
        "strategy": strategy,
        "metadata": {"llm_analysis": {"conviction": conviction}},
    }


def _long(ticker, strategy, score):
    return {"ticker": ticker, "direction": "long", "score": score,
            "strategy": strategy, "metadata": {"llm_analysis": {"conviction": score}}}


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
        assert PortfolioCommittee._short_passes_gate([_short("AAPL", "supply_chain", 0.62)]) is True

    def test_single_low_conviction_fails(self):
        assert PortfolioCommittee._short_passes_gate([_short("LCID", "litigation", 0.55)]) is False

    def test_raw_rule_score_not_treated_as_conviction(self):
        # Congressional cluster score 12, no llm_analysis -> conviction 0, gate fails.
        sigs = [{"ticker": "QCOM", "direction": "short", "score": 12.0,
                 "strategy": "congressional_trades", "metadata": {}}]
        assert PortfolioCommittee._short_passes_gate(sigs) is False

    def test_rule_score_in_unit_range_still_not_conviction(self):
        # earnings_call score 1.0 but no LLM analysis -> conviction 0, gate fails
        # (this is the FUFU case that previously slipped through the score fallback).
        sigs = [{"ticker": "FUFU", "direction": "short", "score": 1.0,
                 "strategy": "earnings_call", "metadata": {}}]
        assert PortfolioCommittee._short_passes_gate(sigs) is False

    def test_two_strategies_pass_regardless_of_conviction(self):
        sigs = [_short("TSLA", "litigation", 0.1), _short("TSLA", "congressional_trades", 0.1)]
        assert PortfolioCommittee._short_passes_gate(sigs) is True


# ---------------------------------------------------------------------------
# Rule-based path
# ---------------------------------------------------------------------------

class TestRuleBasedShortGate:
    def test_single_strategy_low_conviction_short_rejected(self):
        committee = _rule_based_committee()
        recs = committee.synthesize([_short("LCID", "litigation", 0.55)], total_capital=50_000)
        assert all(r.ticker != "LCID" for r in recs)

    def test_single_strategy_high_conviction_short_accepted(self):
        committee = _rule_based_committee()
        recs = committee.synthesize(
            [_short("AAPL", "supply_chain", 0.7)], total_capital=50_000,
            strategy_confidence={"supply_chain": 1.0},
        )
        assert any(r.ticker == "AAPL" and r.direction == "short" for r in recs), \
            f"high-conviction single-strategy short should pass, got {recs}"

    def test_rule_score_short_without_conviction_rejected(self):
        committee = _rule_based_committee()
        # earnings_call FUFU: rule score 1.0, no LLM conviction -> must NOT clear.
        signals = [{"ticker": "FUFU", "direction": "short", "score": 1.0,
                    "strategy": "earnings_call", "metadata": {}}]
        recs = committee.synthesize(
            signals, total_capital=50_000, strategy_confidence={"earnings_call": 1.0},
        )
        assert all(r.ticker != "FUFU" for r in recs), \
            f"conviction-0 rule-score short should be rejected, got {recs}"

    def test_two_strategy_short_accepted(self):
        committee = _rule_based_committee()
        signals = [_short("AAPL", "litigation", 0.7), _short("AAPL", "congressional_trades", 0.6)]
        recs = committee.synthesize(signals, total_capital=50_000)
        short_recs = [r for r in recs if r.ticker == "AAPL"]
        assert len(short_recs) == 1 and short_recs[0].direction == "short"

    def test_single_strategy_long_accepted_at_threshold(self):
        committee = _rule_based_committee()
        recs = committee.synthesize(
            [_long("MSFT", "earnings_call", 0.6)], total_capital=50_000,
            strategy_confidence={"earnings_call": 1.0},
        )
        assert any(r.ticker == "MSFT" and r.direction == "long" for r in recs)

    def test_short_blocked_for_long_only_cohort_takes_precedence(self):
        committee = _rule_based_committee(short_eligible=False)
        signals = [_short("AAPL", "litigation", 0.9), _short("AAPL", "congressional_trades", 0.7)]
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
            _short("LCID", "litigation", 0.5),            # low conviction -> dropped
            _short("AAPL", "supply_chain", 0.7),          # high conviction -> kept
            _short("TSLA", "litigation", 0.3),            # +
            _short("TSLA", "congressional_trades", 0.3),  # = 2 strategies -> kept
            _long("MSFT", "earnings_call", 0.8),
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
            signals=[_short("AAPL", "supply_chain", 0.7)], total_capital=50_000,
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
            signals=[_short("AAPL", "supply_chain", 0.7)], total_capital=50_000,
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
            signals=[_long("MSFT", "earnings_call", 0.8)], total_capital=50_000,
        )
        assert any(r.ticker == "MSFT" and r.direction == "long" for r in recs)
