"""SEC ownership raw/rendered aliases retain one proven document identity."""
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from tradingagents.strategies.data_sources import edgar_source as edgar
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError


INDEX = "https://www.sec.gov/Archives/edgar/data/2033264/000211748026000005/0002117480-26-000005-index.htm"
RAW = "https://www.sec.gov/Archives/edgar/data/2033264/000211748026000005/primary_doc.xml"
RENDERED = "https://www.sec.gov/Archives/edgar/data/2033264/000211748026000005/xslSCHEDULE_13D_X02/primary_doc.xml"
FIXTURE = Path(__file__).with_name("fixtures") / "edgar-ownership-index-0002117480-26-000005.html"


def _install_html(monkeypatch, html):
    monkeypatch.setattr(edgar, "provider_request", lambda *a, **kw: SimpleNamespace(text=html))


@pytest.mark.parametrize("form", ["SCHEDULE 13D", "SC 13D", None])
def test_native_ownership_index_raw_and_rendered_are_one_document(monkeypatch, form):
    # One HTTP-200 native index acquired 2026-10-09; no document request needed.
    body = FIXTURE.read_bytes()
    assert hashlib.sha256(body).hexdigest() == "1ed5a85b01b8b1d3697f1991eb6c8674d3c07f4a011812fb80f85f7cf6328f3b"
    _install_html(monkeypatch, body.decode())
    assert edgar.EDGARSource().get_primary_document_url(INDEX, form) == RAW


def test_native_alias_resolves_through_event_monitor_without_failed_text_coverage(monkeypatch):
    from tradingagents.strategies.data_sources.evidence import CoverageRecords
    from tradingagents.strategies.learning.event_monitor import EventMonitor
    source = edgar.EDGARSource()
    monkeypatch.setattr(source, "search_filings", lambda **kw: CoverageRecords([{
        "form_type": "SCHEDULE 13D", "file_date": "2026-10-09",
        "file_url": INDEX, "adsh": "0002117480-26-000005",
    }], coverage={"complete": True}))

    def transport(provider, method, url, **kwargs):
        if url == INDEX:
            return SimpleNamespace(text=FIXTURE.read_text())
        assert url == RAW, "Only the actually listed canonical XML may be fetched"
        return SimpleNamespace(text="<ownershipDocument><name>Retained owner</name></ownershipDocument>")

    monkeypatch.setattr(edgar, "provider_request", transport)
    monitor = EventMonitor(SimpleNamespace(get=lambda name: source))
    monitor.as_of = "2026-10-09"
    filings = monitor.poll_edgar_filings(["SCHEDULE 13D"])
    assert filings.coverage["complete"] is True
    assert filings[0]["primary_document_url"] == RAW
    assert filings[0]["text_status"] == "available"
    assert filings[0]["current_text"] == "Retained owner"


def _row(sequence, form, url):
    return f'<tr><td>{sequence}</td><td>Main</td><td><a href="{url}">document</a></td><td>{form}</td></tr>'


@pytest.mark.parametrize("second", [
    ("2", "SCHEDULE 13D", RAW),
    ("1", "SCHEDULE 13D", RAW.replace("primary_doc.xml", "other.xml")),
    ("1", "SCHEDULE 13D", RAW.replace("000211748026000005", "000211748026000006")),
    ("1", "SCHEDULE 13D", RAW.replace("www.sec.gov", "untrusted.example")),
    ("1", "SCHEDULE 13D/A", RAW),
])
def test_non_equivalent_rows_cannot_establish_raw_alias(monkeypatch, second):
    html = '<table class="tableFile">' + _row("1", "SCHEDULE 13D", RENDERED) + _row(*second) + '</table>'
    _install_html(monkeypatch, html)
    if second[0] == "2" or "other.xml" in second[2]:
        with pytest.raises(SourceFetchError, match="ambiguous"):
            edgar.EDGARSource().get_primary_document_url(INDEX, "SCHEDULE 13D")
    else:
        # Form, host and accession gates discard the unrelated row. Its raw URL
        # cannot cause fabrication of an unobserved raw alias for the survivor.
        assert edgar.EDGARSource().get_primary_document_url(INDEX, "SCHEDULE 13D") == RENDERED


