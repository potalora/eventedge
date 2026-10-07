from dataclasses import asdict
from datetime import date, datetime, timezone
import hashlib
import json
import sqlite3

import pytest

from tradingagents.strategies.metrics.health import classify_strategy_run
from tradingagents.strategies.metrics.store import _SCHEMA
from tradingagents.strategies.modules import get_paper_trade_strategies
from tradingagents.strategies.orchestration.run_evidence import persist_run_evidence
from tradingagents.strategies.orchestration.run_outcome import DAILY_RESULT_PREFIX, DAILY_RESULT_WIRE_VERSION
from tradingagents.strategies.orchestration.source_inputs import SourceInputStore
from tradingagents.strategies.state.portfolio_ledger import _DDL
from tradingagents.strategies.orchestration.operational_report import build_operational_report, render_operational_report, write_operational_report

SESSION = '2026-10-06'
GENERATION = 'gen_020'
COMMIT = 'a' * 40
EPOCH = GENERATION + '-2026-10-06-' + 'b' * 16
COHORTS = tuple(f'horizon_{h}_size_{s}' for h in ('30d','3m','6m','1y') for s in ('5k','10k','50k','100k'))


def _insert(connection, table, **values):
    for field in connection.execute(f'PRAGMA table_info({table})'):
        if field[3] and field[4] is None and field[1] not in values:
            values[field[1]] = 0 if 'INT' in field[2] else '0'
    keys = ','.join(values)
    markers = ','.join('?' for _ in values)
    connection.execute(f'INSERT INTO {table} ({keys}) VALUES ({markers})', tuple(values.values()))


def _policy_context(connection,name,volatility):
    from tradingagents.strategies.execution.ids import stable_id
    from tradingagents.strategies.state.portfolio_ledger import _canonical_json
    config_json=_canonical_json({'version':'fixture-v1'})
    context_json=_canonical_json({'annualized_volatility':volatility})
    config_digest=stable_id('policy_config',config_json)
    context_digest=stable_id('policy_context',context_json)
    payload_json=_canonical_json({'cohort_id':name,'session':SESSION,'binding_kind':'staging',
        'epoch_id':EPOCH,'policy_version':'fixture-v1','policy_config_digest':config_digest,'context_digest':context_digest})
    _insert(connection,'policy_session_contexts',cohort_id=name,session=SESSION,binding_kind='staging',epoch_id=EPOCH,
        policy_version='fixture-v1',policy_config_json=config_json,policy_config_digest=config_digest,
        context_json=context_json,context_digest=context_digest,payload_json=payload_json,
        payload_digest=stable_id('policy_binding',payload_json),bound_at=SESSION+'T20:00:00+00:00')


def _attempt(repo, wire, *, action='daily', outcome='clean', ordinal=1, commit=COMMIT, return_code=0):
    result = {'outcome': outcome, 'success': outcome == 'clean', 'execution_valid': True,
              'input_coverage_valid': outcome == 'clean', 'source_health_failures': []}
    stdout = DAILY_RESULT_PREFIX + json.dumps({'wire_version': DAILY_RESULT_WIRE_VERSION, 'cohort_results': wire}) if action == 'daily' and wire is not None else ''
    stderr=''
    if action == 'preflight':
        result = {'success':False,'elapsed_s':1.0,'preflight_mode':'screen','screen_ok':False,'screen_failure_count':1,'error':'preflight screen status: failed'}
        stderr='edgar acquisition failed [http_error; http_status=500]'
    if wire is not None:
        from tradingagents.strategies.orchestration.source_coverage import aggregate_source_health_failures
        from tradingagents.strategies.orchestration.daily_pipeline import aggregate_candidate_input_issues
        result['input_coverage_valid']=all(row['input_coverage_valid'] for row in wire.values())
        result['source_health_failures']=aggregate_source_health_failures(wire,SESSION,require_coverage=True)
        issues=aggregate_candidate_input_issues(wire,SESSION)
        if issues:
            result['candidate_input_issues']=issues
    path = persist_run_evidence(repo, {'schema_version':1,'generation_id':GENERATION,'generation_commit':commit,
        'requested_session':SESSION,'action':action,'preflight_mode':'screen' if action == 'preflight' else None,
        'started_at': f'2026-10-06T20:{ordinal:02d}:00+00:00','finished_at':f'2026-10-06T20:{ordinal:02d}:01+00:00',
        'process_return_code':return_code,'process_status':'completed','stdout':stdout,'stderr':stderr,'result':result})
    if action=='daily':
        manifest_path=repo/'data/generations/manifest.json'
        manifest=json.loads(manifest_path.read_text())
        manifest['generations'][0]['run_history'].append({'date':SESSION,'action':'daily','outcome':outcome,'success':outcome=='clean','execution_valid':result['execution_valid'],'input_coverage_valid':result['input_coverage_valid'],'source_health_failures':result['source_health_failures'],'evidence_path':str(path)})
        if result.get('candidate_input_issues'):
            manifest['generations'][0]['run_history'][-1]['candidate_input_issues']=result['candidate_input_issues']
        manifest_path.write_text(json.dumps(manifest))
    return path


