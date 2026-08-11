from __future__ import annotations

import sqlite3
import traceback
from collections.abc import Mapping
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import MappingProxyType
from zoneinfo import ZoneInfo

import pytest

import tradingagents.strategies.orchestration.governed_market_data as governed_market_data_module

from tradingagents.strategies.execution.models import MarketBar
from tradingagents.strategies.execution.price_source import (
    GovernedBarRecoveryEvidence,
    GovernedDailyBarAttempt,
    GovernedDailyBarResolution,
    IntradayBarEvidence,
)
from tradingagents.strategies.metrics.models import GovernedBarRecoveryRecord
from tradingagents.strategies.metrics.store import MetricStore
from tradingagents.strategies.orchestration.governed_market_data import (
    GOVERNED_BAR_RECOVERY_CONTRACT,
    GovernedMarketDataError,
    resolve_governed_bars,
)


SESSION = date(2026, 8, 10)
FETCHED_AT = datetime(2026, 8, 10, 22, 1, 31, tzinfo=timezone.utc)
PROCESSED_AT = datetime(2026, 8, 10, 22, 5, tzinfo=timezone.utc)
PRE_CLOSE_FETCHED_AT = datetime(2026, 8, 10, 19, 59, tzinfo=timezone.utc)
ET = ZoneInfo("America/New_York")
EXPECTED_STARTS = tuple(
    datetime(2026, 8, 10, hour, 30, tzinfo=ET) for hour in range(9, 16)
)
HOURLY_OHLC = (
    ("286.2099914550781", "286.2099914550781", "284.3500061035156", "284.79998779296875"),
    ("284.79998779296875", "285.82501220703125", "284.4700012207031", "285.5"),
    ("285.5", "285.6000061035156", "283.8999938964844", "284.1000061035156"),
    ("284.1000061035156", "284.29998779296875", "282.75", "283.0"),
    ("283.0", "283.5", "282.1000061035156", "282.45001220703125"),
    ("282.45001220703125", "283.45001220703125", "281.5299987792969", "282.8999938964844"),
    ("282.8999938964844", "283.5", "282.70001220703125", "283.2099914550781"),
)


@pytest.fixture(autouse=True)
def _controlled_coordinator_clock(monkeypatch) -> None:
    monkeypatch.setattr(
        governed_market_data_module,
        "_utc_now",
        lambda: PROCESSED_AT + timedelta(seconds=1),
        raising=False,
    )


class FakePriceSource:
    def __init__(self, resolution: GovernedDailyBarResolution | Exception) -> None:
        self.resolution = resolution
        self.calls: list[tuple[tuple[str, ...], date, datetime]] = []

    def resolve_governed_daily_bars(
        self,
        tickers,
        session: date,
        *,
        processed_at: datetime,
        max_age=None,
    ) -> GovernedDailyBarResolution:
        self.calls.append((tuple(tickers), session, processed_at))
        if isinstance(self.resolution, Exception):
            raise self.resolution
        return self.resolution


def _healthy_bar(ticker: str = "AAPL") -> MarketBar:
    return MarketBar(
        ticker=ticker,
        session=SESSION,
        open=Decimal("100"),
        high=Decimal("103"),
        low=Decimal("99"),
        close=Decimal("102"),
        source="yfinance",
        fetched_at=FETCHED_AT,
        adjusted=False,
    )


def _healthy_attempt(
    bar: MarketBar,
    *,
    raw_ohlc: Mapping[str, object] | None = None,
) -> GovernedDailyBarAttempt:
    evidence = raw_ohlc
    if evidence is None:
        evidence = {
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
        }
    return GovernedDailyBarAttempt(
        ticker=bar.ticker,
        session=bar.session,
        source=bar.source,
        fetched_at=bar.fetched_at,
        raw_ohlc=evidence,
        validation_error=None,
    )


