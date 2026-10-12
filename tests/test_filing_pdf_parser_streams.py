"""Parser-internal XRef/ObjStm streams share the complete framing contract."""
import copy
import re
import zlib
import pytest
from test_filing_pdf import pdf_bytes, extract as native_extract
from test_filing_pdf_visual import extract as visual_extract


def parser_stream_pdf(*, objects=False, target='xref', damage=None, twice=False, predictor=False,
                      raw_xref=False, padding=0, object_mutation=None):
    content=(b'BT /F1 12 Tf 72 700 Td (A) Tj ET ' + (b'BT /F2 12 Tf 72 680 Td (B) Tj ET' if twice else b''))
    content+=b' '*padding
    original=pdf_bytes((content,));font=b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>'
    parts={int(m.group(1)):m.group(2) for m in re.finditer(rb'(?ms)^(\d+) 0 obj\n(.*?)\nendobj\n',original)}
    assert set(parts)==set(range(1,7))
    if twice:parts[4]=parts[4].replace(b'/F1 5 0 R',b'/F1 5 0 R\n/F2 9 0 R')
    if objects:del parts[5]
    elif twice:parts[9]=font
    def encode(data,kind):
        encoded=zlib.compress(data)
        if kind==target:
            if damage=='truncated':encoded=encoded[:-1]
            elif damage=='trailing':encoded+=b'ignored'
            elif damage=='concatenated':encoded+=zlib.compress(b'ignored')
        return encoded
    object_bytes=0
    if objects:
        header=b'5 0 '+(b'9 '+str(len(font)+1).encode()+b' ' if twice else b'')
        raw=header+font+(b' '+font if twice else b'')
        if object_mutation is not None:raw=object_mutation(raw)
        object_bytes=len(raw)
        encoded=encode(raw,'objects')
        parts[7]=b'<< /Type /ObjStm /N '+str(2 if twice else 1).encode()+b' /First '+str(len(header)).encode()+b' /Filter /FlateDecode /Length '+str(len(encoded)).encode()+b' >>\nstream\n'+encoded+b'\nendstream'
    prefix=b'%PDF-1.5\n%\xe2\xe3\xcf\xd3\n';offsets={}
    for number,body in sorted(parts.items()):
        offsets[number]=len(prefix);prefix+=str(number).encode()+b' 0 obj\n'+body+b'\nendobj\n'
    xref_number=8 if objects else 7;offsets[xref_number]=len(prefix);size=max(offsets)+1
    if twice:size=max(size,10)
    rows=[]
    for i in range(size):
        if objects and i in (5,9) and (i==5 or twice):rows.append(b'\2'+(7).to_bytes(4,'big')+(0 if i==5 else 1).to_bytes(2,'big'))
        elif i in offsets:rows.append(b'\1'+offsets[i].to_bytes(4,'big')+bytes(2))
        else:rows.append(b'\0'+bytes(4)+b'\xff\xff')
    raw=b''.join(rows)
    if damage=='extra_decoded' and target=='xref':raw+=b'junk'
    if predictor:
        rows=[raw[i:i+7] for i in range(0,len(raw),7)];previous=bytes(7);filtered=[]
        for row in rows:
            filtered.append(b'\2'+bytes((a-b)&255 for a,b in zip(row,previous)));previous=row
        raw=b''.join(filtered)
        if damage=='wrong_row_filter':raw=b'\1'+raw[1:]
        elif damage=='short_row':raw=raw[:-1]
        elif damage=='extra_row':raw+=bytes(8)
    encoded=raw if raw_xref else encode(raw,'xref')
    params=b' /DecodeParms << /Predictor 12 /Columns 7 >>' if predictor else b''
    if damage=='wrong_columns':params=params.replace(b'/Columns 7',b'/Columns 6')
    elif damage=='other_predictor':params=params.replace(b'/Predictor 12',b'/Predictor 15')
    elif damage=='wrong_colors':params=params.replace(b'>>',b'/Colors 3 >>')
    elif damage=='wrong_bits':params=params.replace(b'>>',b'/BitsPerComponent 4 >>')
    filter_declaration=b'' if raw_xref else b' /Filter /FlateDecode'
    body=prefix+str(xref_number).encode()+b' 0 obj\n<< /Type /XRef /Size '+str(size).encode()+b' /W [1 4 2] /Root 3 0 R /Length '+str(len(encoded)).encode()+filter_declaration+params+b' >>\nstream\n'+encoded+b'\nendstream\nendobj\nstartxref\n'+str(offsets[xref_number]).encode()+b'\n%%EOF\n'
    return body,len(content)+len(raw)+object_bytes


