"""One real ordinary controller tick per successive XNYS session.

The external supervisor admits this live sandbox. No timers are installed and no
clock is overridden. A failed or interrupted session permanently blocks this
campaign's sequence; create a new prepared campaign to obtain new observations.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
from zoneinfo import ZoneInfo

from . import runner


def _utc_now():
    return datetime.now(timezone.utc)


def _local_window(session,now):
    runner.require(type(session) is str and date.fromisoformat(session).isoformat()==session,
        'ordinary_canonical_session_required')
    runner.require(now.tzinfo is not None and now.utcoffset() is not None,'ordinary_real_aware_clock_required')
    local=now.astimezone(ZoneInfo('America/New_York'))
    runner.require(local.date().isoformat()==session and local.weekday()<5 and local.hour>=18,
        'ordinary_same_session_after_18_new_york_required')
    return local


def _private_directory(path):
    path=runner.no_symlinks(path);info=path.stat()
    runner.require(stat.S_ISDIR(info.st_mode) and info.st_uid==os.getuid()
        and not info.st_mode & 0o077,'ordinary_private_directory_required')
    return path


def _read_json(path):
    path=runner.no_symlinks(path);info=path.stat()
    runner.require(stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid()
        and info.st_nlink==1 and info.st_size<=32*1024**2,'ordinary_bounded_owned_json_required')
    return json.loads(path.read_bytes())


def _history(folder,contract,contract_sha256,generation):
    previous=None
    for path in sorted(folder.iterdir()):
        _private_directory(path)
        runner.require(date.fromisoformat(path.name).isoformat()==path.name,'ordinary_session_directory_identity')
        runner.require((path/'result.json').is_file(),'ordinary_prior_session_incomplete')
        result=_read_json(path/'result.json')
        runner.require(result.get('session')==path.name and result.get('previous_session')==previous
            and result.get('candidate_commit')==contract['candidate_commit']
            and result.get('profile_sha256')==contract['profile_sha256']
            and result.get('contract_sha256')==contract_sha256 and result.get('generation_id')==generation,
            'ordinary_prior_session_identity')
        runner.require(result.get('status')=='pass' and result.get('native_process_success') is True
            and result.get('report_process_success') is True and result.get('identity_verified') is True
            and result.get('report_identity_verified') is True
            and result.get('ordinary_native_clock') is True and runner.native_acceptance_passed(result),
            'ordinary_prior_session_failed')
        runner.require((previous is not None and result.get('expected_next_session')==path.name)
            or (previous is None and path.name==contract['reference_session']),
            'ordinary_prior_session_sequence')
        previous=path.name
    return previous


def _fresh_session(root,session,generation):
    runner.require(not (root/'native-attempt.json').exists()
        and not (root/'native-attempt.json').is_symlink(),'ordinary_prospective_attempt_existing')
    stem=session+'-'+generation+'-operational-report'
    for suffix in ('.json','.md'):
        path=runner.no_symlinks(root/'repo/docs/reports'/(stem+suffix))
        runner.require(not path.exists(),'ordinary_canonical_report_existing')
    attempts=runner.no_symlinks(root/'repo/data/logs/run_attempts')
    for path in attempts.glob('*.json'):
        artifact=_read_json(path)
        runner.require(not (artifact.get('requested_session')==session
            and artifact.get('generation_id')==generation),'ordinary_native_session_attempt_existing')


def _events(root):
    from .recorded_io import verify_tape
    identity=_read_json(root/'hook-config.json')['run_identity']
    records=verify_tape(root/'tape')
    runner.require(all(row['run_identity']==identity for row in records),'ordinary_tape_identity_changed')
    return {row['event_id']:row for row in records}


def _session_coverage(records):
    phases=Counter();models=Counter();sources=Counter();clocks=Counter();failures=0
    for row in records:
        phases[row['phase']]+=1;failures+=row.get('failure') is not None
        if row['boundary']=='observed_clock':clocks[row['operation']]+=1
        elif row['boundary']=='model_helper':
            request=row['public_request']
            models[(request.get('role'),request.get('model'),request.get('effort'))]+=1
        else:sources[(row['boundary'],row['operation'])]+=1
    return {'events':len(records),'source_events':sum(sources.values()),'model_events':sum(models.values()),
        'observed_failures':failures,'phase_counts':dict(sorted(phases.items())),
        'clock_events':sum(clocks.values()),
        'clock_boundaries':[{'operation':key,'calls':value} for key,value in sorted(clocks.items())],
        'source_boundaries':[{'boundary':key[0],'operation':key[1],'calls':value}
            for key,value in sorted(sources.items())],
        'models':[{'role':key[0],'model':key[1],'effort':key[2],'calls':value}
            for key,value in sorted(models.items(),key=lambda item:repr(item[0]))]}


def _process(root,argv,*,credentials,label,timeout):
    try:
        receipt=runner.run_process(argv,root,env=runner.owned_environment(root,credentials),
            label=label,timeout=timeout)
        return True,receipt
    except runner.AcceptanceError:
        path=root/(label+'-process.json')
        receipt=_read_json(path) if path.exists() else {'failure':'owned_child_failed_without_process_receipt'}
        return False,receipt


def _bootstrap_argv(root,operation,output,session,previous=None):
    command=[str(root/'repo/.venv/bin/python'),'-m','tools.live_workflow.bootstrap',operation,
        '--root',str(root),'--session',session,'--output-dir',str(output)]
    if previous is not None:command+=['--previous-session',previous]
    return command


def run_ordinary(args):
    """Execute one authorized live sandbox observation; never retry an attempt."""
    root=_private_directory(args.root);contract,row=runner.verify_contract(root)
    boundary=runner.require_boundary(root,args.boundary_receipt)
    now=_utc_now();local=_local_window(args.session,now)
    contract_sha256=runner.sha(root/'contract.json')
    folder=runner.no_symlinks(root/'ordinary-sessions')
    if not folder.exists():folder.mkdir(mode=0o700)
    _private_directory(folder)
    output=folder/args.session
    runner.require(not output.exists(),'ordinary_session_attempt_already_exists')
    previous=_history(folder,contract,contract_sha256,row['gen_id'])
    if previous is not None:
        prior=ordinary_coverage(root)
        runner.require(prior.get('status')=='accepted' and prior.get('accepted_sequence') is True
            and (prior.get('observed_last_result') or {}).get('session')==previous,
            'ordinary_prior_session_evidence_invalid')
    runner.require(previous is not None or args.session==contract['reference_session'],
        'ordinary_first_session_must_match_contract_reference')
    runner.require(previous is None or args.session>previous,'ordinary_successive_session_required')
    _fresh_session(root,args.session,row['gen_id'])
    credentials=runner.selected_credential_environment(args.credential_env)
    before=_events(root)
    coverage_before=runner.tape_coverage(root)
    runner.require(coverage_before['unfinished_reservations']==0,'ordinary_parent_capture_incomplete')
    # Exclusive mkdir is the durable claim before any child or provider activity.
    output.mkdir(mode=0o700)
    identity={'schema_version':1,'lane':'ordinary_native_workflow','root':str(root),
        'session':args.session,'previous_session':previous,'candidate_commit':contract['candidate_commit'],
        'profile_sha256':contract['profile_sha256'],'generation_id':row['gen_id'],
        'contract_sha256':contract_sha256,'boundary_receipt_sha256':boundary,
        'credential_env_names':sorted(credentials),'started_at':now.isoformat(),
        'observed_new_york':local.isoformat(),'ordinary_native_clock':True,
        'actual_timer_dispatch':False,'timer_dispatch_evidence':'not_observed_by_manual_native_tick',
        'deployed':False}
    runner.private_json(output/'attempt.json',identity)
    result={**identity,'status':'fail','reason':'ordinary_session_incomplete',
        'checks':runner.report_checks({}),'canonical_report_reproduction':{},
        'recorded_boundary_coverage':coverage_before,'session_recorded_boundary_coverage':{},
        'native_process_success':False,'report_process_success':False,'identity_verified':False,
        'expected_next_session':None,'fill_observation':'native_report_only_no_nonzero_fill_requirement'}
    label='ordinary-'+args.session
    try:
        success,receipt=_process(root,_bootstrap_argv(root,'ordinary-window',output,args.session,previous),
            credentials={},label=label+'-calendar',timeout=120)
        result['calendar_process']=receipt
        runner.require(success,'ordinary_calendar_child_failed')
        window=_read_json(output/'ordinary-window.json');observed=datetime.fromisoformat(window['observed_at'])
        runner.require(observed.tzinfo is not None and observed.utcoffset() is not None
            and -5<=(_utc_now()-observed).total_seconds()<=60,'ordinary_calendar_observation_not_current')
        runner.require(window['session']==args.session and window['allowed'] is True
            and window['ordinary_idle_expected'] is False,'ordinary_candidate_session_window_required')
        result['expected_next_session']=window['expected_next_session']
        runner.require(previous is None or window['expected_next_session']==args.session,
            'ordinary_exact_next_xnys_session_required')
        _local_window(args.session,_utc_now())
        command=[str(root/'repo/.venv/bin/python'),str(root/'repo/scripts/recover_runtime.py'),
            '--repo-root',str(root/'repo')]
        success,receipt=_process(root,command,credentials=credentials,label=label+'-native',timeout=3500)
        result['native_process_success']=success;result['native_process']=receipt
        success,receipt=_process(root,_bootstrap_argv(root,'report',output,args.session),
            credentials={},label=label+'-report',timeout=120)
        result['report_process_success']=success;result['report_process']=receipt
        report=_read_json(output/'native-report.json') if (output/'native-report.json').exists() else {}
        reproduction=_read_json(output/'report-reproduction.json') if (output/'report-reproduction.json').exists() else {}
        result['checks']=runner.report_checks(report)
        result['canonical_report_reproduction']=reproduction
        runner.require(report.get('generation_id')==row['gen_id']
            and report.get('generation_commit')==contract['candidate_commit']
            and report.get('profile_sha256')==contract['profile_sha256'] and report.get('session')==args.session,
            'ordinary_report_identity_incomplete')
        result['report_identity_verified']=True
        result['reason']='ordinary_native_acceptance_incomplete'
    except Exception as error:
        # Arbitrary provider/dependency text is never retained in public receipts.
        result['reason']=str(error) if isinstance(error,runner.AcceptanceError) else 'ordinary_session_failed'
        result['failure_type']=type(error).__name__
    try:
        runner.verify_contract(root)
        runner.require(runner.sha(root/'contract.json')==contract_sha256,'ordinary_contract_changed')
        after=_events(root)
        runner.require(all(event_id in after and after[event_id]==record for event_id,record in before.items()),
            'ordinary_previous_capture_changed')
        result['recorded_boundary_coverage']=runner.tape_coverage(root)
        result['session_recorded_boundary_coverage']=_session_coverage([record for event_id,record in after.items()
            if event_id not in before])
        result['identity_verified']=True
    except Exception as error:
        result['reason']=str(error) if isinstance(error,runner.AcceptanceError) else 'ordinary_post_run_verification_failed'
        result['failure_type']=type(error).__name__
    if (result['native_process_success'] and result['report_process_success']
            and result.get('report_identity_verified') is True and result['identity_verified']
            and runner.native_acceptance_passed(result)):
        result['status']='pass';result['reason']='ordinary_native_workflow_accepted'
    result['finished_at']=_utc_now().isoformat()
    runner.private_json(output/'result.json',result)
    return result


def _coverage_attempt(root,output,contract,contract_sha256,generation,previous):
    attempt=_read_json(output/'attempt.json')
    required={'schema_version','lane','root','session','previous_session','candidate_commit',
        'profile_sha256','generation_id','contract_sha256','boundary_receipt_sha256',
        'credential_env_names','started_at','observed_new_york','ordinary_native_clock',
        'actual_timer_dispatch','timer_dispatch_evidence','deployed'}
    runner.require(type(attempt) is dict and set(attempt)==required,'ordinary_summary_attempt_shape')
    runner.require(type(attempt['schema_version']) is int and attempt['schema_version']==1
        and attempt['lane']=='ordinary_native_workflow'
        and attempt['root']==str(root) and attempt['session']==output.name
        and attempt['previous_session']==previous and attempt['candidate_commit']==contract['candidate_commit']
        and attempt['profile_sha256']==contract['profile_sha256'] and attempt['generation_id']==generation
        and attempt['contract_sha256']==contract_sha256,'ordinary_summary_attempt_identity')
    runner.require(attempt['ordinary_native_clock'] is True and attempt['actual_timer_dispatch'] is False
        and isinstance(attempt['boundary_receipt_sha256'],str)
        and re.fullmatch('[a-f0-9]{64}',attempt['boundary_receipt_sha256']) is not None
        and attempt['deployed'] is False and type(attempt['credential_env_names']) is list
        and all(type(name) is str and name in runner.CREDENTIAL_NAMES for name in attempt['credential_env_names'])
        and len(set(attempt['credential_env_names']))==len(attempt['credential_env_names']),
        'ordinary_summary_attempt_policy')
    started=datetime.fromisoformat(attempt['started_at'])
    runner.require(datetime.fromisoformat(attempt['observed_new_york'])==_local_window(output.name,started),
        'ordinary_summary_attempt_clock')
    return attempt


def _coverage_support(root,output,result):
    """Bind an accepted result to its independent reports and owned child receipts."""
    report=_read_json(output/'native-report.json')
    reproduction=_read_json(output/'report-reproduction.json')
    runner.require(report.get('session')==result['session'] and report.get('generation_id')==result['generation_id']
        and report.get('generation_commit')==result['candidate_commit']
        and report.get('profile_sha256')==result['profile_sha256']
        and runner.report_checks(report)==result['checks']
        and reproduction==result['canonical_report_reproduction'],'ordinary_summary_report_binding')
    window=_read_json(output/'ordinary-window.json')
    observed=datetime.fromisoformat(window['observed_at'])
    started=datetime.fromisoformat(result['started_at']);finished=datetime.fromisoformat(result['finished_at'])
    runner.require(window.get('session')==result['session'] and window.get('allowed') is True
        and window.get('ordinary_idle_expected') is False
        and window.get('expected_next_session')==result['expected_next_session']
        and observed.tzinfo is not None and started<=observed<=finished,
        'ordinary_summary_calendar_binding')
    expected={'calendar':_bootstrap_argv(root,'ordinary-window',output,result['session'],result['previous_session']),
        'native':[str(root/'repo/.venv/bin/python'),str(root/'repo/scripts/recover_runtime.py'),'--repo-root',str(root/'repo')],
        'report':_bootstrap_argv(root,'report',output,result['session'])}
    for operation,command in expected.items():
        label='ordinary-'+result['session']+'-'+operation
        receipt=_read_json(root/(label+'-process.json'))
        runner.require(result.get(operation+'_process')==receipt and receipt.get('argv')==command
            and type(receipt.get('returncode')) is int and receipt['returncode']==0
            and receipt.get('failure') is None
            and runner.sha(runner.no_symlinks(root/'logs'/(label+'.log')))==receipt.get('console_sha256'),
            'ordinary_summary_process_binding')


def ordinary_coverage(root):
    """Read verified ordinary observations; never run a controller or provider.

    A failed/incomplete tail remains visible. Only an entirely accepted, bound
    sequence qualifies; malformed or skipped directories invalidate its counts.
    Timer dispatch and nonzero fills are not inferred from these observations.
    """
    empty={'status':'not_observed','attempted_sessions':0,'accepted_sessions':0,
        'accepted_sequence':False,'sessions':[],'observed_last_result':None,'actual_timer_dispatch':False}
    try:
        root=runner.no_symlinks(root);folder=runner.no_symlinks(root/'ordinary-sessions')
        if not folder.exists():return empty
        _private_directory(folder)
        paths=sorted(folder.iterdir())
        if not paths:return empty
        # Validate all names before counting any observation, including attempts
        # somebody skipped or renamed after a failure.
        for path in paths:
            _private_directory(path)
            runner.require(date.fromisoformat(path.name).isoformat()==path.name,
                'ordinary_summary_session_directory')
        contract,row=runner.verify_contract(root);contract_sha256=runner.sha(root/'contract.json')
        value={**empty,'sessions':[]};previous=None
        for index,output in enumerate(paths):
            runner.require(previous is not None or output.name==contract['reference_session'],
                'ordinary_summary_first_session')
            attempt=_coverage_attempt(root,output,contract,contract_sha256,row['gen_id'],previous)
            value['attempted_sessions']+=1
            if not (output/'result.json').exists():
                runner.require(index==len(paths)-1,'ordinary_summary_skipped_incomplete_session')
                value['sessions'].append({'session':output.name,'status':'incomplete','accepted':False})
                value['status']='incomplete'
                return value
            result=_read_json(output/'result.json')
            runner.require(type(result) is dict and all(result.get(key)==item for key,item in attempt.items())
                and result.get('status') in ('pass','fail') and type(result.get('checks')) is dict
                and type(result.get('canonical_report_reproduction')) is dict
                and type(result.get('recorded_boundary_coverage')) is dict
                and all(type(result.get(key)) is bool for key in
                    ('native_process_success','report_process_success','identity_verified')),
                'ordinary_summary_result_shape_or_binding')
            check_names={'sixteen_books','accounting','staging_complete','staging_valid',
                'evidence_complete','source_completeness'}
            runner.require(set(result['checks'])==check_names|{'outcome'}
                and all(type(result['checks'][key]) is bool for key in check_names)
                and result['checks']['outcome'] in ('clean','degraded','failed','incomplete')
                and isinstance(result.get('reason'),str)
                and re.fullmatch('[a-z0-9_-]{1,160}',result['reason']) is not None,
                'ordinary_summary_public_result_shape')
            finished=datetime.fromisoformat(result['finished_at'])
            runner.require(finished.tzinfo is not None and finished>=datetime.fromisoformat(result['started_at']),
                'ordinary_summary_result_clock')
            observed={key:result.get(key) for key in ('session','status','reason','checks',
                'native_process_success','report_process_success','report_identity_verified','identity_verified')}
            observed['canonical_report_reproduction']={key:
                result['canonical_report_reproduction'].get(key) is True for key in
                ('canonical_json_identical','canonical_markdown_identical')}
            value['observed_last_result']=observed
            if result['status']=='fail':
                runner.require(index==len(paths)-1,'ordinary_summary_skipped_failed_session')
                value['sessions'].append({'session':output.name,'status':'fail','accepted':False})
                value['status']='failed'
                return value
            runner.require(result['native_process_success'] and result['report_process_success']
                and result['identity_verified'] and result.get('report_identity_verified') is True
                and runner.native_acceptance_passed(result)
                and ((previous is None and result.get('expected_next_session') is None)
                    or (previous is not None and result.get('expected_next_session')==output.name)),
                'ordinary_summary_accepted_result_incomplete')
            _coverage_support(root,output,result)
            counts=result.get('session_recorded_boundary_coverage',{})
            count_names=('events','source_events','model_events','observed_failures')
            runner.require(all(type(counts.get(key)) is int and counts[key]>=0 for key in count_names)
                and type(counts.get('clock_events',0)) is int and counts.get('clock_events',0)>=0
                and counts['events']==counts['source_events']+counts['model_events']+counts.get('clock_events',0)
                and counts['observed_failures']<=counts['events'],'ordinary_summary_event_counts')
            value['accepted_sessions']+=1
            value['sessions'].append({'session':output.name,'status':'pass','accepted':True,
                'recorded_boundary_coverage':{**{key:counts[key] for key in count_names},
                    'clock_events':counts.get('clock_events',0)}})
            previous=output.name
        _history(folder,contract,contract_sha256,row['gen_id'])
        value['status']='accepted';value['accepted_sequence']=True
        return value
    except Exception:
        # Arbitrary malformed receipt fields are neither displayed nor counted.
        return {**empty,'status':'invalid','reason':'ordinary_session_receipt_invalid'}
