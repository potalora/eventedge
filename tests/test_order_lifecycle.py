from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from tradingagents.strategies.execution import MarketBar, OrderIntent, SignalRecord
from tradingagents.strategies.execution.cost_model import PaperCostModel
from tradingagents.strategies.execution.stop_execution import stop_reference
from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger


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


def test_resting_stop_survives_missed_eligible_session_then_fills_and_replays_on_execution_session(
    tmp_path,
):
    ledger = PortfolioLedger(tmp_path / "ledger.db", "cohort", Decimal("5000"))
    signal_at = datetime(2026, 8, 1, 22, tzinfo=UTC)
    signal = SignalRecord(
        "signal",
        "epoch",
        "policy",
        "event",
        "test",
        "AAPL",
        "long",
        signal_at,
        signal_at,
        date(2026, 8, 1),
        Decimal("100"),
        signal_at,
        "evidence",
    )
    intent = _intent("sell", "95")
    ledger.record_signal(signal)
    entry = OrderIntent(
        "entry",
        ("signal",),
        "cohort",
        "buy",
        10,
        signal_at,
        SESSION,
        "next_session_open",
        "pending",
        None,
        None,
    )
    try:
        model = PaperCostModel()
        ledger.stage_intent(entry)
        ledger.apply_fill(
            entry,
            model.fill(
                entry,
                Decimal("100"),
                datetime(2026, 8, 3, 13, 30, tzinfo=UTC),
                datetime(2026, 8, 3, 22, tzinfo=UTC),
            ),
        )
        ledger.stage_intent(intent)
        assert ledger.pending_intents(SESSION) == [intent]
        next_session = SESSION + timedelta(days=1)
        assert ledger.pending_intents(next_session) == [intent]
        bar = MarketBar(
            "AAPL",
            next_session,
            Decimal("90"),
            Decimal("91"),
            Decimal("89"),
            Decimal("90"),
            "fixture",
            datetime(2026, 8, 4, 22, tzinfo=UTC),
            False,
        )
        assert stop_reference(intent, bar) == Decimal("90")
        fill = model.fill(
            intent,
            Decimal("90"),
            datetime(2026, 8, 4, 13, 30, tzinfo=UTC),
            datetime(2026, 8, 4, 22, tzinfo=UTC),
        )
        assert fill.session == next_session
        assert (
            fill.fill_id
            != model.fill(
                intent,
                Decimal("90"),
                datetime(2026, 8, 3, 13, 30, tzinfo=UTC),
                datetime(2026, 8, 3, 22, tzinfo=UTC),
            ).fill_id
        )
        state = ledger.apply_fill(intent, fill)
        assert ledger.apply_fill(intent, fill) == state
        assert ledger.pending_intents(next_session) == []
    finally:
        ledger.close()