@pytest.fixture
def native_evidence(tmp_path):
    state = tmp_path / 'data' / 'generations' / GENERATION
    state.mkdir(parents=True)
    manifest = {'generations':[{'gen_id':GENERATION,'git_commit':COMMIT,'state_dir':str(state),'run_history':[],'status':'active'}]}
    (state.parent / 'manifest.json').write_text(json.dumps(manifest))
    for name in COHORTS:
        folder = state / name
        folder.mkdir()
        with sqlite3.connect(folder / 'portfolio.db') as connection:
            for ddl in _DDL:
                connection.execute(ddl)
            _insert(connection,'schema_metadata',metadata_key='cohort_id',metadata_value=name)
            _insert(connection,'session_runs',session_run_id=name,cohort_id=name,session=SESSION,valid=1,invalid_reason='',started_at=SESSION,completed_at=SESSION)
            _insert(connection,'session_execution_contexts',execution_context_id=name,cohort_id=name,session=SESSION,epoch_id=EPOCH)
            _insert(connection,'account_snapshots',snapshot_id=name,cohort_id=name,epoch_id=EPOCH,session=SESSION,valid=1,invalid_reason='',net_equity='5000')
            horizon = name.split('_')[1]
            _insert(connection,'staging_runs',staging_run_id=name,cohort_id=name,session=SESSION,epoch_id=EPOCH,policy_id='foundation-'+horizon,completed_at=SESSION)
            _policy_context(connection,name,{})
        connection.close()
    with sqlite3.connect(state / 'metrics_v2.sqlite3') as connection:
        connection.executescript(_SCHEMA)
        epoch = {'epoch_id':EPOCH,'generation_id':GENERATION,'generation_commit':COMMIT,'start_session':SESSION,'end_session':None,'status':'active'}
        _insert(connection,'metric_epochs',epoch_id=EPOCH,payload_json=json.dumps(epoch))
        for horizon in ('30d','3m','6m','1y'):
            for strategy in get_paper_trade_strategies():
                record = classify_strategy_run(epoch_id=EPOCH,session=date.fromisoformat(SESSION),policy_id='foundation-'+horizon,
                    strategy=strategy.name,data_sources=strategy.data_sources,candidates=[],provider_errors={},exception=None)
                payload = asdict(record)
                payload['session'] = SESSION
                _insert(connection,'strategy_health',health_id=record.health_id,epoch_id=EPOCH,session=SESSION,payload_json=json.dumps(payload))
    connection.close()
    wire = {name:{'error':False,'execution_valid':True,'staging_valid':True,'degraded':False,
                  'input_coverage_valid':True,'source_health_failures':[]} for name in COHORTS}
    store = SourceInputStore(tmp_path/'cache',accepted_dir=state/'source_inputs')
    mandatory={source:{} for strategy in get_paper_trade_strategies() for source in strategy.data_sources if source!='openbb'}
    mandatory['edgar']={'events':[], '_request_diagnostics':[{'provider':'edgar','operation':'filings','reason_code':'success','http_status':200,'attempts':2,'recovered':True}]}
    store.freeze({'generation':GENERATION,'session':SESSION,'commit':COMMIT,'configuration':'frozen-config'},mandatory,
                 acquired_at=datetime(2026,10,6,20,tzinfo=timezone.utc))
    SourceInputStore(tmp_path/'cache',accepted_dir=state/'source_inputs/staging_volatility').freeze(
        {'generation':GENERATION,'session':SESSION,'commit':COMMIT,'configuration':'frozen-config','purpose':'staging-volatility-v1'},
        {'price_history':{},'expected_sessions':(date(2026,10,2),date(2026,10,5)),
         'lookback':1,'floor':0.15,'quarantined_tickers':()},acquired_at=datetime(2026,10,6,20,tzinfo=timezone.utc))
    return tmp_path, state, wire


def _report(repo):
    return build_operational_report(repo, GENERATION, SESSION, snapshot_guaranteed=True)


