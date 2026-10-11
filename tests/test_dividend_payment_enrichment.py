"""Verified payable-date enrichment preserves ex-date entitlement and replay."""
from dataclasses import replace
from decimal import Decimal as D
from unittest.mock import Mock, patch

import pandas as pd
import pytest

from test_execution_audit_repairs import MON, at, cfg, intent, seed
from test_inventory_continuity import Source
from tradingagents.strategies.execution import CorporateAction
from tradingagents.strategies.execution.price_source import YFinancePriceSource
from tradingagents.strategies.orchestration.session_executor import SessionExecutor
from tradingagents.strategies.orchestration.trading_calendar import next_session
from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger


def action():
    return CorporateAction('div', 'AAPL', MON, 'cash_dividend', None, D(1), 'yfinance', at(MON), True)


def native_row(**changes):
    return {'id': 'provider-id', 'symbol': 'AAPL', 'ex_date': str(MON),
            'payable_date': str(next_session(MON)), 'rate': 1.0, 'currency': 'USD',
            'process_date': str(next_session(MON)), 'special': False, 'foreign': False, **changes}


def provider(monkeypatch, rows, now):
    monkeypatch.setenv('ALPACA_API_KEY', 'test')
    monkeypatch.setenv('ALPACA_SECRET_KEY', 'test')
    response = Mock(status_code=200)
    response.json.return_value = {'corporate_actions': {'cash_dividends': rows}, 'next_page_token': None}
    source = YFinancePriceSource(now=lambda: at(now))
    return source, response


def test_normal_yahoo_action_gets_verified_native_payment_date(monkeypatch):
    pay = next_session(MON)
    source, response = provider(monkeypatch, [native_row()], MON)
    frame = pd.DataFrame({'Dividends': [1.0], 'Stock Splits': [0.0]}, index=pd.to_datetime([MON]))
    with patch.object(source, '_raw_frame', return_value=(frame, at(MON))), patch('tradingagents.strategies.execution.price_source.YFinancePriceSource._payment_terms_get', return_value=response.json.return_value):
        result = source.get_corporate_actions(['AAPL'], MON)[0]
    assert result.payment_date == pay
    assert result.payment_source == 'alpaca-corporate-actions-v1'
    assert result.payment_reference == 'provider-id'
    assert result.payment_observed_at == at(MON)


@pytest.mark.parametrize('side', ['buy', 'short'])
@pytest.mark.parametrize('crash_phase', ['apply_corporate_actions', 'execute_exits'])
def test_late_terms_after_close_settle_once_without_rewriting_ex_date(tmp_path, side, crash_phase):
    ledger = PortfolioLedger(tmp_path/'p.db', 'cohort', D(10000))
    seed(ledger, side=side)
    intent(ledger, 'exit', side='sell' if side == 'buy' else 'cover')
    executor = SessionExecutor(ledger, cfg())
    first = executor.execute_open_and_mark(MON, 'epoch', Source(MON, [action()]), {'AAPL': D('.01')}, at(MON))
    assert first.valid, first.invalid_reason
    assert not ledger.open_exit_positions()
    original = dict(ledger.connection.execute('SELECT * FROM corporate_actions').fetchone())
    pay = next_session(MON)
    observed = next_session(pay)
    assert len(ledger.pending_dividend_actions()) == 1
    source = Source(observed)
    source.enrich_dividend_payment_terms = lambda actions: [replace(a, payment_date=pay, payment_source='alpaca-corporate-actions-v1', payment_reference='provider-id', payment_observed_at=at(observed)) for a in actions]
    def crash(phase):
        if phase == crash_phase:
            raise RuntimeError('power loss')
    with pytest.raises(RuntimeError, match='power loss'):
        SessionExecutor(ledger, cfg(), after_phase_commit=crash).execute_open_and_mark(observed, 'epoch', source, {}, at(observed))
    recovered = executor.execute_open_and_mark(observed, 'epoch', Source(observed), {}, at(observed))
    assert recovered.valid, recovered.invalid_reason
    signed = D(10) if side == 'buy' else D(-10)
    assert recovered.snapshot.dividend_cash == 0
    assert recovered.snapshot.dividend_receivable == signed
    settlement = next_session(observed)
    paid = executor.execute_open_and_mark(settlement, 'epoch', Source(settlement), {}, at(settlement))
    assert paid.valid, paid.invalid_reason
    assert paid.snapshot.dividend_cash == signed
    assert paid.snapshot.dividend_receivable == 0
    assert dict(ledger.connection.execute('SELECT * FROM corporate_actions').fetchone()) == original
    assert ledger.read_snapshots(MON, MON)[0] == first.snapshot
    assert ledger.connection.execute("SELECT COUNT(*) FROM cash_events WHERE event_type='dividend'").fetchone()[0] == 1
    assert ledger.connection.execute('SELECT COUNT(*) FROM dividend_payment_observations').fetchone()[0] == 1
    assert not ledger.pending_dividend_actions()
    assert executor.execute_open_and_mark(settlement, 'epoch', Source(settlement), {}, at(settlement)).snapshot == paid.snapshot
    ledger.close()


