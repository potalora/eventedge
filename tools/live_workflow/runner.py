"""Standalone candidate-parameterized acceptance orchestration.

This module imports only the standard library. Application code is imported by
owned child interpreters from the disposable exact candidate, never this checkout.
The trusted external supervisor owns OS isolation and descendant/cgroup cleanup.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from urllib.parse import quote, quote_plus

BOOKS = sorted(f'horizon_{h}_size_{s}' for h in ('30d','3m','6m','1y') for s in ('5k','10k','50k','100k'))
AUTH_PATH = '/home/hermes/.config/eventedge/plan-auth.json'
CREDENTIAL_NAMES = frozenset(('ALPACA_API_KEY','ALPACA_SECRET_KEY','FRED_API_KEY','FINNHUB_API_KEY',
    'FMP_API_KEY','COURTLISTENER_TOKEN','NOAA_CDO_TOKEN','USDA_NASS_API_KEY'))
PATH_FIELDS = {('project_dir',): '', ('results_dir',):'data/results', ('data_cache_dir',):'data/dataflows_cache',
    ('autoresearch','results_dir'):'data/results', ('autoresearch','source_cache_dir'):'data/source_cache'}


class AcceptanceError(ValueError):
    """Fail-closed local campaign admission or evidence error."""


def require(value, reason):
    if not value:
        raise AcceptanceError(reason)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',',':'), allow_nan=False).encode()).hexdigest()


def sha(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024**2), b''):
            result.update(block)
    return result.hexdigest()


def _kernel_overflow_uid():
    with Path('/proc/sys/kernel/overflowuid').open('rb') as stream:
        value=stream.read(33)
    require(len(value)<=32 and re.fullmatch(rb'[0-9]{1,10}\n?',value) is not None,
        'readonly_git_binary_overflow_uid')
    result=int(value)
    require(0 < result < 2**32-1,'readonly_git_binary_overflow_uid')
    return result


def readonly_git_identity(expected_sha256):
    # The supervisor verifies host ownership outside its user namespace and
    # supplies this pin. Incoming argv/current bytes never select the trust pin.
    require(isinstance(expected_sha256,str) and re.fullmatch(r'[0-9a-f]{64}',expected_sha256) is not None,
        'readonly_git_binary_pin_required')
    path=no_symlinks('/usr/bin/git'); info=path.lstat()
    owner_valid=info.st_uid == 0
    if not owner_valid:
        # A host-root-owned /usr binary is unmapped inside the single-UID
        # sandbox. This exception also requires the kernel's overflow identity
        # and a read-only mount; arbitrary non-root owners remain forbidden.
        owner_valid=(info.st_uid == _kernel_overflow_uid()
            and bool(os.statvfs(path).f_flag & os.ST_RDONLY))
    require(stat.S_ISREG(info.st_mode) and owner_valid and not info.st_mode & 0o022,
        'readonly_git_binary_identity')
    require(sha(path) == expected_sha256,'readonly_git_binary_hash_mismatch')
    return {'path':str(path),'sha256':expected_sha256}


def private_json(path, value):
    path = Path(path)
    fd = os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(value, handle, sort_keys=True, indent=2, allow_nan=False)
        handle.write('\n'); handle.flush(); os.fsync(handle.fileno())


def no_symlinks(path):
    path = Path(path).absolute()
    require(not any(c in str(path) for c in '\x00\r\n'), 'invalid_path_characters')
    require(not any(part.is_symlink() for part in (path,*path.parents)), 'path_symlink')
    return path


def git(repo, *args):
    # No ambient Git config, aliases, executable hooks or transport overrides.
    env = {'PATH':'/usr/bin:/bin', 'HOME':'/nonexistent', 'GIT_CONFIG_NOSYSTEM':'1',
        'GIT_CONFIG_GLOBAL':'/dev/null', 'GIT_TERMINAL_PROMPT':'0'}
    return subprocess.check_output(['git','-c','core.hooksPath=/dev/null','-C',str(repo),*args],
        env=env, text=True, stderr=subprocess.PIPE, timeout=45).strip()


def validate_candidate(root, commit):
    root = no_symlinks(root)
    require(re.fullmatch('[0-9a-f]{40}', commit) is not None, 'full_candidate_commit_required')
    require(root.is_dir() and git(root,'rev-parse','--show-toplevel') == str(root), 'candidate_root')
    require(git(root,'rev-parse','HEAD') == commit, 'candidate_commit_mismatch')
    require(not git(root,'status','--porcelain','--untracked-files=all'), 'dirty_candidate')
    extras=git(root,'ls-files','--others','--','tradingagents','scripts','config','configs','tools').splitlines()
    require(not any('__pycache__' not in Path(name).parts and not name.endswith('.pyc') for name in extras),
        'dirty_ignored_application_code')
    tracked=git(root,'ls-files','--','tradingagents','scripts','config','configs').splitlines()
    require(not any((root/name).is_symlink() for name in tracked), 'application_symlink')
    return root


def clone_candidate(root, commit, destination):
    root = validate_candidate(root, commit); destination = no_symlinks(destination)
    require(not destination.exists(), 'destination_exists')
    env = {'PATH':'/usr/bin:/bin','HOME':'/nonexistent','GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null'}
    subprocess.run(['git','-c','core.hooksPath=/dev/null','clone','--no-hardlinks','--no-checkout',
        '--template=','--',str(root),str(destination)], env=env, check=True, capture_output=True, timeout=120)
    git(destination,'config','core.hooksPath','/dev/null')
    git(destination,'remote','remove','origin')
    git(destination,'checkout','--detach',commit)
    require(not (destination/'.git/objects/info/alternates').exists(), 'shared_object_alternates')
    require(all(p.stat().st_nlink == 1 for p in (destination/'.git/objects').rglob('*') if p.is_file()), 'shared_git_objects')
    require(not git(destination,'remote'), 'clone_has_remote')
    validate_candidate(destination,commit)
    return destination


def normalize_profile(original, repo):
    repo = no_symlinks(repo); value = deepcopy(original)
    require(value.get('execution',{}).get('mode') == 'paper' and
        value['execution'].get('execution_enabled') is False, 'paper_only')
    require(value.get('alerts',{}).get('enabled') is False and value['alerts'].get('channels') == [], 'notifications_disabled')
    require(value.get('decision_shadow',{}).get('enabled') is False, 'decision_shadow_disabled')
    ar = value.get('autoresearch',{})
    require(not any(item for key,item in ar.items() if key.endswith(('_api_key','_token','_secret'))),
        'profile_credentials_must_be_empty')
    require(ar.get('llm_billing_mode') == 'chatgpt_plan', 'native_plan_only')
    require(ar.get('chatgpt_plan_auth_path') == AUTH_PATH, 'canonical_auth_path_required')
    require(ar.get('stabilization_profile_policy') == 'supported_sixteen_books_v1', 'stabilization_profile_required')
    audit=[]
    def walk(node, prefix=()):
        if isinstance(node,dict):
            for key, item in node.items():
                location=prefix+(key,)
                if location in PATH_FIELDS:
                    require(isinstance(item,str) and Path(item).is_absolute(), 'invalid_profile_path')
                    replacement=str(repo/PATH_FIELDS[location])
                    audit.append({'path':list(location),'before':item,'after':replacement})
                    node[key]=replacement
                elif location == ('autoresearch','chatgpt_plan_auth_path'):
                    pass
                elif isinstance(item,str) and (item.startswith('/') or key.endswith(('_dir','_path'))):
                    require(not item, 'unapproved_profile_path')
                elif isinstance(item,(dict,list)):
                    walk(item,location)
        elif isinstance(node,list):
            for index,item in enumerate(node):
                walk(item,prefix+(index,))
    walk(value)
    require({tuple(row['path']) for row in audit} == set(PATH_FIELDS), 'missing_profile_path')
    return value,audit


def sanitized_environment(root, ambient=None, credentials=None):
    root=Path(root); credentials=credentials or {}
    require(set(credentials) <= CREDENTIAL_NAMES, 'unapproved_credential_name')
    for name,value in credentials.items():
        require(isinstance(value,str) and not any(c in value for c in '\x00\r\n'), 'invalid_credential_value')
    # Deliberately disregard the ambient environment, including proxies, secrets,
    # feature flags, PYTHON*, loader flags, fake clocks and model/billing routes.
    env={'PATH':str(root/'repo/.venv/bin')+':/usr/bin:/bin', 'HOME':str(root/'home'),
        'XDG_CONFIG_HOME':str(root/'home/.config'),'XDG_CACHE_HOME':str(root/'cache'),
        'XDG_DATA_HOME':str(root/'home/.local/share'),'TMPDIR':str(root/'tmp'),
        'LANG':'C.UTF-8','LC_ALL':'C.UTF-8','TZ':'UTC','PYTHONNOUSERSITE':'1','PYTHONDONTWRITEBYTECODE':'1',
        'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null','GIT_TERMINAL_PROMPT':'0',
        'EVENTEDGE_PYTHON':str(root/'repo/.venv/bin/python'), 'EVENTEDGE_LOG_DIR':str(root/'repo/data/logs'),
        'EVENTEDGE_SOURCE_CACHE_DIR':str(root/'repo/data/source_cache'),
        'AUTORESEARCH_FETCH_TIMEOUT_S':'900'}
    env.update(credentials)
    return env


def dependency_fingerprint(site):
    site=no_symlinks(site)
    require(site.is_dir(), 'dependency_site_missing')
    files={}
    for path in sorted(site.rglob('*')):
        require(not path.is_symlink() or path.resolve().is_relative_to(site), 'dependency_symlink_escape')
        require(path.name.split('.')[0] not in ('sitecustomize','usercustomize'), 'dependency_sitecustomize_forbidden')
        if '__pycache__' in path.parts or path.is_dir():
            continue
        if path.suffix == '.pth' or path.name in ('sitecustomize.py','usercustomize.py'):
            text=path.read_text(errors='replace').lower()
            require(not any(marker in text for marker in ('scheduled_acceptance','acceptance.bootstrap',
                'eventedge_acceptance','freezegun','faketime','transportfixture')), 'synthetic_dependency_hook')
        files[str(path.relative_to(site))]=sha(path)
    return digest(files)


def read_credentials(path):
    if path is None:
        return {}
    path=no_symlinks(path); metadata=path.stat()
    require(stat.S_ISREG(metadata.st_mode) and metadata.st_uid == os.getuid()
        and metadata.st_mode & 0o077 == 0 and metadata.st_nlink == 1 and metadata.st_size < 65536, 'credential_file_permissions')
    value=json.loads(path.read_text())
    require(type(value) is dict, 'credential_file_shape')
    sanitized_environment(path.parent,{},value)
    return value


def owned_environment(root, credentials=None):
    env=sanitized_environment(root,{},credentials)
    env['EVENTEDGE_LIVE_CONFIG']=str(Path(root)/'hook-config.json')
    return env


def _child(root, operation, *, credentials=None, timeout=120, extra=()):
    root=Path(root)
    command=[str(root/'repo/.venv/bin/python'),'-m','tools.live_workflow.bootstrap',operation,'--root',str(root),*extra]
    return run_process(command,root,env=owned_environment(root,credentials),label=operation,timeout=timeout)


def credential_redactions(env):
    values=set()
    for key in CREDENTIAL_NAMES:
        value=env.get(key)
        if value:
            values.update((value,quote(value,safe=''),quote_plus(value)))
    return sorted(values,key=lambda value:(-len(value),value))


def _copy_redacted(source, destination, env):
    secrets=[value.encode() for value in credential_redactions(env)]
    longest=max(map(len,secrets),default=1); pending=b''
    def redact(data):
        for secret in secrets:
            data=data.replace(secret,b'[REDACTED]')
        return data
    read=getattr(source,'read1',source.read)
    while True:
        block=read(8192)
        if not block:
            destination.write(redact(pending)); destination.flush(); return
        data=pending+block; cut=max(0,len(data)-longest)
        # Never divide a credential spanning the retained boundary.
        for secret in secrets:
            start=data.find(secret,max(0,cut-len(secret)+1))
            if 0 <= start < cut < start+len(secret):
                cut=start
        destination.write(redact(data[:cut])); destination.flush(); pending=data[cut:]


def run_process(argv, root, *, env, label, timeout):
    """Keep private output bounded; supervisor cgroup contains detached descendants."""
    root=Path(root); output=root/'logs'/f'{label}.log'
    require(not output.exists(), 'attempt_log_exists')
    started=datetime.now(timezone.utc).isoformat()
    with output.open('xb') as stream:
        os.chmod(output,0o600)
        process=subprocess.Popen(argv,cwd=root/'repo',env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,start_new_session=True)
        pump_errors=[]
        def pump_output():
            try:
                _copy_redacted(process.stdout,stream,env)
            except BaseException:
                pump_errors.append('console_capture_failed')
        pump=threading.Thread(target=pump_output,daemon=True)
        pump.start()
        deadline=time.monotonic()+timeout; reason=None
        try:
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    reason='outer_timeout'; break
                if output.stat().st_size > 1024**2:
                    reason='console_limit'; break
                time.sleep(.05)
        finally:
            if process.poll() is None:
                os.killpg(process.pid,signal.SIGTERM)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid,signal.SIGKILL); process.wait(timeout=3)
            pump.join(timeout=3)
            if pump.is_alive():
                reason=reason or 'descendant_retained_output_pipe_external_cleanup_required'
            else:
                process.stdout.close()
            if pump_errors:
                reason=reason or pump_errors[0]
    receipt={'argv':argv,'started_at':started,'finished_at':datetime.now(timezone.utc).isoformat(),
        'returncode':process.returncode,'failure':reason,'console_sha256':sha(output),
        'console_representation':'explicit_source_credential_redacted_bytes',
        'descendant_cleanup':'external_supervisor_cgroup_required'}
    private_json(root/f'{label}-process.json',receipt)
    require(reason is None and process.returncode == 0, f'{label}_failed_see_private_log')
    return receipt


def prospective_argv(repo, row, session):
    repo=Path(repo)
    return [str(repo/'.venv/bin/python'),str(repo/'scripts/recover_runtime.py'),'--repo-root',str(repo),
        '--prospective-session',session,'--expected-generation',row['gen_id'],'--expected-commit',row['git_commit'],
        '--expected-profile-sha256',row['config_sha256'],'--expected-created-at',row['created_at']]


def load_profile_bytes(source, selected, expected_sha256):
    profile=no_symlinks(selected if selected is not None else Path(source)/'config/profiles/stabilization-v3.json')
    require(profile.is_file(), 'profile_file_required')
    body=profile.read_bytes()
    require(hashlib.sha256(body).hexdigest() == expected_sha256, 'original_profile_hash_mismatch')
    return body


def _write_owned_startup(site, dependency_site, hooks, config_path):
    # The owned .pth binds the exact prepared config even when native supervised
    # workers deliberately clear their environment. Ambient conflicts still fail.
    code=("try:\n from tools.live_workflow.bootstrap import install\n install(config_path="
        +repr(str(config_path))+")\nexcept BaseException:\n import traceback; traceback.print_exc(); os._exit(90)")
    startup='import os; exec('+repr(code)+')'
    (Path(site)/'live_workflow.pth').write_text(str(dependency_site)+'\n'+str(hooks)+'\n'+startup+'\n')


def prepare(args):
    git_identity=readonly_git_identity(args.git_sha256)
    source=validate_candidate(args.candidate_root,args.candidate_commit)
    require(date.fromisoformat(args.session).isoformat() == args.session,'invalid_reference_session')
    root=no_symlinks(args.destination)
    require(not root.exists(), 'destination_exists')
    profile_bytes=load_profile_bytes(source,getattr(args,'profile_file',None),args.profile_sha256)
    original=json.loads(profile_bytes); normalized,audit=normalize_profile(original,root/'repo')
    dependency_site=no_symlinks(args.dependency_site)
    require(dependency_fingerprint(dependency_site) == args.dependency_sha256, 'dependency_hash_mismatch')
    require(not dependency_site.is_relative_to(root), 'dependency_site_must_be_external_readonly')
    # All files are owned by this campaign UID; no ambient application dotenv.
    root.mkdir(mode=0o700)
    for name in ('home','cache','tmp','logs','tape','hooks'):
        (root/name).mkdir(mode=0o700)
    repo=clone_candidate(source,args.candidate_commit,root/'repo')
    (root/'original-profile.json').write_bytes(profile_bytes); os.chmod(root/'original-profile.json',0o600)
    private_json(root/'profile.json',normalized)
    private_json(root/'profile-substitutions.json',audit)
    (repo/'.env').write_text(''); os.chmod(repo/'.env',0o600)
    (repo/'.git/info').mkdir(exist_ok=True)
    with (repo/'.git/info/exclude').open('a') as exclude:
        exclude.write('\n/data/runtime_recovery/\n/data/source_cache/\n/data/dataflows_cache/\n/data/results/\n/data/operational/eventedge-runtime.lock\n')
    env=sanitized_environment(root)
    subprocess.run([str(no_symlinks(args.python)),'-I','-m','venv','--without-pip',str(repo/'.venv')],
        env=env,check=True,capture_output=True,timeout=60)
    py=repo/'.venv/bin/python'
    version=subprocess.check_output([str(py),'-I','-c','import sys;print("%s.%s"%sys.version_info[:2])'],env=env,text=True).strip()
    require(version == args.python_version, 'incompatible_runtime_python')
    expected_site=f'python{version}'
    require(expected_site in dependency_site.parts, 'incompatible_dependency_python')
    site=repo/'.venv/lib'/expected_site/'site-packages'
    require(site.is_dir(), 'owned_venv_site_missing')
    own_package=root/'hooks/tools'; own_package.mkdir()
    (own_package/'__init__.py').write_text('')
    shutil.copytree(Path(__file__).parent,own_package/'live_workflow',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    # Dependency .pth files are deliberately not executed.
    _write_owned_startup(site,dependency_site,root/'hooks',root/'hook-config.json')
    hooks={str(path.relative_to(root/'hooks')):sha(path) for path in sorted((root/'hooks').rglob('*.py'))}
    contract={'schema_version':1,'root':str(root),'candidate_commit':args.candidate_commit,
        'original_profile_sha256':args.profile_sha256,'profile_sha256':sha(root/'profile.json'),
        'reference_session':args.session,
        'dependency_site':str(dependency_site),'dependency_sha256':args.dependency_sha256,
        'python_version':version,'owned_runtime':runtime_identity(repo,version),
        'local_git':git_identity,'runtime_policy':{'source_fetch_seconds':900,'model_phase_seconds':2400,'model_call_seconds':120},
        'hooks':hooks,'prepared_at':datetime.now(timezone.utc).isoformat(),
        'auth_path':AUTH_PATH,'auth_policy':'canonical_shared_parent_rw_same_uid',
        'observer':_optional_checkout(args.observer_root,args.observer_commit),
        'native':_optional_checkout(args.native_root,args.native_commit)}
    private_json(root/'contract.json',contract)
    private_json(root/'hook-config.json',{'mode':'live_record','root':str(root),'candidate_repo':str(repo),
        'tape_dir':str(root/'tape'),'local_git':contract['local_git'],'run_identity':{k:contract[k] for k in ('candidate_commit','original_profile_sha256',
            'profile_sha256','dependency_sha256','prepared_at','reference_session')},'phase':'generation_prepare'})
    _child(root,'prepare')
    row=json.loads((root/'generation.json').read_text())
    require(row['git_commit'] == args.candidate_commit and row['config_sha256'] == sha(root/'profile.json'), 'generation_identity')
    verify_contract(root)
    return {'prepared':True,'root':str(root),'generation':row['gen_id'],'deployed':False}


def _optional_checkout(root, commit):
    require((root is None) == (commit is None), 'composition_checkout_requires_exact_commit')
    return None if root is None else {'root':str(validate_candidate(root,commit)), 'commit':commit}


def runtime_identity(repo, version):
    repo=Path(repo); site=repo/'.venv/lib'/f'python{version}'/'site-packages'
    return {'python_target':str((repo/'.venv/bin/python').resolve()),
        'python_sha256':sha((repo/'.venv/bin/python').resolve()),
        'venv_config_sha256':sha(repo/'.venv/pyvenv.cfg'),
        'site_files':{str(p.relative_to(site)):sha(p) for p in sorted(site.rglob('*'))
            if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc'}}


def verify_contract(root):
    root=no_symlinks(root); contract=json.loads((root/'contract.json').read_text())
    require(contract['root'] == str(root), 'campaign_root_mismatch')
    require(runtime_identity(root/'repo',contract['python_version']) == contract['owned_runtime'], 'owned_runtime_changed')
    require(readonly_git_identity(contract['local_git']['sha256']) == contract['local_git'],
        'readonly_git_binary_changed')
    validate_candidate(root/'repo',contract['candidate_commit'])
    require(sha(root/'profile.json') == contract['profile_sha256'], 'profile_hash_changed')
    require(sha(root/'original-profile.json') == contract['original_profile_sha256'], 'original_profile_hash_changed')
    config=json.loads((root/'hook-config.json').read_text())
    identity={k:contract[k] for k in ('candidate_commit','original_profile_sha256','profile_sha256',
        'dependency_sha256','prepared_at','reference_session')}
    require(config == {'mode':'live_record','root':str(root),'candidate_repo':str(root/'repo'),
        'tape_dir':str(root/'tape'),'local_git':contract['local_git'],
        'run_identity':identity,'phase':'generation_prepare'}, 'hook_configuration_changed')
    require(dependency_fingerprint(contract['dependency_site']) == contract['dependency_sha256'], 'dependency_hash_changed')
    actual={str(p.relative_to(root/'hooks')):sha(p) for p in sorted((root/'hooks').rglob('*.py'))}
    require(actual == contract['hooks'], 'recording_hooks_changed')
    row=json.loads((root/'generation.json').read_text())
    manifest=json.loads((root/'repo/data/generations/manifest.json').read_text())
    require(len(manifest['generations']) == 1, 'one_generation_required')
    current=manifest['generations'][0]
    for key in ('gen_id','git_commit','worktree_path','state_dir','config_file','config_sha256','created_at'):
        require(current[key] == row[key], 'generation_identity_changed')
    require(current['status'] == 'active', 'generation_not_active')
    for key in ('state_dir','config_file','worktree_path'):
        path=no_symlinks(current[key]); require(path.is_relative_to(root/'repo'), 'generation_path_escape')
    require(sha(current['config_file']) == contract['profile_sha256'], 'frozen_profile_changed')
    validate_candidate(current['worktree_path'],contract['candidate_commit'])
    return contract,row


def require_boundary(root, receipt_path, *, offline=False):
    path=no_symlinks(receipt_path); value=json.loads(path.read_text())
    require(value.get('schema') == 'live-workflow-external-boundary-v1' and value.get('root') == str(root), 'boundary_identity')
    require(value.get('uid') == os.getuid() and value.get('uid') == 2001, 'boundary_uid')
    required=('private_mounts_verified','production_state_inaccessible','dependencies_readonly',
        'cgroup_limits_enforced','descendant_cleanup_enforced')
    require(all(value.get(key) is True for key in required), 'external_boundary_incomplete')
    if offline:
        require(value.get('kernel_network_denied') is True, 'offline_kernel_network_denial_required')
    else:
        require(value.get('transport_write_guards_verified') is True and
            value.get('canonical_auth_parent_shared_rw') is True and value.get('notifications_denied') is True,
            'live_external_boundary_incomplete')
    return sha(path)


def native(args):
    root=no_symlinks(args.root); contract,row=verify_contract(root)
    boundary=require_boundary(root,args.boundary_receipt)
    require(args.session == contract['reference_session'], 'reference_session_changed')
    # Claim the attempt before any controller/model/source activity. It is never
    # removed after failures and prevents a second same-session native campaign.
    private_json(root/'native-attempt.json',{'claimed_at':datetime.now(timezone.utc).isoformat(),
        'boundary_sha256':boundary,'reference_session':args.session})
    credentials=read_credentials(args.credentials_json)
    require(set(args.credential_env) <= CREDENTIAL_NAMES, 'unapproved_credential_name')
    for name in args.credential_env:
        require(name in os.environ and name not in credentials,'explicit_credential_missing_or_duplicate')
        credentials[name]=os.environ[name]
    sanitized_environment(root,{},credentials)
    # The actual shell dotenv remains empty; credential values are inherited only
    # in the narrow managed execution environment and are never written to tape.
    env=owned_environment(root,credentials)
    repo=root/'repo'
    try:
        _child(root,'window',credentials=credentials)
        window=json.loads((root/'window.json').read_text())
        require(window['session'] == args.session and window['allowed'], 'native_latest_completed_window_required')
        require(window['ordinary_idle_expected'], 'ordinary_idle_observation_requires_non_session_day')
        run_process([str(repo/'.venv/bin/python'),str(repo/'scripts/recover_runtime.py'),'--repo-root',str(repo)],
            root,env=env,label='idle-controller',timeout=120)
        _child(root,'check-idle')
        run_process(prospective_argv(repo,row,args.session),root,env=env,label='native-prospective',timeout=3500)
    finally:
        # Retain report checks even when native infrastructure/source/model fails.
        # No exception text or credential-bearing provider output enters public JSON.
        _child(root,'report',timeout=120,extra=('--session',args.session))
        verify_contract(root)
    return summarize(root)


def _is_database(path):
    with path.open('rb') as stream:
        return stream.read(16) == b'SQLite format 3\0'


def _logical_database(path):
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
        require(db.execute('PRAGMA quick_check').fetchall() == [('ok',)], 'sqlite_integrity')
        schema=db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall()
        tables={}
        for kind,name,_,_ in schema:
            if kind == 'table':
                quoted='"'+name.replace('"','""')+'"'
                rows=sorted(digest([(type(v).__name__,v.hex() if isinstance(v,bytes) else v) for v in row])
                    for row in db.execute('SELECT * FROM '+quoted))
                tables[name]={'rows':len(rows),'sha256':digest(rows)}
        return {'schema':digest(schema),'tables':tables,
            'user_version':db.execute('PRAGMA user_version').fetchone()[0],
            'application_id':db.execute('PRAGMA application_id').fetchone()[0]}


def state_inventory(state):
    state=Path(state).absolute(); result={}
    for path in sorted(state.rglob('*')):
        require(not path.is_symlink(), 'state_symlink')
        if path.is_file() and not path.name.endswith(('-wal','-shm','-journal')):
            result[str(path.relative_to(state))]=_logical_database(path) if _is_database(path) else sha(path)
    return result


def snapshot_state(source,target):
    source=Path(source).absolute(); target=Path(target).absolute(); target.mkdir(mode=0o700)
    for path in sorted(source.rglob('*')):
        require(not path.is_symlink(), 'state_symlink')
        output=target/path.relative_to(source)
        if path.is_dir():
            output.mkdir(mode=0o700,exist_ok=True)
        elif not path.name.endswith(('-wal','-shm','-journal')):
            if _is_database(path):
                with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as src:
                    with sqlite3.connect(output) as dst:
                        src.backup(dst)
            else:
                shutil.copyfile(path,output)
            os.chmod(output,0o600)


def native_inventory(repo, frozen_profile=None):
    """Hash candidate-owned artifacts, including the actual normalized outputs.

    The runner's own receipts and console logs live outside this candidate root.
    No candidate output/log/cache files are exempt from the replay comparison.
    """
    repo=no_symlinks(repo)
    folders={repo/name for name in ('cache','results','data/source_cache',
        'data/dataflows_cache','data/results','data/runtime_recovery','data/logs','docs/reports')}
    if frozen_profile is not None:
        ar=frozen_profile.get('autoresearch',{})
        for raw in (frozen_profile.get('results_dir'),frozen_profile.get('data_cache_dir'),
                    ar.get('results_dir'),ar.get('source_cache_dir')):
            if raw is None:
                continue
            require(isinstance(raw,str) and Path(raw).is_absolute(), 'invalid_native_artifact_path')
            folder=no_symlinks(raw)
            require(folder.is_relative_to(repo),'native_artifact_path_escape')
            folders.add(folder)
    paths={no_symlinks(repo/'data/generations/manifest.json')}
    for folder in sorted(folders):
        no_symlinks(folder)
        for path in folder.rglob('*'):
            require(not path.is_symlink(),'native_artifact_symlink')
            require(path.is_dir() or path.is_file(),'unsupported_native_artifact')
            if path.is_file():
                paths.add(path)
    return {str(path.relative_to(repo)):sha(path) for path in sorted(paths) if path.exists()}


def accepted_replay(args):
    root=no_symlinks(args.root); contract,row=verify_contract(root)
    boundary=require_boundary(root,args.boundary_receipt,offline=True)
    require(args.session == contract['reference_session'], 'reference_session_changed')
    private_json(root/'replay-attempt.json',{'boundary_sha256':boundary,'session':args.session})
    # The worker holds the real candidate runtime lease across snapshot, replay
    # and hash comparison; source/model/SDK/process attempts fail rather than use tape.
    _child(root,'accepted-replay',timeout=120,extra=('--session',args.session))
    verify_contract(root)
    return json.loads((root/'accepted-replay.json').read_text())


def report_checks(report):
    cohorts=report.get('cohorts',{})
    roster=sorted(cohorts) == BOOKS
    return {'sixteen_books':roster,'accounting':roster and report.get('accounting_valid') is True
            and all(v.get('accounting_valid') is True for v in cohorts.values()),
        'staging_complete':roster and report.get('staging_complete') is True
            and all(v.get('staging_complete') is True for v in cohorts.values()),
        'staging_valid':roster and report.get('staging_valid') is True
            and all(v.get('staging_valid') is True for v in cohorts.values()),
        'evidence_complete':report.get('evidence_complete') is True,
        'source_completeness':report.get('input_coverage_valid') is True,
        'outcome':report.get('outcome','incomplete')}


def native_acceptance_passed(result):
    checks=result.get('checks',{})
    reproduction=result.get('canonical_report_reproduction',{})
    unfinished=result.get('recorded_boundary_coverage',{}).get('unfinished_reservations')
    return (checks.get('outcome') == 'clean'
        and all(checks.get(key) is True for key in ('sixteen_books','accounting','staging_complete',
            'staging_valid','evidence_complete','source_completeness'))
        and all(reproduction.get(key) is True for key in ('canonical_json_identical','canonical_markdown_identical'))
        and type(unfinished) is int and unfinished == 0)


def tape_coverage(root):
    from collections import Counter
    from .recorded_io import verify_tape
    records=verify_tape(Path(root)/'tape')
    completed={row['event_id'] for row in records}
    unfinished=[p.stem for p in (Path(root)/'tape/reservations').glob('*.json') if p.stem not in completed]
    sources=Counter(); models=Counter(); phases=Counter(); failures=0
    roles=set()
    for event in records:
        phases[event['phase']]+=1
        failures+=event.get('failure') is not None
        if event['boundary'] == 'model_helper':
            request=event['public_request']; role=request.get('role')
            if role:
                roles.add(role)
            models[(role,request.get('model'),request.get('effort'))]+=1
        else:
            sources[(event['boundary'],event['operation'])]+=1
    return {'verified_events':len(records),'observed_failures':failures,'phase_counts':dict(phases),
        'unfinished_reservations':len(unfinished),
        'source_boundaries':[{'boundary':key[0],'operation':key[1],'calls':count} for key,count in sorted(sources.items())],
        'models':[{'role':key[0],'model':key[1],'effort':key[2],'calls':count}
            for key,count in sorted(models.items(),key=lambda row:repr(row[0]))],
        'unobserved_model_roles':sorted({'bounded','thesis','committee'}-roles),
        'source_completeness_authority':'canonical operational report and accepted source envelopes',
        'cold_acquisition_replay_supported':'recorded boundaries only; not exercised by accepted-state replay'}


def edge_coverage(root):
    """Summarize only a completed matrix still bound to the immutable native tape."""
    root=Path(root)
    path=root/'edge-cases/matrix.json'
    baseline={'status':'not_executed','whole_workflow_coverage':False}
    if not path.exists() and not path.is_symlink():
        return baseline
    try:
        from .edges import plan_edges
        no_symlinks(path)
        require(path.is_file() and path.stat().st_size <= 1024**2,'edge_matrix_size')
        matrix=json.loads(path.read_bytes())
        require(matrix['schema_version'] == 1 and matrix['lane'] == 'controlled_mutation'
            and matrix['whole_workflow_coverage'] is False and matrix['immutable_parent_preserved'] is True,
            'edge_matrix_claims')
        plan=plan_edges(root/'tape')
        identity=json.loads((root/'hook-config.json').read_bytes())['run_identity']
        require(matrix['run_identity'] == plan['run_identity'] == identity
            and matrix['parent_tape_sha256'] == plan['parent_tape_sha256'],'edge_matrix_identity')
        require(re.fullmatch('[a-f0-9]{64}',matrix['boundary_receipt_sha256']) is not None
            and re.fullmatch(r'net:\[[0-9]+\]',matrix['kernel_network_namespace']) is not None,
            'edge_matrix_boundary')
        cases=matrix['cases'];expected={row['case']:row for row in plan['cases']}
        require(type(cases) is list and len(cases) == len(expected)
            and {row['case'] for row in cases} == set(expected),'edge_matrix_roster')
        counts={'pass':0,'fail':0,'unexercised':0};levels={}
        for row in cases:
            planned=expected[row['case']];status=row['status']
            require(status in counts and row['whole_workflow_coverage'] is False
                and row['level'] == planned['level'],'edge_matrix_case_claims')
            require(status == 'unexercised' or planned['status'] == 'planned','edge_matrix_missing_parent')
            for key in ('parent_event_id','parent_record_sha256','parent_kind','phase','occurrence','request_sha256'):
                require(row.get(key) == planned.get(key),'edge_matrix_parent_binding')
            counts[status]+=1
            levels.setdefault(row['level'],{'pass':0,'fail':0,'unexercised':0})[status]+=1
        require(matrix['counts'] == counts and all(type(value) is int for value in matrix['counts'].values()),
            'edge_matrix_counts')
        return {'status':'recorded','lane':'controlled_mutation','whole_workflow_coverage':False,
            'artifact':'edge-cases/matrix.json','artifact_sha256':sha(path),'counts':counts,
            'level_counts':dict(sorted(levels.items())),
            'unexercised_cases':[{key:row[key] for key in ('case','level','reason') if key in row}
                for row in cases if row['status'] == 'unexercised'],
            'failed_cases':[{key:row[key] for key in ('case','level','reason') if key in row}
                for row in cases if row['status'] == 'fail']}
    except Exception:
        # Untrusted/corrupt artifact text never replaces the visible coverage gap
        # or enters a public exception message.
        return {'status':'invalid','reason':'edge_matrix_invalid_or_stale','whole_workflow_coverage':False}


def summarize(root):
    root=Path(root)
    report=json.loads((root/'native-report.json').read_text()) if (root/'native-report.json').exists() else {}
    reproduction=json.loads((root/'report-reproduction.json').read_text()) if (root/'report-reproduction.json').exists() else {}
    coverage=tape_coverage(root)
    edges=edge_coverage(root)
    gaps=['observer_sidecars_and_native_full_poll_require_external_composition_receipt',
        'cold_acquisition_replay_not_executed','subsequent_session_fills_unobserved','timer_dispatch_unobserved']
    if edges['status'] != 'recorded' or not (edges['counts']['pass']+edges['counts']['fail']):
        gaps.append('controlled_mutations_not_executed')
    if edges['status'] == 'invalid':
        gaps.append('controlled_mutations_artifact_invalid')
    elif edges['status'] == 'recorded':
        if edges['counts']['unexercised']:
            gaps.append('controlled_mutations_unexercised')
        if edges['counts']['fail']:
            gaps.append('controlled_mutations_failed')
    return {'deployed':False,'checks':report_checks(report),'canonical_report_reproduction':reproduction,
        'recorded_boundary_coverage':coverage,'controlled_mutations':edges,'coverage_gaps':gaps,
        'model_coverage':'inspect private tape by exact role/model/effort; absent roles remain unobserved',
        'native_lane':'real_clock_guarded_prospective_one_shot','auth_writes':'normal canonical shared refresh permitted'}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='operation',required=True)
    prep=sub.add_parser('prepare')
    for key in ('candidate-root','candidate-commit','profile-sha256','dependency-site','dependency-sha256',
            'git-sha256','destination','session'):
        prep.add_argument('--'+key,required=True)
    prep.add_argument('--python',default=str(Path(sys.executable).resolve()))
    prep.add_argument('--python-version',default='3.12')
    prep.add_argument('--profile-file',help='Explicit frozen profile; must match --profile-sha256. Defaults to candidate stabilization-v3.json.')
    for key in ('observer-root','observer-commit','native-root','native-commit'):
        prep.add_argument('--'+key)
    run=sub.add_parser('native'); replay=sub.add_parser('accepted-replay')
    for entry in (run,replay):
        entry.add_argument('--root',required=True); entry.add_argument('--session',required=True)
        entry.add_argument('--boundary-receipt',required=True)
    edges=sub.add_parser('edges')
    edges.add_argument('--root',required=True);edges.add_argument('--boundary-receipt',required=True)
    run.add_argument('--credentials-json')
    run.add_argument('--credential-env',action='append',default=[],choices=sorted(CREDENTIAL_NAMES),
        help='Read only this explicitly selected source credential from the supervisor environment; repeatable')
    summary=sub.add_parser('summary'); summary.add_argument('--root',required=True)
    fingerprint=sub.add_parser('fingerprint'); fingerprint.add_argument('--dependency-site',required=True)
    args=parser.parse_args(argv)
    if args.operation == 'prepare': result=prepare(args)
    elif args.operation == 'native': result=native(args)
    elif args.operation == 'accepted-replay': result=accepted_replay(args)
    elif args.operation == 'edges':
        from .edges import run_edges
        result=run_edges(root=args.root,boundary_receipt=args.boundary_receipt)
    elif args.operation == 'fingerprint': result={'dependency_sha256':dependency_fingerprint(args.dependency_site)}
    else: result=summarize(args.root)
    print(json.dumps(result,sort_keys=True,allow_nan=False))
    if args.operation == 'native':
        return int(not native_acceptance_passed(result))
    return int(result['counts']['fail'] > 0) if args.operation == 'edges' else 0
