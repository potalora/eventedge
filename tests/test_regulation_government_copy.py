"""Government copies must preserve the full identified notice and all emails."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from tradingagents.strategies.data_sources.regulation_government_copy import (
    government_copy_plan, extract_government_copy,
)

FIXTURES = Path(__file__).parent / 'fixtures'
DETAIL = (FIXTURES / 'regulation_detail_wv.json').read_bytes()
MAC = (FIXTURES / 'regulation_govinfo_wv_mac.htm').read_bytes()
NATIVE = (FIXTURES / 'regulation_govinfo_wv_native.htm').read_bytes()
URL = 'https://www.govinfo.gov/content/pkg/FR-2026-10-07/html/2026-20611.htm'


def altered_detail(field, value):
    item = json.loads(DETAIL)
    item['data']['attributes'][field] = value
    return json.dumps(item).encode()


def test_plan_binds_original_metadata_to_one_canonical_publisher_url():
    plan = government_copy_plan(DETAIL)
    assert plan == {'url': URL, 'identity': {
        'document_id': 'EPA-R03-OAR-2026-6205-0001',
        'fr_document_number': '2026-20611', 'publication_date': '2026-10-07',
        'docket_id': 'EPA-R03-OAR-2026-6205', 'agency_id': 'EPA',
        'document_type': 'Proposed Rule', 'first_page': 64133, 'last_page': 64135,
        'page_count': 3, 'cfr_part': '40 CFR Part 62'}}


def test_full_pre_text_preserves_whitespace_sections_addresses_and_signature():
    value = extract_government_copy(DETAIL, MAC)
    assert value['url'] == URL
    text = value['text']
    assert text.startswith('\n[Federal Register Volume 91, Number 193')
    assert '    The CAA requires State regulatory agencies' in text
    assert 'He.Yongtian@epa.gov' in text
    assert 'Stankunas.Krystal@epa.gov' in text
    assert 'Air & Radiation Division' in text
    assert 'I. Background\n' in text
    assert 'J. Executive Order 13175:' in text
    assert 'Amy Van Blarcom-Lackey,\nRegional Administrator, Region III.\n' in text
    assert text.endswith('BILLING CODE 6560-50-P\n\n\n')
    assert '<' not in text and '[email' not in text
    assert len(value['transformations']) == 3
    for change in value['transformations'][:2]:
        assert text[change['text_codepoint_start']:change['text_codepoint_end']] == change['email']
        assert MAC.decode()[change['source_codepoint_start']:change['source_codepoint_end']].startswith('<a href="/cdn-cgi/l/email-protection#')


def test_native_nonce_variants_have_equal_complete_decoded_text():
    assert MAC != NATIVE
    mac, native = extract_government_copy(DETAIL, MAC), extract_government_copy(DETAIL, NATIVE)
    assert mac['text'] == native['text']
    assert mac['identity'] == native['identity']
    assert mac['transformations'] != native['transformations']


@pytest.mark.parametrize('field,value', [
    ('frDocNum', '../2026-20611'), ('frDocNum', '2025-20611'),
    ('postedDate', '2026-10-07'), ('postedDate', '2026-02-30T04:00:00Z'),
    ('docketId', 'EPA-OTHER'), ('agencyId', 'FDA'), ('documentType', 'Rule'),
    ('startEndPage', '64135 - 64133'), ('pageCount', 2),
    ('cfrPart', None), ('withdrawn', True), ('restrictReason', 'restricted'),
    ('fileFormats', [{'fileUrl': 'https://downloads.regulations.gov/EPA-R03-OAR-2026-6205-0001/content.pdf', 'format': 'pdf', 'size': 10}]),
])
def test_unbound_or_unsupported_metadata_cannot_make_a_copy(field, value):
    with pytest.raises(ValueError, match='(?:invalid|unsupported)_regulation_'):
        government_copy_plan(altered_detail(field, value))


def test_duplicate_detail_keys_are_rejected():
    with pytest.raises(ValueError, match='invalid_regulation_'):
        government_copy_plan(DETAIL.replace(b'"frDocNum" : "2026-20611"', b'"frDocNum":"2026-20611","frDocNum":"2026-20611"'))


@pytest.mark.parametrize('old,new', [
    (b'[FR Doc No: 2026-20611]', b'[FR Doc No: 2026-20612]'),
    (b'[Pages 64133-64135]', b'[Pages 64132-64135]'),
    (b'[Proposed Rules]', b'[Rules and Regulations]'),
    (b'Wednesday, October 7, 2026', b'Thursday, October 7, 2026'),
    (b'[EPA-R03-OAR-2026-6205;', b'[EPA-R03-OAR-2026-6206;'),
    (b'AGENCY: Environmental Protection Agency (EPA).', b'AGENCY: Other Agency.'),
    (b'ACTION: Proposed rule.', b'ACTION: Final rule.'),
    (b'[[Page 64134]]', b'[[Page 64135]]'),
    (b'[[Page 64135]]', b''),
    (b'[FR Doc. 2026-20611 Filed', b'[FR Doc. 2026-20612 Filed'),
    (b'BILLING CODE 6560-50-P', b''),
    (b'</pre>', b'\n[FR Doc No: 2026-20611]\n</pre>'),
])
def test_identity_and_notice_boundaries_cannot_be_changed(old, new):
    with pytest.raises(ValueError, match='invalid_regulation_'):
        extract_government_copy(DETAIL, MAC.replace(old, new))


@pytest.mark.parametrize('insert', [
    b'<img src="chart.png">', b'<table><tr><td>data</td></tr></table>',
    b'<script>moreContent()</script>', b'<style>pre{display:none}</style>',
    b'<div>text</div>', b'<!-- hidden -->', b'<?dynamic?>',
    b'[GRAPHIC] [TIFF OMITTED] TP07OC26.001', b'[EQUATION OMITTED]',
    b'&unknown;', b'&#0;', b'\x00',
])
def test_unsupported_rendering_or_tabular_material_fails_closed(insert):
    with pytest.raises(ValueError, match='(?:invalid|unsupported)_regulation_'):
        extract_government_copy(DETAIL, MAC.replace(b'SUMMARY:', insert + b'\nSUMMARY:', 1))


@pytest.mark.parametrize('old,new', [
    (b'c28aa7ec9badaca5b6aba3ac82a7b2a3eca5adb4', b'00aa'),
    (b'data-cfemail=', b'data-other='),
    (b'[email&#160;protected]', b'[email protected]'),
    (b'5c5dd728/cloudflare-static/email-decode.min.js', b'unknown/script.js'),
    (b'<a href="http://Regulations.gov">', b'<a onclick="hide()" href="http://Regulations.gov">'),
    (b'<a href="http://Regulations.gov">', b'<a href="javascript:hide()">'),
    (b'</html>', b''), (b'</body>', b'trailing notice</body>'),
])
def test_finite_transport_transformations_cannot_hide_text_or_code(old, new):
    with pytest.raises(ValueError, match='(?:invalid|unsupported)_regulation_'):
        extract_government_copy(DETAIL, MAC.replace(old, new))


def test_expired_check_propagates_before_parsing():
    def expired():
        raise TimeoutError('expired')
    with pytest.raises(TimeoutError, match='expired'):
        extract_government_copy(DETAIL, MAC, check=expired)


@pytest.mark.parametrize('old,new', [
    (b'\nSUMMARY:', b'\nABSTRACT:'),
    (b'\nDATES:', b'\nDEADLINE:'),
    (b'\nADDRESSES:', b'\nADDRESSES: duplicate\nADDRESSES:'),
    (b'\nSUPPLEMENTARY INFORMATION:', b'\nOTHER INFORMATION:'),
    (b'[[Page 64134]]', b'Inline [[Page 64134]] marker'),
    (b'\nSUMMARY:', b'\n[UNKNOWN OMITTED]\nSUMMARY:'),
])
def test_required_sections_and_page_lines_do_not_accept_partial_notice_or_omitted_material(old, new):
    with pytest.raises(ValueError, match='(?:invalid|unsupported)_regulation_'):
        extract_government_copy(DETAIL, MAC.replace(old, new))


def test_ambiguous_original_formats_do_not_select_a_government_copy():
    item = json.loads(DETAIL)
    formats = item['data']['attributes']['fileFormats']
    duplicate = deepcopy(formats[1])
    duplicate['fileUrl'] = duplicate['fileUrl'].replace('content.html', 'other.html')
    formats.append(duplicate)
    with pytest.raises(ValueError, match='invalid_regulation_'):
        government_copy_plan(json.dumps(item).encode())


def test_body_byte_cap_is_enforced_before_parsing():
    with pytest.raises(ValueError, match='invalid_regulation_'):
        extract_government_copy(DETAIL, b'x' * (16 * 1024**2 + 1))


def test_entity_decoding_preserves_supported_visible_characters():
    raw = MAC.replace(b'\nI. Background', b'\nExample: &amp; &#169; &lt; 2 &gt; 1.\nI. Background')
    result = extract_government_copy(DETAIL, raw)
    assert '\nExample: & © < 2 > 1.\nI. Background' in result['text']


def test_literal_preformatted_text_tables_keep_every_character_and_footnote():
    detail = (FIXTURES / 'regulation_detail_truckee.json').read_bytes()
    body = (FIXTURES / 'regulation_govinfo_truckee.htm').read_bytes()
    result = extract_government_copy(detail, body)
    assert result['representation'] == 'preserves_preformatted_layout'
    table = '''                 Table 5--Truckee Meadows PM10 Monitors
------------------------------------------------------------------------
           Site name                 Site ID           Street address
------------------------------------------------------------------------
Toll..........................        32-031-0025  684A State Route 341,
                                                    Reno, NV.
Reno 4........................        32-031-0031  1260-A Stewart St.,
                                                    Reno, NV.
Sparks........................        32-031-1005  750 4th St, Sparks,
                                                    NV.
------------------------------------------------------------------------
Source: Annual Network Plan, Northern Nevada Public Health, June 24,
  2025.'''
    assert table in result['text']
    assert 'PM10' in result['text'] and 'NOX: Nitrogen Oxides' in result['text']
    assert '• Is not a significant regulatory action' in result['text']
    assert result['notice_metadata']['action'] == 'Proposed rule.'
    # Exact source grid/footnotes, including a separate table's special flags.
    assert 'Plumb-Kit (32-031-0030).......................................      0.0      0.0    * 0.0    * 0.0' in result['text']
    assert '\\b\\.......................................' in result['text']


def test_original_proposed_rules_classification_retains_actual_notice_action_and_rin():
    detail = (FIXTURES / 'regulation_detail_pm25.json').read_bytes()
    body = (FIXTURES / 'regulation_govinfo_pm25.htm').read_bytes()
    result = extract_government_copy(detail, body)
    assert result['identity']['document_type'] == 'Proposed Rule'
    assert result['notice_metadata']['action'] == 'Notice of availability; opportunity for public comment.'
    assert result['notice_metadata']['rin'] == '2060-AW33'
    assert 'PM2.5' in result['text']
    assert '      EPA regional office contacts               Affected states\n' in result['text']
    assert 'EPA Region 10, Angeles Herrera,          AK, ID, OR, WA.\n' in result['text']
    for change in result['transformations']:
        if change['kind'] == 'govinfo_inf_notation_v1':
            assert result['text'][change['text_codepoint_start']:change['text_codepoint_end']] == change['value']
            assert body.decode()[change['source_codepoint_start']:change['source_codepoint_end']] == '<INF>' + change['value'] + '</INF>'


@pytest.mark.parametrize('insert', [b'Company    Amount\nA          100', b'Col1 | Col2\nA | 100', b'Company\tAmount\nA\t100'])
def test_literal_ascii_table_grid_is_preserved_without_inferred_or_missing_cells(insert):
    result = extract_government_copy(DETAIL, MAC.replace(b'SUMMARY:', insert+b'\nSUMMARY:'))
    assert insert.decode() in result['text']


@pytest.mark.parametrize('old,new', [
    (b'<INF>2.5</INF>', b'<INF style="display:none">2.5</INF>'),
    (b'<INF>2.5</INF>', b'<INF>unknown</INF>'),
    (b'<bullet>', b'<bullet hidden="yes">'),
    (b'RIN 2060-AW33', b'RIN arbitrary'),
])
def test_unknown_notation_or_rin_variants_remain_failures(old, new):
    detail = (FIXTURES / 'regulation_detail_pm25.json').read_bytes()
    body = (FIXTURES / 'regulation_govinfo_pm25.htm').read_bytes()
    with pytest.raises(ValueError, match='(?:invalid|unsupported)_regulation_'):
        extract_government_copy(detail, body.replace(old, new))


@pytest.mark.parametrize('old,new', [
    (b'[[Page 62375]]', b''),
    (b'[FR Doc. 2026-20102 Filed', b'[FR Doc. 2026-20103 Filed'),
    (b'</pre>', b'[GRAPHIC] [TIFF OMITTED] TP01OC26.001</pre>'),
])
def test_table_notice_must_still_have_complete_pages_footer_and_no_omitted_asset(old, new):
    detail = (FIXTURES / 'regulation_detail_truckee.json').read_bytes()
    body = (FIXTURES / 'regulation_govinfo_truckee.htm').read_bytes()
    with pytest.raises(ValueError, match='(?:invalid|unsupported)_regulation_'):
        extract_government_copy(detail, body.replace(old, new))
