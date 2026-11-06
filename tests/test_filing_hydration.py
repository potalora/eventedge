"""One frozen corpus, complete required population, and genuine prior evidence."""
from contextvars import ContextVar
from copy import deepcopy
import threading
import time

import pytest

from test_filing_evidence import ACCESSION, OBSERVED, role, submission
from tradingagents.strategies.data_sources.filing_evidence import build_evidence, parse_submission
from tradingagents.strategies.data_sources.request_policy import provider_budget, current_provider_deadline
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
from tradingagents.strategies.data_sources.equity_universe import EquityUniverse, normalize_assets
from datetime import datetime, timezone


def accession(n):
    return f'0000000001-26-{n:06d}'


def row(n=1, form='8-K', date='2026-09-30', ciks=('1',), **extra):
    acc = accession(n)
    return {'adsh': acc, 'form_type': form, 'file_date': date, 'ciks': list(ciks),
            'ticker': 'WRONG', 'file_url': f'https://www.sec.gov/Archives/edgar/data/1/{acc.replace("-", "")}/{acc}-index.htm', **extra}


def evidence(record, *, ciks=None, body=None, subject=False):
    form = record['form_type']
    ciks = ciks or record['ciks']
    roles = ''.join(role('SUBJECT-COMPANY' if subject else 'FILER', cik, 'Sanitized issuer') for cik in ciks)
    if subject:
        roles = role('FILED-BY', '99', 'Reporting person') + roles
    raw = submission([(form, 'main.htm', body or '<p>' + 'Narrative ' * 1000 + 'COMPLETE END</p>')], form=form, roles=roles)
    raw = raw.replace(ACCESSION.encode(), record['adsh'].encode()).replace(b'20260930', record['file_date'].replace('-', '').encode())
    parsed = parse_submission(raw, expected_accession=record['adsh'], expected_form=form,
                              expected_date=record['file_date'], observed_at=OBSERVED)
    return build_evidence(parsed)


def history_row(record):
    return {'accession_number': record['adsh'], 'form': record['form_type'],
            'filing_date': record['file_date'], 'primary_document': 'main.htm'}


class Source:
    def __init__(self, records=(), histories=None):
        self.records = {r['adsh']: r for r in records}
        self.histories = histories or {}
        self.body_calls, self.history_calls, self.archive_calls = [], [], []
        self.overrides, self.archives = {}, {}

    def get_complete_submission(self, url, *, accession, form_type, filing_date, **kwargs):
        self.body_calls.append((accession, current_provider_deadline('edgar'), kwargs))
        value = self.overrides.get(accession)
        if isinstance(value, Exception):
            raise value
        return deepcopy(value) if value is not None else evidence(self.records[accession])

    def get_company_submission_history(self, cik):
        self.history_calls.append(cik)
        value = self.histories.get(cik, {'filings': [], 'archives': []})
        if isinstance(value, Exception):
            raise value
        return {'cik': cik, **deepcopy(value), 'coverage': {'complete': True}}

    def get_company_submission_archive(self, cik, descriptor):
        self.archive_calls.append((cik, descriptor['name']))
        value = self.archives[(cik, descriptor['name'])]
        if isinstance(value, Exception):
            raise value
        return {'cik': cik, 'filings': deepcopy(value), 'descriptor': descriptor,
                'coverage': {'complete': True}}


def hydrate(source, collections, **options):
    from tradingagents.strategies.data_sources.filing_hydration import hydrate_filings
    if current_provider_deadline('edgar') is not None:
        return hydrate_filings(source, collections, **options)
    with provider_budget('edgar', time.monotonic() + 10, limits=()):
        return hydrate_filings(source, collections, **options)


def test_cross_collection_dedupe_preserves_all_rows_query_refs_and_whole_text():
    r = row(matched_keywords=['quantum', 'PQC'])
    ordinary = [r, dict(r, query_ref='second-hit')]
    source = Source([r])
    result = hydrate(source, {'filings': ordinary, 'pqc_filings': [dict(r, matched_keyword='quantum')]})
    assert len(source.body_calls) == 1
    assert len(result['collections']['filings']) == 2
    assert result['collections']['filings'][1]['query_ref'] == 'second-hit'
    assert result['collections']['pqc_filings'][0]['matched_keyword'] == 'quantum'
    assert len(result['corpus']) == 1 and result['coverage']['complete'] is True
    assert result['corpus'][r['adsh']]['units'][0]['text'].endswith('COMPLETE END')
    assert len(result['corpus'][r['adsh']]['units'][0]['text']) > 5000
    assert 'current_text' not in result['collections']['filings'][0]
    assert r == ordinary[0] and 'filing_evidence_ref' not in r


