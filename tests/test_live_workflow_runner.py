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
    from tools.live_workflow.runner import sanitized_environment
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
    config=tmp_path/'hook.json'; config.write_text(json.dumps({'candidate_repo':str(repo),'phase':'test'}))
    env['EVENTEDGE_LIVE_CONFIG']=str(config)
    # accepted-replay selects explicit no-IO installation rather than live hooks.
    proc=subprocess.run([str(python),'-c','import tradingagents; print(tradingagents.IDENTITY); print(tradingagents.__file__)',
        'accepted-replay'],env=env,cwd=repo,capture_output=True,text=True)
    assert proc.returncode == 0,proc.stderr
    assert proc.stdout.splitlines() == ['dummy-candidate',str(repo/'tradingagents/__init__.py')]


def test_no_io_guard_counts_forbidden_requests_without_network(monkeypatch):
    from collections import Counter
    from types import ModuleType,SimpleNamespace
    from tools.live_workflow.bootstrap import _forbid_io
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
    attempts=Counter(); _forbid_io(attempts)
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


def test_prepare_refuses_incompatible_python_before_any_application_call(candidate,tmp_path,monkeypatch):
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
        dependency_sha256=dependency_fingerprint(site),python=str(Path(sys.executable).resolve()),python_version='9.9',
        observer_root=None,observer_commit=None,native_root=None,native_commit=None)
    called=[]; monkeypatch.setattr('tools.live_workflow.runner._child',lambda *a,**kw:called.append(a))
    with pytest.raises(AcceptanceError,match='incompatible_runtime_python'):
        prepare(args)
    assert called == []


def test_prepare_allows_only_its_native_runtime_lock_among_new_operational_files(candidate,tmp_path):
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
        dependency_sha256=dependency_fingerprint(site),python=str(Path(sys.executable).resolve()),python_version='9.9',
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
    assert 'controlled_mutations_not_executed' in result['coverage_gaps']
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
    assert 'controlled_mutations_not_executed' not in result['coverage_gaps']
    assert 'controlled_mutations_unexercised' in result['coverage_gaps']
    assert 'controlled_mutations_failed' in result['coverage_gaps']
    assert 'cold_acquisition_replay_not_executed' in result['coverage_gaps']


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
    assert 'controlled_mutations_not_executed' in result['coverage_gaps']
    assert 'controlled_mutations_artifact_invalid' in result['coverage_gaps']


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
    assert 'controlled_mutations_not_executed' in result['coverage_gaps']


def test_summary_invalid_json_matrix_remains_visible_failure(tmp_path):
    from tools.live_workflow.runner import summarize
    edge_summary_fixture(tmp_path)
    (tmp_path/'edge-cases/matrix.json').write_text('{')
    result=summarize(tmp_path)
    assert result['controlled_mutations']['status']=='invalid'
    assert 'controlled_mutations_not_executed' in result['coverage_gaps']


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
