"""Independent SIP recovery must be immutable and safe to replay offline."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from tradingagents.strategies.execution.alpaca_daily_bar import (
    SOURCE as ALPACA_SOURCE,
)
from tradingagents.strategies.execution.alpaca_daily_bar import (
    AlpacaBarFailure,
    AlpacaDailyBarResult,
)
from tradingagents.strategies.execution.models import MarketBar
from tradingagents.strategies.execution.price_source import (
    GovernedBarRecoveryEvidence,
    GovernedDailyBarAttempt,
    GovernedDailyBarResolution,
)
from tradingagents.strategies.metrics.models import GovernedBarRecoveryRecord
from tradingagents.strategies.metrics.store import MetricStore
from tradingagents.strategies.orchestration.governed_market_data import (
    GovernedInputResolution,
    _bar_from_record,
    _binding,
    _summary,
    resolve_governed_bars,
)
from tradingagents.strategies.orchestration.preflight import (
    _validate_governed_resolution,
)

SESSION = date(2026, 9, 22)
EPOCH = "gen_015-2026-09-22-test"


def _sip_record() -> GovernedBarRecoveryRecord:
    return GovernedBarRecoveryRecord.create(
        contract_version="alpaca-sip-1d-raw-v1",
        epoch_id=EPOCH,
        session=SESSION,
        ticker="BRC",
        original_daily={
            "open": Decimal("84.51000213623047"),
            "high": Decimal("84.30500030517578"),
            "low": Decimal("83.06749725341797"),
            "close": Decimal("83.38999938964844"),
            "source": "yfinance",
            "fetched_at": "2026-09-22T22:01:24+00:00",
        },
        original_validation_error="incoherent BRC/2026-09-22",
        expected_starts=tuple(
            f"2026-09-22T{hour:02d}:30:00-04:00" for hour in range(9, 16)
        ),
        observed_starts=(),
        intraday_rows=(),
        yahoo_recovery_error="invalid BRC/2026-09-22",
        alternate_daily={
            "provider": "alpaca",
            "feed": "sip",
            "adjustment": "raw",
            "timeframe": "1Day",
            "request_start": "2026-09-22T00:00:00-04:00",
            "request_end": "2026-09-22T21:50:00+00:00",
            "response_symbol": "BRC",
            "response_timestamp": "2026-09-22T04:00:00+00:00",
            "open": Decimal("84.51"),
            "high": Decimal("84.51"),
            "low": Decimal("83.0675"),
            "close": Decimal("83.39"),
            "fetched_at": "2026-09-22T22:05:00+00:00",
            "row_count": 1,
            "pagination_complete": True,
        },
        reconstructed_bar={
            "open": Decimal("84.51"),
            "high": Decimal("84.51"),
            "low": Decimal("83.0675"),
            "close": Decimal("83.39"),
            "source": "alpaca-sip-1d-raw",
        },
        final_validation_error=None,
        affected_cohort_ids=("horizon_30d_size_5k",),
    )


def test_sip_recovery_round_trips_and_replays_without_provider(tmp_path):
    record = _sip_record()
    store = MetricStore(tmp_path / "metrics.sqlite3")
    store.save_governed_bar_recovery(record)

    loaded = store.load_governed_bar_recovery(
        epoch_id=EPOCH, session=SESSION, ticker="BRC"
    )
    assert loaded == record
    bar = _bar_from_record(loaded)
    assert bar.source == "alpaca-sip-1d-raw"
    assert (bar.open, bar.high, bar.low, bar.close) == (
        Decimal("84.51"),
        Decimal("84.51"),
        Decimal("83.0675"),
        Decimal("83.39"),
    )


def test_tampered_sip_recovery_is_rejected(tmp_path):
    record = _sip_record()
    store = MetricStore(tmp_path / "metrics.sqlite3")
    store.save_governed_bar_recovery(record)

    with pytest.raises(ValueError):
        store.save_governed_bar_recovery(
            replace(
                record, reconstructed_bar={**record.reconstructed_bar, "high": "999"}
            )
        )


def test_governed_preflight_accepts_bound_sip_recovery():
    record = _sip_record()
    resolution = GovernedInputResolution(
        bars={"BRC": _bar_from_record(record)},
        recovery_bindings={"BRC": _binding(record)},
        recovery_summaries=(_summary(record),),
        failure_map={},
    )
    snapshot = SimpleNamespace(
        governed_tickers=("BRC",),
        cohort_ids_by_ticker={"BRC": record.affected_cohort_ids},
    )
    summaries, failures = _validate_governed_resolution(
        resolution,
        snapshot=snapshot,
        session=SESSION,
        processed_at=datetime(2026, 9, 22, 22, 6, tzinfo=timezone.utc),
    )
    assert failures == {}
    assert summaries[0]["contract_version"] == record.contract_version


def test_failed_yahoo_bar_uses_persisted_sip_and_replays_without_network(tmp_path):
    record = _sip_record()
    attempted_at = datetime(2026, 9, 22, 22, 1, 24, tzinfo=timezone.utc)
    fetched_at = datetime(2026, 9, 22, 22, 5, tzinfo=timezone.utc)
    processed_at = datetime(2026, 9, 22, 22, 6, tzinfo=timezone.utc)
    start = datetime(2026, 9, 22, 0, tzinfo=ZoneInfo("America/New_York"))
    attempt = GovernedDailyBarAttempt(
        ticker="BRC",
        session=SESSION,
        source="yfinance",
        fetched_at=attempted_at,
        raw_ohlc={
            key: Decimal(str(record.original_daily[key]))
            for key in ("open", "high", "low", "close")
        },
        validation_error="incoherent BRC/2026-09-22",
    )
    recovery = GovernedBarRecoveryEvidence(
        ticker="BRC",
        session=SESSION,
        daily_attempt=attempt,
        expected_starts=tuple(
            datetime(2026, 9, 22, hour, 30, tzinfo=ZoneInfo("America/New_York"))
            for hour in range(9, 16)
        ),
        observed_starts=(),
        intraday_bars=(),
        reconstructed=None,
        validation_error="invalid BRC/2026-09-22",
    )
    yahoo_resolution = GovernedDailyBarResolution(
        bars={},
        attempts={"BRC": attempt},
        recoveries={"BRC": recovery},
        failure_map={"BRC": "invalid BRC/2026-09-22"},
    )
    sip_bar = MarketBar(
        ticker="BRC",
        session=SESSION,
        open=Decimal("84.51"),
        high=Decimal("84.51"),
        low=Decimal("83.0675"),
        close=Decimal("83.39"),
        source=ALPACA_SOURCE,
        fetched_at=fetched_at,
        adjusted=False,
    )
    sip_result = AlpacaDailyBarResult(
        bar=sip_bar,
        failure=None,
        request_start=start,
        request_end=datetime(2026, 9, 22, 21, 50, tzinfo=timezone.utc),
        bar_timestamp=start,
        response_symbol="BRC",
        row_count=1,
        pagination_complete=True,
    )

    class Yahoo:
        def resolve_governed_daily_bars(self, tickers, session, *, processed_at):
            assert tuple(tickers) == ("BRC",)
            assert session == SESSION
            return yahoo_resolution

    class SIP:
        def fetch_daily_bar(self, ticker, session, *, now):
            assert (ticker, session) == ("BRC", SESSION)
            return sip_result

    store = MetricStore(tmp_path / "metrics.sqlite3")
    first = resolve_governed_bars(
        price_source=Yahoo(),
        alpaca_sip_source=SIP(),
        metric_store=store,
        epoch_id=EPOCH,
        session=SESSION,
        tickers=("BRC",),
        cohort_ids_by_ticker={"BRC": ("horizon_30d_size_5k",)},
        processed_at=processed_at,
        persist=True,
    )
    assert first.failure_map == {}
    assert first.bars["BRC"] == sip_bar
    assert first.recovery_bindings["BRC"].contract_version == "alpaca-sip-1d-raw-v1"

    class NoNetwork:
        def __getattr__(self, name):
            raise AssertionError(f"network was called through {name}")

    replay = resolve_governed_bars(
        price_source=NoNetwork(),
        alpaca_sip_source=NoNetwork(),
        metric_store=store,
        epoch_id=EPOCH,
        session=SESSION,
        tickers=("BRC",),
        cohort_ids_by_ticker={"BRC": ("horizon_30d_size_5k",)},
        processed_at=processed_at,
        persist=True,
    )
    assert replay.bars == first.bars
    assert replay.recovery_bindings == first.recovery_bindings


def test_unavailable_sip_keeps_governed_failure_and_writes_no_recovery(tmp_path):
    record = _sip_record()
    attempt = GovernedDailyBarAttempt(
        ticker="BRC",
        session=SESSION,
        source="yfinance",
        fetched_at=datetime(2026, 9, 22, 22, 1, tzinfo=timezone.utc),
        raw_ohlc={
            key: Decimal(str(record.original_daily[key]))
            for key in ("open", "high", "low", "close")
        },
        validation_error=f"incoherent BRC/{SESSION}",
    )
    recovery = GovernedBarRecoveryEvidence(
        ticker="BRC",
        session=SESSION,
        daily_attempt=attempt,
        expected_starts=tuple(
            datetime(2026, 9, 22, hour, 30, tzinfo=ZoneInfo("America/New_York"))
            for hour in range(9, 16)
        ),
        observed_starts=(),
        intraday_bars=(),
        reconstructed=None,
        validation_error=f"invalid BRC/{SESSION}",
    )

    class Yahoo:
        def resolve_governed_daily_bars(self, tickers, session, *, processed_at):
            return GovernedDailyBarResolution(
                bars={},
                attempts={"BRC": attempt},
                recoveries={"BRC": recovery},
                failure_map={"BRC": f"invalid BRC/{SESSION}"},
            )

    class SIP:
        def fetch_daily_bar(self, ticker, session, *, now):
            return AlpacaDailyBarResult(None, AlpacaBarFailure.MISSING_CREDENTIALS)

    store = MetricStore(tmp_path / "metrics.sqlite3")
    result = resolve_governed_bars(
        price_source=Yahoo(),
        alpaca_sip_source=SIP(),
        metric_store=store,
        epoch_id=EPOCH,
        session=SESSION,
        tickers=("BRC",),
        cohort_ids_by_ticker={"BRC": ("horizon_30d_size_5k",)},
        processed_at=datetime(2026, 9, 22, 22, 6, tzinfo=timezone.utc),
        persist=True,
    )
    assert result.failure_map == {"BRC": f"invalid BRC/{SESSION}"}
    assert result.bars == {}
    assert result.recovery_bindings == {}
    assert (
        store.load_governed_bar_recovery(epoch_id=EPOCH, session=SESSION, ticker="BRC")
        is None
    )
