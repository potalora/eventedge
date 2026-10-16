"""Release gate: actual scheduled shell, frozen prospective profile and16books."""
import json
from pathlib import Path
import subprocess

import pytest

from scheduled_acceptance.harness import create_harness, ORIGINAL_SHA

pytestmark = pytest.mark.scheduled_acceptance


def assert_complete(harness, session):
    report = harness.report(session)
    assert report['accounting_valid'] and report['staging_complete'], report
    # The exact deployed profile intentionally declares EDGAR unavailable;
    # docs/research/2026-10-12-bulk-filing-rollback.md documents this coverage
    # loss. The acceptance must retain it while unrelated earnings still trade.
    assert report['outcome']=='degraded', report
    assert report['input_coverage_valid'] is False and report['evidence_complete'] is True
    assert report['diagnostics']==[]
    assert {row.get('source',row.get('provider')) for row in report['sources']['unresolved']} == {'edgar'}
    assert {row['strategy'] for row in report['source_health_failures']} == {'filing_analysis','insider_activity','quantum_readiness'}
    snapshots = harness.book_rows('account_snapshots')
    staging = harness.book_rows('staging_runs')
    assert len(snapshots)==len(staging)==16
    assert all(any(row['session']==session for row in rows) for rows in snapshots.values())
    assert all(any(row['session']==session for row in rows) for rows in staging.values())
    assert sum(len(rows) for rows in harness.book_rows('order_intents').values()) > 0
    trace = harness.trace()
    assert not any(row['kind'] in {'forbidden_network','unexpected_httpx'} for row in trace)
    # Earnings analysis is a thesis request; bounded visual recognition uses
    # Luna while the frozen thesis and committee routes use Sol.
    expected = {'analysis':'gpt-6.1-sol','document_recognition':'gpt-6-luna',
                'document_verification':'gpt-6-luna','document_thesis':'gpt-6.1-sol','committee':'gpt-6.1-sol'}
    for role,model in expected.items():
        matching = [row for row in trace if row['kind']=='model' and row['role']==role]
        assert matching,role
        assert all(row['model']==model and row['effort']=='high' for row in matching)
    screening = harness.accepted_screening(session)
    effective = harness.effective_config_proof()
    assert effective['blocked_tickers']==['GOOGL','GOOG']
    expected_retrieval = ('verified_government_copy_v2' if harness.audit['profile_variant']=='government_copy_v2'
                          else 'verified_government_copy_v1')
    assert effective['regulation_retrieval_policy']==expected_retrieval
    assert report['sources']['regulation_source_coverage']['retrieval_policy']==expected_retrieval
    assert screening['binding']['source_identity']['configuration']==effective['source_configuration']
    assert set(screening['horizons'])=={'30d','3m','6m','1y'}
    for horizon in screening['horizons'].values():
        health = next(row for row in horizon['health'] if row['strategy']=='bounded_document_research')
        assert health['evidence']['candidate_count']==1 and health['status']!='data_failure'
        document = next(row for row in horizon['signals'] if row['strategy']=='bounded_document_research')
        assert document['ticker']=='CDRE' and document['direction']=='neutral' and document['journal_only']
        assert document['metadata']['analysis_status']=='validated'
        scope = document['metadata']['bounded_document_decision_scope']
        assert scope['effective_until']=='2026-10-17T03:59:59+00:00'
        assert scope['economic_date']=='2021-12-16' and scope['required_pages']==[18]
    workers = [row for row in trace if row['kind']=='bootstrap' and any('run_cohorts.py' in arg for arg in row['argv'])]
    assert len({row['pid'] for row in workers}) >= 3
    assert any(row['kind']=='bootstrap' and any('transport_worker.py' in arg for arg in row['argv']) for row in trace)
    assert harness.audit['original_sha256']==ORIGINAL_SHA
    assert harness.audit['fixture_sha256']!=ORIGINAL_SHA
    if harness.audit['profile_variant']=='retained_production':
        assert harness.audit['policy_overrides']==[]
        assert harness.audit['candidate_profile_sha256']==ORIGINAL_SHA
    return report


