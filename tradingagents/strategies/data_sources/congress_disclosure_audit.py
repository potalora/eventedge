"""Revision-bound free disclosure snapshots for display/audit, never signals.

Publisher extraction and Senate discovery remain separately unverified. House
filing-ID reconciliation does not prove transcription or stock-trade coverage.
"""
from __future__ import annotations

import base64
from collections import Counter
from datetime import date, datetime, timezone
import hashlib
import io
import json
import math
import re
from urllib.parse import parse_qsl, urljoin, urlsplit
import zipfile
from xml.etree import ElementTree

import requests

from .fetch_errors import SourceFetchError
from .request_policy import (provider_call, provider_subbudget, provider_timeout,
                             read_bounded_response)

POLICY = 'display_audit_only_v1'
DATASET = 'austin-starks/congressional-stock-trades'
HF = 'https://huggingface.co'
REVISION_URL = f'{HF}/api/datasets/{DATASET}/revision/main'
TABLES = ('political_filings', 'political_trades')
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 128 * 1024 * 1024
MAX_DECODED_BYTES = 64 * 1024 * 1024
MAX_ROWS = 100_000
MAX_FILES = 32
MAX_REQUESTS = 108  # 32 shards + 2 indexes + revision/manifest, at most 2 redirects each.
MAX_AGE_SECONDS = 48 * 3600
_CDN_HOSTS = frozenset({'cdn-lfs.huggingface.co', 'cdn-lfs-us-1.huggingface.co',
                       'cas-bridge.xethub.hf.co', 'us.aws.cdn.hf.co'})
_SHA = re.compile(r'[0-9a-f]{64}')
_REVISION = re.compile(r'[0-9a-f]{40}')
_FILINGS = ('chamber docId filerFirst filerLast filerSuffix stateDistrict filingDate availableAt '
    'availabilitySource sourceUrl rawArchiveKey rawSha256 parseMethod extractionStatus failureReason '
    'extractedRows extractionModel contractVersion ocrArchiveKey amendedReportDate reportDate '
    'processedAt filerKey memberId displayName identitySource').split()
_TRADES = ('chamber docId rowIndex sourceTransactionId filerFirst filerLast owner ownerCodeRaw action '
    'partialSale actionCodeRaw transactionDate notificationDate filingDate availableAt availabilitySource '
    'assetDescription printedTicker resolvedTicker resolutionStatus resolutionReason assetTypeCode '
    'assetTypeLabel amountBracket amountLow amountHigh capGainsOver200 comment filingStatus sourceUrl '
    'rawArchiveKey rawSha256 filerKey memberId displayName identitySource').split()
_ARROW_TYPES = {name: 'string' for name in _FILINGS + _TRADES}
_ARROW_TYPES.update({name: 'date32[day]' for name in
    ('filingDate','transactionDate','notificationDate','amendedReportDate','reportDate')})
_ARROW_TYPES.update({name: 'timestamp[us]' for name in ('availableAt','processedAt')})
_ARROW_TYPES.update({'extractedRows':'int32','rowIndex':'int32','partialSale':'bool',
                    'capGainsOver200':'bool','amountLow':'double','amountHigh':'double'})
_TERMS = ['https://efdsearch.senate.gov/search/home/',
          f'{HF}/datasets/{DATASET}/raw/main/LICENSE_DATA.md']


class _AnonymousAuth(requests.auth.AuthBase):
    """Explicit no-op auth prevents Requests' ambient .netrc fallback."""
    def __call__(self, request):
        return request


def _utcnow():
    return datetime.now(timezone.utc)


def _require(ok):
    if not ok:
        raise ValueError('invalid_congress_audit_snapshot')


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result)
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON')))


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False).encode()


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _snapshot_digest(snapshot):
    return _sha(_canonical({k: v for k, v in snapshot.items() if k != 'content_sha256'}))


def _aware(value):
    _require(isinstance(value, str))
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    _require(result.tzinfo is not None and result.utcoffset().total_seconds() == 0)
    return result


