"""Pure, bounded NC filing evidence; fixtures preserve native provider framing."""
import hashlib
import json

import pytest

from tradingagents.strategies.data_sources.filing_evidence import (
    EvidenceError, build_evidence, parse_submission,
)

ACCESSION = '0001193125-26-409112'
OBSERVED = '2026-10-10T04:22:51+00:00'


def role(kind, cik, name):
    return (f'<{kind}>\r<COMPANY-DATA>\r<CONFORMED-NAME>{name}\r'
            f'<CIK>{cik}\r</COMPANY-DATA>\r</{kind}>\r')


def submission(documents=None, *, form='8-K', roles=None, newline='\r'):
    documents = documents or [('8-K', 'main.htm', '<html><p>Full narrative.</p></html>')]
    roles = roles or role('FILER', '0002065397', 'Sanitized Credit Fund LLC')
    header = (f'<SUBMISSION>\n<ACCESSION-NUMBER>{ACCESSION}\r<TYPE>{form}\r'
              f'<PUBLIC-DOCUMENT-COUNT>{len(documents)}\r<FILING-DATE>20260930\r' + roles)
    parts = [header]
    for sequence, (kind, filename, body) in enumerate(documents, 1):
        parts.append(f'<DOCUMENT>\r<TYPE>{kind}\r<SEQUENCE>{sequence}\r'
                     f'<FILENAME>{filename}\r<TEXT>\r{body}\r</TEXT>\r</DOCUMENT>\r')
    return ''.join(parts).replace('\r', newline).encode() + b'</SUBMISSION>\r'


def parse(raw=None, *, form='8-K', **kwargs):
    return parse_submission(raw or submission(), expected_accession=ACCESSION,
                            expected_form=form, expected_date='2026-09-30',
                            observed_at=OBSERVED, **kwargs)


def ownership_xml(*, cik='0001549966', form='SCHEDULE 13D'):
    return (f'<XML>\r<?xml version="1.0"?><edgarSubmission xmlns="http://www.sec.gov/edgar/schedule13D" xmlns:com="http://www.sec.gov/edgar/common">'
            f'<headerData><submissionType>{form}</submissionType><filerInfo><filer><filerCredentials>'
            '<cik>0000000002</cik><ccc>DO-NOT-EXPOSE</ccc></filerCredentials></filer></filerInfo></headerData>'
            '<formData><coverPageHeader><issuerInfo>'
            f'<issuerCIK>{cik}</issuerCIK><issuerName>Sanitized Issuer</issuerName><address><com:city>Sanitized City</com:city></address></issuerInfo></coverPageHeader>'
            '<reportingPersons><reportingPersonInfo><percentOfClass>12.5</percentOfClass></reportingPersonInfo>'
            '<reportingPersonInfo><percentOfClass>6</percentOfClass></reportingPersonInfo></reportingPersons>'
            '<items1To7><item4><transactionPurpose>&lt;p&gt;May seek board changes.&lt;/p&gt;</transactionPurpose>'
            '</item4></items1To7></formData></edgarSubmission>\r</XML>')


@pytest.mark.parametrize('newline', ['\r', '\n', '\r\n'])
def test_exact_partition_hashes_offsets_and_actual_observation(newline):
    raw = submission(newline=newline)
    result = parse(raw)
    document = result['documents'][0]
    assert raw[document['body_start']:document['body_end']] == document['body']
    assert document['body_sha256'] == hashlib.sha256(document['body']).hexdigest()
    assert result['submission_sha256'] == hashlib.sha256(raw).hexdigest()
    assert result['accepted_at'] is None
    assert result['observed_at'] == OBSERVED
    assert result['roles'][0]['name'] == 'Sanitized Credit Fund LLC'
    assert result['roles'][0]['cik'] == '0002065397'
    assert result['roles'][0]['sha256'] == hashlib.sha256(raw[result['roles'][0]['start']:result['roles'][0]['end']]).hexdigest()


