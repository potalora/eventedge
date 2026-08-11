# Governed Market-Data Recovery Implementation Plan

> **Required execution skill:** Use subagent-driven-development for the selected
> execution session. The root agent owns integration, every commit, final
> verification, and all live-system decisions.

**Goal:** Recover one otherwise valid governed Yahoo daily bar from complete,
coherent same-provider 60-minute evidence without weakening P0 validation,
while preserving immutable replay evidence and adding a read-only,
state-aware preflight.

**Architecture:** Split governed raw-bar resolution from the existing
all-or-nothing daily fetch. Persist a separate immutable recovery record, bind
its identity/version/digest into every affected P0 context, and verify the
record on resume. Add a read-only state-topology probe and a repo-scoped
shared/exclusive runtime lock so after-close preflight can gate daily execution
without mutating generation state.

**Tech stack:** Python 3.11+, dataclasses, SQLite, pandas, yfinance,
exchange-calendars, pytest, Ruff, Bash, systemd.

**Authoritative design:**
docs/superpowers/specs/2026-08-10-governed-market-data-recovery-design.md

## Global constraints

- Work only in branch codex/p0-market-data-resilience.
- Keep candidate-only retry/quarantine behavior unchanged.
- Never substitute Alpaca IEX, FMP, adjusted, interpolated, clamped, rounded,
  or cross-provider values.
- Only an incoherent raw daily OHLC envelope may enter reconstruction.
- A recovery is acceptable only with the exact XNYS regular-session interval
  start set and exact agreement for daily open, close, and unaffected extreme.
- Persist accepted evidence before binding a cohort context.
- A missing, altered, or unsupported persisted recovery fails replay closed,
  even if reconstructed OHLC is unchanged.
- Keep gen_008 and its 2026-08-10 epoch immutable. Do not trigger any live run.
- Subagents may inspect, test, or edit their assigned files but must not commit.
  The root agent reviews and commits each task.

## File map

**Create:**

- tradingagents/strategies/orchestration/governed_market_data.py
- tradingagents/strategies/orchestration/runtime_lock.py
- tradingagents/strategies/orchestration/preflight_state.py
- tests/test_governed_market_data.py
- tests/test_runtime_lock.py
- tests/test_preflight_state.py

**Modify:**

- tradingagents/strategies/execution/price_source.py
- tradingagents/strategies/metrics/models.py
- tradingagents/strategies/metrics/store.py
- tradingagents/strategies/orchestration/session_executor.py
- tradingagents/strategies/orchestration/cohort_orchestrator.py
- tradingagents/strategies/orchestration/generation_manager.py
- tradingagents/strategies/orchestration/preflight.py
- scripts/run_cohorts.py
- scripts/daily_trading.sh
- tests/test_market_data_contract.py
- tests/test_metrics_store.py
- tests/test_session_executor.py
- tests/test_30day_simulation.py
- tests/test_cohort_failure_reporting.py
- tests/test_generation_manager.py
- tests/test_preflight.py

## Task 1: Add immutable governed-recovery storage

**Files:**

- Modify: tradingagents/strategies/metrics/models.py
- Modify: tradingagents/strategies/metrics/store.py
- Modify: tests/test_metrics_store.py

### Step 1: Write failing model and store tests

Add a fixture builder in tests/test_metrics_store.py with a complete accepted
ESS record:

