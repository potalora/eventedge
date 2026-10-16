"""Opt-in new-entry eligibility from complete exact-session native SIP tape.

This module never creates a price or removes an accounting obligation. Raw
responses are retained for independent classification and immutable replay.
Rules: https://docs.alpaca.markets/us/docs/market-data-faq#how-are-bars-aggregated
Endpoint: https://data.alpaca.markets/v2/stocks/trades
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
import re
import time as clock
from zoneinfo import ZoneInfo

import requests

from tradingagents.strategies.data_sources.request_policy import (
    current_provider_deadline, provider_call, provider_clock_time, provider_request,
    provider_timeout, read_bounded_response,
)
from tradingagents.strategies.orchestration.trading_calendar import is_session

POLICY = 'reference_session_activity_v1'
SOURCE = 'alpaca-sip-session-trades-v1'
URL = 'https://data.alpaca.markets/v2/stocks/trades'
MAX_PAGES, MAX_ROWS, MAX_BYTES, MAX_SYMBOLS = 16, 100_000, 16 * 1024 * 1024, 100
MAX_SECONDS = 60
_ET = ZoneInfo('America/New_York')
_SYMBOL = re.compile(r'[A-Z][A-Z0-9.-]{0,15}\Z')
_STAMP = re.compile(r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?Z\Z')
# All documented daily-bar conditions for these tapes. Unknowns fail closed.
_KNOWN = {'A': set(' BCEFHIKLMNOPQRTUVXZ45679'),
          'B': set(' BCEFHIKLMNOPQRTUVXZ45679'),
          'C': set('@ABCDFGHIKLMNOPQRTUVWXYZ45679')}
_NO_PRICE = {'A': set('BCHIMNQRTUV7'), 'B': set('BCHIMNQRTUV7'),
             'C': set('CHIMNQRTUVW7')}


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _decode(raw):
    def invalid(_):
        raise ValueError('nonfinite JSON value')
    return json.loads(raw, object_pairs_hook=_unique, parse_float=Decimal,
                      parse_constant=invalid)


def _symbols(values):
    result = sorted(set(values))
    if not result or len(result) > MAX_SYMBOLS or any(
            not isinstance(s, str) or not _SYMBOL.fullmatch(s) for s in result):
        raise ValueError('invalid exact symbols')
    return result


def _bounds(session):
    if type(session) is not date or not is_session(session):
        raise ValueError('invalid reference session')
    start = datetime.combine(session, time.min, _ET).astimezone(timezone.utc)
    stop = datetime.combine(session + timedelta(days=1), time.min, _ET).astimezone(timezone.utc)
    # Provider end is inclusive; preserve the final 999 nanoseconds too.
    end = (stop - timedelta(seconds=1)).strftime('%Y-%m-%dT%H:%M:%S') + '.999999999Z'
    return start, stop, end


def _params(symbols, session):
    start, _, end = _bounds(session)
    return {'symbols': ','.join(symbols), 'feed': 'sip', 'asof': '-',
            'currency': 'USD', 'sort': 'asc', 'limit': 10000,
            'start': start.strftime('%Y-%m-%dT%H:%M:%SZ'), 'end': end}


def _timestamp(value):
    match = _STAMP.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError('invalid nanosecond timestamp')
    second = datetime.strptime(match[1], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc)
    return int(second.timestamp()) * 1_000_000_000 + int((match[2] or '').ljust(9, '0'))


def _audit(evidence, session, symbols):
    """Recompute all claims from exact request scope and hashed raw pages."""
    expected = _params(symbols, session)
    start, stop, _ = _bounds(session)
    if (not isinstance(evidence, dict) or evidence.get('source') != SOURCE
            or evidence.get('session') != session.isoformat()
            or evidence.get('symbols') != symbols or evidence.get('complete') is not True):
        raise ValueError('incomplete or mismatched evidence')
    observed = datetime.fromisoformat(evidence['observed_at'])
    if (observed.tzinfo is None or observed < stop + timedelta(minutes=15)
            or observed > datetime.now(timezone.utc)):
        raise ValueError('incomplete session vintage')
    pages = evidence['pages']
    if not isinstance(pages, list) or not 1 <= len(pages) <= MAX_PAGES:
        raise ValueError('invalid page count')
    token = None
    seen_tokens, last = set(), {}
    rows = {s: 0 for s in symbols}
    qualifying = {s: 0 for s in symbols}
    total_bytes = 0
    for index, page in enumerate(pages):
        params = {**expected, **({'page_token': token} if token is not None else {})}
        if (page['url'] != URL or page['params'] != params or page['status'] != 200
                or not isinstance(page['body'], str)):
            raise ValueError('invalid page provenance')
        raw = page['body'].encode('utf-8')
        total_bytes += len(raw)
        if total_bytes > MAX_BYTES or page['sha256'] != _hash(raw):
            raise ValueError('invalid raw page hash/size')
        data = _decode(raw)
        if (not isinstance(data, dict) or not isinstance(data.get('trades'), dict)
                or 'next_page_token' not in data or set(data['trades']) - set(symbols)):
            raise ValueError('invalid tape envelope')
        for symbol, trades in data['trades'].items():
            if not isinstance(trades, list):
                raise ValueError('invalid trades')
            for row in trades:
                if not isinstance(row, dict):
                    raise ValueError('invalid trade')
                stamp = _timestamp(row['t'])
                if not int(start.timestamp()) * 1_000_000_000 <= stamp < int(stop.timestamp()) * 1_000_000_000:
                    raise ValueError('trade outside exact session')
                if stamp < last.get(symbol, stamp):
                    raise ValueError('nonmonotonic tape')
                last[symbol] = stamp
                price = row['p']
                if isinstance(price, bool) or not isinstance(price, (int, Decimal)):
                    raise ValueError('invalid trade price')
                if not Decimal(price).is_finite() or price <= 0:
                    raise ValueError('invalid trade price')
                if (type(row['s']) is not int or row['s'] <= 0
                        or type(row['i']) is not int or row['i'] < 0
                        or not isinstance(row['x'], str) or not re.fullmatch(r'[A-Z]', row['x'])):
                    raise ValueError('invalid trade identity/size')
                tape, conditions = row['z'], row['c']
                if (not isinstance(tape, str) or tape not in _KNOWN
                        or not isinstance(conditions, list) or not conditions
                        or any(not isinstance(c, str) or c not in _KNOWN[tape] for c in conditions)):
                    raise ValueError('unknown trade condition/tape')
                rows[symbol] += 1
                # Do not deduplicate symbol/exchange/ID: no such uniqueness contract.
                qualifying[symbol] += not bool(set(conditions) & _NO_PRICE[tape])
                if sum(rows.values()) > MAX_ROWS:
                    raise ValueError('trade row limit')
        next_token = data['next_page_token']
        if next_token is None:
            if index != len(pages) - 1:
                raise ValueError('page after terminal')
        elif (index == len(pages) - 1 or not isinstance(next_token, str)
              or not next_token or len(next_token) > 1024 or next_token in seen_tokens):
            raise ValueError('incomplete or cyclic paging')
        else:
            seen_tokens.add(next_token)
        token = next_token
    if type(evidence.get('row_count')) is not int or evidence['row_count'] != sum(rows.values()):
        raise ValueError('row count mismatch')
    return qualifying


class AlpacaSessionActivitySource:
    def __init__(self, *, get=None, now=None):
        self._get = get or requests.get
        self._now = now or (lambda: datetime.now(timezone.utc))

    def fetch_session_activity(self, symbols, session, *, now=None, deadline=None):
        symbols = _symbols(symbols)
        params = _params(symbols, session)
        now = now or self._now()
        evidence = {'source': SOURCE, 'session': session.isoformat(), 'symbols': symbols,
                    'complete': False, 'observed_at': now.isoformat(), 'pages': [],
                    'row_count': 0, 'failure': None}
        _, stop, _ = _bounds(session)
        if now.tzinfo is None or now < stop + timedelta(minutes=15):
            evidence['failure'] = 'session_not_ready'
            return evidence
        key, secret = os.environ.get('ALPACA_API_KEY', '').strip(), os.environ.get('ALPACA_SECRET_KEY', '').strip()
        if not key or not secret:
            evidence['failure'] = 'missing_credentials'
            return evidence
        limits = [provider_clock_time('alpaca') + MAX_SECONDS]
        for bound in (deadline, current_provider_deadline('alpaca')):
            if bound is not None:
                limits.append(bound)
        absolute_deadline = min(limits)
        token, seen_tokens, total_bytes = None, set(), 0
        def acquire():
            nonlocal token, total_bytes
            try:
                for _ in range(MAX_PAGES):
                    response = None
                    request_params = {**params, **({'page_token': token} if token is not None else {})}
                    try:
                        response = provider_request('alpaca', 'GET', URL, operation='session_activity',
                            transport=self._get, params=request_params,
                            headers={'APCA-API-KEY-ID': key, 'APCA-API-SECRET-KEY': secret},
                            timeout=(5., 20.), allow_redirects=False, stream=True)
                        raw = read_bounded_response(response, provider='alpaca', max_bytes=MAX_BYTES-total_bytes)
                        total_bytes += len(raw)
                        evidence['pages'].append({'url': URL, 'params': request_params,
                            'status': response.status_code, 'body': raw.decode('utf-8'), 'sha256': _hash(raw)})
                        if response.status_code != 200:
                            raise ValueError('HTTP failure')
                        payload = _decode(raw)
                        trades, token = payload['trades'], payload['next_page_token']
                        if not isinstance(trades, dict) or any(not isinstance(v, list) for v in trades.values()):
                            raise ValueError('invalid tape envelope')
                        evidence['row_count'] += sum(len(v) for v in trades.values())
                        if evidence['row_count'] > MAX_ROWS:
                            raise ValueError('trade row limit')
                        provider_timeout('alpaca')
                        if token is None:
                            evidence['complete'] = True
                            break
                        if not isinstance(token, str) or not token or len(token) > 1024 or token in seen_tokens:
                            raise ValueError('invalid pagination')
                        seen_tokens.add(token)
                    finally:
                        if response is not None:
                            response.close()
                evidence['observed_at'] = self._now().isoformat()
                _audit(evidence, session, symbols)
                provider_timeout('alpaca')
            except Exception:
                evidence['complete'] = False
                evidence['failure'] = 'unresolved_tape_evidence'
        try:
            provider_call('alpaca', 'session_activity_acquisition', acquire,
                maximum_seconds=absolute_deadline-provider_clock_time('alpaca'))
        except Exception:
            evidence['complete'] = False
            evidence['failure'] = 'unresolved_tape_evidence'
        return evidence


def _daily_failure(evidence, attempts, ticker, session):
    """Only explicit absence or an otherwise valid native zero-activity row."""
    from tradingagents.strategies.execution.alpaca_daily_bar import SOURCE as BAR_SOURCE
    start, stop, _ = _bounds(session)
    if (not isinstance(evidence, dict) or evidence.get('source') != BAR_SOURCE
            or evidence.get('session') != session.isoformat() or evidence.get('complete') is not True):
        raise ValueError('daily evidence unavailable')
    symbols = evidence['symbols']
    if (not isinstance(symbols, list) or len(symbols) != len(set(symbols))
            or set(_symbols(symbols)) != set(symbols) or ticker not in symbols):
        raise ValueError('daily identity mismatch')
    observed = datetime.fromisoformat(evidence['observed_at'])
    if (observed.tzinfo is None or observed < stop + timedelta(minutes=15)
            or observed > datetime.now(timezone.utc)):
        raise ValueError('daily vintage incomplete')
    if not isinstance(attempts, (list, tuple)) or not attempts:
        raise ValueError('missing daily attempts')
    final = attempts[-1]
    fetched = final['fetched_at']
    if isinstance(fetched, str):
        fetched = datetime.fromisoformat(fetched)
    if (final['ticker'] != ticker or final['session'] not in (session, session.isoformat())
            or final['source'] != BAR_SOURCE or fetched != observed
            or not isinstance(final['validation_error'], str)
            or not final['validation_error'].startswith(('missing ', 'invalid ', 'invalid_response '))):
        raise ValueError('daily failed-attempt mismatch')
    pages = evidence['pages']
    if not isinstance(pages, list) or not 1 <= len(pages) <= 128:
        raise ValueError('daily page count invalid')
    token, seen, rows, total_bytes, row_count = None, set(), [], 0, 0
    for index, page in enumerate(pages):
        params = page['params']
        if (page['url'] != 'https://data.alpaca.markets/v2/stocks/bars' or page['status'] != 200
                or params.get('symbols') != ','.join(symbols) or params.get('feed') != 'sip'
                or params.get('asof') != '-' or params.get('currency') != 'USD'
                or params.get('adjustment') != 'raw' or params.get('timeframe') != '1Day'
                or params.get('sort') != 'asc' or params.get('limit') != 201
                or datetime.fromisoformat(params['start']) != start
                or datetime.fromisoformat(params['end']) != stop - timedelta(microseconds=1)
                or params.get('page_token') != token):
            raise ValueError('daily request mismatch')
        raw = page['body'].encode('utf-8')
        total_bytes += len(raw)
        if total_bytes > 8 * 1024 * 1024 or _hash(raw) != page['sha256']:
            raise ValueError('daily response hash/size mismatch')
        data = _decode(raw)
        if (not isinstance(data, dict) or not isinstance(data.get('bars'), dict)
                or set(data['bars']) - set(symbols) or 'next_page_token' not in data
                or any(not isinstance(v, list) for v in data['bars'].values())):
            raise ValueError('daily envelope invalid')
        row_count += sum(len(v) for v in data['bars'].values())
        if row_count > 10000:
            raise ValueError('daily row cap')
        rows.extend(data['bars'].get(ticker, []))
        token = data['next_page_token']
        if token is None:
            if index != len(pages)-1:
                raise ValueError('daily page after terminal')
        elif (index == len(pages)-1 or not isinstance(token, str) or not token
              or len(token) > 1024 or token in seen):
            raise ValueError('daily paging incomplete')
        else:
            seen.add(token)
    if not rows:
        return 'missing_data'
    if len(rows) != 1 or not isinstance(rows[0], dict):
        raise ValueError('daily row count invalid')
    row = rows[0]
    if _timestamp(row['t']) != int(start.timestamp()) * 1_000_000_000:
        raise ValueError('daily timestamp invalid')
    prices = [row[k] for k in ('o', 'h', 'l', 'c')]
    if any(isinstance(v, bool) or not isinstance(v, (int, Decimal))
           or not Decimal(v).is_finite() or v <= 0 for v in prices):
        raise ValueError('daily OHLC invalid')
    op, high, low, close = prices
    if not low <= op <= high or not low <= close <= high:
        raise ValueError('daily OHLC incoherent')
    if (isinstance(row['v'], bool) or not isinstance(row['v'], (int, Decimal))
            or row['v'] != 0 or type(row['n']) is not int or row['n'] != 0):
        raise ValueError('daily activity not explicit zero')
    return 'zero_activity'


def evaluate_new_entry_eligibility(evidence, session, candidate_tickers, protected_tickers,
                                   *, listing_snapshot=None, daily_attempts=None, daily_evidence=None):
    candidates = _symbols(candidate_tickers)
    protected = set(protected_tickers)
    requested = sorted(set(candidates) - protected)
    qualifying = None
    universe = None
    try:
        from tradingagents.strategies.data_sources.equity_universe import EquityUniverse
        universe = EquityUniverse(listing_snapshot)
        observed = datetime.fromisoformat(listing_snapshot['observed_at'])
        if observed > datetime.now(timezone.utc):
            universe = None
    except (ValueError, TypeError, KeyError, AttributeError):
        pass
    if requested:
        try:
            qualifying = _audit(evidence, session, requested)
        except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
            pass
    decisions = {}
    for ticker in candidates:
        daily_reason = None
        try:
            daily_reason = _daily_failure(daily_evidence[ticker], daily_attempts[ticker], ticker, session)
        except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
            pass
        if ticker in protected:
            status, reason = 'protected_obligation', 'governed_price_obligation'
        elif (qualifying is not None and qualifying[ticker] == 0 and daily_reason is not None
              and universe is not None and universe.decision(ticker) == 'eligible'):
            status, reason = 'excluded_from_new_entry', 'no_qualifying_reference_session_activity'
        else:
            status, reason = 'unresolved', 'required_reference_price_unresolved'
        decisions[ticker] = {'status': status, 'reason': reason,
                            'daily_failure_reason': daily_reason}
    return decisions


def _eligibility_store(owner, session):
    from tradingagents.strategies.orchestration.source_inputs import SourceInputStore, daily_source_store
    base, identity = daily_source_store(owner, session.isoformat())
    store = SourceInputStore(base.cache_dir / 'new_entry_eligibility',
        accepted_dir=base.accepted_dir / 'new_entry_eligibility', **base.codec_limits)
    return store, {**identity, 'policy': POLICY}


def _check_deadline(deadline):
    if deadline is not None and clock.monotonic() >= deadline:
        raise TimeoutError('eligibility acquisition deadline exceeded')


def _validate_frozen(frozen, session, candidate_scope, protected, listing_snapshot):
    if (not isinstance(frozen, dict) or frozen.get('policy') != POLICY
            or frozen.get('session') != session.isoformat()
            or frozen.get('protected_tickers') != protected
            or frozen.get('listing_snapshot') != listing_snapshot
            or not isinstance(frozen.get('candidate_tickers'), list)
            or not set(frozen['candidate_tickers']) <= set(candidate_scope)):
        raise ValueError('frozen eligibility scope mismatch')
    expected = evaluate_new_entry_eligibility(frozen.get('evidence'), session,
        frozen['candidate_tickers'], protected, listing_snapshot=listing_snapshot,
        daily_attempts=frozen.get('daily_attempts'), daily_evidence=frozen.get('daily_evidence'))
    if frozen.get('decisions') != expected:
        raise ValueError('frozen eligibility evidence mismatch')


def load_new_entry_eligibility(owner, session, candidate_scope, protected_tickers, *, listing_snapshot,
                               deadline=None):
    """Restore a prior failed subset before resolving any candidate prices."""
    if owner._base_config.get('autoresearch', {}).get('new_entry_eligibility_policy') != POLICY:
        return None
    _check_deadline(deadline)
    store, identity = _eligibility_store(owner, session)
    frozen = store.load_frozen(identity)
    if frozen is not None:
        _validate_frozen(frozen, session, candidate_scope, sorted(set(protected_tickers)), listing_snapshot)
    _check_deadline(deadline)
    return frozen


def resolve_new_entry_eligibility(owner, session, candidate_tickers, protected_tickers, *,
                                  listing_snapshot=None, daily_attempts=None, daily_evidence=None,
                                  source=None, deadline=None):
    """Freeze a separate generation/session/config-bound eligibility decision."""
    config = owner._base_config.get('autoresearch', {})
    if config.get('new_entry_eligibility_policy') != POLICY:
        raise ValueError('new-entry eligibility policy is not enabled')
    _check_deadline(deadline)
    candidates = _symbols(candidate_tickers)
    protected = sorted(set(protected_tickers))
    store, identity = _eligibility_store(owner, session)
    scope = {'policy': POLICY, 'session': session.isoformat(),
             'candidate_tickers': candidates, 'protected_tickers': protected,
             'listing_snapshot': listing_snapshot, 'daily_attempts': daily_attempts,
             'daily_evidence': daily_evidence}
    frozen = store.load_frozen(identity)
    _check_deadline(deadline)
    if frozen is None:
        requested = sorted(set(candidates) - set(protected))
        evidence = (source or AlpacaSessionActivitySource()).fetch_session_activity(
            requested, session, deadline=deadline) if requested else None
        _check_deadline(deadline)
        decisions = evaluate_new_entry_eligibility(evidence, session, candidates, protected,
            listing_snapshot=listing_snapshot, daily_attempts=daily_attempts, daily_evidence=daily_evidence)
        frozen = store.freeze(identity, {**scope, 'decisions': decisions, 'evidence': evidence}, deadline=deadline)
    if not isinstance(frozen, dict) or any(frozen.get(k) != v for k, v in scope.items()):
        raise ValueError('frozen eligibility scope mismatch')
    _validate_frozen(frozen, session, candidates, protected, listing_snapshot)
    _check_deadline(deadline)
    return frozen
