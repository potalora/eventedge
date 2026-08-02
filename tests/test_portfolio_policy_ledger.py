from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from tradingagents.strategies.execution import (
    Fill,
    MarketBar,
    OrderIntent,
    SignalRecord,
    stable_id,
)
from tradingagents.strategies.state.portfolio_ledger import (
    LedgerConflictError,
    MissingMarkError,
    PortfolioLedger,
)


UTC = timezone.utc
COHORT = "horizon_30d_size_5k"
FRIDAY = date(2026, 7, 31)
MONDAY = date(2026, 8, 3)
TUESDAY = date(2026, 8, 4)
CAPTURED_AT = datetime(2026, 7, 31, 20, tzinfo=UTC)


def _ledger(tmp_path) -> PortfolioLedger:
    return PortfolioLedger(tmp_path / "portfolio.db", COHORT, Decimal("5000"))


def _bind_policy_context(
    ledger: PortfolioLedger,
    *,
    session: date = FRIDAY,
    epoch_id: str = "epoch-1",
    policy_version: str = "portfolio_policy_v1",
) -> dict[str, object]:
    return ledger.bind_policy_session_context(
        session,
        epoch_id=epoch_id,
        policy_version=policy_version,
        policy_config={"max_positions": 5},
        context={"cash": "5000", "positions": []},
        bound_at=CAPTURED_AT,
    )


def _signal(
    signal_id: str = "signal-1",
    *,
    ticker: str = "AAPL",
    event_key: str = "event-1",
    strategy: str = "litigation",
    reference_close: Decimal = Decimal("100"),
) -> SignalRecord:
    return SignalRecord(
        signal_id=signal_id,
        epoch_id="epoch-1",
        policy_id="param-policy-1",
        event_key=event_key,
        strategy=strategy,
        ticker=ticker,
        direction="long",
        event_at=CAPTURED_AT,
        observed_at=CAPTURED_AT,
        reference_session=FRIDAY,
        reference_close=reference_close,
        decision_at=CAPTURED_AT,
        evidence_hash=f"evidence-{signal_id}",
    )


def _intent(
    intent_id: str = "intent-1",
    *,
    signal_ids: tuple[str, ...] = ("signal-1",),
    side: str = "buy",
    requested_qty: int = 10,
    eligible_session: date = MONDAY,
) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        signal_ids=signal_ids,
        cohort_id=COHORT,
        side=side,
        requested_qty=requested_qty,
        created_at=CAPTURED_AT,
        eligible_session=eligible_session,
        price_rule="next_session_open",
        status="pending",
        stop_price=None,
        external_order_id=None,
    )


def _fill(intent_id: str = "intent-1", *, session: date = MONDAY) -> Fill:
    effective = datetime(2026, 8, session.day, 13, 30, tzinfo=UTC)
    return Fill(
        fill_id=f"fill-{intent_id}",
        intent_id=intent_id,
        side="buy",
        session=session,
        effective_at=effective,
        processed_at=effective,
        reference_price=Decimal("101"),
        fill_price=Decimal("101.10"),
        quantity=10,
        slippage=Decimal("1"),
        commission=Decimal("0"),
        other_fees=Decimal("0"),
    )


def _bar(session: date, close: Decimal) -> MarketBar:
    return MarketBar(
        ticker="AAPL",
        session=session,
        open=close,
        high=close,
        low=close,
        close=close,
        source="fixture",
        fetched_at=datetime(2026, 8, session.day, 21, tzinfo=UTC),
        adjusted=False,
    )


def _record_signal_policy(
    ledger: PortfolioLedger,
    signal: SignalRecord,
    *,
    sector: str = "Technology",
) -> dict[str, object]:
    ledger.record_signal(signal)
    binding = _bind_policy_context(
        ledger, session=signal.reference_session, epoch_id=signal.epoch_id
    )
    return ledger.record_signal_policy_provenance(
        signal.signal_id,
        policy_version="portfolio_policy_v1",
        event_key=signal.event_key,
        source_event_keys=(f"source:{signal.event_key}",),
        strategy_tags=(signal.strategy,),
        risk_tags=(f"event:{signal.event_key}",),
        sector=sector,
        journal_only=False,
        order_eligible=True,
        decision="accepted",
        reason_codes=("accepted",),
        bound_context_digest=str(binding["context_digest"]),
        captured_at=CAPTURED_AT,
    )


