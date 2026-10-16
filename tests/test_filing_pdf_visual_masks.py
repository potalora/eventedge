"""Structural mask/palette declarations never certify pixels or native text."""
import hashlib
import pytest
from pypdf.generic import (ArrayObject, BooleanObject, DecodedStreamObject,
    DictionaryObject, FloatObject, NameObject, NumberObject, TextStringObject)
from test_filing_pdf import pdf_bytes, _rewrite_pdf
from test_filing_pdf_visual import extract
from tradingagents.strategies.data_sources import filing_pdf_visual as visual


def array(values):
    return ArrayObject([NumberObject(x) for x in values])


def image(data=b'\x80', *, mask=False, color='/DeviceGray', width=1, height=1, bits=8):
    obj=DecodedStreamObject();obj.set_data(data)
    obj.update({NameObject('/Type'):NameObject('/XObject'),NameObject('/Subtype'):NameObject('/Image'),
        NameObject('/Width'):NumberObject(width),NameObject('/Height'):NumberObject(height),
        NameObject('/BitsPerComponent'):NumberObject(bits)})
    if mask:obj[NameObject('/ImageMask')]=BooleanObject(True)
    else:obj[NameObject('/ColorSpace')]=NameObject(color)
    return obj.flate_encode()


def masked_pdf(*, change=None, mode='soft'):
    def mutate(writer):
        page=writer.pages[0];base=page['/Resources']['/XObject']['/Im1']
        mask=image(mask=mode=='explicit',bits=1 if mode=='explicit' else 8)
        key='/Mask' if mode in ('explicit','key') else '/SMask'
        base[NameObject(key)]=array([0,255,0,255,0,255]) if mode=='key' else writer._add_object(mask)
        if change:change(writer,base,mask)
    return _rewrite_pdf(pdf_bytes((b'',),image=True),mutate)


def test_soft_mask_complete_receipt_is_source_bound_and_unverified():
    receipt=extract(masked_pdf())
    invocation=receipt['pages'][0]['image_invocations'][0]
    mask=invocation['dictionary']['/SMask']
    assert mask['image_role']=='soft_mask' and mask['image_mask'] is False
    assert mask['decoded_sha256']==hashlib.sha256(b'\x80').hexdigest()
    assert mask['decoded_bytes']==1 and mask['filters']==['/FlateDecode']
    assert mask['declared_raster_bytes_validity']=='unverified'
    assert receipt['native_text_complete'] is False and receipt['order_authority'] is False
    assert visual.validate_pdf_visual_receipt(receipt)


def test_explicit_mask_and_color_key_mask_are_fully_recorded():
    mask=extract(masked_pdf(mode='explicit'))['pages'][0]['image_invocations'][0]['dictionary']['/Mask']
    assert mask['image_role']=='explicit_mask' and mask['image_mask'] is True and mask['bits']==1
    keyed=extract(masked_pdf(mode='key'))['pages'][0]['image_invocations'][0]['dictionary']['/Mask']
    assert keyed==[0,255,0,255,0,255]


def test_mask_and_smask_are_both_inventoried_even_when_overridden():
    def change(writer,base,mask):base[NameObject('/Mask')]=writer._add_object(image(mask=True,bits=1))
    declaration=extract(masked_pdf(change=change))['pages'][0]['image_invocations'][0]['dictionary']
    assert declaration['/Mask']['image_role']=='explicit_mask'
    assert declaration['/SMask']['image_role']=='soft_mask'


@pytest.mark.parametrize('key,value',[
    ('/ColorSpace',NameObject('/DeviceRGB')),('/ImageMask',BooleanObject(True)),
    ('/Mask',array([0,1])),('/SMask',DictionaryObject()),('/Matte',array([0,0,0])),
    ('/Width',NumberObject(0)),('/BitsPerComponent',NumberObject(3)),
    ('/Decode',array([0,1,2])),('/Interpolate',NumberObject(1)),
    ('/F',TextStringObject('external.bin')),('/HiddenFunction',DictionaryObject()),
])
def test_invalid_or_uninventoried_soft_mask_fails(key,value):
    def change(writer,base,mask):mask[NameObject(key)]=value
    with pytest.raises(ValueError):extract(masked_pdf(change=change))