def _daily_attempt(ticker: str = "ESS") -> GovernedDailyBarAttempt:
    return GovernedDailyBarAttempt(
        ticker=ticker,
        session=SESSION,
        source="yfinance",
        fetched_at=FETCHED_AT,
        raw_ohlc={
            "open": Decimal("286.2099914550781"),
            "high": Decimal("285.82501220703125"),
            "low": Decimal("281.5299987792969"),
            "close": Decimal("283.2099914550781"),
        },
        validation_error=f"incoherent {ticker}/{SESSION}",
    )


def _accepted_recovery(ticker: str = "ESS") -> GovernedBarRecoveryEvidence:
    bars = tuple(
        IntradayBarEvidence(
            start=start,
            open=Decimal(values[0]),
            high=Decimal(values[1]),
            low=Decimal(values[2]),
            close=Decimal(values[3]),
            fetched_at=FETCHED_AT,
        )
        for start, values in zip(EXPECTED_STARTS, HOURLY_OHLC)
    )
    return GovernedBarRecoveryEvidence(
        ticker=ticker,
        session=SESSION,
        daily_attempt=_daily_attempt(ticker),
        expected_starts=EXPECTED_STARTS,
        observed_starts=EXPECTED_STARTS,
        intraday_bars=bars,
        reconstructed=MarketBar(
            ticker=ticker,
            session=SESSION,
            open=Decimal("286.2099914550781"),
            high=Decimal("286.2099914550781"),
            low=Decimal("281.5299987792969"),
            close=Decimal("283.2099914550781"),
            source="yfinance-60m-reconstruction",
            fetched_at=FETCHED_AT,
            adjusted=False,
        ),
        validation_error=None,
    )


def _accepted_resolution(ticker: str = "ESS") -> GovernedDailyBarResolution:
    recovery = _accepted_recovery(ticker)
    assert recovery.reconstructed is not None
    return GovernedDailyBarResolution(
        bars={ticker: recovery.reconstructed},
        attempts={ticker: recovery.daily_attempt},
        recoveries={ticker: recovery},
        failure_map={},
    )


def _record(
    *,
    epoch_id: str = "epoch-1",
    cohorts: tuple[str, ...] = ("cohort-a",),
    contract_version: str = GOVERNED_BAR_RECOVERY_CONTRACT,
) -> GovernedBarRecoveryRecord:
    recovery = _accepted_recovery()
    assert recovery.daily_attempt.raw_ohlc is not None
    assert recovery.reconstructed is not None
    return GovernedBarRecoveryRecord.create(
        contract_version=contract_version,
        epoch_id=epoch_id,
        session=SESSION,
        ticker="ESS",
        original_daily={
            **recovery.daily_attempt.raw_ohlc,
            "source": recovery.daily_attempt.source,
            "fetched_at": recovery.daily_attempt.fetched_at,
        },
        original_validation_error=recovery.daily_attempt.validation_error,
        expected_starts=recovery.expected_starts,
        observed_starts=recovery.observed_starts,
        intraday_rows=tuple(
            {
                "start": row.start,
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "fetched_at": row.fetched_at,
            }
            for row in recovery.intraday_bars
        ),
        reconstructed_bar={
            "open": recovery.reconstructed.open,
            "high": recovery.reconstructed.high,
            "low": recovery.reconstructed.low,
            "close": recovery.reconstructed.close,
            "source": recovery.reconstructed.source,
        },
        final_validation_error=recovery.validation_error,
        affected_cohort_ids=cohorts,
    )


def _resolve(source, store, *, tickers=("ESS",), cohorts=None, persist=True):
    return resolve_governed_bars(
        price_source=source,
        metric_store=store,
        epoch_id="epoch-1",
        session=SESSION,
        tickers=tickers,
        cohort_ids_by_ticker=(
            cohorts
            if cohorts is not None
            else {ticker: ("cohort-a",) for ticker in tickers}
        ),
        processed_at=PROCESSED_AT,
        persist=persist,
    )


