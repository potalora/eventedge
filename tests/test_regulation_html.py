"""HTML completeness uses a checked inert subset, without browser/source calls."""
import json

import pytest

from tradingagents.strategies.data_sources.regulation_evidence import (
    _text, build_regulation_evidence, validate_regulation_evidence,
)
from test_regulation_evidence import DOC, STAMP, fixture


def text(body):
    return _text(('<html><body>' + body + '</body></html>').encode(), 'html', None)


def build_html(body):
    row, detail, bodies = fixture()
    bodies[DOC] = body
    detail['data']['attributes']['fileFormats'][0]['size'] = len(body)
    return build_regulation_evidence(row, json.dumps(detail).encode(), bodies, observed_at=STAMP)


def test_inline_composition_cannot_manufacture_clause_or_sentence_boundaries():
    assert text('<p>This does <b>not</b> apply to <span>AC</span><strong>ME</strong> CORPORATION.</p>') == 'This does not apply to ACME CORPORATION.'
    assert text('<p>ACME<b>.</b> The decision is final.</p>') == 'ACME. The decision is final.'
    assert text('<p>inter<span>vening</span> amendments</p>') == 'intervening amendments'


def test_blocks_and_breaks_supply_explicit_boundaries_only():
    assert text('<h1>Title</h1><div><p>First\n  paragraph.</p><p>Second<br>line.</p></div>') == 'Title\nFirst paragraph.\nSecond\nline.'


@pytest.mark.parametrize('body', [
    '<input value="material new requirement">', '<input type="hidden" value="material">',
    '<textarea>Material omitted text</textarea>', '<select><option>Material alternative</option></select>',
    '<button>Material option</button>', '<details><summary>Title</summary>Material text</details>',
    '<output>Material calculated value</output>', '<progress value="12"></progress>',
    '<iframe srcdoc="material"></iframe>', '<object data="rule.pdf"></object>',
    '<template>Material condition</template>', '<noscript>Material alternate</noscript>',
    '<script>document.write("material")</script>', '<custom-rule>Material</custom-rule>',
    '<a href="rule.pdf">Required incorporated agreement</a>',
    '<table><tr><th>Rate</th><th>Year</th></tr><tr><td>2</td><td>2027</td></tr></table>',
    '<pre>Rate  Year\n2     2027</pre>', '<ul><li>Required nested scope</li></ul>',
    '<sup>2</sup>', '<del>Old requirement</del>', '<ins>New requirement</ins>',
])
def test_unsupported_rendering_controls_embeddings_and_layout_fail(body):
    with pytest.raises(ValueError, match='unsupported_regulation_embedded_content'):
        text('<p>Ordinary text.</p>' + body)


@pytest.mark.parametrize('body', [
    '<p style="color:white">Material invisible</p>',
    '<div style="display:none"><p>Material inherited hidden text</p></div>',
    '<div style="visibility:hidden"><b>Material inherited hidden text</b></div>',
    '<p style="opacity:0">Material text</p>', '<p style="font-size:0">Material text</p>',
    '<p style="position:absolute;left:-10000px">Material text</p>',
    '<p style="display:table-cell">2</p>', '<p style="color:red">Redline</p>',
    '<p hidden>Material text</p>', '<p inert>Material text</p>',
    '<p dir="rtl">Material text</p>', '<p onclick="loadMore()">Material text</p>',
    '<p title="Material tooltip">Ordinary text</p>',
])
def test_any_style_or_presentational_interactive_attribute_fails(body):
    with pytest.raises(ValueError):
        text(body)


@pytest.mark.parametrize('head', [
    '<link rel="stylesheet" href="material.css">',
    '<style>p { color:white }</style>', '<style>p::before { content:"Material" }</style>',
    '<meta http-equiv="refresh" content="0;url=rule.html">',
    '<base href="https://example.org/">',
])
def test_head_resources_cannot_be_silently_discarded(head):
    with pytest.raises(ValueError):
        _text(('<html><head>' + head + '</head><body><p>Ordinary text.</p></body></html>').encode(), 'html', None)


@pytest.mark.parametrize('body', [
    '<p>Unclosed', '<p><b>Crossed</p></b>', '<p>One<p>Two</p>',
    '<b>One<p>Two</p></b>', '<div><p>Text</div></p>', '<p/>Text',
    '<p>Text</p bogus>', '<p>Text</unknown>', '<p id="one" id="two">Text</p>',
    '<p>Text <</p>', '<!-- unclosed comment', '<?render material?>',
])
def test_malformed_or_browser_repaired_markup_is_not_supported(body):
    with pytest.raises(ValueError):
        text(body)