@pytest.mark.parametrize('kind', ['missing', 'missing_credentials', 'corrupt', 'deeply_nested', 'wrong_identity'])
def test_optional_clef_status_is_reported_without_changing_financial_validity(native_evidence, kind):
    from tradingagents.strategies.orchestration.decision_shadow import evaluate_shadow
    repo, state, wire = native_evidence
    _attempt(repo, wire)
    baseline = _report(repo)
    path = state / 'decision_shadow' / f'{SESSION}.json'
    if kind != 'missing':
        evaluate_shadow(state_dir=state, generation=GENERATION, session=SESSION, epoch_id=EPOCH, generation_commit=COMMIT,
            signals=[{'event_key':'docket-1','ticker':'NVDA','strategy':'litigation','direction':'short',
                      'metadata':{'docket_id':1,'llm_analysis':{'rationale':'Company faces a patent suit.'}}}],
            data={'courtlistener':{'dockets':[{'docket_id':1,'case_name':'Patent holder v NVDA','nature_of_suit':'Patent'}]}},
            config={'enabled':True,'mode':'shadow'}, environ={})
        if kind == 'corrupt':
            path.write_text('{')
        elif kind == 'deeply_nested':
            path.write_text('['*1200+'0'+']'*1200)
        elif kind == 'wrong_identity':
            document = json.loads(path.read_text())
            document['generation'] = 'gen_other'
            path.write_text(json.dumps(document))
    report = _report(repo)
    expected = 'unavailable' if kind in {'corrupt','deeply_nested','wrong_identity'} else kind
    assert report['decision_shadow']['status'] == expected
    if kind == 'missing_credentials':
        assert report['decision_shadow']['event_counts'] == {'missing_credentials':1}
        assert report['decision_shadow']['assessed_events'] == 0
    assert {key:value for key,value in report.items() if key!='decision_shadow'} == {
        key:value for key,value in baseline.items() if key!='decision_shadow'}
    assert report['outcome'] == 'clean' and report['evidence_complete']
    markdown = render_operational_report(report)
    assert 'Clef evidence shadow' in markdown and expected in markdown
    assert 'does not affect trading' in markdown


def test_multiple_daily_attempts_preserve_preflight_incident_without_tainting_clean(native_evidence):
    repo, state, wire = native_evidence
    _attempt(repo,None,outcome='failed',ordinal=1,return_code=1)
    _attempt(repo,None,action='preflight',ordinal=2,return_code=1)
    _attempt(repo,wire,ordinal=3)
    report = _report(repo)
    assert report['outcome'] == 'clean'
    assert report['evidence_complete'] is True
    assert report['attempt_counts'] == {'daily':2,'preflight':1}
    assert report['preflight_incidents'][0]['failures'][0]['source'] == 'edgar'
    assert report['sources']['recovered'][0]['provider'] == 'edgar'
    assert len(report['cohorts']) == 16
    assert report['accounting_valid'] is True


def test_native_source_failures_reach_report_without_process_error_logs(native_evidence):
    repo, state, wire = native_evidence
    path = _attempt(repo, None, action='preflight', ordinal=1, return_code=1)
    artifact = json.loads(path.read_text())
    failures = [{'source': source, 'reason_code': 'invalid_response',
                 'http_status': None, 'attempts': 1, 'operation_count': count}
                for source, count in (('congress', 2), ('noaa', 150), ('usda', 3))]
    artifact['result'].update(screen_failure_count=3, screen_source_failures=failures)
    artifact['stdout'] = artifact['stderr'] = ''
    path.write_text(json.dumps(artifact))
    _attempt(repo, wire, ordinal=2)
    report = _report(repo)
    assert report['outcome'] == 'clean' and report['evidence_complete']
    assert report['preflight_incidents'][0]['failures'] == failures
    markdown = render_operational_report(report)
    for source in ('congress', 'noaa', 'usda'):
        assert f'{source} invalid_response HTTP unknown' in markdown


def test_native_clean_source_diagnostics_do_not_infer_failures_from_logs(native_evidence):
    repo, state, wire = native_evidence
    path = _attempt(repo, None, action='preflight', ordinal=1)
    artifact = json.loads(path.read_text())
    artifact['result'].update(success=True, screen_ok=True,
                              screen_failure_count=0, screen_source_failures=[])
    # A successful retry or incidental log mention is not a failed source.
    artifact['stderr'] = 'edgar recovered after HTTP 500'
    path.write_text(json.dumps(artifact))
    _attempt(repo, wire, ordinal=2)
    report = _report(repo)
    assert report['outcome'] == 'clean' and report['evidence_complete']
    assert report['preflight_incidents'] == []


@pytest.mark.parametrize('damage', ['unknown_source', 'secret_field', 'successful_http'])
def test_report_rejects_malformed_native_source_diagnostics(native_evidence, damage):
    repo, state, wire = native_evidence
    path = _attempt(repo, None, action='preflight', ordinal=1, return_code=1)
    artifact = json.loads(path.read_text())
    failure = {'source': 'noaa', 'reason_code': 'invalid_response',
               'http_status': None, 'attempts': 1, 'operation_count': 1}
    if damage == 'unknown_source':
        failure['source'] = 'PRIVATE_SECRET'
    elif damage == 'secret_field':
        failure['raw_exception'] = 'PRIVATE_SECRET'
    else:
        failure['http_status'] = 200
    artifact['result']['screen_source_failures'] = [failure]
    artifact['stderr'] = ''
    path.write_text(json.dumps(artifact))
    _attempt(repo, wire, ordinal=2)
    report = _report(repo)
    assert any(row['code'] == 'attempt_unreadable' for row in report['diagnostics'])
    assert 'PRIVATE_SECRET' not in render_operational_report(report)


