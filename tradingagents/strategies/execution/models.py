from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import date, datetime
from decimal import Decimal
from typing import Literal


Side = Literal["buy", "sell", "short", "cover"]
Direction = Literal["long", "short", "neutral"]
IntentStatus = Literal["pending", "filled", "rejected", "cancelled"]


def _require_decimals(instance: object) -> None:
    for field in fields(instance):
        value = getattr(instance, field.name)
        if field.name in {
            "open", "high", "low", "close", "ratio", "cash_per_share",
            "reference_close", "stop_price", "reference_price", "fill_price",
            "slippage", "commission", "other_fees", "cash",
            "long_market_value", "short_liability", "gross_exposure",
            "net_exposure", "margin_used", "buying_power", "realized_pnl",
            "unrealized_pnl", "gross_equity", "slippage_cost",
            "commission_cost", "other_fees", "borrow_cost", "financing_cost",
            "dividend_cash", "net_equity", "high_water_mark", "amount",
        } and value is not None and not isinstance(value, Decimal):
            raise TypeError(f"{field.name} must be Decimal")


@dataclass(frozen=True)
class MarketBar:
    ticker: str
    session: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    source: str
    fetched_at: datetime
    adjusted: bool

    def __post_init__(self) -> None:
        _require_decimals(self)


@dataclass(frozen=True)
class CorporateAction:
    action_id: str
    ticker: str
    session: date
    action_type: Literal["split", "cash_dividend"]
    ratio: Decimal | None
    cash_per_share: Decimal | None
    source: str
    fetched_at: datetime
    verified: bool

    def __post_init__(self) -> None:
        _require_decimals(self)


@dataclass(frozen=True)
class SignalRecord:
    signal_id: str
    epoch_id: str
    policy_id: str
    event_key: str
    strategy: str
    ticker: str
    direction: Direction
    event_at: datetime | None
    observed_at: datetime
    reference_session: date
    reference_close: Decimal
    decision_at: datetime
    evidence_hash: str

    def __post_init__(self) -> None:
        _require_decimals(self)


@dataclass(frozen=True)
class OrderIntent:
    intent_id: str
    signal_ids: tuple[str, ...]
    cohort_id: str
    side: Side
    requested_qty: int
    created_at: datetime
    eligible_session: date
    price_rule: Literal["next_session_open", "resting_stop"]
    status: IntentStatus
    stop_price: Decimal | None
    external_order_id: str | None

    def __post_init__(self) -> None:
        _require_decimals(self)
        if self.requested_qty <= 0:
            raise ValueError("requested_qty must be positive")


@dataclass(frozen=True)
class Fill:
    fill_id: str
    intent_id: str
    side: Side
    session: date
    effective_at: datetime
    processed_at: datetime
    reference_price: Decimal
    fill_price: Decimal
    quantity: int
    slippage: Decimal
    commission: Decimal
    other_fees: Decimal

    def __post_init__(self) -> None:
        _require_decimals(self)


@dataclass(frozen=True)
class FillResult:
    status: Literal["filled", "rejected", "pending"]
    fill: Fill | None
    reason: str


@dataclass(frozen=True)
class LedgerEvent:
    event_id: str
    session: date
    event_type: str
    amount: Decimal
    flagged: bool
    detail: str

    def __post_init__(self) -> None:
        _require_decimals(self)


@dataclass(frozen=True)
class AccountState:
    cohort_id: str
    cash: Decimal
    long_market_value: Decimal
    short_liability: Decimal
    margin_used: Decimal
    buying_power: Decimal
    net_equity: Decimal
    high_water_mark: Decimal

    def __post_init__(self) -> None:
        _require_decimals(self)


@dataclass(frozen=True)
class AccountSnapshot:
    snapshot_id: str
    cohort_id: str
    epoch_id: str
    session: date
    valuation_at: datetime
    cash: Decimal
    long_market_value: Decimal
    short_liability: Decimal
    gross_exposure: Decimal
    net_exposure: Decimal
    margin_used: Decimal
    buying_power: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    gross_equity: Decimal
    slippage_cost: Decimal
    commission_cost: Decimal
    other_fees: Decimal
    borrow_cost: Decimal
    financing_cost: Decimal
    dividend_cash: Decimal
    net_equity: Decimal
    high_water_mark: Decimal
    valid: bool
    invalid_reason: str

    def __post_init__(self) -> None:
        _require_decimals(self)


@dataclass(frozen=True)
class BenchmarkObservation:
    observation_id: str
    cohort_id: str
    epoch_id: str
    session: date
    symbol: str
    close: Decimal
    return_basis: Literal["total_return_adjusted"]
    source: str
    observed_at: datetime
    valid: bool
    invalid_reason: str

    def __post_init__(self) -> None:
        _require_decimals(self)
