"""Offline accepted-artifact fixtures: no runner, provider or model calls."""
from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import gc
import json
import sqlite3
import subprocess

import pytest
from tradingagents.strategies.orchestration.run_outcome import DAILY_RESULT_WIRE_VERSION
from test_operational_report import native_evidence, _attempt, _insert, SESSION, GENERATION, COMMIT, EPOCH, COHORTS
from tradingagents.strategies.execution.ids import stable_id
from tradingagents.strategies.orchestration.source_inputs import SourceInputStore, configuration_fingerprint
from tradingagents.strategies.orchestration.research_screening import _receipt, _digest
from tradingagents.strategies.orchestration.session_executor import PHASES


@pytest.fixture
def evidence(native_evidence, request):
    repo, state, wire = native_evidence
    profile = {'autoresearch': {'research_resilience_policy': 'dependency_isolation_v1',
        'llm_billing_mode': 'chatgpt_plan'}}
    filing_exclusion = getattr(request, 'param', None) == 'filing_exclusion'
    if filing_exclusion:
        profile['autoresearch']['filing_research_policy']='disabled_v1'
    body = json.dumps(profile).encode()
    profile_path = state / 'generation-config.json'
    profile_path.write_bytes(body)
    sha = hashlib.sha256(body).hexdigest()
    policy = 'generation-config-' + sha
    manifest_path = state.parent / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['generations'][0].update(config_file=str(profile_path), config_sha256=sha)
    manifest_path.write_text(json.dumps(manifest))
    slot = configuration_fingerprint({'generation': GENERATION, 'session': SESSION})
    source = SourceInputStore.decode((state / 'source_inputs' / (slot+'.json')).read_text())
    if filing_exclusion:
        source['payload']['edgar']={'error':'filing_research_disabled','coverage':{
            'complete':False,'reason':'filing_research_disabled','filing_research_policy':'disabled_v1'}}
        source['digest']=hashlib.sha256(SourceInputStore.encode(source['payload']).encode()).hexdigest()
        (state/'source_inputs'/(slot+'.json')).write_text(SourceInputStore.encode(source))
    source['acquired_at']=datetime(2026,10,6,20,1,tzinfo=timezone.utc)
    (state/'source_inputs'/(slot+'.json')).write_text(SourceInputStore.encode(source))
    policies = {h: policy + ':health:' + h for h in ('30d', '3m', '6m', '1y')}
    with sqlite3.connect(state / 'metrics_v2.sqlite3') as db:
        epoch = json.loads(db.execute('SELECT payload_json FROM metric_epochs').fetchone()[0])
        epoch['status'] = 'open'
        db.execute('UPDATE metric_epochs SET payload_json=?', (json.dumps(epoch),))
        records = []
        for hid, payload in db.execute('SELECT health_id,payload_json FROM strategy_health').fetchall():
            record = json.loads(payload)
            horizon = record['policy_id'].removeprefix('foundation-')
            if filing_exclusion and 'edgar' in record['evidence']['data_sources']:
                record['status']='data_failure'
                record['evidence']['provider_errors']={'edgar':'filing_research_disabled'}
            record['policy_id'] = policies[horizon]
            from tradingagents.strategies.metrics.identity import _stable_id
            record['health_id'] = _stable_id('health', EPOCH, date.fromisoformat(SESSION), policies[horizon], record['strategy'])
            db.execute('UPDATE strategy_health SET health_id=?,payload_json=? WHERE health_id=?',
                       (record['health_id'], json.dumps(record), hid))
            record['session'] = date.fromisoformat(SESSION)
            records.append(record)
    db.close()
    screening_store = SourceInputStore(repo/'cache', accepted_dir=state/'source_inputs/screening')
    binding = {'policy':'dependency_isolation_v1', 'source_identity':source['identity'],
        'epoch_id':EPOCH, 'source_digest':source['digest'], 'horizon_policies':policies,
        'strategies': sorted({r['strategy'] for r in records})}
    screening = {'version':'research-screening-v1','binding':binding,
        'horizons':{h:{'signals':[], 'regime':{'research_hold_reasons':[]},
            'health':[r for r in records if r['policy_id']==p]} for h,p in policies.items()},
        'model_coverage':{'complete':True}}
    screening['receipt'] = _receipt(screening_store, screening)
    identity = {**source['identity'], 'purpose':'research-screening-v1', 'epoch_id':EPOCH,
        'research_resilience_policy':'dependency_isolation_v1', 'screening_binding_sha256':_digest(screening_store,binding)}
    screening_store.freeze(identity, screening, acquired_at=datetime(2026,10,6,20,1,tzinfo=timezone.utc))
    for name in COHORTS:
        wire[name].update(committee_decision_status={'mode':'model','status':'no_candidates',
            'degraded':False,'reason':'','eligible_count':0,'input_count':0,'selected_count':0},
            research_screening_receipt=screening['receipt'])
        with sqlite3.connect(state/name/'portfolio.db') as db:
            _insert(db,'metric_epochs',epoch_id=EPOCH,generation_id=GENERATION,status='open',start_session=SESSION,end_session=None)
            economic={'market':{'benchmarks':[{'symbol':'SPY'}]}}
            db.execute('UPDATE session_execution_contexts SET economic_inputs_json=?,input_digest=?,market_digest=?',
                (json.dumps(economic),stable_id('session_economic_inputs',economic),stable_id('session_market_inputs',economic['market'])))
            for phase in PHASES:
                _insert(db,'session_phases',session_phase_id=name+phase,cohort_id=name,session=SESSION,phase=phase,completed_at=SESSION)
            _insert(db,'benchmark_observations',cohort_id=name,session=SESSION,epoch_id=EPOCH,symbol='SPY',valid=1)
            db.execute('UPDATE staging_runs SET policy_id=?',(policy,))
            _insert(db,'committee_decisions',session=SESSION,epoch_id=EPOCH,policy_id=policy,
                payload_json=json.dumps({'status':wire[name]['committee_decision_status'],'recommendations':[],'policy_decisions':[]}))
    db.close()
    if filing_exclusion:
        from tradingagents.strategies.orchestration.operational_report import _health_coverage
        failures=_health_coverage(records,EPOCH,SESSION,lambda *args:None)
        for name in wire:
            wire[name].update(degraded=True,input_coverage_valid=False,
                source_health_failures=[f for f in failures if name in f['affected_cohorts']])
    attempt = _attempt(repo, wire, outcome='degraded' if filing_exclusion else 'clean',
        return_code=2 if filing_exclusion else 0)
    identity = dict(session=SESSION,generation_id=GENERATION,generation_commit=COMMIT,profile_sha256=sha,epoch_id=EPOCH)
    replay = dict(version='local-continuity-replay-v1',**identity,zero_calls=True,replayed_cohorts=sorted(COHORTS),
        **{key:'c'*64 for key in ('native_before','native_after','source_before','source_after','ledger_before','ledger_after')})
    anchor = repo/'data/local-continuity/sessions'/SESSION/'replay-anchor.json'
    anchor.parent.mkdir(parents=True)
    anchor.write_text(json.dumps(replay))
    replay['anchor_sha256']=hashlib.sha256(anchor.read_bytes()).hexdigest()
    cleanup=dict(version='local-continuity-cleanup-v1',**identity,owned_workers_reaped=True,inputs_unchanged=True)
    subprocess.run(['git','init',str(repo)],check=True,capture_output=True)
    from tradingagents.strategies.orchestration.runtime_lock import canonical_runtime_lock_path
    lock=canonical_runtime_lock_path(repo)
    lock.parent.mkdir(parents=True)
    lock.touch()
    return repo,state,wire,sha,replay,cleanup,attempt


