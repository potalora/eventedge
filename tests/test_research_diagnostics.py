"""Computable diagnostics use declared scope and dependence-aware uncertainty."""
from datetime import date

import pytest


def series(count=40):
    from tradingagents.strategies.metrics.calendar import XNYSCalendar
    calendar = XNYSCalendar()
    day = date(2026, 8, 3)
    history, spy, bil = [], [], []
    equity = 100000.
    for index in range(count):
        if index:
            equity *= 1 + (.002 if index % 10 < 5 else -.001)
            day = calendar.next_session(day)
        history.append({'session': day.isoformat(), 'net_equity': equity,
            'gross_equity': equity + index * 10, 'net_exposure': equity * .5,
            'gross_exposure': equity * .5, 'cash': equity * .5,
            'cumulative_costs': {'slippage': index * 10, 'commission': 0, 'other_fees': 0, 'borrow': 0, 'financing': 0}})
        spy.append({'session': day.isoformat(), 'close': 100 * 1.001**index,
                    'return_basis': 'paired_total_return_index_v2'})
        bil.append({'session': day.isoformat(), 'close': 100 * 1.0001**index,
                    'return_basis': 'paired_total_return_index_v2'})
    return {'net_equity_history': history, 'benchmarks': {'SPY': spy, 'BIL': bil},
            'benchmark_unavailable_reason': None}


def test_cost_stress_uses_incremental_costs_and_fixed_execution_scope():
    from tradingagents.strategies.metrics.research import research_diagnostics
    report = research_diagnostics(series(3))
    scenarios = report['cost_sensitivity']['scenarios']
    assert scenarios[1]['cost_multiplier'] == 2
    assert scenarios[1]['total_return'] == pytest.approx(scenarios[0]['total_return'] - 20 / 100000)
    assert report['cost_sensitivity']['scope'] == 'fixed recorded executions; no strategy feedback or re-optimization'


def test_realized_largest_contributor_stress_has_explicit_scope():
    from tradingagents.strategies.metrics.research import research_diagnostics
    result = research_diagnostics(series(3), realized_contributions={'AAPL': 1000., 'MSFT': -100.})
    concentration = result['largest_contributor_stress']
    assert concentration['largest_contributor'] == 'AAPL'
    assert concentration['realized_pnl_without_largest'] == -100.
    assert concentration['scope'] == 'realized lot PnL only; excludes open marks, dividends and financing'
    assert concentration['portfolio_return_without_largest'] is None


def test_attribution_uses_lagged_exposure_and_reconciles_portfolio_return():
    from tradingagents.strategies.metrics.research import research_diagnostics
    report = research_diagnostics(series(40))
    attribution = report['market_exposure_attribution']
    assert attribution['status'] == 'available'
    assert attribution['market_contribution'] > 0 and attribution['cash_contribution'] > 0
    assert sum(attribution[key] for key in ('market_contribution', 'cash_contribution', 'residual_contribution')) == pytest.approx(attribution['portfolio_total_return'])
    assert 'not causal alpha' in attribution['scope']


def test_uncertainty_is_block_dependent_deterministic_and_never_pools_books():
    from tradingagents.strategies.metrics.research import research_diagnostics
    first = research_diagnostics(series(40))
    second = research_diagnostics(series(40))
    uncertainty = first['dependence_aware_uncertainty']
    assert uncertainty == second['dependence_aware_uncertainty']
    assert uncertainty['method'] == 'circular_moving_block_bootstrap'
    assert uncertainty['block_lengths'][0]['block_length'] == 5
    assert uncertainty['block_lengths'][0]['status'] == 'available'
    assert uncertainty['block_lengths'][-1]['status'] == 'insufficient_evidence'
    assert uncertainty['pooled_scenarios'] is False
    assert first['model_calibration']['status'] == 'insufficient_evidence'
    assert first['executable_fill_validation']['status'] == 'insufficient_evidence'


def test_missing_history_benchmarks_and_per_name_pnl_stay_unavailable():
    from tradingagents.strategies.metrics.research import research_diagnostics
    report = research_diagnostics({'net_equity_history': [], 'benchmarks': {}})
    for key in ('cost_sensitivity', 'largest_contributor_stress', 'market_exposure_attribution'):
        assert report[key]['status'] == 'insufficient_evidence'
    assert report['dependence_aware_uncertainty']['block_lengths'][0]['status'] == 'insufficient_evidence'