~~~python
def governed_recovery_record(epoch_id: str) -> GovernedBarRecoveryRecord:
    return GovernedBarRecoveryRecord.create(
        contract_version="yfinance-60m-v1",
        epoch_id=epoch_id,
        session=date(2026, 8, 10),
        ticker="ESS",
        original_daily={
            "open": 286.2099914550781,
            "high": 285.82501220703125,
            "low": 281.5299987792969,
            "close": 283.2099914550781,
            "source": "yfinance",
            "fetched_at": "2026-08-10T22:01:31Z",
        },
        original_validation_error="incoherent ESS/2026-08-10",
        expected_starts=(
            "2026-08-10T09:30:00-04:00",
            "2026-08-10T10:30:00-04:00",
            "2026-08-10T11:30:00-04:00",
            "2026-08-10T12:30:00-04:00",
            "2026-08-10T13:30:00-04:00",
            "2026-08-10T14:30:00-04:00",
            "2026-08-10T15:30:00-04:00",
        ),
        observed_starts=(
            "2026-08-10T09:30:00-04:00",
            "2026-08-10T10:30:00-04:00",
            "2026-08-10T11:30:00-04:00",
            "2026-08-10T12:30:00-04:00",
            "2026-08-10T13:30:00-04:00",
            "2026-08-10T14:30:00-04:00",
            "2026-08-10T15:30:00-04:00",
        ),
        intraday_rows=ESS_60M_ROWS,
        reconstructed_bar={
            "open": 286.2099914550781,
            "high": 286.2099914550781,
            "low": 281.5299987792969,
            "close": 283.2099914550781,
            "source": "yfinance-60m-reconstruction",
        },
        final_validation_error=None,
        affected_cohort_ids=("horizon_30d_size_5k",),
    )
~~~

Test these contracts:

1. save_governed_bar_recovery inserts and load_governed_bar_recovery returns
   the same canonical record;
2. saving the identical record is idempotent;
3. saving unequal evidence for the same epoch/session/ticker raises the store's
   immutable-conflict error;
4. open_existing can read the table but never creates it in a legacy store;
5. a missing table returns no record without a migration or write;
6. the schema change leaves the existing metrics schema version unchanged.

Run:

~~~bash
.venv/bin/python -m pytest tests/test_metrics_store.py -q
~~~

Expected: failures for the absent model, table, and store methods.

### Step 2: Implement the record and canonical digest

In tradingagents/strategies/metrics/models.py add the frozen
GovernedBarRecoveryRecord dataclass with the persisted fields represented by
the fixture. Provide create(...) to compute recovery_id and evidence_digest
from the remaining fields. Normalize ticker to uppercase and serialize all
timestamps as ISO 8601 strings.

Add helpers that:

- produce a canonical JSON payload with sorted keys and compact separators;
- exclude evidence_digest from the digest input;
- compute sha256 with a sha256: prefix;
- derive recovery_id from contract version, epoch, session, ticker, and the
  canonical evidence digest;
- reject a supplied ID or digest that does not match the payload.

Do not share CandidateBarRecoveryRecord or its table.

### Step 3: Add the additive SQLite table

In tradingagents/strategies/metrics/store.py add an additive table:

~~~sql
CREATE TABLE IF NOT EXISTS governed_bar_recoveries (
    recovery_id TEXT PRIMARY KEY,
    contract_version TEXT NOT NULL,
    evidence_digest TEXT NOT NULL,
    epoch_id TEXT NOT NULL,
    session TEXT NOT NULL,
    ticker TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (epoch_id, session, ticker)
)
~~~

Use the existing immutable insert helper. Add:

~~~python
def save_governed_bar_recovery(
    self, record: GovernedBarRecoveryRecord
) -> None: ...

def load_governed_bar_recovery(
    self, *, epoch_id: str, session: date, ticker: str
) -> GovernedBarRecoveryRecord | None: ...

def load_governed_bar_recovery_by_id(
    self, recovery_id: str
) -> GovernedBarRecoveryRecord | None: ...
~~~

The read-only path must inspect sqlite_master before selecting and must not run
CREATE TABLE, ALTER TABLE, or schema initialization.

### Step 4: Run and commit

~~~bash
.venv/bin/python -m pytest tests/test_metrics_store.py -q
.venv/bin/ruff check tradingagents/strategies/metrics/models.py \
  tradingagents/strategies/metrics/store.py tests/test_metrics_store.py
git diff --check
git add tradingagents/strategies/metrics/models.py \
  tradingagents/strategies/metrics/store.py tests/test_metrics_store.py
git commit -m "feat: persist governed bar recovery evidence"
~~~

## Task 2: Resolve governed Yahoo daily bars per ticker

**Files:**

- Modify: tradingagents/strategies/execution/price_source.py
- Modify: tests/test_market_data_contract.py

