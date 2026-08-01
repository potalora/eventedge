# Metrics and Promotion Governance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build one versioned P2 metric source for XNYS-session outcomes and portfolio performance, then enforce the P3 production learning lock and read-only promotion gates.

**Architecture:** A new `tradingagents.strategies.metrics` package reads the authoritative P0 SQLite ledgers and owns calendar semantics, identities, epochs, outcomes, portfolio/benchmark calculations, strategy-health evidence, and promotion decisions. Daily execution writes metric inputs and derived v2 records once; dashboards, reports, and comparisons only read the resulting metric service, so they neither reimplement formulas nor fetch live benchmark data.

**Tech Stack:** Python 3.10+, `exchange-calendars>=4.13.2,<5`, pandas, SQLite, `Decimal`, dataclasses, pytest, existing P0 `PortfolioLedger`.

## Global Constraints

- Implement this plan only after the P0 execution-ledger plan supplies `tradingagents/strategies/execution/models.py`, `tradingagents/strategies/state/portfolio_ledger.py`, and `tradingagents/strategies/orchestration/session_executor.py`.
- Use `exchange-calendars>=4.13.2,<5` and `exchange_calendars.get_calendar("XNYS")`; do not maintain a holiday list by hand.
- Production remains paper-trading only.
- Automated learning remains disabled and cannot be enabled by configuration or environment variables.
- A promotion decision is advisory and requires Pedro's manual approval.
- Existing generation history is never rewritten.
- `gen_003` remains immutable, schema `1_legacy_calendar_signed`, observation-only, and promotion-ineligible.
- A missing or stale price invalidates the affected valuation session; no entry-price or nearest-price fallback is allowed.
- The 16 cohorts are dependent scenario portfolios, not independent observations or one combined fund.
- Behavioral, execution-clock, pricing, cost, configuration, model, or metric-schema changes require a fresh metric epoch.
- P2/P3 metric code reads authoritative P0 ledger records, never `paper_trades.json` or `equity_snapshots.jsonl`.
- Reports and dashboards perform no live benchmark or position-price fetches.
- The headline contains four separate `$100k` horizon books plus an explicitly labeled equal-weighted scenario panel; it never sums scenario capital into fund AUM.
- The `$5k`, `$10k`, and `$50k` cohorts remain concentration stress tests in an appendix/heatmap.
- Promotion results are limited to `WAIT`, `FAIL`, and `ELIGIBLE_FOR_MANUAL_REVIEW`; evaluation cannot mutate code, config, generation state, or deployment state.
- All 12 strategies must be classified every session as `signals`, `legitimate_no_event`, `data_failure`, or `strategy_defect`.
- Unit tests are deterministic and API/LLM-free.
- Measure wall time, peak RSS, and API/LLM-call counts before and after; peak RSS must remain well below 8 GB on a 16 GB M4 MacBook Air.
- Do not deploy, patch a live generation, create `gen_004`, merge, or change `trade.timer` while implementing this plan.

---

## Prerequisite P0 Interfaces

The P0 plan must land these exact read contracts before Task 3 begins:

```python
PortfolioLedger.read_snapshots(
    start_session: date | None = None,
    end_session: date | None = None,
    epoch_id: str | None = None,
    valid_only: bool = False,
) -> list[AccountSnapshot]

PortfolioLedger.read_benchmark_observations(
    start_session: date | None = None,
    end_session: date | None = None,
    epoch_id: str | None = None,
) -> list[BenchmarkObservation]

PortfolioLedger.read_signals(
    start_session: date | None = None,
    end_session: date | None = None,
    epoch_id: str | None = None,
) -> list[SignalRecord]

PortfolioLedger.read_fills(
    start_session: date | None = None,
    end_session: date | None = None,
    epoch_id: str | None = None,
) -> list[Fill]
```

`SignalRecord` must persist `epoch_id` and `policy_id`, and its stable ID must be:

```python
signal_id = stable_id(
    "signal",
    epoch_id,
    strategy,
    policy_id,
    direction,
    event_key,
)
```

`AccountSnapshot` must expose `snapshot_id`, `cohort_id`, `epoch_id`, `session`,
`valuation_at`, `cash`, `long_market_value`, `short_liability`,
`gross_exposure`, `net_exposure`, `realized_pnl`, `unrealized_pnl`,
`slippage_cost`, `commission_cost`, `other_fees`, `borrow_cost`,
`financing_cost`, `gross_equity`, `net_equity`, `high_water_mark`, `valid`,
and `invalid_reason`.

`BenchmarkObservation` must expose `epoch_id`, `symbol`, `session`, `close`,
`return_basis`, `observed_at`, `source`, `valid`, and `invalid_reason`. P0
persists total-return-adjusted `SPY` and `BIL` observations with
`return_basis="total_return_adjusted"`; `BIL` is the initial cash proxy.

## File Map

- Modify `pyproject.toml` — add the bounded exchange-calendar dependency.
- Create `tradingagents/strategies/metrics/__init__.py` — public P2/P3 exports.
- Create `tradingagents/strategies/metrics/calendar.py` — XNYS session adapter.
- Create `tradingagents/strategies/metrics/models.py` — immutable v2 metric records.
- Create `tradingagents/strategies/metrics/identity.py` — stable identities, deduplication, and conflict detection.
- Create `tradingagents/strategies/metrics/store.py` — derived v2 SQLite records beside generation state.
- Create `tradingagents/strategies/metrics/epochs.py` — semantic hashes and epoch lifecycle.
- Create `tradingagents/strategies/metrics/outcomes.py` — 5/10/20/30-session outcome tracker and directional accuracy.
- Create `tradingagents/strategies/metrics/portfolio.py` — net/gross returns,
  cash-excess Sharpe, matched-benchmark information ratio, drawdown, and
  common-window comparisons.
- Create `tradingagents/strategies/metrics/health.py` — exhaustive strategy-session classification.
- Create `tradingagents/strategies/metrics/service.py` — sole reader-facing aggregation service.
- Create `tradingagents/strategies/metrics/promotion.py` — pure advisory gates.
- Create `tradingagents/strategies/orchestration/learning_policy.py` — production learning fail-closed policy.
- Create `scripts/migrate_metrics_v2.py` — dry-run-first legacy registry builder.
- Modify `tradingagents/strategies/orchestration/generation_manager.py` — pass generation identity and refuse learning.
- Modify `tradingagents/strategies/orchestration/cohort_orchestrator.py` — require disabled learning and forward health/epoch context.
- Modify `tradingagents/strategies/orchestration/multi_strategy_engine.py` — emit complete strategy-health results and remove production outcome/metric formulas.
- Modify `tradingagents/strategies/orchestration/session_executor.py` — create/validate epochs and process due outcomes with the shared raw bar cache.
- Modify `tradingagents/strategies/orchestration/cohort_comparison.py` — delegate to `MetricsService`.
- Modify `tradingagents/strategies/orchestration/generation_comparison.py` — paired common-session comparison only.
- Modify `scripts/run_cohorts.py` — reject `--learning` and pass generation metadata.
- Modify `scripts/run_generations.py` — add read-only `promotion-status`; reject `run-learning`.
- Modify `scripts/generate_daily_report.py` — render stored v2 metrics only.
- Modify `tradingagents/dashboard/data_loaders.py` — load v2 metric reports; remove live position pricing.
- Modify `tradingagents/dashboard/charts.py` — chart valid net-equity series and scenario panels.
- Modify `tradingagents/dashboard/pages/overview.py` — show epoch, data quality, counts, and scenario disclosure.
- Modify `tradingagents/dashboard/pages/returns.py` — show four `$100k` books,
  annualized daily net Sharpe, and matched-benchmark information ratio.
- Modify `tradingagents/dashboard/pages/cohort_matrix.py` — label non-`$100k` cohorts as stress tests.
- Modify `tradingagents/dashboard/email_export.py` — delete live benchmark fetch and render persisted SPY/BIL observations.
- Modify `README.md`, `AUTORESEARCH_ARCHITECTURE_MAP.md`, `assets/autoresearch.svg`, and `assets/daily-cycle.svg` — truthful v2 metrics, learning-disabled, scenario-panel copy.
- Create `tests/test_metric_calendar.py`.
- Create `tests/test_metric_identity.py`.
- Create `tests/test_metric_epochs.py`.
- Create `tests/test_outcome_metrics_v2.py`.
- Create `tests/test_portfolio_metrics_v2.py`.
- Create `tests/test_metrics_service.py`.
- Create `tests/test_strategy_health.py`.
- Create `tests/test_learning_disabled.py`.
- Create `tests/test_promotion_gates.py`.
- Create `tests/test_metrics_reporting.py`.
- Create `tests/test_metrics_migration.py`.
- Modify `tests/test_cohort_lifecycle.py`, `tests/test_multi_strategy.py`, `tests/test_generation_manager.py`, and `tests/test_30day_simulation.py` — remove assertions that preserve legacy calendar outcomes, trade-P&L Sharpe, or enabled production learning.

---

### Task 1: XNYS Calendar Contract

**Files:**
- Modify: `pyproject.toml`
- Create: `tradingagents/strategies/metrics/__init__.py`
- Create: `tradingagents/strategies/metrics/calendar.py`
- Test: `tests/test_metric_calendar.py`

**Interfaces:**
- Produces: `XNYSCalendar.is_session(session: date) -> bool`
- Produces: `XNYSCalendar.next_session(session: date) -> date`
- Produces: `XNYSCalendar.previous_session(session: date) -> date`
- Produces: `XNYSCalendar.held_session(entry_session: date, holding_sessions: int) -> date`
- Produces: `XNYSCalendar.session_close(session: date) -> datetime`

- [ ] **Step 1: Write the failing holiday, maturity, and early-close tests**

```python
# tests/test_metric_calendar.py
from datetime import UTC, date, datetime

import pytest

from tradingagents.strategies.metrics.calendar import XNYSCalendar


def test_next_session_skips_mlk_holiday() -> None:
    calendar = XNYSCalendar()
    assert calendar.next_session(date(2026, 1, 16)) == date(2026, 1, 20)
    assert calendar.previous_session(date(2026, 1, 20)) == date(2026, 1, 16)


def test_held_session_counts_entry_as_first_held_session() -> None:
    calendar = XNYSCalendar()
    entry = date(2026, 1, 16)
    assert calendar.held_session(entry, 5) == date(2026, 1, 23)


def test_black_friday_close_is_early() -> None:
    calendar = XNYSCalendar()
    assert calendar.session_close(date(2026, 11, 27)) == datetime(
        2026, 11, 27, 18, 0, tzinfo=UTC
    )


def test_held_session_rejects_nonpositive_window() -> None:
    with pytest.raises(ValueError, match="holding_sessions must be positive"):
        XNYSCalendar().held_session(date(2026, 1, 16), 0)
```

- [ ] **Step 2: Run the tests and verify the missing package failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_metric_calendar.py -v
```

Expected: collection fails with
`ModuleNotFoundError: No module named 'tradingagents.strategies.metrics'`.

- [ ] **Step 3: Add and install the bounded dependency**

Add after the pandas dependency in `pyproject.toml`:

```toml
    "exchange-calendars>=4.13.2,<5",
```

Run:

```bash
.venv/bin/pip install "exchange-calendars>=4.13.2,<5"
.venv/bin/python -c "import exchange_calendars; print(exchange_calendars.__version__)"
```

Expected: the printed version is at least `4.13.2` and below `5`.

- [ ] **Step 4: Implement the calendar adapter**

```python
# tradingagents/strategies/metrics/calendar.py
from __future__ import annotations

