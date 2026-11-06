"""Offline release rehearsal: retire frozen failure, then dispatch fresh state.

Lifecycle, checkout identity, locks, journal and manager selection are real.
Reports and the external daily worker are fixtures; these tests do not qualify
native sources/models, canonical completion, or a historical evidence repair.
"""
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from scripts import recover_runtime as controller
from tradingagents.strategies.orchestration import generation_manager as lifecycle
from tradingagents.strategies.orchestration import runtime_recovery as recovery
from tradingagents.strategies.orchestration.cohort_orchestrator import (
    SIZE_PROFILES, build_default_cohorts,
)
from tradingagents.strategies.execution import OrderIntent, SignalRecord
from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger
from tradingagents.strategies.trading.portfolio_committee import TradeRecommendation
from tradingagents.strategies.trading.portfolio_policy import PortfolioPolicyDecision


BEFORE_DAILY = datetime(2026, 10, 29, 21, 25, tzinfo=timezone.utc)
DAILY = datetime(2026, 10, 29, 22, 1, tzinfo=timezone.utc)


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def _files(root):
    # SQLite's real read-only discovery can create transient WAL coordination
    # files. Ignore only shared-memory metadata and an empty WAL; any nonempty
    # WAL or changed database/evidence bytes must still fail preservation.
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file()
            and not path.name.endswith(".db-shm")
            and not (path.name.endswith(".db-wal") and path.stat().st_size == 0)}


def _tick(repo, now, **kwargs):
    return controller.tick(repo, now, clock=lambda: now, **kwargs)


