from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tradingagents.execution.alpaca_broker import AlpacaBroker
from tradingagents.execution.paper_broker import PaperBroker
from tradingagents.strategies.execution import OrderIntent, SignalRecord
from tradingagents.strategies.execution.cost_model import PaperCostModel
from tradingagents.strategies.state.portfolio_ledger import (
    LedgerConflictError,
    PortfolioLedger,
)


UTC = timezone.utc
SESSION = date(2026, 8, 3)
DISABLED = (
    "PaperBroker direct price submission is disabled; stage and execute a ledger intent"
)


def _signal() -> SignalRecord:
    return SignalRecord(
        "signal",
        "epoch",
        "policy",
        "event",
        "litigation",
        "AAPL",
        "long",
        datetime(2026, 7, 31, 19, tzinfo=UTC),
        datetime(2026, 7, 31, 19, 30, tzinfo=UTC),
        date(2026, 7, 31),
        Decimal("100"),
        datetime(2026, 7, 31, 20, tzinfo=UTC),
        "evidence",
    )


def _intent() -> OrderIntent:
    return OrderIntent(
        "intent",
        ("signal",),
        "cohort",
        "buy",
        10,
        datetime(2026, 7, 31, 20, tzinfo=UTC),
        SESSION,
        "next_session_open",
        "pending",
        None,
        None,
    )


def test_paper_broker_reads_authoritative_ledger_and_disables_direct_prices(
    tmp_path,
):
    ledger = PortfolioLedger(tmp_path / "ledger.db", "cohort", Decimal("5000"))
    broker = PaperBroker(ledger)
    try:
        account = broker.get_account()
        assert account.cash == 5000.0
        assert account.portfolio_value == 5000.0
        assert broker.get_positions() == []

        with pytest.raises(RuntimeError, match=DISABLED):
            broker.submit_stock_order("AAPL", "buy", 10, price=Decimal("100"))
        with pytest.raises(RuntimeError, match=DISABLED):
            broker.submit_short_sell("AAPL", 10, Decimal("100"))
        with pytest.raises(RuntimeError, match=DISABLED):
            broker.submit_cover("AAPL", 10, Decimal("100"))
    finally:
        ledger.close()


def test_paper_broker_accepts_only_persisted_intent_and_fill_pair(tmp_path):
    ledger = PortfolioLedger(tmp_path / "ledger.db", "cohort", Decimal("5000"))
    broker = PaperBroker(ledger)
    signal = _signal()
    intent = _intent()
    try:
        ledger.record_signal(signal)
        ledger.stage_intent(intent)
        fill = PaperCostModel().fill(
            intent,
            Decimal("100"),
            datetime(2026, 8, 3, 13, 30, tzinfo=UTC),
            datetime(2026, 8, 3, 22, tzinfo=UTC),
        )
        with pytest.raises(RuntimeError, match=DISABLED):
            broker.submit_stock_order(
                "AAPL",
                "buy",
                10,
                price=Decimal("100"),
                intent=intent,
                fill=fill,
            )
        assert ledger.read_fills() == []
        result = broker.submit_stock_order("AAPL", "buy", 10, intent=intent, fill=fill)
        assert result.status == "filled"
        assert result.order_id == intent.intent_id
        assert broker.get_positions() == [
            {
                "ticker": "AAPL",
                "quantity": 10,
                "avg_price": pytest.approx(100.1),
                "instrument_type": "stock",
                "side": "long",
            }
        ]
        assert ledger.read_fills() == [fill]
    finally:
        ledger.close()


@patch("tradingagents.execution.alpaca_broker.TimeInForce", new_callable=MagicMock)
@patch(
    "tradingagents.execution.alpaca_broker.MarketOrderRequest",
    new_callable=MagicMock,
)
@patch("tradingagents.execution.alpaca_broker.OrderSide", new_callable=MagicMock)
@patch("tradingagents.execution.alpaca_broker.TradingClient")
def test_alpaca_uses_intent_id_and_persists_unfilled_external_order(
    client_class, order_side, market_request, time_in_force, tmp_path
):
    del order_side, time_in_force
    client = MagicMock()
    client.submit_order.return_value = SimpleNamespace(
        id="external-1",
        status="accepted",
        filled_qty="0",
        filled_avg_price=None,
    )
    client_class.return_value = client
    ledger = PortfolioLedger(tmp_path / "ledger.db", "cohort", Decimal("5000"))
    signal = _signal()
    intent = _intent()
    try:
        ledger.record_signal(signal)
        ledger.stage_intent(intent)
        broker = AlpacaBroker("key", "secret", paper=True, ledger=ledger)
        result = broker.submit_stock_order(
            "AAPL", "buy", 10, client_order_id=intent.intent_id
        )

        assert result.status == "accepted"
        market_request.assert_called_once_with(
            symbol="AAPL",
            qty=10,
            side=market_request.call_args.kwargs["side"],
            time_in_force=market_request.call_args.kwargs["time_in_force"],
            client_order_id=intent.intent_id,
        )
        external = ledger.external_order_for_intent(intent.intent_id)
        assert external is not None
        assert external["external_order_id"] == "external-1"
        assert external["status"] == "accepted"
        assert ledger.read_fills() == []
        submitted_at = datetime.fromisoformat(str(external["submitted_at"]))
        with pytest.raises(LedgerConflictError, match="cannot regress"):
            ledger.record_external_order(
                intent_id=intent.intent_id,
                external_order_id="external-1",
                broker="alpaca",
                status="pending",
                submitted_at=submitted_at,
                reconciled_at=submitted_at,
                detail="invalid status regression",
            )
    finally:
        ledger.close()


