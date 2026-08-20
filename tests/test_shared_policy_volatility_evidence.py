from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd
import pytest

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.strategies.execution.contracts import POLICY_DOCUMENT_VERSION
from tradingagents.strategies.execution.models import AccountSnapshot, MarketBar
from tradingagents.strategies.orchestration.cohort_orchestrator import (
    CohortOrchestrator,
    build_default_cohorts,
)
from tradingagents.strategies.orchestration.session_executor import SessionInputBundle
from tradingagents.strategies.orchestration.trading_calendar import previous_session
from tradingagents.strategies.trading.portfolio_policy import (
    annualized_volatility,
    build_annualized_volatility_evidence,
)


SESSION = date(2026, 7, 31)
TICKERS = ("OPEN", "PENDING", "CANDIDATE")


def _xnys_sessions(end: date, count: int) -> tuple[date, ...]:
    descending = [end]
    for _ in range(count - 1):
        descending.append(previous_session(descending[-1]))
    return tuple(reversed(descending))


def _history(step: float, *, end: date | None = None) -> pd.DataFrame:
    closes = [100.0]
    for index in range(61):
        signed_step = step if index % 2 == 0 else -step
        closes.append(closes[-1] * (1.0 + signed_step))
    return pd.DataFrame(
        {"Close": closes},
        index=pd.DatetimeIndex(
            _xnys_sessions(end or previous_session(SESSION), len(closes))
        ),
    )


def _invalid_pending_history(kind: str) -> pd.DataFrame:
    history = _history(0.027)
    if kind == "stale":
        return _history(
            0.027, end=previous_session(previous_session(SESSION))
        ).tail(61)
    if kind == "missing_middle":
        return history.drop(history.index[-30])
    index = list(history.index)
    if kind == "duplicate":
        index[-20] = index[-21]
    elif kind == "out_of_order":
        index[-20], index[-21] = index[-21], index[-20]
    else:  # pragma: no cover - parameter invariant
        raise AssertionError(kind)
    invalid = history.copy()
    invalid.index = pd.DatetimeIndex(index)
    return invalid


def _snapshot(ledger, epoch_id: str) -> AccountSnapshot:
    account = ledger.account_state()
    zero = Decimal("0")
    return AccountSnapshot(
        snapshot_id=f"fixture-{ledger.cohort_id}",
        cohort_id=ledger.cohort_id,
        epoch_id=epoch_id,
        session=SESSION,
        valuation_at=datetime(2026, 7, 31, 20, tzinfo=timezone.utc),
        cash=account.cash,
        long_market_value=account.long_market_value,
        short_liability=account.short_liability,
        gross_exposure=account.long_market_value + account.short_liability,
        net_exposure=account.long_market_value - account.short_liability,
        margin_used=account.margin_used,
        buying_power=account.buying_power,
        realized_pnl=zero,
        unrealized_pnl=zero,
        gross_equity=account.net_equity,
        slippage_cost=zero,
        commission_cost=zero,
        other_fees=zero,
        borrow_cost=zero,
        financing_cost=zero,
        dividend_cash=zero,
        net_equity=account.net_equity,
        high_water_mark=account.high_water_mark,
        valid=True,
        invalid_reason="",
    )


def _position(ticker: str, *, pending: bool = False) -> dict[str, object]:
    row: dict[str, object] = {
        "ticker": ticker,
        "direction": "long",
        "marked_value": Decimal("500"),
        "sector": "Industrials",
        "strategy_tags": ("fixture_strategy",),
        "risk_tags": (f"event:{ticker.lower()}",),
    }
    if pending:
        row["eligible_session"] = date(2026, 8, 3)
    return row


