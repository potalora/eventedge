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
