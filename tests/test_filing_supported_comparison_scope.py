"""Source comparison closure never grants unsupported comparative analysis."""
from copy import deepcopy

import pytest

import test_filing_ownership_hydration as fixtures
from test_filing_pdf import pdf_bytes
from test_filing_supported_audit import capture_extra
from test_filing_research_isolation import CONFIG as BASE_CONFIG, analysis_inputs
from tradingagents.strategies.data_sources.evidence import CoverageRecords
from tradingagents.strategies.data_sources.filing_assessment import prepare_request
from tradingagents.strategies.data_sources.filing_attribution_policy import POLICY as ATTRIBUTION
from tradingagents.strategies.data_sources.filing_comparison_policy import CURRENT_ONLY_POLICY, validate_source_comparison
from tradingagents.strategies.orchestration.filing_acquisition_validation import validate_filing_acquisition_policy
from tradingagents.strategies.orchestration.filing_material_validation import build_material_scope, validate_filing_material_policy
from tradingagents.strategies.orchestration.filing_research_isolation import isolate_filing_research, validate_filing_research_isolation
from tradingagents.strategies.orchestration.filing_supported_audit import validate_supported_raw_capture


POLICIES = ['verified_supported_documents_v3', 'verified_supported_documents_v4']


def body(number, date, kind='plain'):
    content = '<p>Complete report.</p>'
    if kind == 'html':
        content = '<table><tr><td style="color: red; width: 50%">Complete report.</td></tr></table>'
    if kind in {'pdf', 'broken_pdf', 'pdf_text'}:
        content += '<a href="visual.pdf">Selected exhibit</a>'
    documents = [('10-K', 'main.htm', content)]
    if kind in {'pdf', 'broken_pdf', 'pdf_text'}:
        documents.append(('EX-99', 'visual.pdf', 'PLACEHOLDER'))
    row, raw = fixtures.ordinary(number, '10-K', date, documents=documents)
    if kind in {'pdf', 'broken_pdf', 'pdf_text'}:
        text = b'BT /F1 12 Tf 72 700 Td (Text.) Tj ET' if kind == 'pdf_text' else b''
        pdf = b'%PDF-truncated' if kind == 'broken_pdf' else pdf_bytes((text,), image=True)
        raw = raw.replace(b'PLACEHOLDER', b'<PDF>\n' + pdf + b'</PDF>')
    return row, raw


def pair(tmp_path, monkeypatch, *, policy=POLICIES[-1], current='plain', prior='plain', shared=False, attribution=True):
    current_case = body(801, '2026-09-30', current)
    prior_case = body(800, '2025-09-30', prior)
    monkeypatch.setattr(fixtures, 'POLICY', policy)
    original = fixtures.supported
    def supported(source, collections):
        return original(source, collections, comparator_policy=CURRENT_ONLY_POLICY,
                        **({'attribution_policy': ATTRIBUTION} if attribution else {}))
    monkeypatch.setattr(fixtures, 'supported', supported)
    collections = {'filings': [current_case[0]]}
    if shared:
        collections['pqc_filings'] = [deepcopy(current_case[0])]
    collections = {key: CoverageRecords(collections.get(key, []), coverage={
        'mode': 'exhaustive_window', 'complete': True}) for key in
        ('filings', 'activist_13d', 'passive_13g', 'pqc_filings')}
    source = fixtures.NativeSource(extra=(prior_case, current_case))
    data, paths = capture_extra(tmp_path, monkeypatch, source, collections)
    config = {**BASE_CONFIG, 'filing_material_policy': policy}
    return data, paths, config, current_case[0]['adsh'], prior_case[0]['adsh']


