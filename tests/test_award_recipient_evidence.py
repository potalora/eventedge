"""Replay recipient proof against recorded native shapes and hostile alterations."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib
import json

import pytest

from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError

MODULE = 'tradingagents.strategies.data_sources.award_recipient_evidence'
BASE = 'https://api.usaspending.gov/api/v2/'
NOW = datetime(2026, 10, 23, 7, tzinfo=timezone.utc)
FIELDS = ['Award ID', 'Recipient Name', 'Recipient UEI', 'recipient_id', 'Action Date',
          'Transaction Amount', 'Awarding Agency', 'Transaction Description']


def digest(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def envelope(method, url, request, body, second, status=200):
    return dict(method=method, url=BASE + url, request=request, status=status,
                observed_at=f'2026-10-23T06:00:{second:02d}+00:00', body=body,
                body_sha256=digest(body))


@pytest.fixture
def proof():
    # Public native row retained verbatim from the 2026-10-23 investigation.
    raw = {'internal_id': 363616285, 'Award ID': '36C24827C0008',
           'Recipient Name': 'VETERANS DEVELOPMENT CORPORATION, INC.',
           'Recipient UEI': 'X3TQE41XJCA6',
           'recipient_id': '87798ff6-9a29-9efd-f1b2-b3e3bbf326a4-C',
           'Award Amount': 63117507.88, 'Base Obligation Date': '2026-10-21',
           'generated_internal_id': 'CONT_AWD_36C24827C0008_3600_-NONE-_-NONE-',
           'Awarding Agency': 'Department of Veterans Affairs', 'Start Date': '2026-10-21',
           'Last Modified Date': '2026-10-21 15:23:04',
           'Description': 'BB - REPLACE DETERIORATED AHU 21, 22, 23 AND 24, PROJECT NUMBER 573-21-705.',
           'awarding_agency_id': 561, 'agency_slug': 'department-of-veterans-affairs'}
    contract = dict(award_id=raw['Award ID'], award_key=raw['generated_internal_id'],
                    generated_internal_id=raw['generated_internal_id'], internal_id=raw['internal_id'],
                    recipient_name=raw['Recipient Name'], recipient_uei=raw['Recipient UEI'],
                    recipient_id=raw['recipient_id'], base_obligation_date='2026-10-21',
                    award_scope='new_awards_only', amount_basis='cumulative_award_obligations',
                    observed_at='2026-10-23T06:00:25+00:00', amount=63117507.88,
                    agency=raw['Awarding Agency'], start_date='2026-10-21',
                    last_modified_date='2026-10-21', description=raw['Description'],
                    parent_recipient_uei='', recipient_identity_status='native_recipient_verified',
                    recipient_identity_source=BASE + f"recipient/{raw['recipient_id']}/?year=all")
    tx = {key: raw[key] for key in ['Award ID', 'Recipient Name', 'Recipient UEI', 'recipient_id',
                                   'Awarding Agency', 'generated_internal_id', 'internal_id']}
    tx.update({'Action Date': '2026-10-21', 'Transaction Amount': 63117507.88,
               'Transaction Description': raw['Description']})
    request = dict(filters={'award_type_codes': ['A', 'B', 'C', 'D'],
                            'award_unique_id': raw['generated_internal_id']}, fields=FIELDS,
                   page=1, limit=10, sort='Transaction Amount', order='desc')
    response = dict(limit=10, results=[tx], page_metadata=dict(page=1, next=None, previous=None,
                                                            hasNext=False, hasPrevious=False))
    profile = dict(name=raw['Recipient Name'], uei=raw['Recipient UEI'], recipient_id=raw['recipient_id'],
                   recipient_level='C', parent_uei='X3TQE41XJCA6', parents=[{'parent_uei': 'X3TQE41XJCA6'}])
    contract['recipient_identity_evidence'] = dict(policy='exact_award_recipient_v1', search_award=raw,
        detail_attempt=envelope('GET', f"awards/{raw['generated_internal_id']}/", None,
            {'detail': f"No Award found with: {{'generated_unique_award_id_legacy': '{raw['generated_internal_id']}'}}"}, 30, 404),
        transaction_pages=[envelope('POST', 'search/spending_by_transaction/', request, response, 35)],
        recipient_profile=envelope('GET', f"recipient/{raw['recipient_id']}/?year=all", None, profile, 40))
    return contract


def validate(proof):
    return importlib.import_module(MODULE).validate_recipient_fallback(proof, now=NOW)


def test_recipient_fallback_replays_native_proof_without_mutating_or_resolving_parent(proof):
    before = deepcopy(proof)
    evidence = validate(proof)
    assert evidence == proof['recipient_identity_evidence']
    assert proof == before
    assert proof['parent_recipient_uei'] == ''


def test_recipient_fallback_accepts_old_replay_without_expiring_its_observations(proof):
    module = importlib.import_module(MODULE)
    assert module.validate_recipient_fallback(proof, now=datetime(2030, 1, 1, tzinfo=timezone.utc))


def test_future_modification_is_not_evidence_available_at_search_observation(proof):
    proof['recipient_identity_evidence']['search_award']['Last Modified Date'] = '2030-01-01 12:00:00'
    proof['last_modified_date'] = '2030-01-01'
    with pytest.raises(SourceFetchError):
        validate(proof)


def test_future_performance_start_does_not_change_award_publication_evidence(proof):
    proof['recipient_identity_evidence']['search_award']['Start Date'] = '2030-01-01'
    proof['start_date'] = '2030-01-01'
    assert validate(proof)


@pytest.mark.parametrize(('field', 'bad'), [
    ('award_id', 'WRONG'), ('award_key', 'WRONG'), ('generated_internal_id', 'WRONG'),
    ('internal_id', 1), ('internal_id', True), ('recipient_name', 'OTHER'),
    ('recipient_uei', 'OTHER1234567'), ('recipient_id', 'other-C'), ('amount', 1),
    ('agency', 'OTHER'), ('description', 'OTHER'), ('start_date', '2026-10-20'),
    ('last_modified_date', '2026-10-20'), ('base_obligation_date', '2026-10-20'),
    ('parent_recipient_uei', 'X3TQE41XJCA6'), ('recipient_identity_status', 'native_award_verified'),
    ('recipient_identity_source', 'https://evil.test/'), ('award_scope', 'transactions'),
    ('amount_basis', 'initial_obligation'), ('observed_at', '2026-10-23T06:00:25'),
    ('observed_at', '2026-10-24T06:00:25+00:00'), ('observed_at', 'bad'),
])
def test_recipient_fallback_rejects_contract_not_bound_to_search(proof, field, bad):
    proof[field] = bad
    with pytest.raises(SourceFetchError) as error:
        validate(proof)
    assert error.value.reason_code == 'invalid_response'


@pytest.mark.parametrize('part', ['detail_attempt', 'recipient_profile', 'transaction_pages'])
@pytest.mark.parametrize(('field', 'bad'), [('url', 'https://evil.test/'), ('status', 201),
    ('observed_at', 'bad'), ('observed_at', '2026-10-23T06:00:00+00:00'),
    ('observed_at', '2026-10-24T06:00:40+00:00'), ('observed_at', '2026-10-23T06:00:40'),
    ('body_sha256', '0' * 64), ('method', 'DELETE')])
def test_recipient_fallback_rejects_forged_request_envelope(proof, part, field, bad):
    item = proof['recipient_identity_evidence'][part]
    if isinstance(item, list):
        item = item[0]
    item[field] = bad
    with pytest.raises(SourceFetchError):
        validate(proof)


@pytest.mark.parametrize('recompute', [False, True])
@pytest.mark.parametrize(('part', 'field', 'bad'), [
    ('transaction_pages', 'generated_internal_id', 'OTHER'),
    ('transaction_pages', 'internal_id', 1), ('transaction_pages', 'Award ID', 'OTHER'),
    ('transaction_pages', 'Recipient UEI', 'OTHER1234567'),
    ('transaction_pages', 'recipient_id', 'other-C'),
    ('transaction_pages', 'Recipient Name', 'OTHER'),
    ('transaction_pages', 'Awarding Agency', 'OTHER'),
    ('transaction_pages', 'Transaction Description', 'OTHER'),
    ('transaction_pages', 'Transaction Amount', 1),
    ('transaction_pages', 'Action Date', '2026-10-20'),
    ('transaction_pages', 'Action Date', '2026-10-24'),
    ('recipient_profile', 'name', 'OTHER'), ('recipient_profile', 'uei', 'OTHER1234567'),
    ('recipient_profile', 'recipient_id', 'other-C'), ('recipient_profile', 'recipient_level', 'P'),
])
def test_recipient_fallback_rechecks_raw_bodies_even_when_digests_recomputed(proof, part, field, bad, recompute):
    item = proof['recipient_identity_evidence'][part]
    if isinstance(item, list):
        item = item[0]
        item['body']['results'][0][field] = bad
    else:
        item['body'][field] = bad
    if recompute:
        item['body_sha256'] = digest(item['body'])
    with pytest.raises(SourceFetchError):
        validate(proof)


@pytest.mark.parametrize('bad', ['no_evidence', 'wrong_policy', 'wrong_filter', 'extra_filter',
    'wrong_fields', 'missing_request', 'bad_detail_body', 'empty', 'missing_pages',
    'incomplete', 'fake_next', 'wrong_page', 'duplicate', 'overlong_capture'])
def test_recipient_fallback_rejects_unsupported_or_incomplete_proof(proof, bad):
    evidence = proof['recipient_identity_evidence']
    page = evidence['transaction_pages'][0]
    if bad == 'no_evidence':
        del proof['recipient_identity_evidence']
    elif bad == 'wrong_policy':
        evidence['policy'] = 'future_policy'
    elif bad == 'wrong_filter':
        page['request']['filters']['award_unique_id'] = 'OTHER'
    elif bad == 'extra_filter':
        page['request']['filters']['time_period'] = [{'start_date': '2026-10-21'}]
    elif bad == 'wrong_fields':
        page['request']['fields'] = ['Award ID']
    elif bad == 'missing_request':
        del page['request']
    elif bad == 'bad_detail_body':
        evidence['detail_attempt']['body'] = {}
        evidence['detail_attempt']['body_sha256'] = digest({})
    elif bad == 'empty':
        page['body']['results'] = []
    elif bad == 'missing_pages':
        evidence['transaction_pages'] = []
    elif bad == 'incomplete':
        page['body']['page_metadata'].update(hasNext=True, next=2)
    elif bad == 'fake_next':
        page['body']['page_metadata']['next'] = 2
    elif bad == 'wrong_page':
        page['body']['page_metadata']['page'] = 2
    elif bad == 'duplicate':
        page['body']['results'] *= 2
    elif bad == 'overlong_capture':
        evidence['recipient_profile']['observed_at'] = '2026-10-23T06:11:00+00:00'
    page['body_sha256'] = digest(page['body'])
    with pytest.raises(SourceFetchError):
        validate(proof)


def test_replay_checked_in_native_bodies_with_explicit_synthetic_ordered_clock(proof):
    from pathlib import Path
    recorded = json.loads((Path(__file__).parent / 'fixtures' /
                           'usaspending-recipient-fallback-2026-10-23.json').read_text())
    responses = recorded['responses']
    raw = next(row for row in responses['search']['body']['results']
               if row['internal_id'] == proof['internal_id'])
    evidence = proof['recipient_identity_evidence']
    evidence['search_award'] = deepcopy(raw)
    # Recorded bodies are unmodified. These observation times are deliberately
    # synthetic: the live investigation probed profile before transactions, so
    # its original chronology is not an accepted production proof.
    for part, response, second in [('detail_attempt', 'detail', 30),
                                    ('transaction_pages', 'transactions', 35),
                                    ('recipient_profile', 'recipient', 40)]:
        record = responses[response]
        assert json.loads(record['body_raw']) == record['body']
        captured = record['capture']
        item = envelope(captured['method'], captured['url'].removeprefix(BASE),
                        deepcopy(captured['request']), deepcopy(record['body']),
                        second, captured['status'])
        evidence[part] = [item] if part == 'transaction_pages' else item
    assert validate(proof) == evidence


def two_page_proof(proof):
    """A synthetic complete 11-row result set, each row distinguishable by amount."""
    page = proof['recipient_identity_evidence']['transaction_pages'][0]
    template = page['body']['results'][0]
    rows = []
    for amount in range(11, 0, -1):
        row = deepcopy(template)
        row['Transaction Amount'] = amount
        rows.append(row)
    proof['amount'] = sum(range(1, 12))
    proof['recipient_identity_evidence']['search_award']['Award Amount'] = proof['amount']
    page['body']['results'] = rows[:10]
    page['body']['page_metadata'].update(next=2, hasNext=True)
    second = deepcopy(page)
    second['request']['page'] = 2
    second['observed_at'] = '2026-10-23T06:00:36+00:00'
    second['body']['results'] = rows[10:]
    second['body']['page_metadata'].update(page=2, next=None, previous=1,
                                         hasNext=False, hasPrevious=True)
    page['body_sha256'] = digest(page['body'])
    second['body_sha256'] = digest(second['body'])
    proof['recipient_identity_evidence']['transaction_pages'].append(second)
    return proof


def test_complete_multiple_transaction_pages_reconcile(proof):
    assert validate(two_page_proof(proof))


@pytest.mark.parametrize('bad', ['duplicate_page', 'missing_middle_rows', 'backwards_clock',
                               'wrong_previous', 'wrong_previous_flag', 'unsorted_rows',
                               'boolean_page', 'boolean_limit', 'boolean_metadata_page'])
def test_reject_incoherent_multi_page_transaction_proof(proof, bad):
    two_page_proof(proof)
    pages = proof['recipient_identity_evidence']['transaction_pages']
    if bad == 'duplicate_page':
        pages[1]['body']['results'] = deepcopy(pages[0]['body']['results'])
    elif bad == 'missing_middle_rows':
        pages[0]['body']['results'].pop()
    elif bad == 'backwards_clock':
        pages[1]['observed_at'] = '2026-10-23T06:00:34+00:00'
    elif bad == 'wrong_previous':
        pages[1]['body']['page_metadata']['previous'] = None
    elif bad == 'wrong_previous_flag':
        pages[1]['body']['page_metadata']['hasPrevious'] = False
    elif bad == 'unsorted_rows':
        pages[0]['body']['results'].reverse()
    elif bad == 'boolean_page':
        pages[0]['request']['page'] = True
    elif bad == 'boolean_limit':
        pages[0]['body']['limit'] = True
    elif bad == 'boolean_metadata_page':
        pages[0]['body']['page_metadata']['page'] = True
    for page in pages:
        page['body_sha256'] = digest(page['body'])
    with pytest.raises(SourceFetchError):
        validate(proof)


@pytest.mark.parametrize('part', ['detail_attempt', 'recipient_profile'])
@pytest.mark.parametrize('request_value', [{}, [], False, ''])
def test_get_envelopes_require_explicit_null_request(proof, part, request_value):
    proof['recipient_identity_evidence'][part]['request'] = request_value
    with pytest.raises(SourceFetchError):
        validate(proof)


@pytest.mark.parametrize('bad', [None, [], '', True, 0])
def test_malformed_evidence_fails_with_bounded_source_error(proof, bad):
    proof['recipient_identity_evidence'] = bad
    with pytest.raises(SourceFetchError) as error:
        validate(proof)
    assert error.value.reason_code == 'invalid_response'


@pytest.mark.parametrize('amount', [True, '63117507.88', float('nan'), float('inf'), -1, 0, 63117507.881])
def test_award_amount_requires_positive_native_finite_cents(proof, amount):
    proof['amount'] = amount
    proof['recipient_identity_evidence']['search_award']['Award Amount'] = amount
    with pytest.raises(SourceFetchError):
        validate(proof)


def test_profile_parent_metadata_is_neither_accepted_nor_used_for_ownership(proof):
    profile = proof['recipient_identity_evidence']['recipient_profile']
    profile['body'].update(parent_uei='DIFFERENTUEI', parents=[{'parent_uei': 'OTHERPARENT'}])
    profile['body_sha256'] = digest(profile['body'])
    assert validate(proof)
    assert proof['parent_recipient_uei'] == ''


def test_canonical_digest_and_failure_operation_are_deterministic():
    module = importlib.import_module(MODULE)
    assert module.canonical_json_sha256({'b': 2, 'a': 'é'}) == digest({'a': 'é', 'b': 2})
    url = BASE + 'awards/example/'
    assert module.detail_request_operation(url) == 'award_' + hashlib.sha256(url.encode()).hexdigest()[:58]
