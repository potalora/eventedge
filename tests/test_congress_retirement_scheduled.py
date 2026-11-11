"""Constructed external IO; real controller, amendment and sixteen-book pipeline."""
import json
from pathlib import Path
import subprocess

import pytest

from scheduled_acceptance.harness import GENERATION, create_harness

pytestmark = pytest.mark.scheduled_acceptance


def test_retirement_preserves_books_and_fills_prior_intents_without_congress_io(tmp_path):
    from scripts.retire_congress import retire_congress
    from tradingagents.strategies.orchestration.operational_report import build_operational_report
    harness = create_harness(tmp_path, profile_variant='stabilization_v3')
    first = harness.run('2026-10-14T22:00:00+00:00', fault='congress_recent_audit_gap')
    assert first.returncode == 0, first.stdout + first.stderr
    before_report = harness.report('2026-10-14')
    assert before_report['completion']['workflow_complete'] is True
    assert before_report['sources']['unresolved'][0]['provider'] == 'congress'
    old_intents = harness.book_rows('order_intents')
    assert len(old_intents) == 16 and all(rows for rows in old_intents.values())
    before = {path: path.read_bytes() for path in [harness.profile,
        *harness.state.glob('*/portfolio.db'), *harness.state.rglob('source_inputs/**/*.json'),
        *harness.repo.glob('docs/reports/*2026-10-14*')] if path.is_file()}
    trace_offset = len(harness.trace())
    subprocess.run(['git', '-C', str(harness.repo), '-c', 'user.name=Fixture',
        '-c', 'user.email=fixture@example.invalid', 'commit', '--allow-empty',
        '-m', 'Recorded Congress retirement boundary'], check=True, capture_output=True)
    commit = subprocess.check_output(['git', '-C', str(harness.repo), 'rev-parse', 'HEAD'], text=True).strip()
    parameters = dict(repo=harness.repo, generation=GENERATION, effective_session='2026-10-15',
        old_commit=harness.commit, old_profile_sha256=harness.audit['fixture_sha256'], commit=commit)
    proposal = retire_congress(**parameters)
    retire_congress(**parameters, expected_profile_sha256=proposal['new_profile_sha256'],
                    apply=True, scheduler_quiesced=True)
    assert all(path.read_bytes() == body for path, body in before.items())
    historical = build_operational_report(harness.repo, GENERATION, '2026-10-14')
    from tradingagents.strategies.orchestration.source_incidents import load_source_incidents
    historical['source_incidents'] = load_source_incidents(historical, harness.repo/'docs/reports')
    assert historical == before_report
    projected = subprocess.run([str(harness.python), str(harness.repo/'scripts/source_repair.py'),
        'interventions', '--repo-root', str(harness.repo)], cwd=harness.repo, env=harness.env,
        capture_output=True, text=True, timeout=60)
    assert projected.returncode == 0, projected.stdout + projected.stderr
    assert json.loads(projected.stdout)['enrichment_monitors'][0]['status'] == 'complete'
    assert not any(row['kind'] in {'http', 'sdk', 'model'} for row in harness.trace()[trace_offset:])
    second = harness.run('2026-10-15T22:00:00+00:00', fault='congress_recent_audit_gap')
    assert second.returncode == 0, second.stdout + second.stderr
    report = harness.report('2026-10-15')
    assert report['generation_commit'] == commit
    assert report['profile_sha256'] == proposal['new_profile_sha256']
    assert report['epoch_id'] != before_report['epoch_id']
    assert report['outcome'] == 'clean', report
    assert all(report[key] is True for key in
        ('evidence_complete', 'accounting_valid', 'staging_complete', 'staging_valid', 'input_coverage_valid'))
    assert report['completion']['workflow_complete'] is True
    assert report['completion']['audit_warnings'] == []
    assert 'congress' not in report['sources']['observations']
    assert report['sources']['declared_exclusions']['congressional_trades'] == 'congress_source_retired_v1'
    assert report['sources']['recovered'] == []
    assert report['source_incidents']['open_incidents'] == []
    status = json.loads((harness.repo/'data/runtime_recovery/status.json').read_text())
    assert len(status['incidents']) == 2
    assert {row['session'] for row in status['incidents']} == {'2026-10-14', '2026-10-15'}
    assert all(row['reason_code'] == 'complete' for row in status['incidents'])
    for name, rows in harness.book_rows('fills').items():
        filled = [row for row in rows if row['session'] == '2026-10-15' and row['side'] == 'buy']
        assert filled and all(row['quantity'] > 0 for row in filled)
        assert {row['intent_id'] for row in filled} == {
            row['intent_id'] for row in old_intents[name] if row['side'] == 'buy'}
    for horizon in harness.accepted_screening('2026-10-15')['horizons'].values():
        congress, = [row for row in horizon['health'] if row['strategy'] == 'congressional_trades']
        assert congress['signal_count'] == 0
        assert congress['evidence']['reason'] == 'congress_source_retired_v1'
        assert not any(row['strategy'] == 'congressional_trades' for row in horizon['signals'])
    new_trace = harness.trace()[trace_offset:]
    assert not any(row['kind'] in {'forbidden_network', 'unexpected_httpx'} for row in new_trace)
    assert not any(any(domain in row.get('url', '').lower() for domain in
        ('quiverquant', 'house.gov', 'senate.gov', 'congress.gov')) for row in new_trace)
    assert harness.profile.read_bytes() == before[harness.profile]
    for path, body in before.items():
        if path.suffix != '.db':
            assert path.read_bytes() == body
    # Older signals stay bound to their original epoch; pending outcomes are not
    # reassigned to the amendment's new execution identity.
    signals = harness.book_rows('signals')
    assert len(signals) == 16
    assert all(any(row['epoch_id'] == before_report['epoch_id'] for row in rows) for rows in signals.values())
    harness.commit = commit
    readiness = harness.readiness('2026-10-15', sessions=2)
    assert readiness['ready'] is True and readiness['operational_continuity_complete'] is True, readiness
    old_window = harness.readiness('2026-10-14', sessions=1)
    assert old_window['sessions'][0]['ready'] is True
    assert old_window['ready'] is False
