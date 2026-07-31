from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List

from tradingagents.strategies.execution import Fill, OrderIntent
from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger

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


_UNRESOLVED_STATUSES = {"pending", "accepted", "partially_filled"}


class AlpacaBroker(BaseBroker):
    def __init__(
        self,
        api_key: str,
        secret_key: str,
        paper: bool = True,
        ledger: PortfolioLedger | None = None,
    ):
        if TradingClient is None:
            raise ImportError(
                "alpaca-py is required. Install with: pip install alpaca-py"
            )
        self.client = TradingClient(api_key, secret_key, paper=paper)
        self.ledger = ledger

    def submit_stock_order(
        self,
        symbol: str,
        side: str,
        qty: int,
        order_type: str = "market",
        client_order_id: str | None = None,
        **kwargs: object,
    ) -> OrderResult:
        intent = kwargs.pop("intent", None)
        fill = kwargs.pop("fill", None)
        borrow_rate = kwargs.pop("borrow_rate", None)
        self._validate_fill_candidate(client_order_id, intent, fill, borrow_rate)
        submitted_at: datetime | None = None
        if self.ledger is not None and client_order_id is not None:
            existing = self.ledger.external_order_for_intent(client_order_id)
            if (
                existing is not None
                and str(existing["status"]).lower() in _UNRESOLVED_STATUSES
            ):
                submitted_at = datetime.fromisoformat(str(existing["submitted_at"]))
                try:
                    result = self.reconcile_order(client_order_id)
                except Exception as error:
                    provisional_not_found = (
                        existing["external_order_id"] == client_order_id
                        and str(existing["status"]).lower() == "pending"
                        and self._is_order_not_found(error)
                    )
                    if not provisional_not_found:
                        raise
                else:
                    self.ledger.record_external_order(
                        intent_id=client_order_id,
                        external_order_id=result.order_id,
                        broker="alpaca",
                        status=result.status,
                        submitted_at=submitted_at,
                        reconciled_at=datetime.now(timezone.utc),
                        detail=self._result_detail(result),
                    )
                    self._apply_confirmed_fill(
                        client_order_id, result, intent, fill, borrow_rate
                    )
                    return result
            if existing is None:
                submitted_at = datetime.now(timezone.utc)
                self.ledger.prepare_external_order(
                    intent_id=client_order_id,
                    broker="alpaca",
                    submitted_at=submitted_at,
                )

        order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL

        if order_type == "market":
            request_values = dict(
                symbol=symbol,
                qty=qty,
                side=order_side,
                time_in_force=TimeInForce.DAY,
            )
            if client_order_id is not None:
                request_values["client_order_id"] = client_order_id
            request = MarketOrderRequest(**request_values)
        else:
            price = kwargs.get("price", 0.0)
            request_values = dict(
                symbol=symbol,
                qty=qty,
                side=order_side,
                time_in_force=TimeInForce.DAY,
                limit_price=price,
            )
            if client_order_id is not None:
                request_values["client_order_id"] = client_order_id
            request = LimitOrderRequest(**request_values)

        order = self.client.submit_order(request)
        result = self._to_order_result(order)
        if self.ledger is not None and client_order_id is not None:
            if submitted_at is None:  # pragma: no cover - guarded preparation path.
                raise RuntimeError("external order was not prepared before submit")
            self.ledger.record_external_order(
                intent_id=client_order_id,
                external_order_id=result.order_id,
                broker="alpaca",
                status=result.status,
                submitted_at=submitted_at,
                reconciled_at=None,
                detail=self._result_detail(result),
            )
            self._apply_confirmed_fill(
                client_order_id, result, intent, fill, borrow_rate
            )
        return result

    def submit_options_order(
        self,
        symbol: str,
        expiry: str,
        strike: float,
        right: str,
        side: str,
        qty: int,
        **kwargs,
    ) -> OrderResult:
        exp_formatted = expiry.replace("-", "")[2:]
        right_char = "C" if right.lower() == "call" else "P"
        strike_formatted = f"{int(strike * 1000):08d}"
        occ_symbol = f"{symbol:<6}{exp_formatted}{right_char}{strike_formatted}"

        order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL
        request = MarketOrderRequest(
            symbol=occ_symbol,
            qty=qty,
            side=order_side,
            time_in_force=TimeInForce.DAY,
        )

        order = self.client.submit_order(request)
        return self._to_order_result(order)

    def get_positions(self) -> List[Dict[str, Any]]:
        positions = self.client.get_all_positions()
        result = []
        for pos in positions:
            result.append(
                {
                    "ticker": pos.symbol,
                    "quantity": float(pos.qty),
                    "avg_price": float(pos.avg_entry_price),
                    "instrument_type": "stock"
                    if str(pos.asset_class) == "us_equity"
                    else "option",
                }
            )
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

    def reconcile_order(self, client_order_id: str) -> OrderResult:
        order = self.client.get_order_by_client_id(client_order_id)
        return self._to_order_result(order)

    @classmethod
    def _to_order_result(cls, order: object) -> OrderResult:
        return OrderResult(
            order_id=str(getattr(order, "id")),
            status=cls._status_value(getattr(order, "status")),
            filled_qty=float(getattr(order, "filled_qty", 0) or 0),
            filled_price=float(getattr(order, "filled_avg_price", 0) or 0),
        )

    @staticmethod
    def _status_value(status: object) -> str:
        value = getattr(status, "value", status)
        normalized = str(value).lower()
        normalized = normalized.rsplit(".", 1)[-1]
        return "pending" if normalized == "new" else normalized

    @staticmethod
    def _result_detail(result: OrderResult) -> str:
        return (
            f"status={result.status};filled_qty={result.filled_qty};"
            f"filled_price={result.filled_price}"
        )

    @staticmethod
    def _is_order_not_found(error: Exception) -> bool:
        if getattr(error, "status_code", None) == 404:
            return True
        response = getattr(error, "response", None)
        return getattr(response, "status_code", None) == 404

    def _validate_fill_candidate(
        self,
        client_order_id: str | None,
        intent: object,
        fill: object,
        borrow_rate: object,
    ) -> None:
        if intent is None and fill is None and borrow_rate is None:
            return
        if (
            self.ledger is None
            or client_order_id is None
            or not isinstance(intent, OrderIntent)
            or not isinstance(fill, Fill)
        ):
            raise ValueError("Alpaca ledger fills require a persisted intent/fill pair")
        if client_order_id != intent.intent_id or fill.intent_id != intent.intent_id:
            raise ValueError("Alpaca fill candidate does not match client_order_id")
        if borrow_rate is not None and not isinstance(borrow_rate, Decimal):
            raise TypeError("borrow_rate must be Decimal")

    def _apply_confirmed_fill(
        self,
        client_order_id: str,
        result: OrderResult,
        intent: object,
        fill: object,
        borrow_rate: object,
    ) -> None:
        if result.status != "filled" or intent is None or fill is None:
            return
        if not isinstance(intent, OrderIntent) or not isinstance(fill, Fill):
            raise ValueError("invalid Alpaca fill candidate")
        if (
            result.filled_qty != intent.requested_qty
            or Decimal(str(result.filled_price)) != fill.fill_price
        ):
            raise ValueError("broker-confirmed fill does not match persisted candidate")
        if self.ledger is None:
            raise ValueError("broker-confirmed fill requires a ledger")
        stored = self.ledger.intent(client_order_id)
        if stored is None:
            raise ValueError(f"unknown order intent {client_order_id}")
        self.ledger.apply_fill(
            stored,
            fill,
            borrow_rate=borrow_rate if isinstance(borrow_rate, Decimal) else None,
        )
