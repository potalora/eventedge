#!/usr/bin/env python3
"""Bounded manual FRED observation; no model, trading, scheduler or state writes."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import json
import math
from pathlib import Path
import sys
import time

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError, source_fetch_error
from tradingagents.strategies.data_sources.fred_source import FREDSource
from tradingagents.strategies.data_sources.request_policy import provider_budget
from tradingagents.strategies.orchestration.research_resilience import FRED_GROUPS


def check_fred(as_of: str, *, budget_seconds=60, source=None, clock=time.monotonic):
    """Use a fresh adapter and one deadline; retain only fixed safe diagnostics.

    Success means nonempty observations passed the native adapter contract for
    the requested vintage. Period ages are reported, not interpreted as release
    timeliness. Only ordinary accepted session evidence can close incidents.
    """
    if (date.fromisoformat(as_of).isoformat() != as_of or
            type(budget_seconds) not in (int, float) or not math.isfinite(budget_seconds)
            or not 1 <= budget_seconds <= 300):
        raise ValueError('invalid source check arguments')
    start = (pd.Timestamp(as_of) - pd.DateOffset(months=18)).date().isoformat()
    result = {'scope': 'manual_source_observation', 'ok': False,
              'incident_recovery_authority': False, 'vintage_date': as_of,
              'observation_start': start, 'budget_seconds': budget_seconds,
              'started_at': datetime.now(timezone.utc).isoformat(), 'series': {}}
    source = source if source is not None else FREDSource()
    if not source.is_available():
        result['reason_code'] = 'credential_unavailable'
    else:
        deadline = clock() + budget_seconds
        with provider_budget('fred', deadline, clock=clock) as diagnostics:
            for sid in dict.fromkeys(sid for group in FRED_GROUPS.values() for sid in group):
                try:
                    if clock() >= deadline:
                        raise SourceFetchError('Check deadline exhausted', reason_code='timeout')
                    data = source.fetch_series(sid, start, as_of, as_of=as_of)
                    if clock() >= deadline:
                        raise SourceFetchError('Check deadline exhausted', reason_code='timeout')
                    if not isinstance(data, pd.Series) or data.empty or data.dropna().empty:
                        raise SourceFetchError('Empty source observation', reason_code='invalid_response')
                    usable = data.dropna()
                    latest = usable.index.max().date()
                    result['series'][sid] = {'status': 'valid_observations',
                        'observation_count': len(data), 'usable_count': len(usable),
                        'latest_observation': latest.isoformat(),
                        'observation_age_days': (date.fromisoformat(as_of) - latest).days,
                        'acquired_at': data.attrs.get('acquired_at'),
                        'vintage_date': data.attrs.get('vintage_date')}
                except Exception as exc:
                    error = source_fetch_error('FRED check failed', exc)
                    result['series'][sid] = {'status': 'failed', 'reason_code': error.reason_code,
                                             'http_status': error.http_status, 'attempts': error.attempts}
            result['request_diagnostics'] = list(diagnostics)
        result['ok'] = all(row['status'] == 'valid_observations' for row in result['series'].values())
    result['finished_at'] = datetime.now(timezone.utc).isoformat()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--as-of', required=True, help='Exact observation/vintage date (YYYY-MM-DD).')
    parser.add_argument('--budget-seconds', type=float, default=60, help='Shared deadline, 1–300 seconds.')
    args = parser.parse_args()
    try:
        result = check_fred(args.as_of, budget_seconds=args.budget_seconds)
    except ValueError:
        parser.error('invalid date or budget; use YYYY-MM-DD and 1–300 seconds')
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
