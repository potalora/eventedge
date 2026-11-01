"""Runner boundary tests: isolated dummy repositories; never credentials/providers."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from tools.live_workflow.runner import (
    AcceptanceError, BOOKS, clone_candidate, dependency_fingerprint,
    normalize_profile, sanitized_environment, validate_candidate,
    snapshot_state, state_inventory, report_checks, prospective_argv,
)


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


def _test_git_sha256():
    # Unit fixtures explicitly provide a pin; production must get it from the
    # supervisor's host-root ownership check outside the payload namespace.
    return hashlib.sha256(Path('/usr/bin/git').read_bytes()).hexdigest()


def _test_uname_identity():
    return {'path':'/usr/bin/uname','sha256':hashlib.sha256(Path('/usr/bin/uname').read_bytes()).hexdigest()}


@pytest.fixture
def admitted_uname(monkeypatch):
    """Delegation/order tests do not admit the hosted runner's binary layout."""
    from tools.live_workflow import runner
    identity=_test_uname_identity()
    def verified(expected):
        if expected!=identity['sha256']:
            raise AcceptanceError('readonly_uname_binary_hash_mismatch')
        return dict(identity)
    monkeypatch.setattr(runner,'readonly_uname_identity',verified)
    return identity


@pytest.fixture
def candidate(tmp_path):
    root = tmp_path/'candidate'; root.mkdir()
    git(root, 'init', '-q')
    (root/'dummy.txt').write_text('real tracked bytes\n')
    git(root, 'add', '.')
    git(root, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'fixture')
    return root, git(root, 'rev-parse', 'HEAD')


def profile():
    return {'project_dir':'/original', 'results_dir':'/original/results',
        'data_cache_dir':'/original/data_cache',
        'autoresearch':{'results_dir':'/original/results', 'source_cache_dir':'/original/cache',
          'chatgpt_plan_auth_path':'/home/hermes/.config/eventedge/plan-auth.json',
          'llm_billing_mode':'chatgpt_plan', 'stabilization_profile_policy':'supported_sixteen_books_v1'},
        'execution':{'mode':'paper','execution_enabled':False, 'broker':'alpaca'},
        'alerts':{'enabled':False,'channels':[]}, 'decision_shadow':{'enabled':False}}


def test_candidate_requires_exact_full_commit_and_clean_tree(candidate):
    root, commit = candidate
    validate_candidate(root, commit)
    with pytest.raises(AcceptanceError, match='commit'):
        validate_candidate(root, commit[:8])
    (root/'dummy.txt').write_text('modified')
    with pytest.raises(AcceptanceError, match='dirty'):
        validate_candidate(root, commit)


def test_untracked_application_and_symlink_candidates_refused(candidate, tmp_path):
    root, commit = candidate
    (root/'extra.py').write_text('')
    with pytest.raises(AcceptanceError, match='dirty'):
        validate_candidate(root, commit)
    (root/'extra.py').unlink()
    link = tmp_path/'linked'; link.symlink_to(root, target_is_directory=True)
    with pytest.raises(AcceptanceError, match='symlink'):
        validate_candidate(link, commit)


def test_clone_has_no_origin_alternates_or_shared_git_objects(candidate, tmp_path):
    root, commit = candidate
    destination = tmp_path/'clone'
    clone_candidate(root, commit, destination)
    assert git(destination, 'rev-parse', 'HEAD') == commit
    assert git(destination, 'remote') == ''
    assert not (destination/'.git/objects/info/alternates').exists()
    for path in (destination/'.git/objects').rglob('*'):
        if path.is_file():
            assert path.stat().st_nlink == 1
    assert git(destination, 'config', 'core.hooksPath') == '/dev/null'
    with pytest.raises(AcceptanceError, match='exists'):
        clone_candidate(root, commit, destination)


def test_profile_only_allowed_paths_change(tmp_path):
    original = profile(); frozen, audit = normalize_profile(original, tmp_path/'repo')
    assert original == profile()
    assert frozen['autoresearch']['chatgpt_plan_auth_path'] == original['autoresearch']['chatgpt_plan_auth_path']
    assert len(audit) == 5
    for row in audit:
        assert Path(row['after']).is_relative_to(tmp_path/'repo')
    for key in ('execution','alerts','decision_shadow'):
        assert frozen[key] == original[key]


def test_explicit_frozen_profile_preserves_deployed_contact_and_exact_bytes(tmp_path):
    from tools.live_workflow.runner import load_profile_bytes
    source=tmp_path/'source'; template=source/'config/profiles/stabilization-v3.json'
    template.parent.mkdir(parents=True)
    value=profile(); value['autoresearch']['edgar_user_agent']='Research template@example.invalid'
    template.write_text(json.dumps(value))
    value['autoresearch']['edgar_user_agent']='Research deployed@example.invalid'
    selected=tmp_path/'selected.json'; selected.write_text(json.dumps(value,indent=2)+'\n')
    body=load_profile_bytes(source,selected,hashlib.sha256(selected.read_bytes()).hexdigest())
    frozen,_=normalize_profile(json.loads(body),tmp_path/'repo')
    assert body == selected.read_bytes()
    assert frozen['autoresearch']['edgar_user_agent'] == value['autoresearch']['edgar_user_agent']
    assert load_profile_bytes(source,None,hashlib.sha256(template.read_bytes()).hexdigest()) == template.read_bytes()
    with pytest.raises(AcceptanceError,match='profile_hash'):
        load_profile_bytes(source,selected,hashlib.sha256(template.read_bytes()).hexdigest())
    link=tmp_path/'linked.json'; link.symlink_to(selected)
    with pytest.raises(AcceptanceError,match='symlink'):
        load_profile_bytes(source,link,hashlib.sha256(selected.read_bytes()).hexdigest())


@pytest.mark.parametrize('change', [
    lambda p: p['execution'].update(mode='live'),
    lambda p: p['execution'].update(execution_enabled=True),
    lambda p: p['alerts'].update(enabled=True),
    lambda p: p['alerts'].update(channels=['telegram']),
    lambda p: p['decision_shadow'].update(enabled=True),
    lambda p: p['autoresearch'].update(chatgpt_plan_auth_path='/tmp/copied-auth.json'),
    lambda p: p.update(unexpected_output_dir='/host/data'),
])
def test_unsafe_profiles_refused(tmp_path, change):
    value = deepcopy(profile()); change(value)
    with pytest.raises(AcceptanceError):
        normalize_profile(value, tmp_path/'repo')


def test_environment_drops_ambient_overrides_secrets_and_paid_routes(tmp_path):
    ambient = {'OPENAI_API_KEY':'paid', 'TELEGRAM_BOT_TOKEN':'notify',
      'BLOCKED_TICKERS':'AAPL', 'PYTHONPATH':'/host/app', 'LD_PRELOAD':'/host/x.so',
      'FRED_API_KEY':'not-explicitly-approved', 'HOME':'/host', 'EVENTEDGE_ACCEPTANCE_SCENARIO':'fake'}
    result = sanitized_environment(tmp_path, ambient, {'FRED_API_KEY':'explicit'})
    assert result['FRED_API_KEY'] == 'explicit'
    assert all(key not in result for key in ambient if key not in ('HOME','FRED_API_KEY'))
    assert result['HOME'] == str(tmp_path/'home')
    with pytest.raises(AcceptanceError, match='credential'):
        sanitized_environment(tmp_path, {}, {'OPENAI_API_KEY':'paid'})
    with pytest.raises(AcceptanceError, match='credential'):
        sanitized_environment(tmp_path, {}, {'FRED_API_KEY':'x\nEVIL=y'})


def test_dependency_identity_binds_bytes_and_excludes_synthetic_hooks(tmp_path):
    site = tmp_path/'site'; site.mkdir(); (site/'package.py').write_text('original')
    initial = dependency_fingerprint(site)
    (site/'package.py').write_text('changed')
    assert dependency_fingerprint(site) != initial
    (site/'acceptance.pth').write_text('import scheduled_acceptance.bootstrap')
    with pytest.raises(AcceptanceError, match='synthetic'):
        dependency_fingerprint(site)


def test_snapshot_uses_sqlite_backup_including_wal_and_all_tables(tmp_path):
    state = tmp_path/'state'; state.mkdir()
    db = sqlite3.connect(state/'ledger.sqlite')
    db.execute('PRAGMA journal_mode=WAL'); db.execute('CREATE TABLE financial(a,b)')
    db.execute('INSERT INTO financial VALUES (?,?)', (1,b'\x00\x01')); db.commit()
    (state/'source_inputs').mkdir(); (state/'source_inputs/input.json').write_text('{}')
    before = state_inventory(state)
    snapshot_state(state, tmp_path/'copy')
    assert state_inventory(tmp_path/'copy') == before
    db.execute('INSERT INTO financial VALUES (2,3)'); db.commit()
    assert state_inventory(state) != before
    db.close()


def test_report_requires_all_sixteen_and_separate_coverage():
    report = {'cohorts':{book:{'accounting_valid':True,'staging_complete':True,'staging_valid':True} for book in BOOKS},
      'accounting_valid':True,'staging_complete':True,'staging_valid':True,
      'evidence_complete':True,'input_coverage_valid':False,'outcome':'degraded'}
    checks = report_checks(report)
    assert checks['sixteen_books'] and checks['accounting'] and not checks['source_completeness']
    report['cohorts'].pop(BOOKS[0])
    assert report_checks(report)['sixteen_books'] is False


def test_prospective_command_uses_all_native_pins(tmp_path):
    row = dict(gen_id='gen_001', git_commit='a'*40, config_sha256='b'*64, created_at='2026-10-31T12:00:00')
    argv = prospective_argv(tmp_path/'repo', row, '2026-10-30')
    assert argv[-10:] == ['--prospective-session','2026-10-30','--expected-generation','gen_001',
      '--expected-commit','a'*40,'--expected-profile-sha256','b'*64,'--expected-created-at',row['created_at']]


def test_phase_identifies_controller_and_each_native_preflight():
    from tools.live_workflow.bootstrap import phase_for
    assert phase_for(['run_generations.py','preflight','--preflight-mode','screen'],'base') == 'preflight_screen'
    assert phase_for(['run_cohorts.py','--preflight','--preflight-mode','governed'],'base') == 'preflight_governed'
    assert phase_for(['run_cohorts.py','--date','2026-10-30'],'base') == 'daily'
    assert phase_for(['transport_worker.py'],'daily') == 'daily'


def test_boundary_requires_external_enforcement_not_just_python_flags(tmp_path, monkeypatch):
    from tools.live_workflow.runner import require_boundary
    monkeypatch.setattr('os.getuid',lambda:2001)
    path=tmp_path/'boundary.json'
    value={'schema':'live-workflow-external-boundary-v1','root':str(tmp_path),'uid':2001,
      'private_mounts_verified':True,'production_state_inaccessible':True,'dependencies_readonly':True,
      'cgroup_limits_enforced':True,'descendant_cleanup_enforced':True,
      'transport_write_guards_verified':True,'canonical_auth_parent_shared_rw':True,'notifications_denied':True}
    path.write_text(json.dumps(value))
    require_boundary(tmp_path,path)
    with pytest.raises(AcceptanceError,match='kernel_network'):
        require_boundary(tmp_path,path,offline=True)
    value['kernel_network_denied']=True; path.write_text(json.dumps(value))
    require_boundary(tmp_path,path,offline=True)
    value['production_state_inaccessible']=False; path.write_text(json.dumps(value))
    with pytest.raises(AcceptanceError,match='incomplete'):
        require_boundary(tmp_path,path)


