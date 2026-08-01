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
from tradingagents.strategies.orchestration.trading_calendar import previous_session
from tradingagents.strategies.trading.portfolio_policy import annualized_volatility


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
    tmp_path, monkeypatch, histories, *, refetch_histories=None
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
    signal = {
        "ticker": "CANDIDATE",
        "direction": "long",
        "score": 2.0,
        "strategy": "fixture_strategy",
        "event_key": "event-candidate",
        "source_event_keys": ("source-candidate",),
        "strategy_tags": ("fixture_strategy",),
        "risk_tags": ("event:candidate",),
        "metadata": {
            "event_key": "event-candidate",
            "observed_at": "2026-07-31T19:30:00+00:00",
        },
    }
    monkeypatch.setattr(
        orchestrator,
        "_screen_for_horizon",
        lambda _data, _trading_date, _horizon: ([signal], {}, []),
    )
    monkeypatch.setattr(
        orchestrator, "_persist_horizon_health", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        orchestrator,
        "_fetch_openbb_enrichment",
        lambda _signals: {
            "profiles": {ticker: {"sector": "Industrials"} for ticker in TICKERS}
        },
    )
    reference_bar = MarketBar(
        ticker="CANDIDATE",
        session=SESSION,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        source="fixture",
        fetched_at=datetime(2026, 7, 31, 20, tzinfo=timezone.utc),
        adjusted=False,
    )
    monkeypatch.setattr(
        "tradingagents.strategies.orchestration.session_executor.ensure_reference_bars",
        lambda *_args, **_kwargs: {"CANDIDATE": reference_bar},
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
        tmp_path, monkeypatch, histories
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
        tmp_path, monkeypatch, histories
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
            cohort["ledger"].read_policy_session_context(SESSION) is None
            for cohort in orchestrator.cohorts
        )
    finally:
        for cohort in orchestrator.cohorts:
            cohort["ledger"].close()


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
