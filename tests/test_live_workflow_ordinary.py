"""Ordinary session admission/continuity tests never start native providers."""
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.live_workflow import ordinary, runner


@pytest.fixture
def campaign(tmp_path,monkeypatch):
    root=tmp_path/'campaign';root.mkdir(mode=0o700)
    (root/'logs').mkdir();(root/'repo').mkdir();(root/'tape').mkdir()
    contract={'candidate_commit':'a'*40,'reference_session':'2026-11-02','profile_sha256':'b'*64}
    row={'gen_id':'gen_001'}
    (root/'contract.json').write_text(json.dumps(contract))
    (root/'hook-config.json').write_text(json.dumps({'run_identity':{'candidate_commit':'a'*40}}))
    monkeypatch.setattr(runner,'verify_contract',lambda root:(contract,row))
    monkeypatch.setattr(runner,'require_boundary',lambda root,path:'c'*64)
    monkeypatch.setattr(runner,'tape_coverage',lambda root:{'unfinished_reservations':0})
    from tools.live_workflow import recorded_io
    monkeypatch.setattr(recorded_io,'verify_tape',lambda root:[])
    clock=[datetime(2026,11,2,23,5,tzinfo=timezone.utc)]
    monkeypatch.setattr(ordinary,'_utc_now',lambda:clock[0])
    seen=[];controls={'native_fail':False,'report_fail':False,'calendar_next':'2026-11-03'}
    checks={**{name:True for name in ('accounting_valid','staging_complete','staging_valid',
        'evidence_complete','input_coverage_valid')},'outcome':'clean','generation_id':'gen_001',
        'generation_commit':'a'*40,'profile_sha256':'b'*64,'fills':[],
        'cohorts':{book:{'accounting_valid':True,'staging_complete':True,'staging_valid':True} for book in runner.BOOKS}}
    def process(argv,root,*,env,label,timeout):
        seen.append((argv,env,label))
        receipt={'argv':argv,'returncode':0,'failure':None}
        log=root/'logs'/(label+'.log');log.write_bytes(b'private bounded log\n')
        receipt['console_sha256']=runner.sha(log)
        runner.private_json(root/(label+'-process.json'),receipt)
        if 'ordinary-window' in argv:
            out=Path(argv[argv.index('--output-dir')+1])
            runner.private_json(out/'ordinary-window.json',{'observed_at':clock[0].isoformat(),
                'session':argv[argv.index('--session')+1],'allowed':True,'ordinary_idle_expected':False,
                'expected_next_session':controls['calendar_next'] if '--previous-session' in argv else None})
        elif 'report' in argv:
            out=Path(argv[argv.index('--output-dir')+1]);session=argv[argv.index('--session')+1]
            runner.private_json(out/'native-report.json',{**checks,'session':session})
            runner.private_json(out/'report-reproduction.json',{'canonical_json_identical':True,'canonical_markdown_identical':True})
            if controls['report_fail']:raise runner.AcceptanceError('report_failed')
        elif controls['native_fail']:raise runner.AcceptanceError('native_failed')
        return receipt
    monkeypatch.setattr(runner,'run_process',process)
    args=SimpleNamespace(root=str(root),session='2026-11-02',boundary_receipt='/private/boundary.json',credential_env=[])
    return root,args,clock,controls,seen


@pytest.mark.parametrize('now,session',[
    (datetime(2026,11,2,22,59,tzinfo=timezone.utc),'2026-11-02'),
    (datetime(2026,11,3,23,5,tzinfo=timezone.utc),'2026-11-02'),
    (datetime(2026,11,1,23,5,tzinfo=timezone.utc),'2026-11-01'),
])
def test_wrong_day_before18_and_weekend_never_launch(campaign,now,session):
    root,args,clock,controls,seen=campaign;clock[0]=now;args.session=session
    with pytest.raises(runner.AcceptanceError):ordinary.run_ordinary(args)
    assert seen==[]


