"""Tests for tradingagents.autoresearch.fitness."""
import pytest
from unittest.mock import MagicMock, call
from tradingagents.autoresearch.models import Strategy, BacktestResults, ScreenerCriteria, Filter
from tradingagents.autoresearch.fitness import (
    compute_fitness,
    rank_strategies,
    meets_paper_criteria,
    meets_graduation_criteria,
    check_failure_criteria,
    update_analyst_weights,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_br(**kwargs):
    defaults = dict(
        sharpe=1.5,
        total_return=0.3,
        max_drawdown=-0.10,
        win_rate=0.60,
        profit_factor=2.0,
        num_trades=20,
        walk_forward_scores=[1.4, 1.6],
        holdout_sharpe=1.3,
    )
    defaults.update(kwargs)
    return BacktestResults(**defaults)


def make_strategy(**kwargs):
    defaults = dict(
        name="Test",
        entry_rules=[],
        backtest_results=make_br(),
        screener=ScreenerCriteria(),
    )
    defaults.update(kwargs)
    return Strategy(**defaults)


CONFIG = {
    "autoresearch": {
        "min_trades_for_scoring": 5,
        "complexity_penalty_factor": 0.1,
        "fitness_min_sharpe": 1.0,
        "fitness_min_trades": 10,
        "fitness_min_win_rate": 0.50,
        "paper_min_trades": 5,
        "paper_max_divergence_pct": 15,
        "analyst_weight_min": 0.3,
        "analyst_weight_max": 2.5,
    }
}


# ---------------------------------------------------------------------------
# compute_fitness
# ---------------------------------------------------------------------------

class TestComputeFitness:
    def test_basic_formula(self):
        br = make_br(sharpe=1.5, profit_factor=2.0, max_drawdown=-0.10, num_trades=20)
        s = make_strategy(backtest_results=br, entry_rules=[], screener=ScreenerCriteria())
        # base = 1.5 * min(2.0, 3.0) * (1 - 0.10) = 1.5 * 2.0 * 0.9 = 2.7
        # complexity = 0 rules + 0 filters = 0
        # penalty = 1 / (1 + 0.1 * 0) = 1.0
        # fitness = 2.7
        result = compute_fitness(s, CONFIG)
        assert abs(result - 2.7) < 1e-9

    def test_no_backtest_returns_zero(self):
        s = make_strategy(backtest_results=None)
        assert compute_fitness(s, CONFIG) == 0.0

    def test_insufficient_trades_returns_zero(self):
        br = make_br(num_trades=3)
        s = make_strategy(backtest_results=br)
        assert compute_fitness(s, CONFIG) == 0.0

    def test_exactly_min_trades_scores(self):
        br = make_br(num_trades=5)
        s = make_strategy(backtest_results=br, entry_rules=[], screener=ScreenerCriteria())
        assert compute_fitness(s, CONFIG) > 0.0

    def test_profit_factor_capped_at_3(self):
        br_capped = make_br(sharpe=1.0, profit_factor=10.0, max_drawdown=0.0, num_trades=10)
        br_ref = make_br(sharpe=1.0, profit_factor=3.0, max_drawdown=0.0, num_trades=10)
        s_capped = make_strategy(backtest_results=br_capped, entry_rules=[], screener=ScreenerCriteria())
        s_ref = make_strategy(backtest_results=br_ref, entry_rules=[], screener=ScreenerCriteria())
        assert abs(compute_fitness(s_capped, CONFIG) - compute_fitness(s_ref, CONFIG)) < 1e-9

    def test_complexity_penalty_applied(self):
        filters = [Filter("rsi_14", "<", 30), Filter("volume_ratio", ">", 2)]
        screener = ScreenerCriteria(custom_filters=filters)
        entry_rules = ["rule1", "rule2", "rule3"]
        br = make_br(sharpe=1.0, profit_factor=1.0, max_drawdown=0.0, num_trades=10)
        s = make_strategy(backtest_results=br, entry_rules=entry_rules, screener=screener)
        # complexity = 2 filters + 3 rules = 5
        # penalty = 1 / (1 + 0.1 * 5) = 1/1.5 ≈ 0.6667
        # base = 1.0 * 1.0 * 1.0 = 1.0
        # fitness = 0.6667
        result = compute_fitness(s, CONFIG)
        expected = 1.0 / 1.5
        assert abs(result - expected) < 1e-9

    def test_max_drawdown_absolute_value(self):
        # Positive max_drawdown should also reduce score
        br = make_br(sharpe=1.0, profit_factor=1.0, max_drawdown=0.20, num_trades=10)
        s = make_strategy(backtest_results=br, entry_rules=[], screener=ScreenerCriteria())
        # base = 1.0 * 1.0 * (1 - 0.20) = 0.8
        result = compute_fitness(s, CONFIG)
        assert abs(result - 0.8) < 1e-9


# ---------------------------------------------------------------------------
# rank_strategies
# ---------------------------------------------------------------------------

class TestRankStrategies:
    def test_sorted_by_fitness_descending(self):
        s1 = make_strategy(name="Low", backtest_results=make_br(sharpe=0.5, num_trades=10))
        s2 = make_strategy(name="High", backtest_results=make_br(sharpe=2.0, num_trades=10))
        s3 = make_strategy(name="Mid", backtest_results=make_br(sharpe=1.2, num_trades=10))
        ranked = rank_strategies([s1, s2, s3], CONFIG)
        assert ranked[0].name == "High"
        assert ranked[1].name == "Mid"
        assert ranked[2].name == "Low"

    def test_fitness_score_set_on_strategies(self):
        s = make_strategy(backtest_results=make_br(num_trades=10))
        ranked = rank_strategies([s], CONFIG)
        assert ranked[0].fitness_score > 0.0

    def test_insufficient_trades_gets_zero_fitness(self):
        s = make_strategy(backtest_results=make_br(num_trades=2))
        ranked = rank_strategies([s], CONFIG)
        assert ranked[0].fitness_score == 0.0

    def test_tiebreaker_win_rate(self):
        br1 = make_br(sharpe=1.5, profit_factor=2.0, max_drawdown=-0.10, num_trades=10, win_rate=0.55)
        br2 = make_br(sharpe=1.5, profit_factor=2.0, max_drawdown=-0.10, num_trades=10, win_rate=0.70)
        s1 = make_strategy(name="LowWR", backtest_results=br1, entry_rules=[], screener=ScreenerCriteria())
        s2 = make_strategy(name="HighWR", backtest_results=br2, entry_rules=[], screener=ScreenerCriteria())
        ranked = rank_strategies([s1, s2], CONFIG)
        assert ranked[0].name == "HighWR"

    def test_tiebreaker_num_trades(self):
        br1 = make_br(sharpe=1.5, profit_factor=2.0, max_drawdown=-0.10, num_trades=10, win_rate=0.60)
        br2 = make_br(sharpe=1.5, profit_factor=2.0, max_drawdown=-0.10, num_trades=30, win_rate=0.60)
        s1 = make_strategy(name="FewTrades", backtest_results=br1, entry_rules=[], screener=ScreenerCriteria())
        s2 = make_strategy(name="ManyTrades", backtest_results=br2, entry_rules=[], screener=ScreenerCriteria())
        ranked = rank_strategies([s1, s2], CONFIG)
        assert ranked[0].name == "ManyTrades"


# ---------------------------------------------------------------------------
# meets_paper_criteria
# ---------------------------------------------------------------------------

class TestMeetsPaperCriteria:
    def test_passes_all_criteria(self):
        br = make_br(sharpe=1.5, num_trades=15, win_rate=0.60,
                     walk_forward_scores=[1.4, 1.6], holdout_sharpe=1.3)
        s = make_strategy(backtest_results=br)
        assert meets_paper_criteria(s, CONFIG) is True

    def test_fails_no_backtest(self):
        s = make_strategy(backtest_results=None)
        assert meets_paper_criteria(s, CONFIG) is False

    def test_fails_sharpe_too_low(self):
        br = make_br(sharpe=0.9, num_trades=15, win_rate=0.60)
        s = make_strategy(backtest_results=br)
        assert meets_paper_criteria(s, CONFIG) is False

    def test_fails_sharpe_exactly_threshold(self):
        # Must be strictly greater than threshold
        br = make_br(sharpe=1.0, num_trades=15, win_rate=0.60)
        s = make_strategy(backtest_results=br)
        assert meets_paper_criteria(s, CONFIG) is False

    def test_fails_insufficient_trades(self):
        br = make_br(sharpe=1.5, num_trades=5, win_rate=0.60)
        s = make_strategy(backtest_results=br)
        assert meets_paper_criteria(s, CONFIG) is False

    def test_fails_low_win_rate(self):
        br = make_br(sharpe=1.5, num_trades=15, win_rate=0.45)
        s = make_strategy(backtest_results=br)
        assert meets_paper_criteria(s, CONFIG) is False

    def test_fails_holdout_degradation(self):
        # holdout_sharpe < mean_wf * 0.7
        br = make_br(sharpe=1.5, num_trades=15, win_rate=0.60,
                     walk_forward_scores=[2.0, 2.0], holdout_sharpe=1.0)
        # mean_wf=2.0, threshold=1.4, holdout=1.0 < 1.4 -> fail
        s = make_strategy(backtest_results=br)
        assert meets_paper_criteria(s, CONFIG) is False

    def test_passes_no_holdout(self):
        br = make_br(sharpe=1.5, num_trades=15, win_rate=0.60,
                     walk_forward_scores=[], holdout_sharpe=None)
        s = make_strategy(backtest_results=br)
        assert meets_paper_criteria(s, CONFIG) is True


# ---------------------------------------------------------------------------
# meets_graduation_criteria
# ---------------------------------------------------------------------------

def _make_trades(n, win=True, pnl_pct=0.05, exit_date="2026-01-10"):
    return [
        {"exit_date": exit_date, "pnl": 100 if win else -100, "pnl_pct": pnl_pct if win else -pnl_pct}
        for _ in range(n)
    ]


class TestMeetsGraduationCriteria:
    def test_passes_all_criteria(self):
        br = make_br(win_rate=0.60)
        s = make_strategy(backtest_results=br, max_risk_pct=0.05)
        # 5 completed trades with slightly varied positive returns (Sharpe > 0.5)
        # win_rate = 5/5 = 1.0 -> divergence from 0.60 = 40% > 15% -> fails!
        # Use 3 wins, 2 losses to keep win_rate near 0.60
        trades = [
            {"exit_date": "2026-01-06", "pnl": 100, "pnl_pct": 0.04},
            {"exit_date": "2026-01-07", "pnl": 120, "pnl_pct": 0.05},
            {"exit_date": "2026-01-08", "pnl": 110, "pnl_pct": 0.045},
            {"exit_date": "2026-01-09", "pnl": 130, "pnl_pct": 0.055},
            {"exit_date": "2026-01-10", "pnl": 115, "pnl_pct": 0.048},
        ]
        # win_rate = 5/5 = 1.0 -> divergence from 0.60 = 40% -> fails divergence check
        # Need win_rate close to 0.60: use 3 wins (pnl>0) + 2 small losses
        trades = [
            {"exit_date": "2026-01-06", "pnl": 100, "pnl_pct": 0.06},
            {"exit_date": "2026-01-07", "pnl": 120, "pnl_pct": 0.07},
            {"exit_date": "2026-01-08", "pnl": 110, "pnl_pct": 0.065},
            {"exit_date": "2026-01-09", "pnl": -5,  "pnl_pct": -0.003},
            {"exit_date": "2026-01-10", "pnl": -6,  "pnl_pct": -0.003},
        ]
        # win_rate = 3/5 = 0.60 -> divergence = 0% -> pass
        # mean_pnl_pct ≈ 0.0378, std ≈ 0.036, sharpe ≈ 1.05 > 0.5 -> pass
        # max loss = 0.003 < 2 * 0.05 = 0.10 -> pass
        assert meets_graduation_criteria(s, trades, CONFIG) is True

    def test_fails_insufficient_completed_trades(self):
        s = make_strategy()
        trades = _make_trades(3, win=True, pnl_pct=0.05)
        assert meets_graduation_criteria(s, trades, CONFIG) is False

    def test_fails_incomplete_trades_not_counted(self):
        s = make_strategy()
        trades = [{"exit_date": None, "pnl": 100, "pnl_pct": 0.05}] * 10
        assert meets_graduation_criteria(s, trades, CONFIG) is False

    def test_fails_win_rate_divergence(self):
        # Backtest win_rate=0.80, paper=0 wins -> divergence=80% > 15%
        br = make_br(win_rate=0.80)
        s = make_strategy(backtest_results=br, max_risk_pct=0.05)
        trades = _make_trades(5, win=False, pnl_pct=0.05)
        assert meets_graduation_criteria(s, trades, CONFIG) is False

    def test_fails_large_single_loss(self):
        br = make_br(win_rate=0.60)
        s = make_strategy(backtest_results=br, max_risk_pct=0.05)
        # 4 winners + 1 catastrophic loser (> 2x 5% = 10%)
        trades = _make_trades(4, win=True, pnl_pct=0.05)
        trades.append({"exit_date": "2026-01-10", "pnl": -500, "pnl_pct": -0.15})
        assert meets_graduation_criteria(s, trades, CONFIG) is False

    def test_fails_low_paper_sharpe(self):
        br = make_br(win_rate=0.60)
        s = make_strategy(backtest_results=br, max_risk_pct=0.05)
        # Mixed returns with high variance -> low Sharpe
        trades = [
            {"exit_date": "2026-01-10", "pnl": 500, "pnl_pct": 0.10},
            {"exit_date": "2026-01-10", "pnl": -490, "pnl_pct": -0.09},
            {"exit_date": "2026-01-10", "pnl": 500, "pnl_pct": 0.10},
            {"exit_date": "2026-01-10", "pnl": -490, "pnl_pct": -0.09},
            {"exit_date": "2026-01-10", "pnl": 500, "pnl_pct": 0.10},
        ]
        # win_rate = 3/5 = 0.60 -> close to backtest win_rate=0.60 -> passes divergence
        # Sharpe: returns=[0.1, -0.09, 0.1, -0.09, 0.1], mean=0.024, std≈0.099, sharpe≈0.24 < 0.5
        assert meets_graduation_criteria(s, trades, CONFIG) is False


# ---------------------------------------------------------------------------
# check_failure_criteria
# ---------------------------------------------------------------------------

class TestCheckFailureCriteria:
    def test_no_completed_trades_returns_false(self):
        s = make_strategy()
        assert check_failure_criteria(s, []) is False

    def test_no_exit_date_trades_returns_false(self):
        s = make_strategy()
        trades = [{"exit_date": None, "pnl": -100, "pnl_pct": -0.05}] * 5
        assert check_failure_criteria(s, trades) is False

    def test_fails_win_rate_20pts_below_backtest(self):
        br = make_br(win_rate=0.70)
        s = make_strategy(backtest_results=br)
        # paper win_rate = 0/5 = 0.0 -> 70% gap
        trades = [{"exit_date": "2026-01-10", "pnl": -50, "pnl_pct": -0.02}] * 5
        assert check_failure_criteria(s, trades) is True

    def test_passes_win_rate_within_threshold(self):
        br = make_br(win_rate=0.60)
        s = make_strategy(backtest_results=br)
        # paper win_rate = 3/5 = 0.60 -> 0% gap
        trades = [{"exit_date": "2026-01-10", "pnl": 50, "pnl_pct": 0.02}] * 3
        trades += [{"exit_date": "2026-01-10", "pnl": -10, "pnl_pct": -0.005}] * 2
        assert check_failure_criteria(s, trades) is False

    def test_fails_three_consecutive_losses(self):
        br = make_br(win_rate=0.60)
        s = make_strategy(backtest_results=br)
        trades = [
            {"exit_date": "2026-01-08", "pnl": 100, "pnl_pct": 0.05},
            {"exit_date": "2026-01-09", "pnl": -50, "pnl_pct": -0.02},
            {"exit_date": "2026-01-10", "pnl": -50, "pnl_pct": -0.02},
            {"exit_date": "2026-01-11", "pnl": -50, "pnl_pct": -0.02},
        ]
        assert check_failure_criteria(s, trades) is True

    def test_passes_non_consecutive_losses(self):
        br = make_br(win_rate=0.60)
        s = make_strategy(backtest_results=br)
        trades = [
            {"exit_date": "2026-01-08", "pnl": -50, "pnl_pct": -0.02},
            {"exit_date": "2026-01-09", "pnl": 100, "pnl_pct": 0.05},
            {"exit_date": "2026-01-10", "pnl": -50, "pnl_pct": -0.02},
            {"exit_date": "2026-01-11", "pnl": 100, "pnl_pct": 0.05},
            {"exit_date": "2026-01-12", "pnl": -50, "pnl_pct": -0.02},
        ]
        assert check_failure_criteria(s, trades) is False

    def test_fails_negative_sharpe_after_five_trades(self):
        br = make_br(win_rate=0.60)
        s = make_strategy(backtest_results=br)
        # All small losses -> mean negative, negative sharpe
        trades = [
            {"exit_date": "2026-01-0{}".format(i + 1), "pnl": -10, "pnl_pct": -0.01}
            for i in range(5)
        ]
        # win_rate = 0/5 = 0.0, backtest=0.60 -> 60% gap -> fails win_rate first
        # Let's use a backtest win_rate that won't trigger the win_rate check
        br2 = make_br(win_rate=0.10)
        s2 = make_strategy(backtest_results=br2)
        # win_rate gap = 0.10 - 0.0 = 0.10 < 0.20 -> passes win_rate check
        # returns all -0.01, std=0, sharpe undefined -> 0.0 -> not < 0 -> ...
        # Use slightly varied returns to get negative sharpe
        trades2 = [
            {"exit_date": "2026-01-01", "pnl": -10, "pnl_pct": -0.010},
            {"exit_date": "2026-01-02", "pnl": -12, "pnl_pct": -0.012},
            {"exit_date": "2026-01-03", "pnl": -8,  "pnl_pct": -0.008},
            {"exit_date": "2026-01-04", "pnl": -11, "pnl_pct": -0.011},
            {"exit_date": "2026-01-05", "pnl": -9,  "pnl_pct": -0.009},
        ]
        assert check_failure_criteria(s2, trades2) is True

    def test_no_failure_with_good_performance(self):
        br = make_br(win_rate=0.60)
        s = make_strategy(backtest_results=br)
        trades = [
            {"exit_date": "2026-01-0{}".format(i + 1), "pnl": 50, "pnl_pct": 0.02}
            for i in range(5)
        ]
        assert check_failure_criteria(s, trades) is False


# ---------------------------------------------------------------------------
# update_analyst_weights
# ---------------------------------------------------------------------------

class TestUpdateAnalystWeights:
    def _make_db(self, current_weights=None):
        db = MagicMock()
        db.get_analyst_weights.return_value = current_weights or {}
        return db

    def test_returns_all_five_analysts(self):
        db = self._make_db()
        result = update_analyst_weights(db, [], CONFIG)
        assert set(result.keys()) == {"market", "news", "sentiment", "fundamentals", "options"}

    def test_top_quartile_boosted(self):
        # With 5 analysts, q3_cutoff = 5 - 1 = 4, so only index 4 (best scorer) gets boosted
        db = self._make_db({"market": 1.0, "news": 1.0, "sentiment": 1.0, "fundamentals": 1.0, "options": 1.0})
        trades = [
            {"analyst_scores": {"market": 0, "news": 0, "sentiment": 0, "fundamentals": 0, "options": 10}}
        ]
        result = update_analyst_weights(db, trades, CONFIG)
        assert result["options"] == round(1.0 * 1.05, 4)

    def test_bottom_quartile_penalized(self):
        # With 5 analysts, q1_cutoff = 1, so index 0 (worst scorer) gets penalized
        db = self._make_db({"market": 1.0, "news": 1.0, "sentiment": 1.0, "fundamentals": 1.0, "options": 1.0})
        trades = [
            {"analyst_scores": {"market": -10, "news": 0, "sentiment": 0, "fundamentals": 0, "options": 0}}
        ]
        result = update_analyst_weights(db, trades, CONFIG)
        assert result["market"] == round(1.0 * 0.95, 4)

    def test_weight_clamped_to_min(self):
        db = self._make_db({"market": 0.31, "news": 1.0, "sentiment": 1.0, "fundamentals": 1.0, "options": 1.0})
        # market gets the worst score -> penalized -> 0.31 * 0.95 = 0.2945 < 0.3 -> clamp to 0.3
        trades = [
            {"analyst_scores": {"market": -100, "news": 0, "sentiment": 0, "fundamentals": 0, "options": 0}}
        ]
        result = update_analyst_weights(db, trades, CONFIG)
        assert result["market"] == 0.3

    def test_weight_clamped_to_max(self):
        db = self._make_db({"market": 1.0, "news": 1.0, "sentiment": 1.0, "fundamentals": 1.0, "options": 2.48})
        # options gets best score -> 2.48 * 1.05 = 2.604 > 2.5 -> clamp to 2.5
        trades = [
            {"analyst_scores": {"market": 0, "news": 0, "sentiment": 0, "fundamentals": 0, "options": 100}}
        ]
        result = update_analyst_weights(db, trades, CONFIG)
        assert result["options"] == 2.5

    def test_db_upsert_called_for_each_analyst(self):
        db = self._make_db()
        update_analyst_weights(db, [], CONFIG)
        assert db.upsert_analyst_weight.call_count == 5

    def test_missing_analysts_default_to_1(self):
        db = self._make_db({"market": 1.2})  # only market pre-populated
        result = update_analyst_weights(db, [], CONFIG)
        # All 5 should exist; non-populated ones start at 1.0
        assert "news" in result
        assert "sentiment" in result
