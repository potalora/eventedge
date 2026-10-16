"""Verified filing targets remain bound across models, health and frozen replay."""
from copy import deepcopy
from datetime import date, datetime, timezone
import json
import sqlite3
from types import SimpleNamespace

import pytest

from test_filing_attribution_policy import prepared
from test_operational_report import native_evidence, _attempt, _report
from test_prospective_replay_integrity import rewrite_payload
from tradingagents.strategies.data_sources.filing_attribution_policy import POLICY
from tradingagents.strategies.orchestration.filing_attribution_validation import (
    validate_filing_attribution_policy, validate_filing_attribution_health,
)

CONFIG = {'filing_evidence_policy': 'complete_submission_v1', 'filing_attribution_policy': POLICY}
SESSION = '2026-10-09'
HORIZONS = ('30d', '3m', '6m', '1y')


def payload():
    edgar, universe, _ = prepared()
    return {'edgar': edgar, 'equity_universe': {'snapshot': universe.evidence}}


def health(summary):
    return [SimpleNamespace(epoch_id='epoch', session=date.fromisoformat(SESSION),
        policy_id='foundation-' + horizon, strategy='filing_analysis',
        evidence={'filing_attribution_scope': deepcopy(summary)}) for horizon in HORIZONS]


def test_wrapper_checks_real_policy_and_frozen_universe():
    data = payload()
    scope = validate_filing_attribution_policy(data, CONFIG)
    assert (scope['total_rows'], scope['verified_target_rows'], scope['unresolved_rows']) == (4, 1, 3)
    assert validate_filing_attribution_policy({'edgar': {'error': 'acquisition failed'}}, CONFIG) is None
    with pytest.raises(ValueError, match='filing_attribution'):
        validate_filing_attribution_policy(data, {})
    data['equity_universe']['snapshot']['assets'].pop()
    with pytest.raises(ValueError, match='filing_attribution'):
        validate_filing_attribution_policy(data, CONFIG)


@pytest.mark.parametrize('horizon', HORIZONS)
def test_models_and_screens_receive_only_verified_rows_without_changing_source(tmp_path, monkeypatch, horizon):
    from tradingagents.strategies.modules.filing_analysis import FilingAnalysisStrategy
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    strategy = FilingAnalysisStrategy()
    engine = MultiStrategyEngine(config={'autoresearch': {**CONFIG, 'state_dir': str(tmp_path)}},
                                 strategies=[strategy])
    data = payload()
    original = deepcopy(data)
    calls = []
    def check(projected):
        assert len(projected['edgar']['filings']) == 1
        assert projected['edgar']['passive_13g'] == []
        assert len(projected['edgar']['filing_evidence']['corpus']) == 4
        calls.append('checked')
    def regime(projected):
        check(projected)
        return {}
    def screen(projected, *args):
        check(projected)
        return []
    monkeypatch.setattr(engine, '_build_regime_model', regime)
    monkeypatch.setattr(strategy, 'screen', screen)
    monkeypatch.setattr(engine, '_enrich_with_llm', lambda *a, **k: pytest.fail('no admitted candidate'))
    signals, _, records = engine.screen_and_enrich(SESSION, data, horizon=horizon,
        epoch_id='epoch', policy_id='foundation-' + horizon)
    assert calls == ['checked', 'checked'] and signals == [] and data == original
    assert records[0].evidence['filing_attribution_scope'] == validate_filing_attribution_policy(data, CONFIG)