def test_degraded_coverage_keeps_completed_valid_accounting(native_evidence):
    repo, state, wire = native_evidence
    with sqlite3.connect(state/'metrics_v2.sqlite3') as connection:
        row = connection.execute("SELECT health_id,payload_json FROM strategy_health WHERE payload_json LIKE '%foundation-30d%' LIMIT 1").fetchone()
        payload = json.loads(row[1]); payload['status']='data_failure'; payload['evidence']['provider_errors']={'edgar':'http_error'}
        connection.execute('UPDATE strategy_health SET payload_json=? WHERE health_id=?',(json.dumps(payload),row[0]))
    affected = sorted(name for name in COHORTS if name.startswith('horizon_30d_'))
    failure = {key:payload[key] for key in ('health_id','epoch_id','session','policy_id','strategy','status')}
    failure.update(sources=['edgar'],affected_cohorts=affected)
    for name in affected:
        wire[name].update(degraded=True,input_coverage_valid=False,source_health_failures=[failure])
    path = _attempt(repo,wire,outcome='degraded',return_code=2)
    artifact=json.loads(path.read_text()); artifact['result']['source_health_failures']=[failure]; path.write_text(json.dumps(artifact))
    report=_report(repo)
    assert report['outcome']=='degraded'
    assert report['accounting_valid'] is True
    assert report['input_coverage_valid'] is False
    assert report['source_health_failures']==[failure]
    assert report['evidence_complete'] is True
    assert 'degraded' in render_operational_report(report)


def test_missing_book_withholds_performance_and_report_still_written(native_evidence,tmp_path):
    repo,state,wire=native_evidence
    (state/COHORTS[-1]/'portfolio.db').unlink()
    _attempt(repo,wire)
    report=_report(repo)
    assert report['evidence_complete'] is False
    assert report['performance_claims_withheld'] is True
    assert any(item['code']=='ledger_missing' for item in report['diagnostics'])
    paths=write_operational_report(report,tmp_path/'reports')
    assert paths['json'].exists() and paths['markdown'].exists()
    assert json.loads(paths['json'].read_text())==report


@pytest.mark.parametrize('damage',[None,'missing','metadata','wire_scope','summary'])
def test_completed_staging_with_candidate_quarantine_reconciles_durable_issues(native_evidence,damage):
    from tradingagents.strategies.orchestration.candidate_inputs import CandidateInputIssue
    repo,state,wire=native_evidence
    issue=CandidateInputIssue.create(issue_id='candidate_input_issue_'+'c'*32,
        epoch_id=EPOCH,session=date.fromisoformat(SESSION),dependency_kind='reference_bar',
        reason_code='invalid_data',ticker='NVDA',source='sip',fetched_at=datetime(2026,10,6,20,tzinfo=timezone.utc),
        requested_history_digest='sha256:'+'a'*64,returned_history_digest='sha256:'+'b'*64,
        expected_sessions=(date.fromisoformat(SESSION),),observed_sessions=(),retryable=True,
        affected_signal_identities=({'event_key':'nvda-event','strategy':'earnings_call'},),affected_cohorts=COHORTS)
    ref=dict(issue.reference()); ref['affected_cohorts']=list(issue.affected_cohorts)
    with sqlite3.connect(state/'metrics_v2.sqlite3') as connection:
        if damage!='missing':
            _insert(connection,'candidate_input_issues',issue_id=issue.issue_id,epoch_id=EPOCH,
                session=SESSION,dependency_kind=issue.dependency_kind,ticker='BAD' if damage=='metadata' else issue.ticker,
                payload_json=issue.canonical_payload())
    for row in wire.values():
        row.update(degraded=True,staging_valid=False,candidate_input_issues=[ref])
    if damage=='wire_scope':
        wire[COHORTS[0]].pop('candidate_input_issues')
        # Keep this negative case structurally valid; omit every wire reference.
        for row in wire.values(): row.pop('candidate_input_issues',None)
    path=_attempt(repo,wire,outcome='degraded')
    if damage=='summary':
        artifact=json.loads(path.read_text()); artifact['result'].pop('candidate_input_issues'); path.write_text(json.dumps(artifact))
    report=_report(repo)
    assert report['staging_complete'] is True
    assert report['accounting_valid'] is True
    assert report['staging_valid'] is False
    if damage is None:
        assert report['evidence_complete'] is True, report['diagnostics']
        assert report['outcome']=='degraded'
        assert report['candidate_input_issues']==[ref]
        assert 'NVDA' in render_operational_report(report)
    else:
        assert report['evidence_complete'] is False
        assert report['performance_claims_withheld'] is True


@pytest.mark.parametrize('damage',['bundle','epoch','attempt_commit','misleading_wire'])
def test_conflicting_or_corrupt_evidence_cannot_be_reported_clean(native_evidence,damage):
    repo,state,wire=native_evidence
    if damage=='bundle': next((state/'source_inputs').glob('*.json')).write_text('{}')
    if damage=='epoch':
        with sqlite3.connect(state/COHORTS[0]/'portfolio.db') as connection:
            connection.execute("UPDATE account_snapshots SET epoch_id='wrong'")
    if damage=='misleading_wire': wire[COHORTS[0]].update(error=True,staging_valid=False)
    _attempt(repo,wire,commit='wrong' if damage=='attempt_commit' else COMMIT)
    report=_report(repo)
    assert report['evidence_complete'] is False
    assert report['outcome']!='clean'
    assert report['performance_claims_withheld'] is True


