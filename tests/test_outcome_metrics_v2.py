from datetime import UTC, date, datetime
from decimal import Decimal
import json

import pytest

from tradingagents.strategies.execution import stable_id
from tradingagents.strategies.execution.models import MarketBar, SignalRecord
from tradingagents.strategies.metrics.models import SignalMetricRecord
from tradingagents.strategies.metrics.outcomes import (
    OutcomeCalculator,
    directional_accuracy,
)
from tradingagents.strategies.orchestration.session_executor import SessionExecutor
from tradingagents.strategies.orchestration.trading_calendar import session_close
from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger


def _bar(session: date, opening: str, close: str) -> MarketBar:
    return MarketBar(
        ticker="AAPL",
        session=session,
        open=Decimal(opening),
        high=Decimal(max(opening, close)),
        low=Decimal(min(opening, close)),
        close=Decimal(close),
        source="fixture",
        fetched_at=datetime(2026, 8, 31, tzinfo=UTC),
        adjusted=False,
    )


def _signal(direction: str) -> SignalMetricRecord:
    return SignalMetricRecord(
        event_key="event-1",
        signal_id=f"signal-{direction}",
        epoch_id="epoch-1",
        policy_id="30d",
        strategy="filing_analysis",
        ticker="AAPL",
        direction=direction,
        decision_at=datetime(2026, 8, 3, 20, tzinfo=UTC),
        reference_session=date(2026, 8, 3),
    )


def test_five_session_outcome_uses_next_open_and_fifth_close() -> None:
    bars = {
        ("AAPL", date(2026, 8, 4)): _bar(date(2026, 8, 4), "100", "101"),
        ("AAPL", date(2026, 8, 10)): _bar(date(2026, 8, 10), "108", "110"),
    }
    outcome = OutcomeCalculator().build(_signal("long"), 5, bars)
    assert outcome.entry_session == date(2026, 8, 4)
    assert outcome.exit_session == date(2026, 8, 10)
    assert outcome.raw_return == Decimal("0.1")
    assert outcome.signed_return == Decimal("0.1")


def test_short_direction_is_applied_once() -> None:
    bars = {
        ("AAPL", date(2026, 8, 4)): _bar(date(2026, 8, 4), "100", "99"),
        ("AAPL", date(2026, 8, 10)): _bar(date(2026, 8, 10), "90", "90"),
    }
    outcome = OutcomeCalculator().build(_signal("short"), 5, bars)
    assert outcome.raw_return == Decimal("-0.1")
    assert outcome.signed_return == Decimal("0.1")
    assert directional_accuracy([outcome]).rate == 1.0


def test_neutral_is_excluded_from_directional_denominator() -> None:
    bars = {
        ("AAPL", date(2026, 8, 4)): _bar(date(2026, 8, 4), "100", "100"),
        ("AAPL", date(2026, 8, 10)): _bar(date(2026, 8, 10), "100", "110"),
    }
    neutral = OutcomeCalculator().build(_signal("neutral"), 5, bars)
    summary = directional_accuracy([neutral])
    assert neutral.signed_return is None
    assert summary.actionable_count == 0
    assert summary.neutral_count == 1
    assert summary.rate is None


def test_missing_exact_exit_price_is_invalid() -> None:
    bars = {("AAPL", date(2026, 8, 4)): _bar(date(2026, 8, 4), "100", "101")}
    outcome = OutcomeCalculator().build(_signal("long"), 5, bars)
    assert outcome.status == "invalid"
    assert outcome.exit_price is None
    assert outcome.invalid_reason == "missing_exit_bar"


@pytest.mark.parametrize(
    ("bars", "expected_reason"),
    [
        (
            {
                ("AAPL", date(2026, 8, 10)): _bar(
                    date(2026, 8, 10), "100", "0"
                )
            },
            "missing_entry_bar",
        ),
        (
            {
                ("AAPL", date(2026, 8, 4)): _bar(
                    date(2026, 8, 4), "0", "100"
                ),
                ("AAPL", date(2026, 8, 10)): _bar(
                    date(2026, 8, 10), "100", "0"
                ),
            },
            "invalid_entry_price",
        ),
    ],
)
def test_entry_failure_takes_precedence_when_exit_is_also_bad(
    bars, expected_reason
) -> None:
    outcome = OutcomeCalculator().build(_signal("long"), 5, bars)
    assert outcome.status == "invalid"
    assert outcome.invalid_reason == expected_reason


