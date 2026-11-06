"""Structural-only HTML may prove a source disposition, never an LLM input."""
from copy import deepcopy
import pytest

from test_filing_html_structural import v3_source, resource
from tradingagents.strategies.data_sources.filing_assessment import _validate_evidence


@pytest.mark.parametrize('body,graphics', [
    ('<table><tr><td>Material retained solely for locators</td></tr></table>', ()),
    ('<img src="scan.jpg">', (resource(),)),
])
def test_complete_structural_html_only_admitted_for_source_validation(body, graphics):
    evidence, _, _ = v3_source(body, graphics)
    assert _validate_evidence(evidence, allow_unsupported_documents=True) == '0002065397'
    with pytest.raises(ValueError):
        _validate_evidence(evidence)


def test_nonvisual_html_requires_exact_receipt_but_remains_analyzable():
    evidence, _, _ = v3_source('<p>Complete plain narrative.</p>')
    assert _validate_evidence(evidence) == '0002065397'
    for change in ('missing_receipt', 'extra_receipt', 'altered_receipt', 'legacy_policy', 'missing_map'):
        forged = deepcopy(evidence)
        if change == 'missing_receipt':
            forged['html_evidence'].clear()
        elif change == 'extra_receipt':
            forged['html_evidence']['other.htm'] = deepcopy(forged['html_evidence']['main.htm'])
        elif change == 'altered_receipt':
            forged['html_evidence']['main.htm']['source_text_sha256'] = '0' * 64
        elif change == 'legacy_policy':
            forged['supported_document_policy'] = 'verified_supported_documents_v2'
        else:
            forged.pop('html_evidence')
        with pytest.raises(ValueError):
            _validate_evidence(forged, allow_unsupported_documents=True)


def test_structural_image_resources_bind_exact_document_inventory():
    evidence, _, _ = v3_source('<img src="scan.jpg">', (resource(),))
    for key, value in [('body_sha256', '0' * 64), ('type', 'EX-99.1'), ('sequence', 3)]:
        forged = deepcopy(evidence)
        forged['document_inventory'][1][key] = value
        with pytest.raises(ValueError):
            _validate_evidence(forged, allow_unsupported_documents=True)


def test_v3_manifest_and_receipts_bind_current_verifiers_without_rewriting_v2():
    from tradingagents.strategies.data_sources import filing_material_policy as material
    from tradingagents.strategies.data_sources import filing_supported_policy as supported
    from tradingagents.strategies.data_sources import filing_pdf, filing_pdf_visual, filing_html_structural
    from tradingagents.strategies.data_sources.filing_ownership_lineage import POLICY_V2
    scope = material.policy_manifest(material.SUPPORTED_DOCUMENT_POLICY_V3)['supported_scope']
    assert scope['version'] == supported.VERSION_V3
    assert scope['native_pdf_verifier'] == filing_pdf.VERSION
    assert scope['ownership_verifier'] == POLICY_V2
    assert scope['structural_visual_pdf']['version'] == filing_pdf_visual.VERSION
    assert scope['structural_html']['version'] == filing_html_structural.VERSION
    assert supported._verifiers(supported.POLICY_V3)['ownership'] == POLICY_V2
    assert scope['structural_html']['max_html_bytes'] == 24 * 1024**2
    old = material.policy_manifest(material.SUPPORTED_DOCUMENT_POLICY_V2)['supported_scope']
    assert old['version'] == supported.VERSION_V2
    assert old['structural_visual_pdf']['version'] == 'sec-pdf-structural-visual-v1'
    assert 'structural_html' not in old
