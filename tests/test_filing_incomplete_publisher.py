"""Incomplete selected ownership evidence stays negative without poisoning IPC."""
from copy import deepcopy
import hashlib
import time

import pytest

from test_filing_evidence import ACCESSION, OBSERVED, role, submission
from test_filing_ownership_lineage import FILER, SUBJECT, filing
from tradingagents.strategies.data_sources import filing_supported_policy as policy
from tradingagents.strategies.data_sources.filing_evidence import frame_submission, select_primary, build_evidence_from_buffer
from tradingagents.strategies.data_sources.filing_parser_dispatch import parser_scope, dispatch_evidence
from tradingagents.strategies.data_sources.filing_spool import submission_spool_scope, spool_response
from tradingagents.strategies.data_sources.request_policy import provider_budget


class Response:
    def __init__(self, raw):
        self.raw, self.closed = raw, False

    def iter_content(self, chunk_size):
        for offset in range(0, len(self.raw), chunk_size):
            yield self.raw[offset:offset + chunk_size]

    def close(self):
        self.closed = True


def incomplete_source():
    evidence, _, body = filing(1, '2026-09-30')
    raw = submission([(evidence['form'], 'primary.xml', body.decode()),
        ('EX-99', 'selected.pdf', '<PDF>\n%PDF-1.7\ninvalid complete PDF body\n</PDF>')],
        form=evidence['form'], roles=role('FILED-BY', FILER, 'Source Filer') + role('SUBJECT-COMPANY', SUBJECT, 'Subject'))
    return raw


