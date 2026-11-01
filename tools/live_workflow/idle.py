"""Observe an ordinary non-session controller tick; never infer timer dispatch.

The external root supervisor supplies real isolation and timer evidence. No
credential, source/model substitution, prospective override or fake clock is used.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
from zoneinfo import ZoneInfo

from . import runner
from .ordinary import _private_directory, _read_json, _events


def _state(root, row):
    runner.require(not (root/'native-attempt.json').exists()
        and not (root/'native-attempt.json').is_symlink(),'idle_native_attempt_existing')
    ordinary=runner.no_symlinks(root/'ordinary-sessions')
    runner.require(not ordinary.exists() or (_private_directory(ordinary) and not any(ordinary.iterdir())),
        'idle_ordinary_session_existing')
    attempts=runner.no_symlinks(root/'repo/data/logs/run_attempts')
    runner.require(not list(attempts.rglob('*.json')),'idle_native_run_attempt_existing')
    state=_private_directory(row['state_dir']);profile=runner.no_symlinks(row['config_file'])
    runner.require(profile.parent==state and sorted(p.name for p in state.iterdir())==[profile.name],
        'idle_generation_not_config_only')
    info=profile.stat()
    runner.require(stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid()
        and info.st_nlink==1 and not info.st_mode & 0o077,'idle_private_config_required')
    journal=runner.no_symlinks(root/'repo/data/runtime_recovery/journal.json')
    if journal.exists():runner.require(_read_json(journal).get('incidents')=={},'idle_journal_not_empty')
    return {'config_file':profile.name,'sha256':runner.sha(profile),'size':info.st_size,
        'mode':stat.S_IMODE(info.st_mode),'uid':info.st_uid,'inode':info.st_ino,
        'device':info.st_dev,'mtime_ns':info.st_mtime_ns}


def _capture(root):
    records=_events(root);coverage=runner.tape_coverage(root)
    pending=coverage['unfinished_reservations']+len(list((root/'tape/reservations').glob('*.pending')))
    runner.require(pending==0,'idle_unfinished_reservations')
    counts={'source_events':sum(r['boundary'] not in ('observed_clock','model_helper') for r in records.values()),
        'model_events':sum(r['boundary']=='model_helper' for r in records.values())}
    return records,counts,pending


def _history(root, folder, identity):
    for output in sorted(folder.iterdir()):
        _private_directory(output)
        runner.require((output/'result.json').is_file(),'idle_previous_tick_incomplete')
        value=_read_json(output/'result.json')
        runner.require(value.get('status')=='pass','idle_previous_tick_failed')
        runner.require(value.get('attempt_id')==output.name and all(value.get(k)==v for k,v in identity.items()
            if k!='attempt_id'),'idle_previous_tick_identity_changed')
        runner.require(value.get('result_sha256')==runner.digest({k:v for k,v in value.items() if k!='result_sha256'}),
            'idle_previous_tick_receipt_changed')
        runner.require(all(value.get(k) is True for k in ('identity_verified','generation_state_unchanged',
            'journal_incidents_empty','native_process_success','native_idle_projection_verified','non_session_verified'))
            and value.get('source_model_event_delta')=={'source_events':0,'model_events':0}
            and value.get('unfinished_reservations')==0,'idle_previous_tick_facts_incomplete')
        runner.require(type(value.get('artifact_sha256')) is dict and bool(value['artifact_sha256']),
            'idle_previous_tick_artifacts_missing')
        for relative,digest in value['artifact_sha256'].items():
            runner.require(type(relative) is str and not Path(relative).is_absolute()
                and '..' not in Path(relative).parts,'idle_unsafe_artifact_path')
            path=runner.no_symlinks(root/relative)
            runner.require(path.is_relative_to(root) and runner.sha(path)==digest,'idle_previous_tick_artifact_changed')


def _child(root, output, attempt_id, role, argv, artifacts):
    label='idle-'+attempt_id+'-'+role
    try:runner.run_process(argv,root,env=runner.owned_environment(root),label=label,timeout=120)
    except runner.AcceptanceError:pass  # Read its actual retained receipt; exit zero alone cannot pass.
    receipt_path=root/(label+'-process.json');receipt=_read_json(receipt_path)
    log=runner.no_symlinks(root/'logs'/(label+'.log'));info=log.stat()
    runner.require(stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid() and info.st_nlink==1
        and not info.st_mode & 0o077 and info.st_size<=1024**2,'idle_private_bounded_log_required')
    runner.require(receipt.get('argv')==argv and receipt.get('console_sha256')==runner.sha(log),
        'idle_process_artifact_identity')
    artifacts.extend((receipt_path,log))
    runner.require(type(receipt.get('returncode')) is int and receipt['returncode']==0
        and receipt.get('failure') is None,'idle_child_failed')
    return {'receipt':str(receipt_path.relative_to(root)),'receipt_sha256':runner.sha(receipt_path),
        'console':str(log.relative_to(root)),'console_sha256':runner.sha(log)}


def _window(root, output, attempt_id, role, artifacts):
    directory=output/role;directory.mkdir(mode=0o700)
    argv=[str(root/'repo/.venv/bin/python'),'-m','tools.live_workflow.bootstrap','idle-window',
        '--root',str(root),'--output-dir',str(directory)]
    process=_child(root,output,attempt_id,role,argv,artifacts)
    path=directory/'idle-window.json';window=_read_json(path);artifacts.append(path)
    observed=datetime.fromisoformat(window['observed_at']);now=datetime.now(timezone.utc)
    runner.require(observed.tzinfo is not None and observed.utcoffset() is not None
        and -5<=(now-observed).total_seconds()<=60,'idle_calendar_observation_not_current')
    runner.require(window['exchange_date']==observed.astimezone(ZoneInfo('America/New_York')).date().isoformat(),
        'idle_calendar_date_mismatch')
    runner.require(window['ordinary_idle_expected'] is True,'idle_non_session_required')
    return window,process


def _native_projection(root, output, process, artifacts):
    path=root/'repo/data/runtime_recovery/status.json';projection=_read_json(path);raw=path.read_bytes()
    runner.require(json.loads(raw)==projection,'idle_native_status_changed')
    snapshot=output/'native-status.json'
    fd=os.open(snapshot,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as stream:
        stream.write(raw);stream.flush();os.fsync(stream.fileno())
    artifacts.append(snapshot)  # Bind immutable bytes; subsequent ticks replace the live status.
    receipt=_read_json(root/process['receipt'])
    started=datetime.fromisoformat(receipt['started_at']);finished=datetime.fromisoformat(receipt['finished_at'])
    updated=datetime.fromisoformat(projection['updated_at'])
    runner.require(all(value.tzinfo is not None and value.utcoffset() is not None
        for value in (started,finished,updated)) and started<=updated<=finished,'idle_native_status_not_current')
    runner.require(projection.get('status')=='healthy' and projection['controller_reason_code'] is None
        and projection.get('incidents')==[],'idle_native_status_not_healthy')
    lines=[line for line in (root/process['console']).read_text().splitlines() if line.strip()]
    runner.require(bool(lines) and json.loads(lines[-1])==projection,'idle_native_console_projection_mismatch')


def run_idle(args):
    """One bounded real ordinary tick; root attests actual timer dispatch separately."""
    runner.require(type(args.attempt_id) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',args.attempt_id),
        'idle_safe_attempt_id_required')
    root=_private_directory(args.root);contract,row=runner.verify_contract(root)
    boundary=runner.require_boundary(root,args.boundary_receipt);contract_sha=runner.sha(root/'contract.json')
    identity={'schema_version':1,'lane':'ordinary_idle_tick','root':str(root),'attempt_id':args.attempt_id,
        'candidate_commit':contract['candidate_commit'],'profile_sha256':contract['profile_sha256'],
        'generation_id':row['gen_id'],'contract_sha256':contract_sha,'boundary_receipt_sha256':boundary}
    folder=runner.no_symlinks(root/'idle-ticks')
    if not folder.exists():folder.mkdir(mode=0o700)
    _private_directory(folder);output=folder/args.attempt_id
    runner.require(not output.exists() and not output.is_symlink(),'idle_attempt_already_exists')
    _history(root,folder,identity)
    output.mkdir(mode=0o700)  # Exclusive durable claim, before any child activity.
    runner.private_json(output/'attempt.json',identity|{'started_at':datetime.now(timezone.utc).isoformat(),
        'credential_env_names':[],'clock_policy':'real_wall_clock','timer_dispatch_authority':'external_supervisor'})
    artifacts=[output/'attempt.json'];result=identity|{'status':'fail','reason':'idle_tick_incomplete',
        'credential_env_names':[],'clock_policy':'real_wall_clock','timer_dispatch_authority':'external_supervisor',
        'identity_verified':False,'generation_state_unchanged':False,'journal_incidents_empty':False,
        'native_process_success':False,'native_idle_projection_verified':False,'non_session_verified':False,'source_model_event_delta':{},
        'unfinished_reservations':None,'processes':{}}
    before=None;records=None;counts=None;failure=None
    try:
        before=_state(root,row)
        runner.require(before['sha256']==contract['profile_sha256'],'idle_config_hash_changed')
        records,counts,_=_capture(root)
        runner.require(counts=={'source_events':0,'model_events':0},'idle_previous_source_model_activity')
        runner.private_json(output/'state-before.json',before);artifacts.append(output/'state-before.json')
        window,process=_window(root,output,args.attempt_id,'before',artifacts)
        result['window_before']=window;result['processes']['before']=process
        command=[str(root/'repo/.venv/bin/python'),str(root/'repo/scripts/recover_runtime.py'),'--repo-root',str(root/'repo')]
        result['processes']['native']=_child(root,output,args.attempt_id,'native',command,artifacts)
        result['native_process_success']=True
        _native_projection(root,output,result['processes']['native'],artifacts)
        result['native_idle_projection_verified']=True
        window,process=_window(root,output,args.attempt_id,'after',artifacts)
        result['window_after']=window;result['processes']['after']=process;result['non_session_verified']=True
    except Exception as error:
        failure=str(error) if isinstance(error,runner.AcceptanceError) else 'idle_tick_failed'
        result['failure_type']=type(error).__name__
    try:
        runner.verify_contract(root)
        runner.require(runner.sha(root/'contract.json')==contract_sha,'idle_contract_changed')
        runner.require(runner.require_boundary(root,args.boundary_receipt)==boundary,'idle_boundary_changed')
        result['identity_verified']=True
        after=_state(root,row)
        runner.private_json(output/'state-after.json',after);artifacts.append(output/'state-after.json')
        runner.require(before is not None and after==before,'idle_generation_state_changed')
        result['generation_state_unchanged']=True
        journal=_read_json(root/'repo/data/runtime_recovery/journal.json')
        runner.require(journal.get('incidents')=={},'idle_journal_not_empty')
        runner.private_json(output/'journal-after.json',journal);artifacts.append(output/'journal-after.json')
        result['journal_incidents_empty']=True
        after_records,after_counts,pending=_capture(root);result['unfinished_reservations']=pending
        runner.require(records is not None and all(after_records.get(k)==v for k,v in records.items()),
            'idle_previous_records_changed')
        result['source_model_event_delta']={k:after_counts[k]-counts[k] for k in counts}
        runner.require(result['source_model_event_delta']=={'source_events':0,'model_events':0},'idle_unexpected_source_model_activity')
    except Exception as error:
        reason=str(error) if isinstance(error,runner.AcceptanceError) else 'idle_post_verification_failed'
        if failure is None:failure=reason
        else:result['post_verification_failure']=reason
    if failure is None and all(result[k] for k in ('native_process_success','native_idle_projection_verified','non_session_verified',
            'identity_verified','generation_state_unchanged','journal_incidents_empty')):
        result.update(status='pass',reason='ordinary_non_session_idle_observed')
    else:result['reason']=failure or 'idle_tick_incomplete'
    result['artifact_sha256']={str(p.relative_to(root)):runner.sha(p) for p in artifacts}
    result['finished_at']=datetime.now(timezone.utc).isoformat()
    result['result_sha256']=runner.digest(result)
    runner.private_json(output/'result.json',result)
    return result
