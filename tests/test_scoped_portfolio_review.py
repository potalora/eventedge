"""Independent scope/replay checks: synthetic transport and native 16-book state."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from test_source_reliability_pipeline import pipeline, run_campaign, SESSION, GENERATION, accepted_input_bytes
from test_prospective_pipeline import enable_activity_pipeline
from tradingagents.strategies.orchestration import decision_clock


def enable_scoped_court(pipeline, monkeypatch, *, fail=False):
    fixture, owner, config = enable_activity_pipeline(pipeline, monkeypatch)
    for target in (config, owner._base_config):
        target['autoresearch'].update(courtlistener_scope_policy='focused_litigation_v1',
            courtlistener_focused={'watchlist': ['AAPL'], 'case_ids': [], 'lookback_sessions': 5,
                                  'shortlist_limit': 5, 'issuer_query_limit': 5})
    from tradingagents.strategies.data_sources import courtlistener_source
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixture.now.astimezone(tz) if tz else fixture.now.replace(tzinfo=None)
    monkeypatch.setattr(courtlistener_source, 'datetime', FixedDatetime)
    original = fixture.http
    def http(method, url, **kwargs):
        if 'courtlistener.com' in url and not fixture.block_all and fail:
            fixture.calls[url] += 1
            return fixture.response({}, 403)
        return original(method, url, **kwargs)
    fixture.http = http
    return fixture, owner, config


@pytest.mark.parametrize('fail', [False, True], ids=['healthy', 'retained-provider-failure'])
def test_completed_scoped_replay_keeps_original_outcome_and_makes_no_calls(pipeline, monkeypatch, fail):
    fixture, owner, config = enable_scoped_court(pipeline, monkeypatch, fail=fail)
    result, wire, _ = run_campaign(pipeline)
    expected = 'degraded' if fail else 'clean'
    assert result['outcome'] == expected
    assert all(row['execution_valid'] and row['staging_valid'] for row in wire.values())
    epoch = owner.cohorts[0]['ledger'].read_policy_session_context(SESSION)['epoch_id']
    owner._epoch_id = epoch
    before, calls = accepted_input_bytes(config), fixture.calls.copy()
    fixture.block_all = True
    fixture.now = datetime(2026, 10, 5, 16, tzinfo=timezone.utc)
    context = decision_clock.validate_completed_replay(owner, SESSION, epoch, owner.cohorts)
    assert context == owner.cohorts[0]['ledger'].read_policy_session_context(SESSION)['context']['decision_clock']
    repeated, _, _ = run_campaign(pipeline)
    assert repeated['outcome'] == expected
    assert accepted_input_bytes(config) == before and fixture.calls == calls
    assert not fixture.blocked_calls


@pytest.mark.parametrize('fault', ['epoch', 'roster', 'cutoff', 'settings', 'company_map'])
def test_portfolio_scope_rejects_rebound_original_identity(pipeline, monkeypatch, fault):
    from tradingagents.strategies.orchestration.source_inputs import daily_source_store
    from tradingagents.strategies.orchestration.scoped_sources import validate_portfolio_targets
    _, owner, _ = enable_scoped_court(pipeline, monkeypatch)
    run_campaign(pipeline)
    owner._epoch_id = owner.cohorts[0]['ledger'].read_policy_session_context(SESSION)['epoch_id']
    store, identity = daily_source_store(owner, str(SESSION))
    data = deepcopy(store.load_frozen(identity))
    assert validate_portfolio_targets(data, owner, str(SESSION)) is None
    targets = data['_courtlistener_targets']
    if fault == 'epoch':
        owner._epoch_id = 'other-epoch'
    elif fault == 'roster':
        owner.cohorts = owner.cohorts[:-1]
    elif fault == 'cutoff':
        data['_decision_acquisition']['started_at'] = '2026-10-03T17:00:00+00:00'
    elif fault == 'settings':
        owner._base_config['autoresearch']['courtlistener_focused']['watchlist'] = ['MSFT']
    else:
        targets['company_map']['0']['title'] = 'Other Company'
    with pytest.raises(ValueError):
        validate_portfolio_targets(data, owner, str(SESSION))


@pytest.mark.parametrize('fault', ['missing', 'wrong_provider', 'wrong_status', 'wrong_sources',
                                  'wrong_epoch', 'wrong_policy', 'duplicate'])
def test_retained_failure_requires_every_original_dependent_health_record(pipeline, monkeypatch, fault):
    fixture, owner, config = enable_scoped_court(pipeline, monkeypatch, fail=True)
    result, _, _ = run_campaign(pipeline)
    assert result['outcome'] == 'degraded'
    epoch = owner.cohorts[0]['ledger'].read_policy_session_context(SESSION)['epoch_id']
    owner._epoch_id = epoch
    fixture.block_all = True
    health = list(owner._metric_store.read_strategy_health(epoch, session=SESSION))
    index = next(i for i, row in enumerate(health) if row.strategy == 'litigation')
    row = health[index]
    if fault == 'missing':
        health.pop(index)
    elif fault == 'wrong_provider':
        health[index] = replace(row, evidence={**row.evidence, 'provider_errors': {'edgar': 'failed'}})
    elif fault == 'wrong_status':
        health[index] = replace(row, status='legitimate_no_event')
    elif fault == 'wrong_sources':
        health[index] = replace(row, evidence={**row.evidence, 'data_sources': ['edgar']})
    elif fault == 'wrong_epoch':
        health[index] = replace(row, epoch_id='other')
    elif fault == 'wrong_policy':
        health[index] = replace(row, policy_id='other')
    else:
        health.append(row)
    monkeypatch.setattr(owner._metric_store, 'read_strategy_health', lambda *a, **k: tuple(health))
    with pytest.raises(ValueError, match='health'):
        decision_clock.validate_completed_replay(owner, SESSION, epoch, owner.cohorts)
    assert not fixture.blocked_calls


def test_retained_usaspending_failure_also_replays_as_degraded(pipeline, monkeypatch):
    fixture, owner, config = enable_scoped_court(pipeline, monkeypatch)
    for target in (config, owner._base_config):
        target['autoresearch']['award_attribution_policy'] = 'verified_listed_targets_v1'
    original = fixture.http
    def failed_awards(method, url, **kwargs):
        if 'api.usaspending.gov' in url and not fixture.block_all:
            fixture.calls[url] += 1
            return fixture.response({}, 403)
        return original(method, url, **kwargs)
    fixture.http = failed_awards
    result, wire, _ = run_campaign(pipeline)
    assert result['outcome'] == 'degraded'
    assert all(row['execution_valid'] and row['staging_valid'] for row in wire.values())
    fixture.block_all = True
    before, calls = accepted_input_bytes(config), fixture.calls.copy()
    replayed, _, _ = run_campaign(pipeline)
    assert replayed['outcome'] == 'degraded'
    assert accepted_input_bytes(config) == before and fixture.calls == calls
    assert not fixture.blocked_calls


def test_provider_failure_for_only_disabled_strategy_does_not_break_replay(pipeline, monkeypatch):
    fixture, owner, config = enable_scoped_court(pipeline, monkeypatch, fail=True)
    for target in (config, owner._base_config):
        target['autoresearch'].setdefault('disabled_strategies', {})['litigation'] = 'review_disabled'
    result = fixture.manager.run_daily(str(SESSION))[GENERATION]
    assert result['outcome'] == 'clean'
    fixture.block_all = True
    before, calls = accepted_input_bytes(config), fixture.calls.copy()
    replayed = fixture.manager.run_daily(str(SESSION))[GENERATION]
    assert replayed['outcome'] == 'clean'
    assert accepted_input_bytes(config) == before and fixture.calls == calls