@pytest.mark.parametrize('policy', POLICIES)
@pytest.mark.parametrize('kind', ['html', 'pdf'])
@pytest.mark.parametrize('current_visual,prior_visual', [(False, False), (True, False), (False, True), (True, True)])
def test_supported_comparison_full_chain(tmp_path, monkeypatch, policy, kind, current_visual, prior_visual):
    data, paths, config, current, prior = pair(tmp_path, monkeypatch, policy=policy,
        current=kind if current_visual else 'plain', prior=kind if prior_visual else 'plain')
    before = deepcopy(data)
    graph = data['edgar']['filing_evidence']
    row = data['edgar']['filings'][0]
    assert row['prior_status'] == 'available' and row['prior_evidence_ref'] == prior
    assert row.get('filing_assessment_scope') is None
    assert row['comparison_binding']['prior_accession'] == prior
    assert graph['coverage']['failed_rows'] == int(current_visual or prior_visual)
    assert graph['coverage']['prior_analysis_unavailable_rows'] == int(prior_visual)
    material = validate_filing_material_policy(data, config)
    assert material['strict_failed_rows'] == int(current_visual or prior_visual)
    assert material['scoped_failed_rows'] == int(prior_visual)
    assert material['prior_analysis_unavailable_rows'] == int(prior_visual)
    assert material['admitted_rows'] == int(not current_visual)
    acquisition = validate_filing_acquisition_policy(data, config)
    assert acquisition['completed_originals'] == 2
    audit = validate_supported_raw_capture(data, config, paths, check=lambda: 10)
    assert audit['raw_audit_complete'] and audit['verified_source_proofs'] == 2
    projected, receipt = isolate_filing_research(data['edgar'], config,
        strict_material_scope=material, acquisition_scope=acquisition)
    assert validate_filing_research_isolation(data['edgar'], receipt, config, projected_edgar=projected) == receipt
    assert bool(projected['filings']) is (not current_visual and not prior_visual)
    if current_visual or prior_visual:
        with pytest.raises(ValueError):
            prepare_request('filing_change', **analysis_inputs(data['edgar'], row, 'filing_change'))
    else:
        request = prepare_request('filing_change', **analysis_inputs(projected, projected['filings'][0], 'filing_change'))
        assert current in request.user and prior in request.user
    assert data == before


def test_prior_gap_counts_only_required_ordinary_occurrence(tmp_path, monkeypatch):
    data, paths, config, current, prior = pair(tmp_path, monkeypatch, prior='html', shared=True)
    graph = data['edgar']['filing_evidence']
    assert graph['coverage']['required_rows'] == 2
    assert graph['coverage']['failed_rows'] == 1
    assert graph['coverage']['prior_analysis_unavailable_rows'] == 1
    material = validate_filing_material_policy(data, config)
    assert material['admitted_rows'] == 2 and material['scoped_failed_rows'] == 1
    assert material['by_collection']['filings']['prior_analysis_unavailable_rows'] == 1
    assert material['by_collection']['pqc_filings']['prior_analysis_unavailable_rows'] == 0
    projected, receipt = isolate_filing_research(data['edgar'], config, strict_material_scope=material,
        acquisition_scope=validate_filing_acquisition_policy(data, config))
    assert projected['filings'] == [] and len(projected['pqc_filings']) == 1
    assert set(projected['filing_evidence']['corpus']) == {current}
    assert validate_filing_research_isolation(data['edgar'], receipt, config, projected_edgar=projected) == receipt
    assert validate_supported_raw_capture(data, config, paths, check=lambda: 10)['raw_audit_complete']


@pytest.mark.parametrize('damage', ['missing_proof', 'proof_hash', 'original_hash', 'evidence_hash', 'policy', 'history', 'binding'])
def test_source_comparator_authenticates_prior_before_structural_allowance(tmp_path, monkeypatch, damage):
    data, _, _, current, prior = pair(tmp_path, monkeypatch)
    graph = data['edgar']['filing_evidence']; row = graph['collections']['filings'][0]
    if damage == 'missing_proof':
        del graph['supported_source_proofs'][prior]
    elif damage == 'proof_hash':
        graph['supported_source_proofs'][prior]['source_proof_sha256'] = '0' * 64
    elif damage == 'original_hash':
        graph['supported_originals'][prior]['sha256'] = '0' * 64
    elif damage == 'evidence_hash':
        graph['corpus'][prior]['submission_sha256'] = '0' * 64
    elif damage == 'policy':
        graph['corpus'][prior]['supported_document_policy'] = POLICIES[0]
    elif damage == 'history':
        graph['history_corpus'][fixtures.SUBJECT]['filings'] = []
    else:
        row['comparison_binding']['history_snapshot_sha256'] = '0' * 64
    with pytest.raises(ValueError, match='invalid_filing_comparator'):
        validate_source_comparison(graph, row['comparison_binding'], graph['corpus'][current], graph['corpus'][prior])


