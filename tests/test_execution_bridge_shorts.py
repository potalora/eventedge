from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from tradingagents.strategies.execution import (
    MarketBar,
    OrderIntent,
    SignalRecord,
)
from tradingagents.strategies.execution.cost_model import PaperCostModel
from tradingagents.strategies.state.portfolio_ledger import (
    LedgerConflictError,
    PortfolioLedger,
)
from tradingagents.strategies.trading.execution_bridge import ExecutionBridge
from tradingagents.strategies.trading.portfolio_committee import TradeRecommendation


UTC = timezone.utc
FRIDAY = date(2026, 7, 31)
MONDAY = date(2026, 8, 3)


def _signal(
    *,
    signal_id: str = "signal",
    direction: str = "long",
    event_at: datetime | None = datetime(2026, 7, 31, 19, tzinfo=UTC),
    observed_at: datetime = datetime(2026, 7, 31, 19, 30, tzinfo=UTC),
    decision_at: datetime = datetime(2026, 7, 31, 20, tzinfo=UTC),
) -> SignalRecord:
    return SignalRecord(
        signal_id,
        "epoch",
        "policy",
        f"event-{signal_id}",
        "litigation",
        "AAPL",
        direction,
        event_at,
        observed_at,
        FRIDAY,
        Decimal("100"),
        decision_at,
        f"evidence-{signal_id}",
    )


def _recommendation(direction: str = "long") -> TradeRecommendation:
    return TradeRecommendation(
        "AAPL",
        direction,
        0.10,
        0.8,
        "test",
        ["litigation"],
    )


def _bar(
    session: date = MONDAY,
    *,
    open_: str = "101",
    low: str = "99",
    high: str = "103",
    adjusted: bool = False,
) -> MarketBar:
    return MarketBar(
        "AAPL",
        session,
        Decimal(open_),
        Decimal(high),
        Decimal(low),
        Decimal("102"),
        "fixture",
        datetime(2026, 8, 3, 22, tzinfo=UTC),
        adjusted,
    )


def _bridge(tmp_path, *, long_only: bool = False):
    config = {
        "execution": {"mode": "paper"},
        "autoresearch": {
            "total_capital": 5000,
            "risk_gate": {
                "long_only": long_only,
                "min_position_value": 1,
                "max_position_pct": 0.20,
            },
            "short_selling": {"borrow_cost_reject_above": "0.05"},
        },
    }
    ledger = PortfolioLedger(
        tmp_path / "ledger.db",
        "cohort",
        Decimal("5000"),
        short_selling_config=config["autoresearch"]["short_selling"],
    )
    return ExecutionBridge(config, ledger=ledger), ledger


def test_friday_close_recommendation_stages_monday_intent_without_mutation(tmp_path):
    bridge, ledger = _bridge(tmp_path)
    signal = _signal()
    try:
        ledger.record_signal(signal)
        before = ledger.account_state()
        intent = bridge.stage_intent(
            _recommendation(),
            (signal,),
            before,
            signal.decision_at,
            MONDAY,
        )

        assert intent.created_at == signal.decision_at
        assert intent.eligible_session == MONDAY
        assert intent.price_rule == "next_session_open"
        assert intent.status == "pending"
        assert intent.requested_qty == 5
        assert ledger.pending_intents(MONDAY) == [intent]
        assert ledger.account_state() == before
        assert ledger.read_fills() == []
    finally:
        ledger.close()


@pytest.mark.parametrize(
    "signal",
    [
        _signal(
            signal_id="late-event",
            event_at=datetime(2026, 7, 31, 20, 1, tzinfo=UTC),
        ),
        _signal(
            signal_id="late-observation",
            observed_at=datetime(2026, 7, 31, 20, 1, tzinfo=UTC),
        ),
        _signal(
            signal_id="late-decision",
            decision_at=datetime(2026, 7, 31, 20, 1, tzinfo=UTC),
        ),
    ],
)
def test_cutoff_late_signal_cannot_create_intent(tmp_path, signal):
    bridge, ledger = _bridge(tmp_path)
    try:
        ledger.record_signal(signal)
        with pytest.raises(ValueError, match="session cutoff"):
            bridge.stage_intent(
                _recommendation(),
                (signal,),
                ledger.account_state(),
                signal.decision_at,
                MONDAY,
            )
        assert ledger.pending_intents(MONDAY) == []
    finally:
        ledger.close()


def test_intent_decision_after_reference_session_cutoff_is_rejected(tmp_path):
    bridge, ledger = _bridge(tmp_path)
    signal = _signal()
    try:
        ledger.record_signal(signal)
        with pytest.raises(ValueError, match="session cutoff"):
            bridge.stage_intent(
                _recommendation(),
                (signal,),
                ledger.account_state(),
                datetime(2026, 7, 31, 20, 1, tzinfo=UTC),
                MONDAY,
            )
        assert ledger.pending_intents(MONDAY) == []
    finally:
        ledger.close()


