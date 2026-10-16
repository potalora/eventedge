"""Finite structural HTML receipts: whole bodies and exact local resource closure."""
import binascii
import hashlib
from copy import deepcopy
import pytest
from tradingagents.strategies.data_sources import filing_html_structural as html


def resource(name='scan.jpg', payload=b'opaque encoded image'):
    body = b'begin 644 ' + name.encode() + b'\n' + binascii.b2a_uu(payload) + b'`\nend\n'
    return {'type': 'GRAPHIC', 'sequence': 2, 'filename': name, 'body_start': 1000,
            'body_end': 1000 + len(body), 'body_sha256': hashlib.sha256(body).hexdigest(), 'body': body}


def extract(body, resources=(), **kw):
    return html.extract_html_structural_evidence(body, documents=resources,
        body_reader=lambda d: d['body'], **kw)


def test_complete_table_redline_css_receipt_with_no_text_authority():
    body = b'<!DOCTYPE html><html><head><title>T</title></head><body><table><tr><td style="font:10pt Times New Roman; border:Black 1pt solid">A &amp; B</td></tr></table><p><strike>old</strike>new</p></body></html>'
    receipt = extract(body)
    assert receipt['visual_presence'] is True
    assert receipt['native_text_complete'] is False
    assert receipt['css_layout'] == receipt['encoded_image_validity'] == 'unverified'
    assert receipt['order_authority'] is False
    assert receipt['input_sha256'] == hashlib.sha256(body).hexdigest()
    assert list(html.iter_html_structural_tokens(receipt))[0]['start'] == 0
    assert list(html.iter_html_structural_tokens(receipt))[-1]['end'] == len(body)
    assert receipt['obligations'] == ['css_declarations', 'redline', 'table_layout']
    html.validate_html_structural_receipt(receipt)


def test_image_only_full_same_original_closure_including_duplicate_edges():
    doc = resource()
    receipt = extract(b'<html><body><img src="scan.jpg"><img src="scan.jpg" alt="page"></body></html>', [doc])
    assert len(receipt['resource_edges']) == 2
    assert len(receipt['resources']) == 1
    assert receipt['resources'][0]['decoded_sha256'] == hashlib.sha256(b'opaque encoded image').hexdigest()
    assert receipt['resources'][0]['body_sha256'] == doc['body_sha256']
    assert receipt['resource_decoded_bytes'] == len(b'opaque encoded image')
    assert receipt['visual_presence'] is True


@pytest.mark.parametrize('body', [b'<table><tr><td>x</tr></table>', b'<p>x', b'<p x="1">x</p>',
    b'<p style="color:red; mystery:1">x</p>', b'<p style="width:calc(1px)">x</p>',
    b'<p style="background:url(x)">x</p>',
    b'<p style="display:invented">x</p>', b'<img src="https://bad/a.jpg">', b'<img src="../a.jpg">',
    b'<style>p{color:red}</style>', b'<script>x</script>', b'<svg></svg>', b'<p onclick="x">x</p>',
    b'<p title="a" title="b">x</p>', b'<!-- [if x]>x<![endif]-->', b'<p>&unknown;</p>',
    b'<table><td>x</td></table>', b'<p><div>x</div></p>', b'<img srcset="a.jpg 1x">',
    b'<table><tr><td colspan="0">x</td></tr></table>', b'<p>\xff</p>', b'<p>a\x00</p>',
    b'<?xml version="1.0"?><p>x</p>', b'<!DOCTYPE html SYSTEM "x"><p>x</p>'])
def test_unknown_malformed_unsafe_or_incomplete_structure_is_failure(body):
    with pytest.raises(html.FilingHTMLStructuralError):
        extract(body)


def test_missing_wrong_type_hash_and_incomplete_transport_fail():
    body = b'<img src="scan.jpg">'
    for docs in [[], [{**resource(), 'type':'EX-99'}], [{**resource(), 'body_sha256':'0'*64}],
                 [{**resource(), 'body':b'begin 644 scan.jpg\n'}], [resource(), resource()]]:
        with pytest.raises(html.FilingHTMLStructuralError):
            extract(body, docs)


