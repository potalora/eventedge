"""Regression coverage for policy-aware next-open execution invariants."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
import json

import pytest

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.strategies.execution import Fill, MarketBar, OrderIntent, SignalRecord, stable_id
from tradingagents.strategies.execution.price_source import AdjustedClose
from tradingagents.strategies.orchestration.cohort_orchestrator import SIZE_PROFILES
from tradingagents.strategies.orchestration.session_executor import SessionExecutor
from tradingagents.strategies.orchestration.trading_calendar import session_close
from tradingagents.strategies.state.portfolio_ledger import (
    LedgerConflictError,
    MissingMarkError,
    PortfolioLedger,
)
from tradingagents.strategies.trading.portfolio_policy import (
    PolicyPosition,
    PortfolioRiskContext,
    portfolio_risk_context_document,
)


UTC = timezone.utc
FRIDAY = date(2026, 7, 31)
THURSDAY = date(2026, 7, 30)
MONDAY = date(2026, 8, 3)
TUESDAY = date(2026, 8, 4)
PROCESSED = datetime(2026, 8, 3, 22, tzinfo=UTC)
_DEFAULT_VOLATILITY_TICKERS = {
    "AAPL",
    "DUE",
    "FIRST",
    "FUTURE",
    "GAP",
    "HELD",
    "HIGH",
    "LOW0",
    "LOW1",
    "LOW2",
    "LOW3",
    "MSFT",
    "NEW",
    "OLD",
    "SECOND",
    "SMALL",
    "TODAY",
    "TOO_BIG",
}


class _Prices:
    def __init__(self, bars: dict[tuple[str, date], MarketBar]) -> None:
        self.bars = bars

    def get_daily_bars(self, tickers, start_session, end_session_inclusive, adjusted=False):
        return {
            key: value
            for key, value in self.bars.items()
            if key[0] in tickers and start_session <= key[1] <= end_session_inclusive
        }

    def get_corporate_actions(self, tickers, session):
        return []

    def get_total_return_closes(self, symbols, start_session, end_session_inclusive):
        return {
            (symbol, MONDAY): AdjustedClose(
                symbol,
                MONDAY,
                Decimal("100"),
                "fixture",
                PROCESSED,
            )
            for symbol in symbols
            if start_session <= MONDAY <= end_session_inclusive
        }


def _bar(ticker: str, session: date, open_: str = "100", close: str = "100") -> MarketBar:
    opened = Decimal(open_)
    closed = Decimal(close)
    return MarketBar(
        ticker,
        session,
        opened,
        max(opened, closed),
        min(opened, closed),
        closed,
        "fixture-raw",
        PROCESSED,
        False,
    )


def _profile(*, max_positions: int = 5, max_position_pct: float = 0.25):
    return replace(
        SIZE_PROFILES["5k"],
        max_positions=max_positions,
        max_position_pct=max_position_pct,
        cash_reserve_pct=0.0,
        sector_concentration_cap=1.0,
        max_strategy_exposure_pct=1.0,
        max_event_cluster_exposure_pct=1.0,
    )


def _config(tmp_path, profile) -> dict:
    config = deepcopy(DEFAULT_CONFIG)
    config["autoresearch"].update(
        {"state_dir": str(tmp_path), "horizon": "30d", "total_capital": 5000}
    )
    config["autoresearch"]["paper_trade"]["portfolio_committee_enabled"] = False
    config["autoresearch"]["risk_gate"].update(
        {
            "max_positions": profile.max_positions,
            "max_position_pct": profile.max_position_pct,
            "min_position_value": profile.min_position_value,
            "cash_reserve_pct": profile.cash_reserve_pct,
            "long_only": not profile.short_eligible,
        }
    )
    return config


def _ledger(tmp_path) -> PortfolioLedger:
    return PortfolioLedger(tmp_path / "portfolio.db", "cohort", Decimal("5000"))


def _signal(ticker: str, reference_session: date, *, suffix: str, direction: str = "long") -> SignalRecord:
    cutoff = session_close(reference_session)
    return SignalRecord(
        stable_id("signal", ticker, reference_session, suffix),
        "epoch",
        "policy",
        f"event-{suffix}",
        "strategy",
        ticker,
        direction,
        cutoff,
        cutoff,
        reference_session,
        Decimal("100"),
        cutoff,
        stable_id("evidence", suffix),
    )


def _stage_entry(
    ledger: PortfolioLedger,
    executor: SessionExecutor,
    ticker: str,
    eligible_session: date,
    quantity: int,
    *,
    suffix: str,
    policy_provenance: bool = True,
    staging_context: dict[str, object] | None = None,
    reference_session: date = FRIDAY,
) -> OrderIntent:
    signal = _signal(ticker, reference_session, suffix=suffix)
    ledger.record_signal(signal)
    policy = executor.portfolio_policy_config
    assert policy is not None
    if policy_provenance:
        if staging_context is None:
            existing_binding = ledger.read_policy_session_context(
                reference_session, binding_kind="staging"
            )
            staging_context = (
                existing_binding["context"]
                if existing_binding is not None
                else _staging_context(
                    executor,
                    volatility={
                        ticker: policy.annualized_volatility_floor
                        for ticker in _DEFAULT_VOLATILITY_TICKERS
                    },
                )
            )
        binding = ledger.bind_policy_session_context(
            reference_session,
            binding_kind="staging",
            epoch_id="epoch",
            policy_version=policy.version,
            policy_config=executor.portfolio_policy_document() or {},
            context=staging_context,
            bound_at=PROCESSED,
        )
        ledger.record_signal_policy_provenance(
            signal.signal_id,
            policy_version=policy.version,
            event_key=signal.event_key,
            source_event_keys=(f"source-{suffix}",),
            strategy_tags=(signal.strategy,),
            risk_tags=(f"event:{suffix}",),
            sector="Test",
            journal_only=False,
            order_eligible=True,
            decision="accepted",
            reason_codes=("accepted",),
            bound_context_digest=str(binding["context_digest"]),
            captured_at=PROCESSED,
        )
    intent = OrderIntent(
        stable_id("intent", ticker, eligible_session, quantity, suffix),
        (signal.signal_id,),
        ledger.cohort_id,
        "buy",
        quantity,
        session_close(reference_session),
        eligible_session,
        "next_session_open",
        "pending",
        None,
        None,
    )
    ledger.stage_intent(intent)
    if policy_provenance:
        signal_policy = ledger.read_signal_policy_provenance(signal.signal_id)
        assert signal_policy is not None
        ledger.record_intent_policy_provenance(
            intent.intent_id,
            signal_ids=intent.signal_ids,
            policy_version=policy.version,
            event_key=signal.event_key,
            source_event_keys=(f"source-{suffix}",),
            strategy_tags=(signal.strategy,),
            risk_tags=(f"event:{suffix}",),
            sector="Test",
            journal_only=False,
            order_eligible=True,
            decision="accepted",
            reason_codes=("accepted",),
            bound_context_digest=str(signal_policy["bound_context_digest"]),
            captured_at=PROCESSED,
        )
    return intent


def _policy_position(ticker: str, weight: float, volatility: float) -> PolicyPosition:
    return PolicyPosition(
        ticker=ticker,
        direction="long",
        weight=weight,
        sector=ticker,
        strategy_tags=(f"strategy:{ticker}",),
        risk_tags=(f"event:{ticker}",),
        annualized_volatility=volatility,
    )


def _staging_context(
    executor: SessionExecutor,
    *,
    positions: tuple[PolicyPosition, ...] = (),
    pending: tuple[PolicyPosition, ...] = (),
    volatility: dict[str, float],
) -> dict[str, object]:
    policy = executor.portfolio_policy_config
    assert policy is not None
    all_positions = positions + pending
    return portfolio_risk_context_document(
        PortfolioRiskContext(
            portfolio_value=5000.0,
            cash=5000.0 - sum(item.weight * 5000.0 for item in positions),
            positions=positions,
            pending_positions=pending,
            sectors={item.ticker: item.sector for item in all_positions},
            annualized_volatility=volatility,
            earnings_dates={},
            short_interest={},
            borrow_available={},
            margin_used=0.0,
            consumed_event_keys=frozenset(),
            config=policy,
        )
    )


def _seed_post_exit_volatility_case(
    ledger: PortfolioLedger,
    executor: SessionExecutor,
) -> OrderIntent:
    positions = tuple(_policy_position(f"LOW{i}", 0.10, 0.15) for i in range(4))
    context = _staging_context(
        executor,
        positions=positions,
        volatility={**{f"LOW{i}": 0.15 for i in range(4)}, "HIGH": 1.0},
    )
    existing: list[tuple[OrderIntent, Fill]] = []
    for i in range(4):
        intent = _stage_entry(
            ledger,
            executor,
            f"LOW{i}",
            FRIDAY,
            5,
            suffix=f"low-{i}",
            staging_context=context,
        )
        existing.append((intent, _apply_fill(ledger, intent, FRIDAY)))
    ledger.record_marks(
        MONDAY,
        {f"LOW{i}": _bar(f"LOW{i}", MONDAY) for i in range(4)},
        PROCESSED,
    )

    old_intent, old_fill = existing[0]
    exit_signal = _signal("LOW0", FRIDAY, suffix="low-0-exit")
    ledger.record_signal(exit_signal)
    exit_intent = OrderIntent(
        "exit-low-0",
        (exit_signal.signal_id,),
        ledger.cohort_id,
        "sell",
        5,
        session_close(FRIDAY),
        MONDAY,
        "next_session_open",
        "pending",
        None,
        None,
    )
    ledger.stage_exit_intent(
        exit_intent,
        ((stable_id("lot", old_fill.fill_id), old_intent.requested_qty),),
    )
    candidate = _stage_entry(
        ledger,
        executor,
        "HIGH",
        MONDAY,
        1,
        suffix="high",
        staging_context=context,
    )
    return candidate


def _apply_fill(ledger: PortfolioLedger, intent: OrderIntent, session: date) -> Fill:
    fill = Fill(
        stable_id("fill", intent.intent_id, session),
        intent.intent_id,
        intent.side,
        session,
        PROCESSED,
        PROCESSED,
        Decimal("100"),
        Decimal("100"),
        intent.requested_qty,
        Decimal("0"),
        Decimal("0"),
        Decimal("0"),
    )
    ledger.apply_fill(intent, fill)
    return fill


def test_exit_runs_before_execution_policy_binding_and_frees_entry_capacity(tmp_path) -> None:
    profile = _profile(max_position_pct=0.95)
    config = _config(tmp_path, profile)
    ledger = _ledger(tmp_path)
    try:
        executor = SessionExecutor(ledger, config, size_profile=profile)
        old = _stage_entry(ledger, executor, "OLD", FRIDAY, 49, suffix="old")
        old_fill = _apply_fill(ledger, old, FRIDAY)
        old_lot = stable_id("lot", old_fill.fill_id)
        exit_signal = _signal("OLD", FRIDAY, suffix="exit")
        ledger.record_signal(exit_signal)
        exit_intent = OrderIntent(
            "exit-old", (exit_signal.signal_id,), ledger.cohort_id, "sell", 49,
            session_close(FRIDAY), MONDAY, "next_session_open", "pending", None, None,
        )
        ledger.stage_exit_intent(exit_intent, ((old_lot, 49),))
        entry = _stage_entry(ledger, executor, "NEW", MONDAY, 40, suffix="new")

        result = executor.execute_open_and_mark(
            MONDAY,
            "epoch",
            _Prices({("OLD", MONDAY): _bar("OLD", MONDAY), ("NEW", MONDAY): _bar("NEW", MONDAY)}),
            {},
            PROCESSED,
        )

        assert result.valid
        assert ledger.intent(exit_intent.intent_id).status == "filled"
        assert ledger.intent(entry.intent_id).status == "filled"
        binding = ledger.read_policy_session_context(MONDAY, binding_kind="execution")
        assert binding is not None
        assert "OLD" not in binding["context_json"]
    finally:
        ledger.close()


def test_execution_context_excludes_self_prices_due_at_open_and_keeps_future_reservations(tmp_path) -> None:
    profile = _profile(max_position_pct=0.25)
    ledger = _ledger(tmp_path)
    try:
        executor = SessionExecutor(ledger, _config(tmp_path, profile), size_profile=profile)
        current = _stage_entry(ledger, executor, "GAP", MONDAY, 5, suffix="gap")
        future = _stage_entry(ledger, executor, "FUTURE", TUESDAY, 7, suffix="future")
        baseline = PortfolioRiskContext(
            portfolio_value=5000.0,
            cash=5000.0,
            positions=(), pending_positions=(), sectors={}, annualized_volatility={},
            earnings_dates={}, short_interest={}, borrow_available={}, margin_used=0.0,
            consumed_event_keys=frozenset(), config=executor.portfolio_policy_config,
        )

        context = executor._current_intent_policy_context(
            MONDAY,
            current.intent_id,
            {"GAP": Decimal("250")},
            {},
            baseline,
        )

        assert [position.ticker for position in context.pending_positions] == ["FUTURE"]
        assert context.pending_positions[0].weight == pytest.approx(0.14)
        all_pending = executor._policy_pending_with_execution_prices(
            MONDAY, ledger.policy_pending_entry_projection(), {"GAP": Decimal("250")}
        )
        assert {row["intent_id"] for row in all_pending} == {current.intent_id, future.intent_id}
        assert next(row for row in all_pending if row["intent_id"] == current.intent_id)["marked_value"] == Decimal("1250")
    finally:
        ledger.close()


def test_post_exit_fill_revalidation_uses_exact_staged_volatility(tmp_path) -> None:
    profile = replace(
        _profile(max_positions=6),
        max_position_risk_contribution_pct=0.25,
    )
    ledger = _ledger(tmp_path)
    try:
        config = _config(tmp_path, profile)
        config["autoresearch"]["risk_gate"]["per_strategy_max"] = 10
        executor = SessionExecutor(ledger, config, size_profile=profile)
        candidate = _seed_post_exit_volatility_case(ledger, executor)
        bars = {
            (ticker, MONDAY): _bar(ticker, MONDAY)
            for ticker in ("LOW0", "LOW1", "LOW2", "LOW3", "HIGH")
        }

        result = executor.execute_open_and_mark(
            MONDAY, "epoch", _Prices(bars), {}, PROCESSED
        )

        assert result.valid
        assert ledger.intent(candidate.intent_id).status == "rejected"
        reason = ledger.connection.execute(
            "SELECT reason FROM order_status_transitions WHERE intent_id = ? ",
            (candidate.intent_id,),
        ).fetchone()[0]
        assert reason == "portfolio_policy:max_risk_contribution"
        binding = ledger.read_policy_session_context(MONDAY, binding_kind="execution")
        assert binding is not None
        assert binding["context"]["annualized_volatility"]["HIGH"] == 1.0
        assert {item["ticker"] for item in binding["context"]["positions"]} == {
            "LOW1",
            "LOW2",
            "LOW3",
        }
    finally:
        ledger.close()


def test_sequential_intent_rebuild_preserves_current_pending_and_self_volatility(
    tmp_path,
) -> None:
    profile = replace(
        _profile(max_positions=6),
        max_position_risk_contribution_pct=0.25,
    )
    ledger = _ledger(tmp_path)
    try:
        executor = SessionExecutor(
            ledger, _config(tmp_path, profile), size_profile=profile
        )
        held = _stage_entry(ledger, executor, "HELD", MONDAY, 2, suffix="held")
        _apply_fill(ledger, held, MONDAY)
        due = _stage_entry(ledger, executor, "DUE", MONDAY, 1, suffix="due")
        _stage_entry(ledger, executor, "FUTURE", TUESDAY, 1, suffix="future")
        policy = executor.portfolio_policy_config
        assert policy is not None
        baseline = PortfolioRiskContext(
            portfolio_value=5000.0,
            cash=4800.0,
            positions=(),
            pending_positions=(),
            sectors={"HELD": "Held", "DUE": "Due", "FUTURE": "Future"},
            annualized_volatility={"HELD": 0.75, "DUE": 1.0, "FUTURE": 0.85},
            earnings_dates={},
            short_interest={},
            borrow_available={},
            margin_used=0.0,
            consumed_event_keys=frozenset(),
            config=policy,
        )

        rebuilt = executor._current_intent_policy_context(
            MONDAY,
            due.intent_id,
            {"DUE": Decimal("100")},
            {},
            baseline,
        )

        assert rebuilt.positions[0].annualized_volatility == 0.75
        assert rebuilt.pending_positions[0].annualized_volatility == 0.85
        assert rebuilt.annualized_volatility == {
            "HELD": 0.75,
            "DUE": 1.0,
            "FUTURE": 0.85,
        }
    finally:
        ledger.close()


@pytest.mark.parametrize("evidence", ["missing", "conflicting", "tampered"])
def test_fill_fails_closed_for_invalid_referenced_staging_volatility(
    tmp_path, evidence: str
) -> None:
    profile = replace(
        _profile(max_positions=6),
        max_position_risk_contribution_pct=0.25,
    )
    ledger = _ledger(tmp_path)
    try:
        executor = SessionExecutor(
            ledger, _config(tmp_path, profile), size_profile=profile
        )
        context = _staging_context(executor, volatility={"AAPL": 0.90})
        if evidence == "missing":
            context["annualized_volatility"] = {}
        elif evidence == "conflicting":
            context["positions"] = [
                {
                    "ticker": "AAPL",
                    "direction": "long",
                    "weight": 0.02,
                    "sector": "AAPL",
                    "strategy_tags": ["strategy:AAPL"],
                    "risk_tags": ["event:AAPL"],
                    "annualized_volatility": 0.20,
                }
            ]
        else:
            context["annualized_volatility"] = {"AAPL": "not-a-number"}
        entry = _stage_entry(
            ledger,
            executor,
            "AAPL",
            MONDAY,
            1,
            suffix=f"invalid-vol-{evidence}",
            staging_context=context,
        )

        with pytest.raises(LedgerConflictError, match="volatility"):
            executor.execute_open_and_mark(
                MONDAY,
                "epoch",
                _Prices({("AAPL", MONDAY): _bar("AAPL", MONDAY)}),
                {},
                PROCESSED,
            )
        assert ledger.intent(entry.intent_id).status == "pending"
        assert ledger.read_fills(MONDAY, MONDAY) == []
    finally:
        ledger.close()


def test_fill_fails_closed_for_conflicting_due_intent_staging_volatility(
    tmp_path,
) -> None:
    profile = _profile(max_positions=6)
    ledger = _ledger(tmp_path)
    try:
        executor = SessionExecutor(
            ledger, _config(tmp_path, profile), size_profile=profile
        )
        first = _stage_entry(
            ledger,
            executor,
            "AAPL",
            MONDAY,
            1,
            suffix="first-vol-binding",
            staging_context=_staging_context(
                executor, volatility={"AAPL": 0.90, "MSFT": 0.80}
            ),
        )
        second = _stage_entry(
            ledger,
            executor,
            "MSFT",
            MONDAY,
            1,
            suffix="second-vol-binding",
            staging_context=_staging_context(
                executor, volatility={"AAPL": 0.70, "MSFT": 0.80}
            ),
            reference_session=THURSDAY,
        )

        with pytest.raises(LedgerConflictError, match="volatility"):
            executor.execute_open_and_mark(
                MONDAY,
                "epoch",
                _Prices(
                    {
                        ("AAPL", MONDAY): _bar("AAPL", MONDAY),
                        ("MSFT", MONDAY): _bar("MSFT", MONDAY),
                    }
                ),
                {},
                PROCESSED,
            )
        assert ledger.intent(first.intent_id).status == "pending"
        assert ledger.intent(second.intent_id).status == "pending"
        assert ledger.read_fills(MONDAY, MONDAY) == []
    finally:
        ledger.close()


def test_restart_reuses_exact_staged_volatility_without_market_io(tmp_path) -> None:
    profile = replace(
        _profile(max_positions=6),
        max_position_risk_contribution_pct=0.25,
    )
    config = _config(tmp_path, profile)
    config["autoresearch"]["risk_gate"]["per_strategy_max"] = 10
    ledger = _ledger(tmp_path)
    try:
        executor = SessionExecutor(ledger, config, size_profile=profile)
        candidate = _seed_post_exit_volatility_case(ledger, executor)
        bars = {
            (ticker, MONDAY): _bar(ticker, MONDAY)
            for ticker in ("LOW0", "LOW1", "LOW2", "LOW3", "HIGH")
        }

        def crash(phase: str) -> None:
            if phase == "execute_entries":
                raise RuntimeError("restart after volatility validation")

        with pytest.raises(RuntimeError, match="restart after volatility validation"):
            SessionExecutor(
                ledger,
                config,
                size_profile=profile,
                after_phase_mutation=crash,
            ).execute_open_and_mark(MONDAY, "epoch", _Prices(bars), {}, PROCESSED)
        assert ledger.intent(candidate.intent_id).status == "pending"
        bound_execution = ledger.session_execution_context(MONDAY)
        assert bound_execution is not None
        economic_inputs = json.loads(bound_execution["economic_inputs_json"])
        volatility_document = economic_inputs[
            "portfolio_policy_volatility_evidence"
        ]
        assert volatility_document["annualized_volatility"]["HIGH"] == "1.0"
        assert volatility_document["source_bindings"] == [
            {
                "candidate_tickers": ["HIGH"],
                "context_digest": ledger.read_intent_policy_provenance(
                    candidate.intent_id
                )["bound_context_digest"],
                "reference_session": FRIDAY.isoformat(),
            }
        ]
    finally:
        ledger.close()

    reopened = _ledger(tmp_path)
    try:
        restarted = SessionExecutor(reopened, config, size_profile=profile)
        result = restarted.execute_open_and_mark(
            MONDAY,
            "epoch",
            restarted.persisted_input_bundle(MONDAY),
            restarted.persisted_borrow_rates(MONDAY),
            PROCESSED,
        )

        assert result.valid
        assert reopened.intent(candidate.intent_id).status == "rejected"
        binding = reopened.read_policy_session_context(MONDAY, binding_kind="execution")
        assert binding is not None
        assert binding["context"]["annualized_volatility"]["HIGH"] == 1.0
    finally:
        reopened.close()


def test_fully_complete_replay_revalidates_staged_volatility_without_market_io(
    tmp_path,
) -> None:
    profile = _profile()
    config = _config(tmp_path, profile)
    ledger = _ledger(tmp_path)
    try:
        executor = SessionExecutor(ledger, config, size_profile=profile)
        entry = _stage_entry(
            ledger, executor, "AAPL", MONDAY, 2, suffix="complete-replay"
        )
        completed = executor.execute_open_and_mark(
            MONDAY,
            "epoch",
            _Prices({("AAPL", MONDAY): _bar("AAPL", MONDAY)}),
            {},
            PROCESSED,
        )
        assert completed.valid
        assert ledger.intent(entry.intent_id).status == "filled"
        fill_count = len(ledger.read_fills(MONDAY, MONDAY))

        ledger.connection.execute(
            "DELETE FROM policy_session_contexts "
            "WHERE cohort_id = ? AND session = ? AND binding_kind = 'staging'",
            (ledger.cohort_id, FRIDAY.isoformat()),
        )
        ledger.connection.commit()

        with pytest.raises(
            LedgerConflictError, match="staging volatility source binding mismatch"
        ):
            executor.validate_bound_context(MONDAY, "epoch")

        class _NoMarketIO:
            def get_daily_bars(self, *args, **kwargs):
                raise AssertionError("fully complete replay fetched market data")

        replayed = executor.execute_open_and_mark(
            MONDAY,
            "epoch",
            _NoMarketIO(),
            executor.persisted_borrow_rates(MONDAY),
            PROCESSED,
        )
        assert not replayed.valid
        assert "staging volatility source binding mismatch" in replayed.invalid_reason
        assert len(ledger.read_fills(MONDAY, MONDAY)) == fill_count
    finally:
        ledger.close()


def test_same_session_fill_uses_open_reference_but_old_unmarked_lot_fails_closed(tmp_path) -> None:
    profile = _profile()
    ledger = _ledger(tmp_path)
    try:
        executor = SessionExecutor(ledger, _config(tmp_path, profile), size_profile=profile)
        same_day = _stage_entry(ledger, executor, "TODAY", MONDAY, 2, suffix="today")
        _apply_fill(ledger, same_day, MONDAY)
        projection = ledger.policy_open_lot_projection(MONDAY)
        assert projection[0]["mark"] == Decimal("100")
        old = _stage_entry(ledger, executor, "OLD", FRIDAY, 2, suffix="unmarked")
        _apply_fill(ledger, old, FRIDAY)

        with pytest.raises(MissingMarkError, match="missing persisted raw mark for OLD/2026-08-03"):
            ledger.policy_open_lot_projection(MONDAY)
    finally:
        ledger.close()


def test_earlier_due_fill_is_included_when_later_due_entry_is_evaluated(tmp_path) -> None:
    profile = _profile(max_positions=1)
    ledger = _ledger(tmp_path)
    try:
        executor = SessionExecutor(ledger, _config(tmp_path, profile), size_profile=profile)
        first = _stage_entry(ledger, executor, "FIRST", MONDAY, 5, suffix="first")
        second = _stage_entry(ledger, executor, "SECOND", MONDAY, 5, suffix="second")

        result = executor.execute_open_and_mark(
            MONDAY,
            "epoch",
            _Prices({("FIRST", MONDAY): _bar("FIRST", MONDAY), ("SECOND", MONDAY): _bar("SECOND", MONDAY)}),
            {},
            PROCESSED,
        )

        assert result.valid
        assert ledger.intent(first.intent_id).status == "filled"
        assert ledger.intent(second.intent_id).status == "rejected"
        reason = ledger.connection.execute(
            "SELECT reason FROM order_status_transitions WHERE intent_id = ?",
            (second.intent_id,),
        ).fetchone()[0]
        assert reason == "portfolio_policy:max_positions"
    finally:
        ledger.close()


def test_earlier_due_rejection_does_not_reserve_capacity_for_later_entry(tmp_path) -> None:
    profile = _profile(max_position_pct=0.25)
    ledger = _ledger(tmp_path)
    try:
        executor = SessionExecutor(ledger, _config(tmp_path, profile), size_profile=profile)
        rejected = _stage_entry(ledger, executor, "TOO_BIG", MONDAY, 20, suffix="too-big")
        accepted = _stage_entry(ledger, executor, "SMALL", MONDAY, 5, suffix="small")

        result = executor.execute_open_and_mark(
            MONDAY,
            "epoch",
            _Prices({("TOO_BIG", MONDAY): _bar("TOO_BIG", MONDAY), ("SMALL", MONDAY): _bar("SMALL", MONDAY)}),
            {},
            PROCESSED,
        )

        assert result.valid
        assert ledger.intent(rejected.intent_id).status == "rejected"
        assert ledger.intent(accepted.intent_id).status == "filled"
    finally:
        ledger.close()


def test_entry_phase_rollback_and_restart_preserve_execution_context_contract(tmp_path) -> None:
    profile = _profile()
    config = _config(tmp_path, profile)
    ledger = _ledger(tmp_path)
    try:
        executor = SessionExecutor(ledger, config, size_profile=profile)
        entry = _stage_entry(ledger, executor, "AAPL", MONDAY, 2, suffix="restart")

        def crash(phase: str) -> None:
            if phase == "execute_entries":
                raise RuntimeError("injected entry crash")

        with pytest.raises(RuntimeError, match="injected entry crash"):
            SessionExecutor(
                ledger, config, size_profile=profile, after_phase_mutation=crash
            ).execute_open_and_mark(
                MONDAY, "epoch", _Prices({("AAPL", MONDAY): _bar("AAPL", MONDAY)}), {}, PROCESSED
            )
        assert ledger.intent(entry.intent_id).status == "pending"
        assert ledger.read_fills(MONDAY, MONDAY) == []
        assert ledger.read_policy_session_context(MONDAY, binding_kind="execution") is None

        restarted = SessionExecutor(ledger, config, size_profile=profile).execute_open_and_mark(
            MONDAY, "epoch", _Prices({("AAPL", MONDAY): _bar("AAPL", MONDAY)}), {}, PROCESSED
        )
        assert restarted.valid and ledger.intent(entry.intent_id).status == "filled"
    finally:
        ledger.close()


@pytest.mark.parametrize("change", ["policy", "borrow"])
def test_restart_rejects_changed_bound_policy_or_borrow_inputs(tmp_path, change: str) -> None:
    profile = _profile()
    config = _config(tmp_path, profile)
    ledger = _ledger(tmp_path)
    try:
        executor = SessionExecutor(ledger, config, size_profile=profile)
        _stage_entry(ledger, executor, "AAPL", MONDAY, 2, suffix=f"changed-{change}")

        def crash(phase: str) -> None:
            if phase == "validate_market_data":
                raise RuntimeError("bound then crashed")

        borrow = {"AAPL": Decimal("0.01")}
        with pytest.raises(RuntimeError, match="bound then crashed"):
            SessionExecutor(
                ledger, config, size_profile=profile, after_phase_mutation=crash
            ).execute_open_and_mark(
                MONDAY, "epoch", _Prices({("AAPL", MONDAY): _bar("AAPL", MONDAY)}), borrow, PROCESSED
            )
        resumed_config = deepcopy(config)
        resumed_borrow = dict(borrow)
        if change == "policy":
            resumed_config["autoresearch"]["portfolio_policy"]["version"] = "changed-policy"
        else:
            resumed_borrow["AAPL"] = Decimal("0.02")

        result = SessionExecutor(
            ledger, resumed_config, size_profile=profile
        ).execute_open_and_mark(
            MONDAY,
            "epoch",
            SessionExecutor(ledger, config, size_profile=profile).persisted_input_bundle(MONDAY),
            resumed_borrow,
            PROCESSED,
        )

        assert not result.valid
        assert "effective config or borrow inputs changed" in result.invalid_reason
        assert ledger.read_fills(MONDAY, MONDAY) == []
    finally:
        ledger.close()


@pytest.mark.parametrize("mutation", ["delete", "coherent_rewrite"])
def test_restart_rejects_missing_or_changed_committed_execution_policy_binding(
    tmp_path, mutation: str
) -> None:
    profile = _profile()
    config = _config(tmp_path, profile)
    ledger = _ledger(tmp_path)
    try:
        executor = SessionExecutor(ledger, config, size_profile=profile)
        _stage_entry(
            ledger,
            executor,
            "AAPL",
            MONDAY,
            2,
            suffix=f"binding-{mutation}",
        )

        def crash(phase: str) -> None:
            if phase == "execute_entries":
                raise RuntimeError("crash after committed entries")

        with pytest.raises(RuntimeError, match="crash after committed entries"):
            SessionExecutor(
                ledger,
                config,
                size_profile=profile,
                after_phase_commit=crash,
            ).execute_open_and_mark(
                MONDAY,
                "epoch",
                _Prices({("AAPL", MONDAY): _bar("AAPL", MONDAY)}),
                {},
                PROCESSED,
            )
        assert ledger.phase_completed(MONDAY, "execute_entries")

        if mutation == "delete":
            ledger.connection.execute(
                "DELETE FROM policy_session_contexts "
                "WHERE cohort_id = ? AND session = ? AND binding_kind = 'execution'",
                (ledger.cohort_id, MONDAY.isoformat()),
            )
        else:
            row = ledger.connection.execute(
                "SELECT * FROM policy_session_contexts "
                "WHERE cohort_id = ? AND session = ? AND binding_kind = 'execution'",
                (ledger.cohort_id, MONDAY.isoformat()),
            ).fetchone()
            assert row is not None
            context = json.loads(row["context_json"])
            context["cash"] = float(context["cash"]) + 1.0
            context_json = json.dumps(
                context, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            )
            context_digest = stable_id("policy_context", context_json)
            payload = json.loads(row["payload_json"])
            payload["context_digest"] = context_digest
            payload_json = json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            )
            ledger.connection.execute(
                "UPDATE policy_session_contexts SET context_json = ?, "
                "context_digest = ?, payload_json = ?, payload_digest = ? "
                "WHERE cohort_id = ? AND session = ? AND binding_kind = 'execution'",
                (
                    context_json,
                    context_digest,
                    payload_json,
                    stable_id("policy_binding", payload_json),
                    ledger.cohort_id,
                    MONDAY.isoformat(),
                ),
            )
        ledger.connection.commit()

        restart = SessionExecutor(ledger, config, size_profile=profile)
        result = restart.execute_open_and_mark(
            MONDAY,
            "epoch",
            restart.persisted_input_bundle(MONDAY),
            restart.persisted_borrow_rates(MONDAY),
            PROCESSED,
        )

        assert not result.valid
        assert "governed session state conflict" in result.invalid_reason
    finally:
        ledger.close()


def test_policy_enabled_fill_without_intent_provenance_fails_closed(tmp_path) -> None:
    profile = _profile()
    ledger = _ledger(tmp_path)
    try:
        executor = SessionExecutor(ledger, _config(tmp_path, profile), size_profile=profile)
        entry = _stage_entry(
            ledger, executor, "AAPL", MONDAY, 2, suffix="missing", policy_provenance=False
        )
        with pytest.raises(LedgerConflictError, match="missing intent policy provenance"):
            executor.execute_open_and_mark(
                MONDAY, "epoch", _Prices({("AAPL", MONDAY): _bar("AAPL", MONDAY)}), {}, PROCESSED
            )
        assert ledger.intent(entry.intent_id).status == "pending"
        assert ledger.read_fills(MONDAY, MONDAY) == []
    finally:
        ledger.close()
