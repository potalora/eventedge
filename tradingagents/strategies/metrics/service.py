"""The sole reader-facing aggregation boundary for v2 portfolio metrics."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from tradingagents.strategies.execution.models import SignalRecord
from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger

from .identity import deduplicate_signals
from .models import MetricEpoch, PairedComparison, PortfolioMetrics, SignalMetricRecord
from .portfolio import (
    daily_net_returns,
    equal_weighted_scenario_return,
    paired_comparison,
    portfolio_metrics,
    validate_snapshot_window,
)
from .store import MetricStore


_HEADLINE_BOOKS = frozenset(
    f"horizon_{horizon}_size_100k" for horizon in ("30d", "3m", "6m", "1y")
)


class MetricsService:
    """Read authoritative ledgers through an explicit, immutable cohort map."""

    def __init__(
        self,
        generation_state_dir: str | Path,
        cohort_ledgers: Mapping[str, PortfolioLedger],
        *,
        read_only: bool = False,
    ) -> None:
        bindings = dict(cohort_ledgers)
        seen_bindings: dict[str, str] = {}
        seen_databases: dict[tuple[int, int], str] = {}
        for cohort_id, ledger in bindings.items():
            if not cohort_id or ledger.cohort_id != cohort_id:
                raise ValueError(
                    "cohort binding key must exactly match ledger.cohort_id"
                )
            binding_id = ledger.recovery_binding_id()
            if not isinstance(binding_id, str) or not binding_id:
                raise ValueError("ledger recovery binding identity is invalid")
            stat = Path(ledger.path).stat()
            database_id = (stat.st_dev, stat.st_ino)
            if binding_id in seen_bindings or database_id in seen_databases:
                raise ValueError(
                    "multiple cohort IDs reference the same ledger database"
                )
            seen_bindings[binding_id] = cohort_id
            seen_databases[database_id] = cohort_id
        self.generation_state_dir = Path(generation_state_dir)
        self._cohort_ledgers = MappingProxyType(bindings)
        metric_store_path = self.generation_state_dir / "metrics_v2.sqlite3"
        self.store = (
            MetricStore.open_existing(metric_store_path)
            if read_only
            else MetricStore(metric_store_path)
        )

    @property
    def cohort_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._cohort_ledgers))

    def _epoch(self, epoch_id: str | None) -> MetricEpoch | None:
        if epoch_id is None:
            return self.store.current_epoch()
        return self.store.load_epoch(epoch_id)

    @staticmethod
    def _require_available_epoch(epoch: MetricEpoch, epoch_id: str) -> MetricEpoch:
        if epoch.epoch_id != epoch_id:
            raise ValueError("metric store returned a substituted epoch")
        if epoch.metric_schema_version != 2:
            raise ValueError(f"metric epoch {epoch_id!r} is not schema v2")
        if epoch.status == "invalid":
            raise ValueError(f"metric epoch {epoch_id!r} is invalid")
        if epoch.status not in {"open", "closed"}:
            raise ValueError(f"metric epoch {epoch_id!r} has unavailable status")
        return epoch

    @staticmethod
    def _metric_signal(row: SignalRecord) -> SignalMetricRecord:
        return SignalMetricRecord(
            event_key=row.event_key,
            signal_id=row.signal_id,
            epoch_id=row.epoch_id,
            policy_id=row.policy_id,
            strategy=row.strategy,
            ticker=row.ticker,
            direction=row.direction,
            decision_at=row.decision_at,
            reference_session=row.reference_session,
        )

    def _ledger(self, cohort_id: str) -> PortfolioLedger:
        try:
            return self._cohort_ledgers[cohort_id]
        except KeyError as error:
            raise KeyError(f"unknown cohort {cohort_id!r}") from error

    def _inputs(self, cohort_id: str, epoch_id: str):
        ledger = self._ledger(cohort_id)
        snapshots = tuple(ledger.read_snapshots(epoch_id=epoch_id))
        benchmarks = tuple(ledger.read_benchmark_observations(epoch_id=epoch_id))
        signals = tuple(ledger.read_signals(epoch_id=epoch_id))
        window = validate_snapshot_window(
            cohort_id=cohort_id,
            epoch_id=epoch_id,
            snapshots=snapshots,
        )
        deduped = deduplicate_signals(self._metric_signal(row) for row in signals)
        if deduped.conflicts:
            raise ValueError("conflicting signal identities")
        fills = tuple(
            ledger.read_fills(
                window[0].session,
                window[-1].session,
                epoch_id=epoch_id,
            )
        )
        return window, benchmarks, deduped.records, fills

    def cohort_report(self, cohort_id: str, epoch_id: str) -> PortfolioMetrics:
        epoch = self._epoch(epoch_id)
        if epoch is None:
            raise KeyError("no metric epoch is available")
        self._require_available_epoch(epoch, epoch_id)
        snapshots, benchmarks, signals, fills = self._inputs(cohort_id, epoch_id)
        return portfolio_metrics(
            cohort_id=cohort_id,
            epoch_id=epoch_id,
            snapshots=snapshots,
            benchmark_observations=benchmarks,
            signals=signals,
            fills=fills,
        )

    def generation_report(self, epoch_id: str | None = None) -> dict[str, object]:
        epoch = self._epoch(epoch_id)
        if epoch is None:
            return {
                "metric_schema_version": 2,
                "epoch": None,
                "headline_books": {},
                "scenario_panel": None,
                "scenario_panel_available": False,
                "scenario_panel_unavailable_reason": "no_current_epoch",
                "missing_headline_books": sorted(_HEADLINE_BOOKS),
                "stress_tests": {},
                "dependent_scenarios": True,
            }
        self._require_available_epoch(epoch, epoch.epoch_id)
        reports = {
            cohort_id: self.cohort_report(cohort_id, epoch.epoch_id)
            for cohort_id in self.cohort_ids
        }
        headline = {
            key: reports[key] for key in sorted(_HEADLINE_BOOKS & reports.keys())
        }
        missing = sorted(_HEADLINE_BOOKS - headline.keys())
        panel = None
        panel_unavailable_reason = "missing_headline_books" if missing else None
        headline_windows = {
            (item.start_session, item.end_session, item.valid_sessions)
            for item in headline.values()
        }
        if not missing and len(headline_windows) == 1:
            panel = {
                "label": "equal-weighted dependent $100k scenario panel",
                "dependent_scenarios": True,
                "total_return": equal_weighted_scenario_return(
                    item.total_return for item in headline.values()
                ),
            }
        elif not missing:
            panel_unavailable_reason = "mismatched_headline_windows"
        return {
            "metric_schema_version": 2,
            "epoch": asdict(epoch),
            "headline_books": {key: asdict(value) for key, value in headline.items()},
            "scenario_panel": panel,
            "scenario_panel_available": panel is not None,
            "scenario_panel_unavailable_reason": panel_unavailable_reason,
            "missing_headline_books": missing,
            "stress_tests": {
                key: asdict(value)
                for key, value in reports.items()
                if not key.endswith("_size_100k")
            },
            "dependent_scenarios": True,
        }

    def compare(
        self,
        candidate_cohort_id: str,
        candidate_epoch_id: str,
        baseline_service: "MetricsService",
        baseline_cohort_id: str,
        baseline_epoch_id: str,
    ) -> PairedComparison:
        candidate = self.cohort_report(candidate_cohort_id, candidate_epoch_id)
        baseline = baseline_service.cohort_report(baseline_cohort_id, baseline_epoch_id)
        candidate_rows = self._ledger(candidate.cohort_id).read_snapshots(
            start_session=candidate.start_session,
            end_session=candidate.end_session,
            epoch_id=candidate_epoch_id,
        )
        baseline_rows = baseline_service._ledger(baseline.cohort_id).read_snapshots(
            start_session=baseline.start_session,
            end_session=baseline.end_session,
            epoch_id=baseline_epoch_id,
        )
        return paired_comparison(
            candidate_epoch_id=candidate_epoch_id,
            baseline_epoch_id=baseline_epoch_id,
            candidate_returns=daily_net_returns(candidate_rows),
            baseline_returns=daily_net_returns(baseline_rows),
        )
