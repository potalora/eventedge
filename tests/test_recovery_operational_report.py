"""Distinct local recovery proof preserves failed original daily evidence."""
import hashlib
import json
from pathlib import Path

import pytest

from test_source_reliability_pipeline import pipeline, SESSION, GENERATION, COMMIT, InterruptedWorker
from test_runtime_recovery import prospective
from tradingagents.strategies.orchestration import runtime_recovery as recovery
from tradingagents.strategies.orchestration.operational_report import build_operational_report, render_operational_report
from tradingagents.strategies.orchestration.cohort_orchestrator import CohortOrchestrator, build_default_cohorts


def build_recovered(pipeline, monkeypatch, *, original='failed', include_preflight=False):
    fixture, unused_owner, config, repo = prospective(pipeline, monkeypatch)
    from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger
    complete = PortfolioLedger.complete_staging
    last_name = unused_owner.cohorts[-1]['config'].name
    def interrupt(ledger, *args, **kwargs):
        if ledger.cohort_id == last_name:
            raise InterruptedWorker('real interruption after committee, before final staging commit')
        return complete(ledger, *args, **kwargs)
    monkeypatch.setattr(PortfolioLedger, 'complete_staging', interrupt)
    if include_preflight:
        fixture.fault = 'edgar_timeout'
        fixture.manager.run_preflight(SESSION.isoformat(), mode='screen')
        fixture.fault = None
    if original == 'failed':
        result = fixture.manager.run_daily(SESSION.isoformat())[GENERATION]
        assert result['outcome'] == 'failed'
    else:
        with pytest.raises(InterruptedWorker):
            unused_owner.run_daily(SESSION.isoformat())
    monkeypatch.setattr(PortfolioLedger, 'complete_staging', complete)
    owner = CohortOrchestrator(build_default_cohorts(config), config,
        generation_id=GENERATION, generation_commit=COMMIT)
    state = Path(config['autoresearch']['state_dir'])
    profile = state / 'generation-config.json'
    profile.write_text(json.dumps({'autoresearch': {
        'decision_clock_policy': 'prospective_next_open_v1',
        'research_resilience_policy': 'dependency_isolation_v1'}}))
    profile_sha = hashlib.sha256(profile.read_bytes()).hexdigest()
    manifest_path = repo / 'data/generations/manifest.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['generations'][0].update(config_file=str(profile), config_sha256=profile_sha)
    manifest_path.write_text(json.dumps(manifest))
    original_manifest = manifest_path.read_bytes()
    original_attempts = {path.name: path.read_bytes() for path in (repo / 'data/logs/run_attempts').glob('*.json')}
    calls, models = fixture.calls.copy(), fixture.model_calls.copy()
    fixture.block_all = True
    try:
        result = recovery.recover_owner(owner, SESSION, now=fixture.now)
        assert result['disposition'] == 'recovered', result
        path = recovery.write_recovery_receipt(owner, SESSION, now=fixture.now,
            configuration_sha256=profile_sha)
    finally:
        for cohort in owner.cohorts:
            cohort['ledger'].close()
    assert fixture.calls == calls and fixture.model_calls == models and not fixture.blocked_calls
    assert manifest_path.read_bytes() == original_manifest
    assert {path.name: path.read_bytes() for path in (repo / 'data/logs/run_attempts').glob('*.json')} == original_attempts
    return fixture, config, repo, state, path, original_manifest, original_attempts


@pytest.mark.parametrize('original', ['failed', 'missing'])
def test_real_recovery_report_uses_distinct_proof_and_preserves_original_attempt(pipeline, monkeypatch, original):
    fixture, config, repo, state, path, manifest, attempts = build_recovered(pipeline, monkeypatch, original=original, include_preflight=(original == 'failed'))
    report = build_operational_report(repo, GENERATION, str(SESSION), snapshot_guaranteed=True)
    assert report['evidence_complete'] and report['accounting_valid'] and report['staging_complete'], report['diagnostics']
    assert report['outcome'] == 'clean' and report['input_coverage_valid'] and report['staging_valid']
    assert report['recovery']['books_count'] == 16
    assert report['recovery']['original_daily_outcome'] == original
    assert report['attempt_counts']['daily'] == (1 if original == 'failed' else 0)
    if original == 'failed':
        assert next(row for row in report['attempts'] if row['action'] == 'daily')['outcome'] == 'failed'
        assert report['attempt_counts']['preflight'] == 1 and report['preflight_incidents']
    else:
        assert {row['code'] for row in report['recovery']['original_attempt_diagnostics']} == {
            'daily_attempt_missing', 'manifest_session_history_missing'}
    markdown = render_operational_report(report)
    assert 'Accepted local recovery completed' in markdown and 'original daily attempt outcome: ' + original in markdown
    before = path.read_bytes()
    again = build_operational_report(repo, GENERATION, str(SESSION), snapshot_guaranteed=True)
    assert again == report and path.read_bytes() == before
    assert (repo / 'data/generations/manifest.json').read_bytes() == manifest
    assert {item.name: item.read_bytes() for item in (repo / 'data/logs/run_attempts').glob('*.json')} == attempts
    assert not fixture.blocked_calls


@pytest.mark.parametrize('damage', ['receipt', 'source', 'committee', 'profile', 'cash'])
def test_recovery_report_rejects_changed_receipt_or_accepted_proof(pipeline, monkeypatch, damage):
    fixture, config, repo, state, path, manifest, attempts = build_recovered(pipeline, monkeypatch)
    if damage == 'receipt':
        value = json.loads(path.read_text())
        value['proof_sha256'] = '0' * 64
        path.write_text(json.dumps(value))
    elif damage == 'source':
        source = next((state / 'source_inputs').glob('*.json'))
        source.write_text(source.read_text() + ' ')
    elif damage == 'profile':
        (state / 'generation-config.json').write_text('{}')
    else:
        import sqlite3
        book = next(state.glob('horizon_*/portfolio.db'))
        with sqlite3.connect(book) as connection:
            if damage == 'cash':
                connection.execute("UPDATE cash_events SET amount='1.00' WHERE session=?", (str(SESSION),))
            else:
                connection.execute("UPDATE committee_decisions SET payload_json='{}'")
    report = build_operational_report(repo, GENERATION, str(SESSION), snapshot_guaranteed=True)
    assert report['evidence_complete'] is False and report['performance_claims_withheld'] is True
    assert {'code': 'recovery_receipt_invalid'} in report['diagnostics']
    assert 'recovery' not in report
    assert not fixture.blocked_calls