def collect(evidence, **overrides):
    from tradingagents.strategies.orchestration.local_continuity_evidence import collect_session
    repo,_,_,sha,replay,cleanup,_=evidence
    return collect_session(repo,GENERATION,SESSION,expected_commit=COMMIT,expected_profile=sha,
        replay_receipt=overrides.get('replay',replay),cleanup_receipt=overrides.get('cleanup',cleanup))


def test_healthy_no_event_uses_real_report_and_strict_all_book_ledger(evidence):
    result=collect(evidence)
    assert result['qualifies'], result
    assert result['checks']['no_model_work_required'] is True
    assert result['native_models']==[]
    assert result['attempts']==1 and result['epoch_id']==EPOCH
    assert result['fill_lifecycle_observed'] is False


@pytest.mark.parametrize('kind', ['replay_identity','replay_hash','anchor','cleanup_epoch','extra_receipt','zero_calls'])
def test_independent_receipts_must_be_bound_and_anchored(evidence,kind):
    replay,cleanup=deepcopy(evidence[4]),deepcopy(evidence[5])
    if kind=='replay_identity': replay['profile_sha256']='d'*64
    elif kind=='replay_hash': replay['ledger_after']='d'*64
    elif kind=='anchor': replay['anchor_sha256']='d'*64
    elif kind=='cleanup_epoch': cleanup['epoch_id']='other'
    elif kind=='extra_receipt': replay['trusted']=True
    else: replay['zero_calls']=False
    result=collect(evidence,replay=replay,cleanup=cleanup)
    assert not result['qualifies']
    assert ('cleanup' if kind=='cleanup_epoch' else 'replay') in result['reasons']