def test_ordinary_amendments_not_required_but_pqc_and_ownership_amendments_are():
    ordinary = row(1, '8-K/A', universe_membership={'status': 'excluded'})
    ownership = row(2, 'SCHEDULE 13G/A')
    source = Source([ordinary, ownership])
    source.overrides[ownership['adsh']] = evidence(ownership, subject=True)
    result = hydrate(source, {'filings': [ordinary], 'passive_13g': [ownership], 'pqc_filings': [ordinary]})
    assert {r[0] for r in source.body_calls} == {ordinary['adsh'], ownership['adsh']}
    assert result['collections']['filings'][0]['text_status'] == 'not_required_form'
    assert result['collections']['pqc_filings'][0]['filing_evidence_ref'] == ordinary['adsh']


def test_excluded_ordinary_stays_audited_unknown_stays_required():
    outside = row(1, universe_membership={'status': 'excluded', 'reason': 'outside'})
    unknown = row(2, universe_membership={'status': 'unresolved'})
    source = Source([outside, unknown])
    result = hydrate(source, {'filings': [outside, unknown]})
    assert [x[0] for x in source.body_calls] == [unknown['adsh']]
    assert result['collections']['filings'][0]['text_status'] == 'outside_declared_equity_universe'
    assert result['coverage']['required_rows'] == 1


def test_conflicting_accession_metadata_fails_every_reference_without_fetch():
    r = row()
    source = Source([r])
    result = hydrate(source, {'filings': [r], 'pqc_filings': [dict(r, file_date='2026-09-29')]})
    assert source.body_calls == []
    assert result['coverage']['complete'] is False and result['coverage']['failed_rows'] == 2
    assert all(rows[0]['filing_failure']['code'] == 'conflicting_accession_metadata'
               for rows in result['collections'].values())


def test_history_memo_and_nearest_strictly_earlier_exact_form_prior_body_reuse():
    a, b, prior, wrong = row(1, '10-Q'), row(2, '10-Q'), row(3, '10-Q', '2026-06-30'), row(4, '10-Q/A', '2026-09-29')
    source = Source([a, b, prior, wrong], {'0000000001': {'filings': [history_row(x) for x in [a, b, wrong, prior]], 'archives': []}})
    result = hydrate(source, {'filings': [a, b, prior]})
    rows = result['collections']['filings']
    assert rows[0]['prior_evidence_ref'] == prior['adsh'] == rows[1]['prior_evidence_ref']
    assert rows[0]['prior_status'] == 'available'
    assert rows[2]['prior_status'] == 'missing_prior'
    assert source.history_calls == ['0000000001']
    assert len([x for x in source.body_calls if x[0] == prior['adsh']]) == 1
    assert result['coverage']['complete'] is False


def test_shared_pqc_row_does_not_inherit_ordinary_missing_prior_failure():
    current = row(1, '10-K')
    result = hydrate(Source([current]), {'filings': [current], 'pqc_filings': [current]})
    assert result['coverage']['required_rows'] == 2
    assert result['coverage']['prior_required_rows'] == 1
    assert result['coverage']['failed_rows'] == 1
    ordinary = result['collections']['filings'][0]
    thematic = result['collections']['pqc_filings'][0]
    assert ordinary['prior_status'] == 'missing_prior'
    assert thematic['filing_evidence_status'] == 'complete'
    assert thematic['prior_requirement'] == 'current_thematic_only'
    assert 'prior_status' not in thematic


def test_archive_overlap_is_fetched_and_older_irrelevant_archive_is_not():
    current, old, nearer = row(1, '10-K'), row(2, '10-K', '2024-09-30'), row(3, '10-K', '2025-09-30')
    relevant = {'name': 'CIK0000000001-submissions-001.json', 'filingFrom': '2025-01-01', 'filingTo': '2025-12-31', 'filingCount': 1}
    older = {**relevant, 'name': 'CIK0000000001-submissions-002.json', 'filingFrom': '2023-01-01', 'filingTo': '2023-12-31'}
    source = Source([current, old, nearer], {'0000000001': {'filings': [history_row(old)], 'archives': [older, relevant]}})
    source.archives[('0000000001', relevant['name'])] = [history_row(nearer)]
    result = hydrate(source, {'filings': [current]})
    assert result['collections']['filings'][0]['prior_evidence_ref'] == nearer['adsh']
    assert source.archive_calls == [('0000000001', relevant['name'])]


