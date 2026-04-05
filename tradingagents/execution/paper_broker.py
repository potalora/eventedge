import logging
import uuid
from typing import Any, Dict, List, Optional

from .base_broker import BaseBroker, OrderResult, AccountInfo

logger = logging.getLogger(__name__)

REG_T_MARGIN_FACTOR = 1.5  # 150% Reg T margin requirement for short selling


class PaperBroker(BaseBroker):
    def __init__(self, initial_capital: float = 5000.0):
        self.cash = initial_capital
        self.positions: Dict[str, Dict[str, Any]] = {}
        self.short_positions: Dict[str, Dict[str, Any]] = {}
        self.margin_used: float = 0.0
        self.accrued_borrow_cost: float = 0.0

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

    def submit_short_sell(self, symbol: str, qty: int, price: float,
                          stop_loss: float = 0.0, **kwargs) -> OrderResult:
        """Open a short position with Reg T margin reservation (150% of notional).

        Cash is not debited on open; instead margin is reserved from buying power.
        The short proceeds are conceptually held as collateral.
        """
        required_margin = qty * price * REG_T_MARGIN_FACTOR
        available = self.cash - self.margin_used
        if required_margin > available:
            return OrderResult(
                order_id=str(uuid.uuid4()), status="rejected",
                message="Insufficient margin",
            )

        self.margin_used += required_margin

        if symbol in self.short_positions:
            pos = self.short_positions[symbol]
            total_qty = pos["quantity"] + qty
            pos["avg_price"] = (
                (pos["avg_price"] * pos["quantity"] + price * qty) / total_qty
            )
            pos["quantity"] = total_qty
            pos["margin"] += required_margin
        else:
            self.short_positions[symbol] = {
                "ticker": symbol,
                "quantity": qty,
                "avg_price": price,
                "instrument_type": "stock",
                "side": "short",
                "margin": required_margin,
                "stop_loss": stop_loss,
            }

        return OrderResult(
            order_id=str(uuid.uuid4()), status="filled",
            filled_qty=qty, filled_price=price,
        )

    def submit_cover(self, symbol: str, qty: int, price: float,
                     **kwargs) -> OrderResult:
        """Cover (close) a short position, realising inverted P&L.

        Profit = (entry_price - cover_price) * qty
        """
        if symbol not in self.short_positions:
            return OrderResult(
                order_id=str(uuid.uuid4()), status="rejected",
                message=f"No short position for {symbol}",
            )

        pos = self.short_positions[symbol]
        cover_qty = min(qty, pos["quantity"])
        entry_price = pos["avg_price"]
        pnl = (entry_price - price) * cover_qty

        # Release margin proportional to shares covered
        margin_per_share = pos["margin"] / pos["quantity"]
        released_margin = margin_per_share * cover_qty
        self.margin_used = max(0.0, self.margin_used - released_margin)
        self.cash += pnl

        pos["quantity"] -= cover_qty
        pos["margin"] -= released_margin
        if pos["quantity"] <= 0:
            del self.short_positions[symbol]

        return OrderResult(
            order_id=str(uuid.uuid4()), status="filled",
            filled_qty=cover_qty, filled_price=price,
        )

    def accrue_borrow_cost(self, date: str, borrow_rates: Optional[Dict[str, float]] = None) -> None:
        """Deduct daily stock borrow cost for all open short positions.

        Args:
            date: Date string for logging (not used in calculation).
            borrow_rates: Dict of {ticker: annual_rate}. Missing tickers default to 0.
        """
        if borrow_rates is None:
            borrow_rates = {}

        for symbol, pos in self.short_positions.items():
            annual_rate = borrow_rates.get(symbol, 0.0)
            if annual_rate <= 0:
                continue
            notional = pos["quantity"] * pos["avg_price"]
            daily_cost = notional * annual_rate / 365
            self.accrued_borrow_cost += daily_cost
            self.cash -= daily_cost
            logger.debug(
                "Borrow cost accrued for %s on %s: $%.4f (rate=%.4f)",
                symbol, date, daily_cost, annual_rate,
            )

    def get_positions(self) -> List[Dict[str, Any]]:
        long_positions = [pos for pos in self.positions.values() if pos["quantity"] > 0]
        short_pos_list = [
            {**pos, "side": "short"}
            for pos in self.short_positions.values()
            if pos["quantity"] > 0
        ]
        return long_positions + short_pos_list

    def get_account(self) -> AccountInfo:
        positions_value = sum(
            p["quantity"] * p["avg_price"] * (100 if p["instrument_type"] == "option" else 1)
            for p in self.positions.values() if p["quantity"] > 0
        )
        # Short positions represent a liability at current (entry) price
        short_liability = sum(
            p["quantity"] * p["avg_price"]
            for p in self.short_positions.values() if p["quantity"] > 0
        )
        portfolio_value = self.cash + positions_value - short_liability
        buying_power = self.cash - self.margin_used
        return AccountInfo(
            cash=self.cash,
            portfolio_value=portfolio_value,
            buying_power=buying_power,
        )

    def reconstruct_from_trades(self, open_trades: list[dict]) -> None:
        """Rebuild positions and cash from persisted open trades.

        Called at the start of each daily run to restore broker state
        from the persistent StateManager trade records, since PaperBroker
        is ephemeral (created fresh each run).
        """
        self.positions.clear()
        self.short_positions.clear()
        self.margin_used = 0.0

        for t in open_trades:
            ticker = t.get("ticker", "")
            shares = t.get("shares", 0)
            avg_price = t.get("entry_price", 0.0)
            direction = t.get("direction", "long")

            if not (shares > 0 and ticker):
                continue

            if direction == "short":
                required_margin = shares * avg_price * REG_T_MARGIN_FACTOR
                self.margin_used += required_margin
                if ticker in self.short_positions:
                    pos = self.short_positions[ticker]
                    total = pos["quantity"] + shares
                    pos["avg_price"] = (
                        pos["avg_price"] * pos["quantity"] + avg_price * shares
                    ) / total
                    pos["quantity"] = total
                    pos["margin"] += required_margin
                else:
                    self.short_positions[ticker] = {
                        "ticker": ticker,
                        "quantity": shares,
                        "avg_price": avg_price,
                        "instrument_type": "stock",
                        "side": "short",
                        "margin": required_margin,
                        "stop_loss": 0.0,
                    }
            else:
                if ticker in self.positions:
                    pos = self.positions[ticker]
                    total = pos["quantity"] + shares
                    pos["avg_price"] = (
                        pos["avg_price"] * pos["quantity"] + avg_price * shares
                    ) / total
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