def test_true_second_document_remains_ambiguous_after_alias_resolution(monkeypatch):
    html = '<table class="tableFile">' + ''.join([
        _row("1", "SCHEDULE 13D", RENDERED), _row("1", "SCHEDULE 13D", RAW),
        _row("2", "SCHEDULE 13D", RAW.replace("primary_doc.xml", "other.xml")),
    ]) + '</table>'
    _install_html(monkeypatch, html)
    with pytest.raises(SourceFetchError, match="ambiguous"):
        edgar.EDGARSource().get_primary_document_url(INDEX, "SCHEDULE 13D")


def test_matching_schedule_13g_amendment_alias_retains_amendment_identity(monkeypatch):
    rendered = RENDERED.replace("xslSCHEDULE_13D_X02", "xslSCHEDULE_13G_X01")
    html = '<table class="tableFile">' + _row("1", "SCHEDULE 13G/A", rendered) + _row("1", "SC 13G/A", RAW) + '</table>'
    _install_html(monkeypatch, html)
    assert edgar.EDGARSource().get_primary_document_url(INDEX, "SCHEDULE 13G/A") == RAW


@pytest.mark.parametrize("stylesheet", ["xslSCHEDULE_13G_X01", "xslUnrelated_X02"])
def test_unrelated_stylesheet_cannot_establish_equivalent_13d_document(monkeypatch, stylesheet):
    rendered = RENDERED.replace("xslSCHEDULE_13D_X02", stylesheet)
    html = '<table class="tableFile">' + _row("1", "SCHEDULE 13D", rendered) + _row("1", "SCHEDULE 13D", RAW) + '</table>'
    _install_html(monkeypatch, html)
    with pytest.raises(SourceFetchError, match="ambiguous"):
        edgar.EDGARSource().get_primary_document_url(INDEX, "SCHEDULE 13D")


PASSIVE_INDEX = "https://www.sec.gov/Archives/edgar/data/2028336/000202674526000008/0002026745-26-000008-index.htm"
PASSIVE_RAW = "https://www.sec.gov/Archives/edgar/data/2026745/000202674526000008/primary_doc.xml"
PASSIVE_FIXTURE = FIXTURE.with_name("edgar-ownership-index-0002026745-26-000008.html")


@pytest.mark.parametrize("form", ["SCHEDULE 13G/A", "SC 13G/A", None])
def test_native_ownership_index_may_list_same_accession_under_another_cik(monkeypatch, form):
    body = PASSIVE_FIXTURE.read_bytes()
    assert hashlib.sha256(body).hexdigest() == "0df7c9a66fa926a4b39973377df9b4f809ef923271866cccc55458430355244f"
    _install_html(monkeypatch, body.decode())
    assert edgar.EDGARSource().get_primary_document_url(PASSIVE_INDEX, form) == PASSIVE_RAW


@pytest.mark.parametrize("unsafe_url", [
    PASSIVE_RAW.replace("000202674526000008", "000202674526000009"),
    PASSIVE_RAW.replace("www.sec.gov", "untrusted.example"),
    PASSIVE_RAW.replace("/2026745/", "/not-numeric/"),
    PASSIVE_RAW.replace("primary_doc.xml", "another-index.htm"),
    PASSIVE_RAW.replace("primary_doc.xml", "another-index.htm?view=1"),
    PASSIVE_RAW.replace("https://", "http://"),
    PASSIVE_RAW.replace("primary_doc.xml", "%2e%2e/primary_doc.xml"),
    PASSIVE_RAW.replace("primary_doc.xml", "unsafe%2fprimary_doc.xml"),
    PASSIVE_RAW.replace("primary_doc.xml", "unsafe\\primary_doc.xml"),
])
def test_cross_cik_ownership_links_preserve_archive_guards(monkeypatch, unsafe_url):
    _install_html(monkeypatch, '<table class="tableFile">' + _row("1", "SCHEDULE 13G/A", unsafe_url) + '</table>')
    with pytest.raises(SourceFetchError, match="ambiguous or unavailable"):
        edgar.EDGARSource().get_primary_document_url(PASSIVE_INDEX, "SCHEDULE 13G/A")


