"""One-shot root dispatcher for reviewed real systemd paper-workflow timers.

Copy this standalone standard-library file into the root-protected supervisor
directory as scheduled.py (0400). The adjacent schedule.json and all files it
pins must also be root-owned 0400. This module never installs or changes units,
rewrites controls, retries a slot, overrides a clock, or changes native receipts.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal, localcontext
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

ROOT_UID = 0
WORKER_UID = 2001
ENV = {'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'}
SAFE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z')
HEX = re.compile(r'[0-9a-f]{64}\Z')
INVOCATION = re.compile(r'[0-9a-f]{32}\Z')
SERVICE_PROPERTIES = ('Id','InvocationID','MainPID','TriggeredBy','ExecMainStartTimestampMonotonic',
    'ActiveState','SubState','ControlGroup')
TIMER_PROPERTIES = ('Id','Unit','LastTriggerUSecMonotonic','ActiveState')


class Refused(RuntimeError):
    """Fixed non-sensitive reason suitable for private receipts and stderr."""


def require(condition, reason):
    if not condition:
        raise Refused(reason)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def decode(raw):
    def pairs(items):
        value={}
        for key,item in items:
            require(key not in value,'duplicate_json_key')
            value[key]=item
        return value
    try:
        return json.loads(raw,object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(Refused('nonfinite_json')))
    except (ValueError,UnicodeError,RecursionError) as error:
        raise Refused('invalid_json') from error


def timestamp(value):
    require(type(value) is str and value.endswith('Z'),'utc_schedule_required')
    try:
        result=datetime.fromisoformat(value[:-1]+'+00:00')
    except ValueError as error:
        raise Refused('invalid_schedule_date') from error
    require(result.isoformat().replace('+00:00','Z')==value,'canonical_schedule_required')
    return result


def validate_manifest(value,base,slot_id):
    require(type(value) is dict and set(value)=={'schema','base','campaign_root','python','files','slots'},
        'schedule_fields')
    require(value['schema']=='eventedge-real-schedule-v1' and value['base']==str(base)
        and value['python']=='/usr/bin/python3.12','schedule_identity')
    campaign=value['campaign_root']
    require(type(campaign) is str and campaign.startswith('/var/tmp/') and not any(c.isspace() for c in campaign)
        and Path(campaign).name=='campaign' and '..' not in Path(campaign).parts
        and str(Path(campaign))==campaign and not Path(campaign).is_relative_to(base),'schedule_campaign')
    files=value['files']
    require(type(files) is dict and 4<=len(files)<=64
        and {'scheduled.py','supervisor.py','host_worker.py','inside.py'}<=set(files),'schedule_pinned_files')
    require(all(type(name) is str and SAFE.fullmatch(name) and type(digest) is str and HEX.fullmatch(digest)
        for name,digest in files.items()),'schedule_file_identity')
    slots=value['slots']
    require(type(slots) is list and 1<=len(slots)<=7,'schedule_slot_bound')
    seen=set();services=set();timers=set();previous=None;previous_time=None;ordinary_session=None
    selected=None
    for index,slot in enumerate(slots):
        require(type(slot) is dict and set(slot)=={'id','action','session','scheduled_at','window_seconds',
            'service_unit','timer_unit','previous_slot','config','review'},'schedule_slot_fields')
        identifier=slot['id']
        require(type(identifier) is str and SAFE.fullmatch(identifier) and identifier not in seen,'schedule_slot_id')
        require(slot['previous_slot']==previous,'schedule_predecessor_chain')
        require(slot['action']==('idle' if index<2 else 'ordinary'),'schedule_action_order')
        require(type(slot['window_seconds']) is int and 1<=slot['window_seconds']<=600,'schedule_window_bound')
        require(all(type(slot[key]) is str and slot[key] in files and SAFE.fullmatch(slot[key])
            for key in ('config','review')),'schedule_slot_pins')
        require(slot['config']!=slot['review'],'schedule_distinct_controls')
        try:
            session=date.fromisoformat(slot['session'])
        except (ValueError,TypeError) as error:
            raise Refused('schedule_session') from error
        require(session.isoformat()==slot['session'],'schedule_canonical_session')
        start=timestamp(slot['scheduled_at'])
        require(previous_time is None or start>previous_time,'schedule_time_order')
        if slot['action']=='ordinary':
            local=start.astimezone(ZoneInfo('America/New_York'))
            require(local.date()==session and session.weekday()<5 and (local.hour,local.minute,local.second)==(18,0,0)
                and local.microsecond==0 and (ordinary_session is None or session>ordinary_session),
                'ordinary_18_new_york_schedule')
            ordinary_session=session
        for key,suffix,known in (('service_unit','.service',services),('timer_unit','.timer',timers)):
            name=slot[key]
            require(type(name) is str and name.startswith('eventedge-') and name.endswith(suffix)
                and SAFE.fullmatch(name) and name not in known,'schedule_unit_identity')
            known.add(name)
        if identifier==slot_id:selected=slot
        seen.add(identifier);previous=identifier;previous_time=start
    require(selected is not None,'schedule_unknown_slot')
    return selected


def protected_parents(path):
    require(path.is_absolute() and '..' not in path.parts and str(path)==os.path.normpath(str(path)),
        'absolute_canonical_control_path')
    for parent in reversed(path.parents):
        info=parent.lstat()
        shared=(parent==Path('/var/tmp') and bool(info.st_mode & stat.S_ISVTX))
        require(stat.S_ISDIR(info.st_mode) and info.st_uid==0
            and (not info.st_mode & 0o022 or shared),'root_protected_control_parent')


def read_regular(path,*,owners,max_bytes=32*1024**2,readonly=False):
    path=Path(path)
    fd=os.open(path,os.O_RDONLY|os.O_CLOEXEC|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        before=os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_uid in owners and before.st_nlink==1
            and before.st_size<=max_bytes and not before.st_mode & 0o022,'bounded_owned_regular_required')
        if readonly:require(stat.S_IMODE(before.st_mode)==0o400,'immutable_0400_control_required')
        raw=b''
        while len(raw)<=max_bytes:
            part=os.read(fd,min(1024**2,max_bytes+1-len(raw)))
            if not part:break
            raw+=part
        after=os.fstat(fd)
        require(len(raw)<=max_bytes and (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)
            ==(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns),'control_changed_during_read')
        return raw
    except OSError as error:
        raise Refused('control_read_failed') from error
    finally:os.close(fd)


def read_control(path,digest,*,owner_uid=0):
    try:raw=read_regular(path,owners={owner_uid},max_bytes=8*1024**2,readonly=True)
    except OSError as error:raise Refused('control_read_failed') from error
    require(sha(raw)==digest,'control_digest_changed')
    return raw


def supervisor_command(base,slot):
    return ['/usr/bin/python3.12','-I','-B',str(base/'supervisor.py'),
        '--config',str(base/slot['config']),'--action',slot['action'],'--lane','live',
        '--review-file',str(base/slot['review']),'--execute']


def monotonic_microseconds(value):
    """systemctl renders some usec properties as exact human durations."""
    require(type(value) is str and 0<len(value)<=160,'systemd_monotonic_value_required')
    if re.fullmatch(r'[0-9]+',value):
        result=int(value)
        require(result<2**64,'systemd_monotonic_value_bound')
        return result
    number=r'[0-9]+(?:\.[0-9]+)?'
    token=number+r'(?:w|d|h|min|ms|us|s)'
    require(re.fullmatch(token+r'(?: *'+token+r')*',value) is not None,
        'systemd_monotonic_duration_shape')
    factors={'w':604800000000,'d':86400000000,'h':3600000000,'min':60000000,
        's':1000000,'ms':1000,'us':1}
    parts=re.findall('('+number+r')(w|d|h|min|ms|us|s)',value)
    previous=2**64
    with localcontext() as context:
        context.prec=200
        total=Decimal(0)
        for amount,unit in parts:
            factor=factors[unit]
            require(factor<previous,'systemd_monotonic_duration_order')
            previous=factor;total+=Decimal(amount)*factor
        require(total==total.to_integral_value() and 0<=total<2**64,'systemd_monotonic_integer_us_required')
        return int(total)


def verify_timer(slot,service,timer,*,now,monotonic_us,pid,invocation_id):
    require(now.tzinfo is not None and now.utcoffset() is not None,'real_aware_clock_required')
    elapsed=(now-timestamp(slot['scheduled_at'])).total_seconds()
    require(0<=elapsed<=slot['window_seconds'],'outside_real_scheduled_window')
    require(type(invocation_id) is str and INVOCATION.fullmatch(invocation_id)
        and invocation_id!='0'*32 and service.get('InvocationID')==invocation_id,'systemd_invocation_identity')
    require(service.get('Id')==slot['service_unit'] and service.get('MainPID')==str(pid)
        and service.get('TriggeredBy','').split()==[slot['timer_unit']]
        and service.get('ActiveState')=='active' and service.get('SubState')=='running',
        'systemd_current_timer_service_required')
    require(timer.get('Id')==slot['timer_unit'] and timer.get('Unit')==slot['service_unit'],
        'systemd_timer_target_mismatch')
    trigger=monotonic_microseconds(timer.get('LastTriggerUSecMonotonic',''))
    started=monotonic_microseconds(service.get('ExecMainStartTimestampMonotonic',''))
    require(0<trigger<=started<=monotonic_us and 0<=monotonic_us-trigger<=slot['window_seconds']*10**6
        and started-trigger<=10*10**6,'systemd_fresh_timer_trigger_required')


def systemd_properties(unit,names):
    completed=subprocess.run(['/usr/bin/systemctl','show',unit,*('--property='+key for key in names)],
        env=ENV,capture_output=True,timeout=10,check=False)
    require(completed.returncode==0 and len(completed.stdout)<=65536,'systemd_properties_unavailable')
    value={}
    for line in completed.stdout.decode('ascii').splitlines():
        require('=' in line,'systemd_property_shape')
        key,item=line.split('=',1)
        require(key in names and key not in value,'systemd_property_identity')
        value[key]=item
    require(set(value)==set(names),'systemd_properties_incomplete')
    return value


def private_directory(path,*,create=False):
    if create:path.mkdir(mode=0o700,exist_ok=True)
    info=path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid==ROOT_UID and stat.S_IMODE(info.st_mode)==0o700,
        'root_private_receipt_directory')
    return path


def write_exclusive(path,value):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
    with os.fdopen(fd,'wb') as stream:
        stream.write((json.dumps(value,sort_keys=True,indent=2,allow_nan=False)+'\n').encode())
        stream.flush();os.fsync(stream.fileno())
    parent=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:os.fsync(parent)
    finally:os.close(parent)


@contextmanager
def dispatch_lock(base):
    fd=os.open(base/'dispatch.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
    try:
        info=os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid==ROOT_UID and info.st_nlink==1
            and stat.S_IMODE(info.st_mode)==0o600,'root_private_dispatch_lock')
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as error:raise Refused('dispatch_busy') from error
        yield
    finally:os.close(fd)


def claim_slot(base,slot_id,attempt):
    require(type(slot_id) is str and SAFE.fullmatch(slot_id),'slot_claim_identity')
    folder=private_directory(base/'dispatches',create=True)/slot_id
    try:folder.mkdir(mode=0o700)
    except FileExistsError as error:raise Refused('slot_already_claimed') from error
    private_directory(folder)
    parent=os.open(folder.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:os.fsync(parent)
    finally:os.close(parent)
    write_exclusive(folder/'attempt.json',attempt)
    return folder


def finish_slot(folder,result):
    write_exclusive(folder/'result.json',result)
    write_exclusive(folder/'result.sha256',{'sha256':sha((folder/'result.json').read_bytes())})


def previous_result(base,slot,campaign,manifest_sha256=None):
    if slot['previous_slot'] is None:return None
    try:
        folder=private_directory(base/'dispatches')/slot['previous_slot'];private_directory(folder)
        raw=read_regular(folder/'result.json',owners={ROOT_UID})
        digest=decode(read_regular(folder/'result.sha256',owners={ROOT_UID}))
        result=decode(raw)
        require(digest=={'sha256':sha(raw)} and result.get('status')=='pass'
            and result.get('slot')==slot['previous_slot'] and result.get('campaign_root')==str(campaign)
            and (manifest_sha256 is None or (result.get('manifest_sha256')==manifest_sha256
                and result.get('actual_timer_dispatch') is True)),'previous_slot_failed_or_changed')
        return sha(raw)
    except (OSError,ValueError,TypeError,KeyError) as error:
        raise Refused('previous_slot_missing_or_invalid') from error


def evidence(path):
    # Every ancestor is checked to reject symlink traversal, including the
    # private worker's campaign, before the no-follow file descriptor is opened.
    for parent in path.parents:
        require(not parent.is_symlink(),'evidence_parent_symlink')
    raw=read_regular(path,owners={ROOT_UID,WORKER_UID})
    return decode(raw),sha(raw)


def verify_completion(base,campaign,slot,returned):
    require(type(returned) is dict and returned.get('completed') is True
        and returned.get('action')==slot['action'] and returned.get('lane')=='live','supervisor_did_not_complete')
    raw_path=returned.get('run_root')
    require(type(raw_path) is str,'supervisor_run_root_missing')
    run=Path(raw_path)
    require(run.parent==base/'runs' and SAFE.fullmatch(run.name) and str(run)==raw_path,'supervisor_run_root_scope')
    unit,unit_sha=evidence(run/'unit-result.json')
    unit_name=returned.get('unit')
    invocation=unit.get('actual_properties',{}).get('InvocationID')
    require(type(unit_name) is str and SAFE.fullmatch(unit_name) and unit_name.endswith('.service')
        and unit.get('unit')==unit_name and type(invocation) is str and INVOCATION.fullmatch(invocation)
        and invocation!='0'*32,'supervisor_unit_identity_mismatch')
    require(unit.get('returncode')==0 and type(unit.get('returncode')) is int and 'abort' in unit and unit['abort'] is None
        and unit.get('descendant_cleanup',{}).get('empty') is True
        and unit.get('descendant_cleanup',{}).get('removed') is True,'supervisor_unit_or_cleanup_failed')
    if slot['action']=='idle':path=campaign/'idle-ticks'/run.name/'result.json'
    else:path=campaign/'ordinary-sessions'/slot['session']/'result.json'
    child,child_sha=evidence(path)
    require(child.get('status')=='pass' and child.get('root')==str(campaign),'child_result_failed_or_foreign')
    if slot['action']=='idle':
        delta=child.get('source_model_event_delta')
        require(type(child.get('schema_version')) is int and child['schema_version']==1
            and child.get('lane')=='ordinary_idle_tick'
            and child.get('attempt_id')==run.name and child.get('identity_verified') is True
            and child.get('native_process_success') is True and child.get('non_session_verified') is True
            and child.get('native_idle_projection_verified') is True
            and child.get('generation_state_unchanged') is True and child.get('journal_incidents_empty') is True
            and type(delta) is dict and set(delta)=={'source_events','model_events'}
            and all(type(count) is int and count==0 for count in delta.values())
            and type(child.get('unfinished_reservations')) is int and child['unfinished_reservations']==0,
            'idle_result_not_genuine_unchanged_tick')
    else:
        require(child.get('session')==slot['session'] and child.get('ordinary_native_clock') is True
            and child.get('actual_timer_dispatch') is False,'ordinary_result_identity_or_clock')
    return {'run_root':str(run),'supervisor_unit':unit_name,'supervisor_invocation_id':invocation,
        'unit_result_sha256':unit_sha,'child_receipt':str(path),
        'child_receipt_sha256':child_sha,'child_result':child}


def controls(base,manifest):
    for name,digest in manifest['files'].items():read_control(base/name,digest)


def dispatch(base,manifest_path,slot_id):
    require(os.geteuid()==0,'root_dispatcher_required')
    require(base==Path(__file__).absolute().parent and manifest_path==base/'schedule.json','fixed_dispatcher_manifest_required')
    protected_parents(base/'schedule.json')
    manifest_raw=read_regular(manifest_path,owners={0},max_bytes=65536,readonly=True)
    manifest=decode(manifest_raw);slot=validate_manifest(manifest,base,slot_id)
    require(read_control(base/'scheduled.py',manifest['files']['scheduled.py'])==Path(__file__).read_bytes(),
        'running_dispatcher_pin_mismatch')
    manifest_sha=sha(manifest_raw)
    with dispatch_lock(base):
        now=datetime.now(timezone.utc)
        identity={'schema':'eventedge-real-dispatch-v1','slot':slot_id,'action':slot['action'],
            'configured_session':slot['session'],'scheduled_at':slot['scheduled_at'],
            'started_at':now.isoformat(),'campaign_root':manifest['campaign_root'],
            'manifest_sha256':manifest_sha,'config_sha256':manifest['files'][slot['config']],
            'review_sha256':manifest['files'][slot['review']]}
        folder=claim_slot(base,slot_id,identity)
        result={**identity,'status':'fail','actual_timer_dispatch':False}
        try:
            controls(base,manifest)
            config=decode(read_control(base/slot['config'],manifest['files'][slot['config']]))
            require(config.get('campaign_root')==manifest['campaign_root'] and config.get('session')==slot['session'],
                'slot_config_campaign_or_session_mismatch')
            previous=previous_result(base,slot,manifest['campaign_root'],manifest_sha)
            service=systemd_properties(slot['service_unit'],SERVICE_PROPERTIES)
            timer=systemd_properties(slot['timer_unit'],TIMER_PROPERTIES)
            observed=datetime.now(timezone.utc);monotonic_us=time.monotonic_ns()//1000
            verify_timer(slot,service,timer,now=observed,monotonic_us=monotonic_us,
                pid=os.getpid(),invocation_id=os.environ.get('INVOCATION_ID'))
            result.update(actual_timer_dispatch=True,previous_result_sha256=previous,
                systemd_service=service,systemd_timer=timer,observed_at=observed.isoformat(),
                observed_new_york=observed.astimezone(ZoneInfo('America/New_York')).isoformat(),
                observed_monotonic_us=monotonic_us,kernel_boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip())
            write_exclusive(folder/'timer-evidence.json',dict(result,
                schema='eventedge-real-timer-evidence-v1',status='observed'))
            with (folder/'supervisor.stdout').open('xb') as stdout,(folder/'supervisor.stderr').open('xb') as stderr:
                os.chmod(folder/'supervisor.stdout',0o600);os.chmod(folder/'supervisor.stderr',0o600)
                child=subprocess.run(supervisor_command(base,slot),cwd=base,env=ENV,stdout=stdout,stderr=stderr,
                    timeout=4300,check=False)
            require(child.returncode==0,'supervisor_process_failed')
            returned=decode(read_regular(folder/'supervisor.stdout',owners={ROOT_UID},max_bytes=65536))
            result.update(verify_completion(base,Path(manifest['campaign_root']),slot,returned))
            controls(base,manifest)
            require(read_regular(manifest_path,owners={0},readonly=True)==manifest_raw,'schedule_changed_during_slot')
            require(previous_result(base,slot,manifest['campaign_root'],manifest_sha)==previous,'previous_result_changed_during_slot')
            result['status']='pass'
        except Exception as error:
            result['reason']=str(error) if isinstance(error,Refused) else type(error).__name__
        result['finished_at']=datetime.now(timezone.utc).isoformat()
        finish_slot(folder,result)
        return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',required=True,type=Path)
    parser.add_argument('--slot',required=True)
    args=parser.parse_args(argv)
    result=dispatch(Path(__file__).absolute().parent,args.manifest,args.slot)
    print(json.dumps({'slot':result['slot'],'status':result['status'],'actual_timer_dispatch':result['actual_timer_dispatch']}))
    return 0 if result['status']=='pass' else 2


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as error:
        print('Scheduled workflow refused: '+(str(error) if isinstance(error,Refused) else type(error).__name__),file=sys.stderr)
        raise SystemExit(2)
