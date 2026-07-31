from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from tradingagents.strategies.execution import (
    CorporateAction,
    Fill,
    MarketBar,
    OrderIntent,
    SignalRecord,
    stable_id,
)
from tradingagents.strategies.state.compatibility_projection import (
    project_all,
    project_equity_snapshots,
    project_paper_trades,
)
from tradingagents.strategies.state.portfolio_ledger import (
    LedgerConflictError,
    MissingMarkError,
    PortfolioLedger,
    TradeProjectionRecord,
)
from tradingagents.strategies.state.state import StateManager
from tradingagents.strategies.trading.paper_trader import PaperTrader


UTC = timezone.utc
COHORT = "horizon_30d_size_50k"
DAY_ONE = date(2026, 8, 3)
DAY_TWO = date(2026, 8, 4)
DAY_THREE = date(2026, 8, 5)


def _signal(
    signal_id: str,
    ticker: str,
    direction: str,
    *,
    strategy: str | None = None,
) -> SignalRecord:
    observed = datetime(2026, 7, 31, 20, tzinfo=UTC)
    return SignalRecord(
        signal_id,
        "epoch-1",
        "policy-1",
        f"event-{signal_id}",
        strategy or ("litigation" if ticker == "AAPL" else "supply_chain"),
        ticker,
        direction,
        observed,
        observed,
        date(2026, 7, 31),
        Decimal("100"),
        datetime(2026, 7, 31, 22, tzinfo=UTC),
        f"evidence-{signal_id}",
    )


def _intent(
    intent_id: str,
    signal_id: str,
    side: str,
    quantity: int,
    session: date,
) -> OrderIntent:
    return OrderIntent(
        intent_id,
        (signal_id,),
        COHORT,
        side,
        quantity,
        datetime(2026, 7, 31, 22, tzinfo=UTC),
        session,
        "next_session_open",
        "pending",
        None,
        None,
    )


def _fill(
    fill_id: str,
    intent: OrderIntent,
    reference_price: str,
    fill_price: str,
    *,
    slippage: str,
    commission: str,
    other_fees: str,
) -> Fill:
    effective = datetime(
        intent.eligible_session.year,
        intent.eligible_session.month,
        intent.eligible_session.day,
        13,
        30,
        tzinfo=UTC,
    )
    return Fill(
        fill_id,
        intent.intent_id,
        intent.side,
        intent.eligible_session,
        effective,
        effective.replace(hour=22),
        Decimal(reference_price),
        Decimal(fill_price),
        intent.requested_qty,
        Decimal(slippage),
        Decimal(commission),
        Decimal(other_fees),
    )


def _bar(ticker: str, session: date, close: str) -> MarketBar:
    value = Decimal(close)
    return MarketBar(
        ticker,
        session,
        value,
        value,
        value,
        value,
        "fixture",
        datetime(session.year, session.month, session.day, 22, tzinfo=UTC),
        False,
    )


def _seed_ledger(tmp_path) -> PortfolioLedger:
    ledger = PortfolioLedger(tmp_path / "portfolio.db", COHORT, Decimal("50000"))

    long_signal = _signal("signal-long", "AAPL", "long")
    ledger.record_signal(long_signal)
    buy = _intent("intent-buy", long_signal.signal_id, "buy", 10, DAY_ONE)
    ledger.stage_intent(buy)
    ledger.apply_fill(
        buy,
        _fill(
            "fill-buy",
            buy,
            "100",
            "100.10",
            slippage="1.00",
            commission="0.25",
            other_fees="0.05",
        ),
    )
    ledger.mark(
        DAY_ONE,
        {"AAPL": _bar("AAPL", DAY_ONE, "105")},
        "epoch-1",
        datetime(2026, 8, 3, 22, tzinfo=UTC),
    )

    sell = _intent("intent-sell", long_signal.signal_id, "sell", 10, DAY_TWO)
    ledger.stage_exit_intent(
        sell,
        ((stable_id("lot", "fill-buy"), 10),),
    )
    ledger.apply_fill(
        sell,
        _fill(
            "fill-sell",
            sell,
            "110",
            "109.89",
            slippage="1.10",
            commission="0.30",
            other_fees="0.06",
        ),
    )

    short_signal = _signal("signal-short", "TSLA", "short")
    ledger.record_signal(short_signal)
    short = _intent("intent-short", short_signal.signal_id, "short", 5, DAY_TWO)
    ledger.stage_intent(short)
    ledger.apply_fill(
        short,
        _fill(
            "fill-short",
            short,
            "200",
            "199.80",
            slippage="1.00",
            commission="0.20",
            other_fees="0.04",
        ),
        borrow_rate=Decimal("0.01"),
    )
    ledger.mark(
        DAY_TWO,
        {"TSLA": _bar("TSLA", DAY_TWO, "195")},
        "epoch-1",
        datetime(2026, 8, 4, 22, tzinfo=UTC),
    )
    return ledger


