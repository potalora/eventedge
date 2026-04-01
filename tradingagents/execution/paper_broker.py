import logging
import uuid
from typing import Any, Dict, List

from .base_broker import BaseBroker, OrderResult, AccountInfo

logger = logging.getLogger(__name__)


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
        cost = qty * 100 * price

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

    def reconstruct_from_trades(self, open_trades: list[dict]) -> None:
        """Rebuild positions and cash from persisted open trades.

        Called at the start of each daily run to restore broker state
        from the persistent StateManager trade records, since PaperBroker
        is ephemeral (created fresh each run).
        """
        self.positions.clear()
        for t in open_trades:
            ticker = t.get("ticker", "")
            shares = t.get("shares", 0)
            avg_price = t.get("entry_price", 0.0)
            if shares > 0 and ticker:
                if ticker in self.positions:
                    pos = self.positions[ticker]
                    total = pos["quantity"] + shares
                    pos["avg_price"] = (pos["avg_price"] * pos["quantity"] + avg_price * shares) / total
                    pos["quantity"] = total
                else:
                    self.positions[ticker] = {
                        "ticker": ticker, "quantity": shares,
                        "avg_price": avg_price, "instrument_type": "stock",
                    }
                self.cash -= shares * avg_price

        if self.cash < 0:
            logger.warning("Broker cash negative after reconstruction: $%.2f", self.cash)

    def cancel_order(self, order_id: str) -> bool:
        return False
