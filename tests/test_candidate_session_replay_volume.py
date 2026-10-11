"""Execution replay requires complete exact-session evidence, not report pages."""
from copy import deepcopy
from contextlib import contextmanager
from datetime import date, datetime, timezone
import json
import sqlite3
from types import SimpleNamespace

import pytest

from test_source_reliability_pipeline import pipeline, SESSION, InterruptedWorker
from tradingagents.strategies.execution.price_source import (
    CandidateBarAttempt, CandidateBarResolution,
)
from tradingagents.strategies.orchestration import daily_pipeline
from tradingagents.strategies.metrics import store as store_module
from tradingagents.strategies.metrics.store import MetricStore


def _canonical_rows(store):
    with store._connect() as conn:
        return {table: conn.execute(
            f"SELECT * FROM {table} ORDER BY 1"
        ).fetchall() for table in (
            "candidate_bar_recoveries", "candidate_input_issues",
            "candidate_signal_identity_bindings",
        )}


def test_native_volume_interrupted_then_completed_replay_has_all_evidence(pipeline, monkeypatch):
    fixture, owner, config, _ = pipeline
    # This test exercises execution/durable replay, not SDK selection behavior.
    for cohort in owner.cohorts:
        cohort["engine"].config["autoresearch"]["paper_trade"]["portfolio_committee_enabled"] = False
    monkeypatch.setattr(owner, "_fetch_openbb_enrichment", lambda signals: {})
    native_screen = owner._screen_for_horizon
    retained = {}
    def screen(data, trading_date, horizon):
        if horizon not in retained:
            signals, regime, health = native_screen(data, trading_date, horizon)
            signals.extend({"ticker": f"ZZ{index:04d}", "direction": "neutral",
                "score": 0, "strategy": "filing_analysis", "journal_only": True,
                "event_key": f"volume-{index}", "metadata": {
                    "accession_number": f"offline-volume-{index}",
                    "observed_at": "2026-10-02T17:00:00+00:00"}}
                for index in range(1001))
            retained[horizon] = (signals, regime, health)
        return deepcopy(retained[horizon])
    monkeypatch.setattr(owner, "_screen_for_horizon", screen)
    native_resolution = owner._price_source.resolve_candidate_daily_bars
    def resolution(tickers, session, now, max_age):
        synthetic = {ticker for ticker in tickers if ticker.startswith("ZZ")}
        actual = native_resolution([t for t in tickers if t not in synthetic], session, now, max_age)
        attempts = tuple(CandidateBarAttempt(ticker=ticker, session=session,
            attempt=1, source="offline-volume", fetched_at=now,
            open=None, high=None, low=None, close=None,
            validation_error="missing offline reference") for ticker in sorted(synthetic))
        return CandidateBarResolution(actual.bars, actual.attempts + attempts,
            actual.recovered_tickers, actual.quarantined_tickers | frozenset(synthetic))
    monkeypatch.setattr(owner._price_source, "resolve_candidate_daily_bars", resolution)
    native_stage = daily_pipeline.stage_daily_results
    def interrupt(state):
        raise InterruptedWorker("interrupted after complete accepted input phases")
    monkeypatch.setattr(daily_pipeline, "stage_daily_results", interrupt)
    with pytest.raises(InterruptedWorker):
        owner.run_daily(str(SESSION))
    accepted = _canonical_rows(owner._metric_store)
    assert len(accepted["candidate_bar_recoveries"]) > 1000
    assert len(accepted["candidate_input_issues"]) == 1001
    assert len(owner._metric_store.read_candidate_bar_recoveries(owner._epoch_id, SESSION)) == 1000
    assert len(owner._metric_store.read_candidate_input_issues(owner._epoch_id, SESSION)) == 1000
    monkeypatch.setattr(daily_pipeline, "stage_daily_results", native_stage)
    fixture.block_all = True
    calls, models = fixture.calls.copy(), fixture.model_calls.copy()
    forbidden_calls = []
    def forbidden(*args, **kwargs):
        forbidden_calls.append(True)
        raise InterruptedWorker("replay must not acquire any accepted input")
    monkeypatch.setattr(owner._price_source, "resolve_candidate_daily_bars", forbidden)
    monkeypatch.setattr(owner, "_fetch_openbb_enrichment", lambda signals: {})
    monkeypatch.setattr(owner.cohorts[0]["engine"], "_fetch_all_data", forbidden)
    monkeypatch.setattr(owner.cohorts[0]["engine"], "_fetch_missing_prices", forbidden)
    resumed = owner.run_daily(str(SESSION))
    assert len(resumed) == 16
    assert all(not row.get("error") for row in resumed.values()), [
        row.get("invalid_reason") for row in resumed.values()]
    assert all(row["execution_valid"] and not row["staging_valid"] for row in resumed.values())
    assert all(len(row["candidate_input_issues"]) == 1001 for row in resumed.values())
    assert all(len(row["candidate_bar_quarantines"]) == 1001 for row in resumed.values())
    repeated = owner.run_daily(str(SESSION))
    assert all(row["replayed"] and not row.get("error") for row in repeated.values())
    assert all(len(row["candidate_input_issues"]) == 1001 for row in repeated.values())
    assert all(len(row["candidate_bar_quarantines"]) == 1001 for row in repeated.values())
    assert fixture.calls == calls and fixture.model_calls == models
    assert not fixture.blocked_calls and not forbidden_calls
    assert _canonical_rows(owner._metric_store) == accepted
    for cohort in owner.cohorts:
        with cohort["ledger"]._connection as conn:
            assert conn.execute("SELECT COUNT(*) FROM staging_runs").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM committee_decisions").fetchone()[0] == 1


