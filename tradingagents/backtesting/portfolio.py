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
