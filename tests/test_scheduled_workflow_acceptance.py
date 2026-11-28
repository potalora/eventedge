"""Release gate: actual scheduled shell, frozen gemini4-clean profile and 16 books."""
import json
from pathlib import Path
import subprocess

import pytest

from scheduled_acceptance.harness import ORIGINAL_SHA, create_harness

pytestmark = pytest.mark.scheduled_acceptance

_ACTIVE_STRATEGIES = [
    'earnings_call',
    'supply_chain',
    'govt_contracts',
    'weather_ag',
    'commodity_macro',
]


def assert_complete(harness, session):
    report = harness.report(session)
    assert report['accounting_valid'] and report['staging_complete'], report
    assert report['outcome'] == 'clean', report
    assert report['input_coverage_valid'] is True and report['evidence_complete'] is True
    assert report['diagnostics'] == []
    assert report['sources']['unresolved'] == []
    assert report['source_health_failures'] == []
    snapshots = harness.book_rows('account_snapshots')
    staging = harness.book_rows('staging_runs')
    assert len(snapshots) == len(staging) == 16
    assert all(any(row['session'] == session for row in rows) for rows in snapshots.values())
    assert all(any(row['session'] == session for row in rows) for rows in staging.values())
    assert sum(len(rows) for rows in harness.book_rows('order_intents').values()) > 0
    trace = harness.trace()
    assert not any(row['kind'] in {'forbidden_network', 'unexpected_httpx'} for row in trace)
    expected = {'analysis': 'gemini-4-neon', 'committee': 'gemini-4-neon'}
    for role, model in expected.items():
        matching = [row for row in trace if row['kind'] == 'model' and row['role'] == role]
        assert matching, role
        assert all(row['model'] == model and row['effort'] == 'high' for row in matching)
    screening = harness.accepted_screening(session)
    effective = harness.effective_config_proof()
    assert effective['blocked_tickers'] == ['GOOGL', 'GOOG']
    assert effective['enabled_strategies'] == _ACTIVE_STRATEGIES
    assert effective['thesis_model'] == 'gemini-4-neon'
    assert effective['portfolio_committee_model'] == 'gemini-4-neon'
    assert screening['binding']['source_identity']['configuration'] == effective['source_configuration']
    assert set(screening['horizons']) == {'30d', '3m', '6m', '1y'}
    for horizon in screening['horizons'].values():
        assert {row['strategy'] for row in horizon['health']} == set(_ACTIVE_STRATEGIES)
        assert all(row['status'] != 'data_failure' for row in horizon['health'])
    workers = [row for row in trace if row['kind'] == 'bootstrap' and any('run_cohorts.py' in arg for arg in row['argv'])]
    assert len({row['pid'] for row in workers}) >= 2
    assert any(row['kind'] == 'bootstrap' and any('transport_worker.py' in arg for arg in row['argv']) for row in trace)
    assert harness.audit['original_sha256'] == ORIGINAL_SHA
    assert harness.audit['fixture_sha256'] == ORIGINAL_SHA
    assert harness.audit['policy_overrides'] == []
    assert harness.audit['candidate_profile_sha256'] == ORIGINAL_SHA
    return report


def test_retained_compliance_environment_reaches_real_frozen_worker_config(tmp_path):
    harness = create_harness(tmp_path)
    assert (harness.repo / '.env').read_text() == '# Offline acceptance: no ambient dotenv configuration.\n'
    effective = harness.effective_config_proof()
    assert effective['blocked_tickers'] == ['GOOGL', 'GOOG']
    assert effective['enabled_strategies'] == _ACTIVE_STRATEGIES
    assert not [row for row in harness.trace() if row['kind'] in {'http', 'sdk', 'model', 'forbidden_network', 'unexpected_httpx'}]


def test_manager_dotenv_stops_at_disposable_repository_boundary(tmp_path):
    harness = create_harness(tmp_path)
    # This file is entirely test-owned. The manager must retain normal dotenv
    # loading while its empty repository file prevents ancestor discovery.
    (harness.repo.parent / '.env').write_text('EVENTEDGE_ACCEPTANCE_ANCESTOR_CANARY=harmless-test-sentinel\n')
    result = harness.run('2026-10-14T15:00:00+00:00', entrypoint='governed')
    assert result.returncode != 0
    assert any('governed status not_ready' in row.get('stderr', '') for row in harness.attempts())
    workers = [
        row
        for row in harness.trace()
        if row['kind'] == 'bootstrap' and any('run_cohorts.py' in arg for arg in row['argv'])
    ]
    assert workers
    assert all(row['ancestor_dotenv_present'] is False for row in workers)
    assert not [row for row in harness.trace() if row['kind'] in {'http', 'sdk', 'model', 'forbidden_network', 'unexpected_httpx'}]


