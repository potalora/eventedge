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
        strategy_claim_event_counts={"congressional_trades": 30},
        missing_marks=0,
        stale_marks=0,
        sessions_aligned=True,
        stable_epoch_hashes=True,
        crosses_invalid_boundary=False,
        classified_strategy_count=12,
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


def test_promotion_payload_supports_fixture_injection_without_filesystem_writes(
    tmp_path: Path,
) -> None:
    from scripts.run_generations import _promotion_payload

    watched = tmp_path / "watched"
    watched.write_text("immutable")
    before = watched.stat().st_mtime_ns

    payload = _promotion_payload(
        "candidate",
        "baseline",
        repo=tmp_path,
        evidence_builder=lambda *_args: _passing(),
    )

    assert payload["candidate"] == "candidate"
    assert payload["baseline"] == "baseline"
    assert payload["decision"]["status"] == "ELIGIBLE_FOR_MANUAL_REVIEW"
    assert watched.stat().st_mtime_ns == before


def test_candidate_ledgers_close_when_baseline_opening_fails(tmp_path: Path) -> None:
    from scripts.run_generations import (
        PromotionAdvisoryUnavailable,
        _build_promotion_evidence,
    )

    manifest = tmp_path / "data" / "generations"
    manifest.mkdir(parents=True)
    manifest.joinpath("manifest.json").write_text(
        '{"generations":[{"gen_id":"candidate","state_dir":"candidate"},'
        '{"gen_id":"baseline","state_dir":"baseline"}]}'
    )

    class Ledger:
        closed = False

        def close(self) -> None:
            self.closed = True

    candidate_ledger = Ledger()

    def opener(generation_id: str, _record):
        if generation_id == "candidate":
            return object(), (candidate_ledger,)
        raise PromotionAdvisoryUnavailable("baseline ledger unavailable")

    with pytest.raises(
        PromotionAdvisoryUnavailable, match="baseline ledger unavailable"
    ):
        _build_promotion_evidence(
            "candidate", "baseline", tmp_path, service_opener=opener
        )
    assert candidate_ledger.closed is True


def test_cli_refuses_missing_evidence_before_manager_or_state_creation(
    tmp_path: Path, monkeypatch
) -> None:
    from scripts import run_generations

    monkeypatch.setattr(run_generations, "_repo_root", lambda: str(tmp_path))
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_generations.py",
            "promotion-status",
            "--candidate",
            "gen_candidate",
            "--baseline",
            "gen_baseline",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        run_generations.main()

    assert exc.value.code == 2
    assert list(tmp_path.iterdir()) == []


def test_promotion_module_does_not_import_directional_accuracy_or_learning() -> None:
    source = Path("tradingagents/strategies/metrics/promotion.py").read_text()
    assert "directional_accuracy" not in source
    assert "strategies.learning" not in source