def test_same_accession_distinct_cik_documents_remain_ambiguous(monkeypatch):
    rendered = PASSIVE_RAW.replace("primary_doc.xml", "xslSCHEDULE_13G_X02/primary_doc.xml")
    other_raw = PASSIVE_RAW.replace("/2026745/", "/2028336/")
    html = '<table class="tableFile">' + ''.join([
        _row("1", "SCHEDULE 13G/A", rendered),
        _row("1", "SCHEDULE 13G/A", PASSIVE_RAW),
        _row("1", "SCHEDULE 13G/A", other_raw),
    ]) + '</table>'
    _install_html(monkeypatch, html)
    with pytest.raises(SourceFetchError, match="ambiguous"):
        edgar.EDGARSource().get_primary_document_url(PASSIVE_INDEX, "SCHEDULE 13G/A")


def test_cross_cik_alias_requires_raw_counterpart_in_same_actual_directory(monkeypatch):
    rendered = PASSIVE_RAW.replace("primary_doc.xml", "xslSCHEDULE_13G_X02/primary_doc.xml")
    other_raw = PASSIVE_RAW.replace("/2026745/", "/2028336/")
    _install_html(monkeypatch, '<table class="tableFile">' + _row("1", "SCHEDULE 13G/A", rendered)
                  + _row("1", "SCHEDULE 13G/A", other_raw) + '</table>')
    with pytest.raises(SourceFetchError, match="ambiguous"):
        edgar.EDGARSource().get_primary_document_url(PASSIVE_INDEX, "SCHEDULE 13G/A")


def test_cross_cik_rule_does_not_expand_other_filing_forms(monkeypatch):
    _install_html(monkeypatch, '<table class="tableFile">' + _row("1", "10-K", PASSIVE_RAW) + '</table>')
    with pytest.raises(SourceFetchError, match="ambiguous or unavailable"):
        edgar.EDGARSource().get_primary_document_url(PASSIVE_INDEX, "10-K")


def test_cross_cik_native_alias_preserves_event_monitor_subject_identity(monkeypatch):
    from tradingagents.strategies.data_sources.evidence import CoverageRecords
    from tradingagents.strategies.learning.event_monitor import EventMonitor

    source = edgar.EDGARSource()
    identity = {"file_url": PASSIVE_INDEX, "form_type": "SCHEDULE 13G/A",
                "file_date": "2026-10-09", "adsh": "0002026745-26-000008",
                "ciks": ["0002028336", "0002026745"]}
    monkeypatch.setattr(source, "search_filings", lambda **kwargs: CoverageRecords(
        [dict(identity)], coverage={"complete": True}))
    def transport(provider, method, url, **kwargs):
        if url == PASSIVE_INDEX:
            return SimpleNamespace(text=PASSIVE_FIXTURE.read_text())
        assert url == PASSIVE_RAW
        return SimpleNamespace(text="<ownershipDocument><name>Retained owner</name></ownershipDocument>")
    monkeypatch.setattr(edgar, "provider_request", transport)
    monitor = EventMonitor(SimpleNamespace(get=lambda name: source))
    monitor.as_of = "2026-10-09"
    filings = monitor.poll_edgar_filings(["SCHEDULE 13G"])
    assert filings.coverage["complete"] is True
    assert filings[0]["primary_document_url"] == PASSIVE_RAW
    assert filings[0]["current_text"] == "Retained owner"
    assert filings[0]["text_status"] == "available"
    assert all(filings[0][key] == value for key, value in identity.items())
