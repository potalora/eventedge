"""Restartable execution-first XNYS session lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable

from tradingagents.strategies.execution import (
    AccountSnapshot,
    BenchmarkObservation,
    CorporateAction,
    MarketBar,
    stable_id,
)
from tradingagents.strategies.execution.cost_model import PaperCostModel
from tradingagents.strategies.execution.price_source import (
    AdjustedClose,
    BarValidationError,
    PriceSource,
    validate_adjusted_closes,
    validate_required_bars,
)
from tradingagents.strategies.orchestration.trading_calendar import (
    is_session,
    session_close,
)
from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger
from tradingagents.strategies.trading.execution_bridge import ExecutionBridge


PHASES = (
    "validate_market_data",
    "apply_corporate_actions",
    "execute_exits",
    "execute_entries",
    "accrue_borrow",
    "accrue_financing",
    "mark_positions",
    "record_benchmarks",
    "snapshot_account",
)

_SIDE_PRIORITY = {"sell": 0, "cover": 0, "buy": 1, "short": 1}


@dataclass(frozen=True)
class SessionInputBundle:
    """One shared immutable fetch bundle for a session."""

    session: date
    tickers: tuple[str, ...]
    bars: dict[tuple[str, date], MarketBar]
    actions: tuple[CorporateAction, ...]
    benchmarks: dict[tuple[str, date], AdjustedClose]


@dataclass(frozen=True)
class SessionExecutionResult:
    """Typed valid/invalid outcome; invalid sessions never fabricate a snapshot."""

    session: date
    valid: bool
    snapshot: AccountSnapshot | None
    invalid_reason: str
    completed_phases: tuple[str, ...]


class SessionExecutor:
    """Run exact daily economics before any new signal is staged."""

    def __init__(
        self,
        ledger: PortfolioLedger,
        config: dict,
        *,
        after_phase_mutation: Callable[[str], None] | None = None,
        after_phase_commit: Callable[[str], None] | None = None,
    ) -> None:
        self.ledger = ledger
        self.config = config
        if config.get("execution", {}).get("mode", "paper") != "paper":
            raise ValueError("EventEdge cohort execution must remain paper-only")
        self.ar_config = config.get("autoresearch", {})
        self.ledger_config = self.ar_config.get("paper_ledger", {})
        self.benchmark_symbols = tuple(
            self.ledger_config.get("benchmark_symbols", ("SPY", "BIL"))
        )
        if not self.benchmark_symbols or len(set(self.benchmark_symbols)) != len(
            self.benchmark_symbols
        ):
            raise ValueError("benchmark_symbols must be unique and non-empty")
        self.cost_model = PaperCostModel(self.ledger_config)
        self._after_phase_mutation = after_phase_mutation or (lambda phase: None)
        self._after_phase_commit = after_phase_commit or (lambda phase: None)

    def required_tickers(self, session: date) -> tuple[str, ...]:
        """Bounded union of held and due-intent tickers."""
        tickers = {str(position["ticker"]) for position in self.ledger.open_positions()}
        for intent in self.ledger.pending_intents(session):
            signals = self.ledger.signals_for_intent(intent.intent_id)
            provenance = {signal.ticker for signal in signals}
            if len(provenance) != 1:
                raise ValueError(
                    f"intent {intent.intent_id} has ambiguous ticker provenance"
                )
            tickers.update(provenance)
        return tuple(sorted(tickers))

    @classmethod
    def fetch_input_bundle(
        cls,
        session: date,
        tickers: tuple[str, ...],
        price_source: PriceSource,
        benchmark_symbols: tuple[str, ...] = ("SPY", "BIL"),
    ) -> SessionInputBundle:
        """Fetch the raw/action/adjusted set once for any number of cohorts."""
        bars = (
            price_source.get_daily_bars(list(tickers), session, session, adjusted=False)
            if tickers
            else {}
        )
        actions = (
            tuple(price_source.get_corporate_actions(list(tickers), session))
            if tickers
            else ()
        )
        benchmarks = price_source.get_total_return_closes(
            list(benchmark_symbols), session, session
        )
        return SessionInputBundle(
            session, tuple(sorted(tickers)), bars, actions, benchmarks
        )

    def execute_open_and_mark(
        self,
        session: date,
        epoch_id: str,
        price_source: PriceSource | SessionInputBundle,
        borrow_rates: dict[str, Decimal | None],
        processed_at: datetime,
    ) -> SessionExecutionResult:
        """Execute the exact nine phases, resuming only at committed boundaries."""
        self._validate_clock(session, epoch_id, processed_at)
        existing = self.ledger.read_snapshots(session, session)
        if existing and existing[0].epoch_id != epoch_id:
            raise ValueError(
                f"session {session} already belongs to epoch {existing[0].epoch_id}"
            )
        if (
            len(existing) == 1
            and existing[0].valid
            and all(self.ledger.phase_completed(session, phase) for phase in PHASES)
        ):
            return SessionExecutionResult(session, True, existing[0], "", PHASES)
        invalid_reason = self.ledger.session_invalid_reason(session)
        if invalid_reason:
            return SessionExecutionResult(session, False, None, invalid_reason, ())

        required = self.required_tickers(session)
        try:
            bundle = (
                price_source
                if isinstance(price_source, SessionInputBundle)
                else self.fetch_input_bundle(
                    session, required, price_source, self.benchmark_symbols
                )
            )
            bars, actions, benchmarks = self._validate_bundle(
                bundle, required, session, processed_at
            )
        except Exception as error:
            reason = f"market data validation failed: {error}"
            self.ledger.invalidate_session_and_cancel_due(session, reason, processed_at)
            return SessionExecutionResult(session, False, None, reason, ())

        completed: list[str] = []
        bridge = ExecutionBridge(self.config, ledger=self.ledger)
        opening_prices = {ticker: bar.open for ticker, bar in bars.items()}

        self._phase(
            session,
            "validate_market_data",
            processed_at,
            lambda: None,
            completed,
        )
        self._phase(
            session,
            "apply_corporate_actions",
            processed_at,
            lambda: self.ledger.apply_corporate_actions(
                session, list(actions), processed_at
            ),
            completed,
        )
        self._phase(
            session,
            "execute_exits",
            processed_at,
            lambda: self._execute_intents(
                session,
                {"sell", "cover"},
                bridge,
                bars,
                opening_prices,
                borrow_rates,
                processed_at,
            ),
            completed,
        )
        self._phase(
            session,
            "execute_entries",
            processed_at,
            lambda: self._execute_intents(
                session,
                {"buy", "short"},
                bridge,
                bars,
                opening_prices,
                borrow_rates,
                processed_at,
            ),
            completed,
        )
        self._phase(
            session,
            "accrue_borrow",
            processed_at,
            lambda: self.ledger.accrue_borrow(
                session, bars, borrow_rates, processed_at
            ),
            completed,
        )
        self._phase(
            session,
            "accrue_financing",
            processed_at,
            lambda: self.ledger.accrue_financing(
                session, self.cost_model.margin_financing_rate, processed_at
            ),
            completed,
        )
        self._phase(
            session,
            "mark_positions",
            processed_at,
            lambda: self.ledger.record_marks(session, bars, processed_at),
            completed,
        )
        self._phase(
            session,
            "record_benchmarks",
            processed_at,
            lambda: self._record_benchmarks(session, epoch_id, benchmarks),
            completed,
        )

        def snapshot_operation() -> AccountSnapshot:
            snapshot = self.ledger.snapshot_account(session, epoch_id, processed_at)
            if not snapshot.valid:
                raise ValueError(snapshot.invalid_reason or "invalid account snapshot")
            self.ledger.record_session_complete(session, processed_at)
            return snapshot

        _, snapshot = self._phase(
            session,
            "snapshot_account",
            processed_at,
            snapshot_operation,
            completed,
        )
        if snapshot is None:
            snapshots = self.ledger.read_snapshots(session, session, epoch_id)
            if len(snapshots) != 1:
                raise RuntimeError(f"missing completed snapshot for {session}")
            snapshot = snapshots[0]
        return SessionExecutionResult(session, True, snapshot, "", tuple(completed))

    def _phase(
        self,
        session: date,
        phase: str,
        processed_at: datetime,
        operation: Callable[[], object],
        completed: list[str],
    ) -> tuple[bool, object | None]:
        def atomic_operation() -> object:
            value = operation()
            self._after_phase_mutation(phase)
            return value

        executed, value = self.ledger.run_session_phase(
            session, phase, processed_at, atomic_operation
        )
        if executed:
            completed.append(phase)
            self._after_phase_commit(phase)
        return executed, value

    def _execute_intents(
        self,
        session: date,
        sides: set[str],
        bridge: ExecutionBridge,
        bars: dict[str, MarketBar],
        opening_prices: dict[str, Decimal],
        borrow_rates: dict[str, Decimal | None],
        processed_at: datetime,
    ) -> None:
        intents = sorted(
            (
                intent
                for intent in self.ledger.pending_intents(session)
                if intent.side in sides
            ),
            key=lambda intent: (
                _SIDE_PRIORITY[intent.side],
                intent.created_at,
                intent.intent_id,
            ),
        )
        for intent in intents:
            signals = self.ledger.signals_for_intent(intent.intent_id)
            tickers = {signal.ticker for signal in signals}
            if len(tickers) != 1:
                raise ValueError(f"ambiguous ticker for intent {intent.intent_id}")
            ticker = next(iter(tickers))
            bridge.execute_due_intent(
                intent,
                bars[ticker],
                self.ledger.account_state(),
                {
                    "processing_at": processed_at,
                    "opening_prices": opening_prices,
                    "borrow_rate": borrow_rates.get(ticker),
                    "open_trades": [
                        {
                            "ticker": position["ticker"],
                            "strategy": (
                                position["strategies"][0]
                                if position["strategies"]
                                else ""
                            ),
                        }
                        for position in self.ledger.open_exit_positions()
                    ],
                    "earnings_dates": {},
                    "short_interest": {},
                },
                self.cost_model,
            )

    def _record_benchmarks(
        self,
        session: date,
        epoch_id: str,
        benchmarks: dict[str, AdjustedClose],
    ) -> None:
        for symbol in sorted(benchmarks):
            adjusted = benchmarks[symbol]
            self.ledger.record_benchmark_observation(
                BenchmarkObservation(
                    stable_id(
                        "benchmark",
                        self.ledger.cohort_id,
                        epoch_id,
                        session,
                        symbol,
                    ),
                    self.ledger.cohort_id,
                    epoch_id,
                    session,
                    symbol,
                    adjusted.close,
                    "total_return_adjusted",
                    adjusted.source,
                    adjusted.fetched_at,
                    True,
                    "",
                )
            )

    def _validate_bundle(
        self,
        bundle: SessionInputBundle,
        required: tuple[str, ...],
        session: date,
        processed_at: datetime,
    ) -> tuple[
        dict[str, MarketBar],
        tuple[CorporateAction, ...],
        dict[str, AdjustedClose],
    ]:
        if bundle.session != session:
            raise ValueError("input bundle session mismatch")
        if not set(required).issubset(bundle.tickers):
            raise ValueError("input bundle does not cover every required ticker")
        max_age = timedelta(
            hours=float(self.ledger_config.get("bar_max_age_hours", 24))
        )
        validate_required_bars(
            bundle.bars, set(required), session, processed_at, max_age
        )
        validate_adjusted_closes(
            bundle.benchmarks,
            set(self.benchmark_symbols),
            session,
            processed_at,
            max_age,
        )
        cutoff = session_close(session)
        for ticker in required:
            if bundle.bars[(ticker, session)].fetched_at < cutoff:
                raise BarValidationError(f"pre-close {ticker}/{session}")
        for symbol in self.benchmark_symbols:
            if bundle.benchmarks[(symbol, session)].fetched_at < cutoff:
                raise BarValidationError(f"pre-close {symbol}/{session}")
        actions = tuple(
            action for action in bundle.actions if action.ticker in set(required)
        )
        self._validate_actions(actions, required, session, processed_at, max_age)
        bars = {ticker: bundle.bars[(ticker, session)] for ticker in required}
        benchmarks = {
            symbol: bundle.benchmarks[(symbol, session)]
            for symbol in self.benchmark_symbols
        }
        return bars, actions, benchmarks

    @staticmethod
    def _validate_actions(
        actions: tuple[CorporateAction, ...],
        tickers: tuple[str, ...],
        session: date,
        processed_at: datetime,
        max_age: timedelta,
    ) -> None:
        seen: dict[str, CorporateAction] = {}
        for action in actions:
            if action.action_id in seen and seen[action.action_id] != action:
                raise ValueError(f"conflicting corporate action {action.action_id}")
            seen[action.action_id] = action
            if action.ticker not in tickers or action.session != session:
                raise ValueError(f"corporate action scope mismatch {action.action_id}")
            if not action.verified:
                raise ValueError(f"unverified corporate action {action.action_id}")
            if action.fetched_at.tzinfo is None:
                raise ValueError(f"naive corporate action {action.action_id}")
            if action.fetched_at > processed_at:
                raise ValueError(f"future corporate action {action.action_id}")
            if action.fetched_at < session_close(session):
                raise ValueError(f"pre-close corporate action {action.action_id}")
            if processed_at - action.fetched_at > max_age:
                raise ValueError(f"stale corporate action {action.action_id}")
            if action.action_type == "split":
                if (
                    action.ratio is None
                    or not action.ratio.is_finite()
                    or action.ratio <= 0
                    or action.cash_per_share is not None
                ):
                    raise ValueError(f"invalid split {action.action_id}")
            elif action.action_type == "cash_dividend":
                if (
                    action.cash_per_share is None
                    or not action.cash_per_share.is_finite()
                    or action.cash_per_share < 0
                    or action.ratio is not None
                ):
                    raise ValueError(f"invalid dividend {action.action_id}")
            else:
                raise ValueError(f"unsupported corporate action {action.action_id}")

    @staticmethod
    def _validate_clock(session: date, epoch_id: str, processed_at: datetime) -> None:
        if not is_session(session):
            raise ValueError(f"{session} is not an XNYS session")
        if not epoch_id:
            raise ValueError("epoch_id is required")
        if processed_at.tzinfo is None or processed_at.utcoffset() is None:
            raise ValueError("processed_at must be timezone-aware")
        if processed_at < session_close(session):
            raise ValueError("processed_at precedes the exact XNYS close")


def ensure_reference_bars(
    price_source: PriceSource,
    tickers: set[str],
    session: date,
    processed_at: datetime,
    max_age: timedelta = timedelta(hours=24),
) -> dict[str, MarketBar]:
    """Bulk fetch and validate exact-D raw closes for new candidate identities."""
    if not tickers:
        return {}
    bars = price_source.get_daily_bars(
        sorted(tickers), session, session, adjusted=False
    )
    validation_at = max(processed_at, datetime.now(timezone.utc))
    validate_required_bars(bars, tickers, session, validation_at, max_age)
    cutoff = session_close(session)
    for ticker in sorted(tickers):
        if bars[(ticker, session)].fetched_at < cutoff:
            raise BarValidationError(f"pre-close {ticker}/{session}")
    return {ticker: bars[(ticker, session)] for ticker in sorted(tickers)}
