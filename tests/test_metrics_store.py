from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
import sqlite3

import pytest

from tradingagents.strategies.metrics.models import (
    CandidateBarRecoveryRecord,
    CandidateSignalIdentityBinding,
    GovernedBarRecoveryRecord,
    METRIC_SCHEMA_VERSION,
)
from tradingagents.strategies.metrics.store import MetricStore


ESS_60M_ROWS = (
    {
        "start": "2026-08-10T09:30:00-04:00",
        "open": 286.2099914550781,
        "high": 286.2099914550781,
        "low": 284.3500061035156,
        "close": 284.79998779296875,
        "fetched_at": "2026-08-10T22:01:31Z",
    },
    {
        "start": "2026-08-10T10:30:00-04:00",
        "open": 284.79998779296875,
        "high": 285.82501220703125,
        "low": 284.4700012207031,
        "close": 285.5,
        "fetched_at": "2026-08-10T22:01:31Z",
    },
    {
        "start": "2026-08-10T11:30:00-04:00",
        "open": 285.5,
        "high": 285.6000061035156,
        "low": 283.8999938964844,
        "close": 284.1000061035156,
        "fetched_at": "2026-08-10T22:01:31Z",
    },
    {
        "start": "2026-08-10T12:30:00-04:00",
        "open": 284.1000061035156,
        "high": 284.29998779296875,
        "low": 282.75,
        "close": 283.0,
        "fetched_at": "2026-08-10T22:01:31Z",
    },
    {
        "start": "2026-08-10T13:30:00-04:00",
        "open": 283.0,
        "high": 283.5,
        "low": 282.1000061035156,
        "close": 282.45001220703125,
        "fetched_at": "2026-08-10T22:01:31Z",
    },
    {
        "start": "2026-08-10T14:30:00-04:00",
        "open": 282.45001220703125,
        "high": 283.45001220703125,
        "low": 281.5299987792969,
        "close": 282.8999938964844,
        "fetched_at": "2026-08-10T22:01:31Z",
    },
    {
        "start": "2026-08-10T15:30:00-04:00",
        "open": 282.8999938964844,
        "high": 283.5,
        "low": 282.70001220703125,
        "close": 283.2099914550781,
        "fetched_at": "2026-08-10T22:01:31Z",
    },
)


def governed_recovery_record(epoch_id: str) -> GovernedBarRecoveryRecord:
    return GovernedBarRecoveryRecord.create(
        contract_version="yfinance-60m-v1",
        epoch_id=epoch_id,
        session=date(2026, 8, 10),
        ticker="ESS",
        original_daily={
            "open": 286.2099914550781,
            "high": 285.82501220703125,
            "low": 281.5299987792969,
            "close": 283.2099914550781,
            "source": "yfinance",
            "fetched_at": "2026-08-10T22:01:31Z",
        },
        original_validation_error="incoherent ESS/2026-08-10",
        expected_starts=(
            "2026-08-10T09:30:00-04:00",
            "2026-08-10T10:30:00-04:00",
            "2026-08-10T11:30:00-04:00",
            "2026-08-10T12:30:00-04:00",
            "2026-08-10T13:30:00-04:00",
            "2026-08-10T14:30:00-04:00",
            "2026-08-10T15:30:00-04:00",
        ),
        observed_starts=(
            "2026-08-10T09:30:00-04:00",
            "2026-08-10T10:30:00-04:00",
            "2026-08-10T11:30:00-04:00",
            "2026-08-10T12:30:00-04:00",
            "2026-08-10T13:30:00-04:00",
            "2026-08-10T14:30:00-04:00",
            "2026-08-10T15:30:00-04:00",
        ),
        intraday_rows=ESS_60M_ROWS,
        reconstructed_bar={
            "open": 286.2099914550781,
            "high": 286.2099914550781,
            "low": 281.5299987792969,
            "close": 283.2099914550781,
            "source": "yfinance-60m-reconstruction",
        },
        final_validation_error=None,
        affected_cohort_ids=("horizon_30d_size_5k",),
    )