def test_credentials_file_is_private_and_never_shell_evaluated(tmp_path):
    from tools.live_workflow.runner import read_credentials
    path=tmp_path/'credentials.json'; path.write_text(json.dumps({'FRED_API_KEY':'$(never-run)'}))
    path.chmod(0o600)
    assert read_credentials(path) == {'FRED_API_KEY':'$(never-run)'}
    path.chmod(0o644)
    with pytest.raises(AcceptanceError,match='permissions'):
        read_credentials(path)
    path.chmod(0o600); path.write_text(json.dumps({'TELEGRAM_BOT_TOKEN':'disallowed'}))
    with pytest.raises(AcceptanceError,match='credential'):
        read_credentials(path)


def test_owned_startup_imports_candidate_and_does_not_load_dependency_pth(tmp_path):
    """A controlled subprocess exercises interpreter startup without provider hooks."""
    from tools.live_workflow.runner import sanitized_environment,readonly_git_identity
    repo=tmp_path/'repo'; repo.mkdir(); (repo/'tradingagents').mkdir()
    (repo/'tradingagents/__init__.py').write_text('IDENTITY="dummy-candidate"\n')
    for folder in ('home','cache','tmp'):
        (tmp_path/folder).mkdir()
    # A fresh interpreter has no preinstalled acceptance/time/SDK hook.
    env=sanitized_environment(tmp_path)
    subprocess.run([sys.executable,'-I','-m','venv','--without-pip',str(repo/'.venv')],env=env,check=True,capture_output=True)
    python=repo/'.venv/bin/python'
    version=f'python{sys.version_info.major}.{sys.version_info.minor}'
    site=repo/'.venv/lib'/version/'site-packages'
    dep=tmp_path/'dependency-site'; dep.mkdir()
    (dep/'forbidden.pth').write_text('import os; os._exit(99)\n')
    # Minimal import-only dependencies for this controlled no-IO startup test.
    (dep/'requests.py').write_text('class Session:\n def request(self): pass\n')
    (dep/'httpx.py').write_text('class Client:\n def send(self): pass\nclass AsyncClient:\n def send(self): pass\n')
    strategies=repo/'tradingagents/strategies'; strategies.mkdir(); (strategies/'__init__.py').write_text('')
    (strategies/'llm_utils.py').write_text('def call_analysis_model(): pass\n')
    (strategies/'runtime_deadline.py').write_text('def bounded_transport(): pass\n')
    hook_root=Path(__file__).resolve().parents[1]
    code="from tools.live_workflow.bootstrap import install; install()"
    (site/'owned.pth').write_text(str(dep)+'\n'+str(hook_root)+'\n'+'import os; exec('+repr(code)+')\n')
    config=tmp_path/'hook.json'; config.write_text(json.dumps({'candidate_repo':str(repo),'phase':'test',
        'local_git':readonly_git_identity(_test_git_sha256()),'dependency_site':None,'local_uname':None}))
    env['EVENTEDGE_LIVE_CONFIG']=str(config)
    git(repo,'init','-q')
    # accepted-replay selects explicit no-IO installation rather than live hooks.
    payload='''import json,runpy,subprocess,sys,tradingagents
print(tradingagents.IDENTITY);print(tradingagents.__file__)
state=sys._eventedge_replay_process_guard
other=runpy.run_module('tools.live_workflow.bootstrap',run_name='isolated_replay_bootstrap')
other['_forbid_io'](state['attempts'],repo=state['repo'],git_identity=state['identity'])
command=['git','-C',state['repo'],'rev-parse','--path-format=absolute','--git-common-dir']
result=subprocess.run(command,capture_output=True,text=True,check=True)
try:subprocess.Popen(['git','status'])
except RuntimeError:pass
print(json.dumps({'common_dir':result.stdout.strip(),'allowed':dict(state['allowed']),'attempts':dict(state['attempts'])}))
'''
    proc=subprocess.run([str(python),'-c',payload,
        'accepted-replay'],env=env,cwd=repo,capture_output=True,text=True)
    assert proc.returncode == 0,proc.stderr
    lines=proc.stdout.splitlines()
    assert lines[:2] == ['dummy-candidate',str(repo/'tradingagents/__init__.py')]
    assert json.loads(lines[2])=={'common_dir':str(repo/'.git'),
        'allowed':{'canonical_git_common_dir':1},'attempts':{'process':1}}


def test_no_io_guard_counts_forbidden_requests_without_network(monkeypatch,tmp_path):
    from collections import Counter
    from types import ModuleType,SimpleNamespace
    from tools.live_workflow.bootstrap import _forbid_io
    from tools.live_workflow.runner import readonly_git_identity
    # Supply a dummy candidate runtime module, never import any real application.
    strategies=ModuleType('tradingagents.strategies')
    strategies.llm_utils=SimpleNamespace(call_analysis_model=lambda:None)
    strategies.runtime_deadline=SimpleNamespace(bounded_transport=lambda:None)
    monkeypatch.setitem(sys.modules,'tradingagents',ModuleType('tradingagents'))
    monkeypatch.setitem(sys.modules,'tradingagents.strategies',strategies)
    import requests,httpx,urllib.request,socket
    originals=[(socket,'getaddrinfo'),(socket,'gethostbyname'),(socket,'create_connection'),
      (socket.socket,'connect'),(socket.socket,'connect_ex'),(requests.Session,'request'),
      (httpx.Client,'send'),(httpx.AsyncClient,'send'),(urllib.request,'urlopen'),(subprocess,'Popen')]
    # Guard patches live only in this test and are restored by monkeypatch.
    for owner,name in originals:
        monkeypatch.setattr(owner,name,getattr(owner,name))
    for package in ('httpx2','curl_cffi'):
        monkeypatch.setitem(sys.modules,package,None)
    monkeypatch.delattr(sys,'_eventedge_replay_process_guard',raising=False)
    attempts=Counter(); _forbid_io(attempts,repo=tmp_path,git_identity=readonly_git_identity(_test_git_sha256()))
    with pytest.raises(RuntimeError,match='network'):
        socket.create_connection(('invalid.example',443))
    assert attempts == {'network.create_connection':1}
    with pytest.raises(RuntimeError,match='model.analysis'):
        strategies.llm_utils.call_analysis_model()
    assert attempts['model.analysis'] == 1


def test_dependency_directory_symlink_and_sitecustomize_are_rejected(tmp_path):
    site=tmp_path/'site'; site.mkdir(); outside=tmp_path/'outside'; outside.mkdir()
    (outside/'module.py').write_text('unexpected')
    (site/'escape').symlink_to(outside,target_is_directory=True)
    with pytest.raises(AcceptanceError,match='symlink_escape'):
        dependency_fingerprint(site)
    (site/'escape').unlink(); (site/'sitecustomize.py').write_text('raise RuntimeError()')
    with pytest.raises(AcceptanceError,match='sitecustomize'):
        dependency_fingerprint(site)


def test_ignored_extra_application_code_is_still_dirty(candidate):
    root,commit=candidate
    (root/'.git/info/exclude').write_text('tradingagents/hidden.py\n')
    (root/'tradingagents').mkdir(); (root/'tradingagents/hidden.py').write_text('injected')
    with pytest.raises(AcceptanceError,match='ignored_application_code'):
        validate_candidate(root,commit)


def test_prepare_refuses_incompatible_python_before_any_application_call(candidate,tmp_path,monkeypatch,admitted_uname):
    from argparse import Namespace
    from tools.live_workflow.runner import prepare
    root,_=candidate
    config=root/'config/profiles'; config.mkdir(parents=True)
    profile_path=config/'stabilization-v3.json'; profile_path.write_text(json.dumps(profile()))
    git(root,'add','.'); git(root,'-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm','profile')
    commit=git(root,'rev-parse','HEAD')
    site=tmp_path/'deps/lib/python9.9/site-packages'; site.mkdir(parents=True)
    args=Namespace(candidate_root=root,candidate_commit=commit,session='2026-10-30',destination=tmp_path/'campaign',
        profile_sha256=hashlib.sha256(profile_path.read_bytes()).hexdigest(),dependency_site=site,
        dependency_sha256=dependency_fingerprint(site),git_sha256=_test_git_sha256(),
        uname_sha256=_test_uname_identity()['sha256'],
        python=str(Path(sys.executable).resolve()),python_version='9.9',
        observer_root=None,observer_commit=None,native_root=None,native_commit=None)
    called=[]; monkeypatch.setattr('tools.live_workflow.runner._child',lambda *a,**kw:called.append(a))
    with pytest.raises(AcceptanceError,match='incompatible_runtime_python'):
        prepare(args)
    assert called == []


def test_prepare_allows_only_its_native_runtime_lock_among_new_operational_files(candidate,tmp_path,admitted_uname):
    from argparse import Namespace
    from tools.live_workflow.runner import prepare
    source,_=candidate
    (source/'.gitignore').write_text('.venv/\n.env\n')
    config=source/'config/profiles';config.mkdir(parents=True)
    profile_path=config/'stabilization-v3.json';profile_path.write_text(json.dumps(profile()))
    git(source,'add','.')
    git(source,'-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm','profile')
    commit=git(source,'rev-parse','HEAD')
    site=tmp_path/'deps/lib/python9.9/site-packages';site.mkdir(parents=True)
    root=tmp_path/'campaign'
    args=Namespace(candidate_root=source,candidate_commit=commit,session='2026-10-30',destination=root,
        profile_sha256=hashlib.sha256(profile_path.read_bytes()).hexdigest(),dependency_site=site,
        dependency_sha256=dependency_fingerprint(site),git_sha256=_test_git_sha256(),
        uname_sha256=_test_uname_identity()['sha256'],
        python=str(Path(sys.executable).resolve()),python_version='9.9',
        observer_root=None,observer_commit=None,native_root=None,native_commit=None)
    # Stop before any application import; prepare has established clone policy.
    with pytest.raises(AcceptanceError,match='incompatible_runtime_python'):
        prepare(args)
    repo=root/'repo';operational=repo/'data/operational';operational.mkdir(parents=True)
    (operational/'eventedge-runtime.lock').write_text('')
    validate_candidate(repo,commit)
    foreign=operational/'unexpected.json';foreign.write_text('{}')
    with pytest.raises(AcceptanceError,match='dirty_candidate'):
        validate_candidate(repo,commit)
    foreign.unlink()
    app=repo/'tradingagents';app.mkdir();(app/'unexpected.py').write_text('unexpected=True')
    with (repo/'.git/info/exclude').open('a') as stream:
        stream.write('/tradingagents/unexpected.py\n')
    with pytest.raises(AcceptanceError,match='dirty_ignored_application_code'):
        validate_candidate(repo,commit)