def test_joint_filers_are_preserved_without_execution_binding():
    result = parse(submission(roles=role('FILER', '764622', 'Parent') + role('FILER', '7286', 'Subsidiary')))
    assert {x['cik'] for x in result['issuer_candidates']} == {'0000764622', '0000007286'}
    assert 'issuer' not in result and 'ticker' not in result
    assert build_evidence(result)['structural_status'] == 'complete'


@pytest.mark.parametrize(('change', 'code'), [
    (lambda b: b.replace(ACCESSION.encode(), b'0001193125-26-999999'), 'accession_mismatch'),
    (lambda b: b.replace(b'<FILING-DATE>20260930', b'<FILING-DATE>20260929'), 'filing_date_mismatch'),
    (lambda b: b.replace(b'<PUBLIC-DOCUMENT-COUNT>1', b'<PUBLIC-DOCUMENT-COUNT>2'), 'document_count_mismatch'),
    (lambda b: b.replace(b'</TEXT>', b''), 'incomplete_document_text'),
    (lambda b: b.replace(b'</SUBMISSION>', b''), 'incomplete_submission'),
    (lambda b: b.replace(b'<CIK>0002065397', b'<CIK>BAD'), 'invalid_role_cik'),
    (lambda b: b.replace(b'<FILENAME>main.htm', b'<FILENAME>../main.htm'), 'unsafe_document_filename'),
    (lambda b: b.replace(b'</FILER>', b''), 'incomplete_role'),
])
def test_corrupt_identity_or_framing_fails_closed(change, code):
    with pytest.raises(EvidenceError, match=code):
        parse(change(submission()))


def test_unsupported_submission_and_form_are_explicit():
    with pytest.raises(EvidenceError, match='unsupported_submission_format'):
        parse(b'<UNKNOWN>unproven</UNKNOWN>')
    with pytest.raises(EvidenceError, match='unsupported_form'):
        parse(submission(form='6-K'), form='6-K')


def test_duplicate_primary_or_filename_rejected():
    with pytest.raises(EvidenceError, match='ambiguous_primary_document'):
        parse(submission([('8-K', 'one.htm', 'one'), ('8-K', 'two.htm', 'two')]))
    with pytest.raises(EvidenceError, match='unsafe_document_filename'):
        parse(submission([('8-K', 'one.htm', 'one'), ('EX-99.1', 'one.htm', 'two')]))


def test_bounds_never_clip_text_and_full_narrative_survives_prefix():
    raw = submission([('8-K', 'main.htm', '<p>' + 'A ' * 6000 + 'Last substantive paragraph.</p>')])
    evidence = build_evidence(parse(raw))
    unit = evidence['units'][0]
    assert unit['text'].endswith('Last substantive paragraph.')
    assert unit['text_end'] == len(unit['text']) and unit['text_start'] == 0
    assert evidence['analysis_adequacy'] == 'not_assessed'
    json.dumps(evidence)
    with pytest.raises(EvidenceError, match='submission_byte_limit'):
        parse(raw, max_submission_bytes=len(raw) - 1)
    limited = build_evidence(parse(raw), max_document_bytes=100)
    assert limited['structural_status'] == 'insufficient' and limited['units'] == []
    assert limited['issues'][0]['code'] == 'document_byte_limit'


def test_hidden_xbrl_removed_visible_facts_and_entities_preserved():
    html = ('<html xmlns:i="http://www.xbrl.org/2013/inlineXBRL"><head>machine</head>'
            '<i:header>hidden</i:header><p>Revenue&nbsp;&amp; income <i:nonFraction>100</i:nonFraction></p>'
            '<p style="display:none">hidden</p></html>')
    evidence = build_evidence(parse(submission([('8-K', 'main.htm', html)])))
    assert evidence['units'][0]['text'] == 'Revenue & income 100'


