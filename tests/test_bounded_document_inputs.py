"""Bounded retained intake and journal-only admission; no model/provider calls."""
import base64
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib
import json
from pathlib import Path

import pytest

POLICY = 'bounded_document_research_v1'
PDF = b'%PDF-1.4\nretained original\n%%EOF\n'
PNG = b'\x89PNG\r\n\x1a\nretained reviewed page'


def intake():
    name = 'tradingagents.strategies.orchestration.bounded_document_inputs'
    assert importlib.util.find_spec(name), 'bounded intake is missing'
    return importlib.import_module(name)


def strategy():
    name = 'tradingagents.strategies.modules.bounded_document_research'
    assert importlib.util.find_spec(name), 'bounded strategy is missing'
    return importlib.import_module(name).BoundedDocumentResearchStrategy()


@pytest.fixture
def registry(tmp_path):
    original = tmp_path / 'original.pdf'; original.write_bytes(PDF)
    page = tmp_path / 'page.png'; page.write_bytes(PNG)
    original_sha = hashlib.sha256(PDF).hexdigest()
    entry = {'ticker': 'AAPL', 'document_id': 'retained-annual-2025',
        'as_of': '2026-10-12T18:00:00+00:00',
        'original': {'media_type': 'application/pdf', 'filename': 'original.pdf',
                     'sha256': original_sha, 'path': str(original)},
        'original_pages': 2, 'selected_pages': [1],
        'attachments': [{'media_type': 'image/png', 'filename': 'page.png',
            'sha256': hashlib.sha256(PNG).hexdigest(), 'path': str(page),
            'source_sha256': original_sha, 'pages': [1], 'render_identity': 'reviewed-render-v1'}],
        'probes': [{'id': 'historical-heading', 'page': 1, 'question': 'What historical report date is printed?'}],
        'residual_failures': ['original_other_pages_unassessed']}
    return {'bounded_document_research_policy': POLICY, 'bounded_document_registry': [entry]}


def test_disabled_policy_does_no_intake(registry):
    assert intake().configured({}) is False
    assert intake().load_retained_documents({}, '2026-10-12') == {}
    with pytest.raises(ValueError):
        intake().configured({'bounded_document_research_policy': 'wrong'})


def test_intake_preserves_exact_bytes_page_mapping_and_residuals(registry):
    result = intake().load_retained_documents(registry, '2026-10-12')
    assert result['coverage'] == {'complete': True}
    assert result['bounded_document_scope']['population'] == 'configured_retained_document_registry'
    assert result['bounded_document_scope']['accepted_count'] == 1
    row = result['bounded_documents'][0]
    assert set(row) == {'ticker', 'evidence'} and row['ticker'] == 'AAPL'
    evidence = row['evidence']
    assert set(evidence) == {'policy','document_id','as_of','original','original_pages','selected_pages','attachments','probes','residual_failures'}
    assert evidence['policy'] == POLICY
    assert base64.b64decode(evidence['original']['data_base64']) == PDF
    assert base64.b64decode(evidence['attachments'][0]['data_base64']) == PNG
    assert evidence['selected_pages'] == [1] and evidence['attachments'][0]['pages'] == [1]
    assert evidence['residual_failures'] == ['original_other_pages_unassessed','complete_document_unassessed']
    assert 'path' not in json.dumps(result)
    assert registry['bounded_document_registry'][0]['residual_failures'] == ['original_other_pages_unassessed']


@pytest.mark.parametrize('problem', ['missing','corrupt','future','naive','wrong_keys','bad_ticker','wrong_page','bad_probe','render_missing','duplicate_document','symlink','relative','unknown_media','traversal'])
def test_invalid_registry_returns_truthful_empty_records(registry, tmp_path, problem):
    entry = registry['bounded_document_registry'][0]
    if problem == 'missing': Path(entry['original']['path']).unlink()
    if problem == 'corrupt': Path(entry['attachments'][0]['path']).write_bytes(PNG + b'changed')
    if problem == 'future': entry['as_of'] = '2099-01-01T00:00:00+00:00'
    if problem == 'naive': entry['as_of'] = '2026-10-12T18:00:00'
    if problem == 'wrong_keys': entry['url'] = 'https://invalid.example'
    if problem == 'bad_ticker': entry['ticker'] = '../BAD'
    if problem == 'wrong_page': entry['attachments'][0]['pages'] = [2]
    if problem == 'bad_probe': entry['probes'][0]['page'] = 2
    if problem == 'render_missing': entry['attachments'][0]['render_identity'] = ''
    if problem == 'duplicate_document': registry['bounded_document_registry'].append(deepcopy(entry))
    if problem == 'symlink':
        link = tmp_path / 'link.pdf'; link.symlink_to(entry['original']['path']); entry['original']['path'] = str(link)
    if problem == 'relative': entry['original']['path'] = 'relative.pdf'
    if problem == 'unknown_media': entry['original']['media_type'] = 'text/html'
    if problem == 'traversal': entry['attachments'][0]['filename'] = '../page.png'
    result = intake().load_retained_documents(registry, '2026-10-12')
    assert result['bounded_documents'] == [] and result['coverage'] == {'complete': False}
    assert result['error'] == 'bounded_document_intake_invalid'
    assert 'complete_document_unassessed' in result['bounded_document_scope']['residual_failures']


