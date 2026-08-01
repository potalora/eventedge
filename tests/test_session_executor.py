from __future__ import annotations

import json
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.strategies.execution import (
    CorporateAction,
    Fill,
    MarketBar,
    OrderIntent,
    SignalRecord,
    stable_id,
)
from tradingagents.strategies.execution.price_source import AdjustedClose
from tradingagents.strategies.execution.cost_model import PaperCostModel
from tradingagents.strategies.orchestration.session_executor import (
    PHASES,
    SessionInputBundle,
    SessionExecutor,
)
from tradingagents.strategies.orchestration.multi_strategy_engine import (
    MultiStrategyEngine,
)
from tradingagents.strategies.orchestration.cohort_orchestrator import SIZE_PROFILES
from tradingagents.strategies.state.state import StateManager
from tradingagents.strategies.trading.portfolio_committee import TradeRecommendation
from tradingagents.strategies.trading.portfolio_policy import PortfolioPolicyDecision
from tradingagents.strategies.orchestration.trading_calendar import (
    next_session,
    session_close,
)
from tradingagents.strategies.state.portfolio_ledger import (
    LedgerConflictError,
    PortfolioLedger,
)


UTC = timezone.utc
FRIDAY = date(2026, 7, 31)
MONDAY = date(2026, 8, 3)
TUESDAY = date(2026, 8, 4)
PROCESSED = datetime(2026, 8, 3, 22, tzinfo=UTC)


class FakePriceSource:
    def __init__(self, bars=None, actions=None, adjusted=None):
        self.bars = bars or {}
        self.actions = actions or []
        values = adjusted or {"SPY": "650.25", "BIL": "91.10"}
        self.adjusted = {
            (symbol, session): (
                value
                if isinstance(value, AdjustedClose)
                else AdjustedClose(
                    symbol,
                    session,
                    Decimal(str(value)),
                    "fixture-adjusted",
                    datetime.combine(session, datetime.min.time(), tzinfo=UTC).replace(
                        hour=22
                    ),
                )
            )
            for key, value in values.items()
            for symbol, session in [key if isinstance(key, tuple) else (key, MONDAY)]
        }
        self.raw_requests: list[tuple[tuple[str, ...], date, date, bool]] = []

    def get_daily_bars(
        self, tickers, start_session, end_session_inclusive, adjusted=False
    ):
        self.raw_requests.append(
            (tuple(tickers), start_session, end_session_inclusive, adjusted)
        )
        return {
            key: value
            for key, value in self.bars.items()
            if key[0] in tickers and start_session <= key[1] <= end_session_inclusive
        }

    def get_corporate_actions(self, tickers, session):
        return [
            action
            for action in self.actions
            if action.ticker in tickers and action.session == session
        ]

    def get_total_return_closes(self, symbols, start_session, end_session_inclusive):
        return {
            key: value
            for key, value in self.adjusted.items()
            if key[0] in symbols and start_session <= key[1] <= end_session_inclusive
        }


def _config(**risk_overrides):
    risk = {
        "long_only": False,
        "min_position_value": 1,
        "max_position_pct": 1.0,
        "max_positions": 8,
        "per_strategy_max": 8,
    }
    risk.update(risk_overrides)
    return {
        "execution": {"mode": "paper"},
        "autoresearch": {
            "total_capital": 1000,
            "risk_gate": risk,
            "short_selling": {"borrow_cost_reject_above": "0.05"},
            "paper_ledger": {
                "slippage_bps": "10",
                "margin_financing_rate": "0",
                "benchmark_symbols": ["SPY", "BIL"],
            },
        },
    }


def _ledger(tmp_path, cash="1000"):
    return PortfolioLedger(tmp_path / "ledger.db", "cohort", Decimal(cash))


def _bar(ticker, session=MONDAY, open_="100", close="101"):
    fetched = datetime.combine(session, datetime.min.time(), tzinfo=UTC).replace(
        hour=22
    )
    return MarketBar(
        ticker,
        session,
        Decimal(open_),
        max(Decimal(open_), Decimal(close)) + Decimal("1"),
        min(Decimal(open_), Decimal(close)) - Decimal("1"),
        Decimal(close),
        "fixture-raw",
        fetched,
        False,
    )


def _signal(ticker, reference_session, *, strategy="strategy", direction="long"):
    cutoff = session_close(reference_session)
    signal_id = stable_id(
        "signal", "epoch", strategy, "policy", direction, ticker, reference_session
    )
    return SignalRecord(
        signal_id,
        "epoch",
        "policy",
        f"event-{ticker}-{reference_session}",
        strategy,
        ticker,
        direction,
        cutoff,
        cutoff,
        reference_session,
        Decimal("100"),
        cutoff,
        stable_id("evidence", ticker, reference_session),
    )


def _intent(
    ledger,
    ticker,
    side,
    eligible_session,
    qty,
    *,
    reference_session=FRIDAY,
    strategy="strategy",
):
    signal = _signal(
        ticker,
        reference_session,
        strategy=strategy,
        direction="short" if side in {"short", "cover"} else "long",
    )
    ledger.record_signal(signal)
    intent = OrderIntent(
        stable_id("intent", ledger.cohort_id, ticker, side, eligible_session, qty),
        (signal.signal_id,),
        ledger.cohort_id,
        side,
        qty,
        session_close(reference_session),
        eligible_session,
        "next_session_open",
        "pending",
        None,
        None,
    )
    ledger.stage_intent(intent)
    return intent


def _open_long(
    ledger,
    ticker="OLD",
    qty=9,
    *,
    reference=date(2026, 7, 29),
    opened=date(2026, 7, 30),
):
    intent = _intent(ledger, ticker, "buy", opened, qty, reference_session=reference)
    fill = Fill(
        stable_id("fill", intent.intent_id, opened, qty),
        intent.intent_id,
        "buy",
        opened,
        datetime.combine(opened, datetime.min.time(), tzinfo=UTC).replace(
            hour=13, minute=30
        ),
        datetime.combine(opened, datetime.min.time(), tzinfo=UTC).replace(hour=22),
        Decimal("100"),
        Decimal("100"),
        qty,
        Decimal("0"),
        Decimal("0"),
        Decimal("0"),
    )
    ledger.apply_fill(intent, fill)
    return intent


def _resting_stop(
    ledger,
    ticker,
    eligible_session,
    qty,
    *,
    reference_session=date(2026, 7, 30),
):
    signal = _signal(ticker, reference_session)
    ledger.record_signal(signal)
    lot = ledger.connection.execute(
        """SELECT lot_id, open_qty FROM lots
           WHERE cohort_id = ? AND ticker = ? AND open_qty >= ?
           ORDER BY opened_session, lot_id LIMIT 1""",
        (ledger.cohort_id, ticker, qty),
    ).fetchone()
    assert lot is not None
    intent = OrderIntent(
        stable_id(
            "resting_stop",
            ledger.cohort_id,
            ticker,
            eligible_session,
            qty,
            reference_session,
        ),
        (signal.signal_id,),
        ledger.cohort_id,
        "sell",
        qty,
        session_close(reference_session),
        eligible_session,
        "resting_stop",
        "pending",
        Decimal("101"),
        None,
    )
    ledger.stage_exit_intent(intent, ((str(lot["lot_id"]), qty),))
    return intent


def test_friday_intent_never_fills_until_exact_monday_open(tmp_path):
    ledger = _ledger(tmp_path)
    try:
        intent = _intent(ledger, "AAPL", "buy", MONDAY, 5)
        friday_source = FakePriceSource(
            adjusted={
                ("SPY", FRIDAY): Decimal("649"),
                ("BIL", FRIDAY): Decimal("91"),
            }
        )
        friday = SessionExecutor(ledger, _config()).execute_open_and_mark(
            FRIDAY, "epoch", friday_source, {}, datetime(2026, 7, 31, 22, tzinfo=UTC)
        )
        assert friday.valid
        assert friday.snapshot is not None
        assert ledger.read_fills(FRIDAY, FRIDAY) == []
        assert ledger.intent(intent.intent_id).status == "pending"

        monday_source = FakePriceSource({("AAPL", MONDAY): _bar("AAPL")})
        monday = SessionExecutor(ledger, _config()).execute_open_and_mark(
            MONDAY, "epoch", monday_source, {}, PROCESSED
        )
        assert monday.valid
        assert monday.snapshot is not None
        fill = ledger.read_fills(MONDAY, MONDAY)[0]
        assert fill.effective_at == datetime(2026, 8, 3, 13, 30, tzinfo=UTC)
        assert fill.reference_price == Decimal("100")
    finally:
        ledger.close()


def test_overdue_next_open_exit_is_cancelled_and_releases_its_lot(tmp_path):
    ledger = _ledger(tmp_path)
    tuesday_processed = datetime(2026, 8, 4, 22, tzinfo=UTC)
    adjusted = {("SPY", TUESDAY): "650", ("BIL", TUESDAY): "91"}
    try:
        _open_long(ledger, "AAPL", 2)
        overdue = _intent(ledger, "AAPL", "sell", MONDAY, 2)
        lot = ledger.open_exit_positions()[0]
        ledger.stage_exit_intent(overdue, ((str(lot["lot_id"]), 2),))

        result = SessionExecutor(ledger, _config()).execute_open_and_mark(
            TUESDAY,
            "epoch",
            FakePriceSource(
                {("AAPL", TUESDAY): _bar("AAPL", TUESDAY)}, adjusted=adjusted
            ),
            {},
            tuesday_processed,
        )

        assert result.valid
        assert ledger.intent(overdue.intent_id).status == "cancelled"
        assert ledger.read_fills(TUESDAY, TUESDAY) == []

        replacement = _intent(
            ledger,
            "AAPL",
            "sell",
            next_session(TUESDAY),
            2,
            reference_session=MONDAY,
        )
        ledger.stage_exit_intent(replacement, ((str(lot["lot_id"]), 2),))
        assert ledger.intent(replacement.intent_id).status == "pending"

        replay = SessionExecutor(ledger, _config()).execute_open_and_mark(
            TUESDAY,
            "epoch",
            FakePriceSource(
                {("AAPL", TUESDAY): _bar("AAPL", TUESDAY)}, adjusted=adjusted
            ),
            {},
            tuesday_processed,
        )
        assert replay.valid
        transitions = ledger.connection.execute(
            "SELECT status, reason FROM order_status_transitions WHERE intent_id = ?",
            (overdue.intent_id,),
        ).fetchall()
        assert [(row["status"], row["reason"]) for row in transitions] == [
            ("cancelled", "missed exact eligible session")
        ]
    finally:
        ledger.close()


def test_exact_xnys_weekend_holiday_and_early_close_transitions():
    assert next_session(FRIDAY) == MONDAY
    assert next_session(date(2026, 7, 2)) == date(2026, 7, 6)
    assert next_session(date(2026, 11, 25)) == date(2026, 11, 27)
    assert session_close(date(2026, 11, 27)) == datetime(2026, 11, 27, 18, tzinfo=UTC)


@pytest.mark.parametrize(
    ("session", "processed_at", "message"),
    [
        (date(2026, 8, 1), PROCESSED, "not an XNYS session"),
        (MONDAY, datetime(2026, 8, 3, 22), "timezone-aware"),
        (
            MONDAY,
            datetime(2026, 8, 3, 19, tzinfo=UTC),
            "precedes the exact XNYS close",
        ),
    ],
)
def test_invalid_session_clocks_fail_before_fetch_or_mutation(
    tmp_path, session, processed_at, message
):
    ledger = _ledger(tmp_path)
    source = FakePriceSource()
    try:
        with (
            patch.object(
                source,
                "get_total_return_closes",
                side_effect=AssertionError("market data fetch must not run"),
            ),
            pytest.raises(ValueError, match=message),
        ):
            SessionExecutor(ledger, _config()).execute_open_and_mark(
                session, "epoch", source, {}, processed_at
            )
        assert (
            ledger.connection.execute("SELECT COUNT(*) FROM session_phases").fetchone()[
                0
            ]
            == 0
        )
    finally:
        ledger.close()


