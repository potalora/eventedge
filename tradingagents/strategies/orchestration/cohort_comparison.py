"""V2 cohort comparison adapter.

Portfolio aggregation belongs exclusively to :class:`MetricsService`.
"""

from __future__ import annotations

from tradingagents.strategies.metrics.service import MetricsService


class CohortComparison:
    """Expose report-shaped views without re-aggregating ledger metrics."""

    def __init__(self, *, metrics_service: MetricsService) -> None:
        self._metrics_service = metrics_service

    def compare(self, epoch_id: str | None = None) -> dict[str, object]:
        if epoch_id is None:
            return self._metrics_service.generation_report()
        return self._metrics_service.generation_report(epoch_id=epoch_id)

    def heatmap(
        self, metric: str, epoch_id: str | None = None
    ) -> dict[str, dict[str, float | None]]:
        """Project one already-produced scalar across the 4x4 cohort matrix."""
        report = self.compare(epoch_id=epoch_id)
        books = {
            **report.get("headline_books", {}),
            **report.get("stress_tests", {}),
        }
        output: dict[str, dict[str, float | None]] = {}
        for horizon in ("30d", "3m", "6m", "1y"):
            output[horizon] = {}
            for size in ("5k", "10k", "50k", "100k"):
                cohort = books.get(f"horizon_{horizon}_size_{size}")
                if cohort is None:
                    output[horizon][size] = None
                    continue
                value = cohort.get(metric)
                if value is not None and not isinstance(value, (int, float)):
                    raise TypeError(f"heatmap metric {metric!r} is not scalar")
                output[horizon][size] = value
        return output