def test_real_evening_controller_shell_workers_and_all_sixteen_books(tmp_path):
    harness = create_harness(tmp_path)
    result = harness.run('2026-10-14T22:00:00+00:00')
    assert result.returncode == 0, result.stdout + result.stderr
    assert_complete(harness, '2026-10-14')
    staged = harness.book_rows('order_intents')
    assert len(staged) == 16 and all(
        any(row['side'] == 'buy' and row['requested_qty'] > 0 and row['eligible_session'] == '2026-10-15' for row in rows)
        for rows in staged.values()
    )
    signals = harness.book_rows('signals')
    links = harness.book_rows('intent_signals')
    for name, rows in staged.items():
        buys = {row['intent_id'] for row in rows if row['side'] == 'buy'}
        signal_ids = {row['signal_id'] for row in links[name] if row['intent_id'] in buys}
        assert signal_ids and all(row['ticker'] == 'NVDA' for row in signals[name] if row['signal_id'] in signal_ids)
    contexts = harness.book_rows('policy_session_contexts')
    for rows in contexts.values():
        envelope = next(json.loads(row['payload_json']) for row in rows if row['record_id'].endswith(':staging'))
        context = json.loads(envelope['context_json']) if 'context_json' in envelope else envelope.get('context', envelope)
        decision = context['decision_clock']
        assert decision['policy'] == 'prospective_next_open_v1'
        assert decision['reference_session'] == '2026-10-14' and decision['eligible_session'] == '2026-10-15'
        assert decision['cutoff'] == '2026-10-14T22:00:00+00:00'
    assert all(not rows for rows in harness.book_rows('fills').values())
    initial_inputs = {str(path): path.read_bytes() for path in (harness.state / 'source_inputs').rglob('*.json')}
    next_day = harness.run('2026-10-15T22:00:00+00:00')
    assert next_day.returncode == 0, next_day.stdout + next_day.stderr
    assert_complete(harness, '2026-10-15')
    assert all(Path(path).read_bytes() == body for path, body in initial_inputs.items())
    fills = harness.book_rows('fills')
    assert len(fills) == 16 and all(
        any(row['side'] == 'buy' and row['session'] == '2026-10-15' and row['quantity'] > 0 for row in rows)
        for rows in fills.values()
    )
    for name, rows in fills.items():
        assert {row['intent_id'] for row in rows if row['session'] == '2026-10-15'} == {
            row['intent_id'] for row in staged[name] if row['side'] == 'buy'
        }
    before_books = {
        table: harness.book_rows(table)
        for table in ('fills', 'cash_events', 'account_snapshots', 'staging_runs', 'order_intents', 'committee_decisions')
    }
    before_inputs = {str(path): path.read_bytes() for path in (harness.state / 'source_inputs').rglob('*.json')}
    before_io = [row for row in harness.trace() if row['kind'] in {'http', 'sdk', 'model'}]
    repeat = harness.run('2026-10-15T22:05:00+00:00')
    assert repeat.returncode == 0, repeat.stdout + repeat.stderr
    assert {table: harness.book_rows(table) for table in before_books} == before_books
    assert {str(path): path.read_bytes() for path in (harness.state / 'source_inputs').rglob('*.json')} == before_inputs
    assert [row for row in harness.trace() if row['kind'] in {'http', 'sdk', 'model'}] == before_io


