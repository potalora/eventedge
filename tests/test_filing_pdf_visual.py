"""The authorized structural claim deliberately does not validate JPEG entropy."""
import hashlib
import io
import zlib
import pytest
from pypdf import PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject, EncodedStreamObject
from test_filing_pdf import pdf_bytes, _rewrite_pdf


def jpeg(width=2,height=2,entropy=b'\x01\x02',progressive=True):
    def segment(marker,body):return b'\xff'+bytes([marker])+(len(body)+2).to_bytes(2,'big')+body
    return (b'\xff\xd8'+segment(0xdb,b'\0'+b'\1'*64)
        +segment(0xc2 if progressive else 0xc0,bytes([8])+height.to_bytes(2,'big')+width.to_bytes(2,'big')+b'\3\1\x11\0\2\x11\0\3\x11\0')
        +segment(0xc4,b'\0'+b'\1'+b'\0'*15+b'\0')
        +segment(0xda,b'\3\1\0\2\0\3\0\0\0\0')+entropy+b'\xff\xd9')


def image_pdf(data=None,content=b'',pages=1,compress=True,unembedded=False):
    source=pdf_bytes(tuple(content for _ in range(pages)),image=True)
    def change(writer):
        for page in writer.pages:
            image=page['/Resources']['/XObject']['/Im1'].get_object()
            obj=EncodedStreamObject()
            obj.update({key:value for key,value in image.items() if key!='/Filter'})
            obj[NameObject('/Width')]=NumberObject(2);obj[NameObject('/Height')]=NumberObject(2)
            payload=jpeg() if data is None else data
            obj._data=zlib.compress(payload) if compress else payload
            obj[NameObject('/Filter')]=(ArrayObject([NameObject('/FlateDecode'),NameObject('/DCTDecode')]) if compress else NameObject('/DCTDecode'))
            page['/Resources']['/XObject'][NameObject('/Im1')]=writer._add_object(obj)
            if unembedded:
                font=page['/Resources']['/Font']['/F1'].get_object()
                font[NameObject('/Subtype')]=NameObject('/TrueType')
                font[NameObject('/BaseFont')]=NameObject('/ArialMT')
                font[NameObject('/Encoding')]=NameObject('/WinAnsiEncoding')
                font[NameObject('/FirstChar')]=NumberObject(48);font[NameObject('/LastChar')]=NumberObject(48)
                font[NameObject('/Widths')]=ArrayObject([NumberObject(556)])
    return _rewrite_pdf(source,change)


def extract(body,**changes):
    from tradingagents.strategies.data_sources.filing_pdf_visual import extract_pdf_visual_evidence
    limits=dict(max_pdf_bytes=200000,max_pages=20,max_stream_bytes=200000)
    return extract_pdf_visual_evidence(body,**(limits|changes))


def test_visual_receipt_makes_only_authorized_structural_claim():
    body=image_pdf(content=b'BT /F1 0.48 Tf 1 g 99.9974 Tz (0) Tj ET',unembedded=True)
    result=extract(body)
    assert result['version']=='sec-pdf-structural-visual-v1'
    assert result['input_sha256']==hashlib.sha256(body).hexdigest()
    assert result['visual_presence'] is True
    assert result['native_text_complete'] is False and result['order_authority'] is False
    assert result['encoded_image_validity']=='unverified' and result['visual_meaning']=='unresolved'
    assert result['native_unicode']=='unverified' and result['text_visibility']=='unverified'
    assert result['pages'][0]['image_invocations'][0]['declared_raster_bytes']==12
    assert any(x['operator']=='Tj' and x['operands'][0]['raw_hex']=='30' for x in result['pages'][0]['operations'])


def test_jpeg_entropy_is_not_certified_or_decoded():
    result=extract(image_pdf(jpeg(entropy=b'\x7f\xff\x00\x01')))
    assert result['encoded_image_validity']=='unverified'
    assert result['pages'][0]['image_invocations'][0]['jpeg']['entropy_validation']=='not_performed'


