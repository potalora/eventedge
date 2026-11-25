"""Offline end-to-end containment at the shared DAILY staging boundary."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
from tradingagents.strategies.execution.models import MarketBar
from tradingagents.strategies.execution.price_source import CandidateBarAttempt, CandidateBarResolution

import pytest

from test_shared_policy_volatility_evidence import (
    SESSION, _history, _invalid_pending_history, _run_staging_matrix,
)


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    import socket
    def blocked(*_args, **_kwargs):
        raise AssertionError("containment tests must remain offline")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)


@pytest.mark.parametrize("miss", ["missing", "stale", "gap", "bad_close", "provider_error"])
def test_isolated_disposable_history_miss_does_not_hold_16_books(tmp_path, monkeypatch, miss):
    bad = _history(.031)
    if miss in {"missing", "provider_error"}:
        bad = bad.iloc[:0]
    elif miss == "stale":
        bad = _invalid_pending_history("stale")
    elif miss == "gap":
        bad = _invalid_pending_history("missing_middle")
    else:
        bad.iloc[-5, 0] = 0
    staged = {}
    orchestrator, results, calls = _run_staging_matrix(
        tmp_path, monkeypatch,
        {"OPEN": _history(.021), "PENDING": _history(.027), "NCL": _history(.033), "UI": bad},
        signal_tickers=("NCL", "UI"), stage_observations=staged,
        fetch_error=SourceFetchError("fixture symbol failure", reason_code="invalid_response",
                                     failed_operations={"UI": "invalid_response"})
        if miss == "provider_error" else None,
    )
    try:
        issues = orchestrator._metric_store.read_candidate_input_issues(orchestrator._epoch_id, SESSION)
        assert len(issues) == 1 and issues[0].ticker == "UI"
        assert issues[0].dependency_kind == "volatility_history"
        assert issues[0].reason_code != "insufficient_history"
        assert [call[0] for call in calls] == [("UI",)]
        assert len(results) == len(staged) == 16
        assert all(not row["error"] and row["execution_valid"] and row["staging_valid"]
                   and row["degraded"] and row["candidate_input_issues"] == [issues[0].reference()]
                   for row in results.values())
        assert all(row == {"signals": ("NCL",), "reference_bars": ("NCL",)} for row in staged.values())
        for cohort in orchestrator.cohorts:
            ledger = cohort["ledger"]
            context = ledger.read_policy_session_context(SESSION)
            assert set(context["context"]["annualized_volatility"]) == {"OPEN", "PENDING", "NCL"}
            assert not any(signal.ticker == "UI" for signal in ledger.read_signals(SESSION, SESSION, epoch_id=orchestrator._epoch_id))
            assert ledger._connection.execute(
                "SELECT COUNT(*) FROM order_intents i JOIN intent_signals l ON l.intent_id=i.intent_id "
                "JOIN signals s ON s.signal_id=l.signal_id WHERE s.ticker='UI'"
            ).fetchone()[0] == 0
            assert ledger._connection.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0
        before = list(calls)
        replay = orchestrator.run_daily(SESSION.isoformat())
        assert calls == before
        assert all(not row["error"] and row["staging_valid"] for row in replay.values())
    finally:
        for cohort in orchestrator.cohorts:
            cohort["ledger"].close()


@pytest.mark.parametrize("leak", ["signal", "governed_scope", "provider_wide"])
def test_unproven_exclusion_fails_before_any_book_is_staged(tmp_path, monkeypatch, leak):
    from tradingagents.strategies.orchestration import daily_pipeline
    original = daily_pipeline._finish_volatility_validation
    def damage_exclusion(state):
        if leak == "signal":
            # Simulate a regression in filtering, not a provider miss.
            state.volatility_quarantines.clear()
            original(state)
            state.volatility_quarantines.add("UI")
        else:
            original(state)
            if leak == "provider_wide":
                state.shared_data["yfinance"]["error"] = "provider-wide outage"
            else:
                state.cohort_scopes["protected"] = ("UI",)
    monkeypatch.setattr(daily_pipeline, "_finish_volatility_validation", damage_exclusion)
    staged = {}
    orchestrator, results, _ = _run_staging_matrix(
        tmp_path, monkeypatch,
        {"OPEN": _history(.021), "PENDING": _history(.027), "NCL": _history(.033), "UI": _history(.031).iloc[:0]},
        signal_tickers=("NCL", "UI"), stage_observations=staged,
    )
    try:
        assert not staged
        assert all(row["error"] and not row["staging_valid"] for row in results.values())
    finally:
        for cohort in orchestrator.cohorts:
            cohort["ledger"].close()


@pytest.mark.parametrize("error", ["missing daily bar", "stale daily bar"])
@pytest.mark.parametrize("peer_scope", ["batch", "governed"])
def test_isolated_reference_miss_is_recorded_excluded_and_staging_valid(tmp_path, monkeypatch, error, peer_scope):
    if peer_scope == "governed":
        from tradingagents.strategies.orchestration import daily_pipeline
        original = daily_pipeline.run_candidate_reference_validation
        def fixed_clock(state):
            state.processed_at = datetime(2026, 7, 31, 21, tzinfo=timezone.utc)
            return original(state)
        monkeypatch.setattr(daily_pipeline, "run_candidate_reference_validation", fixed_clock)
    calls = []
    def resolve(tickers, session, processed_at, max_age):
        calls.append(tuple(tickers))
        now = processed_at
        bars, attempts = {}, []
        for ticker in tickers:
            prices = (None,) * 4 if ticker == "UI" else tuple(map(Decimal, ("100", "101", "99", "100")))
            attempts.append(CandidateBarAttempt(ticker, session, 1, "fixture", now, *prices,
                                                error if ticker == "UI" else None))
            if ticker != "UI":
                bars[(ticker, session)] = MarketBar(ticker, session, *prices, "fixture", now, False)
        return CandidateBarResolution(bars, tuple(attempts), frozenset(), frozenset({"UI"}),
                                      local_failures={"UI": "missing_data" if error.startswith("missing") else "stale_data"})
    staged = {}
    orchestrator, results, _ = _run_staging_matrix(
        tmp_path, monkeypatch,
        {"OPEN": _history(.021), "PENDING": _history(.027), "NCL": _history(.033), "UI": _history(.031)},
        signal_tickers=("NCL", "UI"), stage_observations=staged,
        price_source=SimpleNamespace(resolve_candidate_daily_bars=resolve),
        governed_reference_tickers=("NCL",) if peer_scope == "governed" else (),
    )
    try:
        issues = orchestrator._metric_store.read_candidate_input_issues(orchestrator._epoch_id, SESSION)
        assert len(issues) == 1 and issues[0].dependency_kind == "reference_bar"
        assert len(results) == len(staged) == 16
        assert all(not row["error"] and row["execution_valid"] and row["staging_valid"]
                   and row["degraded"] and row["candidate_bar_quarantines"] == ["UI"]
                   and row["candidate_input_issues"] == [issues[0].reference()] for row in results.values())
        assert all(row == {"signals": ("NCL",), "reference_bars": ("NCL",)} for row in staged.values())
        from copy import deepcopy
        from tradingagents.strategies.orchestration.generation_manager import _valid_daily_cohort_results

        assert _valid_daily_cohort_results(results, SESSION.isoformat())
        for digest in (None, "not-a-digest"):
            damaged = deepcopy(results)
            acceptance = next(iter(damaged.values()))["candidate_input_acceptance"]
            if digest is None:
                acceptance.pop("reference_health_sha256")
            else:
                acceptance["reference_health_sha256"] = digest
            assert not _valid_daily_cohort_results(damaged, SESSION.isoformat())
        if peer_scope == "governed":
            assert calls == [("UI",)]  # No extra request for the healthy governed peer.
        conn = orchestrator.cohorts[0]['ledger']._connection
        assert conn.execute("SELECT COUNT(*) FROM schema_metadata WHERE metadata_key LIKE 'candidate_containment:v1:%'").fetchone()[0] == 16
        for cohort in orchestrator.cohorts:
            ledger = cohort["ledger"]
            assert not any(s.ticker == "UI" for s in ledger.read_signals(SESSION, SESSION, epoch_id=orchestrator._epoch_id))
            assert ledger._connection.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0
            assert ledger._connection.execute(
                "SELECT COUNT(*) FROM order_intents i JOIN intent_signals l ON l.intent_id=i.intent_id "
                "JOIN signals s ON s.signal_id=l.signal_id WHERE s.ticker='UI'"
            ).fetchone()[0] == 0
        before = list(calls)
        replay = orchestrator.run_daily(SESSION.isoformat())
        assert calls == before
        assert all(not row["error"] and row["staging_valid"] for row in replay.values())
        conn.execute("DELETE FROM schema_metadata WHERE metadata_key LIKE 'candidate_containment:v1:%'")
        conn.commit()
        unproven = orchestrator.run_daily(SESSION.isoformat())
        assert calls == before
        assert all(not row["staging_valid"] for row in unproven.values())
    finally:
        for cohort in orchestrator.cohorts:
            cohort["ledger"].close()
