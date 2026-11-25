"""Completed DAILY replay uses cohort-local policy input membership."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from test_portfolio_policy_ledger import _signal
from test_session_executor import _policy_enabled_staging_fixture, FRIDAY, MONDAY
from tradingagents.strategies.orchestration.daily_pipeline import DailyRunState, partition_daily_replay
from tradingagents.strategies.orchestration.session_executor import PHASES, SessionExecutor


def _completed_state(tmp_path, monkeypatch, *, policy=True):
    ledger, engine, call = _policy_enabled_staging_fixture(tmp_path)
    engine.ar_config['paper_ledger']['policy_id'] = 'shared-policy'
    execution_config = deepcopy(engine.config)
    if not policy:
        engine.ar_config.pop('portfolio_policy', None)
    call['annualized_volatility_evidence'] = {'AAPL': 0.31}
    rejected = deepcopy(call['shared_signals'][0])
    rejected['direction'] = 'neutral'
    rejected['metadata']['event_key'] = 'local-rejected'
    call['shared_signals'].append(rejected)
    original = engine.screen_and_stage(**call)
    cohort = dict(config=SimpleNamespace(name='cohort', horizon='30d'),
        ledger=ledger, engine=engine, size_profile=call['size_profile'],
        executor=SessionExecutor(ledger, execution_config, size_profile=call['size_profile']))
    owner = SimpleNamespace(cohorts=[cohort], _base_config={},
        _metric_store=SimpleNamespace(read_session_candidate_bar_recoveries=lambda *args: (),
                                      read_session_candidate_input_issues=lambda *args: ()))
    state = DailyRunState(owner, FRIDAY.isoformat(), FRIDAY,
        datetime(2026, 7, 31, 22, tzinfo=timezone.utc), epoch_id='epoch')
    # This focused boundary test leaves decision-clock replay to its own suite.
    monkeypatch.setattr('tradingagents.strategies.orchestration.decision_clock.validate_completed_replay',
        lambda *args: None)
    return ledger, state, original


def test_daily_completed_policy_replay_excludes_foreign_signals(tmp_path, monkeypatch):
    ledger, state, original = _completed_state(tmp_path, monkeypatch)
    try:
        manifest = ledger.read_policy_staging_audit_manifests()
        committee = ledger.committee_decision(FRIDAY, 'epoch', 'shared-policy')
        digest = ledger.verify_session_phase_chain(FRIDAY, PHASES)
        intents = ledger.pending_intents(MONDAY)
        ledger.record_signal(replace(_signal('foreign-only'), epoch_id='epoch', policy_id='shared-policy'))
        assert partition_daily_replay(state) is None
        assert state.results['cohort']['signals'] == sorted(original['signals'], key=lambda row: row['signal_id'])
        assert len(state.results['cohort']['signals']) == 2  # Includes the rejected input.
        assert state.results['cohort']['replayed'] is True
        assert ledger.read_policy_staging_audit_manifests() == manifest
        assert ledger.committee_decision(FRIDAY, 'epoch', 'shared-policy') == committee
        assert ledger.verify_session_phase_chain(FRIDAY, PHASES) == digest
        assert ledger.pending_intents(MONDAY) == intents
    finally:
        ledger.close()


@pytest.mark.parametrize('policy', [False, True])
def test_daily_completed_replay_keeps_legacy_global_projection(tmp_path, monkeypatch, policy):
    from test_cohort_signal_membership import _rewrite_manifest

    ledger, state, _ = _completed_state(tmp_path, monkeypatch, policy=policy)
    try:
        if policy:
            _rewrite_manifest(ledger, lambda payload: payload.pop('input_signal_ids'))
        ledger.record_signal(replace(_signal('foreign-only'), epoch_id='epoch', policy_id='shared-policy'))
        assert partition_daily_replay(state) is None
        assert 'foreign-only' in {row['signal_id'] for row in state.results['cohort']['signals']}
    finally:
        ledger.close()


@pytest.mark.parametrize('eligible', [False, True])
def test_daily_completed_policy_membership_failure_uses_critical_gap(tmp_path, monkeypatch, eligible):
    ledger, state, _ = _completed_state(tmp_path, monkeypatch)
    try:
        manifest, = ledger.read_policy_staging_audit_manifests()
        target, = (set(manifest['ingress_signal_ids']) if eligible else
            set(manifest['input_signal_ids']) - set(manifest['ingress_signal_ids']))
        ledger.connection.execute("DELETE FROM policy_audit_records WHERE cohort_id=? "
            "AND record_type='signal_policy_provenance' AND record_id=?", (ledger.cohort_id, target))
        failures = []
        def critical_gap(*args, **kwargs):
            failures.append((args, kwargs))
            return {'critical_gap': True}
        monkeypatch.setattr(state, 'critical_gap', critical_gap)
        assert partition_daily_replay(state) == {'critical_gap': True}
        assert state.results['cohort']['error'] is True
        assert state.completed == []
        assert len(failures) == 1
        assert isinstance(failures[0][1]['original_error'], Exception)
    finally:
        ledger.close()
