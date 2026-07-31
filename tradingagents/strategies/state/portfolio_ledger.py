"""Transactional, per-cohort SQLite persistence for the paper ledger.

All economic mutations in this module are durable SQLite transactions.  The
ledger deliberately derives instruments from persisted signal provenance: an
order or fill never carries an independently asserted ticker.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable, Iterator, Mapping, TypeVar

from tradingagents.strategies.execution import (
    AccountSnapshot,
    AccountState,
    BenchmarkObservation,
    CorporateAction,
    Fill,
    LedgerEvent,
    MarketBar,
    OrderIntent,
    SignalRecord,
    stable_id,
)
from tradingagents.strategies.execution.cost_model import (
    PaperCostModel,
    quantize_cash,
    validate_new_short_borrow_rate,
)
from tradingagents.strategies.execution.price_source import (
    BarValidationError,
    validate_required_bars,
)
from tradingagents.strategies.orchestration.trading_calendar import session_close


SCHEMA_VERSION = 1
_OPENING_AT = datetime(1970, 1, 1)
_T = TypeVar("_T")


class LedgerConflictError(ValueError):
    """A stable ledger identity was reused with different content."""


class MissingMarkError(ValueError):
    """An authoritative valuation was attempted without every required mark."""


@dataclass(frozen=True)
class TradeProjectionRecord:
    """Typed, read-only lot/fill view for compatibility projections."""

    trade_id: str
    signal_ids: tuple[str, ...]
    intent_id: str
    execution_id: str
    exit_fill_ids: tuple[str, ...]
    strategy: str
    strategies: tuple[str, ...]
    ticker: str
    direction: str
    entry_session: date
    entry_price: Decimal
    shares: int
    original_shares: int
    closed_shares: int
    open_shares: int
    status: str
    exit_session: date | None
    exit_price: Decimal | None
    realized_pnl: Decimal
    slippage_cost: Decimal
    commission_cost: Decimal
    other_fees: Decimal


# All schema statements are complete so initialization is deterministic and
# reviewable.  Decimals and timestamps are stored as canonical text.
_DDL: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS schema_metadata (
        metadata_key TEXT PRIMARY KEY, metadata_value TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS metric_epochs (
        epoch_id TEXT PRIMARY KEY, generation_id TEXT NOT NULL,
        schema_version INTEGER NOT NULL, status TEXT NOT NULL,
        start_session TEXT NOT NULL, end_session TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS session_runs (
        session_run_id TEXT PRIMARY KEY, cohort_id TEXT NOT NULL,
        session TEXT NOT NULL, valid INTEGER NOT NULL, invalid_reason TEXT NOT NULL,
        started_at TEXT, completed_at TEXT,
        UNIQUE(cohort_id, session)
    )""",
    """CREATE TABLE IF NOT EXISTS session_phases (
        session_phase_id TEXT PRIMARY KEY, cohort_id TEXT NOT NULL,
        session TEXT NOT NULL, phase TEXT NOT NULL, started_at TEXT,
        completed_at TEXT, pre_state_digest TEXT, post_state_digest TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS session_execution_contexts (
        execution_context_id TEXT PRIMARY KEY, cohort_id TEXT NOT NULL,
        session TEXT NOT NULL, epoch_id TEXT NOT NULL,
        input_digest TEXT NOT NULL, market_digest TEXT NOT NULL,
        config_digest TEXT NOT NULL,
        borrow_digest TEXT NOT NULL, required_tickers_json TEXT NOT NULL,
        economic_inputs_json TEXT NOT NULL, provenance_json TEXT NOT NULL,
        provenance_digest TEXT NOT NULL,
        bound_at TEXT NOT NULL, starting_state_digest TEXT NOT NULL DEFAULT '',
        borrow_inputs_json TEXT NOT NULL DEFAULT '{}', UNIQUE(cohort_id, session)
    )""",
    """CREATE TABLE IF NOT EXISTS staging_runs (
        staging_run_id TEXT PRIMARY KEY, cohort_id TEXT NOT NULL,
        session TEXT NOT NULL, epoch_id TEXT NOT NULL, policy_id TEXT NOT NULL,
        completed_at TEXT NOT NULL, post_state_digest TEXT NOT NULL,
        UNIQUE(cohort_id, session, epoch_id, policy_id)
    )""",
    """CREATE TABLE IF NOT EXISTS signals (
        signal_id TEXT PRIMARY KEY, epoch_id TEXT NOT NULL, policy_id TEXT NOT NULL,
        event_key TEXT NOT NULL, strategy TEXT NOT NULL, ticker TEXT NOT NULL,
        direction TEXT NOT NULL, event_at TEXT, observed_at TEXT NOT NULL,
        reference_session TEXT NOT NULL, reference_close TEXT NOT NULL,
        decision_at TEXT NOT NULL, evidence_hash TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS signal_journal_outbox (
        signal_id TEXT PRIMARY KEY REFERENCES signals(signal_id),
        payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL,
        state TEXT NOT NULL, journal_offset INTEGER, journal_length INTEGER,
        queued_at TEXT NOT NULL, mirrored_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS signal_candidate_contexts (
        signal_id TEXT PRIMARY KEY REFERENCES signals(signal_id),
        payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL,
        captured_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS signal_journal_projection (
        cohort_id TEXT PRIMARY KEY, verified_offset INTEGER NOT NULL,
        initialized_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS order_intents (
        intent_id TEXT PRIMARY KEY, cohort_id TEXT NOT NULL, side TEXT NOT NULL,
        requested_qty INTEGER NOT NULL, created_at TEXT NOT NULL,
        eligible_session TEXT NOT NULL, price_rule TEXT NOT NULL,
        status TEXT NOT NULL, stop_price TEXT, external_order_id TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS intent_signals (
        intent_id TEXT NOT NULL REFERENCES order_intents(intent_id),
        signal_id TEXT NOT NULL REFERENCES signals(signal_id),
        signal_order INTEGER NOT NULL,
        PRIMARY KEY(intent_id, signal_id),
        UNIQUE(intent_id, signal_order)
    )""",
    """CREATE TABLE IF NOT EXISTS exit_intent_lots (
        intent_id TEXT NOT NULL REFERENCES order_intents(intent_id),
        lot_id TEXT NOT NULL REFERENCES lots(lot_id), quantity INTEGER NOT NULL,
        PRIMARY KEY(intent_id, lot_id), UNIQUE(lot_id, intent_id)
    )""",
    """CREATE TABLE IF NOT EXISTS order_status_transitions (
        transition_id TEXT PRIMARY KEY,
        intent_id TEXT NOT NULL REFERENCES order_intents(intent_id),
        status TEXT NOT NULL, occurred_at TEXT NOT NULL, reason TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS external_orders (
        external_order_id TEXT PRIMARY KEY,
        intent_id TEXT NOT NULL REFERENCES order_intents(intent_id),
        broker TEXT NOT NULL, status TEXT NOT NULL, submitted_at TEXT NOT NULL,
        reconciled_at TEXT, detail TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS fills (
        fill_id TEXT PRIMARY KEY, intent_id TEXT NOT NULL REFERENCES order_intents(intent_id),
        side TEXT NOT NULL, session TEXT NOT NULL, effective_at TEXT NOT NULL,
        processed_at TEXT NOT NULL, reference_price TEXT NOT NULL,
        fill_price TEXT NOT NULL, quantity INTEGER NOT NULL, slippage TEXT NOT NULL,
        commission TEXT NOT NULL, other_fees TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS fill_costs (
        fill_cost_id TEXT PRIMARY KEY, fill_id TEXT NOT NULL REFERENCES fills(fill_id),
        cost_type TEXT NOT NULL, amount TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS lots (
        lot_id TEXT PRIMARY KEY, fill_id TEXT NOT NULL REFERENCES fills(fill_id),
        cohort_id TEXT NOT NULL, ticker TEXT NOT NULL, direction TEXT NOT NULL,
        opened_session TEXT NOT NULL, entry_price TEXT NOT NULL,
        original_qty INTEGER NOT NULL, open_qty INTEGER NOT NULL,
        margin_reserved TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS lot_closures (
        closure_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL REFERENCES lots(lot_id),
        fill_id TEXT NOT NULL REFERENCES fills(fill_id), quantity INTEGER NOT NULL,
        realized_pnl TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS corporate_actions (
        action_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, session TEXT NOT NULL,
        action_type TEXT NOT NULL, ratio TEXT, cash_per_share TEXT,
        source TEXT NOT NULL, fetched_at TEXT NOT NULL, verified INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS lot_action_applications (
        application_id TEXT PRIMARY KEY,
        action_id TEXT NOT NULL REFERENCES corporate_actions(action_id),
        lot_id TEXT NOT NULL REFERENCES lots(lot_id), applied_at TEXT NOT NULL,
        detail TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS intent_action_adjustments (
        adjustment_id TEXT PRIMARY KEY,
        action_id TEXT NOT NULL REFERENCES corporate_actions(action_id),
        intent_id TEXT NOT NULL REFERENCES order_intents(intent_id),
        adjustment_sequence INTEGER NOT NULL,
        original_qty INTEGER NOT NULL, adjusted_qty INTEGER NOT NULL,
        original_stop_price TEXT, adjusted_stop_price TEXT, applied_at TEXT NOT NULL,
        UNIQUE(action_id, intent_id)
    )""",
    """CREATE TABLE IF NOT EXISTS session_invalidations (
        invalidation_id TEXT PRIMARY KEY, cohort_id TEXT NOT NULL, session TEXT NOT NULL,
        reason TEXT NOT NULL, invalidated_at TEXT NOT NULL,
        UNIQUE(cohort_id, session, reason)
    )""",
    """CREATE TABLE IF NOT EXISTS ticker_quarantines (
        quarantine_id TEXT PRIMARY KEY, cohort_id TEXT NOT NULL, ticker TEXT NOT NULL,
        reason TEXT NOT NULL, quarantined_at TEXT NOT NULL,
        UNIQUE(cohort_id, ticker, reason)
    )""",
    """CREATE TABLE IF NOT EXISTS corporate_action_conflicts (
        conflict_id TEXT PRIMARY KEY, cohort_id TEXT NOT NULL, session TEXT NOT NULL,
        ticker TEXT NOT NULL, action_id TEXT NOT NULL, content_hash TEXT NOT NULL,
        attempted_payload TEXT NOT NULL, detected_at TEXT NOT NULL,
        UNIQUE(cohort_id, action_id, content_hash)
    )""",
    """CREATE TABLE IF NOT EXISTS corporate_action_batch_rejections (
        batch_id TEXT PRIMARY KEY, cohort_id TEXT NOT NULL, session TEXT NOT NULL,
        payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL,
        errors_json TEXT NOT NULL, rejected_at TEXT NOT NULL,
        UNIQUE(cohort_id, session, payload_hash)
    )""",
    """CREATE TABLE IF NOT EXISTS cash_events (
        cash_event_id TEXT PRIMARY KEY, cohort_id TEXT NOT NULL, session TEXT,
        event_type TEXT NOT NULL, amount TEXT NOT NULL, effective_at TEXT NOT NULL,
        detail TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS accounting_state (
        cohort_id TEXT PRIMARY KEY, cash TEXT NOT NULL, realized_pnl TEXT NOT NULL,
        slippage_cost TEXT NOT NULL, commission_cost TEXT NOT NULL,
        other_fees TEXT NOT NULL, borrow_cost TEXT NOT NULL,
        financing_cost TEXT NOT NULL, dividend_cash TEXT NOT NULL,
        high_water_mark TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS borrow_accruals (
        accrual_id TEXT PRIMARY KEY, lot_id TEXT REFERENCES lots(lot_id),
        session TEXT NOT NULL, amount TEXT NOT NULL, annual_rate TEXT NOT NULL,
        flagged INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS financing_accruals (
        accrual_id TEXT PRIMARY KEY, session TEXT NOT NULL, amount TEXT NOT NULL,
        annual_rate TEXT NOT NULL, flagged INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS dividend_events (
        dividend_event_id TEXT PRIMARY KEY,
        action_id TEXT REFERENCES corporate_actions(action_id), lot_id TEXT REFERENCES lots(lot_id),
        session TEXT NOT NULL, amount TEXT NOT NULL, direction TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS fee_events (
        fee_event_id TEXT PRIMARY KEY, fill_id TEXT REFERENCES fills(fill_id),
        session TEXT NOT NULL, fee_type TEXT NOT NULL, amount TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS marks (
        mark_id TEXT PRIMARY KEY, cohort_id TEXT NOT NULL, ticker TEXT NOT NULL,
        session TEXT NOT NULL, close TEXT NOT NULL, source TEXT NOT NULL,
        observed_at TEXT NOT NULL, adjusted INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS account_snapshots (
        snapshot_id TEXT PRIMARY KEY, cohort_id TEXT NOT NULL, epoch_id TEXT NOT NULL,
        session TEXT NOT NULL, valuation_at TEXT NOT NULL, cash TEXT NOT NULL,
        long_market_value TEXT NOT NULL, short_liability TEXT NOT NULL,
        gross_exposure TEXT NOT NULL, net_exposure TEXT NOT NULL,
        margin_used TEXT NOT NULL, buying_power TEXT NOT NULL, realized_pnl TEXT NOT NULL,
        unrealized_pnl TEXT NOT NULL, gross_equity TEXT NOT NULL,
        slippage_cost TEXT NOT NULL, commission_cost TEXT NOT NULL,
        other_fees TEXT NOT NULL, borrow_cost TEXT NOT NULL, financing_cost TEXT NOT NULL,
        dividend_cash TEXT NOT NULL, net_equity TEXT NOT NULL, high_water_mark TEXT NOT NULL,
        valid INTEGER NOT NULL, invalid_reason TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS benchmark_observations (
        observation_id TEXT PRIMARY KEY, cohort_id TEXT NOT NULL, epoch_id TEXT NOT NULL,
        session TEXT NOT NULL, symbol TEXT NOT NULL, close TEXT NOT NULL,
        return_basis TEXT NOT NULL, source TEXT NOT NULL, observed_at TEXT NOT NULL,
        valid INTEGER NOT NULL, invalid_reason TEXT NOT NULL
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS ux_snapshots_cohort_session
        ON account_snapshots(cohort_id, session)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS ux_benchmarks_epoch_session_symbol
        ON benchmark_observations(cohort_id, epoch_id, session, symbol)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS ux_fills_intent_session
        ON fills(intent_id, session)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS ux_session_phases
        ON session_phases(cohort_id, session, phase)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS ux_action_lot_application
        ON lot_action_applications(action_id, lot_id)""",
    """CREATE INDEX IF NOT EXISTS ix_lots_cohort_ticker_open
        ON lots(cohort_id, ticker, opened_session, lot_id) WHERE open_qty > 0""",
    """CREATE UNIQUE INDEX IF NOT EXISTS ux_marks_cohort_ticker_session
        ON marks(cohort_id, ticker, session)""",
    """CREATE INDEX IF NOT EXISTS ix_lots_cohort_ticker_direction_open
        ON lots(cohort_id, ticker, direction, opened_session, lot_id)
        WHERE open_qty > 0""",
    """CREATE INDEX IF NOT EXISTS ix_cash_events_cohort_event
        ON cash_events(cohort_id, event_type, session)""",
)

_IDENTITY_COLUMNS = {
    "schema_metadata": "metadata_key",
    "signals": "signal_id",
    "order_intents": "intent_id",
    "cash_events": "cash_event_id",
}
_ALLOWED_INSERT_COLUMNS = {
    "schema_metadata": frozenset({"metadata_key", "metadata_value"}),
    "signals": frozenset(
        {
            "signal_id",
            "epoch_id",
            "policy_id",
            "event_key",
            "strategy",
            "ticker",
            "direction",
            "event_at",
            "observed_at",
            "reference_session",
            "reference_close",
            "decision_at",
            "evidence_hash",
        }
    ),
    "order_intents": frozenset(
        {
            "intent_id",
            "cohort_id",
            "side",
            "requested_qty",
            "created_at",
            "eligible_session",
            "price_rule",
            "status",
            "stop_price",
            "external_order_id",
        }
    ),
    "cash_events": frozenset(
        {
            "cash_event_id",
            "cohort_id",
            "session",
            "event_type",
            "amount",
            "effective_at",
            "detail",
        }
    ),
}


def _decimal(value: str | Decimal) -> Decimal:
    return Decimal(value)


def _date(value: str | date) -> date:
    return (
        value
        if isinstance(value, date) and not isinstance(value, datetime)
        else date.fromisoformat(value)
    )


def _datetime(value: str | datetime) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)


