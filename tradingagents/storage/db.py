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
