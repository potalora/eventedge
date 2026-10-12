"""Independent structural HTML counterexamples and bounded proof controls."""
import copy

import pytest

from tradingagents.strategies.data_sources import filing_html_structural as h


def validated(body):
    receipt=h.extract_html_structural_evidence(body)
    h.validate_html_structural_receipt(receipt)
    return receipt


@pytest.mark.parametrize('scheme',['javascript','vbscript','data'])
@pytest.mark.parametrize('separator',['&#x09;','&#x0a;','&#x0d;','\t','\n','\r'])
def test_navigation_scheme_cannot_hide_browser_stripped_ascii_controls(scheme,separator):
    obfuscated=scheme[:2]+separator+scheme[2:]+':payload'
    with pytest.raises(h.FilingHTMLStructuralError):
        validated(('<p><a href="'+obfuscated+'">A</a></p>').encode())


XHTML=(b'<XBRL><?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">'
       b'<body><p>A</p></body></html></XBRL>')


@pytest.mark.parametrize('prefix,suffix',[(b'TAIL',b''),(b'',b'TAIL'),(b'&#32;TAIL',b''),(b'',b'&#65;')])
def test_sec_wrapper_rejects_nonwhitespace_text_outside_original_root(prefix,suffix):
    with pytest.raises(h.FilingHTMLStructuralError):
        validated(prefix+XHTML+suffix)


def test_sec_wrapper_allows_outer_ascii_whitespace():
    receipt=validated(b' \r\n'+XHTML+b'\n\t')
    assert receipt['visual_presence'] is True
    assert receipt['native_text_complete'] is False
    assert receipt['order_authority'] is False


@pytest.mark.parametrize('fragment',[
    b'<font face="Symbol">agreement</font>',
    b'<p style="opacity:0">agreement</p>',
    b'<p style="font-size:0">agreement</p>',
    b'<p style="visibility:hidden">agreement</p>',
    b'<p style="display:none">agreement</p>',
])
def test_source_projection_never_drops_hidden_or_font_dependent_text(fragment):
    receipt=validated(fragment)
    assert receipt['visual_presence'] is True
    assert h.structural_html_source_text(fragment,receipt)=='agreement'
    assert receipt['font_availability']=='unverified'
    assert receipt['text_visibility']=='unverified'
    assert receipt['native_text_complete'] is False
    assert receipt['source_text_use']=='incorporation_locator_only_not_analysis'


def test_receipt_rejects_resealed_plaintext_authority():
    receipt=validated(b'<p style="display:none">incorporated by reference</p>')
    altered=copy.deepcopy(receipt);altered['native_text_complete']=True
    for _ in range(4):altered['inventory_bytes']=len(h._json(altered))
    with pytest.raises(h.FilingHTMLStructuralError):h.validate_html_structural_receipt(altered)


@pytest.mark.parametrize('separator',['&#x09;','&#x0a;','&#x0d;'])
def test_namespaced_navigation_has_same_active_scheme_boundary(separator):
    body=('<XBRL><?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:ix="http://www.xbrl.org/2013/inlineXBRL" '
        'xmlns:link="http://www.xbrl.org/2003/linkbase" '
        'xmlns:xlink="http://www.w3.org/1999/xlink"><body><div><ix:header><ix:references>'
        '<link:schemaRef xlink:type="simple" xlink:href="ja'+separator+'vascript:payload"/>'
        '</ix:references></ix:header></div></body></html></XBRL>').encode()
    with pytest.raises(h.FilingHTMLStructuralError):validated(body)


def test_multiple_graphics_have_per_resource_not_aggregate_decoded_cap():
    from test_filing_html_structural import resource
    documents=[resource(f'scan{i}.png',bytes([65+i])*30) for i in range(3)]
    body=b'<img src="scan2.png"><img src="scan1.png"><img src="scan0.png"><img src="scan2.png">'
    calls=[]
    def read(document):
        calls.append(document['filename']);return document['body']
    receipt=h.extract_html_structural_evidence(body,documents=documents,body_reader=read,max_resource_bytes=80)
    h.validate_html_structural_receipt(receipt)
    assert calls==['scan0.png','scan1.png','scan2.png']
    assert len(receipt['resource_edges'])==4
    assert receipt['resource_decoded_bytes']==90
    assert all(r['decoded_bytes']==30 and 'body' not in r for r in receipt['resources'])
    assert receipt['encoded_image_validity']=='unverified'


def test_selected_projection_and_complete_receipt_share_one_result_cap():
    from test_filing_evidence import submission,parse
    from tradingagents.strategies.data_sources.filing_evidence import build_evidence_from_buffer
    body='<p style="display:none">'+('x'*1000)+'</p>'
    raw=submission([('8-K','main.htm',body)]);parsed=parse(raw)
    initial=build_evidence_from_buffer(parsed,raw,supported_document_policy='verified_supported_documents_v3')
    bound=initial['html_evidence']['main.htm']['inventory_bytes']+len(initial['units'][0]['text'].encode())
    rejected=build_evidence_from_buffer(parsed,raw,supported_document_policy='verified_supported_documents_v3',max_total_text_bytes=bound-1)
    assert rejected['structural_status']=='insufficient'
    assert rejected['issues'][0]['code']=='total_text_byte_limit'
    accepted=build_evidence_from_buffer(parsed,raw,supported_document_policy='verified_supported_documents_v3',max_total_text_bytes=bound)
    assert accepted['structural_status']=='complete'


@pytest.mark.parametrize('suffix',[b'&#32;',b'\xc2\xa0'])
def test_sec_wrapper_outside_whitespace_is_literal_ascii_and_parser_validator_agree(suffix):
    with pytest.raises(h.FilingHTMLStructuralError):
        h.extract_html_structural_evidence(XHTML+suffix)
