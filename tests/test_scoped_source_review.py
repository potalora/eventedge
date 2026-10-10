"""Independent offline attacks on the prospective award-scope health gate."""
from copy import deepcopy
import urllib.request

import pytest
import requests

from test_award_verified_subset_policy import POLICY, acquisition, fetch, records
from tradingagents.strategies.modules.govt_contracts import GovtContractsStrategy
from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
from tradingagents.strategies.orchestration.scoped_sources import (
    accepted_attribution_limitation, source_scope_evidence,
)


@pytest.fixture(autouse=True)
def forbid_transport(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('independent scope review cannot acquire sources')
    monkeypatch.setattr(requests.sessions.Session, 'request', forbidden)
    monkeypatch.setattr(requests.sessions.Session, 'send', forbidden)
    monkeypatch.setattr(urllib.request, 'urlopen', forbidden)


def engine(policy=POLICY):
    value = MultiStrategyEngine.__new__(MultiStrategyEngine)
    value.ar_config = {} if policy is None else {'award_attribution_policy': policy}
    value.paper_trade_strategies = [GovtContractsStrategy()]
    value._on_event = lambda *args, **kwargs: None
    value._analyzer = None
    return value


def screen(payload, policy=POLICY):
    return engine(policy).screen_and_enrich('2026-10-09',
        {'usaspending': payload, 'yfinance': {}}, epoch_id='review', policy_id='30d')


def test_actual_health_gate_retains_complete_unknown_partition_without_actionable_unknowns():
    payload = fetch(acquisition(records()))
    original = deepcopy(payload)
    signals, _, health = screen(payload)
    assert [s['ticker'] for s in signals if not s['journal_only']] == ['UNH']
    assert health[0].status == 'signals'
    assert health[0].evidence['candidate_count'] == 3
    assert health[0].evidence['actionable_candidate_count'] == 1
    manifest = health[0].evidence['admission_manifest']
    assert len(manifest['discovered']) == 3
    assert manifest['award_attribution_scope'] == payload['coverage']['attribution_scope']
    assert health[0].evidence['source_scope_limits']['usaspending']['counts'] == {
        'verified_listed_target': 1, 'verified_no_listed_target': 1, 'unresolved': 1}
    assert payload == original


@pytest.mark.parametrize('mutation', ['missing_config', 'wrong_config', 'missing_policy',
    'wrong_policy', 'changed_raw_identity', 'changed_scope_digest', 'incomplete',
    'nested_failure', 'metadata_failure', 'provider_error'])
def test_real_screen_cannot_emit_actionable_signals_from_invalid_scoped_evidence(mutation):
    payload = fetch(acquisition(records()))
    policy = POLICY
    if mutation == 'missing_config': policy = None
    elif mutation == 'wrong_config': policy = 'unreviewed_policy'
    elif mutation == 'missing_policy': payload.pop('award_attribution_policy')
    elif mutation == 'wrong_policy': payload['award_attribution_policy'] = 'unreviewed_policy'
    elif mutation == 'changed_raw_identity': payload['data']['contracts'][2]['recipient_uei'] = 'XMUZGJN98231'
    elif mutation == 'changed_scope_digest': payload['coverage']['attribution_scope']['scope_sha256'] = '0' * 64
    elif mutation == 'incomplete': payload['coverage']['complete'] = False
    elif mutation == 'nested_failure': payload['coverage']['native_metadata'] = {'complete': False}
    elif mutation == 'metadata_failure': payload['data']['contracts'][2]['recipient_identity_status'] = 'lookup_failed'
    else: payload['error'] = 'provider failed'
    signals, _, health = screen(payload, policy)
    assert signals == []
    assert health[0].status == 'data_failure'
    assert health[0].evidence['provider_errors']['usaspending'] == 'scoped_source_evidence_invalid'


@pytest.mark.parametrize('mutation', ['ticker', 'actionable', 'strategy', 'digest',
    'award_key', 'award_id', 'attribution', 'reason', 'analysis_failure'])
def test_journal_exemption_requires_exact_bound_non_actionable_disposition(mutation):
    payload = fetch(acquisition(records()))
    _, _, proofs = source_scope_evidence({'usaspending': payload},
        {'award_attribution_policy': POLICY}, '2026-10-09')
    candidate = next(c for c in GovtContractsStrategy().screen(
        {'usaspending': payload}, '2026-10-09', {'analysis_budget': 10})
        if c.metadata['award_key'] == 'native-award-2')
    assert accepted_attribution_limitation(candidate, 'govt_contracts', proofs)
    strategy = 'govt_contracts'
    if mutation == 'ticker': candidate.ticker = 'BA'
    elif mutation == 'actionable': candidate.journal_only = False
    elif mutation == 'strategy': strategy = 'other'
    elif mutation == 'digest': candidate.metadata['award_attribution_scope_sha256'] = '0' * 64
    elif mutation == 'award_key': candidate.metadata['award_key'] = 'other'
    elif mutation == 'award_id': candidate.metadata['award_id'] = 'other'
    elif mutation == 'attribution': candidate.metadata['issuer_attribution'] = {'verified': True, 'ticker': 'BA'}
    elif mutation == 'reason': candidate.metadata['non_actionable_reason'] = 'other'
    else: candidate.metadata['analysis_failure_reason'] = 'model_deadline_exhausted'
    assert not accepted_attribution_limitation(candidate, strategy, proofs)


def test_provider_failure_retains_original_rows_but_cannot_receive_scope_proof():
    payload = fetch(acquisition(records(), complete=False))
    assert payload['data']['contracts'] == records()
    errors, limits, proofs = source_scope_evidence({'usaspending': payload},
        {'award_attribution_policy': POLICY}, '2026-10-09')
    assert errors == {'usaspending': 'scoped_source_evidence_invalid'}
    assert limits == proofs == {}
