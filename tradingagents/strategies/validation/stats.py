"""Pure statistical functions for the event study.

All functions are side-effect-free, operating on numpy arrays. The engine
calls these to do the actual math.
"""
from __future__ import annotations

import numpy as np
from scipy import stats as sp_stats

from tradingagents.strategies.validation.models import BootstrapCI, MarketModelFit


def fit_market_model(
    stock_returns: np.ndarray,
    market_returns: np.ndarray,
) -> MarketModelFit:
    """Fit R_stock = alpha + beta * R_market via OLS (np.linalg.lstsq)."""
    n = len(stock_returns)
    X = np.column_stack([np.ones(n), market_returns])
    coeffs, _, _, _ = np.linalg.lstsq(X, stock_returns, rcond=None)
    alpha, beta = float(coeffs[0]), float(coeffs[1])

    predicted = alpha + beta * market_returns
    ss_res = float(np.sum((stock_returns - predicted) ** 2))
    ss_tot = float(np.sum((stock_returns - stock_returns.mean()) ** 2))
    r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    return MarketModelFit(alpha=alpha, beta=beta, r_squared=r_squared, n_obs=n)


def compute_abnormal_returns(
    stock_returns: np.ndarray,
    market_returns: np.ndarray,
    alpha: float,
    beta: float,
) -> np.ndarray:
    """AR_t = R_stock,t - (alpha + beta * R_market,t)."""
    return stock_returns - (alpha + beta * market_returns)


def sum_car(daily_ar: np.ndarray, start: int, end: int) -> float:
    """Cumulative Abnormal Return = sum of daily ARs over [start, end] inclusive.

    Indices are offsets into the event-window array where index 0 is day 0.
    """
    return float(np.sum(daily_ar[start : end + 1]))


def ttest_cars(cars: np.ndarray) -> tuple[float, float]:
    """One-sample two-sided t-test of mean CAR vs 0. Returns (t_stat, p_value)."""
    if len(cars) < 2:
        return 0.0, 1.0
    t_stat, p_value = sp_stats.ttest_1samp(cars, popmean=0.0)
    return float(t_stat), float(p_value)


def bootstrap_ci(
    cars: np.ndarray,
    n_bootstrap: int = 10_000,
    confidence: float = 0.95,
    rng_seed: int | None = None,
) -> BootstrapCI:
    """Percentile bootstrap CI for the mean CAR."""
    rng = np.random.default_rng(rng_seed)
    n = len(cars)
    if n == 0:
        return BootstrapCI(lower=0.0, upper=0.0, confidence=confidence, n_bootstrap=n_bootstrap)
    boot_means = np.array(
        [rng.choice(cars, size=n, replace=True).mean() for _ in range(n_bootstrap)]
    )
    lower_pct = (1 - confidence) / 2 * 100
    upper_pct = (1 + confidence) / 2 * 100
    lower, upper = np.percentile(boot_means, [lower_pct, upper_pct])
    return BootstrapCI(
        lower=float(lower),
        upper=float(upper),
        confidence=confidence,
        n_bootstrap=n_bootstrap,
    )