def test_linked_and_explicit_exhibits_selected_without_global_regex_inference():
    primary = ('<p>Item 9.01. Exhibits</p><a href="ex99.htm#news">99.1 Release</a>'
               '<a href="#part">Internal</a><a href="https://example.org/old.htm">Prior agreement</a>')
    documents = [('8-K', 'main.htm', primary),
                 ('EX-99.1', 'ex99.htm', '<p>Whole news release.</p>'),
                 ('EX-10.1', 'ex10.htm', '<p>Internal Exhibit 9.03. Incorporated by reference.</p>'),
                 ('EX-101.SCH', 'machine.xsd', 'machine')]
    parsed = parse(submission(documents))
    evidence = build_evidence(parsed, required_exhibits=('EX-10.1',))
    assert [u['filename'] for u in evidence['units']] == ['main.htm', 'ex99.htm', 'ex10.htm']
    assert evidence['structural_status'] == 'complete'
    assert {d['resolution'] for d in evidence['dependencies']} == {'same_submission', 'same_document', 'external'}
    assert evidence['dependency_assessment'] == 'not_assessed'
    assert len(evidence['document_inventory']) == 4


def test_missing_required_and_unsupported_pdf_remain_visible():
    parsed = parse(submission([('8-K', 'main.htm', '<p>Narrative</p>'), ('EX-99.1', 'news.pdf', 'PDF')]))
    evidence = build_evidence(parsed, required_exhibits=('EX-99.1', 'absent.htm'))
    assert evidence['structural_status'] == 'insufficient'
    assert {x['code'] for x in evidence['issues']} == {'unsupported_document_format', 'missing_required_dependency'}
    assert next(x for x in evidence['issues'] if x['code'] == 'unsupported_document_format')['filename'] == 'news.pdf'


def test_modern_13d_subject_binding_labels_and_no_credentials():
    roles = role('FILED-BY', '2', 'Reporting Person') + role('SUBJECT-COMPANY', '1549966', 'Sanitized Issuer')
    parsed = parse(submission([('SC 13D', 'primary_doc.xml', ownership_xml())], form='SCHEDULE 13D', roles=roles), form='SC 13D')
    evidence = build_evidence(parsed)
    assert evidence['structural_status'] == 'complete'
    assert [r['cik'] for r in evidence['issuer_candidates']] == ['0001549966']
    text = evidence['units'][0]['text']
    assert 'Percent Of Class: 12.5' in text and 'Percent Of Class: 6' in text
    assert 'Transaction Purpose: May seek board changes.' in text
    assert 'City: Sanitized City' in text
    assert 'DO-NOT-EXPOSE' not in text and 'filerCredentials' not in text


@pytest.mark.parametrize(('replacement', 'code'), [
    (('0001549966', '0000000003'), 'ownership_subject_mismatch'),
    (('http://www.sec.gov/edgar/schedule13D', 'https://untrusted.example/schema'), 'unsupported_ownership_schema'),
    (('SCHEDULE 13D</submissionType>', 'SCHEDULE 13D/A</submissionType>'), 'ownership_form_mismatch'),
    (('<?xml version="1.0"?>', '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY x "bad">]>'), 'unsafe_xml_declaration'),
])
def test_ownership_schema_identity_and_entities_fail_closed(replacement, code):
    xml = ownership_xml().replace(*replacement)
    parsed = parse(submission([('SCHEDULE 13D', 'primary_doc.xml', xml)], form='SCHEDULE 13D',
                              roles=role('SUBJECT-COMPANY', '1549966', 'Issuer')), form='SCHEDULE 13D')
    evidence = build_evidence(parsed)
    assert evidence['structural_status'] == 'insufficient'
    assert evidence['issues'][0]['code'] == code


def test_malformed_link_is_explicit_without_leaking_parser_exception():
    parsed = parse(submission([('8-K', 'main.htm', '<p>Disclosure</p><a href="https://[invalid">Exhibit</a>')]))
    evidence = build_evidence(parsed)
    assert evidence['structural_status'] == 'insufficient'
    assert evidence['issues'][0]['code'] == 'invalid_dependency_href'


def test_total_text_limit_keeps_required_failure_visible_without_truncation():
    parsed = parse(submission([('8-K', 'main.htm', '<p>Primary</p>'), ('EX-99.1', 'news.htm', '<p>Entire exhibit</p>')]))
    evidence = build_evidence(parsed, required_exhibits=('EX-99.1',), max_total_text_bytes=10)
    assert [u['text'] for u in evidence['units']] == ['Primary']
    assert evidence['structural_status'] == 'insufficient'
    assert evidence['issues'][0]['code'] == 'total_text_byte_limit'


