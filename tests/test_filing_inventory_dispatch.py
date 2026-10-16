"""Count reconciliation transport stays closed, typed, and narrowly scoped."""
import time

import pytest

from test_filing_evidence import ACCESSION, submission
from test_filing_spool import Response
from tradingagents.strategies.data_sources import filing_parser_dispatch as dispatcher
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
from tradingagents.strategies.data_sources.filing_spool import submission_spool_scope, spool_response
from tradingagents.strategies.data_sources.request_policy import provider_budget

IDENTITY = {'accession': ACCESSION, 'form': '8-K', 'filing_date': '2026-09-30',
            'source_url': 'https://www.sec.gov/Archives/edgar/data/2065397/000119312526409112/' + ACCESSION + '.txt'}


def arguments(record):
    return {'expected_accession': ACCESSION, 'expected_form': '8-K',
            'expected_date': '2026-09-30', 'observed_at': record.observed_at,
            'max_submission_bytes': 512 * 1024 ** 2, 'required_exhibits': ()}


def reconciliation(record, raw):
    from test_filing_inventory_reconciliation import metadata_responses
    from tradingagents.strategies.data_sources.filing_inventory_reconciliation import build_reconciliation
    metadata = metadata_responses(raw)
    for receipt in metadata.values():
        receipt['observed_at'] = record.observed_at
    return build_reconciliation(metadata, original_identity=dict(record.identity),
        original_sha256=record.sha256, original_size=record.size, observed_at=record.observed_at)


def test_real_closed_count_mismatch_is_typed_and_children_remain_usable(tmp_path):
    raw = submission().replace(b'<PUBLIC-DOCUMENT-COUNT>1', b'<PUBLIC-DOCUMENT-COUNT>2')
    deadline = time.monotonic() + 15
    with provider_budget('edgar', deadline, limits=()):
        with submission_spool_scope(tmp_path, original_deadline=deadline) as spool:
            record = spool_response(Response((raw,)), identity=IDENTITY)
            with dispatcher.parser_scope() as owner:
                children = list(owner.children)
                with pytest.raises(SourceFetchError) as caught:
                    dispatcher.dispatch_evidence(record, **arguments(record))
                assert type(caught.value).__name__ == 'SubmissionDocumentCountMismatch'
                assert isinstance(caught.value, dispatcher.SubmissionDocumentCountMismatch)
                assert caught.value.reason_code == 'invalid_response'
                assert not owner._closed and owner.children == children
                assert spool.stats()['active_child_copies'] == 0
                assert not owner._child_leases and len(owner._available) == 2
                valid = spool_response(Response((submission(),)), identity=IDENTITY)
                result = dispatcher.dispatch_evidence(valid, **arguments(valid))
                assert result['structural_status'] == 'complete'
                assert owner.children == children and spool.stats()['active_child_copies'] == 0
            assert all(child.poll() is not None for child in children)


def test_nonspooled_count_mismatch_never_requests_reconciliation():
    raw = submission().replace(b'<PUBLIC-DOCUMENT-COUNT>1', b'<PUBLIC-DOCUMENT-COUNT>2')
    with provider_budget('edgar', time.monotonic() + 15, limits=()):
        with dispatcher.parser_scope() as owner:
            with pytest.raises(SourceFetchError) as caught:
                dispatcher.dispatch_evidence(raw, expected_accession=ACCESSION, expected_form='8-K',
                    expected_date='2026-09-30', observed_at='2026-10-10T04:22:51+00:00',
                    max_submission_bytes=64 * 1024 ** 2, required_exhibits=())
            assert type(caught.value).__name__ == 'SourceFetchError'
            assert not owner._closed


