from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from tradingagents.strategies.execution import MarketBar, OrderIntent
from tradingagents.strategies.execution.stop_execution import stop_reference


UTC = timezone.utc
SESSION = date(2026, 8, 3)


def _intent(side: str, stop: str | None, rule: str = "resting_stop") -> OrderIntent:
    return OrderIntent(
        f"{side}-{stop}-{rule}",
        ("signal",),
        "cohort",
        side,
        10,
        datetime(2026, 8, 1, 22, tzinfo=UTC),
        SESSION,
        rule,
        "pending",
        Decimal(stop) if stop else None,
        None,
    )


def _bar(open_: str, low: str, high: str) -> MarketBar:
    return MarketBar(
        "AAPL",
        SESSION,
        Decimal(open_),
        Decimal(high),
        Decimal(low),
        Decimal("100"),
        "fixture",
        datetime(2026, 8, 3, 22, tzinfo=UTC),
        False,
    )


def test_long_stop_uses_gap_open_then_trigger_price_or_remains_unfilled():
    intent = _intent("sell", "95")
    assert stop_reference(intent, _bar("90", "89", "91")) == Decimal("90")
    assert stop_reference(intent, _bar("100", "94", "101")) == Decimal("95")
    assert stop_reference(intent, _bar("100", "96", "101")) is None


def test_short_stop_mirrors_gap_and_intraday_trigger_behavior():
    intent = _intent("cover", "105")
    assert stop_reference(intent, _bar("110", "109", "111")) == Decimal("110")
    assert stop_reference(intent, _bar("100", "99", "106")) == Decimal("105")
    assert stop_reference(intent, _bar("100", "99", "104")) is None


def test_stops_reject_entry_sides_and_next_open_returns_open():
    assert stop_reference(
        _intent("buy", None, "next_session_open"), _bar("100", "99", "101")
    ) == Decimal("100")
    with pytest.raises(ValueError, match="exit intents"):
        stop_reference(_intent("buy", "95"), _bar("100", "94", "101"))