from datetime import date, datetime

import exchange_calendars
import pandas as pd


class XNYSCalendar:
    def __init__(self) -> None:
        self._calendar = exchange_calendars.get_calendar("XNYS")

    @staticmethod
    def _timestamp(session: date) -> pd.Timestamp:
        return pd.Timestamp(session.isoformat())

    def is_session(self, session: date) -> bool:
        return bool(self._calendar.is_session(self._timestamp(session)))

    def next_session(self, session: date) -> date:
        current = self._calendar.date_to_session(
            self._timestamp(session), direction="previous"
        )
        return self._calendar.next_session(current).date()

    def previous_session(self, session: date) -> date:
        current = self._calendar.date_to_session(
            self._timestamp(session), direction="next"
        )
        return self._calendar.previous_session(current).date()

    def held_session(self, entry_session: date, holding_sessions: int) -> date:
        if holding_sessions <= 0:
            raise ValueError("holding_sessions must be positive")
        if not self.is_session(entry_session):
            raise ValueError(f"{entry_session} is not an XNYS session")
        window = self._calendar.sessions_window(
            self._timestamp(entry_session), holding_sessions
        )
        return window[-1].date()

    def session_close(self, session: date) -> datetime:
        if not self.is_session(session):
            raise ValueError(f"{session} is not an XNYS session")
        return self._calendar.session_close(
            self._timestamp(session)
        ).to_pydatetime()
```

Create the initial package export:

```python
# tradingagents/strategies/metrics/__init__.py
from .calendar import XNYSCalendar

__all__ = ["XNYSCalendar"]
```

- [ ] **Step 5: Run the focused tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_metric_calendar.py -v
```

Expected: `4 passed`.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml tradingagents/strategies/metrics tests/test_metric_calendar.py
git commit -m "feat(metrics): add XNYS session calendar"
```

---

### Task 2: Immutable Metric Models and Stable Identity

**Files:**
- Create: `tradingagents/strategies/metrics/models.py`
- Create: `tradingagents/strategies/metrics/identity.py`
- Modify: `tradingagents/strategies/metrics/__init__.py`
- Test: `tests/test_metric_identity.py`

**Interfaces:**
- Produces: `MetricEpoch`, `SignalMetricRecord`, `OutcomeRecord`, `StrategyHealthRecord`, `PortfolioMetrics`, and `PairedComparison`.
- Produces: `event_key(source, source_event_id, ticker, event_at, evidence_hash) -> str`.
- Produces: `signal_id(epoch_id, strategy, policy_id, direction, event_key_value) -> str`.
- Produces: `execution_id(cohort_id, signal_id_value, fill_id) -> str`.
- Produces: `deduplicate_signals(records) -> DeduplicationResult`.

- [ ] **Step 1: Write failing deterministic and conflict tests**

```python
# tests/test_metric_identity.py
from datetime import UTC, date, datetime

from tradingagents.strategies.metrics.identity import (
    deduplicate_signals,
    event_key,
    signal_id,
)
from tradingagents.strategies.metrics.models import SignalMetricRecord


def _record(direction: str) -> SignalMetricRecord:
    event = event_key(
        source="courtlistener",
        source_event_id="docket-42",
        ticker="AAPL",
        event_at=datetime(2026, 7, 30, 14, tzinfo=UTC),
        evidence_hash="abc",
    )
    return SignalMetricRecord(
        event_key=event,
        signal_id=signal_id("epoch-1", "litigation", "30d", direction, event),
        epoch_id="epoch-1",
        policy_id="30d",
        strategy="litigation",
        ticker="AAPL",
        direction=direction,
        decision_at=datetime(2026, 7, 30, 20, tzinfo=UTC),
        reference_session=date(2026, 7, 30),
    )


def test_event_key_is_generation_independent_and_stable() -> None:
    args = dict(
        source="edgar",
        source_event_id="accession-1",
        ticker="MSFT",
        event_at=datetime(2026, 7, 29, 12, tzinfo=UTC),
        evidence_hash="hash-1",
    )
    assert event_key(**args) == event_key(**dict(reversed(list(args.items()))))


def test_dedup_is_order_independent() -> None:
    long = _record("long")
    result_a = deduplicate_signals([long, long])
    result_b = deduplicate_signals(list(reversed([long, long])))
    assert result_a.records == result_b.records == (long,)
    assert result_a.conflicts == ()


def test_direction_conflict_is_explicit() -> None:
    result = deduplicate_signals([_record("short"), _record("long")])
    assert result.records == ()
    assert len(result.conflicts) == 1
    assert result.conflicts[0].directions == ("long", "short")
```

- [ ] **Step 2: Run the tests and verify the missing-model failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_metric_identity.py -v
```

Expected: collection fails because `metrics.identity` and `metrics.models` do
not exist.

- [ ] **Step 3: Add the immutable records**

```python
# tradingagents/strategies/metrics/models.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

Direction = Literal["long", "short", "neutral"]
OutcomeStatus = Literal["pending", "valid", "invalid"]
EpochStatus = Literal["open", "closed", "invalid"]
HealthStatus = Literal[
    "signals", "legitimate_no_event", "data_failure", "strategy_defect"
]

METRIC_SCHEMA_VERSION = 2
LEGACY_SCHEMA_LABEL = "1_legacy_calendar_signed"
OUTCOME_WINDOWS = (5, 10, 20, 30)


@dataclass(frozen=True)
class MetricEpoch:
    epoch_id: str
    generation_id: str
    generation_commit: str
    behavior_hash: str
    config_hash: str
    metric_schema_version: int
    execution_clock_version: str
    pricing_version: str
    cost_model_version: str
    start_session: date
    end_session: date | None
    status: EpochStatus
    boundary_reason: str


@dataclass(frozen=True)
class SignalMetricRecord:
    event_key: str
    signal_id: str
    epoch_id: str
    policy_id: str
    strategy: str
    ticker: str
    direction: Direction
    decision_at: datetime
    reference_session: date


@dataclass(frozen=True)
class SignalConflict:
    epoch_id: str
    event_key: str
    strategy: str
    policy_id: str
    directions: tuple[str, ...]


@dataclass(frozen=True)
class DeduplicationResult:
    records: tuple[SignalMetricRecord, ...]
    conflicts: tuple[SignalConflict, ...]


@dataclass(frozen=True)
class OutcomeRecord:
    outcome_id: str
    signal_id: str
    event_key: str
    epoch_id: str
    strategy: str
    policy_id: str
    ticker: str
    direction: Direction
    holding_sessions: int
    entry_session: date
    exit_session: date
    entry_price: Decimal | None
    exit_price: Decimal | None
    raw_return: Decimal | None
    signed_return: Decimal | None
    status: OutcomeStatus
    invalid_reason: str


@dataclass(frozen=True)
class StrategyHealthRecord:
    health_id: str
    epoch_id: str
    session: date
    policy_id: str
    strategy: str
    status: HealthStatus
    signal_count: int
    evidence: dict[str, object]


@dataclass(frozen=True)
class PortfolioMetrics:
    cohort_id: str
    epoch_id: str
    metric_schema_version: int
    start_session: date
    end_session: date
    valuation_at: datetime
    benchmark_at: datetime
    valid_sessions: int
    total_return: float
    gross_return: float
    matched_benchmark_return: float
    matched_excess_return: float
    annualized_daily_net_sharpe: float | None
    sharpe_return_count: int
    annualized_matched_information_ratio: float | None
    information_ratio_return_count: int
    max_drawdown: float
    long_weight: float
    short_weight: float
    gross_weight: float
    net_weight: float
    cash_weight: float
    cumulative_costs: dict[str, float]
    unique_catalysts: int
    strategy_decisions: int
    fills: int
    closed_trades: int
    missing_mark_count: int
    stale_mark_count: int


@dataclass(frozen=True)
class PairedComparison:
    candidate_epoch_id: str
    baseline_epoch_id: str
    common_sessions: tuple[date, ...]
    candidate_return: float
    baseline_return: float
    excess_return: float
```

- [ ] **Step 4: Add canonical hashing and conflict-aware deduplication**

```python
# tradingagents/strategies/metrics/identity.py
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Iterable

from .models import (
    DeduplicationResult,
    Direction,
    SignalConflict,
    SignalMetricRecord,
)


def _stable_id(kind: str, *parts: object) -> str:
    payload = json.dumps(
        [kind, *parts], sort_keys=True, separators=(",", ":"), default=str
    )
    return f"{kind}_{hashlib.sha256(payload.encode()).hexdigest()[:24]}"


def event_key(
    source: str,
    source_event_id: str,
    ticker: str,
    event_at: datetime | None,
    evidence_hash: str,
) -> str:
    if not source_event_id:
        raise ValueError("source_event_id is required")
    return _stable_id(
        "event",
        source.strip().lower(),
        source_event_id.strip(),
        ticker.strip().upper(),
        event_at.isoformat() if event_at else "",
        evidence_hash,
    )


def signal_id(
    epoch_id: str,
    strategy: str,
    policy_id: str,
    direction: Direction,
    event_key_value: str,
) -> str:
    return _stable_id(
        "signal", epoch_id, strategy, policy_id, direction, event_key_value
    )


def execution_id(
    cohort_id: str, signal_id_value: str, fill_id: str
) -> str:
    return _stable_id("execution", cohort_id, signal_id_value, fill_id)


def deduplicate_signals(
    records: Iterable[SignalMetricRecord],
) -> DeduplicationResult:
    unique = {record.signal_id: record for record in records}
    groups: dict[
        tuple[str, str, str, str], list[SignalMetricRecord]
    ] = {}
    for record in unique.values():
        key = (
            record.epoch_id,
            record.event_key,
            record.strategy,
            record.policy_id,
        )
        groups.setdefault(key, []).append(record)

    accepted: list[SignalMetricRecord] = []
    conflicts: list[SignalConflict] = []
    for key in sorted(groups):
        group = groups[key]
        directions = tuple(sorted({record.direction for record in group}))
        if len(directions) > 1:
            conflicts.append(
                SignalConflict(
                    epoch_id=key[0],
                    event_key=key[1],
                    strategy=key[2],
                    policy_id=key[3],
                    directions=directions,
                )
            )
        else:
            accepted.extend(group)
    return DeduplicationResult(
        records=tuple(sorted(accepted, key=lambda item: item.signal_id)),
        conflicts=tuple(conflicts),
    )
```

- [ ] **Step 5: Export the public types and run tests**

Add imports for all model and identity names to
`tradingagents/strategies/metrics/__init__.py`, and list them in `__all__`.

Run:

```bash
.venv/bin/python -m pytest tests/test_metric_identity.py tests/test_metric_calendar.py -v
```

Expected: `7 passed`.

- [ ] **Step 6: Commit**

```bash
git add tradingagents/strategies/metrics tests/test_metric_identity.py
git commit -m "feat(metrics): define v2 records and stable identities"
```

---

### Task 3: Immutable Epochs, Derived Store, and Legacy Registry

**Files:**
- Create: `tradingagents/strategies/metrics/store.py`
- Create: `tradingagents/strategies/metrics/epochs.py`
- Create: `scripts/migrate_metrics_v2.py`
- Modify: `tradingagents/strategies/orchestration/generation_manager.py`
- Test: `tests/test_metric_epochs.py`
- Test: `tests/test_metrics_migration.py`

