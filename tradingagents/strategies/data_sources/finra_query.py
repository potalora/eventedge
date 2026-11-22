"""Bounded official FINRA queries for exact symbols at one published settlement.

This acquisition does not populate or establish coverage of the SDK archive.
Schema four describes query coverage; schemas one through three remain the
separate native-cache contract in finra_bulk.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import hashlib
import json
import math
import re
from types import SimpleNamespace

import requests

from .evidence import acquisition_time
from .fetch_errors import SourceFetchError, source_fetch_error, source_number
from .finra_schedule import (
    DYNAMIC_PUBLICATION_POLICY,
    MAX_SETTLEMENT_AGE_DAYS,
    validate_current_settlement,
)
from .request_policy import (current_provider_deadline, provider_clock_time,
    provider_subbudget, provider_timeout, read_bounded_response)

ENDPOINT = 'https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest'
SOURCE = 'official_finra_public_query_api'
SCOPE = 'exact_requested_symbols_at_required_settlement'
MAX_SYMBOLS = 1000
BATCH_SIZE = 100
MAX_BATCHES = 10
MAX_BYTES = 1024 * 1024
MAX_HEADER_COUNT = 1_000_000
REASONS = {'timeout', 'transport_error', 'http_error', 'invalid_response', 'provider_error', 'batch_failure'}
_SYMBOL = re.compile(r'[A-Za-z0-9.^=_-]{1,32}\Z')
_DIGEST = re.compile(r'[a-f0-9]{64}\Z')


def population_digest(symbols):
    return hashlib.sha256(json.dumps(sorted(symbols), ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()


def new_attempt():
    return {'status': 'error', 'reason_code': 'provider_error', 'source': SOURCE, 'endpoint': ENDPOINT,
        'acquired_at': None, 'query_started_at': None, 'query_finished_at': None,
        'query_elapsed_seconds': None, 'query_population_sha256': None,
        'row_count': 0, 'rowset_sha256': None, 'query_coverage': None, 'batches': []}


def _invalid():
    return SourceFetchError('Invalid FINRA query response', reason_code='invalid_response')


def fail_attempt(attempt, error):
    """Withdraw every positive claim after an acquisition or caller deadline failure."""
    failure = source_fetch_error('FINRA query acquisition failed', error)
    attempt.update(status='error', reason_code=failure.reason_code)
    coverage = attempt.get('query_coverage')
    if coverage is not None:
        coverage.update(accepted_symbols=[], invalid_symbols=[],
            missing_symbols=list(coverage['requested_symbols']), current_complete=False)
    return failure


def _symbols(value, maximum=MAX_SYMBOLS):
    return (type(value) is list and len(value) <= maximum
        and all(type(symbol) is str and _SYMBOL.fullmatch(symbol) for symbol in value)
        and value == sorted(set(value)))


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _invalid()
        result[key] = value
    return result


def _constant(_):
    raise _invalid()


def _float(value):
    number = float(value)
    if not math.isfinite(number):
        raise _invalid()
    return number


def _close(resource):
    if resource is not None:
        try:
            resource.close()
        except Exception:
            pass


def _count_header(headers, name, *, optional=False):
    value = headers.get(name)
    if optional and value is None:
        return None
    if (type(value) is not str or not re.fullmatch(r'[0-9]{1,7}', value)
            or int(value) > MAX_HEADER_COUNT):
        raise _invalid()
    return int(value)


def _rowset_digest(rows):
    digest = hashlib.sha256()
    encoded = sorted(json.dumps(row, sort_keys=True, separators=(',', ':'),
        ensure_ascii=True, allow_nan=False).encode() for row in rows)
    for value in encoded:
        digest.update(len(value).to_bytes(8, 'big'))
        digest.update(value)
    return digest.hexdigest()


def _active_provider() -> str:
    for candidate in ('enrichment', 'openbb', 'finra'):
        if current_provider_deadline(candidate) is not None:
            return candidate
    return 'enrichment'


def normalize_short_interest(ticker, items, *, acquired_at=None, as_of=None):
    """Shared FINRA short-interest row validation; preserve unknown float percentage."""
    acquired_at = acquired_at or datetime.fromisoformat(acquisition_time())
    context = validate_current_settlement(None, acquired_at=acquired_at, as_of=as_of)
    dated = []
    for item in items or []:
        symbol = getattr(item, 'symbol', None)
        if symbol is not None and symbol != ticker:
            raise SourceFetchError('Mismatched FINRA symbol', reason_code='invalid_response')
        try:
            settlement = date.fromisoformat(str(getattr(item, 'settlement_date', '')))
        except (TypeError, ValueError):
            raise SourceFetchError('Invalid FINRA settlement date', reason_code='invalid_response') from None
        validate_current_settlement(settlement, acquired_at=acquired_at, as_of=as_of)
        dated.append((settlement, item))
    required_str = context['required_settlement_date']
    cutoff = date.fromisoformat(required_str) if required_str is not None else date.fromisoformat(context['effective_as_of'])
    dated = [(settlement, item) for settlement, item in dated if settlement <= cutoff]
    freshness = validate_current_settlement(max((day for day, _ in dated), default=None),
        acquired_at=acquired_at, as_of=as_of)
    if freshness['status'] != 'current':
        return {'error': f'Current published FINRA short interest unavailable for {ticker}',
                'reason_code': 'provider_error', 'freshness': freshness}
    latest = max(settlement for settlement, _ in dated)
    normalized = []
    for settlement, item in dated:
        if settlement != latest:
            continue
        short_pos = getattr(item, 'current_short_position', None)
        coverage = getattr(item, 'days_to_cover', None)
        avg_vol = getattr(item, 'avg_daily_volume', getattr(item, 'average_daily_volume', None))
        try:
            if isinstance(short_pos, bool) or not math.isfinite(float(short_pos)) or float(short_pos) < 0:
                raise ValueError
            if coverage is None:
                if isinstance(avg_vol, bool) or not math.isfinite(float(avg_vol)) or float(avg_vol) <= 0:
                    raise ValueError
                coverage = float(short_pos) / float(avg_vol)
            if isinstance(coverage, bool) or not math.isfinite(float(coverage)) or float(coverage) < 0:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            raise SourceFetchError('Invalid FINRA coverage data', reason_code='invalid_response') from None
        short_pct = getattr(item, 'short_percent_of_float', None)
        if short_pct is not None:
            try:
                if isinstance(short_pct, bool) or not math.isfinite(float(short_pct)) or float(short_pct) < 0:
                    raise ValueError
                short_pct = float(short_pct)
            except (TypeError, ValueError, OverflowError):
                raise SourceFetchError('Invalid FINRA float percentage', reason_code='invalid_response') from None
        normalized.append({
            'short_interest': float(short_pos),
            'short_pct_of_float': short_pct,
            'days_to_cover': float(coverage),
            'date': latest.isoformat(),
        })
    result = normalized[0]
    if any(row != result for row in normalized[1:]):
        raise SourceFetchError('Conflicting latest FINRA rows', reason_code='invalid_response')
    return result


def acquire(symbols, attempt, *, acquired_at, as_of=None):
    """Return (values, errors); global failures raise and withdraw all positives.

    A single physical request per batch uses the caller's existing provider
    deadline. Only the caller retries a whole acquisition, with a new receipt.
    """
    provider = _active_provider()
    attempt['query_started_at'] = provider_clock_time(provider)
    raw_rows = []
    session = None
    try:
        if (type(symbols) is not list or not 1 <= len(symbols) <= MAX_SYMBOLS
                or any(type(symbol) is not str or not _SYMBOL.fullmatch(symbol) for symbol in symbols)
                or len(set(symbols)) != len(symbols)):
            raise _invalid()
        symbols = sorted(symbols)
        attempt['query_population_sha256'] = population_digest(symbols)
        if not isinstance(acquired_at, datetime) or acquired_at.utcoffset() is None:
            raise _invalid()
        attempt['acquired_at'] = acquired_at.isoformat()
        context = validate_current_settlement(None, acquired_at=acquired_at, as_of=as_of)
        context.pop('status')
        dynamic_mode = context['publication_policy'] == DYNAMIC_PUBLICATION_POLICY
        target_settlement = context['required_settlement_date']
        effective_day = date.fromisoformat(context['effective_as_of'])
        window_start = (effective_day - timedelta(days=MAX_SETTLEMENT_AGE_DAYS)).isoformat()
        coverage = {**context, 'scope': SCOPE, 'requested_symbols': list(symbols),
            'accepted_symbols': [], 'missing_symbols': list(symbols), 'invalid_symbols': [],
            'current_complete': False}
        attempt['query_coverage'] = coverage
        provider_timeout(provider)
        session = requests.Session()
        session.trust_env = False
        values, errors = {}, {}
        for start in range(0, len(symbols), BATCH_SIZE):
            batch_symbols = symbols[start:start + BATCH_SIZE]
            batch = {'requested_symbols': batch_symbols, 'http_status': None,
                'response_bytes': 0, 'response_sha256': None, 'row_count': 0,
                'record_total': None, 'record_offset': None, 'record_limit': None,
                'total_records_on_page': None}
            attempt['batches'].append(batch)
            response = None
            try:
                timeout = provider_timeout(provider)
                deadline = current_provider_deadline(provider)
                deadline = deadline if deadline is not None else provider_clock_time(provider) + timeout
                if target_settlement is not None:
                    compare_filter = {'fieldName': 'settlementDate',
                        'fieldValue': target_settlement, 'compareType': 'EQUAL'}
                    request_limit = len(batch_symbols) + 1
                else:
                    compare_filter = {'fieldName': 'settlementDate',
                        'fieldValue': window_start, 'compareType': 'GTE'}
                    request_limit = len(batch_symbols) * 3 + 1
                # Scope retry policy explicitly even though this transport does
                # not invoke provider_request: physical requests never retry here.
                with provider_subbudget(provider, maximum_seconds=timeout,
                        absolute_deadline=deadline, max_attempts=1):
                    response = session.post(ENDPOINT, json={'limit': request_limit,
                        'compareFilters': [compare_filter],
                        'domainFilters': [{'fieldName': 'symbolCode', 'values': batch_symbols}]},
                        headers={'Accept': 'application/json', 'User-Agent': 'EventEdge-FINRA-query/1'},
                        timeout=(timeout / 2, timeout / 2), allow_redirects=False, stream=True)
                    provider_timeout(provider)
                    status = response.status_code
                    if type(status) is not int or not 100 <= status <= 599:
                        raise _invalid()
                    batch['http_status'] = status
                    if status != 200:
                        raise SourceFetchError('FINRA query HTTP request failed',
                            reason_code='http_error', http_status=status)
                    headers = {key.lower(): value for key, value in response.headers.items()}
                    if headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/json':
                        raise _invalid()
                    for key in ('record_total', 'record_offset', 'record_limit', 'total_records_on_page'):
                        batch[key] = _count_header(headers, key.replace('_', '-'), optional=key == 'total_records_on_page')
                    if batch['record_offset'] != 0 or batch['record_limit'] != request_limit:
                        raise _invalid()
                    body = read_bounded_response(response, provider=provider, max_bytes=MAX_BYTES)
                    batch.update(response_bytes=len(body), response_sha256=hashlib.sha256(body).hexdigest())
                    try:
                        rows = json.loads(body, object_pairs_hook=_pairs, parse_constant=_constant, parse_float=_float)
                    except (ValueError, UnicodeError, RecursionError):
                        raise _invalid() from None
                    if type(rows) is not list or len(rows) > request_limit:
                        raise _invalid()
                    batch['row_count'] = len(rows)
                    raw_rows.extend(rows)
                    if (batch['record_total'] != len(rows)
                            or batch['total_records_on_page'] is not None and batch['total_records_on_page'] != len(rows)):
                        raise _invalid()
                    seen_pairs = set()
                    for row in rows:
                        provider_timeout(provider)
                        if (type(row) is not dict or type(row.get('symbolCode')) is not str
                                or row['symbolCode'] not in batch_symbols
                                or type(row.get('settlementDate')) is not str
                                or type(row.get('accountingYearMonthNumber')) is not int):
                            raise _invalid()
                        row_date = row['settlementDate']
                        pair = (row['symbolCode'], row_date)
                        if pair in seen_pairs:
                            raise _invalid()
                        seen_pairs.add(pair)
                        if target_settlement is not None and not dynamic_mode:
                            if row_date != target_settlement or row['accountingYearMonthNumber'] != int(target_settlement.replace('-', '')):
                                raise _invalid()
                        else:
                            try:
                                parsed_row_date = date.fromisoformat(row_date)
                            except ValueError:
                                raise _invalid() from None
                            if row['accountingYearMonthNumber'] != int(row_date.replace('-', '')):
                                raise _invalid()
                            if parsed_row_date > effective_day:
                                raise _invalid()
                    if target_settlement is None:
                        eligible_dates = sorted({
                            r['settlementDate'] for r in rows
                            if r['settlementDate'] <= effective_day.isoformat()
                        })
                        if eligible_dates:
                            candidate = eligible_dates[-1]
                            if validate_current_settlement(candidate, acquired_at=acquired_at, as_of=as_of)['status'] != 'current':
                                raise _invalid()
                            target_settlement = candidate
                            coverage['required_settlement_date'] = candidate
                    cycle_rows = (
                        [r for r in rows if r['settlementDate'] == target_settlement]
                        if target_settlement is not None else []
                    )
                    if len(cycle_rows) > len(batch_symbols):
                        raise _invalid()
                    seen = set()
                    for row in cycle_rows:
                        symbol = row['symbolCode']
                        seen.add(symbol)
                        position, volume, days = (row.get(key) for key in (
                            'currentShortPositionQuantity', 'averageDailyVolumeQuantity', 'daysToCoverQuantity'))
                        if (not source_number(position, minimum=0)
                                or not source_number(volume, minimum=0)
                                or days is not None and not source_number(days, minimum=0)
                                or days is None and volume <= 0):
                            errors[symbol] = {'error': 'Invalid FINRA short interest numbers', 'reason_code': 'invalid_response'}
                            continue
                        try:
                            normalized = normalize_short_interest(symbol, [SimpleNamespace(symbol=symbol,
                                settlement_date=row['settlementDate'], current_short_position=position,
                                avg_daily_volume=volume, days_to_cover=days)], acquired_at=acquired_at, as_of=as_of)
                        except SourceFetchError as error:
                            if error.reason_code != 'invalid_response':
                                raise
                            errors[symbol] = {'error': 'Invalid FINRA short interest numbers', 'reason_code': 'invalid_response'}
                            continue
                        if 'error' in normalized:
                            raise _invalid()
                        values[symbol] = normalized
                    for symbol in sorted(set(batch_symbols) - seen):
                        errors[symbol] = {'error': 'Current published FINRA short interest unavailable',
                            'reason_code': 'provider_error'}
                    provider_timeout(provider)
            finally:
                _close(response)
        provider_timeout(provider)
        try:
            attempt['rowset_sha256'] = _rowset_digest(raw_rows)
        except (RecursionError, ValueError, TypeError, OverflowError):
            raise _invalid() from None
        provider_timeout(provider)
        coverage.update(accepted_symbols=sorted(values),
            invalid_symbols=sorted(symbol for symbol, error in errors.items() if error['reason_code'] == 'invalid_response'),
            missing_symbols=sorted(symbol for symbol, error in errors.items() if error['reason_code'] == 'provider_error'),
            current_complete=not errors)
        attempt.update(status='success' if not errors else 'partial' if values else 'error',
            reason_code=None if not errors else 'provider_error')
        provider_timeout(provider)
        return values, errors
    except Exception as error:
        raise fail_attempt(attempt, error) from None
    finally:
        attempt['query_finished_at'] = provider_clock_time(provider)
        attempt['query_elapsed_seconds'] = attempt['query_finished_at'] - attempt['query_started_at']
        attempt['row_count'] = sum(batch['row_count'] for batch in attempt['batches'])
        _close(session)


def validated_acquisition(value):
    """Validate schema four without reinterpreting historical archive receipts."""
    try:
        _validate(value)
        return json.loads(json.dumps(value, allow_nan=False))
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, SourceFetchError):
        raise ValueError('Invalid FINRA acquisition metadata') from None


def _validate(value):
    def require(condition):
        if not condition:
            raise ValueError
    def digest(item, nullable=False):
        require(nullable and item is None or type(item) is str and _DIGEST.fullmatch(item))
    def timestamp(item):
        require(type(item) is str)
        clock = datetime.fromisoformat(item)
        require(clock.utcoffset() is not None and clock.isoformat() == item)
        return clock
    require(type(value) is dict and set(value) == {'schema_version', 'requested_count', 'cached_count',
        'population_sha256', 'attempts'})
    require(type(value['schema_version']) is int and value['schema_version'] == 4)
    for key in ('requested_count', 'cached_count'):
        require(type(value[key]) is int and 0 <= value[key] <= MAX_SYMBOLS)
    require(value['cached_count'] <= value['requested_count'])
    digest(value['population_sha256'])
    require(type(value['attempts']) is list and len(value['attempts']) <= 2)
    if value['requested_count'] == 0:
        require(value['population_sha256'] == population_digest([]) and not value['attempts'])
    prior_symbols = None
    for attempt in value['attempts']:
        require(type(attempt) is dict and set(attempt) == set(new_attempt()))
        require(type(attempt['status']) is str and attempt['status'] in {'success', 'partial', 'error'})
        require(attempt['reason_code'] is None or type(attempt['reason_code']) is str and attempt['reason_code'] in REASONS)
        require((attempt['status'] == 'success') == (attempt['reason_code'] is None))
        require(attempt['source'] == SOURCE and attempt['endpoint'] == ENDPOINT)
        acquired = timestamp(attempt['acquired_at']) if attempt['acquired_at'] is not None else None
        times = [attempt[key] for key in ('query_started_at', 'query_finished_at', 'query_elapsed_seconds')]
        require(all(item is None for item in times) or all(type(item) in (int, float)
            and math.isfinite(item) and item >= 0 for item in times))
        if times[0] is not None:
            require(times[1] >= times[0] and math.isclose(times[1] - times[0], times[2], abs_tol=1e-9))
        digest(attempt['query_population_sha256'], nullable=True)
        digest(attempt['rowset_sha256'], nullable=True)
        require(type(attempt['row_count']) is int and 0 <= attempt['row_count'] <= (MAX_SYMBOLS + MAX_BATCHES) * 3)
        require(type(attempt['batches']) is list and len(attempt['batches']) <= MAX_BATCHES)
        coverage = attempt['query_coverage']
        requested = None
        dynamic_mode = False
        if coverage is not None:
            require(type(coverage) is dict and acquired is not None)
            expected = validate_current_settlement(None, acquired_at=acquired,
                as_of=datetime.fromisoformat(coverage['effective_as_of']).date())
            expected.pop('status')
            dynamic_mode = expected['publication_policy'] == DYNAMIC_PUBLICATION_POLICY
            require(set(coverage) == set(expected) | {'scope', 'requested_symbols', 'accepted_symbols',
                'missing_symbols', 'invalid_symbols', 'current_complete'})
            for key, item in expected.items():
                if dynamic_mode and key == 'required_settlement_date':
                    req_val = coverage[key]
                    require(req_val is None or (type(req_val) is str and validate_current_settlement(
                        req_val, acquired_at=acquired,
                        as_of=datetime.fromisoformat(coverage['effective_as_of']).date())['status'] == 'current'))
                else:
                    require(type(coverage[key]) is type(item) and coverage[key] == item)
            require(coverage['scope'] == SCOPE)
            for key in ('requested_symbols', 'accepted_symbols', 'missing_symbols', 'invalid_symbols'):
                require(_symbols(coverage[key]))
            requested = coverage['requested_symbols']
            require(bool(requested) and len(requested) == value['requested_count'] - value['cached_count'])
            require(prior_symbols is None or requested == prior_symbols)
            prior_symbols = requested
            digest(attempt['query_population_sha256'])
            require(attempt['query_population_sha256'] == population_digest(requested))
            if not value['cached_count']:
                require(value['population_sha256'] == attempt['query_population_sha256'])
            partition = coverage['accepted_symbols'] + coverage['missing_symbols'] + coverage['invalid_symbols']
            require(len(partition) == len(set(partition)) and sorted(partition) == requested)
            require(type(coverage['current_complete']) is bool
                and coverage['current_complete'] == (coverage['accepted_symbols'] == requested))
            if attempt['status'] == 'success':
                require(coverage['current_complete'])
            elif attempt['status'] == 'partial':
                require(bool(coverage['accepted_symbols']) and not coverage['current_complete']
                    and attempt['reason_code'] == 'provider_error')
            else:
                require(not coverage['accepted_symbols'] and not coverage['current_complete'])
        else:
            require(attempt['status'] == 'error' and not attempt['batches'] and attempt['row_count'] == 0)
        queried = []
        for ordinal, batch in enumerate(attempt['batches']):
            require(type(batch) is dict and set(batch) == {'requested_symbols', 'http_status',
                'response_bytes', 'response_sha256', 'row_count', 'record_total', 'record_offset',
                'record_limit', 'total_records_on_page'})
            require(_symbols(batch['requested_symbols'], BATCH_SIZE) and bool(batch['requested_symbols']))
            require(requested is not None and batch['requested_symbols'] == requested[ordinal * BATCH_SIZE:(ordinal + 1) * BATCH_SIZE])
            queried.extend(batch['requested_symbols'])
            require(batch['http_status'] is None or type(batch['http_status']) is int and 100 <= batch['http_status'] <= 599)
            require(type(batch['response_bytes']) is int and 0 <= batch['response_bytes'] <= MAX_BYTES)
            digest(batch['response_sha256'], nullable=True)
            max_batch_rows = len(batch['requested_symbols']) * (3 if dynamic_mode else 1)
            require(type(batch['row_count']) is int and 0 <= batch['row_count'] <= max_batch_rows + 1)
            for key in ('record_total', 'record_offset', 'record_limit', 'total_records_on_page'):
                require(batch[key] is None or type(batch[key]) is int and 0 <= batch[key] <= MAX_HEADER_COUNT)
            if batch['response_sha256'] is None:
                require(batch['response_bytes'] == 0 and batch['row_count'] == 0)
            else:
                require(batch['http_status'] == 200)
            if attempt['status'] in {'success', 'partial'}:
                valid_limits = ({len(batch['requested_symbols']) + 1, len(batch['requested_symbols']) * 3 + 1}
                                if dynamic_mode else {len(batch['requested_symbols']) + 1})
                require(batch['http_status'] == 200 and batch['response_sha256'] is not None
                    and batch['row_count'] <= max_batch_rows)
                require(type(batch['record_total']) is int and batch['record_total'] == batch['row_count']
                    and type(batch['record_offset']) is int and batch['record_offset'] == 0
                    and type(batch['record_limit']) is int and batch['record_limit'] in valid_limits
                    and (batch['total_records_on_page'] is None or batch['total_records_on_page'] == batch['row_count']))
                present = set(coverage['accepted_symbols']) | set(coverage['invalid_symbols'])
                expected_present = len(present & set(batch['requested_symbols']))
                require(batch['row_count'] >= expected_present if dynamic_mode else batch['row_count'] == expected_present)
        require(attempt['row_count'] == sum(batch['row_count'] for batch in attempt['batches']))
        if attempt['status'] in {'success', 'partial'}:
            require(queried == requested and acquired is not None and times[0] is not None
                and attempt['rowset_sha256'] is not None)
            total_present = len(coverage['accepted_symbols']) + len(coverage['invalid_symbols'])
            require(attempt['row_count'] >= total_present if dynamic_mode else attempt['row_count'] == total_present)