def test_one_missing_accounting_phase_cannot_be_laundered_by_operational_report(evidence):
    with sqlite3.connect(evidence[1]/COHORTS[0]/'portfolio.db') as db:
        db.execute('DELETE FROM session_phases WHERE phase=?',(PHASES[0],))
    result=collect(evidence)
    assert not result['qualifies'] and 'accounting' in result['reasons']


@pytest.mark.parametrize('kind',['source_digest','screening_epoch','durable_health','profile','second_attempt','committee_hold','late'])
def test_corrupted_provenance_and_holds_fail_closed(evidence,kind):
    repo,state,wire,sha,replay,cleanup,attempt=evidence
    slot=configuration_fingerprint({'generation':GENERATION,'session':SESSION})+'.json'
    if kind in ('source_digest','screening_epoch'):
        path=state/'source_inputs'/('screening' if kind=='screening_epoch' else '')/slot
        document=SourceInputStore.decode(path.read_text())
        if kind=='source_digest': document['digest']='d'*64
        else: document['identity']['epoch_id']='other'
        path.write_text(SourceInputStore.encode(document))
    elif kind=='durable_health':
        with sqlite3.connect(state/'metrics_v2.sqlite3') as db:
            row=db.execute('SELECT health_id,payload_json FROM strategy_health LIMIT 1').fetchone()
            payload=json.loads(row[1]);payload['evidence']['screen_completed']=False
            db.execute('UPDATE strategy_health SET payload_json=? WHERE health_id=?',(json.dumps(payload),row[0]))
    elif kind=='profile': (state/'generation-config.json').write_text('{}')
    elif kind=='second_attempt': _attempt(repo,wire,ordinal=2)
    elif kind=='committee_hold':
        document=json.loads(attempt.read_text())
        from tradingagents.strategies.orchestration.run_outcome import DAILY_RESULT_PREFIX
        wire[COHORTS[0]]['committee_decision_status'].update(status='research_hold',degraded=True)
        document['stdout']=DAILY_RESULT_PREFIX+json.dumps({'wire_version':DAILY_RESULT_WIRE_VERSION,'cohort_results':wire})
        attempt.write_text(json.dumps(document))
    else:
        document=json.loads(attempt.read_text());document['finished_at']='2026-10-07T14:00:00+00:00'
        attempt.write_text(json.dumps(document))
    assert not collect(evidence)['qualifies']


def _rewrite_screen(evidence, mutate):
    repo,state,wire,_,_,_,attempt=evidence
    slot=configuration_fingerprint({'generation':GENERATION,'session':SESSION})+'.json'
    path=state/'source_inputs/screening'/slot
    envelope=SourceInputStore.decode(path.read_text())
    mutate(envelope['payload'])
    store=SourceInputStore(repo/'cache')
    envelope['payload']['receipt']=_receipt(store,envelope['payload'])
    envelope['digest']=hashlib.sha256(SourceInputStore.encode(envelope['payload']).encode()).hexdigest()
    path.write_text(SourceInputStore.encode(envelope))
    for value in wire.values():
        value['research_screening_receipt']=envelope['payload']['receipt']
    document=json.loads(attempt.read_text())
    from tradingagents.strategies.orchestration.run_outcome import DAILY_RESULT_PREFIX
    document['stdout']=DAILY_RESULT_PREFIX+json.dumps({'wire_version':DAILY_RESULT_WIRE_VERSION,'cohort_results':wire})
    attempt.write_text(json.dumps(document))