**Interfaces:**
- Produces: `MetricStore.save_epoch(epoch)`, `current_epoch()`, `close_epoch()`, `invalidate_epoch()`, `upsert_outcome()`, and strategy-health reads/writes.
- Produces: `EpochManager.ensure_epoch(context, session) -> MetricEpoch`.
- Produces: `EpochManager.invalidate_current(session, reason) -> MetricEpoch`.
- Produces: `build_legacy_registry(manifest: dict) -> dict`.

- [ ] **Step 1: Write failing lifecycle and no-rewrite tests**

```python
# tests/test_metric_epochs.py
from datetime import date

from tradingagents.strategies.metrics.epochs import (
    EpochContext,
    EpochManager,
)
from tradingagents.strategies.metrics.store import MetricStore


def _context(config_hash: str = "config-a") -> EpochContext:
    return EpochContext(
        generation_id="gen_004",
        generation_commit="abc123",
        behavior_hash="behavior-a",
        config_hash=config_hash,
        execution_clock_version="next_open_v1",
        pricing_version="raw_ohlc_v1",
        cost_model_version="paper_cost_v1",
    )


def test_hash_change_closes_old_epoch_and_opens_new(tmp_path) -> None:
    store = MetricStore(tmp_path / "metrics_v2.sqlite3")
    manager = EpochManager(store)
    first = manager.ensure_epoch(_context(), date(2026, 8, 3))
    second = manager.ensure_epoch(
        _context(config_hash="config-b"), date(2026, 8, 4)
    )
    assert first.epoch_id != second.epoch_id
    assert store.load_epoch(first.epoch_id).status == "closed"
    assert store.load_epoch(first.epoch_id).end_session == date(2026, 8, 3)


def test_critical_gap_invalidates_epoch(tmp_path) -> None:
    store = MetricStore(tmp_path / "metrics_v2.sqlite3")
    manager = EpochManager(store)
    epoch = manager.ensure_epoch(_context(), date(2026, 8, 3))
    invalid = manager.invalidate_current(date(2026, 8, 4), "missing_mark")
    assert invalid.epoch_id == epoch.epoch_id
    assert invalid.status == "invalid"
    assert invalid.boundary_reason == "missing_mark"
```

```python
# tests/test_metrics_migration.py
import json

from tradingagents.strategies.metrics.models import LEGACY_SCHEMA_LABEL
from scripts.migrate_metrics_v2 import build_legacy_registry


def test_legacy_registry_does_not_rewrite_generation_files(tmp_path) -> None:
    artifact = tmp_path / "gen_003" / "signal_journal.jsonl"
    artifact.parent.mkdir()
    artifact.write_text('{"legacy": true}\n')
    before = artifact.read_bytes()
    manifest = {"generations": [{"gen_id": "gen_003"}]}
    registry = build_legacy_registry(manifest)
    assert registry["gen_003"]["metric_schema"] == LEGACY_SCHEMA_LABEL
    assert registry["gen_003"]["promotion_eligible"] is False
    assert artifact.read_bytes() == before
```

- [ ] **Step 2: Run tests and verify missing modules**

Run:

```bash
.venv/bin/python -m pytest tests/test_metric_epochs.py tests/test_metrics_migration.py -v
```

Expected: collection fails because `metrics.epochs`, `metrics.store`, and the
migration script do not exist.

- [ ] **Step 3: Implement the derived store schema**

Create `MetricStore` with these exact tables and idempotent inserts:

```python
# tradingagents/strategies/metrics/store.py
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, replace
from datetime import date
from pathlib import Path

from .models import MetricEpoch, OutcomeRecord, StrategyHealthRecord

_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS metric_epochs (
  epoch_id TEXT PRIMARY KEY,
  payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outcomes (
  outcome_id TEXT PRIMARY KEY,
  epoch_id TEXT NOT NULL,
  payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_health (
  health_id TEXT PRIMARY KEY,
  epoch_id TEXT NOT NULL,
  session TEXT NOT NULL,
  payload_json TEXT NOT NULL
);
"""


class MetricStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.executescript(_SCHEMA)

    @staticmethod
    def _json(record: object) -> str:
        return json.dumps(asdict(record), sort_keys=True, default=str)

    @staticmethod
    def _epoch(payload: str) -> MetricEpoch:
        data = json.loads(payload)
        data["start_session"] = date.fromisoformat(data["start_session"])
        if data["end_session"]:
            data["end_session"] = date.fromisoformat(data["end_session"])
        return MetricEpoch(**data)

    def save_epoch(self, epoch: MetricEpoch) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO metric_epochs VALUES (?, ?)",
                (epoch.epoch_id, self._json(epoch)),
            )

    def load_epoch(self, epoch_id: str) -> MetricEpoch:
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT payload_json FROM metric_epochs WHERE epoch_id = ?",
                (epoch_id,),
            ).fetchone()
        if row is None:
            raise KeyError(epoch_id)
        return self._epoch(row[0])

    def current_epoch(self) -> MetricEpoch | None:
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT payload_json FROM metric_epochs ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        return self._epoch(row[0]) if row else None

    def close_epoch(
        self, epoch_id: str, end_session: date, reason: str, invalid: bool
    ) -> MetricEpoch:
        current = self.load_epoch(epoch_id)
        updated = replace(
            current,
            end_session=end_session,
            status="invalid" if invalid else "closed",
            boundary_reason=reason,
        )
        self.save_epoch(updated)
        return updated

    def upsert_outcome(self, outcome: OutcomeRecord) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO outcomes VALUES (?, ?, ?)",
                (outcome.outcome_id, outcome.epoch_id, self._json(outcome)),
            )

    def save_strategy_health(self, health: StrategyHealthRecord) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO strategy_health VALUES (?, ?, ?, ?)",
                (
                    health.health_id,
                    health.epoch_id,
                    health.session.isoformat(),
                    self._json(health),
                ),
            )
```

- [ ] **Step 4: Implement epoch hash and boundary semantics**

```python
# tradingagents/strategies/metrics/epochs.py
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
import hashlib
import json

from .models import METRIC_SCHEMA_VERSION, MetricEpoch
from .store import MetricStore
from .calendar import XNYSCalendar


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


class EpochManager:
    def __init__(
        self,
        store: MetricStore,
        calendar: XNYSCalendar | None = None,
    ) -> None:
        self.store = store
        self.calendar = calendar or XNYSCalendar()

    def ensure_epoch(
        self, context: EpochContext, session: date
    ) -> MetricEpoch:
        semantic_hash = _semantic_hash(context)
        current = self.store.current_epoch()
        if (
            current is not None
            and current.status == "open"
            and current.epoch_id.endswith(semantic_hash[:16])
        ):
            return current
        if current is not None and current.status == "open":
            self.store.close_epoch(
                current.epoch_id,
                self.calendar.previous_session(session),
                "semantic_hash_changed",
                invalid=False,
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
            boundary_reason="initial" if current is None else "semantic_hash_changed",
        )
        self.store.save_epoch(epoch)
        return epoch

    def invalidate_current(
        self, session: date, reason: str
    ) -> MetricEpoch:
        current = self.store.current_epoch()
        if current is None or current.status != "open":
            raise RuntimeError("no open metric epoch")
        return self.store.close_epoch(
            current.epoch_id, session, reason, invalid=True
        )
```

- [ ] **Step 5: Add generation metadata and the legacy registry command**

In `GenerationManager._run_cohorts_subprocess`, add:

```python
env["EVENTEDGE_GENERATION_ID"] = gen_data["gen_id"]
env["EVENTEDGE_GENERATION_COMMIT"] = gen_data["git_commit"]
```

Create:

```python
# scripts/migrate_metrics_v2.py
from __future__ import annotations

import argparse
import json
from pathlib import Path

from tradingagents.strategies.metrics.models import LEGACY_SCHEMA_LABEL


def build_legacy_registry(manifest: dict) -> dict:
    return {
        item["gen_id"]: {
            "metric_schema": LEGACY_SCHEMA_LABEL,
            "promotion_eligible": False,
            "reason": "legacy_same_bar_close_and_unreconciled_costs",
        }
        for item in manifest.get("generations", [])
        if int(item["gen_id"].split("_")[1]) <= 3
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    manifest = json.loads(Path(args.manifest).read_text())
    registry = build_legacy_registry(manifest)
    print(json.dumps(registry, indent=2, sort_keys=True))
    if args.write:
        Path(args.output).write_text(
            json.dumps(registry, indent=2, sort_keys=True) + "\n"
        )


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Run focused tests and the migration dry run**

Run:

```bash
.venv/bin/python -m pytest tests/test_metric_epochs.py tests/test_metrics_migration.py -v
.venv/bin/python scripts/migrate_metrics_v2.py \
  --manifest data/generations/manifest.json \
  --output /tmp/eventedge-metrics-legacy-registry.json
test ! -e /tmp/eventedge-metrics-legacy-registry.json
```

Expected: tests pass; the command prints legacy JSON; `test` exits `0` because
dry-run mode did not write the output file.

- [ ] **Step 7: Commit**

```bash
git add tradingagents/strategies/metrics scripts/migrate_metrics_v2.py \
  tradingagents/strategies/orchestration/generation_manager.py \
  tests/test_metric_epochs.py tests/test_metrics_migration.py
git commit -m "feat(metrics): add immutable epochs and legacy registry"
```

---

### Task 4: Trading-Session Outcomes and Directional Accuracy

**Files:**
- Create: `tradingagents/strategies/metrics/outcomes.py`
- Modify: `tradingagents/strategies/metrics/store.py`
- Modify: `tradingagents/strategies/orchestration/session_executor.py`
- Modify: `tradingagents/strategies/learning/signal_journal.py`
- Test: `tests/test_outcome_metrics_v2.py`
- Modify: `tests/test_cohort_lifecycle.py`
- Modify: `tests/test_30day_simulation.py`

**Interfaces:**
- Produces: `OutcomeCalculator.build(signal, holding_sessions, bars) -> OutcomeRecord`.
- Produces: `directional_accuracy(outcomes) -> DirectionalAccuracy`.
- Consumes exact raw bars keyed by `(ticker, session)`; never searches backward or forward.
- Production v2 callers stop invoking `SignalJournal.fill_outcomes`; legacy journals remain readable.

- [ ] **Step 1: Write failing exact-window, short, neutral, and missing-price tests**

```python
# tests/test_outcome_metrics_v2.py
from datetime import UTC, date, datetime
from decimal import Decimal

from tradingagents.strategies.execution.models import MarketBar
from tradingagents.strategies.metrics.models import SignalMetricRecord
from tradingagents.strategies.metrics.outcomes import (
    OutcomeCalculator,
    directional_accuracy,
)


def _bar(session: date, opening: str, close: str) -> MarketBar:
    return MarketBar(
        ticker="AAPL",
        session=session,
        open=Decimal(opening),
        high=Decimal(max(opening, close)),
        low=Decimal(min(opening, close)),
        close=Decimal(close),
        source="fixture",
        fetched_at=datetime(2026, 8, 31, tzinfo=UTC),
        adjusted=False,
    )


def _signal(direction: str) -> SignalMetricRecord:
    return SignalMetricRecord(
        event_key="event-1",
        signal_id=f"signal-{direction}",
        epoch_id="epoch-1",
        policy_id="30d",
        strategy="filing_analysis",
        ticker="AAPL",
        direction=direction,
        decision_at=datetime(2026, 8, 3, 20, tzinfo=UTC),
        reference_session=date(2026, 8, 3),
    )


