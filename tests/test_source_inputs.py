from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd
import pytest

from tradingagents.strategies.orchestration.source_inputs import (
    SourceInputError, SourceInputStore, cache_identity, configuration_fingerprint,
)

NOW = datetime(2026, 10, 6, 20, 30, tzinfo=timezone.utc)
IDENTITY = cache_identity('fred', '2026-07-08', '2026-10-06', 'config-a')
FROZEN = {'generation': 'gen_test', 'session': '2026-10-06', 'commit': 'abc', 'configuration': 'cfg'}


def test_codec_roundtrips_real_source_payload_and_nonfinite_frame():
    frame = pd.DataFrame({'Close': [3.5, float('nan')], 'count': [1, 2]},
                         index=pd.date_range('2026-10-01', periods=2, tz='UTC', name='session'))
    payload = {'frame': frame, 'series': pd.Series([1.2, float('inf')], name='price'),
               'decimal': Decimal('1.2300'), 'date': date(2026, 10, 2),
               'timestamp': pd.Timestamp('2026-10-02T20:00:00Z'), 'datetime': NOW,
               'tuple': (pd.NA, pd.NaT, float('-inf')), 'nested': [{'$type': 'untouched'}]}
    restored = SourceInputStore.decode(SourceInputStore.encode(payload))
    pd.testing.assert_frame_equal(restored['frame'], frame)
    pd.testing.assert_series_equal(restored['series'], payload['series'])
    for key in ('decimal', 'date', 'timestamp', 'datetime', 'nested'):
        assert restored[key] == payload[key]
    assert restored['tuple'][0] is pd.NA
    assert restored['tuple'][1] is pd.NaT
    assert restored['tuple'][2] == float('-inf')
    assert 'NaN' not in SourceInputStore.encode(payload)


def test_cache_identity_freshness_and_cutoff(tmp_path):
    store = SourceInputStore(tmp_path)
    assert store.save_cached(IDENTITY, {'observations': []}, acquired_at=NOW)
    assert store.load_cached(IDENTITY, now=NOW + timedelta(seconds=299), cutoff=NOW) == {'observations': []}
    assert store.load_cached(IDENTITY, now=NOW + timedelta(seconds=301), cutoff=NOW) is None
    assert store.load_cached(IDENTITY, now=NOW - timedelta(seconds=1), cutoff=NOW) is None
    assert store.load_cached(IDENTITY, now=NOW, cutoff=NOW - timedelta(seconds=1)) is None
    assert store.load_cached({**IDENTITY, 'session': '2026-10-05'}, now=NOW) is None


@pytest.mark.parametrize('payload', [{'error': 'timeout'}, {'observations': [1], 'error': 'partial'},
                                     {'_coverage': {'status': 'partial'}, 'observations': [1]}])
def test_failed_or_partial_source_never_cached(tmp_path, payload):
    store = SourceInputStore(tmp_path)
    assert not store.save_cached(IDENTITY, payload, acquired_at=NOW)
    assert store.load_cached(IDENTITY, now=NOW) is None


def test_corrupt_cache_rejected_and_frozen_corruption_visible(tmp_path):
    store = SourceInputStore(tmp_path / 'cache', accepted_dir=tmp_path / 'accepted')
    store.save_cached(IDENTITY, {}, acquired_at=NOW)
    next((tmp_path / 'cache').glob('*.json')).write_text('{bad')
    assert store.load_cached(IDENTITY, now=NOW) is None
    store.freeze(FROZEN, {'fred': {}}, acquired_at=NOW)
    next((tmp_path / 'accepted').glob('*.json')).write_text('{bad')
    with pytest.raises(SourceInputError, match='frozen'):
        store.load_frozen(FROZEN)


def test_frozen_bundle_reuses_exact_first_values_and_failures(tmp_path):
    store = SourceInputStore(tmp_path / 'cache', accepted_dir=tmp_path / 'accepted')
    original = {'fred': {'rate': Decimal('1')}, 'edgar': {'error': 'timeout'}}
    assert store.freeze(FROZEN, original, acquired_at=NOW) == original
    assert store.freeze(FROZEN, {'fred': {'rate': Decimal('2')}}, acquired_at=NOW) == original
    assert SourceInputStore(tmp_path / 'cache', accepted_dir=tmp_path / 'accepted').load_frozen(FROZEN) == original
    with pytest.raises(SourceInputError, match='identity'):
        store.load_frozen({**FROZEN, 'commit': 'different'})


def test_codec_rejects_unknown_tags_deep_and_oversized_documents():
    with pytest.raises(SourceInputError):
        SourceInputStore.decode('{"$type":"pickle","value":"unsafe"}')
    with pytest.raises(SourceInputError):
        SourceInputStore.decode('[' * 100 + '0' + ']' * 100)
    with pytest.raises(SourceInputError):
        SourceInputStore.encode(object())


