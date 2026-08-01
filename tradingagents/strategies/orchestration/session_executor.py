"""Restartable execution-first XNYS session lifecycle."""

from __future__ import annotations

import json
from importlib.metadata import version as package_version
from dataclasses import asdict, dataclass, fields, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable, Mapping

from tradingagents.strategies.execution import (
    AccountSnapshot,
    BenchmarkObservation,
    CorporateAction,
    MarketBar,
    SignalRecord,
    stable_id,
)
from tradingagents.strategies.execution.cost_model import PaperCostModel
from tradingagents.strategies.execution.contracts import (
    COST_MODEL_VERSION,
    EXECUTION_CLOCK_VERSION,
    POLICY_DOCUMENT_VERSION,
    PRICING_VERSION,
)
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
    session_open,
)
from tradingagents.strategies.metrics.models import OUTCOME_WINDOWS, SignalMetricRecord
from tradingagents.strategies.metrics.epochs import EpochContext, EpochManager
from tradingagents.strategies.metrics.models import MetricEpoch
from tradingagents.strategies.metrics.outcomes import OutcomeCalculator
from tradingagents.strategies.metrics.store import MetricStore
from tradingagents.strategies.state.portfolio_ledger import (
    LedgerConflictError,
    PortfolioLedger,
    SCHEMA_VERSION,
)
from tradingagents.strategies.trading.execution_bridge import ExecutionBridge
from tradingagents.strategies.trading.risk_gate import RiskGateConfig
from tradingagents.strategies.trading.portfolio_policy import (
    PortfolioPolicyConfig,
    PortfolioRiskContext,
    build_portfolio_risk_context,
    portfolio_policy_config_document,
    portfolio_risk_context_document,
)
from tradingagents.strategies.trading.portfolio_committee import TradeRecommendation


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

    def for_tickers(self, tickers: tuple[str, ...]) -> SessionInputBundle:
        """Return a cohort-scoped view of one validated shared response."""
        selected = tuple(sorted(set(tickers)))
        if not set(selected).issubset(self.tickers):
            raise ValueError("cohort ticker scope is outside the shared input bundle")
        return SessionInputBundle(
            self.session,
            selected,
            {key: value for key, value in self.bars.items() if key[0] in selected},
            tuple(action for action in self.actions if action.ticker in selected),
            self.benchmarks,
        )


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
        size_profile: object | None = None,
        metric_store: MetricStore | None = None,
        after_phase_mutation: Callable[[str], None] | None = None,
        after_phase_commit: Callable[[str], None] | None = None,
    ) -> None:
        self.ledger = ledger
        self.config = config
        self.size_profile = size_profile
        if config.get("execution", {}).get("mode", "paper") != "paper":
            raise ValueError("EventEdge cohort execution must remain paper-only")
        self.ar_config = config.get("autoresearch", {})
        policy_settings = self.ar_config.get("portfolio_policy")
        self.policy_enabled = size_profile is not None and isinstance(
            policy_settings, dict
        )
        self.portfolio_policy_config = (
            PortfolioPolicyConfig.from_size_profile(size_profile, policy_settings)
            if self.policy_enabled
            else None
        )
        self.ledger_config = self.ar_config.get("paper_ledger", {})
        self.benchmark_symbols = tuple(
            self.ledger_config.get("benchmark_symbols", ("SPY", "BIL"))
        )
        if not self.benchmark_symbols or len(set(self.benchmark_symbols)) != len(
            self.benchmark_symbols
        ):
            raise ValueError("benchmark_symbols must be unique and non-empty")
        self.cost_model = PaperCostModel(self.ledger_config)
        self.borrow_reject_above = PaperCostModel._configured_decimal(
            self.ar_config.get("short_selling", {}),
            "borrow_cost_reject_above",
            "0.05",
        )
        self.outcome_calculator = OutcomeCalculator()
        state_dir = Path(self.ar_config.get("state_dir", self.ledger.path.parent))
        self.metric_store = metric_store or MetricStore(
            state_dir / "metrics_v2.sqlite3"
        )
        self._after_phase_mutation = after_phase_mutation or (lambda phase: None)
        self._after_phase_commit = after_phase_commit or (lambda phase: None)

    def semantic_policy_document(self) -> dict[str, object]:
        """Return canonical, secret-free, session-invariant execution semantics."""
        config_inputs, _ = self._static_context_documents((), {})
        return config_inputs

    def portfolio_policy_document(self) -> dict[str, object] | None:
        """Return the normalized profile-bound policy or ``None`` for legacy callers."""
        if self.portfolio_policy_config is None:
            return None
        return {
            field.name: getattr(self.portfolio_policy_config, field.name)
            for field in fields(self.portfolio_policy_config)
        }

    def _verify_committed_execution_policy_binding(
        self, session: date, epoch_id: str
    ) -> None:
        """Require the exact policy artifact committed by the entry phase."""
        if not self.ledger.phase_completed(session, "execute_entries"):
            return
        binding = self.ledger.read_policy_session_context(
            session, binding_kind="execution"
        )
        if self.portfolio_policy_config is None:
            if binding is not None:
                raise LedgerConflictError(
                    "execution policy binding exists while portfolio policy is disabled"
                )
            return
        if binding is None:
            raise LedgerConflictError(
                f"missing committed execution policy binding for {session}"
            )
        if (
            binding["epoch_id"] != epoch_id
            or binding["policy_version"] != self.portfolio_policy_config.version
            or binding["policy_config"]
            != portfolio_policy_config_document(self.portfolio_policy_config)
        ):
            raise LedgerConflictError(
                f"committed execution policy binding mismatch for {session}"
            )

    def ensure_metric_epoch(
        self, context: EpochContext, session: date
    ) -> MetricEpoch:
        epoch = EpochManager(self.metric_store).ensure_epoch(context, session)
        if epoch.status == "invalid" and epoch.end_session == session:
            return epoch
        if epoch.status != "open" or epoch.end_session is not None:
            raise RuntimeError("metric epoch is not open")
        return epoch

    def invalidate_metric_epoch(
        self,
        session: date,
        reason: str = "critical_market_data_gap",
        *,
        epoch_id: str | None = None,
    ) -> MetricEpoch:
        if reason != "critical_market_data_gap":
            raise ValueError("metric epoch invalidation reason must be stable")
        if epoch_id is not None:
            return self.metric_store.invalidate_epoch(epoch_id, session, reason)
        return EpochManager(self.metric_store).invalidate_current(session, reason)

    def required_tickers(self, session: date, epoch_id: str) -> tuple[str, ...]:
        """Bounded union of execution and exact outcome tickers."""
        tickers = {str(position["ticker"]) for position in self.ledger.open_positions()}
        for intent in self.ledger.pending_intents(session):
            signals = self.ledger.signals_for_intent(intent.intent_id)
            provenance = {signal.ticker for signal in signals}
            if len(provenance) != 1:
                raise ValueError(
                    f"intent {intent.intent_id} has ambiguous ticker provenance"
                )
            tickers.update(provenance)
        tickers.update(self.outcome_tickers(session, epoch_id))
        return tuple(sorted(tickers))

    def outcome_tickers(self, session: date, epoch_id: str) -> tuple[str, ...]:
        """Return signal tickers requiring an exact raw entry or exit bar today."""
        tickers: set[str] = set()
        earliest_reference = session
        for _ in range(max(OUTCOME_WINDOWS)):
            earliest_reference = self.outcome_calculator.calendar.previous_session(
                earliest_reference
            )
        for signal in self.ledger.read_signals(
            earliest_reference, session, epoch_id=epoch_id
        ):
            metric_signal = self._metric_signal(signal)
            entry_session = self.outcome_calculator.calendar.next_session(
                metric_signal.reference_session
            )
            if entry_session == session:
                tickers.add(metric_signal.ticker)
                continue
            for window in OUTCOME_WINDOWS:
                if self.outcome_calculator.calendar.held_session(
                    entry_session, window
                ) == session:
                    tickers.add(metric_signal.ticker)
                    break
        return tuple(sorted(tickers))

    @staticmethod
    def _metric_signal(signal: SignalRecord) -> SignalMetricRecord:
        return SignalMetricRecord(
            event_key=signal.event_key,
            signal_id=signal.signal_id,
            epoch_id=signal.epoch_id,
            policy_id=signal.policy_id,
            strategy=signal.strategy,
            ticker=signal.ticker,
            direction=signal.direction,
            decision_at=signal.decision_at,
            reference_session=signal.reference_session,
        )

    def due_outcome_signals(
        self, session: date, epoch_id: str
    ) -> tuple[tuple[SignalMetricRecord, int], ...]:
        """Convert authoritative ledger signals whose exact outcome closes today."""
        due: list[tuple[SignalMetricRecord, int]] = []
        earliest_reference = session
        for _ in range(max(OUTCOME_WINDOWS)):
            earliest_reference = self.outcome_calculator.calendar.previous_session(
                earliest_reference
            )
        for signal in self.ledger.read_signals(
            earliest_reference, session, epoch_id=epoch_id
        ):
            metric_signal = self._metric_signal(signal)
            entry_session = self.outcome_calculator.calendar.next_session(
                metric_signal.reference_session
            )
            for window in OUTCOME_WINDOWS:
                if (
                    self.outcome_calculator.calendar.held_session(entry_session, window)
                    == session
                ):
                    due.append((metric_signal, window))
        return tuple(due)

    def record_due_outcomes(
        self,
        session: date,
        epoch_id: str,
        raw_bars: dict[tuple[str, date], MarketBar],
        invalid_reasons: Mapping[str, str] | None = None,
        *,
        preserve_existing_valid: bool = False,
    ) -> int:
        """Persist due outcomes from shared current bars and durable entry bars only."""
        written = 0
        forced_reasons = invalid_reasons or {}
        for signal, window in self.due_outcome_signals(session, epoch_id):
            entry_session = self.outcome_calculator.calendar.next_session(
                signal.reference_session
            )
            bars = dict(raw_bars)
            if self.ledger.session_execution_context(entry_session) is not None:
                bars.update(self.persisted_input_bundle(entry_session).bars)
            outcome = self.outcome_calculator.build(signal, window, bars)
            forced_reason = forced_reasons.get(signal.ticker)
            if (
                forced_reason
                and not outcome.invalid_reason.endswith("entry_bar")
                and not outcome.invalid_reason.endswith("entry_price")
            ):
                outcome = replace(
                    outcome,
                    exit_price=None,
                    raw_return=None,
                    signed_return=None,
                    status="invalid",
                    invalid_reason=forced_reason,
                )
            if preserve_existing_valid:
                try:
                    existing = self.metric_store.load_outcome(outcome.outcome_id)
                except KeyError:
                    existing = None
                if existing is not None and existing.status == "valid":
                    continue
            self.metric_store.upsert_outcome(outcome)
            written += 1
        return written

    def record_due_invalid_outcomes(
        self,
        session: date,
        epoch_id: str,
        invalid_reasons: Mapping[str, str],
        *,
        preserve_existing: bool = True,
    ) -> int:
        """Recover due invalid outcomes without current-session market input."""
        written = 0
        for signal, window in self.due_outcome_signals(session, epoch_id):
            outcome_id = self.outcome_calculator.outcome_id(signal, window)
            if preserve_existing:
                try:
                    self.metric_store.load_outcome(outcome_id)
                except KeyError:
                    pass
                else:
                    continue
            entry_session = self.outcome_calculator.calendar.next_session(
                signal.reference_session
            )
            bars: dict[tuple[str, date], MarketBar] = {}
            if self.ledger.session_execution_context(entry_session) is not None:
                bars.update(self.persisted_input_bundle(entry_session).bars)
            outcome = self.outcome_calculator.build(signal, window, bars)
            forced_reason = invalid_reasons.get(
                signal.ticker, "critical_market_data_gap"
            )
            if not outcome.invalid_reason.endswith("entry_bar") and not (
                outcome.invalid_reason.endswith("entry_price")
            ):
                outcome = replace(
                    outcome,
                    exit_price=None,
                    raw_return=None,
                    signed_return=None,
                    status="invalid",
                    invalid_reason=forced_reason,
                )
            self.metric_store.upsert_outcome(outcome)
            written += 1
        return written

    def validated_outcome_bars(
        self,
        session: date,
        epoch_id: str,
        raw_bars: Mapping[tuple[str, date], MarketBar],
        validation_at: datetime,
    ) -> tuple[dict[tuple[str, date], MarketBar], dict[str, str]]:
        """Validate only exact due-exit bars without fetching or using a fallback."""
        due_tickers = {
            signal.ticker for signal, _ in self.due_outcome_signals(session, epoch_id)
        }
        valid: dict[tuple[str, date], MarketBar] = {}
        invalid: dict[str, str] = {}
        max_age = timedelta(
            hours=float(self.ledger_config.get("bar_max_age_hours", 24))
        )
        for ticker in sorted(due_tickers):
            key = (ticker, session)
            bar = raw_bars.get(key)
            if bar is None:
                invalid[ticker] = "missing_exit_bar"
                continue
            if (
                bar.fetched_at.tzinfo is not None
                and bar.fetched_at.utcoffset() is not None
                and validation_at - bar.fetched_at > max_age
            ):
                invalid[ticker] = "stale_exit_bar"
                continue
            try:
                validate_required_bars(
                    {key: bar}, {ticker}, session, validation_at, max_age
                )
                if bar.fetched_at < session_close(session):
                    raise BarValidationError(f"pre-close {ticker}/{session}")
            except (BarValidationError, KeyError, TypeError, ValueError):
                invalid[ticker] = "invalid_exit_bar"
                continue
            valid[key] = bar
        return valid, invalid

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

    def validate_execution_input_bundle(
        self,
        session: date,
        epoch_id: str,
        bundle: SessionInputBundle,
        processed_at: datetime,
    ) -> None:
        """Preflight a fresh cohort bundle without mutating its P0 ledger."""
        self._validate_clock(session, epoch_id, processed_at)
        required = self.required_tickers(session, epoch_id)
        _, actions, _ = self._validate_bundle(
            bundle, required, session, processed_at
        )
        state_errors = self.ledger.corporate_action_batch_state_errors(
            session, actions
        )
        if state_errors:
            raise CorporateActionBatchError(actions, state_errors)

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

    def persisted_borrow_rates(self, session: date) -> dict[str, Decimal | None]:
        """Rehydrate the exact canonical borrow document bound to a session."""
        context = self.ledger.session_execution_context(session)
        if context is None:
            raise ValueError(f"session {session} has no bound execution context")
        values = json.loads(str(context["borrow_inputs_json"]))
        return {
            str(ticker): Decimal(str(value)) if value is not None else None
            for ticker, value in values.items()
        }

    def validate_bound_context(
        self, session: date, epoch_id: str
    ) -> dict[str, Decimal | None]:
        """Validate a bound complete/stage/partial replay using ledger state only."""
        context = self.ledger.session_execution_context(session)
        if context is None:
            raise LedgerConflictError(f"missing execution context for {session}")
        if context["epoch_id"] != epoch_id:
            raise LedgerConflictError(f"execution context epoch conflict for {session}")
        borrow_rates = self.persisted_borrow_rates(session)
        config_inputs, borrow_inputs = self._static_context_documents(
            tuple(context["required_tickers"]), borrow_rates
        )
        if context["config_digest"] != stable_id(
            "session_execution_config", config_inputs
        ) or context["borrow_digest"] != stable_id(
            "session_borrow_inputs", borrow_inputs
        ):
            raise LedgerConflictError(
                "execution context conflict: effective config or borrow inputs changed"
            )
        self.ledger.verify_session_phase_chain(session, PHASES)
        return borrow_rates

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

        self.ledger.cancel_overdue_next_open_intents(session, session_open(session))

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
            else self.required_tickers(session, epoch_id)
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
        if bound_context is not None:
            try:
                self._expected_state_digest = self.ledger.verify_session_phase_chain(
                    session, PHASES
                )
                self._verify_committed_execution_policy_binding(session, epoch_id)
            except LedgerConflictError as error:
                reason = f"execution context conflict: {error}"
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
            if bound_context is None:
                state_errors = self.ledger.corporate_action_batch_state_errors(
                    session, actions
                )
                if state_errors:
                    raise CorporateActionBatchError(actions, state_errors)
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
                starting_state = self.ledger.execution_starting_state(session)
                economic_inputs = {
                    "market": market_inputs,
                    "starting_state": _canonical_json_value(starting_state),
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
                (
                    stable_id("session_governed_state", starting_state)
                    if bound_context is None
                    else str(bound_context["starting_state_digest"])
                ),
                json.dumps(borrow_inputs, sort_keys=True, separators=(",", ":")),
            )
            if bound_context is not None:
                state_errors = self.ledger.corporate_action_batch_state_errors(
                    session, actions
                )
                if state_errors:
                    raise CorporateActionBatchError(actions, state_errors)
            self._expected_state_digest = self.ledger.verify_session_phase_chain(
                session, PHASES
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
            lambda: self._execute_entry_intents(
                session,
                epoch_id,
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
        cost_model = {
            name: format(getattr(self.cost_model, name), "f")
            for name in PaperCostModel.DEFAULTS
        }
        semantic_inputs: dict[str, object] = {
                "policy_document_version": POLICY_DOCUMENT_VERSION,
                "execution": {
                    "mode": self.config.get("execution", {}).get("mode", "paper"),
                    "price_rules": ["next_session_open", "resting_stop"],
                },
                "schema_version": SCHEMA_VERSION,
                "pricing_contract": PRICING_VERSION,
                "execution_clock_contract": EXECUTION_CLOCK_VERSION,
                "cost_model_contract": COST_MODEL_VERSION,
                "calendar": {
                    "name": "XNYS",
                    "provider": "exchange-calendars",
                    "provider_version": package_version("exchange-calendars"),
                },
                "bar_max_age_hours": self.ledger_config.get("bar_max_age_hours", 24),
                "benchmark_symbols": list(self.benchmark_symbols),
                "cost_model": cost_model,
                "risk_gate": asdict(RiskGateConfig.from_dict(self.config)),
                "short_selling": {
                    "borrow_cost_reject_above": format(self.borrow_reject_above, "f"),
                },
            }
        policy_document = self.portfolio_policy_document()
        if policy_document is not None:
            semantic_inputs["portfolio_policy"] = policy_document
        config_inputs = _canonical_json_value(semantic_inputs)
        assert isinstance(config_inputs, dict)
        return config_inputs, borrow_inputs

    def _execute_entry_intents(
        self,
        session: date,
        epoch_id: str,
        bridge: ExecutionBridge,
        bars: dict[str, MarketBar],
        opening_prices: dict[str, Decimal],
        borrow_rates: dict[str, Decimal | None],
        processed_at: datetime,
        market_validated_at: datetime,
    ) -> None:
        """Bind one post-exit baseline, then validate every entry against it."""
        portfolio_context = None
        if self.portfolio_policy_config is not None:
            account = self.ledger.account_state()
            current = self.ledger.policy_open_lot_projection(session)
            pending = self._policy_pending_with_execution_prices(
                session, self.ledger.policy_pending_entry_projection(), opening_prices
            )
            sectors = {
                str(row["ticker"]): str(row.get("sector", "Unknown"))
                for row in current + pending
            }
            borrow_available = {
                str(row["ticker"]): borrow_rates.get(str(row["ticker"])) is not None
                for row in pending
                if str(row.get("direction")) == "short"
            }
            portfolio_context = build_portfolio_risk_context(
                portfolio_value=float(account.net_equity),
                cash=float(account.cash),
                current_positions=current,
                pending_positions=pending,
                price_cache={},
                earnings_dates={},
                short_interest={},
                borrow_available=borrow_available,
                margin_used=float(account.margin_used),
                consumed_event_keys=self.ledger.consumed_event_keys(),
                config=self.portfolio_policy_config,
                sectors=sectors,
            )
            self.ledger.bind_policy_session_context(
                session,
                binding_kind="execution",
                epoch_id=epoch_id,
                policy_version=self.portfolio_policy_config.version,
                policy_config=portfolio_policy_config_document(
                    self.portfolio_policy_config
                ),
                context=portfolio_risk_context_document(portfolio_context),
                bound_at=processed_at,
            )
        self._execute_intents(
            session,
            {"buy", "short"},
            bridge,
            bars,
            opening_prices,
            borrow_rates,
            processed_at,
            market_validated_at,
            portfolio_context=portfolio_context,
        )

    @staticmethod
    def _policy_pending_with_execution_prices(
        session: date,
        pending: tuple[dict[str, object], ...],
        opening_prices: Mapping[str, Decimal],
    ) -> tuple[dict[str, object], ...]:
        """Use bound raw opens for due reservations; future entries retain D-close."""
        priced: list[dict[str, object]] = []
        for row in pending:
            item = dict(row)
            if item["eligible_session"] == session:
                ticker = str(item["ticker"])
                opening_price = opening_prices.get(ticker)
                if (
                    not isinstance(opening_price, Decimal)
                    or not opening_price.is_finite()
                    or opening_price <= 0
                ):
                    raise LedgerConflictError(
                        f"missing bound opening price for due policy entry {ticker}"
                    )
                item["marked_value"] = Decimal(int(item["quantity"])) * opening_price
            priced.append(item)
        return tuple(priced)

    def _current_intent_policy_context(
        self,
        session: date,
        intent_id: str,
        opening_prices: Mapping[str, Decimal],
        borrow_rates: Mapping[str, Decimal | None],
        baseline: PortfolioRiskContext,
    ) -> PortfolioRiskContext:
        """Rebuild the prospective view after each prior fill/rejection."""
        if self.portfolio_policy_config is None:
            raise LedgerConflictError("portfolio policy config is unavailable")
        current = self.ledger.policy_open_lot_projection(session)
        pending = self._policy_pending_with_execution_prices(
            session,
            self.ledger.policy_pending_entry_projection(
                exclude_intent_id=intent_id
            ),
            opening_prices,
        )
        account = self.ledger.account_state()
        sectors = dict(baseline.sectors)
        sectors.update(
            {
                str(row["ticker"]): str(row.get("sector", "Unknown"))
                for row in current + pending
            }
        )
        borrow_available = {
            str(row["ticker"]): borrow_rates.get(str(row["ticker"])) is not None
            for row in pending
            if str(row.get("direction")) == "short"
        }
        return build_portfolio_risk_context(
            portfolio_value=float(account.net_equity),
            cash=float(account.cash),
            current_positions=current,
            pending_positions=pending,
            price_cache={},
            earnings_dates=baseline.earnings_dates,
            short_interest=baseline.short_interest,
            borrow_available=borrow_available,
            margin_used=float(account.margin_used),
            consumed_event_keys=self.ledger.consumed_event_keys(),
            config=self.portfolio_policy_config,
            sectors=sectors,
        )

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

        executed, value, post_state_digest = self.ledger.run_session_phase(
            session,
            phase,
            processed_at,
            atomic_operation,
            self._expected_state_digest,
        )
        self._expected_state_digest = post_state_digest
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
        *,
        portfolio_context: PortfolioRiskContext | None = None,
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
            recommendation = None
            intent_context = portfolio_context
            if self.policy_enabled and intent.side in {"buy", "short"}:
                if portfolio_context is None:
                    raise LedgerConflictError(
                        f"missing execution policy context for {intent.intent_id}"
                    )
                provenance = self.ledger.read_intent_policy_provenance(
                    intent.intent_id
                )
                if provenance is None:
                    raise LedgerConflictError(
                        f"missing intent policy provenance {intent.intent_id}"
                    )
                if (
                    self.portfolio_policy_config is None
                    or provenance["policy_version"]
                    != self.portfolio_policy_config.version
                ):
                    raise LedgerConflictError(
                        f"intent policy version mismatch for {intent.intent_id}"
                    )
                direction = "short" if intent.side == "short" else "long"
                intent_context = self._current_intent_policy_context(
                    session,
                    intent.intent_id,
                    opening_prices,
                    borrow_rates,
                    portfolio_context,
                )
                intent_context = replace(
                    intent_context,
                    borrow_available={
                        **dict(intent_context.borrow_available),
                        ticker: borrow_rates.get(ticker) is not None,
                    },
                )
                proposed_value = float(Decimal(intent.requested_qty) * bars[ticker].open)
                recommendation = TradeRecommendation(
                    ticker=ticker,
                    direction=direction,
                    position_size_pct=(
                        proposed_value / intent_context.portfolio_value
                    ),
                    confidence=0.0,
                    rationale="rehydrated immutable intent provenance",
                    contributing_strategies=list(provenance["strategy_tags"]),
                    event_key=str(provenance["event_key"]),
                    source_event_keys=tuple(provenance["source_event_keys"]),
                    strategy_tags=tuple(provenance["strategy_tags"]),
                    risk_tags=tuple(provenance["risk_tags"]),
                    journal_only=bool(provenance["journal_only"]),
                )
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
                    "policy_enabled": self.policy_enabled,
                    "recommendation": recommendation,
                    "portfolio_context": intent_context,
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
        self.validate_shared_action_response(bundle.actions, bundle.tickers, session)
        actions = tuple(
            action for action in bundle.actions if action.ticker in set(required)
        )
        self._validate_actions(
            actions,
            required,
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
        bars = {ticker: bundle.bars[(ticker, session)] for ticker in required}
        benchmarks = {
            symbol: bundle.benchmarks[(symbol, session)]
            for symbol in self.benchmark_symbols
        }
        return bars, actions, benchmarks

    @staticmethod
    def validate_shared_action_response(
        actions: tuple[CorporateAction, ...],
        shared_tickers: tuple[str, ...],
        session: date,
    ) -> None:
        """Validate only response-wide identity and scope invariants."""
        seen: dict[str, CorporateAction] = {}
        errors: list[str] = []
        allowed = set(shared_tickers)
        for action in actions:
            if action.action_id in seen and seen[action.action_id] != action:
                errors.append(f"conflicting corporate action {action.action_id}")
            seen[action.action_id] = action
            if action.ticker not in allowed or action.session != session:
                errors.append(f"corporate action scope mismatch {action.action_id}")
        if errors:
            raise CorporateActionBatchError(actions, tuple(sorted(set(errors))))

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