def test_retained_compliance_environment_reaches_real_frozen_worker_config(tmp_path):
    harness = create_harness(tmp_path)
    proof = harness.audit['retained_environment_policy']
    assert proof['environment']=={'BLOCKED_TICKERS':'GOOGL,GOOG'}
    assert proof['source']['attempt_started_at']=='2026-10-14T15:45:02.425392+00:00'
    assert proof['source']['stderr_excerpt'].endswith("Blocked tickers: ['GOOGL', 'GOOG']")
    assert (harness.repo/'.env').read_text()=='# Offline acceptance: no ambient dotenv configuration.\n'
    assert harness.effective_config_proof()['blocked_tickers']==['GOOGL','GOOG']
    assert not [row for row in harness.trace() if row['kind'] in {'http','sdk','model','forbidden_network','unexpected_httpx'}]


def test_manager_dotenv_stops_at_disposable_repository_boundary(tmp_path):
    harness = create_harness(tmp_path)
    # This file is entirely test-owned. The manager must retain normal dotenv
    # loading while its empty repository file prevents ancestor discovery.
    (harness.repo.parent/'.env').write_text('EVENTEDGE_ACCEPTANCE_ANCESTOR_CANARY=harmless-test-sentinel\n')
    result = harness.run('2026-10-14T15:00:00+00:00',entrypoint='governed')
    assert result.returncode != 0
    assert any('governed status not_ready' in row.get('stderr','') for row in harness.attempts())
    workers = [row for row in harness.trace() if row['kind']=='bootstrap'
               and any('run_cohorts.py' in arg for arg in row['argv'])]
    assert workers
    assert all(row['ancestor_dotenv_present'] is False for row in workers)
    assert not [row for row in harness.trace() if row['kind'] in {'http','sdk','model','forbidden_network','unexpected_httpx'}]


@pytest.mark.parametrize('profile_variant', ['retained_production', 'government_copy_v2'])
def test_real_evening_controller_shell_workers_and_all_sixteen_books(tmp_path, profile_variant):
    harness = create_harness(tmp_path, profile_variant=profile_variant)
    result = harness.run('2026-10-14T22:00:00+00:00')
    assert result.returncode==0, result.stdout+result.stderr
    assert_complete(harness,'2026-10-14')
    staged = harness.book_rows('order_intents')
    assert len(staged)==16 and all(any(row['side']=='buy' and row['requested_qty']>0 and row['eligible_session']=='2026-10-15' for row in rows) for rows in staged.values())
    signals = harness.book_rows('signals')
    links = harness.book_rows('intent_signals')
    for name,rows in staged.items():
        buys = {row['intent_id'] for row in rows if row['side']=='buy'}
        signal_ids = {row['signal_id'] for row in links[name] if row['intent_id'] in buys}
        assert signal_ids and all(row['ticker']=='NVDA' for row in signals[name] if row['signal_id'] in signal_ids)
    contexts = harness.book_rows('policy_session_contexts')
    for rows in contexts.values():
        context = next(json.loads(row['context_json']) for row in rows if row['binding_kind']=='staging')
        decision = context['decision_clock']
        assert decision['policy']=='prospective_next_open_v1'
        assert decision['reference_session']=='2026-10-14' and decision['eligible_session']=='2026-10-15'
        assert decision['cutoff']=='2026-10-14T22:00:00+00:00'
    assert all(not rows for rows in harness.book_rows('fills').values())
    initial_inputs = {str(path):path.read_bytes() for path in (harness.state/'source_inputs').rglob('*.json')}
    next_day = harness.run('2026-10-15T22:00:00+00:00')
    assert next_day.returncode==0,next_day.stdout+next_day.stderr
    assert_complete(harness,'2026-10-15')
    assert all(Path(path).read_bytes()==body for path,body in initial_inputs.items())
    fills = harness.book_rows('fills')
    assert len(fills)==16 and all(any(row['side']=='buy' and row['session']=='2026-10-15' and row['quantity']>0 for row in rows) for rows in fills.values())
    for name,rows in fills.items():
        assert {row['intent_id'] for row in rows if row['session']=='2026-10-15'} == {row['intent_id'] for row in staged[name] if row['side']=='buy'}
    before_books = {table:harness.book_rows(table) for table in
        ('fills','cash_events','account_snapshots','staging_runs','order_intents','committee_decisions')}
    before_inputs = {str(path):path.read_bytes() for path in (harness.state/'source_inputs').rglob('*.json')}
    before_io = [row for row in harness.trace() if row['kind'] in {'http','sdk','model'}]
    repeat = harness.run('2026-10-15T22:05:00+00:00')
    assert repeat.returncode==0,repeat.stdout+repeat.stderr
    assert {table:harness.book_rows(table) for table in before_books} == before_books
    assert {str(path):path.read_bytes() for path in (harness.state/'source_inputs').rglob('*.json')} == before_inputs
    assert [row for row in harness.trace() if row['kind'] in {'http','sdk','model'}] == before_io


