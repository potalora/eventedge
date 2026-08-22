from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
import json
import sqlite3

import pytest

import tradingagents.strategies.metrics.store as metrics_store_module
from tradingagents.strategies.metrics.models import (
    CandidateBarRecoveryRecord,
    CandidateSignalIdentityBinding,
    GOVERNED_BAR_RECOVERY_CONTRACT,
    GovernedBarRecoveryRecord,
    METRIC_SCHEMA_VERSION,
)
from tradingagents.strategies.metrics.store import MetricStore
from tradingagents.strategies.orchestration.candidate_inputs import CandidateInputIssue


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
        contract_version=GOVERNED_BAR_RECOVERY_CONTRACT,
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


def _scheduled_governed_recovery_record(
    session: date, starts: tuple[str, ...]
) -> GovernedBarRecoveryRecord:
    rows = tuple(
        {**ESS_60M_ROWS[index % len(ESS_60M_ROWS)], "start": start}
        for index, start in enumerate(starts)
    )
    opening = rows[0]["open"]
    high = max(row["high"] for row in rows)
    low = min(row["low"] for row in rows)
    close = rows[-1]["close"]
    return GovernedBarRecoveryRecord.create(
        contract_version=GOVERNED_BAR_RECOVERY_CONTRACT,
        epoch_id="epoch-1",
        session=session,
        ticker="ESS",
        original_daily={
            "open": opening,
            "high": max(opening, close) - 0.01,
            "low": low,
            "close": close,
            "source": "yfinance",
            "fetched_at": "2026-08-10T22:01:31Z",
        },
        original_validation_error=f"incoherent ESS/{session.isoformat()}",
        expected_starts=starts,
        observed_starts=starts,
        intraday_rows=rows,
        reconstructed_bar={
            "open": opening,
            "high": high,
            "low": low,
            "close": close,
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


_ISSUE_DIGEST = "sha256:" + "a" * 64
_ISSUE_RETURNED_DIGEST = "sha256:" + "b" * 64


def _candidate_input_issue(
    issue_id: str = "candidate-input-issue-alx-reference-bar",
    *,
    epoch_id: str = "epoch-1",
    session: date = date(2026, 8, 3),
    dependency_kind: str = "reference_bar",
    ticker: str = "ALX",
    reason_code: str = "invalid_data",
) -> CandidateInputIssue:
    return CandidateInputIssue.create(
        issue_id=issue_id,
        epoch_id=epoch_id,
        session=session,
        dependency_kind=dependency_kind,
        reason_code=reason_code,
        ticker=ticker,
        source="yfinance",
        fetched_at=datetime(2026, 8, 3, 20, 1, tzinfo=UTC),
        requested_history_digest=_ISSUE_DIGEST,
        returned_history_digest=_ISSUE_RETURNED_DIGEST,
        expected_sessions=(date(2026, 7, 31), session),
        observed_sessions=(date(2026, 7, 31),),
        retryable=True,
        affected_signal_identities=(
            {"event_key": f"event-{ticker.lower()}", "strategy": "litigation"},
        ),
        affected_cohorts=("horizon_30d_size_5k",),
    )


def test_candidate_input_issue_table_has_primary_key_and_unique_scope(tmp_path) -> None:
    store = MetricStore(tmp_path / "metrics.sqlite3")

    with sqlite3.connect(store.path) as connection:
        columns = connection.execute(
            "PRAGMA table_info(candidate_input_issues)"
        ).fetchall()
        indexes = connection.execute(
            "PRAGMA index_list(candidate_input_issues)"
        ).fetchall()
        unique_index_columns = [
                [
                    row[2]
                    for row in connection.execute(
                        f"PRAGMA index_info({index[1]})"
                    ).fetchall()
                ]
            for index in indexes
            if index[2]
        ]

    assert {column[1] for column in columns if column[5]} == {"issue_id"}
    assert any(
        columns == ["epoch_id", "session", "dependency_kind", "ticker"]
        for columns in unique_index_columns
    )


def test_candidate_input_issues_are_immutable_by_id_and_scope(tmp_path) -> None:
    store = MetricStore(tmp_path / "metrics.sqlite3")
    issue = _candidate_input_issue()
    store.save_candidate_input_issue(issue)
    store.save_candidate_input_issue(issue)

    assert store.load_candidate_input_issue(
        epoch_id="epoch-1",
        session=date(2026, 8, 3),
        dependency_kind="reference_bar",
        ticker="alx",
    ) == issue
    assert store.load_candidate_input_issue_by_id(issue.issue_id) == issue

    with pytest.raises(ValueError, match="scope has unequal replay evidence"):
        store.save_candidate_input_issue(
            _candidate_input_issue(
                "candidate-input-issue-alx-reference-bar-changed",
                reason_code="missing_data",
            )
        )
    with pytest.raises(ValueError, match="issue_id.*unequal replay evidence"):
        store.save_candidate_input_issue(
            _candidate_input_issue(issue.issue_id, ticker="MSFT")
        )


def test_candidate_input_issue_rejects_same_id_with_different_timestamp_encoding(
    tmp_path,
) -> None:
    store = MetricStore(tmp_path / "metrics.sqlite3")
    issue = _candidate_input_issue()
    store.save_candidate_input_issue(issue)

    with pytest.raises(ValueError, match="issue_id.*unequal replay evidence"):
        store.save_candidate_input_issue(
            replace(
                issue,
                fetched_at=datetime(
                    2026, 8, 3, 21, 1, tzinfo=timezone(timedelta(hours=1))
                ),
            )
        )


def test_candidate_input_issue_read_revalidates_stored_payload(tmp_path) -> None:
    store = MetricStore(tmp_path / "metrics.sqlite3")
    issue = _candidate_input_issue()
    store.save_candidate_input_issue(issue)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE candidate_input_issues SET payload_json = ? WHERE issue_id = ?",
            ('{"issue_id":"tampered"}', issue.issue_id),
        )

    with pytest.raises(ValueError, match="payload"):
        store.load_candidate_input_issue_by_id(issue.issue_id)


@pytest.mark.parametrize("out_of_range", ["0001-01-01", "9999-12-31"])
def test_candidate_input_issue_read_rejects_out_of_range_session_dates(
    tmp_path, out_of_range: str
) -> None:
    store = MetricStore(tmp_path / "metrics.sqlite3")
    issue = _candidate_input_issue()
    store.save_candidate_input_issue(issue)
    payload = json.loads(issue.canonical_payload())
    payload["observed_sessions"] = [out_of_range]
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE candidate_input_issues SET payload_json = ? WHERE issue_id = ?",
            (
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                issue.issue_id,
            ),
        )

    with pytest.raises(ValueError, match="payload is invalid"):
        store.load_candidate_input_issue_by_id(issue.issue_id)