class PortfolioLedger:
    """One bounded SQLite connection for one authoritative cohort ledger."""

    def __init__(
        self,
        path: Path,
        cohort_id: str,
        initial_cash: Decimal,
        *,
        paper_ledger_config: Mapping[str, object] | None = None,
        short_selling_config: Mapping[str, object] | None = None,
    ) -> None:
        if not isinstance(initial_cash, Decimal):
            raise TypeError("initial_cash must be Decimal")
        self.path = Path(path)
        self.cohort_id = cohort_id
        self._cost_model = PaperCostModel(paper_ledger_config)
        short_values = short_selling_config or {}
        self._borrow_cost_reject_above = self._configured_decimal(
            short_values, "borrow_cost_reject_above", "0.05"
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, isolation_level=None)
        try:
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._initialize(initial_cash)
        except BaseException:
            self._connection.close()
            raise

    @property
    def connection(self) -> sqlite3.Connection:
        """Expose the connection for bounded administrative inspection/tests."""
        return self._connection

    @classmethod
    def open_existing(cls, path: Path) -> PortfolioLedger:
        """Open an initialized ledger without running any write initializer."""
        path = Path(path)
        if not os.path.lexists(path):
            raise FileNotFoundError(path)
        connection = sqlite3.connect(
            f"file:{path}?mode=ro", uri=True, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only=ON")
            cohort_row = connection.execute(
                "SELECT metadata_value FROM schema_metadata WHERE metadata_key = 'cohort_id'"
            ).fetchone()
            if cohort_row is None:
                raise ValueError(f"not an initialized PortfolioLedger: {path}")
            cohort_id = str(cohort_row["metadata_value"])
            cash_row = connection.execute(
                """SELECT amount FROM cash_events
                   WHERE cohort_id = ? AND event_type = 'opening'
                   ORDER BY cash_event_id LIMIT 1""",
                (cohort_id,),
            ).fetchone()
            if cash_row is None:
                raise ValueError(f"ledger has no opening cash event: {path}")
            Decimal(str(cash_row["amount"]))
        except BaseException:
            connection.close()
            raise

        ledger = cls.__new__(cls)
        ledger.path = path
        ledger.cohort_id = cohort_id
        ledger._connection = connection
        return ledger

    def close(self) -> None:
        self._connection.close()

    def _initialize(self, initial_cash: Decimal) -> None:
        with self.transaction():
            for statement in _DDL:
                self._connection.execute(statement)
            columns = {
                row["name"]
                for row in self._connection.execute(
                    "PRAGMA table_info(intent_action_adjustments)"
                )
            }
            if "adjustment_sequence" not in columns:
                self._connection.execute(
                    "ALTER TABLE intent_action_adjustments "
                    "ADD COLUMN adjustment_sequence INTEGER NOT NULL DEFAULT 0"
                )
            context_columns = {
                row["name"]
                for row in self._connection.execute(
                    "PRAGMA table_info(session_execution_contexts)"
                )
            }
            if "market_digest" not in context_columns:
                self._connection.execute(
                    "ALTER TABLE session_execution_contexts "
                    "ADD COLUMN market_digest TEXT NOT NULL DEFAULT ''"
                )
                rows = self._connection.execute(
                    "SELECT execution_context_id, input_digest, economic_inputs_json "
                    "FROM session_execution_contexts"
                ).fetchall()
                for row in rows:
                    economic_inputs = json.loads(row["economic_inputs_json"])
                    self._connection.execute(
                        """UPDATE session_execution_contexts
                           SET market_digest = ?, input_digest = ?
                           WHERE execution_context_id = ?""",
                        (
                            row["input_digest"],
                            stable_id("session_economic_inputs", economic_inputs),
                            row["execution_context_id"],
                        ),
                    )
            if "provenance_digest" not in context_columns:
                self._connection.execute(
                    "ALTER TABLE session_execution_contexts "
                    "ADD COLUMN provenance_digest TEXT NOT NULL DEFAULT ''"
                )
                rows = self._connection.execute(
                    "SELECT execution_context_id, provenance_json "
                    "FROM session_execution_contexts"
                ).fetchall()
                for row in rows:
                    provenance = json.loads(row["provenance_json"])
                    self._connection.execute(
                        """UPDATE session_execution_contexts
                           SET provenance_digest = ? WHERE execution_context_id = ?""",
                        (
                            stable_id("session_input_provenance", provenance),
                            row["execution_context_id"],
                        ),
                    )
            if "starting_state_digest" not in context_columns:
                self._connection.execute(
                    "ALTER TABLE session_execution_contexts "
                    "ADD COLUMN starting_state_digest TEXT NOT NULL DEFAULT ''"
                )
            if "borrow_inputs_json" not in context_columns:
                self._connection.execute(
                    "ALTER TABLE session_execution_contexts "
                    "ADD COLUMN borrow_inputs_json TEXT NOT NULL DEFAULT '{}'"
                )
            phase_columns = {
                row["name"]
                for row in self._connection.execute("PRAGMA table_info(session_phases)")
            }
            if "pre_state_digest" not in phase_columns:
                self._connection.execute(
                    "ALTER TABLE session_phases ADD COLUMN pre_state_digest TEXT"
                )
            if "post_state_digest" not in phase_columns:
                self._connection.execute(
                    "ALTER TABLE session_phases ADD COLUMN post_state_digest TEXT"
                )
            staging_columns = {
                row["name"]
                for row in self._connection.execute("PRAGMA table_info(staging_runs)")
            }
            if "post_state_digest" not in staging_columns:
                self._connection.execute(
                    "ALTER TABLE staging_runs "
                    "ADD COLUMN post_state_digest TEXT NOT NULL DEFAULT ''"
                )
            self._backfill_adjustment_sequences()
            self._connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_intent_adjustment_sequence "
                "ON intent_action_adjustments(intent_id, adjustment_sequence)"
            )
            self._insert_idempotent(
                "schema_metadata",
                "metadata_key",
                "schema_version",
                {
                    "metadata_key": "schema_version",
                    "metadata_value": str(SCHEMA_VERSION),
                },
            )
            self._insert_idempotent(
                "schema_metadata",
                "metadata_key",
                "cohort_id",
                {"metadata_key": "cohort_id", "metadata_value": self.cohort_id},
            )
            self._insert_idempotent(
                "cash_events",
                "cash_event_id",
                stable_id("cash", self.cohort_id, "opening"),
                {
                    "cash_event_id": stable_id("cash", self.cohort_id, "opening"),
                    "cohort_id": self.cohort_id,
                    "session": None,
                    "event_type": "opening",
                    "amount": initial_cash,
                    "effective_at": _OPENING_AT,
                    "detail": "deterministic opening cash",
                },
            )
            self._initialize_accounting_state()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        if self._connection.in_transaction:
            yield self._connection
            return
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield self._connection
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        else:
            self._connection.execute("COMMIT")

    def phase_completed(self, session: date, phase: str) -> bool:
        """Whether a session phase was durably committed."""
        row = self._connection.execute(
            """SELECT completed_at FROM session_phases
               WHERE cohort_id = ? AND session = ? AND phase = ?""",
            (self.cohort_id, self._encode(session), phase),
        ).fetchone()
        return row is not None and row["completed_at"] is not None

    def session_execution_context(self, session: date) -> dict[str, object] | None:
        """Return the immutable epoch/input binding for a started session."""
        row = self._connection.execute(
            """SELECT * FROM session_execution_contexts
               WHERE cohort_id = ? AND session = ?""",
            (self.cohort_id, self._encode(session)),
        ).fetchone()
        if row is None:
            return None
        economic_inputs = json.loads(row["economic_inputs_json"])
        expected_digest = stable_id("session_economic_inputs", economic_inputs)
        expected_market_digest = stable_id(
            "session_market_inputs", economic_inputs.get("market", {})
        )
        provenance = json.loads(row["provenance_json"])
        expected_provenance_digest = stable_id("session_input_provenance", provenance)
        if (
            row["input_digest"] != expected_digest
            or row["market_digest"] != expected_market_digest
            or row["provenance_digest"] != expected_provenance_digest
        ):
            raise LedgerConflictError(
                f"execution context economic payload conflict for {session}"
            )
        borrow_inputs = json.loads(row["borrow_inputs_json"])
        if row["borrow_digest"] != stable_id("session_borrow_inputs", borrow_inputs):
            raise LedgerConflictError(
                f"execution context borrow payload conflict for {session}"
            )
        starting_state = economic_inputs.get("starting_state")
        if row["starting_state_digest"] != stable_id(
            "session_governed_state", starting_state
        ):
            raise LedgerConflictError(
                f"execution context starting-state conflict for {session}"
            )
        return {
            "epoch_id": row["epoch_id"],
            "input_digest": row["input_digest"],
            "market_digest": row["market_digest"],
            "config_digest": row["config_digest"],
            "borrow_digest": row["borrow_digest"],
            "required_tickers": tuple(json.loads(row["required_tickers_json"])),
            "economic_inputs_json": row["economic_inputs_json"],
            "provenance_json": row["provenance_json"],
            "provenance_digest": row["provenance_digest"],
            "starting_state_digest": row["starting_state_digest"],
            "borrow_inputs_json": row["borrow_inputs_json"],
            "bound_at": _datetime(row["bound_at"]),
        }

    def execution_starting_state(self, session: date) -> dict[str, object]:
        """Canonical bounded state governed by the execution phase machine."""
        account = self.account_state()
        due_intents = []
        intent_rows = self._due_intent_rows(session)
        for row in intent_rows:
            intent = self._intent_from_row(row)
            signals = self.signals_for_intent(intent.intent_id)
            due_intents.append(
                {
                    "intent": intent.__dict__,
                    "signals": [signal.__dict__ for signal in signals],
                }
            )
        allocations = [
            allocation
            for intent_row in intent_rows
            for allocation in self._connection.execute(
                """SELECT intent_id, lot_id, quantity FROM exit_intent_lots
                   WHERE intent_id = ? ORDER BY lot_id""",
                (intent_row["intent_id"],),
            ).fetchall()
        ]

        def rows(sql: str, parameters: tuple[object, ...]) -> list[dict[str, object]]:
            return [dict(row) for row in self._connection.execute(sql, parameters)]

        return {
            "account": account.__dict__,
            "due_intents": due_intents,
            "open_lots": self.open_exit_positions(),
            "exit_allocations": [dict(row) for row in allocations],
            "session_cash_events": rows(
                "SELECT * FROM cash_events WHERE cohort_id = ? AND session = ? "
                "ORDER BY cash_event_id",
                (self.cohort_id, self._encode(session)),
            ),
            "session_actions": rows(
                "SELECT * FROM corporate_actions WHERE session = ? ORDER BY action_id",
                (self._encode(session),),
            ),
            "session_marks": rows(
                "SELECT * FROM marks WHERE cohort_id = ? AND session = ? ORDER BY mark_id",
                (self.cohort_id, self._encode(session)),
            ),
            "session_benchmarks": rows(
                "SELECT * FROM benchmark_observations WHERE cohort_id = ? AND session = ? "
                "ORDER BY observation_id",
                (self.cohort_id, self._encode(session)),
            ),
            "session_snapshots": rows(
                "SELECT * FROM account_snapshots WHERE cohort_id = ? AND session = ? "
                "ORDER BY snapshot_id",
                (self.cohort_id, self._encode(session)),
            ),
        }

    def execution_governed_state_digest(self, session: date) -> str:
        """Hash the exact live state allowed to advance only inside a phase."""
        return stable_id(
            "session_governed_state", self.execution_starting_state(session)
        )

    def bind_session_execution_context(
        self,
        session: date,
        epoch_id: str,
        input_digest: str,
        market_digest: str,
        config_digest: str,
        borrow_digest: str,
        required_tickers: tuple[str, ...],
        economic_inputs_json: str,
        provenance_json: str,
        bound_at: datetime,
        starting_state_digest: str,
        borrow_inputs_json: str,
    ) -> None:
        """Bind all phase commits to one epoch and one economic input digest."""
        self._require_timezone_aware(bound_at, "bound_at")
        if (
            not epoch_id
            or not input_digest
            or not market_digest
            or not config_digest
            or not borrow_digest
            or not starting_state_digest
        ):
            raise ValueError("epoch and execution-context digests are required")
        tickers_json = json.dumps(list(required_tickers), separators=(",", ":"))
        provenance = json.loads(provenance_json)
        provenance_digest = stable_id("session_input_provenance", provenance)
        with self.transaction():
            row = self._connection.execute(
                """SELECT * FROM session_execution_contexts
                   WHERE cohort_id = ? AND session = ?""",
                (self.cohort_id, self._encode(session)),
            ).fetchone()
            if row is not None:
                if row["epoch_id"] != epoch_id:
                    raise LedgerConflictError(
                        f"execution context epoch conflict for {session}: "
                        f"{row['epoch_id']} != {epoch_id}"
                    )
                if (
                    row["input_digest"] != input_digest
                    or row["market_digest"] != market_digest
                    or row["config_digest"] != config_digest
                    or row["borrow_digest"] != borrow_digest
                    or row["required_tickers_json"] != tickers_json
                    or row["starting_state_digest"] != starting_state_digest
                    or row["borrow_inputs_json"] != borrow_inputs_json
                ):
                    raise LedgerConflictError(
                        f"execution context input conflict for {session}"
                    )
                return
            self._connection.execute(
                """INSERT INTO session_execution_contexts
                   (execution_context_id, cohort_id, session, epoch_id,
                    input_digest, market_digest, config_digest, borrow_digest,
                    required_tickers_json, economic_inputs_json, provenance_json,
                    provenance_digest, bound_at, starting_state_digest,
                    borrow_inputs_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    stable_id("execution_context", self.cohort_id, session),
                    self.cohort_id,
                    self._encode(session),
                    epoch_id,
                    input_digest,
                    market_digest,
                    config_digest,
                    borrow_digest,
                    tickers_json,
                    economic_inputs_json,
                    provenance_json,
                    provenance_digest,
                    self._encode(bound_at),
                    starting_state_digest,
                    borrow_inputs_json,
                ),
            )

    def run_session_phase(
        self,
        session: date,
        phase: str,
        processed_at: datetime,
        operation: Callable[[], _T],
        expected_pre_state_digest: str,
    ) -> tuple[bool, _T | None, str]:
        """Run one phase and its completion marker in one outer transaction."""
        self._require_timezone_aware(processed_at, "processed_at")
        if not phase:
            raise ValueError("phase is required")
        with self.transaction():
            row = self._connection.execute(
                """SELECT completed_at, pre_state_digest, post_state_digest
                   FROM session_phases
                   WHERE cohort_id = ? AND session = ? AND phase = ?""",
                (self.cohort_id, self._encode(session), phase),
            ).fetchone()
            if row is not None and row["completed_at"] is not None:
                if row["post_state_digest"] is None:
                    raise LedgerConflictError(
                        f"completed phase {phase} has no governed state commitment"
                    )
                return False, None, str(row["post_state_digest"])
            actual_pre_state_digest = self.execution_governed_state_digest(session)
            if actual_pre_state_digest != expected_pre_state_digest:
                raise LedgerConflictError(
                    f"governed session state conflict before {phase}"
                )
            phase_id = stable_id("session_phase", self.cohort_id, session, phase)
            if row is None:
                self._connection.execute(
                    """INSERT INTO session_phases
                       (session_phase_id, cohort_id, session, phase, started_at,
                        completed_at, pre_state_digest, post_state_digest)
                       VALUES (?, ?, ?, ?, ?, NULL, ?, NULL)""",
                    (
                        phase_id,
                        self.cohort_id,
                        self._encode(session),
                        phase,
                        self._encode(processed_at),
                        expected_pre_state_digest,
                    ),
                )
            else:
                self._connection.execute(
                    """UPDATE session_phases
                       SET started_at = ?, pre_state_digest = ?
                       WHERE session_phase_id = ?""",
                    (self._encode(processed_at), expected_pre_state_digest, phase_id),
                )
            value = operation()
            post_state_digest = self.execution_governed_state_digest(session)
            self._connection.execute(
                """UPDATE session_phases
                   SET completed_at = ?, post_state_digest = ?
                   WHERE session_phase_id = ?""",
                (self._encode(processed_at), post_state_digest, phase_id),
            )
            return True, value, post_state_digest

    def verify_session_phase_chain(self, session: date, phases: tuple[str, ...]) -> str:
        """Verify committed phase transitions and the exact current boundary."""
        context = self.session_execution_context(session)
        if context is None:
            raise LedgerConflictError(f"missing execution context for {session}")
        expected = str(context["starting_state_digest"])
        saw_incomplete = False
        for phase in phases:
            row = self._connection.execute(
                """SELECT completed_at, pre_state_digest, post_state_digest
                   FROM session_phases WHERE cohort_id = ? AND session = ? AND phase = ?""",
                (self.cohort_id, self._encode(session), phase),
            ).fetchone()
            if row is None or row["completed_at"] is None:
                saw_incomplete = True
                continue
            if saw_incomplete:
                raise LedgerConflictError(
                    f"non-prefix completed execution phase {phase}"
                )
            if row["pre_state_digest"] != expected or not row["post_state_digest"]:
                raise LedgerConflictError(
                    f"governed state commitment conflict for phase {phase}"
                )
            expected = str(row["post_state_digest"])
        staging_rows = self._connection.execute(
            """SELECT post_state_digest FROM staging_runs
               WHERE cohort_id = ? AND session = ? ORDER BY staging_run_id""",
            (self.cohort_id, self._encode(session)),
        ).fetchall()
        if len(staging_rows) > 1:
            raise LedgerConflictError("multiple staging boundary commitments")
        if staging_rows:
            if not staging_rows[0]["post_state_digest"]:
                raise LedgerConflictError("staging boundary commitment is missing")
            expected = str(staging_rows[0]["post_state_digest"])
        if self.execution_governed_state_digest(session) != expected:
            raise LedgerConflictError(
                "governed session state conflict at resume boundary"
            )
        return expected

    def staging_completed(self, session: date, epoch_id: str, policy_id: str) -> bool:
        row = self._connection.execute(
            """SELECT 1 FROM staging_runs WHERE cohort_id = ? AND session = ?
               AND epoch_id = ? AND policy_id = ?""",
            (self.cohort_id, self._encode(session), epoch_id, policy_id),
        ).fetchone()
        return row is not None

    def complete_staging(
        self,
        session: date,
        epoch_id: str,
        policy_id: str,
        completed_at: datetime,
        operation: Callable[[], _T],
        expected_pre_state_digest: str,
    ) -> tuple[bool, _T | None]:
        """Atomically persist every staged intent and the staging completion."""
        self._require_timezone_aware(completed_at, "completed_at")
        with self.transaction():
            if self.staging_completed(session, epoch_id, policy_id):
                return False, None
            if (
                self.execution_governed_state_digest(session)
                != expected_pre_state_digest
            ):
                raise LedgerConflictError(
                    "governed session state conflict before staging"
                )
            value = operation()
            post_state_digest = self.execution_governed_state_digest(session)
            self._connection.execute(
                "INSERT INTO staging_runs VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    stable_id(
                        "staging_run", self.cohort_id, session, epoch_id, policy_id
                    ),
                    self.cohort_id,
                    self._encode(session),
                    epoch_id,
                    policy_id,
                    self._encode(completed_at),
                    post_state_digest,
                ),
            )
            return True, value

    def _insert_idempotent(
        self,
        table: str,
        identity_column: str,
        identity: str,
        values: dict[str, object],
    ) -> bool:
        if _IDENTITY_COLUMNS.get(table) != identity_column:
            raise ValueError(f"unapproved ledger table or identity column: {table}")
        if (
            set(values) != _ALLOWED_INSERT_COLUMNS[table]
            or values.get(identity_column) != identity
        ):
            raise ValueError(f"unapproved ledger columns for {table}")
        existing = self._connection.execute(
            f"SELECT * FROM {table} WHERE {identity_column} = ?", (identity,)
        ).fetchone()
        encoded = {key: self._encode(value) for key, value in values.items()}
        if existing is not None:
            if any(existing[key] != encoded[key] for key in encoded):
                raise LedgerConflictError(f"conflicting {table} identity {identity}")
            return False
        columns = ", ".join(encoded)
        marks = ", ".join("?" for _ in encoded)
        self._connection.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(encoded.values())
        )
        return True

    def _backfill_adjustment_sequences(self) -> None:
        """Recover each legacy intent's only causal split chain or fail closed."""
        intents = self._connection.execute(
            "SELECT DISTINCT intent_id FROM intent_action_adjustments ORDER BY intent_id"
        ).fetchall()
        for item in intents:
            intent_id = item["intent_id"]
            rows = self._connection.execute(
                "SELECT * FROM intent_action_adjustments WHERE intent_id = ?",
                (intent_id,),
            ).fetchall()
            sequence = [int(row["adjustment_sequence"]) for row in rows]
            if sequence and min(sequence) >= 1 and len(set(sequence)) == len(rows):
                continue

            def endpoint(row: sqlite3.Row, prefix: str) -> tuple[int, str | None]:
                return int(row[f"{prefix}_qty"]), row[f"{prefix}_stop_price"]

            outgoing: dict[tuple[int, str | None], list[sqlite3.Row]] = {}
            targets: set[tuple[int, str | None]] = set()
            for row in rows:
                outgoing.setdefault(endpoint(row, "original"), []).append(row)
                targets.add(endpoint(row, "adjusted"))
            roots = [state for state in outgoing if state not in targets]
            if len(roots) != 1 or any(len(edges) != 1 for edges in outgoing.values()):
                raise LedgerConflictError(
                    f"intent adjustment migration ambiguous chain for {intent_id}"
                )
            ordered: list[sqlite3.Row] = []
            visited: set[str] = set()
            state = roots[0]
            while state in outgoing:
                row = outgoing[state][0]
                if row["adjustment_id"] in visited:
                    raise LedgerConflictError(
                        f"intent adjustment migration cyclic chain for {intent_id}"
                    )
                ordered.append(row)
                visited.add(row["adjustment_id"])
                state = endpoint(row, "adjusted")
            if len(ordered) != len(rows):
                raise LedgerConflictError(
                    f"intent adjustment migration disconnected chain for {intent_id}"
                )
            for value, row in enumerate(ordered, 1):
                self._connection.execute(
                    "UPDATE intent_action_adjustments SET adjustment_sequence = ? "
                    "WHERE adjustment_id = ?",
                    (value, row["adjustment_id"]),
                )

    def _initialize_accounting_state(self) -> None:
        """Create or deterministically migrate the one-row cohort summary."""
        existing = self._connection.execute(
            "SELECT cohort_id FROM accounting_state WHERE cohort_id = ?",
            (self.cohort_id,),
        ).fetchone()
        if existing is not None:
            return
        summary = self._historical_summary_for_migration()
        self._connection.execute(
            """INSERT INTO accounting_state VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                self.cohort_id,
                self._encode(summary["cash"]),
                self._encode(summary["realized_pnl"]),
                self._encode(summary["slippage_cost"]),
                self._encode(summary["commission_cost"]),
                self._encode(summary["other_fees"]),
                self._encode(summary["borrow_cost"]),
                self._encode(summary["financing_cost"]),
                self._encode(summary["dividend_cash"]),
                self._encode(summary["high_water_mark"]),
            ),
        )

    def _historical_summary_for_migration(self) -> dict[str, Decimal]:
        """Read audit detail once only when upgrading a pre-summary ledger."""
        opening = self._connection.execute(
            """SELECT amount FROM cash_events WHERE cohort_id = ?
               AND event_type = 'opening'""",
            (self.cohort_id,),
        ).fetchone()
        if opening is None:
            raise LedgerConflictError("missing deterministic opening cash event")
        cash = self._decimal_column_total(
            "cash_events", "amount", "cohort_id = ?", (self.cohort_id,)
        )
        realized_pnl = self._decimal_column_total("lot_closures", "realized_pnl")
        slippage_cost = self._fill_cost_total_for_migration("slippage")
        commission_cost = self._fill_cost_total_for_migration("commission")
        other_fees = self._fill_cost_total_for_migration("other_fees")
        borrow_cost = self._decimal_column_total("borrow_accruals", "amount")
        financing_cost = self._decimal_column_total("financing_accruals", "amount")
        dividend_cash = self._decimal_column_total("dividend_events", "amount")
        high_water_mark = _decimal(opening["amount"])
        snapshot_rows = self._connection.execute(
            "SELECT high_water_mark FROM account_snapshots WHERE cohort_id = ?",
            (self.cohort_id,),
        ).fetchall()
        for row in snapshot_rows:
            high_water_mark = max(high_water_mark, _decimal(row["high_water_mark"]))
        return {
            "cash": cash,
            "realized_pnl": realized_pnl,
            "slippage_cost": slippage_cost,
            "commission_cost": commission_cost,
            "other_fees": other_fees,
            "borrow_cost": borrow_cost,
            "financing_cost": financing_cost,
            "dividend_cash": dividend_cash,
            "high_water_mark": high_water_mark,
        }

    def _decimal_column_total(
        self,
        table: str,
        column: str,
        where: str = "1 = 1",
        values: tuple[object, ...] = (),
    ) -> Decimal:
        approved = {
            ("cash_events", "amount"),
            ("lot_closures", "realized_pnl"),
            ("borrow_accruals", "amount"),
            ("financing_accruals", "amount"),
            ("dividend_events", "amount"),
        }
        if (table, column) not in approved or where not in {"1 = 1", "cohort_id = ?"}:
            raise ValueError("unapproved accounting migration query")
        rows = self._connection.execute(
            f"SELECT {column} FROM {table} WHERE {where}", values
        ).fetchall()
        return sum((_decimal(row[column]) for row in rows), Decimal("0"))

    def _fill_cost_total_for_migration(self, cost_type: str) -> Decimal:
        if cost_type not in {"slippage", "commission", "other_fees"}:
            raise ValueError("unapproved fill cost type")
        rows = self._connection.execute(
            "SELECT amount FROM fill_costs WHERE cost_type = ?", (cost_type,)
        ).fetchall()
        return sum((_decimal(row["amount"]) for row in rows), Decimal("0"))

    @staticmethod
    def _encode(value: object) -> object:
        if isinstance(value, Decimal):
            return format(value, "f")
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, bool):
            return int(value)
        return value

    @staticmethod
    def _configured_decimal(
        values: Mapping[str, object], key: str, default: str
    ) -> Decimal:
        # PaperCostModel owns common Decimal validation; this threshold is a
        # short-selling policy input rather than a paper-ledger cost field.
        value = PaperCostModel._configured_decimal(values, key, default)
        return value

    def record_signal(self, signal: SignalRecord) -> None:
        with self.transaction():
            self._record_signal(signal)

    def _record_signal(self, signal: SignalRecord) -> None:
        self._insert_idempotent(
            "signals",
            "signal_id",
            signal.signal_id,
            {
                "signal_id": signal.signal_id,
                "epoch_id": signal.epoch_id,
                "policy_id": signal.policy_id,
                "event_key": signal.event_key,
                "strategy": signal.strategy,
                "ticker": signal.ticker,
                "direction": signal.direction,
                "event_at": signal.event_at,
                "observed_at": signal.observed_at,
                "reference_session": signal.reference_session,
                "reference_close": signal.reference_close,
                "decision_at": signal.decision_at,
                "evidence_hash": signal.evidence_hash,
            },
        )

    def record_signal_with_journal(
        self,
        signal: SignalRecord,
        payload: dict[str, object],
        queued_at: datetime,
        candidate_context: dict[str, object] | None = None,
    ) -> None:
        """Atomically persist a signal and its exact immutable journal payload."""
        self._require_timezone_aware(queued_at, "queued_at")
        payload_json = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), default=str
        )
        payload_hash = stable_id("signal_journal_payload", payload_json)
        context_json = json.dumps(
            candidate_context or {},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        context_hash = stable_id("signal_candidate_context", context_json)
        with self.transaction():
            self._record_signal(signal)
            context_row = self._connection.execute(
                "SELECT * FROM signal_candidate_contexts WHERE signal_id = ?",
                (signal.signal_id,),
            ).fetchone()
            if context_row is not None:
                if (
                    context_row["payload_json"] != context_json
                    or context_row["payload_hash"] != context_hash
                ):
                    raise LedgerConflictError(
                        f"conflicting candidate context for signal {signal.signal_id}"
                    )
            else:
                self._connection.execute(
                    "INSERT INTO signal_candidate_contexts VALUES (?, ?, ?, ?)",
                    (
                        signal.signal_id,
                        context_json,
                        context_hash,
                        self._encode(queued_at),
                    ),
                )
            row = self._connection.execute(
                "SELECT * FROM signal_journal_outbox WHERE signal_id = ?",
                (signal.signal_id,),
            ).fetchone()
            if row is not None:
                if (
                    row["payload_json"] != payload_json
                    or row["payload_hash"] != payload_hash
                ):
                    raise LedgerConflictError(
                        f"conflicting journal payload for signal {signal.signal_id}"
                    )
                return
            self._connection.execute(
                """INSERT INTO signal_journal_outbox
                   VALUES (?, ?, ?, 'pending', NULL, NULL, ?, NULL)""",
                (
                    signal.signal_id,
                    payload_json,
                    payload_hash,
                    self._encode(queued_at),
                ),
            )

    def signal_observation(
        self, signal_id: str
    ) -> tuple[SignalRecord, dict[str, object], dict[str, object]] | None:
        """Read one immutable first observation and its committee/journal context."""
        row = self._connection.execute(
            """SELECT s.*, c.payload_json AS candidate_json,
                      o.payload_json AS journal_json
               FROM signals s
               JOIN signal_candidate_contexts c ON c.signal_id = s.signal_id
               JOIN signal_journal_outbox o ON o.signal_id = s.signal_id
               WHERE s.signal_id = ?""",
            (signal_id,),
        ).fetchone()
        if row is None:
            return None
        return (
            self._signal_from_row(row),
            json.loads(row["candidate_json"]),
            json.loads(row["journal_json"]),
        )

    def pending_signal_journal_outbox(
        self, limit: int = 256
    ) -> list[dict[str, object]]:
        """Return one bounded deterministic projection batch."""
        if limit < 1 or limit > 256:
            raise ValueError("journal outbox limit must be between 1 and 256")
        rows = self._connection.execute(
            """SELECT signal_id, payload_json, payload_hash, state
               FROM signal_journal_outbox WHERE state = 'pending'
               ORDER BY queued_at, signal_id LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def journal_projection_offset(self) -> int | None:
        row = self._connection.execute(
            "SELECT verified_offset FROM signal_journal_projection WHERE cohort_id = ?",
            (self.cohort_id,),
        ).fetchone()
        return None if row is None else int(row["verified_offset"])

    def initialize_journal_projection(
        self, verified_offset: int, initialized_at: datetime
    ) -> int:
        """Record the trusted boundary of a pre-outbox legacy journal once."""
        self._require_timezone_aware(initialized_at, "initialized_at")
        if verified_offset < 0:
            raise ValueError("journal projection offset cannot be negative")
        with self.transaction():
            existing = self.journal_projection_offset()
            if existing is not None:
                return existing
            encoded = self._encode(initialized_at)
            self._connection.execute(
                "INSERT INTO signal_journal_projection VALUES (?, ?, ?, ?)",
                (self.cohort_id, verified_offset, encoded, encoded),
            )
        return verified_offset

    def mark_signal_journal_mirrored(
        self,
        projections: list[tuple[str, str, int, int]],
        verified_offset: int,
        mirrored_at: datetime,
    ) -> None:
        """Advance outbox rows and projection boundary in one DB transaction."""
        self._require_timezone_aware(mirrored_at, "mirrored_at")
        with self.transaction():
            current = self.journal_projection_offset()
            if current is None or verified_offset < current:
                raise LedgerConflictError("journal projection offset regressed")
            for signal_id, payload_hash, offset, length in projections:
                row = self._connection.execute(
                    "SELECT payload_hash, state FROM signal_journal_outbox WHERE signal_id = ?",
                    (signal_id,),
                ).fetchone()
                if row is None or row["payload_hash"] != payload_hash:
                    raise LedgerConflictError(
                        f"journal outbox payload conflict for {signal_id}"
                    )
                if row["state"] == "mirrored":
                    stored = self._connection.execute(
                        "SELECT journal_offset, journal_length FROM signal_journal_outbox WHERE signal_id = ?",
                        (signal_id,),
                    ).fetchone()
                    if (
                        stored["journal_offset"] != offset
                        or stored["journal_length"] != length
                    ):
                        raise LedgerConflictError(
                            f"journal outbox offset conflict for {signal_id}"
                        )
                    continue
                self._connection.execute(
                    """UPDATE signal_journal_outbox SET state = 'mirrored',
                       journal_offset = ?, journal_length = ?, mirrored_at = ?
                       WHERE signal_id = ?""",
                    (offset, length, self._encode(mirrored_at), signal_id),
                )
            self._connection.execute(
                """UPDATE signal_journal_projection SET verified_offset = ?, updated_at = ?
                   WHERE cohort_id = ?""",
                (verified_offset, self._encode(mirrored_at), self.cohort_id),
            )

    def stage_intent(self, intent: OrderIntent) -> None:
        if intent.cohort_id != self.cohort_id:
            raise ValueError("intent cohort_id does not match ledger cohort_id")
        with self.transaction():
            if not intent.signal_ids:
                raise ValueError("intent must reference at least one signal")
            if len(set(intent.signal_ids)) != len(intent.signal_ids):
                raise ValueError("intent signal_ids must be unique")
            signal_placeholders = ", ".join("?" for _ in intent.signal_ids)
            provenance_rows = self._connection.execute(
                "SELECT signal_id, epoch_id, policy_id FROM signals WHERE signal_id IN "
                f"({signal_placeholders})",
                intent.signal_ids,
            ).fetchall()
            provenance_by_id = {row["signal_id"]: row for row in provenance_rows}
            missing_signal_ids = [
                signal_id
                for signal_id in intent.signal_ids
                if signal_id not in provenance_by_id
            ]
            if missing_signal_ids:
                raise ValueError(
                    "intent references missing signal IDs: "
                    + ", ".join(missing_signal_ids)
                )
            epoch_ids = {
                provenance_by_id[signal_id]["epoch_id"]
                for signal_id in intent.signal_ids
            }
            if len(epoch_ids) != 1:
                raise ValueError("intent signals must share one epoch_id")
            policy_ids = {
                provenance_by_id[signal_id]["policy_id"]
                for signal_id in intent.signal_ids
            }
            if len(policy_ids) != 1:
                raise ValueError("intent signals must share one policy_id")
            existing_row = self._connection.execute(
                "SELECT * FROM order_intents WHERE intent_id = ?", (intent.intent_id,)
            ).fetchone()
            if existing_row is not None and self._matches_split_adjusted_intent(
                intent, self._intent_from_row(existing_row)
            ):
                return
            inserted = self._insert_idempotent(
                "order_intents",
                "intent_id",
                intent.intent_id,
                {
                    "intent_id": intent.intent_id,
                    "cohort_id": intent.cohort_id,
                    "side": intent.side,
                    "requested_qty": intent.requested_qty,
                    "created_at": intent.created_at,
                    "eligible_session": intent.eligible_session,
                    "price_rule": intent.price_rule,
                    "status": intent.status,
                    "stop_price": intent.stop_price,
                    "external_order_id": intent.external_order_id,
                },
            )
            if not inserted:
                existing_signal_ids = tuple(
                    item[0]
                    for item in self._connection.execute(
                        "SELECT signal_id FROM intent_signals WHERE intent_id = ? "
                        "ORDER BY signal_order, signal_id",
                        (intent.intent_id,),
                    )
                )
                if existing_signal_ids != intent.signal_ids:
                    raise LedgerConflictError(
                        f"conflicting order_intents identity {intent.intent_id}"
                    )
                return
            for signal_order, signal_id in enumerate(intent.signal_ids):
                self._connection.execute(
                    "INSERT INTO intent_signals(intent_id, signal_id, signal_order) VALUES (?, ?, ?)",
                    (intent.intent_id, signal_id, signal_order),
                )

    def stage_exit_intent(
        self, intent: OrderIntent, lot_quantities: tuple[tuple[str, int], ...]
    ) -> None:
        """Stage an exit with exact, durable lot ownership."""
        if intent.side not in {"sell", "cover"}:
            raise ValueError("exit intent must be sell or cover")
        if (
            not lot_quantities
            or sum(quantity for _, quantity in lot_quantities) != intent.requested_qty
        ):
            raise ValueError("exit lot quantities must exactly cover requested_qty")
        if len({lot_id for lot_id, _ in lot_quantities}) != len(lot_quantities):
            raise ValueError("exit lot IDs must be unique")
        with self.transaction():
            for lot_id, quantity in lot_quantities:
                row = self._connection.execute(
                    """SELECT direction, open_qty FROM lots
                       WHERE lot_id = ? AND cohort_id = ?""",
                    (lot_id, self.cohort_id),
                ).fetchone()
                if row is None or quantity <= 0 or quantity > int(row["open_qty"]):
                    raise ValueError(f"invalid exit lot allocation {lot_id}")
                expected_direction = "long" if intent.side == "sell" else "short"
                if row["direction"] != expected_direction:
                    raise ValueError(f"exit side does not match lot {lot_id}")
                occupied = self._connection.execute(
                    """SELECT eil.intent_id FROM exit_intent_lots eil
                       JOIN order_intents i ON i.intent_id = eil.intent_id
                       WHERE eil.lot_id = ? AND i.status = 'pending'""",
                    (lot_id,),
                ).fetchone()
                if occupied is not None and occupied["intent_id"] != intent.intent_id:
                    raise ValueError(f"lot {lot_id} already has a pending exit")
            self.stage_intent(intent)
            for lot_id, quantity in lot_quantities:
                existing = self._connection.execute(
                    """SELECT quantity FROM exit_intent_lots
                       WHERE intent_id = ? AND lot_id = ?""",
                    (intent.intent_id, lot_id),
                ).fetchone()
                if existing is not None:
                    if int(existing["quantity"]) != quantity:
                        raise LedgerConflictError(
                            f"conflicting exit lot ownership {intent.intent_id}/{lot_id}"
                        )
                    continue
                self._connection.execute(
                    "INSERT INTO exit_intent_lots VALUES (?, ?, ?)",
                    (intent.intent_id, lot_id, quantity),
                )

    def _due_intent_rows(self, session: date) -> list[sqlite3.Row]:
        """Return the one canonical set of intents executable in ``session``."""
        return self._connection.execute(
            """SELECT * FROM order_intents
               WHERE cohort_id = ? AND status = 'pending' AND (
                   (price_rule = 'next_session_open' AND eligible_session = ?)
                   OR (price_rule = 'resting_stop' AND eligible_session <= ?)
               )
               ORDER BY created_at, intent_id""",
            (self.cohort_id, self._encode(session), self._encode(session)),
        ).fetchall()

    def pending_intents(self, session: date) -> list[OrderIntent]:
        rows = self._due_intent_rows(session)
        return [self._intent_from_row(row) for row in rows]

    def reject_intent(
        self, intent_id: str, occurred_at: datetime, reason: str
    ) -> OrderIntent:
        """Durably terminalize a pending intent after a current risk rejection."""
        return self._terminalize_intent(intent_id, "rejected", occurred_at, reason)

    def cancel_intent(
        self, intent_id: str, occurred_at: datetime, reason: str
    ) -> OrderIntent:
        """Durably terminalize an intent after broker-confirmed cancellation."""
        return self._terminalize_intent(intent_id, "cancelled", occurred_at, reason)

    def _terminalize_intent(
        self,
        intent_id: str,
        status: str,
        occurred_at: datetime,
        reason: str,
    ) -> OrderIntent:
        self._require_timezone_aware(occurred_at, "occurred_at")
        if status not in {"rejected", "cancelled"}:
            raise ValueError(f"unsupported terminal intent status {status}")
        if not reason:
            raise ValueError(f"intent {status} reason is required")
        with self.transaction():
            row = self._connection.execute(
                "SELECT * FROM order_intents WHERE intent_id = ? AND cohort_id = ?",
                (intent_id, self.cohort_id),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown order intent {intent_id}")
            if row["status"] == status:
                transition = self._connection.execute(
                    """SELECT occurred_at, reason FROM order_status_transitions
                       WHERE intent_id = ? AND status = ?""",
                    (intent_id, status),
                ).fetchone()
                if (
                    transition is None
                    or transition["occurred_at"] != self._encode(occurred_at)
                    or transition["reason"] != reason
                ):
                    replay_label = (
                        "rejection" if status == "rejected" else "cancellation"
                    )
                    raise LedgerConflictError(
                        f"conflicting {replay_label} replay for intent {intent_id}"
                    )
                return self._intent_from_row(row)
            if row["status"] != "pending":
                raise ValueError(
                    f"cannot {status} terminal intent {intent_id}/{row['status']}"
                )
            self._connection.execute(
                "UPDATE order_intents SET status = ? WHERE intent_id = ?",
                (status, intent_id),
            )
            self._connection.execute(
                """INSERT INTO order_status_transitions
                   (transition_id, intent_id, status, occurred_at, reason)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    stable_id("order_status", intent_id, status, reason),
                    intent_id,
                    status,
                    self._encode(occurred_at),
                    reason,
                ),
            )
            updated = self._connection.execute(
                "SELECT * FROM order_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            if updated is None:  # pragma: no cover - same transaction invariant.
                raise RuntimeError(f"{status} intent disappeared")
            return self._intent_from_row(updated)

    def intent(self, intent_id: str) -> OrderIntent | None:
        """Return one persisted intent, including its ordered signal provenance."""
        row = self._connection.execute(
            "SELECT * FROM order_intents WHERE intent_id = ? AND cohort_id = ?",
            (intent_id, self.cohort_id),
        ).fetchone()
        return self._intent_from_row(row) if row is not None else None

    def signals_for_intent(self, intent_id: str) -> tuple[SignalRecord, ...]:
        """Return the exact persisted signals linked to an intent."""
        rows = self._connection.execute(
            """SELECT s.* FROM intent_signals isg
               JOIN signals s ON s.signal_id = isg.signal_id
               JOIN order_intents i ON i.intent_id = isg.intent_id
               WHERE isg.intent_id = ? AND i.cohort_id = ?
               ORDER BY isg.signal_order, s.signal_id""",
            (intent_id, self.cohort_id),
        ).fetchall()
        return tuple(self._signal_from_row(row) for row in rows)

    def open_positions(self) -> list[dict[str, object]]:
        """Return a bounded aggregate view over authoritative open lots."""
        rows = self._connection.execute(
            """SELECT ticker, direction, open_qty, entry_price
               FROM lots
               WHERE cohort_id = ? AND open_qty > 0
               ORDER BY ticker, direction, opened_session, lot_id""",
            (self.cohort_id,),
        ).fetchall()
        grouped: dict[tuple[str, str], tuple[int, Decimal]] = {}
        for row in rows:
            key = (row["ticker"], row["direction"])
            quantity, basis = grouped.get(key, (0, Decimal("0")))
            open_qty = int(row["open_qty"])
            grouped[key] = (
                quantity + open_qty,
                basis + _decimal(row["entry_price"]) * open_qty,
            )
        return [
            {
                "ticker": ticker,
                "quantity": quantity,
                "avg_price": basis / quantity,
                "instrument_type": "stock",
                "side": direction,
            }
            for (ticker, direction), (quantity, basis) in grouped.items()
        ]

    def open_exit_positions(self) -> list[dict[str, object]]:
        """Return bounded lot-level exit state with original signal provenance."""
        rows = self._connection.execute(
            """SELECT l.lot_id, l.ticker, l.direction, l.open_qty,
                      l.entry_price, l.opened_session, f.intent_id
               FROM lots l JOIN fills f ON f.fill_id = l.fill_id
               WHERE l.cohort_id = ? AND l.open_qty > 0
               ORDER BY l.ticker, l.direction, l.opened_session, l.lot_id""",
            (self.cohort_id,),
        ).fetchall()
        positions: list[dict[str, object]] = []
        for row in rows:
            signals = self.signals_for_intent(row["intent_id"])
            if not signals:
                raise LedgerConflictError(
                    f"open lot {row['lot_id']} has no signal provenance"
                )
            positions.append(
                {
                    "lot_id": row["lot_id"],
                    "ticker": row["ticker"],
                    "direction": row["direction"],
                    "quantity": int(row["open_qty"]),
                    "entry_price": _decimal(row["entry_price"]),
                    "opened_session": _date(row["opened_session"]),
                    "signal_ids": tuple(signal.signal_id for signal in signals),
                    "strategies": tuple(
                        sorted({signal.strategy for signal in signals})
                    ),
                    "epoch_id": signals[0].epoch_id,
                    "policy_id": signals[0].policy_id,
                }
            )
        return positions

    def pending_exit_intents(
        self, ticker: str, lot_id: str | None = None
    ) -> list[OrderIntent]:
        """Return pending exits for one ticker, optionally one owned lot."""
        lot_clause = " AND eil.lot_id = ?" if lot_id is not None else ""
        values: tuple[object, ...] = (
            (self.cohort_id, ticker, lot_id)
            if lot_id is not None
            else (self.cohort_id, ticker)
        )
        rows = self._connection.execute(
            """SELECT DISTINCT i.* FROM order_intents i
               JOIN intent_signals isg ON isg.intent_id = i.intent_id
               JOIN signals s ON s.signal_id = isg.signal_id
               JOIN exit_intent_lots eil ON eil.intent_id = i.intent_id
               WHERE i.cohort_id = ? AND i.status = 'pending'
                 AND i.side IN ('sell', 'cover') AND s.ticker = ?"""
            + lot_clause
            + " ORDER BY i.created_at, i.intent_id",
            values,
        ).fetchall()
        return [self._intent_from_row(row) for row in rows]

    def external_order_for_intent(self, intent_id: str) -> dict[str, object] | None:
        """Return the single durable broker order associated with an intent."""
        row = self._connection.execute(
            """SELECT eo.* FROM external_orders eo
               JOIN order_intents i ON i.intent_id = eo.intent_id
               WHERE eo.intent_id = ? AND i.cohort_id = ?
               ORDER BY eo.submitted_at DESC, eo.external_order_id
               LIMIT 1""",
            (intent_id, self.cohort_id),
        ).fetchone()
        return dict(row) if row is not None else None

    def prepare_external_order(
        self,
        *,
        intent_id: str,
        broker: str,
        submitted_at: datetime,
    ) -> None:
        """Persist retry authority before making an external submit call."""
        self._require_timezone_aware(submitted_at, "submitted_at")
        with self.transaction():
            intent = self._connection.execute(
                "SELECT external_order_id FROM order_intents "
                "WHERE intent_id = ? AND cohort_id = ?",
                (intent_id, self.cohort_id),
            ).fetchone()
            if intent is None:
                raise ValueError(f"unknown order intent {intent_id}")
            existing = self.external_order_for_intent(intent_id)
            if existing is not None:
                if existing["broker"] != broker:
                    raise LedgerConflictError(
                        f"conflicting external broker for intent {intent_id}"
                    )
                return
            self._connection.execute(
                """INSERT INTO external_orders
                   (external_order_id, intent_id, broker, status, submitted_at,
                    reconciled_at, detail)
                   VALUES (?, ?, ?, 'pending', ?, NULL, ?)""",
                (
                    intent_id,
                    intent_id,
                    broker,
                    self._encode(submitted_at),
                    "prepared before external submit",
                ),
            )
            self._connection.execute(
                "UPDATE order_intents SET external_order_id = ? WHERE intent_id = ?",
                (intent_id, intent_id),
            )

    def record_external_order(
        self,
        *,
        intent_id: str,
        external_order_id: str,
        broker: str,
        status: str,
        submitted_at: datetime,
        reconciled_at: datetime | None,
        detail: str,
    ) -> None:
        """Persist initial submission or reconciliation for one broker order."""
        if not external_order_id:
            raise ValueError("external_order_id is required")
        self._require_timezone_aware(submitted_at, "submitted_at")
        if reconciled_at is not None:
            self._require_timezone_aware(reconciled_at, "reconciled_at")
            if reconciled_at < submitted_at:
                raise ValueError("reconciled_at precedes submitted_at")
        with self.transaction():
            intent = self._connection.execute(
                "SELECT external_order_id FROM order_intents "
                "WHERE intent_id = ? AND cohort_id = ?",
                (intent_id, self.cohort_id),
            ).fetchone()
            if intent is None:
                raise ValueError(f"unknown order intent {intent_id}")
            linked_id = intent["external_order_id"]
            linked_row = (
                self._connection.execute(
                    "SELECT * FROM external_orders WHERE external_order_id = ?",
                    (linked_id,),
                ).fetchone()
                if linked_id is not None
                else None
            )
            row = self._connection.execute(
                "SELECT * FROM external_orders WHERE external_order_id = ?",
                (external_order_id,),
            ).fetchone()
            values = (
                intent_id,
                broker,
                status,
                self._encode(submitted_at),
                self._encode(reconciled_at) if reconciled_at is not None else None,
                detail,
                external_order_id,
            )
            if (
                linked_row is not None
                and linked_id == intent_id
                and external_order_id != linked_id
                and row is None
            ):
                if linked_row["broker"] != broker:
                    raise LedgerConflictError(
                        f"conflicting external broker for intent {intent_id}"
                    )
                self._validate_external_status_transition(linked_row["status"], status)
                self._connection.execute(
                    """UPDATE external_orders
                       SET external_order_id = ?, status = ?, reconciled_at = ?,
                           detail = ?
                       WHERE external_order_id = ?""",
                    (
                        external_order_id,
                        status,
                        self._encode(reconciled_at)
                        if reconciled_at is not None
                        else None,
                        detail,
                        linked_id,
                    ),
                )
                self._connection.execute(
                    "UPDATE order_intents SET external_order_id = ? "
                    "WHERE intent_id = ?",
                    (external_order_id, intent_id),
                )
                return
            if linked_id is not None and linked_id != external_order_id:
                raise LedgerConflictError(
                    f"intent {intent_id} already has external order {linked_id}"
                )
            if row is None:
                self._connection.execute(
                    """INSERT INTO external_orders
                       (intent_id, broker, status, submitted_at, reconciled_at,
                        detail, external_order_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    values,
                )
                self._connection.execute(
                    "UPDATE order_intents SET external_order_id = ? "
                    "WHERE intent_id = ?",
                    (external_order_id, intent_id),
                )
                return
            if row["intent_id"] != intent_id or row["broker"] != broker:
                raise LedgerConflictError(
                    f"conflicting external order identity {external_order_id}"
                )
            self._validate_external_status_transition(row["status"], status)
            self._connection.execute(
                """UPDATE external_orders SET status = ?, reconciled_at = ?,
                          detail = ?
                   WHERE external_order_id = ?""",
                (
                    status,
                    self._encode(reconciled_at)
                    if reconciled_at is not None
                    else row["reconciled_at"],
                    detail,
                    external_order_id,
                ),
            )

    @staticmethod
    def _validate_external_status_transition(old: str, new: str) -> None:
        if old == new:
            return
        allowed = {
            "pending": {
                "accepted",
                "partially_filled",
                "filled",
                "rejected",
                "cancelled",
            },
            "accepted": {
                "partially_filled",
                "filled",
                "rejected",
                "cancelled",
            },
            "partially_filled": {"filled", "rejected", "cancelled"},
            "filled": set(),
            "rejected": set(),
            "cancelled": set(),
        }
        if old not in allowed or new not in allowed[old]:
            raise LedgerConflictError(
                f"external order status cannot regress from {old} to {new}"
            )

    def session_is_valid(self, session: date, ticker: str | None = None) -> bool:
        """Public fail-closed state query for policy/entry execution callers."""
        return self._session_invalid_reason(session, ticker) is None

    def session_invalid_reason(self, session: date, ticker: str | None = None) -> str:
        return self._session_invalid_reason(session, ticker) or ""

    def reject_corporate_action_batch(
        self,
        session: date,
        actions: tuple[CorporateAction, ...],
        governed_tickers: tuple[str, ...],
        errors: tuple[str, ...],
        rejected_at: datetime,
    ) -> str:
        """Reject one complete provider response without applying any member."""
        self._require_timezone_aware(rejected_at, "rejected_at")
        if not errors:
            raise ValueError("corporate action batch rejection requires errors")
        payload = [
            {
                "action_id": action.action_id,
                "ticker": action.ticker,
                "session": self._encode(action.session),
                "action_type": action.action_type,
                "ratio": self._encode(action.ratio),
                "cash_per_share": self._encode(action.cash_per_share),
                "source": action.source,
                "fetched_at": self._encode(action.fetched_at),
                "verified": action.verified,
            }
            for action in sorted(
                actions, key=lambda item: (item.action_id, item.ticker)
            )
        ]
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        payload_hash = stable_id("corporate_action_batch_payload", payload)
        errors_json = json.dumps(sorted(set(errors)), separators=(",", ":"))
        batch_id = stable_id(
            "corporate_action_batch_rejection",
            self.cohort_id,
            session,
            payload_hash,
        )
        reason = "invalid corporate action batch: " + "; ".join(sorted(set(errors)))
        governed = set(governed_tickers)
        held = {str(position["ticker"]) for position in self.open_positions()}
        affected = sorted(
            {
                action.ticker
                for action in actions
                if action.ticker in governed and action.ticker in held
            }
        )
        with self.transaction():
            self._connection.execute(
                """INSERT OR IGNORE INTO corporate_action_batch_rejections
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    batch_id,
                    self.cohort_id,
                    self._encode(session),
                    payload_json,
                    payload_hash,
                    errors_json,
                    self._encode(rejected_at),
                ),
            )
            if not self.session_invalid_reason(session):
                self.invalidate_session_and_cancel_due(session, reason, rejected_at)
            else:
                self._invalidate_session(session, reason, rejected_at)
            for ticker in affected:
                self._quarantine_ticker(ticker, reason, rejected_at)
        return reason

    def corporate_action_batch_state_errors(
        self, session: date, actions: tuple[CorporateAction, ...]
    ) -> tuple[str, ...]:
        """Validate a complete action batch against live lot/intent state."""
        errors: list[str] = []
        unique_actions: list[CorporateAction] = []
        seen_actions: dict[str, CorporateAction] = {}
        for action in sorted(actions, key=lambda item: item.action_id):
            existing_action = seen_actions.get(action.action_id)
            if existing_action is not None:
                if existing_action != action:
                    errors.append(
                        f"conflicting corporate action identity {action.action_id}"
                    )
                continue
            seen_actions[action.action_id] = action
            unique_actions.append(action)
        lot_quantities: dict[str, list[Decimal]] = {}
        intent_quantities: dict[str, list[Decimal]] = {}
        allocation_quantities: dict[str, list[Decimal]] = {}
        for action in unique_actions:
            ticker = action.ticker
            if ticker in lot_quantities:
                continue
            lot_quantities[ticker] = [
                Decimal(int(row["open_qty"]))
                for row in self._connection.execute(
                    """SELECT open_qty FROM lots
                       WHERE cohort_id = ? AND ticker = ? AND open_qty > 0
                       ORDER BY lot_id""",
                    (self.cohort_id, ticker),
                )
            ]
            pending = self._pending_exit_rows_for_ticker(ticker)
            intent_quantities[ticker] = [
                Decimal(int(row["requested_qty"])) for row in pending
            ]
            allocation_quantities[ticker] = [
                Decimal(int(row["quantity"]))
                for intent in pending
                for row in self._connection.execute(
                    """SELECT quantity FROM exit_intent_lots
                       WHERE intent_id = ? ORDER BY lot_id""",
                    (intent["intent_id"],),
                )
            ]
        for action in unique_actions:
            existing = self._connection.execute(
                "SELECT * FROM corporate_actions WHERE action_id = ?",
                (action.action_id,),
            ).fetchone()
            if existing is not None:
                if not self._same_corporate_action(existing, action):
                    errors.append(
                        f"conflicting corporate action identity {action.action_id}"
                    )
                continue
            if action.session != session:
                errors.append(f"corporate action session mismatch {action.action_id}")
                continue
            if action.action_type != "split":
                continue
            ratio = action.ratio
            if ratio is None or not ratio.is_finite() or ratio <= 0:
                errors.append(f"invalid split ratio {action.action_id}")
                continue
            groups = (
                lot_quantities[action.ticker],
                intent_quantities[action.ticker],
                allocation_quantities[action.ticker],
            )
            scaled_groups = [
                [quantity * ratio for quantity in group] for group in groups
            ]
            if any(
                quantity != quantity.to_integral_value()
                for group in scaled_groups
                for quantity in group
            ):
                errors.append(
                    f"split produces fractional share quantity {action.action_id}"
                )
                continue
            (
                lot_quantities[action.ticker],
                intent_quantities[action.ticker],
                allocation_quantities[action.ticker],
            ) = scaled_groups
        return tuple(sorted(set(errors)))

    def corporate_action_batch_errors(
        self,
        session: date,
        actions: tuple[CorporateAction, ...],
        processed_at: datetime,
    ) -> tuple[str, ...]:
        """Validate structural and state-dependent action invariants without mutation."""
        errors: list[str] = list(
            self.corporate_action_batch_state_errors(session, actions)
        )
        seen: dict[str, CorporateAction] = {}
        for action in actions:
            if action.action_id in seen and seen[action.action_id] != action:
                errors.append(f"conflicting corporate action {action.action_id}")
            seen[action.action_id] = action
            if action.session != session:
                errors.append(f"corporate action session mismatch {action.action_id}")
            if not action.source.strip():
                errors.append(f"missing source corporate action {action.action_id}")
            if not action.verified:
                errors.append(f"unverified corporate action {action.action_id}")
            if (
                action.fetched_at.tzinfo is None
                or action.fetched_at.utcoffset() is None
            ):
                errors.append(f"naive corporate action {action.action_id}")
            elif action.fetched_at > processed_at:
                errors.append(f"future corporate action {action.action_id}")
            elif action.fetched_at < session_close(session):
                errors.append(f"pre-close corporate action {action.action_id}")
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
        return tuple(sorted(set(errors)))

    def invalidate_session_and_cancel_due(
        self, session: date, reason: str, processed_at: datetime
    ) -> None:
        """Persist invalidation and terminalize stranded next-open intents."""
        self._require_timezone_aware(processed_at, "processed_at")
        if not reason:
            raise ValueError("session invalidation reason is required")
        with self.transaction():
            self._invalidate_session(session, reason, processed_at)
            run_id = stable_id("session_run", self.cohort_id, session)
            existing = self._connection.execute(
                "SELECT * FROM session_runs WHERE session_run_id = ?", (run_id,)
            ).fetchone()
            values = (
                run_id,
                self.cohort_id,
                self._encode(session),
                0,
                reason,
                self._encode(processed_at),
                self._encode(processed_at),
            )
            if existing is None:
                self._connection.execute(
                    "INSERT INTO session_runs VALUES (?, ?, ?, ?, ?, ?, ?)", values
                )
            elif existing["valid"] != 0 or existing["invalid_reason"] != reason:
                raise LedgerConflictError(
                    f"conflicting invalid session run {self.cohort_id}/{session}"
                )
            due = self._due_intent_rows(session)
            for row in due:
                self._terminalize_intent(
                    row["intent_id"],
                    "cancelled",
                    processed_at,
                    f"session invalid: {reason}",
                )

    def record_session_complete(self, session: date, processed_at: datetime) -> None:
        """Mark a valid session complete inside the caller's snapshot transaction."""
        self._require_timezone_aware(processed_at, "processed_at")
        run_id = stable_id("session_run", self.cohort_id, session)
        row = self._connection.execute(
            "SELECT * FROM session_runs WHERE session_run_id = ?", (run_id,)
        ).fetchone()
        if row is not None:
            if row["valid"] != 1 or row["invalid_reason"]:
                raise LedgerConflictError(
                    f"conflicting completed session run {self.cohort_id}/{session}"
                )
            return
        self._connection.execute(
            "INSERT INTO session_runs VALUES (?, ?, ?, 1, '', ?, ?)",
            (
                run_id,
                self.cohort_id,
                self._encode(session),
                self._encode(processed_at),
                self._encode(processed_at),
            ),
        )

    def assert_session_tradeable(
        self, session: date, ticker: str | None = None
    ) -> None:
        reason = self._session_invalid_reason(session, ticker)
        if reason is not None:
            target = f"/{ticker}" if ticker is not None else ""
            raise ValueError(f"session {session}{target} is invalid: {reason}")

    def read_snapshots(
        self,
        start_session: date | None = None,
        end_session: date | None = None,
        epoch_id: str | None = None,
        valid_only: bool = False,
    ) -> list[AccountSnapshot]:
        clauses, values = self._session_filters(
            "account_snapshots", start_session, end_session, epoch_id
        )
        if valid_only:
            clauses.append("valid = 1")
        rows = self._connection.execute(
            "SELECT * FROM account_snapshots WHERE cohort_id = ? AND "
            + " AND ".join(clauses)
            + " ORDER BY session, snapshot_id",
            (self.cohort_id, *values),
        ).fetchall()
        return [self._snapshot_from_row(row) for row in rows]

    def read_benchmark_observations(
        self,
        start_session: date | None = None,
        end_session: date | None = None,
        epoch_id: str | None = None,
    ) -> list[BenchmarkObservation]:
        clauses, values = self._session_filters(
            "benchmark_observations", start_session, end_session, epoch_id
        )
        rows = self._connection.execute(
            "SELECT * FROM benchmark_observations WHERE cohort_id = ? AND "
            + " AND ".join(clauses)
            + " ORDER BY session, observation_id",
            (self.cohort_id, *values),
        ).fetchall()
        return [self._benchmark_from_row(row) for row in rows]

    def read_signals(
        self,
        start_session: date | None = None,
        end_session: date | None = None,
        epoch_id: str | None = None,
        policy_id: str | None = None,
    ) -> list[SignalRecord]:
        clauses = ["1 = 1"]
        values: list[object] = []
        if start_session is not None:
            clauses.append("reference_session >= ?")
            values.append(self._encode(start_session))
        if end_session is not None:
            clauses.append("reference_session <= ?")
            values.append(self._encode(end_session))
        if epoch_id is not None:
            clauses.append("epoch_id = ?")
            values.append(epoch_id)
        if policy_id is not None:
            clauses.append("policy_id = ?")
            values.append(policy_id)
        rows = self._connection.execute(
            "SELECT * FROM signals WHERE "
            + " AND ".join(clauses)
            + " ORDER BY reference_session, signal_id",
            values,
        ).fetchall()
        return [self._signal_from_row(row) for row in rows]

    def signals_by_ids(self, signal_ids: tuple[str, ...]) -> tuple[SignalRecord, ...]:
        """Read only the requested signal identities in deterministic order."""
        if not signal_ids:
            return ()
        placeholders = ", ".join("?" for _ in signal_ids)
        rows = self._connection.execute(
            f"SELECT * FROM signals WHERE signal_id IN ({placeholders}) "
            "ORDER BY signal_id",
            signal_ids,
        ).fetchall()
        return tuple(self._signal_from_row(row) for row in rows)

    def read_fills(
        self,
        start_session: date | None = None,
        end_session: date | None = None,
        epoch_id: str | None = None,
    ) -> list[Fill]:
        clauses = ["i.cohort_id = ?"]
        values: list[object] = [self.cohort_id]
        if start_session is not None:
            clauses.append("f.session >= ?")
            values.append(self._encode(start_session))
        if end_session is not None:
            clauses.append("f.session <= ?")
            values.append(self._encode(end_session))
        if epoch_id is not None:
            clauses.append(
                "EXISTS (SELECT 1 FROM intent_signals isg JOIN signals s "
                "ON s.signal_id = isg.signal_id WHERE isg.intent_id = "
                "f.intent_id AND s.epoch_id = ?)"
            )
            values.append(epoch_id)
        rows = self._connection.execute(
            "SELECT f.* FROM fills f JOIN order_intents i ON i.intent_id = f.intent_id WHERE "
            + " AND ".join(clauses)
            + " ORDER BY f.session, f.fill_id",
            values,
        ).fetchall()
        return [self._fill_from_row(row) for row in rows]

    def opening_cash(self) -> Decimal:
        """Return the immutable opening allocation for this cohort."""
        row = self._connection.execute(
            """SELECT amount FROM cash_events
               WHERE cohort_id = ? AND event_type = 'opening'
               ORDER BY cash_event_id LIMIT 1""",
            (self.cohort_id,),
        ).fetchone()
        if row is None:
            raise LedgerConflictError(f"missing opening cash for {self.cohort_id}")
        return _decimal(row["amount"])

    def read_trade_projections(self) -> list[TradeProjectionRecord]:
        """Return deterministic joined lot/fill records without exposing SQLite."""
        lot_rows = self._connection.execute(
            """SELECT l.*, f.fill_id AS entry_fill_id,
                      f.session AS entry_fill_session,
                      f.fill_price AS entry_fill_price,
                      f.slippage AS entry_slippage,
                      f.commission AS entry_commission,
                      f.other_fees AS entry_other_fees,
                      i.intent_id AS opening_intent_id
               FROM lots l
               JOIN fills f ON f.fill_id = l.fill_id
               JOIN order_intents i ON i.intent_id = f.intent_id
               WHERE l.cohort_id = ?
               ORDER BY f.session, f.fill_id""",
            (self.cohort_id,),
        ).fetchall()
        if not lot_rows:
            return []

        signal_rows = self._connection.execute(
            """SELECT isg.intent_id, isg.signal_id, isg.signal_order,
                      s.strategy, s.ticker
               FROM intent_signals isg
               JOIN signals s ON s.signal_id = isg.signal_id
               JOIN order_intents i ON i.intent_id = isg.intent_id
               WHERE i.cohort_id = ?
               ORDER BY isg.intent_id, isg.signal_order, isg.signal_id""",
            (self.cohort_id,),
        ).fetchall()
        signals_by_intent: dict[str, list[sqlite3.Row]] = {}
        for row in signal_rows:
            signals_by_intent.setdefault(row["intent_id"], []).append(row)

        closure_rows = self._connection.execute(
            """SELECT lc.closure_id, lc.lot_id, lc.quantity, lc.realized_pnl,
                      f.fill_id, f.session, f.effective_at, f.fill_price,
                      f.quantity AS fill_quantity, f.slippage, f.commission,
                      f.other_fees
               FROM lot_closures lc
               JOIN lots l ON l.lot_id = lc.lot_id
               JOIN fills f ON f.fill_id = lc.fill_id
               WHERE l.cohort_id = ?
               ORDER BY f.session, f.fill_id, lc.lot_id, lc.closure_id""",
            (self.cohort_id,),
        ).fetchall()
        closures_by_lot: dict[str, list[sqlite3.Row]] = {}
        closures_by_fill: dict[str, list[sqlite3.Row]] = {}
        for row in closure_rows:
            closures_by_lot.setdefault(row["lot_id"], []).append(row)
            closures_by_fill.setdefault(row["fill_id"], []).append(row)

        allocated_costs: dict[tuple[str, str], tuple[Decimal, Decimal, Decimal]] = {}
        for fill_id, rows in closures_by_fill.items():
            allocated = [Decimal("0"), Decimal("0"), Decimal("0")]
            totals = [
                _decimal(rows[0]["slippage"]),
                _decimal(rows[0]["commission"]),
                _decimal(rows[0]["other_fees"]),
            ]
            fill_quantity = int(rows[0]["fill_quantity"])
            for index, row in enumerate(rows):
                if index == len(rows) - 1:
                    amounts = tuple(
                        total - used for total, used in zip(totals, allocated)
                    )
                else:
                    ratio = Decimal(int(row["quantity"])) / Decimal(fill_quantity)
                    amounts = tuple(total * ratio for total in totals)
                    allocated = [
                        used + amount for used, amount in zip(allocated, amounts)
                    ]
                allocated_costs[(fill_id, row["lot_id"])] = amounts

        records: list[TradeProjectionRecord] = []
        for lot in lot_rows:
            intent_id = str(lot["opening_intent_id"])
            signals = signals_by_intent.get(intent_id, [])
            if not signals:
                raise LedgerConflictError(f"opening intent {intent_id} has no signals")
            ticker = str(lot["ticker"])
            if any(str(signal["ticker"]) != ticker for signal in signals):
                raise LedgerConflictError(
                    f"ambiguous ticker provenance for {intent_id}"
                )

            closures = closures_by_lot.get(lot["lot_id"], [])
            realized_pnl = sum(
                (_decimal(row["realized_pnl"]) for row in closures), Decimal("0")
            )
            original_shares = int(lot["original_qty"])
            open_shares = int(lot["open_qty"])
            status = "open" if open_shares else "closed"
            exit_price = None
            exit_session = None
            if status == "closed":
                # Corporate actions adjust the lot's entry price and quantity but
                # intentionally leave historical fills untouched.  Derive the
                # economically equivalent terminal price on that current basis so
                # legacy price-return consumers reconcile exactly to ledger P&L.
                pnl_per_share = realized_pnl / Decimal(original_shares)
                if str(lot["direction"]) == "short":
                    exit_price = _decimal(lot["entry_price"]) - pnl_per_share
                else:
                    exit_price = _decimal(lot["entry_price"]) + pnl_per_share
                exit_session = _date(closures[-1]["session"])

            costs = [
                _decimal(lot["entry_slippage"]),
                _decimal(lot["entry_commission"]),
                _decimal(lot["entry_other_fees"]),
            ]
            for closure in closures:
                amounts = allocated_costs[(closure["fill_id"], lot["lot_id"])]
                costs = [total + amount for total, amount in zip(costs, amounts)]

            strategies = tuple(sorted({str(row["strategy"]) for row in signals}))
            records.append(
                TradeProjectionRecord(
                    trade_id=str(lot["entry_fill_id"]),
                    signal_ids=tuple(str(row["signal_id"]) for row in signals),
                    intent_id=intent_id,
                    execution_id=str(lot["entry_fill_id"]),
                    exit_fill_ids=tuple(
                        dict.fromkeys(str(row["fill_id"]) for row in closures)
                    ),
                    strategy=strategies[0],
                    strategies=strategies,
                    ticker=ticker,
                    direction=str(lot["direction"]),
                    entry_session=_date(lot["opened_session"]),
                    entry_price=_decimal(lot["entry_price"]),
                    shares=open_shares if status == "open" else original_shares,
                    original_shares=original_shares,
                    closed_shares=original_shares - open_shares,
                    open_shares=open_shares,
                    status=status,
                    exit_session=exit_session,
                    exit_price=exit_price,
                    realized_pnl=realized_pnl,
                    slippage_cost=costs[0],
                    commission_cost=costs[1],
                    other_fees=costs[2],
                )
            )
        return records

    def accrue_borrow(
        self,
        session: date,
        close_marks: dict[str, MarketBar],
        rates: dict[str, Decimal | None],
        valuation_at: datetime,
    ) -> LedgerEvent:
        """Accrue ACT/365 borrow once per short ticker at that session close."""
        self._require_timezone_aware(valuation_at, "valuation_at")
        with self.transaction():
            lots = self._open_lots_for_direction("short")
            tickers = sorted({lot["ticker"] for lot in lots})
            marks = self._validate_accrual_marks(
                session, close_marks, tickers, valuation_at
            )
            events: list[LedgerEvent] = []
            for ticker in tickers:
                rate = rates.get(ticker)
                flagged = rate is None
                annual_rate = (
                    self._cost_model.existing_short_missing_borrow_rate
                    if flagged
                    else rate
                )
                if (
                    not isinstance(annual_rate, Decimal)
                    or not annual_rate.is_finite()
                    or annual_rate < 0
                ):
                    raise ValueError(f"invalid borrow rate for existing short {ticker}")
                quantity = sum(
                    (
                        Decimal(lot["open_qty"])
                        for lot in lots
                        if lot["ticker"] == ticker
                    ),
                    Decimal("0"),
                )
                amount = self._cost_model.borrow_charge(
                    quantity * marks[ticker].close, annual_rate
                )
                accrual_id = stable_id("borrow", self.cohort_id, session, ticker)
                event = LedgerEvent(
                    accrual_id,
                    session,
                    "borrow",
                    amount,
                    flagged,
                    "existing short borrow fallback rate"
                    if flagged
                    else "short borrow ACT/365",
                )
                existing = self._connection.execute(
                    "SELECT * FROM borrow_accruals WHERE accrual_id = ?", (accrual_id,)
                ).fetchone()
                expected = (
                    accrual_id,
                    None,
                    self._encode(session),
                    self._encode(amount),
                    self._encode(annual_rate),
                    self._encode(flagged),
                )
                if existing is not None:
                    columns = (
                        "accrual_id",
                        "lot_id",
                        "session",
                        "amount",
                        "annual_rate",
                        "flagged",
                    )
                    if any(
                        existing[column] != value
                        for column, value in zip(columns, expected)
                    ):
                        raise LedgerConflictError(
                            f"conflicting borrow accrual {accrual_id}"
                        )
                    events.append(event)
                    continue
                self._connection.execute(
                    "INSERT INTO borrow_accruals VALUES (?, ?, ?, ?, ?, ?)", expected
                )
                self._insert_named_cash_event(
                    stable_id("cash", self.cohort_id, "borrow", accrual_id),
                    session,
                    "borrow",
                    -amount,
                    marks[ticker].fetched_at,
                    f"borrow {ticker} {accrual_id}",
                )
                self._update_accounting_summary(
                    cash_delta=-amount, borrow_cost_delta=amount
                )
                events.append(event)
        if not events:
            return LedgerEvent(
                stable_id("borrow", self.cohort_id, session, "none"),
                session,
                "borrow",
                Decimal("0.0000"),
                False,
                "no open short lots",
            )
        if len(events) == 1:
            return events[0]
        return LedgerEvent(
            stable_id("borrow_batch", self.cohort_id, session),
            session,
            "borrow",
            sum((event.amount for event in events), Decimal("0.0000")),
            any(event.flagged for event in events),
            "per-ticker short borrow ACT/365",
        )

    def accrue_financing(
        self,
        session: date,
        annual_rate: Decimal,
        processed_at: datetime | None = None,
    ) -> LedgerEvent:
        """Accrue debit-cash financing once; positive idle cash has a zero yield."""
        if (
            not isinstance(annual_rate, Decimal)
            or not annual_rate.is_finite()
            or annual_rate < 0
        ):
            raise ValueError("annual financing rate must be a non-negative Decimal")
        effective_at = processed_at or self._session_close_timestamp(session)
        self._require_timezone_aware(effective_at, "processed_at")
        accrual_id = stable_id("financing", self.cohort_id, session)
        with self.transaction():
            debit_balance = max(-self._accounting_summary()["cash"], Decimal("0"))
            amount = self._cost_model.financing_charge(debit_balance, annual_rate)
            event = LedgerEvent(
                accrual_id,
                session,
                "financing",
                amount,
                False,
                "debit financing ACT/365",
            )
            existing = self._connection.execute(
                "SELECT * FROM financing_accruals WHERE accrual_id = ?", (accrual_id,)
            ).fetchone()
            expected = (
                accrual_id,
                self._encode(session),
                self._encode(amount),
                self._encode(annual_rate),
                0,
            )
            if existing is not None:
                columns = ("accrual_id", "session", "amount", "annual_rate", "flagged")
                if any(
                    existing[column] != value
                    for column, value in zip(columns, expected)
                ):
                    raise LedgerConflictError(
                        f"conflicting financing accrual {accrual_id}"
                    )
                return event
            self._connection.execute(
                "INSERT INTO financing_accruals VALUES (?, ?, ?, ?, ?)", expected
            )
            self._insert_named_cash_event(
                stable_id("cash", self.cohort_id, "financing", accrual_id),
                session,
                "financing",
                -amount,
                effective_at,
                f"financing {accrual_id}",
            )
            self._update_accounting_summary(
                cash_delta=-amount, financing_cost_delta=amount
            )
            return event

    def apply_corporate_actions(
        self,
        session: date,
        actions: list[CorporateAction],
        processed_at: datetime | None = None,
    ) -> list[LedgerEvent]:
        """Apply verified actions once, or durably quarantine uncertain inputs."""
        processed = processed_at or max(
            (action.fetched_at for action in actions),
            default=self._session_close_timestamp(session),
        )
        self._require_timezone_aware(processed, "processed_at")
        if any(
            action.fetched_at.tzinfo is not None
            and action.fetched_at.utcoffset() is not None
            and processed < action.fetched_at
            for action in actions
        ):
            raise ValueError("processed_at precedes corporate action observation")
        events: list[LedgerEvent] = []
        with self.transaction():
            batch = tuple(actions)
            errors = self.corporate_action_batch_errors(session, batch, processed)
            if errors:
                for action in batch:
                    existing = self._connection.execute(
                        "SELECT * FROM corporate_actions WHERE action_id = ?",
                        (action.action_id,),
                    ).fetchone()
                    if existing is not None and not self._same_corporate_action(
                        existing, action
                    ):
                        self._record_corporate_action_conflict(
                            session, action, processed
                        )
                reason = self.reject_corporate_action_batch(
                    session,
                    batch,
                    tuple(sorted({action.ticker for action in batch})),
                    errors,
                    processed,
                )
                return [
                    LedgerEvent(
                        stable_id("action_invalid", self.cohort_id, session, reason),
                        session,
                        "corporate_action_invalid",
                        Decimal("0"),
                        True,
                        reason,
                    )
                ]
            unique_actions = {
                action.action_id: action
                for action in sorted(actions, key=lambda item: item.action_id)
            }
            for action in unique_actions.values():
                existing = self._connection.execute(
                    "SELECT * FROM corporate_actions WHERE action_id = ?",
                    (action.action_id,),
                ).fetchone()
                if existing is not None:
                    if not self._same_corporate_action(existing, action):
                        self._record_corporate_action_conflict(
                            session, action, processed
                        )
                        events.append(
                            self._quarantine_action(
                                session,
                                action,
                                "conflicting corporate action identity",
                                processed,
                            )
                        )
                    continue
                self._connection.execute(
                    "INSERT INTO corporate_actions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    self._corporate_action_values(action),
                )
                if action.session != session:
                    events.append(
                        self._quarantine_action(
                            session,
                            action,
                            "corporate action session mismatch",
                            processed,
                        )
                    )
                elif not action.verified:
                    events.append(
                        self._quarantine_action(
                            session, action, "unverified corporate action", processed
                        )
                    )
                elif action.action_type == "split":
                    invalid_reason = self._invalid_split_reason(action)
                    if invalid_reason is not None:
                        events.append(
                            self._quarantine_action(
                                session, action, invalid_reason, processed
                            )
                        )
                    else:
                        events.extend(self._apply_split(action, processed))
                elif action.action_type == "cash_dividend":
                    if (
                        action.cash_per_share is None
                        or not action.cash_per_share.is_finite()
                        or action.cash_per_share < 0
                    ):
                        events.append(
                            self._quarantine_action(
                                session, action, "invalid cash dividend", processed
                            )
                        )
                    else:
                        events.extend(self._apply_dividend(action, processed))
                else:  # defensive against malformed direct construction
                    events.append(
                        self._quarantine_action(
                            session, action, "unsupported corporate action", processed
                        )
                    )
        return events

    def invalidate_session(self, session: date, reason: str) -> None:
        with self.transaction():
            self._invalidate_session(session, reason, self._session_timestamp(session))

    def quarantine_ticker(self, ticker: str, reason: str) -> None:
        with self.transaction():
            self._quarantine_ticker(
                ticker, reason, self._session_timestamp(date(1970, 1, 1))
            )

    def apply_fill(
        self,
        intent: OrderIntent,
        fill: Fill,
        *,
        borrow_rate: Decimal | None = None,
    ) -> AccountState:
        """Apply one fully validated fill and every linked mutation atomically."""
        with self.transaction():
            stored_row = self._connection.execute(
                "SELECT * FROM order_intents WHERE intent_id = ?", (intent.intent_id,)
            ).fetchone()
            if stored_row is None:
                raise ValueError(f"unknown order intent {intent.intent_id}")
            stored_intent = self._intent_from_row(stored_row)
            existing_fill = self._connection.execute(
                "SELECT * FROM fills WHERE fill_id = ?", (fill.fill_id,)
            ).fetchone()
            if (
                intent != stored_intent
                and not (
                    existing_fill is not None
                    and self._same_intent_except_status(intent, stored_intent)
                )
                and not self._matches_split_adjusted_intent(intent, stored_intent)
            ):
                raise LedgerConflictError(
                    f"conflicting order_intents identity {intent.intent_id}"
                )
            self._validate_fill(stored_intent, fill)
            if existing_fill is not None:
                self._require_identical_fill(existing_fill, fill)
                stored_status = self._connection.execute(
                    "SELECT status FROM order_intents WHERE intent_id = ?",
                    (fill.intent_id,),
                ).fetchone()
                if stored_status is None or stored_status["status"] != "filled":
                    raise LedgerConflictError(
                        f"fill replay {fill.fill_id} has incomplete intent state"
                    )
            else:
                ticker = self._ticker_for_intent(stored_intent.intent_id)
                self.assert_session_tradeable(fill.session, ticker)
                if stored_intent.status != "pending":
                    raise ValueError(
                        f"order intent {stored_intent.intent_id} is not pending"
                    )
                if stored_intent.side == "short":
                    validate_new_short_borrow_rate(
                        borrow_rate, self._borrow_cost_reject_above
                    )
                duplicate_session = self._connection.execute(
                    "SELECT fill_id FROM fills WHERE intent_id = ? AND session = ?",
                    (stored_intent.intent_id, self._encode(fill.session)),
                ).fetchone()
                if duplicate_session is not None:
                    raise LedgerConflictError(
                        "conflicting fills for intent/session "
                        f"{stored_intent.intent_id}/{fill.session}"
                    )
                self._insert_fill(fill)
                self._insert_fill_costs(fill)
                realized_pnl = Decimal("0")
                if stored_intent.side in {"buy", "short"}:
                    self._insert_open_lot(stored_intent, fill, ticker)
                else:
                    realized_pnl = self._close_lots(stored_intent, fill, ticker)
                cash_delta = self._cash_delta(stored_intent.side, fill)
                self._insert_cash_event(fill, cash_delta)
                self._update_accounting_summary(
                    cash_delta=cash_delta,
                    realized_pnl_delta=realized_pnl,
                    slippage_cost_delta=fill.slippage,
                    commission_cost_delta=fill.commission,
                    other_fees_delta=fill.other_fees,
                )
                self._connection.execute(
                    "UPDATE order_intents SET status = 'filled' WHERE intent_id = ?",
                    (stored_intent.intent_id,),
                )
                self._connection.execute(
                    """INSERT INTO order_status_transitions
                    (transition_id, intent_id, status, occurred_at, reason)
                    VALUES (?, ?, ?, ?, ?)""",
                    (
                        stable_id(
                            "order_status", stored_intent.intent_id, fill.fill_id
                        ),
                        stored_intent.intent_id,
                        "filled",
                        self._encode(fill.processed_at),
                        "authoritative fill applied",
                    ),
                )
        return self.account_state()

    def mark(
        self,
        session: date,
        close_marks: dict[str, MarketBar],
        epoch_id: str,
        valuation_at: datetime,
    ) -> AccountSnapshot:
        """Persist exact raw marks and an immutable, reconciled snapshot."""
        self._require_timezone_aware(valuation_at, "valuation_at")
        with self.transaction():
            self._record_marks(session, close_marks, valuation_at)
            return self._snapshot_account(session, epoch_id, valuation_at)

    def record_marks(
        self,
        session: date,
        close_marks: dict[str, MarketBar],
        valuation_at: datetime,
        *,
        validated_at: datetime | None = None,
    ) -> None:
        """Persist raw closes without publishing a snapshot."""
        self._require_timezone_aware(valuation_at, "valuation_at")
        validation_time = validated_at or valuation_at
        self._require_timezone_aware(validation_time, "validated_at")
        with self.transaction():
            self._record_marks(session, close_marks, validation_time)

    def snapshot_account(
        self, session: date, epoch_id: str, valuation_at: datetime
    ) -> AccountSnapshot:
        """Publish a snapshot only from already persisted exact-session marks."""
        self._require_timezone_aware(valuation_at, "valuation_at")
        with self.transaction():
            return self._snapshot_account(session, epoch_id, valuation_at)

    def record_benchmark_observation(self, observation: BenchmarkObservation) -> None:
        """Persist one conflict-safe adjusted benchmark observation."""
        if observation.cohort_id != self.cohort_id:
            raise ValueError("benchmark cohort_id does not match ledger")
        if observation.return_basis != "total_return_adjusted":
            raise ValueError("benchmark return basis must be total_return_adjusted")
        if (
            not observation.close.is_finite()
            or observation.close <= 0
            or not observation.source
        ):
            raise ValueError("invalid benchmark observation")
        self._require_timezone_aware(observation.observed_at, "observed_at")
        with self.transaction():
            row = self._connection.execute(
                "SELECT * FROM benchmark_observations WHERE observation_id = ?",
                (observation.observation_id,),
            ).fetchone()
            values = self._benchmark_values(observation)
            if row is not None:
                columns = (
                    "observation_id",
                    "cohort_id",
                    "epoch_id",
                    "session",
                    "symbol",
                    "close",
                    "return_basis",
                    "source",
                    "observed_at",
                    "valid",
                    "invalid_reason",
                )
                if any(row[column] != value for column, value in zip(columns, values)):
                    raise LedgerConflictError(
                        f"conflicting benchmark identity {observation.observation_id}"
                    )
                return
            occupied = self._connection.execute(
                """SELECT observation_id FROM benchmark_observations
                   WHERE cohort_id = ? AND epoch_id = ? AND session = ? AND symbol = ?""",
                (
                    self.cohort_id,
                    observation.epoch_id,
                    self._encode(observation.session),
                    observation.symbol,
                ),
            ).fetchone()
            if occupied is not None:
                raise LedgerConflictError(
                    f"conflicting benchmark session {observation.symbol}/{observation.session}"
                )
            self._connection.execute(
                "INSERT INTO benchmark_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )

    def account_state(self) -> AccountState:
        """Return bounded cohort state, using a lot's entry until its first mark."""
        open_lots = self._open_lots(include_latest_mark=True)
        summary = self._accounting_summary()
        long_value = Decimal("0")
        short_liability = Decimal("0")
        margin_used = Decimal("0")
        for lot in open_lots:
            mark = (
                _decimal(lot["mark_close"])
                if lot["mark_close"] is not None
                else _decimal(lot["entry_price"])
            )
            value = _decimal(lot["open_qty"]) * mark
            if lot["direction"] == "long":
                long_value += value
            else:
                short_liability += value
                margin_used += _decimal(lot["margin_reserved"])
        net_equity = summary["cash"] + long_value - short_liability
        return AccountState(
            self.cohort_id,
            summary["cash"],
            long_value,
            short_liability,
            margin_used,
            summary["cash"] - margin_used,
            net_equity,
            max(summary["high_water_mark"], net_equity),
        )

    def _validate_fill(self, intent: OrderIntent, fill: Fill) -> None:
        if fill.intent_id != intent.intent_id:
            raise ValueError("fill intent_id does not match order intent")
        if (
            intent.price_rule == "next_session_open"
            and fill.session != intent.eligible_session
        ):
            raise ValueError("fill session does not match eligible session")
        if (
            intent.price_rule == "resting_stop"
            and fill.session < intent.eligible_session
        ):
            raise ValueError("resting stop fill session precedes eligible session")
        if fill.side != intent.side:
            raise ValueError("fill side does not match order intent")
        if fill.quantity != intent.requested_qty:
            raise ValueError("fill quantity does not match requested quantity")
        if fill.quantity <= 0:
            raise ValueError("fill quantity must be positive")
        for label, value, strictly_positive in (
            ("reference_price", fill.reference_price, True),
            ("fill_price", fill.fill_price, True),
            ("slippage", fill.slippage, False),
            ("commission", fill.commission, False),
            ("other_fees", fill.other_fees, False),
        ):
            if not value.is_finite() or (
                value <= 0 if strictly_positive else value < 0
            ):
                raise ValueError(f"fill {label} is invalid")

    def _ticker_for_intent(self, intent_id: str) -> str:
        rows = self._connection.execute(
            """SELECT DISTINCT s.ticker FROM intent_signals isg
               JOIN signals s ON s.signal_id = isg.signal_id
               WHERE isg.intent_id = ? ORDER BY s.ticker""",
            (intent_id,),
        ).fetchall()
        if len(rows) != 1:
            raise ValueError(
                f"intent {intent_id} must have exactly one unambiguous ticker provenance"
            )
        return rows[0]["ticker"]

    @staticmethod
    def _same_intent_except_status(supplied: OrderIntent, stored: OrderIntent) -> bool:
        return (
            supplied.intent_id,
            supplied.signal_ids,
            supplied.cohort_id,
            supplied.side,
            supplied.requested_qty,
            supplied.created_at,
            supplied.eligible_session,
            supplied.price_rule,
            supplied.stop_price,
            supplied.external_order_id,
        ) == (
            stored.intent_id,
            stored.signal_ids,
            stored.cohort_id,
            stored.side,
            stored.requested_qty,
            stored.created_at,
            stored.eligible_session,
            stored.price_rule,
            stored.stop_price,
            stored.external_order_id,
        )

    def _matches_split_adjusted_intent(
        self, supplied: OrderIntent, stored: OrderIntent
    ) -> bool:
        """Accept an idempotent replay of a pre-split exit intent, not a rewrite.

        Intent IDs intentionally remain tied to their pre-split signal/order
        payload.  The immutable adjustment rows provide the deterministic bridge
        to the current executable quantity and stop; a different replay still
        fails closed.
        """
        if (
            supplied.intent_id,
            supplied.signal_ids,
            supplied.cohort_id,
            supplied.side,
            supplied.created_at,
            supplied.eligible_session,
            supplied.price_rule,
            supplied.external_order_id,
        ) != (
            stored.intent_id,
            stored.signal_ids,
            stored.cohort_id,
            stored.side,
            stored.created_at,
            stored.eligible_session,
            stored.price_rule,
            stored.external_order_id,
        ):
            return False
        rows = self._connection.execute(
            """SELECT * FROM intent_action_adjustments WHERE intent_id = ?
               ORDER BY adjustment_sequence""",
            (stored.intent_id,),
        ).fetchall()
        if not rows:
            return False
        quantity = supplied.requested_qty
        stop = supplied.stop_price
        for row in rows:
            if quantity != int(row["original_qty"]):
                return False
            old_stop = (
                _decimal(row["original_stop_price"])
                if row["original_stop_price"] is not None
                else None
            )
            if stop != old_stop:
                return False
            quantity = int(row["adjusted_qty"])
            stop = (
                _decimal(row["adjusted_stop_price"])
                if row["adjusted_stop_price"] is not None
                else None
            )
        return quantity == stored.requested_qty and stop == stored.stop_price

    def _next_adjustment_sequence(self, intent_id: str) -> int:
        row = self._connection.execute(
            """SELECT COALESCE(MAX(adjustment_sequence), 0) AS latest
               FROM intent_action_adjustments WHERE intent_id = ?""",
            (intent_id,),
        ).fetchone()
        return int(row["latest"]) + 1

    def _insert_fill(self, fill: Fill) -> None:
        self._connection.execute(
            "INSERT INTO fills VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            self._fill_values(fill),
        )

    def _insert_fill_costs(self, fill: Fill) -> None:
        for cost_type, amount in (
            ("slippage", fill.slippage),
            ("commission", fill.commission),
            ("other_fees", fill.other_fees),
        ):
            self._connection.execute(
                "INSERT INTO fill_costs VALUES (?, ?, ?, ?)",
                (
                    stable_id("fill_cost", fill.fill_id, cost_type),
                    fill.fill_id,
                    cost_type,
                    self._encode(amount),
                ),
            )

    def _insert_open_lot(self, intent: OrderIntent, fill: Fill, ticker: str) -> None:
        direction = "long" if intent.side == "buy" else "short"
        margin = (
            fill.fill_price * fill.quantity * self._cost_model.margin_requirement
            if direction == "short"
            else Decimal("0")
        )
        self._connection.execute(
            "INSERT INTO lots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                stable_id("lot", fill.fill_id),
                fill.fill_id,
                self.cohort_id,
                ticker,
                direction,
                self._encode(fill.session),
                self._encode(fill.fill_price),
                fill.quantity,
                fill.quantity,
                self._encode(margin),
            ),
        )

    def _close_lots(self, intent: OrderIntent, fill: Fill, ticker: str) -> Decimal:
        direction = "long" if intent.side == "sell" else "short"
        ownership = self._connection.execute(
            """SELECT lot_id, quantity FROM exit_intent_lots
               WHERE intent_id = ? ORDER BY lot_id""",
            (intent.intent_id,),
        ).fetchall()
        allocations: list[tuple[sqlite3.Row, int]] = []
        if ownership:
            if sum(int(row["quantity"]) for row in ownership) != fill.quantity:
                raise ValueError("exit fill does not match owned lot quantity")
            for allocation in ownership:
                lot = self._connection.execute(
                    """SELECT * FROM lots WHERE lot_id = ? AND cohort_id = ?
                       AND ticker = ? AND direction = ?""",
                    (
                        allocation["lot_id"],
                        self.cohort_id,
                        ticker,
                        direction,
                    ),
                ).fetchone()
                close_qty = int(allocation["quantity"])
                if lot is None or int(lot["open_qty"]) < close_qty:
                    raise ValueError(
                        f"exit fill has insufficient owned lot {allocation['lot_id']}"
                    )
                allocations.append((lot, close_qty))
        else:
            lots = self._connection.execute(
                """SELECT * FROM lots WHERE cohort_id = ? AND ticker = ? AND direction = ?
                   AND open_qty > 0 ORDER BY opened_session, lot_id""",
                (self.cohort_id, ticker, direction),
            ).fetchall()
            if sum(int(lot["open_qty"]) for lot in lots) < fill.quantity:
                raise ValueError("close fill has insufficient matching open lots")
            remaining = fill.quantity
            for lot in lots:
                if remaining == 0:
                    break
                close_qty = min(remaining, int(lot["open_qty"]))
                allocations.append((lot, close_qty))
                remaining -= close_qty
        realized_total = Decimal("0")
        for lot, close_qty in allocations:
            lot_open_qty = int(lot["open_qty"])
            entry_price = _decimal(lot["entry_price"])
            realized = (
                (fill.fill_price - entry_price) * close_qty
                if direction == "long"
                else (entry_price - fill.fill_price) * close_qty
            )
            existing_margin = _decimal(lot["margin_reserved"])
            released_margin = (
                existing_margin * Decimal(close_qty) / Decimal(lot_open_qty)
            )
            self._connection.execute(
                "INSERT INTO lot_closures VALUES (?, ?, ?, ?, ?)",
                (
                    stable_id("lot_closure", fill.fill_id, lot["lot_id"]),
                    lot["lot_id"],
                    fill.fill_id,
                    close_qty,
                    self._encode(realized),
                ),
            )
            self._connection.execute(
                "UPDATE lots SET open_qty = ?, margin_reserved = ? WHERE lot_id = ?",
                (
                    lot_open_qty - close_qty,
                    self._encode(existing_margin - released_margin),
                    lot["lot_id"],
                ),
            )
            realized_total += realized
        return realized_total

    @staticmethod
    def _cash_delta(side: str, fill: Fill) -> Decimal:
        notional = fill.fill_price * fill.quantity
        fees = fill.commission + fill.other_fees
        return {
            "buy": -notional - fees,
            "sell": notional - fees,
            "short": notional - fees,
            "cover": -notional - fees,
        }[side]

    def _insert_cash_event(self, fill: Fill, amount: Decimal) -> None:
        self._insert_named_cash_event(
            stable_id("cash", self.cohort_id, "fill", fill.fill_id),
            fill.session,
            "fill",
            amount,
            fill.effective_at,
            f"fill {fill.fill_id}",
        )

    def _insert_named_cash_event(
        self,
        cash_event_id: str,
        session: date,
        event_type: str,
        amount: Decimal,
        effective_at: datetime,
        detail: str,
    ) -> None:
        """Write a caller-owned immutable cash row inside its active transaction."""
        self._connection.execute(
            "INSERT INTO cash_events VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                cash_event_id,
                self.cohort_id,
                self._encode(session),
                event_type,
                self._encode(amount),
                self._encode(effective_at),
                detail,
            ),
        )

    def _open_lots_for_direction(self, direction: str) -> list[sqlite3.Row]:
        if direction not in {"long", "short"}:
            raise ValueError("unsupported lot direction")
        return self._connection.execute(
            """SELECT * FROM lots WHERE cohort_id = ? AND direction = ? AND open_qty > 0
               ORDER BY ticker, opened_session, lot_id""",
            (self.cohort_id, direction),
        ).fetchall()

    def _validate_accrual_marks(
        self,
        session: date,
        close_marks: dict[str, MarketBar],
        tickers: list[str],
        valuation_at: datetime,
    ) -> dict[str, MarketBar]:
        if not tickers:
            return {}
        validated: dict[str, MarketBar] = {}
        for ticker in tickers:
            bar = close_marks.get(ticker)
            if bar is None or bar.ticker != ticker or bar.session != session:
                raise MissingMarkError(f"missing mark for {ticker}/{session}")
            self._require_timezone_aware(bar.fetched_at, "bar.fetched_at")
            validated[ticker] = bar
        try:
            validate_required_bars(
                {(ticker, session): bar for ticker, bar in validated.items()},
                set(tickers),
                session,
                valuation_at,
            )
        except BarValidationError as error:
            raise MissingMarkError(str(error)) from error
        return validated

    def _corporate_action_values(self, action: CorporateAction) -> tuple[object, ...]:
        return (
            action.action_id,
            action.ticker,
            self._encode(action.session),
            action.action_type,
            self._encode(action.ratio) if action.ratio is not None else None,
            self._encode(action.cash_per_share)
            if action.cash_per_share is not None
            else None,
            action.source,
            self._encode(action.fetched_at),
            self._encode(action.verified),
        )

    def _same_corporate_action(self, row: sqlite3.Row, action: CorporateAction) -> bool:
        columns = (
            "action_id",
            "ticker",
            "session",
            "action_type",
            "ratio",
            "cash_per_share",
            "source",
            "fetched_at",
            "verified",
        )
        return all(
            row[column] == value
            for column, value in zip(columns, self._corporate_action_values(action))
        )

    def _invalid_split_reason(self, action: CorporateAction) -> str | None:
        if action.ratio is None or not action.ratio.is_finite() or action.ratio <= 0:
            return "invalid split ratio"
        lots = self._connection.execute(
            "SELECT * FROM lots WHERE cohort_id = ? AND ticker = ? AND open_qty > 0",
            (self.cohort_id, action.ticker),
        ).fetchall()
        pending = self._pending_exit_rows_for_ticker(action.ticker)
        for row in [*lots, *pending]:
            quantity = int(
                row["open_qty"] if "open_qty" in row.keys() else row["requested_qty"]
            )
            if (
                Decimal(quantity) * action.ratio
                != (Decimal(quantity) * action.ratio).to_integral_value()
            ):
                return "split produces fractional share quantity"
        for intent in pending:
            allocations = self._connection.execute(
                """SELECT quantity FROM exit_intent_lots
                   WHERE intent_id = ? ORDER BY lot_id""",
                (intent["intent_id"],),
            ).fetchall()
            scaled_allocations = [
                Decimal(int(allocation["quantity"])) * action.ratio
                for allocation in allocations
            ]
            if any(
                quantity != quantity.to_integral_value()
                for quantity in scaled_allocations
            ):
                return "split produces fractional exit lot allocation"
            if allocations and sum(scaled_allocations) != (
                Decimal(int(intent["requested_qty"])) * action.ratio
            ):
                return "split exit allocation total mismatch"
        return None

    def _apply_split(
        self, action: CorporateAction, processed_at: datetime
    ) -> list[LedgerEvent]:
        assert action.ratio is not None
        lots = self._connection.execute(
            """SELECT * FROM lots WHERE cohort_id = ? AND ticker = ? AND open_qty > 0
               ORDER BY opened_session, lot_id""",
            (self.cohort_id, action.ticker),
        ).fetchall()
        for lot in lots:
            original_qty = int(lot["original_qty"])
            open_qty = int(lot["open_qty"])
            self._connection.execute(
                """UPDATE lots SET original_qty = ?, open_qty = ?, entry_price = ?,
                   margin_reserved = ? WHERE lot_id = ?""",
                (
                    int(Decimal(original_qty) * action.ratio),
                    int(Decimal(open_qty) * action.ratio),
                    self._encode(_decimal(lot["entry_price"]) / action.ratio),
                    # Margin is total reserved margin, so preserving economic
                    # exposure requires retaining (and explicitly rewriting) it.
                    self._encode(_decimal(lot["margin_reserved"])),
                    lot["lot_id"],
                ),
            )
            self._connection.execute(
                "INSERT INTO lot_action_applications VALUES (?, ?, ?, ?, ?)",
                (
                    stable_id("lot_action", action.action_id, lot["lot_id"]),
                    action.action_id,
                    lot["lot_id"],
                    self._encode(processed_at),
                    f"split ratio {format(action.ratio, 'f')}",
                ),
            )
        for row in self._pending_exit_rows_for_ticker(action.ticker):
            old_qty = int(row["requested_qty"])
            new_qty = int(Decimal(old_qty) * action.ratio)
            old_stop = (
                _decimal(row["stop_price"]) if row["stop_price"] is not None else None
            )
            new_stop = old_stop / action.ratio if old_stop is not None else None
            self._connection.execute(
                "UPDATE order_intents SET requested_qty = ?, stop_price = ? WHERE intent_id = ?",
                (
                    new_qty,
                    self._encode(new_stop) if new_stop is not None else None,
                    row["intent_id"],
                ),
            )
            self._connection.execute(
                """UPDATE exit_intent_lots
                   SET quantity = CAST(quantity * ? AS INTEGER)
                   WHERE intent_id = ?""",
                (format(action.ratio, "f"), row["intent_id"]),
            )
            self._connection.execute(
                "INSERT INTO intent_action_adjustments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    stable_id("intent_action", action.action_id, row["intent_id"]),
                    action.action_id,
                    row["intent_id"],
                    self._next_adjustment_sequence(row["intent_id"]),
                    old_qty,
                    new_qty,
                    self._encode(old_stop) if old_stop is not None else None,
                    self._encode(new_stop) if new_stop is not None else None,
                    self._encode(processed_at),
                ),
            )
        return [
            LedgerEvent(
                stable_id("split", self.cohort_id, action.action_id),
                action.session,
                "split",
                Decimal("0"),
                False,
                f"applied split {action.action_id}",
            )
        ]

    def _apply_dividend(
        self, action: CorporateAction, processed_at: datetime
    ) -> list[LedgerEvent]:
        assert action.cash_per_share is not None
        lots = self._connection.execute(
            """SELECT * FROM lots WHERE cohort_id = ? AND ticker = ? AND open_qty > 0
               ORDER BY direction, opened_session, lot_id""",
            (self.cohort_id, action.ticker),
        ).fetchall()
        events: list[LedgerEvent] = []
        for lot in lots:
            gross = quantize_cash(Decimal(lot["open_qty"]) * action.cash_per_share)
            amount = gross if lot["direction"] == "long" else -gross
            event_id = stable_id("dividend", action.action_id, lot["lot_id"])
            self._connection.execute(
                "INSERT INTO dividend_events VALUES (?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    action.action_id,
                    lot["lot_id"],
                    self._encode(action.session),
                    self._encode(amount),
                    lot["direction"],
                ),
            )
            self._insert_named_cash_event(
                stable_id("cash", self.cohort_id, "dividend", event_id),
                action.session,
                "dividend",
                amount,
                processed_at,
                f"dividend {action.action_id} {lot['lot_id']}",
            )
            self._update_accounting_summary(
                cash_delta=amount, dividend_cash_delta=amount
            )
            events.append(
                LedgerEvent(
                    event_id,
                    action.session,
                    "dividend",
                    amount,
                    False,
                    lot["direction"],
                )
            )
        return events

    def _pending_exit_rows_for_ticker(self, ticker: str) -> list[sqlite3.Row]:
        return self._connection.execute(
            """SELECT DISTINCT i.* FROM order_intents i
               JOIN intent_signals isg ON isg.intent_id = i.intent_id
               JOIN signals s ON s.signal_id = isg.signal_id
               WHERE i.cohort_id = ? AND i.status = 'pending'
                 AND i.side IN ('sell', 'cover') AND s.ticker = ?
                 AND NOT EXISTS (
                    SELECT 1 FROM intent_signals other_isg
                    JOIN signals other_s ON other_s.signal_id = other_isg.signal_id
                    WHERE other_isg.intent_id = i.intent_id AND other_s.ticker != ?
                 ) ORDER BY i.intent_id""",
            (self.cohort_id, ticker, ticker),
        ).fetchall()

    def _quarantine_action(
        self,
        session: date,
        action: CorporateAction,
        reason: str,
        processed_at: datetime,
    ) -> LedgerEvent:
        self._invalidate_session(session, reason, processed_at)
        self._quarantine_ticker(action.ticker, reason, processed_at)
        return LedgerEvent(
            stable_id(
                "action_invalid", self.cohort_id, session, action.action_id, reason
            ),
            session,
            "corporate_action_invalid",
            Decimal("0"),
            True,
            reason,
        )

    def _invalidate_session(
        self, session: date, reason: str, invalidated_at: datetime
    ) -> None:
        self._connection.execute(
            "INSERT OR IGNORE INTO session_invalidations VALUES (?, ?, ?, ?, ?)",
            (
                stable_id("session_invalid", self.cohort_id, session, reason),
                self.cohort_id,
                self._encode(session),
                reason,
                self._encode(invalidated_at),
            ),
        )

    def _quarantine_ticker(
        self, ticker: str, reason: str, quarantined_at: datetime
    ) -> None:
        self._connection.execute(
            "INSERT OR IGNORE INTO ticker_quarantines VALUES (?, ?, ?, ?, ?)",
            (
                stable_id("ticker_quarantine", self.cohort_id, ticker, reason),
                self.cohort_id,
                ticker,
                reason,
                self._encode(quarantined_at),
            ),
        )

    def _record_corporate_action_conflict(
        self, session: date, action: CorporateAction, detected_at: datetime
    ) -> None:
        values = self._corporate_action_values(action)
        payload = json.dumps(
            {
                "action_id": action.action_id,
                "ticker": action.ticker,
                "session": self._encode(action.session),
                "action_type": action.action_type,
                "ratio": self._encode(action.ratio),
                "cash_per_share": self._encode(action.cash_per_share),
                "source": action.source,
                "fetched_at": self._encode(action.fetched_at),
                "verified": action.verified,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        content_hash = stable_id("action_content", values)
        self._connection.execute(
            "INSERT OR IGNORE INTO corporate_action_conflicts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                stable_id(
                    "action_conflict", self.cohort_id, action.action_id, content_hash
                ),
                self.cohort_id,
                self._encode(session),
                action.ticker,
                action.action_id,
                content_hash,
                payload,
                self._encode(detected_at),
            ),
        )

    def _session_invalid_reason(
        self, session: date, ticker: str | None = None
    ) -> str | None:
        row = self._connection.execute(
            """SELECT reason FROM session_invalidations
               WHERE cohort_id = ? AND session = ? ORDER BY invalidation_id LIMIT 1""",
            (self.cohort_id, self._encode(session)),
        ).fetchone()
        if row is not None:
            return row["reason"]
        if ticker is not None:
            row = self._connection.execute(
                """SELECT reason FROM ticker_quarantines
                   WHERE cohort_id = ? AND ticker = ? ORDER BY quarantine_id LIMIT 1""",
                (self.cohort_id, ticker),
            ).fetchone()
            if row is not None:
                return row["reason"]
        return None

    @staticmethod
    def _session_timestamp(session: date) -> datetime:
        return datetime.combine(session, time.min, tzinfo=timezone.utc)

    @staticmethod
    def _session_close_timestamp(session: date) -> datetime:
        return session_close(session)

    def _open_lots(self, *, include_latest_mark: bool = False) -> list[sqlite3.Row]:
        mark_column = (
            """, (
                SELECT m.close FROM marks m
                WHERE m.cohort_id = l.cohort_id AND m.ticker = l.ticker
                  AND m.session >= l.opened_session
                ORDER BY m.session DESC, m.mark_id DESC LIMIT 1
            ) AS mark_close"""
            if include_latest_mark
            else ""
        )
        return self._connection.execute(
            """SELECT l.*"""
            + mark_column
            + """ FROM lots l WHERE cohort_id = ? AND open_qty > 0
                 ORDER BY ticker, direction, opened_session, lot_id""",
            (self.cohort_id,),
        ).fetchall()

    def _validate_marks(
        self,
        session: date,
        close_marks: dict[str, MarketBar],
        open_lots: list[sqlite3.Row],
        valuation_at: datetime,
    ) -> dict[str, MarketBar]:
        required_tickers = sorted({lot["ticker"] for lot in open_lots})
        validated: dict[str, MarketBar] = {}
        for ticker in required_tickers:
            bar = close_marks.get(ticker)
            if bar is None:
                raise MissingMarkError(f"missing mark for {ticker}/{session}")
            if bar.ticker != ticker or bar.session != session:
                raise MissingMarkError(f"untrusted mark for {ticker}/{session}")
            self._require_timezone_aware(bar.fetched_at, "bar.fetched_at")
            validated[ticker] = bar
        try:
            validate_required_bars(
                {(ticker, session): bar for ticker, bar in validated.items()},
                set(required_tickers),
                session,
                valuation_at,
            )
        except BarValidationError as error:
            raise MissingMarkError(str(error)) from error
        return validated

    def _insert_mark(self, ticker: str, bar: MarketBar) -> None:
        values = (
            stable_id("mark", self.cohort_id, ticker, bar.session),
            self.cohort_id,
            ticker,
            self._encode(bar.session),
            self._encode(bar.close),
            bar.source,
            self._encode(bar.fetched_at),
            0,
        )
        existing = self._connection.execute(
            "SELECT * FROM marks WHERE mark_id = ?", (values[0],)
        ).fetchone()
        columns = (
            "mark_id",
            "cohort_id",
            "ticker",
            "session",
            "close",
            "source",
            "observed_at",
            "adjusted",
        )
        if existing is not None:
            if any(existing[column] != value for column, value in zip(columns, values)):
                raise LedgerConflictError(f"conflicting marks identity {values[0]}")
            return
        self._connection.execute(
            "INSERT INTO marks VALUES (?, ?, ?, ?, ?, ?, ?, ?)", values
        )

    def _record_marks(
        self,
        session: date,
        close_marks: dict[str, MarketBar],
        valuation_at: datetime,
    ) -> None:
        open_lots = self._open_lots()
        marks_by_ticker = self._validate_marks(
            session, close_marks, open_lots, valuation_at
        )
        for ticker, bar in marks_by_ticker.items():
            self._insert_mark(ticker, bar)

    def _snapshot_account(
        self, session: date, epoch_id: str, valuation_at: datetime
    ) -> AccountSnapshot:
        open_lots = self._open_lots()
        tickers = sorted({lot["ticker"] for lot in open_lots})
        marks_by_ticker: dict[str, MarketBar] = {}
        for ticker in tickers:
            row = self._connection.execute(
                """SELECT * FROM marks WHERE cohort_id = ? AND ticker = ?
                   AND session = ?""",
                (self.cohort_id, ticker, self._encode(session)),
            ).fetchone()
            if row is None:
                raise MissingMarkError(f"missing persisted mark for {ticker}/{session}")
            close = _decimal(row["close"])
            marks_by_ticker[ticker] = MarketBar(
                ticker,
                session,
                close,
                close,
                close,
                close,
                row["source"],
                _datetime(row["observed_at"]),
                bool(row["adjusted"]),
            )
        invalid_reason = self._session_invalid_reason(session)
        if invalid_reason is None:
            for ticker in tickers:
                ticker_reason = self._session_invalid_reason(session, ticker)
                if ticker_reason is not None:
                    invalid_reason = f"quarantined {ticker}: {ticker_reason}"
                    break
        snapshot = self._build_snapshot(
            session,
            epoch_id,
            valuation_at,
            open_lots,
            marks_by_ticker,
            valid=invalid_reason is None,
            invalid_reason=invalid_reason or "",
        )
        existing = self._connection.execute(
            "SELECT * FROM account_snapshots WHERE snapshot_id = ?",
            (snapshot.snapshot_id,),
        ).fetchone()
        if existing is not None:
            self._require_identical_snapshot(existing, snapshot)
            return snapshot
        occupied = self._connection.execute(
            "SELECT snapshot_id FROM account_snapshots WHERE cohort_id = ? AND session = ?",
            (self.cohort_id, self._encode(session)),
        ).fetchone()
        if occupied is not None:
            raise LedgerConflictError(
                f"conflicting account_snapshots session {self.cohort_id}/{session}"
            )
        self._connection.execute(
            """INSERT INTO account_snapshots VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )""",
            self._snapshot_values(snapshot),
        )
        if snapshot.valid:
            self._update_accounting_summary(high_water_mark=snapshot.high_water_mark)
        return snapshot

    def _build_snapshot(
        self,
        session: date,
        epoch_id: str,
        valuation_at: datetime,
        open_lots: list[sqlite3.Row],
        marks_by_ticker: dict[str, MarketBar],
        *,
        valid: bool = True,
        invalid_reason: str = "",
    ) -> AccountSnapshot:
        summary = self._accounting_summary()
        long_market_value = Decimal("0")
        short_liability = Decimal("0")
        unrealized_pnl = Decimal("0")
        margin_used = Decimal("0")
        for lot in open_lots:
            quantity = Decimal(lot["open_qty"])
            entry = _decimal(lot["entry_price"])
            close = marks_by_ticker[lot["ticker"]].close
            if lot["direction"] == "long":
                long_market_value += quantity * close
                unrealized_pnl += (close - entry) * quantity
            else:
                short_liability += quantity * close
                unrealized_pnl += (entry - close) * quantity
                margin_used += _decimal(lot["margin_reserved"])
        net_equity = summary["cash"] + long_market_value - short_liability
        gross_exposure = long_market_value + short_liability
        net_exposure = long_market_value - short_liability
        slippage_cost = summary["slippage_cost"]
        commission_cost = summary["commission_cost"]
        other_fees = summary["other_fees"]
        borrow_cost = summary["borrow_cost"]
        financing_cost = summary["financing_cost"]
        dividend_cash = summary["dividend_cash"]
        cumulative_costs = (
            slippage_cost + commission_cost + other_fees + borrow_cost + financing_cost
        )
        gross_equity = net_equity + cumulative_costs
        assert net_equity == summary["cash"] + long_market_value - short_liability
        assert gross_equity - cumulative_costs == net_equity
        return AccountSnapshot(
            stable_id("snapshot", self.cohort_id, epoch_id, session),
            self.cohort_id,
            epoch_id,
            session,
            valuation_at,
            summary["cash"],
            long_market_value,
            short_liability,
            gross_exposure,
            net_exposure,
            margin_used,
            summary["cash"] - margin_used,
            summary["realized_pnl"],
            unrealized_pnl,
            gross_equity,
            slippage_cost,
            commission_cost,
            other_fees,
            borrow_cost,
            financing_cost,
            dividend_cash,
            net_equity,
            max(summary["high_water_mark"], net_equity),
            valid,
            invalid_reason,
        )

    def _accounting_summary(self) -> dict[str, Decimal]:
        row = self._connection.execute(
            "SELECT * FROM accounting_state WHERE cohort_id = ?", (self.cohort_id,)
        ).fetchone()
        if row is None:
            raise LedgerConflictError(
                f"missing accounting summary for {self.cohort_id}"
            )
        return {
            column: _decimal(row[column])
            for column in (
                "cash",
                "realized_pnl",
                "slippage_cost",
                "commission_cost",
                "other_fees",
                "borrow_cost",
                "financing_cost",
                "dividend_cash",
                "high_water_mark",
            )
        }

    def _update_accounting_summary(
        self,
        *,
        cash_delta: Decimal = Decimal("0"),
        realized_pnl_delta: Decimal = Decimal("0"),
        slippage_cost_delta: Decimal = Decimal("0"),
        commission_cost_delta: Decimal = Decimal("0"),
        other_fees_delta: Decimal = Decimal("0"),
        borrow_cost_delta: Decimal = Decimal("0"),
        financing_cost_delta: Decimal = Decimal("0"),
        dividend_cash_delta: Decimal = Decimal("0"),
        high_water_mark: Decimal | None = None,
    ) -> None:
        """Apply one exact-Decimal summary mutation inside the caller transaction.

        Task 5 accrual/dividend writers extend this hook with their audit-row
        insert and the matching cost/cash delta in the same transaction.
        """
        deltas = {
            "cash": cash_delta,
            "realized_pnl": realized_pnl_delta,
            "slippage_cost": slippage_cost_delta,
            "commission_cost": commission_cost_delta,
            "other_fees": other_fees_delta,
            "borrow_cost": borrow_cost_delta,
            "financing_cost": financing_cost_delta,
            "dividend_cash": dividend_cash_delta,
        }
        if any(not isinstance(value, Decimal) for value in deltas.values()):
            raise TypeError("accounting summary deltas must be Decimal")
        if high_water_mark is not None and not isinstance(high_water_mark, Decimal):
            raise TypeError("high_water_mark must be Decimal")
        summary = self._accounting_summary()
        next_high_water_mark = max(
            summary["high_water_mark"],
            high_water_mark
            if high_water_mark is not None
            else summary["high_water_mark"],
        )
        self._connection.execute(
            """UPDATE accounting_state SET cash = ?, realized_pnl = ?,
               slippage_cost = ?, commission_cost = ?, other_fees = ?,
               borrow_cost = ?, financing_cost = ?, dividend_cash = ?,
               high_water_mark = ? WHERE cohort_id = ?""",
            (
                self._encode(summary["cash"] + deltas["cash"]),
                self._encode(summary["realized_pnl"] + deltas["realized_pnl"]),
                self._encode(summary["slippage_cost"] + deltas["slippage_cost"]),
                self._encode(summary["commission_cost"] + deltas["commission_cost"]),
                self._encode(summary["other_fees"] + deltas["other_fees"]),
                self._encode(summary["borrow_cost"] + deltas["borrow_cost"]),
                self._encode(summary["financing_cost"] + deltas["financing_cost"]),
                self._encode(summary["dividend_cash"] + deltas["dividend_cash"]),
                self._encode(next_high_water_mark),
                self.cohort_id,
            ),
        )

    @staticmethod
    def _require_timezone_aware(value: datetime, label: str) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise MissingMarkError(f"{label} must be timezone-aware")

    @classmethod
    def _fill_values(cls, fill: Fill) -> tuple[object, ...]:
        return (
            fill.fill_id,
            fill.intent_id,
            fill.side,
            cls._encode(fill.session),
            cls._encode(fill.effective_at),
            cls._encode(fill.processed_at),
            cls._encode(fill.reference_price),
            cls._encode(fill.fill_price),
            fill.quantity,
            cls._encode(fill.slippage),
            cls._encode(fill.commission),
            cls._encode(fill.other_fees),
        )

    def _require_identical_fill(self, row: sqlite3.Row, fill: Fill) -> None:
        columns = (
            "fill_id",
            "intent_id",
            "side",
            "session",
            "effective_at",
            "processed_at",
            "reference_price",
            "fill_price",
            "quantity",
            "slippage",
            "commission",
            "other_fees",
        )
        if any(
            row[column] != value
            for column, value in zip(columns, self._fill_values(fill))
        ):
            raise LedgerConflictError(f"conflicting fills identity {fill.fill_id}")

    def _require_identical_snapshot(
        self, row: sqlite3.Row, snapshot: AccountSnapshot
    ) -> None:
        columns = (
            "snapshot_id",
            "cohort_id",
            "epoch_id",
            "session",
            "valuation_at",
            "cash",
            "long_market_value",
            "short_liability",
            "gross_exposure",
            "net_exposure",
            "margin_used",
            "buying_power",
            "realized_pnl",
            "unrealized_pnl",
            "gross_equity",
            "slippage_cost",
            "commission_cost",
            "other_fees",
            "borrow_cost",
            "financing_cost",
            "dividend_cash",
            "net_equity",
            "high_water_mark",
            "valid",
            "invalid_reason",
        )
        if any(
            row[column] != value
            for column, value in zip(columns, self._snapshot_values(snapshot))
        ):
            raise LedgerConflictError(
                f"conflicting account_snapshots identity {snapshot.snapshot_id}"
            )

    @classmethod
    def _snapshot_values(cls, snapshot: AccountSnapshot) -> tuple[object, ...]:
        return (
            snapshot.snapshot_id,
            snapshot.cohort_id,
            snapshot.epoch_id,
            cls._encode(snapshot.session),
            cls._encode(snapshot.valuation_at),
            cls._encode(snapshot.cash),
            cls._encode(snapshot.long_market_value),
            cls._encode(snapshot.short_liability),
            cls._encode(snapshot.gross_exposure),
            cls._encode(snapshot.net_exposure),
            cls._encode(snapshot.margin_used),
            cls._encode(snapshot.buying_power),
            cls._encode(snapshot.realized_pnl),
            cls._encode(snapshot.unrealized_pnl),
            cls._encode(snapshot.gross_equity),
            cls._encode(snapshot.slippage_cost),
            cls._encode(snapshot.commission_cost),
            cls._encode(snapshot.other_fees),
            cls._encode(snapshot.borrow_cost),
            cls._encode(snapshot.financing_cost),
            cls._encode(snapshot.dividend_cash),
            cls._encode(snapshot.net_equity),
            cls._encode(snapshot.high_water_mark),
            cls._encode(snapshot.valid),
            snapshot.invalid_reason,
        )

    @classmethod
    def _benchmark_values(cls, observation: BenchmarkObservation) -> tuple[object, ...]:
        return (
            observation.observation_id,
            observation.cohort_id,
            observation.epoch_id,
            cls._encode(observation.session),
            observation.symbol,
            cls._encode(observation.close),
            observation.return_basis,
            observation.source,
            cls._encode(observation.observed_at),
            cls._encode(observation.valid),
            observation.invalid_reason,
        )

    def _session_filters(
        self,
        table: str,
        start_session: date | None,
        end_session: date | None,
        epoch_id: str | None,
    ) -> tuple[list[str], list[object]]:
        if table not in {"account_snapshots", "benchmark_observations"}:
            raise ValueError("unapproved session table")
        clauses = ["1 = 1"]
        values: list[object] = []
        if start_session is not None:
            clauses.append("session >= ?")
            values.append(self._encode(start_session))
        if end_session is not None:
            clauses.append("session <= ?")
            values.append(self._encode(end_session))
        if epoch_id is not None:
            clauses.append("epoch_id = ?")
            values.append(epoch_id)
        return clauses, values

    def _intent_from_row(self, row: sqlite3.Row) -> OrderIntent:
        signal_ids = tuple(
            item[0]
            for item in self._connection.execute(
                "SELECT signal_id FROM intent_signals WHERE intent_id = ? "
                "ORDER BY signal_order, signal_id",
                (row["intent_id"],),
            )
        )
        return OrderIntent(
            row["intent_id"],
            signal_ids,
            row["cohort_id"],
            row["side"],
            row["requested_qty"],
            _datetime(row["created_at"]),
            _date(row["eligible_session"]),
            row["price_rule"],
            row["status"],
            _decimal(row["stop_price"]) if row["stop_price"] is not None else None,
            row["external_order_id"],
        )

    @staticmethod
    def _signal_from_row(row: sqlite3.Row) -> SignalRecord:
        return SignalRecord(
            row["signal_id"],
            row["epoch_id"],
            row["policy_id"],
            row["event_key"],
            row["strategy"],
            row["ticker"],
            row["direction"],
            _datetime(row["event_at"]) if row["event_at"] is not None else None,
            _datetime(row["observed_at"]),
            _date(row["reference_session"]),
            _decimal(row["reference_close"]),
            _datetime(row["decision_at"]),
            row["evidence_hash"],
        )

    @staticmethod
    def _fill_from_row(row: sqlite3.Row) -> Fill:
        return Fill(
            row["fill_id"],
            row["intent_id"],
            row["side"],
            _date(row["session"]),
            _datetime(row["effective_at"]),
            _datetime(row["processed_at"]),
            _decimal(row["reference_price"]),
            _decimal(row["fill_price"]),
            row["quantity"],
            _decimal(row["slippage"]),
            _decimal(row["commission"]),
            _decimal(row["other_fees"]),
        )

    @staticmethod
    def _snapshot_from_row(row: sqlite3.Row) -> AccountSnapshot:
        decimal_columns = (
            "cash",
            "long_market_value",
            "short_liability",
            "gross_exposure",
            "net_exposure",
            "margin_used",
            "buying_power",
            "realized_pnl",
            "unrealized_pnl",
            "gross_equity",
            "slippage_cost",
            "commission_cost",
            "other_fees",
            "borrow_cost",
            "financing_cost",
            "dividend_cash",
            "net_equity",
            "high_water_mark",
        )
        values = [_decimal(row[column]) for column in decimal_columns]
        return AccountSnapshot(
            row["snapshot_id"],
            row["cohort_id"],
            row["epoch_id"],
            _date(row["session"]),
            _datetime(row["valuation_at"]),
            *values,
            bool(row["valid"]),
            row["invalid_reason"],
        )

    @staticmethod
    def _benchmark_from_row(row: sqlite3.Row) -> BenchmarkObservation:
        return BenchmarkObservation(
            row["observation_id"],
            row["cohort_id"],
            row["epoch_id"],
            _date(row["session"]),
            row["symbol"],
            _decimal(row["close"]),
            row["return_basis"],
            row["source"],
            _datetime(row["observed_at"]),
            bool(row["valid"]),
            row["invalid_reason"],
        )