def test_five_session_outcome_uses_next_open_and_fifth_close() -> None:
    bars = {
        ("AAPL", date(2026, 8, 4)): _bar(date(2026, 8, 4), "100", "101"),
        ("AAPL", date(2026, 8, 10)): _bar(date(2026, 8, 10), "108", "110"),
    }
    outcome = OutcomeCalculator().build(_signal("long"), 5, bars)
    assert outcome.entry_session == date(2026, 8, 4)
    assert outcome.exit_session == date(2026, 8, 10)
    assert outcome.raw_return == Decimal("0.1")
    assert outcome.signed_return == Decimal("0.1")


def test_short_direction_is_applied_once() -> None:
    bars = {
        ("AAPL", date(2026, 8, 4)): _bar(date(2026, 8, 4), "100", "99"),
        ("AAPL", date(2026, 8, 10)): _bar(date(2026, 8, 10), "90", "90"),
    }
    outcome = OutcomeCalculator().build(_signal("short"), 5, bars)
    assert outcome.raw_return == Decimal("-0.1")
    assert outcome.signed_return == Decimal("0.1")
    assert directional_accuracy([outcome]).rate == 1.0


def test_neutral_is_excluded_from_directional_denominator() -> None:
    bars = {
        ("AAPL", date(2026, 8, 4)): _bar(date(2026, 8, 4), "100", "100"),
        ("AAPL", date(2026, 8, 10)): _bar(date(2026, 8, 10), "100", "110"),
    }
    neutral = OutcomeCalculator().build(_signal("neutral"), 5, bars)
    summary = directional_accuracy([neutral])
    assert neutral.signed_return is None
    assert summary.actionable_count == 0
    assert summary.neutral_count == 1
    assert summary.rate is None


def test_missing_exact_exit_price_is_invalid() -> None:
    bars = {
        ("AAPL", date(2026, 8, 4)): _bar(date(2026, 8, 4), "100", "101")
    }
    outcome = OutcomeCalculator().build(_signal("long"), 5, bars)
    assert outcome.status == "invalid"
    assert outcome.exit_price is None
    assert outcome.invalid_reason == "missing_exit_bar"
```

- [ ] **Step 2: Run tests and verify the missing implementation**

Run:

```bash
.venv/bin/python -m pytest tests/test_outcome_metrics_v2.py -v
```

Expected: collection fails because `metrics.outcomes` does not exist.

- [ ] **Step 3: Implement exact-session outcomes**

```python
# tradingagents/strategies/metrics/outcomes.py
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Mapping

from tradingagents.strategies.execution.models import MarketBar

from .calendar import XNYSCalendar
from .identity import _stable_id
from .models import OutcomeRecord, SignalMetricRecord


@dataclass(frozen=True)
class DirectionalAccuracy:
    actionable_count: int
    hit_count: int
    neutral_count: int
    invalid_count: int
    rate: float | None


class OutcomeCalculator:
    def __init__(self, calendar: XNYSCalendar | None = None) -> None:
        self.calendar = calendar or XNYSCalendar()

    def build(
        self,
        signal: SignalMetricRecord,
        holding_sessions: int,
        bars: Mapping[tuple[str, object], MarketBar],
    ) -> OutcomeRecord:
        entry_session = self.calendar.next_session(signal.reference_session)
        exit_session = self.calendar.held_session(
            entry_session, holding_sessions
        )
        entry_bar = bars.get((signal.ticker, entry_session))
        exit_bar = bars.get((signal.ticker, exit_session))
        reason = ""
        if entry_bar is None:
            reason = "missing_entry_bar"
        elif exit_bar is None:
            reason = "missing_exit_bar"
        entry_price = entry_bar.open if entry_bar else None
        exit_price = exit_bar.close if exit_bar else None
        if entry_price is not None and entry_price <= 0:
            reason = "invalid_entry_price"
        if exit_price is not None and exit_price <= 0:
            reason = "invalid_exit_price"
        raw_return = None
        signed_return = None
        if not reason:
            raw_return = (exit_price - entry_price) / entry_price
            if signal.direction == "long":
                signed_return = raw_return
            elif signal.direction == "short":
                signed_return = -raw_return
        return OutcomeRecord(
            outcome_id=_stable_id(
                "outcome", signal.signal_id, holding_sessions
            ),
            signal_id=signal.signal_id,
            event_key=signal.event_key,
            epoch_id=signal.epoch_id,
            strategy=signal.strategy,
            policy_id=signal.policy_id,
            ticker=signal.ticker,
            direction=signal.direction,
            holding_sessions=holding_sessions,
            entry_session=entry_session,
            exit_session=exit_session,
            entry_price=entry_price,
            exit_price=exit_price,
            raw_return=raw_return,
            signed_return=signed_return,
            status="invalid" if reason else "valid",
            invalid_reason=reason,
        )


def directional_accuracy(
    outcomes: Iterable[OutcomeRecord],
) -> DirectionalAccuracy:
    rows = list(outcomes)
    valid = [row for row in rows if row.status == "valid"]
    actionable = [
        row for row in valid if row.direction in {"long", "short"}
    ]
    hits = sum(row.signed_return > 0 for row in actionable)
    return DirectionalAccuracy(
        actionable_count=len(actionable),
        hit_count=hits,
        neutral_count=sum(row.direction == "neutral" for row in valid),
        invalid_count=sum(row.status == "invalid" for row in rows),
        rate=hits / len(actionable) if actionable else None,
    )
```

- [ ] **Step 4: Wire daily processing without a second market-data fetch**

In `SessionExecutor`, add due outcome tickers to the existing shared raw-bar
request, then calculate and upsert all due windows:

```python
calculator = OutcomeCalculator(self.calendar)
for signal in metric_signals:
    for window in OUTCOME_WINDOWS:
        due = calculator.calendar.held_session(
            calculator.calendar.next_session(signal.reference_session),
            window,
        )
        if due == session:
            metric_store.upsert_outcome(
                calculator.build(signal, window, raw_bars)
            )
```

Delete the production call to `SignalJournal.fill_outcomes` from
`MultiStrategyEngine`. Keep the legacy method unchanged for schema-v1
readability, add this first line to its docstring, and remove all v2 callers:

```python
"""Legacy schema-v1 calendar-day updater; never called by v2 production."""
```

Replace calendar-day and short-double-inversion assertions in
`tests/test_cohort_lifecycle.py` and `tests/test_30day_simulation.py` with calls
to `OutcomeCalculator`; do not change legacy fixture files.

- [ ] **Step 5: Run focused and legacy-regression tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_outcome_metrics_v2.py \
  tests/test_cohort_lifecycle.py \
  tests/test_30day_simulation.py -v
```

Expected: all selected tests pass and no v2 test invokes yfinance.

- [ ] **Step 6: Commit**

```bash
git add tradingagents/strategies/metrics/outcomes.py \
  tradingagents/strategies/metrics/store.py \
  tradingagents/strategies/orchestration/session_executor.py \
  tradingagents/strategies/learning/signal_journal.py \
  tests/test_outcome_metrics_v2.py tests/test_cohort_lifecycle.py \
  tests/test_30day_simulation.py
git commit -m "feat(metrics): compute exact XNYS-session outcomes"
```

---

### Task 5: Net Portfolio Metrics and Exposure-Matched Benchmark

**Files:**
- Create: `tradingagents/strategies/metrics/portfolio.py`
- Test: `tests/test_portfolio_metrics_v2.py`

**Interfaces:**
- Produces: `daily_net_returns(snapshots, calendar) -> tuple[DatedReturn, ...]`.
- Produces: `matched_benchmark_returns(snapshots, observations, calendar)`.
- Produces: `portfolio_metrics(...) -> PortfolioMetrics`.
- Produces: `paired_comparison(candidate_returns, baseline_returns) -> PairedComparison`.
- A return is emitted only when two valid snapshots are consecutive XNYS sessions in the same epoch.
- `PortfolioMetrics` is emitted only for one requested cohort+epoch with a
  duplicate-free, complete contiguous valid XNYS snapshot window and complete
  cohort+epoch-scoped total-return-adjusted SPY/BIL coverage. Invalid or
  missing sessions/benchmark rows raise; consumers must surface metrics as
  unavailable rather than bridge or compound disjoint windows. Successful
  metrics therefore have zero missing/stale-mark counts. Sharpe visibility is
  based on 30 actual daily return observations (31 snapshots), not row count
  alone.
- Before any daily, benchmark, or aggregate metric is emitted, every target
  snapshot must use finite Decimal account fields and satisfy exactly:
  `net_equity = cash + long_market_value - short_liability`,
  `gross_exposure = long_market_value + short_liability`, and
  `net_exposure = long_market_value - short_liability`.

- [ ] **Step 1: Write failing known-sequence tests**

```python
# tests/test_portfolio_metrics_v2.py
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from tradingagents.strategies.execution.models import (
    AccountSnapshot,
    BenchmarkObservation,
)
from tradingagents.strategies.metrics.portfolio import (
    annualized_sharpe,
    drawdowns,
    matched_return,
    total_return,
)


def test_total_return_uses_net_equity_endpoints() -> None:
    assert total_return([Decimal("100"), Decimal("110")]) == pytest.approx(0.1)


def test_known_drawdown_sequence() -> None:
    assert drawdowns([100.0, 120.0, 90.0, 99.0]) == (
        0.0,
        0.0,
        -0.25,
        -0.175,
    )


def test_sharpe_is_hidden_before_thirty_valid_sessions() -> None:
    assert annualized_sharpe([0.01] * 28, valid_sessions=29) is None


def test_known_annualized_excess_sharpe() -> None:
    returns = [0.01, -0.01] * 15
    value = annualized_sharpe(returns, valid_sessions=31)
    assert value == pytest.approx(0.0, abs=1e-12)


def test_exposure_matched_benchmark_formula() -> None:
    assert matched_return(
        gross_weight=0.8,
        net_weight=0.6,
        spy_return=0.02,
        cash_return=0.001,
    ) == pytest.approx(0.0122)
```

- [ ] **Step 2: Run and verify the missing-module failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_portfolio_metrics_v2.py -v
```

Expected: collection fails because `metrics.portfolio` does not exist.

- [ ] **Step 3: Implement pure formulas and non-bridging daily returns**

```python
# tradingagents/strategies/metrics/portfolio.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import math
import statistics
from typing import Iterable, Sequence

from tradingagents.strategies.execution.models import AccountSnapshot

from .calendar import XNYSCalendar
from .models import PairedComparison, PortfolioMetrics


@dataclass(frozen=True)
class DatedReturn:
    session: date
    value: float


def total_return(values: Sequence[Decimal | float]) -> float:
    if len(values) < 2 or float(values[0]) <= 0:
        raise ValueError("two positive-endpoint equity values are required")
    return float(values[-1] / values[0] - 1)


def drawdowns(values: Sequence[float]) -> tuple[float, ...]:
    peak = -math.inf
    result: list[float] = []
    for value in values:
        peak = max(peak, value)
        result.append(value / peak - 1)
    return tuple(result)


def annualized_sharpe(
    excess_returns: Sequence[float], valid_sessions: int
) -> float | None:
    if valid_sessions < 30 or len(excess_returns) < 2:
        return None
    deviation = statistics.stdev(excess_returns)
    if deviation == 0:
        return None
    return statistics.mean(excess_returns) / deviation * math.sqrt(252)


def matched_return(
    gross_weight: float,
    net_weight: float,
    spy_return: float,
    cash_return: float,
) -> float:
    return (
        net_weight * spy_return
        + max(0.0, 1.0 - gross_weight) * cash_return
    )


