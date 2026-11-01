"""Actual scheduler admission policy; no systemd mutations or live workflows."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

import pytest

from tools.live_workflow import scheduled


@pytest.fixture
def receipt_owner(monkeypatch):
    # Real local filesystem operations; only the unavailable privileged owner
    # boundary is substituted. Root/foreign ownership rejection is separate.
    monkeypatch.setattr(scheduled,'ROOT_UID',os.getuid())
    monkeypatch.setattr(scheduled,'WORKER_UID',os.getuid())


def manifest(base=Path('/var/tmp/eventedge-scheduled-test-supervisor')):
    slots=[]
    for index in range(7):
        day=2+max(0,index-2)
        slots.append({'id':f'slot-{index}', 'action':'idle' if index<2 else 'ordinary',
            'session':f'2026-11-{day:02}',
            'scheduled_at':f'2026-11-{day:02}T23:00:00Z' if index>=2 else f'2026-11-01T02:0{index}:00Z',
            'window_seconds':600, 'service_unit':f'eventedge-test-{index}.service',
            'timer_unit':f'eventedge-test-{index}.timer',
            'previous_slot':None if index==0 else f'slot-{index-1}',
            'config':f'config-{index}.json','review':f'review-{index}.json'})
    files={name:'0'*64 for name in ('scheduled.py','supervisor.py','host_worker.py','inside.py')}
    files.update({s[k]:'1'*64 for s in slots for k in ('config','review')})
    return {'schema':'eventedge-real-schedule-v1','base':str(base),
        'campaign_root':'/var/tmp/eventedge-scheduled-test-case/campaign',
        'python':'/usr/bin/python3.12','files':files,'slots':slots}


def test_manifest_selects_only_fixed_pinned_command():
    value=manifest(); base=Path(value['base'])
    selected=scheduled.validate_manifest(value,base,'slot-2')
    assert scheduled.supervisor_command(base,selected)==[
        '/usr/bin/python3.12','-I','-B',str(base/'supervisor.py'),
        '--config',str(base/'config-2.json'),'--action','ordinary','--lane','live',
        '--review-file',str(base/'review-2.json'),'--execute']


@pytest.mark.parametrize('change',[
    lambda m:m.update(python='/tmp/python'),
    lambda m:m.update(extra='command'),
    lambda m:m['files'].update({'../escape':'a'*64}),
    lambda m:m['files'].pop('scheduled.py'),
    lambda m:m['slots'][0].update(config='/tmp/config'),
    lambda m:m['slots'][2].update(previous_slot='slot-0'),
    lambda m:m['slots'][2].update(action='native'),
    lambda m:m['slots'][2].update(window_seconds=601),
    lambda m:m['slots'][2].update(window_seconds=True),
    lambda m:m['slots'][2].update(scheduled_at='2026-11-02T23:00:00'),
    lambda m:m['slots'][2].update(scheduled_at='2026-11-03T23:00:00Z'),
    lambda m:m['slots'][2].update(timer_unit='other.service'),
    lambda m:m['slots'][3].update(service_unit=m['slots'][2]['service_unit']),
    lambda m:m['slots'].append(deepcopy(m['slots'][-1])),
])
def test_manifest_rejects_ambiguous_unpinned_or_unbounded_schedule(change):
    value=manifest();change(value)
    with pytest.raises(scheduled.Refused):
        scheduled.validate_manifest(value,Path(value['base']),'slot-2')


def properties(slot):
    service={'Id':slot['service_unit'],'InvocationID':'a'*32,'MainPID':'1234',
        'TriggeredBy':slot['timer_unit'],'ExecMainStartTimestampMonotonic':'100000000',
        'ActiveState':'active','SubState':'running','ControlGroup':'/system.slice/test.service'}
    timer={'Id':slot['timer_unit'],'Unit':slot['service_unit'],
        'LastTriggerUSecMonotonic':'100000000','ActiveState':'active'}
    return service,timer


def test_real_timer_admission_binds_current_process_and_clocks():
    slot=manifest()['slots'][2];service,timer=properties(slot)
    now=datetime(2026,11,2,23,0,1,tzinfo=timezone.utc)
    scheduled.verify_timer(slot,service,timer,now=now,monotonic_us=101000000,
        pid=1234,invocation_id='a'*32)


def test_exact_actual_systemd_monotonic_duration_retains_microseconds():
    assert scheduled.monotonic_microseconds('3w 3h 34min 45.340700s')==1827285340700
    assert scheduled.monotonic_microseconds('1827285340700')==1827285340700
    assert scheduled.monotonic_microseconds('1d 2h 3min 4s 5ms 6us')==93784005006
    slot=manifest()['slots'][2];service,timer=properties(slot)
    service['ExecMainStartTimestampMonotonic']='1827285360160'
    timer['LastTriggerUSecMonotonic']='3w 3h 34min 45.340700s'
    scheduled.verify_timer(slot,service,timer,now=datetime(2026,11,2,23,0,1,tzinfo=timezone.utc),
        monotonic_us=1827286340700,pid=1234,invocation_id='a'*32)


@pytest.mark.parametrize('value',['-1s','1s garbage','1year','NaN','1s 2s','1us 2s','0.5us','1e3s','1s +1ms',''])
def test_monotonic_duration_rejects_malformed_or_nonintegral_us(value):
    with pytest.raises(scheduled.Refused):scheduled.monotonic_microseconds(value)


@pytest.mark.parametrize('field,value',[
    ('InvocationID','b'*32),('MainPID','1235'),('TriggeredBy','other.timer'),
    ('ExecMainStartTimestampMonotonic','200000000'),('ActiveState','inactive'),
])
def test_foreign_or_manual_service_is_not_timer_evidence(field,value):
    slot=manifest()['slots'][2];service,timer=properties(slot);service[field]=value
    with pytest.raises(scheduled.Refused):
        scheduled.verify_timer(slot,service,timer,now=datetime(2026,11,2,23,0,1,tzinfo=timezone.utc),
            monotonic_us=101000000,pid=1234,invocation_id='a'*32)


@pytest.mark.parametrize('delta',[-1,601])
def test_timer_rejects_early_or_missed_wallclock_window(delta):
    from datetime import timedelta
    slot=manifest()['slots'][2];service,timer=properties(slot)
    with pytest.raises(scheduled.Refused):
        scheduled.verify_timer(slot,service,timer,
            now=datetime(2026,11,2,23,tzinfo=timezone.utc)+timedelta(seconds=delta),
            monotonic_us=101000000,pid=1234,invocation_id='a'*32)


@pytest.mark.parametrize('field,value',[
    ('Unit','other.service'),('LastTriggerUSecMonotonic','0'),
    ('LastTriggerUSecMonotonic','102000000'),('LastTriggerUSecMonotonic','-1'),
])
def test_timer_rejects_wrong_target_or_stale_future_trigger(field,value):
    slot=manifest()['slots'][2];service,timer=properties(slot);timer[field]=value
    with pytest.raises(scheduled.Refused):
        scheduled.verify_timer(slot,service,timer,now=datetime(2026,11,2,23,0,1,tzinfo=timezone.utc),
            monotonic_us=101000000,pid=1234,invocation_id='a'*32)


def test_durable_claim_refuses_even_incomplete_attempt_and_lock_overlap(tmp_path,receipt_owner):
    with scheduled.dispatch_lock(tmp_path):
        with pytest.raises(scheduled.Refused,match='dispatch_busy'):
            with scheduled.dispatch_lock(tmp_path):pass
        folder=scheduled.claim_slot(tmp_path,'slot-0',{'slot':'slot-0'})
        assert json.loads((folder/'attempt.json').read_bytes())=={'slot':'slot-0'}
        with pytest.raises(scheduled.Refused,match='slot_already_claimed'):
            scheduled.claim_slot(tmp_path,'slot-0',{'slot':'slot-0'})


def test_previous_slot_requires_pass_and_unchanged_final_digest(tmp_path,receipt_owner):
    slot=manifest()['slots'][1]
    previous=scheduled.claim_slot(tmp_path,'slot-0',{})
    scheduled.finish_slot(previous,{'status':'pass','slot':'slot-0','campaign_root':manifest()['campaign_root']})
    expected=scheduled.previous_result(tmp_path,slot,manifest()['campaign_root'])
    assert expected==hashlib.sha256((previous/'result.json').read_bytes()).hexdigest()
    (previous/'result.json').write_text('{"status":"pass"}')
    with pytest.raises(scheduled.Refused):scheduled.previous_result(tmp_path,slot,manifest()['campaign_root'])


def test_missing_or_failed_predecessor_blocks_sequence(tmp_path,receipt_owner):
    slot=manifest()['slots'][1]
    with pytest.raises(scheduled.Refused):scheduled.previous_result(tmp_path,slot,manifest()['campaign_root'])
    folder=scheduled.claim_slot(tmp_path,'slot-0',{})
    scheduled.finish_slot(folder,{'status':'fail','slot':'slot-0','campaign_root':manifest()['campaign_root']})
    with pytest.raises(scheduled.Refused):scheduled.previous_result(tmp_path,slot,manifest()['campaign_root'])


def test_control_reader_rejects_writable_symlink_hardlink_or_changed_content(tmp_path):
    control=tmp_path/'control';control.write_bytes(b'{}');control.chmod(0o400)
    digest=hashlib.sha256(b'{}').hexdigest()
    assert scheduled.read_control(control,digest,owner_uid=os.getuid())==b'{}'
    control.chmod(0o600)
    with pytest.raises(scheduled.Refused):scheduled.read_control(control,digest,owner_uid=os.getuid())
    control.chmod(0o400);link=tmp_path/'link';link.symlink_to(control)
    with pytest.raises(scheduled.Refused):scheduled.read_control(link,digest,owner_uid=os.getuid())
    os.link(control,tmp_path/'hardlink')
    with pytest.raises(scheduled.Refused):scheduled.read_control(control,digest,owner_uid=os.getuid())


def child_result(action,campaign,runid):
    if action=='idle':
        return {'schema_version':1,'lane':'ordinary_idle_tick','root':str(campaign),
            'attempt_id':runid,'status':'pass','identity_verified':True,
            'native_process_success':True,'non_session_verified':True,
            'native_idle_projection_verified':True,
            'generation_state_unchanged':True,'journal_incidents_empty':True,
            'source_model_event_delta':{'source_events':0,'model_events':0},
            'unfinished_reservations':0}
    return {'status':'pass','root':str(campaign),'session':'2026-11-02'}


def test_worker_result_requires_cleanup_exact_identity_and_child_pass(tmp_path,receipt_owner):
    base=tmp_path/'base';base.mkdir();campaign=tmp_path/'campaign';campaign.mkdir()
    run=base/'runs/run-1';run.mkdir(parents=True)
    unit={'unit':'native.service','returncode':0,'abort':None,
        'actual_properties':{'InvocationID':'b'*32},'descendant_cleanup':{'empty':True,'removed':True}}
    (run/'unit-result.json').write_text(json.dumps(unit))
    result=campaign/'idle-ticks/run-1/result.json';result.parent.mkdir(parents=True)
    result.write_text(json.dumps(child_result('idle',campaign,'run-1')))
    slot=manifest()['slots'][0]
    returned={'run_root':str(run),'unit':'native.service','completed':True,'action':'idle','lane':'live'}
    evidence=scheduled.verify_completion(base,campaign,slot,returned)
    assert evidence['child_receipt_sha256']==hashlib.sha256(result.read_bytes()).hexdigest()
    unit['descendant_cleanup']['empty']=False;(run/'unit-result.json').write_text(json.dumps(unit))
    with pytest.raises(scheduled.Refused):scheduled.verify_completion(base,campaign,slot,returned)


def test_child_receipt_cannot_be_reused_across_attempts_or_synthetic_idle(tmp_path,receipt_owner):
    base=tmp_path/'base';base.mkdir();campaign=tmp_path/'campaign';campaign.mkdir()
    run=base/'runs/run-1';run.mkdir(parents=True)
    (run/'unit-result.json').write_text(json.dumps({'unit':'native.service','returncode':0,'abort':None,
        'actual_properties':{'InvocationID':'b'*32},
        'descendant_cleanup':{'empty':True,'removed':True}}))
    result=campaign/'idle-ticks/run-1/result.json';result.parent.mkdir(parents=True)
    returned={'run_root':str(run),'unit':'native.service','completed':True,'action':'idle','lane':'live'}
    for change in ({'attempt_id':'other'}, {'root':'/other'},
                   {'source_model_event_delta':{'source_events':1,'model_events':0}},
                   {'source_model_event_delta':{'source_events':False,'model_events':False}},
                   {'native_process_success':False},{'non_session_verified':False},
                   {'native_idle_projection_verified':False},
                   {'status':'fail'},{'generation_state_unchanged':False}):
        value=child_result('idle',campaign,'run-1');value.update(change);result.write_text(json.dumps(value))
        with pytest.raises(scheduled.Refused):scheduled.verify_completion(base,campaign,manifest()['slots'][0],returned)
    value=child_result('idle',campaign,'run-1');value.pop('native_idle_projection_verified')
    result.write_text(json.dumps(value))
    with pytest.raises(scheduled.Refused):scheduled.verify_completion(base,campaign,manifest()['slots'][0],returned)


def test_foreign_supervisor_unit_and_zero_invocation_are_rejected(tmp_path,receipt_owner):
    base=tmp_path/'base';base.mkdir();campaign=tmp_path/'campaign';campaign.mkdir()
    run=base/'runs/run-1';run.mkdir(parents=True)
    path=campaign/'idle-ticks/run-1/result.json';path.parent.mkdir(parents=True)
    path.write_text(json.dumps(child_result('idle',campaign,'run-1')))
    returned={'run_root':str(run),'unit':'native.service','completed':True,'action':'idle','lane':'live'}
    unit={'unit':'native.service','returncode':0,'abort':None,'actual_properties':{'InvocationID':'b'*32},
        'descendant_cleanup':{'empty':True,'removed':True}}
    for change in ({'unit':'other.service'},{'actual_properties':{'InvocationID':'0'*32}}):
        value={**unit,**change};(run/'unit-result.json').write_text(json.dumps(value))
        with pytest.raises(scheduled.Refused):scheduled.verify_completion(base,campaign,manifest()['slots'][0],returned)
    unit.pop('abort');(run/'unit-result.json').write_text(json.dumps(unit))
    with pytest.raises(scheduled.Refused):scheduled.verify_completion(base,campaign,manifest()['slots'][0],returned)


def test_control_reader_denies_different_owner_and_same_mode_changed_bytes(tmp_path):
    path=tmp_path/'control';path.write_bytes(b'{}');path.chmod(0o400)
    with pytest.raises(scheduled.Refused):scheduled.read_control(path,hashlib.sha256(b'{}').hexdigest(),owner_uid=os.getuid()+1)
    with pytest.raises(scheduled.Refused):scheduled.read_control(path,'f'*64,owner_uid=os.getuid())


def test_previous_digest_must_remain_bound_to_same_immutable_manifest(tmp_path,receipt_owner):
    slot=manifest()['slots'][1];folder=scheduled.claim_slot(tmp_path,'slot-0',{})
    scheduled.finish_slot(folder,{'status':'pass','slot':'slot-0','campaign_root':manifest()['campaign_root'],
        'manifest_sha256':'a'*64,'actual_timer_dispatch':True})
    assert scheduled.previous_result(tmp_path,slot,manifest()['campaign_root'],'a'*64)
    with pytest.raises(scheduled.Refused):scheduled.previous_result(tmp_path,slot,manifest()['campaign_root'],'b'*64)


@pytest.mark.skipif(os.geteuid()!=0,reason='requires actual root file metadata; systemd/provider calls remain mocked')
@pytest.mark.parametrize('scenario,expected',[
    ('clean','pass'),('process_failure','fail'),('child_failure','fail'),
    ('cleanup_failure','fail'),('pin_changed','fail'),('foreign_run','fail'),('wrong_timer','fail'),
])
def test_dispatch_claims_once_and_binds_real_private_files_without_live_calls(tmp_path,monkeypatch,scenario,expected):
    # Run this bounded test with --basetemp /var/tmp/<private-root-testdir> to
    # exercise the real root/sticky-parent ownership boundary, not a bypass.
    base=tmp_path/'supervisor';base.mkdir(mode=0o700)
    original=Path(scheduled.__file__).read_bytes()
    copied=base/'scheduled.py';copied.write_bytes(original);copied.chmod(0o400)
    monkeypatch.setattr(scheduled,'__file__',str(copied))
    campaign=tmp_path/'case/campaign';campaign.mkdir(parents=True)
    value=manifest(base);value['campaign_root']=str(campaign);slot=value['slots'][0]
    for name in value['files']:
        path=base/name
        if name!='scheduled.py':
            if name.startswith('config-'):
                item=next(s for s in value['slots'] if s['config']==name)
                path.write_text(json.dumps({'campaign_root':str(campaign),'session':item['session']}))
            else:path.write_text('{}')
            path.chmod(0o400)
        value['files'][name]=hashlib.sha256(path.read_bytes()).hexdigest()
    schedule=base/'schedule.json';schedule.write_text(json.dumps(value));schedule.chmod(0o400)
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return datetime(2026,11,1,2,0,1,tzinfo=timezone.utc)
    monkeypatch.setattr(scheduled,'datetime',Clock)
    monkeypatch.setattr(scheduled.time,'monotonic_ns',lambda:101000000000)
    monkeypatch.setenv('INVOCATION_ID','a'*32)
    service,timer=properties(slot);service['MainPID']=str(os.getpid())
    if scenario=='wrong_timer':service['TriggeredBy']='other.timer'
    monkeypatch.setattr(scheduled,'systemd_properties',lambda unit,names:service if unit.endswith('.service') else timer)
    invoked=[]
    def run(command,**kwargs):
        from types import SimpleNamespace
        invoked.append(command)
        assert command==scheduled.supervisor_command(base,slot)
        assert kwargs['env']=={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'}
        run_root=base/'runs/run-1';run_root.mkdir(parents=True)
        unit={'unit':'native.service','returncode':0,'abort':None,'actual_properties':{'InvocationID':'b'*32},
            'descendant_cleanup':{'empty':True,'removed':True}}
        if scenario=='cleanup_failure':unit['descendant_cleanup']['empty']=False
        (run_root/'unit-result.json').write_text(json.dumps(unit))
        path=campaign/'idle-ticks/run-1/result.json';path.parent.mkdir(parents=True)
        child=child_result('idle',campaign,'run-1')
        if scenario=='child_failure':child['status']='fail'
        path.write_text(json.dumps(child))
        if scenario=='pin_changed':(base/slot['config']).write_text('{}')
        kwargs['stdout'].write(json.dumps({'run_root':str(run_root) if scenario!='foreign_run' else '/etc/outside',
            'unit':'native.service',
            'completed':True,'action':'idle','lane':'live'}).encode())
        kwargs['stderr'].write(b'private child diagnostic stays off dispatcher output')
        return SimpleNamespace(returncode=2 if scenario=='process_failure' else 0)
    monkeypatch.setattr(scheduled.subprocess,'run',run)
    result=scheduled.dispatch(base,schedule,'slot-0')
    assert result['status']==expected and result['actual_timer_dispatch'] is (scenario!='wrong_timer')
    folder=base/'dispatches/slot-0'
    if scenario!='wrong_timer':assert (folder/'supervisor.stderr').stat().st_mode & 0o777==0o600
    assert 'private child diagnostic' not in json.dumps(result)
    assert json.loads((folder/'result.sha256').read_bytes())=={
        'sha256':hashlib.sha256((folder/'result.json').read_bytes()).hexdigest()}
    with pytest.raises(scheduled.Refused,match='slot_already_claimed'):
        scheduled.dispatch(base,schedule,'slot-0')
    assert len(invoked)==(0 if scenario=='wrong_timer' else 1)


def test_nonroot_dispatch_is_refused_before_controls_or_commands(monkeypatch):
    monkeypatch.setattr(scheduled.os,'geteuid',lambda:1000)
    monkeypatch.setattr(scheduled.subprocess,'run',lambda *a,**k:pytest.fail('must not invoke any command'))
    with pytest.raises(scheduled.Refused,match='root_dispatcher_required'):
        scheduled.dispatch(Path('/var/tmp/no-access'),Path('/var/tmp/no-access/schedule.json'),'slot-0')


def test_json_controls_reject_duplicate_keys_and_nonfinite_values():
    for raw in (b'{"x":1,"x":2}',b'{"x":NaN}',b'{"x":Infinity}'):
        with pytest.raises(scheduled.Refused):scheduled.decode(raw)
