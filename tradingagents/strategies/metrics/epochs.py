from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
import hashlib
import json

from .calendar import XNYSCalendar
from .models import METRIC_SCHEMA_VERSION, MetricEpoch
from .store import MetricStore


@dataclass(frozen=True)
class EpochContext:
    generation_id: str
    generation_commit: str
    behavior_hash: str
    config_hash: str
    execution_clock_version: str
    pricing_version: str
    cost_model_version: str


def _semantic_hash(context: EpochContext) -> str:
    payload = {
        **asdict(context),
        "metric_schema_version": METRIC_SCHEMA_VERSION,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _matches_context(epoch: MetricEpoch, context: EpochContext) -> bool:
    return (
        epoch.generation_id == context.generation_id
        and epoch.generation_commit == context.generation_commit
        and epoch.behavior_hash == context.behavior_hash
        and epoch.config_hash == context.config_hash
        and epoch.metric_schema_version == METRIC_SCHEMA_VERSION
        and epoch.execution_clock_version == context.execution_clock_version
        and epoch.pricing_version == context.pricing_version
        and epoch.cost_model_version == context.cost_model_version
    )


class EpochManager:
    def __init__(
        self,
        store: MetricStore,
        calendar: XNYSCalendar | None = None,
    ) -> None:
        self.store = store
        self.calendar = calendar or XNYSCalendar()

    def _validate_session(self, session: date) -> None:
        if not self.calendar.is_session(session):
            raise ValueError(f"{session} is not an XNYS session")

    def ensure_epoch(
        self, context: EpochContext, session: date
    ) -> MetricEpoch:
        self._validate_session(session)
        semantic_hash = _semantic_hash(context)
        current = self.store.current_epoch()
        if current is not None and session < current.start_session:
            raise ValueError(
                f"{session} precedes current epoch start {current.start_session}"
            )
        if (
            current is not None
            and current.status == "invalid"
            and current.end_session == session
        ):
            if _matches_context(current, context):
                return current
            raise ValueError("invalidated session context conflict")
        if (
            current is not None
            and current.status == "invalid"
            and current.end_session is not None
            and session < current.end_session
        ):
            raise ValueError(
                f"{session} precedes invalid epoch end {current.end_session}"
            )
        if (
            current is not None
            and current.status == "open"
            and _matches_context(current, context)
        ):
            return current
        if current is not None and current.status == "open":
            self.store.close_epoch(
                current.epoch_id,
                self.calendar.previous_session(session),
                "semantic_hash_changed",
            )
        epoch = MetricEpoch(
            epoch_id=f"{context.generation_id}-{session}-{semantic_hash[:16]}",
            generation_id=context.generation_id,
            generation_commit=context.generation_commit,
            behavior_hash=context.behavior_hash,
            config_hash=context.config_hash,
            metric_schema_version=METRIC_SCHEMA_VERSION,
            execution_clock_version=context.execution_clock_version,
            pricing_version=context.pricing_version,
            cost_model_version=context.cost_model_version,
            start_session=session,
            end_session=None,
            status="open",
            boundary_reason=(
                "initial" if current is None else "semantic_hash_changed"
            ),
        )
        self.store.save_epoch(epoch)
        return epoch

    def invalidate_current(
        self, session: date, reason: str
    ) -> MetricEpoch:
        self._validate_session(session)
        current = self.store.current_epoch()
        if current is None:
            raise RuntimeError("no open metric epoch")
        if (
            current.status == "invalid"
            and current.end_session == session
            and current.boundary_reason == reason
        ):
            return current
        if current.status != "open":
            raise RuntimeError("no open metric epoch")
        return self.store.invalidate_epoch(current.epoch_id, session, reason)
