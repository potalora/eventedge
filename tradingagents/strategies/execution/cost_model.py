"""Deterministic, versioned paper execution costs."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation

from .ids import stable_id
from .models import Fill, OrderIntent


CASH_QUANTUM = Decimal("0.0001")
ACT_365_DAYS = Decimal("365")


def quantize_cash(value: Decimal) -> Decimal:
    """Return a canonical paper-ledger cash amount without binary arithmetic."""
    if not isinstance(value, Decimal):
        raise TypeError("cash value must be Decimal")
    return value.quantize(CASH_QUANTUM)


def validate_new_short_borrow_rate(
    annual_rate: Decimal | None, borrow_cost_reject_above: Decimal
) -> Decimal:
    """Fail closed before a new short is filled; reusable by later risk gates."""
    if annual_rate is None:
        raise ValueError("missing borrow rate for new short")
    if (
        not isinstance(annual_rate, Decimal)
        or not annual_rate.is_finite()
        or annual_rate < 0
    ):
        raise ValueError("invalid borrow rate for new short")
    if (
        not isinstance(borrow_cost_reject_above, Decimal)
        or not borrow_cost_reject_above.is_finite()
        or borrow_cost_reject_above < 0
    ):
        raise ValueError("invalid borrow rejection threshold")
    if annual_rate > borrow_cost_reject_above:
        raise ValueError(
            f"borrow rate {annual_rate} exceeds {borrow_cost_reject_above} for new short"
        )
    return annual_rate


class PaperCostModel:
    """Fixed 10-bps adverse equity execution costs for the paper ledger."""

    DEFAULTS = {
        "slippage_bps": "10",
        "commission_per_fill": "0",
        "other_fee_per_fill": "0",
        "margin_requirement": "1.50",
        "margin_financing_rate": "0",
        "idle_cash_yield_rate": "0",
        "existing_short_missing_borrow_rate": "0.30",
    }

    def __init__(self, config: Mapping[str, object] | None = None) -> None:
        values = config or {}
        nested = values.get("paper_ledger")
        if isinstance(nested, Mapping):
            values = nested
        self.slippage_bps = self._configured_decimal(
            values, "slippage_bps", self.DEFAULTS["slippage_bps"]
        )
        self.commission_per_fill = self._configured_decimal(
            values, "commission_per_fill", self.DEFAULTS["commission_per_fill"]
        )
        self.other_fee_per_fill = self._configured_decimal(
            values, "other_fee_per_fill", self.DEFAULTS["other_fee_per_fill"]
        )
        self.margin_requirement = self._configured_decimal(
            values, "margin_requirement", self.DEFAULTS["margin_requirement"]
        )
        self.margin_financing_rate = self._configured_decimal(
            values,
            "margin_financing_rate",
            self.DEFAULTS["margin_financing_rate"],
        )
        self.idle_cash_yield_rate = self._configured_decimal(
            values, "idle_cash_yield_rate", self.DEFAULTS["idle_cash_yield_rate"]
        )
        self.existing_short_missing_borrow_rate = self._configured_decimal(
            values,
            "existing_short_missing_borrow_rate",
            self.DEFAULTS["existing_short_missing_borrow_rate"],
        )

    @staticmethod
    def validate_new_short_borrow_rate(
        annual_rate: Decimal | None, borrow_cost_reject_above: Decimal
    ) -> Decimal:
        return validate_new_short_borrow_rate(annual_rate, borrow_cost_reject_above)

    def fill(
        self,
        intent: OrderIntent,
        reference_price: Decimal,
        effective_at: datetime,
        processed_at: datetime,
    ) -> Fill:
        if (
            not isinstance(reference_price, Decimal)
            or not reference_price.is_finite()
            or reference_price <= 0
        ):
            raise ValueError("reference_price must be a positive finite Decimal")
        if intent.side in {"buy", "cover"}:
            direction = Decimal("1")
        elif intent.side in {"sell", "short"}:
            direction = Decimal("-1")
        else:  # pragma: no cover - OrderIntent's type is the public boundary.
            raise ValueError(f"unsupported order side {intent.side}")
        fill_price = reference_price * (
            Decimal("1") + direction * self.slippage_bps / Decimal("10000")
        )
        slippage = quantize_cash(
            abs(fill_price - reference_price) * intent.requested_qty
        )
        if effective_at.tzinfo is None or effective_at.utcoffset() is None:
            raise ValueError("effective_at must be timezone-aware")
        if processed_at.tzinfo is None or processed_at.utcoffset() is None:
            raise ValueError("processed_at must be timezone-aware")
        execution_session = effective_at.date()
        return Fill(
            stable_id(
                "fill", intent.intent_id, execution_session, intent.requested_qty
            ),
            intent.intent_id,
            intent.side,
            execution_session,
            effective_at,
            processed_at,
            reference_price,
            fill_price,
            intent.requested_qty,
            slippage,
            quantize_cash(self.commission_per_fill),
            quantize_cash(self.other_fee_per_fill),
        )

    def borrow_charge(self, notional: Decimal, annual_rate: Decimal) -> Decimal:
        return self._daily_charge(notional, annual_rate)

    def financing_charge(self, debit_balance: Decimal, annual_rate: Decimal) -> Decimal:
        return self._daily_charge(debit_balance, annual_rate)

    @staticmethod
    def _daily_charge(notional: Decimal, annual_rate: Decimal) -> Decimal:
        if not isinstance(notional, Decimal) or not isinstance(annual_rate, Decimal):
            raise TypeError("notional and annual_rate must be Decimal")
        if (
            notional < 0
            or annual_rate < 0
            or notional.is_finite() is False
            or annual_rate.is_finite() is False
        ):
            raise ValueError(
                "notional and annual_rate must be non-negative finite Decimals"
            )
        return quantize_cash(notional * annual_rate / ACT_365_DAYS)

    @staticmethod
    def _configured_decimal(
        values: Mapping[str, object], key: str, default: str
    ) -> Decimal:
        try:
            value = Decimal(str(values.get(key, default)))
        except (InvalidOperation, ValueError) as error:
            raise ValueError(f"invalid paper ledger config {key}") from error
        if not value.is_finite() or value < 0:
            raise ValueError(f"invalid paper ledger config {key}")
        return value
