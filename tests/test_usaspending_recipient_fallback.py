"""Exercise the real adapter/request budget against captured official responses."""
from copy import deepcopy
import io
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest
import requests

from tradingagents.strategies.data_sources.award_attribution_policy import (
    POLICY, validate_attribution_scope,
)
from tradingagents.strategies.data_sources.request_policy import provider_budget
from tradingagents.strategies.data_sources.usaspending_source import USASpendingSource
from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
from tradingagents.strategies.orchestration.source_inputs import SourceInputStore, successful_source
from tradingagents.strategies.modules.govt_contracts import GovtContractsStrategy

BASE = 'https://api.usaspending.gov/api/v2/'
FIXTURE = Path(__file__).parent / 'fixtures/usaspending-recipient-fallback-2026-10-23.json'


def response(body, status=200):
    class NativeBody(io.BytesIO):
        def release_conn(self):
            self.close()

    value = requests.Response()
    value.status_code = status
    value.raw = NativeBody(json.dumps(body).encode())
    value.headers['Content-Type'] = 'application/json'
    return value


@pytest.fixture
def transport(monkeypatch):
    fixture = json.loads(FIXTURE.read_text())['responses']
    raw = next(row for row in fixture['search']['body']['results'] if row['internal_id'] == 363616285)
    replies = {
        ('POST', BASE + 'search/spending_by_award/'): (200, {
            'results': [raw], 'page_metadata': {'page': 1, 'hasNext': False}}),
        ('GET', fixture['detail']['capture']['url']): (404, fixture['detail']['body']),
        ('POST', fixture['transactions']['capture']['url']): (200, fixture['transactions']['body']),
        ('GET', fixture['recipient']['capture']['url']): (200, fixture['recipient']['body']),
    }
    seen, returned = [], []

    def request(method, url, **kwargs):
        seen.append((method, url, deepcopy(kwargs.get('json'))))
        status, body = replies[(method, url)]
        if url.endswith('spending_by_transaction/'):
            assert kwargs['json'] == fixture['transactions']['capture']['request']
        result = response(body, status)
        returned.append(result)
        return result

    monkeypatch.setattr(requests, 'get', lambda url, **kwargs: request('GET', url, **kwargs))
    monkeypatch.setattr(requests, 'post', lambda url, **kwargs: request('POST', url, **kwargs))
    return SimpleNamespace(fixture=fixture, raw=raw, replies=replies, seen=seen, returned=returned)


def acquire(*, max_attempts=1):
    source = USASpendingSource()
    engine = MultiStrategyEngine.__new__(MultiStrategyEngine)
    engine.registry = SimpleNamespace(get=lambda _: source)
    engine.ar_config = {'award_attribution_policy': POLICY}
    diagnostics = []
    with provider_budget('usaspending', time.monotonic() + 60, limits=(),
                         max_attempts=max_attempts, sleep=lambda _: None, diagnostics=diagnostics):
        payload = engine._fetch_usaspending_data('2026-10-23')
    payload['_request_diagnostics'] = diagnostics
    return payload


def test_real_404_recovers_recipient_only_through_frozen_scope_and_strategy(transport):
    payload = acquire()
    assert 'error' not in payload
    row = payload['data']['contracts'][0]
    assert row['recipient_identity_status'] == 'native_recipient_verified'
    assert row['parent_recipient_uei'] == ''
    assert row['issuer_attribution']['status'] == 'unresolved'
    evidence = row['recipient_identity_evidence']
    assert evidence['detail_attempt']['status'] == 404
    assert evidence['detail_attempt']['body'] == transport.fixture['detail']['body']
    assert evidence['recipient_profile']['body']['parent_uei'] == 'X3TQE41XJCA6'
    scope = validate_attribution_scope(payload, session='2026-10-23')
    assert scope['counts'] == {'verified_listed_target': 0, 'verified_no_listed_target': 0, 'unresolved': 1}
    assert successful_source(payload)
    frozen = SourceInputStore.decode(SourceInputStore.encode(payload))
    assert validate_attribution_scope(frozen, session='2026-10-23') == scope
    candidates = GovtContractsStrategy().screen({'usaspending': frozen}, '2026-10-23', {})
    assert len(candidates) == 1 and candidates[0].journal_only and candidates[0].ticker == ''
    failures = [item for item in payload['_request_diagnostics'] if item['reason_code']]
    assert len(failures) == 1 and failures[0]['http_status'] == 404
    assert failures[0]['recovered'] is False
    assert len(transport.seen) == 4
    assert all(item.raw.closed for item in transport.returned[1:])
    from tradingagents.strategies.orchestration.operational_report import _award_recipient_transport_recoveries
    recovered, mismatch = _award_recipient_transport_recoveries(payload, session='2026-10-23')
    assert not mismatch and len(recovered) == 1
    assert next(iter(recovered.values()))['award_key'] == row['award_key']