def daily_net_returns(
    snapshots: Iterable[AccountSnapshot],
    calendar: XNYSCalendar | None = None,
) -> tuple[DatedReturn, ...]:
    session_calendar = calendar or XNYSCalendar()
    ordered = sorted(snapshots, key=lambda row: row.session)
    output: list[DatedReturn] = []
    for previous, current in zip(ordered, ordered[1:]):
        if not previous.valid or not current.valid:
            continue
        if previous.epoch_id != current.epoch_id:
            continue
        if session_calendar.next_session(previous.session) != current.session:
            continue
        output.append(
            DatedReturn(
                session=current.session,
                value=float(current.net_equity / previous.net_equity - 1),
            )
        )
    return tuple(output)
```

- [ ] **Step 4: Implement benchmark alignment and reconciliation**

Add to `portfolio.py`:

```python
def benchmark_close_map(observations, symbol: str) -> dict[date, Decimal]:
    return {
        row.session: row.close
        for row in observations
        if row.symbol == symbol and row.valid
    }


def matched_benchmark_returns(
    snapshots,
    observations,
    calendar: XNYSCalendar | None = None,
) -> tuple[DatedReturn, ...]:
    session_calendar = calendar or XNYSCalendar()
    ordered = sorted(
        (row for row in snapshots if row.valid),
        key=lambda row: row.session,
    )
    spy = benchmark_close_map(observations, "SPY")
    cash = benchmark_close_map(observations, "BIL")
    output: list[DatedReturn] = []
    for previous, current in zip(ordered, ordered[1:]):
        if previous.epoch_id != current.epoch_id:
            continue
        if session_calendar.next_session(previous.session) != current.session:
            continue
        required = (previous.session, current.session)
        if not all(item in spy and item in cash for item in required):
            continue
        spy_return = float(spy[current.session] / spy[previous.session] - 1)
        cash_return = float(cash[current.session] / cash[previous.session] - 1)
        equity = float(previous.net_equity)
        gross_weight = float(previous.gross_exposure) / equity
        net_weight = float(previous.net_exposure) / equity
        output.append(
            DatedReturn(
                current.session,
                matched_return(
                    gross_weight, net_weight, spy_return, cash_return
                ),
            )
        )
    return tuple(output)


def cash_proxy_returns(
    observations,
    calendar: XNYSCalendar | None = None,
) -> tuple[DatedReturn, ...]:
    session_calendar = calendar or XNYSCalendar()
    cash = benchmark_close_map(observations, "BIL")
    sessions = sorted(cash)
    return tuple(
        DatedReturn(
            current,
            float(cash[current] / cash[previous] - 1),
        )
        for previous, current in zip(sessions, sessions[1:])
        if session_calendar.next_session(previous) == current
    )


def reconcile_costs(snapshot: AccountSnapshot) -> None:
    costs = (
        snapshot.slippage_cost
        + snapshot.commission_cost
        + snapshot.other_fees
        + snapshot.borrow_cost
        + snapshot.financing_cost
    )
    if snapshot.gross_equity - costs != snapshot.net_equity:
        raise ValueError(
            f"snapshot {snapshot.snapshot_id} does not reconcile gross to net"
        )
```

Add these exact aggregators:

```python
def _compound(values: Sequence[float]) -> float:
    result = 1.0
    for value in values:
        result *= 1.0 + value
    return result - 1.0


def portfolio_metrics(
    *,
    cohort_id: str,
    epoch_id: str,
    snapshots,
    benchmark_observations,
    signals,
    fills,
) -> PortfolioMetrics:
    epoch_rows = sorted(
        (row for row in snapshots if row.epoch_id == epoch_id),
        key=lambda row: row.session,
    )
    valid = [row for row in epoch_rows if row.valid]
    if len(valid) < 2:
        raise ValueError("at least two valid snapshots are required")
    for snapshot in valid:
        reconcile_costs(snapshot)
    book = {row.session: row.value for row in daily_net_returns(valid)}
    benchmark = {
        row.session: row.value
        for row in matched_benchmark_returns(
            valid, benchmark_observations
        )
    }
    common = tuple(sorted(set(book) & set(benchmark)))
    book_values = [book[session] for session in common]
    benchmark_values = [benchmark[session] for session in common]
    matched_excess = [
        book[session] - benchmark[session] for session in common
    ]
    cash_proxy = {
        row.session: row.value
        for row in cash_proxy_returns(benchmark_observations)
    }
    sharpe_sessions = tuple(sorted(set(book) & set(cash_proxy)))
    risk_free_excess = [
        book[session] - cash_proxy[session] for session in sharpe_sessions
    ]
    latest = valid[-1]
    equity = float(latest.net_equity)
    costs = {
        "slippage": float(latest.slippage_cost),
        "commission": float(latest.commission_cost),
        "other_fees": float(latest.other_fees),
        "borrow": float(latest.borrow_cost),
        "financing": float(latest.financing_cost),
    }
    invalid_reasons = [
        row.invalid_reason.lower() for row in epoch_rows if not row.valid
    ]
    return PortfolioMetrics(
        cohort_id=cohort_id,
        epoch_id=epoch_id,
        metric_schema_version=2,
        start_session=valid[0].session,
        end_session=valid[-1].session,
        valuation_at=latest.valuation_at,
        benchmark_at=max(
            row.observed_at
            for row in benchmark_observations
            if row.valid and row.session == latest.session
        ),
        valid_sessions=len(valid),
        total_return=total_return([valid[0].net_equity, valid[-1].net_equity]),
        gross_return=total_return(
            [valid[0].gross_equity, valid[-1].gross_equity]
        ),
        matched_benchmark_return=_compound(benchmark_values),
        matched_excess_return=_compound(book_values) - _compound(
            benchmark_values
        ),
        annualized_daily_net_sharpe=annualized_sharpe(
            risk_free_excess, valid_sessions=len(valid)
        ),
        sharpe_return_count=len(risk_free_excess),
        annualized_matched_information_ratio=annualized_sharpe(
            matched_excess, valid_sessions=len(valid)
        ),
        information_ratio_return_count=len(matched_excess),
        max_drawdown=min(
            drawdowns([float(row.net_equity) for row in valid])
        ),
        long_weight=float(latest.long_market_value) / equity,
        short_weight=float(latest.short_liability) / equity,
        gross_weight=float(latest.gross_exposure) / equity,
        net_weight=float(latest.net_exposure) / equity,
        cash_weight=float(latest.cash) / equity,
        cumulative_costs=costs,
        unique_catalysts=len({row.event_key for row in signals}),
        strategy_decisions=len({row.signal_id for row in signals}),
        fills=len({row.fill_id for row in fills}),
        closed_trades=len(
            {
                row.intent_id
                for row in fills
                if getattr(row, "side", "") in {"sell", "cover"}
            }
        ),
        missing_mark_count=sum(
            "missing" in reason for reason in invalid_reasons
        ),
        stale_mark_count=sum("stale" in reason for reason in invalid_reasons),
    )


def paired_comparison(
    *,
    candidate_epoch_id: str,
    baseline_epoch_id: str,
    candidate_returns: Iterable[DatedReturn],
    baseline_returns: Iterable[DatedReturn],
) -> PairedComparison:
    candidate = {row.session: row.value for row in candidate_returns}
    baseline = {row.session: row.value for row in baseline_returns}
    common = tuple(sorted(set(candidate) & set(baseline)))
    candidate_total = _compound([candidate[item] for item in common])
    baseline_total = _compound([baseline[item] for item in common])
    return PairedComparison(
        candidate_epoch_id=candidate_epoch_id,
        baseline_epoch_id=baseline_epoch_id,
        common_sessions=common,
        candidate_return=candidate_total,
        baseline_return=baseline_total,
        excess_return=candidate_total - baseline_total,
    )
```

P0 must expose `Fill.side` in the ledger read model (copied from its
`OrderIntent.side`) so `closed_trades` is computed without reading JSON.

- [ ] **Step 5: Add invalid-session, epoch-boundary, reconciliation, and common-window tests**

Extend `tests/test_portfolio_metrics_v2.py` with fixture `AccountSnapshot`
records and assert:

```python
assert [row.session for row in daily_net_returns(snapshots)] == [
    date(2026, 8, 4)
]
assert set(candidate_map) & set(baseline_map) == {
    date(2026, 8, 4),
    date(2026, 8, 5),
}
with pytest.raises(ValueError, match="does not reconcile"):
    reconcile_costs(bad_snapshot)
```

The fixture must place an invalid 2026-08-05 snapshot between two valid rows
and change `epoch_id` on 2026-08-07; neither gap may emit a return.

- [ ] **Step 6: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_portfolio_metrics_v2.py -v
```

Expected: all tests pass, including exact drawdown, Sharpe visibility, cost
reconciliation, common-window pairing, and no-gap bridging.

- [ ] **Step 7: Commit**

```bash
git add tradingagents/strategies/metrics/portfolio.py \
  tests/test_portfolio_metrics_v2.py
git commit -m "feat(metrics): add net portfolio and matched benchmark metrics"
```

---

### Task 6: Single Metrics Service and Comparison Consumers

**Files:**
- Create: `tradingagents/strategies/metrics/service.py`
- Modify: `tradingagents/strategies/orchestration/cohort_comparison.py`
- Modify: `tradingagents/strategies/orchestration/generation_comparison.py`
- Modify: `tradingagents/strategies/orchestration/multi_strategy_engine.py`
- Modify: `tradingagents/strategies/learning/prompt_optimizer.py`
- Test: `tests/test_metrics_service.py`
- Modify: `tests/test_generation_manager.py`
- Modify: `tests/test_multi_strategy.py`

**Interfaces:**
- Produces: `MetricsService.cohort_report(cohort_id, epoch_id) -> PortfolioMetrics`.
- Produces: `MetricsService.generation_report(epoch_id) -> GenerationMetricsReport`.
- Produces: `MetricsService.compare(candidate_cohort_id, candidate_epoch_id, baseline_service, baseline_cohort_id, baseline_epoch_id) -> PairedComparison`.
- All readers consume P0 ledger records and `MetricStore`; no consumer calculates hit rate, Sharpe, drawdown, or total return locally.

- [ ] **Step 1: Write the failing delegation test**

```python
# tests/test_metrics_service.py
from unittest.mock import Mock

from tradingagents.strategies.metrics.service import MetricsService
from tradingagents.strategies.orchestration.cohort_comparison import (
    CohortComparison,
)


def test_cohort_comparison_delegates_to_metrics_service() -> None:
    service = Mock(spec=MetricsService)
    service.generation_report.return_value = {"headline": "v2"}
    comparison = CohortComparison(metrics_service=service)
    assert comparison.compare() == {"headline": "v2"}
    service.generation_report.assert_called_once_with()


def test_metrics_modules_do_not_import_learning() -> None:
    from pathlib import Path
    import ast

    root = Path("tradingagents/strategies/metrics")
    imported = []
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
    assert not any(".learning" in name for name in imported)
```

- [ ] **Step 2: Run tests and verify constructor mismatch**

Run:

```bash
.venv/bin/python -m pytest tests/test_metrics_service.py -v
```

Expected: collection fails because `metrics.service` does not exist.

- [ ] **Step 3: Implement the ledger-backed service**