def test_same_day_failed_midday_screen_then_complete_evening(tmp_path):
    harness = create_harness(tmp_path)
    failed = harness.run('2026-10-14T15:45:00+00:00',entrypoint='preflight',fault='midday_congress_503')
    assert failed.returncode!=0, failed.stdout+failed.stderr
    assert not list(harness.state.glob('horizon_*/portfolio.db'))
    assert not any(row['kind']=='model' for row in harness.trace())
    incident_files = list((harness.repo/'data/logs/run_attempts').rglob('*.json'))
    retained = {str(path):path.read_bytes() for path in incident_files}
    assert retained
    failed_attempt = harness.attempts()[-1]
    assert failed_attempt['result']['screen_runtime_failures']==[]
    failures = failed_attempt['result']['screen_source_failures']
    assert {row['source'] for row in failures} == {'congress','edgar'}
    congress = next(row for row in failures if row['source']=='congress')
    assert congress['http_status']==503 and congress['attempts']==1
    assert any(row['kind']=='http' and row['url'].endswith('/v2/assets') for row in harness.trace())
    evening = harness.run('2026-10-14T22:00:00+00:00')
    assert evening.returncode==0, evening.stdout+evening.stderr
    report = assert_complete(harness,'2026-10-14')
    assert report['preflight_incidents'], report
    assert all(Path(path).read_bytes()==body for path,body in retained.items())


def test_every_child_denies_python_and_native_curl_network_boundaries(tmp_path):
    harness = create_harness(tmp_path)
    harness.scenario_path.write_text(json.dumps({'now':'2026-10-14T22:00:00+00:00','session':'2026-10-14','fault':None}))
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
    result = subprocess.run([str(harness.python),'-c',code],env=harness.env,cwd=harness.repo,capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
    denied = [row for row in harness.trace() if row['kind']=='forbidden_network']
    assert len(denied)==3 and sum(row.get('boundary')=='native_curl' for row in denied)==2
    assert not [row for row in harness.trace() if row['kind'] in {'http','sdk','model'}]


def test_candidate_v2_profile_has_one_audited_policy_change_from_retained_baseline(tmp_path):
    harness = create_harness(tmp_path, profile_variant='government_copy_v2')
    original = json.loads((Path(__file__).parent/'fixtures/scheduled_acceptance/production-profile.json').read_text())
    frozen = json.loads(harness.profile.read_text())
    assert original['autoresearch']['regulation_retrieval_policy'] == 'verified_government_copy_v1'
    assert frozen['autoresearch']['regulation_retrieval_policy'] == 'verified_government_copy_v2'
    assert harness.audit['original_sha256'] == ORIGINAL_SHA
    assert harness.audit['profile_variant'] == 'government_copy_v2'
    assert harness.audit['policy_overrides'] == [{
        'path': ['autoresearch', 'regulation_retrieval_policy'],
        'original': 'verified_government_copy_v1', 'fixture': 'verified_government_copy_v2'}]
    assert all(row['path'] != ['autoresearch', 'regulation_retrieval_policy']
               for row in harness.audit['substitutions'])
    assert harness.audit['candidate_profile_sha256'] not in {ORIGINAL_SHA, harness.audit['fixture_sha256']}
    assert harness.effective_config_proof()['regulation_retrieval_policy'] == 'verified_government_copy_v2'
    assert not [row for row in harness.trace() if row['kind'] in {'http','sdk','model','forbidden_network','unexpected_httpx'}]