def test_complete_recorded_35_award_population_survives_the_real_detail_404(transport):
    # Replay the whole captured search, not a hand-selected happy-path award.
    # Bodies are recorded public responses; acquisition clocks are replay clocks.
    transport.replies[('POST', BASE + 'search/spending_by_award/')] = (
        200, transport.fixture['search']['body'])
    payload = acquire()
    assert 'error' not in payload
    rows = payload['data']['contracts']
    assert len(rows) == len({row['award_key'] for row in rows}) == 35
    scope = validate_attribution_scope(payload, session='2026-10-23')
    assert scope['counts'] == {'verified_listed_target': 9, 'verified_no_listed_target': 25, 'unresolved': 1}
    fallback = [row for row in rows if row['recipient_identity_status'] == 'native_recipient_verified']
    assert len(fallback) == 1 and fallback[0]['internal_id'] == 363616285
    assert fallback[0]['issuer_attribution']['status'] == 'unresolved'
    assert fallback[0]['parent_recipient_uei'] == ''
    assert len(transport.seen) == 4
    frozen = SourceInputStore.decode(SourceInputStore.encode(payload))
    assert validate_attribution_scope(frozen, session='2026-10-23') == scope


@pytest.mark.parametrize('status', [400, 403, 429, 500, 503])
def test_other_detail_failures_never_start_recipient_fallback(transport, status):
    key = ('GET', transport.fixture['detail']['capture']['url'])
    transport.replies[key] = (status, {'detail': 'unavailable'})
    payload = acquire()
    assert payload['error']
    assert payload['data']['contracts'][0]['recipient_identity_status'] == 'lookup_failed'
    assert len(transport.seen) == 2


def test_empty_http_200_transaction_result_does_not_repair_404(transport):
    key = ('POST', transport.fixture['transactions']['capture']['url'])
    body = deepcopy(transport.fixture['transactions']['body'])
    body['results'] = []
    transport.replies[key] = (200, body)
    payload = acquire()
    assert payload['error']
    assert payload['data']['contracts'][0]['recipient_identity_status'] == 'lookup_failed'


def test_mismatched_200_detail_is_not_a_fallback_trigger(transport):
    key = ('GET', transport.fixture['detail']['capture']['url'])
    transport.replies[key] = (200, {'id': 1, 'recipient': {}})
    payload = acquire()
    assert payload['error']
    assert len(transport.seen) == 2


def test_conflicting_recipient_profile_cannot_create_acquisition_success(transport):
    key = ('GET', transport.fixture['recipient']['capture']['url'])
    body = deepcopy(transport.fixture['recipient']['body'])
    body['uei'] = 'WZWRLY4G3PL8'
    transport.replies[key] = (200, body)
    payload = acquire()
    assert payload['error']
    row = payload['data']['contracts'][0]
    assert row['recipient_identity_status'] == 'lookup_failed'
    assert row['parent_recipient_uei'] == ''
    assert row['issuer_attribution']['status'] == 'unresolved'


def test_missing_native_numeric_id_keeps_404_without_starting_fallback(transport):
    transport.raw.pop('internal_id')
    payload = acquire()
    assert payload['error']
    assert len(transport.seen) == 2
    row = payload['data']['contracts'][0]
    assert row['recipient_identity_status'] == 'lookup_failed'
    assert row['recipient_identity_evidence']['detail_attempt']['status'] == 404


def test_exhausted_shared_deadline_cannot_start_a_fallback_request(transport, monkeypatch):
    clock = [0.0]
    original_get = requests.get

    def expired_get(url, **kwargs):
        result = original_get(url, **kwargs)
        clock[0] = 10.0
        return result

    monkeypatch.setattr(requests, 'get', expired_get)
    with provider_budget('usaspending', 10.0, clock=lambda: clock[0], limits=(), max_attempts=1):
        rows = USASpendingSource().get_recent_large_contracts(min_amount=50_000_000, as_of='2026-10-23')
    assert rows[0]['recipient_identity_status'] == 'lookup_failed'
    assert len(transport.seen) == 2


def test_oversized_404_body_is_rejected_without_fallback(transport):
    key = ('GET', transport.fixture['detail']['capture']['url'])
    transport.replies[key] = (404, {'detail': 'x' * (2 * 1024 * 1024)})
    payload = acquire()
    assert payload['error']
    assert len(transport.seen) == 2
    assert transport.returned[-1].raw.closed


def test_transient_detail_retry_ending_in_404_can_recover_with_its_actual_attempt_count(transport, monkeypatch):
    from tradingagents.strategies.orchestration.operational_report import _award_recipient_transport_recoveries
    original_get = requests.get
    attempted = []

    def first_transient(url, **kwargs):
        if url == transport.fixture['detail']['capture']['url'] and not attempted:
            attempted.append(url)
            return response({'detail': 'unavailable'}, status=503)
        return original_get(url, **kwargs)

    monkeypatch.setattr(requests, 'get', first_transient)
    payload = acquire(max_attempts=3)
    assert 'error' not in payload
    failures = [item for item in payload['_request_diagnostics'] if item['reason_code']]
    assert len(failures) == 1 and failures[0]['http_status'] == 404 and failures[0]['attempts'] == 2
    assert failures[0]['recovered'] is False
    recovered, mismatch = _award_recipient_transport_recoveries(payload, session='2026-10-23')
    assert len(recovered) == 1 and not mismatch