@pytest.mark.parametrize('forgery', [0, False, None, '1', 2])
def test_prior_gap_counter_cannot_be_forged(tmp_path, monkeypatch, forgery):
    data, _, config, _, _ = pair(tmp_path, monkeypatch, prior='html')
    graph = data['edgar']['filing_evidence']
    graph['coverage']['prior_analysis_unavailable_rows'] = forgery
    with pytest.raises(ValueError, match='invalid_filing_material_policy'):
        validate_filing_material_policy(data, config)


@pytest.mark.parametrize('policy', POLICIES)
@pytest.mark.parametrize('current', ['plain', 'html'])
def test_incomplete_technical_prior_remains_unavailable(tmp_path, monkeypatch, policy, current):
    data, paths, config, _, prior = pair(tmp_path, monkeypatch, policy=policy,
        current=current, prior='broken_pdf')
    graph = data['edgar']['filing_evidence']; row = data['edgar']['filings'][0]
    assert graph['corpus'][prior]['structural_status'] == 'insufficient'
    assert graph['supported_source_proofs'][prior]['residual_failures']
    assert row['prior_status'] == 'prior_evidence_unavailable'
    assert row.get('prior_evidence_ref') is None and row.get('filing_assessment_scope') is None
    assert graph['coverage']['failed_rows'] == 1
    assert graph['coverage']['prior_analysis_unavailable_rows'] == 0
    material = validate_filing_material_policy(data, config)
    assert material['strict_failed_rows'] == material['scoped_failed_rows'] == 1
    projected, _ = isolate_filing_research(data['edgar'], config, strict_material_scope=material,
        acquisition_scope=validate_filing_acquisition_policy(data, config))
    assert projected['filings'] == []
    assert validate_supported_raw_capture(data, config, paths, check=lambda: 10)['raw_audit_complete']


@pytest.mark.parametrize('current', ['plain', 'pdf_text'])
def test_v1_complete_technical_prior_is_failed_analysis_dependency(tmp_path, monkeypatch, current):
    data, paths, config, _, prior = pair(tmp_path, monkeypatch,
        policy='verified_supported_documents_v1', current=current, prior='html')
    graph = data['edgar']['filing_evidence']; row = data['edgar']['filings'][0]
    assert graph['corpus'][prior]['structural_status'] == 'complete'
    assert graph['supported_source_proofs'][prior]['residual_failures']
    assert row['prior_status'] == 'available' and row['prior_evidence_ref'] == prior
    assert graph['coverage']['failed_rows'] == graph['coverage']['prior_analysis_unavailable_rows'] == 1
    material = validate_filing_material_policy(data, config)
    assert material['strict_failed_rows'] == material['scoped_failed_rows'] == 1
    assert material['prior_analysis_unavailable_rows'] == 1
    assert material['admitted_rows'] == int(current == 'plain')
    projected, _ = isolate_filing_research(data['edgar'], config, strict_material_scope=material,
        acquisition_scope=validate_filing_acquisition_policy(data, config))
    assert projected['filings'] == []
    assert validate_supported_raw_capture(data, config, paths, check=lambda: 10)['raw_audit_complete']
    # The unchanged v1 text-only model validator cannot see external proof
    # residuals; this policy's required boundary is isolation before invocation.
    assert material['original_full_analysis_coverage'] is False
    if current == 'pdf_text':
        with pytest.raises(ValueError):
            prepare_request('filing_change', **analysis_inputs(data['edgar'], row, 'filing_change'))


def test_supported_prior_without_attribution_keeps_single_issuer_comparison(tmp_path, monkeypatch):
    data, _, config, _, prior = pair(tmp_path, monkeypatch, prior='html', attribution=False)
    graph = data['edgar']['filing_evidence']
    assert graph['collections']['filings'][0]['prior_evidence_ref'] == prior
    assert graph['coverage']['prior_analysis_unavailable_rows'] == 1
    assert validate_filing_material_policy(data, config)['scoped_failed_rows'] == 1
