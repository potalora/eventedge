"""10-phase session lifecycle, execution context binding, and invalidation."""

from __future__ import annotations

from datetime import date, datetime
import json
import sqlite3
from typing import Callable

from tradingagents.strategies.execution import stable_id

from .connection import (
    LedgerConflictError,
    _T,
    _datetime,
    _encode,
    _require_timezone_aware,
    _session_timestamp,
)


class SessionLifecycleMixin:
    """Session execution context, phase digest chain, staging, and invalidation."""

    def phase_completed(self, session: date, phase: str) -> bool:
        """Whether a session phase was durably committed."""
        row = self._connection.execute(
            """SELECT completed_at FROM session_phases
               WHERE cohort_id = ? AND session = ? AND phase = ?""",
            (self.cohort_id, _encode(session), phase),
        ).fetchone()
        return row is not None and row["completed_at"] is not None

    def session_execution_context(self, session: date) -> dict[str, object] | None:
        """Return the immutable epoch/input binding for a started session."""
        row = self._connection.execute(
            """SELECT * FROM session_execution_contexts
               WHERE cohort_id = ? AND session = ?""",
            (self.cohort_id, _encode(session)),
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
        execution_policy_binding = self.read_policy_session_context(
            session, binding_kind="execution"
        )
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
            return [dict(r) for r in self._connection.execute(sql, parameters)]

        receivables = [dict(r) for r in self._dividend_receivable_rows(session)]
        payment_state: dict[str, list[dict[str, object]]] = {}
        for table in ("dividend_payment_terms", "dividend_payment_observations"):
            exists = self._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            payment_state[table] = (
                rows(
                    f"SELECT p.* FROM {table} p WHERE EXISTS ("
                    "SELECT 1 FROM dividend_events e JOIN dividend_receivables r "
                    "ON r.event_id = e.dividend_event_id WHERE e.action_id = p.action_id "
                    "AND r.cohort_id = ? "
                    "AND (r.settled_session IS NULL OR r.settled_session = ?)) "
                    "ORDER BY p.action_id",
                    (self.cohort_id, session.isoformat()),
                )
                if receivables and exists is not None
                else []
            )

        return {
            "account": account.__dict__,
            "dividend_receivables": receivables,
            **payment_state,
            "execution_policy_binding": (
                None
                if execution_policy_binding is None
                else {
                    "epoch_id": execution_policy_binding["epoch_id"],
                    "policy_version": execution_policy_binding["policy_version"],
                    "policy_config_json": execution_policy_binding[
                        "policy_config_json"
                    ],
                    "policy_config_digest": execution_policy_binding[
                        "policy_config_digest"
                    ],
                    "context_json": execution_policy_binding["context_json"],
                    "context_digest": execution_policy_binding["context_digest"],
                    "payload_json": execution_policy_binding["payload_json"],
                    "payload_digest": execution_policy_binding["payload_digest"],
                    "bound_at": execution_policy_binding["bound_at"],
                }
            ),
            "due_intents": due_intents,
            "open_lots": self.open_exit_positions(),
            "applied_lot_actions": rows(
                "SELECT a.action_id AS action_id, a.lot_id AS lot_id "
                "FROM lot_action_applications a "
                "JOIN lots l ON l.lot_id = a.lot_id "
                "WHERE l.cohort_id = ? AND l.open_qty > 0 "
                "UNION "
                "SELECT e.action_id AS action_id, r.lot_id AS lot_id "
                "FROM dividend_receivables r "
                "JOIN dividend_events e ON e.dividend_event_id = r.event_id "
                "JOIN lots l ON l.lot_id = r.lot_id "
                "WHERE r.cohort_id = ? AND l.open_qty > 0 "
                "ORDER BY action_id, lot_id",
                (self.cohort_id, self.cohort_id),
            ),
            "exit_allocations": [dict(r) for r in allocations],
            "session_cash_events": rows(
                "SELECT * FROM cash_events WHERE cohort_id = ? AND session = ? "
                "ORDER BY cash_event_id",
                (self.cohort_id, _encode(session)),
            ),
            "session_actions": rows(
                "SELECT * FROM corporate_actions WHERE session = ? ORDER BY action_id",
                (_encode(session),),
            ),
            "session_marks": rows(
                "SELECT * FROM marks WHERE session = ? ORDER BY mark_id",
                (_encode(session),),
            ),
            "session_benchmarks": rows(
                "SELECT * FROM benchmark_observations WHERE session = ? "
                "ORDER BY observation_id",
                (_encode(session),),
            ),
            "session_snapshots": rows(
                "SELECT * FROM account_snapshots WHERE cohort_id = ? AND session = ? "
                "ORDER BY snapshot_id",
                (self.cohort_id, _encode(session)),
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
        _require_timezone_aware(bound_at, "bound_at")
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
                (self.cohort_id, _encode(session)),
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
                    _encode(session),
                    epoch_id,
                    input_digest,
                    market_digest,
                    config_digest,
                    borrow_digest,
                    tickers_json,
                    economic_inputs_json,
                    provenance_json,
                    provenance_digest,
                    _encode(bound_at),
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
        _require_timezone_aware(processed_at, "processed_at")
        if not phase:
            raise ValueError("phase is required")
        with self.transaction():
            row = self._connection.execute(
                """SELECT completed_at, pre_state_digest, post_state_digest
                   FROM session_phases
                   WHERE cohort_id = ? AND session = ? AND phase = ?""",
                (self.cohort_id, _encode(session), phase),
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
                        _encode(session),
                        phase,
                        _encode(processed_at),
                        expected_pre_state_digest,
                    ),
                )
            else:
                self._connection.execute(
                    """UPDATE session_phases
                       SET started_at = ?, pre_state_digest = ?
                       WHERE session_phase_id = ?""",
                    (_encode(processed_at), expected_pre_state_digest, phase_id),
                )
            value = operation()
            post_state_digest = self.execution_governed_state_digest(session)
            self._connection.execute(
                """UPDATE session_phases
                   SET completed_at = ?, post_state_digest = ?
                   WHERE session_phase_id = ?""",
                (_encode(processed_at), post_state_digest, phase_id),
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
                   FROM session_phases
                   WHERE cohort_id = ? AND session = ? AND phase = ?""",
                (self.cohort_id, _encode(session), phase),
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
            (self.cohort_id, _encode(session)),
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
            (self.cohort_id, _encode(session), epoch_id, policy_id),
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
        _require_timezone_aware(completed_at, "completed_at")
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
                    _encode(session),
                    epoch_id,
                    policy_id,
                    _encode(completed_at),
                    post_state_digest,
                ),
            )
            return True, value

    def session_is_valid(self, session: date, ticker: str | None = None) -> bool:
        """Public fail-closed state query for policy/entry execution callers."""
        return self._session_invalid_reason(session, ticker) is None

    def session_invalid_reason(self, session: date, ticker: str | None = None) -> str:
        return self._session_invalid_reason(session, ticker) or ""

    def invalidate_session(self, session: date, reason: str) -> None:
        with self.transaction():
            self._invalidate_session(session, reason, _session_timestamp(session))

    def invalidate_session_and_cancel_due(
        self, session: date, reason: str, processed_at: datetime
    ) -> None:
        """Persist invalidation and terminalize stranded next-open intents."""
        _require_timezone_aware(processed_at, "processed_at")
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
                _encode(session),
                0,
                reason,
                _encode(processed_at),
                _encode(processed_at),
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
            context = self.session_execution_context(session)
            bound_intents = (
                None
                if context is None
                else {
                    item["intent"]["intent_id"]
                    for item in json.loads(str(context["economic_inputs_json"]))[
                        "starting_state"
                    ]["due_intents"]
                }
            )
            for row in due:
                if row["price_rule"] == "resting_stop" and (
                    bound_intents is None or row["intent_id"] in bound_intents
                ):
                    continue
                self._terminalize_intent(
                    row["intent_id"],
                    "cancelled",
                    processed_at,
                    f"session invalid: {reason}",
                )

    def record_session_complete(self, session: date, processed_at: datetime) -> None:
        """Mark a valid session complete inside the caller's snapshot transaction."""
        _require_timezone_aware(processed_at, "processed_at")
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
                _encode(session),
                _encode(processed_at),
                _encode(processed_at),
            ),
        )

    def assert_session_tradeable(
        self, session: date, ticker: str | None = None
    ) -> None:
        reason = self._session_invalid_reason(session, ticker)
        if reason is not None:
            target = f"/{ticker}" if ticker is not None else ""
            raise ValueError(f"session {session}{target} is invalid: {reason}")

    def _invalidate_session(
        self, session: date, reason: str, invalidated_at: datetime
    ) -> None:
        self._connection.execute(
            "INSERT OR IGNORE INTO session_invalidations VALUES (?, ?, ?, ?, ?)",
            (
                stable_id("session_invalid", self.cohort_id, session, reason),
                self.cohort_id,
                _encode(session),
                reason,
                _encode(invalidated_at),
            ),
        )

    def _session_invalid_reason(
        self, session: date, ticker: str | None = None
    ) -> str | None:
        row = self._connection.execute(
            """SELECT reason FROM session_invalidations
               WHERE cohort_id = ? AND session = ? ORDER BY invalidation_id LIMIT 1""",
            (self.cohort_id, _encode(session)),
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