def test_healthy_bars_pass_through_without_recovery_record(tmp_path) -> None:
    store = MetricStore(tmp_path / "metrics.sqlite3")
    bar = _healthy_bar()
    source = FakePriceSource(
        GovernedDailyBarResolution(
            bars={"AAPL": bar},
            attempts={"AAPL": _healthy_attempt(bar)},
            recoveries={},
            failure_map={},
        )
    )

    resolved = _resolve(source, store, tickers=("AAPL",))

    assert resolved.bars == {"AAPL": bar}
    assert resolved.recovery_bindings == {}
    assert resolved.recovery_summaries == ()
    assert resolved.failure_map == {}
    assert store.load_governed_bar_recovery(
        epoch_id="epoch-1", session=SESSION, ticker="AAPL"
    ) is None


def test_accepted_recovery_is_persisted_before_it_is_returned(tmp_path) -> None:
    store = MetricStore(tmp_path / "metrics.sqlite3")
    source = FakePriceSource(_accepted_resolution())

    resolved = _resolve(source, store)
    binding = resolved.recovery_bindings["ESS"]
    stored = store.load_governed_bar_recovery_by_id(binding.recovery_id)

    assert stored is not None
    assert stored.evidence_digest == binding.evidence_digest
    assert resolved.bars["ESS"] == _accepted_recovery().reconstructed
    assert resolved.recovery_summaries == (
        {
            "ticker": "ESS",
            "session": SESSION.isoformat(),
            "recovery_id": binding.recovery_id,
            "contract_version": GOVERNED_BAR_RECOVERY_CONTRACT,
            "evidence_digest": binding.evidence_digest,
            "affected_cohort_ids": ("cohort-a",),
        },
    )


def test_second_call_reuses_current_record_without_provider_call(tmp_path) -> None:
    store = MetricStore(tmp_path / "metrics.sqlite3")
    first = _resolve(FakePriceSource(_accepted_resolution()), store)
    source = FakePriceSource(AssertionError("provider must not be called"))

    second = _resolve(source, store)

    assert source.calls == []
    assert second == first


def test_reused_ticker_is_removed_from_mixed_provider_batch(tmp_path) -> None:
    store = MetricStore(tmp_path / "metrics.sqlite3")
    store.save_governed_bar_recovery(_record())
    healthy = _healthy_bar("AAPL")
    source = FakePriceSource(
        GovernedDailyBarResolution(
            bars={"AAPL": healthy},
            attempts={"AAPL": _healthy_attempt(healthy)},
            recoveries={},
            failure_map={},
        )
    )

    resolved = _resolve(source, store, tickers=("ESS", "AAPL"))

    assert source.calls == [(('AAPL',), SESSION, PROCESSED_AT)]
    assert set(resolved.bars) == {"AAPL", "ESS"}
    assert tuple(resolved.recovery_bindings) == ("ESS",)


def test_changed_cohort_membership_fails_closed_without_provider_bypass(
    tmp_path,
) -> None:
    store = MetricStore(tmp_path / "metrics.sqlite3")
    store.save_governed_bar_recovery(_record(cohorts=("cohort-a",)))
    source = FakePriceSource(
        GovernedDailyBarResolution(
            bars={"ESS": _healthy_bar("ESS")},
            attempts={"ESS": _healthy_attempt(_healthy_bar("ESS"))},
            recoveries={},
            failure_map={},
        )
    )

    with pytest.raises(GovernedMarketDataError) as caught:
        _resolve(source, store, cohorts={"ESS": ("cohort-b",)})

    assert source.calls == []
    assert caught.value.failure_map == {"ESS": f"invalid ESS/{SESSION}"}