def test_distinct_fill_ids_not_partial_close_rows_and_read_only_databases(native_evidence):
    repo,state,wire=native_evidence
    ledger=state/COHORTS[0]/'portfolio.db'
    with sqlite3.connect(ledger) as connection:
        for index,side in enumerate(('buy','short','sell')):
            _insert(connection,'order_intents',intent_id=f'i{index}',cohort_id=COHORTS[0],side=side,requested_qty=10,eligible_session=SESSION,price_rule='next_session_open')
            _insert(connection,'fills',fill_id=f'f{index}',intent_id=f'i{index}',side=side,session=SESSION,quantity=10)
        for index in range(2):
            _insert(connection,'lots',lot_id=f'l{index}',fill_id=f'f{index}',cohort_id=COHORTS[0],ticker='AAA',direction='long',opened_session=SESSION)
            _insert(connection,'lot_closures',closure_id=f'c{index}',lot_id=f'l{index}',fill_id='f2',quantity=5)
    connection.close()
    _attempt(repo,wire)
    before={path:path.read_bytes() for path in state.rglob('*.sqlite3')} | {path:path.read_bytes() for path in state.rglob('portfolio.db')}
    report=_report(repo)
    assert report['cohorts'][COHORTS[0]]['fills']=={'total':3,'entries':2,'exits':1}
    assert all(path.read_bytes()==content for path,content in before.items())
    assert _report(repo)==report
    assert render_operational_report(report)==render_operational_report(_report(repo))


def test_frozen_bundle_reader_does_not_depend_on_current_credentials(native_evidence,monkeypatch):
    repo,state,wire=native_evidence
    _attempt(repo,wire)
    monkeypatch.setenv('FRED_API_KEY','credential-rotated-after-acceptance')
    assert _report(repo)['outcome']=='clean'


def test_unresolved_required_acquisition_cannot_be_hidden_by_clean_wire(native_evidence):
    repo,state,wire=native_evidence
    path=next((state/'source_inputs').glob('*.json'))
    envelope=SourceInputStore.decode(path.read_text())
    envelope['payload']['edgar']['error']='provider acquisition failed'
    envelope['digest']=hashlib.sha256(SourceInputStore.encode(envelope['payload']).encode()).hexdigest()
    path.write_text(SourceInputStore.encode(envelope))
    _attempt(repo,wire)
    report=_report(repo)
    assert report['outcome']!='clean'
    assert any(item['code']=='source_health_conflict' for item in report['diagnostics'])


@pytest.mark.parametrize('damage',['empty','missing','non_mapping','failed_coverage','partial_coverage','unknown_coverage'])
def test_frozen_source_presence_and_explicit_coverage_cannot_be_hidden(native_evidence,damage):
    repo,state,wire=native_evidence
    path=next((state/'source_inputs').glob('*.json'))
    envelope=SourceInputStore.decode(path.read_text())
    if damage=='empty': envelope['payload']={}
    elif damage=='missing': envelope['payload'].pop('edgar')
    elif damage=='non_mapping': envelope['payload']['edgar']=[]
    else: envelope['payload']['edgar']['_coverage']={'status':{'failed_coverage':'failed','partial_coverage':'partial','unknown_coverage':'wrong'}[damage]}
    envelope['digest']=hashlib.sha256(SourceInputStore.encode(envelope['payload']).encode()).hexdigest()
    path.write_text(SourceInputStore.encode(envelope))
    _attempt(repo,wire)
    report=_report(repo)
    assert report['evidence_complete'] is False
    assert report['outcome']!='clean'
    assert report['performance_claims_withheld'] is True


def test_failed_source_requires_health_failure_in_every_affected_strategy_scope(native_evidence):
    repo,state,wire=native_evidence
    path=next((state/'source_inputs').glob('*.json'))
    envelope=SourceInputStore.decode(path.read_text())
    envelope['payload']['edgar']['error']='provider acquisition failed'
    envelope['digest']=hashlib.sha256(SourceInputStore.encode(envelope['payload']).encode()).hexdigest()
    path.write_text(SourceInputStore.encode(envelope))
    with sqlite3.connect(state/'metrics_v2.sqlite3') as connection:
        row=connection.execute("SELECT health_id,payload_json FROM strategy_health WHERE payload_json LIKE '%foundation-30d%' LIMIT 1").fetchone()
        payload=json.loads(row[1]); payload['status']='data_failure'; payload['evidence']['provider_errors']={'edgar':'http_error'}
        connection.execute('UPDATE strategy_health SET payload_json=? WHERE health_id=?',(json.dumps(payload),row[0]))
    affected=sorted(name for name in COHORTS if name.startswith('horizon_30d_'))
    failure={key:payload[key] for key in ('health_id','epoch_id','session','policy_id','strategy','status')}
    failure.update(sources=['edgar'],affected_cohorts=affected)
    for name in affected: wire[name].update(degraded=True,input_coverage_valid=False,source_health_failures=[failure])
    _attempt(repo,wire,outcome='degraded')
    report=_report(repo)
    assert report['evidence_complete'] is False
    assert any(row['code']=='source_health_conflict' for row in report['diagnostics'])