def test_missing_or_oversized_registry_is_not_silently_sliced(registry):
    for rows in (None, [], registry['bounded_document_registry'] * 5):
        config = {**registry, 'bounded_document_registry': rows}
        result = intake().load_retained_documents(config, '2026-10-12')
        assert result['bounded_documents'] == [] and not result['coverage']['complete']


def test_failed_sibling_does_not_certify_partial_registry(registry):
    sibling = deepcopy(registry['bounded_document_registry'][0]); sibling['document_id'] = 'missing'
    sibling['original']['path'] += '.missing'
    registry['bounded_document_registry'].append(sibling)
    result = intake().load_retained_documents(registry, '2026-10-12')
    assert result['bounded_documents'] == [] and result['bounded_document_scope']['accepted_count'] == 0
    assert result['coverage']['complete'] is False


def test_byte_caps_reject_before_encoding(registry, monkeypatch):
    monkeypatch.setattr(intake(), 'MAX_ORIGINAL_BYTES', len(PDF)-1)
    assert intake().load_retained_documents(registry, '2026-10-12')['coverage']['complete'] is False
    monkeypatch.setattr(intake(), 'MAX_ORIGINAL_BYTES', len(PDF))
    monkeypatch.setattr(intake(), 'MAX_ATTACHMENT_BYTES', len(PNG)-1)
    assert intake().load_retained_documents(registry, '2026-10-12')['coverage']['complete'] is False
    monkeypatch.setattr(intake(), 'MAX_ATTACHMENT_BYTES', len(PNG))
    monkeypatch.setattr(intake(), 'MAX_TOTAL_ATTACHMENT_BYTES', len(PNG)-1)
    assert intake().load_retained_documents(registry, '2026-10-12')['coverage']['complete'] is False


def test_old_retained_acquisition_is_not_relabelled_current(registry):
    registry['bounded_document_registry'][0]['as_of'] = '2025-01-01T10:00:00+00:00'
    result = intake().load_retained_documents(registry, '2026-10-12')
    assert result['coverage']['complete'] is True
    assert result['bounded_documents'][0]['evidence']['as_of'] == '2025-01-01T10:00:00+00:00'


def test_strategy_keeps_journal_only_evidence_and_admission_manifest(registry):
    envelope = intake().load_retained_documents(registry, '2026-10-12')
    owner = strategy()
    assert set(owner.data_sources) == {'bounded_documents'} and owner.name == 'bounded_document_research'
    candidates = owner.screen({'bounded_documents': envelope}, '2026-10-12', owner.get_default_params())
    assert len(candidates) == 1 and candidates[0].journal_only is True
    candidate = candidates[0]
    assert candidate.metadata['analysis_type'] == 'bounded_document_research'
    assert candidate.metadata['bounded_document_evidence'] == envelope['bounded_documents'][0]['evidence']
    assert candidate.metadata['observed_at'] == '2026-10-12T18:00:00+00:00'
    assert 'non_actionable_reason' not in candidate.metadata
    assert candidate.event_key.startswith('bounddoc_')
    assert candidates.admission_manifest['discovered'][0]['journal_only'] is True
    candidate.metadata['bounded_document_evidence']['selected_pages'].append(2)
    assert envelope['bounded_documents'][0]['evidence']['selected_pages'] == [1]


def test_zero_budget_preserves_discovery_and_failed_intake_makes_no_candidate(registry):
    owner = strategy()
    envelope = intake().load_retained_documents(registry, '2026-10-12')
    candidates = owner.screen({'bounded_documents': envelope}, '2026-10-12', {'analysis_budget': 0})
    assert candidates == [] and len(candidates.admission_manifest['excluded']) == 1
    broken = {**envelope, 'error': 'bounded_document_intake_invalid', 'coverage': {'complete': False}}
    assert owner.screen({'bounded_documents': broken}, '2026-10-12', {}) == []


def test_encoded_envelope_limit_is_explicit_and_all_or_none(registry, monkeypatch):
    monkeypatch.setattr(intake(), 'MAX_ENCODED_EVIDENCE_BYTES', 128, raising=False)
    result = intake().load_retained_documents(registry, '2026-10-12')
    assert result['coverage'] == {'complete': False} and result['bounded_documents'] == []
    assert 'bounded_document_envelope_limit' in result['bounded_document_scope']['residual_failures']
    assert result['bounded_document_scope']['accepted_count'] == 0


@pytest.mark.parametrize('params', [{'analysis_budget':5}, {'max_positions':5}, {'analysis_budget':True}])
def test_strategy_cannot_expand_admission_above_four(params):
    with pytest.raises(ValueError):
        strategy().screen({}, '2026-10-12', params)
