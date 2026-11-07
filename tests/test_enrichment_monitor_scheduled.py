"""Offline composed acceptance of enrichment observation through trusted refresh."""
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest

from scheduled_acceptance.harness import create_harness, GENERATION

pytestmark = pytest.mark.scheduled_acceptance


@pytest.mark.parametrize('missing_symbol', [True, False])
def test_query_enrichment_is_visible_without_repair_or_report_rewrite(tmp_path, missing_symbol, monkeypatch, capsys):
    from tradingagents.strategies.data_sources.finra_bulk import SCHEMA
    harness = create_harness(tmp_path, profile_variant='stabilization_v2')
    # Constructed external API rows are stored in a disposable fixture DB.
    # They are not actual provider evidence or an adapter cache.
    if missing_symbol:
        with sqlite3.connect(Path(harness.env['EVENTEDGE_ACCEPTANCE_BASE'])/'finra-response.db') as connection:
            connection.execute('CREATE TABLE short_interest ('+', '.join('"'+key+'" '+kind for key,kind in SCHEMA)+')')
            connection.execute('INSERT INTO short_interest VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (0,'SPY','Constructed ETF','N',1000,900,100000,.01,11.1,100,'2026-09-30'))
    run = harness.run('2026-10-14T22:00:00+00:00')
    assert run.returncode == 0, run.stdout + run.stderr
    report = harness.report('2026-10-14')
    assert report['completion']['workflow_complete'] is True
    assert report['completion']['continuity_eligible'] is (not missing_symbol)
    folder = harness.repo/'docs/reports'
    stem = folder/f'2026-10-14-{GENERATION}-operational-report'
    protected = {path: path.read_bytes() for path in (stem.with_suffix('.json'), stem.with_suffix('.md'))}
    inputs = {path: path.read_bytes() for path in (harness.state/'source_inputs').rglob('*.json')}
    before_io = [row for row in harness.trace() if row['kind'] in {'http','sdk','model'}]
    if not missing_symbol:
        # Exercise the transient result against the same accepted sixteen-book
        # evidence that the next, fresh CLI process must publish and verify.
        from datetime import datetime
        from scripts import source_repair as cli
        from tradingagents.strategies.orchestration import source_repair as contract
        original_verify = contract._Capture.verify
        def replace_before_verify(capture):
            if capture.root == folder:
                path = stem.with_suffix('.json')
                replacement = path.with_suffix('.replacement')
                replacement.write_bytes(protected[path])
                replacement.replace(path)
            return original_verify(capture)
        with monkeypatch.context() as patch:
            patch.setattr(contract, '_now', lambda *_: datetime.fromisoformat('2026-10-14T22:00:00+00:00'))
            patch.setattr(contract._Capture, 'verify', replace_before_verify)
            assert cli.main(['interventions', '--repo-root', str(harness.repo)]) == 75
            assert 'evidence changed during capture' in capsys.readouterr().err
    result = subprocess.run([str(harness.python), str(harness.repo/'scripts/source_repair.py'),
        'interventions', '--repo-root', str(harness.repo)], cwd=harness.repo, env=harness.env,
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    projected = json.loads(result.stdout)
    monitor, = projected['enrichment_monitors']
    assert monitor['status'] == ('degraded' if missing_symbol else 'complete'), monitor
    value = json.loads(Path(monitor['path']).read_text())
    assert value['automatic_repair_supported'] is False
    assert value['workflow_complete'] is True and value['continuity_eligible'] is (not missing_symbol)
    assert bool(value['failures']) is missing_symbol
    assert all(row['provider']=='finra' and row['operation']=='short_interest'
        and row['reason_code']=='provider_error' for row in value['failures'])
    receipt = value['short_interest_acquisition']
    assert receipt['schema_version'] == 4 and receipt['attempts']
    attempt = receipt['attempts'][-1]
    assert attempt['query_coverage']['current_complete'] is (not missing_symbol)
    assert all(batch['http_status'] == 200 and batch['response_sha256']
        and batch['record_total'] == batch['row_count'] and batch['record_offset'] == 0
        for batch in attempt['batches'])
    assert value['accepted_enrichment_sha256']
    assert value['report_sha256'] == hashlib.sha256(protected[stem.with_suffix('.json')]+b'\0'+protected[stem.with_suffix('.md')]).hexdigest()
    assert {path:path.read_bytes() for path in protected} == protected
    assert {path:path.read_bytes() for path in inputs} == inputs
    assert [row for row in harness.trace() if row['kind'] in {'http','sdk','model'}] == before_io
    assert not list((harness.repo/'.triggers/source-repair/inbox').glob('*.json'))
    # Verification derives both expected artifacts again without publishing.
    verifier = [str(harness.python), str(harness.repo/'scripts/source_repair.py'),
                'verify-projections', '--repo-root', str(harness.repo)]
    checked = subprocess.run(verifier, cwd=harness.repo, env=harness.env, capture_output=True, text=True, timeout=60)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    verified = json.loads(checked.stdout)
    assert verified['version'] == 'eventedge-verified-projections-v1'
    row, = verified['generations']
    assert row['source']['sha256'] == hashlib.sha256(Path(projected['proposals'][0]['path']).read_bytes()).hexdigest()
    assert row['enrichment']['sha256'] == hashlib.sha256(Path(monitor['path']).read_bytes()).hexdigest()
    assert row['enrichment']['status'] == monitor['status']
    value['healthy_scope'] = [{'provider':'openbb','operation':'profiles','symbol':'FORGED'}]
    changed = (json.dumps(value, sort_keys=True, separators=(',', ':'))+'\n').encode()
    Path(monitor['path']).write_bytes(changed)
    refused = subprocess.run(verifier, cwd=harness.repo, env=harness.env, capture_output=True, text=True, timeout=60)
    assert refused.returncode == 2, refused.stdout + refused.stderr
    assert Path(monitor['path']).read_bytes() == changed


def test_reported_court_hold_preserves_full_book_failure_and_survives_verification(tmp_path):
    harness = create_harness(tmp_path, profile_variant='stabilization_v3')
    harness.run('2026-10-14T22:00:00+00:00', fault='court_exhausted_issuer_pages', timeout=360)
    report = harness.report('2026-10-14')
    assert report['diagnostics'] == [{'code':'supported_source_scope_invalid'}]
    assert report['evidence_complete'] is report['completion']['workflow_complete'] is False
    before = [row for row in harness.trace() if row['kind'] in {'http','sdk','model'}]
    def cli(command):
        return subprocess.run([str(harness.python), str(harness.repo/'scripts/source_repair.py'), command,
            '--repo-root', str(harness.repo)], cwd=harness.repo, env=harness.env, capture_output=True, text=True, timeout=60)
    result = cli('interventions')
    assert result.returncode == 0, result.stdout + result.stderr
    monitor, = json.loads(result.stdout)['enrichment_monitors']
    value = json.loads(Path(monitor['path']).read_bytes())
    assert value['version'] == 'eventedge-enrichment-monitor-v2' and value['status'] == 'held'
    assert value['diagnostics'] == ['supported_source_scope_invalid']
    assert value['profile_sha256'] == report['profile_sha256']
    assert value['completion_reason_codes'] == report['completion']['reason_codes']
    assert value['healthy_scope'] == value['failures'] == value['hold_reasons'] == []
    assert value['automatic_repair_supported'] is value['continuity_eligible'] is False
    assert value['accepted_enrichment_sha256'] is value['short_interest_acquisition'] is None
    verified = cli('verify-projections')
    assert verified.returncode == 0, verified.stdout + verified.stderr
    row, = json.loads(verified.stdout)['generations']
    assert row['enrichment']['status'] == 'held' and row['workflow_complete'] is False
    assert [row for row in harness.trace() if row['kind'] in {'http','sdk','model'}] == before
    # Removing accepted decision proof must invalidate a previously valid held artifact.
    slot, = (harness.state/'source_inputs/decision_inputs').glob('*.json')
    slot.unlink()
    assert cli('verify-projections').returncode == 2