def test_generation_prepare_calls_candidate_lifecycle_not_handwritten_manifest(tmp_path,monkeypatch):
    from dataclasses import dataclass
    from types import ModuleType
    from tools.live_workflow.bootstrap import _prepare
    @dataclass
    class Row:
        gen_id:str='gen_001'
        created_at:str='native-created-at'
    calls=[]
    class Manager:
        def __init__(self,root): calls.append(('root',root))
        def start_generation(self,description,*,config_file):
            calls.append(('start',description,str(config_file))); return Row()
    modules={
      'tradingagents.strategies.orchestration.generation_manager':{'GenerationManager':Manager},
      'tradingagents.strategies.orchestration.multi_strategy_engine':{'_fetch_timeout_s':lambda:900},
      'tradingagents.strategies.runtime_deadline':{'DEFAULT_MODEL_BUDGET_S':2400,'MODEL_CALL_CAP_S':120}}
    for name,values in modules.items():
        module=ModuleType(name)
        for key,value in values.items(): setattr(module,key,value)
        monkeypatch.setitem(sys.modules,name,module)
    monkeypatch.setattr('tools.live_workflow.bootstrap._imports',lambda repo:None)
    _prepare(tmp_path)
    assert calls[0] == ('root',str(tmp_path/'repo'))
    assert calls[1][0] == 'start' and calls[1][2] == str(tmp_path/'profile.json')
    assert json.loads((tmp_path/'generation.json').read_text()) == {'gen_id':'gen_001','created_at':'native-created-at'}
    assert not (tmp_path/'repo/data/generations/manifest.json').exists()


def test_source_credential_redaction_handles_chunk_boundary_and_url_encoding():
    from io import BytesIO
    from tools.live_workflow.runner import _copy_redacted
    # Deliberately split a secret across the recorder's read boundary.
    value='dummy+/source-key'
    payload=b'x'*(8192-5)+value.encode()+b' url=dummy%2B%2Fsource-key\n'
    output=BytesIO(); _copy_redacted(BytesIO(payload),output,{'FRED_API_KEY':value})
    assert value.encode() not in output.getvalue()
    assert b'dummy%2B%2Fsource-key' not in output.getvalue()
    assert output.getvalue().count(b'[REDACTED]') == 2


def test_logging_formatter_redacts_explicit_source_credentials(monkeypatch):
    import logging
    from tools.live_workflow.bootstrap import _install_log_redaction
    original=logging.Formatter.format
    monkeypatch.setattr(logging.Formatter,'format',original)
    monkeypatch.setenv('FRED_API_KEY','dummy-source-key')
    _install_log_redaction()
    record=logging.LogRecord('provider',logging.ERROR,'dummy',1,'failed %s',('dummy-source-key',),None)
    assert logging.Formatter().format(record) == 'failed [REDACTED]'


def test_process_console_is_sanitized_before_retention(tmp_path):
    from tools.live_workflow.runner import run_process,sanitized_environment
    (tmp_path/'repo').mkdir(); (tmp_path/'logs').mkdir()
    env=sanitized_environment(tmp_path,{}, {'FRED_API_KEY':'dummy-secret-for-subprocess'})
    result=run_process([sys.executable,'-I','-c','import os; print(os.environ["FRED_API_KEY"])'],
        tmp_path,env=env,label='controlled',timeout=5)
    assert (tmp_path/'logs/controlled.log').read_text() == '[REDACTED]\n'
    assert result['console_representation'] == 'explicit_source_credential_redacted_bytes'
    assert 'dummy-secret-for-subprocess' not in (tmp_path/'controlled-process.json').read_text()


@pytest.mark.parametrize('folder',[
    'data/source_cache','data/dataflows_cache','data/results',
    'data/runtime_recovery','data/logs/run_attempts','data/logs','docs/reports',
])
def test_native_inventory_detects_mutations_in_actual_native_paths(tmp_path,folder):
    from tools.live_workflow.runner import native_inventory
    repo=tmp_path/'repo'; target=repo/folder/'evidence.json'
    target.parent.mkdir(parents=True); target.write_text('original')
    before=native_inventory(repo)
    target.write_text('changed')
    assert native_inventory(repo) != before
    target.write_text('original'); assert native_inventory(repo) == before
    extra=target.with_name('added.json'); extra.write_text('additional')
    assert native_inventory(repo) != before
    extra.unlink(); target.unlink(); assert native_inventory(repo) != before


def test_native_inventory_uses_frozen_profile_output_paths(tmp_path):
    from tools.live_workflow.runner import native_inventory
    repo=tmp_path/'repo'; repo.mkdir()
    frozen={'results_dir':str(repo/'custom/results'), 'data_cache_dir':str(repo/'custom/data-cache'),
      'autoresearch':{'results_dir':str(repo/'custom/research-results'),
      'source_cache_dir':str(repo/'custom/source-cache')}}
    before=native_inventory(repo,frozen)
    for path in (frozen['results_dir'],frozen['data_cache_dir'],*frozen['autoresearch'].values()):
        target=Path(path)/'accepted.json'; target.parent.mkdir(parents=True); target.write_text('{}')
        after=native_inventory(repo,frozen)
        assert after != before
        before=after


def test_native_inventory_rejects_escaped_configured_paths_and_directory_symlinks(tmp_path):
    from tools.live_workflow.runner import native_inventory
    repo=tmp_path/'repo'; repo.mkdir(); outside=tmp_path/'outside'; outside.mkdir()
    with pytest.raises(AcceptanceError,match='native_artifact_path_escape'):
        native_inventory(repo,{'results_dir':str(outside)})
    (repo/'data').mkdir(); (repo/'data/source_cache').symlink_to(outside,target_is_directory=True)
    with pytest.raises(AcceptanceError,match='symlink'):
        native_inventory(repo)


def edge_summary_fixture(root):
    """Constructed private artifacts; never native campaign evidence."""
    import importlib
    from tools.live_workflow import recorded_io as tape
    from tools.live_workflow.edges import plan_edges
    importlib.reload(tape)
    identity={'candidate_commit':'a'*40,'reference_session':'2026-10-30'}
    tape.configure_tape(mode='live_record',tape_dir=root/'tape',run_identity=identity,phase='daily')
    event=tape.reserve_event(boundary='http_decoded',operation='requests.send',public_request={
        'method':'GET','url':'https://example.invalid/data','body':None,'headers':{}})
    tape.finish_event(event,status=200)
    (root/'hook-config.json').write_text(json.dumps({'run_identity':identity}))
    plan=plan_edges(root/'tape')
    cases=[]
    for row in plan['cases']:
        row=dict(row,status='unexercised',reason='constructed_unexercised')
        if row['case']=='source_403':
            row.update(status='pass',observed={'exception_type':'HTTPError','http_status':403})
        elif row['case']=='source_429':
            row.update(status='fail',observed={'returned':True})
        cases.append(row)
    matrix={**plan,'cases':cases,'immutable_parent_preserved':True,
        'boundary_receipt_sha256':'b'*64,'kernel_network_namespace':'net:[12345]',
        'edge_tooling_sha256':{'edges.py':'c'*64},
        'counts':{'pass':1,'fail':1,'unexercised':15}}
    (root/'edge-cases').mkdir()
    return matrix


def test_summary_keeps_mutation_gap_without_completed_matrix(tmp_path):
    from tools.live_workflow.runner import summarize
    matrix=edge_summary_fixture(tmp_path)
    (tmp_path/'edge-cases/plan.json').write_text(json.dumps(matrix))
    result=summarize(tmp_path)
    assert 'controlled_mutations_not_executed' in result['diagnostic_coverage_gaps']
    assert result['controlled_mutations']['status']=='not_executed'


def test_summary_reports_bound_matrix_counts_levels_and_remaining_gaps(tmp_path):
    from tools.live_workflow.runner import summarize
    matrix=edge_summary_fixture(tmp_path)
    (tmp_path/'edge-cases/matrix.json').write_text(json.dumps(matrix))
    result=summarize(tmp_path)
    observed=result['controlled_mutations']
    assert observed['status']=='recorded' and observed['whole_workflow_coverage'] is False
    assert observed['counts']=={'pass':1,'fail':1,'unexercised':15}
    assert observed['level_counts']['http_transport']=={'pass':1,'fail':1,'unexercised':1}
    assert len(observed['unexercised_cases'])==15
    assert observed['failed_cases'][0]['case']=='source_429'
    assert 'controlled_mutations_not_executed' not in result['diagnostic_coverage_gaps']
    assert 'controlled_mutations_unexercised' in result['diagnostic_coverage_gaps']
    assert 'controlled_mutations_failed' in result['diagnostic_coverage_gaps']
    assert 'cold_acquisition_replay_not_executed' in result['diagnostic_coverage_gaps']
    assert not any(gap.startswith(('cold_','controlled_mutations_','observed_clock_')) for gap in result['coverage_gaps'])
    assert result['acceptance_evidence_policy']=='live_sources_real_data_live_models_only'
    assert result['offline_checks_establish_live_readiness'] is False


@pytest.mark.parametrize('corrupt',[
    lambda m:m.update(lane='native_observation'),
    lambda m:m.update(whole_workflow_coverage=True),
    lambda m:m.update(immutable_parent_preserved=False),
    lambda m:m['run_identity'].update(candidate_commit='d'*40),
    lambda m:m.update(parent_tape_sha256='d'*64),
    lambda m:m['counts'].update(**{'pass':17}),
    lambda m:m['cases'][0].update(status='planned'),
    lambda m:m['cases'][1].update(phase='wrong-phase'),
    lambda m:m['cases'][1].update(level='native_finra_adapter'),
    lambda m:m['cases'].pop(),
])
def test_summary_rejects_stale_or_overclaimed_edge_matrix(tmp_path,corrupt):
    from tools.live_workflow.runner import summarize
    matrix=edge_summary_fixture(tmp_path);corrupt(matrix)
    (tmp_path/'edge-cases/matrix.json').write_text(json.dumps(matrix))
    result=summarize(tmp_path)
    assert result['controlled_mutations']['status']=='invalid'
    assert 'controlled_mutations_not_executed' in result['diagnostic_coverage_gaps']
    assert 'controlled_mutations_artifact_invalid' in result['diagnostic_coverage_gaps']


def test_summary_all_unexercised_matrix_does_not_claim_mutation_execution(tmp_path):
    from tools.live_workflow.runner import summarize
    matrix=edge_summary_fixture(tmp_path)
    for row in matrix['cases']:
        row.update(status='unexercised',reason='missing_eligible_native_parent_capture')
    matrix['counts']={'pass':0,'fail':0,'unexercised':17}
    (tmp_path/'edge-cases/matrix.json').write_text(json.dumps(matrix))
    result=summarize(tmp_path)
    assert result['controlled_mutations']['status']=='recorded'
    assert result['controlled_mutations']['counts']=={'pass':0,'fail':0,'unexercised':17}
    assert 'controlled_mutations_not_executed' in result['diagnostic_coverage_gaps']


def test_summary_invalid_json_matrix_remains_visible_failure(tmp_path):
    from tools.live_workflow.runner import summarize
    edge_summary_fixture(tmp_path)
    (tmp_path/'edge-cases/matrix.json').write_text('{')
    result=summarize(tmp_path)
    assert result['controlled_mutations']['status']=='invalid'
    assert 'controlled_mutations_not_executed' in result['diagnostic_coverage_gaps']


