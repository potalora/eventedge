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
        "SELECT * FROM decisions ORDER BY trade_date DESC, id DESC LIMIT ?", (limit,)
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
