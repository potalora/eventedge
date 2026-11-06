"""Finite source-declared font mappings; no downloaded fonts or visual OCR."""
import struct
import pytest
from pypdf.generic import (ArrayObject, DictionaryObject, NameObject, NumberObject, DecodedStreamObject)


def sfnt(*, remap=False, bad_glyph=False):
    # Minimal two-glyph TrueType: .notdef empty, A one triangular contour.
    glyph = struct.pack('>hhhhhHH', 1, 0, 0, 500, 700, 2, 0) + bytes([0x31, 0x21, 0x01]) + struct.pack('>hhh', 500, -250, 700)
    if bad_glyph:
        glyph = glyph[:-1]
    head = bytearray(54)
    struct.pack_into('>II', head, 0, 0x10000, 0x10000)
    struct.pack_into('>I', head, 12, 0x5F0F3CF5)
    struct.pack_into('>H', head, 18, 1000)
    struct.pack_into('>h', head, 50, 1)
    maxp = struct.pack('>IH', 0x10000, 2) + bytes(26)
    hhea = bytearray(36)
    struct.pack_into('>I', hhea, 0, 0x10000)
    struct.pack_into('>H', hhea, 34, 2)
    # format 4, two segments (A and mandatory sentinel).
    cmap4 = struct.pack('>HHHHHHH', 4, 32, 0, 4, 4, 1, 0) + struct.pack('>HHHHHHHHH', 65, 65535, 0, 65, 65535, (2 if remap else 1)-65 & 65535, 1, 0, 0)
    tables = {b'head': bytes(head), b'maxp': maxp, b'hhea': bytes(hhea), b'hmtx': struct.pack('>HHHH', 500, 0, 500, 0),
              b'loca': struct.pack('>III', 0, 0, len(glyph)), b'glyf': glyph,
              b'cmap': struct.pack('>HHHHI', 0, 1, 3, 1, 12) + cmap4}
    n = len(tables)
    out = bytearray(struct.pack('>IHHHH', 0x10000, n, 64, 2, n*16-64) + bytes(16*n))
    for i, (tag, data) in enumerate(sorted(tables.items())):
        offset = len(out)
        padded = data + bytes((-len(data)) % 4)
        checksum = sum(struct.unpack('>' + 'I'*(len(padded)//4), padded)) & 0xffffffff
        struct.pack_into('>4sIII', out, 12+16*i, tag, checksum, offset, len(data))
        out.extend(padded)
    return resign(out)


def resign(data):
    out=bytearray(data)
    for i in range(struct.unpack_from('>H',out,4)[0]):
        tag,_,offset,_=struct.unpack_from('>4sIII',out,12+i*16)
        if tag==b'head':
            struct.pack_into('>I',out,offset+8,0)
            total=sum(v[0] for v in struct.iter_unpack('>I',out))&0xffffffff
            struct.pack_into('>I',out,offset+8,(0xB1B0AFBA-total)&0xffffffff)
            break
    return bytes(out)


def font(data=None):
    stream = DecodedStreamObject(); stream.set_data(data or sfnt())
    descriptor = DictionaryObject({NameObject('/Type'): NameObject('/FontDescriptor'), NameObject('/FontName'): NameObject('/ArbitraryName'),
        NameObject('/Flags'): NumberObject(32), NameObject('/FontBBox'): ArrayObject([NumberObject(x) for x in (0,0,500,700)]),
        NameObject('/FontFile2'): stream})
    return DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/TrueType'),
        NameObject('/BaseFont'): NameObject('/ArbitraryName'), NameObject('/Encoding'): NameObject('/WinAnsiEncoding'),
        NameObject('/FirstChar'): NumberObject(65), NameObject('/LastChar'): NumberObject(65),
        NameObject('/Widths'): ArrayObject([NumberObject(500)]), NameObject('/FontDescriptor'): descriptor})


def build(value):
    from tradingagents.strategies.data_sources.filing_pdf_fonts import build_font
    return build_font(value, consume_stream=lambda stream, kind: stream.get_data(), check=lambda: None)


def test_embedded_unicode_mapping_and_used_glyph_structure():
    decoder = build(font())
    result = decoder.decode(b'A')
    assert result['text'] == 'A'
    assert result['glyphs'][0]['glyph_id'] == 1
    assert result['glyphs'][0]['width'] == 500
    assert result['glyphs'][0]['declared_bbox'] == [0, 0, 500, 700]
    assert decoder.receipt['mapping_trust'] == 'source_declared_unicode'
    assert decoder.receipt['hint_programs_executed'] is False


@pytest.mark.parametrize('kind', ['checksum', 'missing_glyph', 'truncated_glyph', 'unmapped_code'])
def test_malformed_or_missing_font_mapping_fails(kind):
    from tradingagents.strategies.data_sources.filing_pdf_fonts import FontError
    data = sfnt(remap=kind == 'missing_glyph', bad_glyph=kind == 'truncated_glyph')
    if kind == 'checksum': data = data[:-1] + bytes([data[-1] ^ 1])
    with pytest.raises(FontError):
        build(font(data)).decode(b'B' if kind == 'unmapped_code' else b'A')


@pytest.mark.parametrize('hints', [(256,4,128),(65535,65535,65535),(0,0,0)])
def test_stale_binary_search_hints_are_recorded_but_not_used(hints):
    data=bytearray(sfnt());struct.pack_into('>HHH',data,6,*hints)
    decoder=build(font(resign(data)))
    assert decoder.decode(b'A')['text']=='A'
    assert decoder.receipt['directory_hints']['declared']==list(hints)
    assert decoder.receipt['directory_hints']['derived']==[64,2,48]


@pytest.mark.parametrize('mutation', ['table_count','overlap','table_order','font_checksum'])
def test_substantive_font_directory_faults_fail(mutation):
    from tradingagents.strategies.data_sources.filing_pdf_fonts import FontError
    data=bytearray(sfnt())
    if mutation=='table_count':struct.pack_into('>H',data,4,65535)
    elif mutation=='overlap':data[36:40]=data[20:24]
    elif mutation=='table_order':data[12:16]=b'zzzz'
    else:data[6]^=1
    with pytest.raises(FontError):build(font(bytes(data)))


def tounicode(mapping='<0001> <0041>', count=1):
    return (f'/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n'
        '/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n'
        '/CMapName /Test def\n/CMapType 2 def\n1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n'
        f'{count} beginbfchar\n{mapping}\nendbfchar\nendcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n').encode()


def cidfont():
    from pypdf.generic import TextStringObject
    simple=font();cmap=DecodedStreamObject();cmap.set_data(tounicode())
    descendant=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/CIDFontType2'),
       NameObject('/BaseFont'):NameObject('/ArbitraryName'),NameObject('/FontDescriptor'):simple['/FontDescriptor'],
       NameObject('/CIDSystemInfo'):DictionaryObject({NameObject('/Registry'):TextStringObject('Adobe'),
           NameObject('/Ordering'):TextStringObject('Identity'),NameObject('/Supplement'):NumberObject(0)}),
       NameObject('/CIDToGIDMap'):NameObject('/Identity'),NameObject('/DW'):NumberObject(500)})
    return DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type0'),
        NameObject('/BaseFont'):NameObject('/ArbitraryName'),NameObject('/Encoding'):NameObject('/Identity-H'),
        NameObject('/DescendantFonts'):ArrayObject([descendant]),NameObject('/ToUnicode'):cmap})


