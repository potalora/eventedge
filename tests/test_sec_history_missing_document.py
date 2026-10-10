"""SEC legacy metadata can omit a filename without omitting filing identity.

Observed Pillarstone history response SHA-256:
cbb093da7fbe18938058f4f18ad21510c27608fb01f9b896daa6322c0d7bf0ab.
Its 346 rows include 49 literal empty primaryDocument values. These small
public identity rows reproduce the boundary without storing the private capture.
"""
from copy import deepcopy

import pytest

from test_filing_hydration import Source, history_row, hydrate, row
from tradingagents.strategies.data_sources.edgar_source import _history_rows
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
from tradingagents.strategies.data_sources.filing_hydration import _nearest


def arrays():
    return {'form': ['10-K', '10QSB'], 'filingDate': ['2026-10-07', '2000-05-12'],
        'accessionNumber': ['0001437749-26-032293', '0000912057-00-023981'],
        'primaryDocument': ['pscr20251231_10k.htm', ''], 'isXBRL': [1, 0]}


def test_legacy_missing_filename_preserves_every_identity_and_explicit_empty_value():
    native = arrays()
    original = deepcopy(native)
    accepted = _history_rows(native)
    assert native == original
    assert accepted == [
        {'form': '10-K', 'filing_date': '2026-10-07', 'accession_number': '0001437749-26-032293',
         'primary_document': 'pscr20251231_10k.htm'},
        {'form': '10QSB', 'filing_date': '2000-05-12', 'accession_number': '0000912057-00-023981',
         'primary_document': ''}]


@pytest.mark.parametrize('invalid', [None, 0, [], {}, ' ', '../main.htm', '/main.htm',
    'main.htm?query=1', 'https://www.sec.gov/main.htm', 'dir//main.htm', 'a/' * 9 + 'main.htm'])
def test_only_literal_empty_string_relaxes_filename_validation(invalid):
    native = arrays()
    native['primaryDocument'][1] = invalid
    with pytest.raises(ValueError, match='invalid history row'):
        _history_rows(native)


@pytest.mark.parametrize('malformation', ['missing_array', 'unequal_array', 'duplicate_accession', 'bad_date', 'bad_accession'])
def test_missing_filename_does_not_weaken_history_identity(malformation):
    native = arrays()
    if malformation == 'missing_array':
        del native['primaryDocument']
    elif malformation == 'unequal_array':
        native['isXBRL'].append(0)
    elif malformation == 'duplicate_accession':
        native['accessionNumber'][1] = native['accessionNumber'][0]
    elif malformation == 'bad_date':
        native['filingDate'][1] = '2000-99-12'
    else:
        native['accessionNumber'][1] = 'not-an-accession'
    with pytest.raises(ValueError):
        _history_rows(native)


@pytest.mark.parametrize('body_missing', [False, True])
def test_empty_filename_prior_uses_exact_accession_and_still_requires_real_body(body_missing):
    current, prior = row(1, '10-Q'), row(2, '10-Q', '2026-06-30')
    metadata = {'form': ['10-Q'], 'filingDate': [prior['file_date']],
        'accessionNumber': [prior['adsh']], 'primaryDocument': ['']}
    source = Source([current, prior], {'0000000001': {'filings': _history_rows(metadata), 'archives': []}})
    if body_missing:
        source.overrides[prior['adsh']] = SourceFetchError('missing raw body', reason_code='invalid_response')
    result = hydrate(source, {'filings': [current]})
    observed = result['collections']['filings'][0]
    assert sum(call[0] == prior['adsh'] for call in source.body_calls) == 1
    if body_missing:
        assert observed['prior_status'] != 'available' and 'prior_evidence_ref' not in observed
    else:
        assert observed['prior_status'] == 'available' and observed['prior_evidence_ref'] == prior['adsh']
    assert result['history_corpus']['0000000001']['filings'][0]['primary_document'] == ''


def test_empty_filename_does_not_resolve_same_date_prior_ambiguity():
    first, second = history_row(row(2, '10-Q', '2026-06-30')), history_row(row(3, '10-Q', '2026-06-30'))
    first['primary_document'] = second['primary_document'] = ''
    assert _nearest([first, second], '10-Q', '2026-09-30') == (None, 'ambiguous_prior_date')
