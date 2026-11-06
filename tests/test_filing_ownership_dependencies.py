"""Ownership XML must preserve exact local exhibit evidence before assessment."""
import hashlib
import json

import pytest

from test_filing_evidence import ACCESSION, OBSERVED, ownership_xml, parse, role, submission
from tradingagents.strategies.data_sources import filing_evidence
from tradingagents.strategies.data_sources.filing_assessment import evidence_digest, prepare_request


def ownership_submission(exhibits, *, form='SCHEDULE 13D', xml=None, subject='1549966'):
    xml = ownership_xml(form=form) if xml is None else xml
    if form.startswith('SCHEDULE 13G'):
        xml = xml.replace('/schedule13D', '/schedule13g').replace('issuerCIK', 'issuerCik')
    roles = role('FILED-BY', '2', 'Reporting Person') + role('SUBJECT-COMPANY', subject, 'Sanitized Issuer')
    return submission([(form, 'primary_doc.xml', xml), *exhibits], form=form, roles=roles)


def build(raw, form='SCHEDULE 13D', *, buffered=False, **limits):
    if buffered:
        framed = filing_evidence.frame_submission(raw, expected_accession=ACCESSION,
            expected_form=form, expected_date='2026-09-30', observed_at=OBSERVED,
            max_submission_bytes=len(raw))
        selected = filing_evidence.select_primary(framed, raw)
        return filing_evidence.build_evidence_from_buffer(selected, raw, **limits)
    return filing_evidence.build_evidence(parse(raw, form=form), **limits)


@pytest.mark.parametrize('buffered', [False, True])
def test_xml_embedded_local_exhibit_reaches_selected_evidence_and_prompt(buffered):
    xml = ownership_xml().replace('May seek board changes.',
        'May seek board changes. &lt;a href="trades.htm#schedule"&gt;Trading schedule&lt;/a&gt;')
    roles = role('FILED-BY', '2', 'Reporting Person') + role('SUBJECT-COMPANY', '1549966', 'Sanitized Issuer')
    raw = submission([('SC 13D', 'primary_doc.xml', xml),
                      ('EX-99.1', 'trades.htm', '<p>Whole synthetic trading schedule.</p>')],
                     form='SCHEDULE 13D', roles=roles)
    if buffered:
        framed = filing_evidence.frame_submission(raw, expected_accession=ACCESSION,
            expected_form='SCHEDULE 13D', expected_date='2026-09-30', observed_at=OBSERVED,
            max_submission_bytes=len(raw))
        selected = filing_evidence.select_primary(framed, raw)
        evidence = filing_evidence.build_evidence_from_buffer(selected, raw)
    else:
        evidence = filing_evidence.build_evidence(parse(raw, form='SC 13D'))
    assert evidence['structural_status'] == 'complete'
    assert [unit['filename'] for unit in evidence['units']] == ['primary_doc.xml', 'trades.htm']
    exhibit = evidence['units'][1]
    assert exhibit['selection'] == 'dependency'
    assert exhibit['text'] == 'Whole synthetic trading schedule.'
    assert exhibit['body_sha256'] == hashlib.sha256(raw[exhibit['body_start']:exhibit['body_end']]).hexdigest()
    # Selection uses authoritative native document roles, not prose/href guesses.
    assert evidence['dependencies'] == []
    issuer_binding = {'status': 'verified', 'issuers': {ACCESSION: '0001549966'},
                      'corpus_sha256': {ACCESSION: evidence_digest(evidence)}}
    request = prepare_request('activist_stake', evidence, issuer_binding=issuer_binding)
    rendered = json.loads(request.user)['current'][0]
    assert [unit['filename'] for unit in rendered['units']] == ['primary_doc.xml', 'trades.htm']
    assert rendered['units'][1]['text'] == exhibit['text']
    assert request.context.source_provenance['unit_count'] == 2


@pytest.mark.parametrize('form', ['SCHEDULE 13D', 'SCHEDULE 13D/A', 'SCHEDULE 13G', 'SCHEDULE 13G/A'])
@pytest.mark.parametrize('buffered', [False, True])
@pytest.mark.parametrize('exhibits', [
    [('EX-1', 'ex1sc13d14949001_09292026.htm', '<p>Whole trade schedule.</p>'),
     ('EX-99.1', 'ex991sc13d14949001_09292026.htm', '<p>Whole joint filing agreement.</p>')],
    [('EX-99.4', 'tm2626636d2_ex99-4.htm', '<p>Whole master confirmation.</p>')],
])
def test_native_shaped_typed_exhibits_need_no_href_or_filename_guess(form, buffered, exhibits):
    # Native BRSP/HUT inventory shapes with bounded synthetic bodies. Item7 is
    # descriptive text, not a trustworthy filename-to-exhibit-number mapping.
    xml = ownership_xml(form=form).replace('</items1To7>',
        '<item7><filedExhibits>Attached trade schedule and agreement.</filedExhibits></item7></items1To7>')
    extra = [('GRAPHIC', 'image.jpg', 'unselected graphic'),
             ('XML', 'ex99_machine.xml', 'unselected non-exhibit XML')]
    raw = ownership_submission(exhibits + extra, form=form, xml=xml)
    evidence = build(raw, form, buffered=buffered)
    assert evidence['structural_status'] == 'complete'
    assert [unit['filename'] for unit in evidence['units']] == ['primary_doc.xml'] + [row[1] for row in exhibits]
    assert len(evidence['document_inventory']) == len(exhibits) + 3
    assert evidence['dependencies'] == []
    for unit in evidence['units'][1:]:
        assert unit['selection'] == 'dependency'
        assert unit['body_sha256'] == hashlib.sha256(raw[unit['body_start']:unit['body_end']]).hexdigest()
    binding = {'status': 'verified', 'issuers': {ACCESSION: '0001549966'},
               'corpus_sha256': {ACCESSION: evidence_digest(evidence)}}
    task = 'passive_stake' if form.startswith('SCHEDULE 13G') else 'activist_stake'
    request = prepare_request(task, evidence, issuer_binding=binding)
    rendered = json.loads(request.user)['current'][0]['units']
    assert [(unit['filename'], unit['text']) for unit in rendered] == [
        (unit['filename'], unit['text']) for unit in evidence['units']]
    assert request.context.source_provenance['unit_count'] == len(exhibits) + 1