@pytest.mark.parametrize('evidence',['filing_exclusion'],indirect=True)
def test_only_original_configured_edgar_dependency_failure_is_approved(evidence):
    result=collect(evidence)
    assert result['qualifies'],result
    assert result['status']=='qualifying_reduced_scope'
    assert result['source_observations']['edgar']=='failed'
    assert result['unexpected_failures']==[]


@pytest.mark.parametrize('kind',['failed','wrong_model','api','missing_response','low_effort','held','wrong_strategy'])
def test_failed_or_non_native_model_outcome_cannot_supply_model_credit(evidence,kind):
    from tradingagents.strategies.orchestration.research_model_attempt import request_receipt
    provenance={'configured_model':'gpt-6-luna','returned_model':'gpt-6-luna',
        'reasoning_effort':'high','billing_mode':'chatgpt_plan','effective_output_limit':None,'response_id':'resp_1'}
    outcome={'version':'research-model-outcome-v1','model_request':request_receipt(
        model='gpt-6-luna',role='bounded',effort='high',max_tokens=4096,temperature=0.0,system='system',prompt='prompt'),
        'response_received':True,'response_sha256':'e'*64,'response_bytes':100,'response_encoding':'utf-8',
        'analysis_status':'validated','analysis_failure_reason':None,'model_provenance':provenance}
    if kind=='failed': outcome['analysis_status']='failed'
    elif kind=='wrong_model': provenance['returned_model']='gpt-6.1-sol'
    elif kind=='api': provenance['billing_mode']='api'
    elif kind=='missing_response': outcome['response_received']=False
    elif kind=='low_effort': provenance['reasoning_effort']='low'
    _rewrite_screen(evidence,lambda payload:payload['horizons']['30d']['signals'].append(
        {'strategy':'litigation','metadata':{'analysis_status':'validated','research_model_outcome':outcome}}))
    if kind=='held':
        _rewrite_screen(evidence,lambda p:p['horizons']['30d']['signals'][0]['metadata'].update(non_actionable_reason='model_sample_incomplete'))
    elif kind=='wrong_strategy':
        _rewrite_screen(evidence,lambda p:p['horizons']['30d']['signals'][0].update(strategy='invented_strategy'))
    result=collect(evidence)
    assert not result['qualifies'] and result['checks']['model_outcomes'] is False


def test_complete_native_outcome_is_observed_without_forcing_a_second_model(evidence):
    from tradingagents.strategies.orchestration.research_model_attempt import request_receipt
    outcome={'version':'research-model-outcome-v1','model_request':request_receipt(
        model='gpt-6-luna',role='bounded',effort='high',max_tokens=4096,temperature=0.0,system='system',prompt='prompt'),
        'response_received':True,'response_sha256':'e'*64,'response_bytes':100,'response_encoding':'utf-8',
        'analysis_status':'validated','analysis_failure_reason':None,'model_provenance':{
            'configured_model':'gpt-6-luna','returned_model':'gpt-6-luna','reasoning_effort':'high',
            'billing_mode':'chatgpt_plan','effective_output_limit':None,'response_id':'resp_1'}}
    _rewrite_screen(evidence,lambda payload:payload['horizons']['30d']['signals'].append(
        {'strategy':'litigation','metadata':{'analysis_status':'validated','research_model_outcome':outcome}}))
    result=collect(evidence)
    assert result['qualifies'],result
    assert result['native_models']==['gpt-6-luna']
    assert result['checks']['no_model_work_required'] is False


def _rewrite_health(evidence, mutate):
    state=evidence[1]
    with sqlite3.connect(state/'metrics_v2.sqlite3') as db:
        values=[]
        for hid,body in db.execute('SELECT health_id,payload_json FROM strategy_health'):
            item=json.loads(body);mutate(item)
            values.append((json.dumps(item),hid))
        db.executemany('UPDATE strategy_health SET payload_json=? WHERE health_id=?',values)
    def change(payload):
        for horizon in payload['horizons'].values():
            for health in horizon['health']: mutate(health)
    _rewrite_screen(evidence,change)