def test_candidate_input_issue_read_rejects_oversized_payload_before_json_parse(
    tmp_path, monkeypatch
) -> None:
    store = MetricStore(tmp_path / "metrics.sqlite3")
    issue = _candidate_input_issue()
    store.save_candidate_input_issue(issue)
    monkeypatch.setattr(
        metrics_store_module, "_MAX_CANDIDATE_INPUT_ISSUE_PAYLOAD_BYTES", 1_000
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE candidate_input_issues SET payload_json = ? WHERE issue_id = ?",
            (
                " " * (metrics_store_module._MAX_CANDIDATE_INPUT_ISSUE_PAYLOAD_BYTES + 1),
                issue.issue_id,
            ),
        )

    with pytest.raises(ValueError, match="payload exceeds byte bound"):
        store.load_candidate_input_issue_by_id(issue.issue_id)


def test_candidate_input_issue_read_rejects_deeply_nested_payload(tmp_path) -> None:
    store = MetricStore(tmp_path / "metrics.sqlite3")
    issue = _candidate_input_issue()
    store.save_candidate_input_issue(issue)
    deeply_nested = '{"issue_id":' + "[" * 2_000 + "]" * 2_000 + "}"
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE candidate_input_issues SET payload_json = ? WHERE issue_id = ?",
            (deeply_nested, issue.issue_id),
        )

    with pytest.raises(ValueError, match="payload is invalid"):
        store.load_candidate_input_issue_by_id(issue.issue_id)


def test_candidate_input_issue_save_rejects_oversized_payload_before_insert(
    tmp_path, monkeypatch
) -> None:
    store = MetricStore(tmp_path / "metrics.sqlite3")
    issue = _candidate_input_issue()
    monkeypatch.setattr(
        metrics_store_module, "_MAX_CANDIDATE_INPUT_ISSUE_PAYLOAD_BYTES", 1
    )

    with pytest.raises(ValueError, match="payload exceeds byte bound"):
        store.save_candidate_input_issue(issue)

    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM candidate_input_issues"
        ).fetchone()[0] == 0


