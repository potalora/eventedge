"""Scope limitations are explicit; only proved non-actionable rows are exempt."""
from copy import deepcopy

import pytest

from tradingagents.strategies.modules.govt_contracts import GovtContractsStrategy
from tradingagents.strategies.modules.litigation import LitigationStrategy
from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine


def award_engine(tmp_path, monkeypatch, *, policy='verified_listed_targets_v1'):
    ar = {'state_dir': str(tmp_path)}
    if policy is not None:
        ar['award_attribution_policy'] = policy
    engine = MultiStrategyEngine(config={'autoresearch': ar}, strategies=[GovtContractsStrategy()])
    monkeypatch.setattr(engine, '_build_regime_model', lambda _: {})
    monkeypatch.setattr(engine, '_enrich_with_llm', lambda candidates, *a, **k: candidates)
    return engine


def screen(engine, payload):
    return engine.screen_and_enrich('2026-10-09',
        {'usaspending': payload, 'yfinance': {}, 'openbb': {}},
        epoch_id='epoch', policy_id='policy')


def test_unknown_awards_are_visible_without_claiming_complete_attribution(tmp_path, monkeypatch):
    from test_award_verified_subset_policy import records, acquisition, fetch
    signals, _, health = screen(award_engine(tmp_path, monkeypatch), fetch(acquisition(records())))
    assert health[0].status == 'signals'
    assert [s['ticker'] for s in signals if not s['journal_only']] == ['UNH']
    limit = health[0].evidence['source_scope_limits']['usaspending']
    assert limit['attribution_complete'] is False
    assert limit['counts']['unresolved'] == 1
    assert len(health[0].evidence['admission_manifest']['award_attribution_scope']['awards']) == 3


@pytest.mark.parametrize('mutation', ['changed_scope', 'missing_config', 'different_policy', 'lookup_failed'])
def test_source_flags_cannot_bypass_config_or_raw_evidence(tmp_path, monkeypatch, mutation):
    from test_award_verified_subset_policy import records, acquisition, fetch
    rows = records()[:1] if mutation in {'missing_config', 'different_policy'} else records()
    payload = fetch(acquisition(rows))
    policy = None if mutation == 'missing_config' else 'other' if mutation == 'different_policy' else 'verified_listed_targets_v1'
    if mutation == 'changed_scope':
        payload['coverage']['attribution_scope']['counts']['unresolved'] = 0
    elif mutation == 'lookup_failed':
        payload['data']['contracts'][2]['recipient_identity_status'] = 'lookup_failed'
    signals, _, health = screen(award_engine(tmp_path, monkeypatch, policy=policy), payload)
    assert health[0].status in {'data_failure', 'strategy_defect'}
    assert signals == []


def test_other_missing_analysis_still_fails_with_scoped_awards(tmp_path, monkeypatch):
    from test_award_verified_subset_policy import records, acquisition, fetch
    engine = award_engine(tmp_path, monkeypatch)
    def damage(candidates, *args, **kwargs):
        candidates[0].metadata['non_actionable_reason'] = 'missing_source_text'
        candidates[0].journal_only = True
        return candidates
    monkeypatch.setattr(engine, '_enrich_with_llm', damage)
    _, _, health = screen(engine, fetch(acquisition(records())))
    assert health[0].status == 'data_failure'


def test_focused_court_metadata_is_context_and_never_a_standalone_short(monkeypatch):
    strategy = LitigationStrategy()
    monkeypatch.setattr(strategy, '_extract_ticker', lambda _: 'AAPL')
    data = {'courtlistener': {'courtlistener_scope_policy': 'focused_litigation_v1',
        'coverage': {'content_kind': 'docket_metadata_only'},
        'dockets': [{'docket_id': 7, 'case_name': 'Investors v. Apple Inc.',
                     'nature_of_suit': 'Securities', 'cause': 'Fraud'}]}}
    population = strategy.screen(data, '2026-10-09', {})
    assert population == []
    assert population.admission_manifest['context_only_docket_ids'] == [7]
    assert population.admission_manifest['content_kind'] == 'docket_metadata_only'
