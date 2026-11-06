"""Scalar FINRA calls share the bounded current-cycle snapshot path."""
from datetime import date
from types import SimpleNamespace as N

from test_finra_bulk import native, raw
from tradingagents.strategies.data_sources import openbb_source as mod
from tradingagents.strategies.data_sources.request_policy import provider_budget


def test_uncached_scalar_after_bulk_does_not_reenter_sdk_history_preparation(native, monkeypatch):
    monkeypatch.setattr(mod, 'acquisition_time', lambda: '2026-10-22T12:00:00+00:00')
    native.add([raw('A'), raw('B')])
    clock = [0.0]
    def legacy_scalar(**kwargs):
        # Native scalar extract_data would reenter the historical preparation.
        clock[0] = 61.0
        raise TimeoutError('Historical backfill exhausted current-query time')
    native.source._obb = N(equity=N(shorts=N(short_interest=legacy_scalar)))
    assert native.source.fetch_short_interest(['A'])['short_interest']['A']['date'] == '2026-09-30'
    assert 'equity_short_interest|B' not in native.source._cache
    with provider_budget('openbb', 5, clock=lambda: clock[0], limits=()):
        result = native.source.fetch({'method': 'equity_short_interest', 'ticker': 'B'})
    assert result == {'short_interest': 400.0, 'short_pct_of_float': None,
                      'days_to_cover': 4.0, 'date': '2026-09-30'}
    assert native.prepare_calls == 0 and clock[0] == 0
    assert native.source._cache['equity_short_interest|B'] == result


def test_scalar_preserves_reference_session_and_excludes_future_published_tail(native, monkeypatch):
    monkeypatch.setattr(mod, 'acquisition_time', lambda: '2026-10-27T12:00:00+00:00')
    native.add([raw('A'), raw('A', settlementDate='2026-10-15', currentShortPositionQuantity=900)])
    result = native.source.fetch({'method': 'equity_short_interest', 'symbol': 'A',
                                  'as_of': date(2026, 10, 22)})
    assert result['short_interest'] == 400.0 and result['date'] == '2026-09-30'
    assert native.prepare_calls == 0


def test_scalar_stale_failure_preserves_bulk_freshness_evidence(native, monkeypatch):
    monkeypatch.setattr(mod, 'acquisition_time', lambda: '2026-10-22T12:00:00+00:00')
    native.add([raw('A'), raw('B', settlementDate='2026-09-15')])
    expected = native.source.fetch_short_interest(['B'])['errors']['B']
    result = native.source.fetch({'method': 'equity_short_interest', 'ticker': 'B'})
    assert result == expected
    assert result['freshness']['status'] == 'stale'
    assert result['freshness']['required_settlement_date'] == '2026-09-30'
    assert 'equity_short_interest|B' not in native.source._cache


def test_scalar_single_symbol_bulk_keeps_short_inherited_deadline(native, monkeypatch):
    monkeypatch.setattr(mod, 'acquisition_time', lambda: '2026-10-22T12:00:00+00:00')
    native.add([raw('A')])
    clock = [4.0]
    native.transform_hook = lambda *args: clock.__setitem__(0, 6.0)
    with provider_budget('openbb', 5, clock=lambda: clock[0], limits=(), max_attempts=1):
        result = native.source.fetch({'method': 'equity_short_interest', 'ticker': 'A'})
    assert result['reason_code'] == 'timeout'
    assert native.source._cache == {}
    assert native.prepare_calls == 0
