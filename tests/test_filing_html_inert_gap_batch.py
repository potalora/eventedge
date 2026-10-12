"""Finite inert syntax never certifies rendering, taxonomy semantics or analysis."""
import copy

import pytest

from tradingagents.strategies.data_sources import filing_html_structural as h
from test_filing_html_finite_expansion import xhtml, receipt, reseal


def typed(leaf='e:MeasureAxis.domain', value='1', *, uri='http://xbrl.sec.gov/ecd/2026', children=None):
    child=f'<{leaf}>{value}</{leaf}>' if children is None else children
    body=xhtml('<div><ix:header><ix:resources><i:context id="c"><i:entity>'
        '<i:identifier scheme="http://www.sec.gov/CIK">1</i:identifier><i:segment>'
        '<d:typedMember dimension="e:MeasureAxis">'+child+'</d:typedMember>'
        '</i:segment></i:entity><i:period><i:instant>2026-09-30</i:instant>'
        '</i:period></i:context></ix:resources></ix:header></div>')
    return body.replace(b'xmlns:dei=', ('xmlns:e="'+uri+'" xmlns:d="http://xbrl.org/2006/xbrldi" xmlns:dei=').encode())


@pytest.mark.parametrize('leaf,uri,value',[
    ('e:MeasureAxis.domain','http://xbrl.sec.gov/ecd/2026','1'),
    ('e:QName.domain','http://fasb.org/us-gaap/2026','e:Revenues'),
    ('e:date.domain','http://fasb.org/us-gaap/2026','2026-09-30'),
    ('e:RestatementDateAxis.domain','http://xbrl.sec.gov/ecd/2025','2025-12-31')])
def test_exact_bound_standard_typed_leaf_is_inventory_only(leaf,uri,value):
    result=receipt(typed(leaf,value,uri=uri))
    assert result['visual_presence'] and result['native_text_complete'] is False
    assert result['order_authority'] is False
    assert 'inline_xbrl' in result['obligations']
    forged=copy.deepcopy(result);forged['obligations']=[];forged['visual_presence']=False
    with pytest.raises(h.FilingHTMLStructuralError):h.validate_html_structural_receipt(reseal(forged))


@pytest.mark.parametrize('body',[
    typed(uri='https://evil.example/ecd/2026'),
    typed(uri='http://xbrl.sec.gov/ecd/2025'),
    typed(leaf='e:Unknown.domain'),typed(children=''),
    typed(children='<e:MeasureAxis.domain>1</e:MeasureAxis.domain>'*2),
    typed(children='text<e:MeasureAxis.domain>1</e:MeasureAxis.domain>'),
    typed(children='<e:MeasureAxis.domain><span>1</span></e:MeasureAxis.domain>'),
    typed(children='<e:MeasureAxis.domain style="display:none">1</e:MeasureAxis.domain>'),
    typed().replace(b'<i:segment>',b'<i:segment><e:MeasureAxis.domain>2</e:MeasureAxis.domain>'),
])
def test_unknown_rebound_nonleaf_or_incomplete_typed_member_fails(body):
    with pytest.raises(h.FilingHTMLStructuralError):receipt(body)


def test_exact_schema_ref_arcrole_preserved_and_namespace_checked():
    body=xhtml('<div><ix:header><ix:references><link:schemaRef xlink:type="simple" '
        'xlink:arcrole="http://www.xbrl.org/2003/linkbase" xlink:href="issuer.xsd"/>'
        '</ix:references></ix:header></div>')
    result=receipt(body)
    leaf=next(t for t in h.iter_html_structural_tokens(result) if t.get('tag')=='link:schemaRef')
    assert leaf['attributes']['xlink:arcrole']=='http://www.xbrl.org/2003/linkbase'
    for forged in [body.replace(b'2003/linkbase" xlink:href',b'2003/unknown" xlink:href'),
        body.replace(b'http://www.w3.org/1999/xlink',b'http://evil.example/xlink')]:
        with pytest.raises(h.FilingHTMLStructuralError):receipt(forged)


