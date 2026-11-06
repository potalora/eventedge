"""Full-filing source capacity is explicit, finite and isolated from legacy stores."""
from types import SimpleNamespace

import pytest

from test_source_inputs import FROZEN, NOW
from tradingagents.strategies.orchestration.source_inputs import SourceInputError, SourceInputStore


def test_explicit_larger_codec_preserves_full_text_and_enforces_both_bounds(monkeypatch):
    from tradingagents.strategies.orchestration import source_inputs as module
    monkeypatch.setattr(module, 'MAX_BYTES', 100)
    payload = {'text': 'prefix ' * 100 + 'full trailing evidence'}
    with pytest.raises(SourceInputError, match='byte limit'):
        SourceInputStore.encode(payload)
    limits = {'max_bytes': 4096, 'max_nodes': 100}
    encoded = SourceInputStore.encode(payload, **limits)
    assert SourceInputStore.decode(encoded, **limits) == payload
    with pytest.raises(SourceInputError, match='byte limit'):
        SourceInputStore.decode(encoded)
    with pytest.raises(SourceInputError, match='structural bounds'):
        SourceInputStore.decode(encoded, max_bytes=4096, max_nodes=2)
    with pytest.raises(SourceInputError, match='structural bounds'):
        SourceInputStore.encode(payload, max_bytes=4096, max_nodes=2)


def test_custom_store_freezes_once_without_clipping_or_mutable_alias(tmp_path, monkeypatch):
    from tradingagents.strategies.orchestration import source_inputs as module
    monkeypatch.setattr(module, 'MAX_BYTES', 100)
    payload = {'edgar': {'corpus': {'filing': {'text': 'source ' * 100}}}}
    store = SourceInputStore(tmp_path/'cache', accepted_dir=tmp_path/'accepted',
                             max_bytes=8192, max_nodes=1000)
    result = store.freeze(FROZEN, payload, acquired_at=NOW)
    result['edgar']['corpus']['filing']['text'] = 'mutated'
    assert store.load_frozen(FROZEN) == payload
    assert store.freeze(FROZEN, {'changed': True}, acquired_at=NOW) == payload
    # The on-disk envelope obeys the configured capacity even on later reopen.
    reopened = SourceInputStore(tmp_path/'cache', accepted_dir=tmp_path/'accepted',
                                max_bytes=8192, max_nodes=1000)
    assert reopened.load_frozen(FROZEN) == payload
    with pytest.raises(SourceInputError, match='byte limit'):
        SourceInputStore(tmp_path/'cache', accepted_dir=tmp_path/'accepted').load_frozen(FROZEN)


@pytest.mark.parametrize('limits', [
    {'max_bytes': True}, {'max_nodes': True}, {'max_bytes': 0}, {'max_nodes': 0},
    {'max_bytes': 512*1024*1024+1}, {'max_nodes': 8_000_001},
    {'max_bytes': float('inf')}, {'max_nodes': 1.5},
])
def test_capacities_are_finite_strict_integers(tmp_path, limits):
    with pytest.raises(SourceInputError, match='source capacity'):
        SourceInputStore(tmp_path, **limits)
    with pytest.raises(SourceInputError, match='source capacity'):
        SourceInputStore.encode({}, **limits)
    with pytest.raises(SourceInputError, match='source capacity'):
        SourceInputStore.decode('{}', **limits)


def test_only_explicit_filing_policy_enlarges_source_store(tmp_path):
    from tradingagents.strategies.orchestration.source_inputs import (
        daily_source_store, daily_volatility_store, source_codec_limits,
    )
    config = {'autoresearch': {'state_dir': str(tmp_path)}}
    assert source_codec_limits(config) == {}
    config['autoresearch']['filing_evidence_policy'] = 'complete_submission_v1'
    expected = {'max_bytes': 512*1024*1024, 'max_nodes': 8_000_000}
    assert source_codec_limits(config) == expected
    assert source_codec_limits(config, source='edgar') == expected
    assert source_codec_limits(config, source='fred') == {}
    owner = SimpleNamespace(_base_config=config,
        _metric_epoch_context=SimpleNamespace(generation_id='g', generation_commit='c'))
    source, _ = daily_source_store(owner, '2026-10-09')
    volatility, _ = daily_volatility_store(owner, '2026-10-09')
    assert source.codec_limits == expected
    assert volatility.codec_limits == {'max_bytes': None, 'max_nodes': None}
    source.codec_limits['max_bytes'] = 1
    assert source.codec_limits == expected


@pytest.mark.parametrize('phase', ['before', 'encode', 'sync'])
def test_expired_acquisition_cannot_publish_frozen_source(tmp_path, monkeypatch, phase):
    from tradingagents.strategies.orchestration import source_inputs as module
    now = [106.0 if phase == 'before' else 100.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])
    store = SourceInputStore(tmp_path/'cache', accepted_dir=tmp_path/'accepted')
    if phase == 'encode':
        original = SourceInputStore.encode
        def encode(*args, **kwargs):
            value = original(*args, **kwargs)
            now[0] = 106.0
            return value
        monkeypatch.setattr(SourceInputStore, 'encode', staticmethod(encode))
    if phase == 'sync':
        original_sync = module.os.fsync
        def fsync(fd):
            original_sync(fd)
            now[0] = 106.0
        monkeypatch.setattr(module.os, 'fsync', fsync)
    with pytest.raises(SourceInputError, match='deadline'):
        store.freeze(FROZEN, {'source': 'complete'}, acquired_at=NOW, deadline=105.0)
    assert list((tmp_path/'accepted').glob('*.json')) == []
    assert list((tmp_path/'accepted').glob('.source-*')) == []