@pytest.mark.parametrize('damage', ['missing', 'different', 'float', 'duplicate'])
def test_completed_replay_rejects_attribution_health_conflicts(monkeypatch, damage):
    from tradingagents.strategies.orchestration import scoped_replay
    from tradingagents.strategies.modules.filing_analysis import FilingAnalysisStrategy
    data = payload()
    scope = validate_filing_attribution_policy(data, CONFIG)
    records = health(scope)
    owner = SimpleNamespace(_base_config={'autoresearch': CONFIG},
        cohorts=[{'config': SimpleNamespace(horizon=h),
                  'engine': SimpleNamespace(paper_trade_strategies=[FilingAnalysisStrategy()])} for h in HORIZONS],
        _policy_id_for_horizon=lambda h: 'foundation-' + h,
        _metric_store=SimpleNamespace(read_strategy_health=lambda *a, **k: records))
    monkeypatch.setattr(scoped_replay, 'validate_portfolio_targets', lambda *a: None)
    monkeypatch.setattr(scoped_replay, 'source_scope_evidence', lambda *a, **k: ({}, {}, {}))
    kwargs = {'now': datetime(2026, 10, 10, tzinfo=timezone.utc)}
    scoped_replay.validate_replay_source_scopes(data, owner, SESSION, 'epoch', **kwargs)
    if damage == 'missing': records.pop()
    if damage == 'different': records[0].evidence['filing_attribution_scope']['unresolved_rows'] = 0
    if damage == 'float': records[0].evidence['filing_attribution_scope']['total_rows'] = 4.0
    if damage == 'duplicate': records.append(records[0])
    with pytest.raises(ValueError):
        scoped_replay.validate_replay_source_scopes(data, owner, SESSION, 'epoch', **kwargs)


@pytest.mark.parametrize('damage', [False, True])
def test_report_recomputes_attribution_and_checks_all_dependent_health(native_evidence, damage):
    from tradingagents.strategies.modules import get_paper_trade_strategies
    from tradingagents.strategies.orchestration.operational_report import render_operational_report
    repo, state, wire = native_evidence
    data = payload()
    scope = validate_filing_attribution_policy(data, CONFIG)
    dependents = {s.name for s in get_paper_trade_strategies() if 'edgar' in s.data_sources}
    rewrite_payload(next((state / 'source_inputs').glob('*.json')), lambda frozen: frozen.update(data))
    with sqlite3.connect(state / 'metrics_v2.sqlite3') as connection:
        damaged = False
        for identity, raw in connection.execute('SELECT health_id,payload_json FROM strategy_health').fetchall():
            value = json.loads(raw)
            if value['strategy'] not in dependents:
                continue
            value['evidence']['filing_attribution_scope'] = deepcopy(scope)
            if damage and not damaged:
                value['evidence']['filing_attribution_scope']['unresolved_rows'] = 0
                damaged = True
            connection.execute('UPDATE strategy_health SET payload_json=? WHERE health_id=?',
                               (json.dumps(value), identity))
    _attempt(repo, wire)
    report = _report(repo)
    assert report['evidence_complete'] is not damage
    if damage:
        assert {'code': 'filing_attribution_scope_conflict'} in report['diagnostics']
    else:
        assert report['sources']['filing_attribution_scope'] == scope
        rendered = render_operational_report(report)
        assert '3 unresolved' in rendered and 'Unresolved rows remain non-actionable' in rendered


def test_unbound_health_scope_cannot_be_published():
    with pytest.raises(ValueError, match='filing attribution'):
        validate_filing_attribution_health(health({'invented': 1}),
            {'foundation-' + h for h in HORIZONS}, {'filing_analysis'}, None)


