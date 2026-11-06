"""Linked-publication acquisition remains explicit, prospective and isolated."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest


def settings():
    return {'autoresearch': {
        'regulation_publication_policy': 'linked_publications_v1',
        'regulation_evidence_policy': 'complete_document_text_v1',
        'regulation_retrieval_policy': 'verified_government_copy_v4',
        'decision_clock_policy': 'prospective_next_open_v1'},
        'execution': {'mode': 'paper', 'execution_enabled': False}}


@pytest.mark.parametrize('field,value', [
    ('regulation_publication_policy', 'unknown'),
    ('regulation_publication_policy', True),
    ('regulation_evidence_policy', None),
    ('regulation_retrieval_policy', 'verified_government_copy_v3'),
    ('decision_clock_policy', None),
])
def test_invalid_publication_configuration_rejected_before_state_creation(tmp_path, field, value):
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    config = settings()
    config['autoresearch'][field] = value
    config['autoresearch']['state_dir'] = str(tmp_path / 'never-created')
    with pytest.raises(ValueError, match='regulation_publication_policy'):
        MultiStrategyEngine(config)
    assert not (tmp_path / 'never-created').exists()


@pytest.mark.parametrize('execution', [None, {}, {'mode': 'live', 'execution_enabled': False},
                                    {'mode': 'paper', 'execution_enabled': True}])
def test_publication_research_requires_explicit_paper_execution_disabled(tmp_path, execution):
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    config = settings()
    config['execution'] = execution
    config['autoresearch']['state_dir'] = str(tmp_path / 'never-created')
    with pytest.raises(ValueError, match='regulation_publication_policy'):
        MultiStrategyEngine(config)
    assert not (tmp_path / 'never-created').exists()


def test_absent_policy_preserves_legacy_configuration():
    from tradingagents.strategies.orchestration.regulation_publication_policy import configured, validate_configuration
    assert configured({}) is False
    assert validate_configuration({}) is False
    assert validate_configuration(settings()) is True


def test_v4_runtime_cannot_use_the_legacy_posted_date_clock(tmp_path):
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    config = settings()
    config['autoresearch'].pop('regulation_publication_policy')
    config['autoresearch']['state_dir'] = str(tmp_path/'never-created')
    with pytest.raises(ValueError, match='regulation_publication_policy'):
        MultiStrategyEngine(config)
    assert not (tmp_path/'never-created').exists()


def test_engine_monitor_source_chain_preserves_explicit_policy_and_graph(tmp_path):
    from tradingagents.strategies.data_sources.evidence import CoverageRecords
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    calls = []
    graph = {'version': 'regulation-publication-source-v1', 'publication_policy': 'linked_publications_v1',
             'corpus': {}, 'publication_nodes': {}}
    rows = CoverageRecords([], coverage={'complete': True})
    rows.regulation_evidence = graph
    source = SimpleNamespace(is_available=lambda: True,
        get_recent_proposed_rules=lambda **kwargs: calls.append(kwargs) or rows)
    engine = MultiStrategyEngine.__new__(MultiStrategyEngine)
    engine.config = settings()
    engine.ar_config = engine.config['autoresearch']
    engine.registry = SimpleNamespace(get=lambda name: source)
    result = engine._fetch_regulations_data('2026-10-22')
    assert calls == [{'agencies': ['SEC', 'EPA', 'FDA', 'FTC', 'DOL', 'CFPB'], 'days_back': 14,
        'as_of': '2026-10-22', 'evidence_policy': 'complete_document_text_v1',
        'retrieval_policy': 'verified_government_copy_v4', 'publication_policy': 'linked_publications_v1'}]
    assert result['regulation_evidence'] is graph
    assert result['coverage'] == {'complete': True}


@pytest.mark.parametrize('disabled', [True, False])
def test_linked_acquisition_requires_an_enabled_regulatory_consumer(tmp_path, disabled):
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    config = settings()
    config['autoresearch'].update(state_dir=str(tmp_path),
        disabled_strategies={'regulatory_pipeline': 'reviewed'} if disabled else {})
    calls = []
    strategy = SimpleNamespace(name='regulatory_pipeline', data_sources=['regulations'])
    registry = SimpleNamespace(available_sources=lambda: ['regulations'], get=lambda name: None)
    engine = MultiStrategyEngine(config, strategies=[strategy], registry=registry)
    engine._fetch_regulations_data = lambda *args: calls.append('regulations') or {}
    data = engine._fetch_all_data('2026-10-01', '2026-10-22', acquisition_purpose='screen_diagnostic')
    assert calls == ([] if disabled else ['regulations'])
    assert ('regulations' in data) is not disabled


def test_disabled_linked_strategy_screens_without_acquiring_or_requiring_a_source_graph(tmp_path, monkeypatch):
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    config = settings()
    config['autoresearch'].update(state_dir=str(tmp_path),
        research_resilience_policy='dependency_isolation_v1',
        disabled_strategies={'regulatory_pipeline': 'reviewed'})
    strategy = SimpleNamespace(name='regulatory_pipeline', data_sources=['regulations'])
    engine = MultiStrategyEngine(config, strategies=[strategy],
        registry=SimpleNamespace(available_sources=lambda: [], get=lambda name: None))
    monkeypatch.setattr(engine, '_build_regime_model', lambda *args: {
        'vix_level': None, 'credit_spread_bps': None, 'yield_curve_slope': None})
    signals, _, health = engine.screen_and_enrich('2026-10-22', {}, epoch_id='epoch', policy_id='fixture')
    assert signals == []
    assert len(health) == 1 and health[0].status == 'disabled_by_policy'
    assert health[0].evidence['candidate_count'] == 0


def test_ordinary_explicitly_retired_regulatory_source_performs_zero_acquisition(tmp_path):
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    ar = {'state_dir': str(tmp_path), 'source_lifecycle_policy': 'explicit_retirement_v1',
        'research_resilience_policy': 'dependency_isolation_v1',
        'decision_clock_policy': 'prospective_next_open_v1',
        'retired_sources': {'regulations': {'reason': 'reviewed', 'strategies': ['regulatory_pipeline']}},
        'disabled_strategies': {'regulatory_pipeline': 'reviewed'}}
    engine = MultiStrategyEngine({'autoresearch': ar},
        strategies=[SimpleNamespace(name='regulatory_pipeline', data_sources=['regulations'])],
        registry=SimpleNamespace(available_sources=lambda: ['regulations'], get=lambda name: None))
    engine._fetch_regulations_data = lambda *a: pytest.fail('retired source acquisition')
    data = engine._fetch_all_data('2026-10-01', '2026-10-22', acquisition_purpose='screen_diagnostic')
    assert 'regulations' not in data
    assert data['_source_lifecycle_policy']['retired_sources']['regulations']['reason'] == 'reviewed'


def test_linked_optional_regulatory_capability_exclusion_performs_zero_acquisition(tmp_path):
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    config = settings()
    config['autoresearch'].update(state_dir=str(tmp_path),
        disabled_strategy_capabilities={'commodity_macro': {'regulations': 'reviewed'}})
    engine = MultiStrategyEngine(config,
        strategies=[SimpleNamespace(name='commodity_macro', data_sources=['regulations'])],
        registry=SimpleNamespace(available_sources=lambda: ['regulations'], get=lambda name: None))
    engine._fetch_regulations_data = lambda *a: pytest.fail('excluded capability acquired source')
    data = engine._fetch_all_data('2026-10-01', '2026-10-22', acquisition_purpose='screen_diagnostic')
    assert 'regulations' not in data


@pytest.mark.parametrize('source', [None, 'regulations'])
def test_linked_bundle_codec_retains_graph_with_finite_capacity(tmp_path, source):
    from tradingagents.strategies.orchestration.source_inputs import (
        source_codec_limits, SourceInputStore, configuration_fingerprint)
    config = settings()
    limits = source_codec_limits(config, source=source)
    assert limits == {'max_bytes': 32 * 1024**2, 'max_nodes': 500_000}
    graph = {'version': 'regulation-publication-source-v1', 'publication_policy': 'linked_publications_v1',
        'corpus': {'root': {'node_refs': ['node'], 'manifest_sha256': 'a' * 64}},
        'publication_nodes': {'node': {'capture': {'body': 'x' * (17 * 1024**2)}}}}
    data = {'regulations': {'regulation_evidence': graph}}
    store = SourceInputStore(tmp_path/'cache', accepted_dir=tmp_path/'accepted', **limits)
    identity = {'generation': 'gen_test', 'session': '2026-10-22', 'commit': 'a' * 40,
                'configuration': configuration_fingerprint(config)}
    store.freeze(identity, data, acquired_at=datetime(2026, 10, 22, tzinfo=timezone.utc))
    assert store.load_frozen(identity) == data
    assert source_codec_limits(config, source='fred') == {}


def test_publication_policy_cannot_coexist_with_retired_regulatory_source():
    from tradingagents.strategies.orchestration.source_lifecycle_policy import configured
    ar = {'source_lifecycle_policy': 'explicit_retirement_v1',
        'research_resilience_policy': 'dependency_isolation_v1',
        'decision_clock_policy': 'prospective_next_open_v1',
        'retired_sources': {'regulations': {'reason': 'reviewed', 'strategies': ['regulatory_pipeline']}},
        'disabled_strategies': {'regulatory_pipeline': 'reviewed'},
        'regulation_publication_policy': 'linked_publications_v1'}
    with pytest.raises(ValueError, match='source_lifecycle'):
        configured(ar, [SimpleNamespace(name='regulatory_pipeline', data_sources=['regulations'])])


def test_stabilization_profile_rejects_publication_policy_without_activating_research():
    from tradingagents.strategies.orchestration.operating_contract import configure_strategies, EXCLUSIONS, CAPABILITY_EXCLUSIONS
    ar = {'stabilization_profile_policy': 'supported_sixteen_books_v1',
        'retired_sources': {'regulations': {'reason': EXCLUSIONS['regulatory_pipeline'], 'strategies': ['regulatory_pipeline']}},
        'disabled_strategies': deepcopy(EXCLUSIONS),
        'disabled_strategy_capabilities': deepcopy(CAPABILITY_EXCLUSIONS),
        'regulation_publication_policy': 'linked_publications_v1'}
    with pytest.raises(ValueError, match='stabilization_profile'):
        configure_strategies([], ar)


@pytest.mark.parametrize('damage', ['missing_config', 'missing_flag', 'retrieval', 'evidence', 'clock', 'live', 'version'])
def test_report_cannot_infer_linked_authority_from_the_graph_itself(damage):
    from tradingagents.strategies.orchestration.regulation_publication_policy import source_configuration
    config = settings()
    graph = {'version': 'regulation-publication-source-v1', 'publication_policy': 'linked_publications_v1',
             'policy': 'complete_document_text_v1', 'retrieval_policy': 'verified_government_copy_v4'}
    if damage == 'missing_config':
        config = None
    elif damage == 'missing_flag':
        config['autoresearch'].pop('regulation_publication_policy')
    elif damage == 'retrieval':
        graph['retrieval_policy'] = 'verified_government_copy_v3'
    elif damage == 'evidence':
        graph['policy'] = None
    elif damage == 'clock':
        config['autoresearch'].pop('decision_clock_policy')
    elif damage == 'live':
        config['execution']['execution_enabled'] = True
    else:
        graph['version'] = 'regulation-source-v1'
    with pytest.raises(ValueError, match='regulation_publication_policy'):
        source_configuration(graph, config)


def test_report_binds_linked_graph_to_frozen_configuration_and_preserves_legacy():
    from tradingagents.strategies.orchestration.regulation_publication_policy import source_configuration
    config = settings()
    graph = {'version': 'regulation-publication-source-v1', 'publication_policy': 'linked_publications_v1',
             'policy': 'complete_document_text_v1', 'retrieval_policy': 'verified_government_copy_v4'}
    assert source_configuration(graph, config) == config['autoresearch']
    assert source_configuration(None, config) == config['autoresearch']  # retained acquisition outage
    legacy = {'version': 'regulation-source-v1', 'policy': 'complete_document_text_v1'}
    assert source_configuration(legacy) == {'regulation_evidence_policy': 'complete_document_text_v1'}
    with pytest.raises(ValueError, match='regulation_publication_policy'):
        source_configuration(legacy, config)


@pytest.mark.parametrize('config', [[], True, 'invalid'])
def test_report_rejects_nonmapping_frozen_configuration(config):
    from tradingagents.strategies.orchestration.regulation_publication_policy import source_configuration
    with pytest.raises(ValueError, match='regulation_publication_policy'):
        source_configuration(None, config)


def test_report_recovers_shared_publication_node_once_without_multiplying_bundle_references():
    from tradingagents.strategies.orchestration.operational_report import _regulation_transport_recoveries
    unit = {'role': 'primary', 'government_copy': {'retrieval_policy': 'verified_government_copy_v4'},
            'resource_id': 'body', 'unit_id': 'u', 'receipt': {'sha256': 'b' * 64}}
    data = {'regulation_evidence': {'version': 'regulation-publication-source-v1',
        'retrieval_policy': 'verified_government_copy_v4', 'manifest_sha256': 'c' * 64,
        'corpus': {'original': {'node_refs': ['shared']}, 'correction': {'node_refs': ['shared']}},
        'publication_nodes': {'shared': {'units': [unit], 'capture': {'primary_attempt': {
            'resource_id': 'body', 'http_status': 403, 'receipt': {'sha256': 'a' * 64}}}}}},
        '_request_diagnostics': [{'provider': 'regulations', 'operation': 'get',
            'reason_code': 'http_error', 'http_status': 403, 'attempts': 1, 'recovered': False}]}
    recovered, mismatch = _regulation_transport_recoveries(data, {'content_complete': True})
    assert not mismatch
    assert list(recovered) == [0]
    assert recovered[0]['primary_receipt_sha256'] == 'a' * 64
    assert recovered[0]['government_receipt_sha256'] == 'b' * 64
    data['_request_diagnostics'].append(deepcopy(data['_request_diagnostics'][0]))
    assert _regulation_transport_recoveries(data, {'content_complete': True}) == ({}, True)


def test_report_revalidates_retained_originals_with_the_actual_frozen_publication_policy(tmp_path, monkeypatch):
    from test_regulation_publications import acquired
    from tradingagents.strategies.modules import get_paper_trade_strategies
    from tradingagents.strategies.orchestration.operational_report import _sources
    from tradingagents.strategies.orchestration.source_inputs import SourceInputStore, source_codec_limits
    from tradingagents.strategies.data_sources import regulations_source
    payload, _ = acquired(monkeypatch, 'archived')
    assert 'error' not in payload
    monkeypatch.setattr(regulations_source, 'provider_request', lambda *a, **kw: pytest.fail('report acquired new source'))
    config = settings()
    data = {name: {} for strategy in get_paper_trade_strategies()
            for name in strategy.data_sources if name != 'openbb'}
    data['regulations'] = payload
    state = tmp_path/'state'
    store = SourceInputStore(tmp_path/'cache', accepted_dir=state/'source_inputs', **source_codec_limits(config))
    store.freeze({'generation': 'gen_test', 'session': '2026-10-22', 'commit': 'a' * 40,
        'configuration': 'fixture'}, data, acquired_at=datetime.now(timezone.utc))
    path = next((state/'source_inputs').glob('*.json'))
    original = path.read_bytes()
    errors = []
    result = _sources(state, 'gen_test', '2026-10-22', 'a' * 40,
        lambda *args: errors.append(args), frozen_config=config)
    assert not errors
    assert result['regulation_source_coverage']['publication_policy'] == 'linked_publications_v1'
    assert result['regulation_source_coverage']['content_complete'] is True
    assert path.read_bytes() == original
    errors.clear()
    unbound = _sources(state, 'gen_test', '2026-10-22', 'a' * 40, lambda *args: errors.append(args))
    assert ('accepted_sources_invalid',) in errors
    assert 'regulation_source_coverage' not in unbound
