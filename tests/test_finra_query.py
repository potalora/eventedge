"""Offline tests of the official symbol/date-scoped FINRA query boundary."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib
import json
from pathlib import Path

import pytest
import requests

from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
from tradingagents.strategies.data_sources.request_policy import provider_budget

NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
ENDPOINT = 'https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest'


def row(symbol='AAPL', **changes):
    return {'symbolCode': symbol, 'issueName': 'Apple Inc. Common Stock',
        'marketClassCode': 'NNM', 'settlementDate': '2026-10-15',
        'currentShortPositionQuantity': 131591865,
        'previousShortPositionQuantity': 120763539, 'averageDailyVolumeQuantity': 35498663,
        'daysToCoverQuantity': 3.71, 'changePercent': 8.97, 'changePreviousNumber': 10828326,
        'accountingYearMonthNumber': 20261015, 'issuerServicesGroupExchangeCode': 'R',
        'revisionFlag': None, 'stockSplitFlag': None} | changes


class Response:
    def __init__(self, rows=None, *, body=None, status=200, headers=None, chunks=None):
        self.body = json.dumps(rows).encode() if body is None else body
        self.status_code = status
        self.headers = {'Content-Type': 'application/json',
            'record-total': str(len(rows)) if type(rows) is list else '0',
            'record-offset': '0'} | (headers or {})
        self.closed = False
        self.chunks = chunks

    def iter_content(self, chunk_size):
        if self.chunks is not None:
            yield from self.chunks
        else:
            yield self.body

    def close(self):
        self.closed = True


def transport(monkeypatch, responses):
    """Replace only external HTTP; acquisition and normalizer remain real."""
    calls, sessions = [], []
    class Session:
        def __init__(self):
            self.trust_env = True
            self.closed = False
            sessions.append(self)
        def post(self, url, **options):
            calls.append((url, options, self.trust_env))
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            response.headers.setdefault('record-limit', str(options['json']['limit']))
            return response
        def close(self):
            self.closed = True
    monkeypatch.setattr(requests, 'Session', Session)
    return calls, sessions


def acquire(symbols, *, acquired_at=NOW, as_of=None):
    module = importlib.import_module('tradingagents.strategies.data_sources.finra_query')
    attempt = module.new_attempt()
    with provider_budget('openbb', 100, clock=lambda: 1, limits=()):
        values, errors = module.acquire(symbols, attempt, acquired_at=acquired_at, as_of=as_of)
    return module, attempt, values, errors


def receipt(module, attempt, symbols, *, cached_count=0):
    digest = hashlib.sha256(json.dumps(sorted(symbols), separators=(',', ':')).encode()).hexdigest()
    return {'schema_version': 4, 'requested_count': len(symbols), 'cached_count': cached_count,
        'population_sha256': digest, 'attempts': [attempt]}


def test_official_query_returns_current_value_without_claiming_archive_coverage(monkeypatch):
    response = Response([row()])
    calls, sessions = transport(monkeypatch, [response])
    module, attempt, values, errors = acquire(['AAPL'])
    assert values == {'AAPL': {'short_interest': 131591865.0, 'short_pct_of_float': None,
        'days_to_cover': 3.71, 'date': '2026-10-15'}}
    assert errors == {} and attempt['status'] == 'success' and attempt['reason_code'] is None
    assert attempt['query_coverage']['accepted_symbols'] == ['AAPL']
    assert attempt['query_coverage']['current_complete'] is True
    assert 'archive_coverage' not in attempt and 'historical_complete' not in json.dumps(attempt)
    url, options, trust_env = calls[0]
    assert url == ENDPOINT and trust_env is False
    assert options['allow_redirects'] is False and options['stream'] is True and 'auth' not in options
    assert options['json'] == {'limit': 2,
        'compareFilters': [{'fieldName': 'settlementDate', 'fieldValue': '2026-10-15', 'compareType': 'EQUAL'}],
        'domainFilters': [{'fieldName': 'symbolCode', 'values': ['AAPL']}]}
    assert response.closed and sessions[0].closed
    original = receipt(module, attempt, ['AAPL'])
    copied = module.validated_acquisition(original)
    copied['attempts'][0]['query_coverage']['accepted_symbols'].clear()
    assert original['attempts'][0]['query_coverage']['accepted_symbols'] == ['AAPL']


def test_missing_and_invalid_numbers_are_explicit_symbol_failures(monkeypatch):
    transport(monkeypatch, [Response([row('A'), row('B', currentShortPositionQuantity=True)])])
    module, attempt, values, errors = acquire(['C', 'B', 'A'])
    assert set(values) == {'A'}
    assert errors['B']['reason_code'] == 'invalid_response'
    assert errors['C']['reason_code'] == 'provider_error'
    assert attempt['status'] == 'partial'
    assert attempt['query_coverage']['accepted_symbols'] == ['A']
    assert attempt['query_coverage']['invalid_symbols'] == ['B']
    assert attempt['query_coverage']['missing_symbols'] == ['C']
    module.validated_acquisition(receipt(module, attempt, ['A', 'B', 'C']))


@pytest.mark.parametrize('rows', [[row('OTHER')], [row(), row()],
    [row(settlementDate='2026-09-30')], [row(settlementDate='2026-10-30')],
    [row(accountingYearMonthNumber=20260930)], [None], {'data': []}])
def test_wrong_identity_date_duplicates_or_shape_cannot_publish(monkeypatch, rows):
    response = Response(rows)
    transport(monkeypatch, [response])
    module = importlib.import_module('tradingagents.strategies.data_sources.finra_query')
    attempt = module.new_attempt()
    with pytest.raises(SourceFetchError) as failure:
        module.acquire(['AAPL'], attempt, acquired_at=NOW)
    assert failure.value.reason_code == 'invalid_response'
    assert attempt['status'] == 'error'
    assert attempt['query_coverage']['accepted_symbols'] == []
    assert response.closed
    module.validated_acquisition(receipt(module, attempt, ['AAPL']))


def test_batches_are_bounded_and_late_failure_discards_prior_success(monkeypatch):
    symbols = [f'A{i:03}' for i in range(101)]
    first, second = Response([row(symbol) for symbol in symbols[:100]]), Response([], status=403)
    calls, sessions = transport(monkeypatch, [first, second])
    module = importlib.import_module('tradingagents.strategies.data_sources.finra_query')
    attempt = module.new_attempt()
    with pytest.raises(SourceFetchError) as failure:
        module.acquire(symbols, attempt, acquired_at=NOW)
    assert failure.value.reason_code == 'http_error' and failure.value.http_status == 403
    assert len(calls) == 2 and [call[1]['json']['limit'] for call in calls] == [101, 2]
    assert attempt['query_coverage']['accepted_symbols'] == []
    assert attempt['query_coverage']['missing_symbols'] == symbols
    assert first.closed and second.closed and all(session.closed for session in sessions)
    module.validated_acquisition(receipt(module, attempt, symbols))


@pytest.mark.parametrize('response', [Response(body=b'PRIVATE'), Response(body=b'[NaN]'),
    Response(body=b'[{"symbolCode":"AAPL","symbolCode":"PRIVATE"}]'),
    Response(body=b'[]', headers={'Content-Type': 'text/html'}),
    Response(body=b'x' * (1024 * 1024 + 1)),
    Response([], status=302), Response([], status=429)])
def test_malformed_oversized_or_non_success_response_is_safe(monkeypatch, response):
    transport(monkeypatch, [response])
    module = importlib.import_module('tradingagents.strategies.data_sources.finra_query')
    attempt = module.new_attempt()
    with pytest.raises(SourceFetchError) as failure:
        module.acquire(['AAPL'], attempt, acquired_at=NOW)
    assert 'PRIVATE' not in str(failure.value)
    assert attempt['status'] == 'error' and response.closed
    module.validated_acquisition(receipt(module, attempt, ['AAPL']))


@pytest.mark.parametrize('error,reason', [(requests.Timeout('PRIVATE'), 'timeout'),
    (requests.ConnectionError('PRIVATE'), 'transport_error')])
def test_transport_errors_do_not_retry_or_leak(monkeypatch, error, reason):
    calls, sessions = transport(monkeypatch, [error])
    module = importlib.import_module('tradingagents.strategies.data_sources.finra_query')
    attempt = module.new_attempt()
    with pytest.raises(SourceFetchError) as failure:
        module.acquire(['AAPL'], attempt, acquired_at=NOW)
    assert failure.value.reason_code == reason and 'PRIVATE' not in str(failure.value)
    assert len(calls) == 1 and sessions[0].closed
    module.validated_acquisition(receipt(module, attempt, ['AAPL']))


def test_late_body_cannot_publish(monkeypatch):
    clock = [1]
    def chunks():
        yield json.dumps([row()]).encode()
        clock[0] = 100
    response = Response(chunks=chunks())
    transport(monkeypatch, [response])
    module = importlib.import_module('tradingagents.strategies.data_sources.finra_query')
    attempt = module.new_attempt()
    with provider_budget('openbb', 100, clock=lambda: clock[0], limits=()):
        with pytest.raises(SourceFetchError) as failure:
            module.acquire(['AAPL'], attempt, acquired_at=NOW)
    assert failure.value.reason_code == 'timeout'
    assert attempt['query_coverage']['accepted_symbols'] == [] and response.closed
    module.validated_acquisition(receipt(module, attempt, ['AAPL']))


@pytest.mark.parametrize('change', [lambda x:x.update(schema_version=True),
    lambda x:x['attempts'][0].update(credentials='PRIVATE'),
    lambda x:x['attempts'][0].update(endpoint='https://PRIVATE'),
    lambda x:x['attempts'][0].update(row_count=True),
    lambda x:x['attempts'][0]['query_coverage'].update(accepted_symbols=[]),
    lambda x:x['attempts'][0]['query_coverage'].update(historical_complete=True),
    lambda x:x['attempts'][0]['query_coverage'].update(publication_date='2026-10-25'),
    lambda x:x['attempts'][0]['batches'][0].update(requested_symbols=['OTHER']),
    lambda x:x['attempts'][0].update(query_population_sha256='0'*64)])
def test_durable_schema_rejects_identity_scope_and_provenance_contradictions(monkeypatch, change):
    transport(monkeypatch, [Response([row()])])
    module, attempt, _, _ = acquire(['AAPL'])
    value = receipt(module, attempt, ['AAPL'])
    change(value)
    with pytest.raises(ValueError):
        module.validated_acquisition(value)


def test_calendar_failure_still_has_valid_error_receipt(monkeypatch):
    calls, _ = transport(monkeypatch, [])
    module = importlib.import_module('tradingagents.strategies.data_sources.finra_query')
    attempt = module.new_attempt()
    with pytest.raises(SourceFetchError):
        module.acquire(['AAPL'], attempt, acquired_at=datetime(2027, 1, 2, tzinfo=timezone.utc))
    assert calls == [] and attempt['query_coverage'] is None
    module.validated_acquisition(receipt(module, attempt, ['AAPL']))


@pytest.mark.parametrize('headers', [{'record-total': '2'}, {'record-total': None},
    {'record-total': '-1'}, {'record-total': '1.0'}, {'record-total': '1000001'},
    {'record-offset': '1'}, {'record-offset': None}, {'record-limit': '1'}, {'record-limit': '0'},
    {'record-limit': None}, {'total-records-on-page': '2'}])
def test_paging_or_missing_completeness_headers_cannot_certify_full_query(monkeypatch, headers):
    response = Response([row()], headers=headers)
    transport(monkeypatch, [response])
    module = importlib.import_module('tradingagents.strategies.data_sources.finra_query')
    attempt = module.new_attempt()
    with pytest.raises(SourceFetchError) as failure:
        module.acquire(['AAPL'], attempt, acquired_at=NOW)
    assert failure.value.reason_code == 'invalid_response' and response.closed
    assert attempt['query_coverage']['accepted_symbols'] == []
    module.validated_acquisition(receipt(module, attempt, ['AAPL']))


def test_supplied_complete_paging_headers_are_bound_into_durable_receipt(monkeypatch):
    transport(monkeypatch, [Response([row()], headers={'total-records-on-page': '1'})])
    module, attempt, _, _ = acquire(['AAPL'])
    batch = attempt['batches'][0]
    assert (batch['record_total'], batch['record_offset'], batch['record_limit'],
        batch['total_records_on_page'], batch['row_count']) == (1, 0, 2, 1, 1)
    value = receipt(module, attempt, ['AAPL'])
    module.validated_acquisition(value)
    value['attempts'][0]['batches'][0]['record_total'] = 2
    with pytest.raises(ValueError):
        module.validated_acquisition(value)


def test_derived_numeric_overflow_is_one_symbol_failure(monkeypatch):
    transport(monkeypatch, [Response([row('A'), row('B', currentShortPositionQuantity=1e308,
        averageDailyVolumeQuantity=1e-308, daysToCoverQuantity=None)])])
    _, attempt, values, errors = acquire(['A', 'B'])
    assert set(values) == {'A'} and errors['B']['reason_code'] == 'invalid_response'
    assert attempt['status'] == 'partial'


def test_bounded_thousand_symbol_population_uses_ten_complete_batches(monkeypatch):
    symbols = [f'A{i:04}' for i in range(1000)]
    calls, _ = transport(monkeypatch, [Response([row(symbol) for symbol in symbols[start:start+100]])
        for start in range(0, 1000, 100)])
    module, attempt, values, errors = acquire(symbols)
    assert set(values) == set(symbols) and not errors and len(calls) == 10
    module.validated_acquisition(receipt(module, attempt, symbols))


def test_over_limit_or_duplicate_symbol_input_starts_no_request(monkeypatch):
    calls, _ = transport(monkeypatch, [])
    module = importlib.import_module('tradingagents.strategies.data_sources.finra_query')
    for symbols in [['AAPL', 'AAPL'], ['PRIVATE\n'], [f'A{i}' for i in range(1001)]]:
        with pytest.raises(SourceFetchError):
            module.acquire(symbols, module.new_attempt(), acquired_at=NOW)
    assert calls == []


def test_publication_day_uses_preceding_published_cycle(monkeypatch):
    transport(monkeypatch, [Response([row(settlementDate='2026-09-30', accountingYearMonthNumber=20260930)])])
    _, attempt, values, _ = acquire(['AAPL'], acquired_at=datetime(2026, 10, 26, 23, tzinfo=timezone.utc))
    assert values['AAPL']['date'] == '2026-09-30'
    assert attempt['query_coverage']['publication_day_ambiguous'] is True


def test_pre_run_expiry_and_all_cached_receipts_are_readable():
    module = importlib.import_module('tradingagents.strategies.data_sources.finra_query')
    for count, cached in [(1, 0), (1, 1), (0, 0)]:
        value = {'schema_version': 4, 'requested_count': count, 'cached_count': cached,
            'population_sha256': hashlib.sha256(b'["AAPL"]' if count else b'[]').hexdigest(), 'attempts': []}
        assert module.validated_acquisition(value) == value


def test_root_rollback_withdraws_already_normalized_positives(monkeypatch):
    transport(monkeypatch, [Response([row()])])
    module, attempt, _, _ = acquire(['AAPL'])
    module.fail_attempt(attempt, SourceFetchError('deadline', reason_code='timeout'))
    assert attempt['status'] == 'error' and attempt['reason_code'] == 'timeout'
    assert attempt['query_coverage']['accepted_symbols'] == []
    assert attempt['query_coverage']['missing_symbols'] == ['AAPL']
    module.validated_acquisition(receipt(module, attempt, ['AAPL']))


def test_rowset_digest_is_order_independent_while_response_hash_preserves_bytes(monkeypatch):
    transport(monkeypatch, [Response([row('A'), row('B')]), Response([row('B'), row('A')])])
    _, first, _, _ = acquire(['A', 'B'])
    _, second, _, _ = acquire(['A', 'B'])
    assert first['rowset_sha256'] == second['rowset_sha256']
    assert first['batches'][0]['response_sha256'] != second['batches'][0]['response_sha256']


def test_public_observation_fixture_preserves_all_twelve_exact_symbols(monkeypatch):
    fixture = json.loads((Path(__file__).parent / 'fixtures/finra_query_public_20261015.json').read_text())
    symbols = ['AAPL', 'ABR', 'ADM', 'CCJ', 'COPX', 'CPRI', 'CRI', 'DBA', 'MOO', 'SLV', 'UNG', 'UNH']
    transport(monkeypatch, [Response(fixture['rows'])])
    module, attempt, values, errors = acquire(symbols)
    assert sorted(values) == symbols and not errors
    assert values['AAPL'] == {'short_interest': 131591865.0, 'short_pct_of_float': None,
        'days_to_cover': 3.71, 'date': '2026-10-15'}
    module.validated_acquisition(receipt(module, attempt, symbols))


def test_canonicalization_failure_is_sanitized_and_always_closes_transport(monkeypatch):
    response = Response([row(opaque_provider_extension={'nested': ['PRIVATE']})])
    _, sessions = transport(monkeypatch, [response])
    module = importlib.import_module('tradingagents.strategies.data_sources.finra_query')
    def failed_canonicalization(_):
        raise RecursionError('PRIVATE provider extension too deep')
    monkeypatch.setattr(module, '_rowset_digest', failed_canonicalization)
    attempt = module.new_attempt()
    with pytest.raises(SourceFetchError) as failure:
        module.acquire(['AAPL'], attempt, acquired_at=NOW)
    assert failure.value.reason_code == 'invalid_response' and 'PRIVATE' not in str(failure.value)
    assert response.closed and sessions[0].closed
    assert attempt['status'] == 'error' and attempt['query_coverage']['accepted_symbols'] == []
    module.validated_acquisition(receipt(module, attempt, ['AAPL']))


def test_cross_batch_row_count_cannot_claim_a_row_in_a_different_batch(monkeypatch):
    symbols = [f'A{i:03}' for i in range(101)]
    transport(monkeypatch, [Response([row('A000')]), Response([])])
    module, attempt, _, _ = acquire(symbols)
    value = receipt(module, attempt, symbols)
    module.validated_acquisition(value)
    first, second = value['attempts'][0]['batches']
    first.update(row_count=0, record_total=0)
    second.update(row_count=1, record_total=1)
    with pytest.raises(ValueError):
        module.validated_acquisition(value)