@pytest.mark.parametrize('key,value',[
    ('/ImageMask',BooleanObject(False)),('/BitsPerComponent',NumberObject(8)),
    ('/ColorSpace',NameObject('/DeviceGray')),('/Decode',array([0,2])),
])
def test_invalid_explicit_mask_fails(key,value):
    def change(writer,base,mask):mask[NameObject(key)]=value
    with pytest.raises(ValueError):extract(masked_pdf(mode='explicit',change=change))


@pytest.mark.parametrize('values',[[0],[-1,255,0,255,0,255],[256,256,0,255,0,255],[3,2,0,255,0,255]])
def test_invalid_color_key_mask_fails(values):
    def change(writer,base,mask):base[NameObject('/Mask')]=array(values)
    with pytest.raises(ValueError):extract(masked_pdf(mode='key',change=change))


@pytest.mark.parametrize('damage',['truncate','trailing','short','cycle','nonstream','unused'])
def test_mask_dependency_failure_never_becomes_deferral(damage):
    def change(writer,base,mask):
        if damage=='truncate':mask._data=mask._data[:-1]
        elif damage=='trailing':mask._data+=b'x'
        elif damage=='short':mask[NameObject('/Width')]=NumberObject(2)
        elif damage=='cycle':base[NameObject('/SMask')]=base.indirect_reference
        elif damage=='nonstream':base[NameObject('/SMask')]=DictionaryObject()
        else:
            mask._data=mask._data[:-1]
            writer.pages[0]['/Contents'].set_data(b'')
    with pytest.raises(ValueError):extract(masked_pdf(change=change))


def test_mask_decode_charges_shared_budget_and_cancellation():
    receipt=extract(masked_pdf())
    with pytest.raises(ValueError,match='limit'):
        extract(masked_pdf(),max_stream_bytes=receipt['decoded_stream_bytes']-1)
    class Cancel(Exception):pass
    calls=[]
    def check():
        calls.append(1)
        if len(calls)>10:raise Cancel('stop')
    with pytest.raises(Cancel):extract(masked_pdf(),check=check)


def indexed_pdf(*, change=None):
    def mutate(writer):
        base=image(b'\x01');palette=DecodedStreamObject();palette.set_data(b'\0\0\0\xff\xff\xff');palette=palette.flate_encode()
        base[NameObject('/ColorSpace')]=ArrayObject([NameObject('/Indexed'),NameObject('/DeviceRGB'),NumberObject(1),writer._add_object(palette)])
        base[NameObject('/Mask')]=array([1,1])
        if change:change(base,palette)
        writer.pages[0]['/Resources']['/XObject'][NameObject('/Im1')]=writer._add_object(base)
    return _rewrite_pdf(pdf_bytes((b'',),image=True),mutate)


def test_indexed_palette_is_strictly_decoded_and_bound():
    proof=extract(indexed_pdf())['pages'][0]['image_invocations'][0]
    assert proof['components']==1 and proof['dictionary']['/Mask']==[1,1]
    palette=proof['dictionary']['/ColorSpace'][3]['stream']
    assert palette['decoded_bytes']==6 and palette['decoded_sha256']==hashlib.sha256(b'\0\0\0\xff\xff\xff').hexdigest()


@pytest.mark.parametrize('damage',['truncated','short','hival','base','hidden'])
def test_invalid_indexed_dependency_fails(damage):
    def change(base,palette):
        if damage=='truncated':palette._data=palette._data[:-1]
        elif damage=='short':base['/ColorSpace'][2]=NumberObject(2)
        elif damage=='hival':base['/ColorSpace'][2]=NumberObject(256)
        elif damage=='base':base['/ColorSpace'][1]=NameObject('/DeviceN')
        elif damage=='hidden':palette[NameObject('/Function')]=DictionaryObject()
    with pytest.raises(ValueError):extract(indexed_pdf(change=change))


