"""Every native byte stream must have complete framing before text authority."""
import hashlib
import zlib
import pytest
from pypdf.generic import (ArrayObject, DecodedStreamObject, DictionaryObject,
    EncodedStreamObject, NameObject, NullObject, NumberObject)
from test_filing_pdf import pdf_bytes, _rewrite_pdf, extract
from test_filing_pdf_fonts import cidfont

KINDS=('font_cid_set','page','form','embedded_font','font_cmap','font_cid_map','image')


def stream_pdf(kind='page',damage=None,*,filter_name='/FlateDecode',parameters=None):
    def mutate(writer):
        page=writer.pages[0];font=cidfont();desc=font['/DescendantFonts'][0];descriptor=desc['/FontDescriptor']
        cidset=DecodedStreamObject();cidset.set_data(b'\xc0');descriptor[NameObject('/CIDSet')]=writer._add_object(cidset)
        page['/Resources']['/Font'][NameObject('/F1')]=writer._add_object(font)
        if kind=='page':target=page['/Contents'];owner=page;key='/Contents'
        elif kind=='embedded_font':target=descriptor['/FontFile2'];owner=descriptor;key='/FontFile2'
        elif kind=='font_cmap':target=font['/ToUnicode'];owner=font;key='/ToUnicode'
        elif kind=='font_cid_map':
            target=DecodedStreamObject();target.set_data(b'\0\0\0\1');owner=desc;key='/CIDToGIDMap'
        elif kind=='font_cid_set':target=cidset;owner=descriptor;key='/CIDSet'
        elif kind=='image':target=page['/Resources']['/XObject']['/Im1'];owner=page['/Resources']['/XObject'];key='/Im1'
        else:
            target=DecodedStreamObject();target.set_data(b'BT /F1 12 Tf <0001> Tj ET')
            target.update({NameObject('/Type'):NameObject('/XObject'),NameObject('/Subtype'):NameObject('/Form'),
                NameObject('/BBox'):ArrayObject([NumberObject(x) for x in (0,0,200,200)]),
                NameObject('/Resources'):DictionaryObject({NameObject('/Font'):page['/Resources']['/Font']})})
            owner=page['/Resources']['/XObject'];key='/Fm';page['/Contents'].set_data(b'/Fm Do')
        if filter_name is None:
            encoded=DecodedStreamObject();encoded.set_data(target.get_data())
            encoded.update({k:v for k,v in target.items() if k not in ('/Filter','/DecodeParms')})
        else:
            encoded=target.flate_encode();encoded[NameObject('/Filter')]=NameObject(filter_name)
        if damage=='truncated':encoded._data=encoded._data[:-1]
        elif damage=='trailing':encoded._data+=b'unconsumed trailing data'
        elif damage=='concatenated':encoded._data+=zlib.compress(b'unconsumed second stream')
        elif damage=='checksum':encoded._data=encoded._data[:-1]+bytes([encoded._data[-1]^1])
        if parameters is not None:encoded[NameObject('/DecodeParms')]=parameters
        owner[NameObject(key)]=writer._add_object(encoded)
    return _rewrite_pdf(pdf_bytes((b'BT /F1 12 Tf <0001> Tj ET',),image=True),mutate)


@pytest.mark.parametrize('kind',KINDS)
@pytest.mark.parametrize('damage',['truncated','trailing','concatenated','checksum'])
def test_incomplete_or_unconsumed_stream_cannot_produce_native_evidence(kind,damage):
    with pytest.raises(ValueError):extract(stream_pdf(kind,damage))


@pytest.mark.parametrize('kind',KINDS)
@pytest.mark.parametrize('filter_name',[None,'/FlateDecode','/Fl'])
def test_complete_unfiltered_and_flate_streams_keep_native_text(kind,filter_name):
    result=extract(stream_pdf(kind,filter_name=filter_name))
    assert result['text']=='A' and result['native_text_complete'] is True
    assert result['visual_meaning_verified'] is False


@pytest.mark.parametrize('parameters',[NullObject(),ArrayObject([NullObject()]),DictionaryObject(),
    DictionaryObject({NameObject('/Predictor'):NumberObject(1),NameObject('/Colors'):NumberObject(3),NameObject('/BitsPerComponent'):NumberObject(8),NameObject('/Columns'):NumberObject(2)})])
