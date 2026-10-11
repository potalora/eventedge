"""Offline PDF fixtures exercise actual native objects, not mocked extraction."""
import builtins
import hashlib
import io
import json
import binascii

import pytest
from pypdf import PdfWriter
from pypdf.generic import (ArrayObject, DecodedStreamObject, DictionaryObject,
                           NameObject, NumberObject)


def pdf_bytes(streams=(b'BT /F1 12 Tf 72 700 Td (Revenue grew.) Tj ET',), *,
              image=False, form=None, rotate=0, annotation=False, encrypted=False,
              compressed=False, acroform=False, resource_entries=None, image_updates=None):
    writer = PdfWriter()
    for content in streams:
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
            NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
        resources = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
        resources.update(resource_entries or {})
        page[NameObject('/Resources')] = resources
        if image:
            obj = DecodedStreamObject()
            obj.set_data(b'\x00\x00\x00')
            obj.update({NameObject('/Type'): NameObject('/XObject'), NameObject('/Subtype'): NameObject('/Image'),
                        NameObject('/Width'): NumberObject(1), NameObject('/Height'): NumberObject(1),
                        NameObject('/BitsPerComponent'): NumberObject(8), NameObject('/ColorSpace'): NameObject('/DeviceRGB')})
            obj.update(image_updates or {})
            resources[NameObject('/XObject')] = DictionaryObject({NameObject('/Im1'): writer._add_object(obj)})
            content += b' q 100 0 0 100 0 0 cm /Im1 Do Q'
        if form is not None:
            obj = DecodedStreamObject()
            obj.set_data(form)
            obj.update({NameObject('/Type'): NameObject('/XObject'), NameObject('/Subtype'): NameObject('/Form'),
                NameObject('/BBox'): ArrayObject([NumberObject(n) for n in (0, 0, 612, 792)]),
                NameObject('/Resources'): resources})
            resources[NameObject('/XObject')] = DictionaryObject({NameObject('/Fm1'): writer._add_object(obj)})
            content += b' /Fm1 Do'
        stream = DecodedStreamObject()
        stream.set_data(content)
        page[NameObject('/Contents')] = writer._add_object(stream.flate_encode() if compressed else stream)
        if rotate:
            page.rotate(rotate)
        if annotation:
            page[NameObject('/Annots')] = ArrayObject([writer._add_object(DictionaryObject({
                NameObject('/Type'): NameObject('/Annot'), NameObject('/Subtype'): NameObject('/Text'),
                NameObject('/Rect'): ArrayObject([NumberObject(0)] * 4)}))])
    if encrypted:
        writer.encrypt('secret')
    if acroform:
        writer.root_object[NameObject('/AcroForm')] = DictionaryObject({NameObject('/Fields'): ArrayObject()})
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def extract(body, **kwargs):
    from tradingagents.strategies.data_sources.filing_pdf import extract_pdf_evidence
    limits = dict(max_pdf_bytes=200_000, max_text_chars=10_000, max_pages=20, max_stream_bytes=20_000)
    limits.update(kwargs)
    return extract_pdf_evidence(body, **limits)


def test_complete_ordered_inventory_and_exact_text_provenance():
    body = pdf_bytes((b'BT /F1 12 Tf (First page.) Tj ET', b'BT /F1 12 Tf (Second page.) Tj ET'))
    result = extract(body)
    assert [page['page_number'] for page in result['pages']] == [1, 2]
    assert [page['text'] for page in result['pages']] == ['First page.', 'Second page.']
    assert result['decoded_sha256'] == hashlib.sha256(body).hexdigest()
    assert result['input_sha256'] == result['decoded_sha256']
    assert result['page_count'] == 2
    assert result['native_text_complete'] is True
    assert result['visual_meaning_verified'] is False
    for page in result['pages']:
        assert page['text_sha256'] == hashlib.sha256(page['text'].encode()).hexdigest()
    json.dumps(result, allow_nan=False)


def test_sec_uu_wrapper_preserves_encoded_and_decoded_hashes():
    body = pdf_bytes()
    uu = b'begin 644 filing.pdf\n' + b''.join(binascii.b2a_uu(body[i:i+45]) for i in range(0, len(body), 45)) + b'`\nend\n'
    wrapped = b'<PDF>\n' + uu + b'</PDF>\n'
    result = extract(wrapped)
    assert result['encoding'] == 'sec_pdf_uu'
    assert result['decoded_sha256'] == hashlib.sha256(body).hexdigest()
    assert result['input_sha256'] == hashlib.sha256(wrapped).hexdigest()