def test_candidate_input_issue_reads_are_bounded_and_deterministic(tmp_path) -> None:
    store = MetricStore(tmp_path / "metrics.sqlite3")
    later = _candidate_input_issue(
        "issue-z", session=date(2026, 8, 4), ticker="ZKH"
    )
    first = _candidate_input_issue("issue-a", ticker="AAPL")
    other_epoch = _candidate_input_issue("issue-other", epoch_id="epoch-2", ticker="NCL")
    for issue in (later, first, other_epoch):
        store.save_candidate_input_issue(issue)

    assert store.read_candidate_input_issues("epoch-1", limit=1) == (first,)
    assert store.read_candidate_input_issues("epoch-1") == (first, later)
    assert store.read_candidate_input_issues(
        "epoch-1", session=date(2026, 8, 3)
    ) == (first,)


def test_legacy_read_only_store_has_no_candidate_input_issues(tmp_path) -> None:
    path = tmp_path / "legacy-metrics.sqlite3"
    with sqlite3.connect(path) as connection:
        _legacy_schema(connection)

    store = MetricStore.open_existing(path)

    assert store.read_candidate_input_issues("legacy-epoch") == ()
    assert (
        store.load_candidate_input_issue(
            epoch_id="legacy-epoch",
            session=date(2026, 8, 3),
            dependency_kind="reference_bar",
            ticker="ALX",
        )
        is None
    )
    assert store.load_candidate_input_issue_by_id("missing") is None


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
                        "affected_cohort_ids": ("horizon_30d_size_10k",),
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


def _recreate_governed_record(
    record: GovernedBarRecoveryRecord, **changes: object
) -> GovernedBarRecoveryRecord:
    fields = record.evidence_fields()
    fields.update(changes)
    return GovernedBarRecoveryRecord.create(**fields)


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        (
            {
                "original_daily": {
                    **governed_recovery_record("epoch-1").original_daily,
                    "high": 300.0,
                }
            },
            "original daily evidence is coherent",
        ),
        ({"original_validation_error": "incoherent ESS/2026/08/10"}, "reason"),
        (
            {"expected_starts": governed_recovery_record("epoch-1").expected_starts[:-1]},
            "schedule",
        ),
        (
            {
                "reconstructed_bar": {
                    **governed_recovery_record("epoch-1").reconstructed_bar,
                    "high": 280.0,
                }
            },
            "reconstructed",
        ),
        (
            {
                "reconstructed_bar": {
                    **governed_recovery_record("epoch-1").reconstructed_bar,
                    "source": "other",
                }
            },
            "source",
        ),
        ({"final_validation_error": "still invalid"}, "final validation"),
    ],
)
def test_governed_storage_accepts_only_complete_accepted_reconstructions(
    tmp_path, changes: dict[str, object], error: str
) -> None:
    store = MetricStore(tmp_path / "metrics.sqlite3")

    with pytest.raises(ValueError, match=error):
        store.save_governed_bar_recovery(
            _recreate_governed_record(governed_recovery_record("epoch-1"), **changes)
        )


@pytest.mark.parametrize("field_value", [True, 0, -1, float("nan"), float("inf")])
@pytest.mark.parametrize("evidence_kind", ["original", "reconstructed", "intraday"])
def test_governed_storage_rejects_invalid_ohlc_values_with_value_error(
    tmp_path, evidence_kind: str, field_value: object
) -> None:
    record = governed_recovery_record("epoch-1")
    fields = record.evidence_fields()
    if evidence_kind == "original":
        fields["original_daily"] = {**record.original_daily, "open": field_value}
    elif evidence_kind == "reconstructed":
        fields["reconstructed_bar"] = {
            **record.reconstructed_bar,
            "open": field_value,
        }
    else:
        fields["intraday_rows"] = (
            {**record.intraday_rows[0], "open": field_value},
            *record.intraday_rows[1:],
        )

    with pytest.raises(ValueError, match="governed bar recovery"):
        MetricStore(tmp_path / "metrics.sqlite3").save_governed_bar_recovery(
            GovernedBarRecoveryRecord.create(**fields)
        )


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        (
            {
                "original_daily": {
                    **governed_recovery_record("epoch-1").original_daily,
                    "source": "other",
                }
            },
            "original source",
        ),
        (
            {
                "original_daily": {
                    **governed_recovery_record("epoch-1").original_daily,
                    "open": 287.0,
                }
            },
            "open does not match",
        ),
        (
            {
                "original_daily": {
                    **governed_recovery_record("epoch-1").original_daily,
                    "close": 286.0,
                }
            },
            "close does not match",
        ),
        (
            {
                "original_daily": {
                    **governed_recovery_record("epoch-1").original_daily,
                    "high": 280.0,
                    "low": 284.0,
                }
            },
            "both envelope bounds",
        ),
        (
            {
                "original_daily": {
                    **governed_recovery_record("epoch-1").original_daily,
                    "low": 281.0,
                }
            },
            "unbroken low",
        ),
        (
            {
                "original_daily": {
                    **governed_recovery_record("epoch-1").original_daily,
                    "high": 287.0,
                    "low": 284.0,
                }
            },
            "unbroken high",
        ),
        (
            {
                "intraday_rows": (
                    {
                        **governed_recovery_record("epoch-1").intraday_rows[0],
                        "open": 285.0,
                    },
                    *governed_recovery_record("epoch-1").intraday_rows[1:],
                )
            },
            "aggregation",
        ),
    ],
)
def test_governed_storage_binds_reconstruction_to_original_and_intraday_evidence(
    tmp_path, changes: dict[str, object], error: str
) -> None:
    with pytest.raises(ValueError, match=error):
        MetricStore(tmp_path / "metrics.sqlite3").save_governed_bar_recovery(
            _recreate_governed_record(governed_recovery_record("epoch-1"), **changes)
        )


