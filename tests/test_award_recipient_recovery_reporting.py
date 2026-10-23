"""Only replayed, exact-award proof can recover a retained detail 404."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pytest
import requests

from test_award_recipient_evidence import proof
from test_usaspending_recipient_fallback import transport, acquire, BASE as USA_BASE
from test_source_reliability_pipeline import pipeline
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


@pytest.fixture
def failed_fallback_payload(transport):
    transport.raw['Recipient UEI'] = 'UNKNOWN00001'
    transport.raw.pop('recipient_id')
    payload = acquire()
    assert payload['error']
    row = payload['data']['contracts'][0]
    assert row['recipient_identity_status'] == 'lookup_failed'
    assert row['parent_recipient_uei'] == ''
    assert 'recipient_identity_evidence' in row
    return payload


@pytest.mark.parametrize('failure', ['unknown_recipient', 'empty_transactions', 'transaction_timeout',
    'transaction_http_503', 'profile_conflict', 'profile_http_503', 'malformed_detail_404',
    'detail_http_400', 'detail_http_403', 'detail_http_429', 'detail_http_500', 'detail_http_503'])
def test_failed_native_fallback_remains_reported_source_failure(tmp_path, transport, failure, monkeypatch):
    if failure.startswith('detail_http_') or failure == 'malformed_detail_404':
        key = ('GET', transport.fixture['detail']['capture']['url'])
        transport.replies[key] = ((int(failure.rsplit('_', 1)[-1]), {})
                                  if failure.startswith('detail_http_') else (404, {}))
    elif failure == 'transaction_timeout':
        original = requests.post
        def post(url, **kwargs):
            if url.endswith('search/spending_by_transaction/'):
                raise requests.Timeout()
            return original(url, **kwargs)
        monkeypatch.setattr(requests, 'post', post)
    elif failure == 'unknown_recipient':
        transport.raw['Recipient UEI'] = 'UNKNOWN00001'
        transport.raw.pop('recipient_id')
    elif failure in {'empty_transactions', 'transaction_http_503'}:
        key = ('POST', transport.fixture['transactions']['capture']['url'])
        body = deepcopy(transport.fixture['transactions']['body'])
        body['results'] = []
        transport.replies[key] = (503 if failure.endswith('503') else 200, body)
    else:
        key = ('GET', transport.fixture['recipient']['capture']['url'])
        body = deepcopy(transport.fixture['recipient']['body'])
        body['uei'] = 'OTHER1234567'
        transport.replies[key] = (503 if failure.endswith('503') else 200, body)
    payload = acquire()
    assert payload['error'] and 'award_attribution_policy' not in payload
    assert payload['data']['contracts'][0]['recipient_identity_status'] == 'lookup_failed'
    before = deepcopy(payload)
    result, errors = report_sources(tmp_path, payload)
    assert errors == []
    assert result['recovered'] == []
    assert result['observations']['usaspending'] == 'failed'
    assert {'provider': 'usaspending', 'reason_code': 'provider_error', 'http_status': None} in result['unresolved']
    for event in payload['_request_diagnostics']:
        if event['reason_code'] not in (None, 'success'):
            assert {**event, 'source': 'usaspending'} in result['unresolved']
    assert payload == before


def test_empty_failed_observation_has_no_recipient_recovery_authority(tmp_path, failed_fallback_payload):
    failed_fallback_payload['data']['contracts'][0]['recipient_identity_evidence'] = {}
    result, errors = report_sources(tmp_path, failed_fallback_payload)
    assert errors == []
    assert result['recovered'] == [] and result['unresolved']


@pytest.mark.parametrize('error', [None, '', '  ', False, 0, True, 1, {}, []])
def test_failed_observation_requires_an_explicit_nonempty_error(tmp_path, failed_fallback_payload, error):
    failed_fallback_payload['error'] = error
    result, errors = report_sources(tmp_path, failed_fallback_payload)
    assert ('accepted_sources_invalid',) in errors
    assert result['recovered'] == []


@pytest.mark.parametrize('damage', ['verified_flag', 'downgraded_flag', 'parent', 'evidence',
                                   'policy_claim', 'scope_claim', 'detail_recovered'])
def test_failed_observation_cannot_hide_success_claims_or_changed_transport(tmp_path, failed_fallback_payload, damage):
    row = failed_fallback_payload['data']['contracts'][0]
    if damage == 'verified_flag':
        row['recipient_identity_status'] = 'native_recipient_verified'
    elif damage == 'downgraded_flag':
        row['recipient_identity_status'] = 'search_recipient'
    elif damage == 'parent':
        row['parent_recipient_uei'] = 'WZWRLY4G3PL8'
    elif damage == 'evidence':
        row['recipient_identity_evidence'] = []
    elif damage == 'policy_claim':
        failed_fallback_payload['award_attribution_policy'] = 'verified_listed_targets_v1'
    elif damage == 'scope_claim':
        failed_fallback_payload['coverage']['attribution_scope'] = {}
    else:
        event = next(event for event in failed_fallback_payload['_request_diagnostics'] if event['http_status'] == 404)
        event['recovered'] = True
    result, errors = report_sources(tmp_path, failed_fallback_payload)
    assert ('accepted_sources_invalid',) in errors
    assert result['recovered'] == []


@pytest.fixture
def mixed_fallback_payload(transport):
    failed = deepcopy(transport.raw)
    failed.update({'Award ID': 'FAILED-FIXTURE', 'generated_internal_id': 'CONT_AWD_FAILED_FIXTURE',
                   'internal_id': 42, 'Recipient UEI': 'UNKNOWN00001'})
    failed.pop('recipient_id')
    transport.replies[('POST', USA_BASE + 'search/spending_by_award/')] = (200,
        {'results': [transport.raw, failed], 'page_metadata': {'page': 1, 'hasNext': False}})
    transport.replies[('GET', USA_BASE + 'awards/CONT_AWD_FAILED_FIXTURE/')] = (404,
        {'detail': "No Award found with: {'generated_unique_award_id_legacy': 'CONT_AWD_FAILED_FIXTURE'}"})
    payload = acquire()
    assert payload['error'] and 'award_attribution_policy' not in payload
    assert [row['recipient_identity_status'] for row in payload['data']['contracts']] == [
        'native_recipient_verified', 'lookup_failed']
    return payload


@pytest.mark.parametrize('damage', ['digest', 'downgraded_flag', 'successful_detail_recovered',
                                   'failed_detail_recovered', 'empty_failed_detail_recovered'])
def test_mixed_failed_source_still_replays_success_and_preserves_native_failure(tmp_path, mixed_fallback_payload, damage):
    success, failed = mixed_fallback_payload['data']['contracts']
    if damage == 'digest':
        success['recipient_identity_evidence']['recipient_profile']['body_sha256'] = '0' * 64
    elif damage == 'downgraded_flag':
        success['recipient_identity_status'] = 'search_recipient'
    else:
        url = (success if damage == 'successful_detail_recovered' else failed)[
            'recipient_identity_evidence']['detail_attempt']['url']
        operation = 'award_' + hashlib.sha256(url.encode()).hexdigest()[:58]
        event = next(row for row in mixed_fallback_payload['_request_diagnostics']
                     if row['operation'] == operation)
        event['recovered'] = True
        if damage == 'empty_failed_detail_recovered':
            failed['recipient_identity_evidence'] = {}
    result, errors = report_sources(tmp_path, mixed_fallback_payload)
    assert ('accepted_sources_invalid',) in errors
    assert result['recovered'] == []


def test_failed_source_preserves_independent_successful_transport_retry(tmp_path, failed_fallback_payload):
    event = dict(provider='usaspending', operation='discovery', reason_code=None,
                 http_status=200, attempts=2, recovered=True)
    failed_fallback_payload['_request_diagnostics'].append(event)
    result, errors = report_sources(tmp_path, failed_fallback_payload)
    assert errors == []
    assert result['recovered'] == [{**event, 'source': 'usaspending', 'reason_code': 'success'}]
    assert {'provider': 'usaspending', 'reason_code': 'provider_error', 'http_status': None} in result['unresolved']


def test_mixed_recipient_fallback_failure_preserves_full_pipeline_health_and_replay(pipeline, monkeypatch, proof):
    from test_prospective_pipeline import enable_activity_pipeline
    from test_source_reliability_pipeline import run_campaign, accepted_input_bytes, SESSION as session
    fixture, owner, config = enable_activity_pipeline(pipeline, monkeypatch)
    raw = deepcopy(proof['recipient_identity_evidence']['search_award'])
    raw.update({'Base Obligation Date': '2026-10-01', 'Start Date': '2026-10-01',
                'Last Modified Date': '2026-10-01 15:23:04'})
    failed = deepcopy(raw)
    failed.update({'Award ID': 'FAILED-FIXTURE', 'generated_internal_id': 'CONT_AWD_FAILED_FIXTURE',
                   'internal_id': 42, 'Recipient UEI': 'UNKNOWN00001'})
    failed.pop('recipient_id')
    transactions = deepcopy(proof['recipient_identity_evidence']['transaction_pages'][0]['body'])
    transactions['results'][0]['Action Date'] = '2026-10-01'
    original = fixture.http
    def http(method, url, **kwargs):
        if url.startswith(USA_BASE) and not fixture.block_all:
            fixture.calls[url] += 1
            if url.endswith('search/spending_by_award/'):
                return fixture.response({'results': [raw, failed],
                                         'page_metadata': {'page': 1, 'hasNext': False}})
            if '/awards/' in url:
                key = url.removesuffix('/').rsplit('/', 1)[-1]
                return fixture.response({'detail': 'No Award found with: ' +
                    repr({'generated_unique_award_id_legacy': key})}, status=404)
            if url.endswith('search/spending_by_transaction/'):
                return fixture.response(transactions)
            if '/recipient/' in url:
                return fixture.response(proof['recipient_identity_evidence']['recipient_profile']['body'])
            raise AssertionError('unexpected USAspending route')
        return original(method, url, **kwargs)
    fixture.http = http
    result, _, report = run_campaign(pipeline)
    assert result['outcome'] == 'degraded' and report['evidence_complete']
    assert not [row for row in report['sources']['recovered'] if row.get('source') == 'usaspending']
    assert [row for row in report['sources']['unresolved'] if row.get('provider') == 'usaspending']
    envelopes = [SourceInputStore.decode(path.read_text()) for path in
                 (Path(config['autoresearch']['state_dir']) / 'source_inputs').glob('*.json')]
    acquired = [envelope['payload']['usaspending'] for envelope in envelopes
                if 'usaspending' in envelope['payload']]
    assert len(acquired) == 1 and acquired[0]['error']
    assert [row['recipient_identity_status'] for row in acquired[0]['data']['contracts']] == [
        'native_recipient_verified', 'lookup_failed']
    health = [row for row in owner._metric_store.read_strategy_health(session=session)
              if row.strategy == 'govt_contracts']
    assert len(health) == 4 and all(row.status == 'data_failure' for row in health)
    before, calls = accepted_input_bytes(config), fixture.calls.copy()
    fixture.block_all = True
    repeated, _, replay_report = run_campaign(pipeline)
    assert repeated['outcome'] == 'degraded' and replay_report['evidence_complete']
    assert accepted_input_bytes(config) == before and fixture.calls == calls
    assert not fixture.blocked_calls