### Step 1: Add the incident fixture and failing contract tests

Add the exact 2026-08-10 ESS daily bar and seven timezone-aware hourly rows.
Test that the existing raw validator rejects the daily bar, then the new
resolver returns:

~~~python
MarketBar(
    ticker="ESS",
    session=date(2026, 8, 10),
    open=Decimal("286.2099914550781"),
    high=Decimal("286.2099914550781"),
    low=Decimal("281.5299987792969"),
    close=Decimal("283.2099914550781"),
    source="yfinance-60m-reconstruction",
)
~~~

Also add one focused test for every fail-closed edge:

- missing, duplicate, shifted, premarket, or after-hours interval start;
- early-close schedule with the wrong expected starts;
- non-positive, non-finite, or incoherent hourly OHLC;
- mismatched daily open, close, or unaffected extreme;
- both daily extremes inconsistent;
- stale, pre-close, adjusted, ambiguous ticker, or wrong-session evidence;
- missing daily row and non-coherence validation errors never fetch 60m;
- healthy IBM remains byte-for-byte unchanged and is never refetched;
- provider exception creates a normalized failure rather than raw exception
  text;
- no more than one 60m provider attempt occurs per incoherent ticker.

Run:

~~~bash
.venv/bin/python -m pytest tests/test_market_data_contract.py -q
~~~

Expected: failures because governed resolution does not exist.

### Step 2: Add typed evidence and result contracts

In tradingagents/strategies/execution/price_source.py add frozen dataclasses:

~~~python
@dataclass(frozen=True)
class GovernedDailyBarAttempt:
    ticker: str
    session: date
    source: str
    fetched_at: datetime
    raw_ohlc: Mapping[str, Decimal] | None
    validation_error: str | None

@dataclass(frozen=True)
class IntradayBarEvidence:
    start: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    fetched_at: datetime

@dataclass(frozen=True)
class GovernedBarRecoveryEvidence:
    ticker: str
    session: date
    daily_attempt: GovernedDailyBarAttempt
    expected_starts: tuple[datetime, ...]
    observed_starts: tuple[datetime, ...]
    intraday_bars: tuple[IntradayBarEvidence, ...]
    reconstructed: MarketBar | None
    validation_error: str | None

@dataclass(frozen=True)
class GovernedDailyBarResolution:
    bars: Mapping[str, MarketBar]
    attempts: Mapping[str, GovernedDailyBarAttempt]
    recoveries: Mapping[str, GovernedBarRecoveryEvidence]
    failure_map: Mapping[str, str]
~~~

Extend the price-source protocol with:

~~~python
def resolve_governed_daily_bars(
    self,
    tickers: Collection[str],
    session: date,
    *,
    processed_at: datetime,
    max_age: timedelta = timedelta(hours=24),
) -> GovernedDailyBarResolution: ...
~~~

### Step 3: Implement bounded Yahoo reconstruction

Refactor the initial Yahoo frame parsing so a bad ticker does not discard
healthy bars. Preserve the original per-ticker attempt and classify only:

- missing TICKER/SESSION
- incoherent TICKER/SESSION
- invalid TICKER/SESSION
- invalid_benchmark TICKER/SESSION

Only incoherent enters the recovery method. Fetch that ticker once with:

~~~python
yf.download(
    ticker,
    interval="60m",
    auto_adjust=False,
    actions=False,
    prepost=False,
    repair=False,
    threads=False,
    timeout=BOUNDED_TIMEOUT_SECONDS,
)
~~~

Compute expected starts from the exact XNYS open and close. Require observed
starts to equal the expected tuple exactly; do not filter extras. Convert
floats to Decimal through their string representation before equality checks.
Validate every hourly row, aggregate first-open/max-high/min-low/last-close,
apply the exact daily agreement rules, then run the normal final raw-bar clock,
freshness, identity, and OHLC validation.

Keep get_daily_bars behavior compatible for all existing callers and keep
resolve_candidate_daily_bars unchanged.

### Step 4: Run candidate and governed regressions, then commit

