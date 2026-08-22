from __future__ import annotations

import json
import sqlite3
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
    tmp_path,
    monkeypatch,
    histories,
    *,
    refetch_histories=None,
    signal_tickers=("CANDIDATE",),
    open_tickers=("OPEN",),
    pending_tickers=("PENDING",),
    governed_reference_tickers=(),
    stage_observations=None,
    fetch_error: Exception | None = None,
    mutate_before_fetch_error: bool = False,
    fail_stage_once_name: str | None = None,
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
    if fail_stage_once_name == "__first__":
        fail_stage_once_name = orchestrator.cohorts[0]["config"].name

    first_engine = orchestrator.cohorts[0]["engine"]

    def fetch_shared_data(_start: str, _end: str) -> dict:
        first_engine._price_cache.update(histories)
        return {"yfinance": {"prices": histories}}

    monkeypatch.setattr(first_engine, "_fetch_all_data", fetch_shared_data)
    missing_fetch_calls: list[tuple[tuple[str, ...], str, str]] = []

    def fetch_missing_prices(tickers: list[str], start: str, end: str) -> None:
        missing_fetch_calls.append((tuple(tickers), start, end))
        if fetch_error is not None and not mutate_before_fetch_error:
            raise fetch_error
        available = refetch_histories or histories
        first_engine._price_cache.update(
            {ticker: available[ticker] for ticker in tickers if ticker in available}
        )
        if fetch_error is not None:
            raise fetch_error

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
        for ticker in signal_tickers
    ]
    monkeypatch.setattr(
        orchestrator,
        "_screen_for_horizon",
        lambda _data, _trading_date, _horizon: (deepcopy(signals), {}, []),
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
                for ticker in set(histories)
                | set(signal_tickers)
                | set(open_tickers)
                | set(pending_tickers)
                | set(governed_reference_tickers)
            }
        },
    )

    def reference_bar(ticker: str) -> MarketBar:
        return MarketBar(
            ticker=ticker,
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
        lambda _source, tickers, *_args, **_kwargs: {
            ticker: reference_bar(ticker) for ticker in tickers
        },
    )

    for cohort in orchestrator.cohorts:
        ledger = cohort["ledger"]
        name = cohort["config"].name
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
            lambda *_args: {
                ticker: reference_bar(ticker)
                for ticker in governed_reference_tickers
            },
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
            lambda _session: tuple(_position(ticker) for ticker in open_tickers),
        )
        monkeypatch.setattr(
            ledger,
            "policy_pending_entry_projection",
            lambda: tuple(
                _position(ticker, pending=True) for ticker in pending_tickers
            ),
        )
        if stage_observations is not None or name == fail_stage_once_name:
            original_stage = cohort["engine"].screen_and_stage

            def capture_stage(
                *args, _original=original_stage, _name=name, _failed=[False], **kwargs
            ):
                if stage_observations is not None:
                    stage_observations[_name] = {
                        "signals": tuple(
                            signal["ticker"] for signal in kwargs["shared_signals"]
                        ),
                        "reference_bars": tuple(
                            sorted(kwargs["data"]["_execution_reference_bars"])
                        ),
                    }
                if _name == fail_stage_once_name and not _failed[0]:
                    _failed[0] = True
                    raise RuntimeError("fixture staging interruption")
                return _original(*args, **kwargs)

            monkeypatch.setattr(cohort["engine"], "screen_and_stage", capture_stage)

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
        assert orchestrator.cohorts[0]["state"].load_latest_regime() is None
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


