"""Paired, descriptive ETF comparisons from an accepted contiguous window."""
from __future__ import annotations

import math
import random


def benchmark_returns(history: list[dict], rows: list[dict]) -> list[float] | None:
    required = [row['session'] for row in history]
    selected = [row for row in rows if row['session'] in set(required)]
    if len(selected) != len(required) or len({row['session'] for row in selected}) != len(required):
        return None
    by_date = {row['session']: row for row in selected}
    try:
        values = [float(by_date[session]['close']) for session in required]
    except (KeyError, TypeError, ValueError):
        return None
    if (any(not math.isfinite(value) or value <= 0 for value in values)
            or any(row.get('return_basis') != 'paired_total_return_index_v2' for row in selected)):
        return None
    return [current / previous - 1 for previous, current in zip(values, values[1:])]


def paired_excess(portfolio: list[float], benchmark: list[float], *, hurdle: float,
                  block_lengths=(5, 10, 20), replications=1000) -> dict:
    """Resample whole date pairs; statistic is portfolio CAGR minus ETF CAGR.

    The intervals describe this observed regime. Blocks do not establish event
    independence, model causality, future stationarity or live executability.
    """
    if len(portfolio) != len(benchmark) or not portfolio:
        return {'status': 'insufficient_evidence', 'reason': 'matched_returns_required'}
    if any(not math.isfinite(r) or r <= -1 for r in (*portfolio, *benchmark)):
        return {'status': 'insufficient_evidence', 'reason': 'invalid_matched_return'}
    n = len(portfolio)
    p, b = [math.log1p(r) for r in portfolio], [math.log1p(r) for r in benchmark]

    def annualized(p_sum, b_sum):
        return math.expm1(252 * p_sum / n) - math.expm1(252 * b_sum / n)

    result = {'status': 'available', 'method': 'paired_circular_moving_block_bootstrap',
              'pairing': 'same_session_same_resample_indices', 'return_count': n,
              'annualization_sessions': 252, 'annualized_hurdle': hurdle,
              'annualized_excess_return': annualized(sum(p), sum(b)),
              'confidence_level': .95, 'replications': replications, 'seed': 0,
              'pooled_scenarios': False, 'block_lengths': [], 'decision': 'inconclusive',
              'decision_reason': 'interval_overlaps_hurdle',
              'scope': 'descriptive paired CAGR difference; no causal or capital-allocation claim'}
    for length in block_lengths:
        if n < 6 * length:
            result['block_lengths'].append({'status': 'insufficient_evidence',
                'reason': 'fewer_than_six_blocks', 'block_length': length})
            continue
        # Precompute circular block sums; preserve exact final partial block.
        sizes = (length, n % length) if n % length else (length,)
        sums = {size: [(sum(p[(start + offset) % n] for offset in range(size)),
                       sum(b[(start + offset) % n] for offset in range(size)))
                      for start in range(n)] for size in sizes}
        generator, samples = random.Random(0), []
        for _ in range(replications):
            p_sum = b_sum = 0.
            remaining = n
            while remaining:
                size = min(length, remaining)
                p_block, b_block = sums[size][generator.randrange(n)]
                p_sum += p_block
                b_sum += b_block
                remaining -= size
            samples.append(annualized(p_sum, b_sum))
        samples.sort()

        def quantile(probability):
            position = (len(samples) - 1) * probability
            low = int(position)
            high = min(low + 1, len(samples) - 1)
            return samples[low] + (samples[high] - samples[low]) * (position - low)

        result['block_lengths'].append({'status': 'available', 'block_length': length,
            'annualized_excess_confidence_interval': [quantile(.025), quantile(.975)]})
    intervals = [row['annualized_excess_confidence_interval'] for row in result['block_lengths']
                 if row['status'] == 'available']
    if n < 252:
        result['decision_reason'] = 'fewer_than_252_valid_returns'
    elif len(intervals) != len(block_lengths):
        result['decision_reason'] = 'insufficient_blocks'
    elif all(lower > hurdle for lower, _ in intervals):
        result.update(decision='supports_research_hurdle', decision_reason='all_block_intervals_above_hurdle')
    elif all(upper < hurdle for _, upper in intervals):
        result.update(decision='below_research_hurdle', decision_reason='all_block_intervals_below_hurdle')
    result['review_stage'] = 'final_504_return_review' if n >= 504 else 'minimum_252_return_review'
    return result