~~~bash
.venv/bin/python -m pytest tests/test_market_data_contract.py \
  tests/test_30day_simulation.py -q
.venv/bin/ruff check tradingagents/strategies/execution/price_source.py \
  tests/test_market_data_contract.py
git diff --check
git add tradingagents/strategies/execution/price_source.py \
  tests/test_market_data_contract.py
git commit -m "feat: reconstruct incoherent governed yahoo bars"
~~~

## Task 3: Coordinate persistence, reuse, and probe mode

**Files:**

- Create: tradingagents/strategies/orchestration/governed_market_data.py
- Create: tests/test_governed_market_data.py

### Step 1: Write coordinator tests first

Create tests around a fake price source and real temporary MetricStore:

1. healthy bars pass through and create no record;
2. an accepted ESS recovery is persisted before resolve returns;
3. a second call reuses the record with zero provider calls;
4. a provider result unequal to an existing record fails closed;
5. persist=False returns the same proposed recovery but creates no table/row;
6. a rejected recovery returns the exact bounded failure map;
7. two cohorts receive one stable recovery binding;
8. an unsupported contract version is never reused.

Run:

~~~bash
.venv/bin/python -m pytest tests/test_governed_market_data.py -q
~~~

Expected: import failure for the new module.

### Step 2: Implement the coordinator contracts

Add:

~~~python
GOVERNED_BAR_RECOVERY_CONTRACT = "yfinance-60m-v1"

@dataclass(frozen=True)
class GovernedRecoveryBinding:
    ticker: str
    recovery_id: str
    contract_version: str
    evidence_digest: str

@dataclass(frozen=True)
class GovernedInputResolution:
    bars: Mapping[str, MarketBar]
    recovery_bindings: Mapping[str, GovernedRecoveryBinding]
    recovery_summaries: tuple[Mapping[str, object], ...]
    failure_map: Mapping[str, str]

class GovernedMarketDataError(RuntimeError):
    def __init__(self, failure_map: Mapping[str, str]) -> None: ...

def resolve_governed_bars(
    *,
    price_source: PriceSource,
    metric_store: MetricStore | None,
    epoch_id: str,
    session: date,
    tickers: Collection[str],
    cohort_ids_by_ticker: Mapping[str, Collection[str]],
    processed_at: datetime,
    persist: bool,
) -> GovernedInputResolution: ...
~~~

For each ticker, load a current-contract record before calling the provider.
Validate its canonical digest, ID, final bar, session, ticker, and affected
cohorts. If no record exists, resolve once, create the complete record, and
save it before exposing a binding. In persist=False mode, perform the same
validation and produce the same summary without any write.

Return only bounded summaries to callers; never expose credentials, raw vendor
frames, or raw exception text.

### Step 3: Run and commit

~~~bash
.venv/bin/python -m pytest tests/test_governed_market_data.py \
  tests/test_metrics_store.py tests/test_market_data_contract.py -q
.venv/bin/ruff check \
  tradingagents/strategies/orchestration/governed_market_data.py \
  tests/test_governed_market_data.py
git diff --check
git add tradingagents/strategies/orchestration/governed_market_data.py \
  tests/test_governed_market_data.py
git commit -m "feat: coordinate governed bar recovery"
~~~

## Task 4: Bind recovery identity into P0 execution and replay

**Files:**

- Modify: tradingagents/strategies/orchestration/session_executor.py
- Modify: tests/test_session_executor.py

### Step 1: Write failing execution-context tests

Cover:

1. SessionInputBundle carries governed recovery bindings and failure map;
2. for_tickers preserves only relevant bindings;
3. fetch_input_bundle uses the governed coordinator for required P0 tickers;
4. P0 market-input and provenance documents contain recovery ID, contract
   version, and evidence digest;
5. market-input and provenance digests change if any binding field changes;
6. resume succeeds with an intact record and performs no provider call;
7. deletion, payload tampering, digest mismatch, ID mismatch, or contract
   mismatch fails before any persisted phase is accepted;