def test_native_tick_uses_ordinary_argv_and_accepts_real_zero_fills(campaign):
    root,args,clock,controls,seen=campaign
    result=ordinary.run_ordinary(args)
    assert result['status']=='pass' and runner.native_acceptance_passed(result)
    native=[argv for argv,env,label in seen if 'recover_runtime.py' in argv[1]]
    assert native==[[str(root/'repo/.venv/bin/python'),str(root/'repo/scripts/recover_runtime.py'),'--repo-root',str(root/'repo')]]
    assert not any('--prospective-session' in argv for argv,env,label in seen)
    assert result['ordinary_native_clock'] is True
    assert result['actual_timer_dispatch'] is False
    assert (root/'ordinary-sessions/2026-11-02/native-report.json').is_file()
    assert not (root/'native-report.json').exists()


def test_duplicate_attempt_never_starts_a_second_tick(campaign):
    root,args,clock,controls,seen=campaign;ordinary.run_ordinary(args);count=len(seen)
    with pytest.raises(runner.AcceptanceError):ordinary.run_ordinary(args)
    assert len(seen)==count


def test_first_session_must_match_original_contract_reference(campaign):
    root,args,clock,controls,seen=campaign;args.session='2026-11-03';clock[0]=datetime(2026,11,3,23,5,tzinfo=timezone.utc)
    with pytest.raises(runner.AcceptanceError,match='first_session'):ordinary.run_ordinary(args)
    assert not seen


def test_next_session_continues_without_changing_original_identity(campaign):
    root,args,clock,controls,seen=campaign
    initial=(root/'contract.json').read_bytes();ordinary.run_ordinary(args)
    args.session='2026-11-03';clock[0]=datetime(2026,11,3,23,5,tzinfo=timezone.utc)
    result=ordinary.run_ordinary(args)
    assert result['status']=='pass' and result['previous_session']=='2026-11-02'
    assert (root/'contract.json').read_bytes()==initial
    assert len([1 for argv,env,label in seen if 'recover_runtime.py' in argv[1]])==2
    assert len({label for argv,env,label in seen})==len(seen)


def test_session_gap_does_not_start_native_tick(campaign):
    root,args,clock,controls,seen=campaign;ordinary.run_ordinary(args)
    args.session='2026-11-04';clock[0]=datetime(2026,11,4,23,5,tzinfo=timezone.utc)
    result=ordinary.run_ordinary(args)
    assert result['status']=='fail'
    assert len([1 for argv,env,label in seen if 'recover_runtime.py' in argv[1]])==1


def test_failure_cannot_advance_or_retry_campaign(campaign):
    root,args,clock,controls,seen=campaign;controls['native_fail']=True
    result=ordinary.run_ordinary(args);assert result['status']=='fail'
    with pytest.raises(runner.AcceptanceError):ordinary.run_ordinary(args)
    args.session='2026-11-03';clock[0]=datetime(2026,11,3,23,5,tzinfo=timezone.utc)
    with pytest.raises(runner.AcceptanceError,match='prior_session'):ordinary.run_ordinary(args)
    assert len([1 for argv,env,label in seen if 'recover_runtime.py' in argv[1]])==1


def test_failed_report_retains_partial_evidence_without_passing(campaign):
    root,args,clock,controls,seen=campaign;controls['report_fail']=True
    result=ordinary.run_ordinary(args)
    assert result['status']=='fail'
    assert (root/'ordinary-sessions/2026-11-02/native-report.json').is_file()
    assert (root/'ordinary-sessions/2026-11-02/result.json').is_file()


def test_selected_source_credentials_only_native_memory_and_redacted_logs(campaign,monkeypatch):
    root,args,clock,controls,seen=campaign
    monkeypatch.setenv('ALPACA_API_KEY','private-sentinel-value');monkeypatch.setenv('HTTPS_PROXY','untrusted-route')
    args.credential_env=['ALPACA_API_KEY'];ordinary.run_ordinary(args)
    native=[env for argv,env,label in seen if 'recover_runtime.py' in argv[1]][0]
    assert native['ALPACA_API_KEY']=='private-sentinel-value' and 'HTTPS_PROXY' not in native
    for path in (root/'ordinary-sessions').rglob('*.json'):
        assert 'private-sentinel-value' not in path.read_text()


