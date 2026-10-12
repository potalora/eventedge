"""Prospective research containment never grants evidence or order authority."""
import pytest


def test_policy_is_opt_in_and_rejects_unknown_values():
    from tradingagents.strategies.orchestration.research_resilience import configured
    assert configured({}) is False
    assert configured({'research_resilience_policy': None}) is False
    assert configured({'research_resilience_policy': 'dependency_isolation_v1'}) is True
    for value in ('', 'ignore_errors', True, [], {}):
        with pytest.raises(ValueError, match='research_resilience_policy'):
            configured({'research_resilience_policy': value})


def test_policy_changes_source_configuration_identity():
    from tradingagents.strategies.orchestration.source_inputs import source_configuration_fingerprint
    strict = {'autoresearch': {}}
    contained = {'autoresearch': {'research_resilience_policy': 'dependency_isolation_v1'}}
    assert source_configuration_fingerprint(strict) != source_configuration_fingerprint(contained)


def test_bad_policy_is_rejected_before_engine_state_creation(tmp_path):
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    state = tmp_path / 'not-created'
    with pytest.raises(ValueError, match='research_resilience_policy'):
        MultiStrategyEngine({'autoresearch': {
            'state_dir': str(state), 'research_resilience_policy': 'ignore_errors'}})
    assert not state.exists()


def test_policy_rotates_execution_and_epoch_semantics(tmp_path):
    from copy import deepcopy
    from decimal import Decimal
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.strategies.orchestration.session_executor import SessionExecutor
    from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger
    from test_metric_epoch_runtime import _context, _policy
    config = deepcopy(DEFAULT_CONFIG)
    config['autoresearch']['state_dir'] = str(tmp_path)
    ledger = PortfolioLedger(tmp_path / 'ledger.db', 'test', Decimal('5000'))
    try:
        strict = SessionExecutor(ledger, config).semantic_policy_document()
        config['autoresearch']['research_resilience_policy'] = 'dependency_isolation_v1'
        contained = SessionExecutor(ledger, config).semantic_policy_document()
        assert strict != contained
        assert contained['research_resilience_policy'] == 'dependency_isolation_v1'
        assert _context(cohort_policies=(_policy(execution_policy=strict),)).config_hash != \
            _context(cohort_policies=(_policy(execution_policy=contained),)).config_hash
    finally:
        ledger.close()


def test_source_projection_removes_failed_payload_but_retains_diagnostic_identity():
    from copy import deepcopy
    from tradingagents.strategies.orchestration.research_resilience import project_sources
    original = {'good': {'observations': [10]},
                'bad': {'observations': [999], 'error': 'unseen malformed document'},
                'partial': {'observations': [888], 'coverage': {'complete': False}}}
    before = deepcopy(original)
    projected, receipt = project_sources(original, ('good', 'bad', 'partial', 'missing'))
    assert projected == {'good': {'observations': [10]}}
    assert receipt['usable_sources'] == ['good']
    assert receipt['excluded_sources'] == ['bad', 'missing', 'partial']
    assert receipt['sources']['bad']['reason'] == 'recorded_source_failure'
    assert receipt['sources']['partial']['reason'] == 'incomplete_source_coverage'
    assert receipt['sources']['missing']['reason'] == 'missing_source'
    assert receipt['research_coverage_complete'] is False
    assert receipt['order_authority'] is False
    assert original == before


def test_projected_sources_and_receipt_are_bound_to_exact_original():
    from tradingagents.strategies.orchestration.research_resilience import project_sources
    first, receipt = project_sources({'one': {'values': [1]}}, ('one',))
    changed, other = project_sources({'one': {'values': [2]}}, ('one',))
    assert first != changed
    assert receipt['source_bundle_sha256'] != other['source_bundle_sha256']
    assert receipt['sources']['one']['sha256'] != other['sources']['one']['sha256']
    assert receipt['research_coverage_complete'] is True


