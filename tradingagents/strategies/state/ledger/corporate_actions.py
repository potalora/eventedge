"""Corporate actions (splits, cash dividends, payment terms, quarantines)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import json
import sqlite3
from zoneinfo import ZoneInfo

from tradingagents.strategies.execution import (
    CorporateAction,
    LedgerEvent,
    OrderIntent,
    stable_id,
)
from tradingagents.strategies.execution.cost_model import quantize_cash
from tradingagents.strategies.orchestration.trading_calendar import (
    next_session,
    session_close,
    session_open,
)

from .connection import (
    _date,
    _decimal,
    _encode,
    _require_timezone_aware,
    _session_close_timestamp,
    _session_timestamp,
)


class CorporateActionsMixin:
    """Splits, dividend accruals/settlements, batch validation, and quarantines."""

    def quarantine_ticker(self, ticker: str, reason: str) -> None:
        with self.transaction():
            self._quarantine_ticker(
                ticker, reason, _session_timestamp(date(1970, 1, 1))
            )

    def reject_corporate_action_batch(
        self,
        session: date,
        actions: tuple[CorporateAction, ...],
        governed_tickers: tuple[str, ...],
        errors: tuple[str, ...],
        rejected_at: datetime,
        *,
        preserve_committed_session: bool = False,
    ) -> str:
        """Reject one response, optionally preserving an interleaved commit."""
        _require_timezone_aware(rejected_at, "rejected_at")
        if not errors:
            raise ValueError("corporate action batch rejection requires errors")
        payload = [
            {
                "action_id": action.action_id,
                "ticker": action.ticker,
                "session": _encode(action.session),
                "action_type": action.action_type,
                "ratio": _encode(action.ratio),
                "cash_per_share": _encode(action.cash_per_share),
                "source": action.source,
                "fetched_at": _encode(action.fetched_at),
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
                    _encode(session),
                    payload_json,
                    payload_hash,
                    errors_json,
                    _encode(rejected_at),
                ),
            )
            if not preserve_committed_session:
                if not self.session_invalid_reason(session):
                    self.invalidate_session_and_cancel_due(
                        session, reason, rejected_at
                    )
                else:
                    self._invalidate_session(session, reason, rejected_at)
            for ticker in affected:
                self._quarantine_ticker(ticker, reason, rejected_at)
        return reason

    def corporate_action_batch_state_errors(
        self,
        session: date,
        actions: tuple[CorporateAction, ...],
        *,
        allow_prior: bool = False,
    ) -> tuple[str, ...]:
        """Validate a complete action batch against live lot/intent state."""
        errors: list[str] = []
        unique_actions: list[CorporateAction] = []
        seen_actions: dict[str, CorporateAction] = {}
        for action in sorted(
            actions,
            key=lambda item: (
                item.session,
                item.action_type != "split",
                item.ticker,
                item.action_id,
            ),
        ):
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
                if not self._same_corporate_action(
                    existing,
                    action,
                    ignore_observation_time=allow_prior and action.session < session,
                ):
                    errors.append(
                        f"conflicting corporate action identity {action.action_id}"
                    )
                # Note: in unified generation.sqlite3, another cohort may have
                # already inserted the shared corporate_actions row; we still
                # validate split divisibility if this cohort hasn't applied it yet.
                if action.action_type != "split" or self._cohort_applied_action(
                    action.action_id
                ):
                    continue
            if action.session > session or (
                action.session != session and not allow_prior
            ):
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
        *,
        allow_prior: bool = False,
    ) -> tuple[str, ...]:
        """Validate structural and state-dependent action invariants without mutation."""
        errors: list[str] = list(
            self.corporate_action_batch_state_errors(
                session, actions, allow_prior=allow_prior
            )
        )
        seen: dict[str, CorporateAction] = {}
        terms: dict[tuple[date, str, str], str] = {}
        for action in actions:
            key = (action.session, action.ticker, action.action_type)
            if (
                action.action_type == "cash_dividend"
                and key in terms
                and terms[key] != action.action_id
            ):
                errors.append(
                    f"ambiguous corporate action terms {action.ticker}/{action.action_type}"
                )
            terms[key] = action.action_id
            if action.action_id in seen and seen[action.action_id] != action:
                errors.append(f"conflicting corporate action {action.action_id}")
            seen[action.action_id] = action
            if action.session > session or (
                action.session != session and not allow_prior
            ):
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
            elif action.fetched_at < session_close(action.session):
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
            if action.payment_observed_at is not None:
                observed = action.payment_observed_at
                if (
                    observed.tzinfo is None
                    or observed.utcoffset() is None
                    or observed > processed_at
                    or observed.astimezone(ZoneInfo("America/New_York")).date() > session
                    or not action.payment_source
                    or not action.payment_reference
                    or action.payment_date is None
                ):
                    errors.append(
                        f"invalid dividend payment observation {action.action_id}"
                    )
            if action.payment_date is not None and (
                action.action_type != "cash_dividend"
                or action.payment_date < action.session
            ):
                errors.append(f"invalid dividend payment date {action.action_id}")
        return tuple(sorted(set(errors)))

    def apply_corporate_actions(
        self,
        session: date,
        actions: list[CorporateAction],
        processed_at: datetime | None = None,
        *,
        allow_prior: bool = False,
    ) -> list[LedgerEvent]:
        """Apply verified actions once per cohort, or durably quarantine uncertain inputs."""
        processed = processed_at or max(
            (action.fetched_at for action in actions),
            default=_session_close_timestamp(session),
        )
        _require_timezone_aware(processed, "processed_at")
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
            errors = self.corporate_action_batch_errors(
                session, batch, processed, allow_prior=allow_prior
            )
            if errors:
                for action in batch:
                    existing = self._connection.execute(
                        "SELECT * FROM corporate_actions WHERE action_id = ?",
                        (action.action_id,),
                    ).fetchone()
                    if existing is not None and not self._same_corporate_action(
                        existing,
                        action,
                        ignore_observation_time=allow_prior
                        and action.session < session,
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
                for action in sorted(
                    actions,
                    key=lambda item: (
                        item.session,
                        item.action_type != "split",
                        item.ticker,
                        item.action_id,
                    ),
                )
            }
            for action in unique_actions.values():
                existing = self._connection.execute(
                    "SELECT * FROM corporate_actions WHERE action_id = ?",
                    (action.action_id,),
                ).fetchone()
                if existing is not None:
                    if not self._same_corporate_action(
                        existing,
                        action,
                        ignore_observation_time=allow_prior
                        and action.session < session,
                    ):
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
                    self._accept_dividend_payment_terms(action, session, processed)
                    if self._cohort_applied_action(action.action_id):
                        continue
                else:
                    self._connection.execute(
                        """INSERT INTO corporate_actions (
                               action_id, ticker, session, action_type, ratio,
                               cash_per_share, payment_date, source, fetched_at, verified
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        self._corporate_action_values(action),
                    )
                    self._connection.execute(
                        "INSERT OR IGNORE INTO dividend_payment_terms VALUES (?, ?)",
                        (
                            action.action_id,
                            action.payment_date.isoformat()
                            if action.payment_date
                            else None,
                        ),
                    )
                    self._accept_dividend_payment_terms(action, session, processed)

                if action.session > session or (
                    action.session != session and not allow_prior
                ):
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
                else:
                    events.append(
                        self._quarantine_action(
                            session, action, "unsupported corporate action", processed
                        )
                    )
        return events

    def _cohort_applied_action(self, action_id: str) -> bool:
        """Whether this cohort has already applied ``action_id`` to its lots/intents."""
        lot_hit = self._connection.execute(
            """SELECT 1 FROM lot_action_applications a
               JOIN lots l ON l.lot_id = a.lot_id
               WHERE a.action_id = ? AND l.cohort_id = ? LIMIT 1""",
            (action_id, self.cohort_id),
        ).fetchone()
        if lot_hit is not None:
            return True
        intent_hit = self._connection.execute(
            """SELECT 1 FROM intent_action_adjustments a
               JOIN order_intents i ON i.intent_id = a.intent_id
               WHERE a.action_id = ? AND i.cohort_id = ? LIMIT 1""",
            (action_id, self.cohort_id),
        ).fetchone()
        if intent_hit is not None:
            return True
        div_hit = self._connection.execute(
            """SELECT 1 FROM dividend_receivables r
               JOIN dividend_events e ON e.dividend_event_id = r.event_id
               WHERE e.action_id = ? AND r.cohort_id = ? LIMIT 1""",
            (action_id, self.cohort_id),
        ).fetchone()
        return div_hit is not None

    def _corporate_action_values(self, action: CorporateAction) -> tuple[object, ...]:
        return (
            action.action_id,
            action.ticker,
            _encode(action.session),
            action.action_type,
            _encode(action.ratio) if action.ratio is not None else None,
            _encode(action.cash_per_share)
            if action.cash_per_share is not None
            else None,
            action.payment_date.isoformat() if action.payment_date else None,
            action.source,
            _encode(action.fetched_at),
            _encode(action.verified),
        )

    def _same_corporate_action(
        self,
        row: sqlite3.Row,
        action: CorporateAction,
        *,
        ignore_observation_time: bool = False,
    ) -> bool:
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
        expected_values = (
            action.action_id,
            action.ticker,
            _encode(action.session),
            action.action_type,
            _encode(action.ratio) if action.ratio is not None else None,
            _encode(action.cash_per_share)
            if action.cash_per_share is not None
            else None,
            action.source,
            _encode(action.fetched_at),
            _encode(action.verified),
        )
        terms = self._connection.execute(
            "SELECT payment_date FROM dividend_payment_terms WHERE action_id = ?",
            (action.action_id,),
        ).fetchone()
        payment_date = (
            terms[0]
            if terms is not None
            else (row["payment_date"] if "payment_date" in row.keys() else None)
        )
        incoming = action.payment_date.isoformat() if action.payment_date else None
        terms_match = (
            payment_date == incoming
            or incoming is None
            or (
                payment_date is None
                and bool(
                    action.payment_source
                    and action.payment_reference
                    and action.payment_observed_at
                )
            )
        )
        return terms_match and all(
            (ignore_observation_time and column == "fetched_at")
            or row[column] == value
            for column, value in zip(columns, expected_values)
        )

    def _accept_dividend_payment_terms(
        self, action: CorporateAction, session: date, processed: datetime
    ) -> None:
        if action.payment_date is None or action.payment_observed_at is None:
            return
        pay_iso = action.payment_date.isoformat()
        self._connection.execute(
            "INSERT OR IGNORE INTO dividend_payment_observations VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                action.action_id,
                pay_iso,
                action.payment_source,
                action.payment_reference,
                action.payment_observed_at.isoformat(),
                session.isoformat(),
                processed.isoformat(),
            ),
        )
        self._connection.execute(
            "UPDATE dividend_payment_terms SET payment_date = ? "
            "WHERE action_id = ? AND payment_date IS NULL",
            (pay_iso, action.action_id),
        )
        self._connection.execute(
            "UPDATE dividend_events SET payment_date = ? "
            "WHERE action_id = ? AND payment_date IS NULL",
            (pay_iso, action.action_id),
        )
        self._connection.execute(
            "UPDATE dividend_receivables SET payment_date = ? "
            "WHERE cohort_id = ? AND payment_date IS NULL AND settled_session IS NULL "
            "AND event_id IN (SELECT dividend_event_id FROM dividend_events WHERE action_id = ?)",
            (pay_iso, self.cohort_id, action.action_id),
        )

    def pending_dividend_actions(self) -> tuple[CorporateAction, ...]:
        """Unknown payable terms for retained entitlements, including closed lots."""
        rows = self._connection.execute(
            """SELECT DISTINCT a.* FROM corporate_actions a
               JOIN dividend_events e USING(action_id)
               JOIN dividend_receivables r ON r.event_id = e.dividend_event_id
               WHERE r.cohort_id = ? AND r.payment_date IS NULL
                 AND r.settled_session IS NULL
               ORDER BY a.action_id""",
            (self.cohort_id,),
        ).fetchall()
        return tuple(
            CorporateAction(
                row["action_id"],
                row["ticker"],
                _date(row["session"]),
                row["action_type"],
                None,
                _decimal(row["cash_per_share"]),
                row["source"],
                datetime.fromisoformat(row["fetched_at"]),
                bool(row["verified"]),
            )
            for row in rows
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
                    _encode(_decimal(lot["entry_price"]) / action.ratio),
                    _encode(_decimal(lot["margin_reserved"])),
                    lot["lot_id"],
                ),
            )
            self._connection.execute(
                "INSERT INTO lot_action_applications VALUES (?, ?, ?, ?, ?)",
                (
                    stable_id("lot_action", action.action_id, lot["lot_id"]),
                    action.action_id,
                    lot["lot_id"],
                    _encode(processed_at),
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
                    _encode(new_stop) if new_stop is not None else None,
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
                    _encode(old_stop) if old_stop is not None else None,
                    _encode(new_stop) if new_stop is not None else None,
                    _encode(processed_at),
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
        if not lots:
            return []
        shared_event_id = stable_id("dividend_event", action.action_id)
        pay_iso = action.payment_date.isoformat() if action.payment_date else None
        self._connection.execute(
            """INSERT OR IGNORE INTO dividend_events (
                   dividend_event_id, action_id, ticker, ex_session,
                   payment_date, cash_per_share, source, observed_at, payload_json
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                shared_event_id,
                action.action_id,
                action.ticker,
                _encode(action.session),
                pay_iso,
                _encode(action.cash_per_share),
                action.source,
                _encode(action.fetched_at),
                "{}",
            ),
        )
        events: list[LedgerEvent] = []
        for lot in lots:
            gross = quantize_cash(Decimal(lot["open_qty"]) * action.cash_per_share)
            amount = gross if lot["direction"] == "long" else -gross
            receivable_id = stable_id("dividend", action.action_id, lot["lot_id"])
            self._connection.execute(
                """INSERT INTO dividend_receivables (
                       receivable_id, cohort_id, event_id, lot_id, session,
                       direction, amount, payment_date, settled_session
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
                (
                    receivable_id,
                    self.cohort_id,
                    shared_event_id,
                    lot["lot_id"],
                    _encode(action.session),
                    lot["direction"],
                    _encode(amount),
                    pay_iso,
                ),
            )
            events.append(
                LedgerEvent(
                    receivable_id,
                    action.session,
                    "dividend",
                    amount,
                    False,
                    lot["direction"],
                )
            )
        return events

    def _dividend_receivable_rows(
        self, settled_session: date | None = None
    ) -> list[sqlite3.Row]:
        exists = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dividend_receivables'"
        ).fetchone()
        if exists is None:
            return []
        return self._connection.execute(
            """SELECT * FROM dividend_receivables
               WHERE cohort_id = ?
                 AND (settled_session IS NULL OR settled_session = ?)
               ORDER BY receivable_id""",
            (self.cohort_id, settled_session.isoformat() if settled_session else None),
        ).fetchall()

    def dividend_receivable(self) -> Decimal:
        """Signed entitlement: receivable for longs, payable for shorts."""
        return sum(
            (_decimal(row["amount"]) for row in self._dividend_receivable_rows()),
            Decimal(0),
        )

    def dividend_payable_reserve(self) -> Decimal:
        return sum(
            (
                min(_decimal(row["amount"]), Decimal(0))
                for row in self._dividend_receivable_rows()
            ),
            Decimal(0),
        )

    def settle_dividends(self, session: date, processed_at: datetime) -> None:
        """Transfer only verified payable entitlements into cash, once."""
        with self.transaction():
            for row in self._connection.execute(
                """SELECT r.*, o.observed_at FROM dividend_receivables r
                   JOIN dividend_events e ON e.dividend_event_id = r.event_id
                   LEFT JOIN dividend_payment_observations o ON o.action_id = e.action_id
                   WHERE r.cohort_id = ? AND r.settled_session IS NULL
                     AND r.payment_date <= ?
                   ORDER BY r.receivable_id""",
                (self.cohort_id, session.isoformat()),
            ).fetchall():
                if row["observed_at"] and datetime.fromisoformat(
                    row["observed_at"]
                ) >= session_open(session):
                    continue
                amount = _decimal(row["amount"])
                self._insert_named_cash_event(
                    stable_id("dividend_payment", self.cohort_id, row["receivable_id"]),
                    session,
                    "dividend",
                    amount,
                    processed_at,
                    f"payment of {row['receivable_id']}",
                )
                self._update_accounting_summary(
                    cash_delta=amount, dividend_cash_delta=amount
                )
                self._connection.execute(
                    "UPDATE dividend_receivables SET settled_session = ? WHERE receivable_id = ?",
                    (session.isoformat(), row["receivable_id"]),
                )

    def inventory_action_requirements(
        self, session: date
    ) -> dict[date, tuple[str, ...]]:
        """Missing XNYS action dates for carried lots; never invent missed fills."""
        row = self._connection.execute(
            "SELECT MAX(session) FROM account_snapshots WHERE cohort_id = ? AND valid = 1 AND session < ?",
            (self.cohort_id, session.isoformat()),
        ).fetchone()
        boundary = _date(row[0]) if row[0] else None
        needed: dict[date, set[str]] = {}
        for lot in self._open_lots():
            acquired = _date(lot["opened_session"])
            day = next_session(max(acquired, boundary) if boundary else acquired)
            while day < session:
                needed.setdefault(day, set()).add(str(lot["ticker"]))
                day = next_session(day)
        return {day: tuple(sorted(tickers)) for day, tickers in sorted(needed.items())}

    def _matches_split_adjusted_intent(
        self, supplied: OrderIntent, stored: OrderIntent
    ) -> bool:
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
                _encode(quarantined_at),
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
                "session": _encode(action.session),
                "action_type": action.action_type,
                "ratio": _encode(action.ratio),
                "cash_per_share": _encode(action.cash_per_share),
                "source": action.source,
                "fetched_at": _encode(action.fetched_at),
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
                _encode(session),
                action.ticker,
                action.action_id,
                content_hash,
                payload,
                _encode(detected_at),
            ),
        )
