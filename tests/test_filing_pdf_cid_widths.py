"""CID range widths must bind their own operand before parser advancement."""
import pytest
from pypdf.generic import ArrayObject, NameObject, NumberObject

from test_filing_pdf_fonts import build, cidfont
from tradingagents.strategies.data_sources.filing_pdf_fonts import FontError


def with_widths(entries):
    value = cidfont()
    value['/DescendantFonts'][0][NameObject('/W')] = ArrayObject([
        ArrayObject([NumberObject(width) for width in item])
        if isinstance(item, list) else NumberObject(item)
        for item in entries
    ])
    return value


@pytest.mark.parametrize('entries,expected', [
    ([1, 1, 500], {1: 500.0}),
    ([1, 1, 500, 2, 3, 400], {1: 500.0, 2: 400.0, 3: 400.0}),
    ([1, 1, 500, 2, [400, 300]], {1: 500.0, 2: 400.0, 3: 300.0}),
])
def test_cid_range_width_is_bound_to_its_declared_operand(entries, expected):
    decoder = build(with_widths(entries))
    assert decoder.widths == expected
    result = decoder.decode(b'\x00\x01')
    assert result['text'] == 'A'
    assert result['glyphs'][0]['width'] == 500.0


def test_negative_range_width_cannot_borrow_the_following_entry():
    with pytest.raises(FontError, match='pdf_invalid_font_width'):
        build(with_widths([1, 1, -1, 2, [500]]))


def test_correctly_bound_declared_width_still_must_match_the_embedded_glyph():
    with pytest.raises(FontError, match='pdf_font_width_mismatch'):
        build(with_widths([1, 1, 900])).decode(b'\x00\x01')
