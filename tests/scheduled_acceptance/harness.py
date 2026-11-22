"""Disposable, real Git/venv/shell infrastructure for scheduled acceptance."""
from __future__ import annotations

import atexit
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
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
import weakref

ROOT = Path(__file__).resolve().parents[2]
PROFILE = ROOT / 'config/profiles/gen003-clean.json'
ORIGINAL_SHA = hashlib.sha256(PROFILE.read_bytes()).hexdigest()
GENERATION = 'gen_020'

_BOOK_TABLES = frozenset({
    'fills',
    'cash_events',
    'account_snapshots',
    'staging_runs',
    'order_intents',
    'committee_decisions',
    'session_execution_contexts',
    'signals',
    'intent_signals',
    'policy_session_contexts',
    'policy_audit_records',
})


def sha(body):
    return hashlib.sha256(body).hexdigest()


def _json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + '\n')


def _write_bootstrap_hook(site, paths):
    # .pth executes before sitecustomize, whose name may resolve to a system file.
    # Wrap import as well as install: site.py otherwise suppresses hook errors.
    startup = """import os,sys
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
    (site / 'acceptance.pth').write_text('\n'.join([*paths, 'import os; exec(' + repr(startup) + ')']) + '\n')


def _changes(before, after, path=()):
    if type(before) is dict and type(after) is dict:
        assert before.keys() == after.keys()
        return [row for key in before for row in _changes(before[key], after[key], path + (key,))]
    if type(before) is list and type(after) is list:
        assert len(before) == len(after)
        return [row for index, (a, b) in enumerate(zip(before, after)) for row in _changes(a, b, path + (index,))]
    return [] if before == after else [{'path': list(path), 'original': before, 'fixture': after}]


def _git_env():
    env = {key: value for key, value in os.environ.items() if key in ('PATH', 'TMPDIR', 'SYSTEMROOT')}
    env.update(
        GIT_CONFIG_NOSYSTEM='1',
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_AUTHOR_NAME='Offline acceptance fixture',
        GIT_AUTHOR_EMAIL='fixture@example.invalid',
        GIT_COMMITTER_NAME='Offline acceptance fixture',
        GIT_COMMITTER_EMAIL='fixture@example.invalid',
        GIT_AUTHOR_DATE='2026-10-14T00:00:00+00:00',
        GIT_COMMITTER_DATE='2026-10-14T00:00:00+00:00',
    )
    return env


@lru_cache(maxsize=1)
def _session_template():
    """Build ONE disposable template git repo + shared lightweight venv per session."""
    tpl_root = Path(tempfile.mkdtemp(prefix='eventedge-acceptance-tpl-')).resolve()
    atexit.register(shutil.rmtree, tpl_root, True)

    tpl_repo = tpl_root / 'repo'
    tpl_repo.mkdir(parents=True)
    for name in ('tradingagents', 'scripts', 'deploy', 'config'):
        shutil.copytree(ROOT / name, tpl_repo / name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copyfile(ROOT / '.gitignore', tpl_repo / '.gitignore')
    shutil.copyfile(ROOT / 'uv.lock', tpl_repo / 'uv.lock')
    # Fixture state must remain outside the committed code identity.
    with (tpl_repo / '.gitignore').open('a') as stream:
        stream.write('\n/data/\n/docs/reports/\n/.venv/\n/.worktrees/\n/.env\n')
    # The manager deliberately loads dotenv before launching frozen workers.
    # An explicit empty file prevents upward discovery of a host/project .env
    # when retained fixture artifacts live beneath a real repository.
    (tpl_repo / '.env').write_text('# Offline acceptance: no ambient dotenv configuration.\n')

    git_env = _git_env()

    def git(*args):
        return subprocess.run(
            ['git', '-c', 'core.hooksPath=' + os.devnull, *args],
            cwd=tpl_repo,
            env=git_env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git('init', '-b', 'fixture-base')
    git('add', '.')
    git('commit', '-m', 'Disposable acceptance source snapshot')
    commit = git('rev-parse', 'HEAD')

    source_files_sha256 = {
        str(path.relative_to(tpl_repo)): sha(path.read_bytes())
        for folder in ('tradingagents', 'scripts', 'deploy', 'config')
        for path in sorted((tpl_repo / folder).rglob('*'))
        if path.is_file()
    }

    source_python = ROOT / '.venv/bin/python'
    if not source_python.exists():
        source_python = Path(sys.executable)
    tpl_venv = tpl_root / 'venv'
    subprocess.run([str(source_python), '-m', 'venv', '--without-pip', str(tpl_venv)], check=True, capture_output=True)
    dependency_paths = json.loads(
        subprocess.run(
            [str(source_python), '-c', 'import json,sys; print(json.dumps([p for p in sys.path if "site-packages" in p]))'],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    tpl_python = tpl_venv / 'bin/python'
    sites = json.loads(
        subprocess.run(
            [str(tpl_python), '-c', 'import json,site; print(json.dumps(site.getsitepackages()))'],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    _write_bootstrap_hook(Path(sites[0]), [*dependency_paths, str(ROOT / 'tests')])
    return tpl_repo, tpl_venv, commit, source_files_sha256


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
        return self.repo / '.venv/bin/python'

    def run(self, now, *, entrypoint='controller', fault=None, timeout=240, **options):
        instant = datetime.fromisoformat(now)
        assert instant.tzinfo is not None
        scenario = {
            'now': now,
            'session': instant.astimezone(__import__('zoneinfo').ZoneInfo('America/New_York')).date().isoformat(),
            'fault': fault,
            **options,
        }
        _json(self.scenario_path, scenario)
        if entrypoint in ('daily', 'preflight'):
            name = 'daily_trading.sh' if entrypoint == 'daily' else 'preflight.sh'
            command = ['/bin/bash', str(self.repo / 'scripts' / name)]
        elif entrypoint == 'controller':
            command = [str(self.python), str(self.repo / 'scripts/recover_runtime.py'), '--repo-root', str(self.repo)]
        elif entrypoint == 'governed':
            command = [
                str(self.python),
                str(self.repo / 'scripts/run_generations.py'),
                'preflight',
                '--date',
                scenario['session'],
                '--preflight-mode',
                'governed',
            ]
        else:
            raise ValueError(entrypoint)
        process = subprocess.Popen(
            command,
            cwd=self.repo,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except BaseException:
            # The controller creates its own child process groups. Reap only
            # recorded test-owned PIDs, verifying their current command before
            # killing, so even a failed acceptance cannot leak a stalled worker.
            pids = {row[key] for row in self.trace() for key in ('pid', 'ppid') if row.get(key)} | {process.pid}
            for pid in pids:
                probe = subprocess.run(['ps', '-p', str(pid), '-o', 'command='], capture_output=True, text=True)
                if str(self.repo.parent) in probe.stdout:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            process.communicate()
            raise
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

    def report(self, session):
        return json.loads((self.repo / 'docs/reports' / f'{session}-{GENERATION}-operational-report.json').read_text())

    def readiness(self, session, *, sessions):
        """Run the public checker against the same disposable native evidence."""
        code = '''import json,sys
from datetime import date
from pathlib import Path
from tradingagents.strategies.orchestration.release_readiness import assess_generation
print(json.dumps(assess_generation(Path(sys.argv[1]),sys.argv[2],sys.argv[3],
    date.fromisoformat(sys.argv[4]),sessions=int(sys.argv[5]))))
'''
        result = subprocess.run(
            [str(self.python), '-c', code, str(self.repo), GENERATION, self.commit, session, str(sessions)],
            cwd=self.repo,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout)

    def attempts(self):
        return [json.loads(path.read_text()) for path in sorted((self.repo / 'data/logs/run_attempts').rglob('*.json'))]

    def trace(self):
        return [json.loads(line) for line in self.trace_path.read_text().splitlines()] if self.trace_path.exists() else []

    def accepted_screening(self, session):
        from tradingagents.strategies.orchestration.source_inputs import SourceInputStore

        records = [
            SourceInputStore.decode(path.read_text())
            for path in (self.state / 'source_inputs/screening').glob('*.json')
        ]
        matches = [row['payload'] for row in records if row['identity']['session'] == session]
        assert len(matches) == 1
        return matches[0]

    def effective_config_proof(self):
        """Bind the real worker config from the frozen gen003-clean profile."""
        if not self.scenario_path.exists():
            _json(self.scenario_path, {'now': '2026-10-14T22:00:00+00:00', 'session': '2026-10-14', 'fault': None})
        env = {
            **self.env,
            'EVENTEDGE_GENERATION_CONFIG_FILE': str(self.profile),
            'EVENTEDGE_GENERATION_CONFIG_SHA256': self.audit['fixture_sha256'],
            'AUTORESEARCH_STATE_DIR': str(self.state),
        }
        code = '''import json
from types import SimpleNamespace
from scripts.run_cohorts import _build_config
from tradingagents.strategies.orchestration.source_inputs import source_configuration_fingerprint
config = _build_config(SimpleNamespace(block_tickers=''))
ar = config['autoresearch']
print(json.dumps({
    'blocked_tickers': ar['blocked_tickers'],
    'enabled_strategies': ar['enabled_strategies'],
    'thesis_model': ar['thesis_model'],
    'portfolio_committee_model': ar['paper_trade']['portfolio_committee_model'],
    'source_configuration': source_configuration_fingerprint(config),
}))
'''
        result = subprocess.run([str(self.python), '-c', code], cwd=self.repo, env=env, capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout)

    def book_rows(self, table):
        assert table in _BOOK_TABLES
        from tradingagents.strategies.orchestration._io import CANONICAL_COHORTS

        unified = self.state / 'generation.sqlite3'
        if not unified.is_file():
            return {}
        result = {}
        with sqlite3.connect(f'file:{unified}?mode=ro', uri=True) as connection:
            connection.row_factory = sqlite3.Row
            for cohort_id in CANONICAL_COHORTS:
                if table == 'policy_session_contexts':
                    rows = connection.execute(
                        "SELECT *, payload_json AS context_json FROM policy_audit_records "
                        "WHERE cohort_id=? AND record_type='policy_session_context'",
                        (cohort_id,),
                    ).fetchall()
                elif table == 'intent_signals':
                    rows = connection.execute(
                        "SELECT i.* FROM intent_signals i "
                        "JOIN order_intents o ON o.intent_id = i.intent_id "
                        "WHERE o.cohort_id = ?",
                        (cohort_id,),
                    ).fetchall()
                elif table == 'signals':
                    rows = connection.execute(
                        "SELECT DISTINCT s.* FROM signals s "
                        "JOIN intent_signals i ON i.signal_id = s.signal_id "
                        "JOIN order_intents o ON o.intent_id = i.intent_id "
                        "WHERE o.cohort_id = ?",
                        (cohort_id,),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        f'SELECT * FROM {table} WHERE cohort_id = ?',
                        (cohort_id,),
                    ).fetchall()
                result[cohort_id] = [dict(row) for row in rows]
        return result


def create_harness(tmp_path, *, created_at='2026-10-14T00:00:00+00:00'):
    """Instantiate an isolated disposable repo/worktree/venv from session template."""
    tpl_repo, tpl_venv, commit, source_files_sha256 = _session_template()
    base = Path(tmp_path).resolve() / 'scheduled'
    repo = base / 'repo'
    shutil.copytree(tpl_repo, repo, symlinks=True)
    worktree = repo / '.worktrees' / GENERATION
    subprocess.run(
        ['git', '-c', 'core.hooksPath=' + os.devnull, 'worktree', 'add', '--detach', str(worktree), commit],
        cwd=repo,
        env=_git_env(),
        check=True,
        capture_output=True,
        text=True,
    )
    shutil.copytree(tpl_venv, repo / '.venv', symlinks=True)

    state = repo / 'data/generations' / GENERATION
    state.mkdir(parents=True)

    original_body = PROFILE.read_bytes()
    original_sha = sha(original_body)
    assert original_sha == ORIGINAL_SHA
    original = json.loads(original_body)
    normalized = deepcopy(original)

    # Auth storage refuses *any* Git ancestor, including an artifact basetemp
    # nested in the real checkout. Keep the dummy grant in its own 0700 OS-temp
    # directory and remove it when this harness is released (also at exit).
    private = Path(tempfile.mkdtemp(prefix='eventedge-acceptance-auth-')).resolve()
    auth_path = private / 'plan-auth.json'
    _json(
        auth_path,
        {
            'version': 1,
            'issuer': 'https://auth.openai.com',
            'host_id': 'urn:uuid:11111111-1111-4111-8111-111111111111',
            'client_id': 'oaiapp_offline_acceptance',
            'subject': 'constructed-acceptance-subject',
            'profile_identity': '22222222-2222-4222-8222-222222222222',
            'access_token': 'constructed-access-token',
            'refresh_token': 'constructed-refresh-token',
            'id_token': 'constructed-id-token',
            'token_type': 'Bearer',
            'expires_in': 604800,
            'expires_at': 4102444800,
            'scope': 'openid profile email offline_access resource.invoke chatgpt.tokens.use.direct',
        },
    )
    auth_path.chmod(0o600)
    normalized['chatgpt_plan_auth_path'] = str(auth_path)
    substitutions = _changes(original, normalized)
    assert {tuple(row['path']) for row in substitutions} == {('chatgpt_plan_auth_path',)}

    profile = state / 'generation-config.json'
    _json(profile, normalized)
    profile_sha = sha(profile.read_bytes())

    audit = {
        'original_sha256': original_sha,
        'fixture_sha256': profile_sha,
        'profile_variant': 'gen003_clean',
        'policy_overrides': [],
        'candidate_profile_sha256': original_sha,
        'fixture_commit': commit,
        'substitutions': substitutions,
        'dotenv_isolation': 'explicit empty disposable repository .env prevents ancestor discovery',
        'source_files_sha256': source_files_sha256,
        'external_fixtures': [
            'requests HTTP responses',
            'Yahoo SDK download and earnings dates',
            'inert cot_reports availability module; real native archive parser',
            'HTTPX/HTTPX2 physical SSE response',
            'synthetic plan-auth grant',
        ],
        'scope': 'constructed external IO; exact gen003-clean profile; real internal shell/clock/ledger pipeline',
    }
    _json(base / 'substitution-audit.json', audit)
    _json(
        repo / 'data/generations/manifest.json',
        {
            'generations': [
                {
                    'gen_id': GENERATION,
                    'git_commit': commit,
                    'git_branch': 'fixture-base',
                    'worktree_path': str(worktree),
                    'state_dir': str(state),
                    'created_at': created_at,
                    'status': 'active',
                    'description': 'Disposable scheduled acceptance; gen003-clean identity',
                    'run_history': [],
                    'config_file': str(profile),
                    'config_sha256': profile_sha,
                }
            ]
        },
    )

    python = repo / '.venv/bin/python'
    scenario = base / 'scenario.json'
    trace = base / 'transport.jsonl'
    shim = base / 'bin'
    shim.mkdir()
    date_shim = shim / 'date'
    date_shim.write_text(
        '#!'
        + str(python)
        + '\nimport os,json,sys\nfrom datetime import datetime\nfrom zoneinfo import ZoneInfo\n'
        'now=datetime.fromisoformat(json.load(open(os.environ["EVENTEDGE_ACCEPTANCE_SCENARIO"]))["now"]).astimezone(ZoneInfo(os.environ.get("TZ","America/New_York")))\n'
        'print(now.strftime(sys.argv[1][1:]) if len(sys.argv)>1 and sys.argv[1].startswith("+") else now.isoformat())\n'
    )
    date_shim.chmod(0o755)
    runtime_tmp = base / 'tmp'
    runtime_tmp.mkdir()
    # No inherited HOME, API/account credentials, proxy or dotenv settings.
    env = {
        'PATH': str(shim) + os.pathsep + os.defpath,
        'HOME': str(private),
        'TMPDIR': str(runtime_tmp),
        'LANG': 'C.UTF-8',
        'EVENTEDGE_ACCEPTANCE_SCENARIO': str(scenario),
        'EVENTEDGE_ACCEPTANCE_TRACE': str(trace),
        'EVENTEDGE_ACCEPTANCE_WORKTREE': str(worktree),
        'EVENTEDGE_ACCEPTANCE_BASE': str(base),
        'EVENTEDGE_PYTHON': str(python),
        'PYTHONDONTWRITEBYTECODE': '1',
        'GIT_CONFIG_NOSYSTEM': '1',
        'GIT_CONFIG_GLOBAL': os.devnull,
        'ALPACA_API_KEY': 'offline-dummy',
        'ALPACA_SECRET_KEY': 'offline-dummy',
        'FINNHUB_API_KEY': 'offline-dummy',
        'FRED_API_KEY': 'offline-dummy',
        'USDA_NASS_API_KEY': 'offline-dummy',
        'NOAA_CDO_TOKEN': 'offline-dummy',
        'FMP_API_KEY': 'offline-dummy',
        'BLOCKED_TICKERS': ','.join(normalized['blocked_tickers']),
    }
    harness = Harness(repo, state, profile, audit, scenario, trace, env, commit)
    weakref.finalize(harness, shutil.rmtree, private, True)
    return harness
