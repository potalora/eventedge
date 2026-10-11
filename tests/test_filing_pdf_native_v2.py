"""Exact text-show/state provenance and finite visual dependency counterexamples."""
import io
import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, DictionaryObject, NumberObject, ArrayObject, TextStringObject
from test_filing_pdf import pdf_bytes, extract
from test_filing_pdf_fonts import font, cidfont
from tradingagents.strategies.data_sources.filing_pdf import FilingPDFError


def with_font(content,value,**kwargs):
    reader=PdfReader(io.BytesIO(pdf_bytes((content,),**kwargs)))
    reader.pages[0]['/Resources']['/Font'][NameObject('/F1')]=value
    writer=PdfWriter();writer.add_page(reader.pages[0]);out=io.BytesIO();writer.write(out)
    return out.getvalue()


def test_real_tj_adjustments_and_standard_metrics_advance_subsequent_show():
    result=extract(pdf_bytes((b'BT /F1 12 Tf 72 700 Td (A) Tj [(B) 250 (A)] TJ (B) Tj ET',)))
    shows=result['pages'][0]['text_shows']
    assert result['text']=='ABAB'
    assert shows[0]['parts'][0]['glyphs'][0]['width']==667
    assert shows[1]['text_matrix'][4]==pytest.approx(80.004)
    assert shows[2]['text_matrix'][4]==pytest.approx(93.012)
    assert shows[1]['parts'][1]=={'adjustment':250.0,'advance':-3.0}
    assert result['supported_text_layout'] is False
    assert not result['support_limitations']


def test_spacing_quotes_and_saved_text_state_are_reconciled():
    content=b'BT /F1 10 Tf 72 700 Td 12 TL 2 Tc 3 Tw 50 Tz (A ) Tj q 0 Tc (A) Tj Q (A) Tj (B) \' 4 1 (A) " ET'
    shows=extract(pdf_bytes((content,)))['pages'][0]['text_shows']
    assert shows[0]['parts'][0]['advance']==pytest.approx(8.225)
    assert [show['char_space'] for show in shows]==[2,0,2,2,1]
    assert [show['word_space'] for show in shows]==[3,3,3,3,4]
    assert shows[-2]['text_matrix'][5]==688
    assert shows[-1]['text_matrix'][5]==676
    assert shows[-1]['separator_before']=='\n'


def test_embedded_winansi_and_cid_provenance_positive():
    for value,content in ((font(),b'BT /F1 12 Tf 72 700 Td (A) Tj ET 72 697 5 1 re f'),
                          (cidfont(),b'BT /F1 12 Tf 72 700 Td <0001> Tj ET 72 697 5 1 re f')):
        result=extract(with_font(content,value))
        assert result['text']=='A' and result['native_text_complete']
        assert result['pages'][0]['text_shows'][0]['parts'][0]['glyphs'][0]['glyph_id']==1
        assert result['pages'][0]['fonts'][0]['hint_programs_executed'] is False
        assert result['supported_text_layout'] is False
        assert result['support_limitations']==[]
        assert 'vector_graphics' in result['findings']


def test_valid_visual_paint_cannot_hide_unmapped_native_code():
    with pytest.raises(FilingPDFError,match='missing_width|missing_glyph|unmapped'):
        extract(with_font(b'0 0 50 50 re f BT /F1 12 Tf (B) Tj ET',font()))


@pytest.mark.parametrize('content', [b'BT /F1 12 Tf [1e309 (A)] TJ ET',b'EX',b'BX',b'BT /F1 12 Tf <00> Tj ET 0 0 5 5 re f'])
def test_invalid_text_and_state_remain_technical(content):
    with pytest.raises(FilingPDFError):extract(pdf_bytes((content,)))


@pytest.mark.parametrize('content',[b'0 0 612 792 re W n BT /F1 12 Tf 72 700 Td (A) Tj ET',
    b'BT /F1 12 Tf 72 700 Td (A) Tj ET 70 690 30 30 re f',
    b'BT /F1 12 Tf 72 700 Td (A) Tj 3 Tr (B) Tj ET'])
def test_clipping_overpaint_and_invisible_text_never_claim_supported_layout(content):
    result=extract(pdf_bytes((content,)))
    assert not result['supported_text_layout']
    assert result['native_text_complete']
    assert result['pages'][0]['geometry_operations']
    assert result['visual_meaning_verified'] is False


def test_normal_blend_and_typed_marked_metadata_are_finite():
    content=b'/Artifact << /Attached [/Top] /Type /Pagination /Subtype /Header >> BDC /GS1 gs BT /F1 12 Tf 72 700 Td [(A)] TJ ET EMC'
    resources={NameObject('/ExtGState'):DictionaryObject({NameObject('/GS1'):DictionaryObject({NameObject('/BM'):NameObject('/Normal'),NameObject('/ca'):NumberObject(1)})})}
    result=extract(pdf_bytes((content,),resource_entries=resources))
    assert result['text']=='A' and result['support_limitations']==[]
    assert result['pages'][0]['graphics_resources'][0]['parameters']['/BM']=='/Normal'
    for bad in (b'/P << /ActualText (B) >> BDC',b'/P << /Lang 4 >> BDC',b'/P << /Attached [/Top] >> BDC'):
        with pytest.raises(FilingPDFError):extract(pdf_bytes((bad+b' BT /F1 12 Tf (A) Tj ET EMC',)))