@pytest.mark.parametrize('damage',['eoi','suffix','segment','sof','scan','stuffing','empty'])
def test_bad_jpeg_framing_is_failure(damage):
    data=jpeg()
    if damage=='eoi':data=data[:-2]
    elif damage=='suffix':data+=b'extra'
    elif damage=='segment':data=data[:4]+b'\xff\xff'+data[6:]
    elif damage=='sof':data=jpeg(width=3)
    elif damage=='scan':data=data.replace(b'\3\1\0\2\0\3\0\0\0\0',b'\3\x09\0\2\0\3\0\0\0\0')
    elif damage=='stuffing':data=jpeg(entropy=b'\xff\x01')
    else:data=jpeg(entropy=b'')
    with pytest.raises(ValueError):extract(image_pdf(data))


@pytest.mark.parametrize('content',[b'/Missing Do',b'BT /Missing 12 Tf (x) Tj ET',b'1 2 cm',b'Q',b'BT /F1 12 Tf (x) Tj',b'99',b'1 Unknown',b'BI'])
def test_content_and_resource_errors_cannot_become_deferral(content):
    with pytest.raises(ValueError):extract(image_pdf(content=content))


def test_all_pages_and_actual_decoded_streams_share_budget():
    one=extract(image_pdf())
    two=extract(image_pdf(pages=2))
    assert two['page_count']==2 and two['decoded_stream_bytes']==2*one['decoded_stream_bytes']
    with pytest.raises(ValueError,match='limit'):extract(image_pdf(pages=2),max_stream_bytes=one['decoded_stream_bytes']+1)


def test_no_invoked_image_has_no_positive_visual_claim():
    assert extract(pdf_bytes())['visual_presence'] is False


def test_zero_area_or_offpage_invocation_is_not_positive():
    for content in (b'0 0 0 0 0 0 cm',b'1 0 0 1 10000 10000 cm'):
        assert extract(image_pdf(content=content))['visual_presence'] is False


def test_cancellation_escapes_unchanged():
    class Cancel(Exception):pass
    def cancel():raise Cancel('stop')
    with pytest.raises(Cancel):extract(image_pdf(),check=cancel)


def test_flate_truncation_is_not_hidden_by_jpeg_framing():
    def change(writer):
        obj=writer.pages[0]['/Resources']['/XObject']['/Im1'].get_object();obj._data=obj._data[:-1]
    with pytest.raises(ValueError):extract(_rewrite_pdf(image_pdf(),change))


@pytest.mark.parametrize('change',['version','claim','entropy','page_count','page_number','image_hash','presence','extra'])
def test_receipt_validator_rejects_claim_escalation_and_bad_schema(change):
    from tradingagents.strategies.data_sources.filing_pdf_visual import validate_pdf_visual_receipt
    receipt=extract(image_pdf())
    assert validate_pdf_visual_receipt(receipt) is True
    if change=='version':receipt['version']='native-pdf'
    elif change=='claim':receipt['native_text_complete']=True
    elif change=='entropy':receipt['pages'][0]['image_invocations'][0]['jpeg']['entropy_validation']='passed'
    elif change=='page_count':receipt['page_count']=2
    elif change=='page_number':receipt['pages'][0]['page_number']=2
    elif change=='image_hash':receipt['pages'][0]['image_invocations'][0]['encoded_sha256']='z'*64
    elif change=='presence':receipt['visual_presence']=False
    else:receipt['text']='fabricated analysis'
    with pytest.raises(ValueError):validate_pdf_visual_receipt(receipt)


@pytest.mark.parametrize('change',['image_ref','image_mask','stream_file','font_widths','image_dimensions','unknown_resource','page_limit','pdf_limit'])
def test_structural_subset_rejects_unresolved_or_unsupported_graph(change):
    from pypdf.generic import TextStringObject
    def mutate(writer):
        page=writer.pages[0];image=page['/Resources']['/XObject']['/Im1'].get_object()
        if change=='image_ref':del page['/Resources']['/XObject']['/Im1']
        elif change=='image_mask':image[NameObject('/Mask')]=ArrayObject([NumberObject(0)])
        elif change=='stream_file':page['/Contents'].get_object()[NameObject('/F')]=TextStringObject('external.dat')
        elif change=='font_widths':page['/Resources']['/Font']['/F1'].get_object()[NameObject('/FirstChar')]=NumberObject(0)
        elif change=='image_dimensions':image[NameObject('/Width')]=NumberObject(0)
        elif change=='unknown_resource':page['/Resources'][NameObject('/Pattern')]=DictionaryObject()
    body=_rewrite_pdf(image_pdf(),mutate)
    with pytest.raises(ValueError):extract(body,**({'max_pages':1} if change=='page_limit' else {'max_pdf_bytes':10} if change=='pdf_limit' else {})) if change!='page_limit' else extract(image_pdf(pages=2),max_pages=1)