class RacingStore:
    def __init__(self, store: MetricStore) -> None:
        self.store = store

    def load_governed_bar_recovery(self, **scope):
        return self.store.load_governed_bar_recovery(**scope)

    def save_governed_bar_recovery(self, record: GovernedBarRecoveryRecord) -> None:
        self.store.save_governed_bar_recovery(
            GovernedBarRecoveryRecord.create(
                **{**record.evidence_fields(), "affected_cohort_ids": ("cohort-b",)}
            )
        )
        self.store.save_governed_bar_recovery(record)


def test_concurrent_unequal_persistence_race_fails_closed(tmp_path) -> None:
    source = FakePriceSource(_accepted_resolution())
    store = RacingStore(MetricStore(tmp_path / "metrics.sqlite3"))

    with pytest.raises(GovernedMarketDataError) as caught:
        _resolve(source, store)

    assert caught.value.failure_map == {"ESS": f"invalid ESS/{SESSION}"}
    assert "unequal payload" not in str(caught.value)


def test_probe_mode_returns_same_identity_without_creating_state(tmp_path) -> None:
    proposal = _resolve(
        FakePriceSource(_accepted_resolution()), None, persist=False
    )
    assert list(tmp_path.iterdir()) == []

    stored = _resolve(
        FakePriceSource(_accepted_resolution()),
        MetricStore(tmp_path / "metrics.sqlite3"),
        persist=True,
    )

    assert proposal.recovery_bindings == stored.recovery_bindings
    assert proposal.recovery_summaries == stored.recovery_summaries


def test_rejected_recovery_returns_exact_failure_map_without_record(tmp_path) -> None:
    store = MetricStore(tmp_path / "metrics.sqlite3")
    attempt = _daily_attempt()
    failure = f"invalid ESS/{SESSION}"
    recovery = replace(
        _accepted_recovery(), reconstructed=None, validation_error=failure
    )
    source = FakePriceSource(
        GovernedDailyBarResolution(
            bars={},
            attempts={"ESS": attempt},
            recoveries={"ESS": recovery},
            failure_map={"ESS": failure},
        )
    )

    resolved = _resolve(source, store)

    assert resolved.failure_map == {"ESS": failure}
    assert resolved.recovery_bindings == {}
    assert resolved.recovery_summaries == ()
    assert store.load_governed_bar_recovery(
        epoch_id="epoch-1", session=SESSION, ticker="ESS"
    ) is None


def test_two_cohorts_share_one_stable_recovery_binding(tmp_path) -> None:
    store = MetricStore(tmp_path / "metrics.sqlite3")

    resolved = _resolve(
        FakePriceSource(_accepted_resolution()),
        store,
        cohorts={"ESS": ("cohort-b", "cohort-a", "cohort-a")},
    )

    assert tuple(resolved.recovery_bindings) == ("ESS",)
    assert resolved.recovery_summaries[0]["affected_cohort_ids"] == (
        "cohort-a",
        "cohort-b",
    )
    assert store.load_governed_bar_recovery(
        epoch_id="epoch-1", session=SESSION, ticker="ESS"
    ).affected_cohort_ids == ("cohort-a", "cohort-b")


class StaticStore:
    def __init__(self, record: GovernedBarRecoveryRecord) -> None:
        self.record = record
        self.saved: list[GovernedBarRecoveryRecord] = []

    def load_governed_bar_recovery(self, **scope):
        return self.record

    def save_governed_bar_recovery(self, record: GovernedBarRecoveryRecord) -> None:
        self.saved.append(record)