def _screen_engine(tmp_path, *, enabled=True):
    from tradingagents.strategies.modules.base import Candidate
    from tradingagents.strategies.data_sources.registry import DataSourceRegistry
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    class Strategy:
        retirement_reason = None
        track = 'paper_trade'
        def __init__(self, name, source):
            self.name, self.data_sources, self.seen = name, (source,), []
        def get_default_params(self, **_):
            return {}
        def screen(self, data, trading_date, params):
            self.seen.append(data)
            return [Candidate(ticker=self.name.upper(), date=trading_date,
                direction='long', score=80, metadata={'observed_at': '2026-10-09T17:00:00+00:00'})]
    strategies = [Strategy('good', 'good_source'), Strategy('bad', 'bad_source')]
    ar = {'state_dir': str(tmp_path)}
    if enabled:
        ar['research_resilience_policy'] = 'dependency_isolation_v1'
    engine = MultiStrategyEngine({'autoresearch': ar}, strategies=strategies,
        registry=DataSourceRegistry())
    return engine, strategies


def _research_data():
    import pandas as pd
    return {'good_source': {'event': 'usable'},
            'bad_source': {'event': 'untrusted', 'error': 'unseen research failure'},
            'fred': {'VIXCLS': pd.Series([18.0]), 'BAMLH0A0HYM2': pd.Series([3.0]),
                     'T10Y2Y': pd.Series([1.0])}}


def test_engine_contains_provider_failure_before_strategy_or_model_input(tmp_path):
    engine, strategies = _screen_engine(tmp_path)
    original = _research_data()
    signals, regime, health = engine.screen_and_enrich(
        '2026-10-09', original, epoch_id='epoch', policy_id='policy')
    assert [item['strategy'] for item in signals] == ['good']
    assert strategies[1].seen == []
    assert 'bad_source' not in strategies[0].seen[0]
    assert original['bad_source']['event'] == 'untrusted'
    assert next(item for item in health if item.strategy == 'bad').status == 'data_failure'
    assert regime['research_source_scope']['excluded_sources'] == ['bad_source', 'yfinance']
    assert regime['research_hold_reasons'] == []


def test_failed_shared_regime_cannot_influence_unrelated_candidate(tmp_path):
    engine, _ = _screen_engine(tmp_path)
    original = _research_data()
    original['fred']['error'] = 'partial macro source'
    signals, regime, health = engine.screen_and_enrich(
        '2026-10-09', original, epoch_id='epoch', policy_id='policy')
    assert regime['vix_level'] is None
    assert regime['credit_spread_bps'] is None
    assert regime['yield_curve_slope'] is None
    assert regime['research_hold_reasons'] == ['shared_regime_unavailable']
    assert all(row['journal_only'] for row in signals)
    assert all(row['metadata']['non_actionable_reason'] == 'shared_regime_unavailable' for row in signals)
    assert next(item for item in health if item.strategy == 'good').status == 'data_failure'


def test_policy_absence_preserves_existing_provider_projection(tmp_path):
    engine, strategies = _screen_engine(tmp_path, enabled=False)
    signals, regime, _ = engine.screen_and_enrich(
        '2026-10-09', _research_data(), epoch_id='epoch', policy_id='policy')
    assert {row['strategy'] for row in signals} == {'good', 'bad'}
    assert len(strategies[1].seen) == 1
    assert 'research_source_scope' not in regime


def test_engine_binds_each_candidate_to_actual_projection_and_regime(tmp_path):
    from tradingagents.strategies.orchestration.research_resilience import validate_signal
    engine, _ = _screen_engine(tmp_path)
    signals, regime, _ = engine.screen_and_enrich('2026-10-09', _research_data(), epoch_id='epoch', policy_id='policy')
    proof = signals[0]['metadata']['research_admission']
    assert proof['version'] == 'research-candidate-admission-v1'
    assert proof['strategy_sources'] == ['good_source']
    assert len(proof['analysis_input_sha256']) == 64
    validate_signal(signals[0], regime, ('good_source',))


@pytest.mark.parametrize('damage', ['ticker', 'score', 'metadata', 'source_scope', 'regime', 'proof'])
def test_candidate_proof_rejects_changed_admission_inputs(tmp_path, damage):
    from tradingagents.strategies.orchestration.research_resilience import validate_signal
    engine, _ = _screen_engine(tmp_path)
    signals, regime, _ = engine.screen_and_enrich('2026-10-09', _research_data(), epoch_id='epoch', policy_id='policy')
    signal = signals[0]
    if damage == 'ticker': signal['ticker'] = 'INJECTED'
    elif damage == 'score': signal['score'] += 1
    elif damage == 'metadata': signal['metadata']['invented'] = 'facts'
    elif damage == 'source_scope': regime['research_source_scope']['excluded_sources'] = []
    elif damage == 'regime': regime['vix_level'] = 10
    else: del signal['metadata']['research_admission']
    with pytest.raises(ValueError, match='research_candidate'):
        validate_signal(signal, regime, ('good_source',))


