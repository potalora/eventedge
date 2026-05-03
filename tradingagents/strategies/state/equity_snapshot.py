"""Equity snapshot writer — persists daily portfolio value per cohort.

One JSONL line per (cohort, trading_date) appended to equity_snapshots.jsonl
in the cohort's state_dir. Reruns of the same date overwrite the existing row.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Iterable

logger = logging.getLogger(__name__)

SNAPSHOT_FILENAME = "equity_snapshots.jsonl"


def _mark_to_market(trade: dict[str, Any], current_price: float | None) -> tuple[float, float]:
    """Return (position_value, unrealized_pnl) for a single open trade."""
    entry = float(trade.get("entry_price", 0) or 0)
    shares = float(trade.get("shares", 0) or 0)
    direction = trade.get("direction", "long")
    if current_price is None or current_price <= 0:
        current_price = entry

    if direction == "short":
        # Short pnl: (entry - current) * shares; liability = current * shares
        position_value = -current_price * shares  # negative = liability
        unrealized = (entry - current_price) * shares
    else:
        position_value = current_price * shares
        unrealized = (current_price - entry) * shares
    return position_value, unrealized


def _realized_pnl(closed_trades: Iterable[dict[str, Any]]) -> float:
    total = 0.0
    for t in closed_trades:
        entry = float(t.get("entry_price", 0) or 0)
        exit_ = float(t.get("exit_price", 0) or 0)
        shares = float(t.get("shares", 0) or 0)
        direction = t.get("direction", "long")
        if direction == "short":
            total += (entry - exit_) * shares
        else:
            total += (exit_ - entry) * shares
    return total


def _current_price_for(ticker: str, price_cache: dict[str, Any] | None) -> float | None:
    if not price_cache:
        return None
    df = price_cache.get(ticker)
    if df is None or getattr(df, "empty", True):
        return None
    try:
        return float(df["Close"].iloc[-1])
    except (KeyError, IndexError, ValueError):
        return None


def write_snapshot(
    state_dir: str,
    trading_date: str,
    cash: float,
    open_trades: list[dict[str, Any]],
    closed_trades: list[dict[str, Any]],
    price_cache: dict[str, Any] | None,
    total_capital: float,
) -> dict[str, Any]:
    """Append (or replace) one daily equity snapshot for a cohort.

    Returns the snapshot dict that was written.
    """
    long_value = 0.0
    short_liability = 0.0
    unrealized = 0.0
    for trade in open_trades:
        price = _current_price_for(trade.get("ticker", ""), price_cache)
        pv, upnl = _mark_to_market(trade, price)
        unrealized += upnl
        if pv >= 0:
            long_value += pv
        else:
            short_liability += -pv

    realized = _realized_pnl(closed_trades)
    portfolio_value = cash + long_value - short_liability
    total_return_pct = (
        (portfolio_value - total_capital) / total_capital * 100
        if total_capital > 0
        else 0.0
    )

    snapshot = {
        "date": trading_date,
        "cash": round(cash, 2),
        "long_value": round(long_value, 2),
        "short_liability": round(short_liability, 2),
        "portfolio_value": round(portfolio_value, 2),
        "realized_pnl": round(realized, 2),
        "unrealized_pnl": round(unrealized, 2),
        "total_pnl": round(realized + unrealized, 2),
        "total_return_pct": round(total_return_pct, 4),
        "n_open": len(open_trades),
        "n_closed": len(closed_trades),
        "total_capital": round(total_capital, 2),
    }

    os.makedirs(state_dir, exist_ok=True)
    path = os.path.join(state_dir, SNAPSHOT_FILENAME)

    existing: list[dict[str, Any]] = []
    if os.path.exists(path):
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        existing.append(json.loads(line))
        except (json.JSONDecodeError, OSError):
            logger.warning("Could not read existing snapshots at %s; rewriting", path)
            existing = []

    by_date = {row.get("date"): row for row in existing}
    by_date[trading_date] = snapshot

    with open(path, "w") as f:
        for date in sorted(by_date):
            f.write(json.dumps(by_date[date]) + "\n")

    return snapshot


def load_snapshots(state_dir: str) -> list[dict[str, Any]]:
    """Read all equity snapshots for a cohort, sorted by date."""
    path = os.path.join(state_dir, SNAPSHOT_FILENAME)
    if not os.path.exists(path):
        return []
    rows: list[dict[str, Any]] = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    except (json.JSONDecodeError, OSError):
        return []
    return sorted(rows, key=lambda r: r.get("date", ""))