@pytest.mark.parametrize(
    ("row_index", "field", "value"),
    [
        (0, "open", 285.0),
        (2, "high", 290.0),
        (4, "low", 280.0),
        (6, "close", 283.0),
    ],
)
def test_governed_storage_rejects_each_intraday_aggregation_mismatch(
    tmp_path, row_index: int, field: str, value: float
) -> None:
    record = governed_recovery_record("epoch-1")
    rows = [dict(row) for row in record.intraday_rows]
    rows[row_index][field] = value

    with pytest.raises(ValueError, match="aggregation"):
        MetricStore(tmp_path / "metrics.sqlite3").save_governed_bar_recovery(
            _recreate_governed_record(record, intraday_rows=tuple(rows))
        )


@pytest.mark.parametrize(
    ("column", "value", "scope"),
    [
        ("recovery_id", "tampered-id", ("epoch-1", date(2026, 8, 10), "ESS")),
        ("contract_version", "other-contract", ("epoch-1", date(2026, 8, 10), "ESS")),
        ("evidence_digest", "sha256:tampered", ("epoch-1", date(2026, 8, 10), "ESS")),
        ("epoch_id", "other-epoch", ("other-epoch", date(2026, 8, 10), "ESS")),
        ("session", "2026-08-11", ("epoch-1", date(2026, 8, 11), "ESS")),
        ("ticker", "OTHER", ("epoch-1", date(2026, 8, 10), "OTHER")),
    ],
)
def test_governed_loads_fail_closed_on_tampered_persisted_metadata(
    tmp_path, column: str, value: str, scope: tuple[str, date, str]
) -> None:
    store = MetricStore(tmp_path / "metrics.sqlite3")
    record = governed_recovery_record("epoch-1")
    store.save_governed_bar_recovery(record)
    with sqlite3.connect(store.path) as connection:
        payload = connection.execute(
            "SELECT payload_json FROM governed_bar_recoveries"
        ).fetchone()[0]
        connection.execute(
            f"UPDATE governed_bar_recoveries SET {column} = ?", (value,)
        )
        assert connection.execute(
            "SELECT payload_json FROM governed_bar_recoveries"
        ).fetchone()[0] == payload

    recovery_id = value if column == "recovery_id" else record.recovery_id
    with pytest.raises(ValueError, match="metadata"):
        store.load_governed_bar_recovery_by_id(recovery_id)
    with pytest.raises(ValueError, match="metadata"):
        store.load_governed_bar_recovery(
            epoch_id=scope[0], session=scope[1], ticker=scope[2]
        )


@pytest.mark.parametrize(
    "payload",
    [
        '{"x":' * 1_100 + "0" + "}" * 1_100,
        "{not json",
        "x" * 100_001,
    ],
)
def test_governed_load_rejects_malformed_or_unbounded_persisted_payload(
    tmp_path, payload: str
) -> None:
    store = MetricStore(tmp_path / "metrics.sqlite3")
    record = governed_recovery_record("epoch-1")
    store.save_governed_bar_recovery(record)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE governed_bar_recoveries SET payload_json = ?", (payload,)
        )

    with pytest.raises(ValueError, match="payload"):
        store.load_governed_bar_recovery_by_id(record.recovery_id)