def test_due_intent_fills_only_at_raw_monday_open(tmp_path):
    bridge, ledger = _bridge(tmp_path)
    signal = _signal()
    try:
        ledger.record_signal(signal)
        before = ledger.account_state()
        intent = bridge.stage_intent(
            _recommendation(), (signal,), before, signal.decision_at, MONDAY
        )
        result = bridge.execute_due_intent(
            intent,
            _bar(),
            before,
            {
                "strategy": "litigation",
                "processing_at": datetime(2026, 8, 3, 22, tzinfo=UTC),
            },
            PaperCostModel(),
        )

        assert result.status == "filled"
        assert result.fill is not None
        assert result.fill.reference_price == Decimal("101")
        assert result.fill.fill_price == Decimal("101.101")
        assert ledger.read_fills() == [result.fill]
        assert ledger.account_state().cash == Decimal("4494.4950")
    finally:
        ledger.close()


def test_due_intent_rejects_adjusted_or_wrong_session_bar(tmp_path):
    bridge, ledger = _bridge(tmp_path)
    signal = _signal()
    try:
        ledger.record_signal(signal)
        before = ledger.account_state()
        intent = bridge.stage_intent(
            _recommendation(), (signal,), before, signal.decision_at, MONDAY
        )
        with pytest.raises(ValueError, match="raw"):
            bridge.execute_due_intent(
                intent,
                _bar(adjusted=True),
                before,
                {"processing_at": datetime(2026, 8, 3, 22, tzinfo=UTC)},
                PaperCostModel(),
            )
        with pytest.raises(ValueError, match="eligible session"):
            bridge.execute_due_intent(
                intent,
                _bar(date(2026, 8, 4)),
                before,
                {"processing_at": datetime(2026, 8, 4, 22, tzinfo=UTC)},
                PaperCostModel(),
            )
        assert ledger.read_fills() == []
    finally:
        ledger.close()


def test_unknown_borrow_short_is_rejected_before_fill(tmp_path):
    bridge, ledger = _bridge(tmp_path)
    signal = _signal(direction="short")
    try:
        ledger.record_signal(signal)
        before = ledger.account_state()
        intent = bridge.stage_intent(
            _recommendation("short"),
            (signal,),
            before,
            signal.decision_at,
            MONDAY,
        )
        result = bridge.execute_due_intent(
            intent,
            _bar(),
            before,
            {
                "strategy": "litigation",
                "borrow_rate": None,
                "processing_at": datetime(2026, 8, 3, 22, tzinfo=UTC),
            },
            PaperCostModel(),
        )
        assert result.status == "rejected"
        assert "borrow" in result.reason
        assert result.fill is None
        assert ledger.read_fills() == []
        assert ledger.account_state() == before
        assert ledger.intent(intent.intent_id).status == "rejected"
        transition = ledger.connection.execute(
            "SELECT status, reason FROM order_status_transitions WHERE intent_id = ?",
            (intent.intent_id,),
        ).fetchone()
        assert transition["status"] == "rejected"
        assert "borrow" in transition["reason"]
    finally:
        ledger.close()


def test_reject_intent_conflicting_replay_raises(tmp_path):
    _, ledger = _bridge(tmp_path)
    signal = _signal()
    intent = OrderIntent(
        "rejection-replay",
        (signal.signal_id,),
        "cohort",
        "buy",
        5,
        signal.decision_at,
        MONDAY,
        "next_session_open",
        "pending",
        None,
        None,
    )
    rejected_at = datetime(2026, 8, 3, 22, tzinfo=UTC)
    try:
        ledger.record_signal(signal)
        ledger.stage_intent(intent)
        first = ledger.reject_intent(
            intent.intent_id, rejected_at, "risk gate rejected"
        )

        assert (
            ledger.reject_intent(intent.intent_id, rejected_at, "risk gate rejected")
            == first
        )
        with pytest.raises(LedgerConflictError, match="conflicting rejection replay"):
            ledger.reject_intent(
                intent.intent_id,
                rejected_at + timedelta(seconds=1),
                "different rejection",
            )
    finally:
        ledger.close()