def test_raw_image_and_form_provenance_positive():
    body=pdf_bytes((b'BT /F1 12 Tf (native) Tj ET',),image=True)
    result=extract(body)
    assert result['pages'][0]['image_invocations'][0]['jpeg'] is None
    assert result['visual_presence'] is True


def test_declared_huge_raster_is_not_decoded_or_charged_as_pixels():
    # The prospective claim explicitly leaves pixel/entropy validity unresolved.
    body=image_pdf(jpeg(width=60000,height=60000))
    def mutate(writer):
        im=writer.pages[0]['/Resources']['/XObject']['/Im1'].get_object()
        im[NameObject('/Width')]=NumberObject(60000);im[NameObject('/Height')]=NumberObject(60000)
    r=extract(_rewrite_pdf(body,mutate))
    assert r['pages'][0]['image_invocations'][0]['declared_raster_bytes']==10800000000
    assert r['decoded_stream_bytes']<1000


def test_type0_declarations_are_preserved_without_unicode_or_glyph_claims():
    from test_filing_pdf_fonts import cidfont
    def mutate(writer):writer.pages[0]['/Resources']['/Font'][NameObject('/F1')]=writer._add_object(cidfont())
    receipt=extract(_rewrite_pdf(image_pdf(content=b'BT /F1 12 Tf <0001> Tj ET'),mutate))
    assert receipt['native_unicode']=='unverified'
    assert receipt['pages'][0]['resources']['/Font']['/F1']['/Subtype']=='/Type0'
    assert any(row['operator']=='Tj' and row['operands'][0]['raw_hex']=='0001' for row in receipt['pages'][0]['operations'])


def test_content_arrays_preserve_exact_concatenation_without_inserted_bytes():
    from pypdf.generic import DecodedStreamObject
    def mutate(writer):
        a=DecodedStreamObject();a.set_data(b'q 100 0 0 100 0 0 cm /Im1 D')
        b=DecodedStreamObject();b.set_data(b'o Q')
        writer.pages[0][NameObject('/Contents')]=ArrayObject([writer._add_object(a),writer._add_object(b)])
    receipt=extract(_rewrite_pdf(image_pdf(),mutate))
    assert receipt['visual_presence'] is True
    assert receipt['pages'][0]['content_bytes']==sum(s['decoded_bytes'] for s in receipt['pages'][0]['streams'])


@pytest.mark.parametrize('change',['encoding','fontfile_external','descriptor_type'])
def test_malformed_font_declarations_are_not_unverified_semantics(change):
    from test_filing_pdf_fonts import font
    from pypdf.generic import TextStringObject
    def mutate(writer):
        f=font()
        if change=='encoding':f[NameObject('/Encoding')]=NumberObject(7)
        elif change=='fontfile_external':f['/FontDescriptor']['/FontFile2'][NameObject('/F')]=TextStringObject('outside')
        else:f[NameObject('/FontDescriptor')]=NumberObject(7)
        writer.pages[0]['/Resources']['/Font'][NameObject('/F1')]=writer._add_object(f)
    with pytest.raises(ValueError):extract(_rewrite_pdf(image_pdf(),mutate))


def test_explicit_rotation_receipt_is_plain_json_and_validated():
    from tradingagents.strategies.data_sources.filing_pdf_visual import validate_pdf_visual_receipt
    result=extract(pdf_bytes(image=True,rotate=90))
    assert validate_pdf_visual_receipt(result)
    assert type(result['pages'][0]['page_geometry']['rotation']) is int