```python
# tradingagents/strategies/metrics/service.py
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger

from .identity import deduplicate_signals
from .portfolio import portfolio_metrics, paired_comparison
from .store import MetricStore


class MetricsService:
    def __init__(
        self,
        generation_state_dir: str | Path,
        cohort_ledgers: dict[str, PortfolioLedger],
    ) -> None:
        self.generation_state_dir = Path(generation_state_dir)
        self.cohort_ledgers = cohort_ledgers
        self.store = MetricStore(
            self.generation_state_dir / "metrics_v2.sqlite3"
        )

    def cohort_report(self, cohort_id: str, epoch_id: str):
        ledger = self.cohort_ledgers[cohort_id]
        snapshots = ledger.read_snapshots(epoch_id=epoch_id)
        benchmarks = ledger.read_benchmark_observations(epoch_id=epoch_id)
        signals = ledger.read_signals(epoch_id=epoch_id)
        fills = ledger.read_fills(epoch_id=epoch_id)
        deduped = deduplicate_signals(
            self._metric_signal(row) for row in signals
        )
        return portfolio_metrics(
            cohort_id=cohort_id,
            epoch_id=epoch_id,
            snapshots=snapshots,
            benchmark_observations=benchmarks,
            signals=deduped.records,
            fills=fills,
        )

    def generation_report(self) -> dict:
        epoch = self.store.current_epoch()
        if epoch is None:
            return {
                "metric_schema_version": 2,
                "epoch": None,
                "headline_books": {},
                "scenario_panel": None,
                "stress_tests": {},
                "dependent_scenarios": True,
            }
        reports = {
            cohort_id: self.cohort_report(cohort_id, epoch.epoch_id)
            for cohort_id in sorted(self.cohort_ledgers)
        }
        headline = {
            key: value
            for key, value in reports.items()
            if key.endswith("_size_100k")
        }
        stress = {
            key: value for key, value in reports.items() if key not in headline
        }
        panel = (
            sum(item.total_return for item in headline.values()) / len(headline)
            if headline
            else None
        )
        return {
            "metric_schema_version": 2,
            "epoch": asdict(epoch),
            "headline_books": {
                key: asdict(value) for key, value in headline.items()
            },
            "scenario_panel": panel,
            "stress_tests": {
                key: asdict(value) for key, value in stress.items()
            },
            "dependent_scenarios": True,
        }

    @staticmethod
    def _metric_signal(row) -> SignalMetricRecord:
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

    def compare(
        self,
        candidate_cohort_id: str,
        candidate_epoch_id: str,
        baseline_service: "MetricsService",
        baseline_cohort_id: str,
        baseline_epoch_id: str,
    ):
        candidate = self.cohort_ledgers[
            candidate_cohort_id
        ].read_snapshots(epoch_id=candidate_epoch_id)
        baseline = baseline_service.cohort_ledgers[
            baseline_cohort_id
        ].read_snapshots(epoch_id=baseline_epoch_id)
        return paired_comparison(
            candidate_epoch_id=candidate_epoch_id,
            baseline_epoch_id=baseline_epoch_id,
            candidate_returns=daily_net_returns(candidate),
            baseline_returns=daily_net_returns(baseline),
        )
```

Add imports for `SignalMetricRecord`, `daily_net_returns`, and
`paired_comparison` at the top of `service.py`. The conversion must not infer
`event_key`, `epoch_id`, or `policy_id` from filenames.

- [ ] **Step 4: Replace distributed formulas**

Change `CohortComparison` to accept `metrics_service` and make `compare()`:

```python
def compare(self) -> dict:
    return self._metrics_service.generation_report()
```

Change `GenerationComparison.compare()` to call
`MetricsService.compare(candidate, baseline)` for each explicitly selected
candidate/baseline pair. Delete `_hit_rate`, `_sharpe`, `_total_return`, and
closed-trade drawdown helpers from both comparison modules.

Replace `MultiStrategyEngine._compute_strategy_confidence` and
`PromptOptimizer` hit-rate formulas with `directional_accuracy` over v2
outcomes. Production learning is disabled in Task 8, but diagnostics must
still use correct semantics in isolated tests.

- [ ] **Step 5: Add no-legacy-formula regression assertions**

In `tests/test_metrics_service.py`, add:

```python
def test_performance_consumers_do_not_define_metric_formulas() -> None:
    paths = [
        "tradingagents/strategies/orchestration/cohort_comparison.py",
        "tradingagents/strategies/orchestration/generation_comparison.py",
    ]
    forbidden = ("statistics.stdev", "sum(pnl_pcts)", "return_5d")
    for path in paths:
        source = Path(path).read_text()
        assert all(token not in source for token in forbidden)
```

- [ ] **Step 6: Run focused comparison tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_metrics_service.py \
  tests/test_generation_manager.py \
  tests/test_multi_strategy.py -v
```

Expected: all selected tests pass; comparison payloads expose schema v2 and
paired common sessions rather than trade-P&L Sharpe.

- [ ] **Step 7: Commit**

```bash
git add tradingagents/strategies/metrics/service.py \
  tradingagents/strategies/orchestration/cohort_comparison.py \
  tradingagents/strategies/orchestration/generation_comparison.py \
  tradingagents/strategies/orchestration/multi_strategy_engine.py \
  tradingagents/strategies/learning/prompt_optimizer.py \
  tests/test_metrics_service.py tests/test_generation_manager.py \
  tests/test_multi_strategy.py
git commit -m "refactor(metrics): route comparisons through v2 service"
```

---

### Task 7: Exhaustive Strategy-Health Evidence

**Files:**
- Create: `tradingagents/strategies/metrics/health.py`
- Modify: `tradingagents/strategies/orchestration/multi_strategy_engine.py`
- Modify: `tradingagents/strategies/orchestration/cohort_orchestrator.py`
- Modify: `tradingagents/strategies/metrics/store.py`
- Test: `tests/test_strategy_health.py`

**Interfaces:**
- Produces: `classify_strategy_run(...) -> StrategyHealthRecord`.
- `screen_and_enrich` returns `(signals, regime, health_records)`.
- A strategy exception becomes `strategy_defect`; an explicit provider error becomes `data_failure`; a successful zero-candidate screen becomes evidenced `legitimate_no_event`.
- Exactly 12 distinct strategies must be stored for each policy/session.

- [ ] **Step 1: Write failing classification tests**

```python
# tests/test_strategy_health.py
from datetime import date

from tradingagents.strategies.metrics.health import classify_strategy_run


def test_zero_candidates_with_healthy_sources_is_legitimate_no_event() -> None:
    record = classify_strategy_run(
        epoch_id="epoch-1",
        session=date(2026, 8, 3),
        policy_id="30d",
        strategy="earnings_call",
        data_sources=("finnhub", "yfinance"),
        candidates=[],
        provider_errors={},
        exception=None,
    )
    assert record.status == "legitimate_no_event"
    assert record.evidence["candidate_count"] == 0


def test_provider_error_is_data_failure() -> None:
    record = classify_strategy_run(
        epoch_id="epoch-1",
        session=date(2026, 8, 3),
        policy_id="30d",
        strategy="earnings_call",
        data_sources=("finnhub",),
        candidates=[],
        provider_errors={"finnhub": "timeout"},
        exception=None,
    )
    assert record.status == "data_failure"
    assert record.evidence["provider_errors"] == {"finnhub": "timeout"}


def test_exception_is_strategy_defect() -> None:
    record = classify_strategy_run(
        epoch_id="epoch-1",
        session=date(2026, 8, 3),
        policy_id="30d",
        strategy="filing_analysis",
        data_sources=("edgar",),
        candidates=[],
        provider_errors={},
        exception=ValueError("bad filing"),
    )
    assert record.status == "strategy_defect"
    assert record.evidence["error_type"] == "ValueError"
```

- [ ] **Step 2: Run and verify the missing-module failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_strategy_health.py -v
```

Expected: collection fails because `metrics.health` does not exist.

- [ ] **Step 3: Implement deterministic classification**

```python
# tradingagents/strategies/metrics/health.py
from __future__ import annotations

from datetime import date
from typing import Collection, Mapping

from .identity import _stable_id
from .models import StrategyHealthRecord


def classify_strategy_run(
    *,
    epoch_id: str,
    session: date,
    policy_id: str,
    strategy: str,
    data_sources: Collection[str],
    candidates: Collection[object],
    provider_errors: Mapping[str, str],
    exception: Exception | None,
) -> StrategyHealthRecord:
    evidence: dict[str, object] = {
        "data_sources": sorted(data_sources),
        "candidate_count": len(candidates),
    }
    if exception is not None:
        status = "strategy_defect"
        evidence.update(
            error_type=type(exception).__name__, error=str(exception)
        )
    elif provider_errors:
        status = "data_failure"
        evidence["provider_errors"] = dict(sorted(provider_errors.items()))
    elif candidates:
        status = "signals"
    else:
        status = "legitimate_no_event"
        evidence["screen_completed"] = True
    return StrategyHealthRecord(
        health_id=_stable_id(
            "health", epoch_id, session, policy_id, strategy
        ),
        epoch_id=epoch_id,
        session=session,
        policy_id=policy_id,
        strategy=strategy,
        status=status,
        signal_count=len(candidates),
        evidence=evidence,
    )
```

- [ ] **Step 4: Wrap every strategy screen and persist all classifications**

Inside `screen_and_enrich`, use:

```python
try:
    candidates = strategy.screen(data, trading_date, params)
    error = None
except Exception as exc:
    candidates = []
    error = exc

health.append(
    classify_strategy_run(
        epoch_id=epoch_id,
        session=date.fromisoformat(trading_date),
        policy_id=horizon,
        strategy=strategy.name,
        data_sources=tuple(strategy.data_sources),
        candidates=candidates,
        provider_errors=_provider_errors(data, strategy.data_sources),
        exception=error,
    )
)
```

If `error` is not `None`, log the exception and continue diagnostic capture;
do not classify it as no-event. Return health records with signals/regime.
The orchestrator passes the same horizon health records to each size scenario,
and `MetricStore.save_strategy_health` remains idempotent by `health_id`.

Before a session becomes promotion-valid, assert:

```python
if len({record.strategy for record in health}) != 12:
    epoch_manager.invalidate_current(session, "unclassified_strategy_silence")
```

- [ ] **Step 5: Add the all-12 and idempotency tests**

Extend `tests/test_strategy_health.py` using the 12 names returned by
`get_paper_trade_strategies()` and assert that two writes of the same
`health_id` leave exactly 12 rows, not 24. Add `MetricStore.read_strategy_health`
with optional `epoch_id` and `session` filters to support this assertion.

- [ ] **Step 6: Run focused orchestration tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_strategy_health.py \
  tests/test_cohort_failure_reporting.py \
  tests/test_multi_strategy.py -v
```

Expected: all tests pass; a thrown strategy screen is recorded as
`strategy_defect`, and unclassified silence invalidates the epoch.

- [ ] **Step 7: Commit**

```bash
git add tradingagents/strategies/metrics/health.py \
  tradingagents/strategies/metrics/store.py \
  tradingagents/strategies/orchestration/multi_strategy_engine.py \
  tradingagents/strategies/orchestration/cohort_orchestrator.py \
  tests/test_strategy_health.py
git commit -m "feat(metrics): persist exhaustive strategy health"
```

---

### Task 8: Production Learning Lock

**Files:**
- Create: `tradingagents/strategies/orchestration/learning_policy.py`
- Modify: `tradingagents/strategies/orchestration/cohort_orchestrator.py`
- Modify: `tradingagents/strategies/orchestration/generation_manager.py`
- Modify: `scripts/run_cohorts.py`
- Modify: `scripts/run_generations.py`
- Modify: `tradingagents/default_config.py`
- Test: `tests/test_learning_disabled.py`
- Modify: `tests/test_cohort_lifecycle.py`
- Modify: `tests/test_30day_simulation.py`

**Interfaces:**
- Produces: `LearningPolicy(mode: Literal["disabled"] = "disabled")`.
- `CohortConfig.learning_policy` defaults to `LearningPolicy()`.
- Production constructors reject every mode other than `disabled`.
- Both learning CLIs exit `2` before opening a state database.

- [ ] **Step 1: Write failing fail-closed and no-mutation tests**

```python
# tests/test_learning_disabled.py
from pathlib import Path