def test_candidate_only_invalid_volatility_is_persisted_filtered_and_not_fabricated(
    tmp_path, monkeypatch
) -> None:
    duplicate = _invalid_pending_history("duplicate")
    histories = {
        "OPEN": _history(0.021),
        "PENDING": _history(0.027),
        "NCL": _history(0.033),
        "UI": pd.DataFrame({"Close": []}, index=pd.DatetimeIndex([])),
        "ZKH": duplicate,
    }
    staged: dict[str, dict[str, tuple[str, ...]]] = {}
    orchestrator, results, missing_fetch_calls = _run_staging_matrix(
        tmp_path,
        monkeypatch,
        histories,
        signal_tickers=("NCL", "UI", "ZKH"),
        stage_observations=staged,
    )

    try:
        assert [call[0] for call in missing_fetch_calls] == [("UI",), ("ZKH",)]
        issues = orchestrator._metric_store.read_candidate_input_issues(
            orchestrator._epoch_id, SESSION
        )
        assert [
            (issue.ticker, issue.dependency_kind, issue.reason_code)
            for issue in issues
        ] == [
            ("UI", "volatility_history", "missing_data"),
            ("ZKH", "volatility_history", "invalid_data"),
        ]
        expected_sessions = _xnys_sessions(previous_session(SESSION), 61)
        assert all(issue.expected_sessions == expected_sessions for issue in issues)
        assert issues[0].observed_sessions == ()
        assert issues[1].observed_sessions == tuple(
            value.date() for value in duplicate.index
        )
        assert all(issue.retryable is False for issue in issues)
        assert all(issue.affected_cohorts for issue in issues)
        assert all(
            result["error"] is False
            and result["degraded"] is True
            and result["execution_valid"] is True
            and result["staging_valid"] is False
            and result["candidate_bar_quarantines"] == []
            and result["candidate_input_issues"]
            for result in results.values()
        )
        assert set(staged) == set(results)
        assert all(
            observation == {
                "signals": ("NCL",),
                "reference_bars": ("NCL",),
            }
            for observation in staged.values()
        )
        regime = orchestrator.cohorts[0]["state"].load_latest_regime()
        assert regime["execution_valid"] is True
        assert regime["staging_valid"] is False
        assert regime["candidate_bar_quarantines"] == []
        assert "candidate_input_issues" not in regime
        for cohort in orchestrator.cohorts:
            ledger = cohort["ledger"]
            binding = ledger.read_policy_session_context(SESSION)
            assert binding is not None
            assert set(binding["context"]["annualized_volatility"]) == {
                "OPEN",
                "PENDING",
                "NCL",
            }
            assert not {
                decision["ticker"]
                for decision in ledger.read_policy_candidate_decisions(
                    SESSION,
                    SESSION,
                    epoch_id=orchestrator._epoch_id,
                )
            } & {"UI", "ZKH"}
            assert not {
                signal.ticker
                for signal in ledger.read_signals(
                    SESSION,
                    SESSION,
                    epoch_id=orchestrator._epoch_id,
                )
            } & {"UI", "ZKH"}
            assert ledger._connection.execute(
                "SELECT COUNT(*) FROM marks WHERE ticker IN ('UI', 'ZKH') "
                "AND session = ?",
                (SESSION.isoformat(),),
            ).fetchone()[0] == 0
            assert ledger._connection.execute(
                """
                SELECT COUNT(*)
                FROM fills f
                JOIN intent_signals link ON link.intent_id = f.intent_id
                JOIN signals signal ON signal.signal_id = link.signal_id
                WHERE signal.ticker IN ('UI', 'ZKH') AND f.session = ?
                """,
                (SESSION.isoformat(),),
            ).fetchone()[0] == 0
        assert not {
            outcome.ticker
            for outcome in orchestrator._metric_store.read_outcomes(
                orchestrator._epoch_id
            )
            if outcome.exit_session == SESSION
        } & {"UI", "ZKH"}
    finally:
        for cohort in orchestrator.cohorts:
            cohort["ledger"].close()