def test_naive_observation_and_invalid_native_acceptance_rejected():
    with pytest.raises(EvidenceError, match='invalid_observed_at'):
        parse_submission(submission(), expected_accession=ACCESSION, expected_form='8-K',
                         expected_date='2026-09-30', observed_at='2026-10-10T04:00:00')
    raw = submission().replace(b'<FILING-DATE>', b'<ACCEPTANCE-DATETIME>20261330010101\r<FILING-DATE>')
    with pytest.raises(EvidenceError, match='invalid_acceptance_datetime'):
        parse(raw)


@pytest.mark.parametrize(('filename', 'accession', 'form', 'digest'), [
    ('native_8k_primary.nc', ACCESSION, '8-K', 'b18e9d4a95f85fbf69ebd8f4697a2c6c80275227bf68ce7dca9e630d4a1a9edc'),
    ('native_13d.nc', '0001193125-26-409121', 'SCHEDULE 13D', '5b3f799e5193bd7fd77564474f0a121fbb90aad0768ef75e99801c5d6145f700'),
])
def test_small_native_derived_fixtures(filename, accession, form, digest):
    from pathlib import Path
    raw = (Path(__file__).parent / 'fixtures' / 'filing_evidence' / filename).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == digest
    parsed = parse_submission(raw, expected_accession=accession, expected_form=form,
                              expected_date='2026-09-30', observed_at=OBSERVED)
    evidence = build_evidence(parsed)
    assert evidence['structural_status'] == 'complete'
    assert evidence['analysis_adequacy'] == 'not_assessed'
    assert evidence['accepted_at'] is None
    assert evidence['units'][0]['body_sha256'] == parsed['documents'][parsed['primary_index']]['body_sha256']
    text = evidence['units'][0]['text']
    if form == '8-K':
        assert text.index('Item 8.01') > 3000
    else:
        assert 'Percent Of Class:' in text and 'Transaction Purpose:' in text
        assert 'REDACTED' not in text


def direct_fixture():
    from pathlib import Path
    return (Path(__file__).parent / 'fixtures' / 'filing_evidence' / 'native_13g_direct.txt').read_bytes()


def parse_direct(raw=None):
    return parse_submission(raw or direct_fixture(), expected_accession='0002042926-26-000016',
                            expected_form='SCHEDULE 13G/A', expected_date='2026-10-09', observed_at=OBSERVED)


def test_native_direct_13g_partitions_roles_schema_and_labels():
    raw = direct_fixture()
    parsed = parse_direct(raw)
    assert parsed['format'] == 'sec_complete_submission'
    assert {r['role'] for r in parsed['roles']} == {'SUBJECT-COMPANY', 'FILED-BY'}
    assert len(parsed['issuer_candidates']) == 1
    assert parsed['issuer_candidates'][0]['role'] == 'SUBJECT-COMPANY'
    assert parsed['accepted_at'] == '20261009162034'
    assert raw[parsed['documents'][0]['body_start']:parsed['documents'][0]['body_end']] == parsed['documents'][0]['body']
    evidence = build_evidence(parsed)
    assert evidence['structural_status'] == 'complete'
    assert evidence['analysis_adequacy'] == 'not_assessed'
    text = evidence['units'][0]['text']
    assert 'Issuer Cik:' in text and 'Class Percent:' in text
    assert 'REDACTED' not in text


@pytest.mark.parametrize(('old', 'new', 'code'), [
    (b'</SEC-HEADER>', b'', 'incomplete_sec_header'),
    (b'</SEC-DOCUMENT>', b'', 'incomplete_submission'),
    (b'ACCESSION NUMBER:\t\t0002042926-26-000016', b'ACCESSION NUMBER:\t\t0002042926-26-000017', 'accession_mismatch'),
    (b'CONFORMED SUBMISSION TYPE:\tSCHEDULE 13G/A', b'CONFORMED SUBMISSION TYPE:\tSCHEDULE 13D/A', 'form_mismatch'),
    (b'<issuerCik>', b'<unknownIssuerCik>', 'invalid_ownership_xml'),
])
def test_direct_native_tampering_fails_closed(old, new, code):
    raw = direct_fixture()
    assert old in raw
    changed = raw.replace(old, new, 1)
    if code == 'invalid_ownership_xml':
        assert build_evidence(parse_direct(changed))['issues'][0]['code'] == code
    else:
        with pytest.raises(EvidenceError, match=code):
            parse_direct(changed)


