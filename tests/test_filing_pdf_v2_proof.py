"""PDF v2 attestations remain provisional until exact original recomputation."""
import pytest

from test_filing_evidence import ACCESSION
from test_filing_supported_policy import pdf_source, run, audit, reseal
from tradingagents.strategies.data_sources import filing_supported_policy as policy


def test_v2_native_inventory_reaches_assessment_without_changing_text():
    from tradingagents.strategies.data_sources.filing_assessment import _validate_evidence
    evidence, _raw, _inputs = pdf_source()
    evidence['supported_document_policy'] = policy.POLICY
    assert _validate_evidence(evidence) == '0002065397'
    assert evidence['units'][1]['text'] == 'Hello.'


@pytest.mark.parametrize('damage', ['schema', 'schema_bool', 'mapping_trust', 'text_order', 'visual_claim'])
def test_assessment_requires_exact_v2_native_evidence_contract(damage):
    from tradingagents.strategies.data_sources.filing_assessment import _validate_evidence
    evidence, _raw, _inputs = pdf_source()
    evidence['supported_document_policy'] = policy.POLICY
    receipt = evidence['pdf_evidence']['selected.pdf']
    if damage == 'schema': receipt['schema_version'] = 1
    elif damage == 'schema_bool': receipt['schema_version'] = True
    elif damage == 'mapping_trust': receipt['mapping_trust'] = 'visual_shape_verified'
    elif damage == 'text_order': receipt['text_order'] = 'model_reconstructed'
    else: receipt['visual_meaning_verified'] = True
    with pytest.raises(ValueError, match='invalid_filing_pdf_inventory'):
        _validate_evidence(evidence)


def test_exact_native_text_beside_valid_image_retains_verified_visual_obligation():
    case = pdf_source(image=True)
    receipt = run(case)
    pdf = case[0]['pdf_evidence']['selected.pdf']
    assert pdf['native_text_complete'] is True and pdf['text'] == 'Hello.'
    assert pdf['support_limitations'] == []
    assert receipt['status'] == 'deferred' and receipt['residual_failures'] == []
    assert receipt['order_authority'] is False
    assert receipt['reasons'][0]['code'] == 'unsupported_selected_visual_content'
    assert audit(case, receipt) == receipt


@pytest.mark.parametrize('damage', [
    'font_inventory', 'font_mapping_trust', 'font_hints_claim', 'show_source_hash',
    'show_glyph_mapping', 'show_advance', 'show_omission', 'operation_matrix',
    'geometry_omission',
])
def test_rehashed_pdf_native_inventory_cannot_replace_original_proof(damage):
    case = pdf_source()
    evidence, _raw, inputs = case
    pdf = evidence['pdf_evidence']['selected.pdf']
    from tradingagents.strategies.data_sources.filing_pdf import VERSION
    assert pdf['version'] == VERSION
    page = pdf['pages'][0]
    assert page['fonts'] and page['text_shows'] and page['geometry_operations']
    if damage == 'font_inventory':
        page['fonts'].clear()
    elif damage == 'font_mapping_trust':
        page['fonts'][0]['mapping_trust'] = 'visual_shape_certified'
    elif damage == 'font_hints_claim':
        page['fonts'][0]['hint_programs_executed'] = True
    elif damage == 'show_source_hash':
        page['text_shows'][0]['parts'][0]['raw_sha256'] = '0' * 64
    elif damage == 'show_glyph_mapping':
        page['text_shows'][0]['parts'][0]['glyphs'][0]['unicode'] = 'X'
    elif damage == 'show_advance':
        page['text_shows'][0]['parts'][0]['advance'] += 100
    elif damage == 'show_omission':
        page['text_shows'].clear()
    elif damage == 'operation_matrix':
        page['geometry_operations'][0]['ctm'][4] += 100
    else:
        page['geometry_operations'].clear()
    proof = inputs['source_proofs'][ACCESSION]
    proof['evidence_sha256'] = policy._digest(evidence)
    proof['pdf']['selected.pdf'] = policy._pdf_binding(pdf)
    reseal(proof)
    # A caller can recompute every public digest. That cannot confer authority
    # on changed declarations or omitted show/geometry records.
    provisional = run(case)
    assert provisional['order_authority'] is False
    with pytest.raises(policy.SupportedDispositionError, match='invalid_raw_local_source_proof'):
        audit(case, provisional)


def test_closed_v1_pdf_receipt_is_not_accepted_as_prospective_v2():
    case = pdf_source()
    evidence, _raw, inputs = case
    pdf = evidence['pdf_evidence']['selected.pdf']
    pdf['version'] = 'sec-native-pdf-evidence-v1'
    pdf['schema_version'] = 1
    proof = inputs['source_proofs'][ACCESSION]
    proof['evidence_sha256'] = policy._digest(evidence)
    proof['pdf']['selected.pdf'] = policy._pdf_binding(pdf)
    proof['verifiers']['pdf'] = pdf['version']
    reseal(proof)
    assert run(case)['status'] == 'failed'