@pytest.mark.parametrize('style',[
    ';font-size:10pt;; ;font-size:12pt;',
    'width:calc(100% - 1px);display:inline-block',
    'height:calc(1pt + 2px)',
    'position:absolute;clip:rect(0px, 96px, 57px, 0px)',
    'clip:rect(auto, 10px, auto, 0px)',
    'text-underline-style:thick;text-justify-trim:punctuation;mso-bidi-font-weight:normal',
])
def test_finite_css_exact_values_order_and_no_layout_authority(style):
    result=receipt(('<p style="'+style+'">A</p>').encode())
    assert result['css_property_semantics']=='unverified' and result['visual_presence']
    token=next(h.iter_html_structural_tokens(result))
    assert token['attributes']['style']==style
    assert result['order_authority'] is False
    assert token['css']==h._css(style)


@pytest.mark.parametrize('style',[
    'width:calc(1px)','width:calc(100% - var(--x))','width:calc(100% * 1px)',
    'width:calc(100% - url(x))','clip:rect(0px,1px,2px)',
    'clip:rect(0px,1px,2px,3px,4px)','font-size:10pt;;bad;',
    'font-size:10pt;;font-size:;', ';;\u00a0;',
    'z-index:2147483648','z-index:-2147483649','width:calc(1000001px - 1px)',
])
def test_functions_and_separators_do_not_waive_unknown_malformed_or_numeric_limits(style):
    with pytest.raises(h.FilingHTMLStructuralError):receipt(('<p style="'+style+'">A</p>').encode())


@pytest.mark.parametrize('body',[
    b'<html version="-//XBRL International//DTD XHTML Inline XBRL 1.1//EN"><body>A</body></html>',
    b'<div border="0">A</div>',b'<table><tr height="17"><td>A</td></tr></table>',
    b'<table cellpadding="0pt" cellspacing="0px"><tr><td>A</td></tr></table>'
])
def test_static_presentation_lexemes_create_visual_obligation(body):
    result=receipt(body)
    assert result['visual_presence'] and result['order_authority'] is False


@pytest.mark.parametrize('style',[
    'transform:translate(0px, 0px) rotate(0.0deg);z-index:-2036331520',
    'z-index:-2147483648','z-index:2147483647','z-index:+251658240',
    'mso-spacerun:yes;text-align:middle',
])
def test_finite_transform_and_stacking_integer_are_exact_uncomputed(style):
    result=receipt(('<p style="'+style+'">A</p>').encode())
    assert result['visual_presence'] and result['css_layout']=='unverified'
    assert next(h.iter_html_structural_tokens(result))['attributes']['style']==style


@pytest.mark.parametrize('style',[
    'transform:rotate(var(--angle))','transform:translate(0px,0px) rotate(0deg) scale(1)',
    'transform:rotate(0deg) rotate(0deg)','transform:rotate(1000001deg)',
    'z-index:00000000000','z-index:1.1','z-index:-9223372036603117568',
])
def test_transform_and_stacking_bounds_remain_finite(style):
    with pytest.raises(h.FilingHTMLStructuralError):receipt(('<p style="'+style+'">A</p>').encode())


def test_mime_metadata_single_empty_separator_never_changes_encoding():
    body=b'<html><head><meta http-equiv="Content-Type" content="text/html;"></head><body>A</body></html>'
    result=receipt(body)
    assert next(t for t in h.iter_html_structural_tokens(result) if t.get('tag')=='meta')['attributes']['content']=='text/html;'
    with pytest.raises(h.FilingHTMLStructuralError):receipt(body.replace(b'text/html;',b'text/html;charset=shift_jis'))


@pytest.mark.parametrize('space',['\r','\n','\r\n','\t'])
@pytest.mark.parametrize('operator',['+','-'])
def test_multiline_static_calc_matches_its_declared_ascii_whitespace(space,operator):
    style='width:calc(100%'+space+operator+space+'1px)'
    result=receipt(('<p style="'+style+'">A</p>').encode())
    assert next(h.iter_html_structural_tokens(result))['css'][0]['value']==style[6:]


