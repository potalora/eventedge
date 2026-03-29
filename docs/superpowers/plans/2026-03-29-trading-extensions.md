# TradingAgents Extensions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend TradingAgents with options analysis, backtesting, Alpaca execution, Streamlit dashboard, scheduling, and alerts.

**Architecture:** Fork-and-extend approach. Options analyst joins the LangGraph agent pipeline as a peer analyst. Backtesting wraps `propagate()` in a date loop. Execution layer abstracts brokers behind a common interface. Dashboard reads from SQLite. Scheduler runs jobs via APScheduler.

**Tech Stack:** Python 3.11, LangGraph, LangChain, yfinance, py_vollib, numpy, alpaca-py, streamlit, plotly, apprise, apscheduler, SQLite (vectorbt dropped in favor of numpy for simpler metrics computation)

**Design spec:** `docs/superpowers/specs/2026-03-29-trading-extensions-design.md`

---

## File Structure

### New Files

```
tradingagents/storage/__init__.py
tradingagents/storage/db.py                          # SQLite connection + schema
tradingagents/storage/queries.py                     # Common DB queries

tradingagents/dataflows/options_data.py              # Options chain + Greeks from yfinance/vollib
tradingagents/agents/utils/options_tools.py          # LangChain tool definitions for options
tradingagents/agents/analysts/options_analyst.py     # Options analyst agent

tradingagents/backtesting/__init__.py
tradingagents/backtesting/engine.py                  # Main backtest loop
tradingagents/backtesting/portfolio.py               # Position tracking + P&L
tradingagents/backtesting/metrics.py                 # Sharpe, drawdown, accuracy
tradingagents/backtesting/report.py                  # Generate backtest reports

tradingagents/execution/__init__.py
tradingagents/execution/base_broker.py               # Abstract broker interface
tradingagents/execution/paper_broker.py              # Local simulation broker
tradingagents/execution/alpaca_broker.py             # Alpaca paper + live
tradingagents/execution/position_manager.py          # Decision parser + risk enforcement

tradingagents/dashboard/__init__.py
tradingagents/dashboard/app.py                       # Streamlit entry point
tradingagents/dashboard/pages/portfolio.py           # Portfolio page
tradingagents/dashboard/pages/analysis.py            # Agent reports page
tradingagents/dashboard/pages/backtest.py            # Backtest results page
tradingagents/dashboard/pages/trades.py              # Trade history page
tradingagents/dashboard/components/charts.py         # Plotly chart helpers
tradingagents/dashboard/components/formatters.py     # Report formatters

tradingagents/scheduler/__init__.py
tradingagents/scheduler/scheduler.py                 # APScheduler management
tradingagents/scheduler/jobs.py                      # Job definitions
tradingagents/scheduler/alerts.py                    # Apprise notifications

tests/test_storage.py
tests/test_options_data.py
tests/test_options_analyst.py
tests/test_backtesting.py
tests/test_portfolio.py
tests/test_metrics.py
tests/test_execution.py
tests/test_position_manager.py
tests/test_dashboard.py
tests/test_scheduler.py
tests/test_alerts.py
```

### Modified Files

```
tradingagents/default_config.py                      # Add options/backtest/execution/scheduler/alerts config
tradingagents/agents/utils/agent_states.py           # Add options_report field
tradingagents/agents/__init__.py                     # Export create_options_analyst
tradingagents/dataflows/interface.py                 # Register options tools in VENDOR_METHODS
tradingagents/graph/setup.py                         # Add options analyst to graph
tradingagents/graph/trading_graph.py                 # Add options tool node, storage hooks
tradingagents/graph/conditional_logic.py             # Add should_continue_options
tradingagents/agents/researchers/bull_researcher.py  # Add options_report to prompt
tradingagents/agents/researchers/bear_researcher.py  # Add options_report to prompt
tradingagents/agents/managers/portfolio_manager.py   # Add options context to prompt
tradingagents/agents/trader/trader.py                # Add options context to prompt
pyproject.toml                                       # Add new dependencies
```

---

## Phase 1: Storage (SQLite)

### Task 1: SQLite Schema and Connection

**Files:**
- Create: `tradingagents/storage/__init__.py`
- Create: `tradingagents/storage/db.py`
- Test: `tests/test_storage.py`

- [ ] **Step 1: Write failing tests for DB schema creation**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_storage.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tradingagents.storage'`

- [ ] **Step 3: Implement Database class**

```python
# tradingagents/storage/__init__.py
from .db import Database

__all__ = ["Database"]
```

```python
# tradingagents/storage/db.py
import json
import sqlite3
from typing import Any, Dict, List, Optional


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                rating TEXT NOT NULL,
                full_decision TEXT NOT NULL,
                options_report TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id INTEGER REFERENCES decisions(id),
                ticker TEXT NOT NULL,
                instrument_type TEXT NOT NULL,
                action TEXT NOT NULL,
                quantity REAL NOT NULL,
                price REAL NOT NULL,
                option_details TEXT,
                status TEXT NOT NULL,
                pnl REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id INTEGER REFERENCES decisions(id),
                ticker TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                report_type TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS backtest_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tickers TEXT NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                config TEXT NOT NULL,
                metrics TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS equity_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                backtest_run_id INTEGER REFERENCES backtest_runs(id),
                date TEXT NOT NULL,
                portfolio_value REAL NOT NULL,
                cash REAL NOT NULL,
                positions_value REAL NOT NULL
            );
        """)
        self.conn.commit()

    def insert_decision(self, ticker: str, trade_date: str, rating: str,
                        full_decision: str, options_report: str) -> int:
        cursor = self.conn.execute(
            "INSERT INTO decisions (ticker, trade_date, rating, full_decision, options_report) VALUES (?, ?, ?, ?, ?)",
            (ticker, trade_date, rating, full_decision, options_report),
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_decision(self, decision_id: int) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM decisions WHERE id = ?", (decision_id,)
        ).fetchone()
        return dict(row) if row else None

    def insert_trade(self, decision_id: int, ticker: str, instrument_type: str,
                     action: str, quantity: float, price: float,
                     option_details: Optional[str], status: str,
                     pnl: Optional[float]) -> int:
        cursor = self.conn.execute(
            "INSERT INTO trades (decision_id, ticker, instrument_type, action, quantity, price, option_details, status, pnl) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (decision_id, ticker, instrument_type, action, quantity, price,
             json.dumps(option_details) if option_details and not isinstance(option_details, str) else option_details,
             status, pnl),
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_trades_for_ticker(self, ticker: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM trades WHERE ticker = ? ORDER BY created_at DESC", (ticker,)
        ).fetchall()
        return [dict(r) for r in rows]

    def insert_report(self, decision_id: int, ticker: str, trade_date: str,
                      report_type: str, content: str) -> int:
        cursor = self.conn.execute(
            "INSERT INTO reports (decision_id, ticker, trade_date, report_type, content) VALUES (?, ?, ?, ?, ?)",
            (decision_id, ticker, trade_date, report_type, content),
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_reports_for_decision(self, decision_id: int) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM reports WHERE decision_id = ? ORDER BY report_type", (decision_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def insert_backtest_run(self, tickers: List[str], start_date: str,
                            end_date: str, config: dict, metrics: dict) -> int:
        cursor = self.conn.execute(
            "INSERT INTO backtest_runs (tickers, start_date, end_date, config, metrics) VALUES (?, ?, ?, ?, ?)",
            (json.dumps(tickers), start_date, end_date, json.dumps(config), json.dumps(metrics)),
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_backtest_run(self, run_id: int) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM backtest_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["tickers"] = json.loads(d["tickers"])
        d["config"] = json.loads(d["config"])
        d["metrics"] = json.loads(d["metrics"])
        return d

    def insert_equity_snapshot(self, backtest_run_id: Optional[int], date: str,
                               portfolio_value: float, cash: float,
                               positions_value: float) -> int:
        cursor = self.conn.execute(
            "INSERT INTO equity_snapshots (backtest_run_id, date, portfolio_value, cash, positions_value) VALUES (?, ?, ?, ?, ?)",
            (backtest_run_id, date, portfolio_value, cash, positions_value),
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_equity_curve(self, backtest_run_id: int) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM equity_snapshots WHERE backtest_run_id = ? ORDER BY date",
            (backtest_run_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_all_trades(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM trades ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_latest_decisions(self, limit: int = 10) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM decisions ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self):
        self.conn.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_storage.py -v`
Expected: All 9 tests PASS

- [ ] **Step 5: Commit**

```bash
git add tradingagents/storage/__init__.py tradingagents/storage/db.py tests/test_storage.py
git commit -m "feat: add SQLite storage layer for decisions, trades, reports, backtests"
```

### Task 2: Storage Queries Helper

**Files:**
- Create: `tradingagents/storage/queries.py`
- Modify: `tests/test_storage.py`

- [ ] **Step 1: Write failing tests for query helpers**

Append to `tests/test_storage.py`:

```python
from tradingagents.storage.queries import (
    get_portfolio_summary,
    get_recent_signals,
    get_trade_history,
)


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_storage.py::TestQueries -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement query helpers**

```python
# tradingagents/storage/queries.py
from typing import Any, Dict, List, Optional

from .db import Database


def get_portfolio_summary(db: Database) -> Dict[str, Any]:
    row = db.conn.execute(
        "SELECT COUNT(*) as total_trades, COALESCE(SUM(pnl), 0.0) as total_pnl FROM trades"
    ).fetchone()
    return {"total_trades": row["total_trades"], "total_pnl": row["total_pnl"]}


