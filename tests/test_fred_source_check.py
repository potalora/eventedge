"""Manual recovery observations never become trading or incident authority."""
from datetime import datetime, timezone

import pandas as pd

from scripts.check_fred_source import check_fred
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError


class Source:
    def __init__(self):
        self.calls = []

    def is_available(self):
        return True

    def fetch_series(self, sid, start, end, *, as_of):
        self.calls.append((sid, start, end, as_of))
        data = pd.Series([4.5], index=pd.to_datetime(['2026-09-01']))
        data.attrs['acquired_at'] = '2026-10-22T08:00:00+00:00'
        data.attrs['vintage_date'] = as_of
        return data


def test_check_uses_all_nine_real_series_and_one_historical_vintage():
    source = Source()
    result = check_fred('2026-10-21', source=source)
    assert result['ok'] and result['scope'] == 'manual_source_observation'
    assert result['incident_recovery_authority'] is False
    assert len(source.calls) == len(result['series']) == 9
    assert all(args[1:] == ('2025-04-21', '2026-10-21', '2026-10-21') for args in source.calls)
    assert all(row['latest_observation'] == '2026-09-01' for row in result['series'].values())
    assert datetime.fromisoformat(result['finished_at']).tzinfo == timezone.utc


def test_empty_data_and_maintenance_do_not_certify_recovery_or_leak_exception_text():
    class Incomplete(Source):
        def fetch_series(self, sid, *args, **kwargs):
            if sid == 'UNRATE':
                raise SourceFetchError('SECRET_KEY raw provider text', reason_code='provider_maintenance',
                                       http_status=200, attempts=3)
            return pd.Series(dtype=float)
    result = check_fred('2026-10-21', source=Incomplete())
    assert not result['ok']
    assert result['series']['UNRATE'] == {'status': 'failed', 'reason_code': 'provider_maintenance',
                                         'http_status': 200, 'attempts': 3}
    assert result['series']['PAYEMS']['reason_code'] == 'invalid_response'
    assert 'SECRET_KEY' not in str(result)


def test_missing_key_makes_no_requests():
    source = Source()
    source.is_available = lambda: False
    result = check_fred('2026-10-21', source=source)
    assert not result['ok'] and result['reason_code'] == 'credential_unavailable'
    assert not source.calls


def test_shared_deadline_prevents_later_series_dispatch():
    clock = [0.0]
    class Slow(Source):
        def fetch_series(self, *args, **kwargs):
            data = super().fetch_series(*args, **kwargs)
            clock[0] = 61.0
            return data
    source = Slow()
    result = check_fred('2026-10-21', source=source, clock=lambda: clock[0])
    assert not result['ok'] and len(source.calls) == 1
    assert all(row['reason_code'] == 'timeout' for row in result['series'].values())
