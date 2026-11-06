"""Research containment keeps strict evidence failures and closes every input."""
from copy import deepcopy
import importlib
import importlib.util
import time

import pytest

from test_filing_ownership_acquisition_validation import CONFIG as BASE_CONFIG
from test_filing_ownership_hydration import NativeSource, ordinary, filing, record, agreement_case
from test_filing_supported_audit import capture_extra
from tradingagents.strategies.orchestration.filing_material_validation import (
    build_material_scope, validate_filing_material_policy,
)
from tradingagents.strategies.orchestration.filing_acquisition_validation import validate_filing_acquisition_policy
from tradingagents.strategies.orchestration.filing_supported_audit import validate_supported_raw_capture
from tradingagents.strategies.data_sources.evidence import CoverageRecords

CONFIG = {**BASE_CONFIG, 'filing_material_policy': 'verified_supported_documents_v4',
          'research_resilience_policy': 'dependency_isolation_v1'}
MODULE = 'tradingagents.strategies.orchestration.filing_research_isolation'


def api():
    assert importlib.util.find_spec(MODULE) is not None, 'filing research isolation is not implemented'
    return importlib.import_module(MODULE)


def captured(tmp_path, monkeypatch, source, collections):
    monkeypatch.setattr('test_filing_ownership_hydration.POLICY', CONFIG['filing_material_policy'])
    collections = {key: CoverageRecords(collections.get(key, []), coverage={
        'mode': 'exhaustive_window', 'complete': True}) for key in
        ('filings', 'activist_13d', 'passive_13g', 'pqc_filings')}
    return capture_extra(tmp_path, monkeypatch, source, collections)


def scopes(data):
    return dict(strict_material_scope=validate_filing_material_policy(data, CONFIG),
                acquisition_scope=validate_filing_acquisition_policy(data, CONFIG))


def isolate(data):
    return api().isolate_filing_research(data['edgar'], CONFIG, **scopes(data))


def mixed(tmp_path, monkeypatch, *, pqc=False):
    good = ordinary(101, '8-K', '2026-09-30')
    bad = ordinary(102, '8-K', '2026-09-30')
    source = NativeSource(extra=(good, (bad[0], b'previously unseen malformed original')))
    collections = {'filings': [good[0], bad[0]]}
    if pqc:
        collections['pqc_filings'] = deepcopy(collections['filings'])
    data, paths = captured(tmp_path, monkeypatch, source, collections)
    return data, paths, good[0]['adsh'], bad[0]['adsh']


def test_independent_good_filing_survives_unseen_failed_original_without_recertifying_failure(tmp_path, monkeypatch):
    data, paths, good, bad = mixed(tmp_path, monkeypatch)
    before = deepcopy(data)
    projected, receipt = isolate(data)
    assert data == before
    assert data['edgar']['filing_evidence']['coverage']['failed_rows'] == 1
    assert data['edgar']['filing_evidence']['coverage']['complete'] is False
    assert [r['adsh'] for r in projected['filings']] == [good]
    assert set(projected['filing_evidence']['corpus']) == {good}
    assert 'collections' not in projected['filing_evidence']
    assert receipt['version'] == 'filing-research-isolation-v1'
    assert receipt['included_refs'] == [good] and receipt['excluded_refs'] == [bad]
    assert receipt['order_authority'] is False and receipt['original_full_coverage'] is False
    assert len(receipt['occurrences']) == 2
    failure = receipt['occurrences'][1]
    assert failure['outcome'] == 'excluded' and failure['recorded_failure']
    assert failure['parser_failure_independently_certified'] is False
    assert failure['original_sha256'] == data['edgar']['filing_evidence']['acquisition_scope']['originals'][1]['sha256']
    deadline = time.monotonic() + 10
    audit = validate_supported_raw_capture(data, CONFIG, paths, check=lambda: deadline-time.monotonic())
    assert audit['raw_audit_complete'] and audit['failed_evidence_originals'] == 1
    assert audit['completed_originals'] == 2
    assert api().validate_filing_research_isolation(data['edgar'], receipt, CONFIG,
        projected_edgar=projected) == receipt


