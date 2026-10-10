"""Real sixteen-book prospective staging with frozen provider/model fixtures."""
from datetime import datetime, timezone
from pathlib import Path
import json
import hashlib

import pytest

from test_source_reliability_pipeline import (
    pipeline, run_campaign, SESSION, GENERATION, InterruptedWorker, accepted_input_bytes,
)
from tradingagents.strategies.orchestration import decision_clock
from tradingagents.strategies.orchestration.source_inputs import SourceInputStore


def enable_activity_pipeline(pipeline, monkeypatch, *, tape='non_qualifying', listing=True):
    """Use the real native asset/daily/tape adapters at their HTTP boundaries."""
    from tradingagents.strategies.execution import session_activity
    from tradingagents.strategies.data_sources import equity_universe
    fixture, owner, config, _ = pipeline
    for target in (config, owner._base_config):
        target['autoresearch'].update(decision_clock_policy=decision_clock.PROSPECTIVE,
            new_entry_eligibility_policy=session_activity.POLICY,
            equity_universe_policy=equity_universe.POLICY)
    fixture.now = datetime(2026, 10, 3, 18, tzinfo=timezone.utc)
    monkeypatch.setattr(decision_clock, 'utc_now', lambda: fixture.now)
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(fixture.now.timestamp(), tz)
    monkeypatch.setattr(session_activity, 'datetime', FixedDatetime)
    monkeypatch.setattr(equity_universe, 'datetime', FixedDatetime)
    original = fixture.http
    def native_http(method, url, **kwargs):
        if fixture.block_all:
            return original(method, url, **kwargs)
        params = kwargs.get('params') or {}
        if url in (equity_universe.ENDPOINT, session_activity.URL):
            fixture.calls[url] += 1
            fixture.request_trace.append((url, dict(params)))
            if url == equity_universe.ENDPOINT:
                symbols = ['SPY', 'BIL', 'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'META', 'TSLA', 'JPM']
                if listing:
                    symbols.append('NVDA')
                return fixture.response([{'class':'us_equity', 'symbol':s,
                    'exchange':'NASDAQ', 'status':'active', 'tradable':True} for s in symbols])
            assert params['symbols'] == 'NVDA'
            conditions = ['@'] if tape == 'qualifying' else ['@', 'I']
            if tape == 'malformed':
                conditions.append('?')
            return fixture.response({'trades': {'NVDA': [{
                't':'2026-10-02T20:00:00.000000001Z', 'x':'Q', 'p':184,
                's':1, 'i':1, 'z':'C', 'c':conditions}]}, 'next_page_token':None})
        response = original(method, url, **kwargs)
        if url == 'https://data.alpaca.markets/v2/stocks/bars':
            payload = response.json()
            for row in payload['bars'].get('NVDA', []):
                row.update(v=0, n=0)
            return fixture.response(payload)
        return response
    fixture.http = native_http
    return fixture, owner, config


def frozen_eligibility(config):
    files = list((Path(config['autoresearch']['state_dir'])/'source_inputs'/'new_entry_eligibility').glob('*.json'))
    assert len(files) == 1
    return SourceInputStore.decode(files[0].read_text())['payload']


def test_complete_inactive_tape_excludes_only_new_entry_and_preserves_analysis(pipeline, monkeypatch):
    fixture, owner, config = enable_activity_pipeline(pipeline, monkeypatch)
    result, wire, _ = run_campaign(pipeline)
    assert result['outcome'] == 'clean', result
    frozen = frozen_eligibility(config)
    assert frozen['decisions']['NVDA']['status'] == 'excluded_from_new_entry'
    assert frozen['decisions']['NVDA']['daily_failure_reason'] == 'zero_activity'
    assert frozen['daily_attempts']['NVDA'][-1]['validation_error']
    assert frozen['evidence']['row_count'] == 1
    assert len(owner._metric_store.read_strategy_health(session=SESSION)) == 48
    assert fixture.model_calls['enrichment'] >= 1
    assert all(row['input_coverage_valid'] and row['staging_valid'] for row in wire.values())
    assert all(not any(s['ticker'] == 'NVDA' for s in row['signals']) for row in wire.values())
    assert all(not c['ledger'].pending_intents(datetime(2026, 10, 5).date()) for c in owner.cohorts)
    contexts = [c['ledger'].read_policy_session_context(SESSION)['context']['decision_clock'] for c in owner.cohorts]
    assert all(c == contexts[0] for c in contexts)
    assert contexts[0]['eligibility_digest'] == hashlib.sha256(SourceInputStore.encode(frozen).encode()).hexdigest()
    before, calls = accepted_input_bytes(config), fixture.calls.copy()
    fixture.block_all = True
    repeated, _, _ = run_campaign(pipeline)
    assert repeated['outcome'] == 'clean'
    assert accepted_input_bytes(config) == before and fixture.calls == calls
    assert not fixture.blocked_calls


@pytest.mark.parametrize('tape', ['qualifying', 'malformed'])
def test_incomplete_no_activity_proof_preserves_candidate_failure(pipeline, monkeypatch, tape):
    fixture, owner, config = enable_activity_pipeline(pipeline, monkeypatch, tape=tape)
    result, wire, _ = run_campaign(pipeline)
    assert result['outcome'] == 'degraded', result
    assert frozen_eligibility(config)['decisions']['NVDA']['status'] == 'unresolved'
    assert result['candidate_input_issues']
    assert all(not row['staging_valid'] and not row['intents_staged'] for row in wire.values())
    before, calls = accepted_input_bytes(config), fixture.calls.copy()
    fixture.block_all = True
    repeated, _, _ = run_campaign(pipeline)
    assert repeated['outcome'] == 'degraded'
    assert accepted_input_bytes(config) == before and fixture.calls == calls
    assert not fixture.blocked_calls


@pytest.mark.parametrize('tape,expected', [('non_qualifying', 'clean'), ('qualifying', 'degraded')])
def test_eligibility_resume_after_interrupted_first_publication_is_immutable(pipeline, monkeypatch, tape, expected):
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    fixture, owner, config = enable_activity_pipeline(pipeline, monkeypatch, tape=tape)
    original = MultiStrategyEngine.screen_and_stage
    calls = 0
    def interrupt(engine, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 5:
            raise InterruptedWorker('lost worker after four completed books')
        return original(engine, *args, **kwargs)
    monkeypatch.setattr(MultiStrategyEngine, 'screen_and_stage', interrupt)
    failed = fixture.manager.run_daily(str(SESSION))[GENERATION]
    assert failed['outcome'] == 'failed'
    frozen = frozen_eligibility(config)
    before, provider_calls = accepted_input_bytes(config), fixture.calls.copy()
    monkeypatch.setattr(MultiStrategyEngine, 'screen_and_stage', original)
    fixture.block_all = True
    result, wire, _ = run_campaign(pipeline)
    assert result['outcome'] == expected
    assert frozen_eligibility(config) == frozen
    assert accepted_input_bytes(config) == before and fixture.calls == provider_calls
    assert not fixture.blocked_calls


def test_eligibility_resume_repairs_interruption_before_failed_attempt_record(pipeline, monkeypatch):
    from tradingagents.strategies.metrics.store import MetricStore
    fixture, owner, config = enable_activity_pipeline(pipeline, monkeypatch, tape='qualifying')
    original = MetricStore.save_candidate_bar_recovery
    def interrupt(*args, **kwargs):
        raise InterruptedWorker('lost worker after frozen eligibility before recovery record')
    monkeypatch.setattr(MetricStore, 'save_candidate_bar_recovery', interrupt)
    failed = fixture.manager.run_daily(str(SESSION))[GENERATION]
    assert failed['outcome'] == 'failed'
    frozen = frozen_eligibility(config)
    assert frozen['decisions']['NVDA']['status'] == 'unresolved'
    calls = fixture.calls.copy()
    monkeypatch.setattr(MetricStore, 'save_candidate_bar_recovery', original)
    # Volatility had not been acquired when this worker was interrupted.
    # Resuming may acquire that missing phase, but never the frozen daily/tape.
    original_http = fixture.http
    def forbid_reacquisition(method, url, **kwargs):
        if 'data.alpaca.markets' in url:
            raise AssertionError('frozen eligibility attempted native reacquisition')
        return original_http(method, url, **kwargs)
    fixture.http = forbid_reacquisition
    result, wire, _ = run_campaign(pipeline)
    assert result['outcome'] == 'degraded'
    assert result['candidate_bar_quarantines'] == ['NVDA']
    assert frozen_eligibility(config) == frozen
    assert {k:v for k,v in fixture.calls.items() if k != 'yahoo_sdk'} == {
        k:v for k,v in calls.items() if k != 'yahoo_sdk'}
    assert not fixture.blocked_calls


@pytest.mark.parametrize('tape,expected', [('non_qualifying', 'clean'), ('qualifying', 'degraded')])
def test_partial_replay_retains_failed_subset_from_completed_horizon(pipeline, monkeypatch, tape, expected):
    from tradingagents.strategies.orchestration.cohort_orchestrator import CohortOrchestrator
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    fixture, owner, config = enable_activity_pipeline(pipeline, monkeypatch, tape=tape)
    first_horizon = owner.cohorts[0]['config'].horizon
    original_screen = CohortOrchestrator._screen_for_horizon
    def screen(self, data, trading_date, horizon):
        signals, regime, health = original_screen(self, data, trading_date, horizon)
        if horizon != first_horizon:
            signals = [s for s in signals if s.get('ticker') != 'NVDA']
        return signals, regime, health
    monkeypatch.setattr(CohortOrchestrator, '_screen_for_horizon', screen)
    original_stage = MultiStrategyEngine.screen_and_stage
    calls = 0
    def stage(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 5:
            raise InterruptedWorker('lost worker after first full horizon')
        return original_stage(self, *args, **kwargs)
    monkeypatch.setattr(MultiStrategyEngine, 'screen_and_stage', stage)
    failed = fixture.manager.run_daily(str(SESSION))[GENERATION]
    assert failed['outcome'] == 'failed'
    frozen = frozen_eligibility(config)
    before, provider_calls = accepted_input_bytes(config), fixture.calls.copy()
    monkeypatch.setattr(MultiStrategyEngine, 'screen_and_stage', original_stage)
    fixture.block_all = True
    replay, _, _ = run_campaign(pipeline)
    assert replay['outcome'] == expected
    assert frozen_eligibility(config) == frozen
    assert accepted_input_bytes(config) == before and fixture.calls == provider_calls
    assert not fixture.blocked_calls


@pytest.mark.parametrize('uei,expected', [('VMEFT5X61JT9', 'clean'), ('UNKNOWN00001', 'degraded')])
def test_award_ownership_disposition_survives_full_health_and_replay(pipeline, monkeypatch, uei, expected):
    fixture, owner, config = enable_activity_pipeline(pipeline, monkeypatch)
    original = fixture.http
    def http(method, url, **kwargs):
        if 'api.usaspending.gov' in url and not fixture.block_all:
            fixture.calls[url] += 1
            if '/awards/' in url:
                return fixture.response({}, status=404)
            return fixture.response({'results':[{'Award ID':'FIXTURE-AWARD',
                'Recipient Name':'SYNTHETIC RECIPIENT', 'Recipient UEI':uei,
                'Award Amount':250_000_000, 'Base Obligation Date':'2026-10-01',
                'generated_internal_id':'CONT_AWD_FIXTURE', 'internal_id':42}],
                'page_metadata':{'page':1, 'hasNext':False}})
        return original(method, url, **kwargs)
    fixture.http = http
    result, wire, _ = run_campaign(pipeline)
    assert result['outcome'] == expected
    records = [r for r in owner._metric_store.read_strategy_health(session=SESSION)
               if r.strategy == 'govt_contracts']
    assert len(records) == 4
    if expected == 'clean':
        assert all(r.status == 'legitimate_no_event' for r in records)
        for record in records:
            manifest = record.evidence['admission_manifest']
            assert len(manifest['discovered']) == len(manifest['excluded']) == 1
            assert not manifest['admitted']
            proof = manifest['excluded'][0]['universe']['ownership_disposition']
            assert proof['status'] == 'verified_no_listed_target' and proof['provenance']
    else:
        assert all(r.status == 'data_failure' for r in records)
        assert all(not r.evidence['admission_manifest']['excluded'] for r in records)
    before, calls = accepted_input_bytes(config), fixture.calls.copy()
    fixture.block_all = True
    repeated, _, _ = run_campaign(pipeline)
    assert repeated['outcome'] == expected
    assert accepted_input_bytes(config) == before and fixture.calls == calls
    assert not fixture.blocked_calls


def test_weekend_information_stages_next_open_and_replays_after_open(pipeline, monkeypatch):
    fixture, owner, config, _ = pipeline
    config['autoresearch']['decision_clock_policy'] = decision_clock.PROSPECTIVE
    owner._base_config['autoresearch']['decision_clock_policy'] = decision_clock.PROSPECTIVE
    fixture.now = datetime(2026, 10, 3, 18, tzinfo=timezone.utc)
    monkeypatch.setattr(decision_clock, 'utc_now', lambda: fixture.now)
    result, wire, _ = run_campaign(pipeline)
    assert result['outcome'] == 'clean' and result['success'] is True
    assert all(row['staging_valid'] for row in wire.values())
    contexts = [cohort['ledger'].read_policy_session_context(SESSION)['context']['decision_clock']
                for cohort in owner.cohorts]
    assert all(context == contexts[0] for context in contexts)
    assert contexts[0]['eligible_session'] == '2026-10-05'
    assert contexts[0]['cutoff'].startswith('2026-10-03T')
    files = list((Path(config['autoresearch']['state_dir'])/'source_inputs'/'decision_inputs').glob('*.json'))
    assert len(files) == 1
    accepted = SourceInputStore.decode(files[0].read_text())['payload']
    assert accepted['context'] == contexts[0]
    before, calls = accepted_input_bytes(config), fixture.calls.copy()
    fixture.block_all = True
    fixture.now = datetime(2026, 10, 5, 16, tzinfo=timezone.utc)
    repeated, _, _ = run_campaign(pipeline)
    assert repeated['outcome'] == 'clean'
    assert accepted_input_bytes(config) == before and fixture.calls == calls
    assert not fixture.blocked_calls