@pytest.mark.parametrize('suffix',[b' 12',b' (lost text)',b' /F1',b' [(lost)]'])
def test_final_operands_are_not_silently_dropped(suffix):
    with pytest.raises(FilingPDFError,match='trailing_content_operands'):
        extract(pdf_bytes((b'BT /F1 12 Tf (A) Tj ET'+suffix,)))


@pytest.mark.parametrize('kind',['embedded_font','tounicode','cid_map'])
@pytest.mark.parametrize('key',['/F','/FFilter','/FDecodeParms','/UseCMap'])
def test_unresolved_font_stream_dependency_never_becomes_visual_evidence(kind,key):
    from pypdf.generic import DecodedStreamObject
    value=cidfont() if kind!='embedded_font' else font()
    if kind=='embedded_font':stream=value['/FontDescriptor']['/FontFile2']
    elif kind=='tounicode':stream=value['/ToUnicode']
    else:
        stream=DecodedStreamObject();stream.set_data(b'\0\0\0\1')
        value['/DescendantFonts'][0][NameObject('/CIDToGIDMap')]=stream
    stream[NameObject(key)]=NameObject('/Identity-H') if key=='/UseCMap' else TextStringObject('unavailable')
    content=b'BT /F1 12 Tf 72 700 Td '+(b'(A)' if kind=='embedded_font' else b'<0001>')+b' Tj ET 72 697 10 1 re f'
    with pytest.raises(FilingPDFError,match='font_stream_dependency'):
        extract(with_font(content,value))


@pytest.mark.parametrize('prefix',[b'q Q ',b'BX EX ',b'0 g ',b'0 0 m '])
def test_harmless_nonpainting_operators_do_not_invent_visual_reason(prefix):
    result=extract(pdf_bytes((prefix+b'BT /F1 12 Tf 72 700 Td (Hello) Tj ET',)))
    assert 'positioned_text_layout' not in result['findings']
    assert result['supported_text_layout'] is True


def test_nondefault_text_color_does_not_claim_plain_visibility():
    result=extract(pdf_bytes((b'1 g BT /F1 12 Tf 72 700 Td (Hello) Tj ET',)))
    assert not result['supported_text_layout']
    assert 'text_visibility_state_not_validated' in result['support_limitations']
    assert 'positioned_text_layout' not in result['findings']


@pytest.mark.parametrize('key,value',[('/F',TextStringObject('missing-external.bin')),('/Filter',NameObject('/DCTDecode'))])
def test_invoked_page_stream_dependencies_and_opaque_filters_fail(key,value):
    reader=PdfReader(io.BytesIO(pdf_bytes((b'BT /F1 12 Tf 72 700 Td (A) Tj ET 0 0 10 10 re f',))))
    reader.pages[0]['/Contents'][NameObject(key)]=value
    writer=PdfWriter();writer.add_page(reader.pages[0]);out=io.BytesIO();writer.write(out)
    with pytest.raises(FilingPDFError,match='content_stream'):
        extract(out.getvalue())


def test_derived_transform_overflow_fails_even_without_text():
    content=b'1000000000 0 0 1 0 0 cm '*40+b'0 0 10 10 re f'
    with pytest.raises(FilingPDFError,match='geometry'):
        extract(pdf_bytes((content,)),max_stream_bytes=100000)


@pytest.mark.parametrize('prefix',[b'0 0 0 0 k ',b'/DeviceCMYK cs 0 0 0 0 sc '])
def test_cmyk_white_is_not_default_black_text(prefix):
    result=extract(pdf_bytes((prefix+b'BT /F1 12 Tf 72 700 Td (Hello) Tj ET',)))
    assert not result['supported_text_layout']
    assert 'text_visibility_state_not_validated' in result['support_limitations']


@pytest.mark.parametrize('prefix',[b'0 0 0 1 k ',b'/DeviceCMYK cs ',b'1 g q 0 g Q 0 g ',b'0 0 0 rg '])
def test_exact_device_black_states_are_understood(prefix):
    result=extract(pdf_bytes((prefix+b'BT /F1 12 Tf 72 700 Td (Hello) Tj ET',)))
    assert result['supported_text_layout']


@pytest.mark.parametrize('content',[b'3 Tr 0 Tr BT /F1 12 Tf 72 700 Td (Hello.) Tj ET',
    b'BT /F1 12 Tf 72 700 Td (Hello.) Tj 3 Tr ET'])
def test_unused_text_render_mode_does_not_manufacture_visual_reason(content):
    result=extract(pdf_bytes((content,)))
    assert 'nondefault_text_rendering' not in result['findings']
    assert result['supported_text_layout']