@pytest.mark.parametrize('kind', ['same_day_ambiguity', 'history_failure', 'archive_failure', 'prior_role_mismatch'])
def test_prior_ambiguity_or_unproven_metadata_never_selects_favorable_fallback(kind):
    current, prior, other = row(1, '10-K'), row(2, '10-K', '2025-09-30'), row(3, '10-K', '2025-09-30')
    history = {'filings': [history_row(prior)], 'archives': []}
    if kind == 'same_day_ambiguity':
        history['filings'].append(history_row(other))
    source = Source([current, prior, other], {'0000000001': history})
    if kind == 'history_failure':
        source.histories['0000000001'] = SourceFetchError('Offline failure', reason_code='timeout')
    if kind == 'archive_failure':
        desc = {'name': 'CIK0000000001-submissions-001.json', 'filingFrom': '2025-01-01', 'filingTo': '2026-01-01', 'filingCount': 1}
        history['archives'].append(desc)
        source.archives[('0000000001', desc['name'])] = SourceFetchError('Offline failure', reason_code='invalid_response')
    if kind == 'prior_role_mismatch':
        source.overrides[prior['adsh']] = evidence(prior, ciks=['2'])
    result = hydrate(source, {'filings': [current]})
    r = result['collections']['filings'][0]
    assert r['prior_status'] != 'available' and 'prior_evidence_ref' not in r
    assert result['coverage']['complete'] is False


@pytest.mark.parametrize('compatible', [True, False])
def test_joint_filers_require_same_prior_accession_and_complete_compatible_roles(compatible):
    current, prior, other = row(1, '10-K', ciks=('1', '2')), row(2, '10-K', '2025-09-30', ciks=('1', '2')), row(3, '10-K', '2025-09-30', ciks=('2',))
    source = Source([current, prior, other], {
        '0000000001': {'filings': [history_row(prior)], 'archives': []},
        '0000000002': {'filings': [history_row(prior if compatible else other)], 'archives': []}})
    result = hydrate(source, {'filings': [current]})
    r = result['collections']['filings'][0]
    assert sorted(source.history_calls) == ['0000000001', '0000000002']
    assert r['prior_status'] == ('available' if compatible else 'unresolved_joint_prior')
    if compatible:
        assert r['prior_evidence_ref'] == prior['adsh']
    else:
        assert 'prior_evidence_ref' not in r


def universe():
    assets = [{'class': 'us_equity', 'symbol': ticker, 'status': 'active', 'exchange': 'NASDAQ', 'tradable': True} for ticker in ['RIGHT', 'OTHER']]
    snapshot = normalize_assets(assets, observed_at=datetime.now(timezone.utc), response_sha256='a' * 64)
    company_map = {'0': {'cik_str': 1, 'ticker': 'RIGHT'}, '1': {'cik_str': 99, 'ticker': 'OTHER'}}
    return EquityUniverse(snapshot, company_map), company_map


@pytest.mark.parametrize('subjects', [('1',), ('1', '99')])
def test_ownership_binds_parsed_subject_only_never_display_or_filed_by(subjects):
    r = row(1, 'SCHEDULE 13D/A', ciks=('99', '1'))
    source = Source([r]);source.overrides[r['adsh']] = evidence(r, ciks=subjects, subject=True)
    u, company_map = universe()
    result = hydrate(source, {'activist_13d': [r]}, equity_universe=u, company_map=company_map)
    resolved = result['collections']['activist_13d'][0]
    assert resolved['ticker'] != 'WRONG'
    assert resolved['subject_attribution_verified'] is (len(subjects) == 1)
    assert resolved.get('subject_ticker', '') == ('RIGHT' if len(subjects) == 1 else '')
    assert result['corpus'][r['adsh']]['roles'][0]['role'] == 'FILED-BY'


