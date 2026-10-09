"""Native provider shapes retain economic, form-family and entity meaning."""
from copy import deepcopy
from unittest.mock import patch

import pytest

from tradingagents.strategies.data_sources import edgar_source as edgar
from tradingagents.strategies.data_sources import usaspending_source as usa
from tradingagents.strategies.data_sources import courtlistener_source as court
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
from tradingagents.strategies.learning.event_monitor import EventMonitor
from tradingagents.strategies.modules.filing_analysis import FilingAnalysisStrategy
from tradingagents.strategies.modules.govt_contracts import GovtContractsStrategy


class Response:
    status_code = 200
    def __init__(self, body=None, text=''):
        self.body, self.text = body, text
    def json(self):
        return self.body


def sec_body(form):
    return {'hits': {'total': {'value': 1, 'relation': 'eq'}, 'hits': [{'_source': {
        'form': form, 'file_date': '2026-10-09', 'adsh': '0000123456-26-000001',
        'display_names': ['Reporting Fund (FUND) (CIK 0000123456)'], 'ciks': ['0000123456'],
    }}]}}


@pytest.mark.parametrize('family', ['13D', '13G'])
@pytest.mark.parametrize('prefix', ['SC', 'SCHEDULE'])
@pytest.mark.parametrize('amendment', ['', '/A'])
def test_schedule_query_parser_monitor_screen_keep_family_and_issuer(family, prefix, amendment):
    raw_form = f'{prefix} {family}{amendment}'
    source = edgar.EDGARSource()
    calls = []
    def transport(provider, method, url, **kwargs):
        calls.append(kwargs.get('params', {}))
        return Response(sec_body(raw_form))
    with patch.object(edgar, 'provider_request', side_effect=transport):
        rows = source.search_filings(raw_form, '2026-10-01', '2026-10-09')
    assert set(calls[0]['forms'].split(',')) == {f'{p} {family}' for p in ('SC', 'SCHEDULE')}
    assert rows[0]['form_type'] == f'SCHEDULE {family}{amendment}'
    assert rows[0]['source_form_type'] == raw_form
    with patch.object(source, 'search_filings', return_value=rows), patch.object(source, 'get_primary_document_url', return_value='https://www.sec.gov/Archives/edgar/data/123456/main.xml'), patch.object(source, 'get_filing_text', return_value='<xml>Stake terms</xml>'):
        monitor = EventMonitor({'edgar': source}); monitor.as_of = '2026-10-09'
        enriched = monitor.poll_edgar_filings([raw_form])
    assert enriched[0]['current_text'] == 'Stake terms'
    strategy = FilingAnalysisStrategy()
    candidates = strategy.screen({'edgar': {'filings': enriched}}, '2026-10-09', strategy.get_default_params())
    assert len(candidates) == 1
    assert candidates[0].journal_only and not candidates[0].metadata['subject_attribution_verified']
    assert candidates[0].metadata['analysis_type'] == ('activist_stake' if family == '13D' else 'passive_stake')
    # A verified subject supplied upstream is tradable; reporter ticker never proves it.
    enriched[0]['subject_ticker'] = 'AAPL'
    enriched.append(dict(enriched[0], form_type=f'SC {family}{amendment}'))
    verified = strategy.screen({'edgar': {'filings': enriched}}, '2026-10-09', dict(forms_to_analyze=[f'SC {family}']))
    assert len(verified) == 1 and verified[0].ticker == 'AAPL' and not verified[0].journal_only


def award_row(**changes):
    return dict({'Award ID': 'SAME-PIID', 'Recipient Name': 'LOCKHEED MARTIN CORP',
        'Award Amount': 250_000_000, 'Start Date': '2020-01-01',
        'Base Obligation Date': '2026-10-01', 'Last Modified Date': '2026-10-08 10:11:12',
        'internal_id': 42, 'generated_internal_id': 'CONT_AWD_SAME-PIID_AGENCY_PARENT',
    }, **changes)


def parse_awards(rows):
    with patch.object(usa, 'provider_request', return_value=Response({'results': rows, 'page_metadata': {'page': 1, 'hasNext': False}})) as request:
        result = usa.USASpendingSource()._search_contracts_page(date_from='2026-09-09', date_to='2026-10-09')
    return result, request.call_args.kwargs['json']


def test_new_award_semantics_use_base_date_native_identity_and_acquisition_time():
    rows, query = parse_awards([award_row(), award_row(generated_internal_id='CONT_AWD_OTHER_AGENCY', internal_id=43)])
    assert query['filters']['time_period'][0]['date_type'] == 'new_awards_only'
    assert {'Base Obligation Date', 'generated_internal_id'} <= set(query['fields'])
    assert rows[0]['base_obligation_date'] == '2026-10-01'
    assert rows[0]['award_key'] != rows[1]['award_key']
    strategy = GovtContractsStrategy()
    candidates = strategy.screen({'usaspending': {'data': {'contracts': rows}}}, '2026-10-09', {})
    assert len(candidates) == 2
    for candidate in candidates:
        assert candidate.score == .25
        assert candidate.metadata['amount_basis'] == 'cumulative_award_obligations'
        assert candidate.metadata['award_scope'] == 'new_awards_only'
        assert candidate.metadata['base_obligation_date'] == '2026-10-01'
        assert candidate.metadata['observed_at'].endswith('+00:00')
        assert 'last_modified_date' not in candidate.metadata


