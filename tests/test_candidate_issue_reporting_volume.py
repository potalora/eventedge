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
            "input_coverage_valid": True,
            "source_health_failures": [],
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


@pytest.mark.parametrize('reason', ['missing_data', 'stale_data', 'invalid_data', 'provider_error'])
def test_isolated_reference_failure_requires_acceptance_despite_exact_quarantine(reason):
    ref = reference(0, COHORTS)
    ref.update(dependency_kind='reference_bar', reason_code=reason)
    results = carriers([ref])
    for row in results.values():
        row['staging_valid'] = True
        row['candidate_bar_quarantines'] = [ref['ticker']]
    assert not _valid_daily_cohort_results(results, SESSION)
    if reason == 'provider_error':
        with pytest.raises(ValueError):
            aggregate_candidate_input_issues(results, SESSION)
        return
    assert aggregate_candidate_input_issues(results, SESSION) == [ref]
    results[COHORTS[0]]['candidate_bar_quarantines'] = ['OTHER']
    with pytest.raises(ValueError):
        aggregate_candidate_input_issues(results, SESSION)


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


@pytest.mark.parametrize('damage', [None, 'multiple', 'coexisting', 'reference', 'error', 'execution', 'quarantine', 'scope'])
def test_only_isolated_short_history_can_carry_valid_staging(damage):
    ref = reference(0, COHORTS)
    ref['reason_code'] = 'insufficient_history'
    refs = [ref]
    if damage in {'multiple', 'coexisting'}:
        extra = reference(1, COHORTS)
        if damage == 'multiple':
            extra['reason_code'] = 'insufficient_history'
        refs.append(extra)
    if damage == 'reference':
        ref['dependency_kind'] = 'reference_bar'
    results = carriers(refs)
    for row in results.values():
        row['staging_valid'] = True
    first = results[COHORTS[0]]
    if damage == 'error':
        first['error'] = True
    elif damage == 'execution':
        first['execution_valid'] = False
    elif damage == 'quarantine':
        first['candidate_bar_quarantines'] = ['OTHER']
    elif damage == 'scope':
        first['candidate_input_issues'][0]['affected_cohorts'] = COHORTS[:-1]
    if damage in (None, 'multiple'):
        assert aggregate_candidate_input_issues(results, SESSION) == refs
        assert not _valid_daily_cohort_results(results, SESSION)
        for cohort_name, row in results.items():
            proofs = [
                {
                    'version': 1,
                    'issue_sha256': f'{index:064x}',
                    'staging': ['stage-1', cohort_name, SESSION, item['epoch_id'], 'foundation-30d', '2026-08-10T20:00:00+00:00', 'sha256:abc'],
                }
                for index, item in enumerate(refs, start=1)
            ]
            row['candidate_input_acceptance'] = proofs if len(proofs) > 1 else proofs[0]
        assert _valid_daily_cohort_results(results, SESSION)
    else:
        with pytest.raises(ValueError):
            aggregate_candidate_input_issues(results, SESSION)
