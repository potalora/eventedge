from datetime import date, datetime, timezone
from decimal import Decimal

from tradingagents.strategies.execution.ids import stable_id
from tradingagents.strategies.execution.models import (
    Fill,
    MarketBar,
    OrderIntent,
    SignalRecord,
)


UTC = timezone.utc


def test_stable_id_is_order_stable_for_nested_mappings():
    left = stable_id("event", {"b": 2, "a": ["x", 1]})
    right = stable_id("event", {"a": ["x", 1], "b": 2})
    assert left == right
    assert left.startswith("event_")


def test_signal_identity_includes_epoch_and_policy():
    base = ("epoch-2", "litigation", "30d", "short", "docket-17")
    assert stable_id("signal", *base) != stable_id(
        "signal", "epoch-3", *base[1:]
    )
    assert stable_id("signal", *base) != stable_id(
        "signal", base[0], base[1], "3m", *base[3:]
    )


def test_execution_records_reject_float_prices():
    try:
        MarketBar(
            ticker="AAPL",
            session=date(2026, 7, 31),
            open=100.0,
            high=Decimal("102"),
            low=Decimal("99"),
            close=Decimal("101"),
            source="yfinance",
            fetched_at=datetime(2026, 7, 31, 22, tzinfo=UTC),
            adjusted=False,
        )
    except TypeError as exc:
        assert "Decimal" in str(exc)
    else:
        raise AssertionError("float execution price was accepted")


def test_intent_and_fill_are_frozen():
    intent = OrderIntent(
        intent_id="intent-1",
        signal_ids=("signal-1",),
        cohort_id="horizon_30d_size_5k",
        side="buy",
        requested_qty=10,
        created_at=datetime(2026, 7, 31, 22, 5, tzinfo=UTC),
        eligible_session=date(2026, 8, 3),
        price_rule="next_session_open",
        status="pending",
        stop_price=None,
        external_order_id=None,
    )
    fill = Fill(
        fill_id="fill-1",
        intent_id=intent.intent_id,
        side=intent.side,
        session=intent.eligible_session,
        effective_at=datetime(2026, 8, 3, 13, 30, tzinfo=UTC),
        processed_at=datetime(2026, 8, 3, 22, tzinfo=UTC),
        reference_price=Decimal("100"),
        fill_price=Decimal("100.10"),
        quantity=10,
        slippage=Decimal("1.00"),
        commission=Decimal("0"),
        other_fees=Decimal("0"),
    )
    assert fill.intent_id == intent.intent_id
    try:
        fill.quantity = 11
    except Exception:
        pass
    else:
        raise AssertionError("Fill must be immutable")