@pytest.mark.parametrize('changes', [
    {'Base Obligation Date': '1993-11-15'}, {'Base Obligation Date': '2026-10-10'},
    {'Base Obligation Date': None}, {'generated_internal_id': None, 'internal_id': None},
    {'Award Amount': float('nan')},
])
def test_new_award_contract_rejects_contradictory_or_unbound_rows(changes):
    with pytest.raises(SourceFetchError, match='invalid_response'):
        parse_awards([award_row(**changes)])


def test_strategy_rejects_unverified_award_semantics():
    records, _ = parse_awards([award_row()])
    strategy = GovtContractsStrategy()
    for key in ('award_scope', 'amount_basis', 'base_obligation_date', 'award_key', 'observed_at'):
        bad = dict(records[0]); bad.pop(key, None)
        assert strategy.screen({'usaspending': {'data': {'contracts': [bad]}}}, '2026-10-09', {}) == []


def test_court_search_returns_clusters_with_all_nested_opinion_references():
    raw = {'cluster_id': 11024977, 'docket_id': 74944716, 'caseName': 'In re J.S.',
           'dateFiled': '2026-10-09', 'court': 'California Court of Appeal',
           'opinions': [{'id': 11492664, 'type': 'combined-opinion', 'snippet': 'Lead text', 'download_url': 'https://example.test/lead.pdf'},
                        {'id': 11492665, 'type': 'dissent', 'snippet': 'Dissent text'}]}
    with patch.object(court, 'provider_request', return_value=Response({'results': [raw], 'count': 1, 'next': None})):
        rows = court.CourtListenerSource(token='offline').search_opinions('Apple')
    assert len(rows) == 1 and rows[0]['cluster_id'] == 11024977
    assert 'opinion_id' not in rows[0]
    assert rows[0]['opinion_ids'] == [11492664, 11492665]
    assert rows[0]['opinions'][0]['opinion_id'] == 11492664
    assert rows[0]['opinions'][1]['snippet'] == 'Dissent text'
    assert rows.coverage['unit'] == 'opinion_clusters'


def test_schedule_search_deduplicates_native_accession_across_subject_search_hits():
    body = sec_body('SCHEDULE 13D/A')
    second = deepcopy(body['hits']['hits'][0]); second['_source']['ciks'] = ['0000654321']
    body['hits']['hits'].append(second); body['hits']['total']['value'] = 2
    with patch.object(edgar, 'provider_request', return_value=Response(body)):
        rows = edgar.EDGARSource().search_filings('SCHEDULE 13D')
    assert len(rows) == 1


def test_schedule_primary_document_matches_legacy_label_without_conflating_amendment():
    text = '''<table class="tableFile"><tr><td>1</td><td>Main</td><td><a href="/Archives/edgar/data/123/000123/main.xml">main</a></td><td>SC 13D/A</td></tr>
    <tr><td>2</td><td>Other</td><td><a href="/Archives/edgar/data/123/000123/other.xml">other</a></td><td>SCHEDULE 13D</td></tr></table>'''
    with patch.object(edgar, 'provider_request', return_value=Response(text=text)):
        assert edgar.EDGARSource().get_primary_document_url('https://www.sec.gov/Archives/edgar/data/123/000123/000123-index.htm', 'SCHEDULE 13D/A').endswith('/main.xml')


def test_contract_search_always_declares_a_complete_new_award_window():
    with patch.object(usa, 'current_session_date', return_value='2026-10-09'), patch.object(usa, 'provider_request', return_value=Response({'results': [], 'page_metadata': {'page': 1, 'hasNext': False}})) as request:
        usa.USASpendingSource().search_contracts()
    assert request.call_args.kwargs['json']['filters']['time_period'] == [
        {'start_date': '2026-09-09', 'end_date': '2026-10-09', 'date_type': 'new_awards_only'}]


def test_contracts_allow_distinct_native_awards_with_same_display_piid():
    raw = [award_row(), award_row(generated_internal_id=None, internal_id=43)]
    with patch.object(usa, 'provider_request', return_value=Response({'results': raw, 'page_metadata': {'page': 1, 'hasNext': False}})):
        rows = usa.USASpendingSource().search_contracts(date_from='2026-09-09', date_to='2026-10-09')
    assert len(rows) == 2 and rows[1]['award_key'] == 'internal_id:43'


@pytest.mark.parametrize('opinions', [[], [{'id': True}], [{'id': 1}, {'id': 1}], [{'id': 0}]])
def test_court_opinion_identity_must_be_positive_unique_native_ids(opinions):
    raw = {'cluster_id': 10, 'caseName': 'Case', 'dateFiled': '2026-10-09', 'opinions': opinions}
    with patch.object(court, 'provider_request', return_value=Response({'results': [raw]})):
        with pytest.raises(SourceFetchError, match='invalid_response'):
            court.CourtListenerSource(token='offline').search_opinions('Case')
