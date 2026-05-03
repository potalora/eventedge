"""Tests for the gen_007 short conviction gate.

Shorts must have 2+ strategy convergence on the same ticker. Single-strategy
shorts are rejected at three layers: signal pre-filter, LLM system prompt rule,
and LLM-output post-filter; plus the rule-based fallback path.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from tradingagents.strategies.orchestration.cohort_orchestrator import PortfolioSizeProfile
from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee


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
    """Committee with LLM disabled — exercises _rule_based_synthesize."""
    config = {"autoresearch": {"paper_trade": {"portfolio_committee_enabled": False}}}
    return PortfolioCommittee(config, size_profile=_profile(short_eligible))


def _llm_committee(short_eligible: bool = True) -> PortfolioCommittee:
    """Committee with LLM enabled. Caller patches _call_llm + _get_client."""
    config = {"autoresearch": {"paper_trade": {"portfolio_committee_enabled": True}}}
    return PortfolioCommittee(config, size_profile=_profile(short_eligible))


# ---------------------------------------------------------------------------
# Rule-based path
# ---------------------------------------------------------------------------

class TestRuleBasedShortGate:
    def test_single_strategy_short_rejected_high_consensus(self):
        committee = _rule_based_committee()
        signals = [
            {"ticker": "AAPL", "direction": "short", "score": 0.9,
             "strategy": "litigation", "metadata": {}},
        ]
        recs = committee.synthesize(signals, total_capital=50_000)
        assert all(r.ticker != "AAPL" for r in recs), \
            f"single-strategy short should be rejected, got {recs}"

    def test_single_strategy_long_accepted_at_threshold(self):
        committee = _rule_based_committee()
        signals = [
            {"ticker": "MSFT", "direction": "long", "score": 0.6,
             "strategy": "earnings_call", "metadata": {}},
        ]
        # consensus_score = score * confidence(default 0.5) = 0.3, below 0.5 threshold
        # Bump confidence so it crosses.
        recs = committee.synthesize(
            signals, total_capital=50_000,
            strategy_confidence={"earnings_call": 1.0},
        )
        assert any(r.ticker == "MSFT" and r.direction == "long" for r in recs), \
            f"single-strategy long at consensus 0.6 should pass, got {recs}"

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
        assert len(short_recs) == 1
        assert short_recs[0].direction == "short"

    def test_short_blocked_for_long_only_cohort_takes_precedence(self):
        committee = _rule_based_committee(short_eligible=False)
        signals = [
            # Even multi-strategy short rejected when cohort is long-only
            {"ticker": "AAPL", "direction": "short", "score": 0.8,
             "strategy": "litigation", "metadata": {}},
            {"ticker": "AAPL", "direction": "short", "score": 0.7,
             "strategy": "congressional_trades", "metadata": {}},
        ]
        recs = committee.synthesize(signals, total_capital=50_000)
        assert all(r.direction != "short" for r in recs), \
            f"long-only cohort should reject all shorts, got {recs}"


# ---------------------------------------------------------------------------
# LLM path
# ---------------------------------------------------------------------------

class TestLLMShortGate:
    @patch.object(PortfolioCommittee, "_get_client")
    @patch.object(PortfolioCommittee, "_call_llm")
    def test_llm_pre_filter_drops_single_strategy_shorts(self, mock_call, mock_client):
        """Pre-filter: single-strategy short signals should not appear in the prompt."""
        mock_client.return_value = object()  # truthy non-None client
        captured = {}

        def fake_call(*, system, prompt, max_tokens):
            captured["prompt"] = prompt
            return "[]"

        mock_call.side_effect = fake_call

        committee = _llm_committee()
        signals = [
            # Single-strategy short on AAPL — should be filtered out
            {"ticker": "AAPL", "direction": "short", "score": 0.9,
             "strategy": "litigation", "metadata": {}},
            # Two-strategy short on TSLA — should remain
            {"ticker": "TSLA", "direction": "short", "score": 0.7,
             "strategy": "litigation", "metadata": {}},
            {"ticker": "TSLA", "direction": "short", "score": 0.6,
             "strategy": "congressional_trades", "metadata": {}},
            # Long signal — unaffected
            {"ticker": "MSFT", "direction": "long", "score": 0.8,
             "strategy": "earnings_call", "metadata": {}},
        ]
        committee.synthesize(signals, total_capital=50_000)

        prompt = captured.get("prompt", "")
        # AAPL short must NOT appear; TSLA short and MSFT long must appear
        assert "AAPL short" not in prompt, "single-strategy AAPL short leaked into prompt"
        assert "TSLA short" in prompt, "multi-strategy TSLA short was wrongly filtered"
        assert "MSFT long" in prompt, "long signal should be unaffected"

    @patch.object(PortfolioCommittee, "_get_client")
    @patch.object(PortfolioCommittee, "_call_llm")
    def test_llm_post_filter_drops_noncompliant_short(self, mock_call, mock_client):
        """Post-filter: drop short recs the LLM emitted with <2 contributing strategies."""
        mock_client.return_value = object()
        # LLM returns a short with only one contributing strategy — must be dropped
        mock_call.return_value = json.dumps([
            {
                "ticker": "AAPL", "direction": "short", "position_size_pct": 0.05,
                "confidence": 0.9, "rationale": "single-source short",
                "contributing_strategies": ["litigation"], "regime_alignment": "neutral",
            },
            {
                "ticker": "MSFT", "direction": "long", "position_size_pct": 0.05,
                "confidence": 0.8, "rationale": "earnings beat",
                "contributing_strategies": ["earnings_call"], "regime_alignment": "neutral",
            },
        ])

        committee = _llm_committee()
        recs = committee.synthesize(
            # Need at least one short signal to pass pre-filter and exercise post-filter.
            # Use multi-strategy short so pre-filter doesn't drop input signals.
            signals=[
                {"ticker": "AAPL", "direction": "short", "score": 0.8,
                 "strategy": "litigation", "metadata": {}},
                {"ticker": "AAPL", "direction": "short", "score": 0.7,
                 "strategy": "congressional_trades", "metadata": {}},
            ],
            total_capital=50_000,
        )
        # Even though pre-filter let AAPL through, the LLM responded with only one
        # contributing_strategies entry — post-filter must drop it.
        assert all(r.ticker != "AAPL" for r in recs), \
            f"post-filter should have dropped non-compliant AAPL short, got {recs}"
        assert any(r.ticker == "MSFT" and r.direction == "long" for r in recs), \
            "long rec should survive post-filter"

    @patch.object(PortfolioCommittee, "_get_client")
    @patch.object(PortfolioCommittee, "_call_llm")
    def test_llm_multi_strategy_short_passes(self, mock_call, mock_client):
        mock_client.return_value = object()
        mock_call.return_value = json.dumps([
            {
                "ticker": "AAPL", "direction": "short", "position_size_pct": 0.05,
                "confidence": 0.85, "rationale": "two strategies agree",
                "contributing_strategies": ["litigation", "congressional_trades"],
                "regime_alignment": "neutral",
            },
        ])

        committee = _llm_committee()
        recs = committee.synthesize(
            signals=[
                {"ticker": "AAPL", "direction": "short", "score": 0.8,
                 "strategy": "litigation", "metadata": {}},
                {"ticker": "AAPL", "direction": "short", "score": 0.7,
                 "strategy": "congressional_trades", "metadata": {}},
            ],
            total_capital=50_000,
        )
        assert any(r.ticker == "AAPL" and r.direction == "short" for r in recs), \
            f"multi-strategy short should survive, got {recs}"

    @patch.object(PortfolioCommittee, "_get_client")
    @patch.object(PortfolioCommittee, "_call_llm")
    def test_llm_long_unchanged(self, mock_call, mock_client):
        """Single-strategy long should still pass through the LLM path."""
        mock_client.return_value = object()
        mock_call.return_value = json.dumps([
            {
                "ticker": "MSFT", "direction": "long", "position_size_pct": 0.05,
                "confidence": 0.7, "rationale": "earnings beat",
                "contributing_strategies": ["earnings_call"],
                "regime_alignment": "neutral",
            },
        ])

        committee = _llm_committee()
        recs = committee.synthesize(
            signals=[
                {"ticker": "MSFT", "direction": "long", "score": 0.8,
                 "strategy": "earnings_call", "metadata": {}},
            ],
            total_capital=50_000,
        )
        assert any(r.ticker == "MSFT" and r.direction == "long" for r in recs), \
            f"single-strategy long via LLM should pass, got {recs}"