def test_mixed_projection_passes_real_strategy_screen_and_complete_analyzer_request(tmp_path, monkeypatch):
    from test_source_inputs import NOW
    from test_filing_ownership_hydration import SUBJECT
    from tradingagents.strategies.modules.filing_analysis import FilingAnalysisStrategy
    from tradingagents.strategies.orchestration.filing_inputs import filing_analysis_inputs
    from tradingagents.strategies.data_sources.filing_assessment import prepare_request
    from tradingagents.strategies.data_sources.equity_universe import EquityUniverse, normalize_assets
    data, _, good, bad = mixed(tmp_path, monkeypatch)
    company = {'0': {'cik_str': int(SUBJECT), 'ticker': 'RIGHT'}}
    data['edgar']['company_tickers'] = company
    snapshot = normalize_assets([{'class': 'us_equity', 'symbol': 'RIGHT', 'exchange': 'NASDAQ',
        'status': 'active', 'tradable': True}], observed_at=NOW, response_sha256='f'*64)
    universe = EquityUniverse(snapshot, company_map=company)
    projected, receipt = isolate(data)
    strategy = FilingAnalysisStrategy()
    candidates = strategy.screen({'edgar': projected}, '2026-10-09', strategy.get_default_params())
    assert len(candidates) == 1 and candidates[0].ticker == 'RIGHT'
    assert candidates[0].metadata['filing_evidence_ref'] == good
    resolved = filing_analysis_inputs(candidates[0], {'edgar': projected}, universe)
    request = prepare_request(candidates[0].metadata['analysis_type'], **resolved)
    assert good in request.user and bad not in request.user
    assert resolved['target_binding']['status'] == 'verified'
    assert data['edgar']['filing_evidence']['coverage']['failed_rows'] == 1
    assert receipt['order_authority'] is False


@pytest.mark.parametrize('damage', ['receipt', 'accepted_hash', 'missing_original', 'row_identity', 'alias', 'conflicting_accession_alias'])
def test_integrity_damage_never_becomes_contained_research(tmp_path, monkeypatch, damage):
    data, _, good, _ = mixed(tmp_path, monkeypatch)
    projected, receipt = isolate(data)
    if damage == 'receipt':
        receipt['excluded_refs'] = []
    elif damage == 'accepted_hash':
        data['edgar']['filing_evidence']['corpus'][good]['submission_sha256'] = '0'*64
    elif damage == 'missing_original':
        data['edgar']['filing_evidence']['acquisition_scope']['originals'].pop()
    elif damage == 'row_identity':
        data['edgar']['filings'][0]['file_date'] = '2026-09-29'
    elif damage == 'conflicting_accession_alias':
        data['edgar']['filings'][0]['accession_number'] = '0001193125-26-999999'
        rescope(data)
    else:
        projected['filing_evidence']['collections'] = deepcopy(data['edgar']['filing_evidence']['collections'])
    with pytest.raises(ValueError, match='invalid_filing_research_isolation'):
        api().validate_filing_research_isolation(data['edgar'], receipt, CONFIG, projected_edgar=projected)


def rescope(data):
    graph = data['edgar']['filing_evidence']
    graph['material_scope'] = build_material_scope(graph, graph['collections'])
    for key in ('scoped_complete', 'scoped_failed_rows', 'quarantined_rows'):
        graph['coverage'][key] = graph['material_scope'][key]


def test_incomplete_discovery_holds_entire_provider_population(tmp_path, monkeypatch):
    data, _, good, _ = mixed(tmp_path, monkeypatch)
    data['edgar']['filing_evidence']['coverage']['discovery_complete'] = False
    rescope(data)
    projected, receipt = isolate(data)
    assert all(not projected[key] for key in ('filings', 'activist_13d', 'passive_13g', 'pqc_filings'))
    assert projected['filing_evidence']['corpus'] == {}
    assert receipt['discovery_scope']['outcome'] == 'provider_hold'
    assert good in receipt['excluded_refs']


