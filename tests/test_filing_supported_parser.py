"""Prospective support flows through real owned parser children and raw audit."""
from copy import deepcopy
import time

import pytest

from test_filing_evidence import ACCESSION, submission
from test_filing_pdf import pdf_bytes
from test_filing_spool import Response
from tradingagents.strategies.data_sources.filing_parser_dispatch import parser_scope, dispatch_evidence
from tradingagents.strategies.data_sources.filing_spool import submission_spool_scope, spool_response
from tradingagents.strategies.data_sources.request_policy import provider_budget
from tradingagents.strategies.data_sources.filing_supported_policy import (
    POLICY, evaluate_supported_disposition, validate_supported_disposition,
)
from tradingagents.strategies.data_sources.filing_assessment import _validate_evidence

URL = f'https://www.sec.gov/Archives/edgar/data/2065397/{ACCESSION.replace("-", "")}/{ACCESSION}.txt'


def parsed(tmp_path, raw):
    deadline = time.monotonic() + 15
    identity = dict(accession=ACCESSION, form='8-K', filing_date='2026-09-30', source_url=URL)
    with provider_budget('edgar', deadline, limits=()):
        with submission_spool_scope(tmp_path, original_deadline=deadline) as spool:
            with parser_scope() as parser:
                record = spool_response(Response((raw,)), identity=identity)
                result = dispatch_evidence(record, expected_accession=ACCESSION, expected_form='8-K',
                    expected_date='2026-09-30', observed_at=record.observed_at,
                    max_submission_bytes=512 * 1024**2, required_exhibits=[], material_policy=POLICY)
                spool.discard(record)
            assert all(child.poll() is not None for child in parser.children)
    proof = result.pop('supported_source_proof')
    args = dict(source_proofs={ACCESSION: proof}, originals={ACCESSION: proof['original']},
        occurrences=[{'collection': 'filings', 'index': 0}])
    receipt = evaluate_supported_disposition(result, **args)
    assert validate_supported_disposition(receipt, result, raw_submissions={ACCESSION: raw}, **args) == receipt
    return result, receipt


def test_complete_native_text_pdf_goes_through_pure_children(tmp_path):
    raw = submission([('8-K', 'main.htm', '<a href="ex.pdf">Contract</a>'), ('EX-99', 'ex.pdf', 'PDF_BODY')])
    raw = raw.replace(b'PDF_BODY', b'<PDF>\n' + pdf_bytes((b'BT /F1 12 Tf 72 700 Td (Exact contract.) Tj ET',)) + b'</PDF>')
    evidence, disposition = parsed(tmp_path, raw)
    assert disposition['status'] == 'admitted'
    assert evidence['units'][1]['text'] == 'Exact contract.'
    assert evidence['pdf_evidence']['ex.pdf']['page_count'] == 1
    _validate_evidence(evidence)


def test_image_only_pdf_preserves_blank_native_text_and_defers(tmp_path):
    raw = submission([('8-K', 'main.htm', '<a href="ex.pdf">Agreement image</a>'), ('EX-99', 'ex.pdf', 'PDF_BODY')])
    raw = raw.replace(b'PDF_BODY', b'<PDF>\n' + pdf_bytes((b'',), image=True) + b'</PDF>')
    evidence, disposition = parsed(tmp_path, raw)
    assert disposition['status'] == 'deferred'
    assert evidence['units'][1]['text'] == ''
    assert disposition['reasons'][0]['code'] == 'unsupported_selected_visual_content'
    with pytest.raises(ValueError):
        _validate_evidence(evidence)
    _validate_evidence(evidence, allow_unsupported_documents=True)


def test_broken_selected_pdf_is_failure_not_deferral(tmp_path):
    raw = submission([('8-K', 'main.htm', '<a href="ex.pdf">Agreement</a>'), ('EX-99', 'ex.pdf', '%PDF-1.4 BROKEN')])
    evidence, disposition = parsed(tmp_path, raw)
    assert evidence['structural_status'] == 'insufficient'
    assert disposition['status'] == 'failed'
    assert disposition['residual_failures']


def test_rehashed_missing_page_does_not_enter_assessment(tmp_path):
    raw = submission([('8-K', 'main.htm', '<a href="ex.pdf">Contract</a>'), ('EX-99', 'ex.pdf', 'PDF_BODY')])
    raw = raw.replace(b'PDF_BODY', b'<PDF>\n' + pdf_bytes((b'BT /F1 12 Tf 72 700 Td (First.) Tj ET', b'BT /F1 12 Tf 72 700 Td (Last.) Tj ET')) + b'</PDF>')
    evidence, disposition = parsed(tmp_path, raw)
    evidence['pdf_evidence']['ex.pdf']['pages'].pop()
    with pytest.raises(ValueError, match='invalid_filing_pdf_inventory'):
        _validate_evidence(evidence)
