"""A CIDSet is source evidence, never permission to infer missing glyphs."""
import hashlib

import pytest
from pypdf.generic import DecodedStreamObject, NameObject, NumberObject

from test_filing_pdf_fonts import build, cidfont, font
from test_filing_pdf_retained_capabilities import cidset_pdf
from test_filing_pdf import extract
from tradingagents.strategies.data_sources.filing_pdf_fonts import FontError, build_font


def declared_font(payload=b'\xc0'):
    value = cidfont()
    stream = DecodedStreamObject()
    stream.set_data(payload)
    value['/DescendantFonts'][0]['/FontDescriptor'][NameObject('/CIDSet')] = stream
    return value


def test_native_cidset_preserves_declared_bytes_and_checks_used_membership():
    decoder = build(declared_font())
    assert decoder.decode(b'\x00\x01')['text'] == 'A'
    assert decoder.receipt['cid_set'] == {
        'sha256': hashlib.sha256(b'\xc0').hexdigest(),
        'bytes': 1,
        'bit_order': 'most_significant_bit_first',
        'membership_validation': 'every_decoded_character',
        'unused_font_membership': 'unverified',
    }


@pytest.mark.parametrize('payload', [b'\x80', b'\x01', b'\x00'])
def test_cidset_cannot_claim_an_absent_used_cid(payload):
    with pytest.raises(FontError, match='cidset_missing_character'):
        build(declared_font(payload)).decode(b'\x00\x01')


@pytest.mark.parametrize('payload', [b'', b'\xff' * 8193], ids=['empty', 'oversized'])
def test_native_cidset_size_is_bounded_to_the_existing_cid_namespace(payload):
    with pytest.raises(FontError, match='invalid_cidset'):
        build(declared_font(payload))


@pytest.mark.parametrize('change', ['nonstream', 'external', 'other_stream_key', 'simple_font'])
def test_native_cidset_rejects_unknown_dependencies_and_wrong_font_kind(change):
    value = declared_font()
    descriptor = value['/DescendantFonts'][0]['/FontDescriptor']
    stream = descriptor['/CIDSet']
    if change == 'nonstream':
        descriptor[NameObject('/CIDSet')] = NumberObject(192)
    elif change == 'external':
        stream[NameObject('/F')] = NameObject('/external')
    elif change == 'other_stream_key':
        stream[NameObject('/Length1')] = NumberObject(1)
    else:
        value = font()
        value['/FontDescriptor'][NameObject('/CIDSet')] = stream
    with pytest.raises(FontError, match='invalid_cidset'):
        build(value)


def test_cidset_uses_the_callers_existing_stream_budget_and_cancellation():
    seen = []
    def consume(stream, kind):
        seen.append(kind)
        if kind == 'font_cid_set':
            raise TimeoutError('shared deadline')
        return stream.get_data()
    with pytest.raises(TimeoutError, match='shared deadline'):
        build_font(declared_font(), consume_stream=consume, check=lambda: None)
    assert seen.count('font_cid_set') == 1


def test_full_native_pdf_retains_cidset_without_changing_text_or_visual_semantics():
    receipt = extract(cidset_pdf())
    assert receipt['text'] == 'A'
    assert receipt['native_text_complete'] is True
    assert receipt['visual_meaning_verified'] is False
    assert 'cid_set' in str(receipt['pages'])


def test_full_native_pdf_still_rejects_corrupt_cidset_stream():
    with pytest.raises(ValueError):
        extract(cidset_pdf(change='truncated'))


def test_identity_h_symbolic_flag_cannot_override_explicit_verified_mapping():
    value = declared_font()
    value['/DescendantFonts'][0]['/FontDescriptor'][NameObject('/Flags')] = NumberObject(4)
    decoder = build(value)
    assert decoder.decode(b'\x00\x01')['text'] == 'A'
    assert decoder.receipt['declared_flags'] == 4


@pytest.mark.parametrize('flags', [0, 4 | 32, -1, 2**31])
def test_invalid_cid_symbolic_flags_remain_failures(flags):
    value = declared_font()
    value['/DescendantFonts'][0]['/FontDescriptor'][NameObject('/Flags')] = NumberObject(flags)
    with pytest.raises(FontError, match='symbolic_font'):
        build(value)


def test_simple_winansi_still_requires_nonsymbolic_font():
    value = font()
    value['/FontDescriptor'][NameObject('/Flags')] = NumberObject(4)
    with pytest.raises(FontError, match='symbolic_font'):
        build(value)