def test_native_later_resting_stop_fill_counts_once_without_conflict(native_evidence):
    from decimal import Decimal
    from tradingagents.strategies.execution import Fill, OrderIntent, SignalRecord
    from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger
    repo,state,wire=native_evidence
    name=COHORTS[0]; path=state/name/'portfolio.db'; path.unlink()
    ledger=PortfolioLedger(path,name,Decimal('5000'))
    decision=datetime(2026,10,2,20,tzinfo=timezone.utc)
    signal=SignalRecord('stop-signal',EPOCH,'foundation-30d','stop-event','litigation','NVDA','long',decision,decision,
        date(2026,10,2),Decimal('100'),decision,'stop-evidence')
    ledger.record_signal(signal)
    entry=OrderIntent('stop-entry',(signal.signal_id,),name,'buy',10,decision,date(2026,10,5),'next_session_open','pending',None,None)
    ledger.stage_intent(entry)
    zero=Decimal('0')
    ledger.apply_fill(entry,Fill('entry-fill',entry.intent_id,'buy',date(2026,10,5),decision,decision,Decimal('100'),Decimal('100'),10,zero,zero,zero))
    stop=OrderIntent('resting-stop',(signal.signal_id,),name,'sell',10,decision,date(2026,10,5),'resting_stop','pending',Decimal('95'),None)
    ledger.stage_intent(stop)
    ledger.apply_fill(stop,Fill('stop-fill',stop.intent_id,'sell',date.fromisoformat(SESSION),decision,decision,Decimal('90'),Decimal('90'),10,zero,zero,zero))
    assert len(ledger.read_fills(date.fromisoformat(SESSION),date.fromisoformat(SESSION)))==1
    ledger.close()
    with sqlite3.connect(path) as connection:
        _insert(connection,'session_runs',session_run_id=name,cohort_id=name,session=SESSION,valid=1,invalid_reason='',started_at=SESSION,completed_at=SESSION)
        _insert(connection,'session_execution_contexts',execution_context_id=name,cohort_id=name,session=SESSION,epoch_id=EPOCH)
        _insert(connection,'account_snapshots',snapshot_id=name,cohort_id=name,epoch_id=EPOCH,session=SESSION,valid=1,invalid_reason='',net_equity='4900')
        _insert(connection,'staging_runs',staging_run_id=name,cohort_id=name,session=SESSION,epoch_id=EPOCH,policy_id='foundation-30d',completed_at=SESSION)
        _policy_context(connection,name,{})
    _attempt(repo,wire)
    report=_report(repo)
    assert report['evidence_complete'] is True, report['diagnostics']
    assert report['cohorts'][name]['fills']=={'total':1,'entries':0,'exits':1}


@pytest.mark.parametrize('damage',['missing','corrupt','identity','digest','history','sessions'])
def test_completed_staging_requires_valid_accepted_volatility(native_evidence,damage):
    repo,state,wire=native_evidence
    path=next((state/'source_inputs/staging_volatility').glob('*.json'))
    if damage=='missing': path.unlink()
    elif damage=='corrupt': path.write_text('{}')
    else:
        envelope=SourceInputStore.decode(path.read_text())
        if damage=='identity': envelope['identity']['purpose']='wrong'
        elif damage=='history': envelope['payload']['price_history']={'NVDA':[]}
        elif damage=='sessions': envelope['payload']['expected_sessions']=(date(2026,10,2),date(2026,10,6))
        envelope['digest']=hashlib.sha256(SourceInputStore.encode(envelope['payload']).encode()).hexdigest() if damage!='digest' else 'bad'
        path.write_text(SourceInputStore.encode(envelope))
    _attempt(repo,wire)
    report=_report(repo)
    assert report['evidence_complete'] is False
    assert report['performance_claims_withheld'] is True
    assert any('volatility' in row['code'] for row in report['diagnostics'])