def _record_intent_policy(
    ledger: PortfolioLedger,
    intent: OrderIntent,
    signal: SignalRecord,
    *,
    sector: str = "Technology",
) -> dict[str, object]:
    signal_policy = ledger.read_signal_policy_provenance(signal.signal_id)
    assert signal_policy is not None
    return ledger.record_intent_policy_provenance(
        intent.intent_id,
        signal_ids=intent.signal_ids,
        policy_version="portfolio_policy_v1",
        event_key=signal.event_key,
        source_event_keys=(f"source:{signal.event_key}",),
        strategy_tags=(signal.strategy,),
        risk_tags=(f"event:{signal.event_key}",),
        sector=sector,
        journal_only=False,
        order_eligible=True,
        decision="accepted",
        reason_codes=("accepted",),
        bound_context_digest=str(signal_policy["bound_context_digest"]),
        captured_at=CAPTURED_AT,
    )


def _stage_policy_entry(
    ledger: PortfolioLedger,
    *,
    signal: SignalRecord | None = None,
    intent: OrderIntent | None = None,
) -> tuple[SignalRecord, OrderIntent]:
    signal = signal or _signal()
    intent = intent or _intent(signal_ids=(signal.signal_id,))
    _record_signal_policy(ledger, signal)
    ledger.stage_intent(intent)
    _record_intent_policy(ledger, intent, signal)
    return signal, intent


def test_companion_provenance_is_insert_once_reopenable_and_normalized(tmp_path) -> None:
    path = tmp_path / "portfolio.db"
    ledger = _ledger(tmp_path)
    signal = _signal()
    intent = _intent()
    try:
        signal_policy = _record_signal_policy(ledger, signal)
        ledger.stage_intent(intent)
        intent_policy = _record_intent_policy(ledger, intent, signal)

        replay = ledger.record_intent_policy_provenance(
            intent.intent_id,
            signal_ids=intent.signal_ids,
            policy_version="portfolio_policy_v1",
            event_key=signal.event_key,
            source_event_keys=("source:event-1", "source:event-1"),
            strategy_tags=("litigation", "litigation"),
            risk_tags=("event:event-1",),
            sector="Technology",
            journal_only=False,
            order_eligible=True,
            decision="accepted",
            reason_codes=("accepted", "accepted"),
            bound_context_digest=str(signal_policy["bound_context_digest"]),
            captured_at=datetime(2026, 8, 1, 20, tzinfo=UTC),
        )
        assert replay == intent_policy
        assert signal_policy["source_event_keys"] == ("source:event-1",)
        assert intent_policy["strategy_tags"] == ("litigation",)
        assert json.loads(str(intent_policy["payload_json"]))["signal_ids"] == [
            "signal-1"
        ]
    finally:
        ledger.close()

    reopened = PortfolioLedger.open_existing(path)
    try:
        assert reopened.read_signal_policy_provenance("signal-1") == signal_policy
        assert reopened.read_intent_policy_provenance("intent-1") == intent_policy
    finally:
        reopened.close()