@pytest.mark.parametrize('old_evidence',['prospective_attempt','canonical_report','native_attempt'])
def test_existing_session_evidence_cannot_be_borrowed_by_ordinary_lane(campaign,old_evidence):
    root,args,clock,controls,seen=campaign
    if old_evidence=='prospective_attempt':(root/'native-attempt.json').write_text('{}')
    elif old_evidence=='canonical_report':
        path=root/'repo/docs/reports/2026-11-02-gen_001-operational-report.json'
        path.parent.mkdir(parents=True);path.write_text('{}')
    else:
        path=root/'repo/data/logs/run_attempts/prior.json';path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'generation_id':'gen_001','requested_session':'2026-11-02'}))
    with pytest.raises(runner.AcceptanceError,match='ordinary_.*existing'):
        ordinary.run_ordinary(args)
    assert not seen


def test_candidate_calendar_can_reject_nontrading_holiday(campaign,monkeypatch):
    root,args,clock,controls,seen=campaign;process=runner.run_process
    def not_allowed(argv,*positional,**kwargs):
        result=process(argv,*positional,**kwargs)
        if 'ordinary-window' in argv:
            path=Path(argv[argv.index('--output-dir')+1])/'ordinary-window.json'
            value=json.loads(path.read_bytes());value['ordinary_idle_expected']=True;path.write_text(json.dumps(value))
        return result
    monkeypatch.setattr(runner,'run_process',not_allowed)
    assert ordinary.run_ordinary(args)['status']=='fail'
    assert not any('recover_runtime.py' in argv[1] for argv,env,label in seen)


def test_bootstrap_ordinary_window_writes_unique_output_with_native_calendar(tmp_path,monkeypatch):
    import sys
    from types import ModuleType
    from datetime import date
    from tools.live_workflow import bootstrap
    output=tmp_path/'session';output.mkdir()
    clock=datetime(2026,11,3,23,5,tzinfo=timezone.utc)
    decision=ModuleType('tradingagents.strategies.orchestration.decision_clock')
    decision.latest_completed_session=lambda now:date(2026,11,3)
    decision.require_reference=lambda session,now:None
    calendar=ModuleType('tradingagents.strategies.orchestration.trading_calendar')
    calendar.exchange_date=lambda now:date(2026,11,3)
    calendar.is_session=lambda session:True
    calendar.next_session=lambda session:date(2026,11,3)
    monkeypatch.setitem(sys.modules,decision.__name__,decision)
    monkeypatch.setitem(sys.modules,calendar.__name__,calendar)
    monkeypatch.setattr(bootstrap,'datetime',SimpleNamespace(now=lambda zone:clock))
    bootstrap._ordinary_window(tmp_path,'2026-11-03','2026-11-02',output)
    value=json.loads((output/'ordinary-window.json').read_bytes())
    assert value=={'observed_at':clock.isoformat(),'session':'2026-11-03','allowed':True,
        'ordinary_idle_expected':False,'expected_next_session':'2026-11-03'}
    assert not (tmp_path/'window.json').exists()


def test_bootstrap_report_routes_private_output_and_preserves_canonical_reports(tmp_path,monkeypatch):
    import sys
    from types import ModuleType
    from tools.live_workflow import bootstrap
    output=tmp_path/'session';output.mkdir();repo=tmp_path/'repo';canonical=repo/'docs/reports';canonical.mkdir(parents=True)
    (tmp_path/'generation.json').write_text(json.dumps({'gen_id':'gen_001'}))
    operational=ModuleType('tradingagents.strategies.orchestration.operational_report')
    operational.build_operational_report=lambda root,generation,session:{'session':session}
    operational.render_operational_report=lambda report:'native canonical markdown\n'
    incidents=ModuleType('tradingagents.strategies.orchestration.source_incidents')
    incidents.load_source_incidents=lambda report,folder:[]
    monkeypatch.setitem(sys.modules,operational.__name__,operational)
    monkeypatch.setitem(sys.modules,incidents.__name__,incidents)
    monkeypatch.setattr(bootstrap,'_imports',lambda repo:None)
    json_path=canonical/'2026-11-02-gen_001-operational-report.json'
    md_path=canonical/'2026-11-02-gen_001-operational-report.md'
    json_path.write_text(json.dumps({'session':'2026-11-02','source_incidents':[]}));md_path.write_text('native canonical markdown\n')
    original=(json_path.read_bytes(),md_path.read_bytes())
    bootstrap._report(tmp_path,'2026-11-02',output)
    assert (json_path.read_bytes(),md_path.read_bytes())==original
    assert not (tmp_path/'native-report.json').exists()
    assert json.loads((output/'report-reproduction.json').read_bytes())['canonical_json_identical'] is True


