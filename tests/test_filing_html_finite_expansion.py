"""Finite retained-source HTML grammar; source projection never proves visuals."""
import copy

import pytest

from tradingagents.strategies.data_sources import filing_html_structural as h


def receipt(body):
    result = h.extract_html_structural_evidence(body)
    h.validate_html_structural_receipt(result)
    return result


def reseal(value):
    for _ in range(4):
        value['inventory_bytes'] = len(h._json(value))
    return value


@pytest.mark.parametrize('fragment', [
    '<font face="Wingdings">A</font>', '<font size="7">A</font>',
    '<font color="white">A</font>', '<p align="right">A</p>',
    '<html><body text="white">A</body></html>', '<sup>2</sup>', '<sub>2</sub>',
    '<pre>A  B</pre>', '<small>A</small>', '<strong>A</strong>',
    '<ul><li>A</li></ul>', '<hr>',
    '<p xml:space="preserve">A  B</p>',
])
def test_presentational_source_has_positive_visual_obligation(fragment):
    result = receipt(fragment.encode())
    assert result['visual_presence'] is True
    assert result['font_availability'] == 'unverified'
    assert result['order_authority'] is False
    forged = copy.deepcopy(result)
    forged['obligations'] = []
    forged['visual_presence'] = False
    with pytest.raises(h.FilingHTMLStructuralError):
        h.validate_html_structural_receipt(reseal(forged))


NS = ('xmlns="http://www.w3.org/1999/xhtml" '
      'xmlns:ix="http://www.xbrl.org/2013/inlineXBRL" '
      'xmlns:i="http://www.xbrl.org/2003/instance" '
      'xmlns:link="http://www.xbrl.org/2003/linkbase" '
      'xmlns:xlink="http://www.w3.org/1999/xlink" '
      'xmlns:dei="http://xbrl.sec.gov/dei/2026"')


def xhtml(content):
    return ('<XBRL>\r<?xml version="1.0" encoding="UTF-8"?>'
            '<html '+NS+'><head><meta charset="utf-8"/></head><body>'
            +content+'</body></html></XBRL>').encode()


def test_complete_sec_wrapper_namespace_aliases_explicit_empty_tokens():
    body = xhtml('<div style="display:none"><ix:header><ix:hidden>'
        '<ix:nonNumeric contextRef="c-1" name="dei:EntityRegistrantName" id="f-1">Issuer</ix:nonNumeric>'
        '</ix:hidden><ix:references><link:schemaRef xlink:type="simple" xlink:href="issuer.xsd"/>'
        '</ix:references><ix:resources><i:context id="c-1"><i:entity>'
        '<i:identifier scheme="http://www.sec.gov/CIK">123</i:identifier></i:entity>'
        '<i:period><i:instant>2026-09-30</i:instant></i:period></i:context>'
        '</ix:resources></ix:header></div><table><tr><td/><td>A</td></tr></table>')
    result = receipt(body)
    assert result['version'] == 'sec-html-structural-v2'
    assert any(t['kind'] == 'empty' and t['tag'] == 'td' for t in h.iter_html_structural_tokens(result))
    assert 'inline_xbrl' in result['obligations']
    assert result['visual_presence'] is True
    assert h.structural_html_source_text(body, result) == 'Issuer1232026-09-30 A'