8. unchanged reconstructed OHLC does not bypass any mismatch;
9. failure_map raises before open, mark, outcome, or stage mutation.

Run:

~~~bash
.venv/bin/python -m pytest tests/test_session_executor.py -q
~~~

Expected: failures because bundles and replay do not bind recovery evidence.

### Step 2: Extend the input bundle

Add immutable fields:

~~~python
governed_recoveries: Mapping[str, GovernedRecoveryBinding]
governed_failure_map: Mapping[str, str]
~~~

Keep default empty mappings for compatibility. Ensure serialization is stable,
ticker keys are sorted, and for_tickers returns a coherent subset.

Replace the all-or-nothing raw daily fetch in fetch_input_bundle with
resolve_governed_bars. Corporate actions and benchmarks keep their existing
strict validation; benchmark failures enter the normalized governed failure
map.

### Step 3: Bind and verify replay evidence

In _execution_context_documents include, per relevant ticker:

~~~python
{
    "recovery_id": binding.recovery_id,
    "contract_version": binding.contract_version,
    "evidence_digest": binding.evidence_digest,
}
~~~

Include this object in both economic market inputs and provenance so both
existing digests bind it. Before accepting persisted_input_bundle or any
persisted execution phase, load the record by ID and verify:

- current supported contract;
- matching epoch, session, and ticker;
- recomputed canonical evidence digest;
- stable recovery ID;
- exact reconstructed MarketBar;
- exact affected-cohort membership.

Raise the existing fail-closed context error before ledger mutation on any
mismatch.

### Step 4: Run lifecycle regressions and commit

~~~bash
.venv/bin/python -m pytest tests/test_session_executor.py \
  tests/test_metric_epoch_runtime.py tests/test_30day_simulation.py -q
.venv/bin/ruff check \
  tradingagents/strategies/orchestration/session_executor.py \
  tests/test_session_executor.py
git diff --check
git add tradingagents/strategies/orchestration/session_executor.py \
  tests/test_session_executor.py
git commit -m "feat: bind governed recovery to p0 replay"
~~~

## Task 5: Integrate cohort failure handling and degraded reporting

**Files:**

- Modify: tradingagents/strategies/orchestration/cohort_orchestrator.py
- Modify: tradingagents/strategies/orchestration/generation_manager.py
- Modify: scripts/run_cohorts.py
- Modify: tests/test_30day_simulation.py
- Modify: tests/test_cohort_failure_reporting.py
- Modify: tests/test_generation_manager.py

### Step 1: Write failing orchestration tests

Add one shared-bundle scenario with all 16 standard cohorts and the ESS fixture.
Assert:

- one governed resolution occurs before any ledger mutation;
- all cohorts requiring ESS receive the exact same reconstructed MarketBar;
- execution_valid is true;
- staging follows the normal strategy result;
- degraded is true and top-level success is false;
- governed_bar_recoveries contains one bounded ESS summary;
- the worker exits with the existing degraded-run exit code.

Add a rejected-recovery scenario and assert:

- execution_valid and staging_valid are false;
- no fill, mark, outcome, or staged intent is committed;
- every affected cohort points to the same critical-gap marker;
- governed_failure_map contains the exact normalized ticker/reason;
- the marker and generation history preserve the map even with no due outcomes;
- raw provider exception text is absent.

Run:

~~~bash
.venv/bin/python -m pytest tests/test_30day_simulation.py \
  tests/test_cohort_failure_reporting.py tests/test_generation_manager.py -q
~~~

Expected: failures for missing recovery/failure result fields.

### Step 2: Propagate structured results

In cohort_orchestrator.py:

- build the complete ticker-to-cohort map before the shared governed fetch;
- call SessionExecutor.fetch_input_bundle once;
- pass the same sliced bundle into each cohort;
- on GovernedMarketDataError, invoke the existing
  _stop_for_critical_market_data_gap path exactly once;
- store governed_failure_map on the critical-gap marker even when outcome
  signal collections are empty;
- do not use the candidate quarantine path for governed tickers.

In generation_manager.py and scripts/run_cohorts.py:

- add bounded governed_bar_recoveries and governed_failure_map fields;
- preserve existing degraded semantics:
  success=false, degraded=true, execution_valid=true;
- keep an unrecovered P0 gap failed and invalid;
- preserve the current non-zero degraded process exit code.

Do not change strategy selection, sizing, candidate staging, or promotion.

### Step 3: Run and commit

~~~bash
.venv/bin/python -m pytest tests/test_30day_simulation.py \
  tests/test_cohort_lifecycle.py tests/test_cohort_failure_reporting.py \
  tests/test_generation_manager.py -q
.venv/bin/ruff check \
  tradingagents/strategies/orchestration/cohort_orchestrator.py \
  tradingagents/strategies/orchestration/generation_manager.py \
  scripts/run_cohorts.py tests/test_30day_simulation.py \
  tests/test_cohort_failure_reporting.py tests/test_generation_manager.py
git diff --check
git add tradingagents/strategies/orchestration/cohort_orchestrator.py \
  tradingagents/strategies/orchestration/generation_manager.py \
  scripts/run_cohorts.py tests/test_30day_simulation.py \
  tests/test_cohort_failure_reporting.py tests/test_generation_manager.py
git commit -m "feat: report governed recovery and exact p0 failures"
~~~

## Task 6: Add read-only state topology and runtime locking

**Files:**

- Create: tradingagents/strategies/orchestration/runtime_lock.py
- Create: tradingagents/strategies/orchestration/preflight_state.py
- Create: tests/test_runtime_lock.py
- Create: tests/test_preflight_state.py
- Modify: tradingagents/strategies/orchestration/preflight.py
- Modify: tests/test_preflight.py

### Step 1: Write lock tests

Use subprocesses because flock is process-scoped. Prove:

- two preflights can acquire shared locks;
- an exclusive daily lock rejects a concurrent shared preflight;
- a shared preflight rejects a concurrent exclusive daily run;
- locks are non-blocking and always released after exceptions;
- the lock file lives in repo operational state, not a generation directory.

Run:

~~~bash
.venv/bin/python -m pytest tests/test_runtime_lock.py -q
~~~

Expected: import failure.

### Step 2: Implement the advisory lock

Create a context manager around fcntl.flock:

~~~python
@contextmanager
def runtime_lock(
    lock_path: Path,
    *,
    exclusive: bool,
) -> Iterator[None]:
    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    with lock_path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), mode | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeLockBusy(str(lock_path)) from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
~~~

The file may be created, but it must contain no generation data and must never
be used as proof of state identity.

### Step 3: Write state-topology tests

Construct complete, uninitialized, and partial state directories. Assert:

- zero metric stores and zero ledgers reports state_status=uninitialized;
- uninitialized state probes configured benchmarks only;
- one missing DB in an initialized generation fails closed;
- complete state opens MetricStore.open_existing and
  PortfolioLedger.open_existing only;
- pending entries, open lots, due outcomes, and benchmarks form the exact
  governed ticker set;
- an invalid or closed older epoch cannot leak outcomes into the requested
  session;
- an already-invalid requested session reports state_already_invalid;
- file stat identity and PRAGMA data_version are unchanged before/after;
- a changed identity/data version fails the probe;
- all read-only handles close on success and failure.

Run:

~~~bash
.venv/bin/python -m pytest tests/test_preflight_state.py -q
~~~

Expected: import failure.

### Step 4: Implement read-only state discovery

Create frozen topology/result dataclasses and:

~~~python
def inspect_preflight_state(
    *,
    state_dir: Path,
    cohort_ids: Collection[str],
    session: date,
    benchmark_tickers: Collection[str],
) -> PreflightStateSnapshot: ...
~~~

Rules:

- inspect existence before opening anything;
- never instantiate writable stores or run migrations;
- use SQLite URI mode=ro and PRAGMA query_only;
- snapshot path, inode, size, mtime_ns, and PRAGMA data_version before reads;
- recheck the same values after reads and fail on any change;
- use a nonmatching prospective epoch identity when the current epoch ended
  before the requested session so old outcomes cannot be selected;