@pytest.mark.parametrize("governed_kind", ("open", "pending", "p0"))
def test_candidate_governed_overlap_with_invalid_volatility_remains_strict(
    tmp_path, monkeypatch, governed_kind
) -> None:
    open_tickers = ("UI",) if governed_kind == "open" else ("OPEN",)
    pending_tickers = ("UI",) if governed_kind == "pending" else ("PENDING",)
    governed_reference_tickers = ("UI",) if governed_kind == "p0" else ()
    histories = {
        "OPEN": _history(0.021),
        "PENDING": _history(0.027),
        "UI": pd.DataFrame({"Close": []}, index=pd.DatetimeIndex([])),
    }
    orchestrator, results, missing_fetch_calls = _run_staging_matrix(
        tmp_path,
        monkeypatch,
        histories,
        signal_tickers=("UI",),
        open_tickers=open_tickers,
        pending_tickers=pending_tickers,
        governed_reference_tickers=governed_reference_tickers,
    )

    try:
        assert [call[0] for call in missing_fetch_calls] == [("UI",)]
        assert all(result["error"] is True for result in results.values())
        assert all(
            "volatility" in result["invalid_reason"].lower()
            for result in results.values()
        )
        assert orchestrator._metric_store.read_candidate_input_issues(
            orchestrator._epoch_id, SESSION
        ) == ()
        assert all(
            cohort["ledger"].read_policy_session_context(SESSION) is None
            for cohort in orchestrator.cohorts
        )
    finally:
        for cohort in orchestrator.cohorts:
            cohort["ledger"].close()


def test_candidate_volatility_provider_exception_is_sanitized_into_issue(
    tmp_path, monkeypatch, caplog
) -> None:
    secret = "credential=SECRET-VOLATILITY-TOKEN"
    histories = {
        "OPEN": _history(0.021),
        "PENDING": _history(0.027),
    }
    orchestrator, results, missing_fetch_calls = _run_staging_matrix(
        tmp_path,
        monkeypatch,
        histories,
        signal_tickers=("UI",),
        fetch_error=RuntimeError(secret),
    )

    try:
        assert [call[0] for call in missing_fetch_calls] == [("UI",)]
        issues = orchestrator._metric_store.read_candidate_input_issues(
            orchestrator._epoch_id, SESSION
        )
        assert [(issue.ticker, issue.reason_code) for issue in issues] == [
            ("UI", "provider_error")
        ]
        assert secret not in json.dumps(results, sort_keys=True, default=str)
        assert secret not in caplog.text
        assert secret not in "".join(issue.canonical_payload() for issue in issues)
    finally:
        for cohort in orchestrator.cohorts:
            cohort["ledger"].close()


def test_candidate_provider_error_quarantines_even_after_valid_cache_mutation(
    tmp_path, monkeypatch, caplog
) -> None:
    secret = "credential=SECRET-AFTER-CACHE-MUTATION"
    histories = {
        "OPEN": _history(0.021),
        "PENDING": _history(0.027),
    }
    staged: dict[str, dict[str, tuple[str, ...]]] = {}
    orchestrator, results, missing_fetch_calls = _run_staging_matrix(
        tmp_path,
        monkeypatch,
        histories,
        refetch_histories={"UI": _history(0.031)},
        signal_tickers=("UI",),
        stage_observations=staged,
        fetch_error=RuntimeError(secret),
        mutate_before_fetch_error=True,
    )

    try:
        assert [call[0] for call in missing_fetch_calls] == [("UI",)]
        issues = orchestrator._metric_store.read_candidate_input_issues(
            orchestrator._epoch_id, SESSION
        )
        assert [(issue.ticker, issue.reason_code) for issue in issues] == [
            ("UI", "provider_error")
        ]
        assert all(
            observation == {"signals": (), "reference_bars": ()}
            for observation in staged.values()
        )
        assert all(
            result["error"] is False
            and result["degraded"] is True
            and result["staging_valid"] is False
            and result["candidate_input_issues"] == [issues[0].reference()]
            for result in results.values()
        )
        assert secret not in json.dumps(results, sort_keys=True, default=str)
        assert secret not in caplog.text
        assert secret not in "".join(issue.canonical_payload() for issue in issues)
    finally:
        for cohort in orchestrator.cohorts:
            cohort["ledger"].close()


