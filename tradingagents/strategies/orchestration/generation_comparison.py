"""Explicit v2 paired generation comparisons."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Mapping

from tradingagents.strategies.metrics.service import MetricsService


@dataclass(frozen=True)
class ComparisonPair:
    candidate_generation_id: str
    candidate_cohort_id: str
    candidate_epoch_id: str
    baseline_generation_id: str
    baseline_cohort_id: str
    baseline_epoch_id: str

    def __post_init__(self) -> None:
        if any(not value.strip() for value in self.__dict__.values()):
            raise ValueError("comparison pair identifiers must be non-empty")


class GenerationComparison:
    """Delegate every selected pair to its generation's MetricsService."""

    def __init__(self, services: Mapping[str, MetricsService]) -> None:
        self._services = MappingProxyType(dict(services))

    def compare(self, pairs: tuple[ComparisonPair, ...]) -> dict[str, object]:
        comparisons = []
        for pair in pairs:
            try:
                candidate = self._services[pair.candidate_generation_id]
                baseline = self._services[pair.baseline_generation_id]
            except KeyError as error:
                raise KeyError(
                    "comparison references an unknown generation service"
                ) from error
            comparisons.append(
                asdict(
                    candidate.compare(
                        pair.candidate_cohort_id,
                        pair.candidate_epoch_id,
                        baseline,
                        pair.baseline_cohort_id,
                        pair.baseline_epoch_id,
                    )
                )
            )
        return {"metric_schema_version": 2, "comparisons": comparisons}
