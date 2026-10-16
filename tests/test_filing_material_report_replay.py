"""Frozen material exclusions remain distinct from strict filing completeness."""
from copy import deepcopy
from datetime import date, datetime, timezone
import json
import sqlite3
from types import SimpleNamespace

import pytest

from test_operational_report import native_evidence, _attempt, _report
from test_prospective_replay_integrity import rewrite_payload

SESSION = '2026-10-09'
HORIZONS = ('30d', '3m', '6m', '1y')
POLICY = 'retained_three_material_gaps_v1'
CONFIG = {'filing_evidence_policy': 'complete_submission_v1',
          'filing_acquisition_policy': 'bounded_original_submission_v1',
          'filing_parser_policy': 'two_processes_v1', 'filing_material_policy': POLICY}
SCOPE = {'policy': POLICY, 'policy_manifest_sha256': 'a' * 64,
         'approved_accessions': ['0000016732-26-000031', '0000818479-26-000278', '0001193125-26-402806'],
         'total_rows': 8, 'required_rows': 8, 'quarantined_rows': 3,
         'quarantined_accessions': ['0000016732-26-000031', '0000818479-26-000278', '0001193125-26-402806'],
         'quarantines': [], 'strict_complete': False, 'strict_failed_rows': 3,
         'scoped_complete': True, 'scoped_failed_rows': 0, 'by_collection': {}, 'manifest_sha256': 'b' * 64}


@pytest.fixture
def material_hooks(monkeypatch):
    """Isolate orchestration wiring from graph construction and source acquisition."""
    from tradingagents.strategies.orchestration import filing_material_validation as material
    from tradingagents.strategies.orchestration import filing_acquisition_validation as acquisition
    from tradingagents.strategies.orchestration import filing_policy_validation as comparison
    seen = []
    def validate(data, config):
        seen.append((deepcopy(data), deepcopy(config)))
        assert config['filing_material_policy'] == POLICY
        assert data['edgar']['filing_evidence']['coverage']['complete'] is False
        return deepcopy(SCOPE)
    monkeypatch.setattr(material, 'validate_filing_material_policy', validate)
    monkeypatch.setattr(acquisition, 'validate_filing_acquisition_policy', lambda *args: None)
    monkeypatch.setattr(comparison, 'validate_filing_comparison_policy', lambda *args: None)
    return seen


def frozen_data():
    return {'edgar': {'filings': [{'adsh': accession} for accession in SCOPE['quarantined_accessions']],
        'filing_evidence': {'policy': CONFIG['filing_evidence_policy'],
            'coverage': {'complete': False, 'material_policy': POLICY,
                         'acquisition_policy': CONFIG['filing_acquisition_policy'],
                         'parser_policy': CONFIG['filing_parser_policy']},
            'material_scope': deepcopy(SCOPE)}}}


def replay_owner(records):
    from tradingagents.strategies.modules.filing_analysis import FilingAnalysisStrategy
    strategy = FilingAnalysisStrategy()
    return SimpleNamespace(_base_config={'autoresearch': deepcopy(CONFIG)},
        cohorts=[{'config': SimpleNamespace(horizon=h),
                  'engine': SimpleNamespace(paper_trade_strategies=[strategy])} for h in HORIZONS],
        _policy_id_for_horizon=lambda h: 'foundation-' + h,
        _metric_store=SimpleNamespace(read_strategy_health=lambda *a, **k: records))


def replay_health():
    return [SimpleNamespace(epoch_id='epoch', session=date.fromisoformat(SESSION),
        policy_id='foundation-' + h, strategy='filing_analysis', status='legitimate_no_event',
        evidence={'filing_material_scope': deepcopy(SCOPE)}) for h in HORIZONS]


def run_replay(monkeypatch, data, records):
    from tradingagents.strategies.orchestration import scoped_replay
    monkeypatch.setattr(scoped_replay, 'validate_portfolio_targets', lambda *a: None)
    monkeypatch.setattr(scoped_replay, 'source_scope_evidence', lambda *a, **k: ({}, {}, {}))
    scoped_replay.validate_replay_source_scopes(data, replay_owner(records), SESSION, 'epoch',
        now=datetime(2026, 10, 10, tzinfo=timezone.utc))


