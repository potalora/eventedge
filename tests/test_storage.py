# tests/test_storage.py
import os
import sqlite3
import tempfile
import pytest

from tradingagents.storage.db import Database


@pytest.fixture
def tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(path)
    yield db
    db.close()
    os.unlink(path)


class TestDatabase:
    def test_creates_decisions_table(self, tmp_db):
        cursor = tmp_db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='decisions'"
        )
        assert cursor.fetchone() is not None

    def test_creates_trades_table(self, tmp_db):
        cursor = tmp_db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='trades'"
        )
        assert cursor.fetchone() is not None

    def test_creates_reports_table(self, tmp_db):
        cursor = tmp_db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='reports'"
        )
        assert cursor.fetchone() is not None

    def test_creates_backtest_runs_table(self, tmp_db):
        cursor = tmp_db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='backtest_runs'"
        )
        assert cursor.fetchone() is not None

    def test_creates_equity_snapshots_table(self, tmp_db):
        cursor = tmp_db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='equity_snapshots'"
        )
        assert cursor.fetchone() is not None

    def test_insert_and_retrieve_decision(self, tmp_db):
        decision_id = tmp_db.insert_decision(
            ticker="NVDA",
            trade_date="2026-01-15",
            rating="BUY",
            full_decision="Buy NVDA based on strong fundamentals",
            options_report="IV is low, consider long calls",
        )
        assert decision_id is not None
        row = tmp_db.get_decision(decision_id)
        assert row["ticker"] == "NVDA"
        assert row["rating"] == "BUY"

    def test_insert_and_retrieve_trade(self, tmp_db):
        decision_id = tmp_db.insert_decision(
            ticker="SOFI", trade_date="2026-03-01", rating="BUY",
            full_decision="Buy", options_report="",
        )
        trade_id = tmp_db.insert_trade(
            decision_id=decision_id,
            ticker="SOFI",
            instrument_type="stock",
            action="buy",
            quantity=100,
            price=10.50,
            option_details=None,
            status="filled",
            pnl=None,
        )
        assert trade_id is not None
        trades = tmp_db.get_trades_for_ticker("SOFI")
        assert len(trades) == 1
        assert trades[0]["price"] == 10.50

    def test_insert_report(self, tmp_db):
        decision_id = tmp_db.insert_decision(
            ticker="AAPL", trade_date="2026-02-01", rating="HOLD",
            full_decision="Hold", options_report="",
        )
        tmp_db.insert_report(
            decision_id=decision_id,
            ticker="AAPL",
            trade_date="2026-02-01",
            report_type="fundamentals",
            content="Strong balance sheet...",
        )
        reports = tmp_db.get_reports_for_decision(decision_id)
        assert len(reports) == 1
        assert reports[0]["report_type"] == "fundamentals"

    def test_insert_backtest_run(self, tmp_db):
        run_id = tmp_db.insert_backtest_run(
            tickers=["SOFI", "PLTR"],
            start_date="2025-09-01",
            end_date="2026-03-01",
            config={"initial_capital": 5000},
            metrics={"sharpe": 1.5, "max_drawdown": -0.12},
        )
        assert run_id is not None
        run = tmp_db.get_backtest_run(run_id)
        assert run["metrics"]["sharpe"] == 1.5

    def test_insert_equity_snapshot(self, tmp_db):
        run_id = tmp_db.insert_backtest_run(
            tickers=["SOFI"], start_date="2025-09-01", end_date="2026-03-01",
            config={}, metrics={},
        )
        tmp_db.insert_equity_snapshot(
            backtest_run_id=run_id,
            date="2025-09-15",
            portfolio_value=5100.0,
            cash=2000.0,
            positions_value=3100.0,
        )
        snapshots = tmp_db.get_equity_curve(run_id)
        assert len(snapshots) == 1
        assert snapshots[0]["portfolio_value"] == 5100.0