def _run_staging_matrix(
    tmp_path,
    monkeypatch,
    histories,
    *,
    refetch_histories=None,
    candidate_tickers=("CANDIDATE",),
):
    config = deepcopy(DEFAULT_CONFIG)
    config["autoresearch"]["state_dir"] = str(tmp_path)
    config["autoresearch"]["paper_trade"]["portfolio_committee_enabled"] = False
    orchestrator = CohortOrchestrator(
        build_default_cohorts(config),
        config,
        generation_id="gen-volatility-fixture",
        generation_commit="fixture-commit",
        price_source=object(),
    )

    first_engine = orchestrator.cohorts[0]["engine"]

    def fetch_shared_data(_start: str, _end: str) -> dict:
        first_engine._price_cache.update(histories)
        return {"yfinance": {"prices": histories}}

    monkeypatch.setattr(first_engine, "_fetch_all_data", fetch_shared_data)
    missing_fetch_calls: list[tuple[tuple[str, ...], str, str]] = []

    def fetch_missing_prices(tickers: list[str], start: str, end: str) -> None:
        missing_fetch_calls.append((tuple(tickers), start, end))
        available = refetch_histories or histories
        first_engine._price_cache.update(
            {ticker: available[ticker] for ticker in tickers if ticker in available}
        )

    monkeypatch.setattr(first_engine, "_fetch_missing_prices", fetch_missing_prices)
    signals = [
        {
            "ticker": ticker,
            "direction": "long",
            "score": 2.0,
            "strategy": "fixture_strategy",
            "event_key": f"event-{ticker.lower()}",
            "source_event_keys": (f"source-{ticker.lower()}",),
            "strategy_tags": ("fixture_strategy",),
            "risk_tags": (f"event:{ticker.lower()}",),
            "metadata": {
                "event_key": f"event-{ticker.lower()}",
                "observed_at": "2026-07-31T19:30:00+00:00",
            },
        }
        for ticker in candidate_tickers
    ]
    monkeypatch.setattr(
        orchestrator,
        "_screen_for_horizon",
        lambda _data, _trading_date, _horizon: (signals, {}, []),
    )
    monkeypatch.setattr(
        orchestrator, "_persist_horizon_health", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        orchestrator,
        "_fetch_openbb_enrichment",
        lambda _signals: {
            "profiles": {
                ticker: {"sector": "Industrials"}
                for ticker in {*TICKERS, *candidate_tickers}
            }
        },
    )

    def reference_bars(_source, tickers, session, *_args, **_kwargs):
        return {
            ticker: MarketBar(
                ticker=ticker,
                session=session,
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100"),
                source="fixture",
                fetched_at=datetime(2026, 7, 31, 20, tzinfo=timezone.utc),
                adjusted=False,
            )
            for ticker in tickers
        }

    monkeypatch.setattr(
        "tradingagents.strategies.orchestration.session_executor.ensure_reference_bars",
        reference_bars,
    )

    for cohort in orchestrator.cohorts:
        ledger = cohort["ledger"]
        monkeypatch.setattr(ledger, "phase_completed", lambda *_args: True)
        monkeypatch.setattr(
            ledger,
            "read_snapshots",
            lambda _start, _end, *, epoch_id, valid_only=True, _ledger=ledger: [
                _snapshot(_ledger, epoch_id)
            ],
        )
        monkeypatch.setattr(
            cohort["executor"], "validate_bound_context", lambda *_args: {}
        )
        monkeypatch.setattr(
            cohort["executor"],
            "persisted_input_bundle",
            lambda _session: SessionInputBundle(
                session=SESSION,
                tickers=(),
                bars={},
                actions=(),
                benchmarks={},
            ),
        )
        monkeypatch.setattr(
            cohort["executor"],
            "validated_execution_reference_bars",
            lambda *_args: {},
        )
        monkeypatch.setattr(
            ledger,
            "verify_session_phase_chain",
            lambda session, _phases, _ledger=ledger: (
                _ledger.execution_governed_state_digest(session)
            ),
        )
        monkeypatch.setattr(
            ledger,
            "policy_open_lot_projection",
            lambda _session: (_position("OPEN"),),
        )
        monkeypatch.setattr(
            ledger,
            "policy_pending_entry_projection",
            lambda: (_position("PENDING", pending=True),),
        )

    results = orchestrator.run_daily(SESSION.isoformat())
    return orchestrator, results, missing_fetch_calls


