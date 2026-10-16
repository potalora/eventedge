"""Pilot evidence cannot turn sampled model output into whole-document authority."""
import copy
import hashlib
import json

import pytest


def spec():
    return {'policy_version': 'luna-page-pilot-v1', 'source_id': 'retained-fixture',
        'source_sha256': 'a' * 64, 'original_pages': 3, 'source_failure': None,
        'pages': [{'page': 2, 'render_sha256': 'b' * 64,
            'required_text': ['Revenue (USD millions)'],
            'probes': [{'id': 'revenue_current', 'question': 'Revenue in the Current column',
                'support': 'Revenue 1,204.50 999.00',
                'expected': {'label': 'Revenue', 'value': '1,204.50',
                             'unit': 'USD millions', 'column': 'Current'}}]}]}


def response():
    return {'source_sha256': 'a' * 64, 'pages': [{'page': 2, 'status': 'readable',
        'transcription': 'Revenue (USD millions)\nCurrent Prior\nRevenue 1,204.50 999.00',
        'probes': [{'id': 'revenue_current', 'label': 'Revenue', 'value': '1,204.50',
            'unit': 'USD millions', 'column': 'Current', 'citation': 'Revenue 1,204.50 999.00'}],
        'uncertainties': []}]}


def validate(value, manifest=None):
    from tradingagents.strategies.luna_document_pilot import validate_extraction
    return validate_extraction(json.dumps(value), manifest or spec())


def test_exact_sample_pass_preserves_unverified_whole_document_and_transcription():
    receipt = validate(response())
    assert receipt['status'] == 'passed'
    assert receipt['verified_pages'] == [2]
    assert receipt['verified_probes'] == 1
    assert receipt['unassessed_pages'] == [1, 3]
    assert receipt['full_document_quality_accepted'] is False
    assert receipt['transcription_completeness_verified'] is False
    assert receipt['trading_authority'] is False


@pytest.mark.parametrize('field,wrong', [('value', '1,204.05'), ('label', 'Net income'),
    ('unit', 'USD thousands'), ('column', 'Prior')])
def test_wrong_number_unit_row_or_column_fails_independent_probe(field, wrong):
    value = response()
    value['pages'][0]['probes'][0][field] = wrong
    assert validate(value)['status'] == 'failed'


@pytest.mark.parametrize('mutation', ['missing', 'duplicate', 'wrong_page', 'unknown_probe',
    'missing_probe', 'false_source', 'invented_citation', 'missing_text'])
def test_coverage_identity_and_citation_breaks_are_not_accepted(mutation):
    value = response()
    if mutation == 'missing': value['pages'] = []
    elif mutation == 'duplicate': value['pages'].append(copy.deepcopy(value['pages'][0]))
    elif mutation == 'wrong_page': value['pages'][0]['page'] = 1
    elif mutation == 'unknown_probe': value['pages'][0]['probes'][0]['id'] = 'unknown'
    elif mutation == 'missing_probe': value['pages'][0]['probes'] = []
    elif mutation == 'false_source': value['source_sha256'] = 'c' * 64
    elif mutation == 'invented_citation': value['pages'][0]['probes'][0]['citation'] = 'Revenue 1,204.50 0.00'
    elif mutation == 'missing_text': value['pages'][0]['transcription'] = 'Revenue 1,204.50 999.00'
    assert validate(value)['status'] == 'failed'


@pytest.mark.parametrize('status,uncertainties', [('uncertain', []), ('unreadable', []),
    ('readable', ['The column heading is unclear'])])
def test_model_uncertainty_remains_unresolved(status, uncertainties):
    value = response()
    value['pages'][0].update(status=status, uncertainties=uncertainties)
    assert validate(value)['status'] == 'unresolved'


def test_readable_output_does_not_repair_known_malformed_original():
    manifest = spec()
    manifest['source_failure'] = 'pdf_invalid_embedded_font'
    receipt = validate(response(), manifest)
    assert receipt['status'] == 'failed'
    assert 'source_failure' in receipt['reasons']


