"""Compact digest-verified policy audit persistence backed by ``policy_audit_records``.

Replaces the five legacy policy tables (``signal_policy_provenance``,
``intent_policy_provenance``, ``policy_candidate_decisions``,
``policy_staging_audit_manifests``, ``policy_session_contexts``) with one
canonical SHA-256 digest-envelope helper over ``generation.sqlite3``'s
cohort-scoped ``policy_audit_records`` table.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
import math
import sqlite3
import sys
from typing import Mapping

from tradingagents.strategies.execution import SignalRecord, stable_id

from .connection import (
    LedgerConflictError,
    MissingMarkError,
    _canonical_json,
    _date,
    _datetime,
    _decimal,
    _encode,
    _require_timezone_aware,
)


_POLICY_CONTEXT_VALIDATION_MAX_BYTES = 128 * 1024

RECORD_SIGNAL_PROVENANCE = "signal_policy_provenance"
RECORD_INTENT_PROVENANCE = "intent_policy_provenance"
RECORD_CANDIDATE_DECISION = "policy_candidate_decision"
RECORD_STAGING_MANIFEST = "policy_staging_audit_manifest"
RECORD_SESSION_CONTEXT = "policy_session_context"


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _require_exact_date(value: object, label: str) -> date:
    if type(value) is not date:
        raise TypeError(f"{label} must be an exact date")
    return value


def _normalized_string_tuple(
    values: tuple[str, ...], label: str, *, allow_empty: bool = True
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError(f"{label} must be a tuple of strings")
    normalized = tuple(
        sorted({str(value).strip() for value in values if str(value).strip()})
    )
    if not allow_empty and not normalized:
        raise ValueError(f"{label} must not be empty")
    if len(normalized) > 256:
        raise ValueError(f"{label} must contain at most 256 values")
    return normalized


def _policy_timestamp(value: object, label: str, identity: str) -> datetime:
    if not isinstance(value, str):
        raise LedgerConflictError(f"invalid {label} policy timestamp {identity}")
    try:
        ts = _datetime(value)
    except ValueError as exc:
        raise LedgerConflictError(
            f"invalid {label} policy timestamp {identity}"
        ) from exc
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise LedgerConflictError(f"invalid {label} policy timestamp {identity}")
    return ts


def _normalized_policy_payload(
    *,
    policy_version: str,
    event_key: str,
    source_event_keys: tuple[str, ...],
    strategy_tags: tuple[str, ...],
    risk_tags: tuple[str, ...],
    sector: str,
    journal_only: bool,
    order_eligible: bool,
    decision: str,
    reason_codes: tuple[str, ...],
    bound_context_digest: str,
    signal_ids: tuple[str, ...] | None = None,
) -> dict[str, object]:
    normalized: dict[str, object] = {
        "policy_version": str(policy_version).strip(),
        "event_key": str(event_key).strip(),
        "source_event_keys": _normalized_string_tuple(
            source_event_keys, "source_event_keys"
        ),
        "strategy_tags": _normalized_string_tuple(
            strategy_tags, "strategy_tags", allow_empty=False
        ),
        "risk_tags": _normalized_string_tuple(risk_tags, "risk_tags"),
        "sector": str(sector).strip() or "Unknown",
        "journal_only": bool(journal_only),
        "order_eligible": bool(order_eligible),
        "decision": str(decision).strip(),
        "reason_codes": _normalized_string_tuple(
            reason_codes, "reason_codes", allow_empty=False
        ),
        "bound_context_digest": str(bound_context_digest).strip(),
    }
    if signal_ids is not None:
        if isinstance(signal_ids, (str, bytes)):
            raise TypeError("signal_ids must be a tuple of strings")
        exact_signal_ids = tuple(str(value).strip() for value in signal_ids)
        if (
            not exact_signal_ids
            or any(not value for value in exact_signal_ids)
            or len(set(exact_signal_ids)) != len(exact_signal_ids)
            or len(exact_signal_ids) > 256
        ):
            raise ValueError("signal_ids must be nonempty and unique")
        normalized["signal_ids"] = exact_signal_ids
    for required in (
        "policy_version",
        "event_key",
        "decision",
        "bound_context_digest",
    ):
        if not normalized[required]:
            raise ValueError(f"{required} must not be empty")
    if normalized["journal_only"] and normalized["order_eligible"]:
        raise ValueError("journal-only provenance cannot be order eligible")
    if normalized["decision"] not in {"accepted", "trimmed", "rejected"}:
        raise ValueError("decision must be accepted, trimmed, or rejected")
    return normalized


def _policy_projection_limit(limit: int, *, maximum: int = 256) -> int:
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise TypeError("policy projection limit must be an integer")
    if limit < 1 or limit > maximum:
        raise ValueError(f"policy projection limit must be between 1 and {maximum}")
    return limit


def _require_projection_bound(
    rows: list[sqlite3.Row], limit: int, label: str
) -> list[sqlite3.Row]:
    if len(rows) > limit:
        raise LedgerConflictError(
            f"{label} projection limit {limit} exceeded; refusing truncation"
        )
    return rows


class PolicyAuditMixin:
    """Generic digest-envelope CRUD and projections on ``policy_audit_records``."""

    # ------------------------------------------------------------------
    # Generic digest-envelope primitives over policy_audit_records
    # ------------------------------------------------------------------

    def _write_policy_envelope(
        self,
        *,
        record_type: str,
        record_id: str,
        session: date,
        epoch_id: str,
        payload_json: str,
        created_at: datetime,
        conflict_label: str,
    ) -> sqlite3.Row:
        payload_sha256 = _sha256_hex(payload_json)
        with self.transaction():
            existing = self._connection.execute(
                """SELECT * FROM policy_audit_records
                   WHERE cohort_id = ? AND record_type = ? AND record_id = ?""",
                (self.cohort_id, record_type, record_id),
            ).fetchone()
            if existing is not None:
                self._verify_envelope_row(existing, conflict_label)
                if (
                    existing["session"] != _encode(session)
                    or existing["epoch_id"] != epoch_id
                    or existing["payload_json"] != payload_json
                    or existing["payload_sha256"] != payload_sha256
                ):
                    raise LedgerConflictError(
                        f"conflicting {conflict_label} {record_id}"
                    )
                return existing
            self._connection.execute(
                """INSERT INTO policy_audit_records (
                       cohort_id, record_type, record_id, session, epoch_id,
                       payload_json, payload_sha256, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    self.cohort_id,
                    record_type,
                    record_id,
                    _encode(session),
                    epoch_id,
                    payload_json,
                    payload_sha256,
                    _encode(created_at),
                ),
            )
            row = self._connection.execute(
                """SELECT * FROM policy_audit_records
                   WHERE cohort_id = ? AND record_type = ? AND record_id = ?""",
                (self.cohort_id, record_type, record_id),
            ).fetchone()
            assert row is not None
            return row

    def _read_policy_envelope(
        self, record_type: str, record_id: str
    ) -> sqlite3.Row | None:
        return self._connection.execute(
            """SELECT * FROM policy_audit_records
               WHERE cohort_id = ? AND record_type = ? AND record_id = ?""",
            (self.cohort_id, record_type, record_id),
        ).fetchone()

    @staticmethod
    def _verify_envelope_row(
        row: sqlite3.Row, label: str
    ) -> dict[str, object]:
        raw_json = row["payload_json"]
        if not isinstance(raw_json, str) or _sha256_hex(raw_json) != row["payload_sha256"]:
            raise LedgerConflictError(f"tampered {label} {row['record_id']}")
        try:
            payload = json.loads(raw_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise LedgerConflictError(f"tampered {label} {row['record_id']}") from exc
        if not isinstance(payload, dict) or _canonical_json(payload) != raw_json:
            raise LedgerConflictError(f"tampered {label} {row['record_id']}")
        return payload

    # ------------------------------------------------------------------
    # 1. Policy session context binding (staging / execution)
    # ------------------------------------------------------------------

    def bind_policy_session_context(
        self,
        session: date,
        *,
        binding_kind: str = "staging",
        epoch_id: str,
        policy_version: str,
        policy_config: Mapping[str, object],
        context: Mapping[str, object],
        bound_at: datetime,
    ) -> dict[str, object]:
        """Bind one canonical policy/config snapshot for a cohort session."""
        session = _require_exact_date(session, "session")
        _require_timezone_aware(bound_at, "bound_at")
        if binding_kind not in {"staging", "execution"}:
            raise ValueError("binding_kind must be staging or execution")
        epoch_id = str(epoch_id).strip()
        policy_version = str(policy_version).strip()
        if not epoch_id or not policy_version:
            raise ValueError("epoch_id and policy_version must not be empty")
        configured_version = policy_config.get("version")
        if configured_version is not None and str(configured_version) != policy_version:
            raise ValueError("policy_config version does not match policy_version")

        config_json = _canonical_json(policy_config)
        context_json = _canonical_json(context)
        config_digest = stable_id("policy_config", config_json)
        context_digest = stable_id("policy_context", context_json)
        binding_payload = {
            "cohort_id": self.cohort_id,
            "session": _encode(session),
            "binding_kind": binding_kind,
            "epoch_id": epoch_id,
            "policy_version": policy_version,
            "policy_config_digest": config_digest,
            "context_digest": context_digest,
        }
        binding_payload_json = _canonical_json(binding_payload)
        binding_payload_digest = stable_id("policy_binding", binding_payload_json)
        envelope = {
            "cohort_id": self.cohort_id,
            "session": _encode(session),
            "binding_kind": binding_kind,
            "epoch_id": epoch_id,
            "policy_version": policy_version,
            "policy_config_json": config_json,
            "policy_config_digest": config_digest,
            "context_json": context_json,
            "context_digest": context_digest,
            "payload_json": binding_payload_json,
            "payload_digest": binding_payload_digest,
        }
        record_id = f"{session.isoformat()}:{binding_kind}"
        row = self._write_policy_envelope(
            record_type=RECORD_SESSION_CONTEXT,
            record_id=record_id,
            session=session,
            epoch_id=epoch_id,
            payload_json=_canonical_json(envelope),
            created_at=bound_at,
            conflict_label=f"policy session context {self.cohort_id}/{session}/{binding_kind}",
        )
        return self._decode_session_context_row(row)

    def read_policy_session_context(
        self, session: date, *, binding_kind: str = "staging"
    ) -> dict[str, object] | None:
        session = _require_exact_date(session, "session")
        if binding_kind not in {"staging", "execution"}:
            raise ValueError("binding_kind must be staging or execution")
        row = self._read_policy_envelope(
            RECORD_SESSION_CONTEXT, f"{session.isoformat()}:{binding_kind}"
        )
        return None if row is None else self._decode_session_context_row(row)

    def _decode_session_context_row(self, row: sqlite3.Row) -> dict[str, object]:
        raw_row = tuple(row)
        already_validated = raw_row == getattr(
            self, "_validated_policy_context_row", None
        )
        if already_validated:
            envelope = json.loads(row["payload_json"])
            config = json.loads(envelope["policy_config_json"])
            context = json.loads(envelope["context_json"])
            config_json = envelope["policy_config_json"]
            context_json = envelope["context_json"]
            binding_payload_json = envelope["payload_json"]
        else:
            envelope = PolicyAuditMixin._verify_envelope_row(row, "policy session context")
            try:
                config_json = str(envelope["policy_config_json"])
                context_json = str(envelope["context_json"])
                binding_payload_json = str(envelope["payload_json"])
                config = json.loads(config_json)
                context = json.loads(context_json)
                binding_payload = json.loads(binding_payload_json)
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise LedgerConflictError(
                    f"tampered policy session context {row['record_id']}"
                ) from exc
            expected_binding = {
                "cohort_id": row["cohort_id"],
                "session": row["session"],
                "binding_kind": envelope.get("binding_kind"),
                "epoch_id": row["epoch_id"],
                "policy_version": envelope.get("policy_version"),
                "policy_config_digest": envelope.get("policy_config_digest"),
                "context_digest": envelope.get("context_digest"),
            }
            if (
                _canonical_json(config) != config_json
                or _canonical_json(context) != context_json
                or _canonical_json(binding_payload) != binding_payload_json
                or envelope.get("policy_config_digest")
                != stable_id("policy_config", config_json)
                or envelope.get("context_digest")
                != stable_id("policy_context", context_json)
                or binding_payload != expected_binding
                or envelope.get("payload_digest")
                != stable_id("policy_binding", binding_payload_json)
                or row["record_id"]
                != f"{row['session']}:{envelope.get('binding_kind')}"
            ):
                raise LedgerConflictError(
                    f"tampered policy session context {row['record_id']}"
                )
        bound_at = _policy_timestamp(
            row["created_at"], "policy session context", str(row["record_id"])
        )
        result: dict[str, object] = {
            "cohort_id": str(row["cohort_id"]),
            "session": _date(row["session"]),
            "binding_kind": str(envelope["binding_kind"]),
            "epoch_id": str(row["epoch_id"]),
            "policy_version": str(envelope["policy_version"]),
            "policy_config": config,
            "policy_config_json": config_json,
            "policy_config_digest": str(envelope["policy_config_digest"]),
            "context": context,
            "context_json": context_json,
            "context_digest": str(envelope["context_digest"]),
            "payload_json": binding_payload_json,
            "payload_digest": str(envelope["payload_digest"]),
            "bound_at": bound_at,
        }
        if not already_validated and self is not None:
            retained_bytes = sys.getsizeof(raw_row) + sum(
                sys.getsizeof(v) for v in raw_row
            )
            self._validated_policy_context_row = (
                raw_row
                if retained_bytes <= _POLICY_CONTEXT_VALIDATION_MAX_BYTES
                else None
            )
        return result

    _policy_session_context_from_row = _decode_session_context_row

    # ------------------------------------------------------------------
    # 2. Signal policy provenance
    # ------------------------------------------------------------------

    def _require_signal_policy_binding(
        self, signal: SignalRecord, payload: Mapping[str, object]
    ) -> None:
        binding = self.read_policy_session_context(
            signal.reference_session, binding_kind="staging"
        )
        if binding is None:
            raise LedgerConflictError(
                f"missing bound policy context for signal {signal.signal_id}/"
                f"{signal.reference_session}"
            )
        if (
            binding["epoch_id"] != signal.epoch_id
            or binding["policy_version"] != payload["policy_version"]
            or binding["context_digest"] != payload["bound_context_digest"]
        ):
            raise LedgerConflictError(
                f"signal policy binding mismatch for {signal.signal_id}/"
                f"{signal.reference_session}"
            )

    def record_signal_policy_provenance(
        self,
        signal_id: str,
        *,
        policy_version: str,
        event_key: str,
        source_event_keys: tuple[str, ...],
        strategy_tags: tuple[str, ...],
        risk_tags: tuple[str, ...],
        sector: str,
        journal_only: bool,
        order_eligible: bool,
        decision: str,
        reason_codes: tuple[str, ...],
        bound_context_digest: str,
        captured_at: datetime,
    ) -> dict[str, object]:
        """Persist immutable normalized policy metadata beside a signal."""
        _require_timezone_aware(captured_at, "captured_at")
        signals = self.signals_by_ids((signal_id,))
        if len(signals) != 1:
            raise ValueError(f"unknown signal {signal_id}")
        signal = signals[0]
        payload = _normalized_policy_payload(
            policy_version=policy_version,
            event_key=event_key,
            source_event_keys=source_event_keys,
            strategy_tags=strategy_tags,
            risk_tags=risk_tags,
            sector=sector,
            journal_only=journal_only,
            order_eligible=order_eligible,
            decision=decision,
            reason_codes=reason_codes,
            bound_context_digest=bound_context_digest,
        )
        if payload["decision"] not in {"accepted", "rejected"} or bool(
            payload["order_eligible"]
        ) != (payload["decision"] == "accepted"):
            raise ValueError(
                "signal decision must be accepted iff ingress order eligible"
            )
        if payload["event_key"] != signal.event_key:
            raise LedgerConflictError(
                f"signal policy event key mismatch for {signal_id}"
            )
        if signal.strategy not in payload["strategy_tags"]:  # type: ignore[operator]
            raise LedgerConflictError(
                f"signal policy strategy mismatch for {signal_id}"
            )
        self._require_signal_policy_binding(signal, payload)
        row = self._write_policy_envelope(
            record_type=RECORD_SIGNAL_PROVENANCE,
            record_id=signal_id,
            session=signal.reference_session,
            epoch_id=signal.epoch_id,
            payload_json=_canonical_json(payload),
            created_at=captured_at,
            conflict_label="signal policy provenance",
        )
        return self._decode_signal_policy_row(row)

    def read_signal_policy_provenance(
        self, signal_id: str
    ) -> dict[str, object] | None:
        row = self._read_policy_envelope(RECORD_SIGNAL_PROVENANCE, signal_id)
        return None if row is None else self._decode_signal_policy_row(row)

    def _decode_signal_policy_row(self, row: sqlite3.Row) -> dict[str, object]:
        signal_id = str(row["record_id"])
        payload = self._verify_envelope_row(row, "signal policy provenance")
        try:
            normalized = _normalized_policy_payload(
                policy_version=str(payload["policy_version"]),
                event_key=str(payload["event_key"]),
                source_event_keys=tuple(payload["source_event_keys"]),  # type: ignore[arg-type]
                strategy_tags=tuple(payload["strategy_tags"]),  # type: ignore[arg-type]
                risk_tags=tuple(payload["risk_tags"]),  # type: ignore[arg-type]
                sector=str(payload["sector"]),
                journal_only=bool(payload["journal_only"]),
                order_eligible=bool(payload["order_eligible"]),
                decision=str(payload["decision"]),
                reason_codes=tuple(payload["reason_codes"]),  # type: ignore[arg-type]
                bound_context_digest=str(payload["bound_context_digest"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LedgerConflictError(
                f"tampered signal policy provenance {signal_id}"
            ) from exc
        if _canonical_json(normalized) != row["payload_json"]:
            raise LedgerConflictError(f"tampered signal policy provenance {signal_id}")
        if normalized["decision"] not in {"accepted", "rejected"} or bool(
            normalized["order_eligible"]
        ) != (normalized["decision"] == "accepted"):
            raise LedgerConflictError(f"invalid signal policy decision {signal_id}")
        signals = self.signals_by_ids((signal_id,))
        if (
            len(signals) != 1
            or signals[0].event_key != normalized["event_key"]
            or signals[0].strategy not in normalized["strategy_tags"]  # type: ignore[operator]
        ):
            raise LedgerConflictError(
                f"mismatched signal policy provenance {signal_id}"
            )
        self._require_signal_policy_binding(signals[0], normalized)
        captured_at = _policy_timestamp(row["created_at"], "signal", signal_id)
        payload_json = str(row["payload_json"])
        return {
            "signal_id": signal_id,
            **normalized,
            "payload_json": payload_json,
            "payload_digest": stable_id(
                "policy_payload", "signal", signal_id, payload_json
            ),
            "captured_at": captured_at,
        }

    # ------------------------------------------------------------------
    # 3. Intent policy provenance
    # ------------------------------------------------------------------

    def _validate_intent_contributor_policy(
        self, intent_id: str, payload: Mapping[str, object]
    ) -> None:
        intent_row = self._connection.execute(
            "SELECT side FROM order_intents WHERE intent_id = ? AND cohort_id = ?",
            (intent_id, self.cohort_id),
        ).fetchone()
        if intent_row is None:
            raise LedgerConflictError(
                f"missing order intent for policy provenance {intent_id}"
            )
        entry_intent = intent_row["side"] in {"buy", "short"}
        contributors: list[dict[str, object]] = []
        for signal_id in payload["signal_ids"]:  # type: ignore[union-attr]
            companion = self.read_signal_policy_provenance(str(signal_id))
            if companion is None:
                raise LedgerConflictError(
                    f"missing signal policy provenance {signal_id} for {intent_id}"
                )
            if entry_intent and (
                companion["journal_only"] or not companion["order_eligible"]
            ):
                raise LedgerConflictError(
                    f"ineligible signal policy provenance {signal_id} for {intent_id}"
                )
            contributors.append(companion)
        expected_event_key = sorted({str(c["event_key"]) for c in contributors})[0]
        expected_sources = tuple(
            sorted({str(v) for c in contributors for v in c["source_event_keys"]})  # type: ignore[union-attr]
        )
        expected_strategies = tuple(
            sorted({str(v) for c in contributors for v in c["strategy_tags"]})  # type: ignore[union-attr]
        )
        expected_risks = tuple(
            sorted({str(v) for c in contributors for v in c["risk_tags"]})  # type: ignore[union-attr]
        )
        if (
            payload["event_key"] != expected_event_key
            or tuple(payload["source_event_keys"]) != expected_sources  # type: ignore[arg-type]
            or tuple(payload["strategy_tags"]) != expected_strategies  # type: ignore[arg-type]
            or tuple(payload["risk_tags"]) != expected_risks  # type: ignore[arg-type]
            or {str(c["sector"]) for c in contributors} != {payload["sector"]}
            or {str(c["policy_version"]) for c in contributors}
            != {payload["policy_version"]}
            or {str(c["bound_context_digest"]) for c in contributors}
            != {payload["bound_context_digest"]}
        ):
            raise LedgerConflictError(
                f"mismatched intent policy provenance {intent_id}"
            )

    def record_intent_policy_provenance(
        self,
        intent_id: str,
        *,
        signal_ids: tuple[str, ...],
        policy_version: str,
        event_key: str,
        source_event_keys: tuple[str, ...],
        strategy_tags: tuple[str, ...],
        risk_tags: tuple[str, ...],
        sector: str,
        journal_only: bool,
        order_eligible: bool,
        decision: str,
        reason_codes: tuple[str, ...],
        bound_context_digest: str,
        captured_at: datetime,
    ) -> dict[str, object]:
        """Persist the exact recommendation attribution bound to an intent."""
        _require_timezone_aware(captured_at, "captured_at")
        intent_row = self._connection.execute(
            "SELECT * FROM order_intents WHERE intent_id = ? AND cohort_id = ?",
            (intent_id, self.cohort_id),
        ).fetchone()
        if intent_row is None:
            raise ValueError(f"unknown order intent {intent_id}")
        stored_intent = self._intent_from_row(intent_row)
        payload = _normalized_policy_payload(
            policy_version=policy_version,
            event_key=event_key,
            source_event_keys=source_event_keys,
            strategy_tags=strategy_tags,
            risk_tags=risk_tags,
            sector=sector,
            journal_only=journal_only,
            order_eligible=order_eligible,
            decision=decision,
            reason_codes=reason_codes,
            bound_context_digest=bound_context_digest,
            signal_ids=signal_ids,
        )
        if tuple(payload["signal_ids"]) != stored_intent.signal_ids:  # type: ignore[arg-type]
            raise LedgerConflictError(
                f"intent policy signal provenance mismatch for {intent_id}"
            )
        if stored_intent.side in {"buy", "short"} and (
            payload["journal_only"] or not payload["order_eligible"]
        ):
            raise LedgerConflictError(
                f"entry intent has ineligible policy provenance {intent_id}"
            )
        if (
            payload["decision"] not in {"accepted", "trimmed"}
            or not payload["order_eligible"]
            or payload["journal_only"]
        ):
            raise LedgerConflictError(
                f"invalid entry intent policy decision {intent_id}"
            )
        self._validate_intent_contributor_policy(intent_id, payload)
        signals = self.signals_by_ids(stored_intent.signal_ids)
        epoch_id = signals[0].epoch_id if signals else ""
        row = self._write_policy_envelope(
            record_type=RECORD_INTENT_PROVENANCE,
            record_id=intent_id,
            session=stored_intent.eligible_session,
            epoch_id=epoch_id,
            payload_json=_canonical_json(payload),
            created_at=captured_at,
            conflict_label="intent policy provenance",
        )
        return self._decode_intent_policy_row(row)

    def read_intent_policy_provenance(
        self, intent_id: str
    ) -> dict[str, object] | None:
        row = self._read_policy_envelope(RECORD_INTENT_PROVENANCE, intent_id)
        return None if row is None else self._decode_intent_policy_row(row)

    def _decode_intent_policy_row(self, row: sqlite3.Row) -> dict[str, object]:
        intent_id = str(row["record_id"])
        payload = self._verify_envelope_row(row, "intent policy provenance")
        try:
            normalized = _normalized_policy_payload(
                policy_version=str(payload["policy_version"]),
                event_key=str(payload["event_key"]),
                source_event_keys=tuple(payload["source_event_keys"]),  # type: ignore[arg-type]
                strategy_tags=tuple(payload["strategy_tags"]),  # type: ignore[arg-type]
                risk_tags=tuple(payload["risk_tags"]),  # type: ignore[arg-type]
                sector=str(payload["sector"]),
                journal_only=bool(payload["journal_only"]),
                order_eligible=bool(payload["order_eligible"]),
                decision=str(payload["decision"]),
                reason_codes=tuple(payload["reason_codes"]),  # type: ignore[arg-type]
                bound_context_digest=str(payload["bound_context_digest"]),
                signal_ids=tuple(payload["signal_ids"]),  # type: ignore[arg-type]
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LedgerConflictError(
                f"tampered intent policy provenance {intent_id}"
            ) from exc
        if _canonical_json(normalized) != row["payload_json"]:
            raise LedgerConflictError(f"tampered intent policy provenance {intent_id}")
        stored_intent = self.intent(intent_id)
        if stored_intent is None or tuple(normalized["signal_ids"]) != stored_intent.signal_ids:  # type: ignore[arg-type]
            raise LedgerConflictError(
                f"mismatched intent policy provenance {intent_id}"
            )
        if (
            normalized["decision"] not in {"accepted", "trimmed"}
            or not normalized["order_eligible"]
            or normalized["journal_only"]
        ):
            raise LedgerConflictError(f"invalid intent policy decision {intent_id}")
        self._validate_intent_contributor_policy(intent_id, normalized)
        captured_at = _policy_timestamp(row["created_at"], "intent", intent_id)
        payload_json = str(row["payload_json"])
        return {
            "intent_id": intent_id,
            **normalized,
            "payload_json": payload_json,
            "payload_digest": stable_id(
                "policy_payload", "intent", intent_id, payload_json
            ),
            "captured_at": captured_at,
        }

    # ------------------------------------------------------------------
    # 4. Policy candidate decisions
    # ------------------------------------------------------------------

    def record_policy_candidate_decision(
        self,
        session: date,
        *,
        epoch_id: str,
        policy_version: str,
        ticker: str,
        direction: str,
        event_key: str,
        signal_ids: tuple[str, ...],
        requested_weight: float,
        approved_weight: float,
        decision: str,
        reason_codes: tuple[str, ...],
        bound_context_digest: str,
        captured_at: datetime,
    ) -> dict[str, object]:
        """Persist one attributed recommendation as the policy audit unit."""
        session = _require_exact_date(session, "session")
        _require_timezone_aware(captured_at, "captured_at")
        if direction not in {"long", "short"}:
            raise ValueError("candidate policy direction must be long or short")
        if decision not in {"accepted", "trimmed", "rejected"}:
            raise ValueError("candidate policy decision is invalid")
        if (
            not math.isfinite(requested_weight)
            or not math.isfinite(approved_weight)
            or requested_weight <= 0
            or approved_weight < 0
            or approved_weight > requested_weight
        ):
            raise ValueError("candidate policy weights are invalid")
        if decision == "accepted" and approved_weight != requested_weight:
            raise ValueError("accepted candidate must preserve requested weight")
        if decision == "trimmed" and not (0 < approved_weight < requested_weight):
            raise ValueError("trimmed candidate must reduce to positive weight")
        if decision == "rejected" and approved_weight != 0:
            raise ValueError("rejected candidate must approve zero weight")
        exact_signal_ids = tuple(str(value).strip() for value in signal_ids)
        if (
            not exact_signal_ids
            or len(set(exact_signal_ids)) != len(exact_signal_ids)
            or any(not value for value in exact_signal_ids)
            or len(exact_signal_ids) > 256
            or exact_signal_ids != tuple(sorted(exact_signal_ids))
        ):
            raise ValueError(
                "candidate policy signal_ids must be sorted, nonempty, and unique"
            )
        signals = self.signals_by_ids(exact_signal_ids)
        if len(signals) != len(exact_signal_ids) or any(
            s.epoch_id != epoch_id
            or s.reference_session != session
            or s.ticker != ticker
            or s.direction != direction
            for s in signals
        ):
            raise LedgerConflictError("candidate policy signal provenance mismatch")
        companions = [
            self.read_signal_policy_provenance(sid) for sid in exact_signal_ids
        ]
        if any(c is None for c in companions) or any(
            c["policy_version"] != policy_version
            or c["bound_context_digest"] != bound_context_digest
            or not c["order_eligible"]
            for c in companions
            if c is not None
        ):
            raise LedgerConflictError("candidate policy companion mismatch")
        expected_event_key = sorted(
            str(c["event_key"]) for c in companions if c is not None
        )[0]
        if event_key != expected_event_key:
            raise LedgerConflictError("candidate policy event identity mismatch")
        normalized_reasons = _normalized_string_tuple(
            reason_codes, "reason_codes", allow_empty=False
        )
        payload = {
            "cohort_id": self.cohort_id,
            "session": _encode(session),
            "epoch_id": str(epoch_id),
            "policy_version": str(policy_version),
            "ticker": str(ticker),
            "direction": direction,
            "event_key": str(event_key),
            "signal_ids": list(exact_signal_ids),
            "requested_weight": format(Decimal(str(requested_weight)), "f"),
            "approved_weight": format(Decimal(str(approved_weight)), "f"),
            "decision": decision,
            "reason_codes": list(normalized_reasons),
            "bound_context_digest": str(bound_context_digest),
        }
        payload_json = _canonical_json(payload)
        decision_id = stable_id("policy_candidate_decision", payload_json)
        candidate_key = (
            str(policy_version),
            str(ticker),
            direction,
            str(event_key),
            list(exact_signal_ids),
        )
        with self.transaction():
            existing_rows = self._connection.execute(
                """SELECT * FROM policy_audit_records
                   WHERE cohort_id = ? AND record_type = ? AND session = ? AND epoch_id = ?""",
                (self.cohort_id, RECORD_CANDIDATE_DECISION, _encode(session), epoch_id),
            ).fetchall()
            for existing in existing_rows:
                stored = self._decode_candidate_decision_row(existing)
                stored_key = (
                    str(stored["policy_version"]),
                    str(stored["ticker"]),
                    str(stored["direction"]),
                    str(stored["event_key"]),
                    list(stored["signal_ids"]),  # type: ignore[arg-type]
                )
                if stored_key == candidate_key:
                    if stored["payload_json"] != payload_json:
                        raise LedgerConflictError(
                            f"conflicting policy candidate decision {decision_id}"
                        )
                    return stored
            row = self._write_policy_envelope(
                record_type=RECORD_CANDIDATE_DECISION,
                record_id=decision_id,
                session=session,
                epoch_id=epoch_id,
                payload_json=payload_json,
                created_at=captured_at,
                conflict_label="policy candidate decision",
            )
        return self._decode_candidate_decision_row(row)

    def read_policy_candidate_decisions(
        self,
        start: date | None = None,
        end: date | None = None,
        *,
        epoch_id: str | None = None,
        limit: int = 4096,
    ) -> tuple[dict[str, object], ...]:
        """Read and revalidate bounded candidate policy decisions."""
        limit = _policy_projection_limit(limit, maximum=4096)
        clauses = ["cohort_id = ?", "record_type = ?"]
        values: list[object] = [self.cohort_id, RECORD_CANDIDATE_DECISION]
        if start is not None:
            clauses.append("session >= ?")
            values.append(_encode(_require_exact_date(start, "start")))
        if end is not None:
            clauses.append("session <= ?")
            values.append(_encode(_require_exact_date(end, "end")))
        if epoch_id is not None:
            exact_epoch = str(epoch_id).strip()
            if not exact_epoch:
                raise ValueError("epoch_id must not be empty")
            clauses.append("epoch_id = ?")
            values.append(exact_epoch)
        rows = self._connection.execute(
            "SELECT * FROM policy_audit_records WHERE "
            + " AND ".join(clauses)
            + " ORDER BY session, record_id LIMIT ?",
            (*values, limit + 1),
        ).fetchall()
        rows = _require_projection_bound(rows, limit, "policy candidate decision")
        return tuple(self._decode_candidate_decision_row(row) for row in rows)

    def _decode_candidate_decision_row(self, row: sqlite3.Row) -> dict[str, object]:
        payload = self._verify_envelope_row(row, "policy candidate decision")
        try:
            signal_ids = tuple(payload["signal_ids"])  # type: ignore[arg-type]
            reasons = tuple(payload["reason_codes"])  # type: ignore[arg-type]
            requested_weight = float(payload["requested_weight"])  # type: ignore[arg-type]
            approved_weight = float(payload["approved_weight"])  # type: ignore[arg-type]
            captured_at = _datetime(row["created_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise LedgerConflictError("tampered policy candidate decision") from exc
        payload_json = str(row["payload_json"])
        canonical_decision_id = stable_id("policy_candidate_decision", payload_json)
        if (
            row["record_id"] != canonical_decision_id
            or payload.get("cohort_id") != row["cohort_id"]
            or payload.get("session") != row["session"]
            or payload.get("epoch_id") != row["epoch_id"]
            or signal_ids != tuple(sorted(set(signal_ids)))
            or not reasons
            or reasons != tuple(sorted(set(reasons)))
            or any(not isinstance(r, str) or not r for r in reasons)
            or payload.get("decision") not in {"accepted", "trimmed", "rejected"}
            or not math.isfinite(requested_weight)
            or not math.isfinite(approved_weight)
            or requested_weight <= 0
            or approved_weight < 0
            or approved_weight > requested_weight
            or (payload["decision"] == "accepted" and approved_weight != requested_weight)
            or (
                payload["decision"] == "trimmed"
                and not (0 < approved_weight < requested_weight)
            )
            or (payload["decision"] == "rejected" and approved_weight != 0)
            or captured_at.tzinfo is None
            or captured_at.utcoffset() is None
        ):
            raise LedgerConflictError("tampered policy candidate decision")
        signals = self.signals_by_ids(signal_ids)
        companions = [self.read_signal_policy_provenance(sid) for sid in signal_ids]
        if (
            len(signals) != len(signal_ids)
            or any(
                s.epoch_id != row["epoch_id"]
                or s.reference_session != _date(row["session"])
                or s.ticker != payload["ticker"]
                or s.direction != payload["direction"]
                for s in signals
            )
            or any(c is None for c in companions)
            or any(
                c["policy_version"] != payload["policy_version"]
                or c["bound_context_digest"] != payload["bound_context_digest"]
                or not c["order_eligible"]
                for c in companions
                if c is not None
            )
            or payload["event_key"]
            != sorted(str(c["event_key"]) for c in companions if c is not None)[0]
        ):
            raise LedgerConflictError("candidate policy provenance mismatch")
        return {
            "decision_id": canonical_decision_id,
            **payload,
            "session": _date(row["session"]),
            "signal_ids": signal_ids,
            "reason_codes": reasons,
            "requested_weight": requested_weight,
            "approved_weight": approved_weight,
            "payload_json": payload_json,
            "payload_digest": stable_id(
                "policy_candidate_payload", canonical_decision_id, payload_json
            ),
            "captured_at": captured_at,
        }

    # ------------------------------------------------------------------
    # 5. Policy staging audit manifests
    # ------------------------------------------------------------------

    def _explicit_staging_ingress(
        self,
        session: date,
        epoch_id: str,
        policy_id: str,
        input_signal_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        """Prove exact cohort membership, including noneligible inputs."""
        rows = self._connection.execute(
            """SELECT signal_id FROM signals
               WHERE reference_session = ? AND epoch_id = ? AND policy_id = ?
               ORDER BY signal_id""",
            (_encode(session), epoch_id, policy_id),
        ).fetchall()
        scoped_ids = {str(row["signal_id"]) for row in rows}
        members = set(input_signal_ids)
        if not members <= scoped_ids:
            raise LedgerConflictError("policy staging input scope mismatch")
        owned: set[str] = set()
        ingress: list[str] = []
        for signal_id in sorted(scoped_ids):
            companion = self.read_signal_policy_provenance(signal_id)
            if companion is None:
                if signal_id in members:
                    raise LedgerConflictError("missing staging signal policy provenance")
                continue
            owned.add(signal_id)
            if companion["order_eligible"]:
                ingress.append(signal_id)
        if owned != members:
            raise LedgerConflictError("policy staging input membership mismatch")
        return tuple(ingress)

    def record_policy_staging_audit_manifest(
        self,
        session: date,
        *,
        epoch_id: str,
        policy_id: str,
        policy_version: str,
        bound_context_digest: str,
        ingress_signal_ids: tuple[str, ...],
        candidate_decision_ids: tuple[str, ...],
        committee_not_selected_ids: tuple[str, ...],
        recorded_at: datetime,
        input_signal_ids: tuple[str, ...] | None = None,
    ) -> dict[str, object]:
        """Commit the exact policy partition; new calls can prove cohort inputs."""
        session = _require_exact_date(session, "session")
        _require_timezone_aware(recorded_at, "recorded_at")
        epoch_id = str(epoch_id).strip()
        policy_id = str(policy_id).strip()
        policy_version = str(policy_version).strip()
        bound_context_digest = str(bound_context_digest).strip()
        if not all((epoch_id, policy_id, policy_version, bound_context_digest)):
            raise ValueError("policy staging manifest identity must not be empty")

        def exact_ids(values: tuple[str, ...], label: str) -> tuple[str, ...]:
            if isinstance(values, (str, bytes)):
                raise TypeError(f"{label} must be a tuple of strings")
            normalized = tuple(sorted(str(value).strip() for value in values))
            if any(not v for v in normalized) or len(set(normalized)) != len(normalized):
                raise ValueError(f"{label} must contain unique nonempty IDs")
            if len(normalized) > 4096:
                raise ValueError(f"{label} exceeds the staging audit bound")
            return normalized

        if input_signal_ids is not None and (
            isinstance(input_signal_ids, (str, bytes))
            or any(
                not isinstance(value, str) or not value or value != value.strip()
                for value in input_signal_ids
            )
        ):
            raise ValueError("input_signal_ids must contain exact nonempty string IDs")
        inputs = (
            None if input_signal_ids is None
            else exact_ids(input_signal_ids, "input_signal_ids")
        )
        ingress = exact_ids(ingress_signal_ids, "ingress_signal_ids")
        decision_ids = exact_ids(candidate_decision_ids, "candidate_decision_ids")
        nonselected = exact_ids(
            committee_not_selected_ids, "committee_not_selected_ids"
        )
        binding = self.read_policy_session_context(session, binding_kind="staging")
        if (
            binding is None
            or binding["epoch_id"] != epoch_id
            or binding["policy_version"] != policy_version
            or binding["context_digest"] != bound_context_digest
        ):
            raise LedgerConflictError("policy staging manifest binding mismatch")
        if inputs is not None:
            actual_ingress = self._explicit_staging_ingress(
                session, epoch_id, policy_id, inputs
            )
        else:
            # Preserve legacy semantics when no ownership proof exists.
            signal_rows = self._connection.execute(
                """SELECT signal_id FROM signals
                   WHERE reference_session = ? AND epoch_id = ? AND policy_id = ?
                   ORDER BY signal_id""",
                (_encode(session), epoch_id, policy_id),
            ).fetchall()
            actual_ingress = []
            for srow in signal_rows:
                companion = self.read_signal_policy_provenance(str(srow["signal_id"]))
                if companion is None:
                    raise LedgerConflictError("missing staging signal policy provenance")
                if companion["order_eligible"]:
                    actual_ingress.append(str(srow["signal_id"]))
        if ingress != tuple(sorted(actual_ingress)):
            raise LedgerConflictError("policy staging ingress partition mismatch")
        decisions: list[dict[str, object]] = []
        for did in decision_ids:
            drow = self._read_policy_envelope(RECORD_CANDIDATE_DECISION, did)
            if drow is None:
                raise LedgerConflictError(f"missing policy candidate decision {did}")
            decisions.append(self._decode_candidate_decision_row(drow))
        if any(
            d["session"] != session
            or d["epoch_id"] != epoch_id
            or d["policy_version"] != policy_version
            or d["bound_context_digest"] != bound_context_digest
            for d in decisions
        ):
            raise LedgerConflictError("policy staging candidate scope mismatch")
        covered: set[str] = set()
        for d in decisions:
            contributors = set(str(v) for v in d["signal_ids"])  # type: ignore[union-attr]
            if covered & contributors:
                raise LedgerConflictError(
                    "policy staging candidate contributors overlap"
                )
            covered.update(contributors)
        if set(nonselected) & covered or covered | set(nonselected) != set(ingress):
            raise LedgerConflictError("policy staging decision partition mismatch")
        payload = {
            "cohort_id": self.cohort_id,
            "session": _encode(session),
            "epoch_id": epoch_id,
            "policy_id": policy_id,
            "policy_version": policy_version,
            "bound_context_digest": bound_context_digest,
            "ingress_signal_ids": list(ingress),
            "candidate_decision_ids": list(decision_ids),
            "committee_not_selected_ids": list(nonselected),
        }
        if inputs is not None:
            payload["input_signal_ids"] = list(inputs)
        record_id = f"{session.isoformat()}:{epoch_id}:{policy_id}"
        row = self._write_policy_envelope(
            record_type=RECORD_STAGING_MANIFEST,
            record_id=record_id,
            session=session,
            epoch_id=epoch_id,
            payload_json=_canonical_json(payload),
            created_at=recorded_at,
            conflict_label="policy staging audit manifest",
        )
        return self._decode_staging_manifest_row(row, require_complete=False)

    def read_policy_staging_audit_manifests(
        self,
        *,
        epoch_id: str | None = None,
        limit: int = 256,
    ) -> tuple[dict[str, object], ...]:
        """Read and revalidate bounded completed staging audit partitions."""
        limit = _policy_projection_limit(limit, maximum=4096)
        clauses = ["cohort_id = ?", "record_type = ?"]
        values: list[object] = [self.cohort_id, RECORD_STAGING_MANIFEST]
        if epoch_id is not None:
            exact_epoch = str(epoch_id).strip()
            if not exact_epoch:
                raise ValueError("epoch_id must not be empty")
            clauses.append("epoch_id = ?")
            values.append(exact_epoch)
        rows = self._connection.execute(
            "SELECT * FROM policy_audit_records WHERE "
            + " AND ".join(clauses)
            + " ORDER BY session, record_id LIMIT ?",
            (*values, limit + 1),
        ).fetchall()
        rows = _require_projection_bound(rows, limit, "staging audit manifest")
        return tuple(self._decode_staging_manifest_row(row) for row in rows)

    def _decode_staging_manifest_row(
        self, row: sqlite3.Row, *, require_complete: bool = True
    ) -> dict[str, object]:
        payload = self._verify_envelope_row(row, "policy staging audit manifest")
        try:
            if "input_signal_ids" in payload and not isinstance(payload["input_signal_ids"], list):
                raise TypeError("input_signal_ids must be an array")
            inputs = (
                tuple(payload["input_signal_ids"])  # type: ignore[arg-type]
                if "input_signal_ids" in payload else None
            )
            ingress = tuple(payload["ingress_signal_ids"])  # type: ignore[arg-type]
            decisions = tuple(payload["candidate_decision_ids"])  # type: ignore[arg-type]
            nonselected = tuple(payload["committee_not_selected_ids"])  # type: ignore[arg-type]
            policy_id = str(payload["policy_id"])
            policy_version = str(payload["policy_version"])
            bound_context_digest = str(payload["bound_context_digest"])
        except (KeyError, TypeError) as exc:
            raise LedgerConflictError("tampered policy staging audit manifest") from exc
        try:
            recorded_at = _policy_timestamp(
                row["created_at"], "staging manifest", str(row["record_id"])
            )
        except LedgerConflictError as exc:
            raise LedgerConflictError(
                "invalid policy staging audit manifest timestamp"
            ) from exc

        def canonical_ids(values: tuple[object, ...]) -> bool:
            return (
                all(
                    isinstance(v, str) and bool(v) and v == v.strip() for v in values
                )
                and tuple(sorted(set(values))) == values
            )

        if (
            (inputs is not None and (not canonical_ids(inputs) or len(inputs) > 4096))
            or not canonical_ids(ingress)
            or not canonical_ids(decisions)
            or not canonical_ids(nonselected)
            or payload.get("cohort_id") != self.cohort_id
            or payload.get("session") != row["session"]
            or payload.get("epoch_id") != row["epoch_id"]
            or not all((policy_id.strip(), policy_version.strip(), bound_context_digest.strip()))
        ):
            raise LedgerConflictError("tampered policy staging audit manifest")
        session_date = _date(row["session"])
        binding = self.read_policy_session_context(
            session_date, binding_kind="staging"
        )
        if (
            binding is None
            or binding["epoch_id"] != row["epoch_id"]
            or binding["policy_version"] != policy_version
            or binding["context_digest"] != bound_context_digest
        ):
            raise LedgerConflictError("policy staging audit binding mismatch")
        if inputs is not None:
            actual_ingress = self._explicit_staging_ingress(
                session_date, str(row["epoch_id"]), policy_id, inputs
            )
        else:
            signal_rows = self._connection.execute(
                """SELECT signal_id FROM signals
                   WHERE reference_session = ? AND epoch_id = ? AND policy_id = ?
                   ORDER BY signal_id""",
                (row["session"], row["epoch_id"], policy_id),
            ).fetchall()
            actual_ingress = tuple(
                sorted(
                    str(item["signal_id"])
                    for item in signal_rows
                    if (
                        (companion := self.read_signal_policy_provenance(str(item["signal_id"])))
                        is not None
                        and companion["order_eligible"]
                    )
                )
            )
        if actual_ingress != ingress:
            raise LedgerConflictError("policy staging audit ingress mismatch")
        scoped_rows = self._connection.execute(
            """SELECT * FROM policy_audit_records
               WHERE cohort_id = ? AND record_type = ? AND session = ? AND epoch_id = ?""",
            (self.cohort_id, RECORD_CANDIDATE_DECISION, row["session"], row["epoch_id"]),
        ).fetchall()
        scoped_decisions: dict[str, dict[str, object]] = {}
        for crow in scoped_rows:
            candidate = self._decode_candidate_decision_row(crow)
            if (
                candidate["policy_version"] != policy_version
                or candidate["bound_context_digest"] != bound_context_digest
            ):
                continue
            contributor_signals = self.signals_by_ids(
                tuple(str(v) for v in candidate["signal_ids"])  # type: ignore[union-attr]
            )
            if contributor_signals and {s.policy_id for s in contributor_signals} == {
                policy_id
            }:
                scoped_decisions[str(candidate["decision_id"])] = candidate
        missing = set(decisions) - set(scoped_decisions)
        if missing:
            raise LedgerConflictError(
                f"missing policy candidate decision {sorted(missing)[0]}"
            )
        if set(scoped_decisions) - set(decisions):
            raise LedgerConflictError(
                "policy staging audit candidate set is incomplete"
            )
        covered: set[str] = set()
        for did in decisions:
            d = scoped_decisions[str(did)]
            contributors = set(str(v) for v in d["signal_ids"])  # type: ignore[union-attr]
            if covered & contributors:
                raise LedgerConflictError(
                    "policy staging audit candidate contributors overlap"
                )
            covered.update(contributors)
        if covered & set(nonselected) or covered | set(nonselected) != set(ingress):
            raise LedgerConflictError("policy staging audit partition mismatch")
        if require_complete and not self.staging_completed(
            session_date, str(row["epoch_id"]), policy_id
        ):
            raise LedgerConflictError("policy staging audit is incomplete")
        payload_json = str(row["payload_json"])
        manifest_id = stable_id("policy_staging_audit_manifest", payload_json)
        return {
            "manifest_id": manifest_id,
            **payload,
            **({"input_signal_ids": inputs} if inputs is not None else {}),
            "session": session_date,
            "ingress_signal_ids": ingress,
            "candidate_decision_ids": decisions,
            "committee_not_selected_ids": nonselected,
            "payload_json": payload_json,
            "payload_digest": stable_id(
                "policy_staging_audit_payload", manifest_id, payload_json
            ),
            "recorded_at": recorded_at,
        }

    # ------------------------------------------------------------------
    # 6. Projections & consumed event keys
    # ------------------------------------------------------------------

    def policy_pending_entry_projection(
        self,
        exclude_intent_id: str | None = None,
        *,
        limit: int = 256,
    ) -> tuple[dict[str, object], ...]:
        """Return every outstanding entry, including future-eligible intents."""
        limit = _policy_projection_limit(limit)
        clauses = [
            "i.cohort_id = ?",
            "i.status = 'pending'",
            "i.side IN ('buy', 'short')",
        ]
        values: list[object] = [self.cohort_id]
        if exclude_intent_id is not None:
            clauses.append("i.intent_id != ?")
            values.append(str(exclude_intent_id))
        rows = self._connection.execute(
            "SELECT i.* FROM order_intents i WHERE "
            + " AND ".join(clauses)
            + " ORDER BY i.eligible_session, i.created_at, i.intent_id LIMIT ?",
            (*values, limit + 1),
        ).fetchall()
        rows = _require_projection_bound(rows, limit, "pending policy")
        projection: list[dict[str, object]] = []
        for row in rows:
            intent = self._intent_from_row(row)
            provenance = self.read_intent_policy_provenance(intent.intent_id)
            if provenance is None:
                raise LedgerConflictError(
                    f"missing intent policy provenance {intent.intent_id}"
                )
            if provenance["journal_only"] or not provenance["order_eligible"]:
                raise LedgerConflictError(
                    f"ineligible pending entry provenance {intent.intent_id}"
                )
            signals = self.signals_by_ids(intent.signal_ids)
            if len(signals) != len(intent.signal_ids):
                raise LedgerConflictError(
                    f"missing signal provenance for pending intent {intent.intent_id}"
                )
            tickers = {s.ticker for s in signals}
            ref_closes = {s.reference_close for s in signals}
            if len(tickers) != 1:
                raise LedgerConflictError(
                    f"pending intent {intent.intent_id} has ambiguous ticker"
                )
            if len(ref_closes) != 1:
                raise LedgerConflictError(
                    f"pending intent {intent.intent_id} requires unambiguous reference_close"
                )
            ticker = next(iter(tickers))
            reference_close = next(iter(ref_closes))
            if not reference_close.is_finite() or reference_close <= 0:
                raise LedgerConflictError(
                    f"pending intent {intent.intent_id} has invalid reference_close"
                )
            projection.append(
                {
                    "intent_id": intent.intent_id,
                    "ticker": ticker,
                    "direction": "long" if intent.side == "buy" else "short",
                    "quantity": intent.requested_qty,
                    "reference_close": reference_close,
                    "marked_value": Decimal(intent.requested_qty) * reference_close,
                    "eligible_session": intent.eligible_session,
                    "event_key": provenance["event_key"],
                    "source_event_keys": provenance["source_event_keys"],
                    "strategy_tags": provenance["strategy_tags"],
                    "risk_tags": provenance["risk_tags"],
                    "sector": provenance["sector"],
                    "policy_version": provenance["policy_version"],
                    "bound_context_digest": provenance["bound_context_digest"],
                }
            )
        return tuple(projection)

    def policy_open_lot_projection(
        self,
        session: date,
        *,
        limit: int = 256,
        prices: Mapping[str, Decimal] | None = None,
    ) -> tuple[dict[str, object], ...]:
        """Return marked open lots using only persisted raw ledger evidence."""
        session = _require_exact_date(session, "session")
        limit = _policy_projection_limit(limit)
        rows = self._connection.execute(
            """SELECT l.*, f.intent_id,
                      f.reference_price AS opening_reference_price,
                      m.close AS mark_close, m.session AS mark_session,
                      m.source AS mark_source
               FROM lots l
               JOIN fills f ON f.fill_id = l.fill_id
               LEFT JOIN marks m ON m.mark_id = (
                   SELECT candidate.mark_id FROM marks candidate
                   WHERE candidate.ticker = l.ticker
                     AND candidate.adjusted = 0
                     AND candidate.session >= l.opened_session
                     AND candidate.session <= ?
                   ORDER BY candidate.session DESC, candidate.mark_id DESC
                   LIMIT 1
               )
               WHERE l.cohort_id = ? AND l.open_qty > 0
               ORDER BY l.ticker, l.direction, l.opened_session, l.lot_id
               LIMIT ?""",
            (_encode(session), self.cohort_id, limit + 1),
        ).fetchall()
        rows = _require_projection_bound(rows, limit, "open-lot policy")
        projection: list[dict[str, object]] = []
        for row in rows:
            opened_session = _date(row["opened_session"])
            if opened_session > session:
                raise LedgerConflictError(
                    f"open lot {row['lot_id']} is from future session"
                )
            provenance = self.read_intent_policy_provenance(str(row["intent_id"]))
            if provenance is None:
                raise LedgerConflictError(
                    f"missing intent policy provenance {row['intent_id']}"
                )
            if prices is not None:
                mark = prices.get(str(row["ticker"]))
                if not isinstance(mark, Decimal):
                    raise MissingMarkError(
                        f"missing exact opening mark for {row['ticker']}"
                    )
                mark_session = session
                mark_source = "bound_session_open"
            elif row["mark_close"] is None:
                if opened_session != session:
                    raise MissingMarkError(
                        f"missing persisted raw mark for {row['ticker']}/{session}"
                    )
                mark = _decimal(row["opening_reference_price"])
                mark_session = session
                mark_source = "opening_reference"
            else:
                mark = _decimal(row["mark_close"])
                mark_session = _date(row["mark_session"])
                mark_source = str(row["mark_source"])
            quantity = int(row["open_qty"])
            if not mark.is_finite() or mark <= 0:
                raise MissingMarkError(
                    f"invalid persisted raw mark for {row['ticker']}/{session}"
                )
            if quantity <= 0:
                raise LedgerConflictError(
                    f"invalid open quantity for lot {row['lot_id']}"
                )
            projection.append(
                {
                    "lot_id": str(row["lot_id"]),
                    "intent_id": str(row["intent_id"]),
                    "ticker": str(row["ticker"]),
                    "direction": str(row["direction"]),
                    "quantity": quantity,
                    "marked_value": Decimal(quantity) * mark,
                    "mark": mark,
                    "mark_session": mark_session,
                    "mark_source": mark_source,
                    "event_key": provenance["event_key"],
                    "source_event_keys": provenance["source_event_keys"],
                    "strategy_tags": provenance["strategy_tags"],
                    "risk_tags": provenance["risk_tags"],
                    "sector": provenance["sector"],
                    "policy_version": provenance["policy_version"],
                    "bound_context_digest": provenance["bound_context_digest"],
                }
            )
        return tuple(projection)

    def consumed_event_keys(self, *, limit: int = 4096) -> frozenset[str]:
        """Return event identities consumed by authoritative filled entries only."""
        limit = _policy_projection_limit(limit, maximum=4096)
        rows = self._connection.execute(
            """SELECT i.intent_id FROM order_intents i
               WHERE i.cohort_id = ? AND i.status = 'filled'
                 AND i.side IN ('buy', 'short')
               ORDER BY i.eligible_session, i.created_at, i.intent_id LIMIT ?""",
            (self.cohort_id, limit + 1),
        ).fetchall()
        rows = _require_projection_bound(rows, limit, "consumed-event")
        consumed: set[str] = set()
        for row in rows:
            intent_id = str(row["intent_id"])
            provenance = self.read_intent_policy_provenance(intent_id)
            if provenance is None:
                raise LedgerConflictError(
                    f"missing intent policy provenance {intent_id}"
                )
            if provenance["journal_only"] or not provenance["order_eligible"]:
                raise LedgerConflictError(
                    f"ineligible filled entry provenance {intent_id}"
                )
            for signal_id in provenance["signal_ids"]:  # type: ignore[union-attr]
                signal_provenance = self.read_signal_policy_provenance(str(signal_id))
                if signal_provenance is None:
                    raise LedgerConflictError(
                        f"missing signal policy provenance {signal_id} for {intent_id}"
                    )
                consumed.add(str(signal_provenance["event_key"]))
                if len(consumed) > limit:
                    raise LedgerConflictError(
                        f"consumed-event projection limit {limit} exceeded; refusing truncation"
                    )
        return frozenset(consumed)