def _assert_secret_is_absent(
    error: GovernedMarketDataError, secret: str
) -> None:
    assert secret not in str(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert secret not in "".join(traceback.format_exception(error))


class ExplodingTickers:
    def __iter__(self):
        raise RuntimeError("input-credential-secret")


class ExplodingLoadStore:
    def load_governed_bar_recovery(self, **scope):
        raise RuntimeError("load-credential-secret")


class ExplodingRecord:
    contract_version = GOVERNED_BAR_RECOVERY_CONTRACT
    epoch_id = "epoch-1"
    session = SESSION
    ticker = "ESS"
    affected_cohort_ids = ("cohort-a",)

    def validate_integrity(self) -> None:
        raise RuntimeError("record-credential-secret")


class ExplodingSaveStore:
    read_only = False

    def load_governed_bar_recovery(self, **scope):
        return None

    def save_governed_bar_recovery(self, record) -> None:
        raise RuntimeError("save-credential-secret")


def test_input_failure_has_no_raw_exception_chain() -> None:
    with pytest.raises(GovernedMarketDataError) as caught:
        _resolve(
            FakePriceSource(AssertionError("provider must not be called")),
            None,
            tickers=ExplodingTickers(),
            cohorts={"ESS": ("cohort-a",)},
            persist=False,
        )

    _assert_secret_is_absent(caught.value, "input-credential-secret")


def test_store_load_failure_has_no_raw_exception_chain() -> None:
    with pytest.raises(GovernedMarketDataError) as caught:
        _resolve(
            FakePriceSource(AssertionError("provider must not be called")),
            ExplodingLoadStore(),
        )

    _assert_secret_is_absent(caught.value, "load-credential-secret")


def test_record_validation_failure_has_no_raw_exception_chain() -> None:
    with pytest.raises(GovernedMarketDataError) as caught:
        _resolve(
            FakePriceSource(AssertionError("provider must not be called")),
            StaticStore(ExplodingRecord()),
        )

    _assert_secret_is_absent(caught.value, "record-credential-secret")


def test_provider_invariant_failure_has_no_raw_exception_chain(monkeypatch) -> None:
    def explode(*args, **kwargs):
        raise RuntimeError("provider-invariant-credential-secret")

    monkeypatch.setattr(governed_market_data_module, "validate_required_bars", explode)
    bar = _healthy_bar("ESS")
    source = FakePriceSource(
        GovernedDailyBarResolution(
            bars={"ESS": bar},
            attempts={"ESS": _healthy_attempt(bar)},
            recoveries={},
            failure_map={},
        )
    )

    with pytest.raises(GovernedMarketDataError) as caught:
        _resolve(source, None, persist=False)

    _assert_secret_is_absent(
        caught.value, "provider-invariant-credential-secret"
    )


def test_persistence_failure_has_no_raw_exception_chain() -> None:
    with pytest.raises(GovernedMarketDataError) as caught:
        _resolve(FakePriceSource(_accepted_resolution()), ExplodingSaveStore())

    _assert_secret_is_absent(caught.value, "save-credential-secret")


def test_unsupported_contract_record_is_never_reused() -> None:
    store = StaticStore(_record(contract_version="yfinance-60m-v0"))
    source = FakePriceSource(AssertionError("provider must not be called"))

    with pytest.raises(GovernedMarketDataError) as caught:
        _resolve(source, store)

    assert caught.value.failure_map == {"ESS": f"invalid ESS/{SESSION}"}
    assert source.calls == []
    assert store.saved == []


def test_provider_exception_is_normalized_for_each_unresolved_ticker() -> None:
    source = FakePriceSource(RuntimeError("credential=super-secret"))

    resolved = _resolve(
        source,
        None,
        tickers=("ESS", "AAPL"),
        persist=False,
    )

    assert resolved.failure_map == {
        "AAPL": f"invalid AAPL/{SESSION}",
        "ESS": f"invalid ESS/{SESSION}",
    }
    assert "super-secret" not in repr(resolved.failure_map)


@pytest.mark.parametrize(
    "resolution",
    [
        GovernedDailyBarResolution(
            bars={"OTHER": _healthy_bar("OTHER")},
            attempts={"OTHER": _daily_attempt("OTHER")},
            recoveries={},
            failure_map={},
        ),
        GovernedDailyBarResolution(
            bars={"ESS": _accepted_recovery().reconstructed},
            attempts={"ESS": _daily_attempt()},
            recoveries={},
            failure_map={},
        ),
        GovernedDailyBarResolution(
            bars={},
            attempts={"ESS": _daily_attempt()},
            recoveries={"ESS": _accepted_recovery()},
            failure_map={},
        ),
        GovernedDailyBarResolution(
            bars={"ESS": _healthy_bar("ESS")},
            attempts={"ESS": _daily_attempt()},
            recoveries={
                "ESS": replace(
                    _accepted_recovery(),
                    reconstructed=None,
                    validation_error=f"invalid ESS/{SESSION}",
                )
            },
            failure_map={},
        ),
    ],
)
def test_incoherent_provider_result_scope_fails_closed(resolution) -> None:
    with pytest.raises(GovernedMarketDataError) as caught:
        _resolve(FakePriceSource(resolution), None, persist=False)

    assert caught.value.failure_map == {"ESS": f"invalid ESS/{SESSION}"}


def test_healthy_bar_from_non_authoritative_source_fails_closed() -> None:
    bar = replace(_healthy_bar("ESS"), source="alpaca-iex")
    source = FakePriceSource(
        GovernedDailyBarResolution(
            bars={"ESS": bar},
            attempts={"ESS": _healthy_attempt(bar)},
            recoveries={},
            failure_map={},
        )
    )

    with pytest.raises(GovernedMarketDataError) as caught:
        _resolve(source, None, persist=False)

    assert caught.value.failure_map == {"ESS": f"invalid ESS/{SESSION}"}


def test_post_fetch_bar_timestamp_may_follow_processed_at() -> None:
    fetched_at = PROCESSED_AT + timedelta(milliseconds=1)
    bar = replace(_healthy_bar("ESS"), fetched_at=fetched_at)
    source = FakePriceSource(
        GovernedDailyBarResolution(
            bars={"ESS": bar},
            attempts={"ESS": _healthy_attempt(bar)},
            recoveries={},
            failure_map={},
        )
    )

    resolved = _resolve(source, None, persist=False)

    assert resolved.bars == {"ESS": bar}
    assert resolved.failure_map == {}


def test_bar_future_relative_to_controlled_post_fetch_clock_fails_closed() -> None:
    bar = replace(
        _healthy_bar("ESS"), fetched_at=PROCESSED_AT + timedelta(seconds=2)
    )
    source = FakePriceSource(
        GovernedDailyBarResolution(
            bars={"ESS": bar},
            attempts={"ESS": _healthy_attempt(bar)},
            recoveries={},
            failure_map={},
        )
    )

    with pytest.raises(GovernedMarketDataError) as caught:
        _resolve(source, None, persist=False)

    assert caught.value.failure_map == {"ESS": f"invalid ESS/{SESSION}"}


@pytest.mark.parametrize(
    "raw_ohlc",
    [
        None,
        {"open": Decimal("100"), "high": Decimal("103"), "low": Decimal("99")},
        {
            "open": Decimal("100"),
            "high": Decimal("103"),
            "low": Decimal("99"),
            "close": Decimal("102"),
            "extra": Decimal("1"),
        },
        {
            "open": 100.0,
            "high": Decimal("103"),
            "low": Decimal("99"),
            "close": Decimal("102"),
        },
        {
            "open": Decimal("100"),
            "high": Decimal("103"),
            "low": Decimal("99"),
            "close": Decimal("101"),
        },
    ],
)
def test_healthy_attempt_must_bind_exact_decimal_ohlc(raw_ohlc) -> None:
    bar = _healthy_bar("ESS")
    attempt = GovernedDailyBarAttempt(
        ticker="ESS",
        session=SESSION,
        source="yfinance",
        fetched_at=FETCHED_AT,
        raw_ohlc=raw_ohlc,
        validation_error=None,
    )
    source = FakePriceSource(
        GovernedDailyBarResolution(
            bars={"ESS": bar},
            attempts={"ESS": attempt},
            recoveries={},
            failure_map={},
        )
    )

    with pytest.raises(GovernedMarketDataError) as caught:
        _resolve(source, None, persist=False)

    assert caught.value.failure_map == {"ESS": f"invalid ESS/{SESSION}"}


def test_persisted_pre_close_recovery_fails_closed_without_binding(tmp_path) -> None:
    store = MetricStore(tmp_path / "metrics.sqlite3")
    record = _record()
    pre_close_record = GovernedBarRecoveryRecord.create(
        **{
            **record.evidence_fields(),
            "original_daily": {
                **record.original_daily,
                "fetched_at": PRE_CLOSE_FETCHED_AT,
            },
            "intraday_rows": tuple(
                {**row, "fetched_at": PRE_CLOSE_FETCHED_AT}
                for row in record.intraday_rows
            ),
        }
    )
    store.save_governed_bar_recovery(pre_close_record)
    source = FakePriceSource(AssertionError("provider must not be called"))

    with pytest.raises(GovernedMarketDataError) as caught:
        _resolve(source, store)

    assert source.calls == []
    assert caught.value.failure_map == {"ESS": f"invalid ESS/{SESSION}"}


def test_pre_close_healthy_provider_bar_fails_closed_without_binding() -> None:
    bar = replace(_healthy_bar("ESS"), fetched_at=PRE_CLOSE_FETCHED_AT)
    source = FakePriceSource(
        GovernedDailyBarResolution(
            bars={"ESS": bar},
            attempts={"ESS": _healthy_attempt(bar)},
            recoveries={},
            failure_map={},
        )
    )

    with pytest.raises(GovernedMarketDataError) as caught:
        _resolve(source, None, persist=False)

    assert caught.value.failure_map == {"ESS": f"invalid ESS/{SESSION}"}


def test_pre_close_provider_recovery_fails_closed_without_binding() -> None:
    recovery = _accepted_recovery()
    pre_close_recovery = replace(
        recovery,
        daily_attempt=replace(
            recovery.daily_attempt, fetched_at=PRE_CLOSE_FETCHED_AT
        ),
        intraday_bars=tuple(
            replace(row, fetched_at=PRE_CLOSE_FETCHED_AT)
            for row in recovery.intraday_bars
        ),
        reconstructed=replace(
            recovery.reconstructed, fetched_at=PRE_CLOSE_FETCHED_AT
        ),
    )
    assert pre_close_recovery.reconstructed is not None
    source = FakePriceSource(
        GovernedDailyBarResolution(
            bars={"ESS": pre_close_recovery.reconstructed},
            attempts={"ESS": pre_close_recovery.daily_attempt},
            recoveries={"ESS": pre_close_recovery},
            failure_map={},
        )
    )

    with pytest.raises(GovernedMarketDataError) as caught:
        _resolve(source, None, persist=False)

    assert caught.value.failure_map == {"ESS": f"invalid ESS/{SESSION}"}


@pytest.mark.parametrize(
    "cohorts",
    [
        {},
        {"ESS": ()},
        {"ESS": ("cohort-a",), "OTHER": ("cohort-b",)},
    ],
)
def test_cohort_scope_must_be_exact_and_nonempty_before_provider(cohorts) -> None:
    source = FakePriceSource(AssertionError("provider must not be called"))

    with pytest.raises(GovernedMarketDataError):
        _resolve(source, None, cohorts=cohorts, persist=False)

    assert source.calls == []


class CountingStore:
    def __init__(self) -> None:
        self.loads = 0

    def load_governed_bar_recovery(self, **scope):
        self.loads += 1
        return None


def test_yahoo_alias_collision_fails_before_store_or_provider() -> None:
    store = CountingStore()
    source = FakePriceSource(AssertionError("provider must not be called"))

    with pytest.raises(GovernedMarketDataError):
        _resolve(
            source,
            store,
            tickers=("BRK/B", "BRK-B"),
            cohorts={"BRK/B": ("cohort-a",), "BRK-B": ("cohort-b",)},
            persist=False,
        )

    assert store.loads == 0
    assert source.calls == []


def test_persisting_an_accepted_recovery_requires_metric_store() -> None:
    with pytest.raises(GovernedMarketDataError) as caught:
        _resolve(FakePriceSource(_accepted_resolution()), None, persist=True)

    assert caught.value.failure_map == {"ESS": f"invalid ESS/{SESSION}"}


def test_returned_result_and_nested_summaries_are_immutable() -> None:
    resolved = _resolve(
        FakePriceSource(_accepted_resolution()), None, persist=False
    )

    assert isinstance(resolved.bars, MappingProxyType)
    assert isinstance(resolved.recovery_bindings, MappingProxyType)
    assert isinstance(resolved.failure_map, MappingProxyType)
    assert isinstance(resolved.recovery_summaries[0], MappingProxyType)
    with pytest.raises(TypeError):
        resolved.recovery_summaries[0]["ticker"] = "OTHER"


def test_probe_mode_with_existing_store_never_writes(tmp_path) -> None:
    path = tmp_path / "metrics.sqlite3"
    store = MetricStore(path)
    source = FakePriceSource(_accepted_resolution())

    resolved = _resolve(source, store, persist=False)

    assert resolved.recovery_bindings["ESS"].recovery_id
    with sqlite3.connect(path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM governed_bar_recoveries"
        ).fetchone()[0]
    assert count == 0


def test_probe_mode_legacy_read_only_store_preserves_schema_and_data_version(
    tmp_path,
) -> None:
    path = tmp_path / "legacy-metrics.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE metric_epochs (
              epoch_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL
            );
            CREATE TABLE outcomes (
              outcome_id TEXT PRIMARY KEY,
              epoch_id TEXT NOT NULL,
              payload_json TEXT NOT NULL
            );
            CREATE TABLE strategy_health (
              health_id TEXT PRIMARY KEY,
              epoch_id TEXT NOT NULL,
              session TEXT NOT NULL,
              payload_json TEXT NOT NULL
            );
            CREATE TABLE critical_gap_markers (
              marker_id TEXT PRIMARY KEY,
              status TEXT NOT NULL,
              gap_session TEXT NOT NULL,
              payload_json TEXT NOT NULL
            );
            """
        )
    store = MetricStore.open_existing(path)
    with sqlite3.connect(path) as observer:
        tables_before = observer.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
        data_version_before = observer.execute("PRAGMA data_version").fetchone()[0]

        resolved = _resolve(
            FakePriceSource(_accepted_resolution()), store, persist=False
        )

        tables_after = observer.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
        data_version_after = observer.execute("PRAGMA data_version").fetchone()[0]

    assert resolved.recovery_bindings["ESS"].recovery_id
    assert tables_after == tables_before
    assert data_version_after == data_version_before
    assert "governed_bar_recoveries" not in {row[0] for row in tables_after}


def test_probe_mode_applies_store_equivalent_schedule_validation() -> None:
    recovery = _accepted_recovery()
    shifted_starts = tuple(start.replace(minute=31) for start in EXPECTED_STARTS)
    malformed = replace(
        recovery,
        expected_starts=shifted_starts,
        observed_starts=shifted_starts,
        intraday_bars=tuple(
            replace(row, start=start)
            for row, start in zip(recovery.intraday_bars, shifted_starts)
        ),
    )
    assert malformed.reconstructed is not None
    source = FakePriceSource(
        GovernedDailyBarResolution(
            bars={"ESS": malformed.reconstructed},
            attempts={"ESS": malformed.daily_attempt},
            recoveries={"ESS": malformed},
            failure_map={},
        )
    )

    with pytest.raises(GovernedMarketDataError) as caught:
        _resolve(source, None, persist=False)

    assert caught.value.failure_map == {"ESS": f"invalid ESS/{SESSION}"}