def _legacy_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE metric_epochs (epoch_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL);
        CREATE TABLE outcomes (outcome_id TEXT PRIMARY KEY, epoch_id TEXT NOT NULL, payload_json TEXT NOT NULL);
        CREATE TABLE strategy_health (health_id TEXT PRIMARY KEY, epoch_id TEXT NOT NULL, session TEXT NOT NULL, payload_json TEXT NOT NULL);
        CREATE TABLE critical_gap_markers (marker_id TEXT PRIMARY KEY, status TEXT NOT NULL, gap_session TEXT NOT NULL, payload_json TEXT NOT NULL);
        """
    )


def test_governed_bar_recovery_round_trips_immutably(tmp_path) -> None:
    store = MetricStore(tmp_path / "metrics.sqlite3")
    record = governed_recovery_record("epoch-1")

    store.save_governed_bar_recovery(record)
    store.save_governed_bar_recovery(record)

    assert store.load_governed_bar_recovery(
        epoch_id="epoch-1", session=date(2026, 8, 10), ticker="ess"
    ) == record
    assert store.load_governed_bar_recovery_by_id(record.recovery_id) == record


def test_governed_bar_recovery_rejects_unequal_evidence_in_the_same_scope(
    tmp_path,
) -> None:
    store = MetricStore(tmp_path / "metrics.sqlite3")
    record = governed_recovery_record("epoch-1")
    store.save_governed_bar_recovery(record)

    with pytest.raises(ValueError, match="unequal payload"):
        store.save_governed_bar_recovery(
            GovernedBarRecoveryRecord.create(
                **{
                    **record.evidence_fields(),
                    "reconstructed_bar": {
                        **record.reconstructed_bar,
                        "close": 283.0,
                    },
                }
            )
        )


def test_governed_bar_recovery_rejects_mismatched_supplied_identifiers() -> None:
    record = governed_recovery_record("epoch-1")

    with pytest.raises(ValueError, match="evidence digest"):
        GovernedBarRecoveryRecord.create(
            **record.evidence_fields(), evidence_digest="sha256:not-the-digest"
        )
    with pytest.raises(ValueError, match="recovery id"):
        GovernedBarRecoveryRecord.create(
            **record.evidence_fields(), recovery_id="not-the-recovery-id"
        )


def test_open_existing_reads_governed_recovery_without_creating_tables(tmp_path) -> None:
    path = tmp_path / "existing-metrics.sqlite3"
    record = governed_recovery_record("epoch-1")
    with sqlite3.connect(path) as connection:
        _legacy_schema(connection)
        connection.execute(
            """
            CREATE TABLE governed_bar_recoveries (
                recovery_id TEXT PRIMARY KEY,
                contract_version TEXT NOT NULL,
                evidence_digest TEXT NOT NULL,
                epoch_id TEXT NOT NULL,
                session TEXT NOT NULL,
                ticker TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (epoch_id, session, ticker)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO governed_bar_recoveries
              (recovery_id, contract_version, evidence_digest, epoch_id, session,
               ticker, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.recovery_id,
                record.contract_version,
                record.evidence_digest,
                record.epoch_id,
                record.session.isoformat(),
                record.ticker,
                record.canonical_payload(),
                record.original_daily["fetched_at"],
            ),
        )

    store = MetricStore.open_existing(path)

    assert store.load_governed_bar_recovery_by_id(record.recovery_id) == record
    with sqlite3.connect(path) as connection:
        assert {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        } == {
            "metric_epochs",
            "outcomes",
            "strategy_health",
            "critical_gap_markers",
            "governed_bar_recoveries",
        }


def test_open_existing_missing_governed_table_returns_no_record_without_migration(
    tmp_path,
) -> None:
    path = tmp_path / "legacy-metrics.sqlite3"
    with sqlite3.connect(path) as connection:
        _legacy_schema(connection)

    store = MetricStore.open_existing(path)

    assert (
        store.load_governed_bar_recovery(
            epoch_id="legacy-epoch", session=date(2026, 8, 10), ticker="ESS"
        )
        is None
    )
    assert store.load_governed_bar_recovery_by_id("missing") is None
    with sqlite3.connect(path) as connection:
        assert "governed_bar_recoveries" not in {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }


def test_governed_recovery_storage_does_not_change_metrics_schema_version() -> None:
    assert METRIC_SCHEMA_VERSION == 2


def _attempt(
    attempt: int, *, ticker: str = "ALX", error: str | None
) -> dict[str, object]:
    return {
        "ticker": ticker,
        "session": date(2026, 8, 3),
        "attempt": attempt,
        "source": "yfinance",
        "fetched_at": datetime(2026, 8, 3, 20, attempt, tzinfo=timezone.utc),
        "open": Decimal("101.25"),
        "high": Decimal("102.50"),
        "low": Decimal("100.75"),
        "close": Decimal("103.00") if error else Decimal("101.75"),
        "validation_error": error,
    }


def _record(
    recovery_id: str = "candidate-recovery-epoch-1-2026-08-03-ALX",
    *,
    ticker: str = "ALX",
) -> CandidateBarRecoveryRecord:
    return CandidateBarRecoveryRecord(
        recovery_id=recovery_id,
        epoch_id="epoch-1",
        session=date(2026, 8, 3),
        ticker=ticker,
        outcome="quarantined",
        attempts=(
            _attempt(1, ticker=ticker, error="close exceeds high"),
            _attempt(2, ticker=ticker, error="close exceeds high"),
        ),
        signal_identities=(
            {"event_key": "event-alx-1", "strategy": "litigation"},
            {"event_key": "event-alx-2", "strategy": "earnings_call"},
        ),
    )


def test_candidate_bar_recoveries_round_trip_in_deterministic_order(tmp_path) -> None:
    store = MetricStore(tmp_path / "metrics.sqlite3")
    alx = _record()
    aapl = replace(_record("candidate-recovery-aapl", ticker="AAPL"), ticker="AAPL")

    store.save_candidate_bar_recovery(alx)
    store.save_candidate_bar_recovery(aapl)
    store.save_candidate_bar_recovery(alx)

    assert store.read_candidate_bar_recoveries("epoch-1") == (aapl, alx)
    assert store.read_candidate_bar_recoveries("epoch-1", session=date(2026, 8, 3)) == (
        aapl,
        alx,
    )
    assert store.read_candidate_bar_recoveries("epoch-1")[1].attempts[0][
        "session"
    ] == date(2026, 8, 3)
    assert store.read_candidate_bar_recoveries("epoch-1")[1].attempts[0][
        "fetched_at"
    ] == datetime(2026, 8, 3, 20, 1, tzinfo=timezone.utc)


def test_one_attempt_accepted_candidate_round_trips(tmp_path) -> None:
    store = MetricStore(tmp_path / "metrics.sqlite3")
    record = replace(
        _record("candidate-accepted-msft", ticker="MSFT"),
        outcome="accepted",
        attempts=(_attempt(1, ticker="MSFT", error=None),),
        signal_identities=(
            {"event_key": "event-msft", "strategy": "earnings_call"},
        ),
    )

    store.save_candidate_bar_recovery(record)

    assert store.read_candidate_bar_recoveries("epoch-1") == (record,)


def test_candidate_signal_identity_binding_round_trips_immutably(tmp_path) -> None:
    store = MetricStore(tmp_path / "metrics.sqlite3")
    binding = CandidateSignalIdentityBinding(
        binding_id="candidate-signal-binding-epoch-1-2026-08-03",
        epoch_id="epoch-1",
        session=date(2026, 8, 3),
        identities=(
            {
                "horizon": "30d",
                "ticker": "AAPL",
                "event_key": "event-aapl-30d",
                "strategy": "litigation",
            },
            {
                "horizon": "3m",
                "ticker": "AAPL",
                "event_key": "event-aapl-3m",
                "strategy": "litigation",
            },
        ),
    )

    store.save_candidate_signal_identity_binding(binding)
    store.save_candidate_signal_identity_binding(binding)

    assert (
        MetricStore(store.path).read_candidate_signal_identity_binding(
            "epoch-1", date(2026, 8, 3)
        )
        == binding
    )
    with pytest.raises(ValueError, match="unequal payload"):
        store.save_candidate_signal_identity_binding(
            replace(binding, identities=binding.identities[:1])
        )


