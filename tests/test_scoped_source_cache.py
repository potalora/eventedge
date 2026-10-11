"""A changed portfolio target scope cannot reuse a different Court search."""
from types import SimpleNamespace

from test_litigation_targets import owner, retained, COMPANIES, SESSION
from test_source_inputs import _engine
from test_focused_courtlistener import Response
from tradingagents.strategies.data_sources.courtlistener_source import CourtListenerSource
from tradingagents.strategies.modules.litigation import LitigationStrategy


def test_changed_pending_issuer_invalidates_query_cache_without_losing_original_scope(owner, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr('tradingagents.strategies.data_sources.request_policy.PROVIDER_LIMITS', {})
    def http(url, **kwargs):
        calls.append(kwargs['params']['q'])
        return Response({'count': 0, 'results': [], 'next': None})
    monkeypatch.setattr('requests.get', http)
    court = CourtListenerSource('offline')
    edgar = SimpleNamespace(company_ticker_map=lambda: COMPANIES)
    registry = SimpleNamespace(available_sources=lambda: ['courtlistener', 'edgar'],
        get=lambda name: {'courtlistener': court, 'edgar': edgar}.get(name))
    engine = _engine(tmp_path, cache_dir=tmp_path/'cache')
    engine.registry = registry
    engine.paper_trade_strategies = [LitigationStrategy()]
    engine.ar_config.update(courtlistener_scope_policy='focused_litigation_v1',
        courtlistener_focused={'watchlist': ['AAPL']})
    first = engine._fetch_all_data('2026-05-05', str(SESSION), litigation_target_owner=owner)
    assert 'error' not in first['courtlistener']
    assert calls == ['caseName:"Apple Inc."']
    second = engine._fetch_all_data('2026-05-05', str(SESSION), litigation_target_owner=owner)
    assert second['courtlistener'] == first['courtlistener'] and len(calls) == 1
    retained(owner.cohorts[-1]['ledger'], 'MSFT', identity='new-pending', pending=True)
    third = engine._fetch_all_data('2026-05-05', str(SESSION), litigation_target_owner=owner)
    assert calls == ['caseName:"Apple Inc."', 'caseName:"Apple Inc."', 'caseName:"Microsoft Corp."']
    assert [row['ticker'] for row in first['_courtlistener_targets']['scope']['issuers']] == ['AAPL']
    assert [row['ticker'] for row in third['_courtlistener_targets']['scope']['issuers']] == ['AAPL', 'MSFT']
    assert first['courtlistener']['coverage']['scope_sha256'] != third['courtlistener']['coverage']['scope_sha256']


def test_focused_fetch_cannot_start_without_original_deadline(tmp_path):
    import pytest
    engine = _engine(tmp_path)
    engine.ar_config['courtlistener_scope_policy'] = 'focused_litigation_v1'
    with pytest.raises(ValueError, match='parent deadline'):
        engine._fetch_courtlistener_data(str(SESSION), {'policy': 'focused_litigation_v1'})
