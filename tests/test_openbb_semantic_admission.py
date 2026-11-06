"""Malformed optional facts must not become complete committee enrichment."""
from types import SimpleNamespace as N

import pytest

from tradingagents.strategies.data_sources import openbb_source as mod
from tradingagents.strategies.orchestration.research_resilience import project_enrichment


@pytest.fixture
def source(monkeypatch):
    monkeypatch.setattr(mod, 'provider_call', lambda provider, operation, fn, **kwargs: fn())
    monkeypatch.setattr(mod, 'acquisition_time', lambda: '2026-10-22T00:00:00+00:00', raising=False)
    return mod.OpenBBSource()


def factors(source, rows, **params):
    source._obb = N(famafrench=N(factors=lambda **kwargs: N(results=[N(**row) for row in rows])))
    return source.fetch({'method': 'factors_fama_french', **params})


def factor(day='2026-08-31', **changes):
    return dict(date=day, mkt_rf=.01, smb=.02, hml=-.03, rf=.001, **changes)


@pytest.mark.parametrize('field,bad', [('mkt_rf', float('nan')), ('smb', float('inf')), ('hml', True), ('rf', '0.01')])
def test_invalid_factor_numbers_fail_and_do_not_cache(source, field, bad):
    row = factor(); row[field] = bad
    result = factors(source, [row])
    assert result.get('error') and result['reason_code'] == 'invalid_response'
    assert not source._cache


@pytest.mark.parametrize('rows', [[], [dict(date='2026-08-31', mkt_rf=.01)],
    [factor('invalid')], [factor('2026-11-30')], [factor(), factor()],
    [factor('2026-08-01'), factor('2026-08-31')]])
def test_incomplete_undated_future_or_duplicate_months_fail(source, rows):
    assert factors(source, rows).get('error')
    assert not source._cache


def test_monthly_history_is_sorted_dated_and_not_daily_staleness(source):
    result = factors(source, [factor('2026-08-31'), factor('2026-07-31')])
    assert 'error' not in result
    assert result['factors']['observation_date'] == '2026-08-31'
    assert result['factors']['frequency'] == 'monthly'
    assert result['factors']['model'] == '3'
    assert result['factors']['available_at'] == '2026-10-22T00:00:00+00:00'
    assert list(result['history']) == ['2026-07-31', '2026-08-31']


def test_explicit_five_factor_request_requires_both_additional_factors(source):
    assert factors(source, [factor()], model='5').get('error')


@pytest.mark.parametrize('changes', [dict(sector=''), dict(name=''), dict(market_cap=float('nan')),
                                    dict(market_cap=True)])
def test_unusable_profile_has_explicit_symbol_failure(source, changes):
    row = dict(symbol='AAPL', name='Apple', sector='Technology', market_cap=100); row.update(changes)
    source._obb = N(equity=N(profile=lambda **kwargs: N(results=[N(**row)])))
    result = source.fetch_profiles(['AAPL'])
    assert not result['profiles'] and result['errors']['AAPL']['reason_code'] == 'invalid_response'
    assert not source._cache
    _, scope = project_enrichment({'profiles': result['profiles'], 'errors': {'profiles': result['errors']}})
    assert scope['excluded'] and scope['research_coverage_complete'] is False


def test_symbol_only_profile_cannot_authorize_complete_enrichment(source):
    source._obb = N(equity=N(profile=lambda **kwargs: N(results=[N(symbol='AAPL')])))
    result = source.fetch_profiles(['AAPL'])
    assert result['errors'] and not result['profiles']


def test_scalar_profile_rejects_mismatched_symbol(source):
    source._obb = N(equity=N(profile=lambda **kwargs: N(results=[N(symbol='OTHER',name='Other',sector='Tech')])) )
    assert source.fetch({'method':'equity_profile','ticker':'AAPL'}).get('error')