def test_session_snapshot_rejects_epoch_reinterpretation_without_refetch(tmp_path):
    ledger = _ledger(tmp_path)
    source = FakePriceSource()
    try:
        first = SessionExecutor(ledger, _config()).execute_open_and_mark(
            MONDAY, "epoch-a", source, {}, PROCESSED
        )
        assert first.valid
        with (
            patch.object(
                source,
                "get_total_return_closes",
                side_effect=AssertionError("completed session must not refetch"),
            ),
            pytest.raises(ValueError, match="already belongs to epoch epoch-a"),
        ):
            SessionExecutor(ledger, _config()).execute_open_and_mark(
                MONDAY, "epoch-b", source, {}, PROCESSED
            )
    finally:
        ledger.close()


def test_exits_commit_before_entries_and_release_buying_power(tmp_path):
    ledger = _ledger(tmp_path)
    try:
        _open_long(ledger)
        _intent(ledger, "OLD", "sell", MONDAY, 9)
        entry = _intent(ledger, "NEW", "buy", MONDAY, 8)
        source = FakePriceSource(
            {
                ("OLD", MONDAY): _bar("OLD", open_="100", close="100"),
                ("NEW", MONDAY): _bar("NEW", open_="100", close="101"),
            }
        )
        snapshot = SessionExecutor(ledger, _config()).execute_open_and_mark(
            MONDAY, "epoch", source, {}, PROCESSED
        )
        fills = ledger.read_fills(MONDAY, MONDAY)
        execution_order = ledger.connection.execute(
            "SELECT side FROM fills WHERE session = ? ORDER BY rowid",
            (MONDAY.isoformat(),),
        ).fetchall()
        assert snapshot.valid
        assert snapshot.snapshot is not None
        assert [row["side"] for row in execution_order] == ["sell", "buy"]
        assert {fill.side for fill in fills} == {"sell", "buy"}
        assert ledger.intent(entry.intent_id).status == "filled"
    finally:
        ledger.close()


def test_missing_held_mark_invalidates_before_any_economic_mutation(tmp_path):
    ledger = _ledger(tmp_path)
    try:
        _open_long(ledger, "HELD", 2)
        entry = _intent(ledger, "NEW", "buy", MONDAY, 2)
        before = ledger.account_state()
        source = FakePriceSource({("NEW", MONDAY): _bar("NEW")})

        snapshot = SessionExecutor(ledger, _config()).execute_open_and_mark(
            MONDAY, "epoch", source, {}, PROCESSED
        )

        assert not snapshot.valid
        assert snapshot.snapshot is None
        assert "missing HELD/2026-08-03" in snapshot.invalid_reason
        assert ledger.account_state() == before
        assert ledger.intent(entry.intent_id).status == "cancelled"
        assert ledger.read_fills(MONDAY, MONDAY) == []
        assert (
            ledger.connection.execute("SELECT COUNT(*) FROM session_phases").fetchone()[
                0
            ]
            == 0
        )
    finally:
        ledger.close()


@pytest.mark.parametrize(
    ("invalid_action", "expected_error"),
    [
        (
            CorporateAction(
                "bad-unverified",
                "AAPL",
                MONDAY,
                "cash_dividend",
                None,
                Decimal("1"),
                "fixture",
                PROCESSED,
                False,
            ),
            "unverified corporate action bad-unverified",
        ),
        (
            CorporateAction(
                "bad-source",
                "AAPL",
                MONDAY,
                "cash_dividend",
                None,
                Decimal("1"),
                "",
                PROCESSED,
                True,
            ),
            "missing source corporate action bad-source",
        ),
        (
            CorporateAction(
                "bad-scope",
                "MSFT",
                MONDAY,
                "cash_dividend",
                None,
                Decimal("1"),
                "fixture",
                PROCESSED,
                True,
            ),
            "corporate action scope mismatch bad-scope",
        ),
    ],
)
def test_invalid_complete_action_batch_is_rejected_atomically_and_quarantined(
    tmp_path, invalid_action, expected_error
):
    ledger = _ledger(tmp_path)
    try:
        _open_long(ledger, "AAPL", 2)
        due = _intent(ledger, "AAPL", "sell", MONDAY, 2)
        valid = CorporateAction(
            "valid-dividend",
            "AAPL",
            MONDAY,
            "cash_dividend",
            None,
            Decimal("2"),
            "fixture",
            PROCESSED,
            True,
        )
        bundle = SessionInputBundle(
            MONDAY,
            ("AAPL",),
            {("AAPL", MONDAY): _bar("AAPL")},
            (valid, invalid_action),
            FakePriceSource().adjusted,
        )

        result = SessionExecutor(ledger, _config()).execute_open_and_mark(
            MONDAY, "epoch", bundle, {}, PROCESSED
        )

        assert not result.valid
        assert expected_error in result.invalid_reason
        assert ledger.intent(due.intent_id).status == "cancelled"
        assert (
            ledger.connection.execute(
                "SELECT COUNT(*) FROM corporate_actions"
            ).fetchone()[0]
            == 0
        )
        assert (
            ledger.connection.execute(
                "SELECT COUNT(*) FROM dividend_events"
            ).fetchone()[0]
            == 0
        )
        rejection = ledger.connection.execute(
            "SELECT * FROM corporate_action_batch_rejections"
        ).fetchall()
        assert len(rejection) == 1
        assert "valid-dividend" in rejection[0]["payload_json"]
        assert invalid_action.action_id in rejection[0]["payload_json"]
        assert expected_error in rejection[0]["errors_json"]
        assert not ledger.session_is_valid(TUESDAY, "AAPL")

        next_source = FakePriceSource(
            {("AAPL", TUESDAY): _bar("AAPL", TUESDAY)},
            adjusted={
                ("SPY", TUESDAY): Decimal("651"),
                ("BIL", TUESDAY): Decimal("91.2"),
            },
        )
        next_result = SessionExecutor(ledger, _config()).execute_open_and_mark(
            TUESDAY,
            "epoch",
            next_source,
            {},
            datetime(2026, 8, 4, 22, tzinfo=UTC),
        )
        assert not next_result.valid
        assert "quarantined AAPL" in next_result.invalid_reason
        assert next_source.raw_requests == []
    finally:
        ledger.close()


def test_crash_after_fill_insertion_rolls_back_whole_phase_and_rerun_fills_once(
    tmp_path,
):
    ledger = _ledger(tmp_path)
    try:
        entry = _intent(ledger, "AAPL", "buy", MONDAY, 2)
        source = FakePriceSource({("AAPL", MONDAY): _bar("AAPL")})

        def crash_after_mutation(phase):
            if phase == "execute_entries":
                raise RuntimeError("injected crash after fill")

        with pytest.raises(RuntimeError, match="injected crash after fill"):
            SessionExecutor(
                ledger, _config(), after_phase_mutation=crash_after_mutation
            ).execute_open_and_mark(MONDAY, "epoch", source, {}, PROCESSED)

        assert ledger.read_fills(MONDAY, MONDAY) == []
        assert ledger.intent(entry.intent_id).status == "pending"
        assert not ledger.phase_completed(MONDAY, "execute_entries")

        snapshot = SessionExecutor(ledger, _config()).execute_open_and_mark(
            MONDAY, "epoch", source, {}, PROCESSED
        )
        assert snapshot.valid
        assert len(ledger.read_fills(MONDAY, MONDAY)) == 1
    finally:
        ledger.close()


def test_crash_after_completed_action_phase_resumes_without_duplicate_dividend(
    tmp_path,
):
    ledger = _ledger(tmp_path)
    try:
        _open_long(ledger, "AAPL", 2)
        action = CorporateAction(
            stable_id("action", "AAPL", MONDAY, "dividend"),
            "AAPL",
            MONDAY,
            "cash_dividend",
            None,
            Decimal("1"),
            "fixture",
            PROCESSED,
            True,
        )
        source = FakePriceSource({("AAPL", MONDAY): _bar("AAPL")}, actions=[action])

        def crash_after_commit(phase):
            if phase == "apply_corporate_actions":
                raise RuntimeError("injected crash after commit")

        with pytest.raises(RuntimeError, match="injected crash after commit"):
            SessionExecutor(
                ledger, _config(), after_phase_commit=crash_after_commit
            ).execute_open_and_mark(MONDAY, "epoch", source, {}, PROCESSED)

        assert ledger.phase_completed(MONDAY, "apply_corporate_actions")
        assert (
            ledger.connection.execute(
                "SELECT COUNT(*) FROM dividend_events"
            ).fetchone()[0]
            == 1
        )

        snapshot = SessionExecutor(ledger, _config()).execute_open_and_mark(
            MONDAY, "epoch", source, {}, PROCESSED
        )
        assert snapshot.valid
        assert (
            ledger.connection.execute(
                "SELECT COUNT(*) FROM dividend_events"
            ).fetchone()[0]
            == 1
        )
    finally:
        ledger.close()


@pytest.mark.parametrize(
    ("crash_phase", "scenario"),
    [
        ("apply_corporate_actions", "action"),
        ("execute_exits", "exit"),
        ("execute_entries", "entry"),
    ],
)
@pytest.mark.parametrize("inserted_ticker", ["AAPL", "MSFT"])
def test_resume_rejects_due_intent_inserted_after_bound_phase_commit(
    tmp_path, crash_phase, scenario, inserted_ticker
):
    ledger = _ledger(tmp_path, cash="3000")
    try:
        actions = []
        if scenario in {"action", "exit"}:
            _open_long(ledger, "AAPL", 2)
        if scenario == "action":
            actions = [
                CorporateAction(
                    "dividend-aapl",
                    "AAPL",
                    MONDAY,
                    "cash_dividend",
                    None,
                    Decimal("1"),
                    "fixture-action",
                    PROCESSED,
                    True,
                )
            ]
        elif scenario == "exit":
            _intent(ledger, "AAPL", "sell", MONDAY, 1)
        else:
            _intent(ledger, "AAPL", "buy", MONDAY, 1)
        source = FakePriceSource({("AAPL", MONDAY): _bar("AAPL")}, actions=actions)

        def crash_after_commit(phase):
            if phase == crash_phase:
                raise RuntimeError(f"crash after {phase}")

        with pytest.raises(RuntimeError, match=f"crash after {crash_phase}"):
            SessionExecutor(
                ledger, _config(), after_phase_commit=crash_after_commit
            ).execute_open_and_mark(MONDAY, "epoch", source, {}, PROCESSED)

        injected = _intent(ledger, inserted_ticker, "buy", MONDAY, 3)
        resumed = SessionExecutor(ledger, _config()).execute_open_and_mark(
            MONDAY, "epoch", source, {}, PROCESSED
        )

        assert not resumed.valid
        assert "governed session state conflict" in resumed.invalid_reason
        assert ledger.intent(injected.intent_id).status != "filled"
        assert ledger.read_snapshots(MONDAY, MONDAY) == []
    finally:
        ledger.close()


