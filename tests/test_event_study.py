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


from tradingagents.strategies.validation import engine
from tradingagents.strategies.validation.models import EventSpec


def _make_price_fn():
    """Fake price_fn: 400 business days from 2024-01-01.

    SPY rises 0.1%/day. The stock tracks SPY (beta=1, alpha=0) for the whole
    series EXCEPT it gets a +5% one-day jump on the event date 2025-06-02,
    so its [0,+5] CAR should be ~+5%.
    """
    import pandas as pd

    dates = pd.bdate_range("2024-01-01", periods=400).strftime("%Y-%m-%d").tolist()
    spy = {}
    stk = {}
    spy_price = 100.0
    stk_price = 50.0
    for d in dates:
        spy[d] = round(spy_price, 6)
        stk[d] = round(stk_price, 6)
        spy_price *= 1.001
        stk_price *= 1.001
    # Inject a +5% abnormal jump on the event date's close.
    event_date = "2025-06-02"
    assert event_date in stk
    idx = dates.index(event_date)
    for d in dates[idx:]:
        stk[d] = round(stk[d] * 1.05, 6)

    def price_fn(ticker, start, end):
        series = spy if ticker == "SPY" else stk
        return {d: v for d, v in series.items() if start <= d <= end}

    return price_fn, event_date


def test_compute_car_detects_abnormal_jump():
    price_fn, event_date = _make_price_fn()
    events = [EventSpec(ticker="TEST", event_date=event_date, group="demo")]
    result = engine.compute_car(
        events, price_fn, windows=[(0, 5)], n_bootstrap=200, rng_seed=1
    )
    assert len(result.events) == 1
    ev = result.events[0]
    # The +5% jump lands on day 0; [0,+5] CAR should be close to +5%.
    assert abs(ev.cars["[0,+5]"] - 0.05) < 0.005
    assert ev.market_model.n_obs >= 200
    # One aggregate group with one window.
    assert len(result.aggregates) == 1
    agg = result.aggregates[0]
    assert agg.group == "demo"
    assert agg.windows[0].window == "[0,+5]"


def test_compute_car_skips_insufficient_history():
    price_fn, _ = _make_price_fn()
    # Event near the very start has < 200 days of pre-history.
    events = [EventSpec(ticker="TEST", event_date="2024-01-05", group="demo")]
    result = engine.compute_car(events, price_fn, windows=[(0, 5)], n_bootstrap=50, rng_seed=1)
    assert result.events == []
    assert "TEST" in result.skipped_tickers


def test_compute_car_empty_events():
    price_fn, _ = _make_price_fn()
    result = engine.compute_car([], price_fn, windows=[(0, 5)], n_bootstrap=50, rng_seed=1)
    assert result.events == []
    assert result.aggregates == []


def test_yfinance_price_fn_extracts_close_series():
    import pandas as pd

    from tradingagents.strategies.validation.price_adapter import yfinance_price_fn

    class FakeSource:
        def fetch_prices(self, tickers, start, end):
            idx = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"])
            cols = pd.MultiIndex.from_tuples(
                [("Close", "AAPL"), ("Open", "AAPL")]
            )
            return pd.DataFrame(
                [[10.0, 9.0], [11.0, 10.0], [12.0, 11.0]], index=idx, columns=cols
            )

    price_fn = yfinance_price_fn(source=FakeSource())
    closes = price_fn("AAPL", "2024-01-01", "2024-01-31")
    assert closes == {"2024-01-02": 10.0, "2024-01-03": 11.0, "2024-01-04": 12.0}


def test_yfinance_price_fn_handles_empty():
    import pandas as pd

    from tradingagents.strategies.validation.price_adapter import yfinance_price_fn

    class EmptySource:
        def fetch_prices(self, tickers, start, end):
            return pd.DataFrame()

    price_fn = yfinance_price_fn(source=EmptySource())
    assert price_fn("AAPL", "2024-01-01", "2024-01-31") == {}
