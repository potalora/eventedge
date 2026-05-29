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
