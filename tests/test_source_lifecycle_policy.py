"""Explicit prospective retirement preserves source and health evidence."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

POLICY = 'explicit_retirement_v1'
REASON = 'reviewed_source_retirement'


def retirement(source='cftc', strategies=('commodity_macro',)):
    return {'source_lifecycle_policy': POLICY,
        'research_resilience_policy': 'dependency_isolation_v1',
        'decision_clock_policy': 'prospective_next_open_v1',
        'retired_sources': {source: {'reason': REASON, 'strategies': list(strategies)}},
        'disabled_strategies': {strategy: REASON for strategy in strategies}}


def test_explicit_retirement_stops_only_reviewed_source_acquisition(tmp_path):
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    calls = []
    config = {'autoresearch': {**retirement(), 'state_dir': str(tmp_path)}}
    strategy = SimpleNamespace(name='commodity_macro', data_sources=['cftc', 'fred'])
    registry = SimpleNamespace(available_sources=lambda: ['cftc', 'fred'], get=lambda name: None)
    engine = MultiStrategyEngine(config=config, strategies=[strategy], registry=registry)
    engine._fetch_cftc_data = lambda *args: calls.append('cftc') or {}
    engine._fetch_fred_data = lambda *args: calls.append('fred') or {}
    from tradingagents.strategies.orchestration.decision_clock import latest_completed_session
    session = latest_completed_session(datetime.now(timezone.utc)).isoformat()
    data = engine._fetch_all_data('2026-07-01', session)
    assert calls == ['fred']
    assert 'cftc' not in data
    assert data['_source_lifecycle_policy']['retired_sources']['cftc']['reason'] == REASON


@pytest.mark.parametrize('change', [
    lambda ar: ar.pop('source_lifecycle_policy'),
    lambda ar: ar.update(source_lifecycle_policy='unknown'),
    lambda ar: ar.pop('decision_clock_policy'),
    lambda ar: ar.pop('research_resilience_policy'),
    lambda ar: ar.update(disabled_strategies={}),
    lambda ar: ar['retired_sources']['cftc'].update(strategies=[]),
    lambda ar: ar['retired_sources']['cftc'].update(strategies=['unknown']),
    lambda ar: ar['retired_sources']['cftc'].update(reason=''),
    lambda ar: ar['retired_sources'].update(unknown={'reason': REASON, 'strategies': ['commodity_macro']}),
])
def test_invalid_or_implicit_retirement_fails_before_state_creation(tmp_path, change):
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    ar = retirement()
    change(ar)
    ar['state_dir'] = str(tmp_path / 'never-created')
    with pytest.raises(ValueError, match='source_lifecycle'):
        MultiStrategyEngine({'autoresearch': ar})
    assert not (tmp_path / 'never-created').exists()


@pytest.mark.parametrize('source', ['fred', 'yfinance', 'openbb'])
def test_shared_context_sources_cannot_be_retired(source):
    from tradingagents.strategies.orchestration.source_lifecycle_policy import configured
    from tradingagents.strategies.modules import get_paper_trade_strategies
    strategies = get_paper_trade_strategies()
    dependents = sorted(s.name for s in strategies if source in s.data_sources)
    with pytest.raises(ValueError, match='source_lifecycle'):
        configured(retirement(source, dependents), strategies)


def test_source_with_an_enabled_dependent_cannot_be_retired():
    from tradingagents.strategies.orchestration.source_lifecycle_policy import configured
    strategies = [SimpleNamespace(name='commodity_macro', data_sources=['cftc']),
                  SimpleNamespace(name='another', data_sources=['cftc'])]
    with pytest.raises(ValueError, match='source_lifecycle'):
        configured(retirement(), strategies)


def test_edgar_retirement_cannot_bypass_focused_litigation_issuer_verification():
    from tradingagents.strategies.orchestration.source_lifecycle_policy import configured
    from tradingagents.strategies.modules import get_paper_trade_strategies
    config = retirement('edgar', ('filing_analysis', 'insider_activity', 'quantum_readiness'))
    config['courtlistener_scope_policy'] = 'focused_litigation_v1'
    with pytest.raises(ValueError, match='source_lifecycle'):
        configured(config, get_paper_trade_strategies())


def test_declaration_requires_matching_frozen_config_and_exclusion_health():
    from tradingagents.strategies.orchestration.source_lifecycle_policy import (
        declaration, declared_scope, validate_health)
    from tradingagents.strategies.modules import get_paper_trade_strategies
    strategies = get_paper_trade_strategies()
    config = retirement()
    data = declaration(config, strategies)
    scope = declared_scope(data, config, strategies)
    record = {'strategy': 'commodity_macro', 'policy_id': 'horizon-policy',
        'status': 'disabled_by_policy', 'signal_count': 0,
        'evidence': {'reason': REASON, 'candidate_count': 0,
            'data_sources': sorted(next(s.data_sources for s in strategies if s.name == 'commodity_macro')),
            'source_lifecycle_policy': POLICY, 'source_lifecycle_sha256': scope['sha256']}}
    validate_health(scope, [record], {'horizon-policy'})
    changed = deepcopy(config)
    changed['retired_sources']['cftc']['reason'] = 'different_review'
    changed['disabled_strategies']['commodity_macro'] = 'different_review'
    with pytest.raises(ValueError, match='source_lifecycle'):
        declared_scope(data, changed, strategies)
    for key in ('reason', 'candidate_count', 'source_lifecycle_policy', 'source_lifecycle_sha256', 'data_sources'):
        damaged = deepcopy(record)
        damaged['evidence'].pop(key)
        with pytest.raises(ValueError, match='source_lifecycle'):
            validate_health(scope, [damaged], {'horizon-policy'})


def test_edgar_retirement_requires_legacy_identity_consumers_in_explicit_closure():
    from tradingagents.strategies.orchestration.source_lifecycle_policy import configured
    from tradingagents.strategies.modules import get_paper_trade_strategies
    config = retirement('edgar', ('filing_analysis', 'insider_activity', 'quantum_readiness'))
    with pytest.raises(ValueError, match='source_lifecycle'):
        configured(config, get_paper_trade_strategies())