@pytest.mark.parametrize('partial_graph', [False, True])
@pytest.mark.parametrize('damage', ['healthy', 'missing_error', 'different_error', 'wrong_sources'])
def test_replay_binds_retained_edgar_failure_to_all_dependent_health(monkeypatch, partial_graph, damage):
    from tradingagents.strategies.orchestration import scoped_replay
    from tradingagents.strategies.modules.filing_analysis import FilingAnalysisStrategy
    data = payload() if partial_graph else {'edgar': {}}
    data['edgar']['error'] = 'required filing body unavailable'
    scope = validate_filing_attribution_policy(data, CONFIG)
    strategy = FilingAnalysisStrategy()
    records = health(scope)
    for record in records:
        if scope is None:
            record.evidence.pop('filing_attribution_scope')
        record.status = 'data_failure'
        record.evidence.update(provider_errors={'edgar': data['edgar']['error']},
                               data_sources=sorted(strategy.data_sources))
    owner = SimpleNamespace(_base_config={'autoresearch': CONFIG},
        cohorts=[{'config': SimpleNamespace(horizon=h),
                  'engine': SimpleNamespace(paper_trade_strategies=[strategy])} for h in HORIZONS],
        _policy_id_for_horizon=lambda h: 'foundation-' + h,
        _metric_store=SimpleNamespace(read_strategy_health=lambda *a, **k: records))
    monkeypatch.setattr(scoped_replay, 'validate_portfolio_targets', lambda *a: None)
    monkeypatch.setattr(scoped_replay, 'source_scope_evidence', lambda *a, **k: ({}, {}, {}))
    kwargs = {'now': datetime(2026, 10, 10, tzinfo=timezone.utc)}
    scoped_replay.validate_replay_source_scopes(data, owner, SESSION, 'epoch', **kwargs)
    if damage == 'healthy': records[0].status = 'legitimate_no_event'
    if damage == 'missing_error': records[0].evidence.pop('provider_errors')
    if damage == 'different_error': records[0].evidence['provider_errors']['edgar'] = 'different source failure'
    if damage == 'wrong_sources': records[0].evidence['data_sources'] = []
    with pytest.raises(ValueError, match='filing attribution'):
        scoped_replay.validate_replay_source_scopes(data, owner, SESSION, 'epoch', **kwargs)


@pytest.mark.parametrize('partial_graph', [False, True])
@pytest.mark.parametrize('damage', [False, True])
def test_report_binds_original_edgar_error_without_exposing_raw_contents(native_evidence, partial_graph, damage):
    from tradingagents.strategies.modules import get_paper_trade_strategies
    repo, state, wire = native_evidence
    data = payload() if partial_graph else {'edgar': {}}
    error = 'required filing body unavailable'
    data['edgar']['error'] = error
    scope = validate_filing_attribution_policy(data, CONFIG)
    dependents = {strategy.name: sorted(strategy.data_sources) for strategy in get_paper_trade_strategies()
                  if 'edgar' in strategy.data_sources}
    rewrite_payload(next((state / 'source_inputs').glob('*.json')), lambda frozen: frozen.update(data))
    with sqlite3.connect(state / 'metrics_v2.sqlite3') as connection:
        damaged = False
        for identity, raw in connection.execute('SELECT health_id,payload_json FROM strategy_health').fetchall():
            record = json.loads(raw)
            if record['strategy'] not in dependents:
                continue
            record['status'] = 'data_failure'
            record['evidence'].update(provider_errors={'edgar': error}, data_sources=dependents[record['strategy']])
            if scope is not None:
                record['evidence']['filing_attribution_scope'] = deepcopy(scope)
            if damage and not damaged:
                record['evidence']['provider_errors']['edgar'] = 'wrong source failure'
                damaged = True
            connection.execute('UPDATE strategy_health SET payload_json=? WHERE health_id=?',
                               (json.dumps(record), identity))
    _attempt(repo, wire)
    report = _report(repo)
    assert ({'code': 'filing_attribution_scope_conflict'} in report['diagnostics']) is damage
    assert 'filing_source_error_sha256' in report['sources']
    assert error not in json.dumps(report['sources'])


def test_healthy_source_still_allows_durable_analysis_failure():
    from tradingagents.strategies.orchestration.filing_attribution_validation import filing_source_error_sha256
    data = payload()
    scope = validate_filing_attribution_policy(data, CONFIG)
    records = health(scope)
    for record in records:
        record.status = 'analysis_failure'
        record.evidence['provider_errors'] = {'analysis': 'required analysis failed'}
    validate_filing_attribution_health(records, {'foundation-' + h for h in HORIZONS},
                                     {'filing_analysis'}, scope,
                                     expected_error_sha256=filing_source_error_sha256(data['edgar']))
