"""Tests for the pre-run pipeline integrity check (preflight).

No network and no generation state: the engine is constructed against a
throwaway state dir with a stub registry, and the shared fetch is replaced
with fixture data. The screens and event-identity gates under test are the
real production code paths.
"""

from __future__ import annotations

import json
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import MappingProxyType
from unittest.mock import MagicMock

import pytest


@contextmanager
def _state_context(snapshot, metric_store=None):
    yield snapshot, metric_store


def _state_context_factory(snapshot, metric_store=None):
    return lambda **_kwargs: _state_context(snapshot, metric_store)


def _init_empty_git_repo(path):
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)


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

    @staticmethod
    def _bar(
        ticker: str, *, adjusted: bool = False, source: str = "yfinance"
    ):
        from tradingagents.strategies.execution.models import MarketBar

        return MarketBar(
            ticker=ticker,
            session=date(2026, 8, 10),
            open=Decimal("100"),
            high=Decimal("102"),
            low=Decimal("99"),
            close=Decimal("101"),
            source=source,
            fetched_at=datetime(2026, 8, 10, 20, 30, tzinfo=timezone.utc),
            adjusted=adjusted,
        )

    @classmethod
    def _successful_resolution(cls):
        from tradingagents.strategies.orchestration.governed_market_data import (
            GovernedInputResolution,
        )

        return GovernedInputResolution(
            bars={ticker: cls._bar(ticker) for ticker in ("BIL", "SPY")},
            recovery_bindings={},
            recovery_summaries=(),
            failure_map={},
        )

    @staticmethod
    def _freeze_preflight_completion(monkeypatch, completed_at):
        """Keep timestamp-sensitive preflight tests deterministic."""
        import tradingagents.strategies.orchestration.preflight as preflight

        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                if tz is None:
                    return completed_at.replace(tzinfo=None)
                return completed_at.astimezone(tz)

        monkeypatch.setattr(preflight, "datetime", FrozenDatetime)

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
        state_context_factory = _state_context_factory(snapshot)
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
            state_context_factory=state_context_factory,
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
            state_context_factory=state_context_factory,
            governed_resolver=resolver,
        )
        assert default_report["governed_probe_status"] == "not_ready"
        price_source_module.YFinancePriceSource.assert_not_called()

    def test_after_close_uses_shared_resolver_without_persistence(
        self, tmp_path, monkeypatch
    ):
        from tradingagents.strategies.metrics.models import (
            GOVERNED_BAR_RECOVERY_CONTRACT,
        )
        from tradingagents.strategies.orchestration.governed_market_data import (
            GovernedInputResolution,
            GovernedRecoveryBinding,
        )
        from tradingagents.strategies.orchestration.preflight import run_preflight

        snapshot = self._snapshot(tmp_path)
        resolution = GovernedInputResolution(
            bars={
                "BIL": self._bar("BIL"),
                "SPY": self._bar(
                    "SPY", source="yfinance-60m-reconstruction"
                ),
            },
            recovery_bindings={
                "SPY": GovernedRecoveryBinding(
                    ticker="SPY",
                    recovery_id="governed_bar_recovery:" + "a" * 64,
                    contract_version=GOVERNED_BAR_RECOVERY_CONTRACT,
                    evidence_digest="sha256:" + "b" * 64,
                )
            },
            recovery_summaries=(
                {
                    "ticker": "SPY",
                    "session": "2026-08-10",
                    "recovery_id": "governed_bar_recovery:" + "a" * 64,
                    "contract_version": GOVERNED_BAR_RECOVERY_CONTRACT,
                    "evidence_digest": "sha256:" + "b" * 64,
                    "affected_cohort_ids": ("cohort-a",),
                },
            ),
            failure_map={},
        )
        resolver = MagicMock(return_value=resolution)
        price_source = MagicMock()
        engine = MagicMock()
        processed_at = datetime(2026, 8, 10, 21, tzinfo=timezone.utc)
        self._freeze_preflight_completion(monkeypatch, processed_at)
        config = {"autoresearch": {"state_dir": str(tmp_path / "state")}}

        report = run_preflight(
            config,
            "2026-08-10",
            engine=engine,
            mode="governed",
            price_source=price_source,
            processed_at=processed_at,
            state_context_factory=_state_context_factory(snapshot),
            governed_resolver=resolver,
        )

        assert report["ok"] is True
        assert report["governed_probe_status"] == "ready"
        assert report["governed_failure_map"] == {}
        assert report["governed_bar_recoveries"] == [
            {
                "ticker": "SPY",
                "session": "2026-08-10",
                "recovery_id": "governed_bar_recovery:" + "a" * 64,
                "contract_version": GOVERNED_BAR_RECOVERY_CONTRACT,
                "evidence_digest": "sha256:" + "b" * 64,
                "affected_cohort_ids": ["cohort-a"],
            }
        ]
        kwargs = resolver.call_args.kwargs
        assert kwargs["persist"] is False
        assert kwargs["price_source"] is price_source
        assert kwargs["tickers"] == ("BIL", "SPY")
        assert kwargs["cohort_ids_by_ticker"] == snapshot.cohort_ids_by_ticker
        assert engine.mock_calls == []

    def test_fresh_bar_fetched_after_probe_start_is_accepted(self, tmp_path, monkeypatch):
        """The validation boundary is the end of retrieval, not its start.

        A real provider stamps the bar when its response arrives, which is
        necessarily later than the `processed_at` passed into the resolver.
        """
        from tradingagents.strategies.orchestration.governed_market_data import (
            GovernedInputResolution,
        )
        from tradingagents.strategies.orchestration.preflight import run_preflight

        probe_started_at = datetime(2026, 8, 10, 21, tzinfo=timezone.utc)
        completed_at = probe_started_at + timedelta(milliseconds=1)
        self._freeze_preflight_completion(monkeypatch, completed_at)
        resolution = self._successful_resolution()
        fresh_bars = {
            ticker: replace(bar, fetched_at=completed_at)
            for ticker, bar in resolution.bars.items()
        }
        resolver = MagicMock(
            return_value=GovernedInputResolution(
                bars=fresh_bars,
                recovery_bindings=resolution.recovery_bindings,
                recovery_summaries=resolution.recovery_summaries,
                failure_map=resolution.failure_map,
            )
        )

        report = run_preflight(
            {"autoresearch": {"state_dir": str(tmp_path / "state")}},
            "2026-08-10",
            mode="governed",
            price_source=MagicMock(),
            processed_at=probe_started_at,
            state_context_factory=_state_context_factory(self._snapshot(tmp_path)),
            governed_resolver=resolver,
        )

        assert report["ok"] is True
        assert report["governed_probe_status"] == "ready"
        assert report["governed_failure_map"] == {}

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
            state_context_factory=_state_context_factory(snapshot),
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
            {"SPY": "super-secret"},
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
            state_context_factory=_state_context_factory(snapshot),
            governed_resolver=resolver,
        )

        assert report["ok"] is False
        assert report["governed_probe_status"] == "failed"
        assert report["governed_failure_map"] == {
            "BIL": "invalid BIL/2026-08-10",
            "SPY": "invalid SPY/2026-08-10",
        }

    @pytest.mark.parametrize(
        "variant",
        (
            "wrong_type",
            "empty",
            "overlap",
            "invalid_bar",
            "binding_mismatch",
            "unbound_reconstruction",
            "healthy_bound",
            "malformed_summary_identity",
            "untrusted_source",
        ),
    )
    def test_resolution_must_be_concrete_complete_and_internally_bound(
        self, tmp_path, variant
    ):
        from tradingagents.strategies.orchestration.governed_market_data import (
            GovernedInputResolution,
        )
        from tradingagents.strategies.orchestration.preflight import run_preflight

        bars = {ticker: self._bar(ticker) for ticker in ("BIL", "SPY")}
        if variant == "wrong_type":
            resolution = object()
        elif variant == "empty":
            resolution = GovernedInputResolution({}, {}, (), {})
        elif variant == "overlap":
            resolution = GovernedInputResolution(
                bars,
                {},
                (),
                {"BIL": "missing BIL/2026-08-10"},
            )
        elif variant == "invalid_bar":
            resolution = GovernedInputResolution(
                {**bars, "SPY": self._bar("SPY", adjusted=True)}, {}, (), {}
            )
        elif variant == "binding_mismatch":
            resolution = GovernedInputResolution(
                bars,
                {},
                (
                    {
                        "ticker": "SPY",
                        "session": "2026-08-10",
                        "recovery_id": "governed_bar_recovery:" + "a" * 64,
                        "contract_version": "yfinance-60m-v1",
                        "evidence_digest": "sha256:" + "b" * 64,
                        "affected_cohort_ids": ("cohort-a",),
                    },
                ),
                {},
            )
        else:
            from tradingagents.strategies.orchestration.governed_market_data import (
                GovernedRecoveryBinding,
            )

            binding = GovernedRecoveryBinding(
                ticker="SPY",
                recovery_id="governed_bar_recovery:" + "a" * 64,
                contract_version="yfinance-60m-v1",
                evidence_digest="sha256:" + "b" * 64,
            )
            recovery_bars = dict(bars)
            bindings = {"SPY": binding}
            summaries = (
                {
                    "ticker": "SPY",
                    "session": "2026-08-10",
                    "recovery_id": binding.recovery_id,
                    "contract_version": binding.contract_version,
                    "evidence_digest": binding.evidence_digest,
                    "affected_cohort_ids": ("cohort-a",),
                },
            )
            if variant == "unbound_reconstruction":
                recovery_bars["SPY"] = self._bar(
                    "SPY", source="yfinance-60m-reconstruction"
                )
                bindings = {}
                summaries = ()
            elif variant == "malformed_summary_identity":
                recovery_bars["SPY"] = self._bar(
                    "SPY", source="yfinance-60m-reconstruction"
                )
                summaries[0]["evidence_digest"] = "raw-provider-secret"
            elif variant == "untrusted_source":
                recovery_bars["SPY"] = self._bar(
                    "SPY", source="raw-provider-secret"
                )
                bindings = {}
                summaries = ()
            resolution = GovernedInputResolution(
                recovery_bars, bindings, summaries, {}
            )
        snapshot = self._snapshot(tmp_path)
        report = run_preflight(
            {"autoresearch": {"state_dir": str(tmp_path / "state")}},
            "2026-08-10",
            mode="governed",
            price_source=MagicMock(),
            processed_at=datetime(2026, 8, 10, 21, tzinfo=timezone.utc),
            state_context_factory=_state_context_factory(snapshot),
            governed_resolver=MagicMock(return_value=resolution),
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
            state_context_factory=_state_context_factory(snapshot),
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
        from tradingagents.strategies.orchestration.preflight import run_preflight

        config, engine = _make_engine(tmp_path)
        self._freeze_preflight_completion(
            monkeypatch, datetime(2026, 8, 10, 21, tzinfo=timezone.utc)
        )
        monkeypatch.setattr(engine, "_fetch_all_data", lambda start, end: {})
        snapshot = self._snapshot(tmp_path)
        report = run_preflight(
            config,
            "2026-08-10",
            engine=engine,
            mode="all",
            price_source=MagicMock(),
            processed_at=datetime(2026, 8, 10, 21, tzinfo=timezone.utc),
            state_context_factory=_state_context_factory(snapshot),
            governed_resolver=MagicMock(return_value=self._successful_resolution()),
        )

        assert report["horizons"]
        assert report["governed_probe_status"] == "ready"
        assert report["screen_ok"] is True
        assert report["governed_ok"] is True
        assert report["screen_failures"] == []
        assert report["governed_failures"] == []
        assert report["ok"] is True

    def test_all_mode_keeps_screen_and_governed_failures_separate(
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
            state_context_factory=_state_context_factory(snapshot),
            governed_resolver=MagicMock(
                return_value=GovernedInputResolution(
                    {},
                    {},
                    (),
                    {
                        "BIL": "missing BIL/2026-08-10",
                        "SPY": "invalid SPY/2026-08-10",
                    },
                )
            ),
        )

        assert report["screen_ok"] is True
        assert report["screen_failures"] == []
        assert report["governed_ok"] is False
        assert len(report["governed_failures"]) == 2
        assert report["failures"] == report["governed_failures"]
        assert report["ok"] is False

    def test_all_mode_screen_failure_does_not_mask_governed_ready(
        self, tmp_path, monkeypatch
    ):
        from tradingagents.strategies.orchestration.preflight import run_preflight

        config, engine = _make_engine(tmp_path)
        self._freeze_preflight_completion(
            monkeypatch, datetime(2026, 8, 10, 21, tzinfo=timezone.utc)
        )
        monkeypatch.setattr(engine, "_fetch_all_data", lambda start, end: {})
        broken = MagicMock()
        broken.name = "broken_strategy"
        broken.screen.side_effect = RuntimeError("boom")
        monkeypatch.setattr(engine, "paper_trade_strategies", [broken])
        snapshot = self._snapshot(tmp_path)

        report = run_preflight(
            config,
            "2026-08-10",
            engine=engine,
            mode="all",
            price_source=MagicMock(),
            processed_at=datetime(2026, 8, 10, 21, tzinfo=timezone.utc),
            state_context_factory=_state_context_factory(snapshot),
            governed_resolver=MagicMock(return_value=self._successful_resolution()),
        )

        assert report["screen_ok"] is False
        assert report["screen_failures"]
        assert report["governed_ok"] is True
        assert report["governed_failures"] == []
        assert report["governed_probe_status"] == "ready"
        assert report["failures"] == report["screen_failures"]
        assert report["ok"] is False

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

        _init_empty_git_repo(tmp_path)
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
            lambda gen_data, extra_args, **kwargs: dict(
                results_by_gen[gen_data["gen_id"]],
                _args=extra_args,
                _kwargs=kwargs,
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
        assert "outcome" not in results["gen_004"]
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

    def test_mode_is_passed_without_generation_state_logging(
        self, tmp_path, monkeypatch
    ):
        manager, manifest, saved = self._manager(
            tmp_path,
            monkeypatch,
            {
                "gen_004": {
                    "success": True,
                    "elapsed_s": 1.0,
                    "governed_probe_status": "ready",
                }
            },
        )
        calls = []

        def capture(
            gen_data,
            extra_args,
            *,
            preflight_mode=None,
            write_log=True,
            inherited_lock=None,
        ):
            calls.append(
                (gen_data["gen_id"], extra_args, preflight_mode, write_log, inherited_lock)
            )
            return {
                "success": True,
                "elapsed_s": 1.0,
                "governed_probe_status": "ready",
            }

        monkeypatch.setattr(manager, "_run_cohorts_subprocess", capture)
        results = manager.run_preflight("2026-08-06", mode="governed")

        assert results["gen_004"]["success"] is True
        assert calls and calls[0][1] == [
            "--date",
            "2026-08-06",
            "--preflight",
            "--preflight-mode",
            "governed",
        ]
        assert calls[0][2:4] == ("governed", False)
        assert calls[0][4] is not None
        assert manifest["generations"][0]["run_history"] == []
        assert saved == {}

    def test_preflight_busy_starts_no_child_and_changes_no_manifest(
        self, tmp_path, monkeypatch
    ):
        from tradingagents.strategies.orchestration.runtime_lock import (
            RuntimeLockBusy,
            runtime_lock,
        )

        manager, manifest, saved = self._manager(
            tmp_path,
            monkeypatch,
            {"gen_004": {"success": True, "elapsed_s": 1.0}},
        )
        child = MagicMock()
        monkeypatch.setattr(manager, "_run_cohorts_subprocess", child)

        with runtime_lock(manager._runtime_lock_path, exclusive=True):
            with pytest.raises(RuntimeLockBusy, match="runtime lock is busy"):
                manager.run_preflight("2026-08-06", mode="screen")

        child.assert_not_called()
        assert manifest["generations"][0]["run_history"] == []
        assert saved == {}

    @pytest.mark.parametrize(
        ("status", "ok", "governed_ok", "has_recovery", "expected_success"),
        (
            ("ready", True, True, True, True),
            ("not_ready", True, True, False, False),
            ("failed", False, False, False, False),
        ),
    )
    def test_preflight_subprocess_uses_distinct_report_parser_and_governed_gate(
        self,
        tmp_path,
        monkeypatch,
        status,
        ok,
        governed_ok,
        has_recovery,
        expected_success,
    ):
        import tradingagents.strategies.orchestration.generation_manager as gm

        _init_empty_git_repo(tmp_path)
        manager = gm.GenerationManager(str(tmp_path))
        state_dir = tmp_path / "state"
        worktree = tmp_path / "worktree"
        state_dir.mkdir()
        worktree.mkdir()
        recovery = {
            "ticker": "ESS",
            "session": "2026-08-06",
            "recovery_id": "governed_bar_recovery:" + "b" * 64,
            "contract_version": "yfinance-60m-v1",
            "evidence_digest": "sha256:" + "a" * 64,
            "affected_cohort_ids": ["cohort-a"],
        }
        report = {
            "ok": ok,
            "failures": [],
            "horizons": {},
            "state_status": "ready",
            "governed_probe_status": status,
            "governed_ok": governed_ok,
            "governed_bar_recoveries": [recovery] if has_recovery else [],
            "governed_failure_map": {},
        }
        process = MagicMock(
            returncode=0,
            stdout="diagnostic\n" + json.dumps(report, indent=2) + "\nPREFLIGHT\n",
            stderr="",
        )
        monkeypatch.setattr(gm.subprocess, "run", lambda *args, **kwargs: process)
        result = manager._run_cohorts_subprocess(
            {
                "gen_id": "gen_001",
                "git_commit": "a" * 40,
                "state_dir": str(state_dir),
                "worktree_path": str(worktree),
            },
            ["--date", "2026-08-06", "--preflight"],
            preflight_mode="governed",
            write_log=False,
        )

        assert result["success"] is expected_success
        assert "outcome" not in result
        assert result["governed_probe_status"] == status
        assert result["governed_bar_recoveries"] == (
            [recovery] if has_recovery else []
        )
        assert result["governed_failure_map"] == {}
        assert "horizons" not in result
        assert not (state_dir / "last_preflight_output.log").exists()

    def test_malformed_governed_report_fails_closed(self, tmp_path, monkeypatch):
        import tradingagents.strategies.orchestration.generation_manager as gm

        _init_empty_git_repo(tmp_path)
        manager = gm.GenerationManager(str(tmp_path))
        worktree = tmp_path / "worktree"
        state_dir = tmp_path / "state"
        worktree.mkdir()
        state_dir.mkdir()
        process = MagicMock(
            returncode=0,
            stdout=json.dumps({"ok": True, "horizon_fake": {}}),
            stderr="",
        )
        monkeypatch.setattr(gm.subprocess, "run", lambda *args, **kwargs: process)

        result = manager._run_cohorts_subprocess(
            {
                "gen_id": "gen_001",
                "git_commit": "a" * 40,
                "state_dir": str(state_dir),
                "worktree_path": str(worktree),
            },
            ["--date", "2026-08-06", "--preflight"],
            preflight_mode="governed",
            write_log=False,
        )

        assert result["success"] is False
        assert "malformed preflight report" in result["error"]

    def test_legacy_preflight_rejection_is_classified_without_raw_stderr(self):
        from tradingagents.strategies.orchestration.generation_manager import (
            _preflight_subprocess_result,
        )

        result = _preflight_subprocess_result(
            stdout="",
            stderr=(
                "usage: run_cohorts.py\n"
                "unrecognized arguments: --preflight PROVIDER_SECRET"
            ),
            returncode=2,
            elapsed=0.1,
            mode="all",
            trading_date="2026-08-06",
        )

        assert result["success"] is False
        assert "unrecognized arguments" in result["error"]
        assert "PROVIDER_SECRET" not in result["error"]

    def test_preflight_reporting_deduplicates_only_exact_canonical_recoveries(self):
        from tradingagents.strategies.orchestration.generation_manager import (
            _preflight_subprocess_result,
        )

        recovery = {
            "ticker": "ESS",
            "session": "2026-08-06",
            "recovery_id": "governed_bar_recovery:" + "b" * 64,
            "contract_version": "yfinance-60m-v1",
            "evidence_digest": "sha256:" + "a" * 64,
            "affected_cohort_ids": ["cohort-a"],
        }
        report = {
            "ok": True,
            "failures": [],
            "horizons": {},
            "state_status": "ready",
            "governed_probe_status": "ready",
            "governed_ok": True,
            "governed_bar_recoveries": [recovery, dict(recovery)],
            "governed_failure_map": {},
        }

        result = _preflight_subprocess_result(
            stdout=json.dumps(report, indent=2),
            stderr="",
            returncode=0,
            elapsed=0.1,
            mode="governed",
            trading_date="2026-08-06",
        )

        assert result["success"] is True
        assert result["governed_bar_recoveries"] == [recovery]

        report["governed_failure_map"] = {
            "SPY": "invalid SPY/2026-08-06"
        }
        inconsistent = _preflight_subprocess_result(
            stdout=json.dumps(report, indent=2),
            stderr="",
            returncode=0,
            elapsed=0.1,
            mode="governed",
            trading_date="2026-08-06",
        )
        assert inconsistent["success"] is False
        assert inconsistent["error"] == "malformed preflight report"

        report["governed_failure_map"] = {}
        report["governed_bar_recoveries"] = [
            recovery,
            {**recovery, "affected_cohort_ids": ["cohort-b"]},
        ]
        result = _preflight_subprocess_result(
            stdout=json.dumps(report, indent=2),
            stderr="",
            returncode=0,
            elapsed=0.1,
            mode="governed",
            trading_date="2026-08-06",
        )

        assert result["success"] is False
        assert result["error"] == "malformed preflight report"
        assert "governed_bar_recoveries" not in result

    def test_preflight_reporting_uses_task5_grammar_and_256_item_cap(self):
        from tradingagents.strategies.orchestration.generation_manager import (
            _preflight_subprocess_result,
        )

        def recovery(index):
            digest = f"{index:064x}"
            return {
                "ticker": f"T{index:04d}",
                "session": "2026-08-06",
                "recovery_id": "governed_bar_recovery:" + digest,
                "contract_version": "yfinance-60m-v1",
                "evidence_digest": "sha256:" + digest,
                "affected_cohort_ids": [f"cohort-{index:04d}"],
            }

        report = {
            "ok": True,
            "failures": [],
            "horizons": {},
            "state_status": "ready",
            "governed_probe_status": "ready",
            "governed_ok": True,
            "governed_bar_recoveries": [recovery(index) for index in range(256)],
            "governed_failure_map": {},
        }
        result = _preflight_subprocess_result(
            stdout=json.dumps(report, indent=2),
            stderr="",
            returncode=0,
            elapsed=0.1,
            mode="governed",
            trading_date="2026-08-06",
        )
        assert result["success"] is True
        assert len(result["governed_bar_recoveries"]) == 256

        report["governed_bar_recoveries"].append(recovery(256))
        oversized = _preflight_subprocess_result(
            stdout=json.dumps(report, indent=2),
            stderr="",
            returncode=0,
            elapsed=0.1,
            mode="governed",
            trading_date="2026-08-06",
        )
        assert oversized["success"] is False
        assert oversized["error"] == "malformed preflight report"

        report["governed_bar_recoveries"] = [
            {**recovery(0), "provider_secret": "do-not-propagate"}
        ]
        malformed = _preflight_subprocess_result(
            stdout=json.dumps(report, indent=2),
            stderr="",
            returncode=0,
            elapsed=0.1,
            mode="governed",
            trading_date="2026-08-06",
        )
        assert malformed["success"] is False
        assert "do-not-propagate" not in json.dumps(malformed)

    def test_preflight_parser_omits_raw_failure_fields_and_rejects_bad_status(self):
        from tradingagents.strategies.orchestration.generation_manager import (
            _preflight_subprocess_result,
        )

        report = {
            "ok": False,
            "failures": [],
            "horizons": {},
            "screen_ok": False,
            "governed_ok": True,
            "state_status": "ready",
            "governed_probe_status": "ready",
            "screen_failures": [
                {"error": "provider-secret", "nested": {"raw": "payload"}}
            ],
            "governed_failures": [{"error": "another-secret"}],
            "governed_bar_recoveries": [],
            "governed_failure_map": {},
        }
        result = _preflight_subprocess_result(
            stdout=json.dumps(report, indent=2),
            stderr="",
            returncode=0,
            elapsed=0.1,
            mode="all",
            trading_date="2026-08-06",
        )

        assert result["screen_ok"] is False
        assert result["governed_ok"] is True
        assert result["success"] is False
        assert result["error"] == "preflight all status: ready"
        assert "screen_failures" not in result
        assert "governed_failures" not in result
        assert "secret" not in json.dumps(result)

        report["ok"] = True
        contradictory = _preflight_subprocess_result(
            stdout=json.dumps(report, indent=2),
            stderr="",
            returncode=0,
            elapsed=0.1,
            mode="all",
            trading_date="2026-08-06",
        )
        assert contradictory["success"] is False
        assert contradictory["error"] == "malformed preflight report"

        report["governed_probe_status"] = "x" * 10_000
        malformed = _preflight_subprocess_result(
            stdout=json.dumps(report, indent=2),
            stderr="",
            returncode=0,
            elapsed=0.1,
            mode="all",
            trading_date="2026-08-06",
        )
        assert malformed["success"] is False
        assert malformed["error"] == "malformed preflight report"
        assert "x" * 100 not in json.dumps(malformed)


def test_run_cohorts_preflight_exit_contract_is_mode_specific():
    from scripts.run_cohorts import _preflight_exit_status

    assert _preflight_exit_status(
        {
            "ok": True,
            "failures": [],
            "horizons": {},
            "trading_date": "2026-08-06",
            "governed_ok": True,
            "state_status": "ready",
            "governed_probe_status": "ready",
            "governed_bar_recoveries": [],
            "governed_failure_map": {},
        },
        "governed",
    )[0] == 0
    assert _preflight_exit_status(
        {"ok": True, "governed_probe_status": "not_ready"}, "governed"
    )[0] != 0
    assert _preflight_exit_status(
        {
            "ok": True,
            "governed_probe_status": "ready",
            "governed_failure_map": {"SPY": "invalid SPY/2026-08-06"},
        },
        "governed",
    )[0] != 0
    assert _preflight_exit_status(
        {
            "ok": True,
            "failures": [],
            "horizons": {},
            "trading_date": "2026-08-06",
            "screen_ok": True,
            "screen_failures": [],
        },
        "screen",
    )[0] == 0
    assert _preflight_exit_status(
        {
            "ok": False,
            "failures": [{}],
            "horizons": {},
            "trading_date": "2026-08-06",
            "screen_ok": False,
            "screen_failures": [{}],
        },
        "screen",
    )[0] != 0
    assert _preflight_exit_status({"ok": True}, "all")[0] != 0


def test_direct_preflight_rejects_malformed_governed_evidence_without_rendering_raw(
    monkeypatch, capsys, tmp_path
):
    from scripts import run_cohorts
    from tradingagents.strategies.orchestration import preflight

    report = {
        "ok": True,
        "failures": [],
        "horizons": {},
        "trading_date": "2026-08-06",
        "governed_ok": True,
        "state_status": "ready",
        "governed_probe_status": "ready",
        "governed_bar_recoveries": [{"provider_secret": "DO_NOT_RENDER"}],
        "governed_failure_map": {},
    }

    @contextmanager
    def lock_context(**_kwargs):
        yield MagicMock()

    monkeypatch.setattr(preflight, "run_preflight", lambda *args, **kwargs: report)
    monkeypatch.setattr(run_cohorts, "_runtime_lock_context", lock_context)
    monkeypatch.setenv("EVENTEDGE_GENERATION_ID", "gen_001")
    monkeypatch.setenv("EVENTEDGE_GENERATION_COMMIT", "a" * 40)
    monkeypatch.setenv("AUTORESEARCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_cohorts.py",
            "--date",
            "2026-08-06",
            "--preflight",
            "--preflight-mode",
            "governed",
        ],
    )

    with pytest.raises(SystemExit) as raised:
        run_cohorts.main()

    assert raised.value.code == 1
    rendered = capsys.readouterr()
    assert "DO_NOT_RENDER" not in rendered.out + rendered.err
    assert "malformed report" in rendered.err


@pytest.mark.parametrize("mode", ("screen", "governed", "all"))
def test_direct_preflight_wire_round_trips_through_manager_parser(
    mode, monkeypatch, capsys, tmp_path
):
    from scripts import run_cohorts
    from tradingagents.strategies.orchestration import preflight
    from tradingagents.strategies.orchestration.generation_manager import (
        _preflight_subprocess_result,
    )

    report = {
        "ok": True,
        "failures": [],
        "horizons": {},
        "trading_date": "2026-08-06",
    }
    if mode in {"screen", "all"}:
        report.update({"screen_ok": True, "screen_failures": []})
    if mode in {"governed", "all"}:
        report.update(
            {
                "governed_ok": True,
                "state_status": "ready",
                "governed_probe_status": "ready",
                "governed_bar_recoveries": [],
                "governed_failure_map": {},
            }
        )

    @contextmanager
    def lock_context(**_kwargs):
        yield MagicMock()

    monkeypatch.setattr(preflight, "run_preflight", lambda *args, **kwargs: report)
    monkeypatch.setattr(run_cohorts, "_runtime_lock_context", lock_context)
    monkeypatch.setenv("EVENTEDGE_GENERATION_ID", "gen_001")
    monkeypatch.setenv("EVENTEDGE_GENERATION_COMMIT", "a" * 40)
    monkeypatch.setenv("AUTORESEARCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_cohorts.py",
            "--date",
            "2026-08-06",
            "--preflight",
            "--preflight-mode",
            mode,
        ],
    )

    run_cohorts.main()
    worker_stdout = capsys.readouterr().out
    result = _preflight_subprocess_result(
        stdout=worker_stdout,
        stderr="",
        returncode=0,
        elapsed=0.1,
        mode=mode,
        trading_date="2026-08-06",
    )

    assert result["success"] is True
    assert "failures" not in result
    assert "horizons" not in result