from tradingagents.storage.queries import (
    get_portfolio_summary,
    get_recent_signals,
    get_trade_history,
)


class TestAutoresearchStorage:
    # --- Table existence ---
    def test_creates_strategies_table(self, tmp_db):
        cursor = tmp_db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='strategies'"
        )
        assert cursor.fetchone() is not None

    def test_creates_strategy_backtest_results_table(self, tmp_db):
        cursor = tmp_db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='strategy_backtest_results'"
        )
        assert cursor.fetchone() is not None

    def test_creates_strategy_trades_table(self, tmp_db):
        cursor = tmp_db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='strategy_trades'"
        )
        assert cursor.fetchone() is not None

    def test_creates_pipeline_cache_table(self, tmp_db):
        cursor = tmp_db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='pipeline_cache'"
        )
        assert cursor.fetchone() is not None

    def test_creates_reflections_table(self, tmp_db):
        cursor = tmp_db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='reflections'"
        )
        assert cursor.fetchone() is not None

    def test_creates_analyst_weights_table(self, tmp_db):
        cursor = tmp_db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='analyst_weights'"
        )
        assert cursor.fetchone() is not None

    # --- strategies round-trip ---
    def _insert_strategy(self, tmp_db, **kwargs):
        defaults = dict(
            generation=1,
            parent_ids=None,
            name="momentum_v1",
            hypothesis="Stocks with strong momentum continue to rise",
            conviction=75,
            screener_criteria={"min_volume": 1000000},
            instrument="stock",
            entry_rules=["rsi > 60", "price > sma_20"],
            exit_rules=["rsi < 40"],
            position_size_pct=5.0,
            max_risk_pct=2.0,
            time_horizon_days=10,
            regime_born="bull",
            status="proposed",
        )
        defaults.update(kwargs)
        return tmp_db.insert_strategy(**defaults)

    def test_insert_and_get_strategy(self, tmp_db):
        sid = self._insert_strategy(tmp_db)
        assert sid is not None
        s = tmp_db.get_strategy(sid)
        assert s["name"] == "momentum_v1"
        assert s["generation"] == 1
        assert s["status"] == "proposed"

    def test_strategy_json_fields_deserialize(self, tmp_db):
        sid = self._insert_strategy(
            tmp_db,
            screener_criteria={"min_vol": 500000, "sector": "tech"},
            entry_rules=["rule_a", "rule_b"],
            exit_rules=["exit_rule"],
            parent_ids=[10, 11],
        )
        s = tmp_db.get_strategy(sid)
        assert s["screener_criteria"] == {"min_vol": 500000, "sector": "tech"}
        assert s["entry_rules"] == ["rule_a", "rule_b"]
        assert s["exit_rules"] == ["exit_rule"]
        assert s["parent_ids"] == [10, 11]

    def test_get_strategies_by_status(self, tmp_db):
        self._insert_strategy(tmp_db, name="s1", status="proposed")
        self._insert_strategy(tmp_db, name="s2", status="backtested")
        self._insert_strategy(tmp_db, name="s3", status="proposed")
        proposed = tmp_db.get_strategies_by_status("proposed")
        assert len(proposed) == 2
        assert all(s["status"] == "proposed" for s in proposed)

    def test_get_strategies_by_generation(self, tmp_db):
        self._insert_strategy(tmp_db, name="gen1_a", generation=1)
        self._insert_strategy(tmp_db, name="gen1_b", generation=1)
        self._insert_strategy(tmp_db, name="gen2_a", generation=2)
        gen1 = tmp_db.get_strategies_by_generation(1)
        assert len(gen1) == 2

    def test_update_strategy_status(self, tmp_db):
        sid = self._insert_strategy(tmp_db, status="proposed")
        tmp_db.update_strategy_status(sid, "backtested")
        s = tmp_db.get_strategy(sid)
        assert s["status"] == "backtested"

    def test_update_strategy_fitness(self, tmp_db):
        sid = self._insert_strategy(tmp_db)
        tmp_db.update_strategy_fitness(sid, 1.85)
        s = tmp_db.get_strategy(sid)
        assert s["fitness_score"] == pytest.approx(1.85)

    def test_get_top_strategies_orders_by_fitness(self, tmp_db):
        s1 = self._insert_strategy(tmp_db, name="s1", status="backtested")
        s2 = self._insert_strategy(tmp_db, name="s2", status="backtested")
        s3 = self._insert_strategy(tmp_db, name="s3", status="backtested")
        tmp_db.update_strategy_fitness(s1, 1.2)
        tmp_db.update_strategy_fitness(s2, 2.5)
        tmp_db.update_strategy_fitness(s3, 0.8)
        top = tmp_db.get_top_strategies(limit=3)
        assert top[0]["fitness_score"] == pytest.approx(2.5)
        assert top[1]["fitness_score"] == pytest.approx(1.2)
        assert top[2]["fitness_score"] == pytest.approx(0.8)

    def test_get_top_strategies_filters_by_status(self, tmp_db):
        s1 = self._insert_strategy(tmp_db, name="s1", status="proposed")
        s2 = self._insert_strategy(tmp_db, name="s2", status="backtested")
        tmp_db.update_strategy_fitness(s1, 9.9)
        tmp_db.update_strategy_fitness(s2, 1.0)
        top = tmp_db.get_top_strategies(limit=5)
        names = [s["name"] for s in top]
        assert "s1" not in names
        assert "s2" in names

    def test_get_strategy_returns_none_for_missing(self, tmp_db):
        assert tmp_db.get_strategy(9999) is None

    # --- strategy_backtest_results round-trip ---
    def test_insert_and_get_strategy_backtest(self, tmp_db):
        sid = self._insert_strategy(tmp_db)
        bid = tmp_db.insert_strategy_backtest(
            strategy_id=sid,
            sharpe=1.5,
            total_return=0.32,
            max_drawdown=-0.15,
            win_rate=0.55,
            profit_factor=1.8,
            num_trades=45,
            tickers_tested=["AAPL", "MSFT"],
            backtest_period="2025-01-01:2026-01-01",
            walk_forward_scores=[1.2, 1.4, 1.6],
            holdout_sharpe=1.3,
        )
        assert bid is not None
        result = tmp_db.get_strategy_backtest(sid)
        assert result["sharpe"] == pytest.approx(1.5)
        assert result["tickers_tested"] == ["AAPL", "MSFT"]
        assert result["walk_forward_scores"] == [1.2, 1.4, 1.6]

    def test_get_strategy_backtest_returns_none_on_miss(self, tmp_db):
        assert tmp_db.get_strategy_backtest(9999) is None

    # --- strategy_trades round-trip ---
    def test_insert_and_get_strategy_trades(self, tmp_db):
        sid = self._insert_strategy(tmp_db)
        tid = tmp_db.insert_strategy_trade(
            strategy_id=sid,
            ticker="SOFI",
            trade_type="long",
            entry_date="2025-09-01",
            exit_date="2025-09-15",
            instrument="stock",
            entry_price=8.50,
            exit_price=9.75,
            quantity=100,
            pnl=125.0,
            pnl_pct=0.147,
            holding_days=14,
            regime="bull",
        )
        assert tid is not None
        trades = tmp_db.get_strategy_trades(sid)
        assert len(trades) == 1
        assert trades[0]["ticker"] == "SOFI"
        assert trades[0]["pnl"] == pytest.approx(125.0)

    def test_get_strategy_trades_filter_by_type(self, tmp_db):
        sid = self._insert_strategy(tmp_db)
        tmp_db.insert_strategy_trade(
            strategy_id=sid, ticker="AAPL", trade_type="long",
            entry_date="2025-01-01", exit_date="2025-01-10",
            instrument="stock", entry_price=100.0, exit_price=110.0,
            quantity=10, pnl=100.0, pnl_pct=0.1, holding_days=9, regime="bull",
        )
        tmp_db.insert_strategy_trade(
            strategy_id=sid, ticker="AAPL", trade_type="short",
            entry_date="2025-02-01", exit_date="2025-02-10",
            instrument="stock", entry_price=110.0, exit_price=100.0,
            quantity=10, pnl=100.0, pnl_pct=0.09, holding_days=9, regime="bear",
        )
        long_trades = tmp_db.get_strategy_trades(sid, trade_type="long")
        assert len(long_trades) == 1
        assert long_trades[0]["trade_type"] == "long"

    # --- pipeline_cache round-trip ---
    def test_insert_and_get_pipeline_cache(self, tmp_db):
        import json
        rid = tmp_db.insert_pipeline_cache(
            ticker="NVDA",
            trade_date="2026-01-15",
            model_tier="deep",
            rating="BUY",
            market_report="Market looks good",
            sentiment_report="Positive sentiment",
            news_report="No bad news",
            fundamentals_report="Strong fundamentals",
            options_report="Low IV",
            full_decision="Buy NVDA",
            debate_summary="Bulls won",
            analyst_scores={"market": 0.8, "sentiment": 0.7},
        )
        assert rid is not None
        result = tmp_db.get_pipeline_cache("NVDA", "2026-01-15", "deep")
        assert result is not None
        assert result["rating"] == "BUY"
        assert result["analyst_scores"] == {"market": 0.8, "sentiment": 0.7}

    def test_get_pipeline_cache_returns_none_on_miss(self, tmp_db):
        assert tmp_db.get_pipeline_cache("AAPL", "2026-01-01", "quick") is None

    def test_pipeline_cache_upsert_replaces(self, tmp_db):
        tmp_db.insert_pipeline_cache(
            ticker="AAPL", trade_date="2026-01-15", model_tier="quick",
            rating="BUY", market_report="v1", sentiment_report="",
            news_report="", fundamentals_report="", options_report="",
            full_decision="Buy", debate_summary="", analyst_scores={},
        )
        tmp_db.insert_pipeline_cache(
            ticker="AAPL", trade_date="2026-01-15", model_tier="quick",
            rating="SELL", market_report="v2", sentiment_report="",
            news_report="", fundamentals_report="", options_report="",
            full_decision="Sell", debate_summary="", analyst_scores={},
        )
        result = tmp_db.get_pipeline_cache("AAPL", "2026-01-15", "quick")
        assert result["rating"] == "SELL"

    # --- reflections round-trip ---
    def test_insert_and_get_reflections(self, tmp_db):
        tmp_db.insert_reflection(
            generation=1,
            patterns_that_work=["momentum in bull markets"],
            patterns_that_fail=["mean reversion in trending markets"],
            next_generation_guidance=["focus on breakout strategies"],
            regime_notes="Current regime is bullish",
        )
        reflections = tmp_db.get_reflections()
        assert len(reflections) == 1
        assert reflections[0]["generation"] == 1
        assert reflections[0]["patterns_that_work"] == ["momentum in bull markets"]
        assert reflections[0]["patterns_that_fail"] == ["mean reversion in trending markets"]
        assert reflections[0]["next_generation_guidance"] == ["focus on breakout strategies"]

    def test_get_reflections_ordered_by_generation(self, tmp_db):
        tmp_db.insert_reflection(2, ["p2"], ["f2"], ["g2"], "notes2")
        tmp_db.insert_reflection(1, ["p1"], ["f1"], ["g1"], "notes1")
        tmp_db.insert_reflection(3, ["p3"], ["f3"], ["g3"], "notes3")
        reflections = tmp_db.get_reflections()
        gens = [r["generation"] for r in reflections]
        assert gens == sorted(gens)

    def test_get_latest_reflection(self, tmp_db):
        tmp_db.insert_reflection(1, ["p1"], ["f1"], ["g1"], "notes1")
        tmp_db.insert_reflection(3, ["p3"], ["f3"], ["g3"], "notes3")
        tmp_db.insert_reflection(2, ["p2"], ["f2"], ["g2"], "notes2")
        latest = tmp_db.get_latest_reflection()
        assert latest["generation"] == 3

    def test_get_latest_reflection_returns_none_when_empty(self, tmp_db):
        assert tmp_db.get_latest_reflection() is None

    def test_get_reflections_limit(self, tmp_db):
        for i in range(5):
            tmp_db.insert_reflection(i, [f"p{i}"], [f"f{i}"], [f"g{i}"], f"notes{i}")
        limited = tmp_db.get_reflections(limit=3)
        assert len(limited) == 3

    # --- analyst_weights round-trip ---
    def test_upsert_and_get_analyst_weights(self, tmp_db):
        tmp_db.upsert_analyst_weight("fundamentals", 1.5)
        tmp_db.upsert_analyst_weight("sentiment", 0.8)
        weights = tmp_db.get_analyst_weights()
        assert weights == {"fundamentals": 1.5, "sentiment": 0.8}

    def test_upsert_analyst_weight_updates_existing(self, tmp_db):
        tmp_db.upsert_analyst_weight("fundamentals", 1.0)
        tmp_db.upsert_analyst_weight("fundamentals", 2.0)
        weights = tmp_db.get_analyst_weights()
        assert weights["fundamentals"] == pytest.approx(2.0)

    def test_get_analyst_weights_empty(self, tmp_db):
        assert tmp_db.get_analyst_weights() == {}