def test_governed_provider_error_fails_closed_after_valid_cache_mutation(
    tmp_path, monkeypatch, caplog
) -> None:
    secret = "credential=SECRET-GOVERNED-AFTER-MUTATION"
    full_open_history = _history(0.021)
    histories = {
        "OPEN": full_open_history.tail(12),
        "PENDING": _history(0.027),
        "CANDIDATE": _history(0.033),
    }
    staged: dict[str, dict[str, tuple[str, ...]]] = {}
    orchestrator, results, missing_fetch_calls = _run_staging_matrix(
        tmp_path,
        monkeypatch,
        histories,
        refetch_histories={"OPEN": full_open_history},
        stage_observations=staged,
        fetch_error=RuntimeError(secret),
        mutate_before_fetch_error=True,
    )

    try:
        assert [call[0] for call in missing_fetch_calls] == [("OPEN",)]
        assert staged == {}
        assert all(
            result["error"] is True
            and result["staging_valid"] is False
            and result["invalid_reason"]
            == "shared staging volatility evidence failed: provider_error"
            for result in results.values()
        )
        assert orchestrator._metric_store.read_candidate_input_issues(
            orchestrator._epoch_id, SESSION
        ) == ()
        for cohort in orchestrator.cohorts:
            ledger = cohort["ledger"]
            assert ledger.read_policy_session_context(SESSION) is None
            assert ledger.read_policy_candidate_decisions(
                SESSION,
                SESSION,
                epoch_id=orchestrator._epoch_id,
            ) == ()
            assert ledger._connection.execute(
                "SELECT COUNT(*) FROM order_intents"
            ).fetchone()[0] == 0
            assert all(
                ledger._connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE session = ?",
                    (SESSION.isoformat(),),
                ).fetchone()[0]
                == 0
                for table in ("fills", "marks")
            )
        assert not any(
            outcome.exit_session == SESSION
            for outcome in orchestrator._metric_store.read_outcomes(
                orchestrator._epoch_id
            )
        )
        assert orchestrator.cohorts[0]["state"].load_latest_regime() is None
        assert secret not in json.dumps(results, sort_keys=True, default=str)
        assert secret not in caplog.text
    finally:
        for cohort in orchestrator.cohorts:
            cohort["ledger"].close()


def test_equal_volatility_issue_replay_filters_without_candidate_refetch(
    tmp_path, monkeypatch
) -> None:
    histories = {
        "OPEN": _history(0.021),
        "PENDING": _history(0.027),
        "NCL": _history(0.033),
        "UI": pd.DataFrame({"Close": []}, index=pd.DatetimeIndex([])),
    }
    orchestrator, first, missing_fetch_calls = _run_staging_matrix(
        tmp_path,
        monkeypatch,
        histories,
        signal_tickers=("NCL", "UI"),
        fail_stage_once_name="__first__",
    )

    try:
        issue = orchestrator._metric_store.load_candidate_input_issue(
            epoch_id=orchestrator._epoch_id,
            session=SESSION,
            dependency_kind="volatility_history",
            ticker="UI",
        )
        assert issue is not None
        assert sum(result["error"] is True for result in first.values()) == 1
        first_fetch_count = len(missing_fetch_calls)

        def unexpected_refetch(*_args, **_kwargs):
            raise AssertionError("candidate volatility refetched during equal replay")

        monkeypatch.setattr(
            orchestrator.cohorts[0]["engine"],
            "_fetch_missing_prices",
            unexpected_refetch,
        )
        replay = orchestrator.run_daily(SESSION.isoformat())

        assert len(missing_fetch_calls) == first_fetch_count
        assert all(result["error"] is False for result in replay.values())
        assert all(
            result["candidate_input_issues"] == [issue.reference()]
            for result in replay.values()
        )
    finally:
        for cohort in orchestrator.cohorts:
            cohort["ledger"].close()


def test_fresh_process_replays_durable_volatility_issue_without_candidate_cache(
    tmp_path, monkeypatch
) -> None:
    first_histories = {
        "OPEN": _history(0.021),
        "PENDING": _history(0.027),
        "UI": pd.DataFrame({"Close": []}, index=pd.DatetimeIndex([])),
    }
    first_orchestrator, first, first_fetch_calls = _run_staging_matrix(
        tmp_path,
        monkeypatch,
        first_histories,
        signal_tickers=("UI",),
        fail_stage_once_name="__first__",
    )
    second_orchestrator = None

    try:
        issue_reference = next(iter(first.values()))["candidate_input_issues"][0]
        assert [call[0] for call in first_fetch_calls] == [("UI",)]
        second_histories = {
            "OPEN": _history(0.021),
            "PENDING": _history(0.027),
        }
        second_orchestrator, replay, replay_fetch_calls = _run_staging_matrix(
            tmp_path,
            monkeypatch,
            second_histories,
            signal_tickers=("UI",),
        )

        assert replay_fetch_calls == []
        assert all(result["error"] is False for result in replay.values())
        assert all(
            result["candidate_input_issues"] == [issue_reference]
            for result in replay.values()
        )
    finally:
        for cohort in first_orchestrator.cohorts:
            cohort["ledger"].close()
        if second_orchestrator is not None:
            for cohort in second_orchestrator.cohorts:
                cohort["ledger"].close()


