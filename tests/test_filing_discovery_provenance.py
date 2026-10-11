"""Opt-in discovery retains exact query matches before shared hydration."""
from copy import deepcopy

import pytest

from test_filing_hydration import row
from tradingagents.strategies.learning.event_monitor import EventMonitor
from tradingagents.strategies.data_sources.evidence import CoverageRecords
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError


class Source:
    def __init__(self, responses):
        self.responses = iter(responses)

    def is_available(self):
        return True

    def search_filings(self, **kwargs):
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return CoverageRecords(deepcopy(value), coverage={'complete': True, 'provider_total': len(value)})


def monitor(responses, *, complete=True):
    source = Source(responses)
    class Registry:
        def get(self, name):
            return source
    result = EventMonitor(Registry(), filing_policy='complete_submission_v1' if complete else None)
    result.as_of = '2026-10-09'
    return result


def test_all_exact_keyword_matches_and_window_parameters_survive_accession_dedupe():
    m = monitor([[row()], [row()]])
    records = m.poll_keyword_filings(['8-K'], ['quantum', 'PQC'], fetch_text=False)
    assert len(records) == 1
    assert records[0]['matched_queries'] == [
        {'form_type': '8-K', 'keyword': 'quantum', 'date_from': '2026-09-09', 'date_to': '2026-10-09', 'operation': 'keyword_0_0'},
        {'form_type': '8-K', 'keyword': 'PQC', 'date_from': '2026-09-09', 'date_to': '2026-10-09', 'operation': 'keyword_0_1'}]
    window = records.coverage['windows']['keyword_0_1']
    assert window['keyword'] == 'PQC' and window['form_type'] == '8-K'
    assert window['date_from'] == '2026-09-09' and window['date_to'] == '2026-10-09'
    assert window['provider_total'] == 1


def test_partial_page_matches_are_retained_with_failed_exact_window():
    partial = SourceFetchError('Fixed partial failure', reason_code='invalid_response', partial_data={
        'filings': [row()], 'coverage': {'complete': False, 'provider_total': 200}})
    m = monitor([[row()], partial])
    with pytest.raises(SourceFetchError) as caught:
        m.poll_keyword_filings(['8-K'], ['quantum', 'PQC'], fetch_text=False)
    records = caught.value.partial_data['pqc_filings']
    assert len(records) == 1 and len(records[0]['matched_queries']) == 2
    window = records.coverage['windows']['keyword_0_1']
    assert window['complete'] is False and window['keyword'] == 'PQC'
    assert window['date_to'] == '2026-10-09'


def test_missing_url_or_accession_rows_do_not_disappear():
    missing_url = row(1, file_url='')
    missing_identity = row(2, file_url='');missing_identity.pop('adsh')
    records = monitor([[missing_url, missing_identity]]).poll_keyword_filings(['8-K'], ['PQC'], fetch_text=False)
    assert len(records) == 2
    assert all(len(r['matched_queries']) == 1 for r in records)


def test_same_accession_form_date_conflict_is_explicit_not_first_identity_wins():
    m = monitor([[row()], [row(file_date='2026-09-29')]])
    with pytest.raises(SourceFetchError) as caught:
        m.poll_keyword_filings(['8-K'], ['quantum', 'PQC'], fetch_text=False)
    records = caught.value.partial_data['pqc_filings']
    assert len(records) == 1 and records[0]['discovery_identity_conflict'] is True
    assert len(records[0]['matched_queries']) == 2
    assert records[0]['discovery_conflicts'][0]['file_date'] == '2026-09-29'
    assert records.coverage['complete'] is False


def test_legacy_keyword_discovery_path_keeps_existing_shape():
    records = monitor([[row()], [row()]], complete=False).poll_keyword_filings(['8-K'], ['quantum', 'PQC'], fetch_text=False)
    assert len(records) == 1 and records[0]['matched_keyword'] == 'quantum'
    assert 'matched_queries' not in records[0]
