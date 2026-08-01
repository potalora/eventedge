# Executable Paper Ledger Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace same-session close fills and reconstructed JSON balances with an idempotent, SQLite-backed paper ledger that stages orders before their next-session execution price exists.

**Architecture:** Keep shared market-data fetching and four horizon screens, but split the daily cycle into an execution-first phase and a cutoff-safe screening phase. Each cohort owns one authoritative SQLite ledger; deterministic signal, intent, fill, accrual, action, and snapshot IDs make every phase restartable. Raw XNYS daily bars drive fills and marks, while `paper_trades.json` and `equity_snapshots.jsonl` become atomically generated compatibility projections.

**Tech Stack:** Python 3.10+, `sqlite3`, `decimal.Decimal`, `dataclasses`, `hashlib`, `exchange-calendars>=4.13.2,<5`, yfinance with `auto_adjust=False`, pytest.

## Global Constraints

- Execute in an isolated worktree on `codex/p0-p3-foundation`, created from
  the approved planning commit on `codex/p0-p3-portfolio-integrity`. Never
  commit on, push, or merge `main`; never force-push.
- Do not deploy, modify `/home/hermes/trading_agents`, change an active generation, create `gen_004`, archive `gen_003`, or install the production timer without Pedro's explicit current-conversation authorization. This plan ends at a tested topic branch and PR handoff.
- `gen_003` and every legacy JSON artifact are immutable. Migration is a read-only dry run into temporary state; corrected evidence starts with a clean `gen_004` ledger after merge and operator approval.
- Production stays paper-only. `execution.mode != "paper"` must fail closed in the cohort path. Covered-call execution remains inactive.
- P0 owns execution-domain records, ledger persistence, and read interfaces. P2 owns metric formulas and metric-epoch boundary policy; P0 stores `epoch_id` on records and provides the `metric_epochs` table without implementing P2 calculations.
- Use `Decimal` end-to-end for prices, cash, P&L, costs, margin, and marks. Persist decimals as canonical text using `format(value, "f")`; convert to float only in compatibility projections.
- A missing, stale, future, adjusted, NaN, infinite, or nonpositive required bar invalidates the cohort/session before any economic mutation. There is no entry-price or prior-mark fallback.
- Use `exchange_calendars.get_calendar("XNYS")`; do not retain or extend the manually maintained holiday list.
- The daily phase order is fixed: validate bars/actions; apply actions; execute exits; execute entries; accrue borrow/financing; mark; persist benchmarks/snapshot; screen cutoff-safe data; persist signals/intents; project JSON.
- All external APIs and LLMs are mocked in tests. Preserve shared fetches and keep peak RSS below 8 GB; the production service remains capped at 4 GB.
- Run every focused red/green command exactly as written. Before each commit, inspect `git diff --check` and the staged diff. Subagents must not commit.

## File Map

**Create**

- `tradingagents/strategies/execution/__init__.py` — exports the canonical P0 execution contracts.
- `tradingagents/strategies/execution/models.py` — immutable bars, actions, signals, intents, fills, account state, snapshots, and benchmark records.
- `tradingagents/strategies/execution/ids.py` — canonical deterministic ID generation.
- `tradingagents/strategies/execution/price_source.py` — raw inclusive-bar protocol, validation, and yfinance adapter.
- `tradingagents/strategies/execution/cost_model.py` — versioned adverse slippage, explicit fees, borrow, and financing calculations.
- `tradingagents/strategies/execution/stop_execution.py` — deterministic long/short stop trigger basis.
- `tradingagents/strategies/state/portfolio_ledger.py` — per-cohort SQLite schema, transactions, accounting mutations, and authoritative reads.
- `tradingagents/strategies/state/compatibility_projection.py` — atomic JSON projections from ledger rows.
- `tradingagents/strategies/orchestration/session_executor.py` — restartable execution-first daily state machine.
- `scripts/migrate_ledger_state.py` — read-only legacy inspection and clean-ledger readiness check.
- `tests/test_execution_models.py`
- `tests/test_market_data_contract.py`
- `tests/test_portfolio_ledger.py`
- `tests/test_execution_costs.py`
- `tests/test_order_lifecycle.py`
- `tests/test_corporate_actions.py`
- `tests/test_session_executor.py`
- `tests/test_compatibility_projection.py`
- `tests/test_ledger_migration.py`

**Modify**

- `pyproject.toml` — pin the XNYS calendar dependency.
- `tradingagents/default_config.py` — add explicit versioned paper-ledger configuration.
- `tradingagents/strategies/orchestration/trading_calendar.py` — replace manual holidays with XNYS sessions.
- `tradingagents/strategies/data_sources/yfinance_source.py` — make legacy price downloads explicitly unadjusted and delegate execution bars/actions to the new adapter.
- `tradingagents/strategies/trading/execution_bridge.py` — stage intents and execute due intents instead of filling recommendations immediately.
- `tradingagents/execution/base_broker.py` — add stable client-order and reconciliation contracts.
- `tradingagents/execution/paper_broker.py` — become a ledger-backed adapter; remove JSON reconstruction as an authority.
- `tradingagents/execution/alpaca_broker.py` — pass stable client order IDs and reconcile unresolved orders; live cohort execution remains disabled.
- `tradingagents/strategies/trading/paper_trader.py` — read/project ledger fills rather than originate accounting records.
- `tradingagents/strategies/orchestration/multi_strategy_engine.py` — separate cutoff-safe screening/intent creation from execution and remove close fills.
- `tradingagents/strategies/orchestration/cohort_orchestrator.py` — run execution for all cohorts before shared horizon screening and inject cohort ledgers.
- `tradingagents/strategies/state/equity_snapshot.py` — delegate writes/reads to the compatibility projection and remove entry-price fallback.
- `tradingagents/strategies/state/state.py` — route paper-trade compatibility reads through the ledger when present.
- `tradingagents/strategies/learning/signal_journal.py` — mirror authoritative `SignalRecord` rows without creating identities.
- `scripts/run_cohorts.py` — require an exact XNYS session and expose phase failures.
- `scripts/run_generations.py` — preserve exact session validation and fail the generation if any cohort session is invalid.
- `scripts/daily_trading.sh` — rely on XNYS validation instead of weekday-only acceptance.
- `deploy/systemd/trade.timer` — describe and schedule the approved 18:00 ET cycle.
- `README.md`
- `AUTORESEARCH_ARCHITECTURE_MAP.md`
- `assets/autoresearch.svg`
- `assets/daily-cycle.svg`
- `tests/test_execution.py`
- `tests/test_execution_bridge_shorts.py`
- `tests/test_paper_broker_shorts.py`
- `tests/test_equity_snapshot_nan.py`
- `tests/test_fetch_timeout.py`
- `tests/test_cohort_lifecycle.py`
- `tests/test_generation_manager.py`
- `tests/test_30day_simulation.py`
- `tests/test_integration_shorts.py`

---

## Preflight

- [ ] Fetch without changing production or user state:

```bash
git fetch private
git status --short
```

Expected: fetch succeeds. Preserve every pre-existing tracked or untracked user file; stop if any overlaps a file in this plan.

- [ ] Create the required topic branch from the fetched remote:

```bash
git switch --create codex/p0-p3-foundation codex/p0-p3-portfolio-integrity
git branch --show-current
```

Expected: `codex/p0-p3-foundation`. Create this branch in an isolated
worktree with the `using-git-worktrees` skill instead of switching the user's
current checkout.

### Task 1: Add immutable execution contracts and stable identities

**Files:**

- Create: `tradingagents/strategies/execution/__init__.py`
- Create: `tradingagents/strategies/execution/models.py`
- Create: `tradingagents/strategies/execution/ids.py`
- Create: `tests/test_execution_models.py`

**Interfaces:**

```python
stable_id(kind: str, *parts: object) -> str
SignalRecord.signal_id == stable_id(
    "signal", epoch_id, strategy, policy_id, direction, event_key
)
OrderIntent.intent_id == stable_id(
    "intent", cohort_id, eligible_session, side, requested_qty,
    price_rule, signal_ids
)
Fill.fill_id == stable_id("fill", intent_id, session, quantity)
```

- [ ] **Step 1: Write failing contract and determinism tests**

Create `tests/test_execution_models.py`:

```python
from datetime import date, datetime, timezone
from decimal import Decimal

from tradingagents.strategies.execution.ids import stable_id
from tradingagents.strategies.execution.models import (
    Fill,
    MarketBar,
    OrderIntent,
    SignalRecord,
)


UTC = timezone.utc


def test_stable_id_is_order_stable_for_nested_mappings():
    left = stable_id("event", {"b": 2, "a": ["x", 1]})
    right = stable_id("event", {"a": ["x", 1], "b": 2})
    assert left == right
    assert left.startswith("event_")


def test_signal_identity_includes_epoch_and_policy():
    base = ("epoch-2", "litigation", "30d", "short", "docket-17")
    assert stable_id("signal", *base) != stable_id(
        "signal", "epoch-3", *base[1:]
    )
    assert stable_id("signal", *base) != stable_id(
        "signal", base[0], base[1], "3m", *base[3:]
    )


def test_execution_records_reject_float_prices():
    try:
        MarketBar(
            ticker="AAPL",
            session=date(2026, 7, 31),
            open=100.0,
            high=Decimal("102"),
            low=Decimal("99"),
            close=Decimal("101"),
            source="yfinance",
            fetched_at=datetime(2026, 7, 31, 22, tzinfo=UTC),
            adjusted=False,
        )
    except TypeError as exc:
        assert "Decimal" in str(exc)
    else:
        raise AssertionError("float execution price was accepted")


def test_intent_and_fill_are_frozen():
    intent = OrderIntent(
        intent_id="intent-1",
        signal_ids=("signal-1",),
        cohort_id="horizon_30d_size_5k",
        side="buy",
        requested_qty=10,
        created_at=datetime(2026, 7, 31, 22, 5, tzinfo=UTC),
        eligible_session=date(2026, 8, 3),
        price_rule="next_session_open",
        status="pending",
        stop_price=None,
        external_order_id=None,
    )
    fill = Fill(
        fill_id="fill-1",
        intent_id=intent.intent_id,
        side=intent.side,
        session=intent.eligible_session,
        effective_at=datetime(2026, 8, 3, 13, 30, tzinfo=UTC),
        processed_at=datetime(2026, 8, 3, 22, tzinfo=UTC),
        reference_price=Decimal("100"),
        fill_price=Decimal("100.10"),
        quantity=10,
        slippage=Decimal("1.00"),
        commission=Decimal("0"),
        other_fees=Decimal("0"),
    )
    assert fill.intent_id == intent.intent_id
    try:
        fill.quantity = 11
    except Exception:
        pass
    else:
        raise AssertionError("Fill must be immutable")
```

- [ ] **Step 2: Run the tests and verify the import failure**

Run: `.venv/bin/python -m pytest tests/test_execution_models.py -v`

Expected: collection fails with `ModuleNotFoundError: No module named 'tradingagents.strategies.execution'`.

- [ ] **Step 3: Implement canonical serialization and IDs**

Create `tradingagents/strategies/execution/ids.py`:

```python
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any


def _canonical(value: Any) -> Any:
    if is_dataclass(value):
        return _canonical(asdict(value))
    if isinstance(value, dict):
        return {
            str(key): _canonical(value[key])
            for key in sorted(value, key=lambda item: str(item))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"Unsupported stable-ID component: {type(value).__name__}")


def stable_id(kind: str, *parts: object) -> str:
    if not kind or not kind.replace("_", "").isalnum():
        raise ValueError("kind must be a non-empty alphanumeric label")
    payload = json.dumps(
        _canonical(parts),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return f"{kind}_{hashlib.sha256(payload).hexdigest()[:32]}"
```

- [ ] **Step 4: Implement the immutable domain models**

Create `tradingagents/strategies/execution/models.py` with the exact approved fields plus P0 persistence fields:

```python
from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import date, datetime
from decimal import Decimal
from typing import Literal


Side = Literal["buy", "sell", "short", "cover"]
Direction = Literal["long", "short", "neutral"]
IntentStatus = Literal["pending", "filled", "rejected", "cancelled"]


def _require_decimals(instance: object) -> None:
    for field in fields(instance):
        value = getattr(instance, field.name)
        if field.name in {
            "open", "high", "low", "close", "ratio", "cash_per_share",
            "reference_close", "stop_price", "reference_price", "fill_price",
            "slippage", "commission", "other_fees", "cash",
            "long_market_value", "short_liability", "gross_exposure",
            "net_exposure", "margin_used", "buying_power", "realized_pnl",
            "unrealized_pnl", "gross_equity", "slippage_cost",
            "commission_cost", "other_fees", "borrow_cost", "financing_cost",
            "dividend_cash", "net_equity", "high_water_mark", "amount",
        } and value is not None and not isinstance(value, Decimal):
            raise TypeError(f"{field.name} must be Decimal")


@dataclass(frozen=True)
class MarketBar:
    ticker: str
    session: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    source: str
    fetched_at: datetime
    adjusted: bool

    def __post_init__(self) -> None:
        _require_decimals(self)


@dataclass(frozen=True)
class CorporateAction:
    action_id: str
    ticker: str
    session: date
    action_type: Literal["split", "cash_dividend"]
    ratio: Decimal | None
    cash_per_share: Decimal | None
    source: str
    fetched_at: datetime
    verified: bool

    def __post_init__(self) -> None:
        _require_decimals(self)


@dataclass(frozen=True)
class SignalRecord:
    signal_id: str
    epoch_id: str
    policy_id: str
    event_key: str
    strategy: str
    ticker: str
    direction: Direction
    event_at: datetime | None
    observed_at: datetime
    reference_session: date
    reference_close: Decimal
    decision_at: datetime
    evidence_hash: str

    def __post_init__(self) -> None:
        _require_decimals(self)


@dataclass(frozen=True)
class OrderIntent:
    intent_id: str
    signal_ids: tuple[str, ...]
    cohort_id: str
    side: Side
    requested_qty: int
    created_at: datetime
    eligible_session: date
    price_rule: Literal["next_session_open", "resting_stop"]
    status: IntentStatus
    stop_price: Decimal | None
    external_order_id: str | None

    def __post_init__(self) -> None:
        _require_decimals(self)
        if self.requested_qty <= 0:
            raise ValueError("requested_qty must be positive")


@dataclass(frozen=True)
class Fill:
    fill_id: str
    intent_id: str
    side: Side
    session: date
    effective_at: datetime
    processed_at: datetime
    reference_price: Decimal
    fill_price: Decimal
    quantity: int
    slippage: Decimal
    commission: Decimal
    other_fees: Decimal

    def __post_init__(self) -> None:
        _require_decimals(self)


@dataclass(frozen=True)
class FillResult:
    status: Literal["filled", "rejected", "pending"]
    fill: Fill | None
    reason: str


@dataclass(frozen=True)
class LedgerEvent:
    event_id: str
    session: date
    event_type: str
    amount: Decimal
    flagged: bool
    detail: str

    def __post_init__(self) -> None:
        _require_decimals(self)


@dataclass(frozen=True)
class AccountState:
    cohort_id: str
    cash: Decimal
    long_market_value: Decimal
    short_liability: Decimal
    margin_used: Decimal
    buying_power: Decimal
    net_equity: Decimal
    high_water_mark: Decimal

    def __post_init__(self) -> None:
        _require_decimals(self)


@dataclass(frozen=True)
class AccountSnapshot:
    snapshot_id: str
    cohort_id: str
    epoch_id: str
    session: date
    valuation_at: datetime
    cash: Decimal
    long_market_value: Decimal
    short_liability: Decimal
    gross_exposure: Decimal
    net_exposure: Decimal
    margin_used: Decimal
    buying_power: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    gross_equity: Decimal
    slippage_cost: Decimal
    commission_cost: Decimal
    other_fees: Decimal
    borrow_cost: Decimal
    financing_cost: Decimal
    dividend_cash: Decimal
    net_equity: Decimal
    high_water_mark: Decimal
    valid: bool
    invalid_reason: str

    def __post_init__(self) -> None:
        _require_decimals(self)


@dataclass(frozen=True)
class BenchmarkObservation:
    observation_id: str
    cohort_id: str
    epoch_id: str
    session: date
    symbol: str
    close: Decimal
    return_basis: Literal["total_return_adjusted"]
    source: str
    observed_at: datetime
    valid: bool
    invalid_reason: str

    def __post_init__(self) -> None:
        _require_decimals(self)
```

