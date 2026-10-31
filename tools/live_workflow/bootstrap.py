"""Owned startup hook: real candidate imports, real clock, pass-through recording.

The .pth installed by prepare loads this in every owned child interpreter. This
file contains no fake source, model, auth, datetime, date or SDK implementation.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time


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


def install():
    config_path=os.environ.get('EVENTEDGE_LIVE_CONFIG')
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
    phase=phase_for(sys.argv,os.environ.get('EVENTEDGE_LIVE_PHASE',config['phase']))
    os.environ['EVENTEDGE_LIVE_PHASE']=phase
    if getattr(sys,'_eventedge_live_hooks_installed',False):
        return
    sys._eventedge_live_hooks_installed=True
    # Accepted-state no-IO replay uses explicit prohibitions, not transport tape
    # replay. It must never turn an accidental acquisition into a recorded answer.
    if 'accepted-replay' in sys.argv:
        _forbid_io(_replay_attempts())
        return
    from .recorded_io import install_source_io
    from .recorded_models import install_model_io
    arguments={key:config[key] for key in ('mode','tape_dir','run_identity')}
    arguments['phase']=phase
    install_source_io(**arguments)
    install_model_io(**arguments)


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


def _report(root, session):
    from .runner import private_json
    from tradingagents.strategies.orchestration.operational_report import build_operational_report,render_operational_report
    row=json.loads((root/'generation.json').read_text()); repo=root/'repo'
    report=build_operational_report(repo,row['gen_id'],session)
    from tradingagents.strategies.orchestration.source_incidents import load_source_incidents
    report['source_incidents']=load_source_incidents(report,repo/'docs/reports')
    private_json(root/'native-report.json',report)
    stem=f"{session}-{row['gen_id']}-operational-report"
    json_path=repo/'docs/reports'/f'{stem}.json'; md_path=repo/'docs/reports'/f'{stem}.md'
    json_matches=json_path.exists() and json.loads(json_path.read_text()) == report
    markdown_matches=md_path.exists() and md_path.read_text() == render_operational_report(report)
    # Missing or divergent canonical shell reports remain visible, never regenerated
    # to manufacture a passing comparison.
    private_json(root/'report-reproduction.json',{'canonical_json_identical':json_matches,
        'canonical_markdown_identical':markdown_matches,'observed_at':datetime.now(timezone.utc).isoformat()})
    _imports(repo)


def _replay_attempts():
    if not hasattr(sys,'_eventedge_replay_attempts'):
        sys._eventedge_replay_attempts=Counter()
    return sys._eventedge_replay_attempts


def _forbid_io(attempts):
    class ForbiddenIO(RuntimeError):
        pass
    def forbidden(name):
        def stop(*args,**kwargs):
            attempts[name]+=1
            raise ForbiddenIO(name)
        return stop
    subprocess.Popen=forbidden('process')
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


def _accepted_replay(root, session):
    from .runner import (BOOKS,private_json,require,snapshot_state,state_inventory,native_inventory)
    from tradingagents.strategies.orchestration.runtime_lock import runtime_lock,canonical_runtime_lock_path
    from tradingagents.strategies.orchestration.generation_config import CONFIG_FILE_ENV,CONFIG_SHA_ENV
    from tradingagents.strategies.orchestration.cohort_orchestrator import CohortOrchestrator,build_default_cohorts
    from tradingagents.strategies.runtime_deadline import model_budget
    repo=root/'repo'; row=json.loads((root/'generation.json').read_text())
    # Imports of run_cohorts below resolve to the reviewed generation's scripts.
    sys.path.insert(0,row['worktree_path'])
    from scripts.run_cohorts import _build_config,_build_parser
    replay=root/'accepted-state'; attempts=_replay_attempts(); owner=None
    with runtime_lock(canonical_runtime_lock_path(repo),exclusive=True):
        state=Path(row['state_dir']); before=state_inventory(state)
        frozen_profile=json.loads(Path(row['config_file']).read_text())
        native_before=native_inventory(repo,frozen_profile)
        snapshot_state(state,replay); copy_before=state_inventory(replay)
        require(before == copy_before,'replay_copy_mismatch')
        os.environ.update({CONFIG_FILE_ENV:row['config_file'],CONFIG_SHA_ENV:row['config_sha256'],
            'AUTORESEARCH_STATE_DIR':str(replay)})
        forbidden=_forbid_io(attempts)
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
            subprocess.Popen=forbidden('process')
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
        private_json(root/'accepted-replay.json',{'schema_version':1,'kind':'accepted_state_no_io_replay',
            'zero_attempted_calls':True,'attempted_calls':dict(attempts),'replayed_books':sorted(result),
            'all_logical_tables_identical':True,'accepted_inputs_identical':True,'native_artifacts_identical':True,
            'state_before_sha256':__import__('tools.live_workflow.runner',fromlist=['digest']).digest(before),
            'cold_acquisition_replay':False,'session':session})


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=('prepare','window','check-idle','report','accepted-replay'))
    parser.add_argument('--root',required=True); parser.add_argument('--session')
    args=parser.parse_args(argv); root=Path(args.root)
    operations={'prepare':_prepare,'window':_window,'check-idle':_check_idle}
    if args.operation in operations:
        operations[args.operation](root)
    elif args.operation == 'report':
        _report(root,args.session)
    else:
        _accepted_replay(root,args.session)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
