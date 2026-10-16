"""Native ownership envelopes are required before a window can be complete.

Core fields follow SEC Ownership XML Technical Specification table 3.5;
fixtures are synthetic and deliberately retain zero-transaction amendments.
"""
from xml.etree import ElementTree as ET

import pytest

from test_bounded_source_pagination import Response, transport
from test_form4_complete_window import filing, deny_unmocked_network
from tradingagents.strategies.data_sources import edgar_source as edgar
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError


def ownership(form='4'):
    return ET.fromstring(f'''<ownershipDocument>
      <documentType>{form}</documentType><periodOfReport>2026-10-08</periodOfReport>
      <dateOfOriginalSubmission>2026-10-08</dateOfOriginalSubmission>
      <issuer><issuerCik>0000000001</issuerCik><issuerName>Example issuer</issuerName>
        <issuerTradingSymbol>TEST</issuerTradingSymbol></issuer>
      <reportingOwner><reportingOwnerId><rptOwnerCik>123</rptOwnerCik>
        <rptOwnerName>Example owner</rptOwnerName></reportingOwnerId>
        <reportingOwnerRelationship><isDirector>1</isDirector></reportingOwnerRelationship>
      </reportingOwner>
    </ownershipDocument>''')


def window(monkeypatch, root, *, form='4'):
    source = edgar.EDGARSource()
    monkeypatch.setattr(source, 'ticker_to_cik', lambda _: '1')
    monkeypatch.setattr(source, 'get_company_submission_history', lambda _: {
        'filings': [filing(1, form=form)], 'archives': [],
        'response_sha256': 'a'*64, 'coverage': {'complete': True}})
    response = Response({}, 'fixture')
    response.body = ET.tostring(root)
    transport(monkeypatch, edgar, [response])
    return source, response


@pytest.mark.parametrize('form', ['4', '4/A'])
@pytest.mark.parametrize('namespace', ['', 'http://www.sec.gov/ownership'])
def test_structured_zero_transaction_filing_is_retained(monkeypatch, form, namespace):
    root = ownership(form)
    if namespace:
        for element in root.iter():
            element.tag = '{'+namespace+'}'+element.tag
    source, response = window(monkeypatch, root, form=form)
    rows = source.get_recent_form4('TEST', 14, as_of='2026-10-09')
    assert rows.coverage['complete'] is True
    assert rows.coverage['matching_filings'] == 1
    assert rows == [filing(1, form=form)]
    assert response.closed


@pytest.mark.parametrize('field', ['documentType', 'periodOfReport', 'issuer',
    'issuer/issuerCik', 'reportingOwner', 'reportingOwner/reportingOwnerId',
    'reportingOwner/reportingOwnerId/rptOwnerCik'])
def test_incomplete_envelope_cannot_be_clean_no_event(monkeypatch, field):
    root = ownership()
    parent, _, name = field.rpartition('/')
    (root.find(parent) if parent else root).remove(root.find(field))
    source, response = window(monkeypatch, root)
    with pytest.raises(SourceFetchError) as caught:
        source.get_recent_form4('TEST', 14, as_of='2026-10-09')
    assert caught.value.partial_data['coverage']['complete'] is False
    assert caught.value.partial_data['form4_filings'] == []
    assert response.closed


@pytest.mark.parametrize('field,value', [('documentType', '3'), ('documentType', '4/A'),
    ('periodOfReport', ''), ('periodOfReport', '2026-02-30'),
    ('periodOfReport', '2026-10-10'), ('issuer/issuerCik', '2'),
    ('issuer/issuerCik', '0'), ('reportingOwner/reportingOwnerId/rptOwnerCik', '0'),
    ('reportingOwner/reportingOwnerId/rptOwnerCik', 'not-a-cik')])
def test_mismatched_or_invalid_envelope_cannot_complete(monkeypatch, field, value):
    root = ownership()
    root.find(field).text = value
    source, response = window(monkeypatch, root)
    with pytest.raises(SourceFetchError) as caught:
        source.get_recent_form4('TEST', 14, as_of='2026-10-09')
    assert caught.value.partial_data['coverage']['complete'] is False
    assert response.closed


@pytest.mark.parametrize('field', ['documentType', 'periodOfReport', 'issuer'])
def test_duplicate_envelope_identity_is_ambiguous(monkeypatch, field):
    root = ownership()
    root.append(ET.fromstring(ET.tostring(root.find(field))))
    source, response = window(monkeypatch, root)
    with pytest.raises(SourceFetchError):
        source.get_recent_form4('TEST', 14, as_of='2026-10-09')
    assert response.closed
