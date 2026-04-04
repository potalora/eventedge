"""Walk-forward validation utilities for strategy backtesting."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional


@dataclass
class WalkForwardWindow:
    """A single walk-forward train/test window."""
    train_start: str  # YYYY-MM-DD
    train_end: str
    test_start: str
    test_end: str


def generate_windows(
    start_date: str,
    end_date: str,
    num_windows: int = 3,
    holdout_weeks: int = 6,
) -> tuple[list[WalkForwardWindow], tuple[str, str]]:
    """Generate walk-forward train/test windows plus a holdout period.

    Splits the date range into `num_windows` sequential train/test pairs,
    with a holdout period at the end.

    Args:
        start_date: Start of the full backtest range (YYYY-MM-DD).
        end_date: End of the full backtest range (YYYY-MM-DD).
        num_windows: Number of walk-forward windows.
        holdout_weeks: Weeks to reserve at the end for holdout validation.

    Returns:
        Tuple of (list of WalkForwardWindows, (holdout_start, holdout_end)).
    """
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")

    holdout_start = end - timedelta(weeks=holdout_weeks)
    usable_end = holdout_start - timedelta(days=1)

    total_days = (usable_end - start).days
    if total_days <= 0 or num_windows <= 0:
        return [], (holdout_start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))

    window_days = total_days // num_windows
    # Each window: 70% train, 30% test
    train_days = max(1, int(window_days * 0.7))
    test_days = max(1, window_days - train_days)

    windows = []
    cursor = start
    for i in range(num_windows):
        train_start_dt = cursor
        train_end_dt = cursor + timedelta(days=train_days - 1)
        test_start_dt = train_end_dt + timedelta(days=1)
        test_end_dt = test_start_dt + timedelta(days=test_days - 1)

        # Don't exceed usable range
        if test_end_dt > usable_end:
            test_end_dt = usable_end

        windows.append(WalkForwardWindow(
            train_start=train_start_dt.strftime("%Y-%m-%d"),
            train_end=train_end_dt.strftime("%Y-%m-%d"),
            test_start=test_start_dt.strftime("%Y-%m-%d"),
            test_end=test_end_dt.strftime("%Y-%m-%d"),
        ))

        cursor = test_end_dt + timedelta(days=1)

    holdout = (holdout_start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
    return windows, holdout


def get_test_dates(windows: list[WalkForwardWindow]) -> list[str]:
    """Extract all test period start dates from windows.

    Returns a list of test_start dates that can be used as trade dates
    for the pipeline.
    """
    return [w.test_start for w in windows]


def has_regime_diversity(trades: list[dict], min_regimes: int = 2) -> bool:
    """Check if trades span at least `min_regimes` different market regimes.

    Args:
        trades: List of trade dicts, each with a "regime" key.
        min_regimes: Minimum number of distinct regimes required.

    Returns:
        True if the trades span at least `min_regimes` distinct regimes.
    """
    regimes = {t.get("regime") for t in trades if t.get("regime")}
    return len(regimes) >= min_regimes


def cross_ticker_validation_split(
    tickers: list[str],
    ratio: float = 0.3,
) -> tuple[list[str], list[str]]:
    """Split tickers into train and holdout sets.

    The first (1 - ratio) fraction goes to train, the rest to holdout.
    At least one ticker in each set if possible.

    Args:
        tickers: List of ticker symbols.
        ratio: Fraction of tickers for the holdout set.

    Returns:
        Tuple of (train_tickers, holdout_tickers).
    """
    if len(tickers) <= 1:
        return list(tickers), []

    holdout_count = max(1, int(len(tickers) * ratio))
    train_count = len(tickers) - holdout_count

    # Ensure at least 1 in train
    if train_count < 1:
        train_count = 1
        holdout_count = len(tickers) - 1

    return tickers[:train_count], tickers[train_count:]