@pytest.mark.parametrize('damage',[None,'empty_history','changed_context','context_digest','missing_context'])
def test_accepted_volatility_matches_persisted_staging_policy_context(native_evidence,damage):
    import pandas as pd
    repo,state,wire=native_evidence
    path=next((state/'source_inputs/staging_volatility').glob('*.json'))
    envelope=SourceInputStore.decode(path.read_text())
    envelope['payload']['price_history']={'NVDA':pd.DataFrame({'Close':[100.0,100.0]},index=pd.to_datetime(['2026-10-02','2026-10-05']))}
    if damage=='empty_history': envelope['payload']['price_history']={}
    envelope['digest']=hashlib.sha256(SourceInputStore.encode(envelope['payload']).encode()).hexdigest()
    path.write_text(SourceInputStore.encode(envelope))
    with sqlite3.connect(state/COHORTS[0]/'portfolio.db') as connection:
        connection.execute('DELETE FROM policy_session_contexts')
        if damage!='missing_context':
            _policy_context(connection,COHORTS[0],{'NVDA':0.2 if damage=='changed_context' else 0.15})
        if damage=='context_digest': connection.execute("UPDATE policy_session_contexts SET context_digest='bad'")
    _attempt(repo,wire)
    report=_report(repo)
    assert report['evidence_complete'] is (damage is None), report['diagnostics']
    if damage is None:
        assert report['volatility']['valid'] is True
        assert report['volatility']['tickers']==['NVDA']
    else:
        assert report['performance_claims_withheld'] is True
        assert any('volatility' in row['code'] for row in report['diagnostics'])


def test_missing_exact_health_is_diagnostic_and_withholds_performance(native_evidence):
    repo,state,wire=native_evidence
    with sqlite3.connect(state/'metrics_v2.sqlite3') as connection:
        connection.execute('DELETE FROM strategy_health WHERE health_id=(SELECT health_id FROM strategy_health LIMIT 1)')
    connection.close()
    _attempt(repo,wire)
    report=_report(repo)
    assert any(row['status']=='missing_health' for row in report['source_health_failures'])
    assert report['performance_claims_withheld'] is True


def test_equal_time_conflicting_daily_attempts_have_no_invented_winner(native_evidence):
    repo,state,wire=native_evidence
    _attempt(repo,wire,ordinal=1)
    broken={name:dict(row) for name,row in wire.items()}
    broken[COHORTS[0]].update(error=True,staging_valid=False)
    _attempt(repo,broken,ordinal=1,outcome='failed')
    report=_report(repo)
    assert any(row['code']=='attempt_order_ambiguous' for row in report['diagnostics'])
    assert report['performance_claims_withheld'] is True


def test_cli_writes_deterministic_json_and_markdown_for_completed_session(native_evidence,tmp_path):
    from scripts.generate_operational_report import main
    repo,state,wire=native_evidence
    _attempt(repo,wire)
    output=tmp_path/'output'
    assert main(['--repo-root',str(repo),'--generation',GENERATION,'--date',SESSION,
                 '--output-dir',str(output),'--snapshot-guaranteed'])==0
    assert len(list(output.glob('*.json')))==len(list(output.glob('*.md')))==1


@pytest.mark.parametrize('damage',['missing','outcome_conflict','missing_artifact'])
def test_exact_manifest_history_must_match_immutable_attempt(native_evidence,damage):
    repo,state,wire=native_evidence
    path=_attempt(repo,wire)
    manifest_path=state.parent/'manifest.json'
    manifest=json.loads(manifest_path.read_text())
    if damage=='missing': manifest['generations'][0]['run_history']=[]
    elif damage=='outcome_conflict': manifest['generations'][0]['run_history'][0]['outcome']='failed'
    else: manifest['generations'][0]['run_history'][0]['evidence_path']=str(path.parent/'missing.json')
    manifest_path.write_text(json.dumps(manifest))
    report=_report(repo)
    assert report['evidence_complete'] is False
    assert report['performance_claims_withheld'] is True
    assert any(row['code'].startswith('manifest_session_') for row in report['diagnostics'])


def test_cli_all_active_uses_manifest_scope(native_evidence,tmp_path):
    from scripts.generate_operational_report import main
    repo,state,wire=native_evidence
    _attempt(repo,wire)
    assert main(['--repo-root',str(repo),'--all-active','--date',SESSION,
                 '--output-dir',str(tmp_path/'all'),'--snapshot-guaranteed'])==0


def test_live_reader_requires_shared_lock_and_never_bypasses_busy_runtime(native_evidence,monkeypatch):
    import tradingagents.strategies.orchestration.operational_report as module
    from tradingagents.strategies.orchestration.runtime_lock import RuntimeLockBusy,runtime_lock
    repo,state,wire=native_evidence
    _attempt(repo,wire)
    lock=repo/'data/operational/eventedge-runtime.lock'
    monkeypatch.setattr(module,'canonical_runtime_lock_path',lambda root:lock)
    with runtime_lock(lock,exclusive=True):
        with pytest.raises(RuntimeLockBusy):
            build_operational_report(repo,GENERATION,SESSION)
    assert build_operational_report(repo,GENERATION,SESSION)['outcome']=='clean'


def test_corrupt_unknown_attempt_and_malformed_health_fail_visibly(native_evidence):
    repo,state,wire=native_evidence
    _attempt(repo,wire)
    (repo/'data/logs/run_attempts/corrupt.json').write_text('{}')
    with sqlite3.connect(state/'metrics_v2.sqlite3') as connection:
        row=connection.execute('SELECT health_id,payload_json FROM strategy_health LIMIT 1').fetchone()
        value=json.loads(row[1]);value['evidence']=[]
        connection.execute('UPDATE strategy_health SET payload_json=? WHERE health_id=?',(json.dumps(value),row[0]))
    connection.close()
    report=_report(repo)
    assert report['evidence_complete'] is False
    assert any(row['code']=='attempt_unreadable' for row in report['diagnostics'])
    assert any(row['code']=='health_contract_invalid' for row in report['diagnostics'])