@pytest.mark.parametrize('body', [b'not a pdf', b'<PDF>\nbegin 644 x\nM!!!\nend\n</PDF>\n', b'<PDF>\n%PDF-1.7\n</PDF>junk'])
def test_malformed_or_incomplete_source_is_technical_failure(body):
    from tradingagents.strategies.data_sources.filing_pdf import FilingPDFError
    with pytest.raises(FilingPDFError):
        extract(body)


@pytest.mark.parametrize('overrides', [dict(max_pdf_bytes=10), dict(max_pages=1), dict(max_text_chars=3), dict(max_stream_bytes=10)])
def test_caps_fail_without_partial_envelopes(overrides):
    from tradingagents.strategies.data_sources.filing_pdf import FilingPDFError
    with pytest.raises(FilingPDFError, match='limit'):
        extract(pdf_bytes((b'BT /F1 12 Tf (One.) Tj ET', b'BT /F1 12 Tf (Two.) Tj ET')), **overrides)


def test_compressed_stream_expansion_is_bounded():
    from tradingagents.strategies.data_sources.filing_pdf import FilingPDFError
    with pytest.raises(FilingPDFError, match='limit'):
        extract(pdf_bytes((b' ' * 40_000,), compressed=True), max_stream_bytes=1000)


def test_raster_only_page_has_complete_native_inventory_and_visual_evidence():
    result = extract(pdf_bytes((b'',), image=True))
    assert result['pages'][0]['text'] == ''
    assert result['native_text_complete'] is True
    assert 'raster_image' in result['findings']
    assert 'image_only_page' in result['findings']


def test_true_blank_page_is_recorded():
    result = extract(pdf_bytes((b'',)))
    assert result['pages'][0]['text'] == ''
    assert 'blank_page' in result['pages'][0]['findings']


def test_vector_table_lines_rotated_text_and_annotations_are_not_silently_dropped():
    result = extract(pdf_bytes((b'BT /F1 12 Tf (Cell one) Tj 100 0 Td (Cell two) Tj ET 0 0 100 100 re S',), rotate=90, annotation=True))
    assert 'vector_graphics' in result['findings']
    assert 'positioned_text_layout' in result['findings']
    assert 'rotated_page' in result['findings']
    assert 'annotations' in result['findings']
    assert 'Cell one' in result['text'] and 'Cell two' in result['text']


def test_forms_are_recursed_and_interactive_form_is_flagged():
    result = extract(pdf_bytes((b'',), form=b'BT /F1 12 Tf (Inside form) Tj ET', acroform=True))
    assert 'Inside form' in result['text']
    assert 'form_xobject' in result['findings']
    assert 'interactive_form' in result['findings']


@pytest.mark.parametrize('content', [b'BT /F1 12 Tf <00> Tj ET', b'BT /Missing 12 Tf (lost text) Tj ET', b'BT /F1 12 Tf (broken) Tj'])
def test_missing_font_encoding_loss_and_unbalanced_text_fail(content):
    from tradingagents.strategies.data_sources.filing_pdf import FilingPDFError
    with pytest.raises(FilingPDFError):
        extract(pdf_bytes((content,)))


def test_encrypted_and_truncated_pdf_are_technical_failures():
    from tradingagents.strategies.data_sources.filing_pdf import FilingPDFError
    for body in (pdf_bytes(encrypted=True), pdf_bytes()[:-20]):
        with pytest.raises(FilingPDFError):
            extract(body)


def test_deadline_callback_propagates_original_exception_during_extraction():
    calls = 0
    failure = TimeoutError('deadline test')
    def check():
        nonlocal calls
        calls += 1
        if calls == 8:
            raise failure
    with pytest.raises(TimeoutError) as info:
        extract(pdf_bytes(), check=check)
    assert info.value is failure
    assert calls == 8


