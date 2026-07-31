from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from tradingagents.strategies.execution import (
    CorporateAction,
    Fill,
    MarketBar,
    OrderIntent,
    SignalRecord,
    stable_id,
)
from tradingagents.strategies.execution.price_source import AdjustedClose
from tradingagents.strategies.orchestration.session_executor import (
    PHASES,
    SessionExecutor,
)
from tradingagents.strategies.orchestration.multi_strategy_engine import (
    MultiStrategyEngine,
)
from tradingagents.strategies.state.state import StateManager
from tradingagents.strategies.trading.portfolio_committee import TradeRecommendation
from tradingagents.strategies.orchestration.trading_calendar import (
    next_session,
    session_close,
)
from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger


UTC = timezone.utc
FRIDAY = date(2026, 7, 31)
MONDAY = date(2026, 8, 3)
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


def _intent(ledger, ticker, side, eligible_session, qty, *, reference_session=FRIDAY):
    signal = _signal(
        ticker,
        reference_session,
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


def _open_long(ledger, ticker="OLD", qty=9):
    reference = date(2026, 7, 29)
    opened = date(2026, 7, 30)
    intent = _intent(ledger, ticker, "buy", opened, qty, reference_session=reference)
    fill = Fill(
        stable_id("fill", intent.intent_id, opened, qty),
        intent.intent_id,
        "buy",
        opened,
        datetime(2026, 7, 30, 13, 30, tzinfo=UTC),
        datetime(2026, 7, 30, 22, tzinfo=UTC),
        Decimal("100"),
        Decimal("100"),
        qty,
        Decimal("0"),
        Decimal("0"),
        Decimal("0"),
    )
    ledger.apply_fill(intent, fill)
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


class _NeverExitStrategy:
    name = "strategy"

    def get_default_params(self, horizon="30d"):
        return {}

    def check_exit(self, **kwargs):
        return False, ""


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