@pytest.mark.parametrize('style',[
    'flex-grow:1;align-content:start;column-count:2;column-gap:28.800000000000004px',
    'rotate:180deg;writing-mode:vertical-rl;word-wrap:break-word;break-after:column',
    'padding-bottom:-9.473903143468E-15pt',
    'text-justify:inter-ideograph',
    'mso-yfti-irow:0;mso-yfti-firstrow:yes;mso-yfti-lastrow:yes',
    'mso-fareast-font-family:"MS Mincho";mso-border-alt:solid windowtext .5pt',
])
def test_remaining_complete_static_declarations_preserve_exact_tokens(style):
    # Quoted font names are entity encoded inside this complete HTML attribute.
    import html
    body=('<p style="'+html.escape(style,quote=True)+'">A</p>').encode()
    result=receipt(body)
    assert next(h.iter_html_structural_tokens(result))['attributes']['style']==style
    assert result['visual_presence'] and result['css_property_semantics']=='unverified'


@pytest.mark.parametrize('style',[
    'padding:1e7pt','padding:1e999pt','padding:1e-999pt','padding:20..421%',
    'flex-grow:var(--x)','column-count:1001','column-gap:url(x)',
    'rotate:180rad','mso-border-alt:expression(x)',
])
def test_static_extensions_keep_magnitude_function_and_lexical_limits(style):
    with pytest.raises(h.FilingHTMLStructuralError):receipt(('<p style="'+style+'">A</p>').encode())


def test_identical_root_instance_binding_can_repeat_without_new_scope():
    body=xhtml('<div><ix:header><ix:resources><i:unit xmlns:i="http://www.xbrl.org/2003/instance" id="u">'
        '<i:measure>dei:USD</i:measure></i:unit></ix:resources></ix:header></div>')
    assert receipt(body)['visual_presence']
    with pytest.raises(h.FilingHTMLStructuralError):receipt(body.replace(b'<i:unit xmlns:i="http://www.xbrl.org/2003/instance"',b'<i:unit xmlns:i="http://evil.example/instance"'))


def test_exact_resource_default_profile_has_only_bound_prefixed_descendants():
    body=xhtml('<div><ix:header><ix:resources xmlns="http://www.xbrl.org/2003/instance">'
        '<i:context xmlns="" id="c"><i:entity><i:identifier scheme="http://www.sec.gov/CIK">1</i:identifier>'
        '</i:entity><i:period><i:instant>2026-09-30</i:instant></i:period></i:context>'
        '</ix:resources></ix:header></div><p>Later HTML.</p>')
    result=receipt(body)
    assert result['visual_presence'] and 'Later HTML.' in h.structural_html_source_text(body,result)
    assert [t['attributes']['xmlns'] for t in h.iter_html_structural_tokens(result)
        if t['kind'] in {'start','empty'} and t.get('tag') in {'ix:resources','i:context'}]==['http://www.xbrl.org/2003/instance','']
    for bad in [body.replace(b'<i:context xmlns=""',b'<context xmlns=""').replace(b'</i:context>',b'</context>'),
        body.replace(b'<i:context xmlns=""',b'<i:context xmlns="http://evil.example/"'),
        body.replace(b'<ix:resources xmlns="http://www.xbrl.org/2003/instance"',b'<ix:resources xmlns="http://evil.example/"'),
        body.replace(b'<p>Later',b'<p xmlns="http://www.xbrl.org/2003/instance">Later')]:
        with pytest.raises(h.FilingHTMLStructuralError):receipt(bad)


@pytest.mark.parametrize('style',[
    'border: #E6E6E6 1pt solid; padding: 0.75pt; text-align: justify',
    'color:#215E99;border-bottom:1px solid #215E99',
    'color:#1E7','background-color:#FADE2026',
])
def test_hex_color_lexemes_are_not_exponent_numbers(style):
    result=receipt(('<p style="'+style+'">A</p>').encode())
    token=next(h.iter_html_structural_tokens(result))
    assert token['attributes']['style']==style
    assert token['css']==h._css(style)
    assert result['visual_presence'] and result['order_authority'] is False


@pytest.mark.parametrize('style',[
    'border:#E6E6E6 1000001px solid',
    'border:#E6E6E6 1e7px solid',
    'border:#E6E6E6 12345678901234567890123456789012345e-20px solid',
    'color:#215E99;width:1000001px',
    'color:#215E99zz','color:#215E99999',
])
def test_hex_color_distinction_cannot_hide_actual_overflow_or_invalid_color(style):
    with pytest.raises(h.FilingHTMLStructuralError):receipt(('<p style="'+style+'">A</p>').encode())