@pytest.mark.parametrize('raw', [
    b'<html><body><p>One</p></body><body><p>Two</p></body></html>',
    b'<html><body><p>One</p></body></html><html><body><p>Two</p></body></html>',
    b'Visible before root<html><body><p>Text</p></body></html>',
    b'<html><head>Visible undecoded head</head><body><p>Text</p></body></html>',
])
def test_complete_framing_does_not_excuse_invalid_document_structure(raw):
    with pytest.raises(ValueError):
        _text(raw, 'html', None)


def test_inert_head_charset_and_inline_entities_are_retained_without_boundaries():
    raw = b'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>Rule title</title></head><body><p id="scope">ACME &amp; CO<span>RP</span>. &#38; Rule &lt; 4.</p></body></html>'
    assert _text(raw, 'html', None) == 'Rule title\nACME & CORP. & Rule < 4.'


def test_inert_html_stays_original_bound_and_reproducible():
    raw = b'<html><body><h1>Rule title</h1><p>This does <b>not</b> apply to ACME CORPORATION.</p></body></html>'
    value = build_html(raw)
    assert value['units'][0]['text'] == 'Rule title\nThis does not apply to ACME CORPORATION.'
    assert value['content_status'] == 'complete'
    assert validate_regulation_evidence(value) == value


@pytest.mark.parametrize('raw', [
    b'<html><body><p>Text</p><input value="material"></body></html>',
    b'<html><head><link rel="stylesheet" href="required.css"></head><body><p>Text</p></body></html>',
])
def test_omitted_material_reproductions_fail_complete_evidence(raw):
    with pytest.raises(ValueError):
        build_html(raw)


def test_deadline_callback_remains_live_inside_nested_inline_extraction():
    failure = TimeoutError('html deadline')
    calls = 0
    def check():
        nonlocal calls
        calls += 1
        if calls == 8:
            raise failure
    with pytest.raises(TimeoutError) as info:
        _text(b'<html><body><p>A <b>material</b> requirement.</p></body></html>', 'html', check)
    assert info.value is failure


@pytest.mark.parametrize('body', [
    '<p>&unknown;</p>', '<p>&amp missing terminator</p>', '<p>R&D</p>',
    '<p>&#0;</p>', '<p>&#128;</p>', '<p>&#xD800;</p>', '<p>&#x110000;</p>',
    '<p>&#xFFFD;</p>', '<p>&#x7F;</p>', '<p>&#1;</p>',
    '<!bogus><p>Ordinary text.</p>', '<![CDATA[Material text]]><p>Ordinary text.</p>',
    '<!--[if IE]>Material alternate<![endif]--><p>Ordinary text.</p>',
    '<p>One</p><!-- misplaced -- comment --><p>Two</p>',
    '<p>One</p></p>', '<p>One</p >Extra</body bogus>',
])
def test_unresolved_entities_and_repaired_declarations_fail(body):
    with pytest.raises(ValueError):
        text(body)


def test_regular_comments_and_supported_entities_preserve_inline_composition():
    assert text('<p>AC<!-- ordinary comment -->ME&nbsp;&amp;&nbsp;CO.</p>') == 'ACME\xa0&\xa0CO.'


@pytest.mark.parametrize('raw', [
    b'<!DOCTYPE html SYSTEM "rule.dtd"><html><body><p>Text</p></body></html>',
    b'<!DOCTYPE html><!DOCTYPE html><html><body><p>Text</p></body></html>',
    b'<html><head><meta charset="windows-1252"></head><body><p>Text</p></body></html>',
    b'<html><body><p>Text</p><head><title>Misplaced</title></head></body></html>',
    b'<html><body><p class="hidden">Text</p></body></html>',
    b'<html><body><div><p>Text</p></div></body></html> trailing material',
])
def test_encoding_external_declarations_and_repaired_document_structure_fail(raw):
    with pytest.raises(ValueError):
        _text(raw, 'html', None)


def test_deeply_nested_markup_is_bounded():
    with pytest.raises(ValueError, match='invalid_regulation_html_structure'):
        text('<div>' * 200 + '<p>Text</p>' + '</div>' * 200)
