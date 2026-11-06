"""Explicit current vintages never rewrite historical observation windows."""
from types import SimpleNamespace
import pytest

from tradingagents.strategies.data_sources import evidence
from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine


def test_explicit_vintage_allows_old_observation_window_without_backdating(monkeypatch):
    monkeypatch.setattr(evidence, 'current_session_date', lambda: '2026-10-10')
    assert evidence.require_current_vintage('2026-10-09', '2026-10-10') == '2026-10-10'
    with pytest.raises(Exception, match='historical_vintage_unavailable'):
        evidence.require_current_vintage('2026-10-09')
    with pytest.raises(Exception, match='historical_vintage_unavailable'):
        evidence.require_current_vintage('2026-10-09', '2026-10-09')
    with pytest.raises(Exception, match='observation window'):
        evidence.require_current_vintage('2026-10-11', '2026-10-10')


@pytest.mark.parametrize('method,source_method', [('_fetch_noaa_data', 'fetch_ag_weather_summary'),
                                               ('_fetch_cftc_data', 'fetch')])
def test_engine_forwards_distinct_reference_and_vintage(method, source_method):
    calls = []
    def fetch(*args, **kwargs):
        calls.append((args, kwargs))
        return {'ok': True}
    engine = object.__new__(MultiStrategyEngine)
    engine.registry = SimpleNamespace(get=lambda _: SimpleNamespace(**{source_method: fetch}))
    result = getattr(engine, method)('2026-10-09', vintage_as_of='2026-10-10')
    assert result == {'ok': True}
    if method == '_fetch_noaa_data':
        assert calls == [(('2026-10-09',), {'lookback_days': 30, 'vintage_as_of': '2026-10-10'})]
    else:
        assert calls[0][0][0]['as_of'] == '2026-10-09'
        assert calls[0][0][0]['vintage_as_of'] == '2026-10-10'