Export every public model and `stable_id` from `tradingagents/strategies/execution/__init__.py`.

- [ ] **Step 5: Run the focused tests**

Run: `.venv/bin/python -m pytest tests/test_execution_models.py -v`

Expected: `4 passed`.

- [ ] **Step 6: Commit the contracts**

```bash
git add tradingagents/strategies/execution tests/test_execution_models.py
git diff --check
git commit -m "feat(execution): add immutable ledger contracts"
```

---

### Task 2: Replace manual holidays and enforce raw inclusive daily bars

**Files:**

- Modify: `pyproject.toml`
- Modify: `tradingagents/strategies/orchestration/trading_calendar.py`
- Create: `tradingagents/strategies/execution/price_source.py`
- Modify: `tradingagents/strategies/data_sources/yfinance_source.py`
- Create: `tests/test_market_data_contract.py`
- Modify: `tests/test_fetch_timeout.py`

**Interfaces:**

```python
PriceSource.get_daily_bars(
    tickers: list[str],
    start_session: date,
    end_session_inclusive: date,
    adjusted: bool = False,
) -> dict[tuple[str, date], MarketBar]

PriceSource.get_corporate_actions(
    tickers: list[str],
    session: date,
) -> list[CorporateAction]

PriceSource.get_total_return_closes(
    symbols: list[str],
    start_session: date,
    end_session_inclusive: date,
) -> dict[tuple[str, date], Decimal]

is_session(session: date) -> bool
next_session(session: date) -> date
previous_session(session: date) -> date
session_open(session: date) -> datetime
session_close(session: date) -> datetime
```

- [ ] **Step 1: Add failing calendar, yfinance, and validation tests**

In `tests/test_market_data_contract.py`, mock `yfinance.download` and assert:

```python
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pandas as pd
import pytest

from tradingagents.strategies.execution.price_source import (
    BarValidationError,
    YFinancePriceSource,
    validate_required_bars,
)
from tradingagents.strategies.orchestration.trading_calendar import (
    is_session,
    next_session,
    session_close,
)


def test_xnys_weekend_holiday_and_early_close():
    assert next_session(date(2026, 7, 2)) == date(2026, 7, 6)
    assert not is_session(date(2026, 7, 3))
    assert session_close(date(2026, 11, 27)).hour == 18


@patch("tradingagents.strategies.execution.price_source.yf.download")
def test_yfinance_end_is_inclusive_and_raw(mock_download):
    columns = pd.MultiIndex.from_product(
        [["Open", "High", "Low", "Close"], ["AAPL"]]
    )
    mock_download.return_value = pd.DataFrame(
        [[100, 103, 99, 102]],
        index=pd.DatetimeIndex(["2026-07-31"]),
        columns=columns,
    )
    source = YFinancePriceSource(now=lambda: datetime(2026, 7, 31, 22, tzinfo=timezone.utc))
    bars = source.get_daily_bars(
        ["AAPL"], date(2026, 7, 31), date(2026, 7, 31)
    )
    kwargs = mock_download.call_args.kwargs
    assert kwargs["end"] == "2026-08-01"
    assert kwargs["auto_adjust"] is False
    assert bars[("AAPL", date(2026, 7, 31))].close == Decimal("102")
    assert bars[("AAPL", date(2026, 7, 31))].adjusted is False


@patch("tradingagents.strategies.execution.price_source.yf.download")
def test_benchmark_closes_are_total_return_adjusted(mock_download):
    columns = pd.MultiIndex.from_product([["Close"], ["SPY", "BIL"]])
    mock_download.return_value = pd.DataFrame(
        [[550.0, 91.5]],
        index=pd.DatetimeIndex(["2026-07-31"]),
        columns=columns,
    )
    source = YFinancePriceSource(
        now=lambda: datetime(2026, 7, 31, 22, tzinfo=timezone.utc)
    )
    closes = source.get_total_return_closes(
        ["SPY", "BIL"], date(2026, 7, 31), date(2026, 7, 31)
    )
    assert mock_download.call_args.kwargs["auto_adjust"] is True
    assert closes[("SPY", date(2026, 7, 31))] == Decimal("550.0")


def test_missing_adjusted_future_and_stale_bars_fail_closed():
    with pytest.raises(BarValidationError, match="missing"):
        validate_required_bars({}, {"AAPL"}, date(2026, 7, 31), datetime.now(timezone.utc))
```

Add cases using `MarketBar` for adjusted, future `fetched_at`, stale `fetched_at` beyond 24 hours, nonpositive values, `NaN`, and terminal-session absence.

- [ ] **Step 2: Run the tests and verify they fail**

Run: `.venv/bin/python -m pytest tests/test_market_data_contract.py tests/test_fetch_timeout.py -v`

Expected: collection fails because `price_source.py` and the new calendar functions do not exist.

- [ ] **Step 3: Pin the exchange calendar dependency**

Add to `project.dependencies` in `pyproject.toml`:

```toml
    "exchange-calendars>=4.13.2,<5",
```

Install the editable project:

Run: `.venv/bin/pip install -e .`

Expected: `exchange-calendars` resolves within `>=4.13.2,<5`.

- [ ] **Step 4: Replace the manual calendar**

Replace `trading_calendar.py` with an XNYS-backed implementation. Normalize calendar timestamps to UTC and return Python dates:

```python
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import exchange_calendars
import pandas as pd


_XNYS = exchange_calendars.get_calendar("XNYS")
_ET = ZoneInfo("America/New_York")


def _label(session: date) -> pd.Timestamp:
    return pd.Timestamp(session, tz="UTC")


def is_session(session: date) -> bool:
    return bool(_XNYS.is_session(_label(session)))


def next_session(session: date) -> date:
    return _XNYS.date_to_session(_label(session), direction="next").date() if not is_session(session) else _XNYS.next_session(_label(session)).date()


def previous_session(session: date) -> date:
    return _XNYS.date_to_session(_label(session), direction="previous").date() if not is_session(session) else _XNYS.previous_session(_label(session)).date()


def session_open(session: date) -> datetime:
    if not is_session(session):
        raise ValueError(f"{session} is not an XNYS session")
    return _XNYS.session_open(_label(session)).to_pydatetime()


def session_close(session: date) -> datetime:
    if not is_session(session):
        raise ValueError(f"{session} is not an XNYS session")
    return _XNYS.session_close(_label(session)).to_pydatetime()


def resolve_trading_date(date_str: str | None = None) -> str:
    local = datetime.now(_ET).date() if date_str is None else date.fromisoformat(date_str)
    if is_session(local):
        return local.isoformat()
    return _XNYS.date_to_session(_label(local), direction="previous").date().isoformat()
```

- [ ] **Step 5: Implement the raw price adapter and validator**

In `price_source.py`, define a `PriceSource` protocol and
`YFinancePriceSource`. `get_daily_bars()` must call:

```python
frame = yf.download(
    normalize_tickers(tickers),
    start=start_session.isoformat(),
    end=(end_session_inclusive + timedelta(days=1)).isoformat(),
    auto_adjust=adjusted,
    actions=True,
    progress=False,
    timeout=30,
)
```

Convert values through `Decimal(str(value))`, reject non-finite values with `value.is_finite()`, assert every requested terminal ticker has exactly `end_session_inclusive`, and validate:

```python
def validate_required_bars(
    bars: dict[tuple[str, date], MarketBar],
    tickers: set[str],
    session: date,
    as_of: datetime,
    max_fetch_age: timedelta = timedelta(hours=24),
) -> None:
    errors: list[str] = []
    for ticker in sorted(tickers):
        bar = bars.get((ticker, session))
        if bar is None:
            errors.append(f"missing {ticker}/{session}")
            continue
        values = (bar.open, bar.high, bar.low, bar.close)
        if bar.adjusted:
            errors.append(f"adjusted {ticker}/{session}")
        if any(not value.is_finite() or value <= 0 for value in values):
            errors.append(f"invalid {ticker}/{session}")
        if bar.fetched_at > as_of:
            errors.append(f"future {ticker}/{session}")
        if as_of - bar.fetched_at > max_fetch_age:
            errors.append(f"stale {ticker}/{session}")
        if not (bar.low <= bar.open <= bar.high and bar.low <= bar.close <= bar.high):
            errors.append(f"incoherent {ticker}/{session}")
    if errors:
        raise BarValidationError("; ".join(errors))
```