@pytest.mark.parametrize(
    ("crash_phase", "scenario"),
    [
        ("apply_corporate_actions", "action"),
        ("execute_exits", "exit"),
        ("execute_entries", "entry"),
    ],
)
def test_resume_rejects_prior_session_allocated_resting_stop_and_cancels_it(
    tmp_path, crash_phase, scenario
):
    ledger = _ledger(tmp_path, cash="3000")
    try:
        _open_long(ledger, "AAPL", 4)
        actions = []
        if scenario == "action":
            actions = [
                CorporateAction(
                    "dividend-aapl-prior-stop",
                    "AAPL",
                    MONDAY,
                    "cash_dividend",
                    None,
                    Decimal("1"),
                    "fixture-action",
                    PROCESSED,
                    True,
                )
            ]
        elif scenario == "exit":
            _intent(ledger, "AAPL", "sell", MONDAY, 1)
        else:
            _intent(ledger, "AAPL", "buy", MONDAY, 1)
        source = FakePriceSource(
            {("AAPL", MONDAY): _bar("AAPL", open_="100", close="101")},
            actions=actions,
        )

        def crash_after_commit(phase):
            if phase == crash_phase:
                raise RuntimeError(f"crash after {phase}")

        with pytest.raises(RuntimeError, match=f"crash after {crash_phase}"):
            SessionExecutor(
                ledger, _config(), after_phase_commit=crash_after_commit
            ).execute_open_and_mark(MONDAY, "epoch", source, {}, PROCESSED)

        injected = _resting_stop(ledger, "AAPL", FRIDAY, 1)
        resumed = SessionExecutor(ledger, _config()).execute_open_and_mark(
            MONDAY, "epoch", source, {}, PROCESSED
        )

        assert not resumed.valid
        assert "governed session state conflict" in resumed.invalid_reason
        assert ledger.intent(injected.intent_id).status == "cancelled"
        assert ledger.read_snapshots(MONDAY, MONDAY) == []
    finally:
        ledger.close()


@pytest.mark.parametrize("mutation", ["lot", "allocation", "accounting"])
def test_resume_rejects_other_governed_state_mutations_after_binding(
    tmp_path, mutation
):
    ledger = _ledger(tmp_path, cash="3000")
    try:
        source = FakePriceSource()
        if mutation == "allocation":
            opened = _open_long(ledger, "AAPL", 2)
            signal_ids = tuple(
                signal.signal_id
                for signal in ledger.signals_for_intent(opened.intent_id)
            )
            lot_id = ledger.open_exit_positions()[0]["lot_id"]
            exit_intent = OrderIntent(
                stable_id("governed_exit", MONDAY),
                signal_ids,
                ledger.cohort_id,
                "sell",
                2,
                session_close(FRIDAY),
                MONDAY,
                "next_session_open",
                "pending",
                None,
                None,
            )
            ledger.stage_exit_intent(exit_intent, ((str(lot_id), 2),))
            source = FakePriceSource({("AAPL", MONDAY): _bar("AAPL")})

        def crash_after_commit(phase):
            if phase == "validate_market_data":
                raise RuntimeError("crash after binding")

        with pytest.raises(RuntimeError, match="crash after binding"):
            SessionExecutor(
                ledger, _config(), after_phase_commit=crash_after_commit
            ).execute_open_and_mark(MONDAY, "epoch", source, {}, PROCESSED)

        if mutation == "lot":
            _open_long(
                ledger,
                "MSFT",
                1,
                reference=date(2026, 7, 29),
                opened=FRIDAY,
            )
        elif mutation == "allocation":
            ledger.connection.execute(
                "UPDATE exit_intent_lots SET quantity = 1 WHERE intent_id = ?",
                (exit_intent.intent_id,),
            )
        else:
            ledger.connection.execute(
                "UPDATE accounting_state SET cash = '2999' WHERE cohort_id = ?",
                (ledger.cohort_id,),
            )

        resumed = SessionExecutor(ledger, _config()).execute_open_and_mark(
            MONDAY, "epoch", source, {}, PROCESSED
        )
        assert not resumed.valid
        assert "governed session state conflict" in resumed.invalid_reason
        assert ledger.read_snapshots(MONDAY, MONDAY) == []
    finally:
        ledger.close()


def test_mixed_valid_dividend_and_state_invalid_split_rejects_atomically(tmp_path):
    ledger = _ledger(tmp_path, cash="3000")
    try:
        _open_long(ledger, "AAPL", 1)
        due = _intent(ledger, "AAPL", "buy", MONDAY, 1)
        dividend = CorporateAction(
            "a-dividend-aapl",
            "AAPL",
            MONDAY,
            "cash_dividend",
            None,
            Decimal("10"),
            "fixture-action",
            PROCESSED,
            True,
        )
        fractional_split = CorporateAction(
            "z-split-aapl",
            "AAPL",
            MONDAY,
            "split",
            Decimal("1.5"),
            None,
            "fixture-action",
            PROCESSED,
            True,
        )

        result = SessionExecutor(ledger, _config()).execute_open_and_mark(
            MONDAY,
            "epoch",
            FakePriceSource(
                {("AAPL", MONDAY): _bar("AAPL")},
                actions=[dividend, fractional_split],
            ),
            {},
            PROCESSED,
        )

        assert not result.valid
        assert "fractional" in result.invalid_reason
        assert not ledger.phase_completed(MONDAY, "apply_corporate_actions")
        assert (
            ledger.connection.execute(
                "SELECT COUNT(*) FROM dividend_events"
            ).fetchone()[0]
            == 0
        )
        assert (
            ledger.connection.execute(
                "SELECT COUNT(*) FROM corporate_actions"
            ).fetchone()[0]
            == 0
        )
        assert (
            ledger.connection.execute(
                "SELECT COUNT(*) FROM corporate_action_batch_rejections"
            ).fetchone()[0]
            == 1
        )
        assert ledger.intent(due.intent_id).status == "cancelled"
        assert "invalid corporate action batch" in ledger.session_invalid_reason(
            MONDAY, "AAPL"
        )
    finally:
        ledger.close()


def test_conflicting_action_identity_rejects_before_earlier_sorted_action(tmp_path):
    ledger = _ledger(tmp_path, cash="3000")
    try:
        _open_long(ledger, "AAPL", 2)
        due = _intent(ledger, "AAPL", "sell", MONDAY, 2)
        dividend = CorporateAction(
            "a-dividend",
            "AAPL",
            MONDAY,
            "cash_dividend",
            None,
            Decimal("5"),
            "fixture",
            PROCESSED,
            True,
        )
        first = CorporateAction(
            "z-conflict",
            "AAPL",
            MONDAY,
            "split",
            Decimal("2"),
            None,
            "fixture",
            PROCESSED,
            True,
        )
        second = CorporateAction(
            "z-conflict",
            "AAPL",
            MONDAY,
            "split",
            Decimal("3"),
            None,
            "fixture",
            PROCESSED,
            True,
        )
        bundle = SessionInputBundle(
            MONDAY,
            ("AAPL",),
            {("AAPL", MONDAY): _bar("AAPL")},
            (dividend, first, second),
            FakePriceSource().adjusted,
        )

        result = SessionExecutor(ledger, _config()).execute_open_and_mark(
            MONDAY, "epoch", bundle, {}, PROCESSED
        )

        assert not result.valid
        assert "conflicting corporate action z-conflict" in result.invalid_reason
        assert (
            ledger.connection.execute(
                "SELECT COUNT(*) FROM corporate_actions"
            ).fetchone()[0]
            == 0
        )
        assert (
            ledger.connection.execute(
                "SELECT COUNT(*) FROM dividend_events"
            ).fetchone()[0]
            == 0
        )
        assert ledger.intent(due.intent_id).status == "cancelled"
    finally:
        ledger.close()


@pytest.mark.parametrize(
    ("crash_phase", "scenario", "mutated_input"),
    [
        ("apply_corporate_actions", "action", "action"),
        ("execute_exits", "exit", "raw"),
        ("execute_entries", "entry", "benchmark"),
    ],
)
def test_crash_resume_rejects_changed_economic_bundle_without_mixed_snapshot(
    tmp_path, crash_phase, scenario, mutated_input
):
    ledger = _ledger(tmp_path, cash="2000")
    try:
        actions = []
        if scenario in {"action", "exit"}:
            _open_long(ledger, "AAPL", 2)
        if scenario == "action":
            actions = [
                CorporateAction(
                    "dividend-aapl",
                    "AAPL",
                    MONDAY,
                    "cash_dividend",
                    None,
                    Decimal("1"),
                    "fixture-action",
                    PROCESSED,
                    True,
                )
            ]
        elif scenario == "exit":
            _intent(ledger, "AAPL", "sell", MONDAY, 2)
        else:
            _intent(ledger, "AAPL", "buy", MONDAY, 2)

        source = FakePriceSource(
            {("AAPL", MONDAY): _bar("AAPL", open_="100", close="101")},
            actions=actions,
        )

        def crash_after_commit(phase):
            if phase == crash_phase:
                raise RuntimeError(f"crash after {phase}")

        with pytest.raises(RuntimeError, match=f"crash after {crash_phase}"):
            SessionExecutor(
                ledger, _config(), after_phase_commit=crash_after_commit
            ).execute_open_and_mark(MONDAY, "epoch-a", source, {}, PROCESSED)

        mutated_bar = _bar("AAPL", open_="200", close="201")
        mutated_actions = actions
        adjusted = None
        if mutated_input == "action":
            mutated_actions = [
                CorporateAction(
                    "dividend-aapl",
                    "AAPL",
                    MONDAY,
                    "cash_dividend",
                    None,
                    Decimal("2"),
                    "fixture-action",
                    PROCESSED,
                    True,
                )
            ]
            mutated_bar = _bar("AAPL", open_="100", close="101")
        elif mutated_input == "benchmark":
            adjusted = {
                ("SPY", MONDAY): Decimal("700"),
                ("BIL", MONDAY): Decimal("91.10"),
            }
            mutated_bar = _bar("AAPL", open_="100", close="101")

        resumed = SessionExecutor(ledger, _config()).execute_open_and_mark(
            MONDAY,
            "epoch-a",
            FakePriceSource(
                {("AAPL", MONDAY): mutated_bar},
                actions=mutated_actions,
                adjusted=adjusted,
            ),
            {},
            PROCESSED,
        )
        assert not resumed.valid
        assert resumed.snapshot is None
        assert "execution context" in resumed.invalid_reason
        assert ledger.read_snapshots(MONDAY, MONDAY) == []
    finally:
        ledger.close()


@pytest.mark.parametrize(
    ("crash_phase", "scenario"),
    [
        ("apply_corporate_actions", "action"),
        ("execute_exits", "exit"),
        ("execute_entries", "entry"),
    ],
)
def test_crash_resume_rejects_epoch_drift_before_skipping_completed_phase(
    tmp_path, crash_phase, scenario
):
    ledger = _ledger(tmp_path, cash="2000")
    try:
        actions = []
        if scenario in {"action", "exit"}:
            _open_long(ledger, "AAPL", 2)
        if scenario == "action":
            actions = [
                CorporateAction(
                    "dividend-aapl",
                    "AAPL",
                    MONDAY,
                    "cash_dividend",
                    None,
                    Decimal("1"),
                    "fixture-action",
                    PROCESSED,
                    True,
                )
            ]
        elif scenario == "exit":
            _intent(ledger, "AAPL", "sell", MONDAY, 2)
        else:
            _intent(ledger, "AAPL", "buy", MONDAY, 2)
        source = FakePriceSource({("AAPL", MONDAY): _bar("AAPL")}, actions=actions)

        def crash_after_commit(phase):
            if phase == crash_phase:
                raise RuntimeError(f"crash after {phase}")

        with pytest.raises(RuntimeError, match=f"crash after {crash_phase}"):
            SessionExecutor(
                ledger, _config(), after_phase_commit=crash_after_commit
            ).execute_open_and_mark(MONDAY, "epoch-a", source, {}, PROCESSED)

        resumed = SessionExecutor(ledger, _config()).execute_open_and_mark(
            MONDAY, "epoch-b", source, {}, PROCESSED
        )
        assert not resumed.valid
        assert resumed.snapshot is None
        assert "epoch" in resumed.invalid_reason
        assert ledger.read_snapshots(MONDAY, MONDAY) == []
    finally:
        ledger.close()


