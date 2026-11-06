"""Frozen generation profiles cannot change policy or credential/state ownership."""
import copy
import hashlib
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_generation_manager import git_repo, manager, _valid_daily_stdout


def loader():
    name = 'tradingagents.strategies.orchestration.generation_config'
    assert importlib.util.find_spec(name) is not None, 'bounded generation profile loader is missing'
    return importlib.import_module(name)


@pytest.fixture
def profile(tmp_path):
    path = tmp_path / 'profile.json'
    path.write_bytes(b'{\n "autoresearch": {"llm_billing_mode":"chatgpt_plan", "paper_ledger":{"policy_id":"reduced"}, "filing_research_policy":"disabled_v1"}\n}\n')
    return path


def test_loader_merges_without_mutating_defaults_and_binds_exact_bytes(profile):
    defaults = {'autoresearch': {'state_dir': '/owned', 'paper_ledger': {'existing': True}}}
    original = copy.deepcopy(defaults)
    digest = hashlib.sha256(profile.read_bytes()).hexdigest()
    merged = loader().apply_generation_config(defaults, profile, digest)
    assert defaults == original
    assert merged['autoresearch']['paper_ledger'] == {'existing': True, 'policy_id': 'reduced-generation-config-' + digest}
    assert merged['autoresearch']['state_dir'] == '/owned'
    assert merged['autoresearch']['filing_research_policy'] == 'disabled_v1'


@pytest.mark.parametrize('body', [b'{} trailing', b'[]', b'{"a":1,"a":2}',
    b'{"a":NaN}', b'{"a":1e999}', b'{"a":{"b":1,"b":2}}', b'\xff',
    b'{"a":"' + b'x' * 1048576 + b'"}', b'{"autoresearch":{"state_dir":"/unowned"}}',
    b'{"autoresearch":{"fred_api_key":"secret"}}', b'{"nested":{"Authorization":"Bearer secret"}}',
    b'{"nested":{"refresh_token":"secret"}}', b'{"extra":"sk-proj-private"}',
    b'[' * 1500 + b'0' + b']' * 1500], ids=['trailing', 'array', 'duplicate', 'nan', 'overflow',
    'nested_duplicate', 'utf8', 'oversize', 'state_override', 'api_key', 'authorization', 'refresh_token',
    'secret_value', 'depth'])
def test_invalid_or_secret_profile_is_rejected(tmp_path, body):
    path = tmp_path / 'bad.json'; path.write_bytes(body)
    with pytest.raises(ValueError, match='generation_config'):
        loader().read_generation_config(path)


def test_missing_symlink_parent_symlink_and_changed_profile_fail(tmp_path, profile):
    module = loader(); digest = hashlib.sha256(profile.read_bytes()).hexdigest()
    link = tmp_path / 'link.json'; link.symlink_to(profile)
    real = tmp_path / 'real'; real.mkdir(); (real / 'profile.json').write_bytes(profile.read_bytes())
    alias = tmp_path / 'alias'; alias.symlink_to(real, target_is_directory=True)
    for path in (tmp_path / 'missing.json', link, alias / 'profile.json'):
        with pytest.raises(ValueError, match='generation_config'):
            module.read_generation_config(path)
    profile.write_bytes(profile.read_bytes() + b' ')
    with pytest.raises(ValueError, match='generation_config'):
        module.read_generation_config(profile, digest)


def test_auth_path_is_allowed_and_profile_digest_creates_policy_identity(tmp_path):
    path = tmp_path / 'profile.json'; path.write_text('{"autoresearch":{"chatgpt_plan_auth_path":"/private/auth.json"}}')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    result = loader().apply_generation_config({}, path, digest)
    assert result['autoresearch']['chatgpt_plan_auth_path'] == '/private/auth.json'
    assert result['autoresearch']['paper_ledger']['policy_id'] == 'generation-config-' + digest


def test_retained_source_identity_fits_exact_one_mib_without_truncation(tmp_path):
    path = tmp_path / 'retained-profile.json'
    prefix = b'{"autoresearch":{"state_exposure_manifest":{"raw_submission_b64":"'
    suffix = b'"}}}'
    body = prefix + b'A' * (1048576 - len(prefix) - len(suffix)) + suffix
    path.write_bytes(body)
    frozen, overlay, digest = loader().read_generation_config(path)
    assert frozen == body
    assert len(overlay['autoresearch']['state_exposure_manifest']['raw_submission_b64']) > 822726
    assert digest == hashlib.sha256(body).hexdigest()


