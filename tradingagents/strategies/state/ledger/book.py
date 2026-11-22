"""Accounting book: signals, intents, FIFO fills, accruals, marks, and snapshots."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import sqlite3
from typing import Mapping

from tradingagents.strategies.execution import (
    AccountSnapshot,
    AccountState,
    BenchmarkObservation,
    Fill,
    LedgerEvent,
    MarketBar,
    OrderIntent,
    SignalRecord,
    stable_id,
)
from tradingagents.strategies.execution.cost_model import (
    quantize_cash,
    validate_new_short_borrow_rate,
)
from tradingagents.strategies.execution.price_source import (
    BarValidationError,
    validate_required_bars,
)
from tradingagents.strategies.orchestration.trading_calendar import previous_session

from .connection import (
    LedgerConflictError,
    MissingMarkError,
    TradeProjectionRecord,
    _date,
    _datetime,
    _decimal,
    _encode,
    _require_timezone_aware,
    _session_close_timestamp,
)


_CONTEXT_ENVELOPE_MARKER = "__eventedge_signal_context_v2__"


def _unpack_signal_context(
    raw_json: str | None,
) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    if raw_json is None:
        return None, None
    parsed = json.loads(raw_json)
    if isinstance(parsed, dict) and parsed.get(_CONTEXT_ENVELOPE_MARKER) is True:
        candidate_ctx = parsed.get("candidate_context")
        journal_payload = parsed.get("journal_payload")
        return (
            candidate_ctx if isinstance(candidate_ctx, dict) else {},
            journal_payload if isinstance(journal_payload, dict) else {},
        )
    return (parsed if isinstance(parsed, dict) else {}), {}


class LedgerBookMixin:
    """Signals, order intents, FIFO fills, accruals, marks, benchmarks, snapshots."""

    # ------------------------------------------------------------------
    # Signals & Committee Decisions
    # ------------------------------------------------------------------

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
        """Atomically persist a signal and its immutable context/journal payload.

        Stored directly in ``signals`` and ``signal_candidate_contexts`` inside
        ``generation.sqlite3`` (no outbox table or per-cohort ``.jsonl`` mirror).
        """
        _require_timezone_aware(queued_at, "queued_at")
        envelope = {
            _CONTEXT_ENVELOPE_MARKER: True,
            "candidate_context": candidate_context or {},
            "journal_payload": payload,
        }
        context_json = json.dumps(
            envelope, sort_keys=True, separators=(",", ":"), default=str
        )
        context_hash = stable_id("signal_candidate_context", context_json)
        with self.transaction():
            self._record_signal(signal)
            existing = self._connection.execute(
                "SELECT * FROM signal_candidate_contexts WHERE signal_id = ?",
                (signal.signal_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["payload_json"] != context_json
                    or existing["payload_hash"] != context_hash
                ):
                    raise LedgerConflictError(
                        f"conflicting candidate/journal payload for signal {signal.signal_id}"
                    )
                return
            self._connection.execute(
                "INSERT INTO signal_candidate_contexts VALUES (?, ?, ?, ?)",
                (
                    signal.signal_id,
                    context_json,
                    context_hash,
                    _encode(queued_at),
                ),
            )

    def signal_observation(
        self, signal_id: str
    ) -> tuple[SignalRecord, dict[str, object], dict[str, object]] | None:
        """Read one immutable first observation and its candidate/journal context."""
        row = self._connection.execute(
            """SELECT s.*, c.payload_json AS context_json
               FROM signals s
               JOIN signal_candidate_contexts c ON c.signal_id = s.signal_id
               WHERE s.signal_id = ?""",
            (signal_id,),
        ).fetchone()
        if row is None:
            return None
        candidate_ctx, journal_payload = _unpack_signal_context(row["context_json"])
        return (
            self._signal_from_row(row),
            candidate_ctx if candidate_ctx is not None else {},
            journal_payload if journal_payload is not None else {},
        )

    def read_signal_observations(
        self,
        start_session: date | None = None,
        end_session: date | None = None,
        epoch_id: str | None = None,
        policy_id: str | None = None,
    ) -> list[tuple[SignalRecord, dict[str, object] | None, dict[str, object] | None]]:
        """Batch-read signals joined with candidate/journal context in ONE query."""
        clauses = ["1 = 1"]
        values: list[object] = []
        if start_session is not None:
            clauses.append("s.reference_session >= ?")
            values.append(_encode(start_session))
        if end_session is not None:
            clauses.append("s.reference_session <= ?")
            values.append(_encode(end_session))
        if epoch_id is not None:
            clauses.append("s.epoch_id = ?")
            values.append(epoch_id)
        if policy_id is not None:
            clauses.append("s.policy_id = ?")
            values.append(policy_id)
        rows = self._connection.execute(
            "SELECT s.*, c.payload_json AS context_json "
            "FROM signals s "
            "LEFT JOIN signal_candidate_contexts c ON c.signal_id = s.signal_id "
            "WHERE "
            + " AND ".join(clauses)
            + " ORDER BY s.reference_session, s.signal_id",
            values,
        ).fetchall()
        results: list[
            tuple[SignalRecord, dict[str, object] | None, dict[str, object] | None]
        ] = []
        for row in rows:
            candidate_ctx, journal_payload = _unpack_signal_context(row["context_json"])
            results.append(
                (self._signal_from_row(row), candidate_ctx, journal_payload)
            )
        return results

    def committee_decision(
        self, session: date, epoch_id: str, policy_id: str
    ) -> dict | None:
        row = self._connection.execute(
            """SELECT payload_json, payload_sha256 FROM committee_decisions
               WHERE cohort_id = ? AND session = ? AND epoch_id = ? AND policy_id = ?""",
            (self.cohort_id, session.isoformat(), epoch_id, policy_id),
        ).fetchone()
        if row is None:
            return None
        actual_sha = hashlib.sha256(row["payload_json"].encode("utf-8")).hexdigest()
        if actual_sha != row["payload_sha256"]:
            raise LedgerConflictError(
                f"committee_decisions digest mismatch for {self.cohort_id}/{session}"
            )
        return json.loads(row["payload_json"])

    def record_committee_decision(
        self, session: date, epoch_id: str, policy_id: str, payload: dict
    ) -> None:
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        created_at = datetime.now(timezone.utc).isoformat()
        with self.transaction():
            old = self.committee_decision(session, epoch_id, policy_id)
            if old is not None:
                if old != json.loads(encoded):
                    raise LedgerConflictError(
                        "committee decision changed after acquisition"
                    )
                return
            self._connection.execute(
                """INSERT INTO committee_decisions (
                       cohort_id, session, epoch_id, policy_id,
                       payload_json, payload_sha256, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    self.cohort_id,
                    session.isoformat(),
                    epoch_id,
                    policy_id,
                    encoded,
                    digest,
                    created_at,
                ),
            )

    # ------------------------------------------------------------------
    # Order Intents & Open Positions
    # ------------------------------------------------------------------

    def stage_intent(self, intent: OrderIntent) -> None:
        if intent.cohort_id != self.cohort_id:
            raise ValueError("intent cohort_id does not match ledger cohort_id")
        with self.transaction():
            if not intent.signal_ids:
                raise ValueError("intent must reference at least one signal")
            if len(set(intent.signal_ids)) != len(intent.signal_ids):
                raise ValueError("intent signal_ids must be unique")
            placeholders = ", ".join("?" for _ in intent.signal_ids)
            provenance_rows = self._connection.execute(
                "SELECT signal_id, epoch_id, policy_id FROM signals WHERE signal_id IN "
                f"({placeholders})",
                intent.signal_ids,
            ).fetchall()
            provenance_by_id = {row["signal_id"]: row for row in provenance_rows}
            missing = [
                sid for sid in intent.signal_ids if sid not in provenance_by_id
            ]
            if missing:
                raise ValueError(
                    "intent references missing signal IDs: " + ", ".join(missing)
                )
            if (
                len({provenance_by_id[s]["epoch_id"] for s in intent.signal_ids}) != 1
            ):
                raise ValueError("intent signals must share one epoch_id")
            if (
                len({provenance_by_id[s]["policy_id"] for s in intent.signal_ids}) != 1
            ):
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
                    """INSERT INTO intent_signals (cohort_id, intent_id, signal_id, signal_order)
                       VALUES (?, ?, ?, ?)""",
                    (self.cohort_id, intent.intent_id, signal_id, signal_order),
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
            (self.cohort_id, _encode(session), _encode(session)),
        ).fetchall()

    def pending_intents(self, session: date) -> list[OrderIntent]:
        return [self._intent_from_row(row) for row in self._due_intent_rows(session)]

    def reject_intent(
        self, intent_id: str, occurred_at: datetime, reason: str
    ) -> OrderIntent:
        return self._terminalize_intent(intent_id, "rejected", occurred_at, reason)

    def cancel_intent(
        self, intent_id: str, occurred_at: datetime, reason: str
    ) -> OrderIntent:
        return self._terminalize_intent(intent_id, "cancelled", occurred_at, reason)

    def overdue_cancellation_inputs(
        self, session: date, *, intent_ids=None
    ) -> dict[str, object]:
        """Bounded overdue identities/associations and transitions, without effects."""
        parameters: list[object] = [self.cohort_id]
        if intent_ids is None:
            predicate = (
                "status='pending' AND price_rule='next_session_open' "
                "AND eligible_session < ?"
            )
            parameters.append(_encode(session))
        else:
            if len(intent_ids) > 10000 or len(set(intent_ids)) != len(intent_ids):
                raise LedgerConflictError("overdue cancellation inputs exceed bounds")
            predicate = "intent_id IN (" + ",".join("?" for _ in intent_ids) + ")"
            parameters.extend(intent_ids)
        rows = self._connection.execute(
            "SELECT * FROM order_intents WHERE cohort_id = ? AND "
            + predicate
            + " ORDER BY intent_id LIMIT 10001",
            parameters,
        ).fetchall()
        if len(rows) > 10000 or (
            intent_ids is not None and len(rows) != len(intent_ids)
        ):
            raise LedgerConflictError(
                "overdue cancellation inputs exceed bounds or are missing"
            )
        intents, transitions, signals, associations, allocations = [], [], [], [], []
        for row in rows:
            intents.append(dict(row))
            transitions.extend(
                dict(r)
                for r in self._connection.execute(
                    "SELECT * FROM order_status_transitions WHERE intent_id = ? "
                    "ORDER BY transition_id LIMIT 10001",
                    (row["intent_id"],),
                )
            )
            associated = self._connection.execute(
                "SELECT s.* FROM intent_signals a JOIN signals s ON s.signal_id = a.signal_id "
                "WHERE a.intent_id = ? ORDER BY s.signal_id LIMIT 10001",
                (row["intent_id"],),
            ).fetchall()
            references = self._connection.execute(
                "SELECT * FROM intent_signals WHERE intent_id = ? "
                "ORDER BY signal_id LIMIT 10001",
                (row["intent_id"],),
            ).fetchall()
            if not associated or [r["signal_id"] for r in associated] != [
                r["signal_id"] for r in references
            ]:
                raise LedgerConflictError(
                    "overdue cancellation signal identity missing"
                )
            associations.extend(dict(r) for r in references)
            allocations.extend(
                dict(r)
                for r in self._connection.execute(
                    "SELECT * FROM exit_intent_lots WHERE intent_id = ? "
                    "ORDER BY lot_id LIMIT 10001",
                    (row["intent_id"],),
                )
            )
            signals.append(
                {
                    "intent_id": row["intent_id"],
                    "signals": [self._signal_from_row(r).__dict__ for r in associated],
                }
            )
            if (
                len(transitions) > 10000
                or len(associations) > 10000
                or len(allocations) > 10000
                or sum(len(r["signals"]) for r in signals) > 10000
            ):
                raise LedgerConflictError(
                    "overdue cancellation associations exceed bounds"
                )
        return {
            "intents": intents,
            "transitions": transitions,
            "signals": signals,
            "associations": associations,
            "exit_allocations": allocations,
        }

    def cancel_overdue_next_open_intents(
        self, session: date, occurred_at: datetime
    ) -> tuple[OrderIntent, ...]:
        """Cancel next-open intents whose one eligible execution session passed."""
        _require_timezone_aware(occurred_at, "occurred_at")
        with self.transaction():
            rows = self._connection.execute(
                """SELECT intent_id FROM order_intents
                   WHERE cohort_id = ? AND status = 'pending'
                     AND price_rule = 'next_session_open' AND eligible_session < ?
                   ORDER BY eligible_session, created_at, intent_id""",
                (self.cohort_id, _encode(session)),
            ).fetchall()
            return tuple(
                self._terminalize_intent(
                    str(row["intent_id"]),
                    "cancelled",
                    occurred_at,
                    "missed exact eligible session",
                )
                for row in rows
            )

    def _terminalize_intent(
        self,
        intent_id: str,
        status: str,
        occurred_at: datetime,
        reason: str,
    ) -> OrderIntent:
        _require_timezone_aware(occurred_at, "occurred_at")
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
                    or transition["occurred_at"] != _encode(occurred_at)
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
                    _encode(occurred_at),
                    reason,
                ),
            )
            updated = self._connection.execute(
                "SELECT * FROM order_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            assert updated is not None
            return self._intent_from_row(updated)

    def intent(self, intent_id: str) -> OrderIntent | None:
        row = self._connection.execute(
            "SELECT * FROM order_intents WHERE intent_id = ? AND cohort_id = ?",
            (intent_id, self.cohort_id),
        ).fetchone()
        return self._intent_from_row(row) if row is not None else None

    def signals_for_intent(self, intent_id: str) -> tuple[SignalRecord, ...]:
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

    # ------------------------------------------------------------------
    # Read Queries & Trade Projections
    # ------------------------------------------------------------------

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
            "SELECT * FROM benchmark_observations WHERE "
            + " AND ".join(clauses)
            + " ORDER BY session, observation_id",
            values,
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
            values.append(_encode(start_session))
        if end_session is not None:
            clauses.append("reference_session <= ?")
            values.append(_encode(end_session))
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
        clauses = ["f.cohort_id = ?"]
        values: list[object] = [self.cohort_id]
        if start_session is not None:
            clauses.append("f.session >= ?")
            values.append(_encode(start_session))
        if end_session is not None:
            clauses.append("f.session <= ?")
            values.append(_encode(end_session))
        if epoch_id is not None:
            clauses.append(
                "EXISTS (SELECT 1 FROM intent_signals isg JOIN signals s "
                "ON s.signal_id = isg.signal_id WHERE isg.intent_id = "
                "f.intent_id AND s.epoch_id = ?)"
            )
            values.append(epoch_id)
        rows = self._connection.execute(
            "SELECT f.* FROM fills f WHERE "
            + " AND ".join(clauses)
            + " ORDER BY f.session, f.fill_id",
            values,
        ).fetchall()
        return [self._fill_from_row(row) for row in rows]

    def opening_cash(self) -> Decimal:
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

    # ------------------------------------------------------------------
    # Borrow & Financing Accruals
    # ------------------------------------------------------------------

    def accrue_borrow(
        self,
        session: date,
        close_marks: dict[str, MarketBar],
        rates: dict[str, Decimal | None],
        valuation_at: datetime,
        *,
        carried_calendar: bool = False,
        validated_at: datetime | None = None,
    ) -> LedgerEvent:
        """Accrue ACT/365 borrow once per short ticker at that session close."""
        _require_timezone_aware(valuation_at, "valuation_at")
        with self.transaction():
            lots = self._open_lots_for_direction("short")
            tickers = sorted({lot["ticker"] for lot in lots})
            marks = self._validate_accrual_marks(
                session, close_marks, tickers, validated_at or valuation_at
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
                notional = quantity * marks[ticker].close
                days = 1
                if carried_calendar:
                    previous = self._connection.execute(
                        "SELECT session FROM account_snapshots "
                        "WHERE cohort_id = ? AND session < ? AND valid = 1 "
                        "ORDER BY session DESC LIMIT 1",
                        (self.cohort_id, session.isoformat()),
                    ).fetchone()
                    if previous is None:
                        days = 0
                    else:
                        prior_session = _date(previous["session"])
                        days = (session - prior_session).days
                        mark = self._connection.execute(
                            "SELECT close FROM marks "
                            "WHERE ticker = ? AND session = ? AND adjusted = 0",
                            (ticker, prior_session.isoformat()),
                        ).fetchone()
                        if mark is None:
                            raise MissingMarkError(
                                f"missing carried mark {ticker}/{prior_session}"
                            )
                        carried_quantity = quantity
                        for split in self._connection.execute(
                            "SELECT ratio FROM corporate_actions WHERE ticker = ? "
                            "AND action_type = 'split' AND session > ? AND session <= ?",
                            (ticker, prior_session.isoformat(), session.isoformat()),
                        ):
                            carried_quantity /= _decimal(split["ratio"])
                        notional = carried_quantity * _decimal(mark["close"])
                amount = self._cost_model.borrow_charge(notional * days, annual_rate)
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
                    self.cohort_id,
                    None,
                    _encode(session),
                    _encode(amount),
                    _encode(annual_rate),
                    _encode(flagged),
                )
                if existing is not None:
                    columns = (
                        "accrual_id",
                        "cohort_id",
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
                    "INSERT INTO borrow_accruals VALUES (?, ?, ?, ?, ?, ?, ?)",
                    expected,
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
        *,
        carried_calendar: bool = False,
    ) -> LedgerEvent:
        """Accrue debit-cash financing once; positive idle cash has a zero yield."""
        if (
            not isinstance(annual_rate, Decimal)
            or not annual_rate.is_finite()
            or annual_rate < 0
        ):
            raise ValueError("annual financing rate must be a non-negative Decimal")
        effective_at = processed_at or _session_close_timestamp(session)
        _require_timezone_aware(effective_at, "processed_at")
        accrual_id = stable_id("financing", self.cohort_id, session)
        with self.transaction():
            existing = self._connection.execute(
                "SELECT * FROM financing_accruals WHERE accrual_id = ?", (accrual_id,)
            ).fetchone()
            if existing is not None:
                identity = (
                    accrual_id,
                    self.cohort_id,
                    _encode(session),
                    _encode(annual_rate),
                    0,
                )
                columns = ("accrual_id", "cohort_id", "session", "annual_rate", "flagged")
                if any(
                    existing[column] != value
                    for column, value in zip(columns, identity)
                ):
                    raise LedgerConflictError(
                        f"conflicting financing accrual {accrual_id}"
                    )
                return LedgerEvent(
                    accrual_id,
                    session,
                    "financing",
                    _decimal(existing["amount"]),
                    False,
                    "debit financing ACT/365",
                )
            debit_balance = max(-self._accounting_summary()["cash"], Decimal("0"))
            days = 1
            if carried_calendar:
                previous = self._connection.execute(
                    "SELECT session, cash FROM account_snapshots "
                    "WHERE cohort_id = ? AND session < ? AND valid = 1 "
                    "ORDER BY session DESC LIMIT 1",
                    (self.cohort_id, session.isoformat()),
                ).fetchone()
                days = (session - _date(previous["session"])).days if previous else 0
                debit_balance = (
                    max(-_decimal(previous["cash"]), Decimal(0))
                    if previous
                    else Decimal(0)
                )
            amount = self._cost_model.financing_charge(
                debit_balance * days, annual_rate
            )
            event = LedgerEvent(
                accrual_id,
                session,
                "financing",
                amount,
                False,
                "debit financing ACT/365",
            )
            self._connection.execute(
                "INSERT INTO financing_accruals VALUES (?, ?, ?, ?, ?, ?)",
                (
                    accrual_id,
                    self.cohort_id,
                    _encode(session),
                    _encode(amount),
                    _encode(annual_rate),
                    0,
                ),
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

    # ------------------------------------------------------------------
    # Fills & FIFO Lot Accounting
    # ------------------------------------------------------------------

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
                "SELECT * FROM order_intents WHERE intent_id = ? AND cohort_id = ?",
                (intent.intent_id, self.cohort_id),
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
                    (stored_intent.intent_id, _encode(fill.session)),
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
                        _encode(fill.processed_at),
                        "authoritative fill applied",
                    ),
                )
        return self.account_state()

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
        if fill.quantity <= 0:
            raise ValueError("fill quantity must be positive")
        # Support T+1 open trimming for next-session-open entry intents
        # (0 < fill.quantity <= intent.requested_qty); exits & resting stops
        # still require exact quantity match.
        if intent.price_rule == "next_session_open" and intent.side in {"buy", "short"}:
            if fill.quantity > intent.requested_qty:
                raise ValueError("fill quantity does not match requested quantity")
        elif fill.quantity != intent.requested_qty:
            raise ValueError("fill quantity does not match requested quantity")
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

    def _insert_fill(self, fill: Fill) -> None:
        self._connection.execute(
            "INSERT INTO fills VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    _encode(amount),
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
                _encode(fill.session),
                _encode(fill.fill_price),
                fill.quantity,
                fill.quantity,
                _encode(margin),
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
                    (allocation["lot_id"], self.cohort_id, ticker, direction),
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
                "INSERT INTO lot_closures VALUES (?, ?, ?, ?, ?, ?)",
                (
                    stable_id("lot_closure", fill.fill_id, lot["lot_id"]),
                    self.cohort_id,
                    lot["lot_id"],
                    fill.fill_id,
                    close_qty,
                    _encode(realized),
                ),
            )
            self._connection.execute(
                "UPDATE lots SET open_qty = ?, margin_reserved = ? WHERE lot_id = ?",
                (
                    lot_open_qty - close_qty,
                    _encode(existing_margin - released_margin),
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
        self._connection.execute(
            "INSERT INTO cash_events VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                cash_event_id,
                self.cohort_id,
                _encode(session),
                event_type,
                _encode(amount),
                _encode(effective_at),
                detail,
            ),
        )

    # ------------------------------------------------------------------
    # Marks, Benchmarks, Valuation & Snapshots
    # ------------------------------------------------------------------

    def mark(
        self,
        session: date,
        close_marks: dict[str, MarketBar],
        epoch_id: str,
        valuation_at: datetime,
    ) -> AccountSnapshot:
        _require_timezone_aware(valuation_at, "valuation_at")
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
        _require_timezone_aware(valuation_at, "valuation_at")
        validation_time = validated_at or valuation_at
        _require_timezone_aware(validation_time, "validated_at")
        with self.transaction():
            self._record_marks(session, close_marks, validation_time)

    def snapshot_account(
        self, session: date, epoch_id: str, valuation_at: datetime
    ) -> AccountSnapshot:
        _require_timezone_aware(valuation_at, "valuation_at")
        with self.transaction():
            return self._snapshot_account(session, epoch_id, valuation_at)

    def record_benchmark_observation(self, observation: BenchmarkObservation) -> None:
        """Persist one conflict-safe shared adjusted benchmark observation."""
        if observation.cohort_id != self.cohort_id:
            raise ValueError("benchmark cohort_id does not match ledger")
        if observation.return_basis not in {
            "total_return_adjusted",
            "paired_total_return_index_v2",
        }:
            raise ValueError("unsupported benchmark return basis")
        if (
            not observation.close.is_finite()
            or observation.close <= 0
            or not observation.source
        ):
            raise ValueError("invalid benchmark observation")
        _require_timezone_aware(observation.observed_at, "observed_at")
        with self.transaction():
            values = self._benchmark_values(observation)
            existing_by_id = self._connection.execute(
                "SELECT * FROM benchmark_observations WHERE observation_id = ?",
                (observation.observation_id,),
            ).fetchone()
            compare_columns = (
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
            if existing_by_id is not None:
                if any(
                    existing_by_id[col] != val
                    for col, val in zip(compare_columns, values[1:])
                ):
                    raise LedgerConflictError(
                        f"conflicting benchmark identity {observation.observation_id}"
                    )
                return
            occupied = self._connection.execute(
                """SELECT * FROM benchmark_observations
                   WHERE epoch_id = ? AND session = ? AND symbol = ?""",
                (
                    observation.epoch_id,
                    _encode(observation.session),
                    observation.symbol,
                ),
            ).fetchone()
            if occupied is not None:
                # Shared across cohorts in unified generation.sqlite3: identical
                # economic observation is idempotent; conflicting data fails closed.
                if any(
                    occupied[col] != val
                    for col, val in zip(compare_columns, values[1:])
                ):
                    raise LedgerConflictError(
                        f"conflicting benchmark session {observation.symbol}/{observation.session}"
                    )
                return
            self._connection.execute(
                "INSERT INTO benchmark_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )

    def session_realized_net(self, session: date) -> Decimal:
        rows = self._connection.execute(
            """SELECT c.realized_pnl FROM lot_closures c
               JOIN fills f ON f.fill_id = c.fill_id
               WHERE c.cohort_id = ? AND f.session = ?""",
            (self.cohort_id, session.isoformat()),
        ).fetchall()
        pnl = sum((_decimal(r["realized_pnl"]) for r in rows), Decimal(0))
        for fill in self.read_fills(session, session):
            pnl -= fill.commission + fill.other_fees
        rows = self._connection.execute(
            "SELECT amount FROM cash_events WHERE cohort_id = ? AND session = ? "
            "AND event_type IN ('borrow', 'financing', 'dividend')",
            (self.cohort_id, session.isoformat()),
        ).fetchall()
        return pnl + sum((_decimal(r["amount"]) for r in rows), Decimal(0))

    def cooling_tickers(self, session: date, sessions: int) -> set[str]:
        if sessions <= 0:
            return set()
        start = session
        for _ in range(sessions - 1):
            start = previous_session(start)
        rows = self._connection.execute(
            """SELECT DISTINCT f.intent_id FROM fills f
               JOIN order_intents i ON i.intent_id = f.intent_id
               WHERE f.cohort_id = ? AND f.session >= ? AND f.session <= ?
                 AND i.price_rule = 'resting_stop'""",
            (self.cohort_id, start.isoformat(), session.isoformat()),
        ).fetchall()
        return {self._ticker_for_intent(str(r["intent_id"])) for r in rows}

    def opening_equity(self, session: date, prices: Mapping[str, Decimal]) -> Decimal:
        context = self.session_execution_context(session)
        if context is None:
            return self.account_state().net_equity
        payload = json.loads(str(context["economic_inputs_json"]))
        state = payload["starting_state"]
        equity = Decimal(str(state["account"]["cash"])) + Decimal(
            str(state["account"].get("dividend_receivable", "0"))
        )
        actions = sorted(
            {a["action_id"]: a for a in payload["market"]["corporate_actions"]}.values(),
            key=lambda action: (
                action.get("session", session.isoformat()),
                action["action_type"] != "split",
                action["action_id"],
            ),
        )
        applied = {
            (row["action_id"], row["lot_id"])
            for row in state.get("applied_lot_actions", [])
        }
        for lot in state["open_lots"]:
            quantity = Decimal(str(lot["quantity"]))
            sign = Decimal(1) if lot["direction"] == "long" else Decimal(-1)
            for action in actions:
                if (
                    action["ticker"] != lot["ticker"]
                    or (action["action_id"], lot["lot_id"]) in applied
                ):
                    continue
                if action["action_type"] == "split":
                    quantity *= Decimal(str(action["ratio"]))
                elif action["action_type"] == "cash_dividend":
                    equity += sign * quantize_cash(
                        quantity * Decimal(str(action["cash_per_share"]))
                    )
            equity += sign * quantity * prices[str(lot["ticker"])]
        return equity

    def account_state(
        self, prices: Mapping[str, Decimal] | None = None
    ) -> AccountState:
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
            if prices is not None:
                mark = prices.get(str(lot["ticker"]))
                if not isinstance(mark, Decimal) or not mark.is_finite() or mark <= 0:
                    raise MissingMarkError(
                        f"missing exact opening mark for {lot['ticker']}"
                    )
            value = _decimal(lot["open_qty"]) * mark
            if lot["direction"] == "long":
                long_value += value
            else:
                short_liability += value
                margin_used += max(
                    _decimal(lot["margin_reserved"]),
                    value * self._cost_model.margin_requirement,
                )
        net_equity = (
            summary["cash"] + long_value - short_liability + self.dividend_receivable()
        )
        return AccountState(
            self.cohort_id,
            summary["cash"],
            long_value,
            short_liability,
            margin_used,
            summary["cash"] - margin_used + self.dividend_payable_reserve(),
            net_equity,
            max(summary["high_water_mark"], net_equity),
            self.dividend_receivable(),
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
            _require_timezone_aware(bar.fetched_at, "bar.fetched_at")
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

    def _open_lots(self, *, include_latest_mark: bool = False) -> list[sqlite3.Row]:
        mark_column = (
            """, (
                SELECT m.close FROM marks m
                WHERE m.ticker = l.ticker
                  AND m.session >= l.opened_session
                ORDER BY m.session DESC, m.mark_id DESC LIMIT 1
            ) AS mark_close"""
            if include_latest_mark
            else ""
        )
        return self._connection.execute(
            "SELECT l.*"
            + mark_column
            + " FROM lots l WHERE cohort_id = ? AND open_qty > 0 "
            "ORDER BY ticker, direction, opened_session, lot_id",
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
            _require_timezone_aware(bar.fetched_at, "bar.fetched_at")
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
        mark_id = stable_id("mark", ticker, bar.session)
        values = (
            mark_id,
            ticker,
            _encode(bar.session),
            _encode(bar.open),
            _encode(bar.high),
            _encode(bar.low),
            _encode(bar.close),
            bar.source,
            _encode(bar.fetched_at),
            0,
        )
        existing = self._connection.execute(
            "SELECT * FROM marks WHERE ticker = ? AND session = ?",
            (ticker, _encode(bar.session)),
        ).fetchone()
        if existing is not None:
            columns = (
                "ticker",
                "session",
                "close",
                "source",
                "observed_at",
                "adjusted",
            )
            expected = (
                ticker,
                _encode(bar.session),
                _encode(bar.close),
                bar.source,
                _encode(bar.fetched_at),
                0,
            )
            if any(existing[col] != val for col, val in zip(columns, expected)):
                raise LedgerConflictError(f"conflicting marks identity {mark_id}")
            return
        self._connection.execute(
            """INSERT INTO marks (
                   mark_id, ticker, session, open, high, low, close,
                   source, observed_at, adjusted
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            values,
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
                "SELECT * FROM marks WHERE ticker = ? AND session = ?",
                (ticker, _encode(session)),
            ).fetchone()
            if row is None:
                raise MissingMarkError(f"missing persisted mark for {ticker}/{session}")
            close = _decimal(row["close"])
            marks_by_ticker[ticker] = MarketBar(
                ticker,
                session,
                _decimal(row["open"]) if row["open"] is not None else close,
                _decimal(row["high"]) if row["high"] is not None else close,
                _decimal(row["low"]) if row["low"] is not None else close,
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
            (self.cohort_id, _encode(session)),
        ).fetchone()
        if occupied is not None:
            raise LedgerConflictError(
                f"conflicting account_snapshots session {self.cohort_id}/{session}"
            )
        self._connection.execute(
            """INSERT INTO account_snapshots (
                   snapshot_id, cohort_id, epoch_id, session, valuation_at,
                   cash, long_market_value, short_liability, gross_exposure,
                   net_exposure, margin_used, buying_power, realized_pnl,
                   unrealized_pnl, gross_equity, slippage_cost, commission_cost,
                   other_fees, borrow_cost, financing_cost, dividend_cash,
                   net_equity, high_water_mark, valid, invalid_reason,
                   dividend_receivable
               ) VALUES (
                   ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
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
                margin_used += max(
                    _decimal(lot["margin_reserved"]),
                    quantity * close * self._cost_model.margin_requirement,
                )
        div_recv = self.dividend_receivable()
        net_equity = summary["cash"] + long_market_value - short_liability + div_recv
        gross_exposure = long_market_value + short_liability
        net_exposure = long_market_value - short_liability
        cumulative_costs = (
            summary["slippage_cost"]
            + summary["commission_cost"]
            + summary["other_fees"]
            + summary["borrow_cost"]
            + summary["financing_cost"]
        )
        gross_equity = net_equity + cumulative_costs
        assert net_equity == summary["cash"] + long_market_value - short_liability + div_recv
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
            summary["cash"] - margin_used + self.dividend_payable_reserve(),
            summary["realized_pnl"],
            unrealized_pnl,
            gross_equity,
            summary["slippage_cost"],
            summary["commission_cost"],
            summary["other_fees"],
            summary["borrow_cost"],
            summary["financing_cost"],
            summary["dividend_cash"],
            net_equity,
            max(summary["high_water_mark"], net_equity),
            valid,
            invalid_reason,
            div_recv,
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
        next_hwm = max(
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
                _encode(summary["cash"] + deltas["cash"]),
                _encode(summary["realized_pnl"] + deltas["realized_pnl"]),
                _encode(summary["slippage_cost"] + deltas["slippage_cost"]),
                _encode(summary["commission_cost"] + deltas["commission_cost"]),
                _encode(summary["other_fees"] + deltas["other_fees"]),
                _encode(summary["borrow_cost"] + deltas["borrow_cost"]),
                _encode(summary["financing_cost"] + deltas["financing_cost"]),
                _encode(summary["dividend_cash"] + deltas["dividend_cash"]),
                _encode(next_hwm),
                self.cohort_id,
            ),
        )

    # ------------------------------------------------------------------
    # Row Encoders / Decoders
    # ------------------------------------------------------------------

    def _fill_values(self, fill: Fill) -> tuple[object, ...]:
        return (
            fill.fill_id,
            self.cohort_id,
            fill.intent_id,
            fill.side,
            _encode(fill.session),
            _encode(fill.effective_at),
            _encode(fill.processed_at),
            _encode(fill.reference_price),
            _encode(fill.fill_price),
            fill.quantity,
            _encode(fill.slippage),
            _encode(fill.commission),
            _encode(fill.other_fees),
        )

    def _require_identical_fill(self, row: sqlite3.Row, fill: Fill) -> None:
        columns = (
            "fill_id",
            "cohort_id",
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
            "dividend_receivable",
        )
        if any(
            row[column] != value
            for column, value in zip(columns, self._snapshot_values(snapshot))
        ):
            raise LedgerConflictError(
                f"conflicting account_snapshots identity {snapshot.snapshot_id}"
            )

    @staticmethod
    def _snapshot_values(snapshot: AccountSnapshot) -> tuple[object, ...]:
        return (
            snapshot.snapshot_id,
            snapshot.cohort_id,
            snapshot.epoch_id,
            _encode(snapshot.session),
            _encode(snapshot.valuation_at),
            _encode(snapshot.cash),
            _encode(snapshot.long_market_value),
            _encode(snapshot.short_liability),
            _encode(snapshot.gross_exposure),
            _encode(snapshot.net_exposure),
            _encode(snapshot.margin_used),
            _encode(snapshot.buying_power),
            _encode(snapshot.realized_pnl),
            _encode(snapshot.unrealized_pnl),
            _encode(snapshot.gross_equity),
            _encode(snapshot.slippage_cost),
            _encode(snapshot.commission_cost),
            _encode(snapshot.other_fees),
            _encode(snapshot.borrow_cost),
            _encode(snapshot.financing_cost),
            _encode(snapshot.dividend_cash),
            _encode(snapshot.net_equity),
            _encode(snapshot.high_water_mark),
            _encode(snapshot.valid),
            snapshot.invalid_reason,
            _encode(snapshot.dividend_receivable),
        )

    @staticmethod
    def _benchmark_values(observation: BenchmarkObservation) -> tuple[object, ...]:
        return (
            observation.observation_id,
            observation.epoch_id,
            _encode(observation.session),
            observation.symbol,
            _encode(observation.close),
            observation.return_basis,
            observation.source,
            _encode(observation.observed_at),
            _encode(observation.valid),
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
            values.append(_encode(start_session))
        if end_session is not None:
            clauses.append("session <= ?")
            values.append(_encode(end_session))
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
            _decimal(row["dividend_receivable"])
            if "dividend_receivable" in row.keys()
            else Decimal(0),
        )

    def _benchmark_from_row(self, row: sqlite3.Row) -> BenchmarkObservation:
        cohort_id = (
            row["cohort_id"] if "cohort_id" in row.keys() else self.cohort_id
        )
        return BenchmarkObservation(
            row["observation_id"],
            cohort_id,
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
