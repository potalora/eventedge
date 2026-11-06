"""Closed-position entitlements reach the native shared cohort acquisition."""
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

from test_30day_simulation import _authoritative_orchestrator
from test_execution_audit_repairs import MON, at, seed, intent
from test_dividend_payment_enrichment import action
from tradingagents.strategies.orchestration.trading_calendar import next_session


def test_shared_pipeline_enriches_closed_symbol_once_and_preserves_replay(tmp_path):
    orch, source = _authoritative_orchestrator(tmp_path, cohorts=2, strategy_modules=[])
    original_actions = source.get_corporate_actions
    source.get_corporate_actions = lambda tickers, day: (
        [replace(action(), fetched_at=datetime.now(timezone.utc))]
        if day == MON and 'AAPL' in tickers else original_actions(tickers, day))
    pay = next_session(MON)
    paid = next_session(pay)
    fetched = []

    def enrich(actions):
        fetched.append(tuple(a.action_id for a in actions))
        return [replace(a, payment_date=pay, payment_source='alpaca-corporate-actions-v1',
                        payment_reference='native-id', payment_observed_at=at(pay)) for a in actions]

    source.enrich_dividend_payment_terms = enrich
    try:
        for cohort in orch.cohorts:
            seed(cohort['ledger'])
            intent(cohort['ledger'], 'close', side='sell')
        first = orch.run_daily(MON.isoformat())
        assert all(row['execution_valid'] for row in first.values()), first
        assert all(not cohort['ledger'].open_exit_positions() for cohort in orch.cohorts)
        observed = orch.run_daily(pay.isoformat())
        assert all(row['execution_valid'] for row in observed.values()), observed
        assert fetched == [('div',)]
        for cohort in orch.cohorts:
            snapshot = cohort['ledger'].read_snapshots(pay, pay)[0]
            assert snapshot.dividend_cash == 0
            assert snapshot.dividend_receivable == Decimal(10)
        results = orch.run_daily(paid.isoformat())
        assert all(row['execution_valid'] for row in results.values()), results
        for cohort in orch.cohorts:
            snapshot = cohort['ledger'].read_snapshots(paid, paid)[0]
            assert snapshot.dividend_cash == Decimal(10)
            assert snapshot.dividend_receivable == 0
        assert not any('AAPL' in symbols and day == pay for symbols, day in source.raw_calls)
        snapshots = [cohort['ledger'].read_snapshots(paid, paid)[0] for cohort in orch.cohorts]
        calls = (len(source.raw_calls), len(source.action_calls))
        replay = orch.run_daily(paid.isoformat())
        assert all(row['execution_valid'] for row in replay.values()), replay
        assert calls == (len(source.raw_calls), len(source.action_calls))
        assert snapshots == [cohort['ledger'].read_snapshots(paid, paid)[0] for cohort in orch.cohorts]
        assert fetched == [('div',)]
    finally:
        for cohort in orch.cohorts:
            cohort['ledger'].close()
