"""Outcomes retain missing evidence without changing independent paper accounting."""
from dataclasses import replace
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest

from test_30day_simulation import (
    FakeStrategy, _authoritative_orchestrator, _authoritative_committee,
)
from tradingagents.strategies.execution import SignalRecord
from tradingagents.strategies.execution.price_source import CorporateActionValidationError
from tradingagents.strategies.orchestration.trading_calendar import next_session, session_close


def test_unexpected_shared_input_failure_logs_safe_origin_and_keeps_gap_closed(tmp_path, caplog):
    import json
    import logging
    from tradingagents.strategies.orchestration.session_executor import SessionExecutor

    orch, _ = _authoritative_orchestrator(tmp_path, cohorts=2, strategy_modules=[])
    session = date(2026, 3, 30)
    secret = "canary-private-api-key"

    def secret_bearing_fetch(*args, **kwargs):
        raise RuntimeError(f"https://private.example/?key={secret} Authorization: Bearer {secret} /credentials/account/token.json")

    caplog.set_level(logging.ERROR)
    try:
        with patch.object(SessionExecutor, "fetch_input_bundle", side_effect=secret_bearing_fetch):
            results = orch.run_daily(session.isoformat())
        assert all(not row["execution_valid"] for row in results.values())
        assert orch._metric_store.load_epoch(orch._epoch_id).status == "invalid"
        assert all(not c["ledger"].read_fills(session, session) for c in orch.cohorts)
        diagnostics = [json.loads(r.getMessage()) for r in caplog.records
                       if r.getMessage().startswith('{"event": "shared_session_input_boundary_failed"')]
        assert len(diagnostics) == 1
        diagnostic = diagnostics[0]
        assert diagnostic["exception_type"] == "RuntimeError"
        assert 1 <= len(diagnostic["code_frames"]) <= 8
        assert diagnostic["code_frames"][-1]["function"] == "secret_bearing_fetch"
        assert all(isinstance(f["line"], int) and f["line"] > 0 for f in diagnostic["code_frames"])
        assert all("/" not in f["file"] and "\\" not in f["file"] for f in diagnostic["code_frames"])
        assert all(value not in caplog.text for value in (secret, "private.example", "Authorization", "/credentials", "token.json"))
        with patch.object(SessionExecutor, "fetch_input_bundle", side_effect=AssertionError("replay refetched")):
            replay = orch.run_daily(session.isoformat())
        assert all(not row["execution_valid"] for row in replay.values())
    finally:
        for cohort in orch.cohorts:
            cohort["ledger"].close()


@pytest.mark.parametrize("boundary,helper", [
    ("candidate_identity", "_candidate_identity_scope_for_run"),
    ("candidate_reference_replay", "_replay_candidate_reference_issues"),
    ("candidate_reference_resolution", "_resolve_candidate_bars"),
    ("staging_volatility", "_restore_staging_volatility"),
])
def test_candidate_boundary_failure_logs_safe_origin_and_preserves_accounting(
    tmp_path, caplog, monkeypatch, boundary, helper,
):
    import json
    import logging
    from tradingagents.strategies.orchestration import daily_pipeline

    orch, _ = _authoritative_orchestrator(tmp_path, cohorts=2, strategy_modules=[])
    if boundary == "staging_volatility":
        orch._base_config["autoresearch"]["portfolio_policy"] = {}
    session = date(2026, 3, 30)
    secret = "candidate-private-api-key"

    def offline_price_history(engine, tickers, start, end):
        import pandas as pd
        from tradingagents.strategies.orchestration.trading_calendar import previous_session

        assert boundary == "staging_volatility", "unexpected price-history acquisition"
        sessions = [previous_session(date.fromisoformat(end))]
        for _ in range(60):
            sessions.append(previous_session(sessions[-1]))
        for ticker in tickers:
            engine._price_cache[ticker] = pd.DataFrame(
                {"Close": [100.0 + index / 10 for index in range(61)]},
                index=pd.DatetimeIndex(reversed(sessions)),
            )

    for cohort in orch.cohorts:
        engine = cohort["engine"]
        monkeypatch.setattr(engine, "_fetch_missing_prices",
                            lambda tickers, start, end, engine=engine:
                            offline_price_history(engine, tickers, start, end))

    def secret_bearing_failure(*args, **kwargs):
        raise RuntimeError(f"https://private.example/?key={secret} Authorization: Bearer {secret}")

    caplog.set_level(logging.ERROR)
    try:
        with patch.object(daily_pipeline, helper, side_effect=secret_bearing_failure):
            results = orch.run_daily(session.isoformat())
        assert len(results) == 2
        assert all(row["error"] and row["execution_valid"] for row in results.values())
        assert all(row["staging_valid"] is False for row in results.values())
        assert all(not c["ledger"].read_fills(session, session) for c in orch.cohorts)
        diagnostics = [json.loads(record.getMessage()) for record in caplog.records
                       if record.getMessage().startswith('{"event": "' + boundary + '_boundary_failed"')]
        assert len(diagnostics) == 1
        diagnostic = diagnostics[0]
        assert diagnostic["exception_type"] == "RuntimeError"
        assert diagnostic["session"] == session.isoformat()
        assert 1 <= len(diagnostic["code_frames"]) <= 8
        assert diagnostic["code_frames"][-1]["function"] == "secret_bearing_failure"
        assert all("/" not in frame["file"] and "\\" not in frame["file"]
                   for frame in diagnostic["code_frames"])
        assert all(value not in caplog.text for value in (secret, "private.example", "Authorization"))
    finally:
        for cohort in orch.cohorts:
            cohort["ledger"].close()