def get_recent_signals(db: Database, limit: int = 10) -> List[Dict[str, Any]]:
    rows = db.conn.execute(
        "SELECT * FROM decisions ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_trade_history(
    db: Database, ticker: Optional[str] = None, limit: int = 100
) -> List[Dict[str, Any]]:
    if ticker:
        rows = db.conn.execute(
            "SELECT * FROM trades WHERE ticker = ? ORDER BY created_at DESC LIMIT ?",
            (ticker, limit),
        ).fetchall()
    else:
        rows = db.conn.execute(
            "SELECT * FROM trades ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_storage.py -v`
Expected: All 13 tests PASS

- [ ] **Step 5: Commit**

```bash
git add tradingagents/storage/queries.py tests/test_storage.py
git commit -m "feat: add storage query helpers for portfolio summary, signals, trade history"
```

---

## Phase 2: Options Analyst

### Task 3: Options Data Layer

**Files:**
- Create: `tradingagents/dataflows/options_data.py`
- Test: `tests/test_options_data.py`

- [ ] **Step 1: Write failing tests for options data functions**

```python
# tests/test_options_data.py
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime

from tradingagents.dataflows.options_data import (
    get_options_chain,
    get_options_greeks,
    get_put_call_ratio,
)


class TestGetOptionsChain:
    @patch("tradingagents.dataflows.options_data.yf.Ticker")
    def test_returns_formatted_chain(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker.options = ("2026-04-18", "2026-05-16", "2026-06-20")

        import pandas as pd
        calls_df = pd.DataFrame({
            "strike": [70.0, 75.0, 80.0],
            "lastPrice": [5.0, 2.5, 1.0],
            "bid": [4.8, 2.3, 0.9],
            "ask": [5.2, 2.7, 1.1],
            "volume": [1000, 2000, 500],
            "openInterest": [5000, 8000, 3000],
            "impliedVolatility": [0.35, 0.32, 0.30],
        })
        puts_df = pd.DataFrame({
            "strike": [70.0, 75.0, 80.0],
            "lastPrice": [1.0, 2.5, 5.0],
            "bid": [0.9, 2.3, 4.8],
            "ask": [1.1, 2.7, 5.2],
            "volume": [800, 1500, 600],
            "openInterest": [4000, 7000, 2000],
            "impliedVolatility": [0.33, 0.31, 0.29],
        })
        mock_chain = MagicMock()
        mock_chain.calls = calls_df
        mock_chain.puts = puts_df
        mock_ticker.option_chain.return_value = mock_chain
        mock_ticker_cls.return_value = mock_ticker

        result = get_options_chain("SOFI", "2026-04-01")
        assert "strike" in result.lower() or "Strike" in result
        assert "70.0" in result or "70" in result
        assert "call" in result.lower() or "Call" in result

    @patch("tradingagents.dataflows.options_data.yf.Ticker")
    def test_handles_no_options(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker.options = ()
        mock_ticker_cls.return_value = mock_ticker

        result = get_options_chain("FAKE", "2026-04-01")
        assert "no options" in result.lower()


class TestGetOptionsGreeks:
    @patch("tradingagents.dataflows.options_data.yf.Ticker")
    def test_returns_greeks(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker.info = {"regularMarketPrice": 75.0}
        mock_ticker_cls.return_value = mock_ticker

        result = get_options_greeks("SOFI", "2026-06-20", 80.0, "call")
        # Should return a string containing greek names
        assert "delta" in result.lower()
        assert "theta" in result.lower()

    @patch("tradingagents.dataflows.options_data.yf.Ticker")
    def test_handles_missing_price(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker.info = {}
        mock_ticker_cls.return_value = mock_ticker

        result = get_options_greeks("FAKE", "2026-06-20", 80.0, "call")
        assert "error" in result.lower() or "unavailable" in result.lower()


class TestGetPutCallRatio:
    @patch("tradingagents.dataflows.options_data.yf.Ticker")
    def test_returns_ratio(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker.options = ("2026-04-18",)

        import pandas as pd
        calls_df = pd.DataFrame({"openInterest": [5000, 8000]})
        puts_df = pd.DataFrame({"openInterest": [4000, 7000]})
        mock_chain = MagicMock()
        mock_chain.calls = calls_df
        mock_chain.puts = puts_df
        mock_ticker.option_chain.return_value = mock_chain
        mock_ticker_cls.return_value = mock_ticker

        result = get_put_call_ratio("SOFI")
        assert "put/call" in result.lower() or "ratio" in result.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_options_data.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement options data functions**

```python
# tradingagents/dataflows/options_data.py
import math
from datetime import datetime, timedelta

import yfinance as yf


def _days_to_expiry(expiry_str: str, curr_date: str) -> float:
    expiry = datetime.strptime(expiry_str, "%Y-%m-%d")
    current = datetime.strptime(curr_date, "%Y-%m-%d")
    return max((expiry - current).days, 1) / 365.0


def get_options_chain(symbol: str, curr_date: str) -> str:
    ticker = yf.Ticker(symbol)
    expirations = ticker.options

    if not expirations:
        return f"No options data available for {symbol}."

    curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    max_date = curr_dt + timedelta(days=180)

    filtered_exps = [
        e for e in expirations
        if datetime.strptime(e, "%Y-%m-%d") <= max_date
    ]
    if not filtered_exps:
        filtered_exps = expirations[:3]

    sections = []
    for exp in filtered_exps[:3]:  # limit to 3 nearest expirations
        chain = ticker.option_chain(exp)

        calls = chain.calls[["strike", "lastPrice", "bid", "ask", "volume", "openInterest", "impliedVolatility"]].head(10)
        puts = chain.puts[["strike", "lastPrice", "bid", "ask", "volume", "openInterest", "impliedVolatility"]].head(10)

        sections.append(
            f"## Expiration: {exp}\n\n"
            f"### Calls\n{calls.to_string(index=False)}\n\n"
            f"### Puts\n{puts.to_string(index=False)}\n"
        )

    return f"# Options Chain for {symbol}\n\n" + "\n".join(sections)


def get_options_greeks(symbol: str, expiration: str, strike: float,
                       option_type: str) -> str:
    ticker = yf.Ticker(symbol)
    price_info = ticker.info
    spot = price_info.get("regularMarketPrice") or price_info.get("currentPrice")

    if not spot:
        return f"Error: Unable to retrieve current price for {symbol}. Greeks unavailable."

    try:
        from py_vollib.black_scholes.greeks.analytical import delta, gamma, theta, vega
        from py_vollib.black_scholes.implied_volatility import implied_volatility
        from py_vollib.black_scholes import black_scholes
    except ImportError:
        return _estimate_greeks_simple(spot, strike, expiration, option_type)

    t = _days_to_expiry(expiration, datetime.now().strftime("%Y-%m-%d"))
    r = 0.045  # approximate risk-free rate
    flag = "c" if option_type.lower() == "call" else "p"

    # Estimate IV from ATM approximation
    sigma = 0.30  # default IV estimate
    try:
        d = delta(flag, spot, strike, t, r, sigma)
        g = gamma(flag, spot, strike, t, r, sigma)
        th = theta(flag, spot, strike, t, r, sigma)
        v = vega(flag, spot, strike, t, r, sigma)
    except Exception:
        return _estimate_greeks_simple(spot, strike, expiration, option_type)

    return (
        f"# Greeks for {symbol} {expiration} {strike} {option_type.upper()}\n\n"
        f"| Greek | Value |\n|-------|-------|\n"
        f"| Delta | {d:.4f} |\n"
        f"| Gamma | {g:.6f} |\n"
        f"| Theta | {th:.4f} |\n"
        f"| Vega  | {v:.4f} |\n"
        f"| IV (est.) | {sigma:.2%} |\n\n"
        f"Spot: ${spot:.2f}, Strike: ${strike:.2f}, DTE: {int(t*365)} days"
    )


def _estimate_greeks_simple(spot: float, strike: float, expiration: str,
                            option_type: str) -> str:
    """Fallback Greeks estimation without py_vollib."""
    t = _days_to_expiry(expiration, datetime.now().strftime("%Y-%m-%d"))
    moneyness = spot / strike

    if option_type.lower() == "call":
        delta_est = max(0.0, min(1.0, 0.5 + (moneyness - 1.0) * 2.5))
    else:
        delta_est = max(-1.0, min(0.0, -0.5 + (moneyness - 1.0) * 2.5))

    return (
        f"# Estimated Greeks for {spot:.2f}/{strike:.2f} {option_type.upper()}\n\n"
        f"| Greek | Estimate |\n|-------|----------|\n"
        f"| Delta | {delta_est:.2f} |\n"
        f"| DTE   | {int(t*365)} days |\n\n"
        f"Note: Install py_vollib for precise Greeks calculation."
    )


def get_put_call_ratio(symbol: str) -> str:
    ticker = yf.Ticker(symbol)
    expirations = ticker.options

    if not expirations:
        return f"No options data available for {symbol}."

    total_call_oi = 0
    total_put_oi = 0

    for exp in expirations[:3]:
        chain = ticker.option_chain(exp)
        total_call_oi += chain.calls["openInterest"].sum()
        total_put_oi += chain.puts["openInterest"].sum()

    if total_call_oi == 0:
        return f"Put/Call ratio unavailable for {symbol}: no call open interest."

    ratio = total_put_oi / total_call_oi

    if ratio > 1.2:
        sentiment = "Bearish (high put buying relative to calls)"
    elif ratio < 0.8:
        sentiment = "Bullish (high call buying relative to puts)"
    else:
        sentiment = "Neutral"

    return (
        f"# Put/Call Ratio for {symbol}\n\n"
        f"| Metric | Value |\n|--------|-------|\n"
        f"| Total Put OI | {total_put_oi:,} |\n"
        f"| Total Call OI | {total_call_oi:,} |\n"
        f"| Put/Call Ratio | {ratio:.2f} |\n"
        f"| Sentiment | {sentiment} |\n"
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_options_data.py -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add tradingagents/dataflows/options_data.py tests/test_options_data.py
git commit -m "feat: add options data layer with chain, greeks, and put/call ratio"
```

### Task 4: Options Tool Definitions

**Files:**
- Create: `tradingagents/agents/utils/options_tools.py`
- Modify: `tradingagents/dataflows/interface.py`
- Test: `tests/test_options_data.py` (append)

- [ ] **Step 1: Write failing test for tool integration**

Append to `tests/test_options_data.py`:

```python
from tradingagents.agents.utils.options_tools import (
    get_options_chain_tool,
    get_options_greeks_tool,
    get_put_call_ratio_tool,
)


class TestOptionsTools:
    def test_tools_have_correct_names(self):
        assert get_options_chain_tool.name == "get_options_chain"
        assert get_options_greeks_tool.name == "get_options_greeks"
        assert get_put_call_ratio_tool.name == "get_put_call_ratio"

    def test_tools_have_descriptions(self):
        assert len(get_options_chain_tool.description) > 0
        assert len(get_options_greeks_tool.description) > 0
        assert len(get_put_call_ratio_tool.description) > 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_options_data.py::TestOptionsTools -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement options tools**

```python
# tradingagents/agents/utils/options_tools.py
from typing import Annotated
from langchain_core.tools import tool

from tradingagents.dataflows.options_data import (
    get_options_chain as _get_chain,
    get_options_greeks as _get_greeks,
    get_put_call_ratio as _get_pcr,
)


@tool
def get_options_chain_tool(
    symbol: Annotated[str, "Ticker symbol of the company, e.g. SOFI, NVDA"],
    curr_date: Annotated[str, "Current trading date in YYYY-MM-DD format"],
) -> str:
    """Retrieve options chain data for a given ticker symbol.
    Returns available expirations, strikes, bid/ask, volume, open interest,
    and implied volatility for calls and puts within 6 months.
    """
    return _get_chain(symbol, curr_date)


@tool
def get_options_greeks_tool(
    symbol: Annotated[str, "Ticker symbol of the company"],
    expiration: Annotated[str, "Option expiration date in YYYY-MM-DD format"],
    strike: Annotated[float, "Strike price of the option"],
    option_type: Annotated[str, "Option type: 'call' or 'put'"],
) -> str:
    """Calculate options Greeks (delta, gamma, theta, vega) for a specific contract.
    Uses Black-Scholes model to compute theoretical Greeks values.
    """
    return _get_greeks(symbol, expiration, strike, option_type)


@tool
def get_put_call_ratio_tool(
    symbol: Annotated[str, "Ticker symbol of the company"],
) -> str:
    """Retrieve the put/call ratio for a given ticker symbol.
    Calculates total put open interest divided by total call open interest
    across the nearest 3 expirations. Provides sentiment interpretation.
    """
    return _get_pcr(symbol)
```

- [ ] **Step 4: Register options tools in interface.py**

Add to `tradingagents/dataflows/interface.py`:

In `TOOLS_CATEGORIES`, add after the `"news_data"` entry:

```python
    "options_data": {
        "description": "Options chain and analytics",
        "tools": [
            "get_options_chain",
            "get_options_greeks",
            "get_put_call_ratio",
        ]
    },
```

In `VENDOR_METHODS`, add after the `"get_insider_transactions"` entry:

```python
    "get_options_chain": {
        "yfinance": get_yfinance_options_chain,
    },
    "get_options_greeks": {
        "yfinance": get_yfinance_options_greeks,
    },
    "get_put_call_ratio": {
        "yfinance": get_yfinance_put_call_ratio,
    },
```

Add import at top of `interface.py`:

```python
from .options_data import (
    get_options_chain as get_yfinance_options_chain,
    get_options_greeks as get_yfinance_options_greeks,
    get_put_call_ratio as get_yfinance_put_call_ratio,
)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_options_data.py -v`
Expected: All 7 tests PASS

- [ ] **Step 6: Commit**

```bash
git add tradingagents/agents/utils/options_tools.py tradingagents/dataflows/interface.py tests/test_options_data.py
git commit -m "feat: add LangChain options tools and register in data routing layer"
```

### Task 5: Options Analyst Agent

**Files:**
- Create: `tradingagents/agents/analysts/options_analyst.py`
- Modify: `tradingagents/agents/utils/agent_states.py`
- Modify: `tradingagents/agents/__init__.py`
- Test: `tests/test_options_analyst.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_options_analyst.py
import pytest
from unittest.mock import MagicMock, patch
from langchain_core.messages import AIMessage

from tradingagents.agents.analysts.options_analyst import create_options_analyst
from tradingagents.agents.utils.agent_states import AgentState


class TestOptionsAnalyst:
    def test_create_options_analyst_returns_callable(self):
        mock_llm = MagicMock()
        node = create_options_analyst(mock_llm)
        assert callable(node)

    def test_options_analyst_returns_options_report_key(self):
        mock_llm = MagicMock()
        mock_response = AIMessage(content="Options report here", tool_calls=[])
        mock_chain = MagicMock()
        mock_chain.invoke.return_value = mock_response
        mock_llm.bind_tools.return_value = mock_chain

        # Patch the prompt pipe
        with patch("tradingagents.agents.analysts.options_analyst.ChatPromptTemplate") as mock_prompt_cls:
            mock_prompt = MagicMock()
            mock_prompt.partial.return_value = mock_prompt
            mock_prompt.__or__ = MagicMock(return_value=mock_chain)
            mock_prompt_cls.from_messages.return_value = mock_prompt

            node = create_options_analyst(mock_llm)
            state = {
                "trade_date": "2026-03-28",
                "company_of_interest": "SOFI",
                "messages": [],
            }
            result = node(state)

        assert "options_report" in result
        assert "messages" in result

    def test_agent_state_has_options_report_field(self):
        # Verify the field exists in AgentState annotations
        annotations = AgentState.__annotations__
        assert "options_report" in annotations
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_options_analyst.py -v`
Expected: FAIL — `ModuleNotFoundError` and missing `options_report` field

- [ ] **Step 3: Add options_report to AgentState**

In `tradingagents/agents/utils/agent_states.py`, add after the `fundamentals_report` field:

```python
    options_report: Annotated[str, "Report from the Options Analyst"]
```

- [ ] **Step 4: Implement options analyst**

```python
# tradingagents/agents/analysts/options_analyst.py
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.utils.agent_utils import build_instrument_context
from tradingagents.agents.utils.options_tools import (
    get_options_chain_tool,
    get_options_greeks_tool,
    get_put_call_ratio_tool,
)


def create_options_analyst(llm):
    def options_analyst_node(state):
        current_date = state["trade_date"]
        instrument_context = build_instrument_context(state["company_of_interest"])

        tools = [
            get_options_chain_tool,
            get_options_greeks_tool,
            get_put_call_ratio_tool,
        ]

        system_message = """You are an options analyst specializing in evaluating derivatives markets for trading opportunities. Your audience is a beginner to options trading with a small account (~$5,000).

Your analysis process:
1. First, call get_options_chain to see available contracts, volumes, and implied volatility
2. Call get_put_call_ratio to gauge market sentiment from options flow
3. For promising strikes, call get_options_greeks to evaluate risk/reward

Your report MUST include:

## Implied Volatility Analysis
- Current IV vs historical context (is it high, low, or average?)
- IV skew observations (are puts more expensive than calls?)

## Options Flow & Sentiment
- Put/call ratio interpretation
- Notable unusual volume or open interest concentrations

## Strategy Recommendations
Recommend 1-3 options strategies. For EACH strategy include:
- **Strategy name** and brief explanation of how it works
- **Specific contracts**: exact strikes and expirations
- **Max risk**: dollar amount at stake (MUST be under 5% of $5,000 = $250 per trade)
- **Max reward**: dollar amount or "unlimited" for long calls/puts
- **Breakeven**: price(s) where the trade breaks even
- **Why this strategy fits**: connect to the current market conditions

CONSTRAINTS:
- Only recommend DEFINED-RISK strategies (no naked short calls or puts)
- Allowed strategies: long calls, long puts, vertical spreads (bull call, bear put), straddles, strangles
- Explain each strategy in beginner-friendly language
- If IV is elevated, warn that options may be overpriced
- Always include a Markdown summary table at the end

Make sure to append a Markdown table at the end organizing key recommendations."""

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " Use the provided tools to progress towards answering the question."
                    " If you are unable to fully answer, that's OK; another assistant with different tools"
                    " will help where you left off. Execute what you can to make progress."
                    " You have access to the following tools: {tool_names}.\n{system_message}"
                    "For your reference, the current date is {current_date}. {instrument_context}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(tool_names=", ".join([t.name for t in tools]))
        prompt = prompt.partial(current_date=current_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        chain = prompt | llm.bind_tools(tools)
        result = chain.invoke(state["messages"])

        report = ""
        if len(result.tool_calls) == 0:
            report = result.content

        return {
            "messages": [result],
            "options_report": report,
        }

    return options_analyst_node
```

- [ ] **Step 5: Add export to agents/__init__.py**

Add import:
```python
from .analysts.options_analyst import create_options_analyst
```

Add to `__all__`:
```python
    "create_options_analyst",
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_options_analyst.py -v`
Expected: All 3 tests PASS

- [ ] **Step 7: Commit**

```bash
git add tradingagents/agents/analysts/options_analyst.py tradingagents/agents/utils/agent_states.py tradingagents/agents/__init__.py tests/test_options_analyst.py
git commit -m "feat: add options analyst agent with chain, greeks, and strategy recommendations"
```

### Task 6: Integrate Options Analyst into Graph

**Files:**
- Modify: `tradingagents/graph/setup.py`
- Modify: `tradingagents/graph/trading_graph.py`
- Modify: `tradingagents/graph/conditional_logic.py`
- Modify: `tradingagents/default_config.py`
- Test: `tests/test_options_analyst.py` (append)

- [ ] **Step 1: Write failing integration test**

Append to `tests/test_options_analyst.py`:

```python
from tradingagents.default_config import DEFAULT_CONFIG


class TestOptionsConfig:
    def test_default_config_has_options_section(self):
        assert "options" in DEFAULT_CONFIG
        assert DEFAULT_CONFIG["options"]["enabled"] is True
        assert "max_risk_per_trade_pct" in DEFAULT_CONFIG["options"]

    def test_options_in_selected_analysts_default(self):
        # Options should be available as a selectable analyst
        from tradingagents.graph.conditional_logic import ConditionalLogic
        cl = ConditionalLogic(max_debate_rounds=1, max_risk_discuss_rounds=1)
        assert hasattr(cl, "should_continue_options")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_options_analyst.py::TestOptionsConfig -v`
Expected: FAIL

- [ ] **Step 3: Add options config to default_config.py**

Add to `DEFAULT_CONFIG` dict in `tradingagents/default_config.py`, after the `"tool_vendors"` entry:

```python
    # Options analysis configuration
    "options": {
        "enabled": True,
        "max_expiry_months": 6,
        "max_risk_per_trade_pct": 0.05,
        "strategies_allowed": [
            "long_call", "long_put", "vertical_spread", "straddle", "strangle"
        ],
    },
```

- [ ] **Step 4: Add should_continue_options to conditional_logic.py**

Read `tradingagents/graph/conditional_logic.py` to understand the pattern, then add a method matching the existing pattern (e.g., `should_continue_market`):

```python
    def should_continue_options(self, state):
        messages = state["messages"]
        last_message = messages[-1]
        if last_message.tool_calls:
            return "tools_options"
        return "Msg Clear Options"
```

- [ ] **Step 5: Add options analyst to graph setup.py**

In `setup_graph()` method, add the options analyst block alongside the other analysts. After the `if "fundamentals" in selected_analysts:` block, add:

```python
        if "options" in selected_analysts:
            from tradingagents.agents.analysts.options_analyst import create_options_analyst
            analyst_nodes["options"] = create_options_analyst(
                self.quick_thinking_llm
            )
            delete_nodes["options"] = create_msg_delete()
            tool_nodes["options"] = self.tool_nodes["options"]
```

- [ ] **Step 6: Add options tool node in trading_graph.py**

In `_create_tool_nodes()`, add after the `"fundamentals"` entry:

```python
            "options": ToolNode(
                [
                    get_options_chain_tool,
                    get_options_greeks_tool,
                    get_put_call_ratio_tool,
                ]
            ),
```

Add imports at the top of `trading_graph.py`:

```python
from tradingagents.agents.utils.options_tools import (
    get_options_chain_tool,
    get_options_greeks_tool,
    get_put_call_ratio_tool,
)
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_options_analyst.py -v`
Expected: All 5 tests PASS

- [ ] **Step 8: Commit**

```bash
git add tradingagents/graph/setup.py tradingagents/graph/trading_graph.py tradingagents/graph/conditional_logic.py tradingagents/default_config.py tests/test_options_analyst.py
git commit -m "feat: integrate options analyst into LangGraph pipeline"
```

### Task 7: Feed Options Report to Downstream Agents

**Files:**
- Modify: `tradingagents/agents/researchers/bull_researcher.py`
- Modify: `tradingagents/agents/researchers/bear_researcher.py`
- Modify: `tradingagents/agents/trader/trader.py`
- Modify: `tradingagents/agents/managers/portfolio_manager.py`

- [ ] **Step 1: Read all four files to understand current prompt structure**

Read each file and identify where the other analyst reports (market_report, sentiment_report, news_report, fundamentals_report) are injected into the prompt.

- [ ] **Step 2: Add options_report to bull_researcher.py**

Find where the other reports are included in the prompt and add:

```python
options_report = state.get("options_report", "")
```

And include in the prompt context alongside other reports:

```python
f"Options Analysis Report:\n{options_report}\n\n"
```

- [ ] **Step 3: Add options_report to bear_researcher.py**

Same pattern as bull_researcher — add `options_report` to the prompt context.

- [ ] **Step 4: Add options_report to trader.py**

Same pattern — include options analysis in the trader's prompt so it can propose options-based trades.

- [ ] **Step 5: Add options_report to portfolio_manager.py**

Same pattern — the PM needs to see options recommendations to make a final decision that can include options actions.

- [ ] **Step 6: Run full test suite**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 7: Commit**

```bash
git add tradingagents/agents/researchers/bull_researcher.py tradingagents/agents/researchers/bear_researcher.py tradingagents/agents/trader/trader.py tradingagents/agents/managers/portfolio_manager.py
git commit -m "feat: feed options report to researchers, trader, and portfolio manager"
```

---

## Phase 3: Backtesting Engine

### Task 8: Portfolio Tracker

**Files:**
- Create: `tradingagents/backtesting/__init__.py`
- Create: `tradingagents/backtesting/portfolio.py`
- Test: `tests/test_portfolio.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_portfolio.py
import pytest
from tradingagents.backtesting.portfolio import Portfolio, Order, Position


class TestPortfolio:
    def test_initial_state(self):
        p = Portfolio(initial_capital=5000.0)
        assert p.cash == 5000.0
        assert p.get_total_value({}) == 5000.0
        assert len(p.positions) == 0

    def test_buy_stock(self):
        p = Portfolio(initial_capital=5000.0)
        order = Order(
            ticker="SOFI", action="buy", quantity=100,
            instrument_type="stock", price=10.0,
        )
        p.execute_order(order, fill_price=10.0, date="2026-01-15", slippage_bps=0)
        assert p.cash == 4000.0
        assert len(p.positions) == 1
        assert p.positions["SOFI_stock"].quantity == 100

    def test_sell_stock(self):
        p = Portfolio(initial_capital=5000.0)
        buy = Order(ticker="SOFI", action="buy", quantity=100, instrument_type="stock", price=10.0)
        p.execute_order(buy, fill_price=10.0, date="2026-01-15", slippage_bps=0)

        sell = Order(ticker="SOFI", action="sell", quantity=100, instrument_type="stock", price=12.0)
        p.execute_order(sell, fill_price=12.0, date="2026-02-15", slippage_bps=0)

        assert p.cash == 5200.0
        assert p.positions["SOFI_stock"].quantity == 0

    def test_total_value_with_positions(self):
        p = Portfolio(initial_capital=5000.0)
        buy = Order(ticker="SOFI", action="buy", quantity=100, instrument_type="stock", price=10.0)
        p.execute_order(buy, fill_price=10.0, date="2026-01-15", slippage_bps=0)

        prices = {"SOFI": 12.0}
        assert p.get_total_value(prices) == 4000.0 + 100 * 12.0  # 5200

    def test_slippage_applied(self):
        p = Portfolio(initial_capital=5000.0)
        buy = Order(ticker="SOFI", action="buy", quantity=100, instrument_type="stock", price=10.0)
        p.execute_order(buy, fill_price=10.0, date="2026-01-15", slippage_bps=10)
        # 10 bps slippage on buy means price goes up: 10.0 * 1.001 = 10.01
        assert p.cash == pytest.approx(5000.0 - 100 * 10.01, abs=0.01)

    def test_trade_log(self):
        p = Portfolio(initial_capital=5000.0)
        buy = Order(ticker="SOFI", action="buy", quantity=100, instrument_type="stock", price=10.0)
        p.execute_order(buy, fill_price=10.0, date="2026-01-15", slippage_bps=0)
        assert len(p.trade_log) == 1
        assert p.trade_log[0]["ticker"] == "SOFI"
        assert p.trade_log[0]["action"] == "buy"

    def test_equity_curve(self):
        p = Portfolio(initial_capital=5000.0)
        p.record_snapshot("2026-01-15", {"SOFI": 10.0})
        buy = Order(ticker="SOFI", action="buy", quantity=100, instrument_type="stock", price=10.0)
        p.execute_order(buy, fill_price=10.0, date="2026-01-15", slippage_bps=0)
        p.record_snapshot("2026-01-16", {"SOFI": 11.0})
        curve = p.get_equity_curve()
        assert len(curve) == 2
        assert curve[1]["portfolio_value"] == 4000.0 + 100 * 11.0

    def test_buy_options(self):
        p = Portfolio(initial_capital=5000.0)
        order = Order(
            ticker="SOFI", action="buy", quantity=2,
            instrument_type="option", price=1.20,
            option_details={"strike": 10.0, "expiry": "2026-06-20", "right": "call"},
        )
        # Options: quantity is contracts, each contract = 100 shares
        p.execute_order(order, fill_price=1.20, date="2026-03-01", slippage_bps=0)
        assert p.cash == 5000.0 - 2 * 100 * 1.20  # 4760
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_portfolio.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement Portfolio class**

```python
# tradingagents/backtesting/__init__.py
from .portfolio import Portfolio, Order, Position
from .engine import Backtester

__all__ = ["Portfolio", "Order", "Position", "Backtester"]
```

```python
# tradingagents/backtesting/portfolio.py
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Order:
    ticker: str
    action: str  # "buy" or "sell"
    quantity: float
    instrument_type: str  # "stock" or "option"
    price: float
    option_details: Optional[Dict[str, Any]] = None


@dataclass
class Position:
    ticker: str
    instrument_type: str
    quantity: float
    entry_price: float
    entry_date: str
    option_details: Optional[Dict[str, Any]] = None


class Portfolio:
    def __init__(self, initial_capital: float):
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.positions: Dict[str, Position] = {}
        self.trade_log: List[Dict[str, Any]] = []
        self._equity_snapshots: List[Dict[str, Any]] = []

    def _position_key(self, ticker: str, instrument_type: str,
                      option_details: Optional[Dict] = None) -> str:
        if instrument_type == "option" and option_details:
            return f"{ticker}_{option_details['strike']}_{option_details['expiry']}_{option_details['right']}"
        return f"{ticker}_{instrument_type}"

    def execute_order(self, order: Order, fill_price: float, date: str,
                      slippage_bps: float = 0):
        # Apply slippage
        if order.action == "buy":
            adjusted_price = fill_price * (1 + slippage_bps / 10000)
        else:
            adjusted_price = fill_price * (1 - slippage_bps / 10000)

        multiplier = 100 if order.instrument_type == "option" else 1
        cost = order.quantity * multiplier * adjusted_price

        key = self._position_key(order.ticker, order.instrument_type, order.option_details)

        if order.action == "buy":
            self.cash -= cost
            if key in self.positions:
                pos = self.positions[key]
                total_qty = pos.quantity + order.quantity
                pos.entry_price = (
                    (pos.entry_price * pos.quantity + adjusted_price * order.quantity)
                    / total_qty
                )
                pos.quantity = total_qty
            else:
                self.positions[key] = Position(
                    ticker=order.ticker,
                    instrument_type=order.instrument_type,
                    quantity=order.quantity,
                    entry_price=adjusted_price,
                    entry_date=date,
                    option_details=order.option_details,
                )
        elif order.action == "sell":
            self.cash += cost
            if key in self.positions:
                self.positions[key].quantity -= order.quantity

        self.trade_log.append({
            "date": date,
            "ticker": order.ticker,
            "action": order.action,
            "instrument_type": order.instrument_type,
            "quantity": order.quantity,
            "fill_price": adjusted_price,
            "cost": cost,
            "option_details": order.option_details,
        })

    def get_total_value(self, current_prices: Dict[str, float]) -> float:
        positions_value = 0.0
        for key, pos in self.positions.items():
            if pos.quantity <= 0:
                continue
            price = current_prices.get(pos.ticker, pos.entry_price)
            multiplier = 100 if pos.instrument_type == "option" else 1
            positions_value += pos.quantity * multiplier * price
        return self.cash + positions_value

    def record_snapshot(self, date: str, current_prices: Dict[str, float]):
        total = self.get_total_value(current_prices)
        positions_val = total - self.cash
        self._equity_snapshots.append({
            "date": date,
            "portfolio_value": total,
            "cash": self.cash,
            "positions_value": positions_val,
        })

    def get_equity_curve(self) -> List[Dict[str, Any]]:
        return list(self._equity_snapshots)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_portfolio.py -v`
Expected: All 8 tests PASS

- [ ] **Step 5: Commit**

```bash
git add tradingagents/backtesting/__init__.py tradingagents/backtesting/portfolio.py tests/test_portfolio.py
git commit -m "feat: add portfolio tracker with stock and options position management"
```

### Task 9: Backtest Metrics

**Files:**
- Create: `tradingagents/backtesting/metrics.py`
- Test: `tests/test_metrics.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_metrics.py
import pytest
import pandas as pd
from tradingagents.backtesting.metrics import compute_metrics


class TestComputeMetrics:
    def test_basic_metrics(self):
        equity_curve = pd.DataFrame({
            "date": pd.date_range("2025-09-01", periods=10, freq="W"),
            "portfolio_value": [5000, 5100, 5050, 5200, 5300, 5150, 5400, 5500, 5350, 5600],
        })
        trade_log = [
            {"ticker": "SOFI", "action": "buy", "fill_price": 10.0, "date": "2025-09-01", "pnl": 150.0},
            {"ticker": "SOFI", "action": "sell", "fill_price": 11.5, "date": "2025-09-15", "pnl": 150.0},
            {"ticker": "PLTR", "action": "buy", "fill_price": 20.0, "date": "2025-10-01", "pnl": -50.0},
            {"ticker": "PLTR", "action": "sell", "fill_price": 19.0, "date": "2025-10-15", "pnl": -50.0},
        ]
        m = compute_metrics(equity_curve, trade_log)

        assert "total_return" in m
        assert "max_drawdown" in m
        assert "win_rate" in m
        assert "sharpe_ratio" in m
        assert m["total_return"] == pytest.approx(0.12, abs=0.01)  # 5600/5000 - 1

    def test_empty_trade_log(self):
        equity_curve = pd.DataFrame({
            "date": pd.date_range("2025-09-01", periods=5, freq="W"),
            "portfolio_value": [5000, 5000, 5000, 5000, 5000],
        })
        m = compute_metrics(equity_curve, [])
        assert m["total_trades"] == 0
        assert m["win_rate"] == 0.0

    def test_all_winners(self):
        equity_curve = pd.DataFrame({
            "date": pd.date_range("2025-09-01", periods=3, freq="W"),
            "portfolio_value": [5000, 5200, 5500],
        })
        trade_log = [
            {"ticker": "A", "action": "sell", "fill_price": 12, "date": "2025-09-08", "pnl": 200.0},
            {"ticker": "B", "action": "sell", "fill_price": 15, "date": "2025-09-15", "pnl": 300.0},
        ]
        m = compute_metrics(equity_curve, trade_log)
        assert m["win_rate"] == 1.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_metrics.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement compute_metrics**

```python
# tradingagents/backtesting/metrics.py
from typing import Any, Dict, List

import numpy as np
import pandas as pd


def compute_metrics(equity_curve: pd.DataFrame,
                    trade_log: List[Dict[str, Any]]) -> Dict[str, Any]:
    values = equity_curve["portfolio_value"].values
    initial = values[0]
    final = values[-1]

    total_return = (final / initial) - 1 if initial > 0 else 0.0

    # Drawdown
    running_max = np.maximum.accumulate(values)
    drawdowns = (values - running_max) / running_max
    max_drawdown = float(drawdowns.min()) if len(drawdowns) > 0 else 0.0

    # Returns series
    returns = np.diff(values) / values[:-1] if len(values) > 1 else np.array([])

    # Sharpe ratio (annualized, assuming weekly data)
    if len(returns) > 1 and np.std(returns) > 0:
        sharpe_ratio = float(np.mean(returns) / np.std(returns) * np.sqrt(52))
    else:
        sharpe_ratio = 0.0

    # Sortino ratio
    downside = returns[returns < 0]
    if len(downside) > 0 and np.std(downside) > 0:
        sortino_ratio = float(np.mean(returns) / np.std(downside) * np.sqrt(52))
    else:
        sortino_ratio = 0.0

    # Trade statistics
    pnls = [t.get("pnl", 0) for t in trade_log if t.get("pnl") is not None]
    total_trades = len(pnls)
    winners = [p for p in pnls if p > 0]
    losers = [p for p in pnls if p < 0]

    win_rate = len(winners) / total_trades if total_trades > 0 else 0.0

    gross_profit = sum(winners) if winners else 0.0
    gross_loss = abs(sum(losers)) if losers else 0.0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf") if gross_profit > 0 else 0.0

    return {
        "total_return": round(total_return, 4),
        "annualized_return": round(total_return * (252 / max(len(values), 1)), 4),
        "sharpe_ratio": round(sharpe_ratio, 4),
        "sortino_ratio": round(sortino_ratio, 4),
        "max_drawdown": round(max_drawdown, 4),
        "total_trades": total_trades,
        "win_rate": round(win_rate, 4),
        "profit_factor": round(profit_factor, 4),
        "avg_pnl": round(np.mean(pnls), 2) if pnls else 0.0,
        "avg_winner": round(np.mean(winners), 2) if winners else 0.0,
        "avg_loser": round(np.mean(losers), 2) if losers else 0.0,
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_metrics.py -v`
Expected: All 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add tradingagents/backtesting/metrics.py tests/test_metrics.py
git commit -m "feat: add backtest metrics computation (sharpe, drawdown, win rate, etc.)"
```

### Task 10: Backtest Engine

**Files:**
- Create: `tradingagents/backtesting/engine.py`
- Create: `tradingagents/backtesting/report.py`
- Test: `tests/test_backtesting.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_backtesting.py
import pytest
from unittest.mock import MagicMock, patch
from tradingagents.backtesting.engine import Backtester
from tradingagents.default_config import DEFAULT_CONFIG


class TestBacktester:
    def _make_config(self):
        config = DEFAULT_CONFIG.copy()
        config["backtest"] = {
            "initial_capital": 5000,
            "max_position_pct": 0.35,
            "max_options_risk_pct": 0.05,
            "slippage_bps": 10,
            "commission_per_trade": 0,
            "trading_frequency": "weekly",
            "accuracy_windows": [5, 10, 30],
        }
        return config

    @patch("tradingagents.backtesting.engine.TradingAgentsGraph")
    @patch("tradingagents.backtesting.engine.yf.download")
    def test_backtest_runs_and_returns_result(self, mock_yf_download, mock_graph_cls):
        import pandas as pd
        import numpy as np

        # Mock yfinance data
        dates = pd.bdate_range("2025-09-01", "2025-09-30")
        mock_yf_download.return_value = pd.DataFrame({
            "Open": np.random.uniform(10, 12, len(dates)),
            "Close": np.random.uniform(10, 12, len(dates)),
        }, index=dates)

        # Mock graph
        mock_graph = MagicMock()
        mock_graph.propagate.return_value = (
            {"final_trade_decision": "Rating: BUY\nBuy SOFI"},
            "BUY",
        )
        mock_graph_cls.return_value = mock_graph

        config = self._make_config()
        bt = Backtester(config=config)
        result = bt.run(
            tickers=["SOFI"],
            start_date="2025-09-01",
            end_date="2025-09-30",
        )

        assert result is not None
        assert "metrics" in result
        assert "trade_log" in result
        assert "equity_curve" in result

    @patch("tradingagents.backtesting.engine.TradingAgentsGraph")
    @patch("tradingagents.backtesting.engine.yf.download")
    def test_backtest_respects_weekly_frequency(self, mock_yf_download, mock_graph_cls):
        import pandas as pd
        import numpy as np

        dates = pd.bdate_range("2025-09-01", "2025-09-30")
        mock_yf_download.return_value = pd.DataFrame({
            "Open": np.full(len(dates), 10.0),
            "Close": np.full(len(dates), 10.0),
        }, index=dates)

        mock_graph = MagicMock()
        mock_graph.propagate.return_value = (
            {"final_trade_decision": "Rating: HOLD"},
            "HOLD",
        )
        mock_graph_cls.return_value = mock_graph

        config = self._make_config()
        bt = Backtester(config=config)
        bt.run(tickers=["SOFI"], start_date="2025-09-01", end_date="2025-09-30")

        # Weekly frequency means ~4 calls for September (one per week)
        call_count = mock_graph.propagate.call_count
        assert 3 <= call_count <= 5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_backtesting.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement Backtester**

```python
# tradingagents/backtesting/engine.py
from datetime import datetime, timedelta
from typing import Any, Dict, List

import pandas as pd
import yfinance as yf

from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.backtesting.portfolio import Portfolio, Order
from tradingagents.backtesting.metrics import compute_metrics


class Backtester:
    def __init__(self, config: dict):
        self.config = config
        self.bt_config = config.get("backtest", {})

    def _get_trading_dates(self, start_date: str, end_date: str,
                           frequency: str) -> List[str]:
        all_dates = pd.bdate_range(start_date, end_date)
        if frequency == "daily":
            return [d.strftime("%Y-%m-%d") for d in all_dates]
        elif frequency == "weekly":
            # Pick every Monday (or first business day of each week)
            weekly = all_dates[all_dates.weekday == 0]
            if len(weekly) == 0:
                weekly = all_dates[::5]
            return [d.strftime("%Y-%m-%d") for d in weekly]
        return [d.strftime("%Y-%m-%d") for d in all_dates]

    def _fetch_price_data(self, tickers: List[str], start_date: str,
                          end_date: str) -> Dict[str, pd.DataFrame]:
        price_data = {}
        for ticker in tickers:
            data = yf.download(ticker, start=start_date, end=end_date,
                               progress=False)
            if not data.empty:
                price_data[ticker] = data
        return price_data

    def _get_price_on_date(self, price_data: Dict[str, pd.DataFrame],
                           ticker: str, date: str, column: str = "Open") -> float:
        if ticker not in price_data:
            return 0.0
        df = price_data[ticker]
        target = pd.Timestamp(date)
        # Find nearest date on or after target
        mask = df.index >= target
        if mask.any():
            return float(df.loc[mask].iloc[0][column])
        return float(df.iloc[-1][column])

    def _decision_to_order(self, decision: str, ticker: str,
                           price: float, portfolio_value: float) -> Order:
        max_position = portfolio_value * self.bt_config.get("max_position_pct", 0.35)
        qty = int(max_position / price) if price > 0 else 0

        if decision in ("BUY", "OVERWEIGHT"):
            return Order(ticker=ticker, action="buy", quantity=qty,
                         instrument_type="stock", price=price)
        elif decision in ("SELL", "UNDERWEIGHT"):
            return Order(ticker=ticker, action="sell", quantity=qty,
                         instrument_type="stock", price=price)
        return None

    def run(self, tickers: List[str], start_date: str,
            end_date: str) -> Dict[str, Any]:
        frequency = self.bt_config.get("trading_frequency", "weekly")
        initial_capital = self.bt_config.get("initial_capital", 5000)
        slippage = self.bt_config.get("slippage_bps", 10)

        trading_dates = self._get_trading_dates(start_date, end_date, frequency)
        price_data = self._fetch_price_data(tickers, start_date, end_date)

        portfolio = Portfolio(initial_capital=initial_capital)
        ta = TradingAgentsGraph(debug=False, config=self.config)

        decisions_log = []

        for date in trading_dates:
            current_prices = {
                t: self._get_price_on_date(price_data, t, date, "Close")
                for t in tickers
            }
            portfolio.record_snapshot(date, current_prices)

            for ticker in tickers:
                try:
                    state, decision = ta.propagate(ticker, date)
                except Exception:
                    continue

                price = self._get_price_on_date(price_data, ticker, date, "Open")
                if price <= 0:
                    continue

                total_value = portfolio.get_total_value(current_prices)
                order = self._decision_to_order(decision, ticker, price, total_value)

                if order and order.quantity > 0:
                    # Check if we already have a position and the signal is sell
                    key = portfolio._position_key(ticker, "stock")
                    if order.action == "sell" and key in portfolio.positions:
                        order.quantity = min(order.quantity,
                                             int(portfolio.positions[key].quantity))
                        if order.quantity <= 0:
                            continue
                    elif order.action == "sell":
                        continue  # can't sell what we don't have

                    portfolio.execute_order(order, fill_price=price,
                                            date=date, slippage_bps=slippage)

                decisions_log.append({
                    "date": date,
                    "ticker": ticker,
                    "decision": decision,
                    "price": price,
                })

        # Final snapshot
        final_prices = {
            t: self._get_price_on_date(price_data, t, end_date, "Close")
            for t in tickers
        }
        portfolio.record_snapshot(end_date, final_prices)

        equity_curve = pd.DataFrame(portfolio.get_equity_curve())
        metrics = compute_metrics(equity_curve, portfolio.trade_log)

        return {
            "metrics": metrics,
            "trade_log": portfolio.trade_log,
            "equity_curve": equity_curve,
            "decisions_log": decisions_log,
        }
```

```python
# tradingagents/backtesting/report.py
import json
from pathlib import Path
from typing import Any, Dict

import pandas as pd


def generate_backtest_report(result: Dict[str, Any], output_dir: str) -> str:
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    metrics = result["metrics"]
    trade_log = result["trade_log"]
    equity_curve = result["equity_curve"]

    # Markdown report
    lines = [
        "# Backtest Report\n",
        "## Performance Metrics\n",
        "| Metric | Value |",
        "|--------|-------|",
    ]
    for k, v in metrics.items():
        if isinstance(v, float):
            lines.append(f"| {k} | {v:.4f} |")
        else:
            lines.append(f"| {k} | {v} |")

    lines.append(f"\n## Trade Log ({len(trade_log)} trades)\n")
    if trade_log:
        lines.append("| Date | Ticker | Action | Price | Qty |")
        lines.append("|------|--------|--------|-------|-----|")
        for t in trade_log:
            lines.append(
                f"| {t['date']} | {t['ticker']} | {t['action']} | "
                f"${t['fill_price']:.2f} | {t['quantity']} |"
            )

    report_md = "\n".join(lines)
    report_path = Path(output_dir) / "backtest_report.md"
    report_path.write_text(report_md)

    # CSV export
    if isinstance(equity_curve, pd.DataFrame) and not equity_curve.empty:
        equity_curve.to_csv(Path(output_dir) / "equity_curve.csv", index=False)

    if trade_log:
        pd.DataFrame(trade_log).to_csv(
            Path(output_dir) / "trade_log.csv", index=False
        )

    return str(report_path)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_backtesting.py -v`
Expected: All 2 tests PASS

- [ ] **Step 5: Commit**

```bash
git add tradingagents/backtesting/engine.py tradingagents/backtesting/report.py tests/test_backtesting.py
git commit -m "feat: add backtesting engine with date loop, portfolio sim, and reporting"
```

### Task 11: Add backtest config to default_config.py

**Files:**
- Modify: `tradingagents/default_config.py`

- [ ] **Step 1: Add backtest config**

Add to `DEFAULT_CONFIG` after the `"options"` entry:

```python
    # Backtesting configuration
    "backtest": {
        "initial_capital": 5000,
        "max_position_pct": 0.35,
        "max_options_risk_pct": 0.05,
        "slippage_bps": 10,
        "commission_per_trade": 0,
        "trading_frequency": "weekly",
        "accuracy_windows": [5, 10, 30],
    },
```

- [ ] **Step 2: Run all tests**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 3: Commit**

```bash
git add tradingagents/default_config.py
git commit -m "feat: add backtest configuration defaults"
```

---

## Phase 4: Execution Layer

### Task 12: Broker Abstraction and Paper Broker

**Files:**
- Create: `tradingagents/execution/__init__.py`
- Create: `tradingagents/execution/base_broker.py`
- Create: `tradingagents/execution/paper_broker.py`
- Test: `tests/test_execution.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_execution.py
import pytest
from tradingagents.execution.base_broker import BaseBroker, OrderResult, AccountInfo
from tradingagents.execution.paper_broker import PaperBroker


class TestPaperBroker:
    def test_initial_account(self):
        broker = PaperBroker(initial_capital=5000.0)
        acct = broker.get_account()
        assert acct.cash == 5000.0
        assert acct.portfolio_value == 5000.0

    def test_submit_stock_buy(self):
        broker = PaperBroker(initial_capital=5000.0)
        result = broker.submit_stock_order("SOFI", "buy", 100, "market", price=10.0)
        assert result.status == "filled"
        assert result.filled_qty == 100
        acct = broker.get_account()
        assert acct.cash == 4000.0

    def test_submit_stock_sell(self):
        broker = PaperBroker(initial_capital=5000.0)
        broker.submit_stock_order("SOFI", "buy", 100, "market", price=10.0)
        result = broker.submit_stock_order("SOFI", "sell", 100, "market", price=12.0)
        assert result.status == "filled"
        acct = broker.get_account()
        assert acct.cash == 5200.0

    def test_get_positions(self):
        broker = PaperBroker(initial_capital=5000.0)
        broker.submit_stock_order("SOFI", "buy", 100, "market", price=10.0)
        positions = broker.get_positions()
        assert len(positions) == 1
        assert positions[0]["ticker"] == "SOFI"
        assert positions[0]["quantity"] == 100

    def test_insufficient_funds_rejected(self):
        broker = PaperBroker(initial_capital=100.0)
        result = broker.submit_stock_order("NVDA", "buy", 10, "market", price=500.0)
        assert result.status == "rejected"

    def test_cancel_order(self):
        broker = PaperBroker(initial_capital=5000.0)
        # Paper broker fills immediately, so cancel always returns False
        assert broker.cancel_order("fake-id") is False

    def test_submit_options_order(self):
        broker = PaperBroker(initial_capital=5000.0)
        result = broker.submit_options_order(
            symbol="SOFI", expiry="2026-06-20", strike=10.0,
            right="call", side="buy", qty=2, price=1.20,
        )
        assert result.status == "filled"
        acct = broker.get_account()
        assert acct.cash == 5000.0 - 2 * 100 * 1.20  # 4760
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_execution.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement base broker and paper broker**

```python
# tradingagents/execution/__init__.py
from .paper_broker import PaperBroker

__all__ = ["PaperBroker"]
```

```python
# tradingagents/execution/base_broker.py
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class OrderResult:
    order_id: str
    status: str  # "filled", "rejected", "cancelled", "pending"
    filled_qty: float = 0
    filled_price: float = 0.0
    message: str = ""


@dataclass
class AccountInfo:
    cash: float
    portfolio_value: float
    buying_power: float


class BaseBroker(ABC):
    @abstractmethod
    def submit_stock_order(self, symbol: str, side: str, qty: int,
                           order_type: str = "market", **kwargs) -> OrderResult:
        pass

    @abstractmethod
    def submit_options_order(self, symbol: str, expiry: str, strike: float,
                             right: str, side: str, qty: int,
                             **kwargs) -> OrderResult:
        pass

    @abstractmethod
    def get_positions(self) -> List[Dict[str, Any]]:
        pass

    @abstractmethod
    def get_account(self) -> AccountInfo:
        pass

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        pass
```

```python
# tradingagents/execution/paper_broker.py
import uuid
from typing import Any, Dict, List

from .base_broker import BaseBroker, OrderResult, AccountInfo


class PaperBroker(BaseBroker):
    def __init__(self, initial_capital: float = 5000.0):
        self.cash = initial_capital
        self.positions: Dict[str, Dict[str, Any]] = {}

    def submit_stock_order(self, symbol: str, side: str, qty: int,
                           order_type: str = "market", **kwargs) -> OrderResult:
        price = kwargs.get("price", 0.0)
        cost = qty * price

        if side == "buy":
            if cost > self.cash:
                return OrderResult(
                    order_id=str(uuid.uuid4()), status="rejected",
                    message="Insufficient funds",
                )
            self.cash -= cost
            if symbol in self.positions:
                pos = self.positions[symbol]
                total_qty = pos["quantity"] + qty
                pos["avg_price"] = (
                    (pos["avg_price"] * pos["quantity"] + price * qty) / total_qty
                )
                pos["quantity"] = total_qty
            else:
                self.positions[symbol] = {
                    "ticker": symbol, "quantity": qty,
                    "avg_price": price, "instrument_type": "stock",
                }
        elif side == "sell":
            self.cash += cost
            if symbol in self.positions:
                self.positions[symbol]["quantity"] -= qty
                if self.positions[symbol]["quantity"] <= 0:
                    del self.positions[symbol]

        return OrderResult(
            order_id=str(uuid.uuid4()), status="filled",
            filled_qty=qty, filled_price=price,
        )

    def submit_options_order(self, symbol: str, expiry: str, strike: float,
                             right: str, side: str, qty: int,
                             **kwargs) -> OrderResult:
        price = kwargs.get("price", 0.0)
        cost = qty * 100 * price  # each contract = 100 shares

        if side == "buy":
            if cost > self.cash:
                return OrderResult(
                    order_id=str(uuid.uuid4()), status="rejected",
                    message="Insufficient funds",
                )
            self.cash -= cost

        key = f"{symbol}_{expiry}_{strike}_{right}"
        if key not in self.positions:
            self.positions[key] = {
                "ticker": symbol, "quantity": qty, "avg_price": price,
                "instrument_type": "option",
                "option_details": {"expiry": expiry, "strike": strike, "right": right},
            }
        else:
            if side == "buy":
                self.positions[key]["quantity"] += qty
            elif side == "sell":
                self.positions[key]["quantity"] -= qty

        return OrderResult(
            order_id=str(uuid.uuid4()), status="filled",
            filled_qty=qty, filled_price=price,
        )

    def get_positions(self) -> List[Dict[str, Any]]:
        return [pos for pos in self.positions.values() if pos["quantity"] > 0]

    def get_account(self) -> AccountInfo:
        positions_value = sum(
            p["quantity"] * p["avg_price"] * (100 if p["instrument_type"] == "option" else 1)
            for p in self.positions.values() if p["quantity"] > 0
        )
        return AccountInfo(
            cash=self.cash,
            portfolio_value=self.cash + positions_value,
            buying_power=self.cash,
        )

    def cancel_order(self, order_id: str) -> bool:
        return False  # Paper broker fills immediately
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_execution.py -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add tradingagents/execution/__init__.py tradingagents/execution/base_broker.py tradingagents/execution/paper_broker.py tests/test_execution.py
git commit -m "feat: add broker abstraction and paper broker for simulated trading"
```

### Task 13: Position Manager (Decision Parser + Risk Enforcement)

**Files:**
- Create: `tradingagents/execution/position_manager.py`
- Test: `tests/test_position_manager.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_position_manager.py
import pytest
from tradingagents.execution.position_manager import PositionManager
from tradingagents.execution.paper_broker import PaperBroker


class TestPositionManager:
    def _make_pm(self, capital=5000.0):
        broker = PaperBroker(initial_capital=capital)
        config = {
            "execution": {
                "daily_loss_limit_pct": 0.10,
                "execution_enabled": True,
                "confirm_before_trade": False,
            },
            "backtest": {
                "max_position_pct": 0.35,
                "max_options_risk_pct": 0.05,
            },
        }
        return PositionManager(broker=broker, config=config)

    def test_parse_buy_stock_decision(self):
        pm = self._make_pm()
        orders = pm.parse_decision(
            decision_text="Rating: BUY\nBuy 100 shares of SOFI at $10",
            rating="BUY",
            ticker="SOFI",
            current_price=10.0,
        )
        assert len(orders) >= 1
        assert orders[0]["action"] == "buy"
        assert orders[0]["ticker"] == "SOFI"

    def test_parse_sell_decision(self):
        pm = self._make_pm()
        orders = pm.parse_decision(
            decision_text="Rating: SELL\nExit all SOFI positions",
            rating="SELL",
            ticker="SOFI",
            current_price=10.0,
        )
        assert len(orders) >= 1
        assert orders[0]["action"] == "sell"

    def test_parse_hold_decision_no_orders(self):
        pm = self._make_pm()
        orders = pm.parse_decision(
            decision_text="Rating: HOLD\nMaintain position",
            rating="HOLD",
            ticker="SOFI",
            current_price=10.0,
        )
        assert len(orders) == 0

    def test_risk_check_rejects_oversized_position(self):
        pm = self._make_pm(capital=5000.0)
        # Trying to buy $4000 worth when max is 35% ($1750)
        passed, reason = pm.check_risk(
            ticker="SOFI",
            action="buy",
            instrument_type="stock",
            quantity=400,
            price=10.0,
        )
        assert passed is False
        assert "position" in reason.lower()

    def test_risk_check_passes_within_limits(self):
        pm = self._make_pm(capital=5000.0)
        passed, reason = pm.check_risk(
            ticker="SOFI",
            action="buy",
            instrument_type="stock",
            quantity=100,
            price=10.0,
        )
        assert passed is True

    def test_execute_decision_end_to_end(self):
        pm = self._make_pm(capital=5000.0)
        results = pm.execute_decision(
            decision_text="Rating: BUY\nBuy SOFI shares",
            rating="BUY",
            ticker="SOFI",
            current_price=10.0,
        )
        assert len(results) >= 1
        assert results[0].status == "filled"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_position_manager.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement PositionManager**

```python
# tradingagents/execution/position_manager.py
from typing import Any, Dict, List, Tuple

from .base_broker import BaseBroker, OrderResult


class PositionManager:
    def __init__(self, broker: BaseBroker, config: dict):
        self.broker = broker
        self.config = config
        self.exec_config = config.get("execution", {})
        self.bt_config = config.get("backtest", {})

    def parse_decision(self, decision_text: str, rating: str,
                       ticker: str, current_price: float) -> List[Dict[str, Any]]:
        if rating in ("HOLD",):
            return []

        account = self.broker.get_account()
        max_position_pct = self.bt_config.get("max_position_pct", 0.35)
        max_spend = account.portfolio_value * max_position_pct

        if rating in ("BUY", "OVERWEIGHT"):
            qty = int(max_spend / current_price) if current_price > 0 else 0
            if qty <= 0:
                return []
            return [{
                "ticker": ticker,
                "action": "buy",
                "instrument_type": "stock",
                "quantity": qty,
                "price": current_price,
            }]

        elif rating in ("SELL", "UNDERWEIGHT"):
            # Sell existing position
            positions = self.broker.get_positions()
            for pos in positions:
                if pos["ticker"] == ticker and pos["instrument_type"] == "stock":
                    return [{
                        "ticker": ticker,
                        "action": "sell",
                        "instrument_type": "stock",
                        "quantity": pos["quantity"],
                        "price": current_price,
                    }]
            return []

        return []

    def check_risk(self, ticker: str, action: str, instrument_type: str,
                   quantity: float, price: float) -> Tuple[bool, str]:
        account = self.broker.get_account()
        max_position_pct = self.bt_config.get("max_position_pct", 0.35)
        max_options_pct = self.bt_config.get("max_options_risk_pct", 0.05)

        multiplier = 100 if instrument_type == "option" else 1
        order_value = quantity * multiplier * price
        max_allowed = account.portfolio_value * (
            max_options_pct if instrument_type == "option" else max_position_pct
        )

        if action == "buy" and order_value > max_allowed:
            return False, (
                f"Position size ${order_value:.0f} exceeds max "
                f"${max_allowed:.0f} ({max_position_pct:.0%} of portfolio)"
            )

        if action == "buy" and order_value > account.buying_power:
            return False, "Insufficient buying power"

        return True, "OK"

    def execute_decision(self, decision_text: str, rating: str,
                         ticker: str, current_price: float) -> List[OrderResult]:
        if not self.exec_config.get("execution_enabled", False):
            return []

        orders = self.parse_decision(decision_text, rating, ticker, current_price)
        results = []

        for order in orders:
            passed, reason = self.check_risk(
                ticker=order["ticker"],
                action=order["action"],
                instrument_type=order["instrument_type"],
                quantity=order["quantity"],
                price=order["price"],
            )
            if not passed:
                results.append(OrderResult(
                    order_id="", status="rejected", message=reason,
                ))
                continue

            if order["instrument_type"] == "stock":
                result = self.broker.submit_stock_order(
                    symbol=order["ticker"],
                    side=order["action"],
                    qty=order["quantity"],
                    price=order["price"],
                )
            else:
                details = order.get("option_details", {})
                result = self.broker.submit_options_order(
                    symbol=order["ticker"],
                    expiry=details.get("expiry", ""),
                    strike=details.get("strike", 0),
                    right=details.get("right", "call"),
                    side=order["action"],
                    qty=order["quantity"],
                    price=order["price"],
                )
            results.append(result)

        return results
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_position_manager.py -v`
Expected: All 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add tradingagents/execution/position_manager.py tests/test_position_manager.py
git commit -m "feat: add position manager with decision parsing and risk enforcement"
```

### Task 14: Alpaca Broker

**Files:**
- Create: `tradingagents/execution/alpaca_broker.py`
- Modify: `tradingagents/execution/__init__.py`
- Modify: `tradingagents/default_config.py`
- Test: `tests/test_execution.py` (append)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_execution.py`:

```python
from unittest.mock import patch, MagicMock
from tradingagents.execution.alpaca_broker import AlpacaBroker


class TestAlpacaBroker:
    @patch("tradingagents.execution.alpaca_broker.TradingClient")
    def test_get_account(self, mock_client_cls):
        mock_client = MagicMock()
        mock_account = MagicMock()
        mock_account.cash = 5000.0
        mock_account.portfolio_value = 5500.0
        mock_account.buying_power = 5000.0
        mock_client.get_account.return_value = mock_account
        mock_client_cls.return_value = mock_client

        broker = AlpacaBroker(api_key="test", secret_key="test", paper=True)
        acct = broker.get_account()
        assert acct.cash == 5000.0

    @patch("tradingagents.execution.alpaca_broker.TradingClient")
    def test_submit_stock_order(self, mock_client_cls):
        mock_client = MagicMock()
        mock_order = MagicMock()
        mock_order.id = "order-123"
        mock_order.status = "filled"
        mock_order.filled_qty = 100
        mock_order.filled_avg_price = 10.0
        mock_client.submit_order.return_value = mock_order
        mock_client_cls.return_value = mock_client

        broker = AlpacaBroker(api_key="test", secret_key="test", paper=True)
        result = broker.submit_stock_order("SOFI", "buy", 100)
        assert result.status == "filled"
        assert result.filled_qty == 100

    @patch("tradingagents.execution.alpaca_broker.TradingClient")
    def test_get_positions(self, mock_client_cls):
        mock_client = MagicMock()
        mock_pos = MagicMock()
        mock_pos.symbol = "SOFI"
        mock_pos.qty = "100"
        mock_pos.avg_entry_price = "10.0"
        mock_pos.asset_class = "us_equity"
        mock_client.get_all_positions.return_value = [mock_pos]
        mock_client_cls.return_value = mock_client

        broker = AlpacaBroker(api_key="test", secret_key="test", paper=True)
        positions = broker.get_positions()
        assert len(positions) == 1
        assert positions[0]["ticker"] == "SOFI"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_execution.py::TestAlpacaBroker -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement AlpacaBroker**

```python
# tradingagents/execution/alpaca_broker.py
from typing import Any, Dict, List

from .base_broker import BaseBroker, OrderResult, AccountInfo

try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce
except ImportError:
    TradingClient = None


class AlpacaBroker(BaseBroker):
    def __init__(self, api_key: str, secret_key: str, paper: bool = True):
        if TradingClient is None:
            raise ImportError("alpaca-py is required. Install with: pip install alpaca-py")
        self.client = TradingClient(api_key, secret_key, paper=paper)

    def submit_stock_order(self, symbol: str, side: str, qty: int,
                           order_type: str = "market", **kwargs) -> OrderResult:
        order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL

        if order_type == "market":
            request = MarketOrderRequest(
                symbol=symbol, qty=qty, side=order_side,
                time_in_force=TimeInForce.DAY,
            )
        else:
            price = kwargs.get("price", 0.0)
            request = LimitOrderRequest(
                symbol=symbol, qty=qty, side=order_side,
                time_in_force=TimeInForce.DAY, limit_price=price,
            )

        order = self.client.submit_order(request)
        return OrderResult(
            order_id=str(order.id),
            status=str(order.status),
            filled_qty=float(order.filled_qty or 0),
            filled_price=float(order.filled_avg_price or 0),
        )

    def submit_options_order(self, symbol: str, expiry: str, strike: float,
                             right: str, side: str, qty: int,
                             **kwargs) -> OrderResult:
        # Alpaca options use OCC symbology
        # Format: SYMBOL + YYMMDD + C/P + strike*1000 (8 digits)
        exp_formatted = expiry.replace("-", "")[2:]  # YYMMDD
        right_char = "C" if right.lower() == "call" else "P"
        strike_formatted = f"{int(strike * 1000):08d}"
        occ_symbol = f"{symbol:<6}{exp_formatted}{right_char}{strike_formatted}"

        order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL
        request = MarketOrderRequest(
            symbol=occ_symbol, qty=qty, side=order_side,
            time_in_force=TimeInForce.DAY,
        )

        order = self.client.submit_order(request)
        return OrderResult(
            order_id=str(order.id),
            status=str(order.status),
            filled_qty=float(order.filled_qty or 0),
            filled_price=float(order.filled_avg_price or 0),
        )

    def get_positions(self) -> List[Dict[str, Any]]:
        positions = self.client.get_all_positions()
        result = []
        for pos in positions:
            result.append({
                "ticker": pos.symbol,
                "quantity": float(pos.qty),
                "avg_price": float(pos.avg_entry_price),
                "instrument_type": "stock" if str(pos.asset_class) == "us_equity" else "option",
            })
        return result

    def get_account(self) -> AccountInfo:
        acct = self.client.get_account()
        return AccountInfo(
            cash=float(acct.cash),
            portfolio_value=float(acct.portfolio_value),
            buying_power=float(acct.buying_power),
        )

    def cancel_order(self, order_id: str) -> bool:
        try:
            self.client.cancel_order_by_id(order_id)
            return True
        except Exception:
            return False
```

- [ ] **Step 4: Add execution config to default_config.py**

Add to `DEFAULT_CONFIG` after the `"backtest"` entry:

```python
    # Execution configuration
    "execution": {
        "mode": "paper",
        "broker": "alpaca",
        "confirm_before_trade": True,
        "daily_loss_limit_pct": 0.10,
        "execution_enabled": False,
    },
```

- [ ] **Step 5: Update execution __init__.py**

```python
# tradingagents/execution/__init__.py
from .paper_broker import PaperBroker
from .alpaca_broker import AlpacaBroker
from .position_manager import PositionManager

__all__ = ["PaperBroker", "AlpacaBroker", "PositionManager"]
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_execution.py -v`
Expected: All 10 tests PASS

- [ ] **Step 7: Commit**

```bash
git add tradingagents/execution/alpaca_broker.py tradingagents/execution/__init__.py tradingagents/default_config.py tests/test_execution.py
git commit -m "feat: add Alpaca broker with paper/live trading support"
```

---

## Phase 5: Dashboard

### Task 15: Dashboard App Shell and Portfolio Page

**Files:**
- Create: `tradingagents/dashboard/__init__.py`
- Create: `tradingagents/dashboard/app.py`
- Create: `tradingagents/dashboard/pages/portfolio.py`
- Create: `tradingagents/dashboard/components/__init__.py`
- Create: `tradingagents/dashboard/components/charts.py`
- Create: `tradingagents/dashboard/components/formatters.py`
- Test: `tests/test_dashboard.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_dashboard.py
import os
import tempfile
import pytest
from unittest.mock import patch, MagicMock

from tradingagents.storage.db import Database
from tradingagents.dashboard.components.formatters import format_rating_badge, format_currency
from tradingagents.dashboard.components.charts import make_equity_curve_chart


class TestFormatters:
    def test_format_rating_badge_buy(self):
        result = format_rating_badge("BUY")
        assert "BUY" in result

    def test_format_rating_badge_sell(self):
        result = format_rating_badge("SELL")
        assert "SELL" in result

    def test_format_currency(self):
        assert format_currency(5000.0) == "$5,000.00"
        assert format_currency(-150.50) == "-$150.50"
        assert format_currency(0) == "$0.00"


class TestCharts:
    def test_make_equity_curve_chart_returns_figure(self):
        import pandas as pd
        df = pd.DataFrame({
            "date": pd.date_range("2025-09-01", periods=5, freq="W"),
            "portfolio_value": [5000, 5100, 5050, 5200, 5300],
        })
        fig = make_equity_curve_chart(df)
        assert fig is not None
        # Plotly figures have a data attribute
        assert hasattr(fig, "data")

    def test_make_equity_curve_chart_empty_df(self):
        import pandas as pd
        df = pd.DataFrame(columns=["date", "portfolio_value"])
        fig = make_equity_curve_chart(df)
        assert fig is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_dashboard.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement formatters and charts**

```python
# tradingagents/dashboard/__init__.py
```

```python
# tradingagents/dashboard/components/__init__.py
```

```python
# tradingagents/dashboard/components/formatters.py

RATING_COLORS = {
    "BUY": "#22c55e",
    "OVERWEIGHT": "#86efac",
    "HOLD": "#fbbf24",
    "UNDERWEIGHT": "#fb923c",
    "SELL": "#ef4444",
}


def format_rating_badge(rating: str) -> str:
    color = RATING_COLORS.get(rating.upper(), "#6b7280")
    return f'<span style="background-color:{color};color:white;padding:4px 12px;border-radius:4px;font-weight:bold">{rating.upper()}</span>'


def format_currency(amount: float) -> str:
    if amount < 0:
        return f"-${abs(amount):,.2f}"
    return f"${amount:,.2f}"
```

```python
# tradingagents/dashboard/components/charts.py
import plotly.graph_objects as go
import pandas as pd


def make_equity_curve_chart(equity_df: pd.DataFrame,
                            title: str = "Portfolio Value") -> go.Figure:
    fig = go.Figure()

    if equity_df.empty:
        fig.update_layout(title=title)
        return fig

    fig.add_trace(go.Scatter(
        x=equity_df["date"],
        y=equity_df["portfolio_value"],
        mode="lines",
        name="Portfolio",
        line=dict(color="#3b82f6", width=2),
    ))

    fig.update_layout(
        title=title,
        xaxis_title="Date",
        yaxis_title="Value ($)",
        template="plotly_dark",
        height=400,
    )
    return fig


def make_pnl_bar_chart(trade_log: list, title: str = "Trade P&L") -> go.Figure:
    fig = go.Figure()

    if not trade_log:
        fig.update_layout(title=title)
        return fig

    pnls = [t.get("pnl", 0) for t in trade_log if t.get("pnl") is not None]
    colors = ["#22c55e" if p >= 0 else "#ef4444" for p in pnls]
    labels = [t.get("ticker", "") for t in trade_log if t.get("pnl") is not None]

    fig.add_trace(go.Bar(
        x=labels, y=pnls, marker_color=colors,
    ))

    fig.update_layout(
        title=title, yaxis_title="P&L ($)",
        template="plotly_dark", height=300,
    )
    return fig
```

- [ ] **Step 4: Create the Streamlit app shell**

```python
# tradingagents/dashboard/app.py
import os
import sys

import streamlit as st

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from tradingagents.storage.db import Database

DB_PATH = os.environ.get(
    "TRADINGAGENTS_DB",
    os.path.join(os.path.dirname(__file__), "../../results/tradingagents.db"),
)


@st.cache_resource
def get_db():
    return Database(DB_PATH)


st.set_page_config(page_title="TradingAgents", layout="wide")

page = st.sidebar.radio("Navigation", ["Portfolio", "Analysis", "Backtest", "Trades"])

if page == "Portfolio":
    from tradingagents.dashboard.pages.portfolio import render
    render(get_db())
elif page == "Analysis":
    st.title("Analysis")
    st.info("Select a ticker to view agent reports.")
elif page == "Backtest":
    st.title("Backtest Results")
    st.info("Run a backtest to see results here.")
elif page == "Trades":
    st.title("Trade History")
    st.info("Trades will appear here after execution.")
```

```python
# tradingagents/dashboard/pages/__init__.py
```

```python
# tradingagents/dashboard/pages/portfolio.py
import streamlit as st
import pandas as pd

from tradingagents.storage.db import Database
from tradingagents.storage.queries import get_portfolio_summary, get_recent_signals
from tradingagents.dashboard.components.formatters import format_currency, format_rating_badge
from tradingagents.dashboard.components.charts import make_equity_curve_chart


def render(db: Database):
    st.title("Portfolio Overview")

    summary = get_portfolio_summary(db)

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Total Trades", summary["total_trades"])
    with col2:
        st.metric("Total P&L", format_currency(summary["total_pnl"]))
    with col3:
        pnl_pct = (summary["total_pnl"] / 5000 * 100) if summary["total_trades"] > 0 else 0
        st.metric("Return", f"{pnl_pct:.1f}%")

    # Recent signals
    st.subheader("Recent Signals")
    signals = get_recent_signals(db, limit=10)
    if signals:
        for s in signals:
            st.markdown(
                f"**{s['ticker']}** ({s['trade_date']}) — "
                f"{format_rating_badge(s['rating'])}",
                unsafe_allow_html=True,
            )
    else:
        st.info("No signals yet. Run an analysis to generate signals.")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_dashboard.py -v`
Expected: All 5 tests PASS

- [ ] **Step 6: Commit**

```bash
git add tradingagents/dashboard/ tests/test_dashboard.py
git commit -m "feat: add Streamlit dashboard with portfolio page, charts, and formatters"
```

### Task 16: Dashboard Analysis, Backtest, and Trades Pages

**Files:**
- Create: `tradingagents/dashboard/pages/analysis.py`
- Create: `tradingagents/dashboard/pages/backtest.py`
- Create: `tradingagents/dashboard/pages/trades.py`

- [ ] **Step 1: Implement analysis page**

```python
# tradingagents/dashboard/pages/analysis.py
import streamlit as st

from tradingagents.storage.db import Database


def render(db: Database):
    st.title("Agent Analysis Reports")

    decisions = db.get_latest_decisions(limit=50)
    tickers = sorted(set(d["ticker"] for d in decisions)) if decisions else []

    if not tickers:
        st.info("No analysis results yet. Run `tradingagents scan` to generate reports.")
        return

    selected = st.selectbox("Select Ticker", tickers)

    # Get latest decision for this ticker
    ticker_decisions = [d for d in decisions if d["ticker"] == selected]
    if not ticker_decisions:
        return

    latest = ticker_decisions[0]
    st.markdown(f"**Date:** {latest['trade_date']} | **Rating:** {latest['rating']}")

    # Show reports
    reports = db.get_reports_for_decision(latest["id"])
    if reports:
        tabs = st.tabs([r["report_type"].title() for r in reports])
        for tab, report in zip(tabs, reports):
            with tab:
                st.markdown(report["content"])
    else:
        st.markdown(latest.get("full_decision", "No detailed report available."))
```

- [ ] **Step 2: Implement backtest page**

```python
# tradingagents/dashboard/pages/backtest.py
import json

import pandas as pd
import streamlit as st

from tradingagents.storage.db import Database
from tradingagents.dashboard.components.charts import make_equity_curve_chart


def render(db: Database):
    st.title("Backtest Results")

    # List recent backtest runs
    rows = db.conn.execute(
        "SELECT * FROM backtest_runs ORDER BY created_at DESC LIMIT 10"
    ).fetchall()

    if not rows:
        st.info("No backtest runs yet. Run `tradingagents backtest` to generate results.")
        return

    runs = [dict(r) for r in rows]
    labels = [
        f"#{r['id']} — {json.loads(r['tickers'])} ({r['start_date']} to {r['end_date']})"
        for r in runs
    ]
    selected_idx = st.selectbox("Select Run", range(len(labels)), format_func=lambda i: labels[i])
    run = runs[selected_idx]

    # Metrics
    metrics = json.loads(run["metrics"])
    st.subheader("Performance Metrics")
    cols = st.columns(4)
    metric_items = list(metrics.items())
    for i, (k, v) in enumerate(metric_items[:8]):
        with cols[i % 4]:
            display_val = f"{v:.4f}" if isinstance(v, float) else str(v)
            st.metric(k.replace("_", " ").title(), display_val)

    # Equity curve
    snapshots = db.get_equity_curve(run["id"])
    if snapshots:
        df = pd.DataFrame(snapshots)
        st.plotly_chart(make_equity_curve_chart(df), use_container_width=True)
```

- [ ] **Step 3: Implement trades page**

```python
# tradingagents/dashboard/pages/trades.py
import pandas as pd
import streamlit as st

from tradingagents.storage.db import Database
from tradingagents.dashboard.components.formatters import format_currency


def render(db: Database):
    st.title("Trade History")

    trades = db.get_all_trades()

    if not trades:
        st.info("No trades recorded yet.")
        return

    df = pd.DataFrame(trades)

    # Filters
    col1, col2 = st.columns(2)
    with col1:
        tickers = ["All"] + sorted(df["ticker"].unique().tolist())
        selected_ticker = st.selectbox("Filter by Ticker", tickers)
    with col2:
        actions = ["All"] + sorted(df["action"].unique().tolist())
        selected_action = st.selectbox("Filter by Action", actions)

    if selected_ticker != "All":
        df = df[df["ticker"] == selected_ticker]
    if selected_action != "All":
        df = df[df["action"] == selected_action]

    st.dataframe(df, use_container_width=True)

    # Export
    csv = df.to_csv(index=False)
    st.download_button("Export CSV", csv, "trades.csv", "text/csv")
```

- [ ] **Step 4: Wire pages into app.py**

Update the page routing in `tradingagents/dashboard/app.py`. Replace the placeholder `elif` blocks:

```python
elif page == "Analysis":
    from tradingagents.dashboard.pages.analysis import render
    render(get_db())
elif page == "Backtest":
    from tradingagents.dashboard.pages.backtest import render
    render(get_db())
elif page == "Trades":
    from tradingagents.dashboard.pages.trades import render
    render(get_db())
```

- [ ] **Step 5: Run all tests**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add tradingagents/dashboard/pages/analysis.py tradingagents/dashboard/pages/backtest.py tradingagents/dashboard/pages/trades.py tradingagents/dashboard/app.py
git commit -m "feat: add analysis, backtest, and trades dashboard pages"
```

---

## Phase 6: Scheduling & Alerts

### Task 17: Alerts Module

**Files:**
- Create: `tradingagents/scheduler/__init__.py`
- Create: `tradingagents/scheduler/alerts.py`
- Test: `tests/test_alerts.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_alerts.py
import pytest
from unittest.mock import patch, MagicMock
from tradingagents.scheduler.alerts import AlertManager


class TestAlertManager:
    def test_init_with_empty_channels(self):
        config = {"alerts": {"enabled": True, "channels": [], "notify_on": []}}
        am = AlertManager(config)
        assert am.enabled is True

    def test_disabled_does_not_send(self):
        config = {"alerts": {"enabled": False, "channels": [], "notify_on": []}}
        am = AlertManager(config)
        # Should not raise even if called
        am.send("new_signal", "Test message")

    @patch("tradingagents.scheduler.alerts.apprise.Apprise")
    def test_send_calls_apprise(self, mock_apprise_cls):
        mock_ap = MagicMock()
        mock_ap.notify.return_value = True
        mock_apprise_cls.return_value = mock_ap

        config = {
            "alerts": {
                "enabled": True,
                "channels": ["json://localhost"],
                "notify_on": ["new_signal"],
            }
        }
        am = AlertManager(config)
        am.send("new_signal", "SOFI rated BUY")

        mock_ap.notify.assert_called_once()

    @patch("tradingagents.scheduler.alerts.apprise.Apprise")
    def test_send_skips_unsubscribed_types(self, mock_apprise_cls):
        mock_ap = MagicMock()
        mock_apprise_cls.return_value = mock_ap

        config = {
            "alerts": {
                "enabled": True,
                "channels": ["json://localhost"],
                "notify_on": ["stop_loss"],  # only subscribed to stop_loss
            }
        }
        am = AlertManager(config)
        am.send("new_signal", "Test")  # should be skipped

        mock_ap.notify.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_alerts.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement AlertManager**

```python
# tradingagents/scheduler/__init__.py
```

```python
# tradingagents/scheduler/alerts.py
import apprise


class AlertManager:
    def __init__(self, config: dict):
        alerts_config = config.get("alerts", {})
        self.enabled = alerts_config.get("enabled", False)
        self.notify_on = set(alerts_config.get("notify_on", []))
        self.channels = alerts_config.get("channels", [])

        self._apprise = None
        if self.enabled and self.channels:
            self._apprise = apprise.Apprise()
            for channel in self.channels:
                self._apprise.add(channel)

    def send(self, alert_type: str, message: str, title: str = "TradingAgents"):
        if not self.enabled:
            return
        if alert_type not in self.notify_on:
            return
        if self._apprise is None:
            return

        self._apprise.notify(title=f"{title}: {alert_type}", body=message)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_alerts.py -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add tradingagents/scheduler/__init__.py tradingagents/scheduler/alerts.py tests/test_alerts.py
git commit -m "feat: add alert manager with apprise multi-channel notifications"
```

### Task 18: Scheduler with Jobs

**Files:**
- Create: `tradingagents/scheduler/scheduler.py`
- Create: `tradingagents/scheduler/jobs.py`
- Modify: `tradingagents/default_config.py`
- Test: `tests/test_scheduler.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_scheduler.py
import pytest
from unittest.mock import patch, MagicMock
from tradingagents.scheduler.scheduler import TradingScheduler
from tradingagents.scheduler.jobs import daily_scan_job


class TestTradingScheduler:
    def _make_config(self):
        return {
            "scheduler": {
                "enabled": True,
                "watchlist": ["SOFI", "PLTR"],
                "scan_time": "07:00",
                "portfolio_check_times": ["10:00", "15:00"],
                "timezone": "US/Eastern",
                "trading_days_only": True,
            },
            "alerts": {"enabled": False, "channels": [], "notify_on": []},
            "llm_provider": "anthropic",
            "deep_think_llm": "claude-sonnet-4-20250514",
            "quick_think_llm": "claude-haiku-4-5-20251001",
        }

    @patch("tradingagents.scheduler.scheduler.BackgroundScheduler")
    def test_scheduler_creates_jobs(self, mock_sched_cls):
        mock_sched = MagicMock()
        mock_sched_cls.return_value = mock_sched

        config = self._make_config()
        ts = TradingScheduler(config)
        ts.start()

        assert mock_sched.add_job.call_count >= 1
        mock_sched.start.assert_called_once()

    @patch("tradingagents.scheduler.scheduler.BackgroundScheduler")
    def test_scheduler_stop(self, mock_sched_cls):
        mock_sched = MagicMock()
        mock_sched_cls.return_value = mock_sched

        config = self._make_config()
        ts = TradingScheduler(config)
        ts.start()
        ts.stop()

        mock_sched.shutdown.assert_called_once()

    @patch("tradingagents.scheduler.scheduler.BackgroundScheduler")
    def test_scheduler_status(self, mock_sched_cls):
        mock_sched = MagicMock()
        mock_sched.get_jobs.return_value = []
        mock_sched_cls.return_value = mock_sched

        config = self._make_config()
        ts = TradingScheduler(config)
        status = ts.status()
        assert "jobs" in status


class TestDailyScanJob:
    @patch("tradingagents.scheduler.jobs.TradingAgentsGraph")
    def test_daily_scan_runs_propagate(self, mock_graph_cls):
        mock_graph = MagicMock()
        mock_graph.propagate.return_value = (
            {"final_trade_decision": "Rating: BUY"},
            "BUY",
        )
        mock_graph_cls.return_value = mock_graph

        config = {
            "scheduler": {"watchlist": ["SOFI"]},
            "alerts": {"enabled": False, "channels": [], "notify_on": []},
        }
        mock_alert = MagicMock()

        results = daily_scan_job(config, mock_alert)
        assert len(results) == 1
        assert results[0]["ticker"] == "SOFI"
        assert results[0]["rating"] == "BUY"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_scheduler.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement scheduler and jobs**

```python
# tradingagents/scheduler/scheduler.py
from typing import Any, Dict

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .alerts import AlertManager
from .jobs import daily_scan_job


class TradingScheduler:
    def __init__(self, config: dict):
        self.config = config
        self.sched_config = config.get("scheduler", {})
        self.alert_manager = AlertManager(config)
        self.scheduler = BackgroundScheduler(
            timezone=self.sched_config.get("timezone", "US/Eastern")
        )

    def start(self):
        scan_time = self.sched_config.get("scan_time", "07:00")
        hour, minute = scan_time.split(":")

        self.scheduler.add_job(
            daily_scan_job,
            trigger=CronTrigger(
                day_of_week="mon-fri", hour=int(hour), minute=int(minute),
            ),
            args=[self.config, self.alert_manager],
            id="daily_scan",
            name="Daily Watchlist Scan",
        )

        for check_time in self.sched_config.get("portfolio_check_times", []):
            h, m = check_time.split(":")
            self.scheduler.add_job(
                lambda: None,  # placeholder for portfolio check
                trigger=CronTrigger(
                    day_of_week="mon-fri", hour=int(h), minute=int(m),
                ),
                id=f"portfolio_check_{check_time}",
                name=f"Portfolio Check {check_time}",
            )

        self.scheduler.start()

    def stop(self):
        self.scheduler.shutdown(wait=False)

    def status(self) -> Dict[str, Any]:
        jobs = self.scheduler.get_jobs()
        return {
            "running": self.scheduler.running if hasattr(self.scheduler, "running") else False,
            "jobs": [
                {"id": j.id, "name": j.name, "next_run": str(j.next_run_time)}
                for j in jobs
            ],
        }
```

```python
# tradingagents/scheduler/jobs.py
from datetime import datetime
from typing import Any, Dict, List

from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.scheduler.alerts import AlertManager


def daily_scan_job(config: dict, alert_manager: AlertManager) -> List[Dict[str, Any]]:
    watchlist = config.get("scheduler", {}).get("watchlist", [])
    today = datetime.now().strftime("%Y-%m-%d")

    ta = TradingAgentsGraph(debug=False, config=config)
    results = []

    for ticker in watchlist:
        try:
            state, rating = ta.propagate(ticker, today)
            decision_text = state.get("final_trade_decision", "")

            results.append({
                "ticker": ticker,
                "date": today,
                "rating": rating,
                "decision": decision_text,
            })

            if rating in ("BUY", "SELL"):
                alert_manager.send(
                    "new_signal",
                    f"{ticker} rated {rating} on {today}.\n\n{decision_text[:500]}",
                )
        except Exception as e:
            results.append({
                "ticker": ticker, "date": today,
                "rating": "ERROR", "decision": str(e),
            })

    return results
```

- [ ] **Step 4: Add scheduler and alerts config to default_config.py**

Add to `DEFAULT_CONFIG` after the `"execution"` entry:

```python
    # Scheduler configuration
    "scheduler": {
        "enabled": False,
        "watchlist": [],
        "scan_time": "07:00",
        "portfolio_check_times": ["10:00", "15:00"],
        "timezone": "US/Eastern",
        "trading_days_only": True,
    },
    # Alerts configuration
    "alerts": {
        "enabled": False,
        "channels": [],
        "notify_on": ["new_signal", "stop_loss", "target_hit", "daily_summary"],
    },
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_scheduler.py -v`
Expected: All 4 tests PASS

- [ ] **Step 6: Commit**

```bash
git add tradingagents/scheduler/scheduler.py tradingagents/scheduler/jobs.py tradingagents/default_config.py tests/test_scheduler.py
git commit -m "feat: add APScheduler-based job scheduling with daily scan and alerts"
```

---

## Phase 7: CLI Extensions

### Task 19: Add CLI Subcommands

**Files:**
- Modify: `cli/main.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Add new dependencies to pyproject.toml**

Add these to the `dependencies` list in `pyproject.toml`:

```toml
    "py_vollib>=1.0.1",
    "alpaca-py>=0.35.0",
    "streamlit>=1.45.0",
    "plotly>=6.0.0",
    "apprise>=1.9.2",
    "apscheduler>=3.11.0",
```

- [ ] **Step 2: Add CLI subcommands to cli/main.py**

Read `cli/main.py` first to understand the structure, then add the following subcommands after the existing `app` definition and before the main analysis function. Each command should be added as a `@app.command()`:

```python
@app.command()
def scan(
    tickers: list[str] = typer.Argument(..., help="Ticker symbols to analyze"),
    provider: str = typer.Option("anthropic", help="LLM provider"),
):
    """Analyze multiple tickers and display ratings."""
    from dotenv import load_dotenv
    load_dotenv()
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    from tradingagents.default_config import DEFAULT_CONFIG
    from datetime import datetime

    config = DEFAULT_CONFIG.copy()
    config["llm_provider"] = provider
    ta = TradingAgentsGraph(debug=False, config=config)
    today = datetime.now().strftime("%Y-%m-%d")

    from rich.console import Console
    from rich.table import Table
    console = Console()
    table = Table(title="Scan Results")
    table.add_column("Ticker")
    table.add_column("Rating")
    table.add_column("Date")

    for ticker in tickers:
        try:
            _, rating = ta.propagate(ticker, today)
            table.add_row(ticker, rating, today)
        except Exception as e:
            table.add_row(ticker, f"ERROR: {e}", today)

    console.print(table)


@app.command()
def backtest(
    tickers: list[str] = typer.Argument(..., help="Ticker symbols"),
    start: str = typer.Option(..., help="Start date YYYY-MM-DD"),
    end: str = typer.Option(..., help="End date YYYY-MM-DD"),
):
    """Run backtest on tickers over a date range."""
    from dotenv import load_dotenv
    load_dotenv()
    from tradingagents.backtesting.engine import Backtester
    from tradingagents.backtesting.report import generate_backtest_report
    from tradingagents.default_config import DEFAULT_CONFIG
    from rich.console import Console

    config = DEFAULT_CONFIG.copy()
    bt = Backtester(config=config)
    result = bt.run(tickers=tickers, start_date=start, end_date=end)

    console = Console()
    console.print("\n[bold]Backtest Results[/bold]")
    for k, v in result["metrics"].items():
        console.print(f"  {k}: {v}")

    report_path = generate_backtest_report(result, f"results/backtests/{'-'.join(tickers)}")
    console.print(f"\nReport saved to: {report_path}")


@app.command()
def portfolio():
    """Show current portfolio positions and P&L."""
    from tradingagents.storage.db import Database
    from tradingagents.storage.queries import get_portfolio_summary, get_trade_history
    from rich.console import Console
    from rich.table import Table

    console = Console()
    db = Database("results/tradingagents.db")
    summary = get_portfolio_summary(db)

    console.print(f"\n[bold]Portfolio Summary[/bold]")
    console.print(f"  Total Trades: {summary['total_trades']}")
    console.print(f"  Total P&L: ${summary['total_pnl']:.2f}")

    trades = get_trade_history(db, limit=10)
    if trades:
        table = Table(title="Recent Trades")
        table.add_column("Date")
        table.add_column("Ticker")
        table.add_column("Action")
        table.add_column("Price")
        table.add_column("Status")
        for t in trades:
            table.add_row(
                str(t.get("created_at", "")),
                t["ticker"], t["action"],
                f"${t['price']:.2f}", t["status"],
            )
        console.print(table)
    db.close()


@app.command()
def dashboard():
    """Launch the Streamlit dashboard."""
    import subprocess
    import sys
    app_path = os.path.join(os.path.dirname(__file__), "../tradingagents/dashboard/app.py")
    subprocess.run([sys.executable, "-m", "streamlit", "run", app_path])


@app.command()
def scheduler(action: str = typer.Argument(..., help="start, stop, or status")):
    """Manage the background scheduler."""
    from dotenv import load_dotenv
    load_dotenv()
    from tradingagents.scheduler.scheduler import TradingScheduler
    from tradingagents.default_config import DEFAULT_CONFIG
    from rich.console import Console

    console = Console()
    config = DEFAULT_CONFIG.copy()

    if action == "start":
        ts = TradingScheduler(config)
        ts.start()
        console.print("[green]Scheduler started.[/green]")
        console.print("Press Ctrl+C to stop.")
        try:
            import time
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            ts.stop()
            console.print("\n[yellow]Scheduler stopped.[/yellow]")
    elif action == "status":
        ts = TradingScheduler(config)
        status = ts.status()
        console.print(f"Jobs: {len(status['jobs'])}")
        for j in status["jobs"]:
            console.print(f"  {j['name']} — next: {j['next_run']}")
    elif action == "stop":
        console.print("[yellow]Scheduler stop requires the running process to be interrupted.[/yellow]")
```

- [ ] **Step 3: Run all tests**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 4: Verify CLI help works**

Run: `.venv/bin/python -m cli.main --help`
Expected: Shows all commands including scan, backtest, portfolio, dashboard, scheduler

- [ ] **Step 5: Commit**

```bash
git add cli/main.py pyproject.toml
git commit -m "feat: add CLI subcommands for scan, backtest, portfolio, dashboard, scheduler"
```

### Task 20: Install New Dependencies

**Files:** None (runtime only)

- [ ] **Step 1: Install updated package**

Run: `pip install .`

- [ ] **Step 2: Verify all imports work**

Run: `.venv/bin/python -c "from tradingagents.storage import Database; from tradingagents.backtesting import Backtester, Portfolio; from tradingagents.execution import PaperBroker, PositionManager; from tradingagents.scheduler.alerts import AlertManager; print('All imports OK')"`
Expected: "All imports OK"

- [ ] **Step 3: Run full test suite**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 4: Commit any lockfile changes**

```bash
git add uv.lock 2>/dev/null; git add pyproject.toml
git commit -m "chore: update dependencies for options, backtesting, execution, dashboard, scheduling"
```

---

## Phase 8: Integration Testing

### Task 21: End-to-End Integration Test

**Files:**
- Create: `tests/test_integration.py`

- [ ] **Step 1: Write integration test**

```python
# tests/test_integration.py
"""
End-to-end integration test that verifies all modules work together.
Uses mocks for LLM and external APIs to avoid real API calls.
"""
import os
import tempfile
import pytest
from unittest.mock import patch, MagicMock

import pandas as pd
import numpy as np
from langchain_core.messages import AIMessage

from tradingagents.storage.db import Database
from tradingagents.backtesting.portfolio import Portfolio, Order
from tradingagents.backtesting.metrics import compute_metrics
from tradingagents.execution.paper_broker import PaperBroker
from tradingagents.execution.position_manager import PositionManager
from tradingagents.scheduler.alerts import AlertManager


class TestIntegrationPipeline:
    @pytest.fixture
    def tmp_db(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db = Database(path)
        yield db
        db.close()
        os.unlink(path)

    def test_full_pipeline_stock_trade(self, tmp_db):
        """Simulate: analysis → decision → execution → storage → metrics"""
        # 1. Simulate a BUY decision
        decision_id = tmp_db.insert_decision(
            ticker="SOFI", trade_date="2026-03-01", rating="BUY",
            full_decision="Rating: BUY\nBuy SOFI based on strong momentum",
            options_report="IV is low, consider long calls",
        )

        # 2. Store analyst reports
        for report_type in ["fundamentals", "technical", "news", "sentiment", "options"]:
            tmp_db.insert_report(
                decision_id=decision_id, ticker="SOFI",
                trade_date="2026-03-01", report_type=report_type,
                content=f"Mock {report_type} report for SOFI",
            )

        # 3. Execute via paper broker
        broker = PaperBroker(initial_capital=5000.0)
        config = {
            "execution": {"execution_enabled": True, "confirm_before_trade": False},
            "backtest": {"max_position_pct": 0.35, "max_options_risk_pct": 0.05},
        }
        pm = PositionManager(broker=broker, config=config)
        results = pm.execute_decision(
            decision_text="Rating: BUY\nBuy SOFI",
            rating="BUY", ticker="SOFI", current_price=10.0,
        )

        assert len(results) == 1
        assert results[0].status == "filled"

        # 4. Record trade in DB
        tmp_db.insert_trade(
            decision_id=decision_id, ticker="SOFI",
            instrument_type="stock", action="buy",
            quantity=results[0].filled_qty, price=10.0,
            option_details=None, status="filled", pnl=None,
        )

        # 5. Verify storage
        trades = tmp_db.get_trades_for_ticker("SOFI")
        assert len(trades) == 1
        reports = tmp_db.get_reports_for_decision(decision_id)
        assert len(reports) == 5

        # 6. Verify broker state
        positions = broker.get_positions()
        assert len(positions) == 1
        assert positions[0]["ticker"] == "SOFI"

    def test_portfolio_metrics_pipeline(self):
        """Simulate backtest portfolio → metrics computation"""
        portfolio = Portfolio(initial_capital=5000.0)

        # Simulate trades
        buy = Order(ticker="SOFI", action="buy", quantity=100,
                    instrument_type="stock", price=10.0)
        portfolio.execute_order(buy, fill_price=10.0, date="2025-09-01", slippage_bps=0)

        # Record daily snapshots
        for i, price in enumerate([10.0, 10.5, 10.2, 11.0, 11.5]):
            date = f"2025-09-{i+1:02d}"
            portfolio.record_snapshot(date, {"SOFI": price})

        # Sell
        sell = Order(ticker="SOFI", action="sell", quantity=100,
                     instrument_type="stock", price=11.5)
        portfolio.execute_order(sell, fill_price=11.5, date="2025-09-05", slippage_bps=0)
        portfolio.trade_log[-1]["pnl"] = 150.0  # record P&L

        # Compute metrics
        equity_df = pd.DataFrame(portfolio.get_equity_curve())
        metrics = compute_metrics(equity_df, portfolio.trade_log)

        assert metrics["total_trades"] == 1
        assert metrics["win_rate"] == 1.0
        assert metrics["total_return"] > 0

    def test_alerts_integration(self):
        """Verify alert manager handles all alert types gracefully"""
        config = {
            "alerts": {
                "enabled": True,
                "channels": [],  # no real channels
                "notify_on": ["new_signal", "stop_loss"],
            }
        }
        am = AlertManager(config)
        # Should not raise even without channels
        am.send("new_signal", "SOFI rated BUY")
        am.send("stop_loss", "PLTR hit stop")
        am.send("daily_summary", "Should be skipped")
```

- [ ] **Step 2: Run integration tests**

Run: `.venv/bin/python -m pytest tests/test_integration.py -v`
Expected: All 3 tests PASS

- [ ] **Step 3: Run full test suite one final time**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 4: Commit**

```bash
git add tests/test_integration.py
git commit -m "test: add end-to-end integration tests for full trading pipeline"
```