def test_candidate_bar_recovery_rejects_unequal_duplicate_payload(tmp_path) -> None:
    store = MetricStore(tmp_path / "metrics.sqlite3")
    record = _record()
    store.save_candidate_bar_recovery(record)

    with pytest.raises(ValueError, match="unequal payload"):
        store.save_candidate_bar_recovery(replace(record, outcome="recovered"))


def test_candidate_bar_recovery_rejects_unbounded_evidence(tmp_path) -> None:
    store = MetricStore(tmp_path / "metrics.sqlite3")
    record = _record()

    with pytest.raises(ValueError, match="attempt evidence"):
        store.save_candidate_bar_recovery(
            replace(record, attempts=record.attempts + (_attempt(3, error="bad"),))
        )
    with pytest.raises(ValueError, match="signal identity"):
        store.save_candidate_bar_recovery(
            replace(
                record,
                signal_identities=tuple(
                    {"event_key": f"event-{index}", "strategy": "litigation"}
                    for index in range(65)
                ),
            )
        )
    with pytest.raises(ValueError, match="attempt evidence"):
        store.save_candidate_bar_recovery(
            replace(record, attempts=({**record.attempts[0], "source": "x" * 257},))
        )


def test_legacy_read_only_store_has_no_candidate_bar_recoveries(tmp_path) -> None:
    path = tmp_path / "legacy-metrics.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE metric_epochs (epoch_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL);
            CREATE TABLE outcomes (outcome_id TEXT PRIMARY KEY, epoch_id TEXT NOT NULL, payload_json TEXT NOT NULL);
            CREATE TABLE strategy_health (health_id TEXT PRIMARY KEY, epoch_id TEXT NOT NULL, session TEXT NOT NULL, payload_json TEXT NOT NULL);
            CREATE TABLE critical_gap_markers (marker_id TEXT PRIMARY KEY, status TEXT NOT NULL, gap_session TEXT NOT NULL, payload_json TEXT NOT NULL);
            """
        )

    store = MetricStore.open_existing(path)

    assert store.read_only
    assert store.read_candidate_bar_recoveries("legacy-epoch") == ()
    assert (
        store.read_candidate_signal_identity_binding(
            "legacy-epoch", date(2026, 8, 3)
        )
        is None
    )


@pytest.mark.parametrize("price", [Decimal("NaN"), Decimal("Infinity")])
def test_candidate_bar_recovery_rejects_non_finite_prices(tmp_path, price) -> None:
    store = MetricStore(tmp_path / "metrics.sqlite3")

    with pytest.raises(ValueError, match="attempt evidence price"):
        store.save_candidate_bar_recovery(
            replace(_record(), attempts=({**_attempt(1, error="bad"), "open": price},))
        )


def test_candidate_bar_recovery_rejects_oversized_finite_price(tmp_path) -> None:
    store = MetricStore(tmp_path / "metrics.sqlite3")

    with pytest.raises(ValueError, match="attempt evidence price"):
        store.save_candidate_bar_recovery(
            replace(
                _record(),
                attempts=(
                    {
                        **_attempt(1, error="bad"),
                        "open": Decimal("9" * 257),
                    },
                ),
            )
        )


class _OrdinalOne:
    def __eq__(self, other: object) -> bool:
        return other == 1


@pytest.mark.parametrize("attempt", [True, _OrdinalOne()])
def test_candidate_bar_recovery_rejects_non_integer_attempt_ordinal(
    tmp_path, attempt
) -> None:
    store = MetricStore(tmp_path / "metrics.sqlite3")

    with pytest.raises(ValueError, match="attempt evidence order"):
        store.save_candidate_bar_recovery(
            replace(
                _record(), attempts=({**_attempt(1, error="bad"), "attempt": attempt},)
            )
        )