def test_smoothness_is_typed_bounded_and_retained():
    def mutate(writer):
        writer.pages[0]['/Resources'][NameObject('/ExtGState')]=DictionaryObject({NameObject('/GS'):DictionaryObject({NameObject('/SM'):FloatObject(0.02)})})
    result=extract(_rewrite_pdf(pdf_bytes((b'/GS gs',),image=True),mutate))
    assert result['pages'][0]['resources']['/ExtGState']['/GS']['/SM']==0.02


@pytest.mark.parametrize('value',[NumberObject(-1),FloatObject(1.01),BooleanObject(True),TextStringObject('0.02'),DictionaryObject()])
def test_invalid_smoothness_fails(value):
    def mutate(writer):writer.pages[0]['/Resources'][NameObject('/ExtGState')]=DictionaryObject({NameObject('/GS'):DictionaryObject({NameObject('/SM'):value})})
    with pytest.raises(ValueError):extract(_rewrite_pdf(pdf_bytes((b'/GS gs',),image=True),mutate))


def metadata_pdf(*, change=None):
    def mutate(writer,base,mask):
        metadata=DecodedStreamObject();metadata.set_data(b'<x:xmpmeta/>')
        metadata[NameObject('/Type')]=NameObject('/Metadata');metadata[NameObject('/Subtype')]=NameObject('/XML')
        base[NameObject('/Metadata')]=writer._add_object(metadata)
        base[NameObject('/Name')]=NameObject('/X')
        base[NameObject('/HRes')]=NumberObject(96);base[NameObject('/VRes')]=NumberObject(96)
        if change:change(base,metadata)
    return masked_pdf(change=mutate)


def test_image_metadata_is_typed_hashed_and_never_rendering_proof():
    proof=extract(metadata_pdf())['pages'][0]['image_invocations'][0]
    metadata=proof['dictionary']['/Metadata']
    assert metadata['decoded_sha256']==hashlib.sha256(b'<x:xmpmeta/>').hexdigest()
    assert metadata['semantics']=='unverified'
    assert proof['dictionary']['/HRes']==proof['dictionary']['/VRes']==96


@pytest.mark.parametrize('key,value',[('/Name',TextStringObject('X')),('/HRes',TextStringObject('96')),('/VRes',NumberObject(0)),('/Unknown',NumberObject(1))])
def test_invalid_or_unknown_image_metadata_fails(key,value):
    def change(base,metadata):base[NameObject(key)]=value
    with pytest.raises(ValueError):extract(metadata_pdf(change=change))


def test_metadata_hidden_dependency_and_receipt_mask_escalation_fail():
    def change(base,metadata):metadata[NameObject('/Function')]=DictionaryObject()
    with pytest.raises(ValueError):extract(metadata_pdf(change=change))
    result=extract(masked_pdf())
    result['pages'][0]['image_invocations'][0]['dictionary']['/SMask']['declared_raster_bytes_validity']='verified'
    with pytest.raises(ValueError):visual.validate_pdf_visual_receipt(result)


def test_color_resource_cycle_and_depth_fail_within_budget():
    state={'limit':200000,'decoded_stream_bytes':0,'inventory_bytes':0,'check':None}
    resources=DictionaryObject({NameObject('/ColorSpace'):DictionaryObject({NameObject('/A'):NameObject('/B'),NameObject('/B'):NameObject('/A')})})
    with pytest.raises(ValueError,match='cycle_or_depth'):
        visual._color_components(NameObject('/A'),resources,state)
    obj=image()
    with pytest.raises(ValueError,match='cycle_or_depth'):
        visual._image(obj,DictionaryObject(),state,active=tuple(range(32)))