@pytest.mark.parametrize('outer_name', [b'<SEC-DOCUMENT>', b'<SEC-HEADER>'])
def test_direct_outer_accession_cannot_disagree_with_header(outer_name):
    raw = direct_fixture().replace(outer_name + b'0002042926-26-000016', outer_name + b'0002042926-26-999999', 1)
    with pytest.raises(EvidenceError, match='accession_mismatch'):
        parse_direct(raw)


def test_document_body_mutation_cannot_reuse_source_hash():
    parsed = parse()
    parsed['documents'][0]['body'] += b'<p>Injected</p>'
    evidence = build_evidence(parsed)
    assert evidence['structural_status'] == 'insufficient'
    assert evidence['issues'][0]['code'] == 'document_integrity_mismatch'


@pytest.mark.parametrize(('old', 'new', 'code'), [
    (b'http://www.sec.gov/edgar/schedule13g', b'http://www.sec.gov/edgar/schedule13G', 'unsupported_ownership_schema'),
    (b'<submissionType>SCHEDULE 13G/A</submissionType>', b'<submissionType>SCHEDULE 13D/A</submissionType>', 'ownership_form_mismatch'),
])
def test_direct_13g_schema_and_form_are_exact(old, new, code):
    raw = direct_fixture()
    assert old in raw
    evidence = build_evidence(parse_direct(raw.replace(old, new)))
    assert evidence['structural_status'] == 'insufficient'
    assert evidence['issues'][0]['code'] == code


def test_direct_13g_subject_cik_cannot_be_replaced_by_reporting_person():
    import re
    raw = direct_fixture()
    parsed = parse_direct(raw)
    subject = parsed['issuer_candidates'][0]['cik']
    reporter = next(r['cik'] for r in parsed['roles'] if r['role'] == 'FILED-BY')
    assert subject != reporter
    changed, count = re.subn(rb'(<issuerCik>)[0-9]+(</issuerCik>)',
                            lambda m: m[1] + reporter.encode() + m[2], raw)
    assert count == 1
    evidence = build_evidence(parse_direct(changed))
    assert evidence['issues'][0]['code'] == 'ownership_subject_mismatch'


@pytest.mark.parametrize('encoding', ['utf-16', 'utf-16-le', 'utf-16-be', 'utf-32'])
def test_non_utf8_xml_cannot_bypass_forbidden_entity_declarations(encoding):
    xml = ownership_xml().removeprefix('<XML>\r').removesuffix('\r</XML>')
    xml = xml.replace('<?xml version="1.0"?>',
                      '<?xml version="1.0" encoding="' + encoding + '"?>'
                      '<!DOCTYPE edgarSubmission [<!ENTITY injected "ENTITY-SHOULD-BE-REJECTED">]>')
    xml = xml.replace('May seek board changes.', '&injected;')
    raw = submission([('SC 13D', 'primary_doc.xml', 'REPLACEME')], form='SCHEDULE 13D',
                     roles=role('SUBJECT-COMPANY', '1549966', 'Issuer'))
    raw = raw.replace(b'REPLACEME', b'<XML>\n' + xml.encode(encoding) + b'\n</XML>')
    evidence = build_evidence(parse(raw, form='SC 13D'))
    assert evidence['structural_status'] == 'insufficient'
    assert evidence['issues'][0]['code'] == 'unsupported_xml_encoding'
    assert evidence['units'] == []