def test_candidate_volatility_retry_recovers_one_ticker_and_isolates_another(
    tmp_path, monkeypatch
) -> None:
    full_ui_history = _history(0.031)
    histories = {
        "OPEN": _history(0.021),
        "PENDING": _history(0.027),
        "NCL": _history(0.033),
        "UI": full_ui_history.tail(12),
        "ZKH": pd.DataFrame({"Close": []}, index=pd.DatetimeIndex([])),
    }
    staged: dict[str, dict[str, tuple[str, ...]]] = {}
    orchestrator, results, missing_fetch_calls = _run_staging_matrix(
        tmp_path,
        monkeypatch,
        histories,
        refetch_histories={"UI": full_ui_history},
        signal_tickers=("NCL", "UI", "ZKH"),
        stage_observations=staged,
    )

    try:
        assert [call[0] for call in missing_fetch_calls] == [("UI",), ("ZKH",)]
        issues = orchestrator._metric_store.read_candidate_input_issues(
            orchestrator._epoch_id, SESSION
        )
        assert [(issue.ticker, issue.reason_code) for issue in issues] == [
            ("ZKH", "missing_data")
        ]
        assert all(
            observation["signals"] == ("NCL", "UI")
            and observation["reference_bars"] == ("NCL", "UI")
            for observation in staged.values()
        )
        assert all(
            result["error"] is False
            and result["staging_valid"] is False
            and result["candidate_input_issues"] == [issues[0].reference()]
            for result in results.values()
        )
    finally:
        for cohort in orchestrator.cohorts:
            cohort["ledger"].close()


@pytest.mark.parametrize(
    ("invalid_kind", "reason_code"),
    (
        ("missing_middle", "missing_data"),
        ("stale", "stale_data"),
        ("duplicate", "invalid_data"),
        ("out_of_order", "invalid_data"),
    ),
)
def test_candidate_volatility_issue_preserves_exact_session_evidence(
    tmp_path, monkeypatch, invalid_kind, reason_code
) -> None:
    invalid = _invalid_pending_history(invalid_kind)
    histories = {
        "OPEN": _history(0.021),
        "PENDING": _history(0.027),
        "UI": invalid,
    }
    orchestrator, _results, missing_fetch_calls = _run_staging_matrix(
        tmp_path,
        monkeypatch,
        histories,
        signal_tickers=("UI",),
    )

    try:
        assert [call[0] for call in missing_fetch_calls] == [("UI",)]
        issue = orchestrator._metric_store.load_candidate_input_issue(
            epoch_id=orchestrator._epoch_id,
            session=SESSION,
            dependency_kind="volatility_history",
            ticker="UI",
        )
        assert issue is not None
        assert issue.reason_code == reason_code
        assert issue.expected_sessions == _xnys_sessions(
            previous_session(SESSION), 61
        )
        assert issue.observed_sessions == tuple(value.date() for value in invalid.index)
    finally:
        for cohort in orchestrator.cohorts:
            cohort["ledger"].close()