def test_missing_dependency_is_safe_technical_failure(monkeypatch):
    from tradingagents.strategies.data_sources.filing_pdf import FilingPDFError
    original = builtins.__import__
    def unavailable(name, *args, **kwargs):
        if name == 'pypdf':
            raise ImportError('no dependency')
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', unavailable)
    with pytest.raises(FilingPDFError, match='dependency_unavailable'):
        extract(pdf_bytes())


def test_supported_subset_is_ordinary_standard_font_single_column_text():
    result = extract(pdf_bytes((b'BT /F1 12 Tf 72 700 Td (A plain first line.) Tj 0 -18 Td (A plain second line.) Tj ET',)))
    assert result['supported_text_layout'] is True
    assert result['findings'] == []
    assert result['support_limitations'] == []
    assert result['visual_meaning_verified'] is False


def test_positioned_text_without_table_borders_is_observed():
    result = extract(pdf_bytes((b'BT /F1 12 Tf 72 700 Td (left) Tj 100 0 Td (right) Tj ET',)))
    assert result['supported_text_layout'] is False
    assert 'positioned_text_layout' in result['findings']


def test_text_rendering_and_custom_spacing_do_not_qualify_for_plain_subset():
    result = extract(pdf_bytes((b'BT /F1 12 Tf 72 700 Td 3 Tr (hidden) Tj ET',)))
    assert not result['supported_text_layout']
    assert 'nondefault_text_rendering' in result['findings']


def test_declared_page_census_cannot_omit_or_invent_pages():
    from tradingagents.strategies.data_sources.filing_pdf import FilingPDFError
    body = pdf_bytes().replace(b'/Count 1', b'/Count 2')
    with pytest.raises(FilingPDFError, match='page_count_mismatch'):
        extract(body)


def test_invalid_content_object_is_not_proven_blank():
    from tradingagents.strategies.data_sources.filing_pdf import FilingPDFError
    body = pdf_bytes().replace(b'/Contents 6 0 R', b'/Contents 1 0 R')
    with pytest.raises(FilingPDFError):
        extract(body)


def test_silent_nonstandard_font_encoding_is_technical_failure():
    from tradingagents.strategies.data_sources.filing_pdf import FilingPDFError
    body = pdf_bytes().replace(b'/Helvetica', b'/OtherFont')
    with pytest.raises(FilingPDFError, match='unverified_font_encoding'):
        extract(body)


def test_unknown_content_operator_fails_instead_of_visual_deferral():
    from tradingagents.strategies.data_sources.filing_pdf import FilingPDFError
    with pytest.raises(FilingPDFError, match='unknown_content_operator'):
        extract(pdf_bytes((b'BT /F1 12 Tf (valid) Tj ET bogus',)))


@pytest.mark.parametrize('content', [b'BT /F1 12 Tf 7 Tj ET', b'BT /F1 12 Tf [(text) /NotText] TJ ET', b'BT /F1 12 Tf (one) (two) Tj ET'])
def test_malformed_text_operator_operands_fail(content):
    from tradingagents.strategies.data_sources.filing_pdf import FilingPDFError
    with pytest.raises(FilingPDFError, match='invalid_text_operands'):
        extract(pdf_bytes((content,)))


def test_all_pages_contribute_to_aggregate_stream_cap():
    from tradingagents.strategies.data_sources.filing_pdf import FilingPDFError
    with pytest.raises(FilingPDFError, match='stream_limit'):
        extract(pdf_bytes((b' ' * 700, b' ' * 700)), max_stream_bytes=1000)


def test_form_streams_contribute_to_content_inventory():
    result = extract(pdf_bytes((b'BT /F1 12 Tf (outside) Tj ET',), form=b'BT /F1 12 Tf (inside) Tj ET'))
    assert len(result['pages'][0]['streams']) == 2
    assert result['pages'][0]['observations']['native_text_operations'] == 2
    assert result['pages'][0]['supported_text_layout'] is False


def test_zero_page_pdf_and_unexplained_empty_operator_stream_fail():
    from tradingagents.strategies.data_sources.filing_pdf import FilingPDFError
    for body in (pdf_bytes(()), pdf_bytes((b'q Q',))):
        with pytest.raises(FilingPDFError):
            extract(body)


def test_scanned_image_objects_are_hashed_and_bounded():
    from tradingagents.strategies.data_sources.filing_pdf import FilingPDFError
    result = extract(pdf_bytes((b'',), image=True))
    streams = result['pages'][0]['streams']
    assert any(stream['kind'] == 'image' for stream in streams)
    with pytest.raises(FilingPDFError, match='stream_limit'):
        extract(pdf_bytes((b'',), image=True), max_stream_bytes=33)