def test_known_outside_subject_is_distinct_from_unresolved_ownership():
    r = row(1, 'SCHEDULE 13G', ciks=('2', '777'))
    source = Source([r]);source.overrides[r['adsh']] = evidence(r, ciks=['2'], subject=True)
    u, company_map = universe()
    company_map['2'] = {'cik_str': 2, 'ticker': 'OUTSIDE'}
    u = EquityUniverse(u.evidence, company_map)
    result = hydrate(source, {'passive_13g': [r]}, equity_universe=u, company_map=company_map)
    observed = result['collections']['passive_13g'][0]
    assert observed['issuer_binding']['status'] == 'verified'
    assert observed['issuer_binding']['execution_status'] == 'outside_declared_equity_universe'
    assert observed['issuer_binding']['equity_membership']['status'] == 'excluded'
    assert observed['ticker'] == ''
    assert result['coverage']['required_rows'] == 1  # Body discovery remains audited.
    assert result['coverage']['unresolved_ownership_rows'] == 0
    assert result['coverage']['resolved_outside_ownership_rows'] == 1
    assert result['coverage']['complete'] is True


def test_structural_failure_stays_insufficient_and_model_assessment_not_assessed():
    r = row()
    source = Source([r]);source.overrides[r['adsh']] = evidence(r, body='<p>Event.</p><a href="missing.htm">Exhibit</a>')
    source.overrides[r['adsh']]['structural_status'] = 'insufficient'
    source.overrides[r['adsh']]['issues'] = [{'code': 'missing_required_dependency'}]
    result = hydrate(source, {'filings': [r]})
    assert result['collections']['filings'][0]['filing_evidence_status'] == 'insufficient'
    assert result['corpus'][r['adsh']]['analysis_adequacy'] == 'not_assessed'
    assert result['coverage']['complete'] is False


def test_context_propagation_and_bounded_parallelism_preserve_output_order():
    marker = ContextVar('hydration_test_marker', default='lost')
    marker.set('original')
    records = [row(i) for i in range(1, 7)]
    source = Source(records)
    original = source.get_complete_submission
    active, peak, lock, seen = [0], [0], threading.Lock(), []
    def delayed(*args, **kwargs):
        with lock:
            active[0] += 1;peak[0] = max(peak[0], active[0])
            seen.append((marker.get(), current_provider_deadline('edgar')))
        time.sleep(.01)
        try:
            return original(*args, **kwargs)
        finally:
            with lock:active[0] -= 1
    source.get_complete_submission = delayed
    deadline = time.monotonic() + 10
    with provider_budget('edgar', deadline, limits=()):
        result = hydrate(source, {'filings': records[::-1]}, max_workers=2)
    assert peak[0] == 2 and all(x == ('original', deadline) for x in seen)
    assert [x['adsh'] for x in result['collections']['filings']] == [r['adsh'] for r in records[::-1]]


def test_late_completion_and_unscheduled_rows_all_fail_without_post_deadline_acceptance():
    clock = [0.0]
    records = [row(i) for i in range(1, 5)]
    source = Source(records)
    original = source.get_complete_submission
    def late(*args, **kwargs):
        result = original(*args, **kwargs)
        clock[0] = 11.0
        return result
    source.get_complete_submission = late
    with provider_budget('edgar', 10.0, clock=lambda: clock[0], limits=()):
        result = hydrate(source, {'filings': records}, max_workers=1)
    assert not result['corpus']
    assert result['coverage']['failed_rows'] == 4
    assert {r['filing_failure']['reason_code'] for r in result['collections']['filings']} == {'timeout'}


def test_blocked_worker_does_not_extend_owner_deadline_and_is_never_accepted_later():
    release = threading.Event()
    source = Source([row()])
    original = source.get_complete_submission
    def blocked(*args, **kwargs):
        release.wait(2)
        return original(*args, **kwargs)
    source.get_complete_submission = blocked
    start = time.monotonic()
    try:
        with provider_budget('edgar', start + .03, limits=()):
            result = hydrate(source, {'filings': [row()]}, max_workers=1)
        assert time.monotonic() - start < .5
        assert result['coverage']['failed_rows'] == 1 and not result['corpus']
    finally:
        release.set()


def test_missing_deadline_never_silently_creates_new_budget():
    from tradingagents.strategies.data_sources.filing_hydration import hydrate_filings
    with pytest.raises(ValueError, match='deadline'):
        hydrate_filings(Source([row()]), {'filings': [row()]})


def test_incomplete_discovery_cannot_be_masked_by_complete_bodies():
    from tradingagents.strategies.data_sources.evidence import CoverageRecords
    r = row()
    records = CoverageRecords([r], coverage={'mode': 'exhaustive_window', 'complete': False})
    result = hydrate(Source([r]), {'filings': records})
    assert result['coverage']['complete'] is False
    assert result['coverage']['discovery_complete'] is False
    assert result['collections']['filings'][0]['filing_evidence_ref'] == r['adsh']