def test_research_hold_does_not_invoke_committee_model(monkeypatch):
    from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee
    committee = PortfolioCommittee({'autoresearch': {'research_resilience_policy': 'dependency_isolation_v1'}})
    def forbidden(*args, **kwargs): raise AssertionError('research hold must not invoke model')
    monkeypatch.setattr(committee, '_llm_synthesize', forbidden)
    result = committee.synthesize([{'ticker': 'NVDA', 'direction': 'long', 'strategy': 'good', 'metadata': {}}],
        regime_context={'research_hold_reasons': ['shared_regime_unavailable']})
    assert result == []
    assert committee.last_decision_status['status'] == 'research_hold'
    assert committee.last_decision_status['reason'] == 'shared_regime_unavailable'
    assert committee.last_decision_status['degraded'] is True


def test_staging_reconstructs_projection_before_research_use(tmp_path):
    from tradingagents.strategies.orchestration.research_resilience import validate_projection
    engine, _ = _screen_engine(tmp_path)
    original = _research_data()
    _, regime, _ = engine.screen_and_enrich('2026-10-09', original, epoch_id='epoch', policy_id='policy')
    projection = validate_projection(engine, {**original, '_execution_reference_bars': {}, '_decision_context': None}, regime)
    assert 'bad_source' not in projection
    assert 'fred' in projection
    original['good_source']['event'] = 'changed after screening'
    with pytest.raises(ValueError, match='research_source_projection'):
        validate_projection(engine, original, regime)


def test_engine_isolates_mixed_filings_before_strategy_screen(tmp_path, monkeypatch):
    from test_filing_research_isolation import mixed, CONFIG
    engine, strategies = _screen_engine(tmp_path / 'engine')
    (tmp_path / 'capture').mkdir()
    data, _, good, bad = mixed(tmp_path / 'capture', monkeypatch)
    data.update(_research_data())
    engine.ar_config.update(CONFIG)
    strategies[0].name, strategies[0].data_sources = 'filing_analysis', ('edgar',)
    seen = []
    def screen(projected, *args):
        seen.append(projected['edgar'])
        return []
    strategies[0].screen = screen
    _, regime, health = engine.screen_and_enrich('2026-10-09', data, epoch_id='epoch', policy_id='policy')
    assert [row['adsh'] for row in seen[0]['filings']] == [good]
    assert bad not in seen[0]['filing_evidence']['corpus']
    assert regime['filing_research_isolation']['original_full_coverage'] is False
    assert next(row for row in health if row.strategy == 'filing_analysis').status == 'data_failure'
    from tradingagents.strategies.orchestration.research_resilience import validate_projection
    assert validate_projection(engine, data, regime)['edgar'] == seen[0]


def test_recorded_regulation_outage_without_published_graph_is_contained(tmp_path):
    engine, strategies = _screen_engine(tmp_path)
    engine.ar_config['regulation_evidence_policy'] = 'complete_document_text_v1'
    strategies[1].data_sources = ('regulations',)
    data = _research_data()
    data['regulations'] = {'error': 'document source unavailable', 'proposed_rules': []}
    signals, regime, health = engine.screen_and_enrich('2026-10-09', data, epoch_id='epoch', policy_id='policy')
    assert [row['strategy'] for row in signals] == ['good']
    assert strategies[1].seen == []
    assert next(row for row in health if row.strategy == 'bad').status == 'data_failure'


def test_corrupt_published_regulation_graph_is_integrity_failure(tmp_path):
    engine, _ = _screen_engine(tmp_path)
    engine.ar_config['regulation_evidence_policy'] = 'complete_document_text_v1'
    data = _research_data()
    data['regulations'] = {'error': 'cannot excuse a corrupt published graph', 'regulation_evidence': {}}
    with pytest.raises(ValueError, match='regulation_evidence_policy'):
        engine.screen_and_enrich('2026-10-09', data, epoch_id='epoch', policy_id='policy')