def test_third_session_uses_immediately_previous_accepted_session(campaign):
    root,args,clock,controls,seen=campaign
    first=ordinary.run_ordinary(args);assert first['expected_next_session'] is None
    args.session='2026-11-03';clock[0]=datetime(2026,11,3,23,5,tzinfo=timezone.utc)
    second=ordinary.run_ordinary(args)
    assert second['expected_next_session']=='2026-11-03'
    args.session='2026-11-04';clock[0]=datetime(2026,11,4,23,5,tzinfo=timezone.utc)
    controls['calendar_next']='2026-11-04'
    third=ordinary.run_ordinary(args)
    assert third['status']=='pass' and third['previous_session']=='2026-11-03'
    calendar_commands=[argv for argv,env,label in seen if 'ordinary-window' in argv]
    assert calendar_commands[1][calendar_commands[1].index('--previous-session')+1]=='2026-11-02'
    assert calendar_commands[2][calendar_commands[2].index('--previous-session')+1]=='2026-11-03'
    assert len([1 for argv,env,label in seen if 'recover_runtime.py' in argv[1]])==3


def test_failed_second_session_blocks_third_without_another_child(campaign):
    root,args,clock,controls,seen=campaign;ordinary.run_ordinary(args)
    args.session='2026-11-03';clock[0]=datetime(2026,11,3,23,5,tzinfo=timezone.utc)
    controls['report_fail']=True
    assert ordinary.run_ordinary(args)['status']=='fail'
    count=len(seen);args.session='2026-11-04';clock[0]=datetime(2026,11,4,23,5,tzinfo=timezone.utc)
    with pytest.raises(runner.AcceptanceError,match='prior_session'):
        ordinary.run_ordinary(args)
    assert len(seen)==count


def test_postcapture_failure_cannot_overwrite_failed_status_with_pass(campaign,monkeypatch):
    root,args,clock,controls,seen=campaign;events=ordinary._events;calls=[]
    def fail_after_native(root):
        calls.append(True)
        if len(calls)>1:raise ValueError('private source text must not be retained')
        return events(root)
    monkeypatch.setattr(ordinary,'_events',fail_after_native)
    result=ordinary.run_ordinary(args)
    assert result['native_process_success'] is True and result['report_process_success'] is True
    assert runner.native_acceptance_passed(result) is True
    assert result['identity_verified'] is False and result['status']=='fail'
    assert result['reason']=='ordinary_post_run_verification_failed'
    assert 'private source text' not in json.dumps(result)


def test_wrong_report_session_cannot_overwrite_failed_status_with_pass(campaign,monkeypatch):
    root,args,clock,controls,seen=campaign;process=runner.run_process
    def wrong_report(argv,*positional,**kwargs):
        result=process(argv,*positional,**kwargs)
        if 'report' in argv:
            path=Path(argv[argv.index('--output-dir')+1])/'native-report.json'
            value=json.loads(path.read_bytes());value['session']='2026-10-30';path.write_text(json.dumps(value))
        return result
    monkeypatch.setattr(runner,'run_process',wrong_report)
    result=ordinary.run_ordinary(args)
    assert result['native_process_success'] is True and result['report_process_success'] is True
    assert result['identity_verified'] is True and runner.native_acceptance_passed(result) is True
    assert result.get('report_identity_verified') is not True and result['status']=='fail'


