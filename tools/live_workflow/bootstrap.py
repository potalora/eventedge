"""Owned startup hook: real candidate imports, real clock, pass-through recording.

The .pth installed by prepare loads this in every owned child interpreter. This
file observes native live source, model and scoped clock values. Only an explicit
offline replay substitutes recorded clock inputs.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, timezone
import importlib.util
import json
import os
import re
from pathlib import Path
import socket
import subprocess
import sys
import time

OPENBB_VERSION_SHA256='18cef03d4793aaba4f91ffcc094ba9589529858ed13302d768d5080ee457ac92'


def phase_for(argv, inherited):
    args=list(argv)
    name=Path(args[0]).name if args else ''
    if name in ('run_generations.py','run_cohorts.py'):
        if '--preflight' in args or 'preflight' in args:
            if '--preflight-mode' in args:
                index=args.index('--preflight-mode')
                return 'preflight_'+args[index+1]
            return 'preflight'
        return 'daily'
    if name == 'recover_runtime.py':
        return 'prospective_guard' if '--validate-prospective-launch' in args else 'controller'
    if name == 'generate_operational_report.py':
        return 'operational_report'
    return inherited


def _candidate_native_file(raw, repo, relative):
    path=Path(raw)
    if not path.is_absolute() or path.is_symlink():
        return False
    try:
        parts=path.relative_to(repo).parts
    except ValueError:
        return False
    tail=Path(relative).parts
    return (parts==tail or (len(parts)==len(tail)+2 and parts[0]=='.worktrees'
        and re.fullmatch(r'gen_[0-9]{3,}',parts[1]) is not None and parts[2:]==tail))


def _native_parent_phase(argv, repo, parent_cwd):
    # Only a native candidate Python script can bind a cleaned NOAA worker.
    if not argv or Path(argv[0]).absolute()!=Path(sys.executable).absolute():
        raise RuntimeError('owned NOAA parent interpreter mismatch')
    index=1
    while index<len(argv) and argv[index] in ('-B','-I','-u','-s','-E'):
        index+=1
    if index>=len(argv):
        raise RuntimeError('owned NOAA parent command missing')
    parent_cwd=Path(parent_cwd)
    try:
        cwd_parts=parent_cwd.relative_to(repo).parts
    except ValueError:
        raise RuntimeError('owned NOAA parent working directory outside candidate') from None
    if cwd_parts and not (len(cwd_parts)==2 and cwd_parts[0]=='.worktrees'
            and re.fullmatch(r'gen_[0-9]{3,}',cwd_parts[1]) is not None):
        raise RuntimeError('owned NOAA parent working directory unsupported')
    script=Path(argv[index])
    if not script.is_absolute():
        script=parent_cwd/script
    script=Path(os.path.normpath(script))
    args=argv[index+1:];name=script.name
    if name not in ('run_cohorts.py','run_generations.py') or not _candidate_native_file(script,repo,'scripts/'+name):
        raise RuntimeError('owned NOAA parent command unsupported')
    if args.count('--date')!=1:
        raise RuntimeError('owned NOAA parent date missing')
    try:
        label=args[args.index('--date')+1]
        if date.fromisoformat(label).isoformat()!=label:
            raise ValueError
    except (IndexError,ValueError):
        raise RuntimeError('owned NOAA parent date invalid') from None
    if name=='run_generations.py':
        if not args or args[0] not in ('preflight','run-daily'):
            raise RuntimeError('owned NOAA parent operation unsupported')
        preflight=args[0]=='preflight'
    else:
        preflight='--preflight' in args
        if args.count('--preflight')>1 or '--recover-runtime' in args:
            raise RuntimeError('owned NOAA parent operation unsupported')
    if preflight:
        if args.count('--preflight-mode')!=1:
            raise RuntimeError('owned NOAA parent preflight mode missing')
        position=args.index('--preflight-mode')+1
        if position>=len(args) or args[position] not in ('screen','governed'):
            raise RuntimeError('owned NOAA parent preflight mode invalid')
        return 'preflight_'+args[position]
    if '--preflight-mode' in args:
        raise RuntimeError('owned NOAA parent phase conflicting')
    return 'daily'


def _cleaned_noaa_phase(repo):
    parent=os.getppid();folder=Path('/proc')/str(parent)
    try:
        with (folder/'status').open('rb') as stream:
            status=stream.read(8193)
        with (folder/'cmdline').open('rb') as stream:
            command=stream.read(32769)
    except OSError:
        raise RuntimeError('owned NOAA parent metadata unavailable') from None
    if len(status)>8192 or len(command)>32768 or not command.endswith(b'\x00'):
        raise RuntimeError('owned NOAA parent metadata exceeds bound')
    rows=[line.split()[1:] for line in status.splitlines() if line.startswith(b'Uid:')]
    expected=[str(os.getuid()).encode()]*4
    if rows!=[expected] or os.getppid()!=parent:
        raise RuntimeError('owned NOAA parent identity mismatch')
    try:
        argv=[part.decode('utf-8') for part in command[:-1].split(b'\x00')]
    except UnicodeError:
        raise RuntimeError('owned NOAA parent command invalid') from None
    try:
        parent_cwd=os.readlink(folder/'cwd')
    except OSError:
        raise RuntimeError('owned NOAA parent working directory unavailable') from None
    if len(parent_cwd)>4096 or not Path(parent_cwd).is_absolute() or os.getppid()!=parent:
        raise RuntimeError('owned NOAA parent working directory invalid')
    # Full parent argv/cwd is neither logged nor retained; only the strict phase is returned.
    return _native_parent_phase(argv,repo,parent_cwd)


def install(*, config_path=None):
    inherited_config=os.environ.get('EVENTEDGE_LIVE_CONFIG')
    if config_path is not None and inherited_config is not None and str(config_path)!=inherited_config:
        raise RuntimeError('owned hook configuration conflicts with pinned path')
    config_path=config_path if config_path is not None else inherited_config
    if not config_path:
        raise RuntimeError('owned hook requires private live config')
    config=json.loads(Path(config_path).read_text())
    _install_log_redaction()
    repo=Path(config['candidate_repo']); cwd=Path.cwd()
    if cwd.is_relative_to(repo) and (cwd/'tradingagents').is_dir():
        app=cwd
    else:
        app=repo
    # Root operations and generation workers both import only exact clone code.
    sys.path.insert(0,str(app))
    spec=importlib.util.find_spec('tradingagents')
    if spec is None or spec.origin is None or not Path(spec.origin).resolve().is_relative_to(repo):
        raise RuntimeError('candidate import root mismatch')
    inherited_phase=os.environ.get('EVENTEDGE_LIVE_PHASE')
    phase=phase_for(sys.argv,inherited_phase or config['phase'])
    if _candidate_native_file(sys.argv[0],repo,'tradingagents/strategies/data_sources/noaa_yearly_transport.py'):
        phase=_cleaned_noaa_phase(repo)
        if inherited_phase is not None and inherited_phase!=phase:
            raise RuntimeError('owned NOAA inherited phase conflicts with parent')
    os.environ['EVENTEDGE_LIVE_PHASE']=phase
    if getattr(sys,'_eventedge_live_hooks_installed',False):
        return
    sys._eventedge_live_hooks_installed=True
    # Accepted-state no-IO replay uses explicit prohibitions, not transport tape
    # replay. It must never turn an accidental acquisition into a recorded answer.
    if 'accepted-replay' in sys.argv:
        _forbid_io(_replay_attempts(),repo=repo,git_identity=config['local_git'],
            dependency_site=config['dependency_site'],uname_identity=config['local_uname'])
        return
    from .recorded_io import install_source_io
    from .recorded_models import install_model_io
    arguments={key:config[key] for key in ('mode','tape_dir','run_identity')}
    arguments['phase']=phase
    if config['mode']=='offline_replay':
        # Cleaned source children recover the same private cursor from the pinned
        # offline configuration, even when the native launcher clears its env.
        os.environ['EVENTEDGE_LIVE_REPLAY_STATE']=config['replay_state_dir']
    install_source_io(**arguments)
    install_model_io(**arguments)
    if config['mode']=='live_record' or config.get('replay_observed_clocks') is True:
        from .recorded_clocks import install_clock_io
        install_clock_io(config,phase)
    if config['mode']=='offline_replay':
        from .replay_runtime import install_replay_runtime
        install_replay_runtime(config)


def _install_log_redaction():
    import logging
    from .runner import credential_redactions
    values=credential_redactions(os.environ)
    if not values or getattr(logging.Formatter.format,'_eventedge_redacted',False):
        return
    original=logging.Formatter.format
    def safe_format(self,record):
        text=original(self,record)
        for value in values:
            text=text.replace(value,'[REDACTED]')
        return text
    safe_format._eventedge_redacted=True
    logging.Formatter.format=safe_format


def _imports(repo):
    for name,module in list(sys.modules.items()):
        if name == 'tradingagents' or name.startswith('tradingagents.'):
            source=getattr(module,'__file__',None)
            if source and not Path(source).resolve().is_relative_to(repo):
                raise RuntimeError('application imported outside candidate clone')


def _prepare(root):
    from dataclasses import asdict
    from .runner import private_json
    from tradingagents.strategies.orchestration.generation_manager import GenerationManager
    repo=root/'repo'; _imports(repo)
    from tradingagents.strategies.orchestration.multi_strategy_engine import _fetch_timeout_s
    from tradingagents.strategies.runtime_deadline import DEFAULT_MODEL_BUDGET_S,MODEL_CALL_CAP_S
    if (_fetch_timeout_s(),DEFAULT_MODEL_BUDGET_S,MODEL_CALL_CAP_S) != (900,2400,120):
        raise RuntimeError('accepted native runtime deadlines changed')
    row=GenerationManager(str(repo)).start_generation('Isolated real-source live-model workflow acceptance',
        config_file=root/'profile.json')
    _imports(repo)
    private_json(root/'generation.json',asdict(row))


def _window(root):
    from .runner import private_json
    from tradingagents.strategies.orchestration.decision_clock import latest_completed_session,require_reference
    from tradingagents.strategies.orchestration.trading_calendar import exchange_date,is_session,next_session,session_open
    now=datetime.now(timezone.utc); session=latest_completed_session(now); allowed=True
    try:
        require_reference(session,now)
    except ValueError:
        allowed=False
    private_json(root/'window.json',{'observed_at':now.isoformat(),'session':session.isoformat(),'allowed':allowed,
        'next_open':session_open(next_session(session)).isoformat(),
        'ordinary_idle_expected':not is_session(exchange_date(now))})


def _check_idle(root):
    from .runner import private_json,require
    repo=root/'repo'
    journal=json.loads((repo/'data/runtime_recovery/journal.json').read_text())
    require(journal.get('incidents') == {},'ordinary_controller_not_idle')
    row=json.loads((root/'generation.json').read_text())
    state=Path(row['state_dir'])
    require(sorted(p.name for p in state.iterdir()) == [Path(row['config_file']).name], 'idle_created_application_state')
    private_json(root/'ordinary-idle.json',{'idle':True,'observed_at':datetime.now(timezone.utc).isoformat(),
        'journal_incidents':0,'application_state_unchanged':True})


def _ordinary_window(root, session, previous_session, output_dir):
    from .runner import private_json,no_symlinks,require
    from tradingagents.strategies.orchestration.decision_clock import latest_completed_session,require_reference
    from tradingagents.strategies.orchestration.trading_calendar import exchange_date,is_session,next_session
    output=no_symlinks(output_dir)
    require(output.is_dir() and output.is_relative_to(root),'ordinary_output_must_be_private_campaign_directory')
    now=datetime.now(timezone.utc); latest=latest_completed_session(now); allowed=True
    try:
        require_reference(latest,now)
    except ValueError:
        allowed=False
    private_json(output/'ordinary-window.json',{'observed_at':now.isoformat(),
        'session':latest.isoformat(),'allowed':allowed,
        'ordinary_idle_expected':not is_session(exchange_date(now)),
        'expected_next_session':next_session(date.fromisoformat(previous_session)).isoformat() if previous_session else None})


def _report(root, session, output_dir=None):
    from .runner import private_json,no_symlinks,require
    from tradingagents.strategies.orchestration.operational_report import build_operational_report,render_operational_report
    row=json.loads((root/'generation.json').read_text()); repo=root/'repo'
    report=build_operational_report(repo,row['gen_id'],session)
    from tradingagents.strategies.orchestration.source_incidents import load_source_incidents
    report['source_incidents']=load_source_incidents(report,repo/'docs/reports')
    output=root if output_dir is None else no_symlinks(output_dir)
    require(output.is_dir() and output.is_relative_to(root),'report_output_must_be_private_campaign_directory')
    private_json(output/'native-report.json',report)
    stem=f"{session}-{row['gen_id']}-operational-report"
    json_path=repo/'docs/reports'/f'{stem}.json'; md_path=repo/'docs/reports'/f'{stem}.md'
    json_matches=json_path.exists() and json.loads(json_path.read_text()) == report
    markdown_matches=md_path.exists() and md_path.read_text() == render_operational_report(report)
    # Missing or divergent canonical shell reports remain visible, never regenerated
    # to manufacture a passing comparison.
    private_json(output/'report-reproduction.json',{'canonical_json_identical':json_matches,
        'canonical_markdown_identical':markdown_matches,'observed_at':datetime.now(timezone.utc).isoformat()})
    _imports(repo)


def _replay_attempts():
    if not hasattr(sys,'_eventedge_replay_attempts'):
        sys._eventedge_replay_attempts=Counter()
    return sys._eventedge_replay_attempts


def _install_process_guard(attempts, *, repo, git_identity, dependency_site=None, uname_identity=None):
    """Allow pinned native/dependency local reads; all other processes stay denied."""
    from .runner import readonly_git_identity,readonly_uname_identity,no_symlinks,sha
    repo=Path(repo)
    if not repo.is_absolute() or repo != repo.resolve():
        raise RuntimeError('replay process guard configuration invalid')
    if uname_identity is not None and (not isinstance(uname_identity,dict)
            or set(uname_identity)!={'path','sha256'} or uname_identity['path']!='/usr/bin/uname'
            or re.fullmatch(r'[0-9a-f]{64}',str(uname_identity['sha256'])) is None):
        raise RuntimeError('replay process guard uname configuration invalid')
    if (not isinstance(git_identity,dict) or set(git_identity)!= {'path','sha256'}
            or git_identity['path']!='/usr/bin/git'
            or re.fullmatch(r'[0-9a-f]{64}',str(git_identity['sha256'])) is None):
        raise RuntimeError('replay process guard configuration invalid')
    openbb_cwd=None
    if dependency_site is not None:
        site=no_symlinks(dependency_site)
        if site != site.resolve():
            raise RuntimeError('replay process guard dependency configuration invalid')
        openbb_cwd=site/'openbb_core/app'
        if sha(no_symlinks(openbb_cwd/'version.py')) != OPENBB_VERSION_SHA256:
            raise RuntimeError('replay process guard dependency version changed')
    state=getattr(sys,'_eventedge_replay_process_guard',None)
    if state is not None:
        if (state['repo']!=str(repo) or state['identity']!=git_identity or state['attempts'] is not attempts
                or state['openbb_cwd']!=openbb_cwd or state['uname_identity']!=uname_identity):
            raise RuntimeError('replay process guard configuration changed')
        subprocess.Popen=state['wrapper']
        return state
    state={'repo':str(repo),'identity':dict(git_identity),'attempts':attempts,
        'original':subprocess.Popen,'allowed':Counter(),'openbb_cwd':openbb_cwd,
        'uname_identity':None if uname_identity is None else dict(uname_identity)}
    expected=['git','-C',str(repo),'rev-parse','--path-format=absolute','--git-common-dir']
    def guarded(argv,*args,**kwargs):
        canonical=(type(argv) is list and all(type(value) is str for value in argv) and argv==expected
            and not args and set(kwargs)=={'stdout','stderr','text'}
            and type(kwargs['stdout']) is int and kwargs['stdout']==subprocess.PIPE
            and type(kwargs['stderr']) is int and kwargs['stderr']==subprocess.PIPE and kwargs['text'] is True
            and state['allowed']['canonical_git_common_dir']<1)
        openbb=(openbb_cwd is not None and type(argv) is list and all(type(value) is str for value in argv)
            and argv==['/usr/bin/git','rev-parse','--is-inside-work-tree'] and not args
            and set(kwargs)=={'cwd','stdout','stderr'} and type(kwargs['cwd']) is type(openbb_cwd)
            and kwargs['cwd']==openbb_cwd and type(kwargs['stdout']) is int
            and kwargs['stdout']==subprocess.DEVNULL and type(kwargs['stderr']) is int
            and kwargs['stderr']==subprocess.DEVNULL and state['allowed']['openbb_dependency_git_probe']<2)
        uname=(uname_identity is not None and type(argv) is list and all(type(value) is str for value in argv)
            and argv==['uname','-p'] and not args and set(kwargs)=={'stdout','stderr','text','encoding'}
            and type(kwargs['stdout']) is int and kwargs['stdout']==subprocess.PIPE
            and type(kwargs['stderr']) is int and kwargs['stderr']==subprocess.DEVNULL
            and kwargs['text'] is True and kwargs['encoding']=='utf8'
            and state['allowed']['platform_uname_processor']<1)
        valid=canonical or openbb or uname
        if valid:
            try:
                valid=(readonly_uname_identity(state['uname_identity']['sha256'])==state['uname_identity'] if uname
                    else readonly_git_identity(state['identity']['sha256'])==state['identity'])
                if openbb:
                    valid=valid and sha(no_symlinks(openbb_cwd/'version.py'))==OPENBB_VERSION_SHA256
            except (OSError,ValueError):
                valid=False
        if not valid:
            attempts['process']+=1
            raise RuntimeError('process')
        operation=('canonical_git_common_dir' if canonical else
            'platform_uname_processor' if uname else 'openbb_dependency_git_probe')
        state['allowed'][operation]+=1
        if uname:
            return state['original'](['/usr/bin/uname','-p'],cwd=str(repo),stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,encoding='utf8',close_fds=True,
                env={'PATH':'/usr/bin:/bin','HOME':'/nonexistent','LANG':'C.UTF-8'})
        if openbb:
            return state['original'](['/usr/bin/git','--no-optional-locks','-c','core.hooksPath=/dev/null',
                'rev-parse','--is-inside-work-tree'],cwd=str(openbb_cwd),stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,close_fds=True,
                env={'PATH':'/usr/bin:/bin','HOME':'/nonexistent','LANG':'C.UTF-8',
                    'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null','GIT_TERMINAL_PROMPT':'0',
                    'GIT_OPTIONAL_LOCKS':'0','GIT_NO_LAZY_FETCH':'1'})
        return state['original'](['/usr/bin/git','--no-optional-locks','-c','core.hooksPath=/dev/null',
            '-C',str(repo),'rev-parse','--path-format=absolute','--git-common-dir'],
            cwd=str(repo),stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            text=True,close_fds=True,env={'PATH':'/usr/bin:/bin','HOME':'/nonexistent','LANG':'C.UTF-8',
            'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null','GIT_TERMINAL_PROMPT':'0',
            'GIT_OPTIONAL_LOCKS':'0','GIT_NO_LAZY_FETCH':'1'})
    state['wrapper']=guarded
    sys._eventedge_replay_process_guard=state
    subprocess.Popen=guarded
    return state


def _forbid_io(attempts, *, repo, git_identity, dependency_site=None, uname_identity=None):
    class ForbiddenIO(RuntimeError):
        pass
    def forbidden(name):
        def stop(*args,**kwargs):
            attempts[name]+=1
            raise ForbiddenIO(name)
        return stop
    _install_process_guard(attempts,repo=repo,git_identity=git_identity,dependency_site=dependency_site,
        uname_identity=uname_identity)
    for name in ('getaddrinfo','gethostbyname','create_connection'):
        setattr(socket,name,forbidden('network.'+name))
    for name in ('connect','connect_ex'):
        setattr(socket.socket,name,forbidden('network.socket.'+name))
    import requests,httpx,urllib.request
    requests.Session.request=forbidden('requests')
    httpx.Client.send=forbidden('httpx'); httpx.AsyncClient.send=forbidden('httpx.async')
    urllib.request.urlopen=forbidden('urllib')
    try:
        import httpx2
        httpx2.Client.send=forbidden('httpx2'); httpx2.AsyncClient.send=forbidden('httpx2.async')
    except ImportError:
        pass
    try:
        import curl_cffi
        curl_cffi.Curl.perform=forbidden('curl'); curl_cffi.AsyncCurl.add_handle=forbidden('curl.async')
    except ImportError:
        pass
    from tradingagents.strategies import llm_utils,runtime_deadline
    llm_utils.call_analysis_model=forbidden('model.analysis')
    runtime_deadline.bounded_transport=forbidden('model.transport')
    return forbidden


def _accepted_replay(root, session, *, output_dir=None, dependency_site=None, uname_identity=None):
    from .runner import (BOOKS,CREDENTIAL_NAMES,private_json,require,snapshot_state,state_inventory,native_inventory)
    from tradingagents.strategies.orchestration.runtime_lock import runtime_lock,canonical_runtime_lock_path
    from tradingagents.strategies.orchestration.generation_config import CONFIG_FILE_ENV,CONFIG_SHA_ENV
    from tradingagents.strategies.orchestration.cohort_orchestrator import CohortOrchestrator,build_default_cohorts
    from tradingagents.strategies.runtime_deadline import model_budget
    repo=root/'repo'; row=json.loads((root/'generation.json').read_text())
    # Imports of run_cohorts below resolve to the reviewed generation's scripts.
    sys.path.insert(0,row['worktree_path'])
    from scripts.run_cohorts import _build_config,_build_parser
    action=root if output_dir is None else Path(output_dir)
    replay=action/'accepted-state'; attempts=_replay_attempts(); owner=None
    with runtime_lock(canonical_runtime_lock_path(repo),exclusive=True):
        state=Path(row['state_dir']); before=state_inventory(state)
        frozen_profile=json.loads(Path(row['config_file']).read_text())
        native_before=native_inventory(repo,frozen_profile)
        snapshot_state(state,replay); copy_before=state_inventory(replay)
        require(before == copy_before,'replay_copy_mismatch')
        os.environ.update({CONFIG_FILE_ENV:row['config_file'],CONFIG_SHA_ENV:row['config_sha256'],
            'AUTORESEARCH_STATE_DIR':str(replay)})
        config=json.loads((root/'hook-config.json').read_text())
        bound_site=dependency_site if dependency_site is not None else config['dependency_site']
        bound_uname=uname_identity if uname_identity is not None else config['local_uname']
        forbidden=_forbid_io(attempts,repo=repo,git_identity=config['local_git'],dependency_site=bound_site,
            uname_identity=bound_uname)
        config=_build_config(_build_parser().parse_args([]))
        try:
            owner=CohortOrchestrator(build_default_cohorts(config),config,
                generation_id=row['gen_id'],generation_commit=row['git_commit'])
            for cohort in owner.cohorts:
                for source in cohort['engine'].registry._sources.values():
                    for name in dir(type(source)):
                        if name == 'fetch' or name.startswith(('fetch_','get_','search_')):
                            if callable(getattr(source,name,None)):
                                setattr(source,name,forbidden('provider.'+source.name+'.'+name))
            for name in dir(type(owner._price_source)):
                if name.startswith(('fetch','get_','resolve_')) and callable(getattr(owner._price_source,name,None)):
                    setattr(owner._price_source,name,forbidden('price.'+name))
            with model_budget(time.monotonic()+110):
                result=owner.run_daily(session)
            require(sorted(result) == BOOKS and all(v.get('replayed') is True for v in result.values()),'completed_replay_required')
            require(not attempts,'replay_io_attempted')
        finally:
            if owner is not None:
                for cohort in owner.cohorts:
                    cohort['ledger'].close()
        after=state_inventory(state); copy_after=state_inventory(replay); native_after=native_inventory(repo,frozen_profile)
        require(before == after and copy_before == copy_after and native_before == native_after,'immutable_replay_changed_state')
        _imports(repo)
        private_json(action/'accepted-replay.json',{'schema_version':1,'kind':'accepted_state_no_io_replay',
            'zero_attempted_calls':True,'attempted_calls':dict(attempts),
            'attempted_calls_scope':'forbidden_external_io_and_arbitrary_processes',
            'allowed_local_process_calls':dict(sys._eventedge_replay_process_guard['allowed']),
            'credential_env_names':sorted(name for name in CREDENTIAL_NAMES if name in os.environ),
            'replayed_books':sorted(result),
            'all_logical_tables_identical':True,'accepted_inputs_identical':True,'native_artifacts_identical':True,
            'state_before_sha256':__import__('tools.live_workflow.runner',fromlist=['digest']).digest(before),
            'cold_acquisition_replay':False,'session':session})


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=('prepare','window','ordinary-window','check-idle','report','accepted-replay'))
    parser.add_argument('--root',required=True); parser.add_argument('--session')
    parser.add_argument('--output-dir');parser.add_argument('--previous-session')
    args=parser.parse_args(argv); root=Path(args.root)
    operations={'prepare':_prepare,'window':_window,'check-idle':_check_idle}
    if args.operation in operations:
        operations[args.operation](root)
    elif args.operation == 'report':
        _report(root,args.session,args.output_dir)
    elif args.operation == 'ordinary-window':
        _ordinary_window(root,args.session,args.previous_session,args.output_dir)
    else:
        _accepted_replay(root,args.session)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