def test_benchmarks_are_adjusted_separate_from_raw_marks_and_phases_are_exact(tmp_path):
    ledger = _ledger(tmp_path)
    try:
        _open_long(ledger, "SPY", 1)
        source = FakePriceSource(
            {("SPY", MONDAY): _bar("SPY", open_="600", close="601")},
            adjusted={
                ("SPY", MONDAY): Decimal("650.25"),
                ("BIL", MONDAY): Decimal("91.10"),
            },
        )
        snapshot = SessionExecutor(ledger, _config()).execute_open_and_mark(
            MONDAY, "epoch", source, {}, PROCESSED
        )
        observations = ledger.read_benchmark_observations(MONDAY, MONDAY)
        phase_rows = ledger.connection.execute(
            "SELECT phase FROM session_phases WHERE session = ? ORDER BY rowid",
            (MONDAY.isoformat(),),
        ).fetchall()

        assert snapshot.valid
        assert snapshot.snapshot is not None
        assert [(item.symbol, item.close) for item in observations] == [
            ("BIL", Decimal("91.10")),
            ("SPY", Decimal("650.25")),
        ]
        assert all(
            item.return_basis == "total_return_adjusted" for item in observations
        )
        assert observations[1].close != Decimal("601")
        assert tuple(row["phase"] for row in phase_rows) == PHASES
        assert source.raw_requests == [(("SPY",), MONDAY, MONDAY, False)]
    finally:
        ledger.close()


def test_complete_replay_uses_bound_context_without_any_market_io(tmp_path):
    ledger = _ledger(tmp_path)
    try:
        _intent(ledger, "AAPL", "buy", MONDAY, 1)
        first = SessionExecutor(ledger, _config()).execute_open_and_mark(
            MONDAY,
            "epoch",
            FakePriceSource({("AAPL", MONDAY): _bar("AAPL")}),
            {},
            PROCESSED,
        )
        assert first.valid

        class NoMarketIO:
            def __getattr__(self, name):
                raise AssertionError(f"completed replay attempted market I/O: {name}")

        replay = SessionExecutor(ledger, _config()).execute_open_and_mark(
            MONDAY, "epoch", NoMarketIO(), {}, PROCESSED
        )
        assert replay.valid
        assert replay.snapshot == first.snapshot

        context = ledger.session_execution_context(MONDAY)
        assert context is not None
        assert context["input_digest"] == stable_id(
            "session_economic_inputs", json.loads(context["economic_inputs_json"])
        )
        assert "starting_state" in json.loads(context["economic_inputs_json"])
    finally:
        ledger.close()


def test_bound_context_detects_economic_or_provenance_payload_tampering(tmp_path):
    ledger = _ledger(tmp_path)
    try:
        _intent(ledger, "AAPL", "buy", MONDAY, 1)
        result = SessionExecutor(ledger, _config()).execute_open_and_mark(
            MONDAY,
            "epoch",
            FakePriceSource({("AAPL", MONDAY): _bar("AAPL")}),
            {},
            PROCESSED,
        )
        assert result.valid
        ledger.connection.execute(
            """UPDATE session_execution_contexts
               SET provenance_json = '{"tampered":true}' WHERE session = ?""",
            (MONDAY.isoformat(),),
        )
        with pytest.raises(LedgerConflictError, match="economic payload conflict"):
            ledger.session_execution_context(MONDAY)
    finally:
        ledger.close()


def test_partial_resume_rehydrates_bound_economics_and_keeps_original_freshness(
    tmp_path,
):
    ledger = _ledger(tmp_path)
    try:
        _intent(ledger, "AAPL", "buy", MONDAY, 1)
        source = FakePriceSource({("AAPL", MONDAY): _bar("AAPL")})

        def crash_after_commit(phase):
            if phase == "validate_market_data":
                raise RuntimeError("partial crash")

        with pytest.raises(RuntimeError, match="partial crash"):
            SessionExecutor(
                ledger, _config(), after_phase_commit=crash_after_commit
            ).execute_open_and_mark(MONDAY, "epoch", source, {}, PROCESSED)

        executor = SessionExecutor(ledger, _config())
        persisted = executor.persisted_input_bundle(MONDAY)
        resumed = executor.execute_open_and_mark(
            MONDAY,
            "epoch",
            persisted,
            {},
            datetime(2026, 8, 10, 22, tzinfo=UTC),
        )
        assert resumed.valid
        assert resumed.snapshot is not None
        assert len(ledger.read_fills(MONDAY, MONDAY)) == 1
    finally:
        ledger.close()


@pytest.mark.parametrize("drift", ["config", "borrow"])
def test_partial_resume_rejects_effective_config_or_borrow_drift_before_market_io(
    tmp_path, drift
):
    ledger = _ledger(tmp_path)
    try:
        _intent(ledger, "AAPL", "buy", MONDAY, 1)
        initial_borrow = {"AAPL": Decimal("0.01")}

        def crash_after_commit(phase):
            if phase == "validate_market_data":
                raise RuntimeError("partial crash")

        with pytest.raises(RuntimeError, match="partial crash"):
            SessionExecutor(
                ledger, _config(), after_phase_commit=crash_after_commit
            ).execute_open_and_mark(
                MONDAY,
                "epoch",
                FakePriceSource({("AAPL", MONDAY): _bar("AAPL")}),
                initial_borrow,
                PROCESSED,
            )

        resumed_config = _config()
        resumed_borrow = initial_borrow
        if drift == "config":
            resumed_config["autoresearch"]["paper_ledger"]["slippage_bps"] = "20"
        else:
            resumed_borrow = {"AAPL": Decimal("0.02")}

        class NoMarketIO:
            def __getattr__(self, name):
                raise AssertionError(f"context drift attempted market I/O: {name}")

        result = SessionExecutor(ledger, resumed_config).execute_open_and_mark(
            MONDAY, "epoch", NoMarketIO(), resumed_borrow, PROCESSED
        )
        assert not result.valid
        assert "effective config or borrow" in result.invalid_reason
    finally:
        ledger.close()


def test_omitted_cost_default_drift_changes_effective_policy_digest(
    tmp_path, monkeypatch
):
    ledger = _ledger(tmp_path)
    try:
        _intent(ledger, "AAPL", "buy", MONDAY, 1)
        config = _config()
        del config["autoresearch"]["paper_ledger"]["slippage_bps"]

        def crash_after_commit(phase):
            if phase == "validate_market_data":
                raise RuntimeError("partial crash")

        with pytest.raises(RuntimeError, match="partial crash"):
            SessionExecutor(
                ledger, config, after_phase_commit=crash_after_commit
            ).execute_open_and_mark(
                MONDAY,
                "epoch",
                FakePriceSource({("AAPL", MONDAY): _bar("AAPL")}),
                {},
                PROCESSED,
            )

        monkeypatch.setitem(PaperCostModel.DEFAULTS, "slippage_bps", "20")
        resumed = SessionExecutor(ledger, config).execute_open_and_mark(
            MONDAY,
            "epoch",
            SessionExecutor(ledger, config).persisted_input_bundle(MONDAY),
            {},
            PROCESSED,
        )

        assert not resumed.valid
        assert "effective config" in resumed.invalid_reason
        assert ledger.read_fills(MONDAY, MONDAY) == []
    finally:
        ledger.close()


def test_bound_context_rehydrates_canonical_nonempty_borrow_inputs(tmp_path):
    ledger = _ledger(tmp_path)
    try:
        _intent(ledger, "AAPL", "buy", MONDAY, 1)
        borrow = {"AAPL": Decimal("0.0125")}

        def crash_after_commit(phase):
            if phase == "validate_market_data":
                raise RuntimeError("partial crash")

        with pytest.raises(RuntimeError, match="partial crash"):
            SessionExecutor(
                ledger, _config(), after_phase_commit=crash_after_commit
            ).execute_open_and_mark(
                MONDAY,
                "epoch",
                FakePriceSource({("AAPL", MONDAY): _bar("AAPL")}),
                borrow,
                PROCESSED,
            )

        executor = SessionExecutor(ledger, _config())
        assert executor.persisted_borrow_rates(MONDAY) == borrow
        resumed = executor.execute_open_and_mark(
            MONDAY,
            "epoch",
            executor.persisted_input_bundle(MONDAY),
            executor.persisted_borrow_rates(MONDAY),
            PROCESSED,
        )
        assert resumed.valid
    finally:
        ledger.close()


def test_shared_action_batch_isolates_malformed_member_to_affected_cohort(tmp_path):
    (tmp_path / "aapl").mkdir()
    (tmp_path / "msft").mkdir()
    aapl_ledger = _ledger(tmp_path / "aapl", cash="3000")
    msft_ledger = _ledger(tmp_path / "msft", cash="3000")
    try:
        _open_long(aapl_ledger, "AAPL", 1)
        _open_long(msft_ledger, "MSFT", 1)
        actions = (
            CorporateAction(
                "dividend-aapl",
                "AAPL",
                MONDAY,
                "cash_dividend",
                None,
                Decimal("1"),
                "fixture",
                PROCESSED,
                True,
            ),
            CorporateAction(
                "dividend-msft-malformed",
                "MSFT",
                MONDAY,
                "cash_dividend",
                None,
                Decimal("1"),
                "fixture",
                PROCESSED,
                False,
            ),
        )
        shared = SessionInputBundle(
            MONDAY,
            ("AAPL", "MSFT"),
            {
                ("AAPL", MONDAY): _bar("AAPL"),
                ("MSFT", MONDAY): _bar("MSFT"),
            },
            actions,
            FakePriceSource().adjusted,
        )
        SessionExecutor.validate_shared_action_response(
            shared.actions, shared.tickers, MONDAY
        )

        aapl = SessionExecutor(aapl_ledger, _config()).execute_open_and_mark(
            MONDAY, "epoch", shared.for_tickers(("AAPL",)), {}, PROCESSED
        )
        msft = SessionExecutor(msft_ledger, _config()).execute_open_and_mark(
            MONDAY, "epoch", shared.for_tickers(("MSFT",)), {}, PROCESSED
        )

        assert aapl.valid
        assert aapl.snapshot is not None
        assert aapl.snapshot.dividend_cash == Decimal("1.0000")
        assert not msft.valid
        assert "unverified" in msft.invalid_reason
        assert not aapl_ledger.session_invalid_reason(MONDAY)
    finally:
        aapl_ledger.close()
        msft_ledger.close()


def test_partial_resume_accepts_economically_identical_refetch_with_new_provenance(
    tmp_path,
):
    ledger = _ledger(tmp_path)
    try:
        _intent(ledger, "AAPL", "buy", MONDAY, 1)

        def crash_after_commit(phase):
            if phase == "validate_market_data":
                raise RuntimeError("partial crash")

        with pytest.raises(RuntimeError, match="partial crash"):
            SessionExecutor(
                ledger, _config(), after_phase_commit=crash_after_commit
            ).execute_open_and_mark(
                MONDAY,
                "epoch",
                FakePriceSource({("AAPL", MONDAY): _bar("AAPL")}),
                {},
                PROCESSED,
            )
        original_context = ledger.session_execution_context(MONDAY)

        refetched_at = datetime(2026, 8, 3, 22, 30, tzinfo=UTC)
        refetched_bar = MarketBar(
            **{**_bar("AAPL").__dict__, "fetched_at": refetched_at}
        )
        refetched = FakePriceSource(
            {("AAPL", MONDAY): refetched_bar},
            adjusted={
                ("SPY", MONDAY): AdjustedClose(
                    "SPY", MONDAY, Decimal("650.25"), "fixture-adjusted", refetched_at
                ),
                ("BIL", MONDAY): AdjustedClose(
                    "BIL", MONDAY, Decimal("91.10"), "fixture-adjusted", refetched_at
                ),
            },
        )
        resumed = SessionExecutor(ledger, _config()).execute_open_and_mark(
            MONDAY,
            "epoch",
            refetched,
            {},
            datetime(2026, 8, 3, 23, tzinfo=UTC),
        )
        assert resumed.valid
        assert (
            ledger.session_execution_context(MONDAY)["input_digest"]
            == original_context["input_digest"]
        )
    finally:
        ledger.close()


