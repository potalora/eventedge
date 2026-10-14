"""Real acquisition clocks and bounded diagnostics; no external IO or decisions."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.scheduled_acceptance

from tradingagents.strategies.orchestration import multi_strategy_engine as engine_module
from tradingagents.strategies.orchestration.preflight import run_preflight
from tradingagents.strategies.orchestration.generation_manager import (
    normalize_preflight_report, _preflight_subprocess_result,
)

PROFILE = Path(__file__).resolve().parents[1] / 'tests/fixtures/scheduled_acceptance/production-profile.json'


def freeze(monkeypatch, value):
    instant = datetime.fromisoformat(value)
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)
    monkeypatch.setattr(engine_module, 'datetime', FixedDatetime)
    return instant


def local_profile(tmp_path):
    config = json.loads(PROFILE.read_text())
    # Filesystem substitutions only. All deployed behavioral policies survive.
    config['autoresearch']['state_dir'] = str(tmp_path / 'state')
    config['autoresearch']['source_cache_dir'] = str(tmp_path / 'cache')
    config['autoresearch']['results_dir'] = str(tmp_path / 'results')
    config['data_cache_dir'] = str(tmp_path / 'data-cache')
    config['results_dir'] = str(tmp_path / 'results')
    config['project_dir'] = str(tmp_path)
    return config


@pytest.mark.parametrize('instant', ['2026-10-14T15:45:00+00:00', '2026-10-14T22:00:00+00:00'])
def test_deployed_profile_screen_runs_real_acquisition_at_midday_and_evening(tmp_path, monkeypatch, instant):
    freeze(monkeypatch, instant)
    calls = []
    def no_transport(*args, **kwargs):
        calls.append('blocked')
        raise ConnectionError('offline fixture')
    monkeypatch.setattr(socket.socket, 'connect', no_transport)
    monkeypatch.setattr(socket.socket, 'connect_ex', no_transport)
    # A zero source budget is a transport-only failure fixture: all real adapters
    # remain installed but no queued provider operation may start.
    monkeypatch.setenv('AUTORESEARCH_FETCH_TIMEOUT_S', '0')
    report = run_preflight(local_profile(tmp_path), '2026-10-14', mode='screen')
    assert report['screen_runtime_failures'] == []
    assert report['fetched_sources']
    assert not calls
    # This case proves acquisition reached real adapters, not healthy coverage.
    assert report['screen_source_failures']


def test_acquisition_exception_is_a_normalizable_redacted_failure(tmp_path):
    def fail(*args, **kwargs):
        raise RuntimeError('PRIVATE credential URL')
    engine = SimpleNamespace(_fetch_all_data=fail, config={}, ar_config={})
    report = run_preflight({}, '2026-10-14', engine=engine)
    assert report['screen_ok'] is False
    assert report['screen_runtime_failures'] == [{'phase': 'acquisition', 'reason_code': 'acquisition_failed'}]
    wire = normalize_preflight_report(report, mode='screen', trading_date='2026-10-14')
    assert wire['screen_failure_count'] == 1
    assert 'PRIVATE' not in json.dumps(report)
    result = _preflight_subprocess_result(stdout=json.dumps(wire, indent=2), stderr='',
        returncode=1, elapsed=1, mode='screen', trading_date='2026-10-14')
    assert result['error'] == 'preflight screen runtime: acquisition_failed'
    assert result['screen_runtime_failures'] == wire['screen_runtime_failures']


@pytest.mark.parametrize('damage', [None, {}, [{'phase':'other','reason_code':'acquisition_failed'}],
    [{'phase':'screen','reason_code':'acquisition_failed'}],
    [{'phase':'acquisition','reason_code':'PRIVATE'}]])
def test_malformed_runtime_diagnostics_fail_closed(damage):
    report = {'ok':False,'screen_ok':False,'preflight_mode':'screen','screen_failure_count':1,
              'screen_source_failures':[], 'screen_runtime_failures':damage}
    assert normalize_preflight_report(report, mode='screen', trading_date='2026-10-14') is None


def test_profile_fixture_is_exact_deployed_bytes():
    import hashlib
    assert hashlib.sha256(PROFILE.read_bytes()).hexdigest() == 'cfd010bec8effa9936df71a9fe87f72eac9403c43219163998d78d917314673d'


def weather_engine(tmp_path):
    from tradingagents.strategies.data_sources.registry import DataSourceRegistry
    calls = []
    class WeatherTransport:
        name = 'noaa'
        def is_available(self):
            return True
        def fetch_ag_weather_summary(self, session, **options):
            calls.append((session, options))
            return {'observations': [], 'coverage': {'complete': True}}
    registry = DataSourceRegistry()
    registry.register(WeatherTransport())
    config = local_profile(tmp_path)
    engine = engine_module.MultiStrategyEngine(config, registry=registry, use_llm=False)
    return engine, calls


def test_diagnostic_keeps_current_vintage_without_decision_receipt(tmp_path, monkeypatch):
    freeze(monkeypatch, '2026-10-14T15:45:00+00:00')
    engine, calls = weather_engine(tmp_path)
    data = engine._fetch_all_data('2026-07-16', '2026-10-14', acquisition_purpose='screen_diagnostic')
    assert calls == [('2026-10-14', {'lookback_days': 30, 'vintage_as_of': '2026-10-14'})]
    assert '_decision_acquisition' not in data
    assert engine.config['autoresearch']['decision_clock_policy'] == 'prospective_next_open_v1'
    # Diagnostic cache cannot satisfy an ordinary future decision acquisition.
    freeze(monkeypatch, '2026-10-14T22:00:00+00:00')
    accepted = engine._fetch_all_data('2026-07-16', '2026-10-14')
    assert len(calls) == 2
    assert accepted['_decision_acquisition']['reference_session'] == '2026-10-14'
    from tradingagents.strategies.orchestration.source_inputs import SourceInputStore
    identities = [SourceInputStore.decode(path.read_text())['identity']
                  for path in (tmp_path / 'cache').glob('*.json')]
    assert len(identities) == 2
    assert identities[0]['configuration'] != identities[1]['configuration']
    assert identities[0]['window_start'] == identities[1]['window_start']
    assert identities[0]['session'] == identities[1]['session']


@pytest.mark.parametrize('session,instant,message', [
    ('2026-10-14', '2026-10-14T15:45:00+00:00', 'latest completed'),
    ('2026-10-13', '2026-10-14T15:45:00+00:00', 'next reference-session open'),
    ('2026-10-13', '2026-10-14T13:30:00+00:00', 'next reference-session open'),
])
def test_ordinary_prospective_fetch_preserves_real_reference_guard(tmp_path, monkeypatch, session, instant, message):
    freeze(monkeypatch, instant)
    engine, calls = weather_engine(tmp_path)
    with pytest.raises(ValueError, match=message):
        engine._fetch_all_data('2026-07-16', session)
    assert calls == []


def test_ordinary_prospective_fetch_still_accepts_before_next_open(tmp_path, monkeypatch):
    freeze(monkeypatch, '2026-10-14T13:29:59+00:00')
    engine, calls = weather_engine(tmp_path)
    data = engine._fetch_all_data('2026-07-16', '2026-10-13')
    assert data['_decision_acquisition']['vintage_as_of'] == '2026-10-14'
    assert calls[0][1]['vintage_as_of'] == '2026-10-14'


def test_screen_isolates_every_writable_path_and_preserves_caller(tmp_path, monkeypatch):
    freeze(monkeypatch, '2026-10-14T15:45:00+00:00')
    monkeypatch.setenv('AUTORESEARCH_FETCH_TIMEOUT_S', '0')
    config = local_profile(tmp_path)
    before = deepcopy(config)
    sentinel = tmp_path / 'accepted-generation-input'
    sentinel.write_bytes(b'IMMUTABLE')
    monkeypatch.setenv('EVENTEDGE_SOURCE_CACHE_DIR', str(tmp_path / 'inherited-cache'))
    observed = []
    original = engine_module.MultiStrategyEngine
    def observe(config, **options):
        observed.append(deepcopy(config))
        return original(config, **options)
    monkeypatch.setattr(engine_module, 'MultiStrategyEngine', observe)
    report = run_preflight(config, '2026-10-14')
    assert report['screen_runtime_failures'] == []
    assert config == before and sentinel.read_bytes() == b'IMMUTABLE'
    assert list(tmp_path.iterdir()) == [sentinel]
    isolated = observed[0]
    for key in ('state_dir', 'source_cache_dir', 'results_dir'):
        assert isolated['autoresearch'][key] != config['autoresearch'][key]
        assert not Path(isolated['autoresearch'][key]).exists()
    for key in ('project_dir', 'data_cache_dir', 'results_dir'):
        assert isolated[key] != config[key]
    assert isolated['autoresearch']['bounded_document_registry'] == config['autoresearch']['bounded_document_registry']
    assert isolated['autoresearch']['decision_clock_policy'] == 'prospective_next_open_v1'


@pytest.mark.parametrize('purpose,policy', [('typo','prospective_next_open_v1'), ('screen_diagnostic','typo')])
def test_diagnostic_does_not_ignore_invalid_purpose_or_clock_policy(tmp_path, purpose, policy):
    engine, calls = weather_engine(tmp_path)
    engine.config['autoresearch']['decision_clock_policy'] = policy
    with pytest.raises(ValueError):
        engine._fetch_all_data('2026-07-16', '2026-10-14', acquisition_purpose=purpose)
    assert calls == []


def test_legacy_wire_remains_idempotent_without_runtime_field():
    legacy = {'ok':True,'screen_ok':True,'preflight_mode':'screen','screen_failure_count':0,
              'screen_source_failures':[]}
    assert normalize_preflight_report(legacy, mode='screen', trading_date='2026-10-14') == legacy


def test_runtime_rows_cannot_coexist_with_success_or_duplicate():
    row = {'phase':'acquisition', 'reason_code':'acquisition_failed'}
    wire = {'ok':True,'screen_ok':True,'preflight_mode':'screen','screen_failure_count':0,
            'screen_source_failures':[], 'screen_runtime_failures':[row]}
    assert normalize_preflight_report(wire, mode='screen', trading_date='2026-10-14') is None
    wire.update(ok=False, screen_ok=False, screen_failure_count=2, screen_runtime_failures=[row,row])
    assert normalize_preflight_report(wire, mode='screen', trading_date='2026-10-14') is None


def test_source_and_runtime_failures_cannot_underreport_combined_count():
    wire = {'ok':False,'screen_ok':False,'preflight_mode':'screen','screen_failure_count':1,
        'screen_source_failures':[{'source':'fred','reason_code':'timeout','http_status':None,
                                   'attempts':1,'operation_count':1}],
        'screen_runtime_failures':[{'phase':'acquisition','reason_code':'acquisition_failed'}]}
    assert normalize_preflight_report(wire,mode='screen',trading_date='2026-10-14') is None


@pytest.mark.parametrize('timeout', [False, True])
def test_real_preflight_supervisor_preserves_streams_and_signal_handlers(tmp_path, timeout):
    import signal
    import subprocess
    import sys
    from tradingagents.strategies.orchestration.generation_manager import _run_preflight_worker
    previous = {signum:signal.getsignal(signum) for signum in (signal.SIGTERM,signal.SIGINT)}
    command = [sys.executable,'-c',
        "import sys,time; print('retained stdout',flush=True); print('retained stderr',file=sys.stderr,flush=True); "
        + ('time.sleep(60)' if timeout else '')]
    if timeout:
        with pytest.raises(subprocess.TimeoutExpired) as captured:
            _run_preflight_worker(command,cwd=tmp_path,env={},timeout=.2,pass_fds=())
        assert captured.value.stdout=='retained stdout\n'
        assert captured.value.stderr=='retained stderr\n'
    else:
        result = _run_preflight_worker(command,cwd=tmp_path,env={},timeout=5,pass_fds=())
        assert result.returncode==0
        assert result.stdout=='retained stdout\n' and result.stderr=='retained stderr\n'
    assert {signum:signal.getsignal(signum) for signum in previous}==previous