def test_invalid_declared_positive_scope_is_not_a_research_gap(tmp_path):
    engine, _ = _screen_engine(tmp_path)
    engine.ar_config['award_attribution_policy'] = 'verified_award_issuer_attribution_v1'
    data = _research_data()
    data['usaspending'] = {'award_attribution_policy': 'forged', 'data': {'contracts': []}}
    with pytest.raises(ValueError, match='scoped_source_evidence'):
        engine.screen_and_enrich('2026-10-09', data, epoch_id='epoch', policy_id='policy')


def test_missing_required_universe_holds_all_entries_without_models(tmp_path):
    from tradingagents.strategies.data_sources.equity_universe import POLICY
    from tradingagents.strategies.orchestration.research_resilience import validate_projection
    engine, _ = _screen_engine(tmp_path)
    engine.ar_config['equity_universe_policy'] = POLICY
    data = _research_data()
    data['equity_universe'] = {'error': 'asset master source unavailable'}
    signals, regime, _ = engine.screen_and_enrich('2026-10-09', data, epoch_id='epoch', policy_id='policy')
    assert all(row['journal_only'] for row in signals)
    assert regime['research_hold_reasons'] == ['required_universe_unavailable']
    validate_projection(engine, data, regime)


def _regulation_outage_health(tmp_path):
    engine, strategies = _screen_engine(tmp_path)
    engine.ar_config['regulation_evidence_policy'] = 'complete_document_text_v1'
    strategies[1].data_sources = ('regulations',)
    data = _research_data()
    data['regulations'] = {'error': 'document source unavailable', 'proposed_rules': []}
    _, _, health = engine.screen_and_enrich('2026-10-09', data, epoch_id='epoch', policy_id='policy')
    return engine, data, health


def test_recorded_regulation_outage_can_replay_same_failed_health(tmp_path):
    from tradingagents.strategies.orchestration.regulation_validation import validate_regulation_health
    engine, data, health = _regulation_outage_health(tmp_path)
    assert validate_regulation_health(data, health, {'policy'}, {'bad': ('regulations',)}, engine.ar_config) is None


@pytest.mark.parametrize('damage', ['status', 'assessment', 'error', 'missing'])
def test_regulation_outage_replay_rejects_erased_failure(tmp_path, damage):
    from dataclasses import replace
    from tradingagents.strategies.orchestration.regulation_validation import validate_regulation_health
    engine, data, health = _regulation_outage_health(tmp_path)
    bad = next(row for row in health if row.strategy == 'bad')
    if damage == 'status': health = [replace(row, status='legitimate_no_event') if row is bad else row for row in health]
    elif damage == 'assessment': bad.evidence['regulation_assessments'] = [{'fake': 'success'}]
    elif damage == 'error': bad.evidence['provider_errors'].pop('regulations')
    else: health.remove(bad)
    with pytest.raises(ValueError, match='regulation_health'):
        validate_regulation_health(data, health, {'policy'}, {'bad': ('regulations',)}, engine.ar_config)


def test_declared_incomplete_universe_never_reenters_candidate_context(tmp_path):
    from test_source_inputs import NOW
    from tradingagents.strategies.data_sources.equity_universe import normalize_assets, POLICY
    from tradingagents.strategies.orchestration.research_resilience import validate_projection
    engine, _ = _screen_engine(tmp_path)
    engine.ar_config['equity_universe_policy'] = POLICY
    data = _research_data()
    snapshot = normalize_assets([{'class': 'us_equity', 'symbol': 'GOOD', 'exchange': 'NASDAQ',
        'status': 'active', 'tradable': True}], observed_at=NOW, response_sha256='f' * 64)
    data['equity_universe'] = {'snapshot': snapshot, 'coverage': {'complete': False}}
    signals, regime, _ = engine.screen_and_enrich('2026-10-09', data, epoch_id='epoch', policy_id='policy')
    assert regime['research_hold_reasons'] == ['required_universe_unavailable']
    assert all(row['journal_only'] and not row['metadata']['research_admission']['entry_eligible'] for row in signals)
    assert all('equity_universe_post_analysis' not in row['metadata'] for row in signals)
    validate_projection(engine, data, regime)