@pytest.fixture
def release_rehearsal(tmp_path, monkeypatch):
    # Clone only local Git objects. Both frozen worktrees retain the actual
    # candidate commit, rather than claiming a synthetic fixture commit is it.
    source = Path(__file__).resolve().parents[1]
    commit = _git(source, "rev-parse", "HEAD")
    repo = tmp_path / "release"
    subprocess.run(
        ["git", "clone", "--shared", "--no-checkout", str(source), str(repo)],
        check=True, capture_output=True,
    )
    _git(repo, "checkout", "--detach", commit)
    profile = repo / "config/profiles/stabilization-v3.json"
    profile_bytes = profile.read_bytes()

    class Clock(datetime):
        current = datetime(2026, 10, 27, 21, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.current if tz else cls.current.replace(tzinfo=None)

    monkeypatch.setattr(lifecycle, "datetime", Clock)
    manager = lifecycle.GenerationManager(str(repo))
    old = manager.start_generation("retained failed sample", config_file=profile)
    book = build_default_cohorts({"autoresearch": {"state_dir": old.state_dir}})[0]
    ledger = PortfolioLedger(Path(book.state_dir) / "portfolio.db", book.name, Decimal("9876"))
    signal_at = datetime(2026, 10, 27, 22, tzinfo=timezone.utc)
    ledger.record_signal(SignalRecord(
        "old-signal", "old-epoch", "old-policy", "old-event", "earnings_call", "ACU", "long",
        signal_at, signal_at, date(2026, 10, 27), Decimal("100"), signal_at, "fixture-evidence",
    ))
    ledger.stage_intent(OrderIntent(
        "old-pending", ("old-signal",), book.name, "buy", 1, signal_at,
        date(2026, 10, 28), "next_session_open", "pending", None, None,
    ))
    # Reconstruct the historical failure's schema: a retained recommendation
    # with a rounded-zero weight and a contradictory trimmed policy decision.
    # The reporter is still a fixture, not canonical native validation.
    recommendation = TradeRecommendation(
        ticker="ACU", direction="long", position_size_pct=0.0, confidence=0.8,
        rationale="Constructed historical rounded-zero retention fixture",
        contributing_strategies=["earnings_call"], event_key="old-event",
        source_event_keys=("old-event",), strategy_tags=("earnings_call",),
    )
    policy_decision = PortfolioPolicyDecision(
        ticker="ACU", direction="long", event_key="old-event", decision="trimmed",
        reason="strategy:earnings_call", requested_weight=0.08, approved_weight=0.0,
    )
    ledger.record_committee_decision(
        date(2026, 10, 27), "old-epoch", "old-policy",
        {"status": {"status": "selected", "model_coverage": {"complete": True},
                    "degraded": False, "input_count": 1, "eligible_count": 1,
                    "selected_count": 1, "selected_signal_ids": ["old-signal"]},
         "recommendations": [asdict(recommendation)],
         "policy_decisions": [asdict(policy_decision)]},
    )
    ledger.close()
    old_files = _files(Path(old.state_dir))
    journal = {"version": recovery.VERSION, "schema_version": 1, "incidents": {}}
    for session in ("2026-10-27", "2026-10-28"):
        identity = recovery.identity_for(repo, asdict(old), session)
        record = recovery.new_incident(identity, BEFORE_DAILY)
        if session == "2026-10-27":
            record["daily_started"] = True
            recovery.finish_attempt(
                record, recovery._result("requires_user", "checkpoint_invalid"), BEFORE_DAILY,
            )
        journal["incidents"][recovery.incident_key(identity)] = record
    path = repo / "data/runtime_recovery/journal.json"
    recovery.atomic_json(path, journal)
    Clock.current = BEFORE_DAILY
    return repo, manager, old, old_files, journal, path, commit, profile, profile_bytes


def _replace(fixture):
    repo, manager, old, old_files, journal, path, commit, profile, profile_bytes = fixture
    manager.retire_generation(old.gen_id, delete_worktree=False)
    new = manager.start_generation("prospective replacement", config_file=profile)
    assert manager.get_generation(old.gen_id).status == "retired"
    assert _git(Path(old.worktree_path), "rev-parse", "HEAD") == old.git_commit
    assert _files(Path(old.state_dir)) == old_files
    assert recovery.load_journal(path) == journal
    assert new.git_commit == commit
    assert Path(new.config_file).read_bytes() == profile_bytes
    assert new.config_sha256 == hashlib.sha256(profile_bytes).hexdigest()
    assert _files(Path(new.state_dir)) == {"generation-config.json": profile_bytes}
    assert not (repo / "data/generations/.pending-start.json").exists()
    assert [row["gen_id"] for row in controller.manifest_rows(repo)] == [new.gen_id]
    recovery.identity_for(repo, asdict(new), "2026-10-29")
    return new


def test_retired_invalid_sample_remains_frozen_while_fresh_session_dispatches_once(
    release_rehearsal, monkeypatch,
):
    repo, manager, old, old_files, journal, path, *_ = release_rehearsal
    calls, workers = [], []
    healthy = {"staging_complete": True, "accounting_valid": True,
               "evidence_complete": True, "sources": {}}

    def forbidden(*args, **kwargs):
        pytest.fail("blocked or historical generation launched a worker")

    # The original operator incident really blocks a later ordinary session.
    blocked = _tick(repo, DAILY, launcher=forbidden,
                    reporter=lambda *args: {"outcome": "failed"})
    assert blocked["status"] == "operator_required"
    assert _files(Path(old.state_dir)) == old_files
    # The controller may record today's unstarted intent; retain that as well.
    journal = recovery.load_journal(path)
    replacement_fixture = (*release_rehearsal[:4], journal, *release_rehearsal[5:])
    new = _replace(replacement_fixture)
    # A stale explicit worker request also refuses the retired identity before
    # constructing books or attempting its still-pending historical order.
    with monkeypatch.context() as environment:
        environment.setenv("EVENTEDGE_GENERATION_ID", old.gen_id)
        environment.setenv("EVENTEDGE_GENERATION_COMMIT", old.git_commit)
        assert controller.worker(repo, "2026-10-28") == recovery._result(
            "requires_user", "identity_mismatch",
        )

    def external_worker(row, argv, **kwargs):
        workers.append((row["gen_id"], argv))
        assert row["gen_id"] == new.gen_id
        assert argv == ["--date", "2026-10-29"]
        # Model/provider work is replaced. Initialize real independent books to
        # demonstrate the replacement namespace cannot inherit historical cash.
        for book in build_default_cohorts({"autoresearch": {"state_dir": row["state_dir"]}}):
            cash = Decimal(str(SIZE_PROFILES[book.size_profile].total_capital))
            ledger = PortfolioLedger(Path(book.state_dir) / "portfolio.db", book.name, cash)
            try:
                assert ledger.opening_cash() == cash
                assert ledger.open_positions() == []
                assert ledger.connection.execute("SELECT COUNT(*) FROM order_intents").fetchone()[0] == 0
                assert ledger.connection.execute("SELECT COUNT(*) FROM committee_decisions").fetchone()[0] == 0
            finally:
                ledger.close()
        return {"success": True, "outcome": "clean", "elapsed_s": 0.0}

    monkeypatch.setattr(manager, "_run_cohorts_subprocess", external_worker)

    def launcher(root, row, session, action, **kwargs):
        calls.append((row["gen_id"], session, action))
        assert action == "daily" and session == "2026-10-29"
        assert [item["gen_id"] for item in kwargs["expected_roster"]] == [new.gen_id]
        kwargs["heartbeat"]()
        assert set(manager.run_daily(session)) == {new.gen_id}
        return 0, ""

    def reporter(root, row, session):
        assert row["gen_id"] == new.gen_id and session == "2026-10-29"
        assert workers, "success report must follow the fixture worker"
        return healthy

    first = _tick(repo, DAILY, launcher=launcher, reporter=reporter)
    repeated = _tick(repo, DAILY + timedelta(minutes=5), launcher=forbidden, reporter=reporter)
    assert first["status"] == "healthy", first
    assert repeated["status"] == "healthy", repeated
    assert calls == [(new.gen_id, "2026-10-29", "daily")]
    assert workers == [(new.gen_id, ["--date", "2026-10-29"])]
    assert len(list(Path(new.state_dir).glob("horizon_*/portfolio.db"))) == 16
    assert _files(Path(old.state_dir)) == old_files
    after = recovery.load_journal(path)
    for key, record in journal["incidents"].items():
        assert after["incidents"][key] == record
    assert all(item["generation_id"] == new.gen_id for item in first["incidents"])
    assert len(manager.get_generation(new.gen_id).run_history) == 1
    assert manager.get_generation(old.gen_id).run_history == []


@pytest.mark.parametrize("now", [BEFORE_DAILY, DAILY - timedelta(minutes=2)])
def test_replacement_before_daily_window_does_not_launch_or_backfill(release_rehearsal, now):
    repo, manager, old, old_files, journal, path, *_ = release_rehearsal
    new = _replace(release_rehearsal)

    def forbidden(*args, **kwargs):
        pytest.fail("pre-window observation launched or rendered historical work")

    result = _tick(repo, now, launcher=forbidden, reporter=forbidden)
    assert result["status"] == "healthy" and result["incidents"] == []
    assert recovery.load_journal(path) == journal
    assert _files(Path(old.state_dir)) == old_files
    assert list(Path(new.state_dir).iterdir()) == [Path(new.config_file)]
    assert manager.get_generation(new.gen_id).run_history == []
