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
            CREATE TABLE IF NOT EXISTS strategies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                generation INTEGER NOT NULL,
                parent_ids TEXT,
                name TEXT NOT NULL,
                hypothesis TEXT NOT NULL,
                conviction INTEGER,
                screener_criteria TEXT NOT NULL,
                instrument TEXT NOT NULL,
                entry_rules TEXT NOT NULL,
                exit_rules TEXT NOT NULL,
                position_size_pct REAL,
                max_risk_pct REAL,
                time_horizon_days INTEGER,
                regime_born TEXT,
                status TEXT DEFAULT 'proposed',
                fitness_score REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS strategy_backtest_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_id INTEGER REFERENCES strategies(id),
                sharpe REAL,
                total_return REAL,
                max_drawdown REAL,
                win_rate REAL,
                profit_factor REAL,
                num_trades INTEGER,
                tickers_tested TEXT,
                backtest_period TEXT,
                walk_forward_scores TEXT,
                holdout_sharpe REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS strategy_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_id INTEGER REFERENCES strategies(id),
                ticker TEXT NOT NULL,
                trade_type TEXT NOT NULL,
                entry_date TEXT,
                exit_date TEXT,
                instrument TEXT,
                entry_price REAL,
                exit_price REAL,
                quantity REAL,
                pnl REAL,
                pnl_pct REAL,
                holding_days INTEGER,
                regime TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS pipeline_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                model_tier TEXT NOT NULL,
                rating TEXT,
                market_report TEXT,
                sentiment_report TEXT,
                news_report TEXT,
                fundamentals_report TEXT,
                options_report TEXT,
                full_decision TEXT,
                debate_summary TEXT,
                analyst_scores TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(ticker, trade_date, model_tier)
            );
            CREATE TABLE IF NOT EXISTS reflections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                generation INTEGER NOT NULL,
                patterns_that_work TEXT,
                patterns_that_fail TEXT,
                next_generation_guidance TEXT,
                regime_notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS analyst_weights (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                analyst_name TEXT NOT NULL UNIQUE,
                weight REAL NOT NULL DEFAULT 1.0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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

    # --- strategies ---

    def insert_strategy(self, generation: int, parent_ids, name: str, hypothesis: str,
                        conviction: Optional[int], screener_criteria, instrument: str,
                        entry_rules, exit_rules, position_size_pct: Optional[float],
                        max_risk_pct: Optional[float], time_horizon_days: Optional[int],
                        regime_born: Optional[str], status: str = "proposed") -> int:
        cursor = self.conn.execute(
            """INSERT INTO strategies (generation, parent_ids, name, hypothesis, conviction,
               screener_criteria, instrument, entry_rules, exit_rules, position_size_pct,
               max_risk_pct, time_horizon_days, regime_born, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (generation, json.dumps(parent_ids), name, hypothesis, conviction,
             json.dumps(screener_criteria), instrument, json.dumps(entry_rules),
             json.dumps(exit_rules), position_size_pct, max_risk_pct,
             time_horizon_days, regime_born, status),
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_strategy(self, strategy_id: int) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM strategies WHERE id = ?", (strategy_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["parent_ids"] = json.loads(d["parent_ids"]) if d["parent_ids"] is not None else None
        d["screener_criteria"] = json.loads(d["screener_criteria"])
        d["entry_rules"] = json.loads(d["entry_rules"])
        d["exit_rules"] = json.loads(d["exit_rules"])
        return d

    def get_strategies_by_status(self, status: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM strategies WHERE status = ?", (status,)
        ).fetchall()
        return [self.get_strategy(dict(r)["id"]) for r in rows]

    def get_strategies_by_generation(self, generation: int) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM strategies WHERE generation = ?", (generation,)
        ).fetchall()
        return [self.get_strategy(dict(r)["id"]) for r in rows]

    def get_top_strategies(self, limit: int = 5, min_status: str = "backtested") -> List[Dict[str, Any]]:
        valid_statuses = ("backtested", "active", "paper", "ready", "live")
        placeholders = ",".join("?" * len(valid_statuses))
        rows = self.conn.execute(
            f"SELECT * FROM strategies WHERE status IN ({placeholders}) AND fitness_score IS NOT NULL "
            f"ORDER BY fitness_score DESC LIMIT ?",
            (*valid_statuses, limit),
        ).fetchall()
        return [self.get_strategy(dict(r)["id"]) for r in rows]

    def update_strategy_status(self, strategy_id: int, status: str) -> None:
        self.conn.execute(
            "UPDATE strategies SET status = ? WHERE id = ?", (status, strategy_id)
        )
        self.conn.commit()

    def update_strategy_fitness(self, strategy_id: int, fitness_score: float) -> None:
        self.conn.execute(
            "UPDATE strategies SET fitness_score = ? WHERE id = ?", (fitness_score, strategy_id)
        )
        self.conn.commit()

    # --- strategy_backtest_results ---

    def insert_strategy_backtest(self, strategy_id: int, sharpe: float, total_return: float,
                                  max_drawdown: float, win_rate: float, profit_factor: float,
                                  num_trades: int, tickers_tested, backtest_period: str,
                                  walk_forward_scores, holdout_sharpe: Optional[float] = None) -> int:
        cursor = self.conn.execute(
            """INSERT INTO strategy_backtest_results (strategy_id, sharpe, total_return, max_drawdown,
               win_rate, profit_factor, num_trades, tickers_tested, backtest_period,
               walk_forward_scores, holdout_sharpe)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (strategy_id, sharpe, total_return, max_drawdown, win_rate, profit_factor,
             num_trades, json.dumps(tickers_tested), backtest_period,
             json.dumps(walk_forward_scores), holdout_sharpe),
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_strategy_backtest(self, strategy_id: int) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM strategy_backtest_results WHERE strategy_id = ?", (strategy_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["tickers_tested"] = json.loads(d["tickers_tested"])
        d["walk_forward_scores"] = json.loads(d["walk_forward_scores"])
        return d

    # --- strategy_trades ---

    def insert_strategy_trade(self, strategy_id: int, ticker: str, trade_type: str,
                               entry_date: Optional[str], exit_date: Optional[str],
                               instrument: Optional[str], entry_price: Optional[float],
                               exit_price: Optional[float], quantity: Optional[float],
                               pnl: Optional[float], pnl_pct: Optional[float],
                               holding_days: Optional[int], regime: Optional[str]) -> int:
        cursor = self.conn.execute(
            """INSERT INTO strategy_trades (strategy_id, ticker, trade_type, entry_date, exit_date,
               instrument, entry_price, exit_price, quantity, pnl, pnl_pct, holding_days, regime)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (strategy_id, ticker, trade_type, entry_date, exit_date, instrument,
             entry_price, exit_price, quantity, pnl, pnl_pct, holding_days, regime),
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_strategy_trades(self, strategy_id: int, trade_type: Optional[str] = None) -> List[Dict[str, Any]]:
        if trade_type is not None:
            rows = self.conn.execute(
                "SELECT * FROM strategy_trades WHERE strategy_id = ? AND trade_type = ?",
                (strategy_id, trade_type),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM strategy_trades WHERE strategy_id = ?", (strategy_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # --- pipeline_cache ---

    def insert_pipeline_cache(self, ticker: str, trade_date: str, model_tier: str,
                               rating: Optional[str], market_report: Optional[str],
                               sentiment_report: Optional[str], news_report: Optional[str],
                               fundamentals_report: Optional[str], options_report: Optional[str],
                               full_decision: Optional[str], debate_summary: Optional[str],
                               analyst_scores) -> int:
        cursor = self.conn.execute(
            """INSERT OR REPLACE INTO pipeline_cache (ticker, trade_date, model_tier, rating,
               market_report, sentiment_report, news_report, fundamentals_report, options_report,
               full_decision, debate_summary, analyst_scores)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ticker, trade_date, model_tier, rating, market_report, sentiment_report,
             news_report, fundamentals_report, options_report, full_decision,
             debate_summary, json.dumps(analyst_scores)),
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_pipeline_cache(self, ticker: str, trade_date: str, model_tier: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM pipeline_cache WHERE ticker = ? AND trade_date = ? AND model_tier = ?",
            (ticker, trade_date, model_tier),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["analyst_scores"] = json.loads(d["analyst_scores"]) if d["analyst_scores"] is not None else None
        return d

    # --- reflections ---

    def insert_reflection(self, generation: int, patterns_that_work, patterns_that_fail,
                           next_generation_guidance, regime_notes: Optional[str]) -> int:
        cursor = self.conn.execute(
            """INSERT INTO reflections (generation, patterns_that_work, patterns_that_fail,
               next_generation_guidance, regime_notes)
               VALUES (?, ?, ?, ?, ?)""",
            (generation, json.dumps(patterns_that_work), json.dumps(patterns_that_fail),
             json.dumps(next_generation_guidance), regime_notes),
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_reflections(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        if limit is not None:
            rows = self.conn.execute(
                "SELECT * FROM reflections ORDER BY generation ASC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM reflections ORDER BY generation ASC"
            ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d["patterns_that_work"] = json.loads(d["patterns_that_work"]) if d["patterns_that_work"] is not None else None
            d["patterns_that_fail"] = json.loads(d["patterns_that_fail"]) if d["patterns_that_fail"] is not None else None
            d["next_generation_guidance"] = json.loads(d["next_generation_guidance"]) if d["next_generation_guidance"] is not None else None
            result.append(d)
        return result

    def get_latest_reflection(self) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM reflections ORDER BY generation DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["patterns_that_work"] = json.loads(d["patterns_that_work"]) if d["patterns_that_work"] is not None else None
        d["patterns_that_fail"] = json.loads(d["patterns_that_fail"]) if d["patterns_that_fail"] is not None else None
        d["next_generation_guidance"] = json.loads(d["next_generation_guidance"]) if d["next_generation_guidance"] is not None else None
        return d

    # --- analyst_weights ---

    def upsert_analyst_weight(self, analyst_name: str, weight: float) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO analyst_weights (analyst_name, weight) VALUES (?, ?)",
            (analyst_name, weight),
        )
        self.conn.commit()

    def get_analyst_weights(self) -> Dict[str, float]:
        rows = self.conn.execute("SELECT analyst_name, weight FROM analyst_weights").fetchall()
        return {row["analyst_name"]: row["weight"] for row in rows}

    def close(self):
        self.conn.close()