def test_all_16_cohorts_bind_the_same_shared_60_session_volatility(
    tmp_path, monkeypatch
) -> None:
    histories = {
        "OPEN": _history(0.021),
        "PENDING": _history(0.027),
        "CANDIDATE": _history(0.033),
    }
    expected = {
        ticker: annualized_volatility(frame, lookback_sessions=60, floor=0.15)
        for ticker, frame in histories.items()
    }
    orchestrator, results, missing_fetch_calls = _run_staging_matrix(
        tmp_path,
        monkeypatch,
        histories,
        candidate_tickers=("CANDIDATE", "PENDING"),
    )

    try:
        assert missing_fetch_calls == []
        assert len(results) == 16
        assert all(not result["error"] for result in results.values())
        for cohort in orchestrator.cohorts:
            binding = cohort["ledger"].read_policy_session_context(SESSION)
            assert binding is not None
            observed = binding["context"]["annualized_volatility"]
            cohort_name = cohort["config"].name
            assert set(observed) == set(TICKERS), cohort_name
            assert observed == pytest.approx(expected), cohort_name
    finally:
        for cohort in orchestrator.cohorts:
            cohort["ledger"].close()


def test_missing_required_measured_volatility_fails_closed(
    tmp_path, monkeypatch
) -> None:
    histories = {
        "OPEN": _history(0.021),
        "CANDIDATE": _history(0.033),
    }
    orchestrator, results, missing_fetch_calls = _run_staging_matrix(
        tmp_path,
        monkeypatch,
        histories,
        candidate_tickers=("CANDIDATE", "PENDING"),
    )

    try:
        assert len(missing_fetch_calls) == 1
        assert missing_fetch_calls[0][0] == ("PENDING",)
        assert len(results) == 16
        assert all(result["error"] for result in results.values())
        assert all(
            "volatility" in result["invalid_reason"].lower()
            and "PENDING" in result["invalid_reason"]
            for result in results.values()
        )
        assert all(
            result["candidate_volatility_quarantines"] == []
            for result in results.values()
        )
        assert all(
            cohort["ledger"].read_policy_session_context(SESSION) is None
            for cohort in orchestrator.cohorts
        )
    finally:
        for cohort in orchestrator.cohorts:
            cohort["ledger"].close()


def test_missing_candidate_volatility_is_quarantined_without_cascading(
    tmp_path, monkeypatch
) -> None:
    histories = {
        "OPEN": _history(0.021),
        "PENDING": _history(0.027),
        "CANDIDATE": _history(0.033),
    }
    orchestrator, results, missing_fetch_calls = _run_staging_matrix(
        tmp_path,
        monkeypatch,
        histories,
        candidate_tickers=("CANDIDATE", "SHORT"),
    )

    try:
        assert missing_fetch_calls[0][0] == ("SHORT",)
        assert len(results) == 16
        assert all(not result["error"] for result in results.values())
        assert all(result["degraded"] for result in results.values())
        assert all(result["execution_valid"] for result in results.values())
        assert all(not result["staging_valid"] for result in results.values())
        assert all(
            result["candidate_bar_quarantines"] == []
            and result["candidate_volatility_quarantines"] == ["SHORT"]
            and result["candidate_volatility_failure_map"]["SHORT"].startswith(
                "missing valid 60-session volatility evidence"
            )
            for result in results.values()
        )
        assert all(
            {signal["ticker"] for signal in result["signals"]} == {"CANDIDATE"}
            for result in results.values()
        )
        records = orchestrator._metric_store.read_candidate_volatility_quarantines(
            orchestrator._epoch_id, SESSION
        )
        assert len(records) == 1
        assert records[0].ticker == "SHORT"
        assert records[0].lookback_sessions == 60
        assert len(records[0].attempt_errors) == 2
        assert all("SHORT" in error for error in records[0].attempt_errors)
        assert records[0].signal_identities == (
            {"event_key": "event-short", "strategy": "fixture_strategy"},
        )
        regime = orchestrator.cohorts[0]["engine"].state.load_latest_regime()
        assert regime is not None
        assert regime["candidate_volatility_quarantines"] == ["SHORT"]
        assert regime["execution_valid"] is True
        assert regime["staging_valid"] is False

        calls_before_replay = list(missing_fetch_calls)
        replay = orchestrator.run_daily(SESSION.isoformat())
        assert missing_fetch_calls == calls_before_replay
        assert all(
            result["candidate_volatility_quarantines"] == ["SHORT"]
            for result in replay.values()
        )
    finally:
        for cohort in orchestrator.cohorts:
            cohort["ledger"].close()