def _ledger_signal(ticker: str, reference_session: date) -> SignalRecord:
    cutoff = session_close(reference_session)
    return SignalRecord(
        signal_id=f"ledger-{ticker}-{reference_session}",
        epoch_id="epoch-1",
        policy_id="30d",
        event_key=f"event-{ticker}-{reference_session}",
        strategy="filing_analysis",
        ticker=ticker,
        direction="long",
        event_at=cutoff,
        observed_at=cutoff,
        reference_session=reference_session,
        reference_close=Decimal("100"),
        decision_at=cutoff,
        evidence_hash=f"evidence-{ticker}-{reference_session}",
    )


def test_untraded_entry_and_exit_due_signals_join_session_raw_request(tmp_path) -> None:
    ledger = PortfolioLedger(tmp_path / "portfolio.db", "cohort", Decimal("1000"))
    try:
        ledger.record_signal(_ledger_signal("AAPL", date(2026, 8, 3)))
        ledger.record_signal(_ledger_signal("MSFT", date(2026, 8, 3)))
        executor = SessionExecutor(
            ledger,
            {"execution": {"mode": "paper"}, "autoresearch": {}},
        )

        assert executor.required_tickers(date(2026, 8, 4), "epoch-1") == (
            "AAPL",
            "MSFT",
        )
        assert executor.required_tickers(date(2026, 8, 10), "epoch-1") == (
            "AAPL",
            "MSFT",
        )
    finally:
        ledger.close()


def test_missing_historical_entry_context_persists_invalid_outcome(tmp_path) -> None:
    ledger = PortfolioLedger(tmp_path / "portfolio.db", "cohort", Decimal("1000"))
    try:
        signal = _ledger_signal("AAPL", date(2026, 3, 30))
        ledger.record_signal(signal)
        executor = SessionExecutor(
            ledger,
            {"execution": {"mode": "paper"}, "autoresearch": {}},
        )
        exit_session = date(2026, 4, 7)
        exit_bar = _bar(exit_session, "100", "110")

        assert (
            executor.record_due_outcomes(
                exit_session, "epoch-1", {("AAPL", exit_session): exit_bar}
            )
            == 1
        )
        outcome = executor.metric_store.read_outcomes("epoch-1")[0]
        assert outcome.status == "invalid"
        assert outcome.invalid_reason == "missing_entry_bar"
    finally:
        ledger.close()


def test_malformed_persisted_entry_context_fails_closed(tmp_path) -> None:
    ledger = PortfolioLedger(tmp_path / "portfolio.db", "cohort", Decimal("1000"))
    try:
        signal = _ledger_signal("AAPL", date(2026, 3, 30))
        ledger.record_signal(signal)
        executor = SessionExecutor(
            ledger,
            {"execution": {"mode": "paper"}, "autoresearch": {}},
        )
        entry_session = date(2026, 3, 31)
        economic_inputs = {
            "starting_state": {},
            "market": {
                "raw_bars": [
                    {
                        "ticker": "AAPL",
                        "open": "100",
                        "high": "101",
                        "low": "99",
                        "close": "100",
                        "source": "fixture",
                        "adjusted": False,
                    }
                ]
            }
        }
        ledger.bind_session_execution_context(
            entry_session,
            "epoch-1",
            stable_id("session_economic_inputs", economic_inputs),
            stable_id("session_market_inputs", economic_inputs["market"]),
            "config-digest",
            stable_id("session_borrow_inputs", {}),
            ("AAPL",),
            json.dumps(economic_inputs),
            json.dumps({"raw_bars": {"AAPL": "not-a-datetime"}}),
            datetime(2026, 3, 31, 21, tzinfo=UTC),
            stable_id("session_governed_state", {}),
            "{}",
        )
        exit_session = date(2026, 4, 7)

        with pytest.raises(ValueError, match="Invalid isoformat"):
            executor.record_due_outcomes(
                exit_session,
                "epoch-1",
                {("AAPL", exit_session): _bar(exit_session, "100", "110")},
            )
        assert executor.metric_store.read_outcomes("epoch-1") == ()
    finally:
        ledger.close()