def test_library_configuration_is_restored_after_failure():
    from pypdf import get_configuration
    from tradingagents.strategies.data_sources.filing_pdf import FilingPDFError
    original = get_configuration()
    with pytest.raises(FilingPDFError):
        extract(pdf_bytes(), max_stream_bytes=10)
    assert get_configuration() is original


def test_sec_binary_wrapper_and_plain_uu_are_supported():
    body = pdf_bytes()
    assert extract(b'<PDF>\n' + body + b'</PDF>\n')['decoded_sha256'] == hashlib.sha256(body).hexdigest()
    uu = b'begin 644 filing.pdf\n' + b''.join(binascii.b2a_uu(body[i:i+45]) for i in range(0, len(body), 45)) + b'`\nend\n'
    assert extract(uu)['encoding'] == 'uu'


@pytest.mark.parametrize('value', [0, -1, True, 1.5, '100'])
def test_invalid_caller_limits_fail_closed(value):
    from tradingagents.strategies.data_sources.filing_pdf import FilingPDFError
    with pytest.raises(FilingPDFError, match='invalid_limits'):
        extract(pdf_bytes(), max_pages=value)


def test_sec_uu_space_trimmed_rows_and_empty_terminator_are_validated():
    body = pdf_bytes()
    uu = b'begin 644 filing.pdf\n' + b''.join(binascii.b2a_uu(body[i:i+45]).rstrip(b' \n') + b'\n' for i in range(0, len(body), 45)) + b'\nend\n'
    result = extract(b'<PDF>\n' + uu + b'</PDF>\n')
    assert result['decoded_sha256'] == hashlib.sha256(body).hexdigest()
    from tradingagents.strategies.data_sources.filing_pdf import FilingPDFError
    # The unwrapped UU contract requires a standard explicit zero-length line.
    with pytest.raises(FilingPDFError, match='uu_invalid'):
        extract(uu)


def test_blank_page_does_not_disqualify_an_otherwise_plain_document():
    result = extract(pdf_bytes((b'BT /F1 12 Tf 72 700 Td (Plain text.) Tj ET', b'')))
    assert result['supported_text_layout'] is True
    assert result['pages'][1]['supported_text_layout'] is True


def test_sec_uu_ignored_padding_sextet_does_not_change_decoded_bytes():
    body = pdf_bytes()
    while len(body) % 3 != 2:
        body += b'\n'
    lines = [binascii.b2a_uu(body[i:i+45]).rstrip(b'\n') for i in range(0, len(body), 45)]
    lines[-1] = lines[-1][:-1] + b'!'
    wrapped = b'<PDF>\nbegin 644 filing.pdf\n' + b'\n'.join(lines) + b'\n\nend\n</PDF>'
    assert extract(wrapped)['decoded_sha256'] == hashlib.sha256(body).hexdigest()


def test_plain_subset_cannot_accept_malformed_positioning_operators():
    from tradingagents.strategies.data_sources.filing_pdf import FilingPDFError
    for body in (b'BT /F1 12 Tf 72 700 99 Td (plain) Tj ET', b'BT 1 /F1 12 Tf 72 700 Td (plain) Tj ET'):
        with pytest.raises(FilingPDFError):
            extract(pdf_bytes((body,)))


def test_fully_inventoried_graphics_only_has_no_spurious_text_capability_gap():
    image = extract(pdf_bytes((b'',), image=True))
    assert image['support_limitations'] == []
    assert 'image_only_page' in image['findings']
    vector = extract(pdf_bytes((b'0 0 30 30 re f',)))
    assert vector['support_limitations'] == []
    assert 'vector_only_page' in vector['findings']


def test_image_does_not_erase_unsupported_native_text_operator_capability():
    mixed = extract(pdf_bytes(image=True))
    assert 'plain_text_operator_subset_exceeded' in mixed['support_limitations']
    assert 'raster_image' in mixed['findings']


