from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

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
    strategy: str = "litigation",
    ticker: str = "AAPL",
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
        strategy,
        ticker,
        direction,
        event_at,
        observed_at,
        FRIDAY,
        Decimal("100"),
        decision_at,
        f"evidence-{signal_id}",
    )


def _recommendation(
    direction: str = "long",
    *,
    ticker: str = "AAPL",
    contributors: list[str] | None = None,
) -> TradeRecommendation:
    return TradeRecommendation(
        ticker,
        direction,
        0.10,
        0.8,
        "test",
        contributors or ["litigation"],
    )


def _bar(
    session: date = MONDAY,
    *,
    ticker: str = "AAPL",
    open_: str = "101",
    low: str = "99",
    high: str = "103",
    adjusted: bool = False,
) -> MarketBar:
    return MarketBar(
        ticker,
        session,
        Decimal(open_),
        Decimal(high),
        Decimal(low),
        Decimal("102"),
        "fixture",
        datetime(2026, 8, 3, 22, tzinfo=UTC),
        adjusted,
    )


def _bridge(
    tmp_path,
    *,
    long_only: bool = False,
    risk_overrides: dict | None = None,
):
    risk_config = {
        "long_only": long_only,
        "min_position_value": 1,
        "max_position_pct": 0.20,
    }
    risk_config.update(risk_overrides or {})
    config = {
        "execution": {"mode": "paper"},
        "autoresearch": {
            "total_capital": 5000,
            "risk_gate": risk_config,
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


def test_mixed_direction_exact_contributors_stage_winning_direction(tmp_path):
    bridge, ledger = _bridge(tmp_path)
    long_signal = _signal(signal_id="long", strategy="litigation")
    short_signal = _signal(
        signal_id="short",
        strategy="congressional_trades",
        direction="short",
    )
    recommendation = TradeRecommendation(
        "AAPL",
        "long",
        0.10,
        0.8,
        "majority long",
        ["litigation", "congressional_trades"],
    )
    try:
        ledger.record_signal(long_signal)
        ledger.record_signal(short_signal)

        intent = bridge.stage_intent(
            recommendation,
            (short_signal, long_signal),
            ledger.account_state(),
            long_signal.decision_at,
            MONDAY,
        )

        assert intent.side == "buy"
        assert intent.signal_ids == ("long", "short")
        assert ledger.signals_for_intent(intent.intent_id) == (
            long_signal,
            short_signal,
        )
    finally:
        ledger.close()


@pytest.mark.parametrize(
    "contributors",
    [
        ["litigation"],
        ["litigation", "earnings_call"],
    ],
)
def test_stage_intent_rejects_missing_or_substituted_contributors(
    tmp_path, contributors
):
    bridge, ledger = _bridge(tmp_path)
    first = _signal(signal_id="first", strategy="litigation")
    second = _signal(signal_id="second", strategy="congressional_trades")
    recommendation = TradeRecommendation(
        "AAPL", "long", 0.10, 0.8, "invalid provenance", contributors
    )
    try:
        ledger.record_signal(first)
        ledger.record_signal(second)

        with pytest.raises(ValueError, match="contributing strategies"):
            bridge.stage_intent(
                recommendation,
                (first, second),
                ledger.account_state(),
                first.decision_at,
                MONDAY,
            )
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
    current_signal = _signal()
    prior_signal = _signal(
        signal_id="prior-signal", strategy="supply_chain", ticker="MSFT"
    )
    prior = OrderIntent(
        "z-prior",
        (prior_signal.signal_id,),
        "cohort",
        "buy",
        30,
        prior_signal.decision_at,
        MONDAY,
        "next_session_open",
        "pending",
        None,
        None,
    )
    current = OrderIntent(
        "a-current",
        (current_signal.signal_id,),
        "cohort",
        "buy",
        30,
        current_signal.decision_at + timedelta(minutes=1),
        MONDAY,
        "next_session_open",
        "pending",
        None,
        None,
    )
    try:
        ledger.record_signal(prior_signal)
        ledger.record_signal(current_signal)
        ledger.stage_intent(prior)
        ledger.stage_intent(current)
        result = bridge.execute_due_intent(
            current,
            _bar(open_="100"),
            ledger.account_state(),
            {
                "processing_at": datetime(2026, 8, 3, 22, tzinfo=UTC),
                "opening_prices": {
                    "AAPL": Decimal("100"),
                    "MSFT": Decimal("100"),
                },
            },
            PaperCostModel(),
        )
        assert result.status == "rejected"
        assert "buying_power" in result.reason
        assert "after pending entries" in result.reason
        assert ledger.intent(current.intent_id).status == "rejected"
        assert ledger.intent(prior.intent_id).status == "pending"
        assert ledger.read_fills() == []
    finally:
        ledger.close()


def test_restart_uses_authoritative_high_water_mark_for_drawdown(tmp_path):
    bridge, ledger = _bridge(
        tmp_path,
        risk_overrides={"max_drawdown_pct": 0.10},
    )
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
        ledger.connection.execute(
            "UPDATE accounting_state SET high_water_mark = '6000' "
            "WHERE cohort_id = 'cohort'"
        )
        restarted = ExecutionBridge(bridge.config, ledger=ledger)

        result = restarted.execute_due_intent(
            intent,
            _bar(),
            ledger.account_state(),
            {"processing_at": datetime(2026, 8, 3, 22, tzinfo=UTC)},
            PaperCostModel(),
        )

        assert result.status == "rejected"
        assert "max_drawdown" in result.reason
    finally:
        ledger.close()


def test_restart_rejects_second_short_from_authoritative_margin(tmp_path):
    bridge, ledger = _bridge(
        tmp_path,
        risk_overrides={
            "long_only": False,
            "max_margin_utilization_pct": 0.25,
        },
    )
    first_signal = _signal(direction="short")
    second_signal = _signal(
        signal_id="second-short",
        strategy="supply_chain",
        ticker="MSFT",
        direction="short",
    )
    try:
        ledger.record_signal(first_signal)
        first = bridge.stage_intent(
            _recommendation("short"),
            (first_signal,),
            ledger.account_state(),
            first_signal.decision_at,
            MONDAY,
        )
        filled = bridge.execute_due_intent(
            first,
            _bar(open_="100"),
            ledger.account_state(),
            {
                "borrow_rate": Decimal("0.02"),
                "processing_at": datetime(2026, 8, 3, 22, tzinfo=UTC),
            },
            PaperCostModel(),
        )
        assert filled.status == "filled"

        restarted = ExecutionBridge(bridge.config, ledger=ledger)
        ledger.record_signal(second_signal)
        second = restarted.stage_intent(
            _recommendation("short", ticker="MSFT", contributors=["supply_chain"]),
            (second_signal,),
            ledger.account_state(),
            second_signal.decision_at,
            MONDAY,
        )
        result = restarted.execute_due_intent(
            second,
            _bar(ticker="MSFT", open_="100"),
            ledger.account_state(),
            {
                "borrow_rate": Decimal("0.02"),
                "processing_at": datetime(2026, 8, 3, 22, tzinfo=UTC),
            },
            PaperCostModel(),
        )

        assert result.status == "rejected"
        assert "margin_utilization" in result.reason
    finally:
        ledger.close()


@pytest.mark.parametrize(
    ("risk_overrides", "prior_ticker", "current_ticker", "expected"),
    [
        ({"max_positions": 1}, "MSFT", "AAPL", "max_positions"),
        ({}, "AAPL", "AAPL", "duplicate"),
        ({"per_strategy_max": 1}, "MSFT", "AAPL", "per_strategy_max"),
    ],
)
def test_prior_pending_entries_participate_in_portfolio_constraints(
    tmp_path, risk_overrides, prior_ticker, current_ticker, expected
):
    bridge, ledger = _bridge(tmp_path, risk_overrides=risk_overrides)
    prior_signal = _signal(
        signal_id="prior",
        ticker=prior_ticker,
        decision_at=datetime(2026, 7, 31, 19, 59, tzinfo=UTC),
    )
    current_signal = _signal(signal_id="current", ticker=current_ticker)
    try:
        ledger.record_signal(prior_signal)
        ledger.record_signal(current_signal)
        prior = bridge.stage_intent(
            _recommendation(ticker=prior_ticker),
            (prior_signal,),
            ledger.account_state(),
            prior_signal.decision_at,
            MONDAY,
        )
        current = bridge.stage_intent(
            _recommendation(ticker=current_ticker),
            (current_signal,),
            ledger.account_state(),
            current_signal.decision_at,
            MONDAY,
        )

        result = bridge.execute_due_intent(
            current,
            _bar(ticker=current_ticker),
            ledger.account_state(),
            {
                "processing_at": datetime(2026, 8, 3, 22, tzinfo=UTC),
                "opening_prices": {prior_ticker: Decimal("101")},
            },
            PaperCostModel(),
        )

        assert ledger.intent(prior.intent_id).status == "pending"
        assert result.status == "rejected"
        assert expected in result.reason
    finally:
        ledger.close()


def test_prior_pending_short_margin_participates_in_margin_limit(tmp_path):
    bridge, ledger = _bridge(
        tmp_path,
        risk_overrides={
            "long_only": False,
            "max_margin_utilization_pct": 0.25,
        },
    )
    prior_signal = _signal(
        signal_id="prior",
        ticker="AAPL",
        direction="short",
        decision_at=datetime(2026, 7, 31, 19, 59, tzinfo=UTC),
    )
    current_signal = _signal(
        signal_id="current",
        strategy="supply_chain",
        ticker="MSFT",
        direction="short",
    )
    try:
        ledger.record_signal(prior_signal)
        ledger.record_signal(current_signal)
        bridge.stage_intent(
            _recommendation("short"),
            (prior_signal,),
            ledger.account_state(),
            prior_signal.decision_at,
            MONDAY,
        )
        current = bridge.stage_intent(
            _recommendation("short", ticker="MSFT", contributors=["supply_chain"]),
            (current_signal,),
            ledger.account_state(),
            current_signal.decision_at,
            MONDAY,
        )

        result = bridge.execute_due_intent(
            current,
            _bar(ticker="MSFT", open_="100"),
            ledger.account_state(),
            {
                "borrow_rate": Decimal("0.02"),
                "processing_at": datetime(2026, 8, 3, 22, tzinfo=UTC),
                "opening_prices": {"AAPL": Decimal("100")},
            },
            PaperCostModel(),
        )

        assert result.status == "rejected"
        assert "margin_utilization" in result.reason
    finally:
        ledger.close()


@pytest.mark.parametrize("invalid_price", ["NaN", "Infinity", "-Infinity"])
def test_invalid_pending_opening_price_fails_before_any_ledger_mutation(
    tmp_path, invalid_price
):
    bridge, ledger = _bridge(tmp_path)
    prior_signal = _signal(
        signal_id="prior",
        strategy="supply_chain",
        ticker="MSFT",
        decision_at=datetime(2026, 7, 31, 19, 59, tzinfo=UTC),
    )
    current_signal = _signal(signal_id="current")
    try:
        ledger.record_signal(prior_signal)
        ledger.record_signal(current_signal)
        prior = bridge.stage_intent(
            _recommendation(ticker="MSFT", contributors=["supply_chain"]),
            (prior_signal,),
            ledger.account_state(),
            prior_signal.decision_at,
            MONDAY,
        )
        current = bridge.stage_intent(
            _recommendation(),
            (current_signal,),
            ledger.account_state(),
            current_signal.decision_at,
            MONDAY,
        )
        before = ledger.account_state()

        with pytest.raises(ValueError, match="invalid opening price"):
            bridge.execute_due_intent(
                current,
                _bar(),
                before,
                {
                    "processing_at": datetime(2026, 8, 3, 22, tzinfo=UTC),
                    "opening_prices": {"MSFT": Decimal(invalid_price)},
                },
                PaperCostModel(),
            )

        assert ledger.intent(prior.intent_id).status == "pending"
        assert ledger.intent(current.intent_id).status == "pending"
        assert ledger.read_fills() == []
        assert ledger.account_state() == before
    finally:
        ledger.close()


@pytest.mark.parametrize("invalid_high_water", ["NaN", "Infinity", "-Infinity", "-1"])
def test_invalid_authoritative_account_fails_before_any_ledger_mutation(
    tmp_path, invalid_high_water
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
        before = ledger.account_state()
        invalid_account = replace(before, high_water_mark=Decimal(invalid_high_water))

        with patch.object(ledger, "account_state", return_value=invalid_account):
            with pytest.raises(ValueError, match="authoritative high_water_mark"):
                bridge.execute_due_intent(
                    intent,
                    _bar(),
                    invalid_account,
                    {"processing_at": datetime(2026, 8, 3, 22, tzinfo=UTC)},
                    PaperCostModel(),
                )

        assert ledger.intent(intent.intent_id).status == "pending"
        assert ledger.read_fills() == []
        assert ledger.account_state() == before
    finally:
        ledger.close()