@pytest.mark.parametrize('damage', ['missing_scope', 'altered_scope', 'float_count', 'missing_horizon', 'duplicate'])
def test_replay_checks_material_health_across_every_configured_horizon(monkeypatch, material_hooks, damage):
    data, records = frozen_data(), replay_health()
    original = deepcopy(data)
    run_replay(monkeypatch, data, records)
    assert material_hooks[-1][0] == original and data == original
    if damage == 'missing_scope': records[-1].evidence.pop('filing_material_scope')
    elif damage == 'altered_scope': records[-1].evidence['filing_material_scope']['scoped_failed_rows'] = 1
    elif damage == 'float_count': records[-1].evidence['filing_material_scope']['quarantined_rows'] = 3.0
    elif damage == 'missing_horizon': records.pop()
    else: records.append(records[0])
    with pytest.raises(ValueError):
        run_replay(monkeypatch, data, records)


@pytest.mark.parametrize('damage', ['healthy', 'missing_error', 'different_error', 'wrong_sources'])
def test_material_policy_never_erases_an_unrelated_original_edgar_failure(monkeypatch, material_hooks, damage):
    from tradingagents.strategies.modules.filing_analysis import FilingAnalysisStrategy
    data, records = frozen_data(), replay_health()
    data['edgar']['error'] = 'fourth ordinary filing unavailable'
    for record in records:
        record.status = 'data_failure'
        record.evidence.update(provider_errors={'edgar': data['edgar']['error']},
                               data_sources=sorted(FilingAnalysisStrategy.data_sources))
    run_replay(monkeypatch, data, records)
    if damage == 'healthy': records[0].status = 'legitimate_no_event'
    elif damage == 'missing_error': records[0].evidence.pop('provider_errors')
    elif damage == 'different_error': records[0].evidence['provider_errors']['edgar'] = 'different failure'
    else: records[0].evidence['data_sources'] = []
    with pytest.raises(ValueError, match='failure health'):
        run_replay(monkeypatch, data, records)


@pytest.mark.parametrize('damage', [None, 'missing_scope', 'altered_scope'])
def test_report_publishes_recomputed_material_scope_and_rejects_health_conflicts(native_evidence, material_hooks, damage):
    from tradingagents.strategies.modules import get_paper_trade_strategies
    from tradingagents.strategies.orchestration.operational_report import render_operational_report
    repo, state, wire = native_evidence
    data = frozen_data()
    rewrite_payload(next((state / 'source_inputs').glob('*.json')), lambda frozen: frozen.update(data))
    dependents = {s.name for s in get_paper_trade_strategies() if 'edgar' in s.data_sources}
    with sqlite3.connect(state / 'metrics_v2.sqlite3') as connection:
        damaged = False
        for identity, raw in connection.execute('SELECT health_id,payload_json FROM strategy_health').fetchall():
            record = json.loads(raw)
            if record['strategy'] not in dependents:
                continue
            record['evidence']['filing_material_scope'] = deepcopy(SCOPE)
            if damage and not damaged:
                if damage == 'missing_scope': record['evidence'].pop('filing_material_scope')
                else: record['evidence']['filing_material_scope']['scoped_complete'] = False
                damaged = True
            connection.execute('UPDATE strategy_health SET payload_json=? WHERE health_id=?',
                               (json.dumps(record), identity))
    _attempt(repo, wire)
    report = _report(repo)
    assert report['sources']['filing_material_scope'] == SCOPE
    assert material_hooks[-1][0]['edgar'] == data['edgar']
    assert ({'code': 'filing_material_scope_conflict'} in report['diagnostics']) is bool(damage)
    assert report['evidence_complete'] is (damage is None)
    rendered = render_operational_report(report)
    assert 'Strict filing evidence complete: false' in rendered
    assert 'scoped analysis complete: true' in rendered
    assert '3 quarantined rows' in rendered and 'non-actionable' in rendered


