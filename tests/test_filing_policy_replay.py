"""Frozen current-only permission remains bound to complete actual SEC history."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from test_filing_current_only_policy import POLICY, source_case, CIK
from test_filing_hydration import hydrate, history_row, row
from test_operational_report import native_evidence


def payload():
    current, source = source_case()
    graph = hydrate(source, {'filings': [current]}, comparator_policy=POLICY)
    collections = graph.pop('collections')
    return {'edgar': {**collections, 'filing_evidence': graph}}


CONFIG = {'filing_evidence_policy': 'complete_submission_v1', 'filing_comparison_policy': POLICY}


def test_replay_revalidates_history_even_without_other_scoped_sources(monkeypatch):
    from tradingagents.strategies.orchestration import scoped_replay
    data = payload()
    owner = SimpleNamespace(_base_config={'autoresearch': deepcopy(CONFIG)})
    monkeypatch.setattr(scoped_replay, 'validate_portfolio_targets', lambda *args: None)
    monkeypatch.setattr(scoped_replay, 'source_scope_evidence', lambda *args, **kwargs: ({}, {}, {}))
    kwargs = {'now': datetime(2026, 10, 10, tzinfo=timezone.utc)}
    scoped_replay.validate_replay_source_scopes(data, owner, '2026-10-09', 'epoch', **kwargs)
    data['edgar']['filing_evidence']['history_corpus'][CIK]['filings'] = [history_row(row(2, '10-K', '2025-09-30'))]
    with pytest.raises(ValueError, match='filing'):
        scoped_replay.validate_replay_source_scopes(data, owner, '2026-10-09', 'epoch', **kwargs)


@pytest.mark.parametrize('damage', ['policy', 'count', 'reason', 'scope', 'prior', 'requires', 'form', 'date', 'body'])
def test_current_only_replay_rejects_contradictory_labels(damage):
    from tradingagents.strategies.orchestration.filing_policy_validation import validate_filing_comparison_policy
    data, config = payload(), deepcopy(CONFIG)
    filing = data['edgar']['filings'][0]
    graph = data['edgar']['filing_evidence']
    if damage == 'policy': config.pop('filing_comparison_policy')
    if damage == 'count': graph['coverage']['current_only_rows'] = 0
    if damage == 'reason': filing['prior_status'] = 'unproven_prior'
    if damage == 'scope': filing.pop('filing_assessment_scope')
    if damage == 'prior': filing['prior_evidence_ref'] = 'invented'
    if damage == 'requires': filing['requires_prior'] = False
    if damage == 'form': filing['form_type'] = '10-Q'
    if damage == 'date': filing['file_date'] = '2026-09-29'
    if damage == 'body': graph['corpus'][filing['filing_evidence_ref']]['structural_status'] = 'insufficient'
    with pytest.raises(ValueError, match='filing'):
        validate_filing_comparison_policy(data, config)


def test_summary_keeps_absence_distinct_and_failed_source_has_no_permission():
    from tradingagents.strategies.orchestration.filing_policy_validation import validate_filing_comparison_policy
    assert validate_filing_comparison_policy(payload(), CONFIG) == {
        'policy': POLICY, 'current_only_rows': 1, 'current_only_absent_rows': 1,
        'current_only_ambiguous_rows': 0, 'comparative_claims_allowed': False}
    assert validate_filing_comparison_policy({'edgar': {'error': 'source unavailable'}}, CONFIG) is None
    data = payload()
    data['edgar'].pop('filing_evidence')
    data['edgar']['error'] = 'source unavailable'
    with pytest.raises(ValueError, match='filing'):
        validate_filing_comparison_policy(data, CONFIG)


@pytest.mark.parametrize('damage', [False, True])
def test_report_revalidates_and_displays_current_only_scope(native_evidence, damage):
    from test_operational_report import _attempt, _report
    from test_prospective_replay_integrity import rewrite_payload
    from tradingagents.strategies.orchestration.operational_report import render_operational_report
    repo, state, wire = native_evidence
    data = payload()
    if damage:
        data['edgar']['filing_evidence']['coverage']['current_only_rows'] = 0
    rewrite_payload(next((state/'source_inputs').glob('*.json')), lambda frozen: frozen.update(data))
    _attempt(repo, wire)
    report = _report(repo)
    assert report['evidence_complete'] is not damage
    if damage:
        assert {'code': 'accepted_sources_invalid'} in report['diagnostics']
    else:
        assert report['sources']['filing_comparison_scope']['current_only_rows'] == 1
        assert 'Rows permitted for current-only filing assessment: 1 ' in render_operational_report(report)
        assert 'prohibits comparison claims' in render_operational_report(report)