- partial initialization, unreadable state, ambiguous epoch identity, or an
  unprovable required ticker set fails closed.

### Step 5: Extend preflight modes and contracts

In preflight.py retain the isolated screen/event-identity checks. Add modes:

- screen: current isolated integration checks only;
- governed: state-aware P0 probe only;
- all: both, with separate results.

Before XNYS close return governed_probe_status=not_ready without claiming bar
coverage. After close, call resolve_governed_bars with persist=False. Report:

- state_status;
- governed_probe_status;
- sorted governed_tickers;
- bounded proposed governed_bar_recoveries;
- exact governed_failure_map.

Prove in tests that preflight never writes a generation file, changes a SQLite
file, calls an LLM, stages a candidate, persists recovery evidence, or executes
a trade. Retain the existing pending-LLM candidate behavior.

### Step 6: Run and commit

~~~bash
.venv/bin/python -m pytest tests/test_runtime_lock.py \
  tests/test_preflight_state.py tests/test_preflight.py -q
.venv/bin/ruff check \
  tradingagents/strategies/orchestration/runtime_lock.py \
  tradingagents/strategies/orchestration/preflight_state.py \
  tradingagents/strategies/orchestration/preflight.py \
  tests/test_runtime_lock.py tests/test_preflight_state.py \
  tests/test_preflight.py
git diff --check
git add tradingagents/strategies/orchestration/runtime_lock.py \
  tradingagents/strategies/orchestration/preflight_state.py \
  tradingagents/strategies/orchestration/preflight.py \
  tests/test_runtime_lock.py tests/test_preflight_state.py \
  tests/test_preflight.py
git commit -m "feat: add state-aware read-only p0 preflight"
~~~

## Task 7: Wire locks, after-close gating, and run history

**Files:**

- Modify: scripts/run_cohorts.py
- Modify: scripts/daily_trading.sh
- Modify: tradingagents/strategies/orchestration/generation_manager.py
- Modify: tests/test_preflight.py
- Modify: tests/test_generation_manager.py

### Step 1: Write command and shell-contract tests

Add tests that prove:

- run_cohorts.py --preflight --preflight-mode all passes the real
  AUTORESEARCH_STATE_DIR to only the governed probe;
- temporary strategy state is still used for screen preflight;
- standalone pre-close preflight returns not_ready, not a false P0 success;
- daily execution acquires the exclusive lock;
- preflight acquires the shared lock;
- after-close screen preflight remains non-gating;
- after-close governed preflight blocks daily on an unrecovered P0 failure;
- a clean or proposed-recovery governed probe permits daily to start;
- run history preserves recovery summaries and exact failure maps;
- no shell path can invoke daily after a busy-lock or governed-probe failure.

Run:

~~~bash
.venv/bin/python -m pytest tests/test_preflight.py \
  tests/test_generation_manager.py -q
~~~

Expected: failures for CLI mode, lock, and shell gating behavior.

### Step 2: Wire the CLI and generation manager

Add --preflight-mode with choices all, screen, governed. Default to all for
direct invocations. Resolve the repo-level lock path once and:

- acquire shared non-blocking for every preflight;
- acquire exclusive non-blocking around the complete daily generation run;
- return a clear bounded busy status rather than waiting;
- pass real state only to governed inspection;
- preserve screen preflight's temporary state and no-LLM behavior.

Store governed_bar_recoveries and governed_failure_map in the existing
generation history payload with sorted ticker keys and bounded records.

### Step 3: Make after-close governed preflight gating

In scripts/daily_trading.sh run, in order:

1. screen preflight and record its result without gating daily;
2. governed preflight for the requested after-close session;
3. daily only if the governed probe is ready and valid.

Do not treat pre-close not_ready as success in the daily script. Do not add a
retry, sleep loop, historical replay, or automatic manual trigger.

### Step 4: Run shell and orchestration regressions, then commit

~~~bash
.venv/bin/python -m pytest tests/test_preflight.py \
  tests/test_generation_manager.py tests/test_cohort_failure_reporting.py -q