def test_configuration_identity_is_stable_and_changes_with_provider_settings():
    assert configuration_fingerprint({'fred': {'window': 3}, 'api_key': 'secret'}) == configuration_fingerprint({'api_key': 'secret', 'fred': {'window': 3}})
    assert configuration_fingerprint({'fred': {'window': 3}}) != configuration_fingerprint({'fred': {'window': 4}})


def _engine(tmp_path, *, cache_dir=None):
    from types import SimpleNamespace
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    class Registry:
        def available_sources(self): return ['fred']
        def get(self, name): return None
    config = {'autoresearch': {'state_dir': str(tmp_path / 'state')}}
    if cache_dir is not None:
        config['autoresearch']['source_cache_dir'] = str(cache_dir)
    return MultiStrategyEngine(config=config, registry=Registry(), strategies=[SimpleNamespace(data_sources=['fred'])])


def test_engine_two_phases_reuse_matching_success_without_external_fetch(tmp_path):
    cache = tmp_path / 'cache'
    first = _engine(tmp_path, cache_dir=cache)
    first._fetch_fred_data = lambda start, end: {'interest_rate': {'value': 5, 'date': date(2026, 10, 1)}}
    accepted = first._fetch_all_data('2026-07-08', '2026-10-06')
    second = _engine(tmp_path, cache_dir=cache)
    def must_not_fetch(*args): raise AssertionError('same-window successful source should be cached')
    second._fetch_fred_data = must_not_fetch
    assert second._fetch_all_data('2026-07-08', '2026-10-06') == accepted
    assert list(cache.glob('*.json'))


def test_engine_partial_inputs_preserved_but_next_phase_fetches_again(tmp_path):
    first = _engine(tmp_path, cache_dir=tmp_path / 'cache')
    first._fetch_fred_data = lambda *args: {'interest_rate': {'value': 5}, 'error': 'timeout'}
    assert first._fetch_all_data('2026-07-08', '2026-10-06')['fred']['interest_rate']['value'] == 5
    second = _engine(tmp_path, cache_dir=tmp_path / 'cache')
    second._fetch_fred_data = lambda *args: {'interest_rate': {'value': 6}}
    assert second._fetch_all_data('2026-07-08', '2026-10-06')['fred']['interest_rate']['value'] == 6


def test_queued_source_deadline_starts_at_acquisition_not_worker_start(tmp_path, monkeypatch):
    import tradingagents.strategies.orchestration.multi_strategy_engine as module
    import time
    engine = _engine(tmp_path)
    calls = []
    engine._fetch_fred_data = lambda *args: calls.append('requested') or {}
    monkeypatch.setenv('AUTORESEARCH_FETCH_TIMEOUT_S', '0.001')
    def delayed_gather(fetches, timeout):
        time.sleep(0.005)
        return {name: fn(*args) for name, (fn, args) in fetches.items()}
    monkeypatch.setattr(module, '_gather_with_timeout', delayed_gather)
    data = engine._fetch_all_data('2026-07-08', '2026-10-06')
    assert not calls
    assert data['fred']['error']
    assert data['fred']['_coverage']['reason_code'] == 'deadline_exhausted'


def _daily_state(tmp_path, fetcher):
    from types import SimpleNamespace
    from tradingagents.strategies.orchestration.daily_pipeline import DailyRunState
    engine = SimpleNamespace(_fetch_all_data=fetcher, pending_late_signals=lambda *args: [])
    cohort = {'engine': engine, 'config': SimpleNamespace(horizon='30d'),
              'executor': SimpleNamespace(validated_execution_reference_bars=lambda *args: {})}
    screened = []
    owner = SimpleNamespace(
        cohorts=[cohort], _base_config={'autoresearch': {'state_dir': str(tmp_path)}},
        _metric_epoch_context=SimpleNamespace(generation_id='gen_test', generation_commit='abc'),
        _screen_for_horizon=lambda data, *args: screened.append(data) or ([], {}, []),
        _persist_horizon_health=lambda *args: True, _policy_id_for_horizon=lambda *args: 'policy',
    )
    state = DailyRunState(owner, '2026-10-06', date(2026, 10, 6), NOW, valid=[cohort])
    state.fail_candidates = lambda reason, **kwargs: {'error': reason}
    return state, screened