def test_foreign_href_and_non_exhibit_document_roles_do_not_select_or_acquire():
    xml = ownership_xml().replace('May seek board changes.',
        '&lt;a href="https://foreign.invalid/ex99.htm"&gt;Foreign agreement&lt;/a&gt;')
    raw = ownership_submission([('XML', 'ex99.htm', '<p>Wrong native document role.</p>'),
                                ('GRAPHIC', 'ex99-1.htm', '<p>Also wrong native role.</p>')], xml=xml)
    evidence = build(raw)
    assert [unit['filename'] for unit in evidence['units']] == ['primary_doc.xml']
    assert 'Wrong native document role.' not in evidence['units'][0]['text']
    assert evidence['dependency_assessment'] == evidence['analysis_adequacy'] == 'not_assessed'


@pytest.mark.parametrize('name', ['../foreign.htm', '/foreign.htm', 'https://foreign.invalid/ex.htm', 'ex%2fescape.htm'])
def test_unsafe_exhibit_filename_still_fails_native_framing(name):
    with pytest.raises(filing_evidence.EvidenceError, match='unsafe_document_filename'):
        build(ownership_submission([('EX-99.1', name, '<p>Body.</p>')]))


@pytest.mark.parametrize('buffered', [False, True])
@pytest.mark.parametrize('kind,name,body,code', [
    ('EX-99.1', 'ex99.pdf', '<PDF>opaque</PDF>', 'unsupported_document_format'),
    ('EX-101.SCH', 'ex101.xsd', '<schema/>', 'unsupported_document_format'),
    ('EX-99.1', 'empty.htm', '<html><body></body></html>', 'empty_selected_document'),
    ('EX-99.1', 'large.htm', '<p>' + 'x' * 4096 + '</p>', 'document_byte_limit'),
])
def test_typed_exhibits_never_get_format_empty_or_size_exemptions(buffered, kind, name, body, code):
    evidence = build(ownership_submission([(kind, name, body)]), buffered=buffered, max_document_bytes=2048)
    assert evidence['structural_status'] == 'insufficient'
    assert {'code': code, 'filename': name, 'body_sha256': evidence['document_inventory'][1]['body_sha256']} in evidence['issues']


@pytest.mark.parametrize('buffered', [False, True])
def test_selected_exhibit_body_tamper_keeps_integrity_failure(buffered):
    raw = ownership_submission([('EX-99.1', 'trades.htm', '<p>Verified terms.</p>')])
    parsed = parse(raw, form='SCHEDULE 13D')
    document = parsed['documents'][1]
    if buffered:
        framed = filing_evidence.frame_submission(raw, expected_accession=ACCESSION,
            expected_form='SCHEDULE 13D', expected_date='2026-09-30', observed_at=OBSERVED,
            max_submission_bytes=len(raw))
        selected = filing_evidence.select_primary(framed, raw)
        mutated = bytearray(raw)
        mutated[document['body_start'] + 4] ^= 1
        evidence = filing_evidence.build_evidence_from_buffer(selected, mutated)
    else:
        document['body'] = document['body'].replace(b'Verified', b'Falsifie')
        evidence = filing_evidence.build_evidence(parsed)
    assert evidence['structural_status'] == 'insufficient'
    assert any(issue['code'] == 'document_integrity_mismatch' and issue['filename'] == 'trades.htm'
               for issue in evidence['issues'])


def test_local_exhibits_do_not_override_invalid_xml_subject_identity():
    evidence = build(ownership_submission([('EX-99.1', 'ex99.htm', '<p>Complete agreement.</p>')], subject='3'))
    assert evidence['structural_status'] == 'insufficient'
    assert any(issue['code'] == 'ownership_subject_mismatch' for issue in evidence['issues'])
    with pytest.raises(ValueError, match='invalid_filing_structural_evidence'):
        prepare_request('activist_stake', evidence, issuer_binding={})


def test_ownership_exhibits_still_share_the_total_text_budget():
    raw = ownership_submission([('EX-1', 'trades.htm', '<p>Whole trade schedule.</p>'),
                                ('EX-99.1', 'agreement.htm', '<p>Whole agreement.</p>')])
    complete = build(raw)
    total = sum(len(unit['text'].encode()) for unit in complete['units'])
    bounded = build(raw, max_total_text_bytes=total - 1)
    assert bounded['structural_status'] == 'insufficient'
    assert any(issue['code'] == 'total_text_byte_limit' and issue['filename'] == 'agreement.htm'
               for issue in bounded['issues'])


@pytest.mark.parametrize('form', ['8-K', '10-K', 'SCHEDULE 13D'])
def test_unlinked_html_exhibit_selection_remains_unchanged(form):
    roles = role('SUBJECT-COMPANY', '1549966', 'Sanitized Issuer') if form == 'SCHEDULE 13D' else None
    raw = submission([(form, 'main.htm', '<p>Whole primary.</p>'),
                      ('EX-99.1', 'ex99.htm', '<p>Unlinked exhibit.</p>')], form=form, roles=roles)
    evidence = build(raw, form)
    assert evidence['structural_status'] == 'complete'
    assert [unit['filename'] for unit in evidence['units']] == ['main.htm']
