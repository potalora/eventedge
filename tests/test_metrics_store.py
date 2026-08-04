from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
import sqlite3

import pytest

from tradingagents.strategies.metrics.models import (
    CandidateBarRecoveryRecord,
    CandidateSignalIdentityBinding,
)
from tradingagents.strategies.metrics.store import MetricStore


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
