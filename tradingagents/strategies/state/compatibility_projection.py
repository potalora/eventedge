"""Deterministic legacy JSON projections from the authoritative portfolio ledger."""

from __future__ import annotations

import json
import os
import tempfile
from decimal import Decimal
from pathlib import Path

from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger


def _atomic_write_text(destination: Path, text: str) -> None:
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _number(value: Decimal) -> float:
    return float(value)


def project_paper_trades(
    ledger: PortfolioLedger,
    destination: Path,
) -> list[dict[str, object]]:
    """Project ledger lots and fills into the legacy paper-trade JSON shape."""
    projected: list[dict[str, object]] = []
    for record in ledger.read_trade_projections():
        position_value = record.entry_price * record.shares
        pnl_pct = (
            record.realized_pnl / position_value if position_value else Decimal("0")
        )
        projected.append(
            {
                "trade_id": record.trade_id,
                "signal_ids": list(record.signal_ids),
                "intent_id": record.intent_id,
                "execution_id": record.execution_id,
                "exit_execution_ids": list(record.exit_fill_ids),
                "strategy": record.strategy,
                "strategies": list(record.strategies),
                "ticker": record.ticker,
                "direction": record.direction,
                "entry_session": record.entry_session.isoformat(),
                "entry_date": record.entry_session.isoformat(),
                "entry_price": _number(record.entry_price),
                "shares": record.shares,
                "original_shares": record.original_shares,
                "closed_shares": record.closed_shares,
                "open_shares": record.open_shares,
                "position_value": _number(position_value),
                "status": record.status,
                "exit_session": (
                    record.exit_session.isoformat() if record.exit_session else None
                ),
                "exit_date": (
                    record.exit_session.isoformat() if record.exit_session else None
                ),
                "exit_price": (
                    _number(record.exit_price)
                    if record.exit_price is not None
                    else None
                ),
                "realized_pnl": _number(record.realized_pnl),
                "pnl": _number(record.realized_pnl),
                "pnl_pct": _number(pnl_pct),
                "slippage_cost": _number(record.slippage_cost),
                "commission_cost": _number(record.commission_cost),
                "other_fees": _number(record.other_fees),
            }
        )
    text = (
        json.dumps(
            projected,
            indent=2,
            sort_keys=True,
            separators=(",", ": "),
            allow_nan=False,
        )
        + "\n"
    )
    _atomic_write_text(Path(destination), text)
    return projected


def project_equity_snapshots(
    ledger: PortfolioLedger,
    destination: Path,
) -> list[dict[str, object]]:
    """Project authoritative account snapshots into legacy-compatible JSONL."""
    initial_cash = ledger.opening_cash()
    trades = ledger.read_trade_projections()
    projected: list[dict[str, object]] = []
    for snapshot in ledger.read_snapshots():
        opened = [trade for trade in trades if trade.entry_session <= snapshot.session]
        n_closed = sum(
            trade.status == "closed"
            and trade.exit_session is not None
            and trade.exit_session <= snapshot.session
            for trade in opened
        )
        n_open = len(opened) - n_closed
        total_pnl = snapshot.net_equity - initial_cash
        total_return_pct = (
            total_pnl / initial_cash * Decimal("100") if initial_cash else Decimal("0")
        )
        projected.append(
            {
                "date": snapshot.session.isoformat(),
                "cash": _number(snapshot.cash),
                "long_value": _number(snapshot.long_market_value),
                "short_liability": _number(snapshot.short_liability),
                "portfolio_value": _number(snapshot.net_equity),
                "realized_pnl": _number(snapshot.realized_pnl),
                "unrealized_pnl": _number(snapshot.unrealized_pnl),
                "total_pnl": _number(total_pnl),
                "total_return_pct": _number(total_return_pct),
                "n_open": n_open,
                "n_closed": n_closed,
                "total_capital": _number(initial_cash),
                "epoch_id": snapshot.epoch_id,
                "snapshot_id": snapshot.snapshot_id,
                "gross_exposure": _number(snapshot.gross_exposure),
                "net_exposure": _number(snapshot.net_exposure),
                "gross_equity": _number(snapshot.gross_equity),
                "margin_used": _number(snapshot.margin_used),
                "buying_power": _number(snapshot.buying_power),
                "slippage_cost": _number(snapshot.slippage_cost),
                "commission_cost": _number(snapshot.commission_cost),
                "other_fees": _number(snapshot.other_fees),
                "borrow_cost": _number(snapshot.borrow_cost),
                "financing_cost": _number(snapshot.financing_cost),
                "dividend_cash": _number(snapshot.dividend_cash),
                "high_water_mark": _number(snapshot.high_water_mark),
                "mark_timestamp": snapshot.valuation_at.isoformat(),
                "valid": snapshot.valid,
                "invalid_reason": snapshot.invalid_reason,
            }
        )
    text = "".join(
        json.dumps(
            row,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
        for row in projected
    )
    _atomic_write_text(Path(destination), text)
    return projected


def project_all(ledger: PortfolioLedger, state_dir: Path) -> None:
    """Refresh both compatibility files from one authoritative ledger."""
    state_dir = Path(state_dir)
    project_paper_trades(ledger, state_dir / "paper_trades.json")
    project_equity_snapshots(ledger, state_dir / "equity_snapshots.jsonl")
