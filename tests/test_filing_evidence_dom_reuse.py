"""ASCII DOM reuse must preserve full evidence and all fallback behavior."""
import copy

import pytest

from tradingagents.strategies.data_sources import filing_evidence as evidence
from test_filing_evidence import parse, submission


def observe_parses(monkeypatch):
    original = evidence.BeautifulSoup
    calls = []
    def observed(*args, **kwargs):
        soup = original(*args, **kwargs)
        calls.append((type(args[0]), soup.original_encoding))
        return soup
    monkeypatch.setattr(evidence, 'BeautifulSoup', observed)
    return calls


def test_ascii_primary_is_parsed_once_without_losing_hidden_anchor_inventory(monkeypatch):
    primary = ('<html xmlns:i="http://www.xbrl.org/2013/inlineXBRL">'
               '<head>machine</head><i:header>hidden XBRL</i:header>'
               '<div hidden><a href="ex.htm">Hidden link</a></div>'
               '<p>A&amp;B <i:nonFraction>42</i:nonFraction></p>'
               '<script>hidden script</script></html>')
    parsed = parse(submission([('8-K', 'main.htm', primary),
                               ('EX-99.1', 'ex.htm', '<p>Full exhibit.</p>')]))
    calls = observe_parses(monkeypatch)
    result = evidence.build_evidence(parsed)
    assert [unit['text'] for unit in result['units']] == ['A&B 42', 'Full exhibit.']
    assert result['dependencies'] == [{'href': 'ex.htm', 'label': 'Hidden link',
        'resolution': 'same_submission', 'filename': 'ex.htm'}]
    assert result['issues'] == [] and result['structural_status'] == 'complete'
    assert len(calls) == 2  # One primary DOM and one exhibit DOM.


@pytest.mark.parametrize('body', [
    '<p>caf\u00e9</p>',
    '\ufeff<p>BOM text</p>',
    '<meta charset="utf-8"><p>Declared UTF-8 ASCII.</p>',
    '<meta charset="iso-8859-1"><p>Declared Latin-1 ASCII.</p>',
])
def test_non_ascii_or_non_ascii_detected_encoding_keeps_two_parse_path(monkeypatch, body):
    parsed = parse(submission([('8-K', 'main.htm', body)]))
    calls = observe_parses(monkeypatch)
    result = evidence.build_evidence(parsed)
    assert result['structural_status'] == 'complete'
    assert len(calls) == 2


def test_malformed_utf8_still_records_encoding_failure_and_dependency_inventory(monkeypatch):
    raw = submission([('8-K', 'main.htm', '<a href="missing.htm">Bad TOKEN</a>')])
    parsed = parse(raw.replace(b'TOKEN', b'\xff'))
    calls = observe_parses(monkeypatch)
    result = evidence.build_evidence(parsed)
    assert result['structural_status'] == 'insufficient' and result['units'] == []
    assert result['issues'][0]['code'] == 'unsupported_text_encoding'
    assert result['dependencies'][0]['resolution'] == 'unresolved_local'
    assert len(calls) == 1  # Strict UTF-8 decoding fails before visible-text DOM parsing.


def test_repeated_builds_do_not_share_mutated_dom_or_change_parsed_input(monkeypatch):
    parsed = parse(submission([('8-K', 'main.htm',
        '<p>Visible</p><div hidden><a href="#part">Hidden anchor</a></div>')]))
    original = copy.deepcopy(parsed)
    calls = observe_parses(monkeypatch)
    first = evidence.build_evidence(parsed)
    second = evidence.build_evidence(parsed)
    assert first == second and parsed == original
    assert first['units'][0]['text'] == 'Visible'
    assert first['dependencies'][0]['label'] == 'Hidden anchor'
    assert len(calls) == 2


def test_ascii_reuse_preserves_full_text_limit_failure(monkeypatch):
    parsed = parse(submission([('8-K', 'main.htm', '<p>Complete full text.</p>')]))
    calls = observe_parses(monkeypatch)
    result = evidence.build_evidence(parsed, max_total_text_bytes=3)
    assert result['structural_status'] == 'insufficient' and result['units'] == []
    assert result['issues'][0]['code'] == 'total_text_byte_limit'
    assert len(calls) == 1