@pytest.mark.parametrize('kind',[NameObject('/Stream'),NameObject('/Image'),TextStringObject('/Stream')])
def test_font_program_stream_type_is_finite_and_contextual(kind):
    from test_filing_pdf_fonts import cidfont
    def mutate(writer):
        font=cidfont();font['/DescendantFonts'][0]['/FontDescriptor']['/FontFile2'][NameObject('/Type')]=kind
        writer.pages[0]['/Resources']['/Font'][NameObject('/F1')]=writer._add_object(font)
    body=_rewrite_pdf(pdf_bytes((b'',),image=True),mutate)
    if isinstance(kind,NameObject) and kind=='/Stream':
        result=extract(body)
        stream=result['pages'][0]['resources']['/Font']['/F1']['/DescendantFonts'][0]['/FontDescriptor']['/FontFile2']
        assert stream['/Type']=='/Stream' and stream['stream']['decoded_bytes']>0
    else:
        with pytest.raises(ValueError):extract(body)


def test_palette_cannot_use_font_program_stream_type_exception():
    def change(base,palette):palette[NameObject('/Type')]=NameObject('/Stream')
    with pytest.raises(ValueError):extract(indexed_pdf(change=change))


@pytest.mark.parametrize('damage',[None,'truncated','length'])
def test_jpeg_app1_metadata_has_complete_framing_but_unverified_semantics(damage):
    from test_filing_pdf_visual import image_pdf,jpeg
    payload=b'Exif\0\0opaque application data'
    segment=b'\xff\xe1'+(len(payload)+2).to_bytes(2,'big')+payload
    data=jpeg();data=data[:2]+segment+data[2:]
    if damage=='truncated':data=data[:len(segment)]
    elif damage=='length':data=data[:4]+b'\xff\xff'+data[6:]
    if damage:
        with pytest.raises(ValueError):extract(image_pdf(data))
    else:
        proof=extract(image_pdf(data))['pages'][0]['image_invocations'][0]['jpeg']
        assert proof['segments'][0]['marker']==225
        assert proof['application_metadata_validation']=='not_performed'


@pytest.mark.parametrize('change',['role','bits','dimensions','decoded_bytes','mask_semantics','metadata_semantics'])
def test_recursive_receipt_rejects_mask_or_metadata_claim_corruption(change):
    receipt=extract(metadata_pdf())
    proof=receipt['pages'][0]['image_invocations'][0]
    mask=proof['dictionary']['/SMask']
    if change=='role':mask['image_role']='rendered'
    elif change=='bits':mask['bits']=16
    elif change=='dimensions':mask['width']=2
    elif change=='decoded_bytes':mask['decoded_bytes']=10
    elif change=='mask_semantics':mask['declared_raster_bytes_validity']='verified'
    else:proof['dictionary']['/Metadata']['semantics']='verified'
    with pytest.raises(ValueError):visual.validate_pdf_visual_receipt(receipt)


@pytest.mark.parametrize('key',['/SMask','/TR','/HT','/Font','/Unknown'])
def test_smoothness_does_not_allow_hidden_graphics_dependencies(key):
    def mutate(writer):
        writer.pages[0]['/Resources'][NameObject('/ExtGState')]=DictionaryObject({NameObject('/GS'):DictionaryObject({NameObject('/SM'):FloatObject(0.02),NameObject(key):DictionaryObject()})})
    with pytest.raises(ValueError):extract(_rewrite_pdf(pdf_bytes((b'/GS gs',),image=True),mutate))


def test_actual_indexed_cs_operator_preserves_color_arity_and_palette():
    def mutate(writer):
        page=writer.pages[0]
        image=page['/Resources']['/XObject']['/Im1']
        page['/Resources'][NameObject('/ColorSpace')]=DictionaryObject({NameObject('/CS'):image['/ColorSpace']})
        page['/Contents'].set_data(b'/CS cs 1 sc 0 0 10 10 re f /Im1 Do')
    receipt=extract(_rewrite_pdf(indexed_pdf(),mutate))
    assert receipt['visual_presence'] is True
    assert receipt['pages'][0]['operations'][0]['operands']==['/CS']


def test_soft_mask_indirect_devicegray_declaration_is_resolved():
    def change(writer,base,mask):mask[NameObject('/ColorSpace')]=writer._add_object(NameObject('/DeviceGray'))
    assert extract(masked_pdf(change=change))['visual_presence'] is True
