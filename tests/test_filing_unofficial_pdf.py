"""Official text precedes supplemental PDF, without assessing PDF equivalence.

SEC INVALID_UNOFFICIAL_PDF explains this native document ordering:
https://www.sec.gov/submit-filings/filer-support-resources/how-do-i-guides/understand-messages-reported-edgar
The official-PDF exceptions exclude primary 10-K/Q/8-K documents:
https://www.sec.gov/submit-filings/filer-support-resources/how-do-i-guides/observe-data-process-filing-limits

Carnival complete raw SHA47652b671ffbc0caa2a15f1d51bad7dc587f2d179b051d517de1360c5735ba91
contains official ccl-20260831.htm sequence1 and pdfofform10q.pdf sequence12.
Retained SEC history response f82d9bc721785d7e2ed4a11e4d54d6ffda7148c37bd8c42f23065955843002f0
also names ccl-20260831.htm for accession0000815097-26-000107, date2026-09-29.
"""
import hashlib

import pytest

from test_filing_evidence import parse, submission
from tradingagents.strategies.data_sources.filing_evidence import EvidenceError, build_evidence


PDF = '<PDF>\nbegin 644 supplement.pdf\n% Opaque PDF fixture; no equivalence claim.\nend\n</PDF>'


@pytest.mark.parametrize('form', ['10-K', '10-K/A', '10-Q', '10-Q/A', '8-K', '8-K/A'])
@pytest.mark.parametrize('suffix', ['htm', 'html', 'txt'])
def test_official_supported_text_precedes_same_form_native_pdf_and_all_bytes_remain(form, suffix):
    raw = submission([(form, 'official.' + suffix, '<p>Full official narrative.</p>'),
        ('EX-99.1', 'exhibit.htm', '<p>Additional exhibit.</p>'),
        (form, 'supplement.pdf', PDF)], form=form)
    parsed = parse(raw, form=form)
    assert parsed['primary_index'] == 0 and len(parsed['documents']) == 3
    for document in parsed['documents']:
        assert document['body'] == raw[document['body_start']:document['body_end']]
        assert hashlib.sha256(document['body']).hexdigest() == document['body_sha256']
    evidence = build_evidence(parsed)
    assert evidence['structural_status'] == 'complete'
    assert len(evidence['document_inventory']) == 3
    assert [unit['filename'] for unit in evidence['units']] == ['official.' + suffix]
    assert evidence['analysis_adequacy'] == 'not_assessed'
    assert evidence['dependency_assessment'] == 'not_assessed'
    assert evidence['structural_scope'] == 'primary_and_selected_dependencies'
    assert evidence['document_inventory'][2]['body_sha256'] == parsed['documents'][2]['body_sha256']


def test_multiple_later_pdf_companions_are_retained_without_assessment():
    raw = submission([('10-Q', 'official.htm', '<p>Official.</p>'),
        ('10-Q', 'one.pdf', PDF), ('10-Q', 'two.pdf', PDF)], form='10-Q')
    parsed = parse(raw, form='10-Q')
    assert parsed['primary_index'] == 0 and len(parsed['documents']) == 3


@pytest.mark.parametrize('case', ['two_text', 'pdf_first', 'earlier_pdf_sequence', 'unknown_companion',
    'pdf_only', 'missing_wrapper', 'unterminated_wrapper', 'double_wrapper', 'prefix', 'suffix', 'empty_wrapper'])
def test_ambiguous_or_non_native_companions_still_fail(case):
    docs = [('10-Q', 'official.htm', '<p>Official.</p>'), ('10-Q', 'supplement.pdf', PDF)]
    if case == 'two_text':
        docs.append(('10-Q', 'other.htm', '<p>Other narrative.</p>'))
    elif case == 'pdf_first':
        docs.reverse()
    elif case == 'unknown_companion':
        docs[1] = ('10-Q', 'supplement.xml', '<xml/>')
    elif case == 'pdf_only':
        docs = [('10-Q', 'one.pdf', PDF), ('10-Q', 'two.pdf', PDF)]
    elif case in {'missing_wrapper', 'unterminated_wrapper', 'double_wrapper', 'prefix', 'suffix', 'empty_wrapper'}:
        malformed = {'missing_wrapper': 'opaque PDF bytes', 'unterminated_wrapper': '<PDF>\nopaque',
            'double_wrapper': PDF + '\n' + PDF, 'prefix': 'outside\n' + PDF,
            'suffix': PDF + '\noutside', 'empty_wrapper': '<PDF>\n</PDF>'}
        docs[1] = ('10-Q', 'supplement.pdf', malformed[case])
    raw = submission(docs, form='10-Q')
    if case == 'earlier_pdf_sequence':
        raw = raw.replace(b'<SEQUENCE>1', b'<SEQUENCE>9')
    with pytest.raises(EvidenceError, match='ambiguous_primary_document'):
        parse(raw, form='10-Q')


@pytest.mark.parametrize('form', ['DEF 14A', 'SCHEDULE 13D', 'SCHEDULE 13D/A', 'SCHEDULE 13G', 'SCHEDULE 13G/A'])
def test_unapproved_form_families_keep_the_existing_ambiguity_guard(form):
    from test_filing_evidence import role
    roles = role('SUBJECT-COMPANY' if form.startswith('SCHEDULE') else 'FILER', '1', 'Issuer')
    raw = submission([(form, 'official.htm', '<p>Official.</p>'), (form, 'supplement.pdf', PDF)], form=form, roles=roles)
    with pytest.raises(EvidenceError, match='ambiguous_primary_document'):
        parse(raw, form=form)


def test_required_pdf_remains_explicitly_unsupported_instead_of_becoming_assessed():
    raw = submission([('10-Q', 'official.htm', '<p>Official.</p>'), ('10-Q', 'supplement.pdf', PDF)], form='10-Q')
    evidence = build_evidence(parse(raw, form='10-Q'), required_exhibits=['supplement.pdf'])
    assert evidence['structural_status'] == 'insufficient'
    assert evidence['issues'][0]['code'] == 'unsupported_document_format'
    assert evidence['issues'][0]['filename'] == 'supplement.pdf'