Parse yfinance `Stock Splits` and `Dividends` columns into verified `CorporateAction` objects with deterministic IDs. A nonzero action whose terms cannot be parsed must raise `CorporateActionValidationError`.

Implement `get_total_return_closes()` with a separate `yf.download()` call
using the same inclusive boundary and `auto_adjust=True`. It returns Close
only, never a `MarketBar`, and is used exclusively to construct
`BenchmarkObservation(return_basis="total_return_adjusted")`. This prevents
adjusted benchmark prices from reaching fills or position marks.

- [ ] **Step 6: Make legacy downloads explicit**

In `YFinanceSource.fetch_prices()` and `fetch_vix()`, add `auto_adjust=False`. Keep their DataFrame interface for strategy screens, but document that execution code may only use `YFinancePriceSource.get_daily_bars()`.

- [ ] **Step 7: Run focused and regression tests**

Run: `.venv/bin/python -m pytest tests/test_market_data_contract.py tests/test_fetch_timeout.py -v`

Expected: all tests pass.

- [ ] **Step 8: Commit the data contract**

```bash
git add pyproject.toml tradingagents/strategies/orchestration/trading_calendar.py \
  tradingagents/strategies/execution/price_source.py \
  tradingagents/strategies/data_sources/yfinance_source.py \
  tests/test_market_data_contract.py tests/test_fetch_timeout.py
git diff --check
git commit -m "feat(market-data): enforce raw XNYS session bars"
```

---

### Task 3: Create the authoritative per-cohort SQLite ledger

**Files:**

- Create: `tradingagents/strategies/state/portfolio_ledger.py`
- Modify: `tradingagents/strategies/state/__init__.py`
- Create: `tests/test_portfolio_ledger.py`

**Interfaces:**

```python
PortfolioLedger(path: Path, cohort_id: str, initial_cash: Decimal)
PortfolioLedger.record_signal(signal: SignalRecord) -> None
PortfolioLedger.stage_intent(intent: OrderIntent) -> None
PortfolioLedger.pending_intents(session: date) -> list[OrderIntent]
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
    policy_id: str | None = None,
) -> list[SignalRecord]
PortfolioLedger.read_fills(
    start_session: date | None = None,
    end_session: date | None = None,
    epoch_id: str | None = None,
) -> list[Fill]
```

- [ ] **Step 1: Write failing schema, persistence, duplicate, and restart tests**

Create tests that initialize `tmp_path / "portfolio.db"`, inspect `sqlite_master`, and require:

```python
REQUIRED_TABLES = {
    "schema_metadata", "metric_epochs", "session_runs", "session_phases",
    "signals", "order_intents", "intent_signals", "order_status_transitions",
    "external_orders", "fills", "fill_costs", "lots", "lot_closures",
    "corporate_actions", "lot_action_applications", "cash_events",
    "borrow_accruals",
    "financing_accruals", "dividend_events", "fee_events", "marks",
    "account_snapshots", "benchmark_observations",
}
```

Assert SQLite settings `journal_mode=wal`, `foreign_keys=1`, and `synchronous=2`; a duplicate identical signal/intent is a no-op; a duplicate ID with different fields raises `LedgerConflictError`; reopening the database returns the same pending intent and initial cash.

- [ ] **Step 2: Run the tests and verify the failure**

Run: `.venv/bin/python -m pytest tests/test_portfolio_ledger.py -v`

Expected: collection fails because `PortfolioLedger` does not exist.

- [ ] **Step 3: Implement initialization and schema**

Use one connection per `PortfolioLedger`, `isolation_level=None`, `row_factory=sqlite3.Row`, and this transaction boundary:

```python
@contextmanager
def transaction(self) -> Iterator[sqlite3.Connection]:
    self._connection.execute("BEGIN IMMEDIATE")
    try:
        yield self._connection
    except BaseException:
        self._connection.execute("ROLLBACK")
        raise
    else:
        self._connection.execute("COMMIT")
```

Create every required table from a module-level tuple of complete DDL statements. Use `TEXT` for all decimals and ISO timestamps, `INTEGER` for quantities/booleans, foreign keys for intent/signal/fill relations, and primary keys for every stable ID. Required uniqueness constraints:

```sql
CREATE UNIQUE INDEX ux_snapshots_cohort_session
    ON account_snapshots(cohort_id, session);
CREATE UNIQUE INDEX ux_benchmarks_epoch_session_symbol
    ON benchmark_observations(cohort_id, epoch_id, session, symbol);
CREATE UNIQUE INDEX ux_fills_intent_session
    ON fills(intent_id, session);
CREATE UNIQUE INDEX ux_session_phases
    ON session_phases(cohort_id, session, phase);
CREATE UNIQUE INDEX ux_action_lot_application
    ON lot_action_applications(action_id, lot_id);
```

`metric_epochs` stores `epoch_id`, `generation_id`, `schema_version`, `status`, `start_session`, and `end_session`; P2 supplies epoch hashes and boundary policy later. `session_runs` stores session validity and invalid reason. `session_phases` stores `started_at` and `completed_at` so a restart resumes at the first incomplete phase.

Insert schema version `1`, cohort ID, and opening cash exactly once. Opening cash is a deterministic `cash_events` row:

```python
opening_id = stable_id("cash", cohort_id, "opening")
```

- [ ] **Step 4: Implement typed row conversion and read APIs**

Centralize `_decimal(text)`, `_date(text)`, and `_datetime(text)` converters. Read methods must `ORDER BY session, primary_id`, construct immutable models, and never read compatibility JSON. Implement conflict-safe insertion:

```python
def _insert_idempotent(
    self,
    table: str,
    identity_column: str,
    identity: str,
    values: dict[str, object],
) -> bool:
    existing = self._connection.execute(
        f"SELECT * FROM {table} WHERE {identity_column} = ?",
        (identity,),
    ).fetchone()
    encoded = {key: self._encode(value) for key, value in values.items()}
    if existing is not None:
        if any(existing[key] != encoded[key] for key in encoded):
            raise LedgerConflictError(f"conflicting {table} identity {identity}")
        return False
    columns = ", ".join(encoded)
    marks = ", ".join("?" for _ in encoded)
    self._connection.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({marks})",
        tuple(encoded.values()),
    )
    return True
```

Restrict `table` and column names to module-owned constants before using this helper.

- [ ] **Step 5: Run the ledger tests**

Run: `.venv/bin/python -m pytest tests/test_portfolio_ledger.py -v`

Expected: schema, restart, read, and conflict tests pass.

- [ ] **Step 6: Commit the ledger foundation**

```bash
git add tradingagents/strategies/state/portfolio_ledger.py \
  tradingagents/strategies/state/__init__.py tests/test_portfolio_ledger.py
git diff --check
git commit -m "feat(ledger): add authoritative cohort sqlite state"
```

---

### Task 4: Apply fills, lots, cash, and snapshots atomically

**Files:**

- Modify: `tradingagents/strategies/state/portfolio_ledger.py`
- Expand: `tests/test_portfolio_ledger.py`
- Create: `tests/test_accounting_invariants.py`

**Interfaces:**

```python
PortfolioLedger.apply_fill(
    intent: OrderIntent,
    fill: Fill,
) -> AccountState
PortfolioLedger.mark(
    session: date,
    close_marks: dict[str, MarketBar],
    epoch_id: str,
    valuation_at: datetime,
) -> AccountSnapshot
PortfolioLedger.account_state() -> AccountState
```

- [ ] **Step 1: Add failing long/short/FIFO/rollback/idempotency tests**

Use exact Decimal examples:

- Buy 10 at `100.10` with `$1.00` slippage and zero fees: cash decreases by `1001.00`.
- Sell 10 at `109.89`: realized P&L is `97.90` before recorded fill costs and the lot closes.
- Short 10 at `99.90`: cash receives `999.00`, short liability marks at close, and margin is persisted.
- Cover 10 at `90.09`: the short lot closes with direction-correct realized P&L.
- Two entry lots close FIFO and `lot_closures` reconcile quantities.
- Raising after the cash insert rolls back fill, cash, lot, and status together.
- Reapplying the same fill after restart changes no row count or balance.
- Missing one open-lot mark raises `MissingMarkError` and writes no valid snapshot.