def test_utf8_bom_still_cannot_contain_entity_declaration():
    xml = ownership_xml().removeprefix('<XML>\r').removesuffix('\r</XML>')
    xml = xml.replace('<?xml version="1.0"?>',
                      '<?xml version="1.0"?><!DOCTYPE edgarSubmission [<!ENTITY injected "blocked">]>')
    raw = submission([('SC 13D', 'primary_doc.xml', 'REPLACEME')], form='SCHEDULE 13D',
                     roles=role('SUBJECT-COMPANY', '1549966', 'Issuer'))
    raw = raw.replace(b'REPLACEME', b'<XML>\n' + xml.encode('utf-8-sig') + b'\n</XML>')
    evidence = build_evidence(parse(raw, form='SC 13D'))
    assert evidence['issues'][0]['code'] == 'unsafe_xml_declaration'
    assert evidence['units'] == []


@pytest.mark.parametrize('newline', ['\r', '\n', '\r\n'])
def test_framing_tags_embedded_in_document_content_are_not_boundaries(newline):
    body = ('Narrative x<DOCUMENT>\rnot a document\r'
            'embedded<TEXT> payload embedded</TEXT> and embedded</DOCUMENT>\r'
            'embedded</SUBMISSION> is narrative.')
    raw = submission([('8-K', 'main.txt', body)], newline=newline)
    parsed = parse(raw)
    assert len(parsed['documents']) == 1
    assert parsed['documents'][0]['body'] == (newline + body.replace('\r', newline) + newline).encode()
    assert parsed['documents'][0]['body_sha256'] == hashlib.sha256(parsed['documents'][0]['body']).hexdigest()


@pytest.mark.parametrize(('token', 'code'), [
    (b'<DOCUMENT>', 'invalid_document_count'),
    (b'<TEXT>', 'incomplete_document_text'),
    (b'</TEXT>', 'incomplete_document_text'),
    (b'</DOCUMENT>', 'incomplete_document'),
    (b'</SUBMISSION>', 'incomplete_submission'),
])
@pytest.mark.parametrize('prefix', [b'x', b' ', b'\t'])
def test_framing_tokens_require_exact_start_or_cr_lf_boundary(token, code, prefix):
    with pytest.raises(EvidenceError, match=code):
        parse(submission().replace(token, prefix + token))


def test_large_unselected_attachment_does_not_make_framing_scan_bytewise():
    """Native complete submissions retain large image/encoded attachments.

    Compare to one old zero-width boundary scan on the same bytes, rather than
    an absolute machine-speed threshold. Full parsing should be much cheaper
    than that one scan while still retaining every attachment byte and offset.
    """
    import re
    import time
    attachment = 'M' * (8 * 1024 * 1024)
    raw = submission([('8-K', 'main.htm', '<p>Complete narrative.</p>'),
                      ('GRAPHIC', 'image.jpg', attachment)])
    reference = []
    for _ in range(3):
        began = time.perf_counter()
        list(re.finditer(rb'(?:\A|(?<=[\r\n]))<DOCUMENT>[\r\n]', raw))
        reference.append(time.perf_counter() - began)
    elapsed = []
    for _ in range(3):
        began = time.perf_counter()
        parsed = parse(raw)
        elapsed.append(time.perf_counter() - began)
    assert len(parsed['documents']) == 2
    image = parsed['documents'][1]
    assert image['body'] == b'\r' + attachment.encode() + b'\r'
    assert raw[image['body_start']:image['body_end']] == image['body']
    assert image['body_sha256'] == hashlib.sha256(image['body']).hexdigest()
    assert min(elapsed) < min(reference) * .5


@pytest.mark.parametrize(('prefix', 'expected'), [
    (b'', True), (b'\r', True), (b'\n', True), (b'\r\n', True),
    (b'x', False), (b' ', False), (b'\t', False), (b'\x00', False),
])
def test_framing_search_preserves_zero_width_start_and_line_boundaries(prefix, expected):
    from tradingagents.strategies.data_sources.filing_evidence import _line_finditer
    raw = prefix + b'<TEXT>body\r</TEXT>'
    matches = list(_line_finditer(rb'<TEXT>', raw))
    assert [(match.start(), match.end()) for match in matches] == (
        [(len(prefix), len(prefix) + len(b'<TEXT>'))] if expected else [])