class TestQueries:
    def test_get_portfolio_summary_empty(self, tmp_db):
        summary = get_portfolio_summary(tmp_db)
        assert summary["total_trades"] == 0
        assert summary["total_pnl"] == 0.0

    def test_get_portfolio_summary_with_trades(self, tmp_db):
        did = tmp_db.insert_decision(
            ticker="SOFI", trade_date="2026-03-01", rating="BUY",
            full_decision="Buy", options_report="",
        )
        tmp_db.insert_trade(
            decision_id=did, ticker="SOFI", instrument_type="stock",
            action="buy", quantity=100, price=10.0,
            option_details=None, status="filled", pnl=150.0,
        )
        tmp_db.insert_trade(
            decision_id=did, ticker="SOFI", instrument_type="stock",
            action="sell", quantity=100, price=11.50,
            option_details=None, status="filled", pnl=-50.0,
        )
        summary = get_portfolio_summary(tmp_db)
        assert summary["total_trades"] == 2
        assert summary["total_pnl"] == 100.0

    def test_get_recent_signals(self, tmp_db):
        tmp_db.insert_decision(
            ticker="PLTR", trade_date="2026-03-01", rating="BUY",
            full_decision="Buy PLTR", options_report="",
        )
        tmp_db.insert_decision(
            ticker="SOFI", trade_date="2026-03-02", rating="SELL",
            full_decision="Sell SOFI", options_report="",
        )
        signals = get_recent_signals(tmp_db, limit=5)
        assert len(signals) == 2
        assert signals[0]["ticker"] == "SOFI"  # most recent first

    def test_get_trade_history(self, tmp_db):
        did = tmp_db.insert_decision(
            ticker="NVDA", trade_date="2026-01-15", rating="BUY",
            full_decision="Buy", options_report="",
        )
        tmp_db.insert_trade(
            decision_id=did, ticker="NVDA", instrument_type="stock",
            action="buy", quantity=10, price=500.0,
            option_details=None, status="filled", pnl=None,
        )
        history = get_trade_history(tmp_db, ticker="NVDA")
        assert len(history) == 1
        assert history[0]["ticker"] == "NVDA"