@pytest.mark.parametrize('change', [
    {'payable_date': None}, {'payable_date': '2020-01-01'}, {'rate': 2},
    {'currency': 'EUR'}, {'id': ''},
    {'due_bill_on_date': str(MON)}, {'sub_type': 'return_of_capital'},
])
def test_unverified_or_unsupported_native_terms_remain_unknown(monkeypatch, change):
    source, response = provider(monkeypatch, [native_row(**change)], next_session(MON))
    with patch('tradingagents.strategies.execution.price_source.YFinancePriceSource._payment_terms_get', return_value=response.json.return_value):
        assert source.enrich_dividend_payment_terms([action()]) == [action()]


def test_ambiguous_or_incomplete_pagination_cannot_credit_cash(monkeypatch):
    source, response = provider(monkeypatch, [native_row(), native_row(id='other')], next_session(MON))
    with patch('tradingagents.strategies.execution.price_source.YFinancePriceSource._payment_terms_get', return_value=response.json.return_value) as request:
        assert source.enrich_dividend_payment_terms([action()]) == [action()]
        response.json.return_value['next_page_token'] = 'remaining'
        assert source.enrich_dividend_payment_terms([action()]) == [action()]
        assert request.call_count == 4


def test_unknown_to_known_requires_provenance_and_conflicts_never_overwrite(tmp_path):
    ledger = PortfolioLedger(tmp_path/'p.db', 'cohort', D(10000))
    seed(ledger)
    ledger.apply_corporate_actions(MON, [action()], at(MON))
    pay = next_session(MON)
    unverified = replace(action(), payment_date=pay)
    assert ledger.corporate_action_batch_errors(pay, (unverified,), at(pay), allow_prior=True)
    verified = replace(unverified, payment_source='alpaca-corporate-actions-v1', payment_reference='native', payment_observed_at=at(pay))
    assert not ledger.corporate_action_batch_errors(pay, (verified,), at(pay), allow_prior=True)
    ledger.apply_corporate_actions(pay, [verified], at(pay), allow_prior=True)
    assert ledger.corporate_action_batch_errors(pay, (replace(verified, payment_date=next_session(pay)),), at(pay), allow_prior=True)
    assert ledger.corporate_action_batch_errors(pay, (replace(verified, cash_per_share=D(2)),), at(pay), allow_prior=True)
    ledger.apply_corporate_actions(pay, [action()], at(pay), allow_prior=True)
    ledger.settle_dividends(pay, at(pay))
    assert ledger.account_state().cash == 9000
    ledger.settle_dividends(next_session(pay), at(next_session(pay)))
    ledger.settle_dividends(next_session(pay), at(next_session(pay)))
    assert ledger.account_state().cash == 9010
    ledger.close()


def test_payment_observation_cannot_backdate_into_prior_session(tmp_path):
    ledger = PortfolioLedger(tmp_path/'p.db', 'cohort', D(10000))
    seed(ledger)
    ledger.apply_corporate_actions(MON, [action()], at(MON))
    pay = next_session(MON)
    observed = next_session(pay)
    late = replace(action(), payment_date=pay, payment_source='alpaca-corporate-actions-v1', payment_reference='native', payment_observed_at=at(observed))
    assert ledger.corporate_action_batch_errors(pay, (late,), at(observed), allow_prior=True)
    assert ledger.corporate_action_batch_errors(observed, (late,), at(pay), allow_prior=True)
    assert ledger.dividend_receivable() == 10
    assert ledger.account_state().cash == 9000
    ledger.close()


@pytest.mark.parametrize('currency', [None, ''])
def test_missing_optional_currency_enriches_date_only(monkeypatch, currency):
    source, response = provider(monkeypatch, [native_row(currency=currency)], next_session(MON))
    with patch('tradingagents.strategies.execution.price_source.YFinancePriceSource._payment_terms_get', return_value=response.json.return_value):
        enriched = source.enrich_dividend_payment_terms([action()])[0]
    assert enriched.payment_date == next_session(MON)
    assert replace(enriched, payment_date=None, payment_source='', payment_reference='', payment_observed_at=None) == action()


def test_enrichment_paginates_process_dates_then_matches_ex_date(monkeypatch):
    source, response = provider(monkeypatch, [native_row()], next_session(MON))
    first = Mock(status_code=200)
    first.json.return_value = {'corporate_actions': {'cash_dividends': [native_row(ex_date='2020-01-01')]}, 'next_page_token': 'second'}
    with patch('tradingagents.strategies.execution.price_source.YFinancePriceSource._payment_terms_get', side_effect=[first.json.return_value, response.json.return_value]) as get:
        result = source.enrich_dividend_payment_terms([action()])[0]
    assert result.payment_date == next_session(MON)
    assert get.call_count == 2
    assert get.call_args.kwargs['params']['page_token'] == 'second'
    assert get.call_args.kwargs['params']['start'] < str(MON)
    assert get.call_args.kwargs['params']['end'] > str(next_session(MON))