def test_preclose_raw_adjusted_and_action_inputs_fail_before_phases(tmp_path):
    for unsafe in ("raw", "benchmark", "action"):
        case = tmp_path / unsafe
        case.mkdir()
        ledger = _ledger(case)
        try:
            entry = _intent(ledger, "AAPL", "buy", MONDAY, 1)
            bar = _bar("AAPL")
            adjusted = {"SPY": "650", "BIL": "91"}
            actions = []
            if unsafe == "raw":
                bar = MarketBar(
                    **{
                        **bar.__dict__,
                        "fetched_at": datetime(2026, 8, 3, 19, tzinfo=UTC),
                    }
                )
            if unsafe == "benchmark":
                adjusted = {
                    ("SPY", MONDAY): AdjustedClose(
                        "SPY",
                        MONDAY,
                        Decimal("650"),
                        "fixture-adjusted",
                        datetime(2026, 8, 3, 19, tzinfo=UTC),
                    ),
                    ("BIL", MONDAY): AdjustedClose(
                        "BIL", MONDAY, Decimal("91"), "fixture-adjusted", PROCESSED
                    ),
                }
            if unsafe == "action":
                actions = [
                    CorporateAction(
                        "action-preclose",
                        "AAPL",
                        MONDAY,
                        "cash_dividend",
                        None,
                        Decimal("1"),
                        "fixture",
                        datetime(2026, 8, 3, 19, tzinfo=UTC),
                        True,
                    )
                ]
            result = SessionExecutor(ledger, _config()).execute_open_and_mark(
                MONDAY,
                "epoch",
                FakePriceSource(
                    {("AAPL", MONDAY): bar}, actions=actions, adjusted=adjusted
                ),
                {},
                PROCESSED,
            )
            assert not result.valid
            assert "pre-close" in result.invalid_reason
            assert ledger.intent(entry.intent_id).status == "cancelled"
            assert (
                ledger.connection.execute(
                    "SELECT COUNT(*) FROM session_phases"
                ).fetchone()[0]
                == 0
            )
        finally:
            ledger.close()


def test_exit_fill_closes_only_its_durably_owned_lot(tmp_path):
    ledger = _ledger(tmp_path, cash="2000")
    try:
        first = _open_long(ledger, "AAPL", 2)
        second = _open_long(ledger, "AAPL", 3)
        lots = ledger.connection.execute(
            """SELECT l.lot_id, l.open_qty, f.intent_id FROM lots l
               JOIN fills f ON f.fill_id = l.fill_id ORDER BY l.original_qty"""
        ).fetchall()
        owned = next(row for row in lots if row["intent_id"] == second.intent_id)
        second_signal_ids = tuple(
            signal.signal_id for signal in ledger.signals_for_intent(second.intent_id)
        )
        exit_intent = OrderIntent(
            stable_id("owned_exit", owned["lot_id"], MONDAY),
            second_signal_ids,
            ledger.cohort_id,
            "sell",
            3,
            session_close(FRIDAY),
            MONDAY,
            "next_session_open",
            "pending",
            None,
            None,
        )
        ledger.stage_exit_intent(exit_intent, ((owned["lot_id"], 3),))
        with pytest.raises(ValueError, match="already has a pending exit"):
            ledger.stage_exit_intent(
                OrderIntent(
                    stable_id("other_exit", owned["lot_id"], MONDAY),
                    second_signal_ids,
                    ledger.cohort_id,
                    "sell",
                    1,
                    session_close(FRIDAY),
                    MONDAY,
                    "next_session_open",
                    "pending",
                    None,
                    None,
                ),
                ((owned["lot_id"], 1),),
            )

        result = SessionExecutor(ledger, _config()).execute_open_and_mark(
            MONDAY,
            "epoch",
            FakePriceSource({("AAPL", MONDAY): _bar("AAPL")}),
            {},
            PROCESSED,
        )
        remaining = ledger.connection.execute(
            "SELECT lot_id, open_qty FROM lots ORDER BY original_qty"
        ).fetchall()
        closure = ledger.connection.execute(
            "SELECT lot_id, quantity FROM lot_closures"
        ).fetchone()
        assert result.valid
        assert [(row["open_qty"]) for row in remaining] == [2, 0]
        assert closure["lot_id"] == owned["lot_id"]
        assert closure["quantity"] == 3
        assert ledger.intent(first.intent_id).status == "filled"
    finally:
        ledger.close()


@pytest.mark.parametrize("price_rule", ["resting_stop", "next_session_open"])
@pytest.mark.parametrize("lot_quantities", [(2,), (1, 2)])
def test_split_scales_every_owned_exit_allocation_and_replays(
    tmp_path, price_rule, lot_quantities
):
    ledger = _ledger(tmp_path, cash="3000")
    try:
        opened = [_open_long(ledger, "AAPL", qty) for qty in lot_quantities]
        lots = ledger.connection.execute(
            "SELECT lot_id, original_qty FROM lots ORDER BY original_qty, lot_id"
        ).fetchall()
        for index, lot in enumerate(lots):
            signal_ids = tuple(
                signal.signal_id
                for signal in ledger.signals_for_intent(opened[index].intent_id)
            )
            stop_price = Decimal("90") if price_rule == "resting_stop" else None
            exit_intent = OrderIntent(
                stable_id("split_exit", lot["lot_id"], price_rule),
                signal_ids,
                ledger.cohort_id,
                "sell",
                int(lot["original_qty"]),
                session_close(FRIDAY),
                MONDAY,
                price_rule,
                "pending",
                stop_price,
                None,
            )
            ledger.stage_exit_intent(
                exit_intent, ((lot["lot_id"], int(lot["original_qty"])),)
            )

        split = CorporateAction(
            "split-aapl-2x",
            "AAPL",
            MONDAY,
            "split",
            Decimal("2"),
            None,
            "fixture-action",
            PROCESSED,
            True,
        )
        bar = _bar("AAPL", open_="44", close="45")
        result = SessionExecutor(ledger, _config()).execute_open_and_mark(
            MONDAY,
            "epoch",
            FakePriceSource({("AAPL", MONDAY): bar}, actions=[split]),
            {},
            PROCESSED,
        )
        allocations = ledger.connection.execute(
            "SELECT quantity FROM exit_intent_lots ORDER BY quantity"
        ).fetchall()
        fills = ledger.read_fills(MONDAY, MONDAY)
        assert result.valid
        assert [row["quantity"] for row in allocations] == sorted(
            qty * 2 for qty in lot_quantities
        )
        assert (
            sum(fill.quantity for fill in fills if fill.side == "sell")
            == sum(lot_quantities) * 2
        )
        assert all(
            row["open_qty"] == 0
            for row in ledger.connection.execute("SELECT open_qty FROM lots")
        )

        replay = SessionExecutor(ledger, _config()).execute_open_and_mark(
            MONDAY,
            "epoch",
            FakePriceSource({("AAPL", MONDAY): bar}, actions=[split]),
            {},
            PROCESSED,
        )
        assert replay.valid
        assert len(ledger.read_fills(MONDAY, MONDAY)) == len(lot_quantities)
    finally:
        ledger.close()


def test_identical_duplicate_three_for_two_split_applies_once_and_replays(tmp_path):
    ledger = _ledger(tmp_path, cash="3000")
    try:
        _open_long(ledger, "AAPL", 2)
        split = CorporateAction(
            "split-aapl-3-for-2-once",
            "AAPL",
            MONDAY,
            "split",
            Decimal("1.5"),
            None,
            "fixture-action",
            PROCESSED,
            True,
        )
        source = FakePriceSource(
            {("AAPL", MONDAY): _bar("AAPL", open_="66", close="67")},
            actions=[split, split],
        )

        first = SessionExecutor(ledger, _config()).execute_open_and_mark(
            MONDAY, "epoch", source, {}, PROCESSED
        )

        assert first.valid
        assert first.snapshot is not None
        lot = ledger.connection.execute(
            "SELECT original_qty, open_qty, entry_price FROM lots"
        ).fetchone()
        assert (lot["original_qty"], lot["open_qty"]) == (3, 3)
        assert Decimal(lot["entry_price"]) == Decimal("100") / Decimal("1.5")
        assert (
            ledger.connection.execute(
                "SELECT COUNT(*) FROM corporate_actions"
            ).fetchone()[0]
            == 1
        )
        assert (
            ledger.connection.execute(
                "SELECT COUNT(*) FROM lot_action_applications"
            ).fetchone()[0]
            == 1
        )

        replay = SessionExecutor(ledger, _config()).execute_open_and_mark(
            MONDAY, "epoch", source, {}, PROCESSED
        )
        replay_lot = ledger.connection.execute(
            "SELECT original_qty, open_qty, entry_price FROM lots"
        ).fetchone()
        assert replay.valid
        assert replay.snapshot == first.snapshot
        assert tuple(replay_lot) == tuple(lot)
        assert (
            ledger.connection.execute(
                "SELECT COUNT(*) FROM lot_action_applications"
            ).fetchone()[0]
            == 1
        )
    finally:
        ledger.close()


def test_split_rejects_fractional_per_lot_allocation_even_when_total_is_integral(
    tmp_path,
):
    ledger = _ledger(tmp_path, cash="3000")
    try:
        first = _open_long(ledger, "AAPL", 2)
        _open_long(
            ledger,
            "AAPL",
            2,
            reference=date(2026, 7, 30),
            opened=FRIDAY,
        )
        lots = ledger.connection.execute(
            "SELECT lot_id FROM lots ORDER BY lot_id"
        ).fetchall()
        signal_ids = tuple(
            signal.signal_id for signal in ledger.signals_for_intent(first.intent_id)
        )
        exit_intent = OrderIntent(
            stable_id("fractional_owned_exit", MONDAY),
            signal_ids,
            ledger.cohort_id,
            "sell",
            2,
            session_close(FRIDAY),
            MONDAY,
            "next_session_open",
            "pending",
            None,
            None,
        )
        ledger.stage_exit_intent(exit_intent, tuple((row["lot_id"], 1) for row in lots))
        split = CorporateAction(
            "split-aapl-3-for-2",
            "AAPL",
            MONDAY,
            "split",
            Decimal("1.5"),
            None,
            "fixture-action",
            PROCESSED,
            True,
        )

        ledger.apply_corporate_actions(MONDAY, [split], PROCESSED)

        assert ledger.session_invalid_reason(MONDAY)
        assert [
            row["open_qty"]
            for row in ledger.connection.execute("SELECT open_qty FROM lots")
        ] == [2, 2]
        assert ledger.intent(exit_intent.intent_id).requested_qty == 2
        assert [
            row["quantity"]
            for row in ledger.connection.execute(
                "SELECT quantity FROM exit_intent_lots ORDER BY lot_id"
            )
        ] == [1, 1]
    finally:
        ledger.close()


@pytest.mark.parametrize("open_side", ["buy", "short"])
def test_restart_risk_gate_counts_every_open_lot_contributor_membership(
    tmp_path, open_side
):
    path = tmp_path / "ledger.db"
    ledger = PortfolioLedger(path, "cohort", Decimal("5000"))
    opened = date(2026, 7, 30)
    reference = date(2026, 7, 29)
    direction = "short" if open_side == "short" else "long"
    try:
        contributors = tuple(
            _signal("AAPL", reference, strategy=strategy, direction=direction)
            for strategy in ("alpha", "beta")
        )
        for signal in contributors:
            ledger.record_signal(signal)
        intent = OrderIntent(
            stable_id("multi_contributor_intent", open_side),
            tuple(signal.signal_id for signal in contributors),
            ledger.cohort_id,
            open_side,
            2,
            session_close(reference),
            opened,
            "next_session_open",
            "pending",
            None,
            None,
        )
        ledger.stage_intent(intent)
        ledger.apply_fill(
            intent,
            Fill(
                stable_id("multi_contributor_fill", open_side),
                intent.intent_id,
                open_side,
                opened,
                datetime(2026, 7, 30, 13, 30, tzinfo=UTC),
                datetime(2026, 7, 30, 22, tzinfo=UTC),
                Decimal("100"),
                Decimal("100"),
                2,
                Decimal("0"),
                Decimal("0"),
                Decimal("0"),
            ),
            borrow_rate=Decimal("0.01") if open_side == "short" else None,
        )
    finally:
        ledger.close()

    ledger = PortfolioLedger(path, "cohort", Decimal("5000"))
    try:
        candidate = _intent(
            ledger,
            "MSFT",
            "buy",
            MONDAY,
            2,
            strategy="beta",
        )
        config = _config(per_strategy_max=1)
        result = SessionExecutor(ledger, config).execute_open_and_mark(
            MONDAY,
            "epoch",
            FakePriceSource(
                {
                    ("AAPL", MONDAY): _bar("AAPL"),
                    ("MSFT", MONDAY): _bar("MSFT"),
                }
            ),
            {"AAPL": Decimal("0.01")},
            PROCESSED,
        )
        assert result.valid
        assert ledger.intent(candidate.intent_id).status == "rejected"
        transition = ledger.connection.execute(
            """SELECT reason FROM order_status_transitions
               WHERE intent_id = ? ORDER BY occurred_at DESC LIMIT 1""",
            (candidate.intent_id,),
        ).fetchone()
        assert "per_strategy_max: beta" in transition["reason"]
    finally:
        ledger.close()