Run: `.venv/bin/python -m pytest tests/test_portfolio_ledger.py tests/test_accounting_invariants.py -v`

Expected: failures report missing `apply_fill`, `mark`, and `account_state`.

- [ ] **Step 2: Implement transactional fill application**

Inside one `transaction()`:

1. Load the stored intent and require `pending`.
2. Require `fill.intent_id`, eligible session, side, and requested quantity to match.
3. Insert fill and explicit `slippage`, `commission`, and `other_fees` cost rows, including zero amounts.
4. For `buy`, add a long lot and debit `fill_price * quantity + commission + other_fees`.
5. For `short`, add a short lot, credit proceeds less fees, and reserve `1.5 * fill_price * quantity` margin.
6. For `sell`/`cover`, consume matching open lots FIFO, write `lot_closures`, mutate cash, and release proportional short margin.
7. Insert the cash event and `filled` status transition.
8. Return `account_state()` after commit.

Use these cash deltas:

```python
notional = fill.fill_price * fill.quantity
fees = fill.commission + fill.other_fees
cash_delta = {
    "buy": -notional - fees,
    "sell": notional - fees,
    "short": notional - fees,
    "cover": -notional - fees,
}[intent.side]
```

Persist realized lot P&L as:

```python
realized = (
    (fill.fill_price - lot_entry_price) * close_qty
    if lot_direction == "long"
    else (lot_entry_price - fill.fill_price) * close_qty
)
```

- [ ] **Step 3: Implement authoritative marks and invariants**

`mark()` must require one exact raw bar for every open-lot ticker, persist one mark per ticker/session, compute:

```python
long_market_value = sum(long_qty * close)
short_liability = sum(short_qty * close)
net_equity = cash + long_market_value - short_liability
gross_exposure = long_market_value + short_liability
net_exposure = long_market_value - short_liability
unrealized_pnl = sum(
    (close - entry) * qty if direction == "long" else (entry - close) * qty
)
cumulative_costs = slippage_cost + commission_cost + other_fees + borrow_cost + financing_cost
gross_equity = net_equity + cumulative_costs
high_water_mark = max(previous_high_water_mark, net_equity)
```

Require:

```python
assert net_equity == cash + long_market_value - short_liability
assert gross_equity - cumulative_costs == net_equity
```

Write the snapshot with deterministic `stable_id("snapshot", cohort_id, epoch_id, session)`. An identical rerun returns the existing snapshot; a conflicting rerun raises `LedgerConflictError`.

- [ ] **Step 4: Run focused tests**

Run: `.venv/bin/python -m pytest tests/test_portfolio_ledger.py tests/test_accounting_invariants.py -v`

Expected: all tests pass.

- [ ] **Step 5: Commit atomic accounting**

```bash
git add tradingagents/strategies/state/portfolio_ledger.py \
  tests/test_portfolio_ledger.py tests/test_accounting_invariants.py
git diff --check
git commit -m "feat(ledger): apply fills and marks transactionally"
```

---

### Task 5: Add explicit costs, resting stops, accruals, and corporate actions

**Files:**

- Create: `tradingagents/strategies/execution/cost_model.py`
- Create: `tradingagents/strategies/execution/stop_execution.py`
- Modify: `tradingagents/strategies/state/portfolio_ledger.py`
- Modify: `tradingagents/default_config.py`
- Create: `tests/test_execution_costs.py`
- Create: `tests/test_corporate_actions.py`
- Create: `tests/test_order_lifecycle.py`

**Interfaces:**

```python
PaperCostModel.fill(
    intent: OrderIntent,
    reference_price: Decimal,
    effective_at: datetime,
    processed_at: datetime,
) -> Fill
PaperCostModel.borrow_charge(
    notional: Decimal,
    annual_rate: Decimal,
) -> Decimal
PaperCostModel.financing_charge(
    debit_balance: Decimal,
    annual_rate: Decimal,
) -> Decimal
stop_reference(intent: OrderIntent, bar: MarketBar) -> Decimal | None
PortfolioLedger.apply_corporate_actions(
    session: date,
    actions: list[CorporateAction],
) -> list[LedgerEvent]
PortfolioLedger.accrue_borrow(
    session: date,
    close_marks: dict[str, MarketBar],
    rates: dict[str, Decimal | None],
) -> LedgerEvent
PortfolioLedger.accrue_financing(
    session: date,
    annual_rate: Decimal,
) -> LedgerEvent
```

- [ ] **Step 1: Write failing exact-cost and stop tests**

Require 10 bps adverse slippage:

```python
assert model.fill(buy_intent, Decimal("100"), open_at, run_at).fill_price == Decimal("100.100")
assert model.fill(cover_intent, Decimal("100"), open_at, run_at).fill_price == Decimal("100.100")
assert model.fill(sell_intent, Decimal("100"), open_at, run_at).fill_price == Decimal("99.900")
assert model.fill(short_intent, Decimal("100"), open_at, run_at).fill_price == Decimal("99.900")
```

Require long stop behavior: open `90` below stop `95` uses `90`; otherwise low `94` uses `95`; low `96` does not fill. Mirror short stops: open `110` above stop `105` uses `110`; otherwise high `106` uses `105`.

Require borrow `notional * annual_rate / 365`, exact once across restart; unknown new-short rate rejects the intent; existing short with unknown rate uses the configured `0.30` fallback and flags the event. Require financing exact once; idle cash produces zero.

- [ ] **Step 2: Write failing split/dividend tests**

Require a 2-for-1 split to double open quantity, halve basis and stop price, and preserve total basis. Require a `$1.25` dividend to credit a 10-share long by `$12.50` and debit a 10-share short by `$12.50`. Duplicate action IDs must have no second economic effect. `verified=False` must quarantine the ticker and invalidate the session without mutating its lot.

Run: `.venv/bin/python -m pytest tests/test_execution_costs.py tests/test_order_lifecycle.py tests/test_corporate_actions.py -v`

Expected: collection or attribute failures for the new services.

- [ ] **Step 3: Add the versioned configuration**

Add under `autoresearch` in `default_config.py`:

```python
"paper_ledger": {
    "schema_version": 1,
    "calendar": "XNYS",
    "pricing_version": "raw-yfinance-v1",
    "execution_clock_version": "next-xnys-open-v1",
    "cost_model_version": "equity-10bps-v1",
    "slippage_bps": "10",
    "commission_per_fill": "0",
    "other_fee_per_fill": "0",
    "margin_requirement": "1.50",
    "margin_financing_rate": "0",
    "idle_cash_yield_rate": "0",
    "existing_short_missing_borrow_rate": "0.30",
    "benchmark_symbols": ["SPY", "BIL"],
    "bar_max_age_hours": 24,
},
```

- [ ] **Step 4: Implement costs and stops**

`PaperCostModel` parses config strings into `Decimal`, quantizes cash amounts to `Decimal("0.0001")`, persists zero commission/fees, and generates `fill_id = stable_id("fill", intent.intent_id, intent.eligible_session, intent.requested_qty)`.

Implement `stop_reference()` exactly:

```python
def stop_reference(intent: OrderIntent, bar: MarketBar) -> Decimal | None:
    stop = intent.stop_price
    if intent.price_rule != "resting_stop" or stop is None:
        return bar.open if intent.price_rule == "next_session_open" else None
    if intent.side == "sell":
        if bar.open <= stop:
            return bar.open
        return stop if bar.low <= stop else None
    if intent.side == "cover":
        if bar.open >= stop:
            return bar.open
        return stop if bar.high >= stop else None
    raise ValueError("resting stops are exit intents")
```

- [ ] **Step 5: Implement idempotent accruals and actions**

Borrow IDs are `stable_id("borrow", cohort_id, session, ticker)`; financing IDs are `stable_id("financing", cohort_id, session)`; dividend IDs derive from the action and lot. Charge open short notional at the same session close using ACT/365. Reject a new short before fill if its rate is absent or above `borrow_cost_reject_above`; do not silently substitute zero.