def test_missing_discovery_declaration_is_not_vacuously_complete(tmp_path, monkeypatch):
    data, _, _, _ = mixed(tmp_path, monkeypatch)
    graph = data['edgar']['filing_evidence']
    del graph['discovery_coverage']['passive_13g']
    assert graph['coverage']['discovery_complete'] is True
    projected, receipt = isolate(data)
    assert projected['filings'] == []
    assert receipt['discovery_scope']['outcome'] == 'provider_hold'


def test_filing_isolation_never_authorizes_form4_under_generic_edgar_error(tmp_path, monkeypatch):
    data, _, _, _ = mixed(tmp_path, monkeypatch)
    data['edgar']['form4'] = [{'ticker': 'RIGHT', 'unverified': True}]
    projected, _ = isolate(data)
    assert projected.get('form4', []) == []


def test_empty_missing_discovery_scope_cannot_leave_form4_authority(tmp_path, monkeypatch):
    data, _ = captured(tmp_path, monkeypatch, NativeSource(), {})
    del data['edgar']['filing_evidence']['discovery_coverage']['passive_13g']
    data['edgar']['form4'] = [{'ticker': 'RIGHT', 'unverified': True}]
    projected, receipt = isolate(data)
    assert projected.get('form4', []) == []
    assert receipt['discovery_scope']['outcome'] == 'provider_hold'


def test_normal_engine_envelope_without_nested_collection_alias_is_supported(tmp_path, monkeypatch):
    data, _, good, _ = mixed(tmp_path, monkeypatch)
    del data['edgar']['filing_evidence']['collections']
    projected, _ = isolate(data)
    assert [row['adsh'] for row in projected['filings']] == [good]


def test_unrelated_metadata_aliases_never_enter_the_projection(tmp_path, monkeypatch):
    data, _, good, bad = mixed(tmp_path, monkeypatch)
    graph = data['edgar']['filing_evidence']
    graph['history_corpus']['excluded-issuer'] = {'filings': [{'accession': bad, 'text': 'unverified text'}]}
    graph['archive_corpus']['excluded-issuer/archive'] = {'text': 'unverified text'}
    graph['ownership_facts'][bad] = {'text': 'unverified text'}
    projected, _ = isolate(data)
    assert set(projected['filing_evidence']['corpus']) == {good}
    assert projected['filing_evidence']['history_corpus'] == {}
    assert projected['filing_evidence']['archive_corpus'] == {}
    assert projected['filing_evidence']['ownership_facts'] == {}


def test_pqc_aggregate_never_silently_shrinks_but_independent_filing_can_continue(tmp_path, monkeypatch):
    data, _, good, bad = mixed(tmp_path, monkeypatch, pqc=True)
    projected, receipt = isolate(data)
    assert [r['adsh'] for r in projected['filings']] == [good]
    assert projected['pqc_filings'] == []
    assert len(receipt['occurrences']) == 4
    assert receipt['aggregate_scopes']['pqc_filings']['outcome'] == 'held'
    assert receipt['aggregate_scopes']['pqc_filings']['original_refs'] == [good, bad]
    assert any('aggregate_dependency_gap' in row['reasons'] for row in receipt['occurrences'])


def test_failed_predecessor_excludes_comparison_without_hiding_good_current_source(tmp_path, monkeypatch):
    prior = ordinary(101, '10-K', '2025-09-30')
    current = ordinary(102, '10-K', '2026-09-30')
    healthy = ordinary(103, '8-K', '2026-09-30')
    source = NativeSource(extra=((prior[0], b'unseen broken predecessor'), current, healthy))
    data, _ = captured(tmp_path, monkeypatch, source, {'filings': [current[0], healthy[0]]})
    projected, receipt = isolate(data)
    assert [r['adsh'] for r in projected['filings']] == [healthy[0]['adsh']]
    assert current[0]['adsh'] in receipt['excluded_refs']
    assert any(edge['kind'] == 'prior_comparison' and edge['to'] == prior[0]['adsh']
               for edge in receipt['dependency_edges'])


