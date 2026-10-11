"""Independent adversarial checks of the v2 application boundary."""
import pytest

from test_filing_evidence import ACCESSION
from test_filing_structural_policy_v2 import case, acquired_case
from tradingagents.strategies.data_sources import filing_supported_policy as policy


@pytest.mark.parametrize('code', [
    'source_deadline', 'model_deadline', 'spool_cleanup_failure',
    'missing_agreement', 'invalid_filing_citations', 'evidence_byte_limit',
])
def test_structural_reason_never_overrides_independent_failure(code):
    evidence, _, inputs = case()
    result = policy.evaluate_supported_disposition(
        evidence, **inputs, residual_failures=[{'code': code}])
    assert result['status'] == 'failed'
    assert result['reasons'][0]['proof_kind'] == 'structural_visual_pdf'
    assert any(row.get('failure', {}).get('code') == code
        for row in result['residual_failures'])


def test_resealed_schema_valid_structural_forgery_requires_exact_raw_audit():
    evidence, raw, inputs = case()
    pdf = evidence['pdf_evidence']['selected.pdf']
    # A well-shaped hash is an assertion, not proof of the decoded original.
    pdf['decoded_sha256'] = '0' * 64
    proof = inputs['source_proofs'][ACCESSION]
    proof['pdf']['selected.pdf'] = policy._pdf_binding(pdf)
    proof['evidence_sha256'] = policy._digest(evidence)
    proof['source_proof_sha256'] = policy._digest(
        {key: value for key, value in proof.items() if key != 'source_proof_sha256'})
    provisional = policy.evaluate_supported_disposition(evidence, **inputs)
    assert provisional['status'] == 'deferred'
    assert provisional['order_authority'] is False
    with pytest.raises(ValueError, match='invalid_raw_local_source_proof'):
        policy.validate_supported_disposition(provisional, evidence,
            **inputs, raw_submissions={ACCESSION: raw})


def test_visual_capture_still_requires_every_original(tmp_path, monkeypatch):
    from tradingagents.strategies.orchestration.filing_supported_audit import validate_supported_raw_capture
    data, config, _ = acquired_case(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match='invalid_supported_raw_capture'):
        validate_supported_raw_capture(data, config, {}, check=lambda: 10)


def test_structural_success_without_visuals_cannot_replace_native_failure(monkeypatch):
    from tradingagents.strategies.data_sources import filing_pdf, filing_pdf_visual
    from tradingagents.strategies.data_sources.filing_evidence import (
        EvidenceError, extract_supported_pdf_evidence,
    )
    monkeypatch.setattr(filing_pdf_visual, 'extract_pdf_visual_evidence',
        lambda *args, **kwargs: {'visual_presence': False})
    def native_failure(*args, **kwargs):
        raise filing_pdf.FilingPDFError('native_test_failure')
    monkeypatch.setattr(filing_pdf, 'extract_pdf_evidence', native_failure)
    with pytest.raises(EvidenceError, match='native_test_failure'):
        extract_supported_pdf_evidence(b'fixture', policy=policy.POLICY_V2,
            max_document_bytes=1024, max_total_text_bytes=1024)