def test_invalid_candidate_values_produce_distinct_content_bound_issue_ids(
    tmp_path, monkeypatch
) -> None:
    issues = []
    orchestrators = []
    try:
        for label, invalid_value in (("zero", 0.0), ("negative", -999.0)):
            invalid = _history(0.031)
            invalid.iloc[-1, invalid.columns.get_loc("Close")] = invalid_value
            orchestrator, _results, _calls = _run_staging_matrix(
                tmp_path / label,
                monkeypatch,
                {
                    "OPEN": _history(0.021),
                    "PENDING": _history(0.027),
                    "UI": invalid,
                },
                signal_tickers=("UI",),
            )
            orchestrators.append(orchestrator)
            issue = orchestrator._metric_store.load_candidate_input_issue(
                epoch_id=orchestrator._epoch_id,
                session=SESSION,
                dependency_kind="volatility_history",
                ticker="UI",
            )
            assert issue is not None
            issues.append(issue)

        assert issues[0].returned_history_digest != issues[1].returned_history_digest
        assert issues[0].issue_id != issues[1].issue_id
    finally:
        for orchestrator in orchestrators:
            for cohort in orchestrator.cohorts:
                cohort["ledger"].close()


@pytest.mark.parametrize("tamper_kind", ("digest", "scope", "reason"))
def test_volatility_issue_tamper_fails_closed_without_candidate_refetch(
    tmp_path, monkeypatch, tamper_kind
) -> None:
    histories = {
        "OPEN": _history(0.021),
        "PENDING": _history(0.027),
        "UI": pd.DataFrame({"Close": []}, index=pd.DatetimeIndex([])),
    }
    orchestrator, _, missing_fetch_calls = _run_staging_matrix(
        tmp_path,
        monkeypatch,
        histories,
        signal_tickers=("UI",),
        fail_stage_once_name="__first__",
    )

    try:
        with sqlite3.connect(orchestrator._metric_store.path) as connection:
            row = connection.execute(
                "SELECT issue_id, payload_json FROM candidate_input_issues "
                "WHERE dependency_kind = 'volatility_history'"
            ).fetchone()
            payload = json.loads(row[1])
            if tamper_kind == "digest":
                payload["returned_history_digest"] = "sha256:" + "0" * 64
            elif tamper_kind == "scope":
                payload["affected_cohorts"] = payload["affected_cohorts"][:-1]
            else:
                payload["reason_code"] = "stale_data"
            connection.execute(
                "UPDATE candidate_input_issues SET payload_json = ? "
                "WHERE issue_id = ?",
                (json.dumps(payload, sort_keys=True, separators=(",", ":")), row[0]),
            )

        first_fetch_count = len(missing_fetch_calls)

        def unexpected_refetch(*_args, **_kwargs):
            raise AssertionError("candidate volatility refetched after issue tamper")

        monkeypatch.setattr(
            orchestrator.cohorts[0]["engine"],
            "_fetch_missing_prices",
            unexpected_refetch,
        )
        replay = orchestrator.run_daily(SESSION.isoformat())

        assert len(missing_fetch_calls) == first_fetch_count
        failed = [result for result in replay.values() if result["error"] is True]
        assert failed
        assert all(
            result["execution_valid"] is True
            and result["staging_valid"] is False
            and "candidate_input_issues" not in result
            and result["invalid_reason"]
            == "candidate volatility-history validation failed"
            for result in failed
        )
    finally:
        for cohort in orchestrator.cohorts:
            cohort["ledger"].close()


def test_completed_cohort_projection_governs_unfinished_candidate_volatility(
    tmp_path, monkeypatch
) -> None:
    histories = {
        "OPEN": _history(0.021),
        "PENDING": _history(0.027),
        "UI": _history(0.031),
    }
    orchestrator, first, missing_fetch_calls = _run_staging_matrix(
        tmp_path,
        monkeypatch,
        histories,
        signal_tickers=("UI",),
        fail_stage_once_name="__first__",
    )

    try:
        unfinished_name = next(
            name for name, result in first.items() if result["error"] is True
        )
        completed = next(
            cohort
            for cohort in orchestrator.cohorts
            if cohort["config"].name != unfinished_name
        )
        for cohort in orchestrator.cohorts:
            projection = ("UI",) if cohort is completed else ("OPEN",)
            monkeypatch.setattr(
                cohort["ledger"],
                "policy_open_lot_projection",
                lambda _session, _projection=projection: tuple(
                    _position(ticker) for ticker in _projection
                ),
            )
        before = {
            cohort["config"].name: tuple(
                cohort["ledger"]._connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
                for table in ("signals", "order_intents", "fills", "marks")
            )
            for cohort in orchestrator.cohorts
        }
        histories["UI"] = pd.DataFrame(
            {"Close": []}, index=pd.DatetimeIndex([])
        )
        first_fetch_count = len(missing_fetch_calls)
        replay = orchestrator.run_daily(SESSION.isoformat())

        assert [call[0] for call in missing_fetch_calls[first_fetch_count:]] == [
            ("UI",)
        ]
        assert replay[unfinished_name]["error"] is True
        assert "volatility" in replay[unfinished_name]["invalid_reason"].lower()
        assert orchestrator._metric_store.read_candidate_input_issues(
            orchestrator._epoch_id, SESSION
        ) == ()
        after = {
            cohort["config"].name: tuple(
                cohort["ledger"]._connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
                for table in ("signals", "order_intents", "fills", "marks")
            )
            for cohort in orchestrator.cohorts
        }
        assert after == before
    finally:
        for cohort in orchestrator.cohorts:
            cohort["ledger"].close()