def test_same_day_failed_midday_screen_then_complete_evening(tmp_path):
    harness = create_harness(tmp_path)
    failed = harness.run('2026-10-14T15:45:00+00:00', entrypoint='preflight', fault='midday_usaspending_503')
    assert failed.returncode != 0, failed.stdout + failed.stderr
    assert not (harness.state / 'generation.sqlite3').exists()
    assert not any(row['kind'] == 'model' for row in harness.trace())
    incident_files = list((harness.repo / 'data/logs/run_attempts').rglob('*.json'))
    retained = {str(path): path.read_bytes() for path in incident_files}
    assert retained
    failed_attempt = harness.attempts()[-1]
    assert failed_attempt['result']['screen_runtime_failures'] == []
    failures = failed_attempt['result']['screen_source_failures']
    assert {row['source'] for row in failures} == {'usaspending'}
    usaspending = next(row for row in failures if row['source'] == 'usaspending')
    assert usaspending['http_status'] == 503 and usaspending['attempts'] >= 1
    assert any(row['kind'] == 'http' for row in harness.trace())
    evening = harness.run('2026-10-14T22:00:00+00:00')
    assert evening.returncode == 0, evening.stdout + evening.stderr
    report = assert_complete(harness, '2026-10-14')
    assert report['preflight_incidents'], report
    assert all(Path(path).read_bytes() == body for path, body in retained.items())


def test_every_child_denies_python_and_native_curl_network_boundaries(tmp_path):
    harness = create_harness(tmp_path)
    harness.scenario_path.write_text(json.dumps({'now': '2026-10-14T22:00:00+00:00', 'session': '2026-10-14', 'fault': None}))
    code = '''import asyncio,os,socket,tempfile
from pathlib import Path
assert Path(tempfile.gettempdir()) == Path(os.environ['EVENTEDGE_ACCEPTANCE_BASE'])/'tmp'
from curl_cffi import Curl,AsyncCurl,CurlOpt
def denied(call):
    try:
        call()
    except AssertionError as error:
        assert 'scheduled acceptance forbids' in str(error)
    else:
        raise AssertionError('network boundary was not blocked')
denied(lambda:socket.create_connection(('127.0.0.1',9)))
curl=Curl()
curl.setopt(CurlOpt.URL,b'http://127.0.0.1:9')
denied(curl.perform)
async def check_async():
    transport=AsyncCurl()
    try:
        denied(lambda:transport.add_handle(curl))
    finally:
        await transport.close()
asyncio.run(check_async())
curl.close()
print('all outbound boundaries denied before network IO')
'''
    result = subprocess.run([str(harness.python), '-c', code], env=harness.env, cwd=harness.repo, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    denied = [row for row in harness.trace() if row['kind'] == 'forbidden_network']
    assert len(denied) == 3 and sum(row.get('boundary') == 'native_curl' for row in denied) == 2
    assert not [row for row in harness.trace() if row['kind'] in {'http', 'sdk', 'model'}]


def test_gemini4_clean_profile_audit_and_effective_config_roundtrip(tmp_path):
    harness = create_harness(tmp_path)
    original = json.loads((Path(__file__).parents[1] / 'config/profiles/gemini4-clean.json').read_text())
    frozen = json.loads(harness.profile.read_text())
    assert frozen['enabled_strategies'] == _ACTIVE_STRATEGIES
    assert original['thesis_model'] == frozen['thesis_model'] == 'gemini-4-neon'
    assert original['bounded_model'] == frozen['bounded_model'] == 'gemini-4-hydrogen'
    assert original['llm_billing_mode'] == frozen['llm_billing_mode'] == 'api'
    assert harness.audit['original_sha256'] == ORIGINAL_SHA
    assert harness.audit['fixture_sha256'] == ORIGINAL_SHA
    assert harness.audit['profile_variant'] == 'gemini4_clean'
    assert harness.audit['policy_overrides'] == []
    assert harness.audit['substitutions'] == []
    effective = harness.effective_config_proof()
    assert effective['blocked_tickers'] == ['GOOGL', 'GOOG']
    assert effective['enabled_strategies'] == _ACTIVE_STRATEGIES
    assert not [row for row in harness.trace() if row['kind'] in {'http', 'sdk', 'model', 'forbidden_network', 'unexpected_httpx'}]


def test_controller_fails_closed_without_gemini_credentials_on_idle_tick(tmp_path):
    harness = create_harness(tmp_path)
    harness.env.pop('GEMINI_API_KEY', None)
    harness.env.pop('GOOGLE_API_KEY', None)
    result = harness.run('2026-10-14T15:00:00+00:00', entrypoint='controller')
    assert result.returncode == 1, result.stdout + result.stderr
    projection = json.loads(result.stdout)
    assert projection['status'] == 'operator_required'
    assert projection['controller_reason_code'] == 'auth_required'
    status_file = harness.repo / 'data/runtime_recovery/status.json'
    assert json.loads(status_file.read_text())['controller_reason_code'] == 'auth_required'