def test_daily_screening_freezes_before_analysis_and_resume_uses_original(tmp_path):
    from tradingagents.strategies.orchestration.daily_pipeline import run_horizon_screening
    state, screened = _daily_state(tmp_path, lambda *args: {'fred': {'rate': 1}, 'edgar': {'error': 'timeout'}})
    assert run_horizon_screening(state) is None
    assert list((tmp_path / 'source_inputs').glob('*.json'))
    assert screened[0]['fred']['rate'] == 1
    resumed, seen = _daily_state(tmp_path, lambda *args: {'fred': {'rate': 99}})
    assert run_horizon_screening(resumed) is None
    assert seen[0]['fred']['rate'] == 1
    assert seen[0]['edgar']['error'] == 'timeout'


def test_daily_corrupt_frozen_inputs_block_before_analysis(tmp_path):
    from tradingagents.strategies.orchestration.daily_pipeline import run_horizon_screening
    state, _ = _daily_state(tmp_path, lambda *args: {'fred': {}})
    run_horizon_screening(state)
    next((tmp_path / 'source_inputs').glob('*.json')).write_text('{}')
    resumed, screened = _daily_state(tmp_path, lambda *args: pytest.fail('must not refresh accepted data'))
    result = run_horizon_screening(resumed)
    assert result['error'] == 'shared_source_bundle_invalid'
    assert not screened


def test_cache_entitlement_identity_changes_with_environment_credentials(tmp_path, monkeypatch):
    first = _engine(tmp_path, cache_dir=tmp_path / 'cache')
    first._fetch_fred_data = lambda *args: {'rate': 1}
    monkeypatch.setenv('FRED_API_KEY', 'first-entitlement')
    first._fetch_all_data('2026-07-08', '2026-10-06')
    monkeypatch.setenv('FRED_API_KEY', 'second-entitlement')
    second = _engine(tmp_path, cache_dir=tmp_path / 'cache')
    second._fetch_fred_data = lambda *args: {'rate': 2}
    assert second._fetch_all_data('2026-07-08', '2026-10-06')['fred']['rate'] == 2
    assert all('entitlement' not in path.read_text() for path in (tmp_path / 'cache').glob('*.json'))


def test_frozen_shared_prices_hydrate_new_engine_cache_for_resume(tmp_path):
    from tradingagents.strategies.orchestration.daily_pipeline import run_horizon_screening
    original = pd.DataFrame({'Close': [1.0]}, index=pd.DatetimeIndex(['2026-10-05']))
    state, _ = _daily_state(tmp_path, lambda *args: {'yfinance': {'prices': {'SPY': original}}})
    state.owner.cohorts[0]['engine']._price_cache = {}
    assert run_horizon_screening(state) is None
    resumed, _ = _daily_state(tmp_path, lambda *args: pytest.fail('must reuse frozen prices'))
    resumed.owner.cohorts[0]['engine']._price_cache = {}
    assert run_horizon_screening(resumed) is None
    pd.testing.assert_frame_equal(resumed.first_engine._price_cache['SPY'], original)


def test_concurrent_freeze_only_one_bundle_becomes_accepted(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    store = SourceInputStore(tmp_path / 'cache', accepted_dir=tmp_path / 'accepted')
    barrier = Barrier(2)
    def freeze(value):
        barrier.wait()
        return store.freeze(FROZEN, {'fred': {'rate': value}}, acquired_at=NOW)
    with ThreadPoolExecutor(max_workers=2) as pool:
        left = pool.submit(freeze, 1)
        right = pool.submit(freeze, 2)
        assert left.result() == right.result() == store.load_frozen(FROZEN)
    assert len(list((tmp_path / 'accepted').glob('*.json'))) == 1


@pytest.mark.parametrize('coverage', ['partial', {'status': 'failed'}, {'status': None}])
def test_invalid_or_unsuccessful_coverage_cannot_authorize_cache(tmp_path, coverage):
    store = SourceInputStore(tmp_path)
    assert not store.save_cached(IDENTITY, {'_coverage': coverage, 'value': 1}, acquired_at=NOW)


def test_codec_byte_limit_is_enforced_before_decode(monkeypatch):
    import tradingagents.strategies.orchestration.source_inputs as module
    monkeypatch.setattr(module, 'MAX_BYTES', 100)
    with pytest.raises(SourceInputError, match='byte limit'):
        SourceInputStore.decode('"' + 'x' * 101 + '"')
    with pytest.raises(SourceInputError, match='byte limit'):
        SourceInputStore.encode({'large': 'x' * 101})


def test_freeze_durably_syncs_file_and_parent_directory(tmp_path, monkeypatch):
    import os
    import stat
    synced = []
    original = os.fsync
    def sync(fd):
        synced.append(stat.S_ISDIR(os.fstat(fd).st_mode))
        return original(fd)
    monkeypatch.setattr(os, 'fsync', sync)
    SourceInputStore(tmp_path / 'cache', accepted_dir=tmp_path / 'accepted').freeze(FROZEN, {}, acquired_at=NOW)
    assert synced == [False, True]