@pytest.mark.parametrize("kind,gap_index,expected", [
    ("bar", 2, "valid"),  # Intermediate prices are not an outcome dependency.
    ("action", 2, "invalid"),
    ("bar", 5, "invalid"),
])
def test_outcome_only_gap_keeps_books_and_healthy_outcomes_valid(tmp_path, kind, gap_index, expected):
    orch, source = _authoritative_orchestrator(tmp_path, cohorts=2, strategy_modules=[FakeStrategy(hold_days=100)])
    days = [date(2026, 3, 30)]
    for _ in range(5):
        days.append(next_session(days[-1]))
    original_bars, original_actions = source.get_daily_bars, source.get_corporate_actions

    def bars(tickers, start, end, adjusted=False):
        result = original_bars(tickers, start, end, adjusted)
        if kind == "bar" and start == days[gap_index]:
            result.pop(("ZZZZ", start), None)
        return result

    def actions(tickers, session):
        if kind == "action" and session == days[gap_index] and "ZZZZ" in tickers:
            raise CorporateActionValidationError("missing diagnostic action evidence")
        return original_actions(tickers, session)

    source.get_daily_bars, source.get_corporate_actions = bars, actions
    try:
        with patch("tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize", side_effect=_authoritative_committee):
            orch.run_daily(days[0].isoformat())
            epoch = orch._epoch_id
            stamp = session_close(days[0])
            orch.cohorts[0]["ledger"].record_signal(SignalRecord(
                "untraded", epoch, "foundation-30d", "untraded-event", "filing_analysis", "ZZZZ", "long",
                stamp, stamp, days[0], Decimal(100), stamp, "untraded-evidence",
            ))
            for day in days[1:]:
                result = orch.run_daily(day.isoformat())
                assert all(row["execution_valid"] for row in result.values()), result
                assert orch._epoch_id == epoch
            outcomes = orch._metric_store.read_outcomes(epoch)
            untraded = [o for o in outcomes if o.signal_id == "untraded" and o.holding_sessions == 5]
            assert len(untraded) == 1 and untraded[0].status == expected
            assert all(o.status == "valid" for o in outcomes if o.ticker == "AAPL")
            assert any(o.ticker == "AAPL" for o in outcomes)
            if kind == "bar" and gap_index == 2:
                assert not any("ZZZZ" in ts and day == days[2] for ts, day in source.raw_calls)
            calls = (len(source.raw_calls), len(source.action_calls))
            orch.run_daily(days[-1].isoformat())
            assert calls == (len(source.raw_calls), len(source.action_calls))
    finally:
        for cohort in orch.cohorts:
            cohort["ledger"].close()


def test_old_epoch_and_skipped_maturities_remain_in_outcome_denominator(tmp_path):
    orch, _ = _authoritative_orchestrator(tmp_path, strategy_modules=[])
    first = date(2026, 3, 30)
    try:
        orch.run_daily(first.isoformat())
        old_epoch = orch._epoch_id
        stamp = session_close(first)
        ledger = orch.cohorts[0]["ledger"]
        ledger.record_signal(SignalRecord("old", old_epoch, "p", "old-event", "s", "ZZZZ", "long", stamp, stamp, first, Decimal(100), stamp, "e"))
        later = first
        for _ in range(6):
            later = next_session(later)
        orch.cohorts[0]["executor"].invalidate_metric_epoch(next_session(first), epoch_id=old_epoch)
        orch.run_daily(later.isoformat())
        assert orch._epoch_id != old_epoch
        matured = [o for o in orch._metric_store.read_outcomes(old_epoch) if o.signal_id == "old"]
        assert len(matured) == 1 and matured[0].holding_sessions == 5
        assert matured[0].status == "invalid" and matured[0].invalid_reason
    finally:
        for cohort in orch.cohorts:
            cohort["ledger"].close()


