"""SEC history basenames may contain consecutive dots without traversing paths."""
import pytest

from tradingagents.strategies.data_sources.edgar_source import _history_rows


def history_row(document):
    return {'form': ['SC 13G'], 'filingDate': ['2014-01-28'],
            'accessionNumber': ['0001086364-14-000485'],
            'primaryDocument': [document]}


@pytest.mark.parametrize('document', [
    'cal-maine.foods.inc..txt', 'ennis.inc..txt',
    'xslF345X05/issuer..xml',
])
def test_history_preserves_sec_consecutive_dot_basenames(document):
    rows = _history_rows(history_row(document))
    assert len(rows) == 1
    assert rows[0]['primary_document'] == document
    assert rows[0]['accession_number'] == '0001086364-14-000485'


@pytest.mark.parametrize('document', [
    '.', '..', '../filing.txt', 'folder/../filing.txt',
    './filing.txt', 'folder/./filing.txt', '/filing.txt',
    'folder//filing.txt', 'folder/filing.txt/',
    '%2e%2e/filing.txt', 'folder/%2E%2E/filing.txt',
    'folder%2ffiling.txt', 'folder%5cfiling.txt',
    r'..\filing.txt', r'folder\filing.txt',
    'https://example.com/filing.txt', 'filing.txt?query=1',
    'filing.txt#fragment', 'filing\x00.txt',
])
def test_history_rejects_traversal_and_non_native_document_paths(document):
    with pytest.raises(ValueError, match='invalid history row'):
        _history_rows(history_row(document))