def test_same_native_calendar_successor_semantics_allow_weekend_jump(campaign):
    root,args,clock,controls,seen=campaign
    contract=json.loads((root/'contract.json').read_bytes());contract['reference_session']='2026-11-06'
    # The fixture's contract reader retains its original mapping: obtain it and
    # set this campaign's initial reference to a Friday without altering clocks.
    pinned,row=runner.verify_contract(root);pinned['reference_session']='2026-11-06'
    (root/'contract.json').write_text(json.dumps(contract))
    args.session='2026-11-06';clock[0]=datetime(2026,11,6,23,5,tzinfo=timezone.utc)
    assert ordinary.run_ordinary(args)['status']=='pass'
    args.session='2026-11-09';clock[0]=datetime(2026,11,9,23,5,tzinfo=timezone.utc)
    controls['calendar_next']='2026-11-09'
    result=ordinary.run_ordinary(args)
    assert result['status']=='pass' and result['previous_session']=='2026-11-06'


def test_ordinary_summary_absent_directory_does_not_claim_observation(campaign):
    root,args,clock,controls,seen=campaign
    value=ordinary.ordinary_coverage(root)
    assert value['status']=='not_observed' and value['accepted_sessions']==0
    assert value['accepted_sequence'] is False and not seen


def test_campaign_summary_reports_latest_ordinary_checks_and_preserves_unobserved_gaps(campaign):
    root,args,clock,controls,seen=campaign
    ordinary.run_ordinary(args)
    value=runner.summarize(root)
    assert value['native_lane']=='ordinary_real_clock_sessions'
    assert value['ordinary_sessions']['accepted_sessions']==1
    assert value['checks']['sixteen_books'] is True
    assert value['canonical_report_reproduction']=={
        'canonical_json_identical':True,'canonical_markdown_identical':True}
    assert 'timer_dispatch_unobserved' in value['coverage_gaps']
    assert 'subsequent_session_fills_unobserved' in value['coverage_gaps']
    assert 'ordinary_session_workflow_unobserved' not in value['coverage_gaps']
    assert 'multi_session_continuity_unobserved' in value['coverage_gaps']
    assert 'cold_acquisition_replay_not_executed' in value['diagnostic_coverage_gaps']
    assert value['acceptance_evidence_policy']=='live_sources_real_data_live_models_only'
    assert value['offline_checks_establish_live_readiness'] is False


def test_ordinary_summary_verifies_two_session_sequence_readonly(campaign):
    root,args,clock,controls,seen=campaign;ordinary.run_ordinary(args)
    args.session='2026-11-03';clock[0]=datetime(2026,11,3,23,5,tzinfo=timezone.utc)
    ordinary.run_ordinary(args);before=len(seen)
    value=ordinary.ordinary_coverage(root)
    assert value['status']=='accepted' and value['accepted_sequence'] is True
    assert value['accepted_sessions']==value['attempted_sessions']==2
    assert [row['session'] for row in value['sessions']]==['2026-11-02','2026-11-03']
    assert value['actual_timer_dispatch'] is False and len(seen)==before


def test_ordinary_summary_preserves_failed_last_session_without_qualifying_sequence(campaign):
    root,args,clock,controls,seen=campaign;ordinary.run_ordinary(args)
    args.session='2026-11-03';clock[0]=datetime(2026,11,3,23,5,tzinfo=timezone.utc)
    controls['report_fail']=True;ordinary.run_ordinary(args)
    value=ordinary.ordinary_coverage(root)
    assert value['status']=='failed' and value['accepted_sequence'] is False
    assert value['accepted_sessions']==1 and value['attempted_sessions']==2
    assert value['observed_last_result']['session']=='2026-11-03'
    assert value['observed_last_result']['status']=='fail'


def test_ordinary_summary_preserves_incomplete_durable_attempt(campaign):
    root,args,clock,controls,seen=campaign;ordinary.run_ordinary(args)
    (root/'ordinary-sessions/2026-11-02/result.json').unlink()
    value=ordinary.ordinary_coverage(root)
    assert value['status']=='incomplete' and value['accepted_sequence'] is False
    assert value['accepted_sessions']==0 and value['attempted_sessions']==1