@pytest.mark.parametrize('missing', ['EVENTEDGE_GENERATION_CONFIG_FILE', 'EVENTEDGE_GENERATION_CONFIG_SHA256'])
def test_worker_rejects_partial_profile_identity(profile, monkeypatch, missing):
    from scripts import run_cohorts
    monkeypatch.setattr('dotenv.load_dotenv', lambda: None)
    monkeypatch.setenv('EVENTEDGE_GENERATION_CONFIG_FILE', str(profile))
    monkeypatch.setenv('EVENTEDGE_GENERATION_CONFIG_SHA256', hashlib.sha256(profile.read_bytes()).hexdigest())
    monkeypatch.delenv(missing)
    with pytest.raises(ValueError, match='generation_config'):
        run_cohorts._build_config(SimpleNamespace(block_tickers=''))


def test_start_freezes_exact_profile_and_legacy_manifest_stays_unchanged(manager, profile):
    original = profile.read_bytes()
    info = manager.start_generation('profiled', config_file=profile)
    assert Path(info.config_file).read_bytes() == original
    assert Path(info.config_file).parent == Path(info.state_dir)
    assert info.config_sha256 == hashlib.sha256(original).hexdigest()
    assert Path(info.config_file).stat().st_mode & 0o777 == 0o600
    profile.write_text('{}')
    assert Path(info.config_file).read_bytes() == original
    assert manager.get_generation(info.gen_id).config_sha256 == info.config_sha256
    legacy = manager.start_generation('legacy')
    row = next(row for row in json.loads(manager._manifest_path.read_text())['generations'] if row['gen_id'] == legacy.gen_id)
    assert 'config_file' not in row and 'config_sha256' not in row
    assert manager.get_generation(legacy.gen_id).config_file is None


def test_invalid_profile_does_not_create_generation(manager, tmp_path):
    path = tmp_path / 'bad.json'; path.write_text('{"password":"private"}')
    with pytest.raises(ValueError, match='generation_config'):
        manager.start_generation('bad', config_file=path)
    assert manager.list_generations() == []
    assert not (manager._worktrees_dir / 'gen_001').exists()


def test_manager_forwards_frozen_profile_and_clears_inherited_profile_for_legacy(manager, profile, monkeypatch):
    from tradingagents.strategies.orchestration import generation_manager as gm
    info = manager.start_generation('profiled', config_file=profile)
    legacy = manager.start_generation('legacy')
    seen = []
    def run(cmd, **options):
        seen.append(options['env'])
        return SimpleNamespace(stdout=_valid_daily_stdout(), stderr='', returncode=0)
    monkeypatch.setattr(gm, '_run_preflight_worker', run)
    monkeypatch.setenv('EVENTEDGE_GENERATION_CONFIG_FILE', '/unowned')
    monkeypatch.setenv('EVENTEDGE_GENERATION_CONFIG_SHA256', '0' * 64)
    rows = manager._load_manifest()['generations']
    for row in rows:
        result = manager._run_cohorts_subprocess(row, ['--date', '2026-10-09'])
        assert result['success'] is True
    assert seen[0]['EVENTEDGE_GENERATION_CONFIG_FILE'] == info.config_file
    assert seen[0]['EVENTEDGE_GENERATION_CONFIG_SHA256'] == info.config_sha256
    assert seen[0]['AUTORESEARCH_STATE_DIR'] == info.state_dir
    assert 'EVENTEDGE_GENERATION_CONFIG_FILE' not in seen[1]
    assert 'EVENTEDGE_GENERATION_CONFIG_SHA256' not in seen[1]


def test_corrupted_frozen_profile_fails_before_subprocess_and_records_history(manager, profile, monkeypatch):
    from tradingagents.strategies.orchestration import generation_manager as gm
    info = manager.start_generation('profiled', config_file=profile)
    Path(info.config_file).write_text('{}')
    monkeypatch.setattr(gm, '_run_preflight_worker', lambda *a, **k: pytest.fail('invalid profile must not launch worker'))
    results = manager.run_daily('2026-10-09')
    assert results[info.gen_id]['outcome'] == 'failed'
    assert 'generation_config' in results[info.gen_id]['error']
    assert manager.get_generation(info.gen_id).run_history[-1]['outcome'] == 'failed'


