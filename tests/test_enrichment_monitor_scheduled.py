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
def test_query_enrichment_is_visible_without_repair_or_report_rewrite(tmp_path, missing_symbol):
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
