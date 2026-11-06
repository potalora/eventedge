"""Inventory corroboration is validated provenance, not model source material."""
from copy import deepcopy
import hashlib
import json

import pytest

from test_filing_assessment import binding, comparison, module, target
from test_filing_inventory_reconciliation import IDENTITY, metadata_responses, mismatched
from test_filing_evidence import ACCESSION, OBSERVED
from tradingagents.strategies.data_sources import filing_evidence
from tradingagents.strategies.data_sources.filing_inventory_reconciliation import build_reconciliation


def reconciled(form='8-K', *, prior=False):
    accession = '0001193125-25-409112' if prior else ACCESSION
    day = '2025-09-30' if prior else '2026-09-30'
    replacements = [('8-K', form), (ACCESSION, accession),
                    (ACCESSION.replace('-', ''), accession.replace('-', '')),
                    ('20260930', day.replace('-', ''))]
    def changed(value):
        for old, new in replacements:
            value = value.replace(old, new)
        return value
    raw = changed(mismatched().decode()).encode()
    responses = metadata_responses(raw)
    for response in responses.values():
        response['body'] = changed(response['body'])
        response['source_url'] = changed(response['source_url'])
        response['size'] = len(response['body'].encode())
        response['sha256'] = hashlib.sha256(response['body'].encode()).hexdigest()
    identity = {**IDENTITY, 'accession': accession, 'form': form,
                'filing_date': day, 'source_url': changed(IDENTITY['source_url'])}
    proof = build_reconciliation(responses, original_identity=identity,
        original_sha256=hashlib.sha256(raw).hexdigest(), original_size=len(raw), observed_at=OBSERVED)
    framed = filing_evidence.frame_submission(raw, expected_accession=accession,
        expected_form=form, expected_date=day, observed_at=OBSERVED,
        max_submission_bytes=len(raw), inventory_reconciliation=proof)
    item = filing_evidence.build_evidence_from_buffer(filing_evidence.select_primary(framed, raw), raw)
    item['source_url'] = identity['source_url']
    return item


def request(current, prior=None, *, pqc=False, supplied_binding=None):
    corpora = [current] + ([prior] if prior is not None else [])
    return module().prepare_request('quantum_readiness' if pqc else 'filing_change' if prior else 'material_event',
        [current] if pqc else current, prior_evidence=prior,
        issuer_binding=supplied_binding or binding(*corpora),
        target_binding=target() if pqc else None,
        comparison_binding=comparison(current, prior) if prior else None)


@pytest.mark.parametrize('kind', ['current', 'paired', 'pqc'])
def test_reconciliation_is_verified_and_digest_bound_but_absent_from_model_text(kind):
    current = reconciled('10-K' if kind == 'paired' else '8-K')
    prior = reconciled('10-K', prior=True) if kind == 'paired' else None
    corpora = [current] + ([prior] if prior else [])
    originals = deepcopy(corpora)
    prepared = request(current, prior, pqc=kind == 'pqc')
    payload = json.loads(prepared.user)
    rendered = payload['current'] + payload['prior']
    assert len(rendered) == len(corpora)
    for native, shown in zip(corpora, rendered):
        assert 'inventory_reconciliation' not in shown
        assert shown['document_inventory'] == native['document_inventory']
        assert shown['dependencies'] == native['dependencies']
        assert [unit['text'] for unit in shown['units']] == [unit['text'] for unit in native['units']]
        proof_free = {k: v for k, v in native.items() if k != 'inventory_reconciliation'}
        assert payload['issuer_binding']['corpus_sha256'][native['accession']] == module().evidence_digest(native)
        assert module().evidence_digest(native) != module().evidence_digest(proof_free)
    assert '<SEC-HEADER>' not in prepared.user
    assert 'Complete submission text file' not in prepared.user
    assert corpora == originals
    assert prepared.context.source_provenance['corpus_sha256'] == binding(*corpora)['corpus_sha256']


@pytest.mark.parametrize('position', ['current', 'prior', 'pqc'])
def test_forged_raw_metadata_fails_before_prompt_even_with_recomputed_receipt_and_corpus_hashes(position):
    paired = position == 'prior'
    current = reconciled('10-K' if paired else '8-K')
    prior = reconciled('10-K', prior=True) if paired else None
    bad = prior if paired else current
    receipt = bad['inventory_reconciliation']['metadata']['directory']
    receipt['body'] = receipt['body'].replace('main.htm', 'missing.htm')
    receipt['size'] = len(receipt['body'].encode())
    receipt['sha256'] = hashlib.sha256(receipt['body'].encode()).hexdigest()
    with pytest.raises(ValueError, match='invalid_filing_'):
        request(current, prior, pqc=position == 'pqc')


def test_corpus_binding_cannot_be_replaced_with_proof_free_prompt_projection():
    current = reconciled()
    wrong = binding({k: v for k, v in current.items() if k != 'inventory_reconciliation'})
    with pytest.raises(ValueError, match='invalid_filing_binding'):
        request(current, supplied_binding=wrong)