def _day(value):
    _require(isinstance(value, str))
    result = date.fromisoformat(value)
    _require(result.isoformat() == value)
    return result


def _years(start, end, acquired):
    first, last = _day(start), _day(end)
    _require(0 <= (last - first).days <= 366 and last <= acquired.date())
    _require(last.year - first.year <= 1)
    return sorted(set(range(first.year, last.year + 1)) | {acquired.year})


def _record(kind, source_url, raw):
    return {'kind': kind, 'source_url': source_url, 'bytes': len(raw), 'sha256': _sha(raw),
            'body_base64': base64.b64encode(raw).decode('ascii')}


def _raw(record):
    _require(isinstance(record, dict) and set(record) == {'kind','source_url','bytes','sha256','body_base64'})
    _require(type(record['bytes']) is int and 0 <= record['bytes'] <= MAX_FILE_BYTES
             and isinstance(record['sha256'], str) and _SHA.fullmatch(record['sha256'])
             and isinstance(record['body_base64'], str)
             and len(record['body_base64']) <= (MAX_FILE_BYTES + 2) // 3 * 4)
    raw = base64.b64decode(record['body_base64'], validate=True)
    _require(len(raw) == record['bytes'] and _sha(raw) == record['sha256'])
    return raw


def _redirect_allowed(target, *, original, revision, shard):
    p = urlsplit(target)
    if p.scheme != 'https' or p.username or p.password or p.port not in (None, 443) or p.fragment:
        return False
    if p.hostname in _CDN_HOSTS:
        return shard
    if p.hostname != 'huggingface.co':
        return False
    suffix = original.split(f'/resolve/{revision}/', 1)
    if len(suffix) != 2:
        return False
    original_path = f'/datasets/{DATASET}/resolve/{revision}/{suffix[1]}'
    cache_path = f'/api/resolve-cache/datasets/{DATASET}/{revision}/{suffix[1]}'
    if p.path not in (original_path, cache_path):
        return False
    if not p.query:
        return True
    # HF's immutable cache redirect carries the original path as an empty
    # query key and its quoted blob hash. Neither may change the bound path.
    fields = parse_qsl(p.query, keep_blank_values=True)
    query = dict(fields)
    return (p.path == cache_path and len(fields) == len(query) == 2
            and query.get(original_path) == ''
            and re.fullmatch(r'"(?:[0-9a-f]{40}|[0-9a-f]{64})"', query.get('etag', '')) is not None)


def _download(url, *, limit, revision, shard, state):
    """One attempt, with explicit redirects; no cookies or auth are forwarded."""
    original, target = url, url
    for hop in range(3):
        provider_timeout('congress')
        if state['requests'] >= MAX_REQUESTS:
            raise SourceFetchError('Congress audit request cap exhausted', reason_code='invalid_response')
        state['requests'] += 1
        response = requests.get(target, headers={'User-Agent': 'EventEdge-disclosure-audit/1.0'},
            auth=_AnonymousAuth(), timeout=provider_timeout('congress'), stream=True, allow_redirects=False)
        try:
            provider_timeout('congress')
            status = response.status_code
            _require(type(status) is int)
            if status in (301, 302, 303, 307, 308):
                location = response.headers.get('Location')
                candidate = urljoin(target, location) if isinstance(location, str) else ''
                if not (hop < 2 and _redirect_allowed(candidate, original=original, revision=revision, shard=shard)):
                    raise SourceFetchError('Congress audit redirect invalid', reason_code='invalid_response')
                target = candidate
                continue
            if status != 200:
                raise SourceFetchError('Congress audit HTTP request failed', reason_code='http_error', http_status=status)
            raw = read_bounded_response(response, provider='congress',
                max_bytes=min(limit, MAX_TOTAL_BYTES - state['bytes']))
            state['bytes'] += len(raw)
            provider_timeout('congress')
            return raw
        finally:
            response.close()
    raise ValueError('invalid_congress_audit_redirect')


def _planned_files(manifest, years):
    _require(isinstance(manifest, dict) and manifest.get('schemaVersion') == 2
             and manifest.get('dataset') == DATASET and isinstance(manifest.get('tables'), dict))
    _aware(manifest['generatedAt']); _aware(manifest['lastSourceCheckAt'])
    _require(isinstance(manifest.get('audit'), dict) and manifest['audit'].get('passed') is True)
    plan = []
    for table in TABLES:
        info = manifest['tables'][table]
        _require(isinstance(info, dict) and isinstance(info.get('manifests'), list))
        selected = [m for m in info['manifests'] if isinstance(m, dict) and m.get('year') in years]
        _require(len(selected) == len(years) and {m['year'] for m in selected} == set(years))
        for entry in sorted(selected, key=lambda m: m['year']):
            files = entry.get('files')
            _require(isinstance(files, list) and 0 < len(files) <= MAX_FILES)
            seen_parts, declared_total = set(), None
            for f in files:
                _require(isinstance(f, dict) and isinstance(f.get('publicPath'), str))
                match = re.fullmatch(fr'data/{table}/{entry["year"]}-([0-9]{{5}})-of-([0-9]{{5}})\.parquet', f['publicPath'])
                _require(match is not None)
                part, total = map(int, match.groups())
                _require(0 <= part < total <= MAX_FILES and part not in seen_parts
                         and declared_total in (None, total))
                seen_parts.add(part); declared_total = total
                _require(type(f.get('size')) is int and 0 < f['size'] <= MAX_FILE_BYTES
                         and isinstance(f.get('sha256'), str) and _SHA.fullmatch(f['sha256']))
                plan.append((table, f))
            _require(seen_parts == set(range(declared_total)))
    _require(len(plan) <= MAX_FILES and len({f['publicPath'] for _,f in plan}) == len(plan))
    return plan


def _rows(raw, table, remaining_rows, remaining_uncompressed):
    import pyarrow.parquet as parquet
    file = parquet.ParquetFile(io.BytesIO(raw))
    meta = file.metadata
    _require(meta.num_rows <= remaining_rows and 0 < meta.num_columns <= 64 and meta.num_row_groups <= 1024)
    uncompressed = sum(meta.row_group(r).column(c).total_uncompressed_size
        for r in range(meta.num_row_groups) for c in range(meta.num_columns))
    _require(0 <= uncompressed <= remaining_uncompressed)
    schema = {f.name: str(f.type) for f in file.schema_arrow}
    names = _FILINGS if table == TABLES[0] else _TRADES
    _require(len(schema) == meta.num_columns and schema == {name:_ARROW_TYPES[name] for name in names})
    rows, encoded = [], 0
    for batch in file.iter_batches(batch_size=512, use_threads=False):
        for row in batch.to_pylist():
            for key, value in row.items():
                if isinstance(value, (datetime, date)):
                    row[key] = value.isoformat()  # Preserve naive publisher timestamps; do not manufacture UTC.
                elif value is not None:
                    _require(type(value) in (str, int, float, bool))
                    if isinstance(value, float): _require(math.isfinite(value))
            encoded += len(_canonical(row))
            _require(encoded <= MAX_DECODED_BYTES)
            rows.append(row)
    _require(len(rows) == meta.num_rows)
    return rows, schema, uncompressed, encoded


def _official_url(row):
    p = urlsplit(row['sourceUrl'])
    _require(p.scheme == 'https' and not p.username and not p.password and not p.query and not p.fragment
        and ((row['chamber'] == 'house' and p.netloc == 'disclosures-clerk.house.gov'
              and re.fullmatch(r'/public_disc/ptr-pdfs/[0-9]{4}/[0-9]+\.pdf', p.path))
             or (row['chamber'] == 'senate' and p.netloc == 'efdsearch.senate.gov'
                 and re.fullmatch(r'/search/view/(ptr|paper)/[A-Za-z0-9-]+/', p.path))))


def _table_integrity(tables):
    filings, trades, counts = {}, set(), Counter()
    for row in tables[TABLES[0]]:
        key = (row['chamber'], row['docId'])
        _require(row['chamber'] in ('house','senate') and isinstance(row['docId'], str)
                 and 0 < len(row['docId']) <= 128 and key not in filings)
        _day(row['filingDate']); _official_url(row)
        _require(isinstance(row['rawSha256'], str) and _SHA.fullmatch(row['rawSha256'])
                 and isinstance(row['rawArchiveKey'], str) and bool(row['rawArchiveKey'])
                 and row['extractionStatus'] in ('ok','failed') and type(row['extractedRows']) is int
                 and row['extractedRows'] >= 0)
        filings[key] = row
    for row in tables[TABLES[1]]:
        key = (row['chamber'], row['docId'])
        identity = (*key, row['rowIndex'])
        _require(key in filings and type(row['rowIndex']) is int and row['rowIndex'] >= 0
                 and identity not in trades)
        source = filings[key]
        _require(source['extractionStatus'] == 'ok' and all(row[k] == source[k]
            for k in ('sourceUrl','rawSha256','rawArchiveKey','filingDate')))
        trades.add(identity); counts[key] += 1
    _require(all(row['extractedRows'] == counts[key] for key,row in filings.items()))


def _house_index(raw, year, start, end):
    _require(len(raw) <= MAX_METADATA_BYTES)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        infos = archive.infolist()
        _require(len(infos) == 2 and {i.filename for i in infos} == {f'{year}FD.xml',f'{year}FD.txt'}
                 and sum(i.file_size for i in infos) <= 4 * 1024 * 1024
                 and all(not i.is_dir() and not i.flag_bits & 1 for i in infos))
        xml = archive.read(f'{year}FD.xml')
    # The official index is UTF-8. Decode before entity checks so UTF-16 cannot
    # hide a DTD behind interleaved NUL bytes; no alternative encoding repair.
    text = xml.decode('utf-8-sig')
    _require('\x00' not in text and '<!DOCTYPE' not in text.upper() and '<!ENTITY' not in text.upper())
    root = ElementTree.fromstring(text)
    _require(root.tag == 'FinancialDisclosure' and len(root) <= MAX_ROWS)
    selected = []
    for element in root:
        _require(element.tag == 'Member' and len({child.tag for child in element}) == len(element))
        row = {child.tag: child.text for child in element}
        _require(row.get('FilingType') in {'P','W','C','X','D','A','H','T'})
        if row.get('FilingType') != 'P':
            continue
        _require(isinstance(row.get('DocID'), str) and row['DocID'].isdigit()
                 and isinstance(row.get('FilingDate'), str))
        filed = datetime.strptime(row['FilingDate'], '%m/%d/%Y').date().isoformat()
        if start <= filed <= end:
            selected.append(row['DocID'])
    _require(len(selected) == len(set(selected)))
    return selected


def _assemble(artifacts, *, revision, start, end, acquired_at):
    acquired = _aware(acquired_at)
    years = _years(start, end, acquired)
    _require(isinstance(revision, str) and _REVISION.fullmatch(revision))
    _require(isinstance(artifacts, list) and len(artifacts) <= MAX_FILES + 4)
    records = {}
    total = 0
    for record in artifacts:
        raw = _raw(record); total += len(raw)
        _require(total <= MAX_TOTAL_BYTES and record['source_url'] not in records)
        records[record['source_url']] = (record,raw)
    _require(REVISION_URL in records)
    _require(records[REVISION_URL][0]['kind'] == 'revision_metadata'
             and len(records[REVISION_URL][1]) <= MAX_METADATA_BYTES)
    metadata = _json(records[REVISION_URL][1])
    _require(metadata.get('sha') == revision and metadata.get('id') == DATASET)
    manifest_url = f'{HF}/datasets/{DATASET}/resolve/{revision}/snapshot.json'
    manifest_record, raw_manifest = records[manifest_url]
    _require(manifest_record['kind'] == 'manifest' and len(raw_manifest) <= MAX_METADATA_BYTES)
    manifest = _json(raw_manifest)
    plan = _planned_files(manifest, years)
    tables = {t: [] for t in TABLES}; schemas = {}; uncompressed = decoded = 0
    expected = {REVISION_URL,manifest_url}
    for table, info in plan:
        url = f'{HF}/datasets/{DATASET}/resolve/{revision}/{info["publicPath"]}'
        expected.add(url)
        record, raw = records[url]
        _require(record['kind'] == 'parquet' and len(raw) == info['size'] and record['sha256'] == info['sha256'])
        rows, schema, size, encoded = _rows(raw, table, MAX_ROWS-sum(map(len,tables.values())), MAX_UNCOMPRESSED_BYTES-uncompressed)
        _require(table not in schemas or schemas[table] == schema)
        schemas[table] = schema; tables[table].extend(rows); uncompressed += size; decoded += encoded
        _require(decoded <= MAX_DECODED_BYTES)
    _table_integrity(tables)
    window_filings = {c: sorted(r['docId'] for r in tables[TABLES[0]]
        if r['chamber'] == c and start <= r['filingDate'] <= end) for c in ('house','senate')}
    official = []
    for year in range(_day(start).year, _day(end).year + 1):
        url = f'https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.ZIP'
        expected.add(url)
        record,raw = records[url]
        _require(record['kind'] == 'house_index')
        official.extend(_house_index(raw, year, start, end))
    _require(set(records) == expected and len(official) == len(set(official)))
    missing, additional = sorted(set(official)-set(window_filings['house'])), sorted(set(window_filings['house'])-set(official))
    generated, checked = _aware(manifest['generatedAt']), _aware(manifest['lastSourceCheckAt'])
    _require(generated <= acquired and checked <= acquired)
    scope = {'selected_years': years, 'window_filing_ids': window_filings,
        'window_filing_counts': {c:len(v) for c,v in window_filings.items()},
        'window_printed_row_ids': [[r['chamber'],r['docId'],r['rowIndex']] for r in tables[TABLES[1]] if start <= r['filingDate'] <= end],
        'house_reconciliation': {'official_ids':sorted(official), 'matched_count':len(set(official)&set(window_filings['house'])),
            'missing_ids':missing, 'additional_ids':additional},
        'senate_coverage':'publisher_only_unverified',
        'extraction':'publisher_reported_not_independently_validated',
        'historical_availability':'not_independently_witnessed', 'source_use_notice_urls':_TERMS}
    snapshot = {'schema_version':1,'policy':POLICY,'mode':'display_audit_only',
        'signals_enabled':False,'model_context_enabled':False,'acquired_at':acquired_at,
        'window':{'after':start,'before':end,'basis':'filing_date_inclusive'},
        'publisher':{'dataset':DATASET,'revision':revision,'manifest_sha256':manifest_record['sha256'],
            'generated_at':manifest['generatedAt'],'last_source_check_at':manifest['lastSourceCheckAt'],
            'refresh_target_hours':manifest.get('refreshPolicy',{}).get('targetIntervalHours')},
        'raw_artifacts':artifacts,'tables':tables,'table_schemas':schemas,'scope_evidence':scope}
    snapshot['content_sha256'] = _snapshot_digest(snapshot)
    complete = not missing and not additional
    return {'audit_snapshot':snapshot,'coverage':{'policy':POLICY,'mode':'display_audit_only',
        'status':'audit_snapshot_complete' if complete else 'partial_audit_gap','complete':complete,
        'acquisition_complete':True,'integrity_valid':True,'house_ids_reconciled':complete,
        'stock_disclosure_complete':False,'senate_coverage':'publisher_only_unverified'}}


def fetch_audit_snapshot(*, date_filed_after, date_filed_before, absolute_deadline,
                         subbudget_seconds=90, max_snapshot_age_seconds=MAX_AGE_SECONDS):
    """Acquire all declared audit inputs under the original caller deadline."""
    artifacts = []
    try:
        started = _utcnow(); years = _years(date_filed_after, date_filed_before, started)
        _require(type(max_snapshot_age_seconds) in (int,float) and math.isfinite(max_snapshot_age_seconds)
                 and 0 < max_snapshot_age_seconds <= MAX_AGE_SECONDS)
        with provider_subbudget('congress', maximum_seconds=subbudget_seconds,
                absolute_deadline=absolute_deadline, max_attempts=1):
            state = {'bytes':0,'requests':0}
            def get(url, kind, *, revision='', limit=MAX_METADATA_BYTES, shard=False):
                raw = provider_call('congress','disclosure_audit', lambda: _download(url,
                    limit=limit, revision=revision, shard=shard, state=state))
                artifacts.append(_record(kind,url,raw)); return raw
            metadata = _json(get(REVISION_URL,'revision_metadata'))
            revision = metadata['sha']
            _require(isinstance(revision,str) and _REVISION.fullmatch(revision) and metadata.get('id') == DATASET)
            manifest = _json(get(f'{HF}/datasets/{DATASET}/resolve/{revision}/snapshot.json','manifest',revision=revision))
            for _,info in _planned_files(manifest,years):
                get(f'{HF}/datasets/{DATASET}/resolve/{revision}/{info["publicPath"]}',
                    'parquet',revision=revision,limit=MAX_FILE_BYTES,shard=True)
            for year in range(_day(date_filed_after).year,_day(date_filed_before).year+1):
                get(f'https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.ZIP','house_index')
            provider_timeout('congress')
            payload = _assemble(artifacts,revision=revision,start=date_filed_after,end=date_filed_before,
                                acquired_at=_utcnow().isoformat())
            provider_timeout('congress')
            validate_audit_snapshot(payload,date_filed_after=date_filed_after,date_filed_before=date_filed_before,
                                    now=_utcnow(),max_evidence_age_seconds=max_snapshot_age_seconds)
            provider_timeout('congress')
            if not payload['coverage']['complete']:
                raise SourceFetchError('Congress audit House filing reconciliation gap',
                    reason_code='invalid_response', partial_data=payload)
            return payload
    except SourceFetchError as error:
        if not error.partial_data:
            error.partial_data = _partial_capture(artifacts)
        raise
    except (ValueError,TypeError,KeyError,OverflowError,zipfile.BadZipFile,ElementTree.ParseError):
        raise SourceFetchError('Congress audit snapshot invalid',reason_code='invalid_response',
                              partial_data=_partial_capture(artifacts)) from None


def _partial_capture(artifacts):
    return {'raw_audit_artifacts':artifacts,'coverage':{'policy':POLICY,
        'mode':'display_audit_only','status':'audit_acquisition_failed','complete':False,
        'acquisition_complete':False,'stock_disclosure_complete':False}}


def validate_audit_snapshot(payload, *, date_filed_after, date_filed_before, now=None,
                            max_evidence_age_seconds=MAX_AGE_SECONDS):
    """Reconstruct full typed rows and provenance; structural replay can omit age."""
    try:
        _require(isinstance(payload,dict) and set(payload)-{'audit_snapshot','coverage','_request_diagnostics'} == set())
        snapshot = payload['audit_snapshot']
        _require(isinstance(snapshot,dict) and snapshot.get('content_sha256') == _snapshot_digest(snapshot))
        expected = _assemble(snapshot['raw_artifacts'],revision=snapshot['publisher']['revision'],
            start=date_filed_after,end=date_filed_before,acquired_at=snapshot['acquired_at'])
        _require(snapshot == expected['audit_snapshot'] and payload['coverage'] == expected['coverage'])
        check = now if now is not None else (_utcnow() if max_evidence_age_seconds is not None else None)
        if check is not None:
            _require(isinstance(check,datetime) and check.tzinfo is not None and check.utcoffset().total_seconds() == 0)
            _require(_aware(snapshot['acquired_at']) <= check)
            if max_evidence_age_seconds is not None:
                _require(type(max_evidence_age_seconds) in (int,float) and math.isfinite(max_evidence_age_seconds)
                         and 0 <= max_evidence_age_seconds <= MAX_AGE_SECONDS)
                _require((check-_aware(snapshot['acquired_at'])).total_seconds() <= max_evidence_age_seconds)
                _require((check-_aware(snapshot['publisher']['last_source_check_at'])).total_seconds() <= max_evidence_age_seconds)
                _require((check-_aware(snapshot['publisher']['generated_at'])).total_seconds() <= max_evidence_age_seconds)
        return snapshot
    except (ValueError,TypeError,KeyError,OverflowError,zipfile.BadZipFile,ElementTree.ParseError):
        raise ValueError('invalid_congress_audit_snapshot') from None


def audit_summary(payload):
    """Compact canonical display/report limits, never a signal summary."""
    snapshot = payload['audit_snapshot']; scope = snapshot['scope_evidence']
    return canonical_audit_summary({'policy':POLICY,'mode':'display_audit_only','signals_enabled':False,
        'model_context_enabled':False,'content_sha256':snapshot['content_sha256'],
        'revision':snapshot['publisher']['revision'],'acquired_at':snapshot['acquired_at'],
        'publisher_generated_at':snapshot['publisher']['generated_at'],
        'window':snapshot['window'],'filing_counts':scope['window_filing_counts'],
        'printed_row_count':len(scope['window_printed_row_ids']),
        'house_matched_filings':scope['house_reconciliation']['matched_count'],
        'house_missing_filings':len(scope['house_reconciliation']['missing_ids']),
        'house_additional_filings':len(scope['house_reconciliation']['additional_ids']),
        'senate_coverage':scope['senate_coverage'],'extraction':scope['extraction'],
        'historical_availability':scope['historical_availability'],
        'stock_disclosure_complete':False,'status':payload['coverage']['status']})


def canonical_audit_summary(summary):
    """Validate compact report fields without parsing raw disclosure artifacts."""
    try:
        fields = {'policy','mode','signals_enabled','model_context_enabled','content_sha256',
            'revision','acquired_at','publisher_generated_at','window','filing_counts',
            'printed_row_count','house_matched_filings','house_missing_filings',
            'house_additional_filings','senate_coverage','extraction','historical_availability',
            'stock_disclosure_complete','status'}
        _require(isinstance(summary,dict) and set(summary) == fields)
        _require(summary['policy'] == POLICY and summary['mode'] == 'display_audit_only'
            and summary['signals_enabled'] is False and summary['model_context_enabled'] is False
            and summary['stock_disclosure_complete'] is False
            and isinstance(summary['content_sha256'],str) and _SHA.fullmatch(summary['content_sha256'])
            and isinstance(summary['revision'],str) and _REVISION.fullmatch(summary['revision']))
        acquired = _aware(summary['acquired_at'])
        _require(_aware(summary['publisher_generated_at']) <= acquired)
        window = summary['window']
        _require(isinstance(window,dict) and set(window) == {'after','before','basis'}
                 and window['basis'] == 'filing_date_inclusive')
        _years(window['after'],window['before'],acquired)
        counts = summary['filing_counts']
        _require(isinstance(counts,dict) and set(counts) == {'house','senate'}
                 and all(type(v) is int and 0 <= v <= MAX_ROWS for v in counts.values()))
        for field in ('printed_row_count','house_matched_filings','house_missing_filings','house_additional_filings'):
            _require(type(summary[field]) is int and 0 <= summary[field] <= MAX_ROWS)
        _require(summary['house_matched_filings'] <= counts['house']
                 and summary['house_matched_filings'] + summary['house_additional_filings'] == counts['house']
                 and summary['senate_coverage'] == 'publisher_only_unverified'
                 and summary['extraction'] == 'publisher_reported_not_independently_validated'
                 and summary['historical_availability'] == 'not_independently_witnessed'
                 and summary['status'] in ('audit_snapshot_complete','partial_audit_gap'))
        if summary['status'] == 'audit_snapshot_complete':
            _require(summary['house_missing_filings'] == summary['house_additional_filings'] == 0
                     and summary['house_matched_filings'] == counts['house'])
        else:
            _require(summary['house_missing_filings'] > 0 or summary['house_additional_filings'] > 0)
        return _json(_canonical(summary))
    except (ValueError,TypeError,KeyError,OverflowError):
        raise ValueError('invalid_congress_audit_summary') from None