@pytest.mark.parametrize('body', [
    b'<?xml version="1.0"?><p>A</p>',
    b'<html><body><td/></body></html>',
    b'<html><body><ix:nonNumeric>A</ix:nonNumeric></body></html>',
    xhtml('<ix:nonNumeric contextRef="c" name="dei:X">A</ix:nonFraction>'),
    xhtml('<ix:script>A</ix:script>'),
    xhtml('<script>A</script>'),
    xhtml('<ix:nonNumeric xmlns:ix="http://evil.example/">A</ix:nonNumeric>'),
    xhtml('<ix:nonNumeric onclick="run()">A</ix:nonNumeric>'),
    xhtml('<p>A<div>B</div></p>'),
    xhtml('<ix:nonNumeric>A</ix:nonnumeric>'),
    xhtml('<br>'), xhtml('<p title=unquoted>A</p>'),
    xhtml('<p>A &nbsp; B</p>'), xhtml('<p>A & B</p>'),
    xhtml('<p>A</p>')+b'<p>B</p>',
])
def test_namespaces_do_not_waive_malformed_active_or_unknown_input(body):
    with pytest.raises(h.FilingHTMLStructuralError):
        receipt(body)


@pytest.mark.parametrize('style', [
    'display:inline-table;clear:both;-sec-extract:summary',
    'border-width:0pt;border-top-width:0px;border-style:solid none none',
    'background:rgb(204,238,255);border-bottom:1px solid rgb(0, 0, 0)',
    'font:10pt/12pt Times New Roman, Times, Serif;overflow:hidden',
    'text-decoration:underline solid #000000;widows:3;orphans:2',
    '-sec-ix-hidden:Hidden_dei_EntityRegistrantName;float:left',
    'font-family:&quot;\x27Times New Roman\x27,\x27Times\x27,\x27serif\x27&quot;',
    'position:absolute;top:50%;transform:translate(0,-50%);width:100%',
    'padding-right:-2.13pt;text-align:justify',
    'cursor:auto;opacity:1;text-align:start;background-color:whitesmoke',
    'background-color:navajowhite',
    'margin-bottom:12pt;color:#auto',
    '-sec-ix-hidden:Fact_545b9800862c459c861730905ee474ab',
])
def test_retained_finite_css_is_source_bound_and_visual(style):
    result = receipt(('<div style="'+style+'">A</div>').encode())
    assert result['obligations'] == ['css_declarations']
    assert result['css_property_semantics'] == 'unverified'


@pytest.mark.parametrize('style', [
    'background:url(secret.png)', 'color:rgb(256,0,0)',
    'font:expression(alert(1))',
    '-sec-ix-hidden:x y', 'clear:sideways', 'border-width:99999999px',
    'transform:translate(secret,0)', 'transform:rotate(45deg)',
    'margin-top:1000001pt', '-sec-ix-hidden:'+('x'*257),
])
def test_finite_css_rejects_unknown_active_invalid_and_unbounded(style):
    with pytest.raises(h.FilingHTMLStructuralError):
        receipt(('<div style="'+style+'">A</div>').encode())


@pytest.mark.parametrize('fragment', [
    '<font face="Wingdings">A</font>',
    '<p style="padding-right:-2.13pt">exact signed source</p>',
    '<p style="color:#auto">exact inert source color token</p>',
    xhtml('<ix:nonNumeric name="dei:EntityRegistrantName" contextRef="c">Issuer</ix:nonNumeric>').decode(),
])
def test_positive_visual_obligations_defer_complete_original_without_analysis(fragment):
    from test_filing_evidence import ACCESSION
    from test_filing_html_structural import v3_source
    from tradingagents.strategies.data_sources import filing_supported_policy as policy
    from tradingagents.strategies.data_sources.filing_assessment import _validate_evidence
    evidence, raw, inputs = v3_source(fragment)
    assert evidence['units'][0]['representation'] == 'structural_html'
    assert _validate_evidence(evidence, allow_unsupported_documents=True)
    with pytest.raises(ValueError):
        _validate_evidence(evidence)
    result = policy.evaluate_supported_disposition(evidence, **inputs)
    assert result['status'] == 'deferred', result
    assert result['reasons'][0]['code'] == 'unsupported_selected_visual_content'
    assert result['reasons'][0]['order_authority'] is False
    assert policy.validate_supported_disposition(result, evidence, **inputs,
        raw_submissions={ACCESSION:raw}) == result