def test_no_event_requires_completed_screen_not_only_status_label(evidence):
    _rewrite_health(evidence,lambda health:health['evidence'].update(screen_completed=False))
    result=collect(evidence)
    assert not result['qualifies'] and not result['checks']['no_model_work_required']


def test_source_acquisition_must_belong_to_the_normal_live_attempt(evidence):
    state=evidence[1]
    slot=configuration_fingerprint({'generation':GENERATION,'session':SESSION})+'.json'
    path=state/'source_inputs'/slot
    envelope=SourceInputStore.decode(path.read_text())
    envelope['acquired_at']=datetime(2026,10,5,20,tzinfo=timezone.utc)
    path.write_text(SourceInputStore.encode(envelope))
    assert not collect(evidence)['qualifies']


def test_retained_state_event_outcome_is_validated_even_if_no_priceable_signal(evidence):
    def add(health):
        if health['strategy']=='state_economics':
            health['evidence']['state_economic_events']=[{'analysis_status':'failed',
                'analysis_failure_reason':'analysis_unavailable','research_model_outcome':{}}]
    _rewrite_health(evidence,add)
    result=collect(evidence)
    assert not result['qualifies'] and not result['checks']['model_outcomes']


@pytest.mark.parametrize('decision_status', ['abstained', 'selected'])
def test_successful_native_sol_committee_is_observed_from_wire_and_ledger(evidence, decision_status):
    from tradingagents.strategies.orchestration.research_model_attempt import request_receipt
    repo,state,wire,sha,_,_,attempt=evidence
    status={'mode':'model','status':decision_status,'degraded':False,'reason':'',
        'eligible_count':1,'input_count':1,'selected_count':1 if decision_status=='selected' else 0,
        'model_request':request_receipt(model='gpt-6.1-sol',role='committee',effort='high',
            max_tokens=8192,temperature=0.0,system='system',prompt='prompt'),
        'model_provenance':{'configured_model':'gpt-6.1-sol','returned_model':'gpt-6.1-sol',
            'reasoning_effort':'high','billing_mode':'chatgpt_plan','effective_output_limit':None,'response_id':'resp_sol'}}
    wire[COHORTS[0]]['committee_decision_status']=status
    with sqlite3.connect(state/COHORTS[0]/'portfolio.db') as db:
        db.execute('UPDATE committee_decisions SET payload_json=?',
            (json.dumps({'status':status,'recommendations':[],'policy_decisions':[]}),))
    _rewrite_screen(evidence,lambda payload:None)
    result=collect(evidence)
    assert result['qualifies'],result
    assert result['native_models']==['gpt-6.1-sol']


def test_collector_does_not_modify_retained_files(evidence):
    # sqlite's context manager commits without closing; release fixture writers
    # before measuring the collector, so writer GC cannot checkpoint during it.
    gc.collect()
    repo=evidence[0]
    before={str(path.relative_to(repo)):hashlib.sha256(path.read_bytes()).hexdigest()
            for path in repo.rglob('*') if path.is_file() and not path.name.endswith(('-shm','-wal'))}
    assert collect(evidence)['qualifies']
    after={str(path.relative_to(repo)):hashlib.sha256(path.read_bytes()).hexdigest()
           for path in repo.rglob('*') if path.is_file() and not path.name.endswith(('-shm','-wal'))}
    assert after==before


@pytest.mark.parametrize('evidence',['filing_exclusion'],indirect=True)
def test_other_source_failure_cannot_hide_behind_approved_edgar_exclusion(evidence):
    def add(health):
        if 'edgar' in health['evidence']['data_sources']:
            health.update(status='data_failure')
            health['evidence']['provider_errors']={'edgar':'filing_research_disabled','fred':'provider_error'}
    _rewrite_health(evidence,add)
    result=collect(evidence)
    assert not result['qualifies'] and not result['checks']['screening']


def test_health_dependency_scope_must_match_configured_strategy(evidence):
    _rewrite_health(evidence,lambda health:health['evidence'].update(data_sources=['invented_source']))
    assert not collect(evidence)['qualifies']