def test_identity_h_exact_tounicode_and_glyph_mapping():
    result=build(cidfont()).decode(b'\0\1')
    assert result['text']=='A' and result['glyphs'][0]['glyph_id']==1


@pytest.mark.parametrize('case', ['partial_code','missing_code','contradiction','duplicate','trailing_program','bad_width','missing_gid'])
def test_cid_tounicode_failures_never_become_visual_exclusions(case):
    from tradingagents.strategies.data_sources.filing_pdf_fonts import FontError
    value=cidfont();raw=b'\0\1'
    if case=='partial_code':raw=b'\0'
    elif case=='missing_code':raw=b'\0\2'
    elif case=='contradiction':value['/ToUnicode'].set_data(tounicode('<0001> <0042>'))
    elif case=='duplicate':value['/ToUnicode'].set_data(tounicode('<0001> <0041>\n<0001> <0041>',2))
    elif case=='trailing_program':value['/ToUnicode'].set_data(tounicode()+b' unsafe')
    elif case=='bad_width':value['/DescendantFonts'][0][NameObject('/DW')]=NumberObject(900)
    else:
        stream=DecodedStreamObject();stream.set_data(b'\0\0\0\2')
        value['/DescendantFonts'][0][NameObject('/CIDToGIDMap')]=stream
    with pytest.raises(FontError):build(value).decode(raw)


def test_provenance_budget_checked_before_glyph_inventory_expansion():
    from tradingagents.strategies.data_sources.filing_pdf_fonts import FontError
    with pytest.raises(FontError,match='limit'):
        build(font()).decode(b'A'*1000,provenance_budget=1000)