@pytest.mark.parametrize('extractor',[native_extract,visual_extract])
@pytest.mark.parametrize('objects,target',[(False,'xref'),(True,'xref'),(True,'objects')])
@pytest.mark.parametrize('damage',['truncated','trailing','concatenated'])
def test_parser_internal_flate_damage_is_a_technical_failure(extractor,objects,target,damage):
    body,_=parser_stream_pdf(objects=objects,target=target,damage=damage)
    with pytest.raises(ValueError):extractor(body)


@pytest.mark.parametrize('extractor',[native_extract,visual_extract])
@pytest.mark.parametrize('objects,twice',[(False,False),(True,False),(True,True)])
def test_valid_parser_streams_charge_every_decoded_byte_exactly_once(extractor,objects,twice):
    body,expected=parser_stream_pdf(objects=objects,twice=twice)
    result=extractor(body)
    assert result['decoded_stream_bytes']==expected
    if extractor is native_extract:assert result['text']=='A\nB' if twice else result['text']=='A'
    # The shared ceiling includes parser-internal streams and application data.
    with pytest.raises(ValueError,match='limit'):extractor(body,max_stream_bytes=expected-1)


@pytest.mark.parametrize('extractor',[native_extract,visual_extract])
def test_xref_decoded_entry_bytes_must_match_w_and_index(extractor):
    body,_=parser_stream_pdf(damage='extra_decoded')
    with pytest.raises(ValueError):extractor(body)


@pytest.mark.parametrize('extractor',[native_extract,visual_extract])
def test_parser_internal_streams_respect_cancellation(extractor):
    body,_=parser_stream_pdf(objects=True)
    class Cancel(Exception):pass
    def check():raise Cancel('stop')
    with pytest.raises(Cancel):extractor(body,check=check)


@pytest.mark.parametrize('extractor',[native_extract,visual_extract])
def test_xref_png_up_predictor_has_exact_rows_and_shared_byte_charge(extractor):
    body,expected=parser_stream_pdf(objects=True,twice=True,predictor=True)
    assert extractor(body)['decoded_stream_bytes']==expected
    with pytest.raises(ValueError,match='limit'):extractor(body,max_stream_bytes=expected-1)


@pytest.mark.parametrize('extractor',[native_extract,visual_extract])
@pytest.mark.parametrize('damage',['truncated','trailing','concatenated'])
def test_xref_png_up_does_not_hide_incomplete_flate(extractor,damage):
    body,_=parser_stream_pdf(objects=True,predictor=True,damage=damage)
    with pytest.raises(ValueError):extractor(body)


@pytest.mark.parametrize('extractor',[native_extract,visual_extract])
@pytest.mark.parametrize('damage',['wrong_row_filter','short_row','extra_row','wrong_columns',
                                   'other_predictor','wrong_colors','wrong_bits'])
def test_xref_png_up_invalid_rows_or_parameters_fail(extractor,damage):
    body,_=parser_stream_pdf(predictor=True,damage=damage)
    with pytest.raises(ValueError):extractor(body)


