"""A shared issue is counted once even when all scenario books report it."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from scripts import run_cohorts
from tradingagents.strategies.orchestration.cohort_orchestrator import (
    build_default_cohorts,
)
from tradingagents.strategies.orchestration.daily_pipeline import (
    aggregate_candidate_input_issues,
    canonical_candidate_input_issue_summaries,
)
from tradingagents.strategies.orchestration.generation_manager import (
    _extract_cohort_results,
    _valid_daily_cohort_results,
)

SESSION = "2026-09-23"
COHORTS = sorted(c.name for c in build_default_cohorts({}))


def reference(index, affected):
    return {
        "issue_id": f"candidate_input_issue_{index:032x}",
        "epoch_id": f"gen_015-{SESSION}-" + "b" * 16,
        "session": SESSION,
        "dependency_kind": "volatility_history",
        "reason_code": "invalid_data",
        "ticker": f"T{index:03d}",
        "affected_cohorts": affected,
    }


def carriers(references):
    return {
        cohort: {
            "error": False,
            "degraded": True,
            "execution_valid": True,
            "staging_valid": False,
            "candidate_bar_quarantines": [],
            "candidate_input_issues": [
                deepcopy(ref) for ref in references if cohort in ref["affected_cohorts"]
            ],
        }
        for cohort in sorted({c for ref in references for c in ref["affected_cohorts"]})
    }


@pytest.mark.parametrize(
    "shared_count,partial_sizes,expanded_count",
    [(16, [4, 4, 4, 12], 280), (20, [4], 324)],
    ids=["sep23-20-distinct", "sep24-21-distinct"],
)
def test_incident_volume_round_trips_through_worker_and_manager(
    monkeypatch, capsys, shared_count, partial_sizes, expanded_count
):
    references = [reference(i, COHORTS) for i in range(shared_count)] + [
        reference(shared_count + i, COHORTS[:size])
        for i, size in enumerate(partial_sizes)
    ]
    results = carriers(references)
    assert (
        sum(len(r["candidate_input_issues"]) for r in results.values())
        == expanded_count
    )
    monkeypatch.setattr(
        run_cohorts, "_run_locked", lambda exclusive, operation: operation()
    )
    monkeypatch.setattr(
        run_cohorts,
        "_build_orchestrator",
        lambda *args: SimpleNamespace(run_daily=lambda session: results),
    )
    run_cohorts._run_daily(SimpleNamespace(), {}, SESSION, "gen_015", "a" * 40)
    captured = capsys.readouterr()
    assert "DEGRADED: 16/16" in captured.err
    parsed = _extract_cohort_results(captured.out)
    assert _valid_daily_cohort_results(parsed, SESSION)
    assert aggregate_candidate_input_issues(parsed, SESSION) == references
    assert canonical_candidate_input_issue_summaries(references, SESSION) == references


def test_full_unique_budget_can_be_shared_by_all_portfolios():
    references = [reference(i, COHORTS) for i in range(256)]
    assert aggregate_candidate_input_issues(carriers(references), SESSION) == references
    assert canonical_candidate_input_issue_summaries(references, SESSION) == references


def test_unique_budget_still_rejects_257_distinct_issues():
    references = [reference(i, [COHORTS[i % 2]]) for i in range(257)]
    with pytest.raises(ValueError, match="reference collection is invalid"):
        aggregate_candidate_input_issues(carriers(references), SESSION)


def test_each_portfolio_reference_list_remains_bounded():
    ref = reference(0, [COHORTS[0]])
    results = carriers([ref])
    results[COHORTS[0]]["candidate_input_issues"] = [ref] * 257
    with pytest.raises(ValueError, match="reference collection is invalid"):
        aggregate_candidate_input_issues(results, SESSION)