def test_stored_candidate_volatility_issue_becoming_governed_fails_before_fetch(
    tmp_path, monkeypatch
) -> None:
    histories = {
        "OPEN": _history(0.021),
        "PENDING": _history(0.027),
        "UI": pd.DataFrame({"Close": []}, index=pd.DatetimeIndex([])),
    }
    orchestrator, first, missing_fetch_calls = _run_staging_matrix(
        tmp_path,
        monkeypatch,
        histories,
        signal_tickers=("UI",),
        fail_stage_once_name="__first__",
    )

    try:
        issue_reference = next(iter(first.values()))["candidate_input_issues"][0]
        for cohort in orchestrator.cohorts:
            monkeypatch.setattr(
                cohort["ledger"],
                "policy_open_lot_projection",
                lambda _session: (_position("UI"),),
            )
        first_fetch_count = len(missing_fetch_calls)

        def unexpected_refetch(*_args, **_kwargs):
            raise AssertionError("scope conflict performed volatility provider I/O")

        monkeypatch.setattr(
            orchestrator.cohorts[0]["engine"],
            "_fetch_missing_prices",
            unexpected_refetch,
        )
        replay = orchestrator.run_daily(SESSION.isoformat())

        assert len(missing_fetch_calls) == first_fetch_count
        failed = [result for result in replay.values() if result["error"] is True]
        assert failed
        assert all(
            result["invalid_reason"]
            == "candidate volatility-history validation failed"
            and result["execution_valid"] is True
            and result["staging_valid"] is False
            and result["candidate_input_issues"] == [issue_reference]
            for result in failed
        )
    finally:
        for cohort in orchestrator.cohorts:
            cohort["ledger"].close()


def test_stored_candidate_issue_does_not_reclassify_governed_volatility_failure(
    tmp_path, monkeypatch
) -> None:
    histories = {
        "OPEN": _history(0.021),
        "PENDING": _history(0.027),
        "UI": pd.DataFrame({"Close": []}, index=pd.DatetimeIndex([])),
    }
    orchestrator, first, missing_fetch_calls = _run_staging_matrix(
        tmp_path,
        monkeypatch,
        histories,
        signal_tickers=("UI",),
        fail_stage_once_name="__first__",
    )

    try:
        issue_reference = next(iter(first.values()))["candidate_input_issues"][0]
        histories["OPEN"] = pd.DataFrame(
            {"Close": []}, index=pd.DatetimeIndex([])
        )
        first_fetch_count = len(missing_fetch_calls)
        replay = orchestrator.run_daily(SESSION.isoformat())

        assert [call[0] for call in missing_fetch_calls[first_fetch_count:]] == [
            ("OPEN",)
        ]
        failed = [result for result in replay.values() if result["error"] is True]
        assert failed
        assert all(
            result["invalid_reason"].startswith(
                "shared staging volatility evidence failed:"
            )
            and "OPEN" in result["invalid_reason"]
            and result["candidate_input_issues"] == [issue_reference]
            for result in failed
        )
    finally:
        for cohort in orchestrator.cohorts:
            cohort["ledger"].close()