@pytest.mark.parametrize('count', [b'no', b'-1', b'1.0'])
def test_malformed_count_never_raises_reconciliation_signal(tmp_path, count):
    raw = submission().replace(b'<PUBLIC-DOCUMENT-COUNT>1', b'<PUBLIC-DOCUMENT-COUNT>' + count)
    deadline = time.monotonic() + 15
    with provider_budget('edgar', deadline, limits=()):
        with submission_spool_scope(tmp_path, original_deadline=deadline) as spool:
            record = spool_response(Response((raw,)), identity=IDENTITY)
            with dispatcher.parser_scope() as owner:
                with pytest.raises(SourceFetchError) as caught:
                    dispatcher.dispatch_evidence(record, **arguments(record))
                assert type(caught.value) is SourceFetchError and not owner._closed
                assert not owner._child_leases and spool.stats()['active_child_copies'] == 0


@pytest.mark.parametrize('proof', [None, [], 'unsafe proof'])
def test_malformed_proof_rejected_before_child_copy(tmp_path, proof):
    deadline = time.monotonic() + 15
    with provider_budget('edgar', deadline, limits=()):
        with submission_spool_scope(tmp_path, original_deadline=deadline) as spool:
            record = spool_response(Response((submission(),)), identity=IDENTITY)
            with dispatcher.parser_scope() as owner:
                with pytest.raises(SourceFetchError) as caught:
                    dispatcher.dispatch_evidence(record, **arguments(record), inventory_reconciliation=proof)
                assert type(caught.value) is SourceFetchError and not owner._closed
                assert owner._sequence == 0 and not owner._child_leases
                assert spool.stats()['active_child_copies'] == 0


def test_nonspooled_proof_is_not_an_alternate_parser_mode():
    with provider_budget('edgar', time.monotonic() + 15, limits=()):
        with dispatcher.parser_scope() as owner:
            with pytest.raises(SourceFetchError) as caught:
                dispatcher.dispatch_evidence(submission(), expected_accession=ACCESSION,
                    expected_form='8-K', expected_date='2026-09-30',
                    observed_at='2026-10-10T04:22:51+00:00', max_submission_bytes=64 * 1024 ** 2,
                    required_exhibits=(), inventory_reconciliation={})
            assert type(caught.value) is SourceFetchError
            assert owner._sequence == 0 and not owner._closed


def test_count_mismatch_without_closed_ack_poisoned_not_typed(tmp_path, monkeypatch):
    original = dispatcher._read_frame
    def missing_ack(*args, **kwargs):
        response = original(*args, **kwargs)
        response.pop('closed', None)
        return response
    monkeypatch.setattr(dispatcher, '_read_frame', missing_ack)
    raw = submission().replace(b'<PUBLIC-DOCUMENT-COUNT>1', b'<PUBLIC-DOCUMENT-COUNT>2')
    deadline = time.monotonic() + 15
    with provider_budget('edgar', deadline, limits=()):
        with submission_spool_scope(tmp_path, original_deadline=deadline) as spool:
            record = spool_response(Response((raw,)), identity=IDENTITY)
            with dispatcher.parser_scope() as owner:
                with pytest.raises(SourceFetchError) as caught:
                    dispatcher.dispatch_evidence(record, **arguments(record))
                assert type(caught.value) is SourceFetchError and owner._closed
                assert not owner._child_leases and spool.stats()['active_child_copies'] == 0
                assert all(child.poll() is not None for child in owner.children)


def test_unsolicited_result_proof_closes_dispatcher(tmp_path, monkeypatch):
    original = dispatcher._read_frame
    def unsolicited(*args, **kwargs):
        response = original(*args, **kwargs)
        response['result']['inventory_reconciliation'] = {}
        return response
    monkeypatch.setattr(dispatcher, '_read_frame', unsolicited)
    deadline = time.monotonic() + 15
    with provider_budget('edgar', deadline, limits=()):
        with submission_spool_scope(tmp_path, original_deadline=deadline) as spool:
            record = spool_response(Response((submission(),)), identity=IDENTITY)
            with dispatcher.parser_scope() as owner:
                with pytest.raises(SourceFetchError) as caught:
                    dispatcher.dispatch_evidence(record, **arguments(record))
                assert type(caught.value) is SourceFetchError and owner._closed
                assert not owner._child_leases and spool.stats()['active_child_copies'] == 0