def test_governed_record_nested_evidence_is_immutable_after_create_and_load(
    tmp_path,
) -> None:
    store = MetricStore(tmp_path / "metrics.sqlite3")
    record = governed_recovery_record("epoch-1")
    store.save_governed_bar_recovery(record)

    for evidence in (
        record,
        store.load_governed_bar_recovery_by_id(record.recovery_id),
        store.load_governed_bar_recovery(
            epoch_id="epoch-1", session=date(2026, 8, 10), ticker="ESS"
        ),
    ):
        assert evidence is not None
        with pytest.raises(TypeError):
            evidence.original_daily["open"] = 1.0
        with pytest.raises(TypeError):
            evidence.intraday_rows[0]["close"] = 1.0
        with pytest.raises(TypeError):
            evidence.reconstructed_bar["close"] = 1.0
        evidence.validate_integrity()


def test_governed_recovery_rejects_unsupported_contract_on_save_and_load(tmp_path) -> None:
    store = MetricStore(tmp_path / "metrics.sqlite3")
    record = governed_recovery_record("epoch-1")
    unsupported = _recreate_governed_record(record, contract_version="other-v1")

    with pytest.raises(ValueError, match="contract"):
        store.save_governed_bar_recovery(unsupported)

    store.save_governed_bar_recovery(record)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            UPDATE governed_bar_recoveries
            SET recovery_id = ?, contract_version = ?, evidence_digest = ?,
                payload_json = ?
            """,
            (
                unsupported.recovery_id,
                unsupported.contract_version,
                unsupported.evidence_digest,
                unsupported.canonical_payload(),
            ),
        )

    with pytest.raises(ValueError, match="contract"):
        store.load_governed_bar_recovery_by_id(unsupported.recovery_id)


@pytest.mark.parametrize("cohort_id", [1, "", " ", "x" * 4_097])
def test_governed_recovery_rejects_invalid_affected_cohort_id(
    tmp_path, cohort_id: object
) -> None:
    with pytest.raises(ValueError, match="affected cohort"):
        MetricStore(tmp_path / "metrics.sqlite3").save_governed_bar_recovery(
            _recreate_governed_record(
                governed_recovery_record("epoch-1"), affected_cohort_ids=(cohort_id,)
            )
        )


def test_governed_recovery_requires_exact_xnys_hourly_schedule(tmp_path) -> None:
    store = MetricStore(tmp_path / "metrics.sqlite3")
    record = governed_recovery_record("epoch-1")

    with pytest.raises(ValueError, match="interval schedule"):
        store.save_governed_bar_recovery(
            _recreate_governed_record(
                record,
                session=date(2026, 8, 11),
                original_validation_error="incoherent ESS/2026-08-11",
            )
        )
    with pytest.raises(ValueError, match="interval schedule"):
        store.save_governed_bar_recovery(
            _scheduled_governed_recovery_record(
                date(2026, 8, 10),
                tuple(start.replace(":30:00", ":31:00") for start in record.expected_starts),
            )
        )
    with pytest.raises(ValueError, match="interval schedule"):
        store.save_governed_bar_recovery(
            _scheduled_governed_recovery_record(
                date(2026, 8, 10), record.expected_starts[:-1]
            )
        )
    with pytest.raises(ValueError, match="interval schedule"):
        store.save_governed_bar_recovery(
            _scheduled_governed_recovery_record(
                date(2026, 8, 10),
                record.expected_starts + ("2026-08-10T16:30:00-04:00",),
            )
        )
    utc_starts = tuple(
        (datetime(2026, 8, 10, 13, 30, tzinfo=UTC) + timedelta(hours=index)).isoformat()
        for index in range(7)
    )
    with pytest.raises(ValueError, match="interval schedule"):
        store.save_governed_bar_recovery(
            _scheduled_governed_recovery_record(date(2026, 8, 10), utc_starts)
        )


def test_governed_recovery_uses_exact_black_friday_hourly_schedule(tmp_path) -> None:
    store = MetricStore(tmp_path / "metrics.sqlite3")
    early_starts = tuple(
        f"2026-11-27T{hour:02d}:30:00-05:00" for hour in range(9, 13)
    )
    store.save_governed_bar_recovery(
        _scheduled_governed_recovery_record(date(2026, 11, 27), early_starts)
    )

    with pytest.raises(ValueError, match="interval schedule"):
        store.save_governed_bar_recovery(
            _scheduled_governed_recovery_record(
                date(2026, 11, 27),
                early_starts + ("2026-11-27T13:30:00-05:00",),
            )
        )


def test_governed_idempotent_save_revalidates_existing_metadata(tmp_path) -> None:
    store = MetricStore(tmp_path / "metrics.sqlite3")
    record = governed_recovery_record("epoch-1")
    store.save_governed_bar_recovery(record)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE governed_bar_recoveries SET contract_version = 'corrupt'"
        )

    with pytest.raises(ValueError, match="metadata"):
        store.save_governed_bar_recovery(record)


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
