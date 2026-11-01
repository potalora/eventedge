"""Disposable, real Git/venv/shell infrastructure for scheduled acceptance."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import weakref

ROOT = Path(__file__).resolve().parents[2]
PROFILE = ROOT / 'tests/fixtures/scheduled_acceptance/production-profile.json'
ENVIRONMENT_PROOF = ROOT / 'tests/fixtures/scheduled_acceptance/production-environment.json'
ORIGINAL_SHA = 'cfd010bec8effa9936df71a9fe87f72eac9403c43219163998d78d917314673d'
GENERATION = 'gen_020'



def _profile_phase(function):
    """Optional CI timing evidence; retain failures without logging payloads."""
    @wraps(function)
    def measured(*args, **kwargs):
        destination = os.environ.get('EVENTEDGE_CI_PROFILE_REPORT')
        if not destination:
            return function(*args, **kwargs)
        started = time.perf_counter()
        result = None
        failed = True
        try:
            result = function(*args, **kwargs)
            failed = False
            return result
        finally:
            owner = args[0] if args else None
            audit = getattr(owner, 'audit', {})
            record = {'version': 1, 'phase': function.__name__,
                'elapsed_seconds': time.perf_counter()-started, 'raised': failed,
                'profile_variant': kwargs.get('profile_variant',audit.get('profile_variant'))}
            if function.__name__ == 'run':
                record['entrypoint'] = kwargs.get('entrypoint','controller')
                record['returncode'] = getattr(result,'returncode',None)
            # Append each bounded JSON record atomically; no test state is shared.
            descriptor = os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
            try:
                os.write(descriptor,(json.dumps(record,sort_keys=True)+'\n').encode())
            finally:
                os.close(descriptor)
    return measured

def sha(body):
    return hashlib.sha256(body).hexdigest()


def _json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + '\n')


def _write_bootstrap_hook(site, paths):
    # .pth executes before sitecustomize, whose name may resolve to a system file.
    # Wrap import as well as install: site.py otherwise suppresses hook errors.
    startup="""import os,sys
if 'EVENTEDGE_ACCEPTANCE_SCENARIO' in os.environ and not getattr(sys,'_eventedge_acceptance_installed',False):
 sys._eventedge_acceptance_installed=True
 try:
  from scheduled_acceptance.bootstrap import install
  install()
 except BaseException:
  import traceback
  traceback.print_exc()
  os._exit(90)
