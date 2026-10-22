"""Bounded official House originals, separately bound to a publisher audit gap.

PDFs remain unparsed original evidence. They never repair publisher rows,
reconciliation, stock coverage, historical availability or trading authority.
The caller supplies acquisition under its existing provider budget/request cap.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import math
import re

from .fetch_errors import source_fetch_error

VERSION = 'congress-house-originals-v1'
MAX_DOCUMENTS = 8
MAX_DOCUMENT_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024
_REASONS = {'timeout', 'transport_error', 'http_error', 'invalid_response',
            'provider_error', 'provider_maintenance', 'batch_failure', 'capture_limit'}
_FIELDS = {'version', 'publisher_snapshot_sha256', 'started_at', 'completed_at',
    'status', 'documents', 'omitted_documents', 'publisher_diagnostics',
    'signals_enabled', 'model_context_enabled', 'order_authority', 'content_sha256'}
_DOCUMENT_FIELDS = {'doc_id', 'source_url', 'status', 'reason_code', 'http_status',
                    'acquired_at', 'bytes', 'sha256', 'body_base64'}


def _require(value):
    if not value:
        raise ValueError('invalid_congress_house_originals')


def _digest(value):
    return hashlib.sha256(json.dumps({k:v for k,v in value.items() if k != 'content_sha256'},
        sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _instant(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
    _require(isinstance(result, datetime) and result.utcoffset() == timezone.utc.utcoffset(result))
    return result


def _selection(snapshot):
    # Reconstruct native evidence before any input can select an outbound URL.
    # A caller-rehashed missing-ID list cannot select arbitrary documents.
    from .congress_disclosure_audit import _assemble, _raw, _house_index
    window = snapshot['window']
    rebuilt = _assemble(snapshot['raw_artifacts'], revision=snapshot['publisher']['revision'],
        start=window['after'], end=window['before'], acquired_at=snapshot['acquired_at'])
    _require(rebuilt['audit_snapshot'] == snapshot)
    missing = snapshot['scope_evidence']['house_reconciliation']['missing_ids']
    missing_set = set(missing)
    years = {}
    for record in snapshot['raw_artifacts']:
        if record['kind'] == 'house_index':
            year = int(record['source_url'].rsplit('/', 1)[1][:4])
            for ident in _house_index(_raw(record), year, window['after'], window['before']):
                if ident in missing_set:
                    _require(re.fullmatch(r'[0-9]{1,20}', ident) is not None)
                    years[ident] = year
    _require(set(years) == missing_set)
    return [(ident, f'https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{years[ident]}/{ident}.pdf')
            for ident in missing]


def _publisher_diagnostics(snapshot, at):
    publisher = snapshot['publisher']
    age = (at - _instant(publisher['last_source_check_at'])).total_seconds()
    _require(age >= 0)
    hours = publisher['refresh_target_hours']
    target = hours * 3600 if (type(hours) in (int, float) and math.isfinite(hours)
                              and 0 < hours <= 168) else None
    gap = snapshot['scope_evidence']['house_reconciliation']
    return {'generated_at': publisher['generated_at'],
        'last_source_check_at': publisher['last_source_check_at'],
        'last_source_check_age_seconds': age, 'refresh_target_seconds': target,
        'refresh_overdue': age > target if target is not None else None,
        'missing_house_filings': len(gap['missing_ids']),
        'additional_house_filings': len(gap['additional_ids']),
        'publisher_coverage_complete': not gap['missing_ids'] and not gap['additional_ids']}


def _pdf(raw):
    # Acquisition framing only; no claim that transaction text is extracted or
    # independently validated. Truncated/error bodies cannot count as originals.
    _require(type(raw) is bytes and 0 < len(raw) <= MAX_DOCUMENT_BYTES
             and raw.startswith(b'%PDF-') and b'%%EOF' in raw[-1024:])


def capture_house_originals(snapshot, *, download, now):
    """Retain a bounded missing-ID subset; download must use the caller budget."""
    selected = _selection(snapshot)
    started = _instant(now())
    _require(started >= _instant(snapshot['acquired_at']))
    documents, size = [], 0
    for ident, url in selected[:MAX_DOCUMENTS]:
        row = dict(doc_id=ident, source_url=url, status='not_attempted', reason_code='capture_limit',
                   http_status=None, acquired_at=None, bytes=0, sha256=None, body_base64=None)
        if size < MAX_TOTAL_BYTES:
            try:
                raw = download(url, min(MAX_DOCUMENT_BYTES, MAX_TOTAL_BYTES - size))
                _pdf(raw)
                _require(size + len(raw) <= MAX_TOTAL_BYTES)
                acquired = _instant(now())
                _require(acquired >= started)
                size += len(raw)
                row.update(status='original_only', reason_code=None, http_status=200,
                    acquired_at=acquired.isoformat(), bytes=len(raw),
                    sha256=hashlib.sha256(raw).hexdigest(), body_base64=base64.b64encode(raw).decode('ascii'))
            except Exception as error:
                failure = source_fetch_error('House original acquisition failed', error)
                row.update(status='failed', reason_code=(failure.reason_code
                    if not isinstance(error, ValueError) else 'invalid_response'), http_status=failure.http_status)
        documents.append(row)
    completed = _instant(now())
    result = {'version': VERSION, 'publisher_snapshot_sha256': snapshot['content_sha256'],
        'started_at': started.isoformat(), 'completed_at': completed.isoformat(),
        'documents': documents, 'omitted_documents': max(0, len(selected) - MAX_DOCUMENTS),
        'publisher_diagnostics': _publisher_diagnostics(snapshot, completed),
        'signals_enabled': False, 'model_context_enabled': False, 'order_authority': False}
    result['status'] = _status(result)
    result['content_sha256'] = _digest(result)
    validate_house_originals(result, snapshot, now=completed)
    return result


def _status(value):
    if not value['documents'] and not value['omitted_documents']:
        return 'not_required'
    return ('originals_captured' if not value['omitted_documents'] and
            all(r['status'] == 'original_only' for r in value['documents']) else 'partial_originals')


def validate_house_originals(value, snapshot, *, now=None):
    """Validate a separate forward capture without changing frozen publisher v1."""
    try:
        selected = _selection(snapshot)
        _require(type(value) is dict and set(value) == _FIELDS and value['version'] == VERSION
                 and value['publisher_snapshot_sha256'] == snapshot['content_sha256']
                 and value['content_sha256'] == _digest(value)
                 and all(value[k] is False for k in ('signals_enabled', 'model_context_enabled', 'order_authority')))
        start, end = _instant(value['started_at']), _instant(value['completed_at'])
        _require(_instant(snapshot['acquired_at']) <= start <= end
                 and (now is None or end <= _instant(now)))
        rows = value['documents']
        _require(type(rows) is list and len(rows) == min(MAX_DOCUMENTS, len(selected))
                 and type(value['omitted_documents']) is int
                 and value['omitted_documents'] == max(0, len(selected) - MAX_DOCUMENTS)
                 and value['publisher_diagnostics'] == _publisher_diagnostics(snapshot, end))
        total = 0
        for row, (ident, url) in zip(rows, selected):
            _require(type(row) is dict and set(row) == _DOCUMENT_FIELDS
                     and row['doc_id'] == ident and row['source_url'] == url
                     and row['status'] in {'original_only', 'failed', 'not_attempted'}
                     and type(row['bytes']) is int)
            if row['status'] == 'original_only':
                encoded = row['body_base64']
                _require(type(encoded) is str and len(encoded) <= (MAX_DOCUMENT_BYTES + 2) // 3 * 4)
                raw = base64.b64decode(encoded, validate=True)
                _pdf(raw); total += len(raw)
                _require(row['bytes'] == len(raw) and row['sha256'] == hashlib.sha256(raw).hexdigest()
                         and row['reason_code'] is None and type(row['http_status']) is int
                         and row['http_status'] == 200 and start <= _instant(row['acquired_at']) <= end)
            else:
                _require(row['bytes'] == 0 and row['body_base64'] is row['sha256'] is row['acquired_at'] is None
                         and row['reason_code'] in _REASONS
                         and (row['http_status'] is None or type(row['http_status']) is int
                              and 100 <= row['http_status'] <= 599)
                         and (row['status'] != 'not_attempted' or row['reason_code'] == 'capture_limit'))
        _require(total <= MAX_TOTAL_BYTES and value['status'] == _status(value))
        return value
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise ValueError('invalid_congress_house_originals') from None


def originals_summary(value):
    """Compact projection; callers first validate the full bound evidence."""
    rows = value['documents']
    return canonical_originals_summary({'version': VERSION, 'content_sha256': value['content_sha256'],
        'status': value['status'], 'completed_at': value['completed_at'],
        'counts': {**{status: sum(r['status'] == status for r in rows)
                      for status in ('original_only', 'failed', 'not_attempted')},
                   'omitted': value['omitted_documents']},
        'captured_bytes': sum(r['bytes'] for r in rows),
        'publisher_diagnostics': value['publisher_diagnostics'], 'order_authority': False})


def canonical_originals_summary(value):
    """Validate a compact report projection without embedding PDF bodies."""
    try:
        _require(type(value) is dict and set(value) == {'version', 'content_sha256', 'status',
            'completed_at', 'counts', 'captured_bytes', 'publisher_diagnostics', 'order_authority'}
            and value['version'] == VERSION and value['order_authority'] is False
            and type(value['content_sha256']) is str
            and re.fullmatch('[0-9a-f]{64}', value['content_sha256']) is not None)
        completed = _instant(value['completed_at']); counts = value['counts']
        _require(type(counts) is dict and set(counts) == {'original_only', 'failed', 'not_attempted', 'omitted'}
            and all(type(n) is int and 0 <= n <= 100_000 for n in counts.values())
            and sum(n for k,n in counts.items() if k != 'omitted') <= MAX_DOCUMENTS
            and type(value['captured_bytes']) is int and 0 <= value['captured_bytes'] <= MAX_TOTAL_BYTES
            and (counts['original_only'] > 0) == (value['captured_bytes'] > 0))
        expected = ('not_required' if not sum(counts.values()) else 'originals_captured'
            if not any(counts[k] for k in ('failed', 'not_attempted', 'omitted')) else 'partial_originals')
        _require(value['status'] == expected)
        diagnostic = value['publisher_diagnostics']
        _require(type(diagnostic) is dict and set(diagnostic) == {'generated_at', 'last_source_check_at',
            'last_source_check_age_seconds', 'refresh_target_seconds', 'refresh_overdue',
            'missing_house_filings', 'additional_house_filings', 'publisher_coverage_complete'})
        age = (completed - _instant(diagnostic['last_source_check_at'])).total_seconds()
        _require(_instant(diagnostic['generated_at']) <= completed and age >= 0
            and type(diagnostic['last_source_check_age_seconds']) in (int, float)
            and diagnostic['last_source_check_age_seconds'] == age
            and type(diagnostic['missing_house_filings']) is int
            and diagnostic['missing_house_filings'] == sum(counts.values())
            and type(diagnostic['additional_house_filings']) is int
            and 0 <= diagnostic['additional_house_filings'] <= 100_000
            and diagnostic['publisher_coverage_complete'] is
                (not diagnostic['missing_house_filings'] and not diagnostic['additional_house_filings']))
        target = diagnostic['refresh_target_seconds']
        _require(target is None or type(target) in (int, float) and math.isfinite(target) and 0 < target <= 168*3600)
        _require(diagnostic['refresh_overdue'] is (age > target if target is not None else None))
        return json.loads(json.dumps(value, allow_nan=False))
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise ValueError('invalid_congress_house_originals_summary') from None


def validate_original_diagnostics(rows, evidence):
    """Keep supplementary request failures visible without claiming coverage."""
    documents = [r for r in evidence['documents'] if r['status'] != 'not_attempted']
    _require(type(rows) is list and len(rows) == len(documents))
    for row, document in zip(rows, documents):
        _require(type(row) is dict and set(row) == {'provider', 'operation', 'reason_code',
            'http_status', 'attempts', 'recovered'} and row['provider'] == 'congress'
            and row['operation'] == 'house_original' and type(row['attempts']) is int
            and row['attempts'] in (0, 1) and row['recovered'] is False)
        # A bad PDF framing check happens after the bounded HTTP acquisition;
        # that successful request must not be represented as failed transport.
        if row['reason_code'] is None:
            _require(row['http_status'] is None and row['attempts'] == 1
                     and (document['status'] == 'original_only' or document['reason_code'] == 'invalid_response'))
        else:
            _require(document['status'] == 'failed' and row['reason_code'] == document['reason_code']
                and row['http_status'] == document['http_status']
                and (row['attempts'] == 1 or row['reason_code'] == 'timeout'))
