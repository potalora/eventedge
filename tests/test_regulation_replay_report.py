"""All-horizon regulatory replay validates raw sources and exact assessments."""
from copy import deepcopy
from datetime import date, datetime, timezone
import json
import sqlite3
from types import SimpleNamespace

import pytest

from test_regulation_integration import inputs
from test_regulation_assessment import neutral
from test_regulation_evidence import DOC
from test_regulation_source_evidence import run, source, transport
from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer
from tradingagents.strategies.modules.regulatory_pipeline import RegulatoryPipelineStrategy
from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
from tradingagents.strategies.orchestration.regulation_validation import validate_regulation_health

CONFIG = {'regulation_evidence_policy': 'complete_document_text_v1'}
POLICIES = {'foundation-' + value for value in ('30d', '3m', '6m', '1y')}
STRATEGIES = {'regulatory_pipeline': RegulatoryPipelineStrategy.data_sources}


def completed(tmp_path, monkeypatch, *, insufficient=False):
    _, data, assets = inputs(monkeypatch)
    data.update(yfinance={}, openbb={}, equity_universe={'snapshot': assets.evidence})
    engine = MultiStrategyEngine(config={'autoresearch': {**CONFIG, 'state_dir': str(tmp_path),
        'equity_universe_policy': 'us_exchange_listed_sip_v1'}}, strategies=[RegulatoryPipelineStrategy()])
    monkeypatch.setattr(engine, '_build_regime_model', lambda _: {})
    engine._analyzer = LLMAnalyzer()
    response = neutral(data['regulations']['regulation_evidence']['corpus'][DOC])
    if insufficient:
        response.update(outcome='insufficient', direction=None, conviction=None,
            unresolved_material_dependencies=[{'reference': 'Applicability', 'reason': 'Unknown'}])
    monkeypatch.setattr(engine._analyzer, '_call_llm', lambda *a, **kw: json.dumps(response))
    _, _, records = engine.screen_and_enrich('2026-10-09', data, epoch_id='epoch', policy_id='first')
    record = vars(records[0])
    rows = [{**deepcopy(record), 'policy_id': policy} for policy in sorted(POLICIES)]
    return data, rows


def test_completed_no_thesis_is_recomputed_without_source_or_model_calls(tmp_path, monkeypatch):
    data, records = completed(tmp_path, monkeypatch)
    original = deepcopy(data)
    monkeypatch.setattr(LLMAnalyzer, '_call_llm', lambda *a, **k: pytest.fail('new model call'))
    monkeypatch.setattr(source, 'provider_request', lambda *a, **k: pytest.fail('new source call'))
    result = validate_regulation_health(data, records, POLICIES, STRATEGIES, CONFIG)
    assert result['analysis_complete'] is True
    assert len(result['horizons']) == 4
    assert all(row['assessed_documents'] == 1 for row in result['horizons'])
    assert data == original


@pytest.mark.parametrize('damage', ['quote', 'request', 'regime', 'unit', 'source_row',
    'missing_assessment', 'missing_horizon', 'duplicate_document', 'no_thesis_order', 'stripped_scope',
    'missing_attempt', 'original_response', 'attempt_request', 'attempt_model', 'attempt_failed'])
def test_frozen_regulatory_health_tampering_rejected(tmp_path, monkeypatch, damage):
    data, records = completed(tmp_path, monkeypatch)
    health = records[0]['evidence']
    row = health['regulation_assessments'][0]
    assessment = row['assessment']
    if damage == 'quote': assessment['citations'][0]['quote'] = 'Invented literal quote'
    elif damage == 'request': assessment['source_provenance']['request_sha256'] = 'a' * 64
    elif damage == 'regime': assessment['source_provenance']['regime_context'] = {'changed': True}
    elif damage == 'unit': data['regulations']['regulation_evidence']['corpus'][DOC]['units'][0]['text'] += ' Forged'
    elif damage == 'source_row': row['source_rows'][0]['row_sha256'] = 'b' * 64
    elif damage == 'missing_assessment': health['regulation_assessments'] = []
    elif damage == 'missing_horizon': records.pop()
    elif damage == 'duplicate_document': health['regulation_assessments'].append(deepcopy(row))
    elif damage == 'no_thesis_order': row['journal_only'] = False
    elif damage == 'stripped_scope': health.pop('regulation_source_coverage')
    elif damage == 'missing_attempt': row.pop('model_attempt')
    elif damage == 'original_response': row['model_attempt']['response_text'] += 'changed'
    elif damage == 'attempt_request': row['model_attempt']['source_provenance']['request_sha256'] = 'b' * 64
    elif damage == 'attempt_model': row['model_attempt']['model_provenance']['response_id'] = 'forged'
    elif damage == 'attempt_failed': row['model_attempt'].update(validation_status='failed', failure_reason='model_deadline_exhausted')
    with pytest.raises(ValueError, match='invalid_regulation_health'):
        validate_regulation_health(data, records, POLICIES, STRATEGIES, CONFIG)


