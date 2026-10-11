"""Exact-three framing permission must never become an analysis permission."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest
from filing_material_fixtures import ROWS, framed, identity

PATH = Path(__file__).parents[1] / 'tradingagents/strategies/data_sources/filing_material_policy.py'

def policy():
    assert PATH.exists(), 'pure material quarantine policy is missing'
    spec = importlib.util.spec_from_file_location('_test_material_policy', PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def envelope(index=0):
    frame, raw = framed(index)
    return policy().quarantine_evidence(frame, submission_size=len(raw)), raw


def test_pure_module_and_canonical_fresh_manifest():
    module = policy()
    manifest = module.policy_manifest()
    assert manifest['policy'] == module.POLICY == 'retained_three_material_gaps_v1'
    assert len(manifest['identities']) == 3
    encoded = json.dumps(manifest, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode('ascii')
    assert module.policy_manifest_sha256() == hashlib.sha256(encoded).hexdigest()
    manifest['identities'][0]['gap_codes'].clear()
    assert module.policy_manifest()['identities'][0]['gap_codes']
    assert not any(k.startswith('_test_material_policy.') for k in sys.modules)


@pytest.mark.parametrize('index', range(3))
def test_complete_inventory_exact_identity_and_unselected_primaries(index):
    module = policy()
    result, raw = envelope(index)
    assert result['structural_status'] == result['analysis_adequacy'] == 'insufficient'
    assert result['structural_scope'] == 'complete_original_inventory_unselected'
    assert result['dependency_assessment'] == 'not_assessed'
    assert result['units'] == result['dependencies'] == []
    assert len(result['document_inventory']) == ROWS[index][6]
    declaration = result['material_quarantine']
    assert declaration['submission_size'] == len(raw)
    assert declaration['document_count'] == ROWS[index][6]
    assert [(d['sequence'], d['filename']) for d in declaration['primary_candidates']] == ROWS[index][-1]
    assert result['issues'] == [{'code': code} for code in declaration['gap_codes']]
    assert module.validate_quarantined_evidence(result) == module.approved_identity(identity(index))
    assert result['submission_sha256'] == hashlib.sha256(raw).hexdigest()
    declaration['gap_codes'].clear()
    assert module.approved_identity(identity(index))['gap_codes']


@pytest.mark.parametrize('field', ['form', 'filing_date', 'source_url'])
def test_known_accession_wrong_identity_fails(field):
    row = identity(0)
    row[field] += 'wrong'
    with pytest.raises(ValueError): policy().approved_identity(row)


def test_fourth_accession_never_quarantined():
    assert policy().approved_identity(dict(identity(0), accession='0001193125-26-999999')) is None
    frame, raw = framed(0)
    frame['accession'] = '0001193125-26-999999'
    with pytest.raises(ValueError): policy().quarantine_evidence(frame, submission_size=len(raw))


@pytest.mark.parametrize('field,value', [
    ('filing_evidence_policy', 'wrong'), ('filing_acquisition_policy', 'wrong'),
    ('filing_parser_policy', 'wrong'), ('filing_material_policy', 'wrong')])
def test_config_pairing_and_unknown_policy_fail(field, value):
    config = dict(filing_material_policy='retained_three_material_gaps_v1',
        filing_evidence_policy='complete_submission_v1',
        filing_acquisition_policy='bounded_original_submission_v1', filing_parser_policy='two_processes_v1')
    assert policy().configured(config) is True
    config[field] = value
    with pytest.raises(ValueError): policy().configured(config)
    assert policy().configured({}) is False


@pytest.mark.parametrize('change', [
    lambda e: e.update(form='8-K'),
    lambda e: e.update(filing_date='2026-09-24'),
    lambda e: e.update(source_url=e['source_url'] + '?wrong'),
    lambda e: e['roles'][0].update(cik='0000000001'),
    lambda e: e['roles'].append(copy.deepcopy(e['roles'][0])),
    lambda e: e['issuer_candidates'][0].update(name='wrong'),
    lambda e: e.update(accepted_at=None),
    lambda e: e.update(header_sha256='not-a-hash'),
    lambda e: e.update(submission_sha256='g'*64),
    lambda e: e['document_inventory'].pop(),
    lambda e: e['document_inventory'][0].update(body_end=10**10),
    lambda e: e['document_inventory'][1].update(body_start=1),
    lambda e: e['document_inventory'][0].update(body_start=e['document_inventory'][0]['body_end']),
    lambda e: e['document_inventory'][0].update(body_sha256='bad'),
    lambda e: e['document_inventory'][0].update(filename='wrong.htm'),
    lambda e: e['material_quarantine']['primary_candidates'].clear(),
    lambda e: e['material_quarantine'].update(submission_size=True),
    lambda e: e['material_quarantine'].update(policy_manifest_sha256='0'*64),
    lambda e: e['material_quarantine']['gap_codes'].append('missing_body'),
    lambda e: e['issues'].append({'code': 'arbitrary_parse_failure'}),
    lambda e: e['units'].append({'text': 'analysis leak'}),
    lambda e: e['dependencies'].append({'filename': 'other'}),
    lambda e: e.update(analysis_adequacy='not_assessed'),
])
def test_changed_evidence_or_declaration_rejected(change):
    result, _ = envelope()
    change(result)
    with pytest.raises(ValueError): policy().validate_quarantined_evidence(result)


def test_required_exhibits_must_exist_unambiguously_before_quarantine():
    frame, raw = framed(0)
    module = policy()
    with pytest.raises(ValueError): module.quarantine_evidence(frame, submission_size=len(raw), required_exhibits=['missing.htm'])
    with pytest.raises(ValueError): module.quarantine_evidence(frame, submission_size=len(raw), required_exhibits=['GRAPHIC'])
    result = module.quarantine_evidence(frame, submission_size=len(raw), required_exhibits=['fixture-2.jpg'])
    assert result['material_quarantine']['required_exhibits'] == ['fixture-2.jpg']
    module.validate_quarantined_evidence(result)


def test_campbell_ambiguity_remains_an_explicit_material_issue():
    result, _ = envelope(1)
    assert {'code': 'ambiguous_primary_document'} in result['issues']
    assert 'ambiguous_primary_document' in result['material_quarantine']['gap_codes']


def test_inventory_sequence_reordering_rejected_even_after_rebinding_digest():
    result, _ = envelope()
    result['document_inventory'][1]['sequence'], result['document_inventory'][2]['sequence'] = 3, 2
    result['material_quarantine']['document_inventory_sha256'] = hashlib.sha256(json.dumps(
        result['document_inventory'], sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()
    with pytest.raises(ValueError): policy().validate_quarantined_evidence(result)


def test_direct_pure_load_does_not_initialize_package_or_providers():
    import subprocess
    script = '''import builtins, importlib.util, sys
native_import = builtins.__import__
def guarded(name, *args, **kwargs):
    assert name.split('.')[0] not in {'tradingagents', 'bs4', 'openbb', 'requests', 'openai'}
    return native_import(name, *args, **kwargs)
builtins.__import__ = guarded
spec = importlib.util.spec_from_file_location('_isolated_material', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert len(module.policy_manifest()['identities']) == 3
assert 'tradingagents' not in sys.modules
'''
    subprocess.run([sys.executable, '-I', '-B', '-c', script, str(PATH)], check=True, capture_output=True)


def test_declaration_count_type_is_exact_integer():
    result, _ = envelope()
    result['material_quarantine']['document_count'] = 310.0
    with pytest.raises(ValueError): policy().validate_quarantined_evidence(result)
