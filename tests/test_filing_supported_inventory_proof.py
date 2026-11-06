"""Reconciled originals keep their exact proof through supported raw validation."""
from copy import deepcopy
import hashlib
import time

import pytest

from test_filing_evidence import ACCESSION, OBSERVED, submission
from test_filing_inventory_dispatch import arguments, reconciliation
from test_filing_inventory_reconciliation import ARGUMENTS, IDENTITY, proof_for
from test_filing_spool import Response
from tradingagents.strategies.data_sources import filing_evidence as evidence
from tradingagents.strategies.data_sources import filing_parser_dispatch as dispatcher
from tradingagents.strategies.data_sources import filing_supported_policy as supported
from tradingagents.strategies.data_sources.filing_spool import submission_spool_scope, spool_response
from tradingagents.strategies.data_sources.request_policy import provider_budget


POLICIES = ['verified_supported_documents_v' + str(i) for i in range(1, 5)]


def complete_original():
    raw = submission([('8-K', 'main.htm',
        '<html><head></head><body><p>Complete narrative.</p></body></html>')])
    return raw.replace(b'<PUBLIC-DOCUMENT-COUNT>1', b'<PUBLIC-DOCUMENT-COUNT>2')


def retained_evidence(policy):
    raw = complete_original()
    framed = evidence.frame_submission(raw, **ARGUMENTS, max_submission_bytes=len(raw),
        inventory_reconciliation=proof_for(raw))
    current = evidence.build_evidence_from_buffer(evidence.select_primary(framed, raw), raw,
        supported_document_policy=policy)
    current['source_url'] = IDENTITY['source_url']
    original = dict(identity=deepcopy(IDENTITY), observed_at=OBSERVED, size=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(), complete=True)
    return raw, current, original


def validate_admitted(raw, current, proof):
    assert current['structural_status'] == 'complete' and current['issues'] == []
    assert proof['residual_failures'] == [] and proof['reasons'] == []
    args = dict(source_proofs={ACCESSION: proof}, originals={ACCESSION: proof['original']},
        occurrences=[{'collection': 'filings', 'index': 0}])
    receipt = supported.evaluate_supported_disposition(current, **args)
    assert receipt['status'] == 'admitted'
    assert supported.validate_supported_disposition(receipt, current,
        raw_submissions={ACCESSION: raw}, **args) == receipt
    return receipt


@pytest.mark.parametrize('policy', POLICIES)
def test_reconciled_complete_source_proof_and_raw_disposition(policy):
    raw, current, original = retained_evidence(policy)
    before = deepcopy(current)
    proof = supported.build_local_source_proof(current, original=original, raw_submission=raw)
    validate_admitted(raw, current, proof)
    assert current == before
    assert current['inventory_reconciliation']['declared_document_count'] == 2
    assert current['inventory_reconciliation']['actual_document_count'] == 1
    if policy in POLICIES[2:]:
        assert set(proof['html']) == set(current['html_evidence']) == {'main.htm'}


@pytest.mark.parametrize('policy', POLICIES)
def test_real_parser_reconciles_same_original_and_retains_supported_proof(tmp_path, policy):
    raw = complete_original()
    deadline = time.monotonic() + 15
    with provider_budget('edgar', deadline, limits=()):
        with submission_spool_scope(tmp_path, original_deadline=deadline) as spool:
            record = spool_response(Response((raw,)), identity=IDENTITY)
            with dispatcher.parser_scope() as owner:
                children = list(owner.children)
                args = {**arguments(record), 'material_policy': policy}
                with pytest.raises(dispatcher.SubmissionDocumentCountMismatch):
                    dispatcher.dispatch_evidence(record, **args)
                assert not owner._child_leases and spool.stats()['active_child_copies'] == 0
                current = dispatcher.dispatch_evidence(record, **args,
                    inventory_reconciliation=reconciliation(record, raw))
                proof = current.pop('supported_source_proof')
                validate_admitted(raw, current, proof)
                assert current['submission_sha256'] == record.sha256 == hashlib.sha256(raw).hexdigest()
                assert owner.children == children and not owner._closed
                assert not owner._child_leases and spool.stats()['active_child_copies'] == 0
            assert all(child.poll() is not None for child in children)
    assert spool.stats()['closed'] is True


@pytest.mark.parametrize('policy', POLICIES[2:])
@pytest.mark.parametrize('damage', ['header', 'declared', 'actual', 'original', 'metadata',
    'missing_bound', 'extra_field', 'null'])
def test_bound_reconciliation_is_validated_before_projection(policy, damage):
    raw, current, original = retained_evidence(policy)
    proof = current['inventory_reconciliation']
    if damage == 'header':
        proof['native_header_sha256'] = '0' * 64
    elif damage == 'declared':
        proof['declared_document_count'] = 3
    elif damage == 'actual':
        proof['actual_document_count'] = 2
    elif damage == 'original':
        proof['original']['sha256'] = '0' * 64
    elif damage == 'metadata':
        proof['metadata']['directory']['sha256'] = '0' * 64
    elif damage == 'missing_bound':
        del proof['native_header_sha256']
    elif damage == 'extra_field':
        proof['invented_authority'] = True
    else:
        current['inventory_reconciliation'] = None
    with pytest.raises(ValueError):
        supported._source(current, original, raw, (), None)