def test_worker_profile_keeps_environment_credentials_and_manager_state(profile, monkeypatch):
    from scripts import run_cohorts
    monkeypatch.setattr('dotenv.load_dotenv', lambda: None)
    monkeypatch.setenv('EVENTEDGE_GENERATION_CONFIG_FILE', str(profile))
    monkeypatch.setenv('EVENTEDGE_GENERATION_CONFIG_SHA256', hashlib.sha256(profile.read_bytes()).hexdigest())
    monkeypatch.setenv('AUTORESEARCH_STATE_DIR', '/manager/state')
    monkeypatch.setenv('FRED_API_KEY', 'environment-private')
    config = run_cohorts._build_config(SimpleNamespace(block_tickers=''))
    assert config['autoresearch']['state_dir'] == '/manager/state'
    assert config['autoresearch']['fred_api_key'] == 'environment-private'
    assert config['autoresearch']['filing_research_policy'] == 'disabled_v1'
    profile.write_text('{}')
    with pytest.raises(ValueError, match='generation_config'):
        run_cohorts._build_config(SimpleNamespace(block_tickers=''))


def test_frozen_worker_never_discovers_another_dotenv(profile, monkeypatch):
    from scripts import run_cohorts
    def discovered():
        monkeypatch.setenv('BLOCKED_TICKERS', 'SPY')
        monkeypatch.setenv('FRED_API_KEY', 'unfrozen-other-file')
    monkeypatch.setattr('dotenv.load_dotenv', discovered)
    monkeypatch.setenv('EVENTEDGE_GENERATION_CONFIG_FILE', str(profile))
    monkeypatch.setenv('EVENTEDGE_GENERATION_CONFIG_SHA256', hashlib.sha256(profile.read_bytes()).hexdigest())
    monkeypatch.delenv('BLOCKED_TICKERS', raising=False)
    monkeypatch.setenv('FRED_API_KEY', 'explicit-private-environment')
    config = run_cohorts._build_config(SimpleNamespace(block_tickers=''))
    assert not config['autoresearch'].get('blocked_tickers')
    assert config['autoresearch']['fred_api_key'] == 'explicit-private-environment'


def test_start_cli_accepts_optional_config_file(manager, profile, monkeypatch):
    from scripts import run_generations
    from tradingagents.strategies.orchestration import generation_manager as gm
    monkeypatch.setattr(gm, 'GenerationManager', lambda repo: manager)
    monkeypatch.setattr('dotenv.load_dotenv', lambda: None)
    monkeypatch.setattr('sys.argv', ['run_generations.py', 'start', 'profiled', '--config-file', str(profile)])
    run_generations._main()
    assert manager.list_generations()[0].config_sha256 == hashlib.sha256(profile.read_bytes()).hexdigest()


@pytest.mark.parametrize('status', ['success', 'runtime', 'malformed', 'timeout', 'launch_error'])
def test_preflight_attempt_binds_only_validated_profile(manager, profile, monkeypatch, status):
    """Operational resolution can require the exact frozen profile on every attempt."""
    from tradingagents.strategies.orchestration import generation_manager as gm
    info = manager.start_generation('bound preflight', config_file=profile)
    wire = {'ok':status == 'success', 'preflight_mode':'screen',
            'screen_ok':status == 'success', 'screen_failure_count':0 if status == 'success' else 1,
            'screen_source_failures':[], 'screen_runtime_failures':[] if status == 'success' else
                [{'phase':'acquisition','reason_code':'acquisition_failed'}]}
    def run(cmd, **options):
        if status == 'timeout':
            raise gm.subprocess.TimeoutExpired(cmd, 1)
        if status == 'launch_error':
            raise OSError('worker unavailable')
        return SimpleNamespace(stdout='invalid' if status == 'malformed' else json.dumps(wire, indent=2),
                               stderr='', returncode=0 if status == 'success' else 1)
    monkeypatch.setattr(gm, '_run_preflight_worker', run)
    row = manager._load_manifest()['generations'][0]
    result = manager._run_cohorts_subprocess(row, ['--date','2026-10-14','--preflight'],
                                           preflight_mode='screen', write_log=False)
    assert result['config_sha256'] == info.config_sha256
    artifact = json.loads(Path(result['evidence_path']).read_text())
    assert artifact['result']['config_sha256'] == info.config_sha256
    assert result['success'] is (status == 'success')
    Path(info.config_file).write_text('{}')
    rejected = manager._run_cohorts_subprocess(row, ['--date','2026-10-14','--preflight'],
                                              preflight_mode='screen', write_log=False)
    assert rejected['error'] == 'generation_config_invalid'
    assert 'config_sha256' not in rejected
    assert 'config_sha256' not in json.loads(Path(rejected['evidence_path']).read_text())['result']