def test_run_cohorts_rejects_preflight_mode_without_preflight(monkeypatch, capsys):
    from scripts import run_cohorts

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_cohorts.py",
            "--date",
            "2026-08-06",
            "--preflight-mode",
            "governed",
        ],
    )

    with pytest.raises(SystemExit) as raised:
        run_cohorts.main()

    assert raised.value.code == 2
    assert "--preflight-mode requires --preflight" in capsys.readouterr().err


def test_direct_run_cohorts_preflight_holds_shared_lock_during_probe(
    monkeypatch, tmp_path
):
    from scripts import run_cohorts
    from tradingagents.strategies.orchestration import preflight

    active = []

    @contextmanager
    def lock_context(*, exclusive):
        assert exclusive is False
        active.append(True)
        try:
            yield MagicMock()
        finally:
            active.pop()

    def run(config, trading_date, *, mode):
        assert active == [True]
        assert mode == "screen"
        return {
            "ok": True,
            "screen_ok": True,
            "screen_failures": [],
            "failures": [],
            "horizons": {},
        }

    monkeypatch.setattr(run_cohorts, "_runtime_lock_context", lock_context)
    monkeypatch.setattr(preflight, "run_preflight", run)
    monkeypatch.setenv("EVENTEDGE_GENERATION_ID", "gen_001")
    monkeypatch.setenv("EVENTEDGE_GENERATION_COMMIT", "a" * 40)
    monkeypatch.setenv("AUTORESEARCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_cohorts.py",
            "--date",
            "2026-08-06",
            "--preflight",
            "--preflight-mode",
            "screen",
        ],
    )

    run_cohorts.main()
    assert active == []


