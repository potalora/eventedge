"""Deterministic daily-bar references for persisted resting stop intents."""

from __future__ import annotations

from decimal import Decimal

from .models import MarketBar, OrderIntent


def stop_reference(intent: OrderIntent, bar: MarketBar) -> Decimal | None:
    stop = intent.stop_price
    if intent.price_rule != "resting_stop" or stop is None:
        return bar.open if intent.price_rule == "next_session_open" else None
    if intent.side == "sell":
        if bar.open <= stop:
            return bar.open
        return stop if bar.low <= stop else None
    if intent.side == "cover":
        if bar.open >= stop:
            return bar.open
        return stop if bar.high >= stop else None
    raise ValueError("resting stops are exit intents")