@patch("tradingagents.execution.alpaca_broker.TimeInForce", new_callable=MagicMock)
@patch(
    "tradingagents.execution.alpaca_broker.MarketOrderRequest",
    new_callable=MagicMock,
)
@patch("tradingagents.execution.alpaca_broker.OrderSide", new_callable=MagicMock)
@patch("tradingagents.execution.alpaca_broker.TradingClient")
def test_alpaca_reconciles_unresolved_external_order_before_any_resubmit(
    client_class, order_side, market_request, time_in_force, tmp_path
):
    del order_side, market_request, time_in_force
    client = MagicMock()
    accepted = SimpleNamespace(
        id="external-1",
        status="accepted",
        filled_qty="0",
        filled_avg_price=None,
    )
    partial = SimpleNamespace(
        id="external-1",
        status="partially_filled",
        filled_qty="4",
        filled_avg_price="100.05",
    )
    client.submit_order.return_value = accepted
    client.get_order_by_client_id.return_value = partial
    client_class.return_value = client
    ledger = PortfolioLedger(tmp_path / "ledger.db", "cohort", Decimal("5000"))
    signal = _signal()
    intent = _intent()
    try:
        ledger.record_signal(signal)
        ledger.stage_intent(intent)
        broker = AlpacaBroker("key", "secret", paper=True, ledger=ledger)
        first = broker.submit_stock_order(
            "AAPL", "buy", 10, client_order_id=intent.intent_id
        )
        second = broker.submit_stock_order(
            "AAPL", "buy", 10, client_order_id=intent.intent_id
        )

        assert first.status == "accepted"
        assert second.status == "partially_filled"
        assert client.submit_order.call_count == 1
        client.get_order_by_client_id.assert_called_once_with(intent.intent_id)
        assert (
            ledger.external_order_for_intent(intent.intent_id)["status"]
            == "partially_filled"
        )
        assert ledger.read_fills() == []
    finally:
        ledger.close()


@patch("tradingagents.execution.alpaca_broker.TimeInForce", new_callable=MagicMock)
@patch(
    "tradingagents.execution.alpaca_broker.MarketOrderRequest",
    new_callable=MagicMock,
)
@patch("tradingagents.execution.alpaca_broker.OrderSide", new_callable=MagicMock)
@patch("tradingagents.execution.alpaca_broker.TradingClient")
def test_alpaca_applies_prebuilt_fill_only_after_reconciliation_confirms_full_fill(
    client_class, order_side, market_request, time_in_force, tmp_path
):
    del order_side, market_request, time_in_force
    client = MagicMock()
    accepted = SimpleNamespace(
        id="external-1",
        status="new",
        filled_qty="0",
        filled_avg_price=None,
    )
    full = SimpleNamespace(
        id="external-1",
        status="filled",
        filled_qty="10",
        filled_avg_price="100.1",
    )
    client.submit_order.return_value = accepted
    client.get_order_by_client_id.return_value = full
    client_class.return_value = client
    ledger = PortfolioLedger(tmp_path / "ledger.db", "cohort", Decimal("5000"))
    signal = _signal()
    intent = _intent()
    try:
        ledger.record_signal(signal)
        ledger.stage_intent(intent)
        fill = PaperCostModel().fill(
            intent,
            Decimal("100"),
            datetime(2026, 8, 3, 13, 30, tzinfo=UTC),
            datetime(2026, 8, 3, 22, tzinfo=UTC),
        )
        broker = AlpacaBroker("key", "secret", paper=True, ledger=ledger)
        first = broker.submit_stock_order(
            "AAPL",
            "buy",
            10,
            client_order_id=intent.intent_id,
            intent=intent,
            fill=fill,
        )
        assert first.status == "pending"
        assert ledger.read_fills() == []

        second = broker.submit_stock_order(
            "AAPL",
            "buy",
            10,
            client_order_id=intent.intent_id,
            intent=intent,
            fill=fill,
        )
        assert second.status == "filled"
        assert client.submit_order.call_count == 1
        assert ledger.read_fills() == [fill]
    finally:
        ledger.close()


