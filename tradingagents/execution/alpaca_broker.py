from typing import Any, Dict, List

from .base_broker import BaseBroker, OrderResult, AccountInfo

try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce
    _ALPACA_AVAILABLE = True
except ImportError:
    TradingClient = None
    MarketOrderRequest = None
    LimitOrderRequest = None
    OrderSide = None
    TimeInForce = None
    _ALPACA_AVAILABLE = False


class AlpacaBroker(BaseBroker):
    def __init__(self, api_key: str, secret_key: str, paper: bool = True):
        if TradingClient is None:
            raise ImportError("alpaca-py is required. Install with: pip install alpaca-py")
        self.client = TradingClient(api_key, secret_key, paper=paper)

    def submit_stock_order(self, symbol: str, side: str, qty: int,
                           order_type: str = "market", **kwargs) -> OrderResult:
        order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL

        if order_type == "market":
            request = MarketOrderRequest(
                symbol=symbol, qty=qty, side=order_side,
                time_in_force=TimeInForce.DAY,
            )
        else:
            price = kwargs.get("price", 0.0)
            request = LimitOrderRequest(
                symbol=symbol, qty=qty, side=order_side,
                time_in_force=TimeInForce.DAY, limit_price=price,
            )

        order = self.client.submit_order(request)
        return OrderResult(
            order_id=str(order.id),
            status=str(order.status),
            filled_qty=float(order.filled_qty or 0),
            filled_price=float(order.filled_avg_price or 0),
        )

    def submit_options_order(self, symbol: str, expiry: str, strike: float,
                             right: str, side: str, qty: int,
                             **kwargs) -> OrderResult:
        exp_formatted = expiry.replace("-", "")[2:]
        right_char = "C" if right.lower() == "call" else "P"
        strike_formatted = f"{int(strike * 1000):08d}"
        occ_symbol = f"{symbol:<6}{exp_formatted}{right_char}{strike_formatted}"

        order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL
        request = MarketOrderRequest(
            symbol=occ_symbol, qty=qty, side=order_side,
            time_in_force=TimeInForce.DAY,
        )

        order = self.client.submit_order(request)
        return OrderResult(
            order_id=str(order.id),
            status=str(order.status),
            filled_qty=float(order.filled_qty or 0),
            filled_price=float(order.filled_avg_price or 0),
        )

    def get_positions(self) -> List[Dict[str, Any]]:
        positions = self.client.get_all_positions()
        result = []
        for pos in positions:
            result.append({
                "ticker": pos.symbol,
                "quantity": float(pos.qty),
                "avg_price": float(pos.avg_entry_price),
                "instrument_type": "stock" if str(pos.asset_class) == "us_equity" else "option",
            })
        return result

    def get_account(self) -> AccountInfo:
        acct = self.client.get_account()
        return AccountInfo(
            cash=float(acct.cash),
            portfolio_value=float(acct.portfolio_value),
            buying_power=float(acct.buying_power),
        )

    def cancel_order(self, order_id: str) -> bool:
        try:
            self.client.cancel_order_by_id(order_id)
            return True
        except Exception:
            return False
