"""Restartable execution-first XNYS session lifecycle."""

from __future__ import annotations

import json
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
from tradingagents.strategies.state.portfolio_ledger import (
    LedgerConflictError,
    PortfolioLedger,
)
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


def _canonical_json_value(value: object) -> object:
    """Convert bounded execution configuration/state to deterministic JSON."""
    if isinstance(value, dict):
        return {
            str(key): _canonical_json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_json_value(item) for item in value]
    if isinstance(value, set):
        return sorted(_canonical_json_value(item) for item in value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        decimal_value = Decimal(str(value))
        if not decimal_value.is_finite():
            raise ValueError("execution context contains non-finite configuration")
        return format(decimal_value, "f")
    raise TypeError(f"unsupported execution-context value {type(value).__name__}")


@dataclass(frozen=True)
class SessionInputBundle:
    """One shared immutable fetch bundle for a session."""

    session: date
    tickers: tuple[str, ...]
    bars: dict[tuple[str, date], MarketBar]
    actions: tuple[CorporateAction, ...]
    benchmarks: dict[tuple[str, date], AdjustedClose]


@dataclass(frozen=True)
class _PersistedSessionInputBundle(SessionInputBundle):
    """Internal bundle whose freshness was validated when its context was bound."""

    validated_at: datetime


@dataclass(frozen=True)
class SessionExecutionResult:
    """Typed valid/invalid outcome; invalid sessions never fabricate a snapshot."""

    session: date
    valid: bool
    snapshot: AccountSnapshot | None
    invalid_reason: str
    completed_phases: tuple[str, ...]


class CorporateActionBatchError(ValueError):
    """The complete provider action response failed validation."""

    def __init__(
        self, actions: tuple[CorporateAction, ...], errors: tuple[str, ...]
    ) -> None:
        self.actions = actions
        self.errors = errors
        super().__init__("; ".join(errors))


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

    def persisted_input_bundle(self, session: date) -> SessionInputBundle:
        """Rehydrate bound canonical economics for a partial-session resume."""
        context = self.ledger.session_execution_context(session)
        if context is None:
            raise ValueError(f"session {session} has no bound execution context")
        economic = json.loads(str(context["economic_inputs_json"]))
        market = economic.get("market", {})
        provenance = json.loads(str(context["provenance_json"]))
        required = tuple(context["required_tickers"])
        bars = {
            (str(item["ticker"]), session): MarketBar(
                str(item["ticker"]),
                session,
                Decimal(str(item["open"])),
                Decimal(str(item["high"])),
                Decimal(str(item["low"])),
                Decimal(str(item["close"])),
                str(item["source"]),
                datetime.fromisoformat(
                    str(provenance["raw_bars"][str(item["ticker"])])
                ),
                bool(item["adjusted"]),
            )
            for item in market.get("raw_bars", [])
        }
        actions = tuple(
            CorporateAction(
                str(item["action_id"]),
                str(item["ticker"]),
                session,
                str(item["action_type"]),
                Decimal(str(item["ratio"])) if item.get("ratio") is not None else None,
                (
                    Decimal(str(item["cash_per_share"]))
                    if item.get("cash_per_share") is not None
                    else None
                ),
                str(item["source"]),
                datetime.fromisoformat(
                    str(provenance["corporate_actions"][str(item["action_id"])])
                ),
                bool(item["verified"]),
            )
            for item in market.get("corporate_actions", [])
        )
        benchmarks = {
            (str(item["symbol"]), session): AdjustedClose(
                str(item["symbol"]),
                session,
                Decimal(str(item["close"])),
                str(item["source"]),
                datetime.fromisoformat(
                    str(provenance["benchmarks"][str(item["symbol"])])
                ),
            )
            for item in market.get("benchmarks", [])
        }
        return _PersistedSessionInputBundle(
            session,
            required,
            bars,
            actions,
            benchmarks,
            validated_at=context["bound_at"],
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
        invalid_reason = self.ledger.session_invalid_reason(session)
        if invalid_reason:
            return SessionExecutionResult(session, False, None, invalid_reason, ())

        bound_context = self.ledger.session_execution_context(session)
        if bound_context is not None and bound_context["epoch_id"] != epoch_id:
            reason = (
                f"execution context epoch conflict: {bound_context['epoch_id']} "
                f"!= {epoch_id}"
            )
            if not existing:
                self.ledger.invalidate_session_and_cancel_due(
                    session, reason, processed_at
                )
            return SessionExecutionResult(session, False, None, reason, ())
        required = (
            tuple(bound_context["required_tickers"])
            if bound_context is not None
            else self.required_tickers(session)
        )
        config_inputs, borrow_inputs = self._static_context_documents(
            required, borrow_rates
        )
        config_digest = stable_id("session_execution_config", config_inputs)
        borrow_digest = stable_id("session_borrow_inputs", borrow_inputs)
        fully_complete = (
            len(existing) == 1
            and existing[0].valid
            and all(self.ledger.phase_completed(session, phase) for phase in PHASES)
        )
        if bound_context is not None and (
            bound_context["config_digest"] != config_digest
            or bound_context["borrow_digest"] != borrow_digest
        ):
            reason = (
                "execution context conflict: effective config or borrow inputs changed"
            )
            if not existing:
                self.ledger.invalidate_session_and_cancel_due(
                    session, reason, processed_at
                )
            return SessionExecutionResult(session, False, None, reason, ())
        if fully_complete and bound_context is not None:
            return SessionExecutionResult(session, True, existing[0], "", PHASES)
        for ticker in required:
            quarantine_reason = self.ledger.session_invalid_reason(session, ticker)
            if quarantine_reason:
                reason = f"quarantined {ticker}: {quarantine_reason}"
                self.ledger.invalidate_session_and_cancel_due(
                    session, reason, processed_at
                )
                return SessionExecutionResult(session, False, None, reason, ())
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
            market_inputs, config_inputs, borrow_inputs, provenance = (
                self._execution_context_documents(
                    session,
                    required,
                    bars,
                    actions,
                    benchmarks,
                    borrow_rates,
                )
            )
            if bound_context is None:
                economic_inputs = {
                    "market": market_inputs,
                    "starting_state": _canonical_json_value(
                        self.ledger.execution_starting_state(session)
                    ),
                }
                economic_json = json.dumps(
                    economic_inputs, sort_keys=True, separators=(",", ":")
                )
            else:
                economic_json = str(bound_context["economic_inputs_json"])
            market_digest = stable_id("session_market_inputs", market_inputs)
            input_digest = (
                stable_id("session_economic_inputs", economic_inputs)
                if bound_context is None
                else str(bound_context["input_digest"])
            )
            self.ledger.bind_session_execution_context(
                session,
                epoch_id,
                input_digest,
                market_digest,
                config_digest,
                borrow_digest,
                required,
                economic_json,
                json.dumps(provenance, sort_keys=True, separators=(",", ":")),
                processed_at,
            )
        except CorporateActionBatchError as error:
            reason = self.ledger.reject_corporate_action_batch(
                session,
                error.actions,
                required,
                error.errors,
                processed_at,
            )
            return SessionExecutionResult(session, False, None, reason, ())
        except LedgerConflictError as error:
            reason = f"execution context conflict: {error}"
            if not existing:
                self.ledger.invalidate_session_and_cancel_due(
                    session, reason, processed_at
                )
            return SessionExecutionResult(session, False, None, reason, ())
        except Exception as error:
            reason = f"market data validation failed: {error}"
            self.ledger.invalidate_session_and_cancel_due(session, reason, processed_at)
            return SessionExecutionResult(session, False, None, reason, ())

        if fully_complete:
            return SessionExecutionResult(session, True, existing[0], "", PHASES)

        completed: list[str] = []
        bridge = ExecutionBridge(self.config, ledger=self.ledger)
        opening_prices = {ticker: bar.open for ticker, bar in bars.items()}
        market_validated_at = (
            bundle.validated_at
            if isinstance(bundle, _PersistedSessionInputBundle)
            else processed_at
        )

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
                market_validated_at,
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
                market_validated_at,
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
            lambda: self.ledger.record_marks(
                session,
                bars,
                processed_at,
                validated_at=market_validated_at,
            ),
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

    def _execution_context_documents(
        self,
        session: date,
        required: tuple[str, ...],
        bars: dict[str, MarketBar],
        actions: tuple[CorporateAction, ...],
        benchmarks: dict[str, AdjustedClose],
        borrow_rates: dict[str, Decimal | None],
    ) -> tuple[
        dict[str, object],
        dict[str, object],
        dict[str, object],
        dict[str, object],
    ]:
        """Canonical economics exclude refetch timestamps; provenance retains them."""
        market_inputs: dict[str, object] = {
            "session": session.isoformat(),
            "required_tickers": list(required),
            "raw_bars": [
                {
                    "ticker": ticker,
                    "open": format(bars[ticker].open, "f"),
                    "high": format(bars[ticker].high, "f"),
                    "low": format(bars[ticker].low, "f"),
                    "close": format(bars[ticker].close, "f"),
                    "source": bars[ticker].source,
                    "adjusted": bars[ticker].adjusted,
                }
                for ticker in required
            ],
            "corporate_actions": [
                {
                    "action_id": action.action_id,
                    "ticker": action.ticker,
                    "action_type": action.action_type,
                    "ratio": (
                        format(action.ratio, "f") if action.ratio is not None else None
                    ),
                    "cash_per_share": (
                        format(action.cash_per_share, "f")
                        if action.cash_per_share is not None
                        else None
                    ),
                    "source": action.source,
                    "verified": action.verified,
                }
                for action in sorted(actions, key=lambda item: item.action_id)
            ],
            "benchmarks": [
                {
                    "symbol": symbol,
                    "close": format(benchmarks[symbol].close, "f"),
                    "source": benchmarks[symbol].source,
                }
                for symbol in sorted(benchmarks)
            ],
        }
        config_inputs, borrow_inputs = self._static_context_documents(
            required, borrow_rates
        )
        provenance: dict[str, object] = {
            "raw_bars": {
                ticker: bars[ticker].fetched_at.isoformat() for ticker in required
            },
            "corporate_actions": {
                action.action_id: action.fetched_at.isoformat() for action in actions
            },
            "benchmarks": {
                symbol: benchmarks[symbol].fetched_at.isoformat()
                for symbol in sorted(benchmarks)
            },
        }
        return market_inputs, config_inputs, borrow_inputs, provenance

    def _static_context_documents(
        self,
        required: tuple[str, ...],
        borrow_rates: dict[str, Decimal | None],
    ) -> tuple[dict[str, object], dict[str, object]]:
        """Canonical config and borrow inputs available without market I/O."""
        borrow_inputs: dict[str, object] = {
            ticker: (
                format(borrow_rates[ticker], "f")
                if borrow_rates.get(ticker) is not None
                else None
            )
            for ticker in required
        }
        config_inputs = _canonical_json_value(
            {
                "execution": {"mode": self.config.get("execution", {}).get("mode")},
                "paper_ledger": self.ledger_config,
                "risk_gate": self.ar_config.get("risk_gate", {}),
                "short_selling": self.ar_config.get("short_selling", {}),
                "total_capital": self.ar_config.get("total_capital"),
                "options": self.config.get("options", {}),
            }
        )
        assert isinstance(config_inputs, dict)
        return config_inputs, borrow_inputs

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
        market_validated_at: datetime,
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
                    "bar_validation_at": market_validated_at,
                    "opening_prices": opening_prices,
                    "borrow_rate": borrow_rates.get(ticker),
                    "open_trades": [
                        {
                            "ticker": position["ticker"],
                            "strategies": tuple(position["strategies"]),
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
        validation_at = (
            bundle.validated_at
            if isinstance(bundle, _PersistedSessionInputBundle)
            else processed_at
        )
        # The response is one provider batch. Validate it globally before
        # selecting the governed subset for this cohort.
        self._validate_actions(
            bundle.actions,
            tuple(sorted(set(bundle.tickers))),
            session,
            validation_at,
            max_age,
        )
        validate_required_bars(
            bundle.bars, set(required), session, validation_at, max_age
        )
        validate_adjusted_closes(
            bundle.benchmarks,
            set(self.benchmark_symbols),
            session,
            validation_at,
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
        errors: list[str] = []
        for action in actions:
            if action.action_id in seen and seen[action.action_id] != action:
                errors.append(f"conflicting corporate action {action.action_id}")
            seen[action.action_id] = action
            if action.ticker not in tickers or action.session != session:
                errors.append(f"corporate action scope mismatch {action.action_id}")
            if not action.source.strip():
                errors.append(f"missing source corporate action {action.action_id}")
            if not action.verified:
                errors.append(f"unverified corporate action {action.action_id}")
            if (
                action.fetched_at.tzinfo is None
                or action.fetched_at.utcoffset() is None
            ):
                errors.append(f"naive corporate action {action.action_id}")
            else:
                if action.fetched_at > processed_at:
                    errors.append(f"future corporate action {action.action_id}")
                if action.fetched_at < session_close(session):
                    errors.append(f"pre-close corporate action {action.action_id}")
                if processed_at - action.fetched_at > max_age:
                    errors.append(f"stale corporate action {action.action_id}")
            if action.action_type == "split":
                if (
                    action.ratio is None
                    or not action.ratio.is_finite()
                    or action.ratio <= 0
                    or action.cash_per_share is not None
                ):
                    errors.append(f"invalid split {action.action_id}")
            elif action.action_type == "cash_dividend":
                if (
                    action.cash_per_share is None
                    or not action.cash_per_share.is_finite()
                    or action.cash_per_share < 0
                    or action.ratio is not None
                ):
                    errors.append(f"invalid dividend {action.action_id}")
            else:
                errors.append(f"unsupported corporate action {action.action_id}")
        if errors:
            raise CorporateActionBatchError(actions, tuple(sorted(set(errors))))

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