@pytest.mark.parametrize('content', [
    b'/Missing sh', b'0 0 m 1 1 l /Missing gs S',
    b'/Missing cs 0 sc 0 0 1 1 re f', b'/Missing CS 0 SC 0 0 m 1 1 l S',
    b'/Missing scn 0 0 1 1 re f', b'/Tag /Missing BDC 0 0 1 1 re f EMC',
])
def test_missing_visual_resources_fail_technically(content):
    from tradingagents.strategies.data_sources.filing_pdf import FilingPDFError
    with pytest.raises(FilingPDFError, match='(missing|invalid|unsupported)_'):
        extract(pdf_bytes((content,)))


@pytest.mark.parametrize('content', [
    b'17 sh', b'(not a name) gs 0 0 1 1 re f', b'1 2 cs 0 0 1 1 re f',
    b'/DeviceRGB cs 0 0 sc 0 0 1 1 re f', b'0 0 0 0 SC 0 0 1 1 re f',
    b'/DeviceRGB CS /NotNumber 0 0 SCN 0 0 1 1 re f',
    b'/Bad ri 0 0 1 1 re f', b'[0 0] 0 d 0 0 m 1 1 l S',
    b'/Tag 12 BDC 0 0 1 1 re f EMC', b'/Tag /Prop DP 0 0 1 1 re f',
])
def test_malformed_visual_operator_operands_fail_technically(content):
    from tradingagents.strategies.data_sources.filing_pdf import FilingPDFError
    with pytest.raises(FilingPDFError):
        extract(pdf_bytes((content,)))


def test_valid_simple_graphics_resources_remain_complete_visual_evidence():
    resources = {
        NameObject('/ExtGState'): DictionaryObject({NameObject('/Checked'): DictionaryObject({NameObject('/LW'): NumberObject(2)})}),
        NameObject('/ColorSpace'): DictionaryObject({NameObject('/RGB'): NameObject('/DeviceRGB')}),
        NameObject('/Properties'): DictionaryObject({NameObject('/P'): DictionaryObject({NameObject('/MCID'): NumberObject(0)})}),
    }
    result = extract(pdf_bytes((b'/Checked gs /RGB cs 1 0 0 sc /Tag /P BDC 0 0 20 20 re f EMC',), resource_entries=resources))
    assert result['native_text_complete'] is True
    assert result['support_limitations'] == []
    assert 'vector_only_page' in result['findings']


@pytest.mark.parametrize('category,content', [
    ('/ExtGState', b'/Bad gs 0 0 1 1 re f'),
    ('/Shading', b'/Bad sh'),
    ('/ColorSpace', b'/Bad cs 0 sc 0 0 1 1 re f'),
    ('/Properties', b'/Tag /Bad BDC 0 0 1 1 re f EMC'),
])
def test_wrong_visual_resource_object_types_fail(category, content):
    from tradingagents.strategies.data_sources.filing_pdf import FilingPDFError
    resources = {NameObject(category): DictionaryObject({NameObject('/Bad'): NumberObject(12)})}
    with pytest.raises(FilingPDFError):
        extract(pdf_bytes((content,), resource_entries=resources))


def test_complex_uninventoried_graphics_capabilities_are_technical_failures():
    from tradingagents.strategies.data_sources.filing_pdf import FilingPDFError
    for resources, content in (
        ({NameObject('/Shading'): DictionaryObject({NameObject('/S'): DictionaryObject({NameObject('/ShadingType'): NumberObject(2)})})}, b'/S sh'),
        ({NameObject('/ExtGState'): DictionaryObject({NameObject('/G'): DictionaryObject({NameObject('/Font'): ArrayObject([NumberObject(1), NumberObject(12)])})})}, b'/G gs 0 0 1 1 re f'),
        ({NameObject('/ColorSpace'): DictionaryObject({NameObject('/C'): ArrayObject([NameObject('/ICCBased'), DictionaryObject()])})}, b'/C cs 0 sc 0 0 1 1 re f'),
    ):
        with pytest.raises(FilingPDFError):
            extract(pdf_bytes((content,), resource_entries=resources))


