"""Portfolio-policy integration at the committee boundary."""

from __future__ import annotations

from copy import deepcopy
from unittest.mock import patch

import pytest

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.strategies.orchestration.cohort_orchestrator import SIZE_PROFILES
from tradingagents.strategies.trading.portfolio_committee import (
    PortfolioCommittee,
    TradeRecommendation,
)
from tradingagents.strategies.trading.portfolio_policy import (
    PortfolioPolicy,
    PortfolioPolicyConfig,
    PortfolioRiskContext,
)
from tradingagents.strategies.modules.base import OptionSpec


_ORIGINAL_POLICY_APPLY = PortfolioPolicy.apply


def _empty_context() -> PortfolioRiskContext:
    return PortfolioRiskContext(
        portfolio_value=100_000.0,
        cash=100_000.0,
        positions=(),
        pending_positions=(),
        sectors={"MSFT": "Technology"},
        annualized_volatility={"MSFT": 0.15},
        earnings_dates={},
        short_interest={},
        borrow_available={},
        margin_used=0.0,
        consumed_event_keys=frozenset(),
        config=PortfolioPolicyConfig.from_size_profile(
            SIZE_PROFILES["100k"],
            DEFAULT_CONFIG["autoresearch"]["portfolio_policy"],
        ),
    )


def _signal() -> dict:
    return {
        "ticker": "MSFT",
        "direction": "long",
        "score": 1.0,
        "strategy": "earnings_call",
        "event_key": "event-msft-q2",
        "source_event_keys": ("native-disclosure-1",),
        "strategy_tags": ("earnings_call", "material-event"),
        "risk_tags": ("event:msft-q2",),
    }


def _oversized(**changes: object) -> TradeRecommendation:
    values: dict[str, object] = {
        "ticker": "MSFT",
        "direction": "long",
        "position_size_pct": 0.50,
        "confidence": 0.9,
        "rationale": "test",
        "contributing_strategies": ["untrusted"],
        "event_key": "untrusted-event",
        "source_event_keys": ("untrusted-source",),
        "strategy_tags": ("untrusted-tag",),
        "risk_tags": ("untrusted-risk",),
        "journal_only": False,
    }
    values.update(changes)
    return TradeRecommendation(**values)


def _apply_policy_once(
    policy: PortfolioPolicy,
    recommendations: list[TradeRecommendation],
    context: PortfolioRiskContext,
) -> list[TradeRecommendation]:
    return _ORIGINAL_POLICY_APPLY(policy, recommendations, context)


def test_llm_result_passes_through_portfolio_policy_once() -> None:
    committee = PortfolioCommittee(DEFAULT_CONFIG, size_profile=SIZE_PROFILES["100k"])
    with (
        patch.object(committee, "_llm_synthesize", return_value=[_oversized()]),
        patch.object(
            PortfolioPolicy,
            "apply",
            autospec=True,
            side_effect=_apply_policy_once,
        ) as apply,
    ):
        result = committee.synthesize([_signal()], risk_context=_empty_context())

    assert apply.call_count == 1
    assert result[0].position_size_pct == 0.08


def test_fallback_result_passes_through_portfolio_policy_once() -> None:
    config = deepcopy(DEFAULT_CONFIG)
    config["autoresearch"]["paper_trade"]["portfolio_committee_enabled"] = False
    committee = PortfolioCommittee(config, size_profile=SIZE_PROFILES["100k"])
    with (
        patch.object(committee, "_rule_based_synthesize", return_value=[_oversized()]),
        patch.object(
            PortfolioPolicy,
            "apply",
            autospec=True,
            side_effect=_apply_policy_once,
        ) as apply,
    ):
        result = committee.synthesize([_signal()], risk_context=_empty_context())

    assert apply.call_count == 1
    assert result[0].position_size_pct == 0.08


def test_policy_sidecar_missing_fails_closed_without_second_evaluation() -> None:
    committee = PortfolioCommittee(DEFAULT_CONFIG, size_profile=SIZE_PROFILES["100k"])
    with (
        patch.object(committee, "_llm_synthesize", return_value=[_oversized()]),
        patch.object(PortfolioPolicy, "apply", return_value=[]) as apply,
        pytest.raises(RuntimeError, match="decision sidecar"),
    ):
        committee.synthesize([_signal()], risk_context=_empty_context())

    assert apply.call_count == 1


def test_duplicate_recommendation_choice_is_permutation_invariant() -> None:
    config = deepcopy(DEFAULT_CONFIG)
    config["autoresearch"].pop("portfolio_policy")
    committee = PortfolioCommittee(config, size_profile=SIZE_PROFILES["100k"])
    equity = _oversized(
        position_size_pct=0.05,
        regime_alignment="neutral",
        vehicle="equity",
    )
    call = _oversized(
        position_size_pct=0.05,
        regime_alignment="aligned",
        vehicle="option",
        option_spec=OptionSpec("call_spread", 45, 0.05, 0.02),
    )

    with patch.object(committee, "_llm_synthesize", return_value=[equity, call]):
        forward = committee.synthesize([_signal()])
    with patch.object(committee, "_llm_synthesize", return_value=[call, equity]):
        reverse = committee.synthesize([_signal()])

    assert forward == reverse


def test_post_pass_derives_attribution_from_matching_input_signals() -> None:
    committee = PortfolioCommittee(DEFAULT_CONFIG, size_profile=SIZE_PROFILES["100k"])
    second = {
        **_signal(),
        "strategy": "filing_analysis",
        "event_key": "event-msft-filing",
        "source_event_keys": ("native-filing-2",),
        "strategy_tags": ("filing_analysis",),
        "risk_tags": ("event:msft-filing",),
    }
    unrelated = {**_signal(), "ticker": "AAPL", "event_key": "event-aapl"}
    with patch.object(committee, "_llm_synthesize", return_value=[_oversized()]):
        result = committee.synthesize(
            [_signal(), second, unrelated], risk_context=_empty_context()
        )

    assert len(result) == 1
    recommendation = result[0]
    assert recommendation.event_key == "event-msft-filing"
    assert recommendation.source_event_keys == (
        "native-disclosure-1",
        "native-filing-2",
    )
    assert recommendation.strategy_tags == (
        "earnings_call",
        "filing_analysis",
        "material-event",
    )
    assert recommendation.risk_tags == ("event:msft-filing", "event:msft-q2")
    assert recommendation.contributing_strategies == [
        "earnings_call",
        "filing_analysis",
    ]
    assert recommendation.journal_only is False


def test_policy_enabled_committee_fails_closed_without_risk_context() -> None:
    committee = PortfolioCommittee(DEFAULT_CONFIG, size_profile=SIZE_PROFILES["100k"])

    with pytest.raises(ValueError, match="risk_context"):
        committee.synthesize([_signal()])


def test_legacy_committee_without_policy_block_remains_context_optional() -> None:
    committee = PortfolioCommittee(
        {"autoresearch": {"paper_trade": {"portfolio_committee_enabled": False}}},
        size_profile=SIZE_PROFILES["100k"],
    )

    assert committee.synthesize([_signal()])