def test_run_generations_preflight_mode_defaults_all_and_zero_governed_fails(
    monkeypatch, capsys
):
    from scripts import run_generations
    from tradingagents.strategies.orchestration.generation_manager import (
        GenerationManager,
    )

    captured = []
    monkeypatch.setattr(GenerationManager, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(
        GenerationManager,
        "run_preflight",
        lambda self, date, *, mode: captured.append((date, mode)) or {},
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_generations.py", "preflight", "--date", "2026-08-06"],
    )

    with pytest.raises(SystemExit) as raised:
        run_generations.main()

    assert raised.value.code == 1
    assert captured == [("2026-08-06", "all")]
    assert "No active generations" in capsys.readouterr().out


def test_run_generations_screen_mode_allows_zero_active(monkeypatch):
    from scripts import run_generations
    from tradingagents.strategies.orchestration.generation_manager import (
        GenerationManager,
    )

    monkeypatch.setattr(GenerationManager, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(
        GenerationManager,
        "run_preflight",
        lambda self, date, *, mode: {},
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_generations.py",
            "preflight",
            "--date",
            "2026-08-06",
            "--preflight-mode",
            "screen",
        ],
    )

    run_generations.main()


@pytest.mark.parametrize("command", ("run-daily", "preflight"))
def test_run_generations_busy_is_bounded_without_traceback(monkeypatch, capsys, command):
    from pathlib import Path

    from scripts import run_generations
    from tradingagents.strategies.orchestration.generation_manager import (
        GenerationManager,
    )
    from tradingagents.strategies.orchestration.runtime_lock import RuntimeLockBusy

    def busy(*args, **kwargs):
        raise RuntimeLockBusy(Path("/tmp/eventedge-runtime.lock"))

    monkeypatch.setattr(GenerationManager, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(GenerationManager, "run_daily", busy)
    monkeypatch.setattr(GenerationManager, "run_preflight", busy)
    argv = ["run_generations.py", command, "--date", "2026-08-06"]
    if command == "preflight":
        argv.extend(("--preflight-mode", "governed"))
    monkeypatch.setattr(sys, "argv", argv)

    with pytest.raises(SystemExit) as raised:
        run_generations.main()

    captured = capsys.readouterr()
    assert raised.value.code == 1
    assert '"busy": true' in captured.err
    assert '"success": false' in captured.err
    assert "Traceback" not in captured.err


def test_run_generations_constructor_lock_invalid_is_bounded_without_traceback(
    monkeypatch, capsys
):
    from scripts import run_generations
    from tradingagents.strategies.orchestration.generation_manager import (
        GenerationManager,
    )
    from tradingagents.strategies.orchestration.runtime_lock import RuntimeLockInvalid

    def invalid(*args, **kwargs):
        raise RuntimeLockInvalid("canonical runtime lock path is invalid")

    monkeypatch.setattr(GenerationManager, "__init__", invalid)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_generations.py",
            "preflight",
            "--date",
            "2026-08-06",
            "--preflight-mode",
            "governed",
        ],
    )

    with pytest.raises(SystemExit) as raised:
        run_generations.main()

    captured = capsys.readouterr()
    assert raised.value.code == 1
    assert json.loads(captured.err) == {
        "busy": False,
        "error": "canonical runtime lock path is invalid",
        "success": False,
    }
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("command", ("run-daily", "preflight"))
def test_run_generations_method_lock_invalid_is_bounded_without_traceback(
    monkeypatch, capsys, command
):
    from scripts import run_generations
    from tradingagents.strategies.orchestration.generation_manager import (
        GenerationManager,
    )
    from tradingagents.strategies.orchestration.runtime_lock import RuntimeLockInvalid

    def invalid(*args, **kwargs):
        raise RuntimeLockInvalid("runtime lock identity changed")

    monkeypatch.setattr(GenerationManager, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(GenerationManager, "run_daily", invalid)
    monkeypatch.setattr(GenerationManager, "run_preflight", invalid)
    argv = ["run_generations.py", command, "--date", "2026-08-06"]
    if command == "preflight":
        argv.extend(("--preflight-mode", "governed"))
    monkeypatch.setattr(sys, "argv", argv)

    with pytest.raises(SystemExit) as raised:
        run_generations.main()

    captured = capsys.readouterr()
    assert raised.value.code == 1
    assert json.loads(captured.err) == {
        "busy": False,
        "error": "runtime lock identity changed",
        "success": False,
    }
    assert "Traceback" not in captured.err