def test_resting_stop_survives_eligible_day_and_fills_on_later_raw_bar(tmp_path):
    bridge, ledger = _bridge(tmp_path)
    signal = _signal()
    try:
        ledger.record_signal(signal)
        entry = bridge.stage_intent(
            _recommendation(),
            (signal,),
            ledger.account_state(),
            signal.decision_at,
            MONDAY,
        )
        entry_result = bridge.execute_due_intent(
            entry,
            _bar(open_="100"),
            ledger.account_state(),
            {
                "strategy": "litigation",
                "processing_at": datetime(2026, 8, 3, 22, tzinfo=UTC),
            },
            PaperCostModel(),
        )
        assert entry_result.status == "filled"

        stop = OrderIntent(
            "resting-stop",
            (signal.signal_id,),
            "cohort",
            "sell",
            entry.requested_qty,
            signal.decision_at,
            MONDAY,
            "resting_stop",
            "pending",
            Decimal("95"),
            None,
        )
        ledger.stage_intent(stop)
        not_triggered = bridge.execute_due_intent(
            stop,
            _bar(open_="100", low="96", high="103"),
            ledger.account_state(),
            {"processing_at": datetime(2026, 8, 3, 22, tzinfo=UTC)},
            PaperCostModel(),
        )
        assert not_triggered.status == "pending"
        assert ledger.pending_intents(MONDAY) == [stop]

        tuesday = date(2026, 8, 4)
        triggered_bar = MarketBar(
            "AAPL",
            tuesday,
            Decimal("90"),
            Decimal("92"),
            Decimal("89"),
            Decimal("91"),
            "fixture",
            datetime(2026, 8, 4, 22, tzinfo=UTC),
            False,
        )
        triggered = bridge.execute_due_intent(
            stop,
            triggered_bar,
            ledger.account_state(),
            {"processing_at": datetime(2026, 8, 4, 22, tzinfo=UTC)},
            PaperCostModel(),
        )
        assert triggered.status == "filled"
        assert triggered.fill is not None
        assert triggered.fill.reference_price == Decimal("90")
        assert bridge.get_positions() == []
    finally:
        ledger.close()


def test_terminal_intent_is_rejected_as_terminal_on_replay(tmp_path):
    bridge, ledger = _bridge(tmp_path)
    signal = _signal()
    try:
        ledger.record_signal(signal)
        intent = bridge.stage_intent(
            _recommendation(),
            (signal,),
            ledger.account_state(),
            signal.decision_at,
            MONDAY,
        )
        bridge.execute_due_intent(
            intent,
            _bar(),
            ledger.account_state(),
            {"processing_at": datetime(2026, 8, 3, 22, tzinfo=UTC)},
            PaperCostModel(),
        )
        with pytest.raises(ValueError, match="already terminal"):
            bridge.execute_due_intent(
                intent,
                _bar(),
                ledger.account_state(),
                {"processing_at": datetime(2026, 8, 3, 22, tzinfo=UTC)},
                PaperCostModel(),
            )
    finally:
        ledger.close()


@pytest.mark.parametrize(
    ("fetched_at", "processing_at", "message"),
    [
        (
            datetime(2026, 8, 3, 23, tzinfo=UTC),
            datetime(2026, 8, 3, 22, tzinfo=UTC),
            "future",
        ),
        (
            datetime(2026, 8, 2, 19, tzinfo=UTC),
            datetime(2026, 8, 3, 22, tzinfo=UTC),
            "stale",
        ),
        (
            datetime(2026, 8, 3, 19, tzinfo=UTC),
            datetime(2026, 8, 3, 22, tzinfo=UTC),
            "before session close",
        ),
    ],
)
def test_execution_rejects_future_or_stale_bar_against_independent_processing_cutoff(
    tmp_path, fetched_at, processing_at, message
):
    bridge, ledger = _bridge(tmp_path)
    signal = _signal()
    try:
        ledger.record_signal(signal)
        intent = bridge.stage_intent(
            _recommendation(),
            (signal,),
            ledger.account_state(),
            signal.decision_at,
            MONDAY,
        )
        bar = MarketBar(
            "AAPL",
            MONDAY,
            Decimal("101"),
            Decimal("103"),
            Decimal("99"),
            Decimal("102"),
            "fixture",
            fetched_at,
            False,
        )
        with pytest.raises(ValueError, match=message):
            bridge.execute_due_intent(
                intent,
                bar,
                ledger.account_state(),
                {"processing_at": processing_at},
                PaperCostModel(),
            )
        assert ledger.intent(intent.intent_id).status == "pending"
        assert ledger.read_fills() == []
    finally:
        ledger.close()


def test_prior_pending_entry_reserves_current_opening_buying_power(tmp_path):
    bridge, ledger = _bridge(tmp_path)
    bridge.risk_gate.config.max_position_pct = 1.0
    signal = _signal()
    prior = OrderIntent(
        "z-prior",
        (signal.signal_id,),
        "cohort",
        "buy",
        30,
        signal.decision_at,
        MONDAY,
        "next_session_open",
        "pending",
        None,
        None,
    )
    current = OrderIntent(
        "a-current",
        (signal.signal_id,),
        "cohort",
        "buy",
        30,
        signal.decision_at + timedelta(minutes=1),
        MONDAY,
        "next_session_open",
        "pending",
        None,
        None,
    )
    try:
        ledger.record_signal(signal)
        ledger.stage_intent(prior)
        ledger.stage_intent(current)
        result = bridge.execute_due_intent(
            current,
            _bar(open_="100"),
            ledger.account_state(),
            {
                "processing_at": datetime(2026, 8, 3, 22, tzinfo=UTC),
                "opening_prices": {"AAPL": Decimal("100")},
            },
            PaperCostModel(),
        )
        assert result.status == "rejected"
        assert "pending buying power" in result.reason
        assert ledger.intent(current.intent_id).status == "rejected"
        assert ledger.intent(prior.intent_id).status == "pending"
        assert ledger.read_fills() == []
    finally:
        ledger.close()