@patch("tradingagents.execution.alpaca_broker.TimeInForce", new_callable=MagicMock)
@patch(
    "tradingagents.execution.alpaca_broker.MarketOrderRequest",
    new_callable=MagicMock,
)
@patch("tradingagents.execution.alpaca_broker.OrderSide", new_callable=MagicMock)
@patch("tradingagents.execution.alpaca_broker.TradingClient")
def test_alpaca_reconciles_pre_submit_pending_row_after_crash_before_resubmit(
    client_class, order_side, market_request, time_in_force, tmp_path
):
    del order_side, market_request, time_in_force
    client = MagicMock()
    client.get_order_by_client_id.return_value = SimpleNamespace(
        id="external-1",
        status="accepted",
        filled_qty="0",
        filled_avg_price=None,
    )
    client_class.return_value = client
    ledger = PortfolioLedger(tmp_path / "ledger.db", "cohort", Decimal("5000"))
    signal = _signal()
    intent = _intent()
    prepared_at = datetime(2026, 7, 31, 0, tzinfo=UTC)
    try:
        ledger.record_signal(signal)
        ledger.stage_intent(intent)
        ledger.prepare_external_order(
            intent_id=intent.intent_id,
            broker="alpaca",
            submitted_at=prepared_at,
        )
        broker = AlpacaBroker("key", "secret", paper=True, ledger=ledger)
        result = broker.submit_stock_order(
            "AAPL", "buy", 10, client_order_id=intent.intent_id
        )
        assert result.status == "accepted"
        client.get_order_by_client_id.assert_called_once_with(intent.intent_id)
        client.submit_order.assert_not_called()
        external = ledger.external_order_for_intent(intent.intent_id)
        assert external["external_order_id"] == "external-1"
        assert external["status"] == "accepted"
    finally:
        ledger.close()


@patch("tradingagents.execution.alpaca_broker.TimeInForce", new_callable=MagicMock)
@patch(
    "tradingagents.execution.alpaca_broker.MarketOrderRequest",
    new_callable=MagicMock,
)
@patch("tradingagents.execution.alpaca_broker.OrderSide", new_callable=MagicMock)
@patch("tradingagents.execution.alpaca_broker.TradingClient")
def test_alpaca_submits_once_when_pre_submit_row_reconciles_as_not_found(
    client_class, order_side, market_request, time_in_force, tmp_path
):
    del order_side, market_request, time_in_force
    call_order = []
    not_found = RuntimeError("broker lookup failed")
    not_found.status_code = 404
    client = MagicMock()

    def reconcile(_client_order_id):
        call_order.append("reconcile")
        raise not_found

    def submit(_request):
        call_order.append("submit")
        return SimpleNamespace(
            id="external-1",
            status="accepted",
            filled_qty="0",
            filled_avg_price=None,
        )

    client.get_order_by_client_id.side_effect = reconcile
    client.submit_order.side_effect = submit
    client_class.return_value = client
    ledger = PortfolioLedger(tmp_path / "ledger.db", "cohort", Decimal("5000"))
    signal = _signal()
    intent = _intent()
    prepared_at = datetime(2026, 7, 31, 0, tzinfo=UTC)
    try:
        ledger.record_signal(signal)
        ledger.stage_intent(intent)
        ledger.prepare_external_order(
            intent_id=intent.intent_id,
            broker="alpaca",
            submitted_at=prepared_at,
        )
        broker = AlpacaBroker("key", "secret", paper=True, ledger=ledger)

        result = broker.submit_stock_order(
            "AAPL", "buy", 10, client_order_id=intent.intent_id
        )

        assert result.status == "accepted"
        assert call_order == ["reconcile", "submit"]
        client.get_order_by_client_id.assert_called_once_with(intent.intent_id)
        client.submit_order.assert_called_once()
        external = ledger.external_order_for_intent(intent.intent_id)
        assert external["external_order_id"] == "external-1"
        assert external["status"] == "accepted"
        assert external["submitted_at"] == prepared_at.isoformat()
    finally:
        ledger.close()