For splits, update quantities, per-share basis, reserved margin, resting stop price, and pending exit quantity in one transaction. For dividends, write direction-aware `dividend_events` and `cash_events`. Any unverified or conflicting action calls `invalidate_session(session, reason)` and `quarantine_ticker(ticker, reason)`.

- [ ] **Step 6: Run focused tests**

Run: `.venv/bin/python -m pytest tests/test_execution_costs.py tests/test_order_lifecycle.py tests/test_corporate_actions.py tests/test_accounting_invariants.py -v`

Expected: all tests pass.

- [ ] **Step 7: Commit costs and actions**

```bash
git add tradingagents/default_config.py \
  tradingagents/strategies/execution/cost_model.py \
  tradingagents/strategies/execution/stop_execution.py \
  tradingagents/strategies/state/portfolio_ledger.py \
  tests/test_execution_costs.py tests/test_order_lifecycle.py \
  tests/test_corporate_actions.py tests/test_accounting_invariants.py
git diff --check
git commit -m "feat(execution): model costs stops and corporate actions"
```

---

### Task 6: Stage intents in the bridge and make brokers ledger-safe

**Files:**

- Modify: `tradingagents/strategies/trading/execution_bridge.py`
- Modify: `tradingagents/execution/base_broker.py`
- Modify: `tradingagents/execution/paper_broker.py`
- Modify: `tradingagents/execution/alpaca_broker.py`
- Modify: `tests/test_execution.py`
- Modify: `tests/test_execution_bridge_shorts.py`
- Modify: `tests/test_paper_broker_shorts.py`
- Modify: `tests/test_integration_shorts.py`

**Interfaces:**

```python
ExecutionBridge.stage_intent(
    recommendation: TradeRecommendation,
    signal_records: tuple[SignalRecord, ...],
    marked_account: AccountState,
    decision_at: datetime,
    eligible_session: date,
) -> OrderIntent
ExecutionBridge.execute_due_intent(
    intent: OrderIntent,
    opening_bar: MarketBar,
    marked_account: AccountState,
    risk_context: dict,
    cost_model: PaperCostModel,
) -> FillResult
BaseBroker.submit_stock_order(
    symbol: str,
    side: str,
    qty: int,
    order_type: str = "market",
    client_order_id: str | None = None,
    **kwargs: object,
) -> OrderResult
BaseBroker.reconcile_order(client_order_id: str) -> OrderResult
```

- [ ] **Step 1: Replace immediate-fill tests with intent lifecycle tests**

Assert a recommendation decided at Friday close creates a Monday-eligible pending intent and no position/cash mutation. Execute it with Monday's raw open and assert the fill appears only then. Assert `event_at` or `observed_at` after the session cutoff rejects signal-to-intent creation. Assert an unknown-borrow short is rejected before fill.

For Alpaca, mock an accepted-but-unfilled response, rerun with the same client order ID, and require `get_order_by_client_id()` reconciliation before any second submit. Require no unconfirmed order to become a `Fill`.

Run: `.venv/bin/python -m pytest tests/test_execution.py tests/test_execution_bridge_shorts.py tests/test_paper_broker_shorts.py tests/test_integration_shorts.py -v`

Expected: failures show the old same-call fill API and missing reconciliation contract.

- [ ] **Step 2: Implement intent staging**

`stage_intent()` must:

1. Require at least one persisted signal and one common epoch/policy.
2. Require `signal.decision_at <= session_close(signal.reference_session)` and `observed_at <= decision_at`.
3. Size using `marked_account` and the recommendation's approved percentage.
4. Derive side from direction.
5. Set `created_at=decision_at`, exact `eligible_session`, `price_rule="next_session_open"`, `status="pending"`.
6. Persist through `PortfolioLedger.stage_intent()` before returning.

Use the stable intent identity from Task 1.

- [ ] **Step 3: Implement due-intent execution**

`execute_due_intent()` rejects mismatched/adjusted bars and future or already terminal intents. Re-run the existing `RiskGate` against current marked account, pending intents, current borrow/earnings context, and opening price. Use `stop_reference()` for resting stops; a non-triggered stop remains pending. Build a fill through `PaperCostModel`, call `PortfolioLedger.apply_fill()`, and return the persisted result.

- [ ] **Step 4: Make PaperBroker a ledger adapter**

Construct `PaperBroker(ledger: PortfolioLedger)`. `get_account()` and `get_positions()` read the ledger. Submission methods may only accept a prebuilt persisted intent/fill pair and delegate to `apply_fill`; remove `reconstruct_from_trades()` from the production path. Preserve legacy method signatures temporarily by raising:

```python
RuntimeError(
    "PaperBroker direct price submission is disabled; stage and execute a ledger intent"
)
```

Update tests to use ledger fixtures rather than mutable `broker.cash`.

- [ ] **Step 5: Add stable Alpaca IDs and reconciliation**

Pass `client_order_id=intent.intent_id` to Alpaca request objects. Implement:

```python
def reconcile_order(self, client_order_id: str) -> OrderResult:
    order = self.client.get_order_by_client_id(client_order_id)
    return self._to_order_result(order)
```

Before `submit_order`, reconcile a persisted `external_orders` row in `pending`, `accepted`, or `partially_filled` state. Persist only a broker-confirmed full fill. The cohort constructor must reject non-paper execution, so this code remains compatibility-only.

- [ ] **Step 6: Run broker and bridge tests**

Run: `.venv/bin/python -m pytest tests/test_execution.py tests/test_execution_bridge_shorts.py tests/test_paper_broker_shorts.py tests/test_integration_shorts.py -v`

Expected: all tests pass.

- [ ] **Step 7: Commit the staged bridge**

```bash
git add tradingagents/strategies/trading/execution_bridge.py \
  tradingagents/execution/base_broker.py tradingagents/execution/paper_broker.py \
  tradingagents/execution/alpaca_broker.py tests/test_execution.py \
  tests/test_execution_bridge_shorts.py tests/test_paper_broker_shorts.py \
  tests/test_integration_shorts.py
git diff --check
git commit -m "refactor(execution): stage and reconcile ledger intents"
```

---

### Task 7: Implement the restartable execution-first session state machine

**Files:**

- Create: `tradingagents/strategies/orchestration/session_executor.py`
- Modify: `tradingagents/strategies/orchestration/multi_strategy_engine.py`
- Modify: `tradingagents/strategies/orchestration/cohort_orchestrator.py`
- Modify: `tradingagents/strategies/learning/signal_journal.py`
- Create: `tests/test_session_executor.py`
- Modify: `tests/test_cohort_lifecycle.py`
- Modify: `tests/test_30day_simulation.py`

**Interfaces:**

```python
SessionExecutor.execute_open_and_mark(
    session: date,
    epoch_id: str,
    price_source: PriceSource,
    borrow_rates: dict[str, Decimal | None],
    processed_at: datetime,
) -> AccountSnapshot
MultiStrategyEngine.screen_and_stage(
    trading_date: str,
    data: dict,
    shared_signals: list[dict],
    shared_regime: dict,
    enrichment: dict,
    size_profile: PortfolioSizeProfile,
    marked_account: AccountSnapshot,
) -> dict
CohortOrchestrator.run_daily(trading_date: str) -> dict[str, object]
```

- [ ] **Step 1: Write failing lifecycle and crash-resume tests**

Use mocked bars for Friday `2026-07-31` and Monday `2026-08-03`:

- A Friday close signal creates a Monday intent and cannot fill Friday.
- Monday exits execute before entries; released cash/buying power admits an entry that would otherwise fail.
- A weekend, July 3 holiday, and early-close transition select the exact next XNYS open.
- A late event observed after Friday close is journaled as cutoff-late but creates no intent.
- A required held-ticker mark missing invalidates Monday before any fill/accrual/action.
- Inject a crash after fill insertion and verify rollback; inject a crash after a completed phase and verify the rerun skips it without duplicating economics.
- SPY and BIL observations use the same session and observation-time quality
  checks as marks, but persist a separate total-return-adjusted close and
  `return_basis="total_return_adjusted"`. Raw execution/mark bars must never be
  reused as benchmark returns.

Run: `.venv/bin/python -m pytest tests/test_session_executor.py tests/test_cohort_lifecycle.py tests/test_30day_simulation.py -v`

Expected: lifecycle assertions fail because the engine still screens, fills at close, and checks exits afterward.