@pytest.mark.parametrize('rotation',[12,1.5])
def test_invalid_rotation_is_failure(rotation):
    from pypdf.generic import FloatObject
    def mutate(writer):writer.pages[0][NameObject('/Rotate')]=FloatObject(rotation)
    with pytest.raises(ValueError):extract(_rewrite_pdf(image_pdf(),mutate))


def form_pdf():
    from pypdf.generic import DecodedStreamObject
    def mutate(writer):
        page=writer.pages[0]
        inner=page['/Resources']
        form=DecodedStreamObject();form.set_data(b'q 100 0 0 100 0 0 cm /Im1 Do Q')
        form.update({NameObject('/Type'):NameObject('/XObject'),NameObject('/Subtype'):NameObject('/Form'),
            NameObject('/BBox'):ArrayObject([NumberObject(n) for n in (0,0,612,792)]),
            NameObject('/Matrix'):ArrayObject([NumberObject(n) for n in (1,0,0,1,10,20)]),NameObject('/Resources'):inner})
        page[NameObject('/Resources')]=DictionaryObject({NameObject('/XObject'):DictionaryObject({NameObject('/Fm1'):writer._add_object(form)})})
        content=DecodedStreamObject();content.set_data(b'/Fm1 Do');page[NameObject('/Contents')]=writer._add_object(content)
    return _rewrite_pdf(image_pdf(),mutate)


def test_form_resources_and_invocations_are_complete_and_bounded():
    from tradingagents.strategies.data_sources.filing_pdf_visual import validate_pdf_visual_receipt
    r=extract(form_pdf())
    assert r['visual_presence'] and validate_pdf_visual_receipt(r)
    invocation=r['pages'][0]['image_invocations'][0]['form']
    assert invocation['form_geometry']['matrix']==[1,0,0,1,10,20]
    assert invocation['image_invocations'][0]['bounds']==[10,20,110,120]
    assert r['decoded_stream_bytes']>len(jpeg())*2


def test_uninvoked_form_has_no_positive_presence():
    from pypdf.generic import DecodedStreamObject
    def mutate(writer):
        content=DecodedStreamObject();content.set_data(b'');writer.pages[0][NameObject('/Contents')]=writer._add_object(content)
    r=extract(_rewrite_pdf(form_pdf(),mutate))
    assert r['visual_presence'] is False


@pytest.mark.parametrize('extra',[b'extra',zlib.compress(b'extra')])
def test_flate_trailing_data_remains_failure(extra):
    def mutate(writer):writer.pages[0]['/Resources']['/XObject']['/Im1'].get_object()._data+=extra
    with pytest.raises(ValueError,match='trailing_flate'):extract(_rewrite_pdf(image_pdf(),mutate))


def test_catalog_metadata_is_source_bound_and_explicitly_unverified():
    from pypdf.generic import DecodedStreamObject,BooleanObject,TextStringObject
    def mutate(writer):
        metadata=DecodedStreamObject();metadata.set_data(b'<metadata>uninterpreted</metadata>')
        metadata.update({NameObject('/Type'):NameObject('/Metadata'),NameObject('/Subtype'):NameObject('/XML')})
        writer.root_object[NameObject('/Metadata')]=writer._add_object(metadata)
        writer.root_object[NameObject('/Lang')]=TextStringObject('en')
        writer.root_object[NameObject('/MarkInfo')]=DictionaryObject({NameObject('/Marked'):BooleanObject(True)})
    receipt=extract(_rewrite_pdf(image_pdf(),mutate))
    declarations=receipt['document_declarations']
    assert declarations['metadata_semantics']=='unverified' and declarations['tagging_semantics']=='unverified'
    assert declarations['declarations']['/Metadata']['decoded_sha256']==hashlib.sha256(b'<metadata>uninterpreted</metadata>').hexdigest()


@pytest.mark.parametrize('key,value',[('/Lang',NumberObject(7)),('/Metadata',NumberObject(7)),('/MarkInfo',NumberObject(7)),('/StructTreeRoot',NumberObject(7)),('/ViewerPreferences',NumberObject(7))])
def test_catalog_declaration_types_are_checked(key,value):
    def mutate(writer):writer.root_object[NameObject(key)]=value
    with pytest.raises(ValueError):extract(_rewrite_pdf(image_pdf(),mutate))