def test_flate_identity_parameters_are_exactly_validated(parameters):
    assert extract(stream_pdf(parameters=parameters))['text']=='A'


@pytest.mark.parametrize('parameters',[
    ArrayObject(),ArrayObject([NullObject(),NullObject()]),NumberObject(1),
    DictionaryObject({NameObject('/Predictor'):NumberObject(2)}),
    DictionaryObject({NameObject('/Colors'):NumberObject(0)}),
    DictionaryObject({NameObject('/Columns'):NumberObject(-1)}),
    DictionaryObject({NameObject('/BitsPerComponent'):NumberObject(3)}),
    DictionaryObject({NameObject('/Function'):DictionaryObject()}),
])
@pytest.mark.parametrize('kind',['page','image'])
def test_unimplemented_or_invalid_decode_parameters_fail(kind,parameters):
    with pytest.raises(ValueError):extract(stream_pdf(kind,parameters=parameters))


@pytest.mark.parametrize('filter_name',['/ASCIIHexDecode','/ASCII85Decode','/LZWDecode','/RunLengthDecode','/DCTDecode','/Crypt'])
def test_unimplemented_filters_never_fall_back_to_generic_recovery(filter_name):
    with pytest.raises(ValueError):extract(stream_pdf(filter_name=filter_name))


def test_filter_chains_are_explicitly_unsupported():
    def mutate(writer):
        stream=writer.pages[0]['/Contents'];stream._data=zlib.compress(stream._data)
        stream[NameObject('/Filter')]=ArrayObject([NameObject('/FlateDecode'),NameObject('/FlateDecode')])
    with pytest.raises(ValueError):extract(_rewrite_pdf(stream_pdf(),mutate))


def test_native_content_array_hashes_exact_source_concatenation():
    blocks=[b'BT /F1 12 Tf 72 720 Td (A',b') Tj ET']
    def mutate(writer):
        refs=[]
        for block in blocks:
            stream=DecodedStreamObject();stream.set_data(block);refs.append(writer._add_object(stream.flate_encode()))
        writer.pages[0][NameObject('/Contents')]=ArrayObject(refs)
    result=extract(_rewrite_pdf(pdf_bytes(),mutate))
    assert result['text']=='A'
    assert result['pages'][0]['content_sha256']==hashlib.sha256(b''.join(blocks)).hexdigest()
    assert result['pages'][0]['content_bytes']==sum(map(len,blocks))


def test_content_array_does_not_repair_missing_token_boundary():
    def mutate(writer):
        first=DecodedStreamObject();first.set_data(b'BT /F1 12 Tf (A) Tj ET')
        second=DecodedStreamObject();second.set_data(b'BT /F1 12 Tf (A) Tj ET')
        writer.pages[0][NameObject('/Contents')]=ArrayObject([writer._add_object(x.flate_encode()) for x in (first,second)])
    with pytest.raises(ValueError):extract(_rewrite_pdf(pdf_bytes(),mutate))


def test_each_content_array_component_requires_complete_stream_framing():
    def mutate(writer):
        first=DecodedStreamObject();first.set_data(b'BT /F1 12 Tf (A) Tj ET ')
        second=DecodedStreamObject();second.set_data(b'BT /F1 12 Tf (A) Tj ET');second=second.flate_encode();second._data=second._data[:-1]
        writer.pages[0][NameObject('/Contents')]=ArrayObject([writer._add_object(first.flate_encode()),writer._add_object(second)])
    with pytest.raises(ValueError):extract(_rewrite_pdf(pdf_bytes(),mutate))


def test_unfiltered_stream_bytes_are_also_under_aggregate_budget():
    body=stream_pdf();reference=extract(body)
    with pytest.raises(ValueError,match='limit'):extract(body,max_stream_bytes=reference['decoded_stream_bytes']-1)


def test_font_program_cmap_and_image_do_not_call_pypdf_get_data(monkeypatch):
    body=stream_pdf('embedded_font')
    def forbidden(*args,**kwargs):raise AssertionError('generic stream recovery is forbidden')
    monkeypatch.setattr(EncodedStreamObject,'get_data',forbidden)
    assert extract(body)['text']=='A'
