"""The sole reader-facing aggregation boundary for v2 portfolio metrics."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from tradingagents.strategies.execution.models import SignalRecord
from tradingagents.strategies.state.portfolio_ledger import (
    LedgerConflictError,
    PortfolioLedger,
)

from .identity import deduplicate_signals
from .models import MetricEpoch, PairedComparison, PortfolioMetrics, SignalMetricRecord
from .outcomes import directional_accuracy
from .portfolio import (
    daily_net_returns,
    equal_weighted_scenario_return,
    matched_benchmark_returns,
    paired_comparison,
    portfolio_metrics,
    reconcile_costs,
    validate_snapshot_series,
    validate_snapshot_window,
)
from .store import MetricStore


_HEADLINE_BOOKS = frozenset(
    f"horizon_{horizon}_size_100k" for horizon in ("30d", "3m", "6m", "1y")
)
_STRESS_BOOKS = frozenset(
    f"horizon_{horizon}_size_{size}"
    for horizon in ("30d", "3m", "6m", "1y")
    for size in ("5k", "10k", "50k")
)
_SCENARIO_BOOKS = _HEADLINE_BOOKS | _STRESS_BOOKS
_POLICY_AUDIT_MAX_RECORDS = 4096
_CANDIDATE_RECOVERY_REPORT_LIMIT = 1_000
_POLICY_AUDIT_DECISIONS = frozenset({"accepted", "trimmed", "rejected"})


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

    def _inputs(
        self,
        cohort_id: str,
        epoch_id: str,
        *,
        allow_insufficient: bool = False,
    ):
        ledger = self._ledger(cohort_id)
        connection = ledger.connection
        owns_snapshot = not connection.in_transaction
        if owns_snapshot:
            connection.execute("BEGIN")
        try:
            snapshots = tuple(ledger.read_snapshots(epoch_id=epoch_id))
            benchmarks = tuple(ledger.read_benchmark_observations(epoch_id=epoch_id))
            signals = tuple(ledger.read_signals(epoch_id=epoch_id))
            window = (
                validate_snapshot_series(
                    cohort_id=cohort_id,
                    epoch_id=epoch_id,
                    snapshots=snapshots,
                )
                if allow_insufficient
                else validate_snapshot_window(
                    cohort_id=cohort_id,
                    epoch_id=epoch_id,
                    snapshots=snapshots,
                )
            )
            deduped = deduplicate_signals(self._metric_signal(row) for row in signals)
            if deduped.conflicts:
                raise ValueError("conflicting signal identities")
            fills = (
                tuple(
                    ledger.read_fills(
                        window[0].session,
                        window[-1].session,
                        epoch_id=epoch_id,
                    )
                )
                if window
                else ()
            )
        finally:
            if owns_snapshot:
                connection.execute("ROLLBACK")
        return window, benchmarks, deduped.records, fills

    @staticmethod
    def _assert_epoch_unchanged(before: MetricEpoch, after: MetricEpoch) -> None:
        if before != after:
            raise RuntimeError(
                f"metric epoch {before.epoch_id!r} changed while report was built"
            )

    @staticmethod
    def _portfolio_from_inputs(
        cohort_id: str, epoch_id: str, inputs: tuple
    ) -> PortfolioMetrics:
        snapshots, benchmarks, signals, fills = inputs
        return portfolio_metrics(
            cohort_id=cohort_id,
            epoch_id=epoch_id,
            snapshots=snapshots,
            benchmark_observations=benchmarks,
            signals=signals,
            fills=fills,
        )

    @staticmethod
    def _directional_accuracy_5d(signals: tuple, outcomes: tuple) -> float | None:
        signal_ids = {row.signal_id for row in signals}
        summary = directional_accuracy(
            row
            for row in outcomes
            if row.holding_sessions == 5 and row.signal_id in signal_ids
        )
        return summary.rate

    def cohort_report(self, cohort_id: str, epoch_id: str) -> PortfolioMetrics:
        epoch = self._epoch(epoch_id)
        if epoch is None:
            raise KeyError("no metric epoch is available")
        self._require_available_epoch(epoch, epoch_id)
        report = self._portfolio_from_inputs(
            cohort_id,
            epoch_id,
            self._inputs(cohort_id, epoch_id),
        )
        self._assert_epoch_unchanged(epoch, self.store.load_epoch(epoch_id))
        return report

    @staticmethod
    def _unavailable_policy_audit(
        *, epoch_id: str, status: str
    ) -> dict[str, object]:
        """Keep audit failures explicit rather than projecting zero activity."""
        return {
            "available": False,
            "status": status,
            "metric_epoch_id": epoch_id,
            "policy_version": None,
            "signals_examined": None,
            "policy_candidate_decisions_examined": None,
            "max_records": _POLICY_AUDIT_MAX_RECORDS,
            "counts": None,
            "reason_code_counts": None,
        }

    def _policy_audit_for_cohort(
        self, cohort_id: str, epoch_id: str
    ) -> dict[str, object]:
        """Build one bounded, tamper-evident policy-governance projection.

        Recommendation decisions are counted once from immutable candidate
        rows; signal companions supply ingress blocks and committee coverage.
        It is deliberately not a financial-performance metric.
        """
        ledger = self._ledger(cohort_id)
        connection = ledger.connection
        owns_snapshot = not connection.in_transaction
        if owns_snapshot:
            connection.execute("BEGIN")
        try:
            rows = connection.execute(
                """SELECT signal_id, reference_session, policy_id
                   FROM signals WHERE epoch_id = ?
                   ORDER BY reference_session, signal_id LIMIT ?""",
                (epoch_id, _POLICY_AUDIT_MAX_RECORDS + 1),
            ).fetchall()
            if len(rows) > _POLICY_AUDIT_MAX_RECORDS:
                return self._unavailable_policy_audit(
                    epoch_id=epoch_id, status="projection_limit_exceeded"
                )
            staging_rows = connection.execute(
                """SELECT r.session, r.policy_id, r.epoch_id AS staging_epoch_id,
                          b.epoch_id AS binding_epoch_id, b.policy_version,
                          b.context_digest
                   FROM staging_runs r
                   JOIN policy_session_contexts b
                     ON b.cohort_id = r.cohort_id AND b.session = r.session
                    AND b.binding_kind = 'staging'
                   WHERE r.cohort_id = ? AND r.epoch_id = ?
                   ORDER BY r.session, r.policy_id LIMIT ?""",
                (cohort_id, epoch_id, _POLICY_AUDIT_MAX_RECORDS + 1),
            ).fetchall()
            if len(staging_rows) > _POLICY_AUDIT_MAX_RECORDS:
                return self._unavailable_policy_audit(
                    epoch_id=epoch_id, status="projection_limit_exceeded"
                )
            if any(row["binding_epoch_id"] != epoch_id for row in staging_rows):
                return self._unavailable_policy_audit(
                    epoch_id=epoch_id, status="provenance_read_failed"
                )
            policy_staging_keys = {
                (date.fromisoformat(str(row["session"])), str(row["policy_id"]))
                for row in staging_rows
            }
            if not rows and not policy_staging_keys:
                return self._unavailable_policy_audit(
                    epoch_id=epoch_id, status="no_policy_provenance"
                )
            signal_staging_keys = {
                (date.fromisoformat(str(row["reference_session"])), str(row["policy_id"]))
                for row in rows
            }
            if any(
                not ledger.staging_completed(session, epoch_id, policy_id)
                for session, policy_id in signal_staging_keys
            ):
                return self._unavailable_policy_audit(
                    epoch_id=epoch_id, status="incomplete_staging"
                )
            provenances: list[dict[str, object]] = []
            for row in rows:
                try:
                    provenance = ledger.read_signal_policy_provenance(
                        str(row["signal_id"])
                    )
                except (LedgerConflictError, ValueError, TypeError):
                    return self._unavailable_policy_audit(
                        epoch_id=epoch_id, status="provenance_read_failed"
                    )
                if provenance is None:
                    return self._unavailable_policy_audit(
                        epoch_id=epoch_id, status="missing_policy_provenance"
                    )
                provenances.append(provenance)

            candidate_decisions = ledger.read_policy_candidate_decisions(
                epoch_id=epoch_id,
                limit=4096,
            )
            decisions = {str(provenance["decision"]) for provenance in provenances}
            if not decisions <= _POLICY_AUDIT_DECISIONS:
                return self._unavailable_policy_audit(
                    epoch_id=epoch_id, status="invalid_policy_decision"
                )
            manifests = ledger.read_policy_staging_audit_manifests(
                epoch_id=epoch_id,
                limit=_POLICY_AUDIT_MAX_RECORDS,
            )
            manifest_keys = {
                (manifest["session"], str(manifest["policy_id"]))
                for manifest in manifests
            }
            if manifest_keys != policy_staging_keys:
                return self._unavailable_policy_audit(
                    epoch_id=epoch_id, status="missing_policy_audit_manifest"
                )
            policy_versions = {
                *(str(provenance["policy_version"]) for provenance in provenances),
                *(str(decision["policy_version"]) for decision in candidate_decisions),
                *(str(manifest["policy_version"]) for manifest in manifests),
                *(str(row["policy_version"]) for row in staging_rows),
            }
            if len(policy_versions) != 1:
                return self._unavailable_policy_audit(
                    epoch_id=epoch_id, status="mixed_policy_versions"
                )
            manifest_decision_ids = {
                str(decision_id)
                for manifest in manifests
                for decision_id in manifest["candidate_decision_ids"]
            }
            if manifest_decision_ids != {
                str(decision["decision_id"]) for decision in candidate_decisions
            }:
                return self._unavailable_policy_audit(
                    epoch_id=epoch_id, status="policy_audit_partition_mismatch"
                )

            counts = {
                "accepted": 0,
                "trimmed": 0,
                "rejected": 0,
                "journal_only": 0,
                "consumed_event_blocks": 0,
                "cutoff_late": 0,
                "committee_not_selected": 0,
            }
            reason_code_counts: dict[str, int] = {}
            candidate_signal_ids: set[str] = set()
            for candidate in candidate_decisions:
                decision = str(candidate["decision"])
                if decision not in _POLICY_AUDIT_DECISIONS:
                    return self._unavailable_policy_audit(
                        epoch_id=epoch_id, status="invalid_policy_decision"
                    )
                counts[decision] += 1
                candidate_signal_ids.update(
                    str(signal_id) for signal_id in candidate["signal_ids"]
                )
                for reason in candidate["reason_codes"]:
                    code = str(reason)
                    reason_code_counts[code] = reason_code_counts.get(code, 0) + 1

            ingress_eligible_signal_ids: set[str] = set()
            for provenance in provenances:
                if bool(provenance["journal_only"]):
                    counts["journal_only"] += 1
                reasons = tuple(str(code) for code in provenance["reason_codes"])
                if "consumed_event" in reasons:
                    counts["consumed_event_blocks"] += 1
                if "cutoff_late" in reasons:
                    counts["cutoff_late"] += 1
                if bool(provenance["order_eligible"]):
                    ingress_eligible_signal_ids.add(str(provenance["signal_id"]))
                else:
                    for reason in reasons:
                        reason_code_counts[reason] = (
                            reason_code_counts.get(reason, 0) + 1
                        )
            if not candidate_signal_ids <= ingress_eligible_signal_ids:
                return self._unavailable_policy_audit(
                    epoch_id=epoch_id, status="candidate_signal_mismatch"
                )
            manifest_ingress = {
                str(signal_id)
                for manifest in manifests
                for signal_id in manifest["ingress_signal_ids"]
            }
            manifest_nonselected = {
                str(signal_id)
                for manifest in manifests
                for signal_id in manifest["committee_not_selected_ids"]
            }
            if (
                manifest_ingress != ingress_eligible_signal_ids
                or manifest_nonselected
                != ingress_eligible_signal_ids - candidate_signal_ids
            ):
                return self._unavailable_policy_audit(
                    epoch_id=epoch_id, status="policy_audit_partition_mismatch"
                )
            counts["committee_not_selected"] = len(manifest_nonselected)
            return {
                "available": True,
                "status": "available",
                "metric_epoch_id": epoch_id,
                "policy_version": next(iter(policy_versions)),
                "signals_examined": len(provenances),
                "policy_candidate_decisions_examined": len(candidate_decisions),
                "max_records": _POLICY_AUDIT_MAX_RECORDS,
                "counts": counts,
                "reason_code_counts": dict(sorted(reason_code_counts.items())),
            }
        except (sqlite3.Error, LedgerConflictError, ValueError, TypeError):
            return self._unavailable_policy_audit(
                epoch_id=epoch_id, status="provenance_read_failed"
            )
        finally:
            if owns_snapshot:
                connection.execute("ROLLBACK")

    def generation_report(self, epoch_id: str | None = None) -> dict[str, object]:
        unexpected = sorted(set(self.cohort_ids) - _SCENARIO_BOOKS)
        if unexpected:
            raise ValueError(
                "unexpected scenario cohort bindings: " + ", ".join(unexpected)
            )
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
                "cohort_series": {},
                "candidate_bar_recoveries": [],
                "candidate_bar_recovery_scope": {
                    "total_records": 0,
                    "returned_records": 0,
                    "truncated": False,
                    "order": "newest_first",
                },
                "dependent_scenarios": True,
                "policy_audit": {
                    "aggregation_prohibited": True,
                    "aggregate": None,
                    "per_cohort": {},
                },
            }
        self._require_available_epoch(epoch, epoch.epoch_id)
        outcomes = self.store.read_outcomes(epoch.epoch_id)
        materialized = {
            cohort_id: self._materialize_cohort(
                cohort_id, epoch.epoch_id, outcomes
            )
            for cohort_id in self.cohort_ids
        }
        reports = {
            key: self._book_payload(value[0]) for key, value in materialized.items()
        }
        series = {key: value[1] for key, value in materialized.items()}
        policy_audit = {
            cohort_id: self._policy_audit_for_cohort(cohort_id, epoch.epoch_id)
            for cohort_id in self.cohort_ids
        }
        candidate_records, candidate_total = (
            self.store.read_candidate_bar_recovery_window(
                epoch.epoch_id, limit=_CANDIDATE_RECOVERY_REPORT_LIMIT
            )
        )
        candidate_bar_recoveries = [asdict(record) for record in candidate_records]
        candidate_bar_recovery_scope = {
            "total_records": candidate_total,
            "returned_records": len(candidate_records),
            "truncated": candidate_total > len(candidate_records),
            "order": "newest_first",
        }
        final_epoch = self.store.load_epoch(epoch.epoch_id)
        self._assert_epoch_unchanged(epoch, final_epoch)
        if epoch_id is None:
            final_current = self.store.current_epoch()
            if final_current is None:
                raise RuntimeError(
                    "current metric epoch disappeared while report was built"
                )
            self._assert_epoch_unchanged(epoch, final_current)
        headline = {
            key: reports[key] for key in sorted(_HEADLINE_BOOKS & reports.keys())
        }
        missing = sorted(_HEADLINE_BOOKS - headline.keys())
        panel = None
        panel_unavailable_reason = "missing_headline_books" if missing else None
        headline_windows = {
            (item["start_session"], item["end_session"], item["valid_sessions"])
            for item in headline.values()
            if item.get("metrics_available", True)
        }
        unavailable_headline = any(
            not item.get("metrics_available", True) for item in headline.values()
        )
        if not missing and unavailable_headline:
            panel_unavailable_reason = "insufficient_history"
        elif not missing and len(headline_windows) == 1:
            panel = {
                "label": "equal-weighted dependent $100k scenario panel",
                "dependent_scenarios": True,
                "total_return": equal_weighted_scenario_return(
                    item["total_return"] for item in headline.values()
                ),
            }
        elif not missing:
            panel_unavailable_reason = "mismatched_headline_windows"
        return {
            "metric_schema_version": 2,
            "epoch": asdict(epoch),
            "headline_books": headline,
            "scenario_panel": panel,
            "scenario_panel_available": panel is not None,
            "scenario_panel_unavailable_reason": panel_unavailable_reason,
            "missing_headline_books": missing,
            "stress_tests": {
                key: value
                for key, value in reports.items()
                if key in _STRESS_BOOKS
            },
            # These are projections of the immutable ledger, not dashboard-side
            # calculations.  Keep the raw persisted observations available so all
            # reporting surfaces use the same valuation and benchmark evidence.
            "cohort_series": series,
            # Persisted candidate staging evidence only.  This reports recovery
            # and quarantine without synthesizing portfolio performance.
            "candidate_bar_recoveries": candidate_bar_recoveries,
            "candidate_bar_recovery_scope": candidate_bar_recovery_scope,
            "dependent_scenarios": True,
            # Policy audit evidence is per dependent scenario only.  Never
            # aggregate it into a cross-cohort event count or alpha statistic.
            "policy_audit": {
                "aggregation_prohibited": True,
                "aggregate": None,
                "per_cohort": policy_audit,
            },
        }

    def _materialize_cohort(
        self, cohort_id: str, epoch_id: str, outcomes: tuple = ()
    ) -> tuple[PortfolioMetrics | dict[str, object], dict[str, object]]:
        """Build metrics and reporting series from one SQLite snapshot."""
        inputs = self._inputs(cohort_id, epoch_id, allow_insufficient=True)
        series = self._cohort_series_from_inputs(inputs)
        if len(inputs[0]) < 2:
            return self._insufficient_book(cohort_id, epoch_id, inputs, outcomes), series
        report = self._book_payload(
            self._portfolio_from_inputs(cohort_id, epoch_id, inputs)
        )
        report["directional_accuracy_5d"] = self._directional_accuracy_5d(
            inputs[2], outcomes
        )
        return (
            report,
            series,
        )

    @staticmethod
    def _book_payload(report: PortfolioMetrics | dict[str, object]) -> dict[str, object]:
        if isinstance(report, PortfolioMetrics):
            return {
                **asdict(report),
                "metrics_available": True,
                "unavailable_reason": None,
            }
        return report

    @staticmethod
    def _insufficient_book(
        cohort_id: str,
        epoch_id: str,
        inputs: tuple,
        outcomes: tuple,
    ) -> dict[str, object]:
        snapshots, benchmarks, signals, fills = inputs
        latest = snapshots[-1] if snapshots else None
        latest_benchmarks = (
            [
                row.observed_at
                for row in benchmarks
                if latest is not None
                and row.session == latest.session
                and row.symbol in {"SPY", "BIL"}
                and row.valid
            ]
            if latest is not None
            else []
        )
        if latest is not None:
            reconcile_costs(latest)
            equity = float(latest.net_equity)
            costs = {
                "slippage": float(latest.slippage_cost),
                "commission": float(latest.commission_cost),
                "other_fees": float(latest.other_fees),
                "borrow": float(latest.borrow_cost),
                "financing": float(latest.financing_cost),
            }
        else:
            equity = 0.0
            costs = {
                "slippage": 0.0,
                "commission": 0.0,
                "other_fees": 0.0,
                "borrow": 0.0,
                "financing": 0.0,
            }
        unique_fills = {row.fill_id: row for row in fills}
        return {
            "cohort_id": cohort_id,
            "epoch_id": epoch_id,
            "metric_schema_version": 2,
            "metrics_available": False,
            "unavailable_reason": "insufficient_history",
            "start_session": snapshots[0].session if snapshots else None,
            "end_session": latest.session if latest is not None else None,
            "valuation_at": latest.valuation_at if latest is not None else None,
            "benchmark_at": max(latest_benchmarks) if latest_benchmarks else None,
            "valid_sessions": len(snapshots),
            "total_return": None,
            "gross_return": None,
            "matched_benchmark_return": None,
            "matched_excess_return": None,
            "annualized_daily_net_sharpe": None,
            "sharpe_return_count": 0,
            "annualized_matched_information_ratio": None,
            "information_ratio_return_count": 0,
            "max_drawdown": None,
            "long_weight": (
                float(latest.long_market_value) / equity if equity else None
            ),
            "short_weight": (
                float(latest.short_liability) / equity if equity else None
            ),
            "gross_weight": float(latest.gross_exposure) / equity if equity else None,
            "net_weight": float(latest.net_exposure) / equity if equity else None,
            "cash_weight": float(latest.cash) / equity if equity else None,
            "cumulative_costs": costs,
            "unique_catalysts": len({row.event_key for row in signals}),
            "strategy_decisions": len({row.signal_id for row in signals}),
            "fills": len(unique_fills),
            "closed_trades": len(
                {
                    row.intent_id
                    for row in unique_fills.values()
                    if row.side in {"sell", "cover"}
                }
            ),
            "missing_mark_count": 0,
            "stale_mark_count": 0,
            "directional_accuracy_5d": MetricsService._directional_accuracy_5d(
                signals, outcomes
            ),
        }

    @staticmethod
    def _cohort_series_from_inputs(inputs: tuple) -> dict[str, object]:
        """Serialize one valid ledger window and its persisted benchmarks."""
        snapshots, benchmarks, _signals, _fills = inputs
        if not snapshots:
            return {
                "net_equity_history": [],
                "benchmarks": {"SPY": [], "BIL": []},
                "matched_benchmark_returns": [],
            }
        benchmark_rows = tuple(
            row for row in benchmarks if row.valid and row.symbol in {"SPY", "BIL"}
        )
        by_symbol = {
            symbol: [
                {
                    "session": row.session.isoformat(),
                    "close": float(row.close),
                    "observed_at": row.observed_at.isoformat(),
                    "return_basis": row.return_basis,
                    "source": row.source,
                }
                for row in benchmark_rows
                if row.symbol == symbol
            ]
            for symbol in ("SPY", "BIL")
        }
        matched = matched_benchmark_returns(snapshots, benchmark_rows)
        first_equity = float(snapshots[0].net_equity)
        return {
            "net_equity_history": [
                {
                    "session": row.session.isoformat(),
                    "valuation_at": row.valuation_at.isoformat(),
                    "net_equity": float(row.net_equity),
                    "gross_equity": float(row.gross_equity),
                    "gross_exposure": float(row.gross_exposure),
                    "net_exposure": float(row.net_exposure),
                    "cash": float(row.cash),
                    "cumulative_costs": {
                        "slippage": float(row.slippage_cost),
                        "commission": float(row.commission_cost),
                        "other_fees": float(row.other_fees),
                        "borrow": float(row.borrow_cost),
                        "financing": float(row.financing_cost),
                    },
                    "total_return": float(row.net_equity) / first_equity - 1.0,
                }
                for row in snapshots
            ],
            "benchmarks": by_symbol,
            "matched_benchmark_returns": [
                {"session": row.session.isoformat(), "return": row.value}
                for row in matched
            ],
        }

    def compare(
        self,
        candidate_cohort_id: str,
        candidate_epoch_id: str,
        baseline_service: "MetricsService",
        baseline_cohort_id: str,
        baseline_epoch_id: str,
    ) -> PairedComparison:
        candidate_epoch = self._epoch(candidate_epoch_id)
        baseline_epoch = baseline_service._epoch(baseline_epoch_id)
        if candidate_epoch is None or baseline_epoch is None:
            raise KeyError("comparison metric epoch is unavailable")
        self._require_available_epoch(candidate_epoch, candidate_epoch_id)
        baseline_service._require_available_epoch(baseline_epoch, baseline_epoch_id)
        candidate_inputs = self._inputs(candidate_cohort_id, candidate_epoch_id)
        baseline_inputs = baseline_service._inputs(
            baseline_cohort_id, baseline_epoch_id
        )
        self._portfolio_from_inputs(
            candidate_cohort_id, candidate_epoch_id, candidate_inputs
        )
        baseline_service._portfolio_from_inputs(
            baseline_cohort_id, baseline_epoch_id, baseline_inputs
        )
        self._assert_epoch_unchanged(
            candidate_epoch, self.store.load_epoch(candidate_epoch_id)
        )
        baseline_service._assert_epoch_unchanged(
            baseline_epoch, baseline_service.store.load_epoch(baseline_epoch_id)
        )
        return paired_comparison(
            candidate_epoch_id=candidate_epoch_id,
            baseline_epoch_id=baseline_epoch_id,
            candidate_returns=daily_net_returns(candidate_inputs[0]),
            baseline_returns=daily_net_returns(baseline_inputs[0]),
        )
