"""Prospective exchange-listed SIP scope, independent of successful bar retrieval.

A complete Alpaca US-equity asset master defines supported current securities.
All observations, including inactive history and duplicate symbols, are retained.
SEC CIK membership is conservative: unresolved roles never justify skipping text.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
import time
import requests
import re
from typing import Any

POLICY = 'us_exchange_listed_sip_v1'
EXCHANGES = frozenset({'AMEX', 'ARCA', 'BATS', 'NASDAQ', 'NYSE', 'NYSEARCA'})
ENDPOINT = 'https://paper-api.alpaca.markets/v2/assets'
_SYMBOL = re.compile(r'[A-Z0-9][A-Z0-9._-]{0,31}\Z')
_ASSET_SYMBOL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,31}\Z')
_SHA = re.compile(r'[a-f0-9]{64}\Z')


def _digest(rows: list) -> str:
    return hashlib.sha256(json.dumps(rows, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


def normalize_assets(rows: Any, *, observed_at: datetime, response_sha256: str) -> dict:
    if (not isinstance(rows, list) or not 1 <= len(rows) <= 100_000
            or not isinstance(observed_at, datetime) or observed_at.tzinfo is None
            or observed_at.utcoffset() is None or not isinstance(response_sha256, str)
            or _SHA.fullmatch(response_sha256) is None):
        raise ValueError('invalid complete equity asset master')
    normalized = []
    for row in rows:
        if (not isinstance(row, dict) or row.get('class') != 'us_equity'
                or not isinstance(row.get('symbol'), str) or _ASSET_SYMBOL.fullmatch(row['symbol']) is None
                or not isinstance(row.get('exchange'), str) or not row['exchange']
                or len(row['exchange']) > 32 or row.get('status') not in ('active', 'inactive')
                or type(row.get('tradable')) is not bool):
            raise ValueError('invalid equity asset observation')
        normalized.append([row['symbol'], row['exchange'], row['status'], row['tradable']])
    normalized.sort()
    return {'policy': POLICY, 'endpoint': ENDPOINT, 'asset_class': 'us_equity',
            'observed_at': observed_at.isoformat(), 'response_sha256': response_sha256,
            'assets_sha256': _digest(normalized), 'assets': normalized,
            'coverage': {'complete': True, 'mode': 'complete_asset_master', 'returned': len(rows)}}


class EquityUniverse:
    def __init__(self, evidence: dict, company_map: dict | None = None):
        if (not isinstance(evidence, dict) or evidence.get('policy') != POLICY
                or evidence.get('endpoint') != ENDPOINT or evidence.get('asset_class') != 'us_equity'
                or evidence.get('coverage', {}).get('complete') is not True):
            raise ValueError('universe evidence unavailable')
        assets = evidence.get('assets')
        if not isinstance(assets, list) or any(not isinstance(row, list) or len(row) != 4 for row in assets):
            raise ValueError('universe asset evidence invalid')
        try:
            canonical = normalize_assets([dict(zip(('symbol', 'exchange', 'status', 'tradable'), row),
                                               **{'class': 'us_equity'}) for row in assets],
                observed_at=datetime.fromisoformat(evidence['observed_at']),
                response_sha256=evidence['response_sha256'])
        except (ValueError, TypeError, KeyError):
            raise ValueError('universe evidence invalid') from None
        if canonical != evidence:
            raise ValueError('universe evidence binding mismatch')
        self.evidence = evidence
        self._company_symbols = self.company_symbols(company_map) if company_map is not None else None
        self._assets = defaultdict(list)
        for symbol, exchange, status, tradable in assets:
            self._assets[symbol].append((exchange, status, tradable))

    def decision(self, symbol: str) -> str:
        if symbol == '':
            return 'unresolved_issuer'
        if not isinstance(symbol, str) or _SYMBOL.fullmatch(symbol) is None:
            return 'invalid_symbol'
        observations = self._assets.get(symbol)
        if not observations:
            # Recognize uncertainty without binding a different security. Apply
            # this to every candidate path, not only CIK-based filing discovery.
            alternatives = {symbol.replace('-', '.'), symbol.replace('.', '-')}
            if any(alternative != symbol and any(
                    row[1] == 'active' and row[0] in EXCHANGES
                    for row in self._assets.get(alternative, ()))
                    for alternative in alternatives):
                return 'unresolved_symbol_alias'
            return 'absent_from_asset_master'
        active = [row for row in observations if row[1] == 'active']
        if not active:
            return 'inactive_asset'
        # Do not choose the first of multiple active identities, even if their
        # public fields agree. A future identity resolver must prove which one.
        if len(active) != 1:
            return 'ambiguous_active_asset'
        if active[0][0] not in EXCHANGES:
            return 'outside_sip_exchange_universe'
        return 'eligible'

    @staticmethod
    def company_symbols(company_map: dict) -> dict[str, set[str]]:
        by_cik = defaultdict(set)
        if not isinstance(company_map, dict) or not company_map:
            raise ValueError('company ticker evidence unavailable')
        for row in company_map.values():
            if (not isinstance(row, dict) or type(row.get('cik_str')) is not int
                    or not 0 < row['cik_str'] < 10**10 or not isinstance(row.get('ticker'), str)
                    or _SYMBOL.fullmatch(row['ticker']) is None):
                raise ValueError('company ticker evidence invalid')
            by_cik[str(row['cik_str'])].add(row['ticker'])
        return dict(by_cik)

    def filing_decision(self, ciks: list[str], company_map: dict | None = None) -> dict:
        """Conservative pre-hydration membership; not subject-issuer attribution."""
        by_cik = self.company_symbols(company_map) if company_map is not None else self._company_symbols
        if by_cik is None:
            return {'status': 'unresolved', 'reason': 'missing_company_map', 'symbols': {}}
        reasons = {}
        if not isinstance(ciks, list) or not ciks:
            return {'status': 'unresolved', 'reason': 'unresolved_issuer', 'symbols': {}}
        unknown = False
        for cik in ciks:
            if not isinstance(cik, str) or not re.fullmatch(r'[0-9]{1,10}', cik) or int(cik) <= 0:
                unknown = True
                continue
            symbols = by_cik.get(str(int(cik)))
            if not symbols:
                unknown = True
                continue
            for symbol in symbols:
                reason = self.decision(symbol)
                reasons[symbol] = reason
        if 'eligible' in reasons.values():
            status, reason = 'eligible', 'possible_eligible_issuer'
        elif unknown or any(value in ('invalid_symbol', 'ambiguous_active_asset', 'unresolved_symbol_alias') for value in reasons.values()):
            status, reason = 'unresolved', 'unresolved_issuer'
        else:
            status, reason = 'excluded', 'outside_sip_exchange_universe'
        return {'status': status, 'reason': reason, 'symbols': dict(sorted(reasons.items()))}


def fetch_equity_universe() -> dict:
    """Acquire one bounded, complete read-only master for the frozen source set."""
    from .fetch_errors import SourceFetchError
    from .request_policy import (current_provider_deadline, provider_budget,
        provider_request, provider_timeout, read_bounded_response)
    if current_provider_deadline('alpaca') is None:
        with provider_budget('alpaca', time.monotonic() + 300):
            return fetch_equity_universe()
    key = os.environ.get('ALPACA_API_KEY', '').strip()
    secret = os.environ.get('ALPACA_SECRET_KEY', '').strip()
    if not key or not secret:
        raise SourceFetchError('Equity universe credentials unavailable', reason_code='provider_error')
    response = None
    def unique_fields(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate asset response field')
            result[key] = value
        return result
    try:
        response = provider_request('alpaca', 'GET', ENDPOINT, operation='equity_asset_master',
            transport=requests.get, params={'asset_class': 'us_equity'},
            headers={'APCA-API-KEY-ID': key, 'APCA-API-SECRET-KEY': secret},
            timeout=(5.0, 20.0), stream=True, allow_redirects=False)
        if response.status_code != 200:
            raise ValueError('unexpected asset response')
        raw = read_bounded_response(response, provider='alpaca', max_bytes=30 * 1024**2)
        rows = json.loads(raw, object_pairs_hook=unique_fields)
        snapshot = normalize_assets(rows, observed_at=datetime.now(timezone.utc),
                                    response_sha256=hashlib.sha256(raw).hexdigest())
        provider_timeout('alpaca')
        return {'snapshot': snapshot, 'coverage': dict(snapshot['coverage'])}
    except (ValueError, TypeError, UnicodeError):
        raise SourceFetchError('Equity asset master invalid', reason_code='invalid_response') from None
    finally:
        if response is not None:
            response.close()