def test_original_acquisition_deadline_also_covers_freeze_readback(tmp_path, monkeypatch):
    from tradingagents.strategies.orchestration import source_inputs as module
    now = [100.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])
    store = SourceInputStore(tmp_path/'cache', accepted_dir=tmp_path/'accepted')
    original = store.load_frozen
    calls = []
    def load(identity):
        result = original(identity)
        calls.append(result)
        if len(calls) == 2:
            now[0] = 106.0
        return result
    monkeypatch.setattr(store, 'load_frozen', load)
    with pytest.raises(SourceInputError, match='deadline'):
        store.freeze(FROZEN, {'source': 'complete'}, acquired_at=NOW, deadline=105.0)
    # Retain rejected evidence outside the accepted namespace. A fresh resume
    # cannot bypass the original deadline by loading the rejected observation.
    assert not list((tmp_path/'accepted').glob('*.json'))
    assert len(list((tmp_path/'accepted'/'rejected').glob('*.json'))) == 1
    assert original(FROZEN) is None


def test_late_link_publication_is_not_reusable_on_resume(tmp_path, monkeypatch):
    from tradingagents.strategies.orchestration import source_inputs as module
    now = [100.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])
    original_link = module.os.link
    def late_link(*args, **kwargs):
        now[0] = 106.0
        return original_link(*args, **kwargs)
    monkeypatch.setattr(module.os, 'link', late_link)
    store = SourceInputStore(tmp_path/'cache', accepted_dir=tmp_path/'accepted')
    with pytest.raises(SourceInputError, match='deadline'):
        store.freeze(FROZEN, {'source': 'complete'}, acquired_at=NOW, deadline=105.0)
    assert store.load_frozen(FROZEN) is None
    assert len(list((tmp_path/'accepted'/'rejected').glob('*.json'))) == 1


def test_deadline_failure_after_losing_publication_does_not_evict_winner(tmp_path, monkeypatch):
    from tradingagents.strategies.orchestration import source_inputs as module
    now = [100.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])
    store = SourceInputStore(tmp_path/'cache', accepted_dir=tmp_path/'accepted')
    winner = SourceInputStore(tmp_path/'cache', accepted_dir=tmp_path/'accepted')
    def competing_write(*args, **kwargs):
        winner.freeze(FROZEN, {'source': 'winner'}, acquired_at=NOW)
        now[0] = 106.0
        raise FileExistsError
    monkeypatch.setattr(store, '_write', competing_write)
    with pytest.raises(SourceInputError, match='deadline'):
        store.freeze(FROZEN, {'source': 'loser'}, acquired_at=NOW, deadline=105.0)
    assert winner.load_frozen(FROZEN) == {'source': 'winner'}
    assert not (tmp_path/'accepted'/'rejected').exists()


def test_full_filing_pipeline_carries_original_deadline_through_freeze(tmp_path, monkeypatch):
    import time
    from test_source_inputs import _daily_state
    from tradingagents.strategies.orchestration import daily_pipeline
    now, deadlines = [100.0], []
    monkeypatch.setattr(time, 'monotonic', lambda: now[0])
    monkeypatch.setenv('AUTORESEARCH_FETCH_TIMEOUT_S', '5')
    def acquire(*args, acquisition_deadline=None):
        deadlines.append(acquisition_deadline)
        now[0] = 104.0
        return {'edgar': {'corpus': {'complete': 'evidence'}}}
    state, screened = _daily_state(tmp_path, acquire)
    state.owner._base_config['autoresearch']['filing_evidence_policy'] = 'complete_submission_v1'
    original = SourceInputStore.encode
    def encode(*args, **kwargs):
        result = original(*args, **kwargs)
        if isinstance(args[0], dict) and 'edgar' in args[0]:
            now[0] = 106.0
        return result
    monkeypatch.setattr(SourceInputStore, 'encode', staticmethod(encode))
    assert daily_pipeline.run_horizon_screening(state) == {'error': 'shared_source_bundle_invalid'}
    assert deadlines == [105.0]
    assert not screened
    assert not list((tmp_path/'source_inputs').glob('*.json'))


@pytest.mark.parametrize('outer_deadline,expected', [(102.0, 102.0), (900.0, 105.0)])
def test_engine_original_deadline_cannot_be_reset_or_extended(tmp_path, monkeypatch, outer_deadline, expected):
    import time
    from test_source_inputs import _engine
    from tradingagents.strategies.orchestration import multi_strategy_engine as module
    now, budgets = [100.0], []
    monkeypatch.setattr(time, 'monotonic', lambda: now[0])
    monkeypatch.setenv('AUTORESEARCH_FETCH_TIMEOUT_S', '5')
    engine = _engine(tmp_path)
    engine._fetch_fred_data = lambda *args: {}
    def gather(fetches, timeout):
        budgets.append(timeout)
        now[0] = expected + 1
        return {name: fn(*args) for name, (fn, args) in fetches.items()}
    monkeypatch.setattr(module, '_gather_with_timeout', gather)
    with pytest.raises(SourceInputError, match='deadline'):
        engine._fetch_all_data('2026-07-08', '2026-10-06', acquisition_deadline=outer_deadline)
    assert budgets == [expected - 100.0]