def test_contradictory_duplicate_history_accession_blocks_comparison():
    current, prior = row(1, '10-K'), row(2, '10-K', '2025-09-30')
    desc = {'name': 'CIK0000000001-submissions-001.json', 'filingFrom': '2025-01-01', 'filingTo': '2026-01-01', 'filingCount': 1}
    source = Source([current, prior], {'0000000001': {'filings': [history_row(prior)], 'archives': [desc]}})
    source.archives[('0000000001', desc['name'])] = [{**history_row(prior), 'form': '10-Q'}]
    result = hydrate(source, {'filings': [current]})
    assert result['collections']['filings'][0]['prior_status'] == 'conflicting_history_metadata'
    assert result['coverage']['complete'] is False


def test_insufficient_ownership_evidence_never_binds_even_valid_header_roles():
    r = row(1, 'SCHEDULE 13D')
    source = Source([r]);source.overrides[r['adsh']] = evidence(r, subject=True)
    source.overrides[r['adsh']]['structural_status'] = 'insufficient'
    source.overrides[r['adsh']]['issues'] = [{'code': 'ownership_subject_cik_mismatch'}]
    u, company_map = universe()
    result = hydrate(source, {'activist_13d': [r]}, equity_universe=u, company_map=company_map)
    candidate = result['collections']['activist_13d'][0]
    assert candidate['subject_attribution_verified'] is False
    assert candidate['ticker'] == '' and candidate['subject_ticker'] == ''


def test_body_failure_preserves_safe_status_for_every_ref_and_continues_other_rows():
    bad, good = row(1), row(2)
    source = Source([bad, good])
    source.overrides[bad['adsh']] = SourceFetchError('Fixed failure', reason_code='http_error', http_status=403)
    result = hydrate(source, {'filings': [bad, good], 'pqc_filings': [bad]})
    assert result['collections']['filings'][1]['text_status'] == 'available'
    assert result['coverage']['failed_rows'] == 2
    assert result['collections']['pqc_filings'][0]['filing_failure']['http_status'] == 403


def test_single_source_issuer_binding_is_separate_from_execution_ticker():
    r = row()
    result = hydrate(Source([r]), {'pqc_filings': [r]})
    binding = result['collections']['pqc_filings'][0]['issuer_binding']
    unit = result['corpus'][r['adsh']]
    assert binding['status'] == 'verified' and binding['issuer_cik'] == '0000000001'
    assert binding['role'] == 'FILER'
    assert binding['submission_sha256'] == unit['submission_sha256']
    assert binding['header_sha256'] == unit['header_sha256']
    assert binding['role_sha256s'] == [unit['roles'][0]['sha256']]
    assert binding['execution_status'] == 'unresolved' and 'ticker' not in binding


def test_joint_source_roles_preserve_proof_without_single_issuer_guess():
    r = row(ciks=('1', '2'))
    result = hydrate(Source([r]), {'filings': [r]})
    binding = result['collections']['filings'][0]['issuer_binding']
    assert binding['status'] == 'unresolved' and 'issuer_cik' not in binding
    assert binding['issuer_ciks'] == ['0000000001', '0000000002']
    assert len(binding['role_sha256s']) == 2
    assert result['coverage']['complete'] is False


def test_monitor_opt_in_wrapper_calls_one_complete_population_and_keeps_legacy_default():
    from tradingagents.strategies.learning.event_monitor import EventMonitor
    r = row()
    source = Source([r]);source.is_available = lambda: True
    class Registry:
        def get(self, name):
            return source
    legacy = EventMonitor(Registry())
    with pytest.raises(ValueError, match='policy'):
        legacy.hydrate_collections({'filings': [r]})
    monitor = EventMonitor(Registry(), filing_policy='complete_submission_v1')
    with provider_budget('edgar', time.monotonic() + 10, limits=()):
        result = monitor.hydrate_collections({'filings': [r], 'pqc_filings': [r]})
    assert len(source.body_calls) == 1
    assert result['collections']['filings'][0]['filing_evidence_ref'] == r['adsh']
    with pytest.raises(ValueError):
        EventMonitor(Registry(), filing_policy='silent_prefix')


