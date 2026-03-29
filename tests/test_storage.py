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