def test_valid_insufficient_response_is_replayed_as_failure(tmp_path, monkeypatch):
    data, records = completed(tmp_path, monkeypatch, insufficient=True)
    result = validate_regulation_health(data, records, POLICIES, STRATEGIES, CONFIG)
    assert result['acquisition_complete'] is True and result['analysis_complete'] is False
    assert all(row['failed_documents'] == 1 for row in result['horizons'])
    records[0]['status'] = 'signals'
    with pytest.raises(ValueError):
        validate_regulation_health(data, records, POLICIES, STRATEGIES, CONFIG)


def test_acquisition_failure_cannot_be_neutral_or_healthy(monkeypatch):
    from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
    from tradingagents.strategies.orchestration.regulation_inputs import validate_regulation_policy
    transport(monkeypatch, status=403)
    with pytest.raises(SourceFetchError) as failure:
        run(source.RegulationsSource(api_key='fixture-key'))
    payload = deepcopy(failure.value.partial_data)
    payload['error'] = str(failure.value)
    data = {'regulations': payload}
    scope = validate_regulation_policy(data, CONFIG)
    records = [dict(policy_id=policy, strategy='regulatory_pipeline', status='data_failure',
        evidence={'data_sources': sorted(STRATEGIES['regulatory_pipeline']),
            'provider_errors': {'regulations': payload['error']},
            'regulation_source_coverage': deepcopy(scope), 'regulation_assessments': []}) for policy in sorted(POLICIES)]
    assert validate_regulation_health(data, records, POLICIES, STRATEGIES, CONFIG)['analysis_complete'] is False
    records[0]['evidence']['provider_errors']['regulations'] = 'different failure'
    with pytest.raises(ValueError):
        validate_regulation_health(data, records, POLICIES, STRATEGIES, CONFIG)


def test_scoped_replay_rechecks_regulatory_health(tmp_path, monkeypatch):
    from tradingagents.strategies.orchestration import scoped_replay
    data, records = completed(tmp_path, monkeypatch)
    objects = [SimpleNamespace(**row) for row in records]
    owner = SimpleNamespace(_base_config={'autoresearch': CONFIG},
        cohorts=[{'config': SimpleNamespace(horizon=h),
                  'engine': SimpleNamespace(paper_trade_strategies=[RegulatoryPipelineStrategy()])}
                 for h in ('30d', '3m', '6m', '1y')],
        _policy_id_for_horizon=lambda h: 'foundation-' + h,
        _metric_store=SimpleNamespace(read_strategy_health=lambda *a, **k: objects))
    monkeypatch.setattr(scoped_replay, 'validate_portfolio_targets', lambda *a: None)
    monkeypatch.setattr(scoped_replay, 'source_scope_evidence', lambda *a, **k: ({}, {}, {}))
    scoped_replay.validate_replay_source_scopes(data, owner, '2026-10-09', 'epoch', now=datetime.now(timezone.utc))
    objects[0].evidence['regulation_assessments'][0]['assessment']['citations'].clear()
    with pytest.raises(ValueError):
        scoped_replay.validate_replay_source_scopes(data, owner, '2026-10-09', 'epoch', now=datetime.now(timezone.utc))


from test_operational_report import native_evidence, _attempt, _report
from test_prospective_replay_integrity import rewrite_payload


@pytest.mark.parametrize('tamper', [False, True])
def test_operational_report_separates_acquisition_and_analysis(native_evidence, tmp_path, monkeypatch, tamper):
    from tradingagents.strategies.orchestration.operational_report import render_operational_report
    repo, state, wire = native_evidence
    data, records = completed(tmp_path / 'engine', monkeypatch)
    rewrite_payload(next((state / 'source_inputs').glob('*.json')), lambda payload: payload.update(data))
    with sqlite3.connect(state / 'metrics_v2.sqlite3') as connection:
        changed = False
        for identity, raw in connection.execute('SELECT health_id,payload_json FROM strategy_health').fetchall():
            record = json.loads(raw)
            if record['strategy'] == 'commodity_macro':
                record['evidence']['regulation_source_coverage'] = deepcopy(
                    records[0]['evidence']['regulation_source_coverage'])
                record['evidence']['regulation_assessments'] = []
                connection.execute('UPDATE strategy_health SET payload_json=? WHERE health_id=?',
                    (json.dumps(record), identity))
                continue
            if record['strategy'] != 'regulatory_pipeline':
                continue
            record['evidence'] = deepcopy(records[0]['evidence'])
            record['status'] = records[0]['status']
            if tamper and not changed:
                record['evidence']['regulation_assessments'][0]['assessment']['citations'].clear()
                changed = True
            connection.execute('UPDATE strategy_health SET payload_json=? WHERE health_id=?',
                (json.dumps(record), identity))
    _attempt(repo, wire)
    report = _report(repo)
    assert report['sources']['regulation_source_coverage']['content_complete'] is True
    assert ({'code': 'regulation_evidence_conflict'} in report['diagnostics']) is tamper
    rendered = render_operational_report(report)
    assert 'Regulatory acquisition complete: true' in rendered
    if not tamper:
        assert report['sources']['regulation_analysis_coverage']['analysis_complete'] is True
        assert 'Regulatory analysis complete across configured horizons: true' in rendered
    assert 'This action applies to ACME' not in rendered
