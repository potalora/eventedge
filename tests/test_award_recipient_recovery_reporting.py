"""Only replayed, exact-award proof can recover a retained detail 404."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json

import pytest

from test_award_recipient_evidence import proof
from tradingagents.strategies.data_sources.award_attribution_policy import apply_attribution_policy
from tradingagents.strategies.modules import get_paper_trade_strategies
from tradingagents.strategies.orchestration.operational_report import _sources
from tradingagents.strategies.orchestration.source_inputs import SourceInputStore

SESSION = '2026-10-23'


def report_sources(tmp_path, usaspending, *, extra=None):
    data = {name: {} for strategy in get_paper_trade_strategies()
            for name in strategy.data_sources if name != 'openbb'}
    data.update(usaspending=usaspending, **(extra or {}))
    state = tmp_path / 'state'
    SourceInputStore(tmp_path / 'cache', accepted_dir=state / 'source_inputs').freeze(
        {'generation': 'gen_022', 'session': SESSION, 'commit': 'a' * 40,
         'configuration': 'fixture'}, data,
        acquired_at=datetime(2026, 10, 23, 6, 1, tzinfo=timezone.utc))
    path = next((state / 'source_inputs').glob('*.json'))
    before = path.read_bytes()
    errors = []
    result = _sources(state, 'gen_022', SESSION, 'a' * 40,
                      lambda *event: errors.append(event))
    assert path.read_bytes() == before
    return result, errors


@pytest.fixture
def recovered_payload(proof):
    coverage = dict(mode='exhaustive_window', complete=True, returned=1, pages=1,
                    date_from='2026-09-23', date_to=SESSION, date_type='new_awards_only',
                    amount_basis='cumulative_award_obligations')
    policy = apply_attribution_policy([proof], coverage, session=SESSION)
    operation = 'award_' + hashlib.sha256(
        proof['recipient_identity_evidence']['detail_attempt']['url'].encode()).hexdigest()[:58]
    event = dict(provider='usaspending', operation=operation, reason_code='http_error',
                 http_status=404, attempts=1, recovered=False)
    return {**policy, 'data': {'contracts': [proof], 'coverage': deepcopy(policy['coverage'])},
            '_request_diagnostics': [event]}


def canonical_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


@pytest.mark.parametrize('attempts', [1, 2, 5])
def test_exact_recipient_proof_recovers_detail_404_without_rewriting_transport(tmp_path, recovered_payload, attempts):
    recovered_payload['_request_diagnostics'][0]['attempts'] = attempts
    before = deepcopy(recovered_payload)
    result, errors = report_sources(tmp_path, recovered_payload)
    assert errors == []
    assert result['unresolved'] == []
    assert len(result['recovered']) == 1
    recovered = result['recovered'][0]
    evidence = recovered_payload['data']['contracts'][0]['recipient_identity_evidence']
    assert recovered == {**before['_request_diagnostics'][0], 'source': 'usaspending',
        'recovered': True, 'transport_recovered': False,
        'recovery_basis': {
            'policy': 'exact_award_recipient_v1',
            'award_attribution_scope_sha256': before['coverage']['attribution_scope']['scope_sha256'],
            'award_key': 'CONT_AWD_36C24827C0008_3600_-NONE-_-NONE-',
            'detail_body_sha256': evidence['detail_attempt']['body_sha256'],
            'transaction_proof_sha256': canonical_digest(evidence['transaction_pages']),
            'recipient_profile_body_sha256': evidence['recipient_profile']['body_sha256']}}
    assert recovered_payload == before


@pytest.mark.parametrize('status', [404, 503])
def test_unrelated_failure_cannot_borrow_recipient_proof(tmp_path, recovered_payload, status):
    extra = dict(provider='usaspending', operation='award_unrelated', reason_code='http_error',
                 http_status=status, attempts=1, recovered=False)
    recovered_payload['_request_diagnostics'].append(extra)
    result, errors = report_sources(tmp_path, recovered_payload)
    assert errors == []
    assert len(result['recovered']) == 1
    assert result['unresolved'] == [{**extra, 'source': 'usaspending'}]


def test_other_source_cannot_borrow_exact_award_operation(tmp_path, recovered_payload):
    event = deepcopy(recovered_payload['_request_diagnostics'][0])
    result, errors = report_sources(tmp_path, recovered_payload,
                                    extra={'edgar': {'_request_diagnostics': [event]}})
    assert errors == []
    assert len(result['recovered']) == 1
    assert result['recovered'][0]['source'] == 'usaspending'
    assert result['unresolved'] == [{**event, 'source': 'edgar'}]


@pytest.mark.parametrize('damage', ['duplicate', 'missing', 'operation', 'provider',
                                   'status', 'zero_attempts', 'excess_attempts', 'bool_attempts', 'reason'])
def test_recipient_proof_requires_one_exact_transport_failure(tmp_path, recovered_payload, damage):
    events = recovered_payload['_request_diagnostics']
    if damage == 'duplicate':
        events.append(deepcopy(events[0]))
    elif damage == 'missing':
        events.clear()
    else:
        field, value = {'operation': ('operation', 'get'), 'provider': ('provider', 'edgar'),
                        'status': ('http_status', 503), 'zero_attempts': ('attempts', 0),
                        'excess_attempts': ('attempts', 6),
                        'bool_attempts': ('attempts', True),
                        'reason': ('reason_code', 'timeout')}[damage]
        events[0][field] = value
    result, errors = report_sources(tmp_path, recovered_payload)
    assert errors == ([('accepted_sources_invalid',)]
                      if damage in {'excess_attempts', 'bool_attempts'} else [])
    assert result['recovered'] == []
    assert any(row['reason_code'] == 'recipient_fallback_diagnostics_mismatch'
               for row in result['unresolved'])


def test_forged_transport_recovery_cannot_replace_preserved_404(tmp_path, recovered_payload):
    recovered_payload['_request_diagnostics'][0]['recovered'] = True
    result, errors = report_sources(tmp_path, recovered_payload)
    assert ('accepted_sources_invalid',) in errors
    assert result['recovered'] == []


@pytest.mark.parametrize('damage', ['digest', 'body', 'scope', 'duplicate_award', 'error'])
def test_recipient_proof_is_replayed_before_report_recovery(tmp_path, recovered_payload, damage):
    contract = recovered_payload['data']['contracts'][0]
    if damage == 'digest':
        contract['recipient_identity_evidence']['detail_attempt']['body_sha256'] = '0' * 64
    elif damage == 'body':
        profile = contract['recipient_identity_evidence']['recipient_profile']
        profile['body']['uei'] = 'OTHER1234567'
        profile['body_sha256'] = canonical_digest(profile['body'])
    elif damage == 'scope':
        recovered_payload['coverage']['attribution_scope']['scope_sha256'] = '0' * 64
    elif damage == 'duplicate_award':
        recovered_payload['data']['contracts'].append(deepcopy(contract))
    else:
        recovered_payload['error'] = 'request failed'
    result, errors = report_sources(tmp_path, recovered_payload)
    assert ('accepted_sources_invalid',) in errors
    assert result['recovered'] == []


def test_unbound_new_recipient_proof_is_invalid_in_report(tmp_path, proof):
    data = {'data': {'contracts': [proof]}}
    result, errors = report_sources(tmp_path, data)
    assert ('accepted_sources_invalid',) in errors
    assert result['recovered'] == []


def test_historical_award_payload_keeps_original_failure(tmp_path):
    event = dict(provider='usaspending', operation='get', reason_code='http_error',
                 http_status=404, attempts=1, recovered=False)
    result, errors = report_sources(tmp_path, {'data': {'contracts': []},
                                              '_request_diagnostics': [event]})
    assert errors == []
    assert result['recovered'] == []
    assert result['unresolved'] == [{**event, 'source': 'usaspending'}]


def test_historical_malformed_source_keeps_existing_source_diagnostic(tmp_path):
    result, errors = report_sources(tmp_path, [])
    assert errors == [('accepted_source_payload_invalid', 'usaspending')]
    assert result['unresolved'] == [{'provider': 'usaspending',
                                    'reason_code': 'invalid_response', 'http_status': None}]
