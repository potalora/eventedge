from copy import deepcopy
from datetime import datetime, timezone
import json
import time
from types import SimpleNamespace

import pytest

from test_form4_evidence import FIXTURES, original, bindings
from tradingagents.strategies.data_sources.edgar_source import EDGARSource
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
from tradingagents.strategies.data_sources.form4_evidence import validate_form4_acquisition


def acquire(monkeypatch, *, empty=False, cap=4, mutate=None, as_of='2026-10-22'):
    doc = original()
    recent = {'form': [] if empty else ['4'], 'filingDate': [] if empty else ['2026-10-19'],
        'accessionNumber': [] if empty else [doc['accession']], 'primaryDocument': [] if empty else ['xslF345X06/ownership.xml']}
    payload = {'cik': 320193, 'filings': {'recent': recent, 'files': []}}
    raw = (FIXTURES / 'aapl_original.xml').read_bytes()
    calls = []
    def transport(url, **kwargs):
        calls.append(url)
        body = json.dumps(payload).encode() if 'submissions' in url else raw
        if mutate: body = mutate(url, body)
        return SimpleNamespace(status_code=200, url=url, headers={}, iter_content=lambda chunk_size: iter([body]), close=lambda: None)
    monkeypatch.setattr('requests.get', transport)
    result = EDGARSource().get_form4_window_evidence('AAPL', issuer_cik='0000320193',
        days_back=21, as_of=as_of, security_bindings=bindings(doc),
        absolute_deadline=time.monotonic()+10, request_cap=cap)
    return result, calls


def test_bounded_acquisition_replays_original_metadata_and_all_context(monkeypatch):
    value, calls = acquire(monkeypatch)
    assert len(calls) == 2
    assert value['coverage']['discovery_complete']
    assert value['acquisition']['requests'] == 2
    assert len(value['window']['admitted_transactions']) == 5
    assert validate_form4_acquisition(value) == value


def test_empty_window_has_positive_complete_metadata_proof(monkeypatch):
    value, calls = acquire(monkeypatch, empty=True)
    assert len(calls) == 1
    assert value['window']['documents'] == []
    assert validate_form4_acquisition(value) == value


def test_acquisition_request_limit_is_not_reset(monkeypatch):
    with pytest.raises(SourceFetchError):
        acquire(monkeypatch, cap=1)


def test_failed_native_xml_retains_raw_original_and_incomplete_scope(monkeypatch):
    with pytest.raises(SourceFetchError) as failure:
        acquire(monkeypatch, mutate=lambda url, raw: raw if 'submissions' in url else b'<ownershipDocument/>')
    partial = failure.value.partial_data['form4_qualification']
    assert partial['coverage']['complete'] is False
    assert len(partial['captured_originals']) == 2


def test_future_window_cannot_be_claimed_complete_even_when_empty(monkeypatch):
    from datetime import timedelta
    future = (datetime.now(timezone.utc)+timedelta(days=365)).date().isoformat()
    with pytest.raises(SourceFetchError):
        acquire(monkeypatch, empty=True, as_of=future)


def test_replay_scope_must_fit_retained_acquisition_clock(monkeypatch):
    from tradingagents.strategies.data_sources.form4_evidence import qualify_form4_window, digest
    value, _ = acquire(monkeypatch, empty=True)
    value['window'] = qualify_form4_window([], security_bindings=value['window']['security_bindings'],
        date_from='2099-01-01', date_to='2099-01-01')
    value['acquisition_receipt_sha256'] = digest({key: item for key, item in value.items() if key != 'acquisition_receipt_sha256'})
    with pytest.raises(ValueError, match='invalid_form4'):
        validate_form4_acquisition(value)


@pytest.mark.parametrize('damage', ['source', 'metadata', 'drop', 'coverage', 'ticker'])
def test_acquisition_replay_rejects_tampered_scope(monkeypatch, damage):
    value, _ = acquire(monkeypatch)
    if damage == 'source': value['history_originals'][0]['sha256'] = '0'*64
    elif damage == 'metadata': value['history_originals'][0]['original_base64'] = 'e30='
    elif damage == 'drop': value['window']['documents'].clear()
    elif damage == 'coverage': value['coverage']['discovery_complete'] = False
    else: value['ticker'] = 'OTHER'
    with pytest.raises(ValueError, match='invalid_form4'):
        validate_form4_acquisition(value)
