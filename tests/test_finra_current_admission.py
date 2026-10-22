"""Publication-aware normalizer and cache admission; no native SDK calls."""
from datetime import datetime
from types import SimpleNamespace as N


from tradingagents.strategies.data_sources import openbb_source as mod
from test_finra_bulk import native, raw


def row(day):
    return N(symbol='AAPL',settlement_date=day,current_short_position=400,
             days_to_cover=4,average_daily_volume=100)


def instant(day):
    return datetime.fromisoformat(day + 'T22:00:00+00:00')


def test_stale_symbol_is_explicitly_unavailable_even_with_valid_numbers():
    result = mod.normalize_short_interest('AAPL',[row('2026-09-15')], acquired_at=instant('2026-10-22'))
    assert result['reason_code'] == 'provider_error'
    assert result['freshness']['required_settlement_date'] == '2026-09-30'


def test_unpublished_future_tail_does_not_replace_current_cycle():
    result = mod.normalize_short_interest('AAPL',[row('2026-09-30'),row('2026-10-15')], acquired_at=instant('2026-10-22'))
    assert result['date'] == '2026-09-30'


def test_only_unpublished_rows_cannot_be_cached_as_current():
    assert mod.normalize_short_interest('AAPL',[row('2026-10-15')], acquired_at=instant('2026-10-22')).get('error')


def test_scalar_cache_is_rechecked_after_next_publication(native, monkeypatch):
    clock=['2026-10-22T22:00:00+00:00']
    monkeypatch.setattr(mod,'acquisition_time',lambda:clock[0])
    source=native.source
    native.add([raw('AAPL')])
    assert source.fetch({'method':'equity_short_interest','ticker':'AAPL'})['date']=='2026-09-30'
    clock[0]='2026-10-27T22:00:00+00:00'
    result=source.fetch({'method':'equity_short_interest','ticker':'AAPL'})
    assert result.get('error') and 'equity_short_interest|AAPL' not in source._cache


def test_absent_native_history_retains_explicit_missing_scope():
    result = mod.normalize_short_interest('AAPL', None, acquired_at=instant('2026-10-22'))
    assert result['freshness']['status'] == 'missing'
    assert result['reason_code'] == 'provider_error'
