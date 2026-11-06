"""Prospective research holds against real daily phases and sixteen ledgers.

Only provider/model response boundaries use the existing offline transport
fixture. Account fills, protective exits, atomic staging and resumes are real.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path

import pytest
import requests

from test_source_reliability_pipeline import (
    GENERATION, SESSION, InterruptedWorker, accepted_input_bytes, pipeline,
)
from test_session_executor import _intent
from test_portfolio_policy_ledger import _record_intent_policy, _record_signal_policy
from tradingagents.strategies.execution import Fill, OrderIntent, stable_id
from tradingagents.strategies.orchestration.trading_calendar import next_session, previous_session, session_close


def _enable(pipeline):
    fixture, owner, config, _ = pipeline
    config['autoresearch']['research_resilience_policy'] = 'dependency_isolation_v1'
    return fixture, owner, config


def _seed_position(ledger, ticker, *, price, opened, quantity=1):
    intent = _intent(ledger, ticker, 'buy', opened, quantity,
        reference_session=previous_session(opened), strategy='earnings_call')
    signal = ledger.signals_for_intent(intent.intent_id)[0]
    _record_signal_policy(ledger, signal)
    _record_intent_policy(ledger, intent, signal)
    fill = Fill(stable_id('fill', intent.intent_id, opened, quantity), intent.intent_id,
        'buy', opened, datetime.combine(opened, datetime.min.time(), tzinfo=timezone.utc).replace(hour=13, minute=30),
        datetime.combine(opened, datetime.min.time(), tzinfo=timezone.utc).replace(hour=22),
        Decimal(price), Decimal(price), quantity, Decimal('0'), Decimal('0'), Decimal('0'))
    ledger.apply_fill(intent, fill)
    return intent


def _seed_books(owner, exit_kind='time'):
    """Keep one stop safe and make one deterministic exit due."""
    result = {}
    for cohort in owner.cohorts:
        ledger = cohort['ledger']
        old = _seed_position(ledger, 'AAPL', price='100' if exit_kind == 'time' else '200',
            opened=date(2025, 1, 2) if exit_kind == 'time' else date(2026, 9, 30))
        kept = _seed_position(ledger, 'MSFT', price='100', opened=date(2026, 9, 30))
        lot = next(row for row in ledger.open_exit_positions() if row['ticker'] == 'MSFT')
        stop = OrderIntent(stable_id('fixture_stop', ledger.cohort_id, 'MSFT'), kept.signal_ids,
            ledger.cohort_id, 'sell', 1, session_close(date(2026, 9, 30)), date(2026, 10, 1),
            'resting_stop', 'pending', Decimal('90'), None)
        ledger.stage_exit_intent(stop, ((str(lot['lot_id']), 1),))
        result[cohort['config'].name] = {'stop': stop, 'exit': old}
    assert len(result) == 16 and len({cohort['config'].horizon for cohort in owner.cohorts}) == 4
    return result


def _hold_fault(fixture, monkeypatch, hold):
    fixture.fault = 'edgar_partial'
    if hold == 'regime':
        from tradingagents.strategies.data_sources.fred_source import FREDSource
        def unavailable(*args, **kwargs):
            fixture.calls['fred_unavailable'] += 1
            raise requests.Timeout('offline required shared regime source unavailable')
        monkeypatch.setattr(FREDSource, '_get_series', unavailable)
    else:
        from tradingagents.strategies import llm_utils
        from tradingagents.strategies.runtime_deadline import ModelDeadlineExceeded
        def exhausted(*args, **kwargs):
            fixture.model_calls['deadline'] += 1
            raise ModelDeadlineExceeded('model_deadline_exhausted')
        monkeypatch.setattr(llm_utils, 'call_analysis_model', exhausted)


def _run(pipeline):
    fixture, _, _, _ = pipeline
    from tradingagents.strategies.orchestration.generation_manager import _extract_cohort_results
    fixture.now += timedelta(seconds=1)
    result = fixture.manager.run_daily(str(fixture.session))[GENERATION]
    artifact = json.loads(Path(result['evidence_path']).read_text())
    return result, _extract_cohort_results(artifact['stdout']), artifact


def _assert_safe_hold(owner, seeded, result, wire):
    assert result['outcome'] == 'degraded', result
    assert len(wire) == 16
    assert all(not row['error'] and row['execution_valid'] and row['staging_valid'] for row in wire.values()), wire
    assert result['input_coverage_valid'] is False
    next_day = next_session(SESSION)
    for cohort in owner.cohorts:
        name, ledger = cohort['config'].name, cohort['ledger']
        pending = ledger.pending_intents(next_day)
        assert not [intent for intent in pending if intent.side in ('buy', 'short')], (name, pending)
        current = ledger.read_fills(SESSION, SESSION)
        assert not current and not wire[name]['trades_opened']
        assert ledger.intent(seeded[name]['stop'].intent_id) == seeded[name]['stop']
        exits = [intent for intent in ledger.pending_exit_intents('AAPL')
                 if intent.price_rule == 'next_session_open']
        assert len(exits) == 1 and exits[0].requested_qty == 1
        assert exits[0].eligible_session == next_day
        assert ledger._connection.execute('SELECT COUNT(*) FROM staging_runs WHERE session=?',
            (str(SESSION),)).fetchone()[0] == 1


def _durable_book_snapshot(owner):
    return {cohort['config'].name: {
        'fills': tuple(cohort['ledger'].read_fills()),
        'exits': tuple(cohort['ledger'].pending_exit_intents(ticker)
            for ticker in ('AAPL', 'MSFT', 'GOOGL')),
        'intents': tuple(cohort['ledger'].pending_intents(next_session(SESSION))),
        'cash': tuple(tuple(row) for row in cohort['ledger']._connection.execute(
            'SELECT * FROM cash_events ORDER BY cash_event_id').fetchall()),
        'staging': cohort['ledger']._connection.execute('SELECT COUNT(*) FROM staging_runs WHERE session=?',
            (str(SESSION),)).fetchone()[0],
    } for cohort in owner.cohorts}


@pytest.mark.parametrize('hold', ['regime', 'model_deadline'])
@pytest.mark.parametrize('exit_kind', ['time', 'price'])
def test_research_hold_preserves_exits_stops_prior_fills_and_zero_new_entries_in_16_books(pipeline, monkeypatch, hold, exit_kind):
    fixture, owner, config = _enable(pipeline)
    seeded = _seed_books(owner, exit_kind)
    _hold_fault(fixture, monkeypatch, hold)
    result, wire, _ = _run(pipeline)
    _assert_safe_hold(owner, seeded, result, wire)
    health = owner._metric_store.read_strategy_health(session=SESSION)
    assert len(health) == 48
    assert any(row.status == 'data_failure' for row in health)
    accepted = accepted_input_bytes(config)
    assert any(name.startswith('screening/') for name in accepted)
    durable = _durable_book_snapshot(owner)
    calls, models = fixture.calls.copy(), fixture.model_calls.copy()
    fixture.block_all = True
    repeated, repeated_wire, _ = _run(pipeline)
    _assert_safe_hold(owner, seeded, repeated, repeated_wire)
    assert accepted_input_bytes(config) == accepted
    assert _durable_book_snapshot(owner) == durable
    assert fixture.calls == calls and fixture.model_calls == models and not fixture.blocked_calls


def _interrupt_hold_after_five_staging_commits(pipeline, monkeypatch):
    from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger
    fixture, owner, config = _enable(pipeline)
    seeded = _seed_books(owner)
    _hold_fault(fixture, monkeypatch, 'regime')
    original = PortfolioLedger.complete_staging
    committed = []
    def stop_after_commit(ledger, session, *args, **kwargs):
        result = original(ledger, session, *args, **kwargs)
        if session == SESSION and result[0]:
            committed.append(ledger.cohort_id)
            if len(committed) == 5:
                raise InterruptedWorker('worker lost after fifth real staging transaction')
        return result
    monkeypatch.setattr(PortfolioLedger, 'complete_staging', stop_after_commit)
    result, _, artifact = _run(pipeline)
    assert result['outcome'] == 'failed' and artifact['process_status'] == 'timeout'
    assert len(committed) == 5
    assert sum(cohort['ledger']._connection.execute('SELECT COUNT(*) FROM staging_runs WHERE session=?',
        (str(SESSION),)).fetchone()[0] for cohort in owner.cohorts) == 5
    monkeypatch.setattr(PortfolioLedger, 'complete_staging', original)
    fixture.block_all = True
    return fixture, owner, config, seeded


def test_interrupted_hold_restores_entire_sample_and_commits_remaining_books_once(pipeline, monkeypatch):
    fixture, owner, config, seeded = _interrupt_hold_after_five_staging_commits(pipeline, monkeypatch)
    accepted = accepted_input_bytes(config)
    assert any(name.startswith('screening/') for name in accepted)
    calls, models = fixture.calls.copy(), fixture.model_calls.copy()
    existing = {cohort['config'].name: tuple(cohort['ledger'].read_fills()) for cohort in owner.cohorts}
    result, wire, artifact = _run(pipeline)
    _assert_safe_hold(owner, seeded, result, wire)
    assert artifact['process_status'] == 'completed'
    assert fixture.calls == calls and fixture.model_calls == models and not fixture.blocked_calls
    assert accepted_input_bytes(config) == accepted
    assert {cohort['config'].name: tuple(cohort['ledger'].read_fills()) for cohort in owner.cohorts} == existing
    complete = _durable_book_snapshot(owner)
    again, again_wire, _ = _run(pipeline)
    _assert_safe_hold(owner, seeded, again, again_wire)
    assert _durable_book_snapshot(owner) == complete


@pytest.mark.parametrize('damage', ['screening', 'volatility', 'account'])
def test_research_hold_does_not_hide_corrupt_accepted_state_on_resume(pipeline, monkeypatch, damage):
    fixture, owner, config, _ = _interrupt_hold_after_five_staging_commits(pipeline, monkeypatch)
    calls, models = fixture.calls.copy(), fixture.model_calls.copy()
    fills = {cohort['config'].name: tuple(cohort['ledger'].read_fills()) for cohort in owner.cohorts}
    damaged_path = None
    if damage in {'screening', 'volatility'}:
        folder = 'screening' if damage == 'screening' else 'staging_volatility'
        paths = list((Path(config['autoresearch']['state_dir']) / 'source_inputs' / folder).glob('*.json'))
        assert len(paths) == 1, (damage, paths)
        damaged_path = paths[0]
        damaged_path.write_text('{}')
    else:
        cohort = owner.cohorts[7]
        cohort['ledger']._connection.execute('UPDATE accounting_state SET cash=? WHERE cohort_id=?',
            ('999999', cohort['config'].name))
    result, wire, _ = _run(pipeline)
    assert result['outcome'] == 'failed' and result['success'] is False
    if damage == 'account':
        # A corrupted accepted phase chain fails before a cohort wire envelope
        # can be emitted. Do not require fabricated accounting result rows.
        if wire is None:
            assert 'governed session state conflict' in result['error']
        else:
            assert wire[owner.cohorts[7]['config'].name]['execution_valid'] is False
    else:
        assert any(row['error'] for row in wire.values()), wire
        assert any(row['staging_valid'] is False for row in wire.values())
        assert damaged_path.read_text() == '{}'
    assert fixture.calls == calls and fixture.model_calls == models and not fixture.blocked_calls
    assert {cohort['config'].name: tuple(cohort['ledger'].read_fills()) for cohort in owner.cohorts} == fills
    assert not any(intent.side in {'buy', 'short'} for cohort in owner.cohorts
        for intent in cohort['ledger'].pending_intents(next_session(SESSION)))


def test_untrusted_held_bar_blocks_execution_despite_classified_research_outage(pipeline, monkeypatch):
    fixture, owner, _ = _enable(pipeline)
    seeded = _seed_books(owner)
    _hold_fault(fixture, monkeypatch, 'regime')
    original = fixture.http
    def corrupt_held_bar(method, url, **kwargs):
        response = original(method, url, **kwargs)
        if 'data.alpaca.markets' in url:
            payload = response.json()
            rows = payload.get('bars', {})
            if isinstance(rows, dict) and 'AAPL' in rows:
                rows['AAPL'][0]['l'] = rows['AAPL'][0]['h'] + 1
            elif payload.get('symbol') == 'AAPL' and isinstance(rows, list):
                rows[0]['l'] = rows[0]['h'] + 1
            return fixture.response(payload)
        return response
    fixture.http = corrupt_held_bar
    before = _durable_book_snapshot(owner)
    result, wire, _ = _run(pipeline)
    assert result['outcome'] == 'failed'
    assert len(wire) == 16 and all(row['error'] and not row['execution_valid'] for row in wire.values())
    for cohort in owner.cohorts:
        name, ledger = cohort['config'].name, cohort['ledger']
        assert tuple(ledger.read_fills()) == before[name]['fills']
        assert ledger.intent(seeded[name]['stop'].intent_id) == seeded[name]['stop']
        assert not ledger.pending_exit_intents('AAPL')
        assert ledger._connection.execute('SELECT COUNT(*) FROM staging_runs WHERE session=?',
            (str(SESSION),)).fetchone()[0] == 0


def test_prior_accepted_entry_fills_remain_truthful_when_today_research_holds(pipeline, monkeypatch):
    fixture, owner, config = _enable(pipeline)
    first, _, _ = _run(pipeline)
    assert first['outcome'] == 'clean'
    following = next_session(SESSION)
    due = {cohort['config'].name: {intent.intent_id for intent in cohort['ledger'].pending_intents(following)
        if intent.side == 'buy'} for cohort in owner.cohorts}
    assert all(due.values())
    fixture.session = following
    fixture.now = datetime.combine(following, datetime.min.time(), tzinfo=timezone.utc).replace(hour=22)
    _hold_fault(fixture, monkeypatch, 'model_deadline')
    result, wire, _ = _run(pipeline)
    assert result['outcome'] == 'degraded'
    assert all(not row['error'] and row['execution_valid'] and row['staging_valid'] for row in wire.values())
    for cohort in owner.cohorts:
        name, ledger = cohort['config'].name, cohort['ledger']
        fills = ledger.read_fills(following, following)
        assert len(fills) == 1 and fills[0].intent_id in due[name]
        assert wire[name]['trades_opened'] == [fills[0].fill_id]
        assert not any(intent.side in {'buy', 'short'} for intent in ledger.pending_intents(next_session(following)))
        assert ledger.pending_exit_intents('NVDA'), 'filled holdings retain protective exits during research hold'
    calls, models = fixture.calls.copy(), fixture.model_calls.copy()
    frozen = accepted_input_bytes(config)
    previous = {cohort['config'].name: tuple(cohort['ledger'].read_fills()) for cohort in owner.cohorts}
    fixture.block_all = True
    again, _, _ = _run(pipeline)
    assert again['outcome'] == 'degraded'
    assert accepted_input_bytes(config) == frozen
    assert fixture.calls == calls and fixture.model_calls == models and not fixture.blocked_calls
    assert {cohort['config'].name: tuple(cohort['ledger'].read_fills()) for cohort in owner.cohorts} == previous


def test_completed_hold_missing_screening_checkpoint_fails_without_research_calls(pipeline, monkeypatch):
    fixture, owner, config = _enable(pipeline)
    seeded = _seed_books(owner)
    _hold_fault(fixture, monkeypatch, 'regime')
    first, wire, _ = _run(pipeline)
    _assert_safe_hold(owner, seeded, first, wire)
    paths = list((Path(config['autoresearch']['state_dir']) / 'source_inputs' / 'screening').glob('*.json'))
    assert len(paths) == 1
    paths[0].unlink()
    accepted = accepted_input_bytes(config)
    durable = _durable_book_snapshot(owner)
    calls, models = fixture.calls.copy(), fixture.model_calls.copy()
    fixture.block_all = True
    repeated, repeated_wire, _ = _run(pipeline)
    assert repeated['outcome'] == 'failed', repeated
    assert repeated['success'] is False
    if repeated_wire is not None:
        assert any(row['error'] for row in repeated_wire.values())
        assert any(not row['staging_valid'] for row in repeated_wire.values())
    assert not paths[0].exists()
    assert accepted_input_bytes(config) == accepted
    assert _durable_book_snapshot(owner) == durable
    assert fixture.calls == calls and fixture.model_calls == models and not fixture.blocked_calls


@pytest.mark.parametrize('hold', ['regime', 'model_deadline'])
def test_completed_replay_preserves_accepted_research_regime_and_health(pipeline, monkeypatch, hold):
    fixture, owner, config = _enable(pipeline)
    seeded = _seed_books(owner)
    _hold_fault(fixture, monkeypatch, hold)
    first, wire, _ = _run(pipeline)
    _assert_safe_hold(owner, seeded, first, wire)
    regimes = {name: row['regime'] for name, row in wire.items()}
    assert all(regimes.values())
    assert all(regime['research_source_scope']['policy'] == 'dependency_isolation_v1'
        for regime in regimes.values())
    health = owner._metric_store.read_strategy_health(session=SESSION)
    frozen = accepted_input_bytes(config)
    calls, models = fixture.calls.copy(), fixture.model_calls.copy()
    fixture.block_all = True
    repeated, repeated_wire, _ = _run(pipeline)
    _assert_safe_hold(owner, seeded, repeated, repeated_wire)
    assert {name: row['regime'] for name, row in repeated_wire.items()} == regimes
    assert owner._metric_store.read_strategy_health(session=SESSION) == health
    assert accepted_input_bytes(config) == frozen
    assert fixture.calls == calls and fixture.model_calls == models and not fixture.blocked_calls