@pytest.mark.parametrize('tamper',['profile','root','session','sequence','report','process','calendar','unknown_directory'])
def test_ordinary_summary_never_counts_corrupt_or_unbound_session_receipts(campaign,tamper):
    root,args,clock,controls,seen=campaign;ordinary.run_ordinary(args)
    output=root/'ordinary-sessions/2026-11-02';path=output/'result.json'
    if tamper in ('profile','root','session','sequence'):
        value=json.loads(path.read_bytes())
        value[{'profile':'profile_sha256','root':'root','session':'session','sequence':'previous_session'}[tamper]]='unbound'
        path.write_text(json.dumps(value))
    elif tamper=='report':
        path=output/'native-report.json';value=json.loads(path.read_bytes());value['profile_sha256']='unbound';path.write_text(json.dumps(value))
    elif tamper=='process':
        (root/'logs/ordinary-2026-11-02-native.log').write_bytes(b'changed process log')
    elif tamper=='calendar':
        path=output/'ordinary-window.json';value=json.loads(path.read_bytes());value['session']='2026-10-30';path.write_text(json.dumps(value))
    else:(root/'ordinary-sessions/skipped').mkdir(mode=0o700)
    value=ordinary.ordinary_coverage(root)
    assert value['status']=='invalid' and value['accepted_sequence'] is False
    assert value['accepted_sessions']==0


@pytest.mark.parametrize('tamper',['boolean_shape','unbounded_reason','event_counts'])
def test_ordinary_summary_rejects_untrusted_result_shapes(campaign,tamper):
    root,args,clock,controls,seen=campaign;ordinary.run_ordinary(args)
    path=root/'ordinary-sessions/2026-11-02/result.json';value=json.loads(path.read_bytes())
    if tamper=='boolean_shape':value['checks']['accounting']='true'
    elif tamper=='unbounded_reason':value['reason']='private text must never enter summary'
    else:value['session_recorded_boundary_coverage']['model_events']=-1
    path.write_text(json.dumps(value));coverage=ordinary.ordinary_coverage(root)
    assert coverage['status']=='invalid' and coverage['accepted_sessions']==0
    assert 'private text' not in json.dumps(coverage)


def test_summary_two_verified_ordinary_sessions_classifies_continuity_without_fill_or_timer_claims(campaign):
    root,args,clock,controls,seen=campaign;ordinary.run_ordinary(args)
    args.session='2026-11-03';clock[0]=datetime(2026,11,3,23,5,tzinfo=timezone.utc)
    ordinary.run_ordinary(args);value=runner.summarize(root)
    assert 'ordinary_session_workflow_unobserved' not in value['coverage_gaps']
    assert 'multi_session_continuity_unobserved' not in value['coverage_gaps']
    assert 'subsequent_session_fills_unobserved' in value['coverage_gaps']
    assert 'timer_dispatch_unobserved' in value['coverage_gaps']
    assert value['ordinary_sessions']['actual_timer_dispatch'] is False


@pytest.mark.parametrize('artifact,damage',[
    ('attempt.json','missing'),
    ('native-report.json','missing'),
    ('native-report.json','corrupt'),
    ('report-reproduction.json','corrupt'),
    ('ordinary-window.json','missing'),
    ('native-process','corrupt'),
    ('native-console','corrupt'),
])
def test_next_session_refuses_lost_or_corrupt_prior_support_before_claim_or_children(campaign,artifact,damage):
    root,args,clock,controls,seen=campaign
    assert ordinary.run_ordinary(args)['status']=='pass'
    prior=root/'ordinary-sessions/2026-11-02'
    if artifact=='native-process':path=root/'ordinary-2026-11-02-native-process.json'
    elif artifact=='native-console':path=root/'logs/ordinary-2026-11-02-native.log'
    else:path=prior/artifact
    if damage=='missing':path.unlink()
    else:path.write_bytes(b'corrupt retained artifact')
    retained={str(file.relative_to(prior)):file.read_bytes() for file in prior.rglob('*') if file.is_file()}
    before=len(seen);args.session='2026-11-03';clock[0]=datetime(2026,11,3,23,5,tzinfo=timezone.utc)
    with pytest.raises(runner.AcceptanceError,match='ordinary_prior_session_evidence'):
        ordinary.run_ordinary(args)
    assert len(seen)==before
    assert not (root/'ordinary-sessions/2026-11-03').exists()
    assert {str(file.relative_to(prior)):file.read_bytes() for file in prior.rglob('*') if file.is_file()}==retained