"""
    (site/'acceptance.pth').write_text('\n'.join([*paths,'import os; exec('+repr(startup)+')'])+'\n')


def _changes(before, after, path=()):
    if type(before) is dict and type(after) is dict:
        assert before.keys() == after.keys()
        return [row for key in before for row in _changes(before[key], after[key], path+(key,))]
    if type(before) is list and type(after) is list:
        assert len(before) == len(after)
        return [row for index, (a, b) in enumerate(zip(before, after)) for row in _changes(a, b, path+(index,))]
    return [] if before == after else [{'path': list(path), 'original': before, 'fixture': after}]


@dataclass
class Harness:
    repo: Path
    state: Path
    profile: Path
    audit: dict
    scenario_path: Path
    trace_path: Path
    env: dict
    commit: str

    @property
    def python(self):
        return self.repo/'.venv/bin/python'

    @_profile_phase
    def run(self, now, *, entrypoint='controller', fault=None, timeout=240, **options):
        instant = datetime.fromisoformat(now)
        assert instant.tzinfo is not None
        scenario = {'now': now, 'session': instant.astimezone(__import__('zoneinfo').ZoneInfo('America/New_York')).date().isoformat(),
                    'fault': fault, **options}
        _json(self.scenario_path, scenario)
        if entrypoint in ('daily', 'preflight'):
            name = 'daily_trading.sh' if entrypoint == 'daily' else 'preflight.sh'
            command = ['/bin/bash', str(self.repo / 'scripts' / name)]
        elif entrypoint == 'controller':
            command = [str(self.repo / '.venv/bin/python'), str(self.repo/'scripts/recover_runtime.py'), '--repo-root', str(self.repo)]
        elif entrypoint == 'governed':
            command = [str(self.python), str(self.repo/'scripts/run_generations.py'), 'preflight',
                       '--date',scenario['session'],'--preflight-mode','governed']
        else:
            raise ValueError(entrypoint)
        process = subprocess.Popen(command, cwd=self.repo, env=self.env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            stdout,stderr = process.communicate(timeout=timeout)
        except BaseException:
            # The controller creates its own child process groups. Reap only
            # recorded test-owned PIDs, verifying their current command before
            # killing, so even a failed acceptance cannot leak a stalled worker.
            pids = {row[key] for row in self.trace() for key in ('pid','ppid') if row.get(key)} | {process.pid}
            for pid in pids:
                probe = subprocess.run(['ps','-p',str(pid),'-o','command='],capture_output=True,text=True)
                if str(self.repo.parent) in probe.stdout:
                    try:
                        os.kill(pid,signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            process.communicate()
            raise
        return subprocess.CompletedProcess(command,process.returncode,stdout,stderr)

    def report(self, session):
        return json.loads((self.repo / 'docs/reports' / f'{session}-{GENERATION}-operational-report.json').read_text())

    @_profile_phase
    def readiness(self, session, *, sessions):
        """Run the public checker against the same disposable native evidence."""
        code = '''import json,sys
from datetime import date
from pathlib import Path
from tradingagents.strategies.orchestration.release_readiness import assess_generation
print(json.dumps(assess_generation(Path(sys.argv[1]),sys.argv[2],sys.argv[3],
    date.fromisoformat(sys.argv[4]),sessions=int(sys.argv[5]))))
'''
        result = subprocess.run([str(self.python), '-c', code, str(self.repo), GENERATION,
                                 self.commit, session, str(sessions)], cwd=self.repo, env=self.env,
                                capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout)

    def attempts(self):
        return [json.loads(path.read_text()) for path in sorted((self.repo/'data/logs/run_attempts').rglob('*.json'))]

    def trace(self):
        return [json.loads(line) for line in self.trace_path.read_text().splitlines()] if self.trace_path.exists() else []

    def accepted_screening(self,session):
        from tradingagents.strategies.orchestration.source_inputs import SourceInputStore
        records = [SourceInputStore.decode(path.read_text()) for path in (self.state/'source_inputs/screening').glob('*.json')]
        matches = [row['payload'] for row in records if row['identity']['session']==session]
        assert len(matches)==1
        return matches[0]

    @_profile_phase
    def effective_config_proof(self):
        """Bind the real worker config, including retained compliance policy."""
        if not self.scenario_path.exists():
            _json(self.scenario_path,{'now':'2026-10-14T22:00:00+00:00','session':'2026-10-14','fault':None})
        env = {**self.env,'EVENTEDGE_GENERATION_CONFIG_FILE':str(self.profile),
               'EVENTEDGE_GENERATION_CONFIG_SHA256':self.audit['fixture_sha256'],
               'AUTORESEARCH_STATE_DIR':str(self.state)}
        code = '''import json
from types import SimpleNamespace
from scripts.run_cohorts import _build_config
from tradingagents.strategies.orchestration.source_inputs import source_configuration_fingerprint
config=_build_config(SimpleNamespace(block_tickers=''))
print(json.dumps({'blocked_tickers':config['autoresearch']['blocked_tickers'],
                  'regulation_retrieval_policy':config['autoresearch']['regulation_retrieval_policy'],
                  'noaa_delivery_policy':config['autoresearch'].get('noaa_delivery_policy'),
                  'enrichment_capability_policy':config['autoresearch'].get('enrichment_capability_policy'),
                  'retired_enrichment_capabilities':config['autoresearch'].get('retired_enrichment_capabilities'),
                  'congress_audit_continuity_policy':config['autoresearch'].get('congress_audit_continuity_policy'),
                  'source_configuration':source_configuration_fingerprint(config)}))
'''
        result = subprocess.run([str(self.python),'-c',code],cwd=self.repo,env=env,
                                capture_output=True,text=True,timeout=20)
        assert result.returncode==0,result.stdout+result.stderr
        return json.loads(result.stdout)

    def book_rows(self, table):
        assert table in {'fills', 'cash_events', 'account_snapshots', 'staging_runs', 'order_intents', 'committee_decisions', 'session_execution_contexts', 'signals', 'intent_signals', 'policy_session_contexts'}
        result = {}
        for path in sorted(self.state.glob('horizon_*/portfolio.db')):
            with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as connection:
                connection.row_factory = sqlite3.Row
                result[path.parent.name] = [dict(row) for row in connection.execute('SELECT * FROM '+table)]
        return result


@_profile_phase
def create_harness(tmp_path, *, created_at='2026-10-14T00:00:00+00:00',
                   profile_variant='retained_production'):
    """Snapshot candidate bytes in a *separate* local Git repository.

    The snapshot commit has its own identity. No source-repository Git writes,
    remotes, user configuration, real credentials or production state are used.
    """
    stabilization_profiles = {'stabilization_v1': 'stabilization-v1.json',
                              'stabilization_v2': 'stabilization-v2.json',
                              'stabilization_v3': 'stabilization-v3.json'}
    stabilization = profile_variant in stabilization_profiles
    if profile_variant not in {'retained_production', 'government_copy_v2', 'government_copy_v3', 'fresh_generation_v3_v4', *stabilization_profiles}:
        raise ValueError('unsupported acceptance profile variant')
    base = Path(tmp_path).resolve() / 'scheduled'
    repo = base / 'repo'
    repo.mkdir(parents=True)
    for name in ('tradingagents', 'scripts', 'deploy'):
        shutil.copytree(ROOT/name, repo/name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copyfile(ROOT/'.gitignore', repo/'.gitignore')
    shutil.copyfile(ROOT/'uv.lock', repo/'uv.lock')
    # Fixture state must remain outside the committed code identity.
    with (repo/'.gitignore').open('a') as stream:
        stream.write('\n/data/\n/docs/reports/\n/.venv/\n/.worktrees/\n/.env\n')
    # The manager deliberately loads dotenv before launching frozen workers.
    # An explicit empty file prevents upward discovery of a host/project .env
    # when retained fixture artifacts live beneath a real repository.
    (repo/'.env').write_text('# Offline acceptance: no ambient dotenv configuration.\n')
    git_env = {key: value for key, value in os.environ.items() if key in ('PATH', 'TMPDIR', 'SYSTEMROOT')}
    git_env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_AUTHOR_NAME='Offline acceptance fixture', GIT_AUTHOR_EMAIL='fixture@example.invalid',
                   GIT_COMMITTER_NAME='Offline acceptance fixture', GIT_COMMITTER_EMAIL='fixture@example.invalid',
                   GIT_AUTHOR_DATE='2026-10-14T00:00:00+00:00', GIT_COMMITTER_DATE='2026-10-14T00:00:00+00:00')
    def git(*args):
        return subprocess.run(['git', '-c', 'core.hooksPath='+os.devnull, *args], cwd=repo,
                              env=git_env, check=True, capture_output=True, text=True).stdout.strip()
    git('init', '-b', 'fixture-base')
    git('add', '.')
    git('commit', '-m', 'Disposable acceptance source snapshot')
    commit = git('rev-parse', 'HEAD')
    worktree = repo/'.worktrees'/GENERATION
    git('worktree', 'add', '--detach', str(worktree), commit)
    state = repo/'data/generations'/GENERATION
    state.mkdir(parents=True)
    original_body = ((ROOT/'config/profiles'/stabilization_profiles[profile_variant])
                     if stabilization else PROFILE).read_bytes()
    original_sha = sha(original_body)
    if not stabilization:
        assert original_sha == ORIGINAL_SHA
    original = json.loads(original_body)
    candidate = deepcopy(original)
    if profile_variant in {'government_copy_v2', 'government_copy_v3', 'fresh_generation_v3_v4'}:
        assert candidate['autoresearch']['regulation_retrieval_policy'] == 'verified_government_copy_v1'
        candidate['autoresearch']['regulation_retrieval_policy'] = ('verified_government_copy_v3'
            if profile_variant == 'fresh_generation_v3_v4' else 'verified_' + profile_variant)
    if profile_variant == 'fresh_generation_v3_v4':
        scope = candidate['autoresearch']['bounded_document_registry'][0]['decision_scope']
        scope['purpose'] = "Verify only the historical pledge security counts printed on selected page 18 for Cadre Holdings, Inc. as of December 16, 2021, preserving each count's printed unit, row and column association. This scope establishes historical count context only; it makes no claim about current holdings, current catalysts or present trading relevance."
    policy_overrides = _changes(original, candidate)
    if profile_variant == 'fresh_generation_v3_v4':
        field = 'bounded_document_recognition_request_version'
        assert field not in original['autoresearch']
        candidate['autoresearch'][field] = 'bounded-document-recognition-request-v4'
        policy_overrides.append({'path':['autoresearch',field], 'original':None,
                                 'fixture':candidate['autoresearch'][field], 'added':True})
        field = 'model_runtime_policy'
        assert field not in original['autoresearch']
        candidate['autoresearch'][field] = 'full_regulation_graphics_v1'
        policy_overrides.append({'path':['autoresearch',field], 'original':None,
                                 'fixture':candidate['autoresearch'][field], 'added':True})
    candidate_body = (json.dumps(candidate, sort_keys=True, indent=2) + '\n').encode()
    candidate_sha = sha(candidate_body) if policy_overrides else original_sha
    environment_body = ENVIRONMENT_PROOF.read_bytes()
    retained_environment = json.loads(environment_body)
    assert retained_environment['version']=='retained-production-environment-v1'
    assert retained_environment['environment']=={'BLOCKED_TICKERS':'GOOGL,GOOG'}
    normalized = deepcopy(candidate)
    ar = normalized['autoresearch']
    # Auth storage refuses *any* Git ancestor, including an artifact basetemp
    # nested in the real checkout. Keep the dummy grant in its own 0700 OS-temp
    # directory and remove it when this harness is released (also at exit).
    private = Path(tempfile.mkdtemp(prefix='eventedge-acceptance-auth-')).resolve()
    auth_path = private/'plan-auth.json'
    _json(auth_path, {'version': 1, 'issuer': 'https://auth.openai.com',
        'host_id': 'urn:uuid:11111111-1111-4111-8111-111111111111',
        'client_id': 'oaiapp_offline_acceptance', 'subject': 'constructed-acceptance-subject',
        'profile_identity': '22222222-2222-4222-8222-222222222222',
        'access_token': 'constructed-access-token', 'refresh_token': 'constructed-refresh-token',
        'id_token': 'constructed-id-token', 'token_type': 'Bearer', 'expires_in': 604800,
        'expires_at': 4102444800,
        'scope': 'openid profile email offline_access resource.invoke chatgpt.tokens.use.direct'})
    auth_path.chmod(0o600)
    ar['chatgpt_plan_auth_path'] = str(auth_path)
    if stabilization:
        # Freeze the same inert contact header used by the external HTTP fixture;
        # reporting must reconstruct the effective source configuration exactly.
        ar['edgar_user_agent'] = 'Offline acceptance fixture fixture@example.invalid'
    for target, key, suffix in ((normalized, 'project_dir', ''), (normalized, 'results_dir', 'data/results'),
            (normalized, 'data_cache_dir', 'data/dataflows_cache'), (ar, 'results_dir', 'data/results/bounded_documents'),
            (ar, 'source_cache_dir', 'data/source_cache')):
        target[key] = str(repo/suffix)
    # Synthetic bytes are not described as the retained original or native OCR.
    original_bytes = b'%PDF-1.7\nconstructed scheduled acceptance document fixture\n%%EOF\n'
    image_bytes = b'\x89PNG\r\n\x1a\nconstructed scheduled acceptance selected page fixture'
    if ar['bounded_document_registry']:
        document = ar['bounded_document_registry'][0]
        originals = [(document['original'], original_bytes), (document['attachments'][0], image_bytes)]
        for spec, body in originals:
            target = repo/'data/document-inputs'/spec['filename']
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
            spec.update(path=str(target), sha256=sha(body))
        document['attachments'][0]['source_sha256'] = sha(original_bytes)
        document['decision_scope']['original_sha256'] = sha(original_bytes)
    substitutions = _changes(candidate, normalized)
    allowed_paths = {('project_dir',), ('results_dir',), ('data_cache_dir',),
        ('autoresearch', 'results_dir'), ('autoresearch', 'source_cache_dir'), ('autoresearch', 'chatgpt_plan_auth_path'),
        ('autoresearch', 'bounded_document_registry', 0, 'original', 'path'),
        ('autoresearch', 'bounded_document_registry', 0, 'original', 'sha256'),
        ('autoresearch', 'bounded_document_registry', 0, 'attachments', 0, 'path'),
        ('autoresearch', 'bounded_document_registry', 0, 'attachments', 0, 'sha256'),
        ('autoresearch', 'bounded_document_registry', 0, 'attachments', 0, 'source_sha256'),
        ('autoresearch', 'bounded_document_registry', 0, 'decision_scope', 'original_sha256')}
    if stabilization:
        allowed_paths = {path for path in allowed_paths if 'bounded_document_registry' not in path}
        allowed_paths.add(('autoresearch', 'edgar_user_agent'))
    assert {tuple(row['path']) for row in substitutions} == allowed_paths
    profile = state/'generation-config.json'
    _json(profile, normalized)
    profile_sha = sha(profile.read_bytes())
    audit = {'original_sha256': original_sha, 'fixture_sha256': profile_sha,
             'profile_variant':profile_variant, 'policy_overrides':policy_overrides,
             'candidate_profile_sha256':candidate_sha,
             'fixture_commit': commit, 'substitutions': substitutions,
             # Runtime policy is separately evidenced, not a profile edit or
             # an invented external transport response.
             'retained_environment_policy':retained_environment,
             'retained_environment_fixture_sha256':sha(environment_body),
             'dotenv_isolation':'explicit empty disposable repository .env prevents ancestor discovery',
             'source_files_sha256': {str(path.relative_to(repo)):sha(path.read_bytes())
                for folder in ('tradingagents','scripts','deploy') for path in sorted((repo/folder).rglob('*')) if path.is_file()},
             'external_fixtures': ['requests HTTP responses', 'Yahoo SDK download and earnings dates',
                'OpenBB/FINRA SDK responses and cache with declared external SDK versions',
                'inert cot_reports availability module; real native archive parser',
                'HTTPX/HTTPX2 physical SSE response', 'synthetic plan-auth grant', 'synthetic local document bytes'],
             'scope': 'constructed external IO and document bytes; real internal shell/clock/ledger pipeline'}
    if stabilization:
        audit['external_fixtures'].remove('synthetic local document bytes')
        audit['scope'] = 'constructed external IO; exact stabilization profile; real internal shell/clock/ledger pipeline'
    _json(base/'substitution-audit.json', audit)
    _json(repo/'data/generations/manifest.json', {'generations': [{
        'gen_id': GENERATION, 'git_commit': commit, 'git_branch': 'fixture-base',
        'worktree_path': str(worktree), 'state_dir': str(state), 'created_at': created_at,
        'status': 'active', 'description': 'Disposable scheduled acceptance; independent fixture identity',
        'run_history': [], 'config_file': str(profile), 'config_sha256': profile_sha}]})
    # Use local installed dependency directories; no downloads or editable install.
    source_python = ROOT/'.venv/bin/python'
    if not source_python.exists():
        source_python = Path(sys.executable)
    subprocess.run([str(source_python), '-m', 'venv', '--without-pip', str(repo/'.venv')], check=True, capture_output=True)
    dependency_paths = json.loads(subprocess.run([str(source_python), '-c',
        'import json,sys; print(json.dumps([p for p in sys.path if "site-packages" in p]))'],
        check=True, capture_output=True, text=True).stdout)
    python = repo/'.venv/bin/python'
    sites = json.loads(subprocess.run([str(python), '-c', 'import json,site; print(json.dumps(site.getsitepackages()))'],
                                     check=True, capture_output=True, text=True).stdout)
    site = Path(sites[0])
    _write_bootstrap_hook(site,[*dependency_paths,str(ROOT/'tests')])
    scenario = base/'scenario.json'
    trace = base/'transport.jsonl'
    shim = base/'bin'
    shim.mkdir()
    date_shim = shim/'date'
    date_shim.write_text('#!'+str(python)+'\nimport os,json,sys\nfrom datetime import datetime\nfrom zoneinfo import ZoneInfo\n'
        'now=datetime.fromisoformat(json.load(open(os.environ["EVENTEDGE_ACCEPTANCE_SCENARIO"]))["now"]).astimezone(ZoneInfo(os.environ.get("TZ","America/New_York")))\n'
        'print(now.strftime(sys.argv[1][1:]) if len(sys.argv)>1 and sys.argv[1].startswith("+") else now.isoformat())\n')
    date_shim.chmod(0o755)
    runtime_tmp = base/'tmp'
    runtime_tmp.mkdir()
    # No inherited HOME, API/account credentials, proxy or dotenv settings.
    env = {'PATH': str(shim)+os.pathsep+os.defpath, 'HOME': str(private),
        'TMPDIR': str(runtime_tmp), 'LANG': 'C.UTF-8',
        'EVENTEDGE_ACCEPTANCE_SCENARIO': str(scenario), 'EVENTEDGE_ACCEPTANCE_TRACE': str(trace),
        'EVENTEDGE_ACCEPTANCE_WORKTREE': str(worktree), 'EVENTEDGE_ACCEPTANCE_BASE': str(base),
        'EVENTEDGE_PYTHON': str(python), 'PYTHONDONTWRITEBYTECODE': '1',
        'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
        'ALPACA_API_KEY': 'offline-dummy', 'ALPACA_SECRET_KEY': 'offline-dummy',
        'FINNHUB_API_KEY': 'offline-dummy', 'FRED_API_KEY': 'offline-dummy',
        'REGULATIONS_API_KEY': 'offline-dummy', 'COURTLISTENER_TOKEN': 'offline-dummy',
        'USDA_NASS_API_KEY': 'offline-dummy', 'FMP_API_KEY': 'offline-dummy',
        'EDGAR_USER_AGENT': 'Offline acceptance fixture fixture@example.invalid',
        'BLOCKED_TICKERS':retained_environment['environment']['BLOCKED_TICKERS']}
    harness = Harness(repo, state, profile, audit, scenario, trace, env, commit)
    weakref.finalize(harness,shutil.rmtree,private,True)
    return harness