def source(raw, required_exhibits=None):
    required_exhibits = ['selected.pdf'] if required_exhibits is None else required_exhibits
    framed = frame_submission(raw, expected_accession=ACCESSION, expected_form='SCHEDULE 13D',
        expected_date='2026-09-30', observed_at=OBSERVED, max_submission_bytes=512 * 1024**2)
    evidence = build_evidence_from_buffer(select_primary(framed, raw), raw,
        required_exhibits=required_exhibits, max_document_bytes=16 * 1024**2,
        supported_document_policy=policy.POLICY_V4)
    evidence['source_url'] = f'https://www.sec.gov/Archives/edgar/data/1549966/{ACCESSION.replace("-", "")}/{ACCESSION}.txt'
    original = {'identity': {k: evidence[k] for k in ('accession', 'form', 'filing_date', 'source_url')},
        'observed_at': OBSERVED, 'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(), 'complete': True}
    proof = policy.build_local_source_proof(evidence, original=original, raw_submission=raw,
        required_exhibits=required_exhibits)
    return evidence, original, proof


def reseal(proof):
    proof['source_proof_sha256'] = policy._digest({k: v for k, v in proof.items() if k != 'source_proof_sha256'})


def test_incomplete_receipt_keeps_issue_failures_without_running_publisher_facts():
    evidence, original, proof = source(incomplete_source())
    assert evidence['structural_status'] == 'insufficient' and evidence['issues']
    assert proof['publisher_facts'] == [] and proof['reasons'] == []
    assert {'code': 'incomplete_current_evidence'} in proof['residual_failures']
    assert all({'code': 'selected_document_failure', 'issue': issue} in proof['residual_failures']
               for issue in evidence['issues'])
    assert not any(f['code'] == 'invalid_supported_disposition_input' for f in proof['residual_failures'])
    assert policy._checked_local_proof(proof, evidence, original) == proof
    result = policy.evaluate_supported_disposition(evidence, source_proofs={ACCESSION: proof},
        originals={ACCESSION: original}, occurrences=[{'collection': 'activist_13d', 'index': 0}])
    assert result['status'] == 'failed' and result['order_authority'] is False


@pytest.mark.parametrize('corruption', ['publisher_facts', 'publisher_reason', 'incomplete_failure',
    'selected_failure', 'issue_identity', 'original_hash', 'evidence_hash', 'version', 'policy'])
def test_negative_receipt_cannot_launder_corrupted_proof_or_strip_required_failures(corruption):
    evidence, original, proof = source(incomplete_source())
    if corruption == 'publisher_facts':
        proof['publisher_facts'] = [{'fabricated': 'facts'}]
    elif corruption == 'publisher_reason':
        proof['reasons'] = [{'code': 'unresolved_publisher_facts', 'facts': [{'fabricated': 'facts'}]}]
    elif corruption == 'incomplete_failure':
        proof['residual_failures'] = [f for f in proof['residual_failures'] if f['code'] != 'incomplete_current_evidence']
    elif corruption == 'selected_failure':
        proof['residual_failures'] = [f for f in proof['residual_failures'] if f['code'] != 'selected_document_failure']
    elif corruption == 'issue_identity':
        proof['residual_failures'] = [{**f, 'issue': {**f['issue'], 'filename': 'different.pdf'}}
            if f['code'] == 'selected_document_failure' else f for f in proof['residual_failures']]
    elif corruption == 'original_hash':
        proof['original'] = {**original, 'sha256': '0' * 64}
    elif corruption == 'evidence_hash':
        proof['evidence_sha256'] = '0' * 64
    elif corruption == 'version':
        proof['version'] = 'invented'
    else:
        proof['policy'] = policy.POLICY_V3
    reseal(proof)
    with pytest.raises(ValueError):
        policy._checked_local_proof(proof, evidence, original)


def test_actual_native_spool_negative_ownership_then_valid_parse_keeps_same_children(tmp_path):
    raw = incomplete_source()
    plain = submission([('8-K', 'main.htm', '<p>Complete source narrative.</p>')])
    deadline = time.monotonic() + 15
    with provider_budget('edgar', deadline, limits=()):
        with submission_spool_scope(tmp_path, original_deadline=deadline) as spool:
            with parser_scope() as owner:
                children = tuple(owner.children)
                response = Response(raw)
                record = spool_response(response, identity={'accession': ACCESSION, 'form': 'SCHEDULE 13D',
                    'filing_date': '2026-09-30', 'source_url': f'https://www.sec.gov/Archives/edgar/data/1549966/{ACCESSION.replace("-", "")}/{ACCESSION}.txt'})
                result = dispatch_evidence(record, expected_accession=ACCESSION, expected_form='SCHEDULE 13D',
                    expected_date='2026-09-30', observed_at=record.observed_at, max_submission_bytes=512 * 1024**2,
                    required_exhibits=['selected.pdf'], material_policy=policy.POLICY_V4)
                assert response.closed and result['structural_status'] == 'insufficient'
                assert result['supported_source_proof']['publisher_facts'] == []
                spool.discard(record)
                valid_response = Response(plain)
                valid = spool_response(valid_response, identity={'accession': ACCESSION, 'form': '8-K',
                    'filing_date': '2026-09-30', 'source_url': f'https://www.sec.gov/Archives/edgar/data/1549966/{ACCESSION.replace("-", "")}/{ACCESSION}.txt'})
                second = dispatch_evidence(valid, expected_accession=ACCESSION, expected_form='8-K',
                    expected_date='2026-09-30', observed_at=valid.observed_at, max_submission_bytes=512 * 1024**2,
                    required_exhibits=[], material_policy=policy.POLICY_V4)
                assert second['structural_status'] == 'complete' and not second['issues']
                assert second['supported_source_proof']['residual_failures'] == []
                assert tuple(owner.children) == children and all(c.poll() is None for c in children)
                spool.discard(valid)
    assert all(c.poll() is not None and c.stdin.closed and c.stdout.closed for c in children)
    assert spool.stats()['closed'] and spool.stats()['total_bytes'] == 0


def complete_contradictory_source():
    evidence, _, body = filing(1, '2026-09-30')
    xml = body.decode().replace('</coverPageHeader>', '<dateOfEvent>09/30/2026</dateOfEvent></coverPageHeader>')
    xml = xml.replace('</reportingPersonInfo>', '<aggregateAmountOwned>0</aggregateAmountOwned>'
        '<percentOfClass>19.7</percentOfClass><isAggregateExcludeShares>N</isAggregateExcludeShares></reportingPersonInfo>')
    raw = submission([(evidence['form'], 'primary.xml', xml)], form=evidence['form'],
        roles=role('FILED-BY', FILER, 'Source Filer') + role('SUBJECT-COMPANY', SUBJECT, 'Subject'))
    return source(raw, required_exhibits=[])


def test_complete_publisher_contradiction_remains_exact_and_cannot_be_stripped():
    evidence, original, proof = complete_contradictory_source()
    assert evidence['structural_status'] == 'complete' and evidence['issues'] == []
    assert proof['residual_failures'] == [] and len(proof['publisher_facts']) == 1
    assert proof['publisher_facts'][0]['fields'][0]['value'] == '0'
    assert proof['reasons'] == [{'code': 'unresolved_publisher_facts', 'facts': proof['publisher_facts']}]
    assert policy._checked_local_proof(proof, evidence, original) == proof
    proof['publisher_facts'] = []
    proof['reasons'] = []
    reseal(proof)
    with pytest.raises(ValueError, match='local_publisher_facts_mismatch'):
        policy._checked_local_proof(proof, evidence, original)


@pytest.mark.parametrize('mutation', ['publisher_facts', 'strip_failures', 'original_hash', 'evidence_hash'])
def test_native_ipc_corruption_still_closes_dispatcher(tmp_path, monkeypatch, mutation):
    from tradingagents.strategies.data_sources import filing_parser_dispatch as dispatch_module
    from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
    native = dispatch_module._read_frame

    def corrupt(*args, **kwargs):
        response = native(*args, **kwargs)
        proof = response['result']['supported_source_proof']
        if mutation == 'publisher_facts':
            proof['publisher_facts'] = [{'fabricated': 'facts'}]
        elif mutation == 'strip_failures':
            proof['residual_failures'] = [{'code': 'unrelated_failure'}]
        elif mutation == 'original_hash':
            proof['original']['sha256'] = '0' * 64
        else:
            proof['evidence_sha256'] = '0' * 64
        reseal(proof)
        return response

    monkeypatch.setattr(dispatch_module, '_read_frame', corrupt)
    deadline = time.monotonic() + 15
    with provider_budget('edgar', deadline, limits=()):
        with submission_spool_scope(tmp_path, original_deadline=deadline) as spool:
            with parser_scope() as owner:
                children = tuple(owner.children)
                record = spool_response(Response(incomplete_source()), identity={'accession': ACCESSION,
                    'form': 'SCHEDULE 13D', 'filing_date': '2026-09-30',
                    'source_url': f'https://www.sec.gov/Archives/edgar/data/1549966/{ACCESSION.replace("-", "")}/{ACCESSION}.txt'})
                with pytest.raises(SourceFetchError, match='provider_error'):
                    dispatch_evidence(record, expected_accession=ACCESSION, expected_form='SCHEDULE 13D',
                        expected_date='2026-09-30', observed_at=record.observed_at,
                        max_submission_bytes=512 * 1024**2, required_exhibits=['selected.pdf'],
                        material_policy=policy.POLICY_V4)
                assert all(c.poll() is not None for c in children)
    assert all(c.stdin.closed and c.stdout.closed for c in children)
    assert spool.stats()['closed'] and spool.stats()['total_bytes'] == 0
