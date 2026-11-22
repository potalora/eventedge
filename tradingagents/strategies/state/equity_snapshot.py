"""Equity snapshot helpers — read-only ledger projections with zero write-on-read."""

from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path
from typing import Any, Iterable

from tradingagents.strategies.orchestration._io import atomic_write_text
from tradingagents.strategies.state.generation_db import (
    GENERATION_DB_FILENAME,
    GenerationDB,
)

logger = logging.getLogger(__name__)

SNAPSHOT_FILENAME = "equity_snapshots.jsonl"


def _mark_to_market(
    trade: dict[str, Any], current_price: float | None
) -> tuple[float, float]:
    """Return (position_value, unrealized_pnl) for a single open trade."""
    entry = float(trade.get("entry_price", 0) or 0)
    shares = float(trade.get("shares", 0) or 0)
    direction = trade.get("direction", "long")
    if current_price is None or math.isnan(current_price) or current_price <= 0:
        raise ValueError("missing valid mark for open paper trade")

    if direction == "short":
        position_value = -current_price * shares
        unrealized = (entry - current_price) * shares
    else:
        position_value = current_price * shares
        unrealized = (current_price - entry) * shares
    return position_value, unrealized


def _realized_pnl(closed_trades: Iterable[dict[str, Any]]) -> float:
    total = 0.0
    for trade in closed_trades:
        entry = float(trade.get("entry_price", 0) or 0)
        exit_ = float(trade.get("exit_price", 0) or 0)
        shares = float(trade.get("shares", 0) or 0)
        direction = trade.get("direction", "long")
        if direction == "short":
            total += (entry - exit_) * shares
        else:
            total += (exit_ - entry) * shares
    return total


def _current_price_for(
    ticker: str, price_cache: dict[str, Any] | None
) -> float | None:
    if not price_cache:
        return None
    df = price_cache.get(ticker)
    if df is None or getattr(df, "empty", True):
        return None
    try:
        val = float(df["Close"].iloc[-1])
    except (KeyError, IndexError, ValueError):
        return None
    return None if math.isnan(val) else val


def _snapshots_from_ledger(
    ledger_source: Path | GenerationDB,
    *,
    cohort_id: str | None = None,
) -> list[dict[str, Any]]:
    """Project authoritative AccountSnapshots to dicts in memory (zero writes)."""
    from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger

    ledger = PortfolioLedger.open_existing(ledger_source, cohort_id=cohort_id)
    try:
        opening = float(ledger.opening_cash())
        snapshots = ledger.read_snapshots(valid_only=True)
    finally:
        ledger.close()

    rows: list[dict[str, Any]] = []
    for snap in snapshots:
        net_equity = float(snap.net_equity)
        total_return_pct = (
            (net_equity - opening) / opening * 100.0 if opening > 0 else 0.0
        )
        rows.append(
            {
                "date": snap.session.isoformat(),
                "cash": round(float(snap.cash), 2),
                "long_value": round(float(snap.long_market_value), 2),
                "short_liability": round(float(snap.short_liability), 2),
                "portfolio_value": round(net_equity, 2),
                "realized_pnl": round(float(snap.realized_pnl), 2),
                "unrealized_pnl": round(float(snap.unrealized_pnl), 2),
                "total_pnl": round(
                    float(snap.realized_pnl + snap.unrealized_pnl), 2
                ),
                "total_return_pct": round(total_return_pct, 4),
                "total_capital": round(opening, 2),
            }
        )
    return rows


def _resolve_ledger_source(
    state_dir: str | Path, db: GenerationDB | None = None
) -> Path | GenerationDB | None:
    if db is not None:
        return db
    root = Path(state_dir)
    for candidate in (
        root / GENERATION_DB_FILENAME,
        root.parent / GENERATION_DB_FILENAME,
        root / "portfolio.db",
    ):
        if os.path.lexists(candidate):
            return candidate
    return None


def write_snapshot(
    state_dir: str,
    trading_date: str,
    cash: float,
    open_trades: list[dict[str, Any]],
    closed_trades: list[dict[str, Any]],
    price_cache: dict[str, Any] | None,
    total_capital: float,
    *,
    cohort_id: str | None = None,
    db: GenerationDB | None = None,
) -> dict[str, Any]:
    """Return authoritative ledger snapshot or write legacy JSONL when no DB exists."""
    ledger_source = _resolve_ledger_source(state_dir, db)
    if ledger_source is not None:
        from tradingagents.strategies.state.portfolio_ledger import MissingMarkError

        snapshots = _snapshots_from_ledger(ledger_source, cohort_id=cohort_id)
        match = next(
            (row for row in snapshots if row.get("date") == trading_date), None
        )
        if match is None:
            raise MissingMarkError(
                f"no authoritative PortfolioLedger snapshot for {trading_date}"
            )
        return match

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
    portfolio_value = total_capital + realized + unrealized
    cash = portfolio_value - long_value + short_liability
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

    path = Path(state_dir) / SNAPSHOT_FILENAME
    existing: list[dict[str, Any]] = []
    if path.is_file():
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        existing.append(json.loads(line))
        except (json.JSONDecodeError, OSError):
            logger.warning("Could not read existing snapshots at %s; rewriting", path)
            existing = []

    by_date = {row.get("date"): row for row in existing}
    by_date[trading_date] = snapshot
    text = "".join(json.dumps(by_date[d]) + "\n" for d in sorted(by_date))
    atomic_write_text(path, text)
    return snapshot


def load_snapshots(
    state_dir: str | Path,
    *,
    cohort_id: str | None = None,
    db: GenerationDB | None = None,
) -> list[dict[str, Any]]:
    """Read all equity snapshots sorted by date (strictly read-only, zero writes)."""
    ledger_source = _resolve_ledger_source(state_dir, db)
    if ledger_source is not None:
        return _snapshots_from_ledger(ledger_source, cohort_id=cohort_id)
    path = Path(state_dir) / SNAPSHOT_FILENAME
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    except (json.JSONDecodeError, OSError):
        return []
    return sorted(rows, key=lambda r: r.get("date", ""))