def test_material_count_mismatch_stays_generic_and_cannot_take_proof(tmp_path):
    from tradingagents.strategies.data_sources.filing_material_policy import POLICY, policy_manifest
    approved = policy_manifest()['identities'][0]
    identity = {key: approved[key] for key in ('accession', 'form', 'filing_date', 'source_url')}
    raw = submission(form=identity['form'], documents=[(identity['form'], 'main.htm', '<p>Full material.</p>')])
    raw = raw.replace(ACCESSION.encode(), identity['accession'].encode())
    raw = raw.replace(b'20260930', identity['filing_date'].replace('-', '').encode())
    raw = raw.replace(b'<PUBLIC-DOCUMENT-COUNT>1', b'<PUBLIC-DOCUMENT-COUNT>2')
    deadline = time.monotonic() + 15
    with provider_budget('edgar', deadline, limits=()):
        with submission_spool_scope(tmp_path, original_deadline=deadline) as spool:
            record = spool_response(Response((raw,)), identity=identity)
            args = dict(expected_accession=identity['accession'], expected_form=identity['form'],
                expected_date=identity['filing_date'], observed_at=record.observed_at,
                max_submission_bytes=512 * 1024 ** 2, required_exhibits=(), material_policy=POLICY)
            with dispatcher.parser_scope() as owner:
                with pytest.raises(SourceFetchError) as caught:
                    dispatcher.dispatch_evidence(record, **args)
                assert type(caught.value) is SourceFetchError and not owner._closed
                sequence = owner._sequence
                with pytest.raises(SourceFetchError) as caught:
                    dispatcher.dispatch_evidence(record, **args, inventory_reconciliation={})
                assert type(caught.value) is SourceFetchError and not owner._closed
                assert owner._sequence == sequence and spool.stats()['active_child_copies'] == 0


def test_real_reconciled_result_is_validated_after_closed_ack(tmp_path, monkeypatch):
    from tradingagents.strategies.data_sources import filing_inventory_reconciliation as inventory
    raw = submission().replace(b'<PUBLIC-DOCUMENT-COUNT>1', b'<PUBLIC-DOCUMENT-COUNT>2')
    original = inventory.validate_reconciled_evidence
    calls = []
    deadline = time.monotonic() + 15
    with provider_budget('edgar', deadline, limits=()):
        with submission_spool_scope(tmp_path, original_deadline=deadline) as spool:
            record = spool_response(Response((raw,)), identity=IDENTITY)
            proof = reconciliation(record, raw)
            with dispatcher.parser_scope() as owner:
                def validated(result, **kwargs):
                    assert spool.stats()['active_child_copies'] == 0 and not owner._child_leases
                    assert kwargs['check'].__self__ is owner
                    calls.append(kwargs['original'])
                    return original(result, **kwargs)
                monkeypatch.setattr(inventory, 'validate_reconciled_evidence', validated)
                children = list(owner.children)
                with pytest.raises(dispatcher.SubmissionDocumentCountMismatch):
                    dispatcher.dispatch_evidence(record, **arguments(record))
                result = dispatcher.dispatch_evidence(record, **arguments(record), inventory_reconciliation=proof)
                assert result['structural_status'] == 'complete'
                assert result['inventory_reconciliation']['original'] == proof['original']
                assert set(proof) == {'version', 'original', 'metadata'}
                assert result['inventory_reconciliation']['actual_document_count'] == 1
                assert result['inventory_reconciliation']['declared_document_count'] == 2
                assert calls == [dict(identity=dict(record.identity), sha256=record.sha256,
                                      size=record.size, observed_at=record.observed_at)]
                assert owner.children == children and not owner._closed
            assert all(child.poll() is not None for child in children)