def test_volatility_sequence_error_identifies_missing_session() -> None:
    history = _history(0.027)
    expected = _xnys_sessions(previous_session(SESSION), 61)
    missing = expected[-30]
    history = history.drop(pd.Timestamp(missing))

    with pytest.raises(ValueError) as raised:
        build_annualized_volatility_evidence(
            {"ANTA": history},
            ("ANTA",),
            lookback_sessions=60,
            floor=0.15,
            expected_sessions=expected,
        )

    assert "invalid volatility session sequence for ANTA" in str(raised.value)
    assert f"missing expected XNYS session(s): {missing.isoformat()}" in str(
        raised.value
    )


def test_shared_measured_volatility_rotates_policy_document_version() -> None:
    assert POLICY_DOCUMENT_VERSION == "execution-policy-v3"


def test_insufficient_cached_history_is_refetched_once_for_all_cohorts(
    tmp_path, monkeypatch
) -> None:
    full_pending_history = _history(0.027)
    histories = {
        "OPEN": _history(0.021),
        "PENDING": full_pending_history.tail(12),
        "CANDIDATE": _history(0.033),
    }
    expected = {
        "OPEN": annualized_volatility(
            histories["OPEN"], lookback_sessions=60, floor=0.15
        ),
        "PENDING": annualized_volatility(
            full_pending_history, lookback_sessions=60, floor=0.15
        ),
        "CANDIDATE": annualized_volatility(
            histories["CANDIDATE"], lookback_sessions=60, floor=0.15
        ),
    }

    orchestrator, results, missing_fetch_calls = _run_staging_matrix(
        tmp_path,
        monkeypatch,
        histories,
        refetch_histories={"PENDING": full_pending_history},
    )

    try:
        assert missing_fetch_calls == [
            (
                ("PENDING",),
                (SESSION - timedelta(days=120)).isoformat(),
                SESSION.isoformat(),
            )
        ]
        assert len(results) == 16
        assert all(not result["error"] for result in results.values())
        for cohort in orchestrator.cohorts:
            binding = cohort["ledger"].read_policy_session_context(SESSION)
            assert binding is not None
            assert binding["context"]["annualized_volatility"] == pytest.approx(
                expected
            )
    finally:
        for cohort in orchestrator.cohorts:
            cohort["ledger"].close()


@pytest.mark.parametrize(
    "invalid_kind",
    ("stale", "missing_middle", "duplicate", "out_of_order"),
)
def test_invalid_cached_session_sequence_is_refetched_once_for_all_cohorts(
    tmp_path, monkeypatch, invalid_kind
) -> None:
    full_pending_history = _history(0.027)
    histories = {
        "OPEN": _history(0.021),
        "PENDING": _invalid_pending_history(invalid_kind),
        "CANDIDATE": _history(0.033),
    }
    orchestrator, results, missing_fetch_calls = _run_staging_matrix(
        tmp_path,
        monkeypatch,
        histories,
        refetch_histories={"PENDING": full_pending_history},
    )

    try:
        assert len(missing_fetch_calls) == 1
        tickers, start, end = missing_fetch_calls[0]
        assert tickers == ("PENDING",)
        assert date.fromisoformat(start) <= _xnys_sessions(
            previous_session(SESSION), 61
        )[0]
        assert end == SESSION.isoformat()
        assert len(results) == 16
        assert all(not result["error"] for result in results.values())
        for cohort in orchestrator.cohorts:
            binding = cohort["ledger"].read_policy_session_context(SESSION)
            assert binding is not None
            observed = binding["context"]["annualized_volatility"]
            assert observed["PENDING"] == pytest.approx(
                annualized_volatility(
                    full_pending_history,
                    lookback_sessions=60,
                    floor=0.15,
                )
            )
    finally:
        for cohort in orchestrator.cohorts:
            cohort["ledger"].close()
