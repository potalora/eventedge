"""Tests for the pre-run pipeline integrity check (preflight).

No network and no generation state: the engine is constructed against a
throwaway state dir with a stub registry, and the shared fetch is replaced
with fixture data. The screens and event-identity gates under test are the
real production code paths.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import MappingProxyType
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
            engine,
            "_fetch_all_data",
            lambda start, end: _govt_contracts_fixture("2026-07-07"),
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

    def test_empty_ticker_candidates_counted_as_pending_llm(
        self, tmp_path, monkeypatch
    ):
        """Production screen_and_enrich discards empty-ticker signals before
        staging (LLM enrichment resolves tickers first); preflight must mirror
        that filter instead of failing on it."""
        from tradingagents.strategies.modules.base import Candidate
        from tradingagents.strategies.orchestration.preflight import run_preflight

        config, engine = _make_engine(tmp_path)
        monkeypatch.setattr(engine, "_fetch_all_data", lambda start, end: {})

        llm_mapped = MagicMock()
        llm_mapped.name = "regulatory_pipeline"
        llm_mapped.screen.return_value = [
            Candidate(
                ticker="",
                date="2026-08-06",
                direction="short",
                score=0.5,
                metadata={"needs_llm_analysis": True, "document_id": "RULE-1"},
            ),
            Candidate(
                ticker="   ",
                date="2026-08-06",
                direction="short",
                score=0.5,
                metadata={"needs_llm_analysis": True, "document_id": "RULE-2"},
            ),
        ]
        monkeypatch.setattr(engine, "paper_trade_strategies", [llm_mapped])

        report = run_preflight(config, "2026-08-06", engine=engine)

        entry = report["horizons"]["30d"]["regulatory_pipeline"]
        assert entry["candidates"] == 2
        assert entry["pending_llm"] == 2
        assert entry["staged"] == 0
        assert report["ok"] is True
        assert report["failures"] == []

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


class TestGovernedPreflight:
    @staticmethod
    def _snapshot(tmp_path, *, status="uninitialized"):
        from tradingagents.strategies.orchestration.preflight_state import (
            PreflightStateSnapshot,
        )

        return PreflightStateSnapshot(
            state_status=status,
            epoch_id="preflight-prospective-2026-08-10",
            governed_tickers=("BIL", "SPY"),
            cohort_ids_by_ticker=MappingProxyType(
                {"BIL": ("cohort-a",), "SPY": ("cohort-a",)}
            ),
            metric_store_path=None,
            file_identities=(),
        )

    def test_preclose_governed_probe_is_not_ready_without_provider_call(
        self, tmp_path, monkeypatch
    ):
        from tradingagents.strategies.execution import (
            price_source as price_source_module,
        )
        from tradingagents.strategies.orchestration.preflight import run_preflight
        from tradingagents.strategies.orchestration.trading_calendar import (
            session_close,
        )

        snapshot = self._snapshot(tmp_path)
        inspector = MagicMock(return_value=snapshot)
        resolver = MagicMock()
        price_source = MagicMock()
        monkeypatch.setattr(
            price_source_module,
            "YFinancePriceSource",
            MagicMock(side_effect=AssertionError("provider instantiated pre-close")),
        )
        before_close = session_close(date(2026, 8, 10)) - timedelta(seconds=1)
        config = {
            "autoresearch": {
                "state_dir": str(tmp_path / "state"),
                "paper_ledger": {"benchmark_symbols": ["SPY", "BIL"]},
            }
        }

        report = run_preflight(
            config,
            "2026-08-10",
            mode="governed",
            price_source=price_source,
            processed_at=before_close,
            state_inspector=inspector,
            governed_resolver=resolver,
        )

        assert report["ok"] is True
        assert report["state_status"] == "uninitialized"
        assert report["governed_probe_status"] == "not_ready"
        assert report["governed_tickers"] == ["BIL", "SPY"]
        assert report["governed_bar_recoveries"] == []
        assert report["governed_failure_map"] == {}
        resolver.assert_not_called()
        price_source.assert_not_called()
        assert price_source.mock_calls == []

        default_report = run_preflight(
            config,
            "2026-08-10",
            mode="governed",
            processed_at=before_close,
            state_inspector=inspector,
            governed_resolver=resolver,
        )
        assert default_report["governed_probe_status"] == "not_ready"
        price_source_module.YFinancePriceSource.assert_not_called()

    def test_after_close_uses_shared_resolver_without_persistence(self, tmp_path):
        from tradingagents.strategies.orchestration.governed_market_data import (
            GovernedInputResolution,
        )
        from tradingagents.strategies.orchestration.preflight import run_preflight

        snapshot = self._snapshot(tmp_path)
        resolution = GovernedInputResolution(
            bars={},
            recovery_bindings={},
            recovery_summaries=(
                {
                    "ticker": "SPY",
                    "session": "2026-08-10",
                    "recovery_id": "recovery-spy",
                    "contract_version": "yfinance-60m-v1",
                    "evidence_digest": "digest-spy",
                    "affected_cohort_ids": ("cohort-a",),
                },
            ),
            failure_map={},
        )
        resolver = MagicMock(return_value=resolution)
        price_source = MagicMock()
        engine = MagicMock()
        processed_at = datetime(2026, 8, 10, 21, tzinfo=timezone.utc)
        config = {"autoresearch": {"state_dir": str(tmp_path / "state")}}

        report = run_preflight(
            config,
            "2026-08-10",
            engine=engine,
            mode="governed",
            price_source=price_source,
            processed_at=processed_at,
            state_inspector=MagicMock(return_value=snapshot),
            governed_resolver=resolver,
        )

        assert report["ok"] is True
        assert report["governed_probe_status"] == "ready"
        assert report["governed_failure_map"] == {}
        assert report["governed_bar_recoveries"] == [
            {
                "ticker": "SPY",
                "session": "2026-08-10",
                "recovery_id": "recovery-spy",
                "contract_version": "yfinance-60m-v1",
                "evidence_digest": "digest-spy",
                "affected_cohort_ids": ["cohort-a"],
            }
        ]
        kwargs = resolver.call_args.kwargs
        assert kwargs["persist"] is False
        assert kwargs["price_source"] is price_source
        assert kwargs["tickers"] == ("BIL", "SPY")
        assert kwargs["cohort_ids_by_ticker"] == snapshot.cohort_ids_by_ticker
        assert engine.mock_calls == []

    def test_governed_failure_map_is_exact_and_marks_report_failed(self, tmp_path):
        from tradingagents.strategies.orchestration.governed_market_data import (
            GovernedInputResolution,
        )
        from tradingagents.strategies.orchestration.preflight import run_preflight

        snapshot = self._snapshot(tmp_path)
        resolver = MagicMock(
            return_value=GovernedInputResolution(
                bars={},
                recovery_bindings={},
                recovery_summaries=(),
                failure_map={
                    "BIL": "missing BIL/2026-08-10",
                    "SPY": "invalid SPY/2026-08-10",
                },
            )
        )
        report = run_preflight(
            {"autoresearch": {"state_dir": str(tmp_path / "state")}},
            "2026-08-10",
            mode="governed",
            price_source=MagicMock(),
            processed_at=datetime(2026, 8, 10, 21, tzinfo=timezone.utc),
            state_inspector=MagicMock(return_value=snapshot),
            governed_resolver=resolver,
        )

        assert report["ok"] is False
        assert report["governed_probe_status"] == "failed"
        assert report["governed_failure_map"] == {
            "BIL": "missing BIL/2026-08-10",
            "SPY": "invalid SPY/2026-08-10",
        }

    @pytest.mark.parametrize(
        "failure_map",
        (
            {},
            {"OTHER": "invalid OTHER/2026-08-10"},
            {"SPY": ""},
        ),
    )
    def test_invalid_governed_error_map_fails_entire_snapshot(
        self, tmp_path, failure_map
    ):
        from tradingagents.strategies.orchestration.governed_market_data import (
            GovernedMarketDataError,
        )
        from tradingagents.strategies.orchestration.preflight import run_preflight

        snapshot = self._snapshot(tmp_path)
        resolver = MagicMock(side_effect=GovernedMarketDataError(failure_map))
        report = run_preflight(
            {"autoresearch": {"state_dir": str(tmp_path / "state")}},
            "2026-08-10",
            mode="governed",
            price_source=MagicMock(),
            processed_at=datetime(2026, 8, 10, 21, tzinfo=timezone.utc),
            state_inspector=MagicMock(return_value=snapshot),
            governed_resolver=resolver,
        )

        assert report["ok"] is False
        assert report["governed_probe_status"] == "failed"
        assert report["governed_failure_map"] == {
            "BIL": "invalid BIL/2026-08-10",
            "SPY": "invalid SPY/2026-08-10",
        }

    def test_uninitialized_topology_creation_during_resolution_fails_closed(
        self, tmp_path
    ):
        from tradingagents.strategies.metrics.store import MetricStore
        from tradingagents.strategies.orchestration.governed_market_data import (
            GovernedInputResolution,
        )
        from tradingagents.strategies.orchestration.preflight import run_preflight

        state_dir = tmp_path / "state"

        def mutate_topology(**_kwargs):
            MetricStore(state_dir / "metrics_v2.sqlite3")
            return GovernedInputResolution({}, {}, (), {})

        report = run_preflight(
            {"autoresearch": {"state_dir": str(state_dir)}},
            "2026-08-10",
            mode="governed",
            price_source=MagicMock(),
            processed_at=datetime(2026, 8, 10, 21, tzinfo=timezone.utc),
            governed_resolver=mutate_topology,
        )

        assert report["ok"] is False
        assert report["governed_probe_status"] == "failed"
        assert report["governed_failure_map"] == {}
        assert report["failures"][0]["error"] == (
            "state topology changed during preflight"
        )

    def test_already_invalid_state_skips_provider(self, tmp_path):
        from tradingagents.strategies.orchestration.preflight import run_preflight

        snapshot = self._snapshot(tmp_path, status="state_already_invalid")
        resolver = MagicMock()
        price_source = MagicMock()
        report = run_preflight(
            {"autoresearch": {"state_dir": str(tmp_path / "state")}},
            "2026-08-10",
            mode="governed",
            price_source=price_source,
            processed_at=datetime(2026, 8, 10, 21, tzinfo=timezone.utc),
            state_inspector=MagicMock(return_value=snapshot),
            governed_resolver=resolver,
        )

        assert report["ok"] is False
        assert report["state_status"] == "state_already_invalid"
        assert report["governed_probe_status"] == "state_already_invalid"
        resolver.assert_not_called()
        assert price_source.mock_calls == []

    def test_all_mode_composes_existing_screen_and_governed_reports(
        self, tmp_path, monkeypatch
    ):
        from tradingagents.strategies.orchestration.governed_market_data import (
            GovernedInputResolution,
        )
        from tradingagents.strategies.orchestration.preflight import run_preflight

        config, engine = _make_engine(tmp_path)
        monkeypatch.setattr(engine, "_fetch_all_data", lambda start, end: {})
        snapshot = self._snapshot(tmp_path)
        report = run_preflight(
            config,
            "2026-08-10",
            engine=engine,
            mode="all",
            price_source=MagicMock(),
            processed_at=datetime(2026, 8, 10, 21, tzinfo=timezone.utc),
            state_inspector=MagicMock(return_value=snapshot),
            governed_resolver=MagicMock(
                return_value=GovernedInputResolution({}, {}, (), {})
            ),
        )

        assert report["horizons"]
        assert report["governed_probe_status"] == "ready"
        assert report["ok"] is True

    def test_invalid_mode_rejected_before_any_work(self, tmp_path):
        from tradingagents.strategies.orchestration.preflight import run_preflight

        engine = MagicMock()
        with pytest.raises(ValueError, match="preflight mode"):
            run_preflight(
                {"autoresearch": {"state_dir": str(tmp_path)}},
                "2026-08-10",
                engine=engine,
                mode="other",
            )
        assert engine.mock_calls == []


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
            lambda gen_data, extra_args, log_name="last_run_output.log": dict(
                results_by_gen[gen_data["gen_id"]], _args=extra_args
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
