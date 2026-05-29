# Event Study Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an offline, read-only event study engine that measures market-adjusted Cumulative Abnormal Returns (CAR) around the events EventEdge's strategies trade, with significance testing (t-test + bootstrap CI), and expose it as a `event-study` CLI subcommand.

**Architecture:** New package `tradingagents/strategies/validation/` with a pure stats core (numpy + scipy), plain dataclass models, a data-source-agnostic engine that takes a `price_fn` callable, a yfinance price adapter, and a journal adapter that builds events from `SignalJournal` entries (deduped across a generation's 16 cohort journals). Mirrors the design in `docs/superpowers/specs/2026-05-29-event-study-engine-design.md`.

**Tech Stack:** Python, numpy, scipy (new dep), pandas (existing, via yfinance source), dataclasses, pytest.

---

## File Structure

- Create: `tradingagents/strategies/validation/__init__.py` — package exports
- Create: `tradingagents/strategies/validation/models.py` — dataclasses
- Create: `tradingagents/strategies/validation/stats.py` — pure stats functions
- Create: `tradingagents/strategies/validation/engine.py` — `compute_car` orchestration
- Create: `tradingagents/strategies/validation/price_adapter.py` — yfinance `price_fn`
- Create: `tradingagents/strategies/validation/journal_source.py` — events from journals
- Create: `tests/test_event_study.py` — all unit tests (offline)
- Modify: `scripts/run_generations.py` — add `event-study` subcommand
- Modify: `pyproject.toml` — add `scipy>=1.11`
- Modify: `CLAUDE.md` — document module, fix strategy count, note pruned modules

---

## Task 1: Add scipy dependency

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add scipy to dependencies**

Find the `dependencies = [` array in `pyproject.toml` and add the `scipy` line right after the existing `numpy>=1.26.0` entry:

```toml
    "numpy>=1.26.0",
    "scipy>=1.11",
```

- [ ] **Step 2: Install it**

Run: `.venv/bin/pip install "scipy>=1.11"`
Expected: scipy installs (or "already satisfied").

- [ ] **Step 3: Verify import**

Run: `.venv/bin/python -c "import scipy.stats; print(scipy.__version__)"`
Expected: prints a version >= 1.11.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "build: add scipy for event study significance tests"
```

---

## Task 2: Models

**Files:**
- Create: `tradingagents/strategies/validation/__init__.py`
- Create: `tradingagents/strategies/validation/models.py`
- Test: `tests/test_event_study.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_event_study.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_event_study.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tradingagents.strategies.validation'`

- [ ] **Step 3: Create the package and models**

Create `tradingagents/strategies/validation/__init__.py`:

```python
"""Offline event study: market-adjusted CAR validation for event-driven strategies."""

from tradingagents.strategies.validation.models import (
    AggregateResult,
    BootstrapCI,
    EventCAR,
    EventSpec,
    EventStudyResult,
    MarketModelFit,
    WindowStats,
)

__all__ = [
    "AggregateResult",
    "BootstrapCI",
    "EventCAR",
    "EventSpec",
    "EventStudyResult",
    "MarketModelFit",
    "WindowStats",
]
```

Create `tradingagents/strategies/validation/models.py`:

```python
"""Dataclasses for the event study pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class EventSpec:
    """An event to study: one ticker anchored at one date, tagged with a group."""

    ticker: str
    event_date: str          # YYYY-MM-DD, anchors day 0
    group: str               # aggregation key (e.g. strategy name)
    metadata: dict = field(default_factory=dict)


@dataclass
class MarketModelFit:
    """OLS fit R_stock = alpha + beta * R_market over the estimation window."""

    alpha: float
    beta: float
    r_squared: float
    n_obs: int


@dataclass
class EventCAR:
    """CAR result for a single event."""

    ticker: str
    event_date: str
    group: str
    market_model: MarketModelFit
    daily_ar: list[float] = field(default_factory=list)
    cars: dict[str, float | None] = field(default_factory=dict)  # window label -> CAR
    metadata: dict = field(default_factory=dict)


@dataclass
class BootstrapCI:
    """Percentile bootstrap confidence interval for a mean CAR."""

    lower: float
    upper: float
    confidence: float = 0.95
    n_bootstrap: int = 10_000


@dataclass
class WindowStats:
    """Aggregate stats for one CAR window across many events in a group."""

    window: str
    n_events: int
    mean_car: float
    std_car: float
    t_stat: float
    p_value: float
    ci: BootstrapCI


@dataclass
class AggregateResult:
    """Cross-sectional results for one group (e.g. all earnings_call events)."""

    group: str
    n_events: int
    windows: list[WindowStats] = field(default_factory=list)


@dataclass
class EventStudyResult:
    """Top-level result returned by compute_car()."""

    events: list[EventCAR] = field(default_factory=list)
    aggregates: list[AggregateResult] = field(default_factory=list)
    skipped_tickers: list[str] = field(default_factory=list)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_event_study.py -v`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add tradingagents/strategies/validation/__init__.py tradingagents/strategies/validation/models.py tests/test_event_study.py
git commit -m "feat(validation): event study dataclass models"
```

---

## Task 3: Market model fit

**Files:**
- Create: `tradingagents/strategies/validation/stats.py`
- Test: `tests/test_event_study.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_event_study.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_event_study.py::test_fit_market_model_recovers_known_params -v`
Expected: FAIL with `ModuleNotFoundError` / `AttributeError: module ... has no attribute 'fit_market_model'`

- [ ] **Step 3: Create stats.py with fit_market_model**

Create `tradingagents/strategies/validation/stats.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_event_study.py::test_fit_market_model_recovers_known_params -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tradingagents/strategies/validation/stats.py tests/test_event_study.py
git commit -m "feat(validation): market model OLS fit"
```

---

## Task 4: Abnormal returns and CAR sum

**Files:**
- Modify: `tradingagents/strategies/validation/stats.py`
- Test: `tests/test_event_study.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_event_study.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_event_study.py -k "abnormal or sum_car" -v`
Expected: FAIL with `AttributeError`.

- [ ] **Step 3: Add the functions to stats.py**

Append to `tradingagents/strategies/validation/stats.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_event_study.py -k "abnormal or sum_car" -v`
Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
git add tradingagents/strategies/validation/stats.py tests/test_event_study.py
git commit -m "feat(validation): abnormal returns and CAR sum"
```

---

## Task 5: Significance tests (t-test + bootstrap CI)

**Files:**
- Modify: `tradingagents/strategies/validation/stats.py`
- Test: `tests/test_event_study.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_event_study.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_event_study.py -k "ttest or bootstrap" -v`
Expected: FAIL with `AttributeError`.

- [ ] **Step 3: Add the functions to stats.py**

Append to `tradingagents/strategies/validation/stats.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_event_study.py -k "ttest or bootstrap" -v`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add tradingagents/strategies/validation/stats.py tests/test_event_study.py
git commit -m "feat(validation): t-test and bootstrap CI"
```

---

## Task 6: Engine — compute_car

**Files:**
- Create: `tradingagents/strategies/validation/engine.py`
- Test: `tests/test_event_study.py`

The engine takes a `price_fn(ticker, start, end) -> dict[str, float]` (date string -> close).
It fetches the market proxy once, then per ticker builds aligned daily returns, locates
each event's day-0 index (snapping forward to the next available trading day), fits the
market model on the estimation window, computes abnormal returns over the event window,
and aggregates CARs by group.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_event_study.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_event_study.py -k "compute_car" -v`
Expected: FAIL with `ModuleNotFoundError: ... validation.engine`.

- [ ] **Step 3: Create engine.py**

Create `tradingagents/strategies/validation/engine.py`:

```python
"""Event study engine: compute CARs around events using the market model."""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Callable

import numpy as np

from tradingagents.strategies.validation.models import (
    AggregateResult,
    EventCAR,
    EventSpec,
    EventStudyResult,
    WindowStats,
)
from tradingagents.strategies.validation.stats import (
    bootstrap_ci,
    compute_abnormal_returns,
    fit_market_model,
    sum_car,
    ttest_cars,
)

logger = logging.getLogger(__name__)

PriceFn = Callable[[str, str, str], "dict[str, float]"]

WINDOWS_DEFAULT: list[tuple[int, int]] = [(0, 5), (0, 10), (0, 30)]
_ESTIMATION_DEFAULT: tuple[int, int] = (-250, -11)
_MIN_ESTIMATION_DAYS = 200
_MARKET_TICKER = "SPY"
# Wide fetch range so any event has >= 250 prior trading days and a +30 tail.
_FETCH_START = "2022-01-01"
_FETCH_END = "2099-01-01"


def _window_label(start: int, end: int) -> str:
    return f"[{start:+d},{end:+d}]".replace("+0", "0")


def _event_index(dates: list[str], event_date: str) -> int | None:
    """First index whose date >= event_date (snap forward). None if past the end."""
    for i, d in enumerate(dates):
        if d >= event_date:
            return i
    return None


def compute_car(
    events: list[EventSpec],
    price_fn: PriceFn,
    *,
    windows: list[tuple[int, int]] = WINDOWS_DEFAULT,
    estimation: tuple[int, int] = _ESTIMATION_DEFAULT,
    market_ticker: str = _MARKET_TICKER,
    min_estimation_days: int = _MIN_ESTIMATION_DAYS,
    n_bootstrap: int = 10_000,
    rng_seed: int | None = None,
) -> EventStudyResult:
    """Compute CARs for a list of events and aggregate cross-sectionally by group."""
    if not events:
        return EventStudyResult()

    spy_closes = price_fn(market_ticker, _FETCH_START, _FETCH_END)
    if not spy_closes:
        logger.warning("No %s prices — cannot compute CARs", market_ticker)
        return EventStudyResult(skipped_tickers=sorted({e.ticker for e in events}))

    est_start, est_end = estimation
    max_window = max(end for _, end in windows)

    all_events: list[EventCAR] = []
    skipped: set[str] = set()

    # Group events by ticker so each ticker is priced once.
    by_ticker: dict[str, list[EventSpec]] = defaultdict(list)
    for ev in events:
        by_ticker[ev.ticker].append(ev)

    for ticker, ticker_events in by_ticker.items():
        stk_closes = price_fn(ticker, _FETCH_START, _FETCH_END)
        if not stk_closes:
            skipped.add(ticker)
            continue

        # Align stock and market on the SAME common trading-day grid, then
        # compute both return series on that grid so day-over-day returns line up.
        common = sorted(set(stk_closes) & set(spy_closes))
        if len(common) < min_estimation_days + max_window + 2:
            skipped.add(ticker)
            continue
        stk_prices = np.array([stk_closes[d] for d in common], dtype=float)
        spy_prices = np.array([spy_closes[d] for d in common], dtype=float)
        stk_rets = np.zeros(len(common))
        stk_rets[1:] = stk_prices[1:] / stk_prices[:-1] - 1.0
        mkt_rets = np.zeros(len(common))
        mkt_rets[1:] = spy_prices[1:] / spy_prices[:-1] - 1.0

        produced_any = False
        for ev in ticker_events:
            e = _event_index(common, ev.event_date)
            if e is None:
                continue
            est_lo = e + est_start          # inclusive
            est_hi = e + est_end            # inclusive
            if est_lo < 1 or (est_hi - est_lo + 1) < min_estimation_days:
                continue

            stock_est = stk_rets[est_lo : est_hi + 1]
            market_est = mkt_rets[est_lo : est_hi + 1]
            fit = fit_market_model(stock_est, market_est)

            win_hi = min(e + max_window, len(common) - 1)
            stock_win = stk_rets[e : win_hi + 1]
            market_win = mkt_rets[e : win_hi + 1]
            daily_ar = compute_abnormal_returns(
                stock_win, market_win, fit.alpha, fit.beta
            )

            cars: dict[str, float | None] = {}
            for w_start, w_end in windows:
                label = _window_label(w_start, w_end)
                if w_end <= (len(daily_ar) - 1):
                    cars[label] = sum_car(daily_ar, w_start, w_end)
                else:
                    cars[label] = None  # window runs past available data

            all_events.append(
                EventCAR(
                    ticker=ticker,
                    event_date=ev.event_date,
                    group=ev.group,
                    market_model=fit,
                    daily_ar=[float(x) for x in daily_ar],
                    cars=cars,
                    metadata=dict(ev.metadata),
                )
            )
            produced_any = True

        if not produced_any:
            skipped.add(ticker)

    aggregates = _aggregate(all_events, windows, n_bootstrap, rng_seed)
    return EventStudyResult(
        events=all_events, aggregates=aggregates, skipped_tickers=sorted(skipped)
    )


def _aggregate(
    events: list[EventCAR],
    windows: list[tuple[int, int]],
    n_bootstrap: int,
    rng_seed: int | None,
) -> list[AggregateResult]:
    by_group: dict[str, list[EventCAR]] = defaultdict(list)
    for ev in events:
        by_group[ev.group].append(ev)

    results: list[AggregateResult] = []
    for group in sorted(by_group):
        group_events = by_group[group]
        window_stats: list[WindowStats] = []
        for w_start, w_end in windows:
            label = _window_label(w_start, w_end)
            vals = np.array(
                [ev.cars[label] for ev in group_events if ev.cars.get(label) is not None],
                dtype=float,
            )
            if len(vals) == 0:
                continue
            t_stat, p_value = ttest_cars(vals)
            ci = bootstrap_ci(vals, n_bootstrap=n_bootstrap, rng_seed=rng_seed)
            window_stats.append(
                WindowStats(
                    window=label,
                    n_events=len(vals),
                    mean_car=float(vals.mean()),
                    std_car=float(vals.std(ddof=1)) if len(vals) > 1 else 0.0,
                    t_stat=t_stat,
                    p_value=p_value,
                    ci=ci,
                )
            )
        results.append(
            AggregateResult(
                group=group, n_events=len(group_events), windows=window_stats
            )
        )
    return results
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_event_study.py -k "compute_car" -v`
Expected: 3 PASS.

- [ ] **Step 5: Run the whole test file**

Run: `.venv/bin/python -m pytest tests/test_event_study.py -v`
Expected: all PASS so far.

- [ ] **Step 6: Commit**

```bash
git add tradingagents/strategies/validation/engine.py tests/test_event_study.py
git commit -m "feat(validation): event study engine compute_car"
```

---

## Task 7: yfinance price adapter

**Files:**
- Create: `tradingagents/strategies/validation/price_adapter.py`
- Test: `tests/test_event_study.py`

The adapter wraps `YFinanceSource.fetch_prices` (returns a pandas DataFrame with
MultiIndex columns `(Price, Ticker)`) into the `price_fn` shape the engine expects.
The test injects a fake source so no network call happens.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_event_study.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_event_study.py -k "yfinance_price_fn" -v`
Expected: FAIL with `ModuleNotFoundError: ... validation.price_adapter`.

- [ ] **Step 3: Create price_adapter.py**

Create `tradingagents/strategies/validation/price_adapter.py`:

```python
"""Adapter turning YFinanceSource into the engine's price_fn callable."""
from __future__ import annotations

import logging
from typing import Any, Callable

import pandas as pd

logger = logging.getLogger(__name__)


def yfinance_price_fn(source: Any | None = None) -> Callable[[str, str, str], dict[str, float]]:
    """Build a price_fn(ticker, start, end) -> {date: close} backed by YFinanceSource.

    The returned callable closes over one source instance so its in-memory cache
    is shared across all tickers in a single event study run.
    """
    if source is None:
        from tradingagents.strategies.data_sources.yfinance_source import YFinanceSource

        source = YFinanceSource()

    def price_fn(ticker: str, start: str, end: str) -> dict[str, float]:
        df = source.fetch_prices([ticker], start, end)
        if df is None or df.empty:
            return {}
        try:
            close = df["Close"][ticker]
        except (KeyError, TypeError):
            logger.warning("No Close column for %s", ticker)
            return {}
        close = close.dropna()
        out: dict[str, float] = {}
        for ts, val in close.items():
            key = ts.strftime("%Y-%m-%d") if isinstance(ts, pd.Timestamp) else str(ts)[:10]
            out[key] = float(val)
        return out

    return price_fn
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_event_study.py -k "yfinance_price_fn" -v`
Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
git add tradingagents/strategies/validation/price_adapter.py tests/test_event_study.py
git commit -m "feat(validation): yfinance price adapter"
```

---

## Task 8: Journal event source (cross-cohort dedupe)

**Files:**
- Create: `tradingagents/strategies/validation/journal_source.py`
- Test: `tests/test_event_study.py`

Signal journals live per-cohort: `data/generations/gen_NNN/horizon_*_size_*/signal_journal.jsonl`.
Within one generation the same signal fires across cohorts, so events must be deduped by
`(strategy, ticker, event_date)`. `event_date` is the `timestamp` field's date part.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_event_study.py`:

```python
def test_events_from_journals_dedupes_across_cohorts(tmp_path):
    from tradingagents.strategies.learning.signal_journal import JournalEntry, SignalJournal
    from tradingagents.strategies.validation.journal_source import events_from_journals

    # Two cohort journals with an overlapping signal.
    j1 = SignalJournal(str(tmp_path / "cohortA"))
    j2 = SignalJournal(str(tmp_path / "cohortB"))
    entry = JournalEntry(
        timestamp="2026-05-01T00:00:00",
        strategy="earnings_call",
        ticker="AAPL",
        direction="long",
        score=0.8,
    )
    j1.log_signal(entry)
    j2.log_signal(entry)  # duplicate across cohorts
    j2.log_signal(
        JournalEntry(
            timestamp="2026-05-02T00:00:00",
            strategy="insider_activity",
            ticker="MSFT",
            direction="long",
            score=0.6,
        )
    )

    events = events_from_journals([j1, j2])
    keys = sorted((e.ticker, e.group, e.event_date) for e in events)
    assert keys == [
        ("AAPL", "earnings_call", "2026-05-01"),
        ("MSFT", "insider_activity", "2026-05-02"),
    ]


def test_events_from_journals_filters_by_strategy(tmp_path):
    from tradingagents.strategies.learning.signal_journal import JournalEntry, SignalJournal
    from tradingagents.strategies.validation.journal_source import events_from_journals

    j = SignalJournal(str(tmp_path / "c"))
    j.log_signal(JournalEntry(timestamp="2026-05-01T00:00:00", strategy="earnings_call", ticker="AAPL", direction="long", score=0.8))
    j.log_signal(JournalEntry(timestamp="2026-05-01T00:00:00", strategy="litigation", ticker="XYZ", direction="short", score=0.5))

    events = events_from_journals([j], strategy="earnings_call")
    assert len(events) == 1
    assert events[0].ticker == "AAPL"
    assert events[0].group == "earnings_call"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_event_study.py -k "events_from_journals" -v`
Expected: FAIL with `ModuleNotFoundError: ... validation.journal_source`.

- [ ] **Step 3: Create journal_source.py**

Create `tradingagents/strategies/validation/journal_source.py`:

```python
"""Build EventSpec lists from SignalJournal entries, deduped across cohorts."""
from __future__ import annotations

from typing import Any

from tradingagents.strategies.validation.models import EventSpec


def events_from_journals(
    journals: list[Any],
    *,
    strategy: str | None = None,
    since: str | None = None,
) -> list[EventSpec]:
    """Read entries from one or more SignalJournals into deduped EventSpecs.

    Events are deduped by (strategy, ticker, event_date) where event_date is the
    date part of the entry timestamp. The journal's `strategy` becomes the group.
    """
    seen: set[tuple[str, str, str]] = set()
    events: list[EventSpec] = []

    for journal in journals:
        for entry in journal.get_entries(strategy=strategy, since=since):
            strat = entry.get("strategy", "")
            ticker = entry.get("ticker", "")
            ts = entry.get("timestamp", "")
            if not strat or not ticker or not ts:
                continue
            event_date = ts[:10]
            key = (strat, ticker, event_date)
            if key in seen:
                continue
            seen.add(key)
            events.append(
                EventSpec(
                    ticker=ticker,
                    event_date=event_date,
                    group=strat,
                    metadata={
                        "direction": entry.get("direction", ""),
                        "score": entry.get("score", 0.0),
                    },
                )
            )
    return events
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_event_study.py -k "events_from_journals" -v`
Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
git add tradingagents/strategies/validation/journal_source.py tests/test_event_study.py
git commit -m "feat(validation): journal event source with cross-cohort dedupe"
```

---

## Task 9: CLI subcommand `event-study`

**Files:**
- Modify: `scripts/run_generations.py`
- Create: `tradingagents/strategies/validation/report.py`
- Test: `tests/test_event_study.py`

A small `format_report(result)` function (testable, no I/O) renders the per-group table;
the CLI wires journals → events → engine → report.

- [ ] **Step 1: Write the failing test for the report formatter**

Append to `tests/test_event_study.py`:

```python
def test_format_report_renders_group_and_windows():
    from tradingagents.strategies.validation.models import (
        AggregateResult,
        BootstrapCI,
        EventStudyResult,
        WindowStats,
    )
    from tradingagents.strategies.validation.report import format_report

    result = EventStudyResult(
        aggregates=[
            AggregateResult(
                group="earnings_call",
                n_events=42,
                windows=[
                    WindowStats(
                        window="[0,+5]",
                        n_events=42,
                        mean_car=0.0183,
                        std_car=0.04,
                        t_stat=2.41,
                        p_value=0.020,
                        ci=BootstrapCI(lower=0.0031, upper=0.0328),
                    )
                ],
            )
        ],
        skipped_tickers=["BADX"],
    )
    text = format_report(result)
    assert "earnings_call" in text
    assert "n=42" in text
    assert "[0,+5]" in text
    assert "BADX" in text  # skipped tickers surfaced
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_event_study.py -k "format_report" -v`
Expected: FAIL with `ModuleNotFoundError: ... validation.report`.

- [ ] **Step 3: Create report.py**

Create `tradingagents/strategies/validation/report.py`:

```python
"""Human-readable rendering of an EventStudyResult."""
from __future__ import annotations

from tradingagents.strategies.validation.models import EventStudyResult


def format_report(result: EventStudyResult) -> str:
    lines: list[str] = []
    if not result.aggregates:
        lines.append("No events with sufficient data.")
    for agg in result.aggregates:
        lines.append(f"{agg.group}   (n={agg.n_events} events)")
        lines.append("  window     mean_CAR    t       p       95% CI")
        for w in agg.windows:
            ci = f"[{w.ci.lower * 100:+.2f}%, {w.ci.upper * 100:+.2f}%]"
            lines.append(
                f"  {w.window:<9} {w.mean_car * 100:>+7.2f}%  "
                f"{w.t_stat:>5.2f}  {w.p_value:>5.3f}  {ci}"
            )
        lines.append("")
    if result.skipped_tickers:
        lines.append(f"Skipped (insufficient data): {', '.join(result.skipped_tickers)}")
    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_event_study.py -k "format_report" -v`
Expected: PASS.

- [ ] **Step 5: Add the CLI subparser**

In `scripts/run_generations.py`, find the `retire` subparser block (around line 75-82, ends before `args = parser.parse_args()`) and add this subparser registration immediately after it:

```python
    # event-study
    p_es = sub.add_parser("event-study", help="Run an event study (CAR) over journaled signals")
    p_es.add_argument("--gen", default=None, help="Generation ID (default: all active)")
    p_es.add_argument("--strategy", default=None, help="Limit to one strategy/group")
    p_es.add_argument("--since", default=None, help="Only signals on/after this date (YYYY-MM-DD)")
    p_es.add_argument("--json", default=None, help="Optional path to dump full result as JSON")
```

- [ ] **Step 6: Add the CLI dispatch branch**

In `scripts/run_generations.py`, find the `elif args.command == "retire":` block and add this branch immediately after it (before the end of the `main()` function):

```python
    elif args.command == "event-study":
        import glob
        from dataclasses import asdict

        from tradingagents.strategies.learning.signal_journal import SignalJournal
        from tradingagents.strategies.validation.engine import compute_car
        from tradingagents.strategies.validation.journal_source import events_from_journals
        from tradingagents.strategies.validation.price_adapter import yfinance_price_fn
        from tradingagents.strategies.validation.report import format_report

        gens = manager.list_generations()
        if args.gen:
            gens = [g for g in gens if g.gen_id == args.gen]
        if not gens:
            print("No generations found.")
            return

        journals: list[SignalJournal] = []
        for g in gens:
            for path in glob.glob(f"{g.state_dir}/*/signal_journal.jsonl"):
                cohort_dir = path.rsplit("/", 1)[0]
                journals.append(SignalJournal(cohort_dir))

        events = events_from_journals(journals, strategy=args.strategy, since=args.since)
        if not events:
            print("No journaled signals matched.")
            return
        print(f"Studying {len(events)} unique events across {len(journals)} cohort journals...")

        result = compute_car(events, yfinance_price_fn(), rng_seed=1)
        print(format_report(result))

        if args.json:
            import json

            with open(args.json, "w") as f:
                json.dump(
                    {
                        "events": [asdict(e) for e in result.events],
                        "aggregates": [asdict(a) for a in result.aggregates],
                        "skipped_tickers": result.skipped_tickers,
                    },
                    f,
                    indent=2,
                    default=str,
                )
            print(f"Wrote {args.json}")
```

- [ ] **Step 7: Verify the CLI parses and help works**

Run: `.venv/bin/python scripts/run_generations.py event-study --help`
Expected: usage text showing `--gen`, `--strategy`, `--since`, `--json`.

- [ ] **Step 8: Run the full test file**

Run: `.venv/bin/python -m pytest tests/test_event_study.py -v`
Expected: all PASS.

- [ ] **Step 9: Commit**

```bash
git add scripts/run_generations.py tradingagents/strategies/validation/report.py tests/test_event_study.py
git commit -m "feat(validation): event-study CLI subcommand and report"
```

---

## Task 10: Documentation

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Fix the strategy count and add quantum_readiness**

In `CLAUDE.md`, the Project Overview says "11 event-driven strategies". Change "11" to "12" in that sentence, and in the "### Active Strategies (11 event-driven, paper-trade only)" heading change to "12". Add a row to the strategy table after the `commodity_macro` row:

```markdown
| `quantum_readiness` | `QuantumReadinessStrategy` | yfinance, OpenBB (sector, momentum) |
```

(If the actual class name or data sources differ, open `tradingagents/strategies/modules/quantum_readiness.py` and use its real `name`, class, and `data_sources` values.)

- [ ] **Step 2: Note the pruned modules**

In `CLAUDE.md`, near the top architecture description, add a short note that the 6-agent core pipeline and the `backtesting/` module have been pruned (the repo is autoresearch-only as of commit `80c2bd4`). Keep it factual and brief — one sentence.

- [ ] **Step 3: Document the validation module**

Add a new subsection under the Autoresearch "### Key Components" list:

```markdown
- `strategies/validation/event_study.py` family — offline event study: measures market-adjusted Cumulative Abnormal Returns (CAR) around journaled signals, grouped by strategy, with t-test + bootstrap CI. Run via `python scripts/run_generations.py event-study [--gen gen_NNN] [--strategy NAME]`. Read-only; never touches the live trading path.
```

- [ ] **Step 4: Add the test file to the test list**

In the `## Testing` section's key test files list, add:

```markdown
  - `tests/test_event_study.py` — event study engine: stats (OLS, t-test, bootstrap), compute_car windows/skip logic, journal dedupe, report formatting
```

- [ ] **Step 5: Run the full test suite to confirm nothing regressed**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all pass (event study tests included).

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document event study module, fix strategy count"
```

---

## Notes for the implementer

- **No network in tests.** Every test uses synthetic data or a fake source/journal. Never call real yfinance.
- **Trading-day snap-forward:** `_event_index` returns the first date `>= event_date`, so a weekend/holiday event date lands on the next trading day. Day 0 is that index.
- **Window past today:** if an event's `+30` window extends beyond available prices, that window's CAR is `None` and is excluded from that window's aggregate (other windows still count).
- **Determinism:** the CLI passes `rng_seed=1` so repeated runs give identical CIs. Tests pass explicit seeds.
- **Dataclasses, not pydantic:** matches `modules/base.py` style and avoids new serialization deps; `asdict` handles JSON dump.