- [ ] **Step 2: Implement execution-first phases**

`SessionExecutor.execute_open_and_mark()` records and completes phases in this exact order:

```python
PHASES = (
    "validate_market_data",
    "apply_corporate_actions",
    "execute_exits",
    "execute_entries",
    "accrue_borrow",
    "accrue_financing",
    "mark_positions",
    "record_benchmarks",
    "snapshot_account",
)
```

Before phase one, collect required tickers from open lots and due intents;
fetch exact raw bars in one bulk request; fetch actions; and separately fetch
total-return-adjusted SPY/BIL closes. Validate the complete set before any
economic phase. Within execution, sort by
`(side priority, created_at, intent_id)` with `sell/cover` before `buy/short`.

Each phase checks `ledger.phase_completed(session, phase)`. Completion is written in the same transaction as that phase's last mutation. Session invalidation stores the reason and returns a non-valid result to the orchestrator.

- [ ] **Step 3: Split screening from execution**

Remove current-price execution, JSON broker reconstruction, close-based exits, permissive missing-price continues, and direct snapshot reconstruction from `run_paper_trade_phase()`. Rename its cutoff-safe half to `screen_and_stage()`.

For each candidate, construct `event_key` from source metadata, `evidence_hash` from canonical evidence, and:

```python
signal_id = stable_id(
    "signal",
    epoch_id,
    strategy_name,
    horizon,
    direction,
    event_key,
)
```

Persist `SignalRecord` before committee synthesis. A recommendation references every contributing `signal_id`. Scheduled exits and resting stops are also intents; strategy `check_exit()` decides whether to stage a next-session exit, never closes at the current close.

Mirror ledger signals to `signal_journal.jsonl` only after persistence; journal code must use `signal_id` for deduplication and cannot invent an entry price.

- [ ] **Step 4: Reorder cohort orchestration**

For every cohort, open its ledger and run `execute_open_and_mark()` first. If any cohort is invalid, record its exact error and do not screen/stage that cohort. Then fetch shared strategy data once, screen four horizons once, enrich once, and call `screen_and_stage()` with each cohort's already marked account. This preserves API/LLM sharing while enforcing the order lifecycle.

- [ ] **Step 5: Run lifecycle tests**

Run: `.venv/bin/python -m pytest tests/test_session_executor.py tests/test_cohort_lifecycle.py tests/test_30day_simulation.py tests/test_fetch_timeout.py -v`

Expected: all tests pass.

- [ ] **Step 6: Commit the state machine**

```bash
git add tradingagents/strategies/orchestration/session_executor.py \
  tradingagents/strategies/orchestration/multi_strategy_engine.py \
  tradingagents/strategies/orchestration/cohort_orchestrator.py \
  tradingagents/strategies/learning/signal_journal.py \
  tests/test_session_executor.py tests/test_cohort_lifecycle.py \
  tests/test_30day_simulation.py
git diff --check
git commit -m "feat(orchestration): execute and mark before screening"
```

---

### Task 8: Generate read-compatible JSON from the ledger

**Files:**

- Create: `tradingagents/strategies/state/compatibility_projection.py`
- Modify: `tradingagents/strategies/trading/paper_trader.py`
- Modify: `tradingagents/strategies/state/equity_snapshot.py`
- Modify: `tradingagents/strategies/state/state.py`
- Create: `tests/test_compatibility_projection.py`
- Modify: `tests/test_equity_snapshot_nan.py`

**Interfaces:**

```python
project_paper_trades(
    ledger: PortfolioLedger,
    destination: Path,
) -> list[dict[str, object]]
project_equity_snapshots(
    ledger: PortfolioLedger,
    destination: Path,
) -> list[dict[str, object]]
project_all(ledger: PortfolioLedger, state_dir: Path) -> None
```

- [ ] **Step 1: Write failing projection parity and crash-safety tests**

Seed a ledger with one closed long, one open short, costs, and two snapshots. Require:

- `paper_trades.json` totals and statuses match ledger fills/lots exactly.
- `equity_snapshots.jsonl` `portfolio_value` equals `float(snapshot.net_equity)`.
- Projection reruns are byte-for-byte deterministic.
- Replacing either file is atomic; an injected `os.replace` failure leaves the previous file intact.
- Missing marks never fall back to entry price; a ledger error propagates.
- `StateManager.load_paper_trades()` and `equity_snapshot.load_snapshots()` prefer `portfolio.db` when present and read legacy JSON only when no ledger exists.

Run: `.venv/bin/python -m pytest tests/test_compatibility_projection.py tests/test_equity_snapshot_nan.py -v`

Expected: failures show JSON remains authoritative and missing marks fall back.

- [ ] **Step 2: Implement deterministic atomic projections**

Read only public ledger APIs, sort trades by execution session/fill ID and snapshots by session/snapshot ID, convert Decimal to float only at the final JSON boundary, and write using a shared atomic helper that flushes and `fsync()`s before `os.replace()`.

The trade projection includes `trade_id` as the opening fill ID, `signal_ids`, `intent_id`, `execution_id`, strategy, ticker, direction, entry/exit session and price, shares, status, realized P&L, and actual cost fields. The snapshot projection preserves the legacy keys and adds `epoch_id`, `snapshot_id`, gross/net exposure, each cost category, mark timestamp, and validity.

- [ ] **Step 3: Convert PaperTrader and readers**

`PaperTrader` becomes a read/projection wrapper. Remove its ability to create or close an accounting record directly. `StateManager` detects `{state_dir}/portfolio.db` and delegates trade reads; its legacy write methods raise a clear error when a ledger exists. `equity_snapshot.write_snapshot()` delegates to `project_equity_snapshots()` and must not reconstruct equity from JSON.

- [ ] **Step 4: Run projection and dashboard-loader regressions**

Run: `.venv/bin/python -m pytest tests/test_compatibility_projection.py tests/test_equity_snapshot_nan.py -v`

Expected: all tests pass.

Run: `.venv/bin/python -m pytest tests/ -k 'dashboard or equity_snapshot' -v`

Expected: all selected tests pass.

- [ ] **Step 5: Commit compatibility projections**

```bash
git add tradingagents/strategies/state/compatibility_projection.py \
  tradingagents/strategies/trading/paper_trader.py \
  tradingagents/strategies/state/equity_snapshot.py \
  tradingagents/strategies/state/state.py \
  tests/test_compatibility_projection.py tests/test_equity_snapshot_nan.py
git diff --check
git commit -m "refactor(state): project compatibility json from ledger"
```

---

### Task 9: Wire exact-session CLI behavior, scheduler truth, and clean-generation readiness

**Files:**

- Modify: `scripts/run_cohorts.py`
- Modify: `scripts/run_generations.py`
- Modify: `scripts/daily_trading.sh`
- Modify: `deploy/systemd/trade.timer`
- Create: `scripts/migrate_ledger_state.py`
- Create: `tests/test_ledger_migration.py`
- Modify: `tests/test_generation_manager.py`
- Modify: `README.md`
- Modify: `AUTORESEARCH_ARCHITECTURE_MAP.md`
- Modify: `assets/autoresearch.svg`
- Modify: `assets/daily-cycle.svg`

**Interfaces:**

```bash
.venv/bin/python scripts/migrate_ledger_state.py \
  --legacy-state data/generations/gen_003 \
  --output-dir /tmp/eventedge-ledger-dry-run \
  --dry-run
```

- [ ] **Step 1: Write failing CLI/session and migration tests**

Require:

- `run_cohorts.py --date` rejects a non-XNYS date instead of rolling it to another session.
- A generation subprocess returns failure if one cohort is invalid, even if no exception escaped.
- The migration command never edits its legacy input, reports legacy same-close semantics, inventories all 16 cohorts, and creates no authoritative opening positions.
- A clean output readiness check creates empty ledger schemas with configured opening cash only.
- `run-learning` behavior is unchanged in this P0 task; P3 will hard-lock it in its own plan.

Run: `.venv/bin/python -m pytest tests/test_ledger_migration.py tests/test_generation_manager.py -v`

Expected: migration import/CLI tests fail and invalid cohort status is not yet surfaced.

- [ ] **Step 2: Make CLI dates exact**

