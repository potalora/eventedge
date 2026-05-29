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