@pytest.mark.parametrize('failures',[0,1])
def test_edges_cli_preserves_offline_arguments_and_failure_exit(tmp_path,monkeypatch,capsys,failures):
    from tools.live_workflow import edges,runner
    root=tmp_path/'campaign';receipt=tmp_path/'offline-boundary.json'
    def controlled_run(*,root: str,boundary_receipt: str):
        assert root==str(tmp_path/'campaign')
        assert boundary_receipt==str(receipt)
        return {'counts':{'pass':1-failures,'fail':failures,'unexercised':16},
            'whole_workflow_coverage':False,'immutable_parent_preserved':True}
    monkeypatch.setattr(edges,'run_edges',controlled_run)
    code=runner.main(['edges','--root',str(root),'--boundary-receipt',str(receipt)])
    assert code==failures
    result=json.loads(capsys.readouterr().out)
    assert result['counts']['fail']==failures
    assert result['whole_workflow_coverage'] is False


def native_summary_fixture():
    return {'checks':{'sixteen_books':True,'accounting':True,'staging_complete':True,
        'staging_valid':True,'evidence_complete':True,'source_completeness':True,'outcome':'clean'},
        'canonical_report_reproduction':{'canonical_json_identical':True,'canonical_markdown_identical':True},
        'recorded_boundary_coverage':{'unfinished_reservations':0,'observed_failures':2,
            'unobserved_model_roles':['bounded','committee','thesis']}}


@pytest.mark.parametrize('field',[
    ('checks','sixteen_books'),('checks','accounting'),('checks','staging_complete'),
    ('checks','staging_valid'),('checks','evidence_complete'),('checks','source_completeness'),
    ('canonical_report_reproduction','canonical_json_identical'),
    ('canonical_report_reproduction','canonical_markdown_identical'),
    ('recorded_boundary_coverage','unfinished_reservations'),
])
def test_native_cli_rejects_incomplete_acceptance_evidence_without_discarding_observation(tmp_path,monkeypatch,capsys,field):
    from tools.live_workflow import runner
    result=native_summary_fixture()
    result[field[0]][field[1]]=1 if field[1]=='unfinished_reservations' else False
    monkeypatch.setattr(runner,'native',lambda args:result)
    code=runner.main(['native','--root',str(tmp_path),'--session','2026-10-30',
        '--boundary-receipt',str(tmp_path/'boundary.json')])
    assert code==1
    assert json.loads(capsys.readouterr().out)==result


def test_native_cli_allows_natural_unobserved_roles_and_retained_prior_failures(tmp_path,monkeypatch,capsys):
    from tools.live_workflow import runner
    result=native_summary_fixture()
    monkeypatch.setattr(runner,'native',lambda args:result)
    assert runner.main(['native','--root',str(tmp_path),'--session','2026-10-30',
        '--boundary-receipt',str(tmp_path/'boundary.json')])==0
    assert json.loads(capsys.readouterr().out)==result


@pytest.mark.parametrize('outcome',['failed','degraded'])
def test_native_cli_preserves_failed_or_degraded_outcome_precedence(tmp_path,monkeypatch,capsys,outcome):
    from tools.live_workflow import runner
    result=native_summary_fixture();result['checks']['outcome']=outcome
    monkeypatch.setattr(runner,'native',lambda args:result)
    code=runner.main(['native','--root',str(tmp_path),'--session','2026-10-30',
        '--boundary-receipt',str(tmp_path/'boundary.json')])
    assert code==1
    assert json.loads(capsys.readouterr().out)==result


def test_summary_cli_remains_informational_when_native_checks_fail(tmp_path,monkeypatch,capsys):
    from tools.live_workflow import runner
    result=native_summary_fixture();result['checks']['source_completeness']=False
    monkeypatch.setattr(runner,'summarize',lambda root:result)
    assert runner.main(['summary','--root',str(tmp_path)])==0
    assert json.loads(capsys.readouterr().out)==result


@pytest.fixture
def owned_worker_startup(tmp_path):
    """Owned .pth plus controlled installers, with the native cleaned child env."""
    import os
    import shutil
    from tools.live_workflow import bootstrap,runner
    repo=tmp_path/'candidate'; repo.mkdir()
    app=repo/'tradingagents'; app.mkdir();(app/'__init__.py').write_text('')
    scripts=repo/'scripts';scripts.mkdir()
    worker=app/'strategies/data_sources/noaa_yearly_transport.py';worker.parent.mkdir(parents=True)
    worker.write_text('import json,os\nprint(json.dumps({"controlled_transport_reached":True,"phase":os.environ["EVENTEDGE_LIVE_PHASE"]}))\n')
    hooks=tmp_path/'hooks';package=hooks/'tools/live_workflow';package.mkdir(parents=True)
    (hooks/'tools/__init__.py').write_text('');(package/'__init__.py').write_text('')
    shutil.copyfile(bootstrap.__file__,package/'bootstrap.py');shutil.copyfile(runner.__file__,package/'runner.py')
    trace=tmp_path/'trace';trace.mkdir()
    (package/'recorded_io.py').write_text('import json,os\nfrom pathlib import Path\ndef install_source_io(**options):\n Path(options["tape_dir"],str(os.getpid())+".json").write_text(json.dumps({"phase":options["phase"]}))\n')
    (package/'recorded_models.py').write_text('def install_model_io(**options): pass\n')
    (package/'recorded_clocks.py').write_text('import os\ndef install_clock_io(config,phase):\n assert phase==os.environ["EVENTEDGE_LIVE_PHASE"]\n assert config["mode"]=="live_record"\n')
    dependency=tmp_path/'controlled-dependencies';dependency.mkdir()
    (dependency/'httpx.py').write_text('''BODY=b"offline controlled transport; not NOAA evidence\\n"
class TimeoutException(Exception): pass
class TransportError(Exception): pass
class Timeout:
 def __init__(self,**options): pass
class Response:
 status_code=200
 headers={"Content-Length":str(len(BODY)),"Content-Encoding":"identity"}
 def __enter__(self): return self
 def __exit__(self,*args): pass
 def iter_raw(self,chunk_size): yield BODY
class Client:
 def __init__(self,**options): pass
 def __enter__(self): return self
 def __exit__(self,*args): pass
 def stream(self,*args,**kwargs): return Response()
''')
    config=tmp_path/'hook-config.json'
    config.write_text(json.dumps({'candidate_repo':str(repo),'phase':'generation_prepare','mode':'live_record',
        'tape_dir':str(trace),'run_identity':{'test':'controlled-no-network'}}))
    env={'PATH':'/usr/bin:/bin','HOME':str(tmp_path),'LANG':'C.UTF-8','LC_ALL':'C.UTF-8'}
    subprocess.run([sys.executable,'-I','-B','-m','venv','--without-pip',str(repo/'.venv')],env=env,check=True,capture_output=True)
    site=repo/'.venv/lib'/f'python{sys.version_info.major}.{sys.version_info.minor}'/'site-packages'
    runner._write_owned_startup(site,dependency,hooks,config)
    parent_code='''import json,os,subprocess,sys
from pathlib import Path
repo=Path(__file__).resolve().parents[1]
worker=repo/'tradingagents/strategies/data_sources/noaa_yearly_transport.py'
env={'LANG':'C.UTF-8','LC_ALL':'C.UTF-8','PYTHONIOENCODING':'utf-8'}
command=[sys.executable,'-B',str(worker),'--url','https://offline.invalid/noaa','--output',str(repo/'body'),'--max-bytes','1024','--connect-timeout','1','--read-timeout','1']
result=subprocess.run(command,env=env,capture_output=True,text=True,timeout=5)
print(json.dumps({'returncode':result.returncode,'stdout':result.stdout,'stderr':result.stderr,'worker_environment_names':sorted(env)}))
'''
    for name in ('run_cohorts.py','run_generations.py','unknown.py'):
        (scripts/name).write_text(parent_code)
    return {'repo':repo,'config':config,'trace':trace,'env':env,'worker':worker,'python':repo/'.venv/bin/python'}