def test_explicit_empty_xhtml_anchor_keeps_independent_incorporation_duty():
    from test_filing_ownership_unit_references import with_exhibit
    from tradingagents.strategies.data_sources import filing_ownership_lineage as lineage
    target='0001193125-26-000002'
    url='https://www.sec.gov/Archives/edgar/data/42/'+target.replace('-','')+'/agreement.htm'
    evidence, body, _=with_exhibit('',html=xhtml('<p>Incorporated by reference <a href="'+url+'"/>.</p>').decode())
    assert evidence['issues']==[]
    reference=lineage.ownership_facts(evidence,body)['incorporations'][0]
    assert reference['target_accessions']==[target]
    assert reference['target_documents']==[{'accession':target,'filename':'agreement.htm'}]


def test_leaf_xlink_binding_is_local_exact_and_does_not_leak():
    body=xhtml('<ix:header><ix:references>'
        '<link:schemaRef xmlns:leaf="http://www.w3.org/1999/xlink" leaf:type="simple" leaf:href="issuer.xsd"/>'
        '</ix:references></ix:header>')
    assert receipt(body)['visual_presence'] is True
    redundant=body.replace(b'<link:schemaRef xmlns:leaf=',
        b'<link:schemaRef xmlns:link="http://www.xbrl.org/2003/linkbase" xmlns:leaf=')
    assert receipt(redundant)['visual_presence'] is True
    with pytest.raises(h.FilingHTMLStructuralError):
        receipt(redundant.replace(b'xmlns:link="http://www.xbrl.org/2003/linkbase" xmlns:leaf=',
            b'xmlns:link="http://evil.example/linkbase" xmlns:leaf='))
    forged=body.replace(b'</body>',b'<p leaf:href="other.xsd">A</p></body>')
    with pytest.raises(h.FilingHTMLStructuralError):receipt(forged)
    later=body.replace(b'</ix:references>',
        b'<link:schemaRef leaf:type="simple" leaf:href="second.xsd"/></ix:references>')
    with pytest.raises(h.FilingHTMLStructuralError):receipt(later)
    for bad in (body.replace(b'www.w3.org/1999/xlink" leaf',b'evil.example/1999/xlink" leaf'),
                body.replace(b'leaf:type="simple"',b'leaf:type="extended"')):
        with pytest.raises(h.FilingHTMLStructuralError):receipt(bad)


def test_extra_nonzero_uu_sextet_is_failure_not_a_visual_deferral():
    import binascii
    import hashlib
    from test_filing_html_structural import extract
    line=b'..4444 %%%% !1110!_]D!'
    # Independent stdlib transport decoding rejects the exact retained line.
    with pytest.raises(binascii.Error):binascii.a2b_uu(line)
    body=b'begin 644 scan.jpg\n'+line+b'\n`\nend\n'
    resource={'type':'GRAPHIC','sequence':2,'filename':'scan.jpg',
        'body_start':0,'body_end':len(body),'body_sha256':hashlib.sha256(body).hexdigest(),'body':body}
    with pytest.raises(h.FilingHTMLStructuralError,match='resource_uu_line'):
        extract(b'<img src="scan.jpg">',[resource])


def test_repeated_css_declarations_keep_every_value_priority_and_order():
    result=receipt(b'<p style="display:none;display:block!important;display:inline">all source</p>')
    token=next(h.iter_html_structural_tokens(result))
    assert token['css']==[
        {'property':'display','value':'none','important':False},
        {'property':'display','value':'block','important':True},
        {'property':'display','value':'inline','important':False}]
    assert result['visual_presence'] is True
    assert result['css_layout']==result['css_property_semantics']=='unverified'
    with pytest.raises(h.FilingHTMLStructuralError):
        receipt(b'<p style="color:red" style="color:blue">A</p>')
    with pytest.raises(h.FilingHTMLStructuralError):
        receipt(b'<p style="display:none;display:url(secret)">A</p>')