@pytest.mark.parametrize('updates', [
    {NameObject('/ColorSpace'): NameObject('/Missing')},
    {NameObject('/BitsPerComponent'): NumberObject(3)},
    {NameObject('/Width'): NumberObject(2)},
    {NameObject('/SMask'): NumberObject(7)},
    {NameObject('/Mask'): ArrayObject([NumberObject(0)])},
    {NameObject('/Filter'): NameObject('/DCTDecode')},
])
def test_malformed_or_opaque_images_are_not_valid_scan_evidence(updates):
    from tradingagents.strategies.data_sources.filing_pdf import FilingPDFError
    with pytest.raises(FilingPDFError):
        extract(pdf_bytes((b'',), image=True, image_updates=updates))


def test_explicit_standard_font_width_overrides_are_capability_gap():
    writer = PdfWriter()
    reader = __import__('pypdf').PdfReader(io.BytesIO(pdf_bytes((b'BT /F1 12 Tf 72 700 Td (AAAAAA) Tj ET',))))
    font = reader.pages[0]['/Resources']['/Font']['/F1'].get_object()
    font.update({NameObject('/FirstChar'): NumberObject(65), NameObject('/LastChar'): NumberObject(65),
                 NameObject('/Widths'): ArrayObject([NumberObject(10000)])})
    writer.add_page(reader.pages[0])
    out = io.BytesIO()
    writer.write(out)
    result = extract(out.getvalue())
    assert not result['supported_text_layout']
    assert 'font_geometry_not_validated' in result['support_limitations']


def test_graphics_color_state_is_restored_across_save_restore():
    result = extract(pdf_bytes((b'/DeviceRGB cs q /DeviceGray cs 0 sc Q 1 0 0 sc 0 0 10 10 re f',)))
    assert result['native_text_complete'] is True
    assert 'vector_only_page' in result['findings']


def test_missing_graphics_resource_inside_form_is_failure():
    from tradingagents.strategies.data_sources.filing_pdf import FilingPDFError
    with pytest.raises(FilingPDFError, match='missing_visual_resource'):
        extract(pdf_bytes((b'',), form=b'/Missing gs 0 0 10 10 re f'))


def test_invalid_default_color_resource_is_not_ignored_by_implicit_gray():
    from tradingagents.strategies.data_sources.filing_pdf import FilingPDFError
    resources = {NameObject('/ColorSpace'): DictionaryObject({NameObject('/DefaultGray'): NameObject('/Missing')})}
    with pytest.raises(FilingPDFError):
        extract(pdf_bytes((b'0 0 10 10 re f',), resource_entries=resources))


def _rewrite_pdf(body, change):
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(body))
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    change(writer)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


@pytest.mark.parametrize('key', ['/AcroForm', '/OCProperties', '/Names', '/AF'])
def test_malformed_root_visual_objects_are_technical_failures(key):
    from tradingagents.strategies.data_sources.filing_pdf import FilingPDFError
    body = _rewrite_pdf(pdf_bytes(), lambda writer: writer.root_object.update({NameObject(key): NumberObject(123)}))
    with pytest.raises(FilingPDFError):
        extract(body)


def test_malformed_annotation_appearance_is_technical_failure():
    from tradingagents.strategies.data_sources.filing_pdf import FilingPDFError
    def change(writer):
        annotation = writer.pages[0]['/Annots'][0].get_object()
        annotation[NameObject('/AP')] = DictionaryObject({NameObject('/N'): NumberObject(123)})
    body = _rewrite_pdf(pdf_bytes(annotation=True), change)
    with pytest.raises(FilingPDFError):
        extract(body)


def test_uninventoried_optional_content_and_form_fields_fail_as_capabilities():
    from tradingagents.strategies.data_sources.filing_pdf import FilingPDFError
    for key, value in (
        ('/AcroForm', DictionaryObject({NameObject('/Fields'): ArrayObject([DictionaryObject()])})),
        ('/OCProperties', DictionaryObject()),
        ('/Names', DictionaryObject()),
    ):
        body = _rewrite_pdf(pdf_bytes(), lambda writer: writer.root_object.update({NameObject(key): value}))
        with pytest.raises(FilingPDFError):
            extract(body)


@pytest.mark.parametrize('content', [b'0.5 J 0 0 10 10 re f', b'-1 w 0 0 10 10 re f', b'200 i 0 0 10 10 re f'])
def test_invalid_graphics_state_parameter_ranges_fail(content):
    from tradingagents.strategies.data_sources.filing_pdf import FilingPDFError
    with pytest.raises(FilingPDFError):
        extract(pdf_bytes((content,)))
