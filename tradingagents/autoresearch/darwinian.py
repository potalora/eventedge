"""Darwinian weight system for strategy evolution.

Inspired by Atlas-GIC: each strategy has a weight [0.3, 2.5].
Top quartile performers get weight * 1.05, bottom quartile * 0.95.
Weights determine position sizing allocation.
"""
from __future__ import annotations

import logging
import math

import pandas as pd

logger = logging.getLogger(__name__)

WEIGHT_MIN = 0.3
WEIGHT_MAX = 2.5
WEIGHT_DEFAULT = 1.0
WEIGHT_UP = 1.05
WEIGHT_DOWN = 0.95


def initialize_weights(
    strategy_names: list[str], default: float = WEIGHT_DEFAULT
) -> dict[str, float]:
    """Initialize all strategies with default weight."""
    return {name: default for name in strategy_names}


def update_weights(
    weights: dict[str, float],
    scores: dict[str, float],
    weight_min: float = WEIGHT_MIN,
    weight_max: float = WEIGHT_MAX,
    weight_up: float = WEIGHT_UP,
    weight_down: float = WEIGHT_DOWN,
) -> dict[str, float]:
    """Update strategy weights based on performance quartiles.

    Args:
        weights: Current weights by strategy name.
        scores: Performance scores by strategy name (higher = better).
        weight_min/max: Bounds for weights.
        weight_up/down: Multipliers for top/bottom quartile.

    Returns:
        Updated weights dict.
    """
    if not scores:
        return dict(weights)

    # Only update strategies that have both a weight and a score
    scored = {k: v for k, v in scores.items() if k in weights}
    if not scored:
        return dict(weights)

    sorted_names = sorted(scored, key=lambda k: scored[k], reverse=True)
    n = len(sorted_names)

    if n == 1:
        # Single strategy: no quartile split possible
        return dict(weights)

    # Check if all scores are identical — no differentiation possible
    unique_scores = set(scored.values())
    if len(unique_scores) == 1:
        logger.info("All scores identical (%.4f) — weights unchanged", next(iter(unique_scores)))
        return dict(weights)

    top_cutoff = max(1, n // 4)
    bottom_cutoff = max(1, n // 4)

    top_set = set(sorted_names[:top_cutoff])
    bottom_set = set(sorted_names[n - bottom_cutoff :])

    updated = dict(weights)
    for name in scored:
        old_w = updated[name]
        if name in top_set:
            new_w = min(old_w * weight_up, weight_max)
        elif name in bottom_set:
            new_w = max(old_w * weight_down, weight_min)
        else:
            new_w = old_w

        if new_w != old_w:
            logger.info(
                "Weight %s: %.4f -> %.4f (%s quartile)",
                name,
                old_w,
                new_w,
                "top" if name in top_set else "bottom",
            )
        updated[name] = new_w

    return updated


def get_allocation(
    weights: dict[str, float], total_capital: float
) -> dict[str, float]:
    """Convert weights to dollar allocations proportional to weight.

    Args:
        weights: Strategy weights.
        total_capital: Total portfolio capital.

    Returns:
        Dict of strategy_name -> dollar allocation.
    """
    if not weights:
        return {}
    total_weight = sum(weights.values())
    if total_weight == 0:
        return {k: 0.0 for k in weights}
    return {k: (v / total_weight) * total_capital for k, v in weights.items()}


def get_quartile_summary(
    weights: dict[str, float], scores: dict[str, float]
) -> dict[str, list[dict[str, float]]]:
    """Return which strategies are in which quartile and their weight changes.

    Returns:
        Dict with keys 'top', 'middle', 'bottom', each containing a list of
        dicts with 'name', 'score', 'weight', 'new_weight'.
    """
    scored = {k: v for k, v in scores.items() if k in weights}
    if not scored:
        return {"top": [], "middle": [], "bottom": []}

    sorted_names = sorted(scored, key=lambda k: scored[k], reverse=True)
    n = len(sorted_names)
    top_cutoff = max(1, n // 4) if n > 1 else 0
    bottom_cutoff = max(1, n // 4) if n > 1 else 0

    top_names = set(sorted_names[:top_cutoff])
    bottom_names = set(sorted_names[n - bottom_cutoff :]) if n > 1 else set()

    new_weights = update_weights(weights, scores)

    result: dict[str, list[dict[str, float]]] = {"top": [], "middle": [], "bottom": []}
    for name in sorted_names:
        entry = {
            "name": name,
            "score": scored[name],
            "weight": weights[name],
            "new_weight": new_weights[name],
        }
        if name in top_names:
            result["top"].append(entry)
        elif name in bottom_names:
            result["bottom"].append(entry)
        else:
            result["middle"].append(entry)

    return result


def weight_history_to_df(history: list[dict]) -> pd.DataFrame:
    """Convert a list of weight snapshots to a DataFrame for analysis.

    Each snapshot should have 'generation', 'timestamp', and 'weights' (dict).

    Returns:
        DataFrame with columns: generation, timestamp, strategy, weight.
    """
    rows = []
    for snap in history:
        gen = snap.get("generation", 0)
        ts = snap.get("timestamp", "")
        for strategy, weight in snap.get("weights", {}).items():
            rows.append(
                {
                    "generation": gen,
                    "timestamp": ts,
                    "strategy": strategy,
                    "weight": weight,
                }
            )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Conservative mode for paper-trade phase
# ---------------------------------------------------------------------------

WEIGHT_UP_CONSERVATIVE = 1.02
WEIGHT_DOWN_CONSERVATIVE = 0.98
MIN_TRADES_DEFAULT = 20


def compute_confidence(
    trade_count: int,
    min_trades: int = MIN_TRADES_DEFAULT,
    full_confidence_trades: int = 100,
) -> float:
    """Return confidence in [0, 1] based on completed trade count.

    Returns 0.0 if trade_count < min_trades.
    Linear ramp from 0.0 to 1.0 between min_trades and full_confidence_trades.
    Capped at 1.0.
    """
    if trade_count < min_trades:
        return 0.0
    if trade_count >= full_confidence_trades:
        return 1.0
    return (trade_count - min_trades) / (full_confidence_trades - min_trades)


def confidence_weighted_adjustment(
    base_multiplier: float,
    confidence: float,
) -> float:
    """Scale a weight multiplier toward 1.0 based on confidence.

    At confidence=1.0: returns base_multiplier unchanged.
    At confidence=0.0: returns 1.0 (no adjustment).
    Linear interpolation on log scale between.

    Formula: exp(confidence * log(base_multiplier))
    """
    if confidence == 0.0:
        return 1.0
    return math.exp(confidence * math.log(base_multiplier))


def update_weights_conservative(
    weights: dict[str, float],
    scores: dict[str, float],
    trade_counts: dict[str, int],
    min_trades: int = MIN_TRADES_DEFAULT,
    weight_min: float = WEIGHT_MIN,
    weight_max: float = WEIGHT_MAX,
    weight_up: float = WEIGHT_UP_CONSERVATIVE,
    weight_down: float = WEIGHT_DOWN_CONSERVATIVE,
) -> dict[str, float]:
    """Conservative weight update for paper-trade phase.

    Only adjusts strategies that have completed >= min_trades round-trips.
    Adjustment magnitude scales with confidence (trade count).
    Uses same quartile logic as update_weights but with smaller multipliers
    and confidence scaling.
    """
    if not scores:
        return dict(weights)

    # Filter to strategies with weight, score, and sufficient trades
    qualified = {
        k: scores[k]
        for k in scores
        if k in weights and k in trade_counts and trade_counts[k] >= min_trades
    }

    if len(qualified) < 2:
        return dict(weights)

    # Check if all scores are identical
    unique_scores = set(qualified.values())
    if len(unique_scores) == 1:
        logger.info(
            "All qualified scores identical (%.4f) — weights unchanged",
            next(iter(unique_scores)),
        )
        return dict(weights)

    sorted_names = sorted(qualified, key=lambda k: qualified[k], reverse=True)
    n = len(sorted_names)

    top_cutoff = max(1, n // 4)
    bottom_cutoff = max(1, n // 4)

    top_set = set(sorted_names[:top_cutoff])
    bottom_set = set(sorted_names[n - bottom_cutoff:])

    updated = dict(weights)
    for name in qualified:
        old_w = updated[name]
        if name in top_set:
            base = weight_up
        elif name in bottom_set:
            base = weight_down
        else:
            continue

        confidence = compute_confidence(trade_counts[name], min_trades=min_trades)
        adjusted = confidence_weighted_adjustment(base, confidence)

        if name in top_set:
            new_w = min(old_w * adjusted, weight_max)
        else:
            new_w = max(old_w * adjusted, weight_min)

        if new_w != old_w:
            logger.info(
                "Weight %s: %.4f -> %.4f (%s quartile, confidence=%.2f)",
                name,
                old_w,
                new_w,
                "top" if name in top_set else "bottom",
                confidence,
            )
        updated[name] = new_w

    return updated