def _small_store(tmp_path):
    from tradingagents.strategies.metrics.models import CandidateBarRecoveryRecord
    from tradingagents.strategies.orchestration.daily_pipeline import _candidate_reference_issue
    store = MetricStore(tmp_path / "metrics.sqlite3")
    attempt = dict(ticker="ZZ0000", session=SESSION, attempt=1, source="offline",
        fetched_at=datetime(2026, 10, 2, 20, tzinfo=timezone.utc),
        open=None, high=None, low=None, close=None, validation_error="missing bar")
    recovery = CandidateBarRecoveryRecord(recovery_id="recovery-1", epoch_id="epoch",
        session=SESSION, ticker="ZZ0000", outcome="quarantined", attempts=(attempt,),
        signal_identities=({"event_key": "event", "strategy": "filing_analysis"},))
    store.save_candidate_bar_recovery(recovery)
    issue = _candidate_reference_issue(recovery, signal_identity_scope=({
        "horizon": "1y", "ticker": recovery.ticker,
        "event_key": "event", "strategy": "filing_analysis"},),
        cohorts=[{"config": SimpleNamespace(name="book", horizon="1y")}])
    store.save_candidate_input_issue(issue)
    return store


@pytest.mark.parametrize("kind", ["recoveries", "issues"])
def test_complete_session_read_is_scoped_and_preserves_listing_api(tmp_path, kind):
    store = _small_store(tmp_path)
    method = (store.read_session_candidate_bar_recoveries if kind == "recoveries"
              else store.read_session_candidate_input_issues)
    before = _canonical_rows(store)
    assert len(method("epoch", SESSION)) == 1
    assert method("different-epoch", SESSION) == ()
    assert method("epoch", date(2026, 10, 5)) == ()
    for bad_epoch, bad_session in [("", SESSION), ("epoch", None), ("epoch", str(SESSION)),
                                    ("epoch", datetime(2026, 10, 2))]:
        with pytest.raises(ValueError):
            method(bad_epoch, bad_session)
    assert _canonical_rows(store) == before


@pytest.mark.parametrize("kind", ["recoveries", "issues"])
@pytest.mark.parametrize("guard", ["count", "bytes", "record_bytes"])
def test_complete_session_read_bounds_before_parsing(tmp_path, monkeypatch, kind, guard):
    store = _small_store(tmp_path)
    method = (store.read_session_candidate_bar_recoveries if kind == "recoveries"
              else store.read_session_candidate_input_issues)
    before = _canonical_rows(store)
    constant = {"count": "_MAX_CANDIDATE_SIGNAL_IDENTITIES",
                "bytes": "_MAX_CANDIDATE_SIGNAL_BINDING_PAYLOAD_BYTES",
                "record_bytes": "_MAX_CANDIDATE_SESSION_RECORD_BYTES"}[guard]
    monkeypatch.setattr(store_module, constant, 0)
    def forbidden(*args, **kwargs):
        raise AssertionError("oversized collection must be rejected before parsing")
    monkeypatch.setattr(store, "_candidate_bar_recovery", forbidden)
    monkeypatch.setattr(store, "_candidate_input_issue", forbidden)
    with pytest.raises(ValueError, match="bound"):
        method("epoch", SESSION)
    assert _canonical_rows(store) == before


@pytest.mark.parametrize("kind", ["recoveries", "issues"])
@pytest.mark.parametrize("damage", ["scope", "identifier", "schema", "canonical"])
def test_complete_session_read_rejects_tampering_without_repair(tmp_path, kind, damage):
    store = _small_store(tmp_path)
    table = "candidate_bar_recoveries" if kind == "recoveries" else "candidate_input_issues"
    method = (store.read_session_candidate_bar_recoveries if kind == "recoveries"
              else store.read_session_candidate_input_issues)
    with store._connect() as conn:
        payload = json.loads(conn.execute(f"SELECT payload_json FROM {table}").fetchone()[0])
        if damage == "scope": payload["epoch_id"] = "different-epoch"
        elif damage == "identifier": payload["recovery_id" if kind == "recoveries" else "issue_id"] = "different-id"
        elif damage == "schema": payload["unexpected"] = True
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        if damage == "canonical": encoded += " "
        conn.execute(f"UPDATE {table} SET payload_json=?", (encoded,))
    before = _canonical_rows(store)
    with pytest.raises(ValueError):
        method("epoch", SESSION)
    assert _canonical_rows(store) == before


