"""Machine-only inline XBRL must not displace supplied filing narrative."""
from types import SimpleNamespace

from tradingagents.strategies.learning.event_monitor import EventMonitor


def test_hidden_xbrl_does_not_fill_excerpt_before_visible_narrative():
    raw = ('<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL" '
           'xmlns:xbrli="http://www.xbrl.org/2003/instance"><body>'
           '<ix:header><ix:hidden>' + 'MACHINE_CONTEXT ' * 1000
           + '</ix:hidden></ix:header><xbrli:context>2026-09-30</xbrli:context>'
           '<xbrli:unit>USD</xbrli:unit><p>Item 1A. Risk Factors</p>'
           '<p>Our sole supplier closed its factory.</p>'
           '<p>Revenue fell <ix:nonFraction>25</ix:nonFraction> percent.</p></body></html>')
    excerpt = EventMonitor._strip_html(raw)[:3000]
    assert 'Our sole supplier closed its factory.' in excerpt
    assert 'Revenue fell 25 percent.' in excerpt
    assert 'MACHINE_CONTEXT' not in excerpt and 'USD' not in excerpt


def test_hidden_only_document_is_empty_not_available_text():
    raw = ('<html><head><title>Annual report</title><style>.hidden{display:none}</style></head>'
           '<body><ix:header><ix:hidden>context data</ix:hidden></ix:header>'
           '<div hidden>hidden fact</div><div style="DISPLAY : none !important">more hidden</div>'
           '<script>ignored script</script></body></html>')
    assert EventMonitor._strip_html(raw) == ''


def test_namespace_aliases_and_entity_decoding_preserve_visible_facts():
    raw = ('<html xmlns:i="http://www.xbrl.org/2013/inlineXBRL" '
           'xmlns:x="http://www.xbrl.org/2003/instance"><body>'
           '<i:header><i:hidden>hidden</i:hidden></i:header><x:context>context</x:context>'
           '<p>Revenue&nbsp;&amp; income: <i:nonFraction>100</i:nonFraction>.</p></body></html>')
    assert EventMonitor._strip_html(raw) == 'Revenue & income: 100 .'


def test_current_and_prior_monitor_paths_use_same_visible_text_extraction():
    current = {'form_type': '10-K', 'file_url': 'https://www.sec.gov/current.htm',
               'file_date': '2026-10-09', 'ciks': ['0000000001'], 'adsh': 'current'}
    raw = '<ix:header>' + 'context ' * 1000 + '</ix:header><p>Visible operating disclosure.</p>'
    source = SimpleNamespace(
        is_available=lambda: True, search_filings=lambda **kw: [dict(current)],
        get_filing_text=lambda url: raw,
        get_company_filings=lambda **kw: [{'filing_date': '2025-10-09',
                                           'accession_number': '0000000001-25-000001',
                                           'primary_document': 'prior.htm'}],
    )
    monitor = EventMonitor(SimpleNamespace(get=lambda name: source))
    monitor.as_of = '2026-10-09'
    result = monitor.poll_edgar_filings(['10-K'])
    assert result[0]['current_text'] == 'Visible operating disclosure.'
    assert result[0]['prior_text'] == result[0]['current_text']
    assert result[0]['text_status'] == 'available'