class _NeverExitStrategy:
    name = "strategy"

    def get_default_params(self, horizon="30d"):
        return {}

    def check_exit(self, **kwargs):
        return False, ""


def _production_finnhub_payload(tmp_path):
    from tradingagents.strategies.data_sources.finnhub_source import FinnhubSource
    from tradingagents.strategies.data_sources.registry import DataSourceRegistry

    published_at = datetime(2026, 7, 31, 19, tzinfo=UTC)
    raw_news = {
        "headline": "Quantum milestone after supply chain disruption closes factory",
        "summary": "PQC deadline and quantum-safe migration after shortage",
        "source": "fixture-wire",
        "datetime": int(published_at.timestamp()),
        "url": "https://example.test/production-news-1",
        "category": "company",
    }
    adapter = FinnhubSource(api_key="fixture-key")
    with (
        patch.object(adapter, "_wait_for_request_slot", return_value=True),
        patch.object(adapter, "_call_with_retry", return_value=[raw_news]),
    ):
        normalized_news = adapter.fetch_company_news(
            "CRWD", "2026-07-24", FRIDAY.isoformat()
        )

    source = MagicMock()
    source.name = "finnhub"
    source.is_available.return_value = True
    source.new_workflow_deadline.return_value = 30.0
    source.fetch_recent_earnings.return_value = [
        {
            "symbol": "AAPL",
            "date": FRIDAY.isoformat(),
            "year": 2026,
            "quarter": 2,
            "epsActual": 2.0,
            "epsEstimate": 1.0,
        }
    ]
    source.fetch_earnings_news.side_effect = lambda *args, **kwargs: [
        dict(item) for item in normalized_news
    ]
    source.fetch_company_news.side_effect = lambda *args, **kwargs: [
        dict(item) for item in normalized_news
    ]
    source.fetch_supply_chains.return_value = {}
    registry = DataSourceRegistry()
    registry.register(source)
    fetch_engine = MultiStrategyEngine(
        config={
            "autoresearch": {
                "state_dir": str(tmp_path / "fetch-state"),
                "total_capital": 1000,
            }
        },
        strategies=[_NeverExitStrategy()],
        registry=registry,
        state_manager=StateManager(str(tmp_path / "fetch-state")),
    )
    return fetch_engine._fetch_finnhub_data(FRIDAY.isoformat()), normalized_news


def _stage_real_strategy_candidate(tmp_path, strategy_name, data):
    from tradingagents.strategies.modules import get_paper_trade_strategies

    strategy = next(
        item for item in get_paper_trade_strategies() if item.name == strategy_name
    )
    candidates = strategy.screen(
        data, FRIDAY.isoformat(), strategy.get_default_params("30d")
    )
    assert candidates, f"production-shaped {strategy_name} data produced no candidate"
    candidate = candidates[0]
    state_dir = str(tmp_path / f"stage-{strategy_name}")
    config = _config()
    config["autoresearch"].update({"state_dir": state_dir, "horizon": "30d"})
    ledger = PortfolioLedger(
        Path(state_dir) / "portfolio.db", "cohort", Decimal("1000")
    )
    try:
        lifecycle = SessionExecutor(ledger, config).execute_open_and_mark(
            FRIDAY,
            "epoch",
            FakePriceSource(
                adjusted={
                    ("SPY", FRIDAY): Decimal("649"),
                    ("BIL", FRIDAY): Decimal("91"),
                }
            ),
            {},
            datetime(2026, 7, 31, 22, tzinfo=UTC),
        )
        assert lifecycle.snapshot is not None
        engine = MultiStrategyEngine(
            config=config,
            strategies=[strategy],
            state_manager=StateManager(state_dir),
            ledger=ledger,
        )
        shared_signal = {
            "ticker": candidate.ticker,
            "direction": candidate.direction,
            "score": candidate.score,
            "strategy": strategy_name,
            "metadata": candidate.metadata,
            "event_key": candidate.event_key,
            "source_event_keys": candidate.source_event_keys,
            "strategy_tags": candidate.strategy_tags,
            "risk_tags": candidate.risk_tags,
            "journal_only": candidate.journal_only,
        }
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            return_value=[],
        ):
            result = engine.screen_and_stage(
                FRIDAY.isoformat(),
                {
                    "_execution_reference_bars": {
                        candidate.ticker: _bar(candidate.ticker, FRIDAY)
                    }
                },
                [shared_signal],
                {},
                {},
                None,
                lifecycle.snapshot,
            )
        assert len(result["signals"]) == 1
        assert result["signals"][0]["strategy"] == strategy_name
        return candidate, result
    finally:
        ledger.close()


def test_production_finnhub_earnings_normalization_stages_real_candidate(tmp_path):
    payload, _ = _production_finnhub_payload(tmp_path)

    assert payload["transcripts"][0]["published_at"] == "2026-07-31T19:00:00+00:00"
    _stage_real_strategy_candidate(tmp_path, "earnings_call", {"finnhub": payload})


def test_production_finnhub_supply_chain_normalization_stages_real_candidate(
    tmp_path,
):
    payload, normalized_news = _production_finnhub_payload(tmp_path)

    assert normalized_news[0]["published_at"] == "2026-07-31T19:00:00+00:00"
    _stage_real_strategy_candidate(tmp_path, "supply_chain", {"finnhub": payload})


def test_production_finnhub_quantum_normalization_stages_real_candidate(tmp_path):
    payload, _ = _production_finnhub_payload(tmp_path)

    assert payload["pqc_news"][0]["published_at"] == "2026-07-31T19:00:00+00:00"
    _stage_real_strategy_candidate(tmp_path, "quantum_readiness", {"finnhub": payload})


def test_production_congress_pub_date_normalization_stages_real_candidate(tmp_path):
    from tradingagents.strategies.data_sources.congress_source import (
        _normalize_fmp_trade,
    )

    normalized = _normalize_fmp_trade(
        {
            "symbol": "AAPL",
            "assetDescription": "Apple Inc.",
            "transactionDate": "2026-07-25",
            "disclosureDate": "2026-07-30",
            "type": "Purchase",
            "amount": "$15,001 - $50,000",
            "office": "Member One",
            "link": "https://example.test/disclosure-1",
        },
        "house",
    )

    assert normalized["publication_date"] == normalized["pub_date"]
    second = _normalize_fmp_trade(
        {
            "symbol": "AAPL",
            "assetDescription": "Apple Inc.",
            "transactionDate": "2026-07-25",
            "disclosureDate": "2026-07-30",
            "type": "Purchase",
            "amount": "$15,001 - $50,000",
            "office": "Member Two",
            "link": "https://example.test/disclosure-2",
        },
        "house",
    )
    candidate, result = _stage_real_strategy_candidate(
        tmp_path,
        "congressional_trades",
        {"congress": {"recent_trades": [normalized, second]}},
    )
    assert result["signals"][0]["event_key"] == candidate.event_key


def test_production_usaspending_availability_stages_real_candidate(tmp_path):
    from tradingagents.strategies.data_sources.usaspending_source import (
        USASpendingSource,
    )

    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "results": [
            {
                "Award ID": "AWARD-1",
                "Recipient Name": "Lockheed Martin",
                "Award Amount": 50_000_000,
                "Awarding Agency": "DOD",
                "Start Date": "2026-07-01",
                "Last Modified Date": "2026-07-30",
                "Description": "Production contract",
            }
        ]
    }
    source = USASpendingSource()
    with patch("requests.post", return_value=response):
        contracts = source.search_contracts(
            date_from="2026-07-01", date_to=FRIDAY.isoformat()
        )

    assert contracts[0]["last_modified_date"] == "2026-07-30"
    registry = MagicMock()
    registry.get.side_effect = lambda name: source if name == "usaspending" else None
    source.get_recent_large_contracts = MagicMock(return_value=contracts)
    fetch_engine = MultiStrategyEngine(
        config={"autoresearch": {"state_dir": str(tmp_path / "usa-fetch")}},
        strategies=[_NeverExitStrategy()],
        registry=registry,
        state_manager=StateManager(str(tmp_path / "usa-fetch")),
    )
    payload = fetch_engine._fetch_usaspending_data(FRIDAY.isoformat())
    _stage_real_strategy_candidate(tmp_path, "govt_contracts", {"usaspending": payload})


def test_exit_specs_cover_each_lot_once_and_replay_without_duplicates(tmp_path):
    state_dir = str(tmp_path / "state")
    config = _config()
    config["autoresearch"]["state_dir"] = state_dir
    config["autoresearch"]["horizon"] = "30d"
    ledger = PortfolioLedger(
        Path(state_dir) / "portfolio.db", "cohort", Decimal("2000")
    )
    try:
        _open_long(ledger, "AAPL", 2)
        _open_long(ledger, "AAPL", 3)
        engine = MultiStrategyEngine(
            config=config,
            strategies=[_NeverExitStrategy()],
            state_manager=StateManager(state_dir),
            ledger=ledger,
        )
        args = (
            FRIDAY,
            session_close(FRIDAY),
            MONDAY,
            {"AAPL": _bar("AAPL", FRIDAY, open_="99", close="100")},
            {},
            "30d",
        )

        specs, cancellations = engine._build_exit_specs(*args)
        assert cancellations == []
        assert len(specs) == 2
        assert sum(intent.requested_qty for intent, _ in specs) == 5
        ownership = [ownership for _, ownership in specs]
        assert len({lot_id for owned in ownership for lot_id, _ in owned}) == 2
        assert sum(quantity for owned in ownership for _, quantity in owned) == 5

        for intent, owned in specs:
            ledger.stage_exit_intent(intent, owned)

        replay_specs, replay_cancellations = engine._build_exit_specs(*args)
        pending = ledger.pending_exit_intents("AAPL")
        durable_ownership = ledger.connection.execute(
            "SELECT lot_id, quantity FROM exit_intent_lots ORDER BY lot_id"
        ).fetchall()
        assert replay_specs == []
        assert replay_cancellations == []
        assert len(pending) == 2
        assert sum(intent.requested_qty for intent in pending) == 5
        assert len({row["lot_id"] for row in durable_ownership}) == 2
        assert sum(row["quantity"] for row in durable_ownership) == 5
    finally:
        ledger.close()


