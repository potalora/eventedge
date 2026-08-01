"""Pure, advisory-only promotion gates for paper-trading research."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PromotionDecisionStatus(str, Enum):
    """The only permitted promotion outcomes; none executes a promotion."""

    WAIT = "WAIT"
    FAIL = "FAIL"
    ELIGIBLE_FOR_MANUAL_REVIEW = "ELIGIBLE_FOR_MANUAL_REVIEW"


@dataclass(frozen=True)
class PromotionPolicy:
    min_clean_common_sessions: int = 30
    min_initial_ideas: int = 30
    min_manual_ideas: int = 50
    min_strategy_claim_events: int = 30
    max_drawdown: float = 0.15
    max_drawdown_delta: float = 0.02


@dataclass(frozen=True)
class PromotionEvidence:
    clean_common_sessions: int
    independent_completed_ideas: int
    strategy_claim_event_counts: dict[str, int]
    missing_marks: int
    stale_marks: int
    sessions_aligned: bool
    stable_epoch_hashes: bool
    crosses_invalid_boundary: bool
    classified_strategy_count: int
    cost_categories_present: bool
    risk_limit_breach: bool
    matched_excess_return: float
    winning_strategies: int
    candidate_max_drawdown: float
    baseline_max_drawdown: float
    delayed_fill_excess_return: float
    slippage_20bps_excess_return: float


@dataclass(frozen=True)
class PromotionDecision:
    status: PromotionDecisionStatus
    reasons: tuple[str, ...]
    research_review_ready: bool


class PromotionEvaluator:
    """Evaluate immutable evidence without imports from execution or learning."""

    def __init__(self, policy: PromotionPolicy | None = None) -> None:
        self.policy = policy or PromotionPolicy()

    def evaluate(self, evidence: PromotionEvidence) -> PromotionDecision:
        failures: list[str] = []
        if evidence.missing_marks or evidence.stale_marks:
            failures.append("missing_or_stale_marks")
        if not evidence.sessions_aligned:
            failures.append("unaligned_candidate_baseline_benchmark_cash")
        if not evidence.stable_epoch_hashes:
            failures.append("unstable_epoch_hashes")
        if evidence.crosses_invalid_boundary:
            failures.append("invalid_session_or_epoch_bridge")
        if evidence.classified_strategy_count != 12:
            failures.append("unclassified_strategy_silence")
        if not evidence.cost_categories_present:
            failures.append("missing_cost_or_borrow_category")
        if evidence.risk_limit_breach:
            failures.append("risk_limit_breach")
        if failures:
            return PromotionDecision(
                PromotionDecisionStatus.FAIL, tuple(failures), False
            )

        research_review_ready = (
            evidence.clean_common_sessions >= self.policy.min_clean_common_sessions
            and evidence.independent_completed_ideas >= self.policy.min_initial_ideas
        )
        waits: list[str] = []
        if evidence.clean_common_sessions < self.policy.min_clean_common_sessions:
            waits.append("need_30_clean_common_sessions")
        if evidence.independent_completed_ideas < self.policy.min_initial_ideas:
            waits.append("need_30_independent_completed_ideas")
        if any(
            count < self.policy.min_strategy_claim_events
            for count in evidence.strategy_claim_event_counts.values()
        ):
            waits.append("strategy_claim_needs_30_unique_matured_events")
        if evidence.independent_completed_ideas < self.policy.min_manual_ideas:
            waits.append("need_50_independent_completed_ideas")
        if waits:
            return PromotionDecision(
                PromotionDecisionStatus.WAIT, tuple(waits), research_review_ready
            )

        performance_failures: list[str] = []
        if evidence.matched_excess_return <= 0:
            performance_failures.append("matched_excess_return_not_positive")
        if evidence.winning_strategies < 2:
            performance_failures.append("winners_from_fewer_than_two_strategies")
        if abs(min(0.0, evidence.candidate_max_drawdown)) > self.policy.max_drawdown:
            performance_failures.append("candidate_drawdown_exceeds_15_percent")
        if (
            evidence.candidate_max_drawdown
            < evidence.baseline_max_drawdown - self.policy.max_drawdown_delta
        ):
            performance_failures.append("drawdown_more_than_two_points_worse")
        if evidence.delayed_fill_excess_return <= 0:
            performance_failures.append("delayed_fill_sensitivity_not_positive")
        if evidence.slippage_20bps_excess_return <= 0:
            performance_failures.append("slippage_20bps_sensitivity_not_positive")
        if performance_failures:
            return PromotionDecision(
                PromotionDecisionStatus.FAIL,
                tuple(performance_failures),
                research_review_ready,
            )
        return PromotionDecision(
            PromotionDecisionStatus.ELIGIBLE_FOR_MANUAL_REVIEW,
            ("manual_review_required",),
            True,
        )