def test_prompt_never_discloses_gold_values_or_claims_whole_document_coverage():
    from tradingagents.strategies.luna_document_pilot import extraction_prompt
    prompt = extraction_prompt(spec())
    assert '1,204.50' not in prompt
    assert 'Revenue in the Current column' in prompt
    assert 'selected pages' in prompt
    assert 'all 3 pages' not in prompt


@pytest.mark.parametrize('text', ['{"source_sha256":', '{"pages": [], "pages": []}', 'NaN', '[' * 1500 + '0' + ']' * 1500], ids=['truncated', 'duplicate_keys', 'nonfinite', 'nested'])
def test_malformed_duplicate_keys_and_nonfinite_json_are_failed(text):
    from tradingagents.strategies.luna_document_pilot import validate_extraction
    assert validate_extraction(text, spec())['status'] == 'failed'


def test_replay_recomputes_result_without_model_or_provider_and_rejects_altered_bytes(tmp_path):
    from tradingagents.strategies.luna_document_pilot import replay_capture
    files = {'spec.json': json.dumps(spec()).encode(), 'response.json': json.dumps(response()).encode(),
             'original.pdf': b'%PDF-fixture', 'page-2.png': b'\x89PNG\r\n\x1a\nfixture'}
    manifest = spec()
    manifest['source_sha256'] = hashlib.sha256(files['original.pdf']).hexdigest()
    manifest['pages'][0]['render_sha256'] = hashlib.sha256(files['page-2.png']).hexdigest()
    output = response()
    output['source_sha256'] = manifest['source_sha256']
    files.update({'spec.json': json.dumps(manifest).encode(), 'response.json': json.dumps(output).encode()})
    capture = {'version': 'luna-page-capture-v1', 'spec': 'spec.json', 'response': 'response.json',
        'original': 'original.pdf', 'renders': {'2': 'page-2.png'},
        'files': {name: hashlib.sha256(body).hexdigest() for name, body in files.items()}}
    for name, body in files.items(): (tmp_path / name).write_bytes(body)
    raw_capture = json.dumps(capture).encode()
    (tmp_path / 'capture.json').write_bytes(raw_capture)
    anchor = hashlib.sha256(raw_capture).hexdigest()
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert replay_capture(tmp_path, anchor)['status'] == 'passed'
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    (tmp_path / 'original.pdf').write_bytes(b'%PDF-altered')
    with pytest.raises(ValueError, match='evidence_hash_mismatch'):
        replay_capture(tmp_path, anchor)


def test_replay_requires_external_capture_hash_and_rejects_path_escape(tmp_path):
    from tradingagents.strategies.luna_document_pilot import replay_capture
    raw = json.dumps({'version': 'luna-page-capture-v1', 'files': {'../outside': 'a' * 64}}).encode()
    (tmp_path / 'capture.json').write_bytes(raw)
    with pytest.raises(ValueError, match='capture_hash_mismatch'):
        replay_capture(tmp_path, 'b' * 64)
    with pytest.raises(ValueError, match='evidence_path_invalid'):
        replay_capture(tmp_path, hashlib.sha256(raw).hexdigest())


def test_blank_template_cell_is_null_without_invented_number_or_uncertainty():
    manifest = spec()
    page = manifest['pages'][0]
    page['no_numeric_values'] = True
    page['required_text'] = ['Revenue (USD millions)', 'Current Prior']
    page['probes'][0]['expected']['value'] = None
    page['probes'][0]['support'] = 'Revenue (USD millions) Current Prior'
    output = response()
    output['pages'][0]['transcription'] = 'Revenue (USD millions)\nCurrent Prior'
    output['pages'][0]['probes'][0].update(value=None, citation='Revenue (USD millions) Current Prior')
    assert validate(output, manifest)['status'] == 'passed'
    output['pages'][0]['probes'][0]['value'] = '0'
    assert validate(output, manifest)['status'] == 'failed'


