"""Joint source retention keeps genuine prior proof and strict model exclusion."""
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import pytest

from test_filing_hydration import Source, evidence, history_row, hydrate, row
from test_filing_material_integration import material_record
from tradingagents.strategies.data_sources.filing_attribution_policy import POLICY as ATTRIBUTION
from tradingagents.strategies.data_sources.filing_comparison_policy import CURRENT_ONLY_POLICY
from tradingagents.strategies.data_sources.filing_material_policy import POLICY, TEN_POLICY


def joint_source():
    current = row(900, '10-K', ciks=('1', '2', '3', '4'))
    prior = row(901, '10-K', '2025-09-30', ciks=('1', '2', '3', '4'))
    histories = {}
    source = Source([current, prior])
    for cik in ('0000000001', '0000000002', '0000000003', '0000000004'):
        descriptor = {'name': f'CIK{cik}-submissions-001.json', 'filingFrom': '2025-01-01',
                      'filingTo': '2025-12-31', 'filingCount': 1}
        histories[cik] = {'filings': [history_row(current)], 'archives': [descriptor]}
        source.archives[(cik, descriptor['name'])] = [history_row(prior)]
    source.histories = histories
    return current, prior, source


def prepared(policy=POLICY):
    current, prior, source = joint_source()
    material, envelope = material_record(0)
    if policy == TEN_POLICY:
        from filing_material_fixtures import framed, identity
        from tradingagents.strategies.data_sources.filing_material_policy import quarantine_evidence
        frame, raw = framed(0)
        envelope = quarantine_evidence(frame, submission_size=len(raw), policy=policy)
        envelope['source_url'] = identity(0)['source_url']
    source.records[material['adsh']] = material
    source.overrides[material['adsh']] = envelope
    graph = hydrate(source, {'filings': [current, material]}, comparator_policy=CURRENT_ONLY_POLICY,
                    attribution_policy=ATTRIBUTION, material_policy=policy)
    return graph, current, prior, source


@pytest.mark.parametrize('policy', [POLICY, TEN_POLICY])
def test_material_attribution_hydration_retains_joint_prior_without_model_permission(policy):
    from tradingagents.strategies.data_sources.filing_assessment import _validate_evidence, prepare_request
    from tradingagents.strategies.data_sources.filing_attribution_policy import validate_attribution, signal_edgar
    from tradingagents.strategies.orchestration.filing_inputs import filing_analysis_inputs
    graph, current, prior, source = prepared(policy)
    scope = graph['material_scope']
    assert scope['scoped_complete'] is True and scope['strict_complete'] is False
    assert scope['scoped_failed_rows'] == 0 and scope['quarantined_rows'] == 1
    retained = graph['collections']['filings'][0]
    assert retained['prior_status'] == 'available' and retained['prior_evidence_ref'] == prior['adsh']
    assert retained['issuer_binding']['status'] == 'unresolved'
    assert len(source.archive_calls) == 4
    assert set(source.histories) <= set(source.history_calls)
    edgar = {**graph['collections'], 'filing_evidence': graph}
    summary = validate_attribution(edgar, None)
    assert summary['reason_counts']['joint_source_issuers'] == 1
    assert signal_edgar(edgar, summary)['filings'] == []
    corpus = graph['corpus'][current['adsh']]
    with pytest.raises(ValueError, match='invalid_filing_issuer'):
        _validate_evidence(corpus)
    with pytest.raises(ValueError, match='invalid_filing_issuer'):
        prepare_request('filing_change', corpus, prior_evidence=graph['corpus'][prior['adsh']],
                        issuer_binding={'status': 'verified'})
    candidate = SimpleNamespace(ticker='', metadata={'full_filing_evidence_policy': 'complete_submission_v1',
        'analysis_type': 'filing_change', 'filing_evidence_ref': current['adsh'],
        'accession_number': current['adsh'], 'comparison_binding': retained['comparison_binding'],
        'prior_evidence_ref': prior['adsh']})
    with pytest.raises(ValueError, match='invalid_filing_comparator'):
        filing_analysis_inputs(candidate, {'edgar': edgar}, None)