def test_closed_ownership_incorporation_retains_only_its_verified_dependency_graph(tmp_path, monkeypatch):
    current, extra = agreement_case()
    healthy = ordinary(103, '8-K', '2026-09-30')
    source = NativeSource(current, extra=(extra, healthy))
    data, _ = captured(tmp_path, monkeypatch, source, {'activist_13d': [record(current)], 'filings': [healthy[0]]})
    projected, receipt = isolate(data)
    acc, dependency = current[0]['accession'], extra[0]['adsh']
    assert [r['adsh'] for r in projected['activist_13d']] == [acc]
    assert set(projected['filing_evidence']['corpus']) == {acc, dependency, healthy[0]['adsh']}
    assert any(e['from'] == acc and e['to'] == dependency and e['kind'] == 'ownership_source'
               for e in receipt['dependency_edges'])


def analysis_inputs(projected, row, kind):
    from types import SimpleNamespace
    from tradingagents.strategies.orchestration.filing_inputs import filing_analysis_inputs
    metadata = {'full_filing_evidence_policy': 'complete_submission_v1', 'analysis_type': kind,
        'filing_evidence_ref': row['filing_evidence_ref'], 'accession_number': row['adsh']}
    metadata.update({key: row[key] for key in ('prior_evidence_ref', 'comparison_binding') if key in row})
    return filing_analysis_inputs(SimpleNamespace(metadata=metadata, ticker=''), {'edgar': projected}, None)


def test_closed_comparison_still_passes_real_analyzer_input_validation(tmp_path, monkeypatch):
    prior = ordinary(101, '10-K', '2025-09-30')
    current = ordinary(102, '10-K', '2026-09-30')
    data, _ = captured(tmp_path, monkeypatch, NativeSource(extra=(prior, current)), {'filings': [current[0]]})
    projected, receipt = isolate(data)
    row = projected['filings'][0]
    resolved = analysis_inputs(projected, row, 'filing_change')
    assert resolved['prior_evidence']['accession'] == prior[0]['adsh']
    assert receipt['included_refs'] == [prior[0]['adsh'], current[0]['adsh']]


def test_closed_ownership_chain_keeps_negative_membership_history_for_real_input_validation(tmp_path, monkeypatch):
    base = filing(1, '2026-01-01')
    current = filing(2, '2026-09-30', amendment=1)
    data, _ = captured(tmp_path, monkeypatch, NativeSource(base, current), {'activist_13d': [record(current)]})
    projected, _ = isolate(data)
    resolved = analysis_inputs(projected, projected['activist_13d'][0], 'ownership_change')
    receipt = resolved['ownership_bundles'][current[0]['accession']]['receipt']
    assert receipt['status'] == 'complete'
    assert receipt['chain_accessions'] == [base[0]['accession'], current[0]['accession']]
    assert projected['filing_evidence']['history_corpus']


def test_unclosed_ownership_incorporation_excludes_dependent_filing_only(tmp_path, monkeypatch):
    current, extra = agreement_case()
    healthy = ordinary(103, '8-K', '2026-09-30')
    source = NativeSource(current, extra=((extra[0], b'unseen broken agreement'), healthy))
    data, _ = captured(tmp_path, monkeypatch, source, {'activist_13d': [record(current)], 'filings': [healthy[0]]})
    projected, receipt = isolate(data)
    assert projected['activist_13d'] == []
    assert [r['adsh'] for r in projected['filings']] == [healthy[0]['adsh']]
    assert set(projected['filing_evidence']['corpus']) == {healthy[0]['adsh']}
    assert extra[0]['adsh'] in receipt['excluded_refs']