def test_blank_page_cannot_hide_hallucinated_values_elsewhere_in_transcription():
    manifest = spec()
    page = manifest['pages'][0]
    page['no_numeric_values'] = True
    page['probes'][0]['expected']['value'] = None
    page['probes'][0]['support'] = 'Revenue (USD millions)'
    output = response()
    output['pages'][0]['probes'][0].update(value=None, citation='Revenue (USD millions)')
    assert validate(output, manifest)['status'] == 'failed'


@pytest.mark.parametrize('corrupted', ['Revenue 11,204.50 999.00', 'Expenses 1,204.50 999.00'])
def test_numeric_substring_or_wrong_row_citation_cannot_pass(corrupted):
    output = response()
    output['pages'][0]['transcription'] = 'Revenue (USD millions)\nCurrent Prior\n' + corrupted
    output['pages'][0]['probes'][0]['citation'] = corrupted
    assert validate(output)['status'] == 'failed'


def test_uncertainty_does_not_bypass_schema_or_probe_coverage():
    output = response()
    output['pages'][0].update(status='uncertain', probes=[{'totally_wrong': True}])
    receipt = validate(output)
    assert receipt['status'] == 'failed'
    assert 'invalid_response' in receipt['reasons']


def test_support_number_cannot_be_prefix_of_wrong_row_value_with_unrelated_correct_value():
    manifest = spec()
    probe = manifest['pages'][0]['probes'][0]
    probe['support'] = 'Revenue 1,204.50'
    output = response()
    text = 'Revenue (USD millions) Current Prior Revenue 1,204.50,000 Unrelated number: 1,204.50'
    output['pages'][0]['transcription'] = text
    output['pages'][0]['probes'][0]['citation'] = text
    assert validate(output, manifest)['status'] == 'failed'


def test_extraction_replay_preserves_native_deadline_rejection_even_with_readable_raw_text(tmp_path):
    from tradingagents.strategies.luna_document_pilot import replay_capture
    original = b'%PDF-fixture'
    rendered = b'\x89PNG\r\n\x1a\nfixture'
    manifest = spec()
    manifest['source_sha256'] = hashlib.sha256(original).hexdigest()
    manifest['pages'][0]['render_sha256'] = hashlib.sha256(rendered).hexdigest()
    output = response()
    output['source_sha256'] = manifest['source_sha256']
    files = {'original.pdf': original, 'page.png': rendered,
        'spec.json': json.dumps(manifest).encode(), 'raw.txt': json.dumps(output).encode(),
        'model-result.json': json.dumps({'error_class': 'ModelDeadlineExceeded',
                                         'provenance': {}}).encode()}
    for name, body in files.items(): (tmp_path / name).write_bytes(body)
    capture = {'version': 'luna-page-capture-v1', 'spec': 'spec.json', 'response': 'raw.txt',
        'original': 'original.pdf', 'renders': {'2': 'page.png'}, 'provenance': 'model-result.json',
        'files': {name: hashlib.sha256(body).hexdigest() for name, body in files.items()}}
    body = json.dumps(capture).encode()
    (tmp_path / 'capture.json').write_bytes(body)
    receipt = replay_capture(tmp_path, hashlib.sha256(body).hexdigest())
    assert receipt['status'] == 'failed'
    assert 'transport_not_accepted' in receipt['reasons']
    assert receipt['verified_pages'] == []
    assert receipt['verified_probes'] == 0


def test_revised_prompt_policy_preserves_legacy_receipts_and_has_explicit_new_identity():
    manifest = spec()
    manifest['policy_version'] = 'luna-page-pilot-v2'
    receipt = validate(response(), manifest)
    assert receipt['status'] == 'passed'
    assert receipt['policy_version'] == 'luna-page-pilot-v2'
    assert validate(response())['policy_version'] == 'luna-page-pilot-v1'