def test_screen_and_stage_persists_all_events_partitions_late_and_replays(tmp_path):
    state_dir = str(tmp_path / "state")
    config = _config()
    config["autoresearch"]["state_dir"] = state_dir
    config["autoresearch"]["horizon"] = "30d"
    ledger = PortfolioLedger(
        Path(state_dir) / "portfolio.db", "cohort", Decimal("1000")
    )
    try:
        friday_source = FakePriceSource(
            adjusted={
                ("SPY", FRIDAY): Decimal("649"),
                ("BIL", FRIDAY): Decimal("91"),
            }
        )
        lifecycle = SessionExecutor(ledger, config).execute_open_and_mark(
            FRIDAY,
            "epoch",
            friday_source,
            {},
            datetime(2026, 7, 31, 22, tzinfo=UTC),
        )
        assert lifecycle.snapshot is not None
        engine = MultiStrategyEngine(
            config=config,
            strategies=[_NeverExitStrategy()],
            state_manager=StateManager(state_dir),
            ledger=ledger,
        )
        reference = _bar("AAPL", FRIDAY, open_="99", close="100")
        signals = [
            {
                "ticker": "AAPL",
                "direction": "long",
                "score": 2.0,
                "strategy": "strategy",
                "metadata": {
                    "event_key": "timely-event",
                    "observed_at": "2026-07-31T19:30:00+00:00",
                    "llm_analysis": {"conviction": 0.7},
                },
            },
            {
                "ticker": "AAPL",
                "direction": "long",
                "score": 2.5,
                "strategy": "strategy",
                "metadata": {
                    "event_key": "second-timely-event",
                    "observed_at": "2026-07-31T19:45:00+00:00",
                },
            },
            {
                "ticker": "AAPL",
                "direction": "long",
                "score": 3.0,
                "strategy": "strategy",
                "metadata": {
                    "event_key": "late-event",
                    "observed_at": "2026-07-31T20:30:00+00:00",
                },
            },
        ]
        recommendation = TradeRecommendation(
            "AAPL", "long", 0.20, 0.8, "test", ["strategy"]
        )
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            return_value=[recommendation],
        ):
            result = engine.screen_and_stage(
                FRIDAY.isoformat(),
                {"_execution_reference_bars": {"AAPL": reference}},
                signals,
                {"overall_regime": "normal"},
                {},
                None,
                lifecycle.snapshot,
            )

        records = ledger.read_signals(FRIDAY, FRIDAY)
        intents = ledger.pending_intents(MONDAY)
        journal = engine._journal.get_entries()
        assert len(records) == 3
        assert len(result["cutoff_late"]) == 1
        assert len(intents) == 1
        timely_signal_ids = {
            record.signal_id
            for record in records
            if record.event_key in {"timely-event", "second-timely-event"}
        }
        assert set(intents[0].signal_ids) == timely_signal_ids
        assert {entry["status"] for entry in journal} == {"timely", "cutoff-late"}
        assert all(entry["entry_price"] is None for entry in journal)
        assert ledger.read_fills() == []

        signals[0]["metadata"]["llm_analysis"] = {"conviction": 0.1}
        replay = engine.screen_and_stage(
            FRIDAY.isoformat(),
            {"_execution_reference_bars": {"AAPL": reference}},
            signals,
            {"overall_regime": "normal"},
            {},
            None,
            lifecycle.snapshot,
        )
        assert replay["replayed"]
        assert len(ledger.read_signals(FRIDAY, FRIDAY)) == 3
        assert len(ledger.pending_intents(MONDAY)) == 1
        assert len(engine._journal.get_entries()) == 3
    finally:
        ledger.close()


def test_profile_bound_policy_stages_with_provenance_and_revalidates_at_fill(
    tmp_path,
) -> None:
    state_dir = str(tmp_path / "state")
    config = deepcopy(DEFAULT_CONFIG)
    config["autoresearch"].update(
        {"state_dir": state_dir, "horizon": "30d", "total_capital": 5000}
    )
    config["autoresearch"]["paper_trade"]["portfolio_committee_enabled"] = False
    profile = SIZE_PROFILES["5k"]
    config["autoresearch"]["risk_gate"].update(
        {
            "max_positions": profile.max_positions,
            "max_position_pct": profile.max_position_pct,
            "min_position_value": profile.min_position_value,
            "cash_reserve_pct": profile.cash_reserve_pct,
            "long_only": True,
        }
    )
    ledger = PortfolioLedger(
        Path(state_dir) / "portfolio.db", "cohort", Decimal("5000")
    )
    try:
        friday = SessionExecutor(
            ledger, config, size_profile=profile
        ).execute_open_and_mark(
            FRIDAY,
            "epoch",
            FakePriceSource(
                adjusted={
                    ("SPY", FRIDAY): Decimal("649"),
                    ("BIL", FRIDAY): Decimal("91"),
                }
            ),
            {},
            datetime(2026, 7, 31, 22, tzinfo=UTC),
        )
        assert friday.snapshot is not None
        engine = MultiStrategyEngine(
            config=config,
            strategies=[_NeverExitStrategy()],
            state_manager=StateManager(state_dir),
            ledger=ledger,
        )
        candidate = {
            "ticker": "AAPL",
            "direction": "long",
            "score": 2.0,
            "strategy": "strategy",
            "event_key": "event-aapl",
            "source_event_keys": ("native-aapl",),
            "strategy_tags": ("strategy",),
            "risk_tags": ("event:aapl",),
            "metadata": {
                "event_key": "event-aapl",
                "observed_at": "2026-07-31T19:30:00+00:00",
            },
        }

        def committee_output(weight: float):
            def synthesize(committee, **_kwargs):
                committee.last_policy_decisions = (
                    PortfolioPolicyDecision(
                        "AAPL",
                        "long",
                        "event-aapl",
                        "accepted",
                        "accepted",
                        weight,
                        weight,
                    ),
                )
                return [
                    TradeRecommendation(
                        "AAPL",
                        "long",
                        weight,
                        0.8,
                        "test",
                        ["strategy"],
                        event_key="event-aapl",
                        source_event_keys=("native-aapl",),
                        strategy_tags=("strategy",),
                        risk_tags=("event:aapl",),
                    )
                ]

            return synthesize

        with (
            patch(
                "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
                new=committee_output(0.04),
            ),
            patch.object(
                ledger,
                "record_intent_policy_provenance",
                side_effect=RuntimeError("staging crash"),
            ),
            pytest.raises(RuntimeError, match="staging crash"),
        ):
            engine.screen_and_stage(
                FRIDAY.isoformat(),
                {"_execution_reference_bars": {"AAPL": _bar("AAPL", FRIDAY)}},
                [candidate],
                {},
                {"profiles": {"AAPL": {"sector": "Technology"}}},
                profile,
                friday.snapshot,
            )
        assert ledger.read_policy_candidate_decisions() == ()
        assert ledger.read_policy_staging_audit_manifests() == ()
        assert ledger.pending_intents(MONDAY) == []

        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            new=committee_output(0.03),
        ):
            result = engine.screen_and_stage(
                FRIDAY.isoformat(),
                {"_execution_reference_bars": {"AAPL": _bar("AAPL", FRIDAY)}},
                [candidate],
                {},
                {"profiles": {"AAPL": {"sector": "Technology"}}},
                profile,
                friday.snapshot,
            )
        assert len(result["intents_staged"]) == 1
        decisions = ledger.read_policy_candidate_decisions()
        assert len(decisions) == 1
        assert decisions[0]["approved_weight"] == pytest.approx(0.03)
        assert ledger.read_policy_session_context(
            FRIDAY, binding_kind="staging"
        ) is not None

        replay_engine = MultiStrategyEngine(
            config=deepcopy(config),
            strategies=[_NeverExitStrategy()],
            state_manager=StateManager(state_dir),
            ledger=ledger,
        )
        assert replay_engine._price_cache == {}
        replay = replay_engine.screen_and_stage(
            FRIDAY.isoformat(),
            {"_execution_reference_bars": {}},
            [],
            {},
            {},
            profile,
            friday.snapshot,
        )
        assert replay["replayed"] is True

        policy_changed = deepcopy(config)
        policy_changed["autoresearch"]["portfolio_policy"]["version"] = (
            "portfolio_policy_v2"
        )
        changed_engine = MultiStrategyEngine(
            config=policy_changed,
            strategies=[_NeverExitStrategy()],
            state_manager=StateManager(state_dir),
            ledger=ledger,
        )
        with pytest.raises(LedgerConflictError, match="binding mismatch"):
            changed_engine.screen_and_stage(
                FRIDAY.isoformat(),
                {"_execution_reference_bars": {}},
                [],
                {},
                {},
                profile,
                friday.snapshot,
            )

        policy_removed = deepcopy(config)
        policy_removed["autoresearch"].pop("portfolio_policy")
        replay_engine = MultiStrategyEngine(
            config=policy_removed,
            strategies=[_NeverExitStrategy()],
            state_manager=StateManager(state_dir),
            ledger=ledger,
        )
        with pytest.raises(
            LedgerConflictError, match="policy artifacts.*policy is disabled"
        ):
            replay_engine.screen_and_stage(
                FRIDAY.isoformat(),
                {"_execution_reference_bars": {"AAPL": _bar("AAPL", FRIDAY)}},
                [],
                {},
                {},
                profile,
                friday.snapshot,
            )

        monday = SessionExecutor(
            ledger, config, size_profile=profile
        ).execute_open_and_mark(
            MONDAY,
            "epoch",
            FakePriceSource(
                {("AAPL", MONDAY): _bar("AAPL", MONDAY)},
                adjusted={
                    ("SPY", MONDAY): Decimal("650"),
                    ("BIL", MONDAY): Decimal("91.1"),
                },
            ),
            {},
            PROCESSED,
        )
        assert monday.valid
        assert ledger.intent(result["intents_staged"][0]).status == "filled"
        assert ledger.read_policy_session_context(
            MONDAY, binding_kind="execution"
        ) is not None
    finally:
        ledger.close()


@pytest.mark.parametrize(
    ("borrow_rate", "expected_status", "expected_reason"),
    (
        (None, "rejected", "portfolio_policy:borrow_unavailable"),
        (Decimal("0.01"), "filled", ""),
    ),
)
def test_short_stages_without_borrow_but_fill_requires_bound_availability(
    tmp_path, borrow_rate, expected_status: str, expected_reason: str
) -> None:
    state_dir = str(tmp_path / expected_status)
    config = deepcopy(DEFAULT_CONFIG)
    config["autoresearch"].update(
        {"state_dir": state_dir, "horizon": "3m", "total_capital": 50_000}
    )
    config["autoresearch"]["paper_trade"]["portfolio_committee_enabled"] = False
    profile = SIZE_PROFILES["50k"]
    config["autoresearch"]["risk_gate"].update(
        {
            "max_positions": profile.max_positions,
            "max_position_pct": profile.max_position_pct,
            "min_position_value": profile.min_position_value,
            "cash_reserve_pct": profile.cash_reserve_pct,
            "long_only": False,
        }
    )
    ledger = PortfolioLedger(
        Path(state_dir) / "portfolio.db", "cohort", Decimal("50000")
    )
    try:
        friday = SessionExecutor(
            ledger, config, size_profile=profile
        ).execute_open_and_mark(
            FRIDAY,
            "epoch",
            FakePriceSource(
                adjusted={
                    ("SPY", FRIDAY): Decimal("649"),
                    ("BIL", FRIDAY): Decimal("91"),
                }
            ),
            {},
            datetime(2026, 7, 31, 22, tzinfo=UTC),
        )
        assert friday.snapshot is not None
        engine = MultiStrategyEngine(
            config=config,
            strategies=[_NeverExitStrategy()],
            state_manager=StateManager(state_dir),
            ledger=ledger,
        )
        staged = engine.screen_and_stage(
            FRIDAY.isoformat(),
            {"_execution_reference_bars": {"MSFT": _bar("MSFT", FRIDAY)}},
            [
                {
                    "ticker": "MSFT",
                    "direction": "short",
                    "score": 2.0,
                    "strategy": "strategy",
                    "event_key": "event-msft-short",
                    "source_event_keys": ("native-msft",),
                    "strategy_tags": ("strategy",),
                    "risk_tags": ("event:msft-short",),
                    "metadata": {
                        "event_key": "event-msft-short",
                        "observed_at": "2026-07-31T19:30:00+00:00",
                        "llm_analysis": {"conviction": 0.9},
                    },
                }
            ],
            {},
            {"profiles": {"MSFT": {"sector": "Technology"}}},
            profile,
            friday.snapshot,
        )
        assert len(staged["intents_staged"]) == 1
        intent_id = staged["intents_staged"][0]

        result = SessionExecutor(
            ledger, config, size_profile=profile
        ).execute_open_and_mark(
            MONDAY,
            "epoch",
            FakePriceSource(
                {("MSFT", MONDAY): _bar("MSFT", MONDAY)},
                adjusted={
                    ("SPY", MONDAY): Decimal("650"),
                    ("BIL", MONDAY): Decimal("91.1"),
                },
            ),
            {"MSFT": borrow_rate},
            PROCESSED,
        )
        assert result.valid
        assert ledger.intent(intent_id).status == expected_status
        transitions = ledger.connection.execute(
            "SELECT reason FROM order_status_transitions WHERE intent_id = ?",
            (intent_id,),
        ).fetchall()
        if expected_reason:
            assert transitions[-1]["reason"] == expected_reason
    finally:
        ledger.close()


