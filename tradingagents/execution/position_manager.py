from typing import Any, Dict, List, Tuple
from .base_broker import BaseBroker, OrderResult


class PositionManager:
    def __init__(self, broker: BaseBroker, config: dict):
        self.broker = broker
        self.config = config
        self.exec_config = config.get("execution", {})
        self.bt_config = config.get("backtest", {})

    def parse_decision(self, decision_text: str, rating: str,
                       ticker: str, current_price: float) -> List[Dict[str, Any]]:
        if rating in ("HOLD",):
            return []
        account = self.broker.get_account()
        max_position_pct = self.bt_config.get("max_position_pct", 0.35)
        max_spend = account.portfolio_value * max_position_pct

        if rating in ("BUY", "OVERWEIGHT"):
            qty = int(max_spend / current_price) if current_price > 0 else 0
            if qty <= 0:
                return []
            return [{"ticker": ticker, "action": "buy", "instrument_type": "stock",
                      "quantity": qty, "price": current_price}]
        elif rating in ("SELL", "UNDERWEIGHT"):
            positions = self.broker.get_positions()
            for pos in positions:
                if pos["ticker"] == ticker and pos["instrument_type"] == "stock":
                    return [{"ticker": ticker, "action": "sell", "instrument_type": "stock",
                              "quantity": pos["quantity"], "price": current_price}]
            # No existing position: still emit a sell signal with a default quantity
            qty = int(max_spend / current_price) if current_price > 0 else 0
            if qty <= 0:
                return []
            return [{"ticker": ticker, "action": "sell", "instrument_type": "stock",
                      "quantity": qty, "price": current_price}]
        return []

    def check_risk(self, ticker: str, action: str, instrument_type: str,
                   quantity: float, price: float) -> Tuple[bool, str]:
        account = self.broker.get_account()
        max_position_pct = self.bt_config.get("max_position_pct", 0.35)
        max_options_pct = self.bt_config.get("max_options_risk_pct", 0.05)
        multiplier = 100 if instrument_type == "option" else 1
        order_value = quantity * multiplier * price
        max_allowed = account.portfolio_value * (
            max_options_pct if instrument_type == "option" else max_position_pct
        )
        if action == "buy" and order_value > max_allowed:
            return False, (f"Position size ${order_value:.0f} exceeds max "
                          f"${max_allowed:.0f} ({max_position_pct:.0%} of portfolio)")
        if action == "buy" and order_value > account.buying_power:
            return False, "Insufficient buying power"
        return True, "OK"

    def execute_decision(self, decision_text: str, rating: str,
                         ticker: str, current_price: float) -> List[OrderResult]:
        if not self.exec_config.get("execution_enabled", False):
            return []
        orders = self.parse_decision(decision_text, rating, ticker, current_price)
        results = []
        for order in orders:
            passed, reason = self.check_risk(
                ticker=order["ticker"], action=order["action"],
                instrument_type=order["instrument_type"],
                quantity=order["quantity"], price=order["price"],
            )
            if not passed:
                results.append(OrderResult(order_id="", status="rejected", message=reason))
                continue
            if order["instrument_type"] == "stock":
                result = self.broker.submit_stock_order(
                    symbol=order["ticker"], side=order["action"],
                    qty=order["quantity"], price=order["price"],
                )
            else:
                details = order.get("option_details", {})
                result = self.broker.submit_options_order(
                    symbol=order["ticker"], expiry=details.get("expiry", ""),
                    strike=details.get("strike", 0), right=details.get("right", "call"),
                    side=order["action"], qty=order["quantity"], price=order["price"],
                )
            results.append(result)
        return results
