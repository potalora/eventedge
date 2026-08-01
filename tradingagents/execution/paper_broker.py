"""Paper broker compatibility adapter over the authoritative cohort ledger."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from tradingagents.strategies.execution import Fill, OrderIntent
from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger

from .base_broker import AccountInfo, BaseBroker, OrderResult


DIRECT_SUBMISSION_DISABLED = (
    "PaperBroker direct price submission is disabled; stage and execute a ledger intent"
)


class PaperBroker(BaseBroker):
    """Read ledger state and apply only prebuilt, persisted intent/fill pairs."""

    def __init__(self, ledger: PortfolioLedger) -> None:
        if not isinstance(ledger, PortfolioLedger):
            raise TypeError("PaperBroker requires a PortfolioLedger")
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
        if order_type != "market" or client_order_id is not None or "price" in kwargs:
            raise RuntimeError(DIRECT_SUBMISSION_DISABLED)
        return self._apply_pair(symbol, side, qty, kwargs)

    def submit_options_order(
        self,
        symbol: str,
        expiry: str,
        strike: float,
        right: str,
        side: str,
        qty: int,
        **kwargs: object,
    ) -> OrderResult:
        del symbol, expiry, strike, right, side, qty, kwargs
        raise RuntimeError(DIRECT_SUBMISSION_DISABLED)

    def submit_short_sell(
        self,
        symbol: str,
        qty: int,
        price: float | Decimal | None = None,
        stop_loss: float = 0.0,
        **kwargs: object,
    ) -> OrderResult:
        if price is not None or stop_loss != 0.0:
            raise RuntimeError(DIRECT_SUBMISSION_DISABLED)
        return self._apply_pair(symbol, "short", qty, kwargs)

    def submit_cover(
        self,
        symbol: str,
        qty: int,
        price: float | Decimal | None = None,
        **kwargs: object,
    ) -> OrderResult:
        if price is not None:
            raise RuntimeError(DIRECT_SUBMISSION_DISABLED)
        return self._apply_pair(symbol, "cover", qty, kwargs)

    def _apply_pair(
        self,
        symbol: str,
        side: str,
        qty: int,
        values: dict[str, object],
    ) -> OrderResult:
        if set(values) - {"intent", "fill", "borrow_rate"}:
            raise RuntimeError(DIRECT_SUBMISSION_DISABLED)
        intent = values.get("intent")
        fill = values.get("fill")
        if not isinstance(intent, OrderIntent) or not isinstance(fill, Fill):
            raise RuntimeError(DIRECT_SUBMISSION_DISABLED)
        signals = self.ledger.signals_for_intent(intent.intent_id)
        if (
            not signals
            or {signal.ticker for signal in signals} != {symbol}
            or intent.side != side
            or intent.requested_qty != qty
            or fill.intent_id != intent.intent_id
        ):
            raise ValueError("persisted intent/fill pair does not match submission")
        borrow_rate = values.get("borrow_rate")
        if borrow_rate is not None and not isinstance(borrow_rate, Decimal):
            raise TypeError("borrow_rate must be Decimal")
        self.ledger.apply_fill(intent, fill, borrow_rate=borrow_rate)
        return OrderResult(
            order_id=intent.intent_id,
            status="filled",
            filled_qty=fill.quantity,
            filled_price=float(fill.fill_price),
        )

    def get_positions(self) -> list[dict[str, Any]]:
        return [
            {
                **position,
                "avg_price": float(position["avg_price"]),
            }
            for position in self.ledger.open_positions()
        ]

    def get_account(self) -> AccountInfo:
        state = self.ledger.account_state()
        return AccountInfo(
            cash=float(state.cash),
            portfolio_value=float(state.net_equity),
            buying_power=float(state.buying_power),
        )

    def cancel_order(self, order_id: str) -> bool:
        del order_id
        return False

    def reconcile_order(self, client_order_id: str) -> OrderResult:
        del client_order_id
        raise NotImplementedError("paper ledger intents do not use external orders")