@pytest.mark.parametrize('damage', ['absent', 'original', 'header', 'inventory', 'identity'])
def test_changed_reconciled_result_poisoned_after_copy_closes(tmp_path, monkeypatch, damage):
    original = dispatcher._read_frame
    def altered(*args, **kwargs):
        response = original(*args, **kwargs)
        result = response['result']
        if damage == 'absent':
            del result['inventory_reconciliation']
        elif damage == 'original':
            result['inventory_reconciliation']['original']['sha256'] = '0' * 64
        elif damage == 'header':
            result['inventory_reconciliation']['native_header_sha256'] = '0' * 64
        elif damage == 'inventory':
            result['document_inventory'][0]['filename'] = 'other.htm'
        else:
            result['accession'] = '0001193125-26-999999'
        return response
    monkeypatch.setattr(dispatcher, '_read_frame', altered)
    raw = submission().replace(b'<PUBLIC-DOCUMENT-COUNT>1', b'<PUBLIC-DOCUMENT-COUNT>2')
    deadline = time.monotonic() + 15
    with provider_budget('edgar', deadline, limits=()):
        with submission_spool_scope(tmp_path, original_deadline=deadline) as spool:
            record = spool_response(Response((raw,)), identity=IDENTITY)
            with dispatcher.parser_scope() as owner:
                with pytest.raises(SourceFetchError) as caught:
                    dispatcher.dispatch_evidence(record, **arguments(record),
                        inventory_reconciliation=reconciliation(record, raw))
                assert type(caught.value) is SourceFetchError and owner._closed
                assert spool.stats()['active_child_copies'] == 0 and not owner._child_leases
                assert all(child.poll() is not None for child in owner.children)


def test_count_mismatch_with_proof_still_rejected_without_retry_signal(tmp_path):
    raw = submission().replace(b'<PUBLIC-DOCUMENT-COUNT>1', b'<PUBLIC-DOCUMENT-COUNT>2')
    deadline = time.monotonic() + 15
    with provider_budget('edgar', deadline, limits=()):
        with submission_spool_scope(tmp_path, original_deadline=deadline) as spool:
            record = spool_response(Response((raw,)), identity=IDENTITY)
            proof = reconciliation(record, raw)
            proof['metadata']['directory']['sha256'] = '0' * 64
            with dispatcher.parser_scope() as owner:
                with pytest.raises(SourceFetchError) as caught:
                    dispatcher.dispatch_evidence(record, **arguments(record), inventory_reconciliation=proof)
                assert type(caught.value) is SourceFetchError and not owner._closed
                assert spool.stats()['active_child_copies'] == 0 and not owner._child_leases


def test_original_deadline_governs_parent_proof_validation(tmp_path, monkeypatch):
    from tradingagents.strategies.data_sources import filing_inventory_reconciliation as inventory
    now = [10.]
    original = inventory.validate_reconciled_evidence
    def late_validation(result, **kwargs):
        now[0] = 20.
        return original(result, **kwargs)
    monkeypatch.setattr(inventory, 'validate_reconciled_evidence', late_validation)
    raw = submission().replace(b'<PUBLIC-DOCUMENT-COUNT>1', b'<PUBLIC-DOCUMENT-COUNT>2')
    with provider_budget('edgar', 20., clock=lambda: now[0], limits=()):
        with submission_spool_scope(tmp_path, original_deadline=20.) as spool:
            record = spool_response(Response((raw,)), identity=IDENTITY)
            with dispatcher.parser_scope() as owner:
                with pytest.raises(SourceFetchError) as caught:
                    dispatcher.dispatch_evidence(record, **arguments(record),
                        inventory_reconciliation=reconciliation(record, raw))
                assert type(caught.value) is SourceFetchError and caught.value.reason_code == 'timeout'
                assert owner._closed and not owner._child_leases
                assert spool.stats()['active_child_copies'] == 0
