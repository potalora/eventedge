"""Promotion gates are pure advisory checks and must fail closed."""

from __future__ import annotations

from dataclasses import replace
from math import prod
from pathlib import Path

import pytest

from tradingagents.strategies.metrics.promotion import (
    PromotionDecisionStatus,
    PromotionEvaluator,
    PromotionEvidence,
    PromotionPolicy,
)


def _passing() -> PromotionEvidence:
    return PromotionEvidence(
        clean_common_sessions=30,
        independent_completed_ideas=50,
        strategy_claim_event_counts={"earnings_call": 30},
        missing_marks=0,
        stale_marks=0,
        sessions_aligned=True,
        stable_epoch_hashes=True,
        crosses_invalid_boundary=False,
        classified_strategy_count=5,
        cost_categories_present=True,
        risk_limit_breach=False,
        matched_excess_return=0.01,
        winning_strategies=2,
        candidate_max_drawdown=-0.10,
        baseline_max_drawdown=-0.09,
        delayed_fill_excess_return=0.002,
        slippage_20bps_excess_return=0.001,
    )


def test_insufficient_sample_waits() -> None:
    decision = PromotionEvaluator().evaluate(
        replace(_passing(), independent_completed_ideas=29)
    )
    assert decision.status is PromotionDecisionStatus.WAIT
    assert decision.research_review_ready is False


def test_thirty_ideas_marks_initial_research_review_ready() -> None:
    decision = PromotionEvaluator().evaluate(
        replace(_passing(), independent_completed_ideas=30)
    )
    assert decision.status is PromotionDecisionStatus.WAIT
    assert decision.research_review_ready is True


def test_integrity_failure_fails() -> None:
    decision = PromotionEvaluator().evaluate(replace(_passing(), missing_marks=1))
    assert decision.status is PromotionDecisionStatus.FAIL
    assert decision.reasons == ("missing_or_stale_marks",)


def _excess_return(candidate: tuple[float, ...], baseline: tuple[float, ...]) -> float:
    return prod(1 + value for value in candidate) - prod(
        1 + value for value in baseline
    )


@pytest.mark.parametrize(
    ("field", "scenario", "reason"),
    [
        (
            "delayed_fill_excess_return",
            # one-XNYS-session delayed fills lose the normal-fill edge
            (0.001, -0.004, 0.002),
            "delayed_fill_sensitivity_not_positive",
        ),
        (
            "slippage_20bps_excess_return",
            # 20bp adverse slippage per fill similarly removes the edge
            (0.000, -0.003, 0.002),
            "slippage_20bps_sensitivity_not_positive",
        ),
    ],
)
def test_deterministic_sensitivity_failure_is_not_eligible(
    field: str, scenario: tuple[float, ...], reason: str
) -> None:
    baseline = (0.001, -0.002, 0.003)
    normal_fills = (0.003, -0.001, 0.005)
    assert _excess_return(normal_fills, baseline) > 0
    assert _excess_return(scenario, baseline) <= 0
    decision = PromotionEvaluator().evaluate(
        replace(_passing(), **{field: _excess_return(scenario, baseline)})
    )
    assert decision.status is PromotionDecisionStatus.FAIL
    assert decision.reasons == (reason,)


def test_passing_evidence_is_advisory_only() -> None:
    evidence = _passing()
    before = repr(evidence)
    decision = PromotionEvaluator().evaluate(evidence)
    assert decision.status is PromotionDecisionStatus.ELIGIBLE_FOR_MANUAL_REVIEW
    assert repr(evidence) == before
    assert not hasattr(decision, "apply")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("matched_excess_return", float("nan")),
        ("delayed_fill_excess_return", float("inf")),
        ("slippage_20bps_excess_return", float("-inf")),
        ("clean_common_sessions", True),
        ("winning_strategies", "two"),
        ("candidate_max_drawdown", 0.01),
    ],
)
def test_invalid_evidence_is_rejected_before_a_decision(
    field: str, value: object
) -> None:
    with pytest.raises(ValueError):
        replace(_passing(), **{field: value})


def test_invalid_strategy_claim_count_and_mutation_are_rejected_or_isolated() -> None:
    with pytest.raises(ValueError, match="strategy claim event count must be an int"):
        replace(_passing(), strategy_claim_event_counts={"strategy": True})

    mutable_counts = {"congressional_trades": 30}
    evidence = replace(_passing(), strategy_claim_event_counts=mutable_counts)
    mutable_counts["congressional_trades"] = 0
    assert evidence.strategy_claim_event_counts["congressional_trades"] == 30


def test_evidence_subclass_cannot_bypass_validation() -> None:
    class BypassEvidence(PromotionEvidence):
        def __post_init__(self) -> None:
            pass

    bypass = BypassEvidence(
        **{
            **_passing().__dict__,
            "matched_excess_return": float("nan"),
        }
    )

    with pytest.raises(TypeError, match="exactly PromotionEvidence"):
        PromotionEvaluator().evaluate(bypass)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("min_clean_common_sessions", True),
        ("max_drawdown", float("nan")),
        ("max_drawdown_delta", -0.01),
    ],
)
def test_invalid_policy_is_rejected_before_evaluation(
    field: str, value: object
) -> None:
    with pytest.raises(ValueError):
        PromotionPolicy(**{field: value})


def test_promotion_policy_expected_strategy_count_is_configurable() -> None:
    assert PromotionPolicy().expected_strategy_count == 5
    # Mismatched count fails closed
    wrong = PromotionEvaluator().evaluate(replace(_passing(), classified_strategy_count=4))
    assert wrong.status is PromotionDecisionStatus.FAIL
    assert wrong.reasons == ("unclassified_strategy_silence",)
    # Explicit override is honored
    custom = PromotionEvaluator(PromotionPolicy(expected_strategy_count=7)).evaluate(
        replace(_passing(), classified_strategy_count=7)
    )
    assert custom.status is PromotionDecisionStatus.ELIGIBLE_FOR_MANUAL_REVIEW


def test_promotion_module_does_not_import_directional_accuracy_or_learning() -> None:
    source = Path("tradingagents/strategies/metrics/promotion.py").read_text()
    assert "directional_accuracy" not in source
    assert "strategies.learning" not in source
