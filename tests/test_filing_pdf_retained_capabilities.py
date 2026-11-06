"""Finite declarations reproduced from retained PDFs; no source text fixtures."""
import hashlib

import pytest
from pypdf.generic import (ArrayObject, DecodedStreamObject, DictionaryObject,
                           NameObject, NumberObject, TextStringObject)
from test_filing_pdf import pdf_bytes, _rewrite_pdf, extract as native_extract
from test_filing_pdf_fonts import cidfont, font
from test_filing_pdf_visual import extract as visual_extract


def artifact_pdf(properties=b'/BBox [533.304 740.9041 564 757.512]', tag=b'/Artifact'):
    content = (tag + b' << /Type /Pagination /Subtype /Header /Attached [/Top] '
               + properties + b' >> BDC BT /F1 12 Tf 72 700 Td (Header) Tj ET EMC')
    return pdf_bytes((content,), image=True)


def test_artifact_bbox_keeps_native_text_and_exact_structural_operands():
    # Rejecting the standard Artifact bounding box loses otherwise supported
    # source declarations; accepting it must not erase its provenance.
    body = artifact_pdf()
    native = native_extract(body)
    assert native['text'] == 'Header'
    assert native['native_text_complete'] is True
    assert native['visual_meaning_verified'] is False
    visual = visual_extract(body)
    marked = next(op for op in visual['pages'][0]['operations'] if op['operator'] == 'BDC')
    assert marked['operands'] == ['/Artifact', {'/Type': '/Pagination', '/Subtype': '/Header',
        '/Attached': ['/Top'], '/BBox': [533.304, 740.9041, 564, 757.512]}]
    assert marked['content_sha256'] == visual['pages'][0]['content_sha256']
    assert visual['native_text_complete'] is False


@pytest.mark.parametrize('properties,tag', [
    (b'/BBox [0 0 1]', b'/Artifact'),
    (b'/BBox [0 0 1 1 2]', b'/Artifact'),
    (b'/BBox [0 0 (1) 1]', b'/Artifact'),
    (b'/BBox [0 0 true 1]', b'/Artifact'),
    (b'/BBox [2 0 1 1]', b'/Artifact'),
    (b'/BBox [0 2 1 1]', b'/Artifact'),
    (b'/BBox [0 0 1 1] /ActualText (Replacement)', b'/Artifact'),
    (b'/BBox [0 0 1 1]', b'/Span'),
])
@pytest.mark.parametrize('extractor', [native_extract, visual_extract])
def test_invalid_or_semantic_marked_properties_remain_failures(properties, tag, extractor):
    with pytest.raises(ValueError, match='marked_properties'):
        extractor(artifact_pdf(properties, tag))


def cidset_pdf(data=b'\xc0', *, change=None):
    def mutate(writer):
        value = font() if change == 'simple_font' else cidfont()
        descriptor = (value['/FontDescriptor'] if change == 'simple_font'
                      else value['/DescendantFonts'][0]['/FontDescriptor'])
        stream = DecodedStreamObject(); stream.set_data(data)
        stream = stream.flate_encode()
        if change == 'truncated':
            stream._data = stream._data[:-1]
        if change == 'external':
            stream[NameObject('/F')] = TextStringObject('external.bin')
        if change == 'wrong_dependency':
            stream[NameObject('/Length1')] = NumberObject(1)
        descriptor[NameObject('/CIDSet')] = (ArrayObject([NumberObject(192)])
            if change == 'nonstream' else stream)
        writer.pages[0]['/Resources']['/Font'][NameObject('/F1')] = writer._add_object(value)
    return _rewrite_pdf(pdf_bytes((b'BT /F1 12 Tf <0001> Tj ET',), image=True), mutate)


def test_structural_cidset_is_bounded_and_preserves_stream_provenance():
    # CIDSet is a declared CID bitset, not a Unicode or font-program proof.
    body = cidset_pdf()
    receipt = visual_extract(body)
    declaration = receipt['pages'][0]['resources']['/Font']['/F1']['/DescendantFonts'][0]
    stream = declaration['/FontDescriptor']['/CIDSet']['stream']
    assert stream['decoded_sha256'] == hashlib.sha256(b'\xc0').hexdigest()
    assert stream['decoded_bytes'] == 1
    assert stream['encoded_bytes'] > 1
    assert receipt['native_unicode'] == 'unverified'
    assert receipt['native_text_complete'] is False
    assert receipt['visual_presence'] is True


@pytest.mark.parametrize('data,change', [
    (b'', None), (b'\x80' * 8193, None), (b'\xc0', 'nonstream'),
    (b'\xc0', 'external'), (b'\xc0', 'wrong_dependency'),
    (b'\xc0', 'truncated'), (b'\xc0', 'simple_font'),
])
def test_invalid_cidset_declaration_cannot_become_visual_evidence(data, change):
    with pytest.raises(ValueError):
        visual_extract(cidset_pdf(data, change=change))


def test_cidset_decode_consumes_existing_shared_stream_budget():
    body = cidset_pdf(bytes(8192))
    with pytest.raises(ValueError, match='limit'):
        visual_extract(body, max_stream_bytes=8191)