def test_aggregate_evidence_bound_accounts_for_every_rejected_document():
    records = [row(1), row(2)]
    result = hydrate(Source(records), {'filings': records, 'pqc_filings': [records[0]]}, max_evidence_bytes=500)
    assert result['corpus'] == {} and result['coverage']['complete'] is False
    assert result['coverage']['failed_rows'] == 3
    assert result['coverage']['evidence_byte_limit'] == 500
    assert result['coverage']['accepted_evidence_bytes'] == 0
    assert result['coverage']['rejected_evidence_objects'] == 2
    assert {r['filing_failure']['code'] for rows in result['collections'].values() for r in rows} == {'evidence_byte_limit'}


def test_aggregate_bound_includes_recent_and_archive_metadata():
    r = row(1, '10-K')
    previous = [history_row(row(n, '10-K', '2025-09-30')) for n in range(2, 12)]
    source = Source([r], {'0000000001': {'filings': previous, 'archives': []}})
    result = hydrate(source, {'filings': [r]}, max_evidence_bytes=500)
    assert result['history_corpus']['0000000001']['failure']['code'] == 'evidence_byte_limit'
    assert result['coverage']['accepted_evidence_bytes'] == 0
    assert result['coverage']['rejected_evidence_objects'] == 2


def test_conflict_flag_from_deduplicated_discovery_never_fetches_chosen_first_identity():
    r = row(discovery_identity_conflict=True)
    source = Source([r])
    result = hydrate(source, {'pqc_filings': [r]})
    assert source.body_calls == []
    assert result['collections']['pqc_filings'][0]['filing_failure']['code'] == 'conflicting_accession_metadata'


def test_archive_metadata_also_consumes_the_aggregate_evidence_budget():
    current, old = row(1, '10-K'), row(2, '10-K', '2024-09-30')
    desc = {'name': 'CIK0000000001-submissions-001.json', 'filingFrom': '2025-01-01', 'filingTo': '2025-12-31', 'filingCount': 100}
    source = Source([current], {'0000000001': {'filings': [history_row(old)], 'archives': [desc]}})
    source.overrides[current['adsh']] = evidence(current, body='<p>Whole current.</p>')
    source.archives[('0000000001', desc['name'])] = [history_row(row(n, '10-K', '2025-09-30')) for n in range(3, 103)]
    result = hydrate(source, {'filings': [current]}, max_evidence_bytes=4000)
    archive = result['archive_corpus']['0000000001/' + desc['name']]
    assert archive['failure']['code'] == 'evidence_byte_limit'
    assert result['collections']['filings'][0]['prior_status'] == 'unproven_prior'
    assert 0 < result['coverage']['accepted_evidence_bytes'] < 4000
    assert result['coverage']['rejected_evidence_bytes'] > 4000


def test_actual_parsed_filer_not_display_cik_selects_the_prior_history():
    current, prior = row(1, '10-Q', ciks=('99',)), row(2, '10-Q', '2026-06-30', ciks=('1',))
    source = Source([current, prior], {'0000000001': {'filings': [history_row(prior)], 'archives': []}})
    source.overrides[current['adsh']] = evidence(current, ciks=['1'])
    result = hydrate(source, {'filings': [current]})
    r = result['collections']['filings'][0]
    assert r['prior_status'] == 'available' and r['issuer_binding']['issuer_cik'] == '0000000001'
    assert r['prior_comparison']['issuer_ciks'] == ['0000000001']
    assert sorted(source.history_calls) == ['0000000001', '0000000099']