def test_trade_and_snapshot_projections_match_ledger_and_are_deterministic(tmp_path):
    ledger = _seed_ledger(tmp_path)
    trade_path = tmp_path / "paper_trades.json"
    snapshot_path = tmp_path / "equity_snapshots.jsonl"
    try:
        trades = project_paper_trades(ledger, trade_path)
        snapshots = project_equity_snapshots(ledger, snapshot_path)

        assert [trade["trade_id"] for trade in trades] == [
            "fill-buy",
            "fill-short",
        ]
        closed, open_short = trades
        assert closed["status"] == "closed"
        assert closed["shares"] == 10
        assert closed["realized_pnl"] == pytest.approx(97.9)
        assert closed["slippage_cost"] == pytest.approx(2.1)
        assert closed["commission_cost"] == pytest.approx(0.55)
        assert closed["other_fees"] == pytest.approx(0.11)
        assert closed["signal_ids"] == ["signal-long"]
        assert open_short["status"] == "open"
        assert open_short["open_shares"] == 5
        assert open_short["direction"] == "short"

        authoritative = ledger.read_snapshots()
        assert [row["portfolio_value"] for row in snapshots] == [
            float(snapshot.net_equity) for snapshot in authoritative
        ]
        assert snapshots[-1]["snapshot_id"] == authoritative[-1].snapshot_id
        assert (
            snapshots[-1]["mark_timestamp"]
            == authoritative[-1].valuation_at.isoformat()
        )
        assert snapshots[-1]["n_open"] == 1
        assert snapshots[-1]["n_closed"] == 1

        first_trade_bytes = trade_path.read_bytes()
        first_snapshot_bytes = snapshot_path.read_bytes()
        project_all(ledger, tmp_path)
        assert trade_path.read_bytes() == first_trade_bytes
        assert snapshot_path.read_bytes() == first_snapshot_bytes
    finally:
        ledger.close()


def test_ledger_exposes_typed_ordered_trade_projection_rows(tmp_path):
    ledger = _seed_ledger(tmp_path)
    try:
        rows = ledger.read_trade_projections()
        assert all(isinstance(row, TradeProjectionRecord) for row in rows)
        assert [(row.entry_session, row.trade_id) for row in rows] == [
            (DAY_ONE, "fill-buy"),
            (DAY_TWO, "fill-short"),
        ]
        assert rows[0].signal_ids == ("signal-long",)
        assert rows[0].exit_fill_ids == ("fill-sell",)
        assert rows[0].realized_pnl == Decimal("97.90")
        assert rows[1].open_shares == 5
    finally:
        ledger.close()


