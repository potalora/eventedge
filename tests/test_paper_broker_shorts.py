from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from tradingagents.execution.paper_broker import PaperBroker
from tradingagents.strategies.execution import OrderIntent, SignalRecord
from tradingagents.strategies.execution.cost_model import PaperCostModel
from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger


UTC = timezone.utc
SESSION = date(2026, 8, 3)


def _signal(direction: str) -> SignalRecord:
    return SignalRecord(
        f"{direction}-signal",
        "epoch",
        "policy",
        f"{direction}-event",
        "litigation",
        "AAPL",
        direction,
        datetime(2026, 7, 31, 19, tzinfo=UTC),
        datetime(2026, 7, 31, 19, 30, tzinfo=UTC),
        date(2026, 7, 31),
        Decimal("100"),
        datetime(2026, 7, 31, 20, tzinfo=UTC),
        "evidence",
    )


def _intent(side: str, signal_id: str) -> OrderIntent:
    return OrderIntent(
        f"{side}-intent",
        (signal_id,),
        "cohort",
        side,
        10,
        datetime(2026, 7, 31, 20, tzinfo=UTC),
        SESSION,
        "next_session_open",
        "pending",
        None,
        None,
    )


def test_short_and_cover_positions_are_authoritative_ledger_views(tmp_path):
    ledger = PortfolioLedger(
        tmp_path / "ledger.db",
        "cohort",
        Decimal("50000"),
        short_selling_config={"borrow_cost_reject_above": "0.05"},
    )
    broker = PaperBroker(ledger)
    signal = _signal("short")
    short = _intent("short", signal.signal_id)
    try:
        ledger.record_signal(signal)
        ledger.stage_intent(short)
        short_fill = PaperCostModel().fill(
            short,
            Decimal("100"),
            datetime(2026, 8, 3, 13, 30, tzinfo=UTC),
            datetime(2026, 8, 3, 22, tzinfo=UTC),
        )
        result = broker.submit_short_sell(
            "AAPL",
            10,
            intent=short,
            fill=short_fill,
            borrow_rate=Decimal("0.02"),
        )
        assert result.status == "filled"
        assert broker.get_positions()[0]["side"] == "short"
        assert broker.get_account().buying_power < 50000

        cover = _intent("cover", signal.signal_id)
        ledger.stage_intent(cover)
        cover_fill = PaperCostModel().fill(
            cover,
            Decimal("90"),
            datetime(2026, 8, 3, 13, 31, tzinfo=UTC),
            datetime(2026, 8, 3, 22, tzinfo=UTC),
        )
        result = broker.submit_cover("AAPL", 10, intent=cover, fill=cover_fill)
        assert result.status == "filled"
        assert broker.get_positions() == []
        assert broker.get_account().portfolio_value > 50000
    finally:
        ledger.close()


def test_paper_broker_has_no_mutable_reconstruction_path(tmp_path):
    ledger = PortfolioLedger(tmp_path / "ledger.db", "cohort", Decimal("5000"))
    broker = PaperBroker(ledger)
    try:
        assert not hasattr(broker, "cash")
        assert not hasattr(broker, "positions")
        assert not hasattr(broker, "short_positions")
        assert not hasattr(broker, "reconstruct_from_trades")
        with pytest.raises(
            RuntimeError,
            match="PaperBroker direct price submission is disabled",
        ):
            broker.submit_short_sell("AAPL", 10, Decimal("100"))
    finally:
        ledger.close()