@pytest.mark.parametrize("interrupt", [False, True])
def test_portfolio_gap_preserves_healthy_outcome_and_replays_without_fetch(tmp_path, interrupt):
    orch, source = _authoritative_orchestrator(tmp_path, cohorts=2, strategy_modules=[FakeStrategy(hold_days=100)])
    days = [date(2026, 3, 30)]
    for _ in range(5):
        days.append(next_session(days[-1]))
    try:
        with patch("tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize", side_effect=_authoritative_committee):
            orch.run_daily(days[0].isoformat())
            epoch, stamp = orch._epoch_id, session_close(days[0])
            orch.cohorts[0]["ledger"].record_signal(SignalRecord(
                "independent", epoch, "p", "independent-event", "s", "ZZZZ", "long",
                stamp, stamp, days[0], Decimal(100), stamp, "independent-evidence",
            ))
            for day in days[1:-1]:
                orch.run_daily(day.isoformat())
            resolve = source.resolve_governed_daily_bars

            def held_gap(tickers, session, *, processed_at):
                resolved = resolve(tickers, session, processed_at=processed_at)
                if session == days[-1]:
                    return replace(resolved, bars={t: b for t, b in resolved.bars.items() if t != "AAPL"}, failure_map={"AAPL": "missing held price"})
                return resolved

            source.resolve_governed_daily_bars = held_gap
            if interrupt:
                def crash(_marker):
                    raise RuntimeError("after diagnostic evidence and gap marker")
                orch._after_gap_marker = crash
                with pytest.raises(RuntimeError, match="after diagnostic"):
                    orch.run_daily(days[-1].isoformat())
                calls = (len(source.raw_calls), len(source.action_calls))
                orch._after_gap_marker = lambda _marker: None
                result = orch.run_daily(days[-1].isoformat())
                assert calls == (len(source.raw_calls), len(source.action_calls))
            else:
                result = orch.run_daily(days[-1].isoformat())
            assert all(not row["execution_valid"] for row in result.values())
            outcomes = orch._metric_store.read_outcomes(epoch)
            assert next(o for o in outcomes if o.signal_id == "independent").status == "valid"
            assert all(o.status == "invalid" for o in outcomes if o.ticker == "AAPL")
            calls = (len(source.raw_calls), len(source.action_calls))
            orch.run_daily(days[-1].isoformat())
            assert calls == (len(source.raw_calls), len(source.action_calls))
    finally:
        for cohort in orch.cohorts:
            cohort["ledger"].close()


def test_legacy_bound_replay_never_fetches_missing_diagnostic_inputs(tmp_path):
    orch, source = _authoritative_orchestrator(tmp_path, strategy_modules=[FakeStrategy(hold_days=100)])
    days = [date(2026, 3, 30)]
    for _ in range(5):
        days.append(next_session(days[-1]))
    try:
        with patch("tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize", side_effect=_authoritative_committee):
            orch.run_daily(days[0].isoformat())
            stamp = session_close(days[0])
            orch.cohorts[0]["ledger"].record_signal(SignalRecord(
                "legacy-untraded", orch._epoch_id, "p", "legacy-event", "s", "ZZZZ", "long",
                stamp, stamp, days[0], Decimal(100), stamp, "e",
            ))
            for day in days[1:]:
                orch.run_daily(day.isoformat())
            # Simulate a previously accepted accounting context that predates
            # the additive diagnostic acquisition table. Keep accounting intact.
            with orch._metric_store._connect() as connection:
                connection.execute("DELETE FROM outcome_inputs WHERE session=?", (days[-1].isoformat(),))
                connection.execute("DELETE FROM outcomes")
            calls = (len(source.raw_calls), len(source.action_calls), len(source.benchmark_calls))
            result = orch.run_daily(days[-1].isoformat())
            assert all(row["replayed"] for row in result.values())
            assert calls == (len(source.raw_calls), len(source.action_calls), len(source.benchmark_calls))
            missing = orch._metric_store.load_outcome_input("ZZZZ", days[-1])
            assert missing is not None and missing["price_error"] and missing["action_error"]
            bound = orch._metric_store.load_outcome_input("AAPL", days[-1])
            assert bound is not None and bound["bar"] is not None
            assert not bound["price_error"] and not bound["action_error"]
    finally:
        for cohort in orch.cohorts:
            cohort["ledger"].close()


def test_later_action_fetch_failure_cannot_erase_governed_price_failure(tmp_path):
    orch, source = _authoritative_orchestrator(tmp_path, strategy_modules=[FakeStrategy(hold_days=100)])
    days = [date(2026, 3, 30)]
    for _ in range(5):
        days.append(next_session(days[-1]))
    try:
        with patch("tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize", side_effect=_authoritative_committee):
            for day in days[:-1]:
                orch.run_daily(day.isoformat())
            epoch = orch._epoch_id
            original_actions = source.get_corporate_actions
            failed_once = False

            def no_governed_quotes(*args, **kwargs):
                raise RuntimeError("governed quote unavailable")

            def actions(tickers, session):
                nonlocal failed_once
                if not failed_once:
                    failed_once = True
                    raise RuntimeError("shared action fetch failed")
                return original_actions(tickers, session)

            source.resolve_governed_daily_bars = no_governed_quotes
            source.get_corporate_actions = actions
            result = orch.run_daily(days[-1].isoformat())
            assert all(not row["execution_valid"] for row in result.values())
            mature = [row for row in orch._metric_store.read_outcomes(epoch) if row.ticker == "AAPL"]
            assert mature and all(row.status == "invalid" for row in mature)
            evidence = orch._metric_store.load_outcome_input("AAPL", days[-1])
            assert evidence["bar"] is None and evidence["price_error"]
            assert all("AAPL" in row["governed_failure_map"] for row in result.values())
    finally:
        for cohort in orch.cohorts:
            cohort["ledger"].close()