def test_complete_session_recovery_read_rejects_duplicate_ticker(tmp_path):
    store = _small_store(tmp_path)
    with store._connect() as conn:
        payload = json.loads(conn.execute("SELECT payload_json FROM candidate_bar_recoveries").fetchone()[0])
        payload["recovery_id"] = "recovery-2"
        conn.execute("INSERT INTO candidate_bar_recoveries VALUES (?,?,?,?)",
            (payload["recovery_id"], "epoch", str(SESSION),
             json.dumps(payload, sort_keys=True, separators=(",", ":"))))
    before = _canonical_rows(store)
    with pytest.raises(ValueError, match="duplicate"):
        store.read_session_candidate_bar_recoveries("epoch", SESSION)
    assert _canonical_rows(store) == before


@pytest.mark.parametrize("kind", ["recoveries", "issues"])
@pytest.mark.parametrize("guard", ["count", "bytes", "record_bytes"])
def test_complete_session_read_accepts_exact_bounds_rejects_one_over(tmp_path, monkeypatch, kind, guard):
    store = _small_store(tmp_path)
    table = "candidate_bar_recoveries" if kind == "recoveries" else "candidate_input_issues"
    method = (store.read_session_candidate_bar_recoveries if kind == "recoveries"
              else store.read_session_candidate_input_issues)
    with store._connect() as conn:
        size = conn.execute(f"SELECT length(CAST(payload_json AS BLOB)) FROM {table}").fetchone()[0]
    if guard == "count":
        monkeypatch.setattr(store_module, "_MAX_CANDIDATE_SIGNAL_IDENTITIES", 1)
        if kind == "issues":
            # Two dependency issues per identity are allowed; fill that boundary.
            with store._connect() as conn:
                payload = json.loads(conn.execute("SELECT payload_json FROM candidate_input_issues").fetchone()[0])
                payload["issue_id"] = "second-issue"
                payload["dependency_kind"] = "volatility_history"
                encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
                conn.execute("INSERT INTO candidate_input_issues VALUES (?,?,?,?,?,?)",
                    (payload["issue_id"], "epoch", str(SESSION), "volatility_history", "ZZ0000", encoded))
    else:
        constant = ("_MAX_CANDIDATE_SIGNAL_BINDING_PAYLOAD_BYTES" if guard == "bytes"
                    else "_MAX_CANDIDATE_SESSION_RECORD_BYTES")
        monkeypatch.setattr(store_module, constant, size)
    assert len(method("epoch", SESSION)) == (2 if kind == "issues" and guard == "count" else 1)
    if guard == "count":
        monkeypatch.setattr(store_module, "_MAX_CANDIDATE_SIGNAL_IDENTITIES", 0)
    else:
        monkeypatch.setattr(store_module, constant, size - 1)
    before = _canonical_rows(store)
    with pytest.raises(ValueError, match="bound"):
        method("epoch", SESSION)
    assert _canonical_rows(store) == before


def test_complete_session_read_budget_and_fetch_share_sqlite_snapshot(tmp_path, monkeypatch):
    store = _small_store(tmp_path)
    monkeypatch.setattr(store_module, "_MAX_CANDIDATE_SIGNAL_IDENTITIES", 1)
    native_connect = store._connect
    appended = []
    def trace(sql):
        if not appended and sql.startswith("SELECT recovery_id, epoch_id"):
            appended.append(True)
            with sqlite3.connect(store.path) as writer:
                payload = json.loads(writer.execute("SELECT payload_json FROM candidate_bar_recoveries").fetchone()[0])
                payload["recovery_id"] = "concurrent-append"
                payload["ticker"] = "ZZ0001"
                payload["attempts"][0]["ticker"] = "ZZ0001"
                writer.execute("INSERT INTO candidate_bar_recoveries VALUES (?,?,?,?)",
                    (payload["recovery_id"], "epoch", str(SESSION),
                     json.dumps(payload, sort_keys=True, separators=(",", ":"))))
    @contextmanager
    def connected():
        with native_connect() as conn:
            conn.set_trace_callback(trace)
            yield conn
    monkeypatch.setattr(store, "_connect", connected)
    assert len(store.read_session_candidate_bar_recoveries("epoch", SESSION)) == 1
    assert appended
    with pytest.raises(ValueError, match="bound"):
        store.read_session_candidate_bar_recoveries("epoch", SESSION)
