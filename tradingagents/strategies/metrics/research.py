"""Descriptive research diagnostics on one validated dependent scenario book.

Stress calculations hold the recorded trades fixed. They are sensitivity
analyses, not executable counterfactual portfolios or evidence of causal alpha.
"""
from __future__ import annotations

import math
import random
import statistics


def _unavailable(reason: str, **details) -> dict:
    return dict(status='insufficient_evidence', reason=reason, **details)


def _quantile(ordered: list[float], probability: float) -> float:
    position = (len(ordered) - 1) * probability
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def block_uncertainty(returns: list[float], *, block_lengths=(5, 10, 20), replications=1000) -> dict:
    """Seeded circular blocks retain within-block serial dependence.

    At least six blocks are required. Scenarios share evidence and must never be
    pooled as independent samples. This is a descriptive finite-sample interval.
    """
    output = {'method': 'circular_moving_block_bootstrap', 'pooled_scenarios': False,
              'replications': replications, 'seed': 0, 'return_count': len(returns),
              'confidence_level': .95, 'block_lengths': [],
              'scope': 'single cohort daily returns; descriptive interval, not causal alpha significance'}
    for length in block_lengths:
        if len(returns) < 6 * length:
            output['block_lengths'].append(_unavailable('fewer_than_six_blocks', block_length=length))
            continue
        generator = random.Random(0)
        means = []
        for _ in range(replications):
            sample = []
            while len(sample) < len(returns):
                start = generator.randrange(len(returns))
                sample.extend(returns[(start + offset) % len(returns)] for offset in range(length))
            means.append(statistics.mean(sample[:len(returns)]))
        means.sort()
        output['block_lengths'].append(dict(status='available', block_length=length,
            mean_daily_return=statistics.mean(returns),
            confidence_interval=[_quantile(means, .025), _quantile(means, .975)]))
    return output


def aggregate_realized_contributions(rows) -> dict[str, float]:
    result: dict[str, float] = {}
    for row in rows:
        ticker = str(row["ticker"])
        result[ticker] = result.get(ticker, 0.) + float(row["realized_pnl"])
    return result


def research_diagnostics(series: dict, *, realized_contributions: dict[str, float] | None = None) -> dict:
    """Compute only diagnostics supported by retained accounting observations."""
    history = series.get('net_equity_history', [])
    result = {
        'cost_sensitivity': _unavailable('at_least_two_account_snapshots_required'),
        'largest_contributor_stress': _unavailable('no_realized_lot_contributions'),
        'market_exposure_attribution': _unavailable('matched_total_return_benchmarks_required'),
        'dependence_aware_uncertainty': block_uncertainty([]),
        'model_calibration': _unavailable('no_held_out_probability_forecasts',
            required_evidence='event-linked probability target, live model outputs and matured held-out outcomes; conviction scores are not calibrated probabilities'),
        'executable_fill_validation': _unavailable('no_independent_executable_quote_evidence',
            required_evidence='timestamped independent bid/ask or executable quote and order/fill linkage; offline fixtures and simulated next-open fills do not validate execution'),
        'aggregation_prohibited': True,
    }
    if len(history) < 2:
        return result
    values = [float(row['net_equity']) for row in history]
    if any(not math.isfinite(value) or value <= 0 for value in values):
        raise ValueError('research diagnostics require positive finite accepted equity')
    returns = [current / previous - 1 for previous, current in zip(values, values[1:])]
    result['dependence_aware_uncertainty'] = block_uncertainty(returns)
    first, last = history[0], history[-1]
    costs = {key: float(last['cumulative_costs'][key]) - float(first['cumulative_costs'][key])
             for key in ('slippage', 'commission', 'other_fees', 'borrow', 'financing')}
    if any(not math.isfinite(value) or value < 0 for value in costs.values()):
        result['cost_sensitivity'] = _unavailable('cost_counters_not_finite_monotonic')
    else:
        total_cost = sum(costs.values())
        net_return = values[-1] / values[0] - 1
        result['cost_sensitivity'] = {
            'status': 'available', 'scope': 'fixed recorded executions; no strategy feedback or re-optimization',
            'incremental_costs': costs, 'baseline_equity': values[0],
            'scenarios': [{'cost_multiplier': multiplier,
                           'total_return': net_return - (multiplier - 1) * total_cost / values[0]}
                          for multiplier in (1, 2, 3)],
        }
    if realized_contributions:
        if any(not math.isfinite(float(value)) for value in realized_contributions.values()):
            raise ValueError('contribution PnL must be finite')
        largest = sorted(realized_contributions, key=lambda key: (-realized_contributions[key], key))[0]
        total = sum(realized_contributions.values())
        absolute = sum(abs(value) for value in realized_contributions.values())
        result['largest_contributor_stress'] = {
            'status': 'available', 'scope': 'realized lot PnL only; excludes open marks, dividends and financing',
            'per_ticker_realized_pnl': dict(sorted(realized_contributions.items())),
            'largest_contributor': largest, 'largest_realized_pnl': realized_contributions[largest],
            'realized_pnl_total': total, 'realized_pnl_without_largest': total - realized_contributions[largest],
            'absolute_pnl_concentration_hhi': sum((abs(value) / absolute)**2 for value in realized_contributions.values()) if absolute else None,
            'portfolio_return_without_largest': None,
            'portfolio_return_unavailable_reason': 'reconciled_per_name_total_pnl_and_dynamic_counterfactual_unavailable',
        }
    benchmarks = series.get('benchmarks') or {}
    by_symbol = {symbol: {row['session']: row for row in benchmarks.get(symbol, [])}
                 for symbol in ('SPY', 'BIL')}
    required = {row['session'] for row in history}
    complete = all(required <= set(by_symbol[symbol]) and
        all(by_symbol[symbol][session].get('return_basis') == 'paired_total_return_index_v2' for session in required)
        for symbol in ('SPY', 'BIL'))
    if complete and not series.get('benchmark_unavailable_reason'):
        market, cash, residual, wealth = 0., 0., 0., 1.
        daily = []
        for previous, current, portfolio_return in zip(history, history[1:], returns):
            previous_session, current_session = previous['session'], current['session']
            spy = float(by_symbol['SPY'][current_session]['close']) / float(by_symbol['SPY'][previous_session]['close']) - 1
            bil = float(by_symbol['BIL'][current_session]['close']) / float(by_symbol['BIL'][previous_session]['close']) - 1
            net_weight = float(previous['net_exposure']) / float(previous['net_equity'])
            gross_weight = float(previous['gross_exposure']) / float(previous['net_equity'])
            market_piece, cash_piece = net_weight * spy, max(0., 1 - gross_weight) * bil
            residual_piece = portfolio_return - market_piece - cash_piece
            market += wealth * market_piece
            cash += wealth * cash_piece
            residual += wealth * residual_piece
            wealth *= 1 + portfolio_return
            daily.append({'session': current_session, 'lagged_net_weight': net_weight,
                          'lagged_gross_weight': gross_weight, 'market_return_component': market_piece,
                          'cash_return_component': cash_piece, 'residual_return_component': residual_piece})
        result['market_exposure_attribution'] = {
            'status': 'available', 'scope': 'lagged exposure to SPY and uninvested BIL; residual is not causal alpha',
            'linking_method': 'prior_portfolio_wealth_arithmetic_components',
            'market_contribution': market, 'cash_contribution': cash, 'residual_contribution': residual,
            'portfolio_total_return': wealth - 1, 'daily_components': daily,
        }
    return result
