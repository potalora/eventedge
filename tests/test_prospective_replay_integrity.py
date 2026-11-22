"""Completed prospective books must retain their immutable decision evidence."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_source_reliability_pipeline import pipeline, run_campaign, SESSION, GENERATION, accepted_input_bytes
from test_prospective_pipeline import enable_activity_pipeline
from tradingagents.strategies.orchestration import decision_clock
from tradingagents.strategies.orchestration.source_inputs import SourceInputStore


def completed(pipeline, monkeypatch):
    fixture, owner, config = enable_activity_pipeline(pipeline, monkeypatch)
    result, _, _ = run_campaign(pipeline)
    assert result['outcome'] == 'clean'
    epoch = owner.cohorts[0]['ledger'].read_policy_session_context(SESSION)['epoch_id']
    fixture.block_all = True
    fixture.now = datetime(2026, 10, 5, 16, tzinfo=timezone.utc)
    return fixture, owner, config, epoch


def evidence_file(config, kind):
    directory = Path(config['autoresearch']['state_dir']) / 'source_inputs'
    if kind != 'source':
        directory /= {'decision': 'decision_inputs', 'eligibility': 'new_entry_eligibility'}[kind]
    paths = list(directory.glob('*.json'))
    assert len(paths) == 1
    return paths[0]


def rewrite_payload(path, mutate):
    table_dir = path.parent / (path.stem + ".tables")
    document = SourceInputStore.decode(
        path.read_text(),
        table_dir=table_dir if table_dir.is_dir() else None,
    )
    mutate(document['payload'])
    # A self-consistent envelope cannot replace the independently bound payload.
    document['digest'] = hashlib.sha256(SourceInputStore.encode(document['payload']).encode()).hexdigest()
    path.write_text(SourceInputStore.encode(document))


@pytest.mark.parametrize('kind', ['source', 'decision', 'eligibility'])
@pytest.mark.parametrize('mutation', ['missing', 'changed'])
def test_completed_replay_rejects_missing_or_rebound_inputs(pipeline, monkeypatch, kind, mutation):
    fixture, owner, config, epoch = completed(pipeline, monkeypatch)
    path = evidence_file(config, kind)
    if mutation == 'missing':
        path.unlink()
    elif kind == 'source':
        rewrite_payload(path, lambda value: value.update(review_tamper=True))
    elif kind == 'decision':
        rewrite_payload(path, lambda value: value['context'].update(cutoff='2026-10-03T17:59:59+00:00'))
    else:
        rewrite_payload(path, lambda value: value['protected_tickers'].append('NVDA'))
    before, calls = accepted_input_bytes(config), fixture.calls.copy()
    with pytest.raises(ValueError):
        decision_clock.validate_completed_replay(owner, SESSION, epoch, owner.cohorts)
    result = fixture.manager.run_daily(str(SESSION))[GENERATION]
    # Coverage reports durable strategy health; damaged replay evidence instead
    # invalidates staging, without rewriting that already accepted health.
    assert result['outcome'] == 'failed'
    from tradingagents.strategies.orchestration.generation_manager import _extract_cohort_results
    wire = _extract_cohort_results(json.loads(Path(result['evidence_path']).read_text())['stdout'])
    assert len(wire) == 16
    assert all(row['error'] and row['staging_valid'] is False
               and row['invalid_reason'] == 'prospective_replay_evidence_invalid' for row in wire.values())
    assert accepted_input_bytes(config) == before and fixture.calls == calls
    assert not fixture.blocked_calls


def test_valid_completed_replay_after_open_is_read_only(pipeline, monkeypatch):
    fixture, owner, config, epoch = completed(pipeline, monkeypatch)
    before, calls = accepted_input_bytes(config), fixture.calls.copy()
    def forbid(*args, **kwargs):
        raise AssertionError('completed replay attempted to publish evidence')
    monkeypatch.setattr(SourceInputStore, 'freeze', forbid)
    context = decision_clock.validate_completed_replay(owner, SESSION, epoch, owner.cohorts)
    assert context == owner.cohorts[0]['ledger'].read_policy_session_context(SESSION)['context']['decision_clock']
    assert accepted_input_bytes(config) == before and fixture.calls == calls
    assert not fixture.blocked_calls


@pytest.mark.parametrize('change', ['missing', 'clock', 'epoch'])
def test_every_completed_book_must_match_shared_clock(pipeline, monkeypatch, change):
    _, owner, _, epoch = completed(pipeline, monkeypatch)
    ledger = owner.cohorts[-1]['ledger']
    binding = deepcopy(ledger.read_policy_session_context(SESSION))
    if change == 'missing':
        binding = None
    elif change == 'clock':
        binding['context']['decision_clock']['source_digest'] = 'a' * 64
    else:
        binding['epoch_id'] = 'different-epoch'
    monkeypatch.setattr(ledger, 'read_policy_session_context', lambda *a, **k: binding)
    with pytest.raises(ValueError):
        decision_clock.validate_completed_replay(owner, SESSION, epoch, owner.cohorts)


def test_completed_replay_requires_full_candidate_identity_binding(pipeline, monkeypatch):
    _, owner, _, epoch = completed(pipeline, monkeypatch)
    monkeypatch.setattr(owner._metric_store, 'read_candidate_signal_identity_binding', lambda *a: None)
    with pytest.raises(ValueError, match='identity'):
        decision_clock.validate_completed_replay(owner, SESSION, epoch, owner.cohorts)


def test_historical_completed_replay_does_not_require_prospective_evidence():
    owner = SimpleNamespace(_base_config={})
    assert decision_clock.validate_completed_replay(owner, SESSION, 'historical', [object()]) is None


def test_completed_replay_rechecks_original_protected_scope(pipeline, monkeypatch):
    _, owner, _, epoch = completed(pipeline, monkeypatch)
    executor = owner.cohorts[-1]['executor']
    monkeypatch.setattr(executor, 'benchmark_symbols', (*executor.benchmark_symbols, 'NVDA'))
    with pytest.raises(ValueError):
        decision_clock.validate_completed_replay(owner, SESSION, epoch, owner.cohorts)