import pytest

from tradingagents.strategies.orchestration.learning_policy import (
    LearningPolicy,
)


def test_learning_policy_rejects_enabled_mode() -> None:
    with pytest.raises(ValueError, match="production learning is disabled"):
        LearningPolicy(mode="enabled")


def test_run_cohorts_learning_refuses_before_state_write(
    tmp_path, monkeypatch
) -> None:
    from scripts import run_cohorts

    monkeypatch.setenv("AUTORESEARCH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        "sys.argv", ["run_cohorts.py", "--learning"]
    )
    with pytest.raises(SystemExit) as exc:
        run_cohorts.main()
    assert exc.value.code == 2
    assert list(tmp_path.iterdir()) == []


def test_metrics_package_has_no_learning_import() -> None:
    for path in Path("tradingagents/strategies/metrics").glob("*.py"):
        assert "strategies.learning" not in path.read_text()
```

- [ ] **Step 2: Run and verify the missing-policy failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_learning_disabled.py -v
```

Expected: collection fails because `learning_policy.py` does not exist.

- [ ] **Step 3: Implement the only accepted production policy**

```python
# tradingagents/strategies/orchestration/learning_policy.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class LearningPolicy:
    mode: Literal["disabled"] = "disabled"

    def __post_init__(self) -> None:
        if self.mode != "disabled":
            raise ValueError(
                "production learning is disabled; only mode='disabled' is accepted"
            )
```

Replace `CohortConfig.adaptive_confidence` and `learning_enabled` with:

```python
learning_policy: LearningPolicy = field(default_factory=LearningPolicy)
```

Always construct production `MultiStrategyEngine` with
`adaptive_confidence=False`. Remove `adaptive_confidence` and
`learning_enabled` from production config defaults and environment parsing.
Direct learning-class tests may use isolated `tmp_path` state, but no production
builder exposes that path.

- [ ] **Step 4: Refuse both CLI paths before manager/orchestrator creation**

At the top of each `--learning`/`run-learning` branch:

```python
print(
    "Production learning is disabled; no generation state was changed.",
    file=sys.stderr,
)
raise SystemExit(2)
```

Change `GenerationManager.run_learning()` to:

```python
def run_learning(self) -> dict[str, dict]:
    raise RuntimeError(
        "production learning is disabled; no subprocess was started"
    )
```

Delete `CohortOrchestrator.run_learning`; no production path may call
`MultiStrategyEngine.run_learning_loop`.

- [ ] **Step 5: Update old enabled-learning tests**

Move algorithm-only learning tests to temporary state and instantiate their
learning classes directly. Replace production cohort assertions with:

```python
assert all(
    cohort.learning_policy.mode == "disabled"
    for cohort in build_default_cohorts(config)
)
```

Delete test fixtures that set `adaptive_confidence=True` or
`learning_enabled=True` on `CohortConfig`.

- [ ] **Step 6: Run focused tests and explicit CLI checks**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_learning_disabled.py \
  tests/test_cohort_lifecycle.py \
  tests/test_30day_simulation.py \
  tests/test_generation_manager.py -v
set +e
.venv/bin/python scripts/run_generations.py run-learning
test "$?" -eq 2
.venv/bin/python scripts/run_cohorts.py --learning
test "$?" -eq 2
set -e
```

Expected: tests pass; both commands print the disabled message and exit `2`
without creating or changing state.

- [ ] **Step 7: Commit**

```bash
git add tradingagents/strategies/orchestration/learning_policy.py \
  tradingagents/strategies/orchestration/cohort_orchestrator.py \
  tradingagents/strategies/orchestration/generation_manager.py \
  tradingagents/default_config.py scripts/run_cohorts.py \
  scripts/run_generations.py tests/test_learning_disabled.py \
  tests/test_cohort_lifecycle.py tests/test_30day_simulation.py
git commit -m "feat(governance): lock production learning disabled"
```

---

### Task 9: Pure Advisory Promotion Gates and Read-Only CLI

**Files:**
- Create: `tradingagents/strategies/metrics/promotion.py`
- Modify: `scripts/run_generations.py`
- Test: `tests/test_promotion_gates.py`

**Interfaces:**
- Produces: `PromotionPolicy` with approved thresholds.
- Produces: `PromotionEvidence` containing integrity, sample, return, drawdown, diversity, and sensitivity fields.
- Produces: `PromotionEvaluator.evaluate(evidence) -> PromotionDecision`.
- CLI: `python scripts/run_generations.py promotion-status --candidate gen_005 --baseline gen_004`.

- [ ] **Step 1: Write failing WAIT, FAIL, eligible, and immutability tests**

```python
# tests/test_promotion_gates.py
from dataclasses import replace

from tradingagents.strategies.metrics.promotion import (
    PromotionDecisionStatus,
    PromotionEvaluator,
    PromotionEvidence,
)


def _passing() -> PromotionEvidence:
    return PromotionEvidence(
        clean_common_sessions=30,
        independent_completed_ideas=50,
        strategy_claim_event_counts={"congressional_trades": 30},
        missing_marks=0,
        stale_marks=0,
        sessions_aligned=True,
        stable_epoch_hashes=True,
        crosses_invalid_boundary=False,
        classified_strategy_count=12,
        cost_categories_present=True,
        risk_limit_breach=False,
        matched_excess_return=0.01,
        winning_strategies=2,
        candidate_max_drawdown=-0.10,
        baseline_max_drawdown=-0.09,
        delayed_fill_excess_return=0.002,
        slippage_20bps_excess_return=0.001,
    )


def test_insufficient_sample_waits() -> None:
    decision = PromotionEvaluator().evaluate(
        replace(_passing(), independent_completed_ideas=29)
    )
    assert decision.status is PromotionDecisionStatus.WAIT
    assert decision.research_review_ready is False


def test_thirty_ideas_marks_initial_research_review_ready() -> None:
    decision = PromotionEvaluator().evaluate(
        replace(_passing(), independent_completed_ideas=30)
    )
    assert decision.status is PromotionDecisionStatus.WAIT
    assert decision.research_review_ready is True


def test_integrity_failure_fails() -> None:
    decision = PromotionEvaluator().evaluate(
        replace(_passing(), missing_marks=1)
    )
    assert decision.status is PromotionDecisionStatus.FAIL


def test_passing_evidence_is_advisory_only() -> None:
    evidence = _passing()
    before = repr(evidence)
    decision = PromotionEvaluator().evaluate(evidence)
    assert decision.status is PromotionDecisionStatus.ELIGIBLE_FOR_MANUAL_REVIEW
    assert repr(evidence) == before
    assert not hasattr(decision, "apply")
```

- [ ] **Step 2: Run and verify the missing-module failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_promotion_gates.py -v
```

Expected: collection fails because `metrics.promotion` does not exist.

- [ ] **Step 3: Implement frozen evidence and policy types**

```python
# tradingagents/strategies/metrics/promotion.py
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PromotionDecisionStatus(str, Enum):
    WAIT = "WAIT"
    FAIL = "FAIL"
    ELIGIBLE_FOR_MANUAL_REVIEW = "ELIGIBLE_FOR_MANUAL_REVIEW"


@dataclass(frozen=True)
class PromotionPolicy:
    min_clean_common_sessions: int = 30
    min_initial_ideas: int = 30
    min_manual_ideas: int = 50
    min_strategy_claim_events: int = 30
    max_drawdown: float = 0.15
    max_drawdown_delta: float = 0.02


@dataclass(frozen=True)
class PromotionEvidence:
    clean_common_sessions: int
    independent_completed_ideas: int
    strategy_claim_event_counts: dict[str, int]
    missing_marks: int
    stale_marks: int
    sessions_aligned: bool
    stable_epoch_hashes: bool
    crosses_invalid_boundary: bool
    classified_strategy_count: int
    cost_categories_present: bool
    risk_limit_breach: bool
    matched_excess_return: float
    winning_strategies: int
    candidate_max_drawdown: float
    baseline_max_drawdown: float
    delayed_fill_excess_return: float
    slippage_20bps_excess_return: float


@dataclass(frozen=True)
class PromotionDecision:
    status: PromotionDecisionStatus
    reasons: tuple[str, ...]
    research_review_ready: bool
```

- [ ] **Step 4: Implement gate ordering**

```python
class PromotionEvaluator:
    def __init__(self, policy: PromotionPolicy | None = None) -> None:
        self.policy = policy or PromotionPolicy()

    def evaluate(self, evidence: PromotionEvidence) -> PromotionDecision:
        failures = []
        if evidence.missing_marks or evidence.stale_marks:
            failures.append("missing_or_stale_marks")
        if not evidence.sessions_aligned:
            failures.append("unaligned_candidate_baseline_benchmark_cash")
        if not evidence.stable_epoch_hashes:
            failures.append("unstable_epoch_hashes")
        if evidence.crosses_invalid_boundary:
            failures.append("invalid_session_or_epoch_bridge")
        if evidence.classified_strategy_count != 12:
            failures.append("unclassified_strategy_silence")
        if not evidence.cost_categories_present:
            failures.append("missing_cost_or_borrow_category")
        if evidence.risk_limit_breach:
            failures.append("risk_limit_breach")
        if failures:
            return PromotionDecision(
                PromotionDecisionStatus.FAIL, tuple(failures), False
            )

        research_review_ready = (
            evidence.clean_common_sessions
            >= self.policy.min_clean_common_sessions
            and evidence.independent_completed_ideas
            >= self.policy.min_initial_ideas
        )
        waits = []
        if evidence.clean_common_sessions < self.policy.min_clean_common_sessions:
            waits.append("need_30_clean_common_sessions")
        if evidence.independent_completed_ideas < self.policy.min_initial_ideas:
            waits.append("need_30_independent_completed_ideas")
        if any(
            count < self.policy.min_strategy_claim_events
            for count in evidence.strategy_claim_event_counts.values()
        ):
            waits.append("strategy_claim_needs_30_unique_matured_events")
        if evidence.independent_completed_ideas < self.policy.min_manual_ideas:
            waits.append("need_50_independent_completed_ideas")
        if waits:
            return PromotionDecision(
                PromotionDecisionStatus.WAIT,
                tuple(waits),
                research_review_ready,
            )

        performance_failures = []
        if evidence.matched_excess_return <= 0:
            performance_failures.append("matched_excess_return_not_positive")
        if evidence.winning_strategies < 2:
            performance_failures.append("winners_from_fewer_than_two_strategies")
        if abs(min(0.0, evidence.candidate_max_drawdown)) > self.policy.max_drawdown:
            performance_failures.append("candidate_drawdown_exceeds_15_percent")
        if (
            evidence.candidate_max_drawdown
            < evidence.baseline_max_drawdown - self.policy.max_drawdown_delta
        ):
            performance_failures.append("drawdown_more_than_two_points_worse")
        if evidence.delayed_fill_excess_return <= 0:
            performance_failures.append("delayed_fill_sensitivity_not_positive")
        if evidence.slippage_20bps_excess_return <= 0:
            performance_failures.append("slippage_20bps_sensitivity_not_positive")
        if performance_failures:
            return PromotionDecision(
                PromotionDecisionStatus.FAIL,
                tuple(performance_failures),
                research_review_ready,
            )
        return PromotionDecision(
            PromotionDecisionStatus.ELIGIBLE_FOR_MANUAL_REVIEW,
            ("manual_review_required",),
            True,
        )
```

