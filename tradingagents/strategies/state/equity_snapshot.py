"""Equity snapshot writer — persists daily portfolio value per cohort.

One JSONL line per (cohort, trading_date) appended to equity_snapshots.jsonl
in the cohort's state_dir. Reruns of the same date overwrite the existing row.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

SNAPSHOT_FILENAME = "equity_snapshots.jsonl"


def _atomic_write_text(path: str, text: str) -> None:
    """Write text via a temp file + os.replace so a crash can't truncate the
    existing file. Mirrors generation_manager._save_manifest."""
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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
        v = float(df["Close"].iloc[-1])
    except (KeyError, IndexError, ValueError):
        return None
    return None if math.isnan(v) else v


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
    ledger_path = Path(state_dir) / "portfolio.db"
    if ledger_path.is_file():
        from tradingagents.strategies.state.compatibility_projection import (
            project_equity_snapshots,
        )
        from tradingagents.strategies.state.portfolio_ledger import (
            MissingMarkError,
            PortfolioLedger,
        )

        ledger = PortfolioLedger.open_existing(ledger_path)
        try:
            snapshots = project_equity_snapshots(
                ledger, Path(state_dir) / SNAPSHOT_FILENAME
            )
        finally:
            ledger.close()
        snapshot = next(
            (row for row in snapshots if row.get("date") == trading_date), None
        )
        if snapshot is None:
            raise MissingMarkError(
                f"no authoritative PortfolioLedger snapshot for {trading_date}"
            )
        return snapshot

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
    # Equity identity: equity == starting capital + banked realized P&L +
    # mark-to-market unrealized P&L. This is provably equal to
    # (cash + long_value - short_liability) when cash is correct, but does NOT
    # depend on the live broker's cash — which is stale on exit days because
    # `close_trade` updates persisted state, not the in-memory broker. Deriving
    # portfolio_value from the identity keeps the snapshot correct regardless.
    portfolio_value = total_capital + realized + unrealized
    # Report the cash implied by the identity so the row is self-consistent
    # (cash + long_value - short_liability == portfolio_value).
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

    text = "".join(json.dumps(by_date[d]) + "\n" for d in sorted(by_date))
    _atomic_write_text(path, text)

    return snapshot


def load_snapshots(state_dir: str) -> list[dict[str, Any]]:
    """Read all equity snapshots for a cohort, sorted by date."""
    ledger_path = Path(state_dir) / "portfolio.db"
    if ledger_path.is_file():
        from tradingagents.strategies.state.compatibility_projection import (
            project_equity_snapshots,
        )
        from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger

        ledger = PortfolioLedger.open_existing(ledger_path)
        try:
            return project_equity_snapshots(ledger, Path(state_dir) / SNAPSHOT_FILENAME)
        finally:
            ledger.close()
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
