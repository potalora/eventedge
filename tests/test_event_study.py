"""Tests for the event study engine (offline, no network)."""
from __future__ import annotations

from tradingagents.strategies.validation.models import (
    AggregateResult,
    BootstrapCI,
    EventCAR,
    EventSpec,
    EventStudyResult,
    MarketModelFit,
    WindowStats,
)


def test_event_spec_defaults():
    spec = EventSpec(ticker="AAPL", event_date="2026-01-15", group="earnings_call")
    assert spec.ticker == "AAPL"
    assert spec.metadata == {}


def test_event_car_holds_window_dict():
    fit = MarketModelFit(alpha=0.0, beta=1.0, r_squared=0.5, n_obs=200)
    car = EventCAR(
        ticker="AAPL",
        event_date="2026-01-15",
        group="earnings_call",
        market_model=fit,
        daily_ar=[0.01, 0.02],
        cars={"[0,+5]": 0.03, "[0,+30]": None},
        metadata={"score": 0.8},
    )
    assert car.cars["[0,+5]"] == 0.03
    assert car.cars["[0,+30]"] is None


def test_event_study_result_defaults():
    result = EventStudyResult()
    assert result.events == []
    assert result.aggregates == []
    assert result.skipped_tickers == []


import numpy as np

from tradingagents.strategies.validation import stats


def test_fit_market_model_recovers_known_params():
    # Construct R_stock = 0.001 + 1.5 * R_market exactly (no noise).
    rng = np.random.default_rng(0)
    market = rng.normal(0.0, 0.01, size=300)
    stock = 0.001 + 1.5 * market
    fit = stats.fit_market_model(stock, market)
    assert abs(fit.alpha - 0.001) < 1e-9
    assert abs(fit.beta - 1.5) < 1e-9
    assert abs(fit.r_squared - 1.0) < 1e-9
    assert fit.n_obs == 300


def test_compute_abnormal_returns():
    stock = np.array([0.02, 0.03, 0.01])
    market = np.array([0.01, 0.01, 0.00])
    # expected = alpha + beta*market = 0.005 + 1.0*market
    ar = stats.compute_abnormal_returns(stock, market, alpha=0.005, beta=1.0)
    np.testing.assert_allclose(ar, [0.005, 0.015, 0.005])


def test_sum_car_inclusive_window():
    daily_ar = np.array([0.01, 0.02, 0.03, 0.04, 0.05, 0.06])
    # window [0, +1] = days 0 and 1 = 0.01 + 0.02
    assert abs(stats.sum_car(daily_ar, 0, 1) - 0.03) < 1e-12
    # window [0, +5] = all six = 0.21
    assert abs(stats.sum_car(daily_ar, 0, 5) - 0.21) < 1e-12


def test_ttest_cars_detects_nonzero_mean():
    # Strongly positive CARs -> small p-value, positive t-stat.
    cars = np.array([0.02, 0.03, 0.025, 0.018, 0.022, 0.027])
    t_stat, p_value = stats.ttest_cars(cars)
    assert t_stat > 0
    assert p_value < 0.01


def test_ttest_cars_handles_too_few():
    t_stat, p_value = stats.ttest_cars(np.array([0.01]))
    assert t_stat == 0.0
    assert p_value == 1.0


def test_bootstrap_ci_is_deterministic_with_seed():
    cars = np.array([0.01, 0.02, 0.03, -0.01, 0.015, 0.005])
    ci_a = stats.bootstrap_ci(cars, n_bootstrap=1000, rng_seed=42)
    ci_b = stats.bootstrap_ci(cars, n_bootstrap=1000, rng_seed=42)
    assert ci_a.lower == ci_b.lower
    assert ci_a.upper == ci_b.upper
    assert ci_a.lower < ci_a.upper
    assert ci_a.n_bootstrap == 1000
