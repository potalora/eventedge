"""Prospective rollback excludes SEC research before any provider acquisition."""
import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket
    def forbidden(*args, **kwargs):
        pytest.fail('rollback test attempted network access')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)


def test_disabled_research_makes_no_provider_or_monitor_calls(tmp_path, monkeypatch):
    from test_source_inputs import _engine
    from tradingagents.strategies.learning.event_monitor import EventMonitor
    from tradingagents.strategies.orchestration.source_inputs import successful_source
    engine = _engine(tmp_path)
    engine.ar_config.update(filing_research_policy='disabled_v1',
                           research_resilience_policy='dependency_isolation_v1')
    def forbidden(*args, **kwargs):
        pytest.fail('disabled SEC research attempted acquisition')
    engine.registry.get = forbidden
    monkeypatch.setattr(EventMonitor, '__init__', forbidden)
    result = engine._fetch_edgar_events('2026-10-12', {'snapshot': {}})
    assert result['error'] == 'filing_research_disabled'
    assert result['coverage']['complete'] is False
    assert result['coverage']['reason'] == 'filing_research_disabled'
    assert not successful_source(result)


@pytest.mark.parametrize('value', ['', 'skip', True, [], {}])
def test_unknown_policy_fails_before_engine_state_creation(tmp_path, value):
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    state = tmp_path / 'never-created'
    with pytest.raises(ValueError, match='filing_research_policy'):
        MultiStrategyEngine({'autoresearch': {'state_dir': str(state),
                            'filing_research_policy': value}})
    assert not state.exists()


@pytest.mark.parametrize('key', [
    'filing_evidence_policy', 'filing_comparison_policy', 'filing_parser_policy',
    'filing_attribution_policy', 'filing_acquisition_policy', 'filing_material_policy',
    'filing_material_manifest', 'filing_material_policy_manifest_sha256',
])
def test_disabled_policy_rejects_conflicting_full_filing_configuration(key):
    from tradingagents.strategies.orchestration.filing_research_policy import configured
    with pytest.raises(ValueError, match='filing_research_policy'):
        configured({'filing_research_policy': 'disabled_v1',
                    'research_resilience_policy': 'dependency_isolation_v1', key: 'conflict'})


def test_disabled_policy_requires_dependency_isolation():
    from tradingagents.strategies.orchestration.filing_research_policy import configured
    with pytest.raises(ValueError, match='filing_research_policy'):
        configured({'filing_research_policy': 'disabled_v1'})


def test_legacy_and_historical_full_filing_configuration_remain_opt_in():
    from tradingagents.strategies.orchestration.filing_research_policy import configured
    assert configured({}) is False
    assert configured({'filing_research_policy': None}) is False
    assert configured({'filing_evidence_policy': 'complete_submission_v1'}) is False


def test_disabled_policy_changes_source_configuration_identity():
    from tradingagents.strategies.orchestration.source_inputs import source_configuration_fingerprint
    original = {'autoresearch': {'research_resilience_policy': 'dependency_isolation_v1'}}
    disabled = {'autoresearch': {**original['autoresearch'],
                               'filing_research_policy': 'disabled_v1'}}
    assert source_configuration_fingerprint(original) != source_configuration_fingerprint(disabled)


def test_disabled_edgar_is_excluded_before_strategy_screening(tmp_path):
    from test_research_resilience import _screen_engine, _research_data
    engine, strategies = _screen_engine(tmp_path)
    engine.ar_config['filing_research_policy'] = 'disabled_v1'
    strategies[1].data_sources = ('edgar',)
    data = _research_data()
    data.pop('bad_source')
    data['edgar'] = engine._fetch_edgar_events('2026-10-12')
    signals, regime, health = engine.screen_and_enrich(
        '2026-10-09', data, epoch_id='epoch', policy_id='policy')
    assert [row['strategy'] for row in signals] == ['good']
    assert strategies[1].seen == []
    assert 'edgar' not in strategies[0].seen[0]
    assert 'edgar' in regime['research_source_scope']['excluded_sources']
    assert regime['research_source_scope']['order_authority'] is False
    assert next(row for row in health if row.strategy == 'bad').status == 'data_failure'