def test_native_nc_direct_fixtures_share_real_global_pacing_and_exact_source_roles(monkeypatch):
    from pathlib import Path
    import requests
    from tradingagents.strategies.data_sources.edgar_source import EDGARSource
    from test_filing_acquisition import Response
    cases = [
        ('native_8k_primary.nc', '0001193125-26-409112', '8-K', '2026-09-30', '2065397'),
        ('native_13d.nc', '0001193125-26-409121', 'SCHEDULE 13D', '2026-09-30', '64996'),
        ('native_13g_direct.txt', '0002042926-26-000016', 'SCHEDULE 13G/A', '2026-10-09', '1888151')]
    raw, records = {}, []
    for filename, acc, form, date, cik in cases:
        raw[acc] = (Path(__file__).parent / 'fixtures' / 'filing_evidence' / filename).read_bytes()
        records.append({'adsh': acc, 'form_type': form, 'file_date': date, 'ciks': [cik],
                        'file_url': f'https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace("-", "")}/{acc}-index.htm'})
    starts, responses = [], []
    deadline = time.monotonic() + 5
    def get(url, **kwargs):
        starts.append((time.monotonic(), current_provider_deadline('edgar')))
        acc = url.rsplit('/', 1)[1].removesuffix('.txt')
        response = Response(raw[acc]);response.url = url
        responses.append(response)
        return response
    monkeypatch.setattr(requests, 'get', get)
    u, _ = universe()
    company_map = {'0': {'cik_str': 64996, 'ticker': 'RIGHT'}, '1': {'cik_str': 1888151, 'ticker': 'OTHER'}}
    with provider_budget('edgar', deadline, max_attempts=1):
        result = hydrate(EDGARSource(), {'pqc_filings': records, 'passive_13g': [records[2]]},
                         equity_universe=u, company_map=company_map, max_workers=16)
    assert len(starts) == 3 and all(t[1] == deadline for t in starts)
    assert starts[-1][0] - starts[0][0] >= .19
    assert all(response.closed for response in responses)
    assert result['coverage']['complete'] is True
    ownership = result['collections']['passive_13g'][0]
    assert ownership['subject_ticker'] == 'OTHER'
    assert ownership['issuer_binding']['issuer_cik'] == '0001888151'
    assert result['corpus']['0002042926-26-000016']['analysis_adequacy'] == 'not_assessed'


def test_pqc_only_annual_evidence_does_not_invent_filing_change_prior_obligation():
    r = row(1, '10-K')
    source = Source([r])
    result = hydrate(source, {'pqc_filings': [r]})
    assert source.history_calls == []
    candidate = result['collections']['pqc_filings'][0]
    assert candidate['requires_prior'] is False
    assert candidate['prior_requirement'] == 'current_thematic_only'
    assert 'prior_status' not in candidate and result['coverage']['complete'] is True
    assert result['coverage']['prior_required_accessions'] == 0


def test_joint_history_same_accession_with_conflicting_dates_never_proves_prior():
    current, prior = row(1, '10-K', ciks=('1', '2')), row(2, '10-K', '2025-09-30', ciks=('1', '2'))
    source = Source([current, prior], {
        '0000000001': {'filings': [history_row(prior)], 'archives': []},
        '0000000002': {'filings': [{**history_row(prior), 'filing_date': '2024-09-30'}], 'archives': []}})
    result = hydrate(source, {'filings': [current]})
    candidate = result['collections']['filings'][0]
    assert candidate['prior_status'] == 'conflicting_history_metadata'
    assert 'prior_evidence_ref' not in candidate


def test_contradictory_current_accession_as_prior_is_never_available():
    current = row(1, '10-K')
    source = Source([current], {'0000000001': {'filings': [{**history_row(current), 'filing_date': '2025-09-30'}], 'archives': []}})
    result = hydrate(source, {'filings': [current]})
    candidate = result['collections']['filings'][0]
    assert candidate['prior_status'] != 'available' and 'prior_evidence_ref' not in candidate


def test_comparison_binding_hashes_exact_once_stored_consulted_history_proof():
    import hashlib
    import json
    current, prior = row(1, '10-Q'), row(2, '10-Q', '2026-06-30')
    source = Source([current, prior], {'0000000001': {'filings': [history_row(prior)], 'archives': []}})
    result = hydrate(source, {'filings': [current], 'pqc_filings': [current]})
    r = result['collections']['filings'][0]
    assert r['requires_prior'] is True and r['prior_requirement'] == 'ordinary_filing_change'
    binding = r['comparison_binding']
    assert binding['policy'] == 'nearest_strictly_earlier_exact_form_v1'
    assert binding['current_accession'] == current['adsh'] and binding['prior_accession'] == prior['adsh']
    assert binding['form_type'] == '10-Q' and binding['prior_filing_date'] == '2026-06-30'
    assert binding['history_refs'] == ['0000000001'] and binding['archive_refs'] == []
    proof = {'recent': result['history_corpus'], 'archives': {}}
    expected = hashlib.sha256(json.dumps(proof, sort_keys=True, separators=(',', ':'),
                                        ensure_ascii=True, allow_nan=False).encode()).hexdigest()
    assert binding['history_snapshot_sha256'] == expected
    assert result['collections']['pqc_filings'][0]['comparison_binding'] == binding
    assert len(result['corpus']) == 2 and result['coverage']['prior_required_accessions'] == 1