bash -n scripts/daily_trading.sh
.venv/bin/ruff check scripts/run_cohorts.py \
  tradingagents/strategies/orchestration/generation_manager.py \
  tests/test_preflight.py tests/test_generation_manager.py
git diff --check
git add scripts/run_cohorts.py scripts/daily_trading.sh \
  tradingagents/strategies/orchestration/generation_manager.py \
  tests/test_preflight.py tests/test_generation_manager.py
git commit -m "feat: gate daily execution on governed preflight"
~~~

## Task 8: Complete verification, review, and approved rollout preparation

**Files:**

- Modify only if verification reveals an in-scope defect.
- Do not modify deployment or generation state without a new explicit approval.

### Step 1: Run the focused safety suite

~~~bash
.venv/bin/python -m pytest \
  tests/test_market_data_contract.py \
  tests/test_governed_market_data.py \
  tests/test_metrics_store.py \
  tests/test_session_executor.py \
  tests/test_metric_epoch_runtime.py \
  tests/test_30day_simulation.py \
  tests/test_cohort_lifecycle.py \
  tests/test_cohort_failure_reporting.py \
  tests/test_generation_manager.py \
  tests/test_runtime_lock.py \
  tests/test_preflight_state.py \
  tests/test_preflight.py -q
~~~

Expected: all pass with no live provider calls.

### Step 2: Run static and full regression checks

~~~bash
.venv/bin/ruff check tradingagents scripts tests
.venv/bin/python -m pytest tests/ -q
bash -n scripts/daily_trading.sh scripts/preflight.sh
git diff --check
git status --short
~~~

Expected: Ruff clean, full suite green, shell syntax valid, and only intentional
files changed.

### Step 3: Review against the approved design

Invoke requesting-code-review. The reviewer must inspect at least:

- eligibility limited to daily OHLC incoherence;
- exact XNYS interval coverage including early close;
- no adjusted/cross-provider/synthetic price path;
- record saved before context binding;
- ID/version/digest bound into both P0 context digests;
- deletion/tamper/version mismatch crash-resume tests;
- exact shared bar across cohorts;
- candidate recovery unchanged;
- read-only topology and lock race behavior;
- degraded success=false semantics;
- exact governed failure reporting when no outcomes are due.

Resolve every P0/P1 finding with a new failing test first, rerun the focused
suite, and commit each coherent correction through the root agent.

### Step 4: Prepare publication, but stop before live changes

Invoke the ship skill only after all checks and review pass. It may commit,
push, open a PR, and merge only when the user authorizes those publication
steps. Before any Hermes action, stop and present:

- final commit and PR/merge identity;
- local verification evidence;
- files and migrations changed;
- exact rollback and read-only audit commands;
- confirmation that no provider or live run was invoked.

### Step 5: Perform deployment only under separate explicit approval

After a reviewed merge and a new approval:

1. verify Hermes trade.timer/service status, last run, remote commit, clean
   worktree, active generation manifest, and gen_008 immutable state;
2. create a timestamped rollback archive and verify its checksum;
3. fast-forward Hermes main only to the reviewed merge commit;
4. allocate the next generation only after rereading the manifest; gen_009 is
   expected, not assumed;
5. run read-only preflight for the next XNYS session;
6. do not touch gen_008, rerun 2026-08-10, or touch the run-now trigger;
7. leave the normal timer in control unless the user separately authorizes a
   same-session manual run.

Report deployed commit, generation ID, archive path/checksum, preflight result,
timer state, and whether any trigger was touched.

## Definition of done

- Every verification item in the approved design has a focused automated test.
- Existing candidate recovery and P0 fail-closed regressions pass unchanged.
- Accepted recovery evidence is immutable, bounded, replay-verifiable, and
  bound into both economic and provenance digests.
- Failed recovery remains mutation-free and reports the exact ticker/reason.
- Preflight derives the real governed ticker set without altering state.
- The full test suite and Ruff pass.
- Review finds no unresolved P0/P1 issue.
- No live deployment, generation creation, trigger, or historical rerun occurs
  without the separate approval defined above.
