"""Disclosure-only policy is explicit across acquisition, screening and reports."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from test_operational_report import native_evidence, _attempt, _report, SESSION
from test_prospective_replay_integrity import rewrite_payload
from tradingagents.strategies.orchestration.congress_policy import POLICY, declaration

CONFIG = {'congress_disclosure_policy': POLICY,
          'disabled_strategies': {'congressional_trades': POLICY}}


@pytest.mark.parametrize('config', [
    {'congress_disclosure_policy': POLICY},
    {'congress_disclosure_policy': 'unknown'},
    {'disabled_strategies': {'congressional_trades': POLICY}},
])
def test_policy_requires_matching_explicit_signal_exclusion(config):
    from tradingagents.strategies.orchestration.congress_policy import configured
    with pytest.raises(ValueError, match='congress'):
        configured(config)


def test_public_registry_needs_no_fmp_key(monkeypatch):
    from tradingagents.strategies.data_sources.registry import build_default_registry
    monkeypatch.delenv('FMP_API_KEY', raising=False)
    source = build_default_registry(CONFIG).get('congress')
    assert source.is_available() and source.requires_api_key is False


def test_engine_forwards_full_filing_date_window_and_original_deadline():
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    calls = []
    source = SimpleNamespace(get_audit_snapshot=lambda **kw: calls.append(kw) or {'audit_snapshot': 'captured'})
    engine = MultiStrategyEngine.__new__(MultiStrategyEngine)
    engine.ar_config = deepcopy(CONFIG)
    engine.registry = SimpleNamespace(get=lambda name: source)
    assert engine._fetch_congress_data('2026-10-09', 123.0) == {'audit_snapshot': 'captured'}
    assert calls == [{'date_filed_after': '2026-09-09', 'date_filed_before': '2026-10-09', 'absolute_deadline': 123.0}]


def test_disabled_strategy_never_screens_and_audit_does_not_enter_context(tmp_path, monkeypatch):
    from tradingagents.strategies.modules.congressional_trades import CongressionalTradesStrategy
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    engine = MultiStrategyEngine(config={'autoresearch': {**deepcopy(CONFIG), 'state_dir': str(tmp_path)}},
        strategies=[CongressionalTradesStrategy()])
    def forbidden(*args, **kwargs):
        pytest.fail('Congress display rows must not screen or invoke model enrichment')
    monkeypatch.setattr(engine.paper_trade_strategies[0], 'screen', forbidden)
    monkeypatch.setattr(engine, '_enrich_with_llm', forbidden)
    def regime(data):
        assert 'congress' not in data and '_congress_disclosure_policy' not in data
        return {}
    monkeypatch.setattr(engine, '_build_regime_model', regime)
    data = {**declaration(CONFIG), 'congress': {'error': 'audit source unavailable'}}
    original = deepcopy(data)
    signals, _, health = engine.screen_and_enrich('2026-10-09', data, epoch_id='epoch', policy_id='policy')
    assert signals == [] and data == original
    assert health[0].status == 'disabled_by_policy' and health[0].signal_count == 0
    assert health[0].evidence['disclosure_policy'] == POLICY


@pytest.mark.parametrize('declare', [True, False])
def test_report_requires_frozen_declaration_for_disabled_congress_and_preserves_audit_failure(native_evidence, declare):
    repo, state, wire = native_evidence
    with sqlite3.connect(state/'metrics_v2.sqlite3') as connection:
        for identity, raw in connection.execute('SELECT health_id,payload_json FROM strategy_health').fetchall():
            value = json.loads(raw)
            if value['strategy'] == 'congressional_trades':
                value['status'] = 'disabled_by_policy'
                value['evidence'].update(reason=POLICY, disclosure_policy=POLICY)
                connection.execute('UPDATE strategy_health SET payload_json=? WHERE health_id=?', (json.dumps(value), identity))
    path = next((state/'source_inputs').glob('*.json'))
    def change(data):
        data['congress'] = {'error': 'audit source unavailable'}
        if declare:
            data.update(declaration(CONFIG))
    rewrite_payload(path, change)
    _attempt(repo, wire)
    report = _report(repo)
    assert any(row['provider'] == 'congress' for row in report['sources']['unresolved'])
    assert report['input_coverage_valid'] is declare
    assert ('congressional_trades' in report['disabled_strategies']) is declare
    assert report['evidence_complete'] is declare


def test_report_accepts_full_evidence_capacity_without_truncating(tmp_path):
    from tradingagents.strategies.orchestration.operational_report import _accepted_envelope
    from tradingagents.strategies.orchestration.source_inputs import SourceInputStore, source_codec_limits
    limits = source_codec_limits({'autoresearch': CONFIG}, source='congress')
    assert limits and source_codec_limits({'autoresearch': CONFIG}, source='fred') == {}
    payload = {'congress': {'opaque_test_data': 'x' * (17 * 1024 * 1024)}, **declaration(CONFIG)}
    encoded = SourceInputStore.encode(payload, **limits)
    envelope = {'version': 'source-inputs-v1', 'identity': {'generation': 'gen', 'session': SESSION,
        'commit': 'commit', 'configuration': 'configuration'}, 'payload': payload,
        'digest': hashlib.sha256(encoded.encode()).hexdigest(),
        'acquired_at': datetime(2026, 10, 6, tzinfo=timezone.utc)}
    path = tmp_path/'full-evidence.json'
    path.write_text(SourceInputStore.encode(envelope, **limits))
    assert _accepted_envelope(path, 'gen', SESSION, 'commit')['payload'] == payload


@pytest.mark.parametrize('broken', [{'audit_snapshot': 'broken'}, {'trades': [{'ticker': 'AAPL'}]}, {}])
def test_report_revalidates_declared_audit_payload(native_evidence, broken):
    repo, state, wire = native_evidence
    path = next((state/'source_inputs').glob('*.json'))
    def change(data):
        data.update(declaration(CONFIG))
        data['congress'] = deepcopy(broken)
    rewrite_payload(path, change)
    _attempt(repo, wire)
    report = _report(repo)
    assert report['evidence_complete'] is False
    assert {'code': 'accepted_sources_invalid'} in report['diagnostics']


def test_report_rejects_durable_audit_summary_without_frozen_snapshot(native_evidence, monkeypatch):
    repo, state, wire = native_evidence
    from test_congress_disclosure_audit import module, synthetic_raw, install_transport, fetch
    adapter = module()
    install_transport(monkeypatch, adapter, synthetic_raw())
    summary = adapter.audit_summary(fetch(adapter))
    with sqlite3.connect(state/'metrics_v2.sqlite3') as connection:
        for identity, raw in connection.execute('SELECT health_id,payload_json FROM strategy_health').fetchall():
            value = json.loads(raw)
            if value['strategy'] == 'congressional_trades':
                value['status'] = 'disabled_by_policy'
                value['evidence'].update(reason=POLICY, disclosure_policy=POLICY,
                    source_scope_limits={'congress': summary})
                connection.execute('UPDATE strategy_health SET payload_json=? WHERE health_id=?', (json.dumps(value), identity))
    path = next((state/'source_inputs').glob('*.json'))
    rewrite_payload(path, lambda data: data.update(**declaration(CONFIG), congress={'error': 'audit source unavailable'}))
    _attempt(repo, wire)
    report = _report(repo)
    assert {'code': 'congress_audit_scope_conflict'} in report['diagnostics']
    assert report['evidence_complete'] is False


@pytest.mark.parametrize('omit_horizon', [False, True])
def test_report_requires_audit_summary_in_every_horizon(native_evidence, omit_horizon):
    from datetime import date, timedelta
    from test_congress_replay_health import frozen_payload
    from test_congress_disclosure_audit import NOW
    from tradingagents.strategies.data_sources import congress_disclosure_audit as audit
    from tradingagents.strategies.orchestration.source_inputs import SourceInputStore
    repo, state, wire = native_evidence
    original = frozen_payload()['audit_snapshot']
    payload = audit._assemble(original['raw_artifacts'], revision=original['publisher']['revision'],
        start=(date.fromisoformat(SESSION)-timedelta(days=30)).isoformat(), end=SESSION,
        acquired_at=NOW.isoformat())
    summary = audit.audit_summary(payload)
    path = next((state/'source_inputs').glob('*.json'))
    document = SourceInputStore.decode(path.read_text())
    document['payload'].update(**declaration(CONFIG), congress=payload)
    document['acquired_at'] = NOW
    document['digest'] = hashlib.sha256(SourceInputStore.encode(document['payload']).encode()).hexdigest()
    path.write_text(SourceInputStore.encode(document))
    with sqlite3.connect(state/'metrics_v2.sqlite3') as connection:
        for identity, raw in connection.execute('SELECT health_id,payload_json FROM strategy_health').fetchall():
            value = json.loads(raw)
            if value['strategy'] == 'congressional_trades':
                value['status'] = 'disabled_by_policy'
                value['evidence'].update(reason=POLICY, disclosure_policy=POLICY)
                if not omit_horizon or value['policy_id'] != 'foundation-30d':
                    value['evidence']['source_scope_limits'] = {'congress': summary}
                connection.execute('UPDATE strategy_health SET payload_json=? WHERE health_id=?', (json.dumps(value), identity))
    for name, value in wire.items():
        if not omit_horizon or not name.startswith('horizon_30d_'):
            value['source_scope_limits'] = {'congress': summary}
    _attempt(repo, wire)
    report = _report(repo)
    assert report['evidence_complete'] is not omit_horizon
    if omit_horizon:
        assert {'code': 'congress_audit_scope_conflict', 'scope': 'foundation-30d'} in report['diagnostics']