def test_screen_and_stage_reuses_first_observation_context_across_retry_and_repeat(
    tmp_path,
):
    state_dir = str(tmp_path / "state")
    config = _config()
    config["autoresearch"]["state_dir"] = state_dir
    config["autoresearch"]["horizon"] = "30d"
    ledger = PortfolioLedger(
        Path(state_dir) / "portfolio.db", "cohort", Decimal("1000")
    )
    try:
        friday = SessionExecutor(ledger, config).execute_open_and_mark(
            FRIDAY,
            "epoch",
            FakePriceSource(
                adjusted={
                    ("SPY", FRIDAY): Decimal("649"),
                    ("BIL", FRIDAY): Decimal("91"),
                }
            ),
            {},
            datetime(2026, 7, 31, 22, tzinfo=UTC),
        )
        assert friday.snapshot is not None
        engine = MultiStrategyEngine(
            config=config,
            strategies=[_NeverExitStrategy()],
            state_manager=StateManager(state_dir),
            ledger=ledger,
        )
        first = {
            "ticker": "AAPL",
            "direction": "long",
            "score": 2.0,
            "strategy": "strategy",
            "metadata": {
                "event_key": "immutable-catalyst",
                "observed_at": "2026-07-31T19:30:00+00:00",
                "llm_analysis": {"conviction": 0.7, "rationale": "first"},
            },
        }
        common = {
            "trading_date": FRIDAY.isoformat(),
            "data": {"_execution_reference_bars": {"AAPL": _bar("AAPL", FRIDAY)}},
            "shared_regime": {},
            "enrichment": {},
            "size_profile": None,
            "marked_account": friday.snapshot,
        }
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=RuntimeError("committee crash"),
        ):
            with pytest.raises(RuntimeError, match="committee crash"):
                engine.screen_and_stage(shared_signals=[first], **common)

        changed = json.loads(json.dumps(first))
        changed["score"] = 9.0
        changed["metadata"]["llm_analysis"] = {
            "conviction": 0.1,
            "rationale": "changed",
        }
        first_recommendation = TradeRecommendation(
            "AAPL", "long", 0.20, 0.8, "first", ["strategy"]
        )
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            return_value=[first_recommendation],
        ) as committee:
            retry = engine.screen_and_stage(shared_signals=[changed], **common)

        committee_signal = committee.call_args.kwargs["signals"][0]
        assert committee_signal["score"] == 2.0
        assert committee_signal["metadata"]["llm_analysis"]["conviction"] == 0.7
        assert len(retry["signals"]) == 1
        observation = ledger.signal_observation(retry["signals"][0]["signal_id"])
        assert observation is not None
        assert observation[2]["score"] == 2.0
        assert observation[2]["llm_conviction"] == 0.7
        assert len(retry["intents_staged"]) == 1
        first_intent_id = retry["intents_staged"][0]

        monday = SessionExecutor(ledger, config).execute_open_and_mark(
            MONDAY,
            "epoch",
            FakePriceSource(
                {("AAPL", MONDAY): _bar("AAPL", MONDAY)},
                adjusted={
                    ("SPY", MONDAY): Decimal("650"),
                    ("BIL", MONDAY): Decimal("91.1"),
                },
            ),
            {},
            PROCESSED,
        )
        assert monday.snapshot is not None
        repeat = json.loads(json.dumps(changed))
        repeat["metadata"]["observed_at"] = "2026-08-03T19:30:00+00:00"
        distinct = json.loads(json.dumps(repeat))
        distinct["ticker"] = "MSFT"
        distinct["metadata"]["event_key"] = "distinct-catalyst"
        distinct_recommendation = TradeRecommendation(
            "MSFT", "long", 0.20, 0.8, "distinct", ["strategy"]
        )
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            return_value=[distinct_recommendation],
        ) as later_committee:
            later = engine.screen_and_stage(
                trading_date=MONDAY.isoformat(),
                data={
                    "_execution_reference_bars": {
                        "AAPL": _bar("AAPL", MONDAY),
                        "MSFT": _bar("MSFT", MONDAY),
                    }
                },
                shared_signals=[repeat, distinct],
                shared_regime={},
                enrichment={},
                size_profile=None,
                marked_account=monday.snapshot,
            )

        assert [
            item["ticker"] for item in later_committee.call_args.kwargs["signals"]
        ] == ["MSFT"]
        later_entries = [
            intent_id
            for intent_id in later["intents_staged"]
            if ledger.intent(intent_id).side == "buy"
        ]
        assert len(later_entries) == 1
        assert later_entries[0] != first_intent_id
        assert {
            signal.ticker for signal in ledger.signals_for_intent(later_entries[0])
        } == {"MSFT"}
        assert ledger.intent(first_intent_id).status == "filled"
        aapl_buys = ledger.connection.execute(
            """SELECT i.intent_id FROM order_intents i
               JOIN intent_signals isg ON isg.intent_id = i.intent_id
               JOIN signals s ON s.signal_id = isg.signal_id
               WHERE i.side = 'buy' AND s.ticker = 'AAPL'"""
        ).fetchall()
        assert [row["intent_id"] for row in aapl_buys] == [first_intent_id]
        assert len(ledger.read_signals()) == 2
        original = ledger.signal_observation(retry["signals"][0]["signal_id"])
        assert original is not None
        assert original[0].reference_session == FRIDAY
        assert original[2]["score"] == 2.0
    finally:
        ledger.close()


def test_active_strategy_ignores_caller_event_key_and_requires_source_timing(tmp_path):
    from tradingagents.strategies.orchestration.event_identity import (
        canonical_event_key,
    )

    state_dir = str(tmp_path / "state")
    config = _config()
    config["autoresearch"]["state_dir"] = state_dir
    config["autoresearch"]["horizon"] = "30d"
    ledger = PortfolioLedger(
        Path(state_dir) / "portfolio.db", "cohort", Decimal("1000")
    )
    try:
        lifecycle = SessionExecutor(ledger, config).execute_open_and_mark(
            FRIDAY,
            "epoch",
            FakePriceSource(
                adjusted={
                    ("SPY", FRIDAY): Decimal("649"),
                    ("BIL", FRIDAY): Decimal("91"),
                }
            ),
            {},
            datetime(2026, 7, 31, 22, tzinfo=UTC),
        )
        assert lifecycle.snapshot is not None
        engine = MultiStrategyEngine(
            config=config,
            strategies=[_NeverExitStrategy()],
            state_manager=StateManager(state_dir),
            ledger=ledger,
        )
        metadata = {
            "year": 2026,
            "quarter": 2,
            "published_at": "2026-07-31T19:00:00+00:00",
            "event_key": "caller-controlled-bypass",
        }
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            return_value=[],
        ):
            result = engine.screen_and_stage(
                trading_date=FRIDAY.isoformat(),
                data={"_execution_reference_bars": {"AAPL": _bar("AAPL", FRIDAY)}},
                shared_signals=[
                    {
                        "ticker": "AAPL",
                        "direction": "long",
                        "score": 1.0,
                        "strategy": "earnings_call",
                        "metadata": metadata,
                    }
                ],
                shared_regime={},
                enrichment={},
                size_profile=None,
                marked_account=lifecycle.snapshot,
            )

        assert result["signals"][0]["event_key"] == canonical_event_key(
            "earnings_call", "AAPL", metadata, FRIDAY
        )
        assert result["signals"][0]["event_key"] != "caller-controlled-bypass"
    finally:
        ledger.close()


@pytest.mark.parametrize(
    "metadata",
    [
        {"event_key": "event", "observed_at": "2026-07-31T19:00:00"},
        {"event_key": "event", "observed_at": "not-a-time"},
        {"event_key": "event", "event_at": "2026-07-31T19:00:00"},
        {"event_key": "event", "published_at": "not-a-time"},
    ],
)
def test_supplied_invalid_or_naive_candidate_timestamp_fails_closed(tmp_path, metadata):
    state_dir = str(tmp_path / "state")
    config = _config()
    config["autoresearch"].update({"state_dir": state_dir, "horizon": "30d"})
    ledger = PortfolioLedger(
        Path(state_dir) / "portfolio.db", "cohort", Decimal("1000")
    )
    try:
        lifecycle = SessionExecutor(ledger, config).execute_open_and_mark(
            FRIDAY,
            "epoch",
            FakePriceSource(
                adjusted={
                    ("SPY", FRIDAY): Decimal("649"),
                    ("BIL", FRIDAY): Decimal("91"),
                }
            ),
            {},
            datetime(2026, 7, 31, 22, tzinfo=UTC),
        )
        engine = MultiStrategyEngine(
            config=config,
            strategies=[_NeverExitStrategy()],
            state_manager=StateManager(state_dir),
            ledger=ledger,
        )
        signal = {
            "ticker": "AAPL",
            "direction": "long",
            "score": 1.0,
            "strategy": "strategy",
            "metadata": metadata,
        }
        with (
            patch(
                "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
                return_value=[],
            ),
            pytest.raises(ValueError, match="timestamp"),
        ):
            engine.screen_and_stage(
                FRIDAY.isoformat(),
                {"_execution_reference_bars": {"AAPL": _bar("AAPL", FRIDAY)}},
                [signal],
                {},
                {},
                None,
                lifecycle.snapshot,
            )
        assert ledger.read_signals(FRIDAY, FRIDAY) == []
    finally:
        ledger.close()


def test_date_only_event_uses_end_of_date_and_is_same_session_cutoff_late(tmp_path):
    state_dir = str(tmp_path / "state")
    config = _config()
    config["autoresearch"].update({"state_dir": state_dir, "horizon": "30d"})
    ledger = PortfolioLedger(
        Path(state_dir) / "portfolio.db", "cohort", Decimal("1000")
    )
    try:
        lifecycle = SessionExecutor(ledger, config).execute_open_and_mark(
            FRIDAY,
            "epoch",
            FakePriceSource(
                adjusted={
                    ("SPY", FRIDAY): Decimal("649"),
                    ("BIL", FRIDAY): Decimal("91"),
                }
            ),
            {},
            datetime(2026, 7, 31, 22, tzinfo=UTC),
        )
        engine = MultiStrategyEngine(
            config=config,
            strategies=[_NeverExitStrategy()],
            state_manager=StateManager(state_dir),
            ledger=ledger,
        )
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            return_value=[],
        ):
            result = engine.screen_and_stage(
                FRIDAY.isoformat(),
                {"_execution_reference_bars": {"AAPL": _bar("AAPL", FRIDAY)}},
                [
                    {
                        "ticker": "AAPL",
                        "direction": "long",
                        "score": 1.0,
                        "strategy": "strategy",
                        "metadata": {
                            "event_key": "filing-date-event",
                            "file_date": FRIDAY.isoformat(),
                        },
                    }
                ],
                {},
                {},
                None,
                lifecycle.snapshot,
            )
        record = ledger.read_signals(FRIDAY, FRIDAY)[0]
        assert result["cutoff_late"] == [record.signal_id]
        assert record.event_at == datetime(2026, 7, 31, 23, 59, 59, 999999, UTC)
        assert ledger.pending_intents(MONDAY) == []
    finally:
        ledger.close()