def _run_owned_worker(fixture,parent,args):
    result=subprocess.run([str(fixture['python']),'-B',str(fixture['repo']/'scripts'/parent),*args],
        env=fixture['env'],cwd=fixture['repo'],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize('parent,args,phase',[
    ('run_cohorts.py',['--date','2026-10-30','--preflight','--preflight-mode','screen'],'preflight_screen'),
    ('run_cohorts.py',['--date','2026-10-30','--preflight','--preflight-mode','governed'],'preflight_governed'),
    ('run_cohorts.py',['--date','2026-10-30'],'daily'),
    ('run_generations.py',['preflight','--date','2026-10-30','--preflight-mode','screen'],'preflight_screen'),
    ('run_generations.py',['run-daily','--date','2026-10-30'],'daily'),
])
def test_owned_pth_cleaned_worker_infers_only_verified_native_parent_phase(owned_worker_startup,parent,args,phase):
    result=_run_owned_worker(owned_worker_startup,parent,args)
    assert result['returncode']==0,result['stderr']
    assert json.loads(result['stdout'])=={'controlled_transport_reached':True,'phase':phase}
    assert result['worker_environment_names']==['LANG','LC_ALL','PYTHONIOENCODING']


def test_owned_worker_rejects_unrecognized_parent(owned_worker_startup):
    result=_run_owned_worker(owned_worker_startup,'unknown.py',['--date','2026-10-30'])
    assert result['returncode']==90
    assert result['stdout']==''


@pytest.mark.parametrize('problem',['missing_pinned_config','conflicting_environment_config'])
def test_owned_pth_does_not_fallback_from_missing_or_conflicting_pinned_config(owned_worker_startup,problem,tmp_path):
    fixture=owned_worker_startup
    if problem=='missing_pinned_config':fixture['config'].unlink()
    else:fixture['env']['EVENTEDGE_LIVE_CONFIG']=str(tmp_path/'unrelated-config.json')
    result=subprocess.run([str(fixture['python']),'-B','-c','print("must not reach payload")'],env=fixture['env'],
        cwd=fixture['repo'],capture_output=True,text=True,timeout=5)
    assert result.returncode==90 and result.stdout==''


@pytest.mark.parametrize('relative_parent',[False,True])
def test_actual_noaa_worker_reaches_controlled_transport_with_cleaned_environment(owned_worker_startup,relative_parent):
    import os
    candidate_root=os.environ.get('EVENTEDGE_TEST_CANDIDATE')
    if not candidate_root:pytest.skip('actual candidate NOAA transport requires explicit EVENTEDGE_TEST_CANDIDATE')
    source=Path(candidate_root)/'tradingagents/strategies/data_sources/noaa_yearly_transport.py'
    if not source.is_file():pytest.skip('supplied candidate lacks native NOAA transport')
    fixture=owned_worker_startup;fixture['worker'].write_bytes(source.read_bytes())
    if relative_parent:
        generation=_controlled_generation_parent(fixture)
        process=subprocess.run([str(fixture['python']),'scripts/run_cohorts.py','--date','2026-10-30'],
            env=fixture['env'],cwd=generation,capture_output=True,text=True,timeout=10)
        assert process.returncode==0,process.stderr
        result=json.loads(process.stdout)
    else:
        generation=fixture['repo']
        result=_run_owned_worker(fixture,'run_cohorts.py',['--date','2026-10-30'])
    assert result['returncode']==0,result['stderr']
    response=json.loads(result['stdout']);body=(generation/'body').read_bytes()
    assert response['status_code']==200 and response['bytes']==len(body)
    assert response['sha256']==hashlib.sha256(body).hexdigest()
    assert body==b'offline controlled transport; not NOAA evidence\n'


@pytest.mark.parametrize('args',[
    ['--date','2026-10-30','--preflight','--preflight-mode','unknown'],
    ['--date','2026-10-30','--preflight'],
    ['--date','2026-10-30','--preflight-mode','screen'],
    ['--date','2026-10-30','--recover-runtime'],
])
def test_owned_worker_rejects_ambiguous_or_non_native_parent_phase(owned_worker_startup,args):
    result=_run_owned_worker(owned_worker_startup,'run_cohorts.py',args)
    assert result['returncode']==90 and result['stdout']==''


@pytest.mark.parametrize('problem',['uid','parent_changed','status_bound','argv_bound'])
def test_cleaned_worker_parent_metadata_is_same_uid_stable_and_bounded(tmp_path,monkeypatch,problem):
    from io import BytesIO
    import os
    from tools.live_workflow.bootstrap import _cleaned_noaa_phase
    uid=os.getuid()+1 if problem=='uid' else os.getuid()
    status=('Uid:\t'+('\t'.join([str(uid)]*4))+'\n').encode()
    command=b'\x00'.join(str(value).encode() for value in
        (sys.executable,tmp_path/'scripts/run_cohorts.py','--date','2026-10-30'))+b'\x00'
    if problem=='status_bound':status=b'x'*8193
    if problem=='argv_bound':command=b'x'*32768+b'\x00'
    pids=iter((123,124)) if problem=='parent_changed' else None
    monkeypatch.setattr(os,'getppid',lambda:next(pids) if pids else 123)
    def read_metadata(path,*args,**kwargs):
        assert path.parent==Path('/proc/123')
        return BytesIO(status if path.name=='status' else command)
    monkeypatch.setattr(Path,'open',read_metadata)
    with pytest.raises(RuntimeError,match='owned NOAA parent'):
        _cleaned_noaa_phase(tmp_path)


def _controlled_generation_parent(fixture):
    """Exact GenerationManager shape: relative script, generation worktree cwd."""
    generation=fixture['repo']/'.worktrees/gen_001'
    scripts=generation/'scripts';scripts.mkdir(parents=True)
    (scripts/'run_cohorts.py').write_bytes((fixture['repo']/'scripts/run_cohorts.py').read_bytes())
    package=generation/'tradingagents';package.mkdir();(package/'__init__.py').write_text('')
    worker=package/'strategies/data_sources/noaa_yearly_transport.py';worker.parent.mkdir(parents=True)
    worker.write_bytes(fixture['worker'].read_bytes())
    return generation


@pytest.mark.parametrize('args,phase',[
    (['--date','2026-10-30','--preflight','--preflight-mode','screen'],'preflight_screen'),
    (['--date','2026-10-30','--preflight','--preflight-mode','governed'],'preflight_governed'),
    (['--date','2026-10-30'],'daily'),
])
def test_cleaned_worker_accepts_exact_generation_manager_relative_argv_and_cwd(owned_worker_startup,args,phase):
    fixture=owned_worker_startup;generation=_controlled_generation_parent(fixture)
    process=subprocess.run([str(fixture['python']),'scripts/run_cohorts.py',*args],env=fixture['env'],
        cwd=generation,capture_output=True,text=True,timeout=10)
    assert process.returncode==0,process.stderr
    result=json.loads(process.stdout)
    assert result['returncode']==0,result['stderr']
    assert json.loads(result['stdout'])['phase']==phase


@pytest.fixture
def replay_git_guard(candidate,monkeypatch):
    from collections import Counter
    from tools.live_workflow import bootstrap,runner
    repo,_=candidate
    # Both globally patched objects must return to their original state after each
    # test; the subprocess guard itself deliberately persists across module loads.
    monkeypatch.delattr(sys,'_eventedge_replay_process_guard',raising=False)
    monkeypatch.setattr(subprocess,'Popen',subprocess.Popen)
    return bootstrap,repo,runner.readonly_git_identity(_test_git_sha256()),Counter()


def _canonical_git_command(repo):
    return ['git','-C',str(repo),'rev-parse','--path-format=absolute','--git-common-dir']


def test_replay_guard_permits_only_real_local_canonical_git_identity(replay_git_guard):
    bootstrap,repo,identity,attempts=replay_git_guard
    bootstrap._install_process_guard(attempts,repo=repo,git_identity=identity)
    result=subprocess.run(_canonical_git_command(repo),capture_output=True,text=True,check=True)
    assert Path(result.stdout.strip())==repo/'.git'
    assert not attempts
    assert sys._eventedge_replay_process_guard['allowed']=={'canonical_git_common_dir':1}
    with pytest.raises(RuntimeError,match='process'):
        subprocess.run(['git','-C',str(repo),'status'],capture_output=True,text=True)
    assert attempts=={'process':1}


@pytest.mark.parametrize('variant',[
    'other_repo','symlink_repo','executable','shell_string','bytes','tuple','extra_arg','head','status',
    'config','worktree','fetch','shell_kw','env','cwd','executable_kw','preexec_fn','pass_fds',
    'extra_positional','stdout','stdin','close_fds','creationflags',
])
def test_replay_guard_rejects_all_noncanonical_process_forms(replay_git_guard,monkeypatch,tmp_path,variant):
    bootstrap,repo,identity,attempts=replay_git_guard
    launches=[]
    monkeypatch.setattr(subprocess,'Popen',lambda *args,**kwargs:launches.append((args,kwargs)))
    bootstrap._install_process_guard(attempts,repo=repo,git_identity=identity)
    argv=_canonical_git_command(repo);extra=[]
    kwargs={'stdout':subprocess.PIPE,'stderr':subprocess.PIPE,'text':True}
    if variant=='other_repo':argv[2]=str(repo)+'-sibling'
    elif variant=='symlink_repo':
        link=tmp_path/'alias';link.symlink_to(repo,target_is_directory=True);argv[2]=str(link)
    elif variant=='executable':argv[0]='/usr/bin/git'
    elif variant=='shell_string':argv='git status'
    elif variant=='bytes':argv=[value.encode() for value in argv]
    elif variant=='tuple':argv=tuple(argv)
    elif variant=='extra_arg':argv.append('--help')
    elif variant in ('head','status','config','worktree','fetch'):
        argv=argv[:3]+({'head':['rev-parse','HEAD'],'status':['status'],'config':['config','-l'],
            'worktree':['worktree','list'],'fetch':['fetch']}[variant])
    elif variant=='extra_positional':extra=[0]
    elif variant=='stdout':kwargs['stdout']=subprocess.DEVNULL
    else:
        key={'shell_kw':'shell','executable_kw':'executable'}.get(variant,variant)
        kwargs[key]={'shell':True,'env':{},'cwd':str(repo),'executable':'/bin/true',
            'preexec_fn':lambda:None,'pass_fds':(),'stdin':subprocess.DEVNULL,
            'close_fds':True,'creationflags':0}[key]
    with pytest.raises(RuntimeError,match='process'):
        subprocess.Popen(argv,*extra,**kwargs)
    assert attempts=={'process':1} and launches==[]


def test_replay_git_child_has_fixed_environment_and_no_inherited_descriptors(replay_git_guard,monkeypatch):
    bootstrap,repo,identity,attempts=replay_git_guard
    launches=[]
    monkeypatch.setattr(subprocess,'Popen',lambda *args,**kwargs:launches.append((args,kwargs)))
    for name in ('PATH','GIT_DIR','GIT_WORK_TREE','GIT_CONFIG_COUNT','GIT_CONFIG_KEY_0',
            'GIT_CONFIG_VALUE_0','GIT_EXEC_PATH','LD_PRELOAD','FRED_API_KEY'):
        monkeypatch.setenv(name,'dummy-untrusted-ambient')
    bootstrap._install_process_guard(attempts,repo=repo,git_identity=identity)
    subprocess.Popen(_canonical_git_command(repo),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    (args,options),=launches
    assert args[0]==['/usr/bin/git','--no-optional-locks','-c','core.hooksPath=/dev/null',
        '-C',str(repo),'rev-parse','--path-format=absolute','--git-common-dir']
    assert options=={'cwd':str(repo),'stdin':subprocess.DEVNULL,'stdout':subprocess.PIPE,
        'stderr':subprocess.PIPE,'text':True,'close_fds':True,'env':{
        'PATH':'/usr/bin:/bin','HOME':'/nonexistent','LANG':'C.UTF-8','GIT_CONFIG_NOSYSTEM':'1',
        'GIT_CONFIG_GLOBAL':'/dev/null','GIT_TERMINAL_PROMPT':'0','GIT_OPTIONAL_LOCKS':'0',
        'GIT_NO_LAZY_FETCH':'1'}}
    assert not attempts


def test_replay_process_guard_is_idempotent_across_module_instances(replay_git_guard,monkeypatch):
    import importlib.util
    bootstrap,repo,identity,attempts=replay_git_guard
    original=subprocess.Popen
    bootstrap._install_process_guard(attempts,repo=repo,git_identity=identity)
    first=subprocess.Popen
    spec=importlib.util.spec_from_file_location('tools.live_workflow.isolated_bootstrap_namespace',bootstrap.__file__)
    other=importlib.util.module_from_spec(spec);spec.loader.exec_module(other)
    other._install_process_guard(attempts,repo=repo,git_identity=identity)
    assert subprocess.Popen is first
    assert sys._eventedge_replay_process_guard['original'] is original
    subprocess.run(_canonical_git_command(repo),capture_output=True,text=True,check=True)
    assert sys._eventedge_replay_process_guard['allowed']=={'canonical_git_common_dir':1}
    with pytest.raises(RuntimeError,match='configuration'):
        other._install_process_guard(attempts,repo=repo.parent,git_identity=identity)


def test_replay_guard_does_not_reset_local_read_limit_or_denied_attempts(replay_git_guard):
    bootstrap,repo,identity,attempts=replay_git_guard
    bootstrap._install_process_guard(attempts,repo=repo,git_identity=identity)
    with pytest.raises(RuntimeError,match='process'):
        subprocess.Popen(['git','status'])
    subprocess.run(_canonical_git_command(repo),capture_output=True,text=True,check=True)
    bootstrap._install_process_guard(attempts,repo=repo,git_identity=identity)
    with pytest.raises(RuntimeError,match='process'):
        subprocess.run(_canonical_git_command(repo),capture_output=True,text=True,check=True)
    assert attempts=={'process':2}
    assert sys._eventedge_replay_process_guard['allowed']=={'canonical_git_common_dir':1}


@pytest.mark.parametrize('defect',['symlink','nonregular','wrong_owner','group_writable','world_writable'])
def test_readonly_git_identity_refuses_untrusted_binary_metadata(monkeypatch,defect):
    from types import SimpleNamespace
    from tools.live_workflow.runner import readonly_git_identity
    expected=_test_git_sha256()
    original_symlink=Path.is_symlink
    original_lstat=Path.lstat
    def is_symlink(path):
        return True if path==Path('/usr/bin/git') and defect=='symlink' else original_symlink(path)
    def lstat(path):
        if path!=Path('/usr/bin/git'):return original_lstat(path)
        mode=0o100755
        if defect=='nonregular':mode=0o040755
        if defect=='group_writable':mode|=0o020
        if defect=='world_writable':mode|=0o002
        return SimpleNamespace(st_mode=mode,st_uid=1000 if defect=='wrong_owner' else 0)
    monkeypatch.setattr(Path,'is_symlink',is_symlink)
    monkeypatch.setattr(Path,'lstat',lstat)
    with pytest.raises(AcceptanceError,match='symlink|git_binary'):
        readonly_git_identity(expected)


def test_replay_git_binary_pin_mismatch_denies_before_launch(replay_git_guard,monkeypatch):
    bootstrap,repo,identity,attempts=replay_git_guard
    launches=[]
    monkeypatch.setattr(subprocess,'Popen',lambda *args,**kwargs:launches.append((args,kwargs)))
    bad={**identity,'sha256':'0'*64}
    bootstrap._install_process_guard(attempts,repo=repo,git_identity=bad)
    with pytest.raises(RuntimeError,match='process'):
        subprocess.Popen(_canonical_git_command(repo),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    assert attempts=={'process':1} and not launches


def test_actual_native_canonical_lock_identity_runs_under_replay_gate(replay_git_guard):
    import importlib.util
    import os
    bootstrap,repo,identity,attempts=replay_git_guard
    source_root=os.environ.get('EVENTEDGE_TEST_CANDIDATE')
    if not source_root:pytest.skip('native lock requires explicit EVENTEDGE_TEST_CANDIDATE')
    source=Path(source_root)/'tradingagents/strategies/orchestration/runtime_lock.py'
    spec=importlib.util.spec_from_file_location('controlled_native_runtime_lock',source)
    module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module
    try:
        spec.loader.exec_module(module)
        bootstrap._install_process_guard(attempts,repo=repo,git_identity=identity)
        actual=module.canonical_runtime_lock_path(repo)
        assert actual==repo/'data/operational/eventedge-runtime.lock'
        with module.runtime_lock(actual,exclusive=True):
            assert actual.is_file()
        assert not attempts
        assert sys._eventedge_replay_process_guard['allowed']=={'canonical_git_common_dir':1}
    finally:
        sys.modules.pop(spec.name,None)


@pytest.mark.parametrize('readonly,owner,pin_matches,accepted',[
    (True,65534,True,True),
    (False,65534,True,False),
    (True,65533,True,False),
    (True,65534,False,False),
    (True,0,False,False),
])
def test_git_identity_mapped_owner_requires_external_pin_and_readonly_mount(
        monkeypatch,readonly,owner,pin_matches,accepted):
    import os
    from types import SimpleNamespace
    from tools.live_workflow import runner
    expected=hashlib.sha256(Path('/usr/bin/git').read_bytes()).hexdigest()
    if not pin_matches:expected='0'*64
    original_lstat=Path.lstat
    monkeypatch.setattr(Path,'lstat',lambda path:SimpleNamespace(st_mode=0o100755,st_uid=owner)
        if path==Path('/usr/bin/git') else original_lstat(path))
    monkeypatch.setattr(runner,'_kernel_overflow_uid',lambda:65534)
    monkeypatch.setattr(os,'statvfs',lambda path:SimpleNamespace(f_flag=os.ST_RDONLY if readonly else 0))
    if accepted:
        assert runner.readonly_git_identity(expected)=={'path':'/usr/bin/git','sha256':expected}
    else:
        with pytest.raises(AcceptanceError,match='git_binary'):
            runner.readonly_git_identity(expected)


@pytest.mark.parametrize('value',[None,'','0'*63,'G'*64])
def test_git_identity_cannot_discover_an_unpinned_binary(value):
    from tools.live_workflow.runner import readonly_git_identity
    with pytest.raises(AcceptanceError,match='git_binary_pin'):
        readonly_git_identity(value)


@pytest.mark.parametrize('value,expected',[
    (b'65534\n',65534),(b'12345\n',12345),(b'x'*33,None),(b'-1\n',None),
    (b'0\n',None),(b'4294967295\n',None),(b'65534\nextra',None),
])
def test_kernel_overflow_uid_uses_only_bounded_fixed_public_kernel_metadata(monkeypatch,value,expected):
    from io import BytesIO
    from tools.live_workflow.runner import _kernel_overflow_uid
    def read_metadata(path,*args,**kwargs):
        assert path==Path('/proc/sys/kernel/overflowuid') and args==('rb',)
        return BytesIO(value)
    monkeypatch.setattr(Path,'open',read_metadata)
    if expected is None:
        with pytest.raises(AcceptanceError,match='git_binary_overflow_uid'):
            _kernel_overflow_uid()
    else:
        assert _kernel_overflow_uid()==expected


def test_replay_guard_accepts_mapped_git_only_with_its_bound_external_pin(replay_git_guard,monkeypatch):
    import os
    from types import SimpleNamespace
    from tools.live_workflow import runner
    bootstrap,repo,identity,attempts=replay_git_guard
    original_lstat=Path.lstat
    monkeypatch.setattr(Path,'lstat',lambda path:SimpleNamespace(st_mode=0o100755,st_uid=65534)
        if path==Path('/usr/bin/git') else original_lstat(path))
    monkeypatch.setattr(runner,'_kernel_overflow_uid',lambda:65534)
    monkeypatch.setattr(os,'statvfs',lambda path:SimpleNamespace(f_flag=os.ST_RDONLY))
    bootstrap._install_process_guard(attempts,repo=repo,git_identity=identity)
    result=subprocess.run(_canonical_git_command(repo),capture_output=True,text=True,check=True)
    assert result.stdout.strip()==str(repo/'.git') and not attempts


def test_prepare_requires_supervisor_supplied_git_pin_before_any_filesystem_work(capsys):
    from tools.live_workflow.runner import main
    args=['prepare']
    for key in ('candidate-root','candidate-commit','profile-sha256','dependency-site','dependency-sha256',
            'destination','session'):
        args+=['--'+key,'dummy-unused-input']
    with pytest.raises(SystemExit) as error:
        main(args)
    assert error.value.code==2 and '--git-sha256' in capsys.readouterr().err


@pytest.fixture
def openbb_git_guard(replay_git_guard,tmp_path,monkeypatch):
    bootstrap,repo,identity,attempts=replay_git_guard
    site=tmp_path/'reviewed-dependency-site';version=site/'openbb_core/app/version.py'
    version.parent.mkdir(parents=True);version.write_text('# controlled version-module fixture\n')
    monkeypatch.setattr(bootstrap,'OPENBB_VERSION_SHA256',hashlib.sha256(version.read_bytes()).hexdigest())
    bootstrap._install_process_guard(attempts,repo=repo,git_identity=identity,dependency_site=site)
    return bootstrap,repo,identity,attempts,site


def test_openbb_local_git_probe_has_separate_exact_two_call_limit(openbb_git_guard):
    bootstrap,repo,identity,attempts,site=openbb_git_guard
    command=['/usr/bin/git','rev-parse','--is-inside-work-tree']
    for _ in range(2):
        result=subprocess.run(command,cwd=site/'openbb_core/app',stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        assert result.returncode==128  # native OpenBB converts this to False
    canonical=subprocess.run(_canonical_git_command(repo),capture_output=True,text=True,check=True)
    assert canonical.stdout.strip()==str(repo/'.git') and not attempts
    bootstrap._install_process_guard(attempts,repo=repo,git_identity=identity,dependency_site=site)
    with pytest.raises(RuntimeError,match='process'):
        subprocess.run(command,cwd=site/'openbb_core/app',stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    assert attempts=={'process':1}
    assert sys._eventedge_replay_process_guard['allowed']=={
        'canonical_git_common_dir':1,'openbb_dependency_git_probe':2}


@pytest.mark.parametrize('variant',['other_cwd','repo_cwd','cwd_string','other_argv','extra_arg','text',
    'env','shell','stdout','cwd_missing','version_changed'])
def test_openbb_git_probe_denies_every_other_process_shape(openbb_git_guard,variant):
    bootstrap,repo,identity,attempts,site=openbb_git_guard
    command=['/usr/bin/git','rev-parse','--is-inside-work-tree']
    kwargs={'cwd':site/'openbb_core/app','stdout':subprocess.DEVNULL,'stderr':subprocess.DEVNULL}
    if variant=='other_cwd':kwargs['cwd']=site/'openbb_core'
    elif variant=='repo_cwd':kwargs['cwd']=repo
    elif variant=='cwd_string':kwargs['cwd']=str(site/'openbb_core/app')
    elif variant=='other_argv':command[-1]='HEAD'
    elif variant=='extra_arg':command.append('--help')
    elif variant=='text':kwargs['text']=True
    elif variant=='env':kwargs['env']={}
    elif variant=='shell':kwargs['shell']=True
    elif variant=='stdout':kwargs['stdout']=subprocess.PIPE
    elif variant=='cwd_missing':kwargs.pop('cwd')
    elif variant=='version_changed':(site/'openbb_core/app/version.py').write_text('# changed\n')
    with pytest.raises(RuntimeError,match='process'):
        subprocess.Popen(command,**kwargs)
    assert attempts=={'process':1} and not sys._eventedge_replay_process_guard['allowed']


def test_external_worker_requires_no_site_isolation_and_retains_frozen_failure(tmp_path):
    from tools.live_workflow import replay_worker
    worker=Path(replay_worker.__file__)
    root=tmp_path/'frozen';root.mkdir();(root/'contract.json').write_text('{}')
    old=root/'accepted-replay-process.json';old.write_text('{"retained":"old failure"}')
    output=tmp_path/'new-output';output.mkdir(mode=0o700)
    args=['--root',str(root),'--output-dir',str(output),'--session','2026-10-30',
        '--contract-sha256','0'*64,'--worker-sha256',hashlib.sha256(worker.read_bytes()).hexdigest(),
        '--uname-sha256',_test_uname_identity()['sha256']]
    env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','PYTHONPATH':str(root),
        'EVENTEDGE_LIVE_CONFIG':'/must-not-load-recording-hooks','FRED_API_KEY':'dummy-unused'}
    unisolated=subprocess.run([sys.executable,'-I','-B',str(worker),*args],env=env,
        capture_output=True,text=True,timeout=5)
    assert unisolated.returncode!=0 and 'requires_isolated_no_site' in unisolated.stderr
    result=subprocess.run([sys.executable,'-I','-S','-B',str(worker),*args],env=env,
        capture_output=True,text=True,timeout=5)
    assert result.returncode==2
    failure=json.loads((output/'replay-worker-failure.json').read_text())
    assert failure['failure_reason']=='external_replay_contract_hash_mismatch'
    assert 'dummy-unused' not in json.dumps(failure)
    assert old.read_text()=='{"retained":"old failure"}'
    assert not (output/'accepted-state').exists()


@pytest.mark.parametrize('startup',['external_no_site','fresh_owned_pth'])
def test_full_accepted_openbb_import_under_offline_gate(tmp_path,startup):
    import os
    candidate_root=os.environ.get('EVENTEDGE_TEST_CANDIDATE')
    site=os.environ.get('EVENTEDGE_TEST_DEPENDENCY_SITE')
    if not candidate_root or not site:
        pytest.skip('full accepted OpenBB import requires explicit candidate and dependency site')
    site=Path(site)
    if not (site/'openbb_core/app/version.py').is_file():
        pytest.skip('explicit dependency site is unavailable to this test process')
    tool=Path(__file__).resolve().parents[1]
    repo=tmp_path/'dummy-repo';repo.mkdir()
    # No source calls, models, settings credentials or retained baseline. Only
    # exact native source availability and the actual accepted dependency import.
    code='''import json,sys
from pathlib import Path
sys.path[:0]=[sys.argv[2],sys.argv[1],sys.argv[3]]
from tools.live_workflow import bootstrap,runner
attempts=bootstrap._replay_attempts()
bootstrap._forbid_io(attempts,repo=Path(sys.argv[4]),
 git_identity={'path':'/usr/bin/git','sha256':sys.argv[5]},dependency_site=Path(sys.argv[3]),
 uname_identity={'path':'/usr/bin/uname','sha256':sys.argv[6]})
from tradingagents.strategies.data_sources.openbb_source import OpenBBSource
import tradingagents
assert Path(tradingagents.__file__).is_relative_to(Path(sys.argv[2]))
available=OpenBBSource().is_available()
print(json.dumps({'available':available,'attempted_calls':dict(attempts),
 'allowed_local_process_calls':dict(sys._eventedge_replay_process_guard['allowed'])}))
'''
    home=tmp_path/'home';home.mkdir(mode=0o700)
    env={'PATH':'/usr/bin:/bin','HOME':str(home),'LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1',
        'XDG_CONFIG_HOME':str(home/'.config'),'XDG_CACHE_HOME':str(home/'.cache'),
        'XDG_DATA_HOME':str(home/'.local/share')}
    if startup=='external_no_site':
        command=[sys.executable,'-I','-S','-B','-c',code,str(tool),candidate_root,str(site),
            str(repo),_test_git_sha256(),_test_uname_identity()['sha256']]
    else:
        from tools.live_workflow.runner import _write_owned_startup
        subprocess.run([sys.executable,'-I','-B','-m','venv','--without-pip',str(repo/'.venv')],
            env=env,check=True,capture_output=True,timeout=10)
        owned_site=repo/'.venv/lib'/f'python{sys.version_info.major}.{sys.version_info.minor}'/'site-packages'
        config=tmp_path/'fresh-hook-config.json'
        config.write_text(json.dumps({'candidate_repo':candidate_root,'phase':'test',
            'dependency_site':str(site),'local_git':{'path':'/usr/bin/git','sha256':_test_git_sha256()},
            'local_uname':_test_uname_identity()}))
        _write_owned_startup(owned_site,site,tool,config)
        payload='''import json,sys
from tradingagents.strategies.data_sources.openbb_source import OpenBBSource
available=OpenBBSource().is_available()
print(json.dumps({'available':available,'attempted_calls':dict(sys._eventedge_replay_attempts),
 'allowed_local_process_calls':dict(sys._eventedge_replay_process_guard['allowed'])}))
'''
        command=[str(repo/'.venv/bin/python'),'-I','-B','-c',payload,'accepted-replay']
    result=subprocess.run(command,env=env,cwd=tmp_path,capture_output=True,text=True,timeout=35)
    assert result.returncode==0,result.stderr[-4000:]
    assert json.loads(result.stdout.splitlines()[-1])=={'available':True,'attempted_calls':{},
        'allowed_local_process_calls':{'openbb_dependency_git_probe':2,'platform_uname_processor':1}}


def test_uname_probe_requires_explicit_pin_and_has_one_separate_call(replay_git_guard,admitted_uname):
    bootstrap,repo,identity,attempts=replay_git_guard
    bootstrap._install_process_guard(attempts,repo=repo,git_identity=identity,
        uname_identity=_test_uname_identity())
    value=subprocess.check_output(['uname','-p'],stderr=subprocess.DEVNULL,text=True,encoding='utf8')
    assert isinstance(value,str) and value.strip()
    subprocess.run(_canonical_git_command(repo),capture_output=True,text=True,check=True)
    with pytest.raises(RuntimeError,match='process'):
        subprocess.check_output(['uname','-p'],stderr=subprocess.DEVNULL,text=True,encoding='utf8')
    assert attempts=={'process':1}
    assert sys._eventedge_replay_process_guard['allowed']=={
        'canonical_git_common_dir':1,'platform_uname_processor':1}


@pytest.mark.parametrize('variant',['no_pin','bad_pin','other_binary','other_flag','extra_arg',
    'encoding','cwd','env','stdout','stderr','text','shell'])
def test_uname_probe_rejects_unpinned_or_non_native_shapes(replay_git_guard,variant):
    bootstrap,repo,identity,attempts=replay_git_guard
    uname=None if variant=='no_pin' else _test_uname_identity()
    if variant=='bad_pin':uname['sha256']='0'*64
    bootstrap._install_process_guard(attempts,repo=repo,git_identity=identity,uname_identity=uname)
    command=['uname','-p'];kwargs={'stdout':subprocess.PIPE,'stderr':subprocess.DEVNULL,
        'text':True,'encoding':'utf8'}
    if variant=='other_binary':command[0]='/usr/bin/uname'
    elif variant=='other_flag':command[-1]='-a'
    elif variant=='extra_arg':command.append('--help')
    elif variant=='encoding':kwargs['encoding']='utf-8'
    elif variant=='cwd':kwargs['cwd']=repo
    elif variant=='env':kwargs['env']={}
    elif variant=='stdout':kwargs['stdout']=subprocess.DEVNULL
    elif variant=='stderr':kwargs['stderr']=subprocess.PIPE
    elif variant=='text':kwargs['text']=False
    elif variant=='shell':kwargs['shell']=True
    with pytest.raises(RuntimeError,match='process'):
        subprocess.Popen(command,**kwargs)
    assert attempts=={'process':1} and not sys._eventedge_replay_process_guard['allowed']


def test_uname_delegation_uses_fixed_binary_environment_and_fd_controls(replay_git_guard,monkeypatch,admitted_uname):
    bootstrap,repo,identity,attempts=replay_git_guard
    launches=[]
    monkeypatch.setattr(subprocess,'Popen',lambda *args,**kwargs:launches.append((args,kwargs)))
    monkeypatch.setenv('PATH','dummy-poison');monkeypatch.setenv('LD_PRELOAD','dummy-poison')
    bootstrap._install_process_guard(attempts,repo=repo,git_identity=identity,uname_identity=_test_uname_identity())
    subprocess.Popen(['uname','-p'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,encoding='utf8')
    (args,kwargs),=launches
    assert args==(['/usr/bin/uname','-p'],)
    assert kwargs=={'cwd':str(repo),'stdin':subprocess.DEVNULL,'stdout':subprocess.PIPE,
        'stderr':subprocess.DEVNULL,'text':True,'encoding':'utf8','close_fds':True,
        'env':{'PATH':'/usr/bin:/bin','HOME':'/nonexistent','LANG':'C.UTF-8'}}
    assert not attempts


def test_readonly_git_status_does_not_refresh_copied_index(candidate,tmp_path):
    import shutil
    from tools.live_workflow import runner
    original,_=candidate;copy=tmp_path/'private-copy'
    shutil.copytree(original,copy)
    before=(copy/'.git/index').read_bytes()
    assert runner.git(copy,'status','--porcelain','--untracked-files=all')==''
    assert (copy/'.git/index').read_bytes()==before


@pytest.mark.parametrize('contract_kind',['retained_original','fresh_with_dependency_pins'])
def test_hook_shape_is_selected_by_verified_contract_not_stripped_config(tmp_path,contract_kind):
    from tools.live_workflow.runner import _verify_hook_configuration
    identity={key:'controlled-'+key for key in ('candidate_commit','original_profile_sha256',
        'profile_sha256','dependency_sha256','prepared_at','reference_session')}
    contract={**identity,'dependency_site':str(tmp_path/'deps'),
        'local_git':{'path':'/usr/bin/git','sha256':'0'*64}}
    config={'mode':'live_record','root':str(tmp_path),'candidate_repo':str(tmp_path/'repo'),
        'tape_dir':str(tmp_path/'tape'),'local_git':contract['local_git'],
        'run_identity':identity,'phase':'generation_prepare'}
    if contract_kind=='fresh_with_dependency_pins':
        contract['local_uname']={'path':'/usr/bin/uname','sha256':'1'*64}
        config.update(dependency_site=contract['dependency_site'],local_uname=contract['local_uname'])
    path=tmp_path/'hook-config.json';path.write_text(json.dumps(config))
    _verify_hook_configuration(tmp_path,contract)
    if contract_kind=='fresh_with_dependency_pins':
        config.pop('dependency_site');config.pop('local_uname')
    else:
        config['dependency_site']=contract['dependency_site']
    path.write_text(json.dumps(config))
    with pytest.raises(AcceptanceError,match='hook_configuration_changed'):
        _verify_hook_configuration(tmp_path,contract)


def test_explicit_source_credential_selection_drops_every_unselected_ambient_value(tmp_path):
    from tools.live_workflow.runner import selected_credential_environment
    ambient={'FRED_API_KEY':'dummy-fred','ALPACA_API_KEY':'dummy-alpaca',
        'OPENAI_API_KEY':'dummy-paid','TELEGRAM_BOT_TOKEN':'dummy-notification','HTTPS_PROXY':'dummy-proxy'}
    selected=selected_credential_environment(['FRED_API_KEY'],ambient)
    env=sanitized_environment(tmp_path,ambient,selected)
    assert selected=={'FRED_API_KEY':'dummy-fred'}
    assert env['FRED_API_KEY']=='dummy-fred'
    assert all(key not in env for key in ambient if key!='FRED_API_KEY')
    assert selected_credential_environment([],ambient)=={}


@pytest.mark.parametrize('names,ambient,reason',[
    (['FRED_API_KEY','FRED_API_KEY'],{'FRED_API_KEY':'dummy'},'duplicate'),
    (['FRED_API_KEY'],{},'missing'),
    (['FRED_API_KEY'],{'FRED_API_KEY':''},'missing'),
    (['OPENAI_API_KEY'],{'OPENAI_API_KEY':'dummy'},'unapproved'),
    (['FRED_API_KEY'],{'FRED_API_KEY':'dummy\ninvalid'},'invalid'),
])
def test_selected_source_credentials_fail_closed_without_disclosing_values(names,ambient,reason):
    from tools.live_workflow.runner import selected_credential_environment
    with pytest.raises(AcceptanceError,match=reason) as error:
        selected_credential_environment(names,ambient)
    assert all(value not in str(error.value) for value in ambient.values() if value)


@pytest.mark.parametrize('selection',['same_selected','none_selected','wrong_selected'])
def test_actual_native_frozen_source_identity_requires_the_same_selected_dummy_credentials(tmp_path,selection):
    import os
    candidate=os.environ.get('EVENTEDGE_TEST_CANDIDATE');site=os.environ.get('EVENTEDGE_TEST_DEPENDENCY_SITE')
    if not candidate or not site:
        pytest.skip('native frozen-source identity requires explicit candidate and accepted dependencies')
    tool=Path(__file__).resolve().parents[1]
    code='''import hashlib,json,os,sys
from pathlib import Path
sys.path[:0]=[sys.argv[1],sys.argv[2],sys.argv[3]]
from tools.live_workflow.runner import selected_credential_environment,sanitized_environment
from tradingagents.strategies.orchestration.source_inputs import SourceInputStore,source_configuration_fingerprint,SourceInputError
root=Path(sys.argv[4]);mode=sys.argv[5]
names=['FRED_API_KEY','ALPACA_API_KEY','ALPACA_SECRET_KEY']
dummy={key:'controlled-dummy-'+key for key in names}
os.environ.update(dummy)
config={'autoresearch':{'equity_universe_policy':'us_exchange_listed_sip_v1'}}
identity={'generation':'gen_001','session':'2026-10-30','commit':'a'*40,
 'configuration':source_configuration_fingerprint(config)}
store=SourceInputStore(root/'cache',accepted_dir=root/'accepted')
payload={'controlled':'offline fixture; not provider evidence'}
store.freeze(identity,payload)
path=next((root/'accepted').glob('*.json'));before=path.read_bytes()
if mode=='wrong_selected':os.environ['FRED_API_KEY']='controlled-different-dummy'
selected=selected_credential_environment(names if mode!='none_selected' else [])
env=sanitized_environment(root,{},selected);os.environ.clear();os.environ.update(env)
expected={**identity,'configuration':source_configuration_fingerprint(config)}
try:
 loaded=store.load_frozen(expected)
 outcome='matched' if loaded==payload else 'unexpected_payload'
except SourceInputError as error:
 assert str(error)=='frozen source bundle rejected: source identity mismatch'
 outcome='identity_mismatch'
assert path.read_bytes()==before
assert all(value.encode() not in before for value in dummy.values())
print(json.dumps({'outcome':outcome,'selected_names':sorted(selected),'source_envelope_unchanged':True}))
'''
    env={'PATH':'/usr/bin:/bin','HOME':str(tmp_path),'LANG':'C.UTF-8'}
    result=subprocess.run([sys.executable,'-I','-S','-B','-c',code,candidate,str(tool),site,str(tmp_path),selection],
        env=env,cwd=tmp_path,capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr[-3000:]
    observed=json.loads(result.stdout)
    assert observed['outcome']==('matched' if selection=='same_selected' else 'identity_mismatch')
    assert observed['selected_names']==([] if selection=='none_selected' else
        ['ALPACA_API_KEY','ALPACA_SECRET_KEY','FRED_API_KEY'])
    assert observed['source_envelope_unchanged'] is True


def test_uname_production_identity_still_rejects_symlinks(monkeypatch):
    from tools.live_workflow.runner import readonly_uname_identity
    identity=_test_uname_identity();original=Path.is_symlink
    monkeypatch.setattr(Path,'is_symlink',lambda path:True if path==Path('/usr/bin/uname') else original(path))
    with pytest.raises(AcceptanceError,match='path_symlink'):
        readonly_uname_identity(identity['sha256'])


def test_selected_credential_logging_redaction_covers_direct_file_handlers(tmp_path,monkeypatch):
    import logging
    import os
    from urllib.parse import quote_plus
    from tools.live_workflow.bootstrap import _install_log_redaction
    value='dummy selected source/value?private=redaction'
    monkeypatch.setattr(os,'environ',{'FRED_API_KEY':value})
    monkeypatch.setattr(logging.Formatter,'format',logging.Formatter.format)
    _install_log_redaction()
    path=tmp_path/'direct-native-file.log';handler=logging.FileHandler(path)
    logger=logging.Logger('controlled-direct-handler');logger.addHandler(handler)
    try:
        logger.error('native direct file %s encoded %s',value,quote_plus(value))
        handler.flush()
    finally:
        handler.close()
    text=path.read_text()
    assert value not in text and quote_plus(value) not in text
    assert text.count('[REDACTED]')==2


def test_normal_accepted_replay_forwards_only_explicit_selected_source_values(tmp_path,monkeypatch):
    from argparse import Namespace
    from tools.live_workflow import runner
    monkeypatch.setenv('FRED_API_KEY','controlled-dummy-fred')
    monkeypatch.setenv('OPENAI_API_KEY','controlled-dummy-paid')
    monkeypatch.setattr(runner,'verify_contract',lambda root:({'reference_session':'2026-10-30'},{}))
    monkeypatch.setattr(runner,'require_boundary',lambda *args,**kwargs:'0'*64)
    calls=[]
    def child(root,operation,**kwargs):
        calls.append((operation,kwargs))
        (root/'accepted-replay.json').write_text('{"controlled":"fixture"}')
    monkeypatch.setattr(runner,'_child',child)
    args=Namespace(root=tmp_path,session='2026-10-30',boundary_receipt=tmp_path/'unused',credential_env=['FRED_API_KEY'])
    assert runner.accepted_replay(args)=={'controlled':'fixture'}
    assert calls==[('accepted-replay',{'credentials':{'FRED_API_KEY':'controlled-dummy-fred'},
        'timeout':120,'extra':('--session','2026-10-30')})]
    attempt=(tmp_path/'replay-attempt.json').read_text()
    assert json.loads(attempt)['credential_env_names']==['FRED_API_KEY']
    assert 'controlled-dummy-fred' not in attempt and 'controlled-dummy-paid' not in attempt


@pytest.mark.parametrize('status,exit_code',[('pass',0),('fail',1)])
def test_ordinary_cli_requires_complete_process_acceptance(monkeypatch,capsys,status,exit_code):
    from tools.live_workflow import ordinary,runner
    seen=[]
    def observation(args):
        seen.append(args)
        return {'status':status,'native_process_success':status=='pass'}
    monkeypatch.setattr(ordinary,'run_ordinary',observation)
    # Report content alone must not override an infrastructure failure.
    monkeypatch.setattr(runner,'native_acceptance_passed',lambda result:True)
    result=runner.main(['ordinary','--root','/private/campaign','--session','2026-11-02',
        '--boundary-receipt','/private/boundary.json','--credential-env','FRED_API_KEY'])
    assert result==exit_code
    assert json.loads(capsys.readouterr().out)['status']==status
    assert seen[0].credential_env==['FRED_API_KEY'] and seen[0].session=='2026-11-02'


def test_observed_clocks_are_counted_separately_from_source_and_model_calls(tmp_path,monkeypatch):
    from tools.live_workflow import recorded_io,runner
    from tools.live_workflow.ordinary import _session_coverage
    records=[
        {'event_id':'a','phase':'daily','boundary':'observed_clock','operation':'datetime.now','failure':None},
        {'event_id':'b','phase':'daily','boundary':'http_decoded','operation':'requests.send','failure':None},
        {'event_id':'c','phase':'daily','boundary':'model_helper','operation':'call_analysis_model','failure':None,
            'public_request':{'role':'committee','model':'native-model','effort':'high'}},
    ]
    monkeypatch.setattr(recorded_io,'verify_tape',lambda root:records)
    cumulative=runner.tape_coverage(tmp_path)
    assert cumulative['verified_events']==3 and cumulative['clock_events']==1
    assert sum(row['calls'] for row in cumulative['source_boundaries'])==1
    assert sum(row['calls'] for row in cumulative['models'])==1
    session=_session_coverage(records)
    assert session['events']==3 and session['clock_events']==1
    assert session['source_events']==session['model_events']==1


@pytest.mark.parametrize('change,expected',[
    ('none','complete'),('budget_failure','incomplete'),('unsupported_event','incomplete'),
    ('wrong_actor','invalid'),('wrong_identity','invalid'),('missing_receipt','invalid'),
    ('changed_global_clock','invalid'),
])
def test_clock_capture_summary_preserves_failures_and_requires_bound_receipts(tmp_path,change,expected):
    from tools.live_workflow import runner
    identity={'controlled':'native-clock-receipt-test'}
    runner.private_json(tmp_path/'identity.json',identity)
    actor={'entrypoint':'scripts/run_cohorts.py','source_sha256':'b'*64}
    name='tradingagents.strategies.data_sources.evidence'
    receipt={'schema_version':1,'kind':'observed_clock_runtime','installed':True,'mode':'live_record','phase':'daily',
        'run_identity_sha256':runner.digest(identity),'actor':actor,'modules':{name:'a'*64},
        'unsupported_local_imports':[{'module':name,'line':13,'reason':'local_clock_import_unobserved'}],
        'capture_failures':{},'native_deadline_clocks':True,'global_datetime_and_pandas_unchanged':True,
        'concurrent_same_key':'unqualified_not_cross_assigned'}
    event={'boundary':'observed_clock','public_request':{'module':name,'source_sha256':'a'*64,
        'actor':actor},'failure':None}
    if change=='budget_failure':receipt['capture_failures']={'clock_event_limit':1}
    elif change=='unsupported_event':event['failure']={'type':'UnsupportedClock','reason':'capture_unsupported'}
    elif change=='wrong_actor':event['public_request']['actor']={**actor,'source_sha256':'c'*64}
    elif change=='wrong_identity':receipt['run_identity_sha256']='d'*64
    elif change=='changed_global_clock':receipt['global_datetime_and_pandas_unchanged']=False
    if change!='missing_receipt':runner.private_json(tmp_path/'.clock-runtime-123.json',receipt)
    value=runner.clock_capture_coverage(tmp_path,[event])
    assert value['status']==expected
    if change=='budget_failure':assert value['capture_failures']=={'clock_event_limit':1}
    if change=='none':
        assert value['clock_events']==1 and value['failed_clock_events']==0
        assert value['potential_unobserved_local_imports']==[{'module':name,'line':13}]


def test_optional_clock_capture_failure_is_diagnostic_not_live_readiness_gap(tmp_path,monkeypatch):
    from tools.live_workflow import runner
    edge_summary_fixture(tmp_path)
    monkeypatch.setattr(runner,'clock_capture_coverage',lambda *args:{'status':'incomplete',
        'capture_failures':{'observed_clock_capture_unsupported':1}})
    result=runner.summarize(tmp_path)
    assert 'observed_clock_capture_incomplete' in result['diagnostic_coverage_gaps']
    assert 'observed_clock_capture_incomplete' not in result['coverage_gaps']
    assert result['recorded_boundary_coverage']['clock_capture']['capture_failures']=={
        'observed_clock_capture_unsupported':1}
    assert 'ordinary_session_workflow_unobserved' in result['coverage_gaps']
    assert 'multi_session_continuity_unobserved' in result['coverage_gaps']
    assert result['offline_checks_establish_live_readiness'] is False


def test_recorded_source_and_model_failures_remain_distinct_from_clock_diagnostics(tmp_path,monkeypatch):
    from tools.live_workflow import recorded_io,runner
    records=[
        {'event_id':'a','phase':'daily','boundary':'observed_clock','operation':'unsupported',
            'failure':{'type':'UnsupportedClock'}},
        {'event_id':'b','phase':'daily','boundary':'http_decoded','operation':'requests.send',
            'failure':{'type':'Timeout'}},
        {'event_id':'c','phase':'daily','boundary':'model_helper','operation':'call_analysis_model',
            'failure':{'type':'ModelDeadlineExceeded'},
            'public_request':{'role':'committee','model':'native-model','effort':'high'}},
    ]
    monkeypatch.setattr(recorded_io,'verify_tape',lambda root:records)
    value=runner.tape_coverage(tmp_path)
    assert value['observed_failures']==3
    assert value['observed_source_failures']==value['observed_model_failures']==value['observed_clock_failures']==1
    assert value['observed_source_model_failures']==2
