"""Tests for walk-forward validation utilities."""

import pytest
from datetime import datetime

from tradingagents.autoresearch.walk_forward import (
    WalkForwardWindow,
    generate_windows,
    get_test_dates,
    has_regime_diversity,
    cross_ticker_validation_split,
)


class TestGenerateWindows:
    """Tests for generate_windows()."""

    def test_correct_window_count(self):
        windows, holdout = generate_windows("2024-01-01", "2025-01-01", num_windows=3, holdout_weeks=6)
        assert len(windows) == 3

    def test_holdout_at_end(self):
        windows, holdout = generate_windows("2024-01-01", "2025-01-01", num_windows=3, holdout_weeks=6)
        holdout_start, holdout_end = holdout
        assert holdout_end == "2025-01-01"
        # Holdout should be ~6 weeks before end
        hs = datetime.strptime(holdout_start, "%Y-%m-%d")
        he = datetime.strptime(holdout_end, "%Y-%m-%d")
        assert (he - hs).days == 6 * 7  # exactly 6 weeks

    def test_sequential_dates(self):
        windows, _ = generate_windows("2024-01-01", "2025-01-01", num_windows=3, holdout_weeks=6)
        for i in range(len(windows) - 1):
            # Each window's test_end should be before next window's train_start
            curr_end = datetime.strptime(windows[i].test_end, "%Y-%m-%d")
            next_start = datetime.strptime(windows[i + 1].train_start, "%Y-%m-%d")
            assert curr_end < next_start

    def test_train_before_test(self):
        windows, _ = generate_windows("2024-01-01", "2025-01-01", num_windows=3, holdout_weeks=6)
        for w in windows:
            assert w.train_start <= w.train_end
            assert w.train_end < w.test_start
            assert w.test_start <= w.test_end

    def test_windows_before_holdout(self):
        windows, holdout = generate_windows("2024-01-01", "2025-01-01", num_windows=3, holdout_weeks=6)
        holdout_start = datetime.strptime(holdout[0], "%Y-%m-%d")
        for w in windows:
            test_end = datetime.strptime(w.test_end, "%Y-%m-%d")
            assert test_end < holdout_start

    def test_single_window(self):
        windows, holdout = generate_windows("2024-01-01", "2025-01-01", num_windows=1, holdout_weeks=6)
        assert len(windows) == 1
        assert windows[0].train_start == "2024-01-01"

    def test_zero_windows_returns_empty(self):
        windows, holdout = generate_windows("2024-01-01", "2025-01-01", num_windows=0, holdout_weeks=6)
        assert len(windows) == 0

    def test_short_range_returns_empty(self):
        windows, holdout = generate_windows("2024-01-01", "2024-01-15", num_windows=3, holdout_weeks=6)
        # Range is only 14 days, holdout is 42 days — no usable space
        assert len(windows) == 0

    def test_two_windows(self):
        windows, holdout = generate_windows("2023-01-01", "2024-06-01", num_windows=2, holdout_weeks=6)
        assert len(windows) == 2

    def test_holdout_returned_even_when_no_windows(self):
        windows, holdout = generate_windows("2024-01-01", "2024-02-01", num_windows=3, holdout_weeks=6)
        assert holdout[1] == "2024-02-01"


class TestGetTestDates:
    """Tests for get_test_dates()."""

    def test_extracts_test_starts(self):
        windows = [
            WalkForwardWindow("2024-01-01", "2024-03-01", "2024-03-02", "2024-04-01"),
            WalkForwardWindow("2024-04-02", "2024-06-01", "2024-06-02", "2024-07-01"),
        ]
        dates = get_test_dates(windows)
        assert dates == ["2024-03-02", "2024-06-02"]

    def test_empty_windows(self):
        assert get_test_dates([]) == []


class TestHasRegimeDiversity:
    """Tests for has_regime_diversity()."""

    def test_diverse(self):
        trades = [
            {"regime": "RISK_ON", "pnl": 100},
            {"regime": "RISK_OFF", "pnl": -50},
            {"regime": "CRISIS", "pnl": -200},
        ]
        assert has_regime_diversity(trades, min_regimes=2) is True

    def test_not_diverse(self):
        trades = [
            {"regime": "RISK_ON", "pnl": 100},
            {"regime": "RISK_ON", "pnl": 50},
        ]
        assert has_regime_diversity(trades, min_regimes=2) is False

    def test_empty_trades(self):
        assert has_regime_diversity([], min_regimes=2) is False

    def test_none_regimes_ignored(self):
        trades = [
            {"regime": "RISK_ON"},
            {"regime": None},
            {"pnl": 100},  # no regime key at all
        ]
        assert has_regime_diversity(trades, min_regimes=2) is False

    def test_exactly_min(self):
        trades = [
            {"regime": "RISK_ON"},
            {"regime": "RISK_OFF"},
        ]
        assert has_regime_diversity(trades, min_regimes=2) is True

    def test_min_regimes_three(self):
        trades = [
            {"regime": "RISK_ON"},
            {"regime": "RISK_OFF"},
        ]
        assert has_regime_diversity(trades, min_regimes=3) is False


class TestCrossTickerValidationSplit:
    """Tests for cross_ticker_validation_split()."""

    def test_standard_split(self):
        tickers = ["AAPL", "MSFT", "GOOG", "AMZN", "META", "NVDA", "TSLA", "JPM", "BAC", "GS"]
        train, holdout = cross_ticker_validation_split(tickers, ratio=0.3)
        assert len(train) == 7
        assert len(holdout) == 3
        assert train + holdout == tickers

    def test_single_ticker(self):
        train, holdout = cross_ticker_validation_split(["AAPL"], ratio=0.3)
        assert train == ["AAPL"]
        assert holdout == []

    def test_empty_list(self):
        train, holdout = cross_ticker_validation_split([], ratio=0.3)
        assert train == []
        assert holdout == []

    def test_two_tickers(self):
        train, holdout = cross_ticker_validation_split(["AAPL", "MSFT"], ratio=0.3)
        assert len(train) >= 1
        assert len(holdout) >= 0
        assert len(train) + len(holdout) == 2

    def test_preserves_order(self):
        tickers = ["AAPL", "MSFT", "GOOG", "AMZN", "META"]
        train, holdout = cross_ticker_validation_split(tickers, ratio=0.3)
        # Train should be the first N, holdout the rest
        assert train == tickers[:len(train)]
        assert holdout == tickers[len(train):]

    def test_high_ratio(self):
        tickers = ["AAPL", "MSFT", "GOOG"]
        train, holdout = cross_ticker_validation_split(tickers, ratio=0.9)
        # Should still have at least 1 in train
        assert len(train) >= 1