def test_resource_caps_inventory_caps_and_inherited_deadline_fail():
    with pytest.raises(html.FilingHTMLStructuralError, match='byte_limit'):
        extract(b'<table></table>', max_html_bytes=2)
    with pytest.raises(html.FilingHTMLStructuralError, match='inventory'):
        extract(b'<table></table>', max_inventory_bytes=2)
    with pytest.raises(html.FilingHTMLStructuralError, match='resource'):
        extract(b'<img src="scan.jpg">', [resource()], max_resource_bytes=2)
    def expired():
        raise TimeoutError('original inherited deadline')
    with pytest.raises(TimeoutError, match='inherited'):
        extract(b'<table></table>', check=expired)


def test_receipt_rejects_extra_fields_and_incomplete_token_coverage():
    receipt = extract(b'<table></table>')
    for forged in [{**receipt, 'text':'invented'}, {**receipt, 'tokens': receipt['tokens'][:-1]}]:
        with pytest.raises(html.FilingHTMLStructuralError):
            html.validate_html_structural_receipt(forged)


def v3_source(body, graphics=()):
    from test_filing_evidence import submission, parse, ACCESSION, OBSERVED
    from tradingagents.strategies.data_sources.filing_evidence import build_evidence_from_buffer
    from tradingagents.strategies.data_sources import filing_supported_policy as policy
    raw=submission([('8-K','main.htm',body)]+[(d['type'],d['filename'],d['body'].decode('ascii')) for d in graphics])
    parsed=parse(raw)
    evidence=build_evidence_from_buffer(parsed,raw,supported_document_policy='verified_supported_documents_v3')
    evidence['source_url']=f'https://www.sec.gov/Archives/edgar/data/2065397/{ACCESSION.replace("-", "")}/{ACCESSION}.txt'
    original={'identity':{k:evidence[k] for k in ('accession','form','filing_date','source_url')},'observed_at':OBSERVED,
              'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),'complete':True}
    proof=policy.build_local_source_proof(evidence,original=original,raw_submission=raw)
    inputs={'source_proofs':{ACCESSION:proof},'originals':{ACCESSION:original},'occurrences':[{'collection':'filings','index':0}]}
    return evidence,raw,inputs


def test_v3_table_deferral_reconstructed_from_original_and_resealed_forgery_rejected():
    from test_filing_evidence import ACCESSION
    from tradingagents.strategies.data_sources import filing_supported_policy as policy
    evidence,raw,inputs=v3_source('<html><body><table><tr><td>full material</td></tr></table></body></html>')
    assert evidence['units'][0]['text']=='full material'
    assert evidence['html_evidence']['main.htm']['source_text_use']=='incorporation_locator_only_not_analysis'
    assert evidence['units'][0]['representation']=='structural_html'
    result=policy.evaluate_supported_disposition(evidence,**inputs)
    assert result['status']=='deferred',result
    assert result['reasons'][0]['proof_kind']=='structural_html'
    assert policy.validate_supported_disposition(result,evidence,**inputs,raw_submissions={ACCESSION:raw})==result
    evidence['html_evidence']['main.htm']['token_hashes'][0]='0'*64
    proof=inputs['source_proofs'][ACCESSION]
    proof['evidence_sha256']=policy._digest(evidence)
    proof['html']['main.htm']['receipt_sha256']=policy._digest(evidence['html_evidence']['main.htm'])
    proof['source_proof_sha256']=policy._digest({k:v for k,v in proof.items() if k!='source_proof_sha256'})
    provisional=policy.evaluate_supported_disposition(evidence,**inputs)
    with pytest.raises(policy.SupportedDispositionError):
        policy.validate_supported_disposition(provisional,evidence,**inputs,raw_submissions={ACCESSION:raw})


def test_v3_image_only_selected_exhibit_closure_is_not_empty_text_failure():
    from tradingagents.strategies.data_sources import filing_supported_policy as policy
    evidence,raw,inputs=v3_source('<html><body><img src="scan.jpg"></body></html>',[resource()])
    assert evidence['issues']==[]
    assert len(evidence['html_evidence']['main.htm']['resources'])==1
    assert policy.evaluate_supported_disposition(evidence,**inputs)['status']=='deferred'


def test_v3_missing_resource_and_other_required_failure_have_precedence():
    from tradingagents.strategies.data_sources import filing_supported_policy as policy
    evidence,raw,inputs=v3_source('<table><tr><td>x<img src="missing.jpg"></td></tr></table>')
    result=policy.evaluate_supported_disposition(evidence,**inputs)
    assert result['status']=='failed'
    assert evidence['issues'][0]['code']=='html_missing_or_ambiguous_resource'


def test_html_24mib_only_new_policy_with_pdf_and_text_limits_unchanged():
    from test_filing_evidence import submission, parse
    from tradingagents.strategies.data_sources.filing_evidence import build_evidence_from_buffer
    body='<p>'+('a'*(16*1024**2))+'</p>'
    raw=submission([('8-K','main.htm',body)])
    parsed=parse(raw,max_submission_bytes=64*1024**2)
    old=build_evidence_from_buffer(parsed,raw,supported_document_policy='verified_supported_documents_v2')
    assert old['issues'][0]['code']=='document_byte_limit'
    new=build_evidence_from_buffer(parsed,raw,supported_document_policy='verified_supported_documents_v3')
    assert new['issues']==[]
    assert len(new['units'][0]['text'])==16*1024**2
    for suffix in ('txt','xml','pdf'):
        raw=submission([('8-K','main.'+suffix,body)])
        parsed=parse(raw,max_submission_bytes=64*1024**2)
        assert build_evidence_from_buffer(parsed,raw,supported_document_policy='verified_supported_documents_v3')['issues'][0]['code']=='document_byte_limit'


@pytest.mark.parametrize('declaration', ['width:1055', 'display:flex', 'visibility:hidden',
    'font-kerning:none', 'justify-content:flex-start', 'align-items:baseline', 'box-sizing:border-box',
    'flex-direction:column', 'position:relative', 'top:-4.6899999999999995pt', 'tab-size:26.66px',
    '-webkit-text-size-adjust:100%', 'margin-inline-start:auto', 'border-spacing:0',
    'table-layout:auto', 'min-width:fit-content', 'text-decoration:underline solid',
    'text-indent:-1.351%', 'border-bottom:0.5pt solid #ffffff03', 'border-bottom:0.5pt solid',
    'margin:auto!important', 'min-width:1.3419508119154764%'])
def test_finite_retained_css_declarations_are_inventoried_without_layout_claim(declaration):
    receipt=extract(('<p style="'+declaration+'">all material deferred</p>').encode())
    assert receipt['css_layout']=='unverified'
    assert receipt['visual_presence'] is True
    assert list(html.iter_html_structural_tokens(receipt))[0]['css'][0]['property']==declaration.split(':')[0]


def test_explicit_finite_empty_paragraph_omission_in_image_only_document():
    receipt=extract(b'<div><p><hr noshade><p> \n<div><img src="scan.jpg"></div><p> </div>',[resource()])
    assert [t['implicit_closes'] for t in html.iter_html_structural_tokens(receipt) if t.get('implicit_closes')]==[['p'],['p'],['p']]
    html.validate_html_structural_receipt(receipt)


@pytest.mark.parametrize('body',[b'<div><p>material<hr></div>',b'<div><p>material</div>',b'<p>'])
def test_populated_or_eof_unclosed_paragraph_is_never_recovered(body):
    with pytest.raises(html.FilingHTMLStructuralError):extract(body)


def test_numeric_entity_stops_at_its_semicolon_before_following_source_text():
    receipt=extract(b'<table><tr><td>&#160;Income &#34;2025 &#x41;BC</td></tr></table>')
    assert receipt['visual_presence'] is True


def test_repeated_complete_tokens_use_compact_provenance_without_dropping_spans():
    body=b'<div>'+b'<p style="font-size:10pt">material</p>'*10000+b'</div>'
    receipt=extract(body,max_inventory_bytes=2*1024**2)
    rows=list(html.iter_html_structural_tokens(receipt))
    assert len(rows)==30002
    assert rows[0]['start']==0 and rows[-1]['end']==len(body)
    assert all(rows[i]['end']==rows[i+1]['start'] for i in range(len(rows)-1))
    html.validate_html_structural_receipt(receipt)


@pytest.mark.parametrize('body', [b'content<!DOCTYPE html><p>x</p>',
    b'<html><head><meta http-equiv="content-type" content="text/html; charset=windows-1252"></head><body><table></table></body></html>',
    b'<html><head></head><head></head><body><table></table></body></html>',
    b'<html><body><table></table></body><head></head></html>',
    b'<html><body><table></table></body></html><html><body>x</body></html>'])
def test_document_declarations_and_wrapper_order_do_not_need_browser_recovery(body):
    with pytest.raises(html.FilingHTMLStructuralError):extract(body)


def test_validator_uses_inherited_deadline():
    receipt=extract(b'<table></table>')
    def expired():raise TimeoutError('inherited validator deadline')
    with pytest.raises(TimeoutError):html.validate_html_structural_receipt(receipt,check=expired)


def test_inventory_budget_charges_complete_serialized_receipt():
    import json
    receipt=extract(b'<table></table>')
    encoded=json.dumps(receipt,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode('ascii')
    assert receipt['inventory_bytes']==len(encoded)
    with pytest.raises(html.FilingHTMLStructuralError,match='inventory'):
        extract(b'<table></table>',max_inventory_bytes=len(encoded)-1)



def test_same_original_graphic_sec_padding_rules_bind_exact_payload_without_repair():
    doc=resource(payload=b'a')
    # One declared payload byte has two ignored UU sextets. Nonzero values in
    # only those positions are the explicitly framed SEC transport convention.
    lines=doc['body'].splitlines()
    lines[1]=lines[1][:-2]+b'AB'
    body=b'\n'.join(lines)+b'\n'
    doc={**doc,'body':body,'body_end':doc['body_start']+len(body),'body_sha256':hashlib.sha256(body).hexdigest()}
    receipt=extract(b'<img src="scan.jpg">',[doc])
    assert receipt['resources'][0]['decoded_sha256']==hashlib.sha256(b'a').hexdigest()
    assert receipt['resources'][0]['ignored_padding_sextets']==2
    lines[1]=binascii.b2a_uu(b'abc').rstrip(b'\n').rstrip(b' ')
    body=b'\n'.join(lines)+b'\n'
    doc={**doc,'body':body,'body_end':doc['body_start']+len(body),'body_sha256':hashlib.sha256(body).hexdigest()}
    receipt=extract(b'<img src="scan.jpg">',[doc])
    assert receipt['resources'][0]['decoded_sha256']==hashlib.sha256(b'abc').hexdigest()


@pytest.mark.parametrize('damage',['filename','trailing','too_long','terminator','encoded_character'])
def test_graphic_transport_damage_cannot_be_deferred(damage):
    doc=resource()
    lines=doc['body'].splitlines()
    if damage=='filename':lines[0]=b'begin 644 other.jpg'
    elif damage=='trailing':lines.append(b'unaccounted trailing bytes')
    elif damage=='too_long':lines[1]+=b'A'
    elif damage=='terminator':lines[-2]=b'A'
    else:lines[1]=lines[1][:-1]+b'~'
    body=b'\n'.join(lines)+b'\n'
    doc={**doc,'body':body,'body_end':doc['body_start']+len(body),'body_sha256':hashlib.sha256(body).hexdigest()}
    with pytest.raises(html.FilingHTMLStructuralError):extract(b'<img src="scan.jpg">',[doc])


def test_structural_source_projection_keeps_hidden_text_and_inline_word_continuations():
    body=b'<div><p style="display:none">incorpo<span>rated</span> by reference Secret Contract.</p><p>Visible.</p></div>'
    receipt=extract(body)
    text=html.structural_html_source_text(body,receipt)
    assert text=='incorporated by reference Secret Contract. Visible.'
    assert receipt['source_text_sha256']==hashlib.sha256(text.encode()).hexdigest()
    assert receipt['source_text_use']=='incorporation_locator_only_not_analysis'


def test_source_font_variant_is_finite_declaration_without_glyph_or_layout_authority():
    receipt=extract(b'<p style="font-variant:small-caps">incorporated by reference</p>')
    assert receipt['font_availability']=='unverified' and receipt['visual_presence'] is True
    with pytest.raises(html.FilingHTMLStructuralError):extract(b'<p style="font-variant:unknown">text</p>')