def test_report_rejects_unbound_material_health(native_evidence):
    repo, state, wire = native_evidence
    with sqlite3.connect(state / 'metrics_v2.sqlite3') as connection:
        identity, raw = connection.execute('SELECT health_id,payload_json FROM strategy_health LIMIT 1').fetchone()
        record = json.loads(raw)
        record['evidence']['filing_material_scope'] = deepcopy(SCOPE)
        connection.execute('UPDATE strategy_health SET payload_json=? WHERE health_id=?', (json.dumps(record), identity))
    _attempt(repo, wire)
    report = _report(repo)
    assert {'code': 'filing_material_scope_conflict'} in report['diagnostics']
    assert report['evidence_complete'] is False


def test_replay_revalidates_real_original_material_graph_before_health(monkeypatch):
    from test_filing_material_integration import prepared
    from tradingagents.strategies.orchestration import filing_acquisition_validation as acquisition
    from tradingagents.strategies.orchestration.filing_material_validation import validate_filing_material_policy
    # This hydration fixture supplies synthetic parsed originals, without a spool
    # owner. Only the independent bounded-acquisition validator is isolated here.
    monkeypatch.setattr(acquisition, 'validate_filing_acquisition_policy', lambda *args: None)
    edgar, _ = prepared(duplicate=True)
    data = {'edgar': edgar}
    scope = validate_filing_material_policy(data, CONFIG)
    records = replay_health()
    for record in records:
        record.evidence['filing_material_scope'] = deepcopy(scope)
    run_replay(monkeypatch, data, records)
    assert scope['strict_complete'] is False and scope['scoped_complete'] is True
    assert scope['quarantined_rows'] == 4 and len(scope['quarantined_accessions']) == 3
    edgar['filings'][0]['file_date'] = '2026-09-24'
    with pytest.raises(ValueError, match='invalid_filing_material_policy'):
        run_replay(monkeypatch, data, records)


def test_report_recomputes_real_material_graph_without_publishing_narratives(native_evidence, monkeypatch):
    from test_filing_material_integration import prepared
    from tradingagents.strategies.modules import get_paper_trade_strategies
    from tradingagents.strategies.orchestration import filing_acquisition_validation as acquisition
    from tradingagents.strategies.orchestration.filing_material_validation import validate_filing_material_policy
    from tradingagents.strategies.orchestration.operational_report import render_operational_report
    monkeypatch.setattr(acquisition, 'validate_filing_acquisition_policy', lambda *args: None)
    repo, state, wire = native_evidence
    edgar, _ = prepared()
    coverage = edgar['filing_evidence']['coverage']
    coverage.update(parser_policy=CONFIG['filing_parser_policy'], acquisition_policy=CONFIG['filing_acquisition_policy'])
    scope = validate_filing_material_policy({'edgar': edgar}, CONFIG)
    rewrite_payload(next((state / 'source_inputs').glob('*.json')), lambda frozen: frozen.update(edgar=edgar))
    dependents = {s.name for s in get_paper_trade_strategies() if 'edgar' in s.data_sources}
    with sqlite3.connect(state / 'metrics_v2.sqlite3') as connection:
        for identity, raw in connection.execute('SELECT health_id,payload_json FROM strategy_health').fetchall():
            record = json.loads(raw)
            if record['strategy'] not in dependents:
                continue
            record['evidence']['filing_material_scope'] = deepcopy(scope)
            connection.execute('UPDATE strategy_health SET payload_json=? WHERE health_id=?',
                               (json.dumps(record), identity))
    _attempt(repo, wire)
    report = _report(repo)
    assert report['evidence_complete'] is True
    assert report['sources']['filing_material_scope'] == scope
    assert scope['strict_complete'] is False and scope['scoped_complete'] is True
    assert 'Synthetic full body' not in json.dumps(report)
    assert 'Narrative ' not in json.dumps(report)
    rendered = render_operational_report(report)
    for quarantine in scope['quarantines']:
        assert quarantine['accession'] in rendered
        assert all(code in rendered for code in quarantine['gap_codes'])