`run_generations.py run-daily` and `run_cohorts.py --date` parse `date.fromisoformat()`, require `is_session()`, and exit nonzero with `YYYY-MM-DD is not an XNYS session`. `daily_trading.sh` may still be invoked Monday-Friday, but Python is authoritative for holidays. Remove wording that implies weekday equals a valid market session.

- [ ] **Step 3: Implement the read-only migration/readiness tool**

The tool hashes every legacy input before and after inspection, refuses an output path inside the legacy tree, labels the report:

```text
legacy_execution_model=same_session_close
authoritative_import=false
eligible_for_promotion=false
```

With `--dry-run`, it writes only a report under the explicit output directory. With `--initialize-clean`, it creates empty cohort ledger schemas and opening-cash events; it never imports legacy positions, fills, returns, or signals.

- [ ] **Step 4: Update scheduler and documentation sources**

Change `deploy/systemd/trade.timer` to:

```ini
[Unit]
Description=Run EventEdge paper ledger after XNYS daily bars finalize

[Timer]
OnCalendar=Mon..Fri 18:00 America/New_York
Persistent=true

[Install]
WantedBy=timers.target
```

Do not install it. Update README, architecture map, and SVG sources to show signal at close → next-session-open intent execution, SQLite authority, JSON projections, costs, 18:00 ET, dependent scenario portfolios, and covered calls inactive. Do not claim `gen_004` exists or is deployed.

- [ ] **Step 5: Run CLI, migration, and SVG validation**

Run: `.venv/bin/python -m pytest tests/test_ledger_migration.py tests/test_generation_manager.py -v`

Expected: all tests pass.

Run: `.venv/bin/python -c "import xml.etree.ElementTree as ET, pathlib; [ET.parse(p) for p in pathlib.Path('assets').glob('*.svg')]"`

Expected: exit 0.

- [ ] **Step 6: Commit release wiring**

```bash
git add scripts/run_cohorts.py scripts/run_generations.py \
  scripts/daily_trading.sh scripts/migrate_ledger_state.py \
  deploy/systemd/trade.timer tests/test_ledger_migration.py \
  tests/test_generation_manager.py README.md \
  AUTORESEARCH_ARCHITECTURE_MAP.md assets/autoresearch.svg \
  assets/daily-cycle.svg
git diff --check
git commit -m "docs(release): wire ledger session schedule and readiness"
```

---

### Task 10: Prove P0 acceptance, offline performance, and branch readiness

**Files:**

- Create: `tests/test_execution_ledger_acceptance.py`
- Modify only if an acceptance test exposes a defect: the P0 files owned by Tasks 1-9 and the directly corresponding focused test.
- Record results in the PR description; do not create a committed benchmark artifact containing local paths or environment details.

- [ ] **Step 1: Add one table-driven offline acceptance test**

`tests/test_execution_ledger_acceptance.py` must exercise all approved P0 cases with mocked bars/actions:

```python
CASES = (
    "close_signal_never_fills_same_session",
    "weekend_holiday_and_early_close_next_open",
    "late_event_cannot_create_intent",
    "adjusted_bar_fails_before_mutation",
    "long_and_short_pnl_reconcile",
    "long_and_short_stop_gap_reconcile",
    "costs_borrow_financing_apply_once_after_restart",
    "exits_restore_buying_power_before_entries",
    "missing_mark_invalidates_without_zero_pnl",
    "transaction_crash_rolls_back",
    "rerun_does_not_duplicate_economic_effects",
    "unresolved_external_order_reconciles_before_retry",
    "split_and_directional_dividend_apply_once",
    "compatibility_json_matches_ledger",
)
```

For each case, assert ledger row counts, cash, lots, costs, snapshot validity, and stable IDs—not only the returned orchestration dictionary.

- [ ] **Step 2: Run acceptance tests red, then make the smallest corrections**

Run: `.venv/bin/python -m pytest tests/test_execution_ledger_acceptance.py -v`

Expected on first run: any missed integration contract fails with a specific assertion. Correct only the owning P0 module and add the focused regression alongside the fix.

- [ ] **Step 3: Run the complete focused P0 suite**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_execution_models.py \
  tests/test_market_data_contract.py \
  tests/test_portfolio_ledger.py \
  tests/test_accounting_invariants.py \
  tests/test_execution_costs.py \
  tests/test_order_lifecycle.py \
  tests/test_corporate_actions.py \
  tests/test_execution.py \
  tests/test_execution_bridge_shorts.py \
  tests/test_paper_broker_shorts.py \
  tests/test_session_executor.py \
  tests/test_compatibility_projection.py \
  tests/test_ledger_migration.py \
  tests/test_execution_ledger_acceptance.py -v
```

Expected: all selected tests pass with no network access.

- [ ] **Step 4: Run the full offline suite**

Run: `.venv/bin/python -m pytest tests/ -m "not live" -q`

Expected: all offline tests pass. Any failure blocks branch handoff.

- [ ] **Step 5: Measure wall time and peak RSS**

Run:

```bash
/usr/bin/time -lp .venv/bin/python -m pytest \
  tests/test_session_executor.py \
  tests/test_execution_ledger_acceptance.py -q
```

Expected: exit 0; record elapsed time and maximum resident set size in the PR. Peak RSS must be below 8 GB and should remain below the 4 GB production service cap. Unit-test fixtures must confirm one raw bulk bar request per session, zero API calls from policy/ledger code, and zero LLM calls in execution phases.

- [ ] **Step 6: Perform the migration dry run against copied legacy state**

```bash
P0_DRY_RUN_DIR="$(mktemp -d)"
.venv/bin/python scripts/migrate_ledger_state.py \
  --legacy-state data/generations/gen_003 \
  --output-dir "$P0_DRY_RUN_DIR" \
  --dry-run
```

Expected: report identifies legacy same-close limitations, inventories cohort files, imports no economic history, and confirms every source hash unchanged.

- [ ] **Step 7: Verify diff scope and commit acceptance coverage**

```bash
git status --short
git diff --check
git diff --stat private/main...HEAD
git add tests/test_execution_ledger_acceptance.py
git diff --cached --check
git commit -m "test(execution): prove ledger acceptance invariants"
```

Expected: only files in this plan are changed; no state database, JSON state, `.env`, credentials, logs, or dry-run directory is tracked.

- [ ] **Step 8: Push the topic branch and open a draft PR**

```bash
git push -u private codex/p0-p3-foundation
```

Open a draft PR to `main` with the focused/full-suite results, timing/RSS, migration dry-run result, P0 version strings, and explicit statements:

- `gen_003` was not modified.
- No production checkout, timer, service, generation, or state was changed.
- `gen_004` must be created with clean state only after foundation merge and Pedro's explicit deployment authorization.
- P2/P3 must consume `PortfolioLedger.read_*` interfaces before the foundation release is complete.

Expected: a draft PR exists; no merge or deployment occurs.

## Final Self-Review

- [ ] Confirm every approved P0 acceptance test appears in Task 10.
- [ ] Confirm `SignalRecord` persists `epoch_id` and `policy_id`, and its ID hashes both.
- [ ] Confirm `AccountSnapshot` and `BenchmarkObservation` reads are sufficient for P2 without JSON or network access.
- [ ] Confirm only raw `auto_adjust=False` bars can reach execution and marks.
- [ ] Confirm exits precede entries and every cost/action/accrual is idempotent across restart.
- [ ] Confirm a missing mark, untrusted action, or unresolved external order fails closed.
- [ ] Confirm the plan never imports legacy positions or rewrites generation history.
- [ ] Confirm `exchange-calendars>=4.13.2,<5` and `get_calendar("XNYS")` are exact.
- [ ] Scan the plan:

```bash
PLAN_SMELL_PATTERN='TO''DO|TB''D|implement lat''er|similar t''o|appropriate err''or|place''holder'
rg -n "$PLAN_SMELL_PATTERN" docs/superpowers/plans/2026-07-31-execution-ledger.md
```

Expected: no matches.

- [ ] Confirm interface names and types are consistent:

```bash
rg -n 'PortfolioLedger|AccountSnapshot|BenchmarkObservation|SignalRecord|OrderIntent|FillResult' \
  docs/superpowers/plans/2026-07-31-execution-ledger.md
```

Expected: all consumers use the canonical models and ledger read methods defined in Tasks 1 and 3.