@pytest.mark.parametrize('extractor',[native_extract,visual_extract])
@pytest.mark.parametrize('predictor',[False,True])
def test_parser_application_and_provenance_share_document_limits(extractor,predictor):
    # Padding keeps the byte ceiling above receipt overhead, exercising the
    # aggregate decoded budget rather than the independent provenance cap.
    body,expected=parser_stream_pdf(objects=True,predictor=predictor,padding=10000)
    assert extractor(body,max_stream_bytes=expected)['decoded_stream_bytes']==expected
    with pytest.raises(ValueError,match='pdf_stream_limit$'):
        extractor(body,max_stream_bytes=expected-1)
    with pytest.raises(ValueError,match='pdf_stream_limit_provenance'):
        extractor(parser_stream_pdf(objects=True,predictor=predictor)[0],max_stream_bytes=500)


@pytest.mark.parametrize('extractor',[native_extract,visual_extract])
@pytest.mark.parametrize('raw_xref,predictor',[(True,False),(False,False),(False,True)])
def test_parser_receipts_retain_source_hashes_and_valid_accounting(extractor,raw_xref,predictor):
    from tradingagents.strategies.data_sources.filing_pdf import validate_pdf_stream_accounting
    from tradingagents.strategies.data_sources.filing_pdf_visual import validate_pdf_visual_receipt
    body,expected=parser_stream_pdf(objects=True,twice=True,raw_xref=raw_xref,predictor=predictor)
    result=extractor(body)
    assert validate_pdf_stream_accounting(result)
    if extractor is visual_extract:assert validate_pdf_visual_receipt(result)
    assert [row['kind'] for row in result['parser_streams']]==['xref','object']
    assert result['decoded_stream_bytes']==expected
    xref=result['parser_streams'][0]
    assert xref['predictor']==(12 if predictor else 1)
    assert xref['filters']==([] if raw_xref else ['/FlateDecode'])
    assert xref['inflated_bytes']==xref['decoded_bytes']+(xref['rows'] if predictor else 0)
    assert result['application_stream_bytes']+sum(row['charged_bytes'] for row in result['parser_streams'])==expected


@pytest.mark.parametrize('extractor',[native_extract,visual_extract])
@pytest.mark.parametrize('field,value',[
    ('index',1),('object_number',True),('generation',-1),('kind','font'),
    ('encoded_sha256','x'*64),('decoded_sha256','x'),('inflated_sha256','0'*64),
    ('charged_bytes',-1),('decoded_bytes',0),('inflated_bytes',0),('filters',['/ASCII85Decode']),
    ('decode_parameters',{'/Predictor':1,'/Columns':False}),('predictor',True),
    ('columns',0),('rows',0),('object_count',1),('first_object_byte',0)])
def test_parser_receipt_mutations_cannot_validate(extractor,field,value):
    from tradingagents.strategies.data_sources.filing_pdf import validate_pdf_stream_accounting
    from tradingagents.strategies.data_sources.filing_pdf_visual import validate_pdf_visual_receipt
    result=extractor(parser_stream_pdf(objects=True)[0])
    result['parser_streams'][0][field]=value
    validator=validate_pdf_stream_accounting if extractor is native_extract else validate_pdf_visual_receipt
    with pytest.raises(ValueError):validator(result)


@pytest.mark.parametrize('extractor',[native_extract,visual_extract])
@pytest.mark.parametrize('mutation',['duplicate','missing','application','total','predictor_charge'])
def test_parser_receipt_aggregate_mutations_fail(extractor,mutation):
    from tradingagents.strategies.data_sources.filing_pdf import validate_pdf_stream_accounting
    from tradingagents.strategies.data_sources.filing_pdf_visual import validate_pdf_visual_receipt
    result=extractor(parser_stream_pdf(objects=True,predictor=True)[0])
    if mutation=='duplicate':
        duplicate=copy.deepcopy(result['parser_streams'][0]);duplicate['index']=2
        result['parser_streams'].append(duplicate)
    elif mutation=='missing':result.pop('parser_streams')
    elif mutation=='application':result['application_stream_bytes']+=1
    elif mutation=='total':result['decoded_stream_bytes']-=1
    else:result['parser_streams'][0]['charged_bytes']-=result['parser_streams'][0]['rows']
    validator=validate_pdf_stream_accounting if extractor is native_extract else validate_pdf_visual_receipt
    with pytest.raises(ValueError):validator(result)