def test_unbound_health_policy_is_conflicting_evidence(native_evidence):
    repo,state,wire=native_evidence
    _attempt(repo,wire)
    with sqlite3.connect(state/'metrics_v2.sqlite3') as connection:
        row=connection.execute('SELECT payload_json FROM strategy_health LIMIT 1').fetchone()
        value=json.loads(row[0]);value.update(health_id='health_unknown',policy_id='unbound')
        _insert(connection,'strategy_health',health_id=value['health_id'],epoch_id=EPOCH,session=SESSION,payload_json=json.dumps(value))
    connection.close()
    report=_report(repo)
    assert report['evidence_complete'] is False
    assert any(row['code']=='health_scope_conflict' for row in report['diagnostics'])


def test_native_request_diagnostics_allow_success_none_and_zero_attempt_deadline(native_evidence):
    repo,state,wire=native_evidence
    path=next((state/'source_inputs').glob('*.json'))
    envelope=SourceInputStore.decode(path.read_text())
    envelope['payload']['edgar']['_request_diagnostics'][0]['reason_code']=None
    envelope['payload']['openbb']={'error':'deadline','_request_diagnostics':[{
        'provider':'openbb','operation':'get','reason_code':'timeout','http_status':None,'attempts':0,'recovered':False}]}
    envelope['digest']=hashlib.sha256(SourceInputStore.encode(envelope['payload']).encode()).hexdigest()
    path.write_text(SourceInputStore.encode(envelope))
    _attempt(repo,wire)
    report=_report(repo)
    assert report['evidence_complete'] is True
    assert report['outcome']=='clean'
    assert report['sources']['recovered'][0]['reason_code']=='success'
    assert any(row.get('attempts')==0 for row in report['sources']['unresolved'])


def test_wrong_shared_epoch_window_cannot_claim_accounting_valid(native_evidence):
    repo,state,wire=native_evidence
    with sqlite3.connect(state/'metrics_v2.sqlite3') as connection:
        row=connection.execute('SELECT payload_json FROM metric_epochs WHERE epoch_id=?',(EPOCH,)).fetchone()
        value=json.loads(row[0]);value['start_session']='2026-10-07'
        connection.execute('UPDATE metric_epochs SET payload_json=? WHERE epoch_id=?',(json.dumps(value),EPOCH))
    connection.close()
    _attempt(repo,wire)
    report=_report(repo)
    assert report['accounting_valid'] is False
    assert report['performance_claims_withheld'] is True


def test_boolean_process_return_code_is_not_successful_integer_zero(native_evidence):
    repo,state,wire=native_evidence
    path=_attempt(repo,wire)
    value=json.loads(path.read_text());value['process_return_code']=False
    path.write_text(json.dumps(value))
    report=_report(repo)
    assert report['evidence_complete'] is False
    assert report['outcome']!='clean'


def test_report_publication_does_not_rewrite_existing_canonical_files_in_place(native_evidence,tmp_path,monkeypatch):
    from pathlib import Path
    repo,state,wire=native_evidence
    _attempt(repo,wire)
    report=_report(repo)
    paths=write_operational_report(report,tmp_path/'reports')
    original=Path.write_text
    def write(path,*args,**kwargs):
        if path in paths.values():
            pytest.fail('canonical monitor report rewritten in place')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'write_text',write)
    write_operational_report(report,tmp_path/'reports')
    assert json.loads(paths['json'].read_text())==report


def test_native_successful_preflight_is_not_fabricated_incident(native_evidence):
    repo,state,wire=native_evidence
    path=_attempt(repo,None,action='preflight',ordinal=1,return_code=0)
    value=json.loads(path.read_text())
    value['result']={'success':True,'elapsed_s':1.0,'preflight_mode':'screen','screen_ok':True,'screen_failure_count':0}
    value['stdout']='';value['stderr']=''
    path.write_text(json.dumps(value))
    _attempt(repo,wire,ordinal=2)
    report=_report(repo)
    assert report['outcome']=='clean'
    assert report['attempt_counts']['preflight']==1
    assert report['preflight_incidents']==[]


def test_shared_metric_epoch_is_authoritative_when_book_epoch_tables_are_empty(native_evidence):
    repo,state,wire=native_evidence
    for name in COHORTS:
        with sqlite3.connect(state/name/'portfolio.db') as connection:
            connection.execute('DELETE FROM metric_epochs')
        connection.close()
    _attempt(repo,wire)
    report=_report(repo)
    assert report['outcome']=='clean'
    assert report['accounting_valid'] is True
    assert report['evidence_complete'] is True
