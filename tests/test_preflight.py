"""Tests for the pre-run pipeline integrity check (preflight).

No network and no generation state: the engine is constructed against a
throwaway state dir with a stub registry, and the shared fetch is replaced
with fixture data. The screens and event-identity gates under test are the
real production code paths.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _make_engine(tmp_path):
    """Real MultiStrategyEngine, stub registry, throwaway state dir."""
    from tradingagents.strategies.orchestration.multi_strategy_engine import (
        MultiStrategyEngine,
    )

    config = {
        "autoresearch": {
            "state_dir": str(tmp_path / "preflight-state"),
            "blocked_tickers": [],
        }
    }
    registry = MagicMock()
    registry.get.return_value = None
    engine = MultiStrategyEngine(config, registry=registry, use_llm=False)
    return config, engine


def _govt_contracts_fixture(last_modified_date: str) -> dict:
    """Minimal shared-data fixture carrying one govt_contracts candidate."""
    return {
        "usaspending": {
            "data": {
                "contracts": [
                    {
                        "recipient_name": "Lockheed Martin",
                        "amount": 50_000_000,
                        "award_id": "AWARD-1",
                        "last_modified_date": last_modified_date,
                    }
                ]
            }
        }
    }


class TestRunPreflight:
    def test_clean_dates_stage(self, tmp_path, monkeypatch):
        from tradingagents.strategies.orchestration.preflight import run_preflight

        config, engine = _make_engine(tmp_path)
        monkeypatch.setattr(
            engine, "_fetch_all_data", lambda start, end: _govt_contracts_fixture("2026-07-07")
        )

        report = run_preflight(config, "2026-08-06", engine=engine)

        assert report["trading_date"] == "2026-08-06"
        govt = report["horizons"]["30d"]["govt_contracts"]
        assert govt["candidates"] >= 1
        assert govt["staged"] == govt["candidates"]
        assert govt["errors"] == []
        assert report["ok"] is True
        assert report["failures"] == []

    def test_naive_api_timestamp_fails_preflight(self, tmp_path, monkeypatch):
        """Replays the 2026-08-03..06 outage shape: the exact naive string
        USASpending returns must be flagged before the scheduled run."""
        from tradingagents.strategies.orchestration.preflight import run_preflight

        config, engine = _make_engine(tmp_path)
        monkeypatch.setattr(
            engine,
            "_fetch_all_data",
            lambda start, end: _govt_contracts_fixture("2026-07-07 17:57:06"),
        )

        report = run_preflight(config, "2026-08-06", engine=engine)

        assert report["ok"] is False
        govt_failures = [
            failure
            for failure in report["failures"]
            if failure["strategy"] == "govt_contracts"
        ]
        assert govt_failures, "expected govt_contracts staging failures"
        assert all(
            "timezone awareness" in failure["error"] for failure in govt_failures
        )
        govt = report["horizons"]["30d"]["govt_contracts"]
        assert govt["candidates"] >= 1
        assert govt["staged"] == 0

    def test_screen_exception_reported(self, tmp_path, monkeypatch):
        from tradingagents.strategies.orchestration.preflight import run_preflight

        config, engine = _make_engine(tmp_path)
        monkeypatch.setattr(engine, "_fetch_all_data", lambda start, end: {})

        broken = MagicMock()
        broken.name = "broken_strategy"
        broken.screen.side_effect = RuntimeError("boom")
        monkeypatch.setattr(engine, "paper_trade_strategies", [broken])

        report = run_preflight(config, "2026-08-06", engine=engine)

        assert report["ok"] is False
        failure = report["failures"][0]
        assert failure["strategy"] == "broken_strategy"
        assert "screen failed" in failure["error"]

    def test_non_session_rejected(self, tmp_path):
        from tradingagents.strategies.orchestration.preflight import run_preflight

        config, engine = _make_engine(tmp_path)
        with pytest.raises(ValueError, match="not an XNYS session"):
            run_preflight(config, "2026-08-08", engine=engine)  # Saturday

    def test_state_dir_isolated(self, tmp_path, monkeypatch):
        """Preflight must never write into the configured state dir."""
        from tradingagents.strategies.orchestration.preflight import run_preflight

        config, engine = _make_engine(tmp_path)
        monkeypatch.setattr(engine, "_fetch_all_data", lambda start, end: {})

        run_preflight(config, "2026-08-06", engine=engine)

        state_dir = tmp_path / "preflight-state"
        assert not state_dir.exists() or not any(state_dir.iterdir())


class TestGenerationManagerPreflight:
    def _manager(self, tmp_path, monkeypatch, results_by_gen):
        from tradingagents.strategies.orchestration.generation_manager import (
            GenerationManager,
        )

        manager = GenerationManager(str(tmp_path))
        manifest = {
            "generations": [
                {
                    "gen_id": gen_id,
                    "status": "active",
                    "worktree_path": str(tmp_path / gen_id),
                    "state_dir": str(tmp_path / gen_id / "state"),
                    "run_history": [],
                }
                for gen_id in results_by_gen
            ]
        }
        monkeypatch.setattr(manager, "_load_manifest", lambda: manifest)
        saved = {}
        monkeypatch.setattr(manager, "_save_manifest", lambda data: saved.update(data))
        monkeypatch.setattr(
            manager,
            "_run_cohorts_subprocess",
            lambda gen_data, extra_args, log_name="last_run_output.log": (
                dict(results_by_gen[gen_data["gen_id"]], _args=extra_args)
            ),
        )
        return manager, manifest, saved

    def test_unsupported_flag_classified(self, tmp_path, monkeypatch):
        manager, manifest, saved = self._manager(
            tmp_path,
            monkeypatch,
            {
                "gen_004": {
                    "success": False,
                    "elapsed_s": 1.2,
                    "error": "usage: run_cohorts.py ... unrecognized arguments: --preflight",
                }
            },
        )

        results = manager.run_preflight("2026-08-06")

        assert results["gen_004"]["unsupported"] is True
        assert "predates --preflight" in results["gen_004"]["error"]
        # Manifest must remain untouched (no run_history for preflight).
        assert manifest["generations"][0]["run_history"] == []
        assert saved == {}

    def test_success_not_marked_unsupported(self, tmp_path, monkeypatch):
        manager, manifest, saved = self._manager(
            tmp_path,
            monkeypatch,
            {"gen_004": {"success": True, "elapsed_s": 42.0}},
        )

        results = manager.run_preflight("2026-08-06")

        assert results["gen_004"]["success"] is True
        assert "unsupported" not in results["gen_004"]
        assert manifest["generations"][0]["run_history"] == []
        assert saved == {}

    def test_failure_propagates(self, tmp_path, monkeypatch):
        manager, _, _ = self._manager(
            tmp_path,
            monkeypatch,
            {
                "gen_004": {
                    "success": False,
                    "elapsed_s": 90.0,
                    "error": "PREFLIGHT FAILED: staging rejected 3 candidates",
                }
            },
        )

        results = manager.run_preflight("2026-08-06")

        assert results["gen_004"]["success"] is False
        assert results["gen_004"].get("unsupported") is not True