@pytest.mark.parametrize('status', [401, 403, 429, 500])
def test_native_provider_failure_never_creates_payment_terms(monkeypatch, status):
    source, response = provider(monkeypatch, [native_row()], next_session(MON))
    with patch('tradingagents.strategies.runtime_deadline.bounded_transport', return_value={'status_code': status, 'body': '{}'}) as get:
        assert source.enrich_dividend_payment_terms([action()]) == [action()]
        assert get.call_count == 1


def test_crash_inside_payment_rolls_back_cash_and_settlement_together(tmp_path):
    ledger = PortfolioLedger(tmp_path/'p.db', 'cohort', D(10000))
    seed(ledger)
    pay = next_session(MON)
    known = replace(action(), payment_date=pay, payment_source='alpaca-corporate-actions-v1', payment_reference='native', payment_observed_at=at(MON))
    ledger.apply_corporate_actions(MON, [known], at(MON))
    with patch.object(ledger, '_update_accounting_summary', side_effect=RuntimeError('power loss')):
        with pytest.raises(RuntimeError, match='power loss'):
            ledger.settle_dividends(pay, at(pay))
    assert ledger.connection.execute("SELECT COUNT(*) FROM cash_events WHERE event_type='dividend'").fetchone()[0] == 0
    assert ledger.dividend_receivable() == 10
    ledger.settle_dividends(pay, at(pay))
    ledger.settle_dividends(pay, at(pay))
    assert ledger.account_state().cash == 9010
    assert ledger.dividend_receivable() == 0
    ledger.close()


@pytest.mark.parametrize('mutation', ['observation_time', 'observation_delete', 'terms_date', 'unchanged'])
def test_payment_phase_resume_binds_retained_terms_and_observation(tmp_path, mutation):
    ledger = PortfolioLedger(tmp_path/'p.db', 'cohort', D(10000))
    seed(ledger)
    pay = next_session(MON)
    known = replace(action(), payment_date=pay, payment_source='alpaca-corporate-actions-v1', payment_reference='native', payment_observed_at=at(MON))
    executor = SessionExecutor(ledger, cfg())
    assert executor.execute_open_and_mark(MON, 'epoch', Source(MON, [known]), {}, at(MON)).valid
    def crash(phase):
        if phase == 'validate_market_data':
            raise RuntimeError('power loss')
    with pytest.raises(RuntimeError, match='power loss'):
        SessionExecutor(ledger, cfg(), after_phase_commit=crash).execute_open_and_mark(pay, 'epoch', Source(pay), {}, at(pay))
    digest = ledger.execution_governed_state_digest(pay)
    if mutation == 'observation_time':
        ledger.connection.execute('UPDATE dividend_payment_observations SET observed_at=?', (at(next_session(pay)).isoformat(),))
    elif mutation == 'observation_delete':
        ledger.connection.execute('DELETE FROM dividend_payment_observations')
    elif mutation == 'terms_date':
        ledger.connection.execute('UPDATE dividend_payment_terms SET payment_date=?', (next_session(pay).isoformat(),))
    changed_digest = ledger.execution_governed_state_digest(pay)
    result = executor.execute_open_and_mark(pay, 'epoch', Source(pay), {}, at(pay))
    if mutation == 'unchanged':
        assert result.valid, result.invalid_reason
        assert result.snapshot.dividend_cash == 10
        assert result.snapshot.dividend_receivable == 0
    else:
        assert changed_digest != digest
        assert not result.valid
        assert 'state' in result.invalid_reason and 'conflict' in result.invalid_reason
        assert not ledger.phase_completed(pay, 'apply_corporate_actions')
        assert ledger.connection.execute("SELECT COUNT(*) FROM cash_events WHERE event_type='dividend'").fetchone()[0] == 0
        assert ledger.dividend_receivable() == 10
    ledger.close()


def test_retained_payment_state_legacy_read_only_missing_tables(tmp_path):
    import sqlite3
    path = tmp_path/'p.db'
    ledger = PortfolioLedger(path, 'cohort', D(10000))
    seed(ledger)
    ledger.apply_corporate_actions(MON, [action()], at(MON))
    ledger.close()
    connection = sqlite3.connect(path)
    connection.execute('DROP TABLE dividend_payment_observations')
    connection.execute('DROP TABLE dividend_payment_terms')
    connection.commit()
    connection.close()
    before = path.read_bytes()
    ledger = PortfolioLedger.open_existing(path)
    state = ledger.execution_starting_state(next_session(MON))
    assert state['dividend_receivables']
    assert state['dividend_payment_terms'] == []
    assert state['dividend_payment_observations'] == []
    ledger.close()
    assert path.read_bytes() == before