@pytest.mark.parametrize(
    ("projector", "filename"),
    [
        (project_paper_trades, "paper_trades.json"),
        (project_equity_snapshots, "equity_snapshots.jsonl"),
    ],
)
def test_projection_replace_failure_preserves_previous_file(
    tmp_path, monkeypatch, projector, filename
):
    ledger = _seed_ledger(tmp_path)
    destination = tmp_path / filename
    destination.write_text("previous contents\n")

    def fail_replace(*_args, **_kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(
        "tradingagents.strategies.state.compatibility_projection.os.replace",
        fail_replace,
    )
    try:
        with pytest.raises(OSError, match="simulated replace failure"):
            projector(ledger, destination)
        assert destination.read_text() == "previous contents\n"
        assert list(tmp_path.glob("*.tmp")) == []
    finally:
        ledger.close()


def test_projection_propagates_authoritative_missing_mark_errors(tmp_path, monkeypatch):
    ledger = PortfolioLedger(tmp_path / "portfolio.db", COHORT, Decimal("50000"))

    def fail_closed(*_args, **_kwargs):
        raise MissingMarkError("missing mark for AAPL/2026-08-03")

    monkeypatch.setattr(ledger, "read_snapshots", fail_closed)
    try:
        with pytest.raises(MissingMarkError, match="missing mark"):
            project_equity_snapshots(ledger, tmp_path / "equity_snapshots.jsonl")
    finally:
        ledger.close()


def test_state_and_equity_readers_prefer_ledger_and_mutators_fail_closed(tmp_path):
    ledger = _seed_ledger(tmp_path)
    try:
        (tmp_path / "paper_trades.json").write_text(
            json.dumps([{"trade_id": "stale-json", "status": "open"}])
        )
        (tmp_path / "equity_snapshots.jsonl").write_text(
            json.dumps({"date": "1900-01-01", "portfolio_value": -1}) + "\n"
        )
    finally:
        ledger.close()

    state = StateManager(str(tmp_path))
    assert {trade["trade_id"] for trade in state.load_paper_trades()} == {
        "fill-buy",
        "fill-short",
    }
    with pytest.raises(RuntimeError, match="PortfolioLedger"):
        state.save_paper_trade({"ticker": "IGNORED"})
    with pytest.raises(RuntimeError, match="PortfolioLedger"):
        state.update_paper_trade("fill-buy", {"status": "open"})

    trader = PaperTrader(state)
    assert {trade["trade_id"] for trade in trader.project()} == {
        "fill-buy",
        "fill-short",
    }
    with pytest.raises(RuntimeError, match="read-only"):
        trader.open_trade("x", "AAPL", "long", 1.0, "2026-08-03")
    with pytest.raises(RuntimeError, match="read-only"):
        trader.close_trade("fill-buy", 1.0, "2026-08-04", "not allowed")

    from tradingagents.strategies.state import equity_snapshot

    snapshots = equity_snapshot.load_snapshots(str(tmp_path))
    assert [snapshot["date"] for snapshot in snapshots] == [
        DAY_ONE.isoformat(),
        DAY_TWO.isoformat(),
    ]
    assert snapshots[-1]["portfolio_value"] > 0


def test_write_snapshot_projects_ledger_and_ignores_legacy_accounting_inputs(tmp_path):
    ledger = _seed_ledger(tmp_path)
    ledger.close()

    from tradingagents.strategies.state import equity_snapshot

    projected = equity_snapshot.write_snapshot(
        str(tmp_path),
        DAY_TWO.isoformat(),
        cash=-999,
        open_trades=[],
        closed_trades=[],
        price_cache=None,
        total_capital=-999,
    )

    assert projected["date"] == DAY_TWO.isoformat()
    assert projected["portfolio_value"] > 0
    assert projected["total_capital"] == 50000.0
    persisted = equity_snapshot.load_snapshots(str(tmp_path))
    assert persisted[-1] == projected


def test_snapshot_counts_partially_closed_lot_as_open(tmp_path):
    ledger = PortfolioLedger(tmp_path / "portfolio.db", COHORT, Decimal("50000"))
    signal = _signal("signal-partial", "AAPL", "long")
    ledger.record_signal(signal)
    buy = _intent("intent-partial-buy", signal.signal_id, "buy", 10, DAY_ONE)
    ledger.stage_intent(buy)
    ledger.apply_fill(
        buy,
        _fill(
            "fill-partial-buy",
            buy,
            "100",
            "100",
            slippage="0",
            commission="0",
            other_fees="0",
        ),
    )
    sell = _intent("intent-partial-sell", signal.signal_id, "sell", 4, DAY_TWO)
    ledger.stage_exit_intent(sell, ((stable_id("lot", "fill-partial-buy"), 4),))
    ledger.apply_fill(
        sell,
        _fill(
            "fill-partial-sell",
            sell,
            "110",
            "110",
            slippage="0",
            commission="0",
            other_fees="0",
        ),
    )
    ledger.mark(
        DAY_TWO,
        {"AAPL": _bar("AAPL", DAY_TWO, "105")},
        "epoch-1",
        datetime(2026, 8, 4, 22, tzinfo=UTC),
    )
    try:
        snapshots = project_equity_snapshots(
            ledger, tmp_path / "equity_snapshots.jsonl"
        )
        assert snapshots[-1]["n_open"] == 1
        assert snapshots[-1]["n_closed"] == 0
    finally:
        ledger.close()


def test_state_reader_uses_legacy_json_only_without_ledger(tmp_path):
    expected = [{"trade_id": "legacy", "strategy": "test", "status": "open"}]
    (tmp_path / "paper_trades.json").write_text(json.dumps(expected))
    assert StateManager(str(tmp_path)).load_paper_trades() == expected


def test_open_existing_is_read_only_and_does_not_reconstruct_accounting_state(
    tmp_path,
):
    ledger = _seed_ledger(tmp_path)
    ledger_path = ledger.path
    ledger.connection.execute("DELETE FROM accounting_state")
    ledger.close()
    before = ledger_path.read_bytes()

    reopened = PortfolioLedger.open_existing(ledger_path)
    try:
        assert (
            reopened.connection.execute(
                "SELECT COUNT(*) FROM accounting_state"
            ).fetchone()[0]
            == 0
        )
        with pytest.raises(LedgerConflictError, match="missing accounting summary"):
            reopened.account_state()
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reopened.record_signal(_signal("read-only", "AAPL", "long"))
    finally:
        reopened.close()

    assert ledger_path.read_bytes() == before


@pytest.mark.parametrize("marker_kind", ["directory", "broken_symlink", "malformed"])
def test_present_invalid_ledger_marker_fails_closed_without_legacy_fallback(
    tmp_path, marker_kind
):
    marker = tmp_path / "portfolio.db"
    if marker_kind == "directory":
        marker.mkdir()
    elif marker_kind == "broken_symlink":
        marker.symlink_to(tmp_path / "missing-ledger-target")
    else:
        marker.write_bytes(b"not a sqlite database")

    legacy = [{"trade_id": "legacy", "strategy": "test", "status": "open"}]
    legacy_path = tmp_path / "paper_trades.json"
    legacy_path.write_text(json.dumps(legacy))
    state = StateManager(str(tmp_path))

    assert state.has_portfolio_ledger
    with pytest.raises((OSError, sqlite3.Error, ValueError)):
        state.load_paper_trades()
    with pytest.raises(RuntimeError, match="PortfolioLedger"):
        state.save_paper_trade({"ticker": "MUST_NOT_PERSIST"})
    assert json.loads(legacy_path.read_text()) == legacy

    from tradingagents.strategies.state import equity_snapshot

    with pytest.raises((OSError, sqlite3.Error, ValueError)):
        equity_snapshot.load_snapshots(str(tmp_path))


def test_partial_exit_projects_current_legacy_position_and_no_terminal_exit(
    tmp_path,
):
    ledger = PortfolioLedger(tmp_path / "portfolio.db", COHORT, Decimal("50000"))
    signal = _signal("signal-partial-projection", "AAPL", "long")
    ledger.record_signal(signal)
    buy = _intent("intent-partial-projection-buy", signal.signal_id, "buy", 10, DAY_ONE)
    ledger.stage_intent(buy)
    ledger.apply_fill(
        buy,
        _fill(
            "fill-partial-projection-buy",
            buy,
            "100",
            "100",
            slippage="0",
            commission="0",
            other_fees="0",
        ),
    )
    sell = _intent(
        "intent-partial-projection-sell", signal.signal_id, "sell", 4, DAY_TWO
    )
    ledger.stage_exit_intent(
        sell, ((stable_id("lot", "fill-partial-projection-buy"), 4),)
    )
    ledger.apply_fill(
        sell,
        _fill(
            "fill-partial-projection-sell",
            sell,
            "110",
            "110",
            slippage="0",
            commission="0",
            other_fees="0",
        ),
    )
    try:
        trade = project_paper_trades(ledger, tmp_path / "paper_trades.json")[0]
        assert trade["status"] == "open"
        assert trade["shares"] == 6
        assert trade["open_shares"] == 6
        assert trade["position_value"] == pytest.approx(600.0)
        assert trade["original_shares"] == 10
        assert trade["closed_shares"] == 4
        assert trade["exit_execution_ids"] == ["fill-partial-projection-sell"]
        assert trade["exit_session"] is None
        assert trade["exit_date"] is None
        assert trade["exit_price"] is None
    finally:
        ledger.close()


@pytest.mark.parametrize(
    (
        "ticker",
        "direction",
        "entry_side",
        "exit_side",
        "partial_price",
        "final_price",
        "expected_exit_price",
    ),
    [
        ("AAPL", "long", "buy", "sell", "110", "55", 55.0),
        ("TSLA", "short", "short", "cover", "90", "45", 45.0),
    ],
)
def test_split_after_partial_close_projects_adjusted_return_that_reconciles_to_pnl(
    tmp_path,
    ticker,
    direction,
    entry_side,
    exit_side,
    partial_price,
    final_price,
    expected_exit_price,
):
    ledger = PortfolioLedger(tmp_path / "portfolio.db", COHORT, Decimal("50000"))
    signal = _signal(f"signal-split-{direction}", ticker, direction)
    ledger.record_signal(signal)
    entry = _intent(
        f"intent-split-{direction}-entry", signal.signal_id, entry_side, 10, DAY_ONE
    )
    ledger.stage_intent(entry)
    ledger.apply_fill(
        entry,
        _fill(
            f"fill-split-{direction}-entry",
            entry,
            "100",
            "100",
            slippage="0",
            commission="0",
            other_fees="0",
        ),
        **({"borrow_rate": Decimal("0.01")} if direction == "short" else {}),
    )
    partial = _intent(
        f"intent-split-{direction}-partial",
        signal.signal_id,
        exit_side,
        4,
        DAY_TWO,
    )
    lot_id = stable_id("lot", f"fill-split-{direction}-entry")
    ledger.stage_exit_intent(partial, ((lot_id, 4),))
    ledger.apply_fill(
        partial,
        _fill(
            f"fill-split-{direction}-partial",
            partial,
            partial_price,
            partial_price,
            slippage="0",
            commission="0",
            other_fees="0",
        ),
    )
    action_at = datetime(2026, 8, 4, 23, tzinfo=UTC)
    ledger.apply_corporate_actions(
        DAY_TWO,
        [
            CorporateAction(
                f"split-{direction}-2x",
                ticker,
                DAY_TWO,
                "split",
                Decimal("2"),
                None,
                "fixture",
                action_at,
                True,
            )
        ],
        action_at,
    )
    final = _intent(
        f"intent-split-{direction}-final",
        signal.signal_id,
        exit_side,
        12,
        DAY_THREE,
    )
    ledger.stage_exit_intent(final, ((lot_id, 12),))
    ledger.apply_fill(
        final,
        _fill(
            f"fill-split-{direction}-final",
            final,
            final_price,
            final_price,
            slippage="0",
            commission="0",
            other_fees="0",
        ),
    )
    try:
        trade = project_paper_trades(ledger, tmp_path / "paper_trades.json")[0]
        assert trade["status"] == "closed"
        assert trade["entry_price"] == pytest.approx(50.0)
        assert trade["shares"] == 20
        assert trade["realized_pnl"] == pytest.approx(100.0)
        assert trade["exit_price"] == pytest.approx(expected_exit_price)
        projected_return = (trade["exit_price"] - trade["entry_price"]) / trade[
            "entry_price"
        ]
        if direction == "short":
            projected_return = -projected_return
        assert projected_return == pytest.approx(0.10)
        assert trade["pnl_pct"] == pytest.approx(projected_return)
    finally:
        ledger.close()


def test_multi_strategy_projection_preserves_and_filters_every_contributor(tmp_path):
    ledger = PortfolioLedger(tmp_path / "portfolio.db", COHORT, Decimal("50000"))
    zeta = _signal("signal-zeta", "AAPL", "long", strategy="zeta_strategy")
    alpha = _signal("signal-alpha", "AAPL", "long", strategy="alpha_strategy")
    ledger.record_signal(zeta)
    ledger.record_signal(alpha)
    intent = OrderIntent(
        "intent-multi-strategy",
        (zeta.signal_id, alpha.signal_id),
        COHORT,
        "buy",
        10,
        datetime(2026, 7, 31, 22, tzinfo=UTC),
        DAY_ONE,
        "next_session_open",
        "pending",
        None,
        None,
    )
    ledger.stage_intent(intent)
    ledger.apply_fill(
        intent,
        _fill(
            "fill-multi-strategy",
            intent,
            "100",
            "100",
            slippage="0",
            commission="0",
            other_fees="0",
        ),
    )
    ledger.close()

    state = StateManager(str(tmp_path))
    trade = state.load_paper_trades()[0]
    assert trade["strategies"] == ["alpha_strategy", "zeta_strategy"]
    assert trade["strategy"] == "alpha_strategy"
    assert [
        row["trade_id"] for row in state.load_paper_trades(strategy="alpha_strategy")
    ] == ["fill-multi-strategy"]
    assert [
        row["trade_id"] for row in state.load_paper_trades(strategy="zeta_strategy")
    ] == ["fill-multi-strategy"]


@pytest.mark.parametrize("method", ["open_trade", "check_exits", "close_trade"])
def test_paper_trader_mutators_are_read_only_without_ledger(tmp_path, method):
    state = StateManager(str(tmp_path))
    trader = PaperTrader(state)

    with pytest.raises(RuntimeError, match="read-only"):
        if method == "open_trade":
            trader.open_trade("test", "AAPL", "long", 100.0, DAY_ONE.isoformat())
        elif method == "check_exits":
            trader.check_exits({}, {}, DAY_TWO.isoformat())
        else:
            trader.close_trade("missing", 110.0, DAY_TWO.isoformat(), "must not mutate")

    assert not (tmp_path / "paper_trades.json").exists()