Directional accuracy must not appear in `PromotionEvaluator.evaluate`; it is
supporting report evidence only.

- [ ] **Step 5: Add the read-only CLI**

Add parser arguments:

```python
p_promotion = sub.add_parser(
    "promotion-status", help="Evaluate advisory promotion evidence"
)
p_promotion.add_argument("--candidate", required=True)
p_promotion.add_argument("--baseline", required=True)
```

The branch loads both generations through `MetricsService`, constructs
`PromotionEvidence`, prints `PromotionDecision` JSON, and returns. It must not
call `start_generation`, `pause_generation`, `resume_generation`,
`retire_generation`, git, SSH, or service-management functions.

- [ ] **Step 6: Add the sensitivity fixtures**

Extend `tests/test_promotion_gates.py` with deterministic candidate returns for
normal fills, fills delayed one XNYS session, and 20-basis-point adverse
slippage per fill. Assert that either nonpositive sensitivity result returns
`FAIL` with its exact reason.

- [ ] **Step 7: Run focused tests and CLI help**

Run:

```bash
.venv/bin/python -m pytest tests/test_promotion_gates.py -v
.venv/bin/python scripts/run_generations.py promotion-status --help
```

Expected: all tests pass; help lists required `--candidate` and `--baseline`;
no state directory timestamp changes during the evaluator test.

- [ ] **Step 8: Commit**

```bash
git add tradingagents/strategies/metrics/promotion.py \
  scripts/run_generations.py tests/test_promotion_gates.py
git commit -m "feat(governance): add advisory promotion gates"
```

---

### Task 10: Truthful Reports, Dashboard, Documentation, and System Verification

**Files:**
- Modify: `scripts/generate_daily_report.py`
- Modify: `tradingagents/dashboard/data_loaders.py`
- Modify: `tradingagents/dashboard/charts.py`
- Modify: `tradingagents/dashboard/pages/overview.py`
- Modify: `tradingagents/dashboard/pages/returns.py`
- Modify: `tradingagents/dashboard/pages/cohort_matrix.py`
- Modify: `tradingagents/dashboard/email_export.py`
- Modify: `README.md`
- Modify: `AUTORESEARCH_ARCHITECTURE_MAP.md`
- Modify: `assets/autoresearch.svg`
- Modify: `assets/daily-cycle.svg`
- Test: `tests/test_metrics_reporting.py`
- Modify: `tests/test_email_dashboard.py`

**Interfaces:**
- `load_generation_metrics(gen_id, state_dir) -> dict` delegates to `MetricsService.generation_report`.
- Markdown, Streamlit, and email surfaces consume the same dictionary.
- Headline books are exactly the four cohort IDs ending `_size_100k`.
- Scenario-panel return is an arithmetic mean of those four scenario returns and is never labeled AUM.

- [ ] **Step 1: Write failing scope and no-network tests**

```python
# tests/test_metrics_reporting.py
from unittest.mock import patch

from tradingagents.dashboard.email_export import render_dashboard_html


def test_headline_contains_only_four_100k_books(metric_report) -> None:
    assert sorted(metric_report["headline_books"]) == [
        "horizon_1y_size_100k",
        "horizon_30d_size_100k",
        "horizon_3m_size_100k",
        "horizon_6m_size_100k",
    ]
    assert len(metric_report["stress_tests"]) == 12


@patch("yfinance.download", side_effect=AssertionError("network forbidden"))
def test_email_render_uses_persisted_benchmarks_only(
    _download, generation_metadata
) -> None:
    html = render_dashboard_html(
        [generation_metadata], date="2026-08-31"
    )
    assert "Dependent scenario portfolios" in html
    assert "Equal-weighted scenario panel" in html
    assert "Fund AUM" not in html


def test_report_discloses_epoch_quality_counts_and_costs(rendered_report) -> None:
    required = (
        "Metric epoch",
        "Schema v2",
        "Valuation timestamp",
        "Benchmark timestamp",
        "Valid sessions",
        "Unique catalysts",
        "Strategy decisions",
        "Fills",
        "Closed trades",
        "Missing/stale marks",
        "Gross exposure",
        "Net exposure",
        "Costs",
    )
    assert all(label in rendered_report for label in required)
```

- [ ] **Step 2: Run and verify legacy reporting failures**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_metrics_reporting.py tests/test_email_dashboard.py -v
```

Expected: failures show the current capital-weighted/summed-cohort headline and
the live yfinance benchmark call.

- [ ] **Step 3: Replace dashboard loaders and copy**

Add:

```python
def load_generation_metrics(gen_id: str, gen_state_dir: str) -> dict[str, Any]:
    service = _metrics_service_for_generation(gen_state_dir)
    return service.generation_report()
```

Make `load_cohort_metrics`, `load_signal_stats`, and `load_equity_history`
compatibility wrappers project fields from that report. Remove
`load_current_prices` and all yfinance calls from dashboard loaders.

Use these exact labels:

```python
HEADLINE_TITLE = "Four $100k horizon books"
PANEL_LABEL = "Equal-weighted scenario panel"
DEPENDENCE_DISCLOSURE = (
    "Dependent scenario portfolios: shared signals and market data mean the "
    "books are not independent observations and are not combined fund AUM."
)
STRESS_TEST_LABEL = "$5k/$10k/$50k concentration stress tests"
SHARPE_LABEL = "Annualized daily net Sharpe"
INFORMATION_RATIO_LABEL = "Annualized matched-benchmark information ratio"
ACCURACY_LABEL = "Directional accuracy (5 XNYS sessions)"
```

Hide Sharpe and information ratio when their values are `None` and show
`"Insufficient history (<30 valid sessions)"`.

- [ ] **Step 4: Delete live benchmark fetching**

Delete `BENCHMARK_TICKERS`, `BENCHMARK_LABELS`,
`_fetch_benchmark_returns`, and every `yf.download` branch from
`email_export.py`. Render persisted SPY, BIL, matched benchmark, gross/net
exposure, and benchmark timestamps from the metric report.

Make `generate_daily_report.py` call only `load_generation_metrics`; delete its
calendar-day hit-rate, summed closed-trade return, and trade-P&L Sharpe blocks.

- [ ] **Step 5: Update documentation and diagrams**

In `README.md` and `AUTORESEARCH_ARCHITECTURE_MAP.md`, state:

```text
EventEdge runs 16 dependent scenario portfolios. Headline performance shows
four separate $100k horizon books plus an equal-weighted scenario panel; the
panel is not investable fund AUM. Smaller books are concentration stress tests.
Metrics use XNYS sessions, next-session-open signal outcomes, persisted SPY/BIL
benchmarks, explicit costs, and immutable schema-v2 epochs. Production learning
is disabled. Promotion output is advisory and requires Pedro's manual review.
Covered-call execution remains inactive scaffolding.
```

Update `assets/autoresearch.svg` and `assets/daily-cycle.svg` source text to show
`18:00 ET close-safe cycle`, `next-session open`, `ledger snapshot + SPY/BIL`,
`metrics v2`, `learning disabled`, and `manual review only`. Preserve the
existing dark-theme styles.

- [ ] **Step 6: Run report/dashboard tests and scan for forbidden formulas**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_metrics_reporting.py tests/test_email_dashboard.py -v
rg -n "yf\\.download|return_5d|statistics\\.stdev|sum\\(pnl_pcts\\)" \
  scripts/generate_daily_report.py \
  tradingagents/dashboard \
  tradingagents/strategies/orchestration/cohort_comparison.py \
  tradingagents/strategies/orchestration/generation_comparison.py
```

Expected: tests pass; `rg` exits `1` with no matches.

- [ ] **Step 7: Run the full deterministic suite**

Run:

```bash
.venv/bin/python -m pytest tests/ -m "not live" -q
```

Expected: all offline tests pass; no external API or LLM call occurs.

- [ ] **Step 8: Verify JSON projections and legacy dry-run**

Run:

```bash
tmp_dir=$(mktemp -d)
cp -R data/generations/gen_003 "$tmp_dir/gen_003"
.venv/bin/python scripts/migrate_metrics_v2.py \
  --manifest data/generations/manifest.json \
  --output "$tmp_dir/legacy_registry.json" \
  --write
diff -qr data/generations/gen_003 "$tmp_dir/gen_003"
.venv/bin/python -m pytest \
  tests/test_metrics_migration.py \
  tests/test_metrics_service.py -v
```

Expected: `diff` prints nothing and exits `0`; only the new registry beside the
copy exists; compatibility and service tests pass.

- [ ] **Step 9: Run the mocked clean-generation smoke test**

Add a deterministic fixture to `tests/test_metrics_service.py` that creates
paired `gen_004` and `gen_005` temporary ledgers with 30 XNYS sessions, SPY/BIL
observations, explicit zero/nonzero cost rows, all 12 health classifications,
and no missing marks. Run:

```bash
.venv/bin/python -m pytest \
  tests/test_metrics_service.py::test_clean_gen004_gen005_mocked_smoke -v
```

Expected: PASS; both generations have schema v2; comparison contains identical
common sessions; no network mock is called.

- [ ] **Step 10: Measure optimization constraints**

Run before and after the full mocked 16-cohort report:

```bash
/usr/bin/time -l .venv/bin/python -m pytest \
  tests/test_metrics_service.py::test_clean_gen004_gen005_mocked_smoke -q \
  2> /tmp/eventedge-metrics-time.txt
rg "real|maximum resident set size" /tmp/eventedge-metrics-time.txt
.venv/bin/python -m pytest \
  tests/test_metrics_service.py::test_metrics_add_no_api_or_llm_calls -v
```

Expected: the smoke test passes, maximum resident set size is below
`8589934592` bytes, and the call-count test reports zero new API and LLM calls
for reporting/comparison/promotion.

- [ ] **Step 11: Review diff and commit**

Run:

```bash
git diff --check
git status --short
```

Expected: no whitespace errors; only files listed in this plan are modified,
plus any separately reviewed P0/P1 files already present on the branch.

Commit:

```bash
git add scripts/generate_daily_report.py \
  tradingagents/dashboard README.md AUTORESEARCH_ARCHITECTURE_MAP.md \
  assets/autoresearch.svg assets/daily-cycle.svg \
  tests/test_metrics_reporting.py tests/test_email_dashboard.py
git commit -m "docs(metrics): publish truthful v2 performance surfaces"
```

---

## Completion Gate

Do not create `gen_004` or `gen_005` from this implementation session. Before
requesting review, verify all of the following:

- `git diff --check` is clean.
- Every focused command in Tasks 1-10 passes.
- `.venv/bin/python -m pytest tests/ -m "not live" -q` passes.
- Metrics/report/promotion tests make zero external API and LLM calls.
- No v2 return bridges an invalid session or epoch.
- No performance consumer defines a local hit-rate, Sharpe, drawdown, or return formula.
- No report or dashboard imports yfinance for benchmarks or marks.
- Both production learning commands exit `2` without state mutation.
- Promotion evaluation returns only an immutable advisory decision.
- Legacy migration leaves `gen_003` bytes unchanged.
- The final diff does not include deployment, timer, production state, generation creation, merge, or live-patching changes.

After branch review and merge, generation creation and the 18:00 ET production
timer change remain separate Pedro-authorized deployment work.