def digest_bound_history(graph, binding):
    proof = {'recent': {cik: graph['history_corpus'][cik] for cik in binding['history_refs']},
             'archives': {ref: graph['archive_corpus'][ref] for ref in binding['archive_refs']}}
    binding['history_snapshot_sha256'] = hashlib.sha256(json.dumps(proof, sort_keys=True,
        separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()).hexdigest()


@pytest.mark.parametrize('damage', ['missing_cik', 'changed_roles', 'duplicate_roles', 'nonnearest',
    'divergent_role', 'digest', 'archive_missing', 'archive_count', 'archive_range', 'archive_cik',
    'history_rows', 'prior_primary', 'current_accession', 'prior_accession', 'current_date',
    'prior_date', 'form', 'issuer_refs', 'history_refs', 'archive_refs', 'extra_binding', 'prior_text'])
def test_material_source_comparison_rechecks_full_proof_even_when_digest_is_rebuilt(damage):
    from tradingagents.strategies.orchestration.filing_material_validation import build_material_scope
    graph, current, prior, _ = prepared()
    current_value, prior_value = graph['corpus'][current['adsh']], graph['corpus'][prior['adsh']]
    binding = graph['collections']['filings'][0]['comparison_binding']
    if damage == 'missing_cik': graph['history_corpus'].pop('0000000004')
    elif damage == 'changed_roles':
        prior_value['roles'].pop(); prior_value['issuer_candidates'].pop()
    elif damage == 'duplicate_roles':
        prior_value['roles'].append(deepcopy(prior_value['roles'][0]))
        prior_value['issuer_candidates'].append(deepcopy(prior_value['roles'][0]))
    elif damage == 'nonnearest':
        for recent in graph['history_corpus'].values():
            recent['filings'].append(history_row(row(902, '10-K', '2026-09-29')))
    elif damage == 'divergent_role':
        graph['history_corpus']['0000000004']['filings'].append(history_row(row(902, '10-K', '2026-09-29')))
    elif damage == 'digest': binding['history_snapshot_sha256'] = '0' * 64
    elif damage == 'archive_missing': graph['archive_corpus'].pop(binding['archive_refs'][0])
    elif damage == 'archive_count': graph['archive_corpus'][binding['archive_refs'][0]]['filings'] = []
    elif damage == 'archive_range': graph['archive_corpus'][binding['archive_refs'][0]]['filings'][0]['filing_date'] = '2024-09-30'
    elif damage == 'archive_cik': graph['archive_corpus'][binding['archive_refs'][0]]['cik'] = '0000000009'
    elif damage == 'history_rows': graph['history_corpus']['0000000004']['filings'][0]['unknown'] = 'invented'
    elif damage == 'prior_primary':
        graph['archive_corpus'][binding['archive_refs'][0]]['filings'][0]['primary_document'] = 'other.htm'
    elif damage in ('current_accession', 'prior_accession'): binding[damage] = row(999)['adsh']
    elif damage in ('current_date', 'prior_date'): binding[damage.replace('_date', '_filing_date')] = '2025-01-01'
    elif damage == 'form': binding['form_type'] = '10-Q'
    elif damage == 'issuer_refs': binding['issuer_ciks'] = binding['issuer_ciks'][:-1]
    elif damage == 'history_refs': binding['history_refs'] = binding['history_refs'][:-1]
    elif damage == 'archive_refs': binding['archive_refs'] = binding['archive_refs'][:-1]
    elif damage == 'extra_binding': binding['permission'] = 'joint model use'
    elif damage == 'prior_text': prior_value['units'][0]['text'] += 'unbound text'
    if damage not in ('digest', 'missing_cik', 'archive_missing'):
        digest_bound_history(graph, binding)
    with pytest.raises(ValueError, match='invalid_filing_material_policy'):
        build_material_scope(graph, graph['collections'])


@pytest.mark.parametrize('failure', ['missing_prior', 'divergent_prior', 'missing_history', 'archive_failure'])
def test_joint_source_permission_never_waives_a_missing_or_divergent_prior(failure):
    from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
    current, prior, source = joint_source()
    if failure == 'missing_prior': source.overrides[prior['adsh']] = SourceFetchError('missing', reason_code='invalid_response')
    elif failure == 'divergent_prior': source.histories['0000000004']['filings'].append(history_row(row(902, '10-K', '2026-09-29')))
    elif failure == 'missing_history': source.histories['0000000004'] = SourceFetchError('missing', reason_code='invalid_response')
    else: source.archives[next(iter(source.archives))] = SourceFetchError('missing', reason_code='invalid_response')
    graph = hydrate(source, {'filings': [current]}, comparator_policy=CURRENT_ONLY_POLICY,
                    attribution_policy=ATTRIBUTION, material_policy=POLICY)
    assert graph['material_scope']['scoped_complete'] is False
    assert graph['material_scope']['scoped_failed_rows'] == 1
    assert graph['collections']['filings'][0]['prior_status'] != 'available'


def test_existing_joint_current_only_source_retention_stays_nonactionable():
    from tradingagents.strategies.data_sources.filing_assessment import _validate_evidence
    from tradingagents.strategies.data_sources.filing_attribution_policy import validate_attribution, signal_edgar
    current, _, source = joint_source()
    for recent in source.histories.values(): recent['archives'] = []
    graph = hydrate(source, {'filings': [current]}, comparator_policy=CURRENT_ONLY_POLICY,
                    attribution_policy=ATTRIBUTION, material_policy=POLICY)
    assert graph['material_scope']['scoped_complete'] is True
    assert graph['collections']['filings'][0]['filing_assessment_scope'] == 'current_only'
    edgar = {**graph['collections'], 'filing_evidence': graph}
    assert signal_edgar(edgar, validate_attribution(edgar, None))['filings'] == []
    with pytest.raises(ValueError, match='invalid_filing_issuer'):
        _validate_evidence(graph['corpus'][current['adsh']])


@pytest.mark.parametrize('primary_document', ['', 'legacy/path/main.htm'])
def test_existing_legacy_history_primary_metadata_does_not_block_complete_source(primary_document):
    current, _, source = joint_source()
    for rows in source.archives.values(): rows[0]['primary_document'] = primary_document
    graph = hydrate(source, {'filings': [current]}, comparator_policy=CURRENT_ONLY_POLICY,
                    attribution_policy=ATTRIBUTION, material_policy=POLICY)
    assert graph['material_scope']['scoped_complete'] is True
    assert graph['collections']['filings'][0]['prior_status'] == 'available'


def test_source_comparator_and_material_scope_require_explicit_attribution():
    from tradingagents.strategies.data_sources.filing_comparison_policy import validate_source_comparison
    from tradingagents.strategies.orchestration.filing_material_validation import build_material_scope
    graph, current, prior, _ = prepared()
    graph['coverage'].pop('attribution_policy')
    binding = graph['collections']['filings'][0]['comparison_binding']
    with pytest.raises(ValueError, match='invalid_filing_comparator'):
        validate_source_comparison(graph, binding, graph['corpus'][current['adsh']], graph['corpus'][prior['adsh']])
    with pytest.raises(ValueError, match='invalid_filing_material_policy'):
        build_material_scope(graph, graph['collections'])