def test_companion_provenance_conflict_and_tamper_fail_closed(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    signal, intent = _stage_policy_entry(ledger)
    try:
        signal_policy = ledger.read_signal_policy_provenance(signal.signal_id)
        assert signal_policy is not None
        with pytest.raises(LedgerConflictError, match="intent policy provenance"):
            ledger.record_intent_policy_provenance(
                intent.intent_id,
                signal_ids=intent.signal_ids,
                policy_version="portfolio_policy_v1",
                event_key=signal.event_key,
                source_event_keys=("source:event-1",),
                strategy_tags=("changed-strategy",),
                risk_tags=("event:event-1",),
                sector="Technology",
                journal_only=False,
                order_eligible=True,
                decision="accepted",
                reason_codes=("accepted",),
                bound_context_digest=str(signal_policy["bound_context_digest"]),
                captured_at=CAPTURED_AT,
            )

        ledger.connection.execute(
            "UPDATE intent_policy_provenance SET payload_json = ? WHERE intent_id = ?",
            ('{"tampered":true}', intent.intent_id),
        )
        with pytest.raises(LedgerConflictError, match="tampered intent policy"):
            ledger.read_intent_policy_provenance(intent.intent_id)
    finally:
        ledger.close()


@pytest.mark.parametrize(
    "artifact",
    (
        "signal_empty_reasons",
        "signal_naive_time",
        "intent_empty_reasons",
        "intent_naive_time",
    ),
)
def test_companion_reads_reject_coherently_rehashed_invalid_payloads_on_reopen(
    tmp_path, artifact: str
) -> None:
    path = tmp_path / "portfolio.db"
    ledger = _ledger(tmp_path)
    try:
        _stage_policy_entry(ledger)
        if artifact in {"signal_empty_reasons", "intent_empty_reasons"}:
            kind = artifact.split("_", 1)[0]
            table = f"{kind}_policy_provenance"
            identity_column = f"{kind}_id"
            identity = f"{kind}-1"
            row = ledger.connection.execute(
                f"SELECT payload_json FROM {table} WHERE {identity_column} = ?",
                (identity,),
            ).fetchone()
            payload = json.loads(str(row["payload_json"]))
            payload["reason_codes"] = []
            payload_json = json.dumps(
                payload, sort_keys=True, separators=(",", ":")
            )
            ledger.connection.execute(
                f"""UPDATE {table}
                   SET reason_codes_json = '[]', payload_json = ?, payload_digest = ?
                   WHERE {identity_column} = ?""",
                (
                    payload_json,
                    stable_id("policy_payload", kind, identity, payload_json),
                    identity,
                ),
            )
        else:
            kind = artifact.split("_", 1)[0]
            ledger.connection.execute(
                f"""UPDATE {kind}_policy_provenance
                   SET captured_at = '2026-07-31T20:00:00'
                   WHERE {kind}_id = ?""",
                (f"{kind}-1",),
            )
    finally:
        ledger.close()

    reopened = PortfolioLedger.open_existing(path)
    try:
        with pytest.raises(LedgerConflictError, match="policy"):
            if artifact.startswith("signal_"):
                reopened.read_signal_policy_provenance("signal-1")
            else:
                reopened.read_intent_policy_provenance("intent-1")
    finally:
        reopened.close()


def test_noncanonical_companion_column_tamper_raises_ledger_conflict(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    try:
        _stage_policy_entry(ledger)
        ledger.connection.execute(
            "UPDATE intent_policy_provenance SET strategy_tags_json = '{' "
            "WHERE intent_id = 'intent-1'"
        )

        with pytest.raises(LedgerConflictError, match="tampered intent policy"):
            ledger.read_intent_policy_provenance("intent-1")
    finally:
        ledger.close()


def test_non_boolean_companion_flag_tamper_raises_ledger_conflict(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    try:
        _stage_policy_entry(ledger)
        ledger.connection.execute(
            "UPDATE intent_policy_provenance SET order_eligible = 2 "
            "WHERE intent_id = 'intent-1'"
        )

        with pytest.raises(LedgerConflictError, match="tampered intent policy"):
            ledger.read_intent_policy_provenance("intent-1")
    finally:
        ledger.close()


def test_bound_policy_context_is_canonical_idempotent_and_tamper_evident(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    try:
        first = ledger.bind_policy_session_context(
            MONDAY,
            epoch_id="epoch-1",
            policy_version="portfolio_policy_v1",
            policy_config={"max_positions": 5, "limits": {"sector": 0.5}},
            context={"cash": "5000", "positions": []},
            bound_at=datetime(2026, 8, 3, 12, tzinfo=UTC),
        )
        replay = ledger.bind_policy_session_context(
            MONDAY,
            epoch_id="epoch-1",
            policy_version="portfolio_policy_v1",
            policy_config={"limits": {"sector": 0.5}, "max_positions": 5},
            context={"positions": [], "cash": "5000"},
            bound_at=datetime(2026, 8, 3, 13, tzinfo=UTC),
        )
        assert replay == first
        assert replay["bound_at"] == datetime(2026, 8, 3, 12, tzinfo=UTC)
        assert replay["policy_config_digest"]
        assert replay["context_digest"]

        with pytest.raises(LedgerConflictError, match="policy session context"):
            ledger.bind_policy_session_context(
                MONDAY,
                epoch_id="epoch-1",
                policy_version="portfolio_policy_v1",
                policy_config={"max_positions": 6, "limits": {"sector": 0.5}},
                context={"cash": "5000", "positions": []},
                bound_at=datetime(2026, 8, 3, 13, tzinfo=UTC),
            )

        ledger.connection.execute(
            "UPDATE policy_session_contexts SET context_digest = 'bad' WHERE session = ?",
            (MONDAY.isoformat(),),
        )
        with pytest.raises(LedgerConflictError, match="tampered policy session context"):
            ledger.read_policy_session_context(MONDAY)
    finally:
        ledger.close()


def test_staging_and_execution_policy_bindings_coexist_for_same_session(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    try:
        staging = _bind_policy_context(ledger, session=MONDAY)
        execution = ledger.bind_policy_session_context(
            MONDAY,
            binding_kind="execution",
            epoch_id="epoch-1",
            policy_version="portfolio_policy_v1",
            policy_config={"max_positions": 5},
            context={"cash": "4900", "positions": ["AAPL"]},
            bound_at=datetime(2026, 8, 3, 21, tzinfo=UTC),
        )

        assert staging["binding_kind"] == "staging"
        assert execution["binding_kind"] == "execution"
        assert staging["context_digest"] != execution["context_digest"]
        assert ledger.read_policy_session_context(MONDAY) == staging
        assert (
            ledger.read_policy_session_context(
                MONDAY, binding_kind="execution"
            )
            == execution
        )
        with pytest.raises(ValueError, match="binding_kind"):
            ledger.read_policy_session_context(
                MONDAY, binding_kind="unknown"
            )
    finally:
        ledger.close()


def test_candidate_policy_decision_is_one_tamper_evident_multi_signal_unit(
    tmp_path,
) -> None:
    path = tmp_path / "portfolio.db"
    ledger = _ledger(tmp_path)
    first = _signal("signal-a", event_key="event-a")
    second = _signal(
        "signal-b", event_key="event-b", strategy="earnings_call"
    )
    try:
        _record_signal_policy(ledger, first)
        _record_signal_policy(ledger, second)
        binding = ledger.read_policy_session_context(FRIDAY)
        assert binding is not None
        decision = ledger.record_policy_candidate_decision(
            FRIDAY,
            epoch_id="epoch-1",
            policy_version="portfolio_policy_v1",
            ticker="AAPL",
            direction="long",
            event_key="event-a",
            signal_ids=(first.signal_id, second.signal_id),
            requested_weight=0.25,
            approved_weight=0.20,
            decision="trimmed",
            reason_codes=("max_sector",),
            bound_context_digest=str(binding["context_digest"]),
            captured_at=CAPTURED_AT,
        )
        assert decision["signal_ids"] == ("signal-a", "signal-b")
        assert decision["decision"] == "trimmed"
        assert len(ledger.read_policy_candidate_decisions()) == 1
    finally:
        ledger.close()

    tamper = sqlite3.connect(path)
    tamper.execute(
        "UPDATE policy_candidate_decisions SET approved_weight = '0.19'"
    )
    tamper.commit()
    tamper.close()
    reopened = PortfolioLedger.open_existing(path)
    try:
        with pytest.raises(LedgerConflictError, match="tampered"):
            reopened.read_policy_candidate_decisions()
    finally:
        reopened.close()


def test_candidate_policy_decision_rejects_coherent_payload_rewrite(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    signal = _signal()
    try:
        _record_signal_policy(ledger, signal)
        binding = ledger.read_policy_session_context(FRIDAY)
        assert binding is not None
        decision = ledger.record_policy_candidate_decision(
            FRIDAY,
            epoch_id="epoch-1",
            policy_version="portfolio_policy_v1",
            ticker="AAPL",
            direction="long",
            event_key=signal.event_key,
            signal_ids=(signal.signal_id,),
            requested_weight=0.25,
            approved_weight=0.25,
            decision="accepted",
            reason_codes=("accepted",),
            bound_context_digest=str(binding["context_digest"]),
            captured_at=CAPTURED_AT,
        )
        payload = json.loads(str(decision["payload_json"]))
        payload.update(
            {
                "approved_weight": "0.1",
                "decision": "trimmed",
                "reason_codes": ["position_cap"],
            }
        )
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        payload_digest = stable_id(
            "policy_candidate_payload", decision["decision_id"], payload_json
        )
        ledger.connection.execute(
            """UPDATE policy_candidate_decisions
               SET approved_weight = ?, decision = ?, reason_codes_json = ?,
                   payload_json = ?, payload_digest = ?
               WHERE decision_id = ?""",
            (
                "0.1",
                "trimmed",
                '["position_cap"]',
                payload_json,
                payload_digest,
                decision["decision_id"],
            ),
        )

        with pytest.raises(LedgerConflictError, match="tampered"):
            ledger.read_policy_candidate_decisions()
    finally:
        ledger.close()


def test_staging_audit_manifest_is_atomic_idempotent_and_revalidates_on_reopen(
    tmp_path,
) -> None:
    path = tmp_path / "portfolio.db"
    ledger = _ledger(tmp_path)
    first = _signal("signal-a", event_key="event-a")
    second = _signal("signal-b", event_key="event-b")
    try:
        _record_signal_policy(ledger, first)
        _record_signal_policy(ledger, second)
        binding = ledger.read_policy_session_context(FRIDAY)
        assert binding is not None
        decision = ledger.record_policy_candidate_decision(
            FRIDAY,
            epoch_id="epoch-1",
            policy_version="portfolio_policy_v1",
            ticker="AAPL",
            direction="long",
            event_key=first.event_key,
            signal_ids=(first.signal_id,),
            requested_weight=0.20,
            approved_weight=0.20,
            decision="accepted",
            reason_codes=("accepted",),
            bound_context_digest=str(binding["context_digest"]),
            captured_at=CAPTURED_AT,
        )

        def record_manifest() -> dict[str, object]:
            return ledger.record_policy_staging_audit_manifest(
                FRIDAY,
                epoch_id="epoch-1",
                policy_id="param-policy-1",
                policy_version="portfolio_policy_v1",
                bound_context_digest=str(binding["context_digest"]),
                ingress_signal_ids=(first.signal_id, second.signal_id),
                candidate_decision_ids=(str(decision["decision_id"]),),
                committee_not_selected_ids=(second.signal_id,),
                recorded_at=CAPTURED_AT,
            )

        ledger.complete_staging(
            FRIDAY,
            "epoch-1",
            "param-policy-1",
            CAPTURED_AT,
            record_manifest,
            ledger.execution_governed_state_digest(FRIDAY),
        )
        stored = ledger.read_policy_staging_audit_manifests(epoch_id="epoch-1")
        assert len(stored) == 1
        assert record_manifest() == stored[0]
    finally:
        ledger.close()

    reopened = PortfolioLedger.open_existing(path)
    try:
        assert len(reopened.read_policy_staging_audit_manifests()) == 1
    finally:
        reopened.close()

    mutable = _ledger(tmp_path)
    try:
        binding = mutable.read_policy_session_context(FRIDAY)
        assert binding is not None
        extra = mutable.record_policy_candidate_decision(
            FRIDAY,
            epoch_id="epoch-1",
            policy_version="portfolio_policy_v1",
            ticker="AAPL",
            direction="long",
            event_key=second.event_key,
            signal_ids=(second.signal_id,),
            requested_weight=0.10,
            approved_weight=0.10,
            decision="accepted",
            reason_codes=("accepted",),
            bound_context_digest=str(binding["context_digest"]),
            captured_at=CAPTURED_AT,
        )
        with pytest.raises(LedgerConflictError, match="candidate set is incomplete"):
            mutable.read_policy_staging_audit_manifests()
        mutable.connection.execute(
            "DELETE FROM policy_candidate_decisions WHERE decision_id = ?",
            (extra["decision_id"],),
        )
    finally:
        mutable.close()

    tamper = sqlite3.connect(path)
    tamper.execute("DELETE FROM policy_candidate_decisions")
    tamper.commit()
    tamper.close()
    reopened = PortfolioLedger.open_existing(path)
    try:
        with pytest.raises(LedgerConflictError, match="missing policy candidate"):
            reopened.read_policy_staging_audit_manifests()
    finally:
        reopened.close()


def test_staging_audit_manifest_rejects_overlapping_candidate_contributors(
    tmp_path,
) -> None:
    ledger = _ledger(tmp_path)
    first = _signal("signal-a", event_key="event-a")
    second = _signal("signal-b", event_key="event-b")
    try:
        _record_signal_policy(ledger, first)
        _record_signal_policy(ledger, second)
        binding = ledger.read_policy_session_context(FRIDAY)
        assert binding is not None

        def decision(signal_ids: tuple[str, ...]) -> dict[str, object]:
            return ledger.record_policy_candidate_decision(
                FRIDAY,
                epoch_id="epoch-1",
                policy_version="portfolio_policy_v1",
                ticker="AAPL",
                direction="long",
                event_key=first.event_key,
                signal_ids=signal_ids,
                requested_weight=0.20,
                approved_weight=0.20,
                decision="accepted",
                reason_codes=("accepted",),
                bound_context_digest=str(binding["context_digest"]),
                captured_at=CAPTURED_AT,
            )

        one = decision((first.signal_id,))
        both = decision((first.signal_id, second.signal_id))
        with pytest.raises(LedgerConflictError, match="contributors overlap"):
            ledger.record_policy_staging_audit_manifest(
                FRIDAY,
                epoch_id="epoch-1",
                policy_id="param-policy-1",
                policy_version="portfolio_policy_v1",
                bound_context_digest=str(binding["context_digest"]),
                ingress_signal_ids=(first.signal_id, second.signal_id),
                candidate_decision_ids=tuple(
                    sorted((str(one["decision_id"]), str(both["decision_id"])))
                ),
                committee_not_selected_ids=(),
                recorded_at=CAPTURED_AT,
            )
    finally:
        ledger.close()


@pytest.mark.parametrize("tamper", ("empty_policy_id", "naive_recorded_at"))
def test_staging_manifest_read_rejects_coherent_invalid_rewrite_on_reopen(
    tmp_path, tamper: str
) -> None:
    path = tmp_path / "portfolio.db"
    ledger = _ledger(tmp_path)
    try:
        binding = _bind_policy_context(ledger)

        def record_empty_manifest() -> None:
            ledger.record_policy_staging_audit_manifest(
                FRIDAY,
                epoch_id="epoch-1",
                policy_id="param-policy-1",
                policy_version="portfolio_policy_v1",
                bound_context_digest=str(binding["context_digest"]),
                ingress_signal_ids=(),
                candidate_decision_ids=(),
                committee_not_selected_ids=(),
                recorded_at=CAPTURED_AT,
            )

        ledger.complete_staging(
            FRIDAY,
            "epoch-1",
            "param-policy-1",
            CAPTURED_AT,
            record_empty_manifest,
            ledger.execution_governed_state_digest(FRIDAY),
        )
        if tamper == "empty_policy_id":
            row = ledger.connection.execute(
                "SELECT payload_json FROM policy_staging_audit_manifests"
            ).fetchone()
            payload = json.loads(str(row["payload_json"]))
            payload["policy_id"] = ""
            payload_json = json.dumps(
                payload, sort_keys=True, separators=(",", ":")
            )
            manifest_id = stable_id(
                "policy_staging_audit_manifest", payload_json
            )
            ledger.connection.execute(
                """UPDATE policy_staging_audit_manifests
                   SET manifest_id = ?, policy_id = '', payload_json = ?,
                       payload_digest = ?""",
                (
                    manifest_id,
                    payload_json,
                    stable_id(
                        "policy_staging_audit_payload", manifest_id, payload_json
                    ),
                ),
            )
            ledger.connection.execute(
                "UPDATE staging_runs SET policy_id = ''"
            )
        else:
            ledger.connection.execute(
                """UPDATE policy_staging_audit_manifests
                   SET recorded_at = '2026-07-31T20:00:00'"""
            )
    finally:
        ledger.close()

    reopened = PortfolioLedger.open_existing(path)
    try:
        with pytest.raises(LedgerConflictError, match="manifest"):
            reopened.read_policy_staging_audit_manifests()
    finally:
        reopened.close()


def test_signal_policy_rejects_arbitrary_digest_without_bound_context(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    signal = _signal()
    try:
        ledger.record_signal(signal)

        with pytest.raises(LedgerConflictError, match="missing bound policy context"):
            ledger.record_signal_policy_provenance(
                signal.signal_id,
                policy_version="portfolio_policy_v1",
                event_key=signal.event_key,
                source_event_keys=("source:event-1",),
                strategy_tags=(signal.strategy,),
                risk_tags=("event:event-1",),
                sector="Technology",
                journal_only=False,
                order_eligible=True,
                decision="accepted",
                reason_codes=("accepted",),
                bound_context_digest="arbitrary-digest",
                captured_at=CAPTURED_AT,
            )
    finally:
        ledger.close()


@pytest.mark.parametrize("mismatch", ["epoch", "version", "digest"])
def test_signal_policy_rejects_mismatched_bound_context(tmp_path, mismatch: str) -> None:
    ledger = _ledger(tmp_path)
    signal = _signal()
    try:
        ledger.record_signal(signal)
        binding = _bind_policy_context(
            ledger,
            epoch_id="epoch-2" if mismatch == "epoch" else signal.epoch_id,
        )
        policy_version = (
            "portfolio_policy_v2"
            if mismatch == "version"
            else "portfolio_policy_v1"
        )
        bound_digest = (
            "wrong-digest"
            if mismatch == "digest"
            else str(binding["context_digest"])
        )

        with pytest.raises(LedgerConflictError, match="signal policy binding mismatch"):
            ledger.record_signal_policy_provenance(
                signal.signal_id,
                policy_version=policy_version,
                event_key=signal.event_key,
                source_event_keys=("source:event-1",),
                strategy_tags=(signal.strategy,),
                risk_tags=("event:event-1",),
                sector="Technology",
                journal_only=False,
                order_eligible=True,
                decision="accepted",
                reason_codes=("accepted",),
                bound_context_digest=bound_digest,
                captured_at=CAPTURED_AT,
            )
    finally:
        ledger.close()


def test_removed_bound_context_breaks_companion_read_and_projection_on_reopen(
    tmp_path,
) -> None:
    path = tmp_path / "portfolio.db"
    ledger = _ledger(tmp_path)
    try:
        _stage_policy_entry(ledger)
        ledger.connection.execute(
            "DELETE FROM policy_session_contexts WHERE cohort_id = ? AND session = ?",
            (COHORT, FRIDAY.isoformat()),
        )
    finally:
        ledger.close()

    reopened = PortfolioLedger.open_existing(path)
    try:
        with pytest.raises(LedgerConflictError, match="missing bound policy context"):
            reopened.read_signal_policy_provenance("signal-1")
        with pytest.raises(LedgerConflictError, match="missing bound policy context"):
            reopened.policy_pending_entry_projection()
    finally:
        reopened.close()


def test_tampered_bound_context_breaks_existing_companion_projection(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    try:
        _stage_policy_entry(ledger)
        ledger.connection.execute(
            "UPDATE policy_session_contexts SET context_digest = 'tampered' "
            "WHERE cohort_id = ? AND session = ?",
            (COHORT, FRIDAY.isoformat()),
        )

        with pytest.raises(LedgerConflictError, match="tampered policy session context"):
            ledger.policy_pending_entry_projection()
    finally:
        ledger.close()


def test_policy_session_apis_reject_datetime_before_write_or_read(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    wrong_type = datetime(2026, 7, 31, 20, tzinfo=UTC)
    try:
        with pytest.raises(TypeError, match="session must be an exact date"):
            ledger.bind_policy_session_context(
                wrong_type,  # type: ignore[arg-type]
                epoch_id="epoch-1",
                policy_version="portfolio_policy_v1",
                policy_config={"max_positions": 5},
                context={"cash": "5000"},
                bound_at=CAPTURED_AT,
            )
        assert (
            ledger.connection.execute(
                "SELECT COUNT(*) FROM policy_session_contexts"
            ).fetchone()[0]
            == 0
        )

        with pytest.raises(TypeError, match="session must be an exact date"):
            ledger.read_policy_session_context(wrong_type)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="session must be an exact date"):
            ledger.policy_open_lot_projection(wrong_type)  # type: ignore[arg-type]
    finally:
        ledger.close()


def test_pending_projection_includes_future_entries_and_supports_self_exclusion(
    tmp_path,
) -> None:
    ledger = _ledger(tmp_path)
    try:
        first_signal = _signal()
        first_intent = _intent()
        _stage_policy_entry(ledger, signal=first_signal, intent=first_intent)

        future_signal = _signal(
            "signal-2",
            ticker="MSFT",
            event_key="event-2",
            strategy="earnings_call",
            reference_close=Decimal("50"),
        )
        future_intent = _intent(
            "intent-2",
            signal_ids=(future_signal.signal_id,),
            requested_qty=4,
            eligible_session=TUESDAY,
        )
        _stage_policy_entry(ledger, signal=future_signal, intent=future_intent)

        projection = ledger.policy_pending_entry_projection()
        assert [row["intent_id"] for row in projection] == ["intent-1", "intent-2"]
        assert [row["marked_value"] for row in projection] == [
            Decimal("1000"),
            Decimal("200"),
        ]
        assert projection[1]["eligible_session"] == TUESDAY
        assert [row["intent_id"] for row in ledger.policy_pending_entry_projection(
            exclude_intent_id="intent-1"
        )] == ["intent-2"]
    finally:
        ledger.close()


def test_pending_projection_rejects_missing_or_ambiguous_provenance(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    try:
        signal = _signal()
        _record_signal_policy(ledger, signal)
        intent = _intent()
        ledger.stage_intent(intent)
        with pytest.raises(LedgerConflictError, match="missing intent policy provenance"):
            ledger.policy_pending_entry_projection()

        second = _signal(
            "signal-2", event_key="event-2", reference_close=Decimal("101")
        )
        _record_signal_policy(ledger, second)
        ambiguous = _intent(
            "intent-2", signal_ids=(signal.signal_id, second.signal_id)
        )
        ledger.stage_intent(ambiguous)
        signal_policy = ledger.read_signal_policy_provenance(signal.signal_id)
        assert signal_policy is not None
        ledger.record_intent_policy_provenance(
            ambiguous.intent_id,
            signal_ids=ambiguous.signal_ids,
            policy_version="portfolio_policy_v1",
            event_key="event-1",
            source_event_keys=("source:event-1", "source:event-2"),
            strategy_tags=("litigation",),
            risk_tags=("event:event-1", "event:event-2"),
            sector="Technology",
            journal_only=False,
            order_eligible=True,
            decision="accepted",
            reason_codes=("accepted",),
            bound_context_digest=str(signal_policy["bound_context_digest"]),
            captured_at=CAPTURED_AT,
        )
        with pytest.raises(LedgerConflictError, match="unambiguous reference_close"):
            ledger.policy_pending_entry_projection(exclude_intent_id="intent-1")
    finally:
        ledger.close()


@pytest.mark.parametrize(
    ("journal_only", "order_eligible"),
    [(True, False), (False, False)],
)
def test_entry_intent_cannot_override_ineligible_signal_policy(
    tmp_path, journal_only: bool, order_eligible: bool
) -> None:
    ledger = _ledger(tmp_path)
    signal = _signal()
    intent = _intent()
    try:
        ledger.record_signal(signal)
        binding = _bind_policy_context(
            ledger, session=signal.reference_session, epoch_id=signal.epoch_id
        )
        ledger.record_signal_policy_provenance(
            signal.signal_id,
            policy_version="portfolio_policy_v1",
            event_key=signal.event_key,
            source_event_keys=("source:event-1",),
            strategy_tags=(signal.strategy,),
            risk_tags=("event:event-1",),
            sector="Technology",
            journal_only=journal_only,
            order_eligible=order_eligible,
            decision="rejected",
            reason_codes=("position_cap",),
            bound_context_digest=str(binding["context_digest"]),
            captured_at=CAPTURED_AT,
        )
        ledger.stage_intent(intent)

        with pytest.raises(
            LedgerConflictError,
            match="ineligible signal policy provenance signal-1 for intent-1",
        ):
            _record_intent_policy(ledger, intent, signal)
    finally:
        ledger.close()


def test_open_lot_projection_uses_fill_only_same_session_then_requires_raw_mark(
    tmp_path,
) -> None:
    ledger = _ledger(tmp_path)
    try:
        _, intent = _stage_policy_entry(ledger)
        ledger.apply_fill(intent, _fill())

        same_session = ledger.policy_open_lot_projection(MONDAY)
        assert same_session[0]["marked_value"] == Decimal("1010")
        assert same_session[0]["mark_source"] == "opening_reference"

        with pytest.raises(MissingMarkError, match="AAPL"):
            ledger.policy_open_lot_projection(TUESDAY)

        ledger.record_marks(
            TUESDAY,
            {"AAPL": _bar(TUESDAY, Decimal("105"))},
            datetime(2026, 8, 4, 21, tzinfo=UTC),
        )
        marked = ledger.policy_open_lot_projection(TUESDAY)
        assert marked[0]["marked_value"] == Decimal("1050")
        assert marked[0]["mark_source"] == "fixture"
    finally:
        ledger.close()


def test_consumed_event_keys_require_filled_entry_and_verified_provenance(tmp_path) -> None:
    path = tmp_path / "portfolio.db"
    ledger = _ledger(tmp_path)
    try:
        _, intent = _stage_policy_entry(ledger)
        assert ledger.consumed_event_keys() == frozenset()
        ledger.apply_fill(intent, _fill())
        assert ledger.consumed_event_keys() == frozenset({"event-1"})
    finally:
        ledger.close()

    reopened = PortfolioLedger.open_existing(path)
    try:
        assert reopened.consumed_event_keys() == frozenset({"event-1"})
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reopened.bind_policy_session_context(
                TUESDAY,
                epoch_id="epoch-1",
                policy_version="portfolio_policy_v1",
                policy_config={"max_positions": 5},
                context={"cash": "3988"},
                bound_at=datetime(2026, 8, 4, 12, tzinfo=UTC),
            )
    finally:
        reopened.close()


def test_consumed_event_keys_include_every_filled_contributor_only(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    try:
        first = _signal("signal-a", event_key="event-a")
        second = _signal("signal-b", event_key="event-b")
        _record_signal_policy(ledger, first)
        _record_signal_policy(ledger, second)
        filled = _intent(
            "intent-filled", signal_ids=(first.signal_id, second.signal_id)
        )
        ledger.stage_intent(filled)
        first_policy = ledger.read_signal_policy_provenance(first.signal_id)
        assert first_policy is not None
        ledger.record_intent_policy_provenance(
            filled.intent_id,
            signal_ids=filled.signal_ids,
            policy_version="portfolio_policy_v1",
            event_key="event-a",
            source_event_keys=("source:event-a", "source:event-b"),
            strategy_tags=("litigation",),
            risk_tags=("event:event-a", "event:event-b"),
            sector="Technology",
            journal_only=False,
            order_eligible=True,
            decision="accepted",
            reason_codes=("accepted",),
            bound_context_digest=str(first_policy["bound_context_digest"]),
            captured_at=CAPTURED_AT,
        )

        pending_signal = _signal("signal-pending", event_key="event-pending")
        pending = _intent("intent-pending", signal_ids=(pending_signal.signal_id,))
        _stage_policy_entry(ledger, signal=pending_signal, intent=pending)
        rejected_signal = _signal("signal-rejected", event_key="event-rejected")
        rejected = _intent(
            "intent-rejected", signal_ids=(rejected_signal.signal_id,)
        )
        _stage_policy_entry(ledger, signal=rejected_signal, intent=rejected)
        ledger.reject_intent(
            rejected.intent_id,
            datetime(2026, 8, 3, 12, tzinfo=UTC),
            "test rejection",
        )

        assert ledger.consumed_event_keys() == frozenset()
        ledger.apply_fill(filled, _fill(filled.intent_id))

        assert ledger.consumed_event_keys() == frozenset({"event-a", "event-b"})
    finally:
        ledger.close()


def test_policy_projections_are_hard_bounded_without_silent_truncation(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    try:
        for index in range(3):
            signal = _signal(
                f"signal-{index}",
                event_key=f"event-{index}",
                reference_close=Decimal("10"),
            )
            intent = _intent(
                f"intent-{index}",
                signal_ids=(signal.signal_id,),
                requested_qty=1,
                eligible_session=MONDAY,
            )
            _stage_policy_entry(ledger, signal=signal, intent=intent)

        with pytest.raises(LedgerConflictError, match="projection limit"):
            ledger.policy_pending_entry_projection(limit=2)
        with pytest.raises(ValueError, match="between 1 and 256"):
            ledger.policy_pending_entry_projection(limit=257)
    finally:
        ledger.close()