def test_dependency_with_an_independent_record_failure_cannot_reenter_through_owner(tmp_path, monkeypatch):
    from tradingagents.strategies.orchestration.filing_supported_scope import build_supported_dispositions
    current, extra = agreement_case()
    extra[0]['required_exhibits'] = ['agreement.htm']
    source = NativeSource(current, extra=(extra,))
    data, _ = captured(tmp_path, monkeypatch, source,
        {'activist_13d': [record(current)], 'filings': [extra[0]]})
    graph = data['edgar']['filing_evidence']
    failed = data['edgar']['filings'][0]
    failed['filing_failure'] = {'code': 'source_failure', 'reason_code': 'invalid_response'}
    graph['supported_dispositions'] = build_supported_dispositions(graph, graph['collections'])
    failed['filing_material_disposition'] = 'failed'
    graph['coverage'].update(complete=False, failed_rows=1)
    data['edgar']['error'] = 'retained independent record failure'
    rescope(data)
    assert graph['supported_dispositions'][current[0]['accession']]['status'] == 'admitted'
    projected, receipt = isolate(data)
    assert projected['activist_13d'] == [] and projected['filings'] == []
    assert extra[0]['adsh'] in receipt['excluded_refs']


def test_supplied_scopes_cannot_forge_success(tmp_path, monkeypatch):
    data, _, _, _ = mixed(tmp_path, monkeypatch)
    supplied = scopes(data)
    supplied['strict_material_scope']['strict_complete'] = True
    with pytest.raises(ValueError, match='invalid_filing_research_isolation'):
        api().isolate_filing_research(data['edgar'], CONFIG, **supplied)


def test_contradictory_occurrence_alias_is_integrity_failure_even_with_recomputed_scopes(tmp_path, monkeypatch):
    data, _, _, _ = mixed(tmp_path, monkeypatch)
    data['edgar']['filings'][0]['accession_number'] = '0001193125-26-999999'
    rescope(data)
    with pytest.raises(ValueError, match='invalid_filing_research_isolation'):
        isolate(data)


def test_disabled_policy_is_exact_copy_and_opaque_provider_failure_is_a_hold():
    original = {'error': 'provider outage', 'filings': []}
    projected, receipt = api().isolate_filing_research(original, {}, strict_material_scope=None, acquisition_scope=None)
    assert projected == original and projected is not original and receipt is None
    projected, receipt = api().isolate_filing_research(original, CONFIG, strict_material_scope=None, acquisition_scope=None)
    assert projected['filings'] == [] and receipt['discovery_scope']['outcome'] == 'provider_hold'


def test_opaque_provider_failure_retains_partial_discovery_and_duplicate_occurrences_without_authority():
    row = ordinary(101, '8-K', '2026-09-30')[0]
    row['filing_failure'] = {'code': 'source_failure', 'reason_code': 'invalid_response'}
    original = {'error': 'hydration failed before evidence graph publication',
        'filings': [row, deepcopy(row)], 'coverage': {'filings': {'complete': False}}}
    before = deepcopy(original)
    projected, receipt = api().isolate_filing_research(original, CONFIG,
        strict_material_scope=None, acquisition_scope=None)
    assert original == before
    assert all(projected[category] == [] for category in ('filings', 'activist_13d', 'passive_13g', 'pqc_filings'))
    assert 'filing_evidence' not in projected and receipt['included_refs'] == []
    assert receipt['excluded_refs'] == [row['adsh']]
    assert [(item['collection'], item['index']) for item in receipt['occurrences']] == [('filings', 0), ('filings', 1)]
    assert all(item['original_sha256'] is None and item['evidence_sha256'] is None
        and item['recorded_failure'] == row['filing_failure'] and item['outcome'] == 'excluded'
        for item in receipt['occurrences'])
    assert all(item['original'] is None for item in receipt['source_outcomes'])
    assert receipt['original_full_coverage'] is False and receipt['order_authority'] is False
    assert api().validate_filing_research_isolation(original, receipt, CONFIG, projected_edgar=projected) == receipt
    receipt['occurrences'].pop()
    with pytest.raises(ValueError, match='invalid_filing_research_isolation'):
        api().validate_filing_research_isolation(original, receipt, CONFIG, projected_edgar=projected)
