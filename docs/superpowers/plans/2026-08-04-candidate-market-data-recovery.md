# Candidate Market-Data Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Recover one transient malformed raw bar for a new candidate without weakening fail-closed accounting or modifying active generations.

**Architecture:** `get_daily_bars()` stays the all-or-nothing governed-execution contract. A new candidate-only resolver keeps healthy batch bars, makes exactly one cache-bypassing single-symbol retry for an invalid candidate, stores immutable bounded recovery evidence, and removes only an unrecovered candidate before every cohort stages. A completed P0 account session stays valid; its screening result is explicitly degraded.

**Tech Stack:** Python 3.12, pytest, pandas/yfinance, SQLite JSON records.

## Global Constraints

- Branch is based on `private/main`; never change, replay, or deploy `gen_004` or `gen_005`.
- Open lots, due exits, pending entries, outcomes, corporate actions, marks, and benchmarks retain current strict fail-closed behavior.
- A candidate gets one batch attempt and at most one explicit cache-bypassing single-ticker retry. Never substitute a second vendor or synthetic price.
- A candidate overlapping governed execution data reuses the validated bundle and gets no retry/quarantine.
- Persist only bounded normalized evidence: ticker, session, source, fetch time, OHLC if available, validation reason, attempt order, outcome, strategy, and event identity.
- Candidate quarantine is session/epoch-scoped only; never use the persistent ledger `ticker_quarantines` table.
- Candidate data failure is a reportable data/availability failure, never a silent strategy.
- Use red-green TDD. Subagents never commit, push, merge, start a generation, or touch Hermes.

---

### Task 1: Structured candidate bar attempts and cache-bypass retrieval

**Files:** Modify `tradingagents/strategies/execution/price_source.py`; modify `tests/test_market_data_contract.py`.

**Interfaces:** Add frozen `CandidateBarAttempt` (ticker, session, attempt, source, fetched_at, open, high, low, close, validation_error) and `CandidateBarResolution` (bars, attempts, recovered_tickers, quarantined_tickers). Add `PriceSource.resolve_candidate_daily_bars(tickers, session, processed_at, max_age)` and `YFinancePriceSource.refresh_daily_bars(tickers, session, session)`.

- [ ] Step 1: Add tests using AAPL/MSFT. First mocked batch has coherent AAPL and MSFT close greater than high; refreshed MSFT is coherent. Assert two provider calls, both bars returned, attempt outcomes `invalid`, `valid`, and MSFT recovered.
- [ ] Step 2: Add a persistent-invalid MSFT case. Assert AAPL remains returned; MSFT is the only quarantined ticker; exactly two attempts preserve normalized OHLC/error evidence.
- [ ] Step 3: Add a cache test: call raw daily bars, then `refresh_daily_bars(["AAPL"], session, session)` and assert the mocked provider call count increments. This proves fresh retrieval is not just a different bounded-cache key.
- [ ] Step 4: Run `/Users/potalora/ai_workspace/trading_agents/.venv/bin/python -m pytest tests/test_market_data_contract.py -q`; observe red because the new interfaces do not exist.
- [ ] Step 5: Implement a per-ticker parsing/validation helper that uses existing `validate_required_bars()` for each complete `MarketBar`, converts conversion/validation failure into an attempt, and retains valid batch bars. `get_daily_bars()` must not change. A whole-request/provider exception still raises. Retry only initially invalid candidate tickers via the explicit cache-bypass method; a second invalid result is a quarantine, not an exception.
- [ ] Step 6: Re-run the same test file and require all tests green.
- [ ] Step 7: Root review: `git diff --check`, inspect only the two task files, then commit with `feat(market-data): recover candidate bars once`.

### Task 2: Immutable candidate recovery evidence in shared metrics

**Files:** Modify `tradingagents/strategies/metrics/models.py`, `tradingagents/strategies/metrics/store.py`, and `tests/test_metrics_store.py`.

**Interfaces:** Add frozen `CandidateBarRecoveryRecord` with recovery_id, epoch_id, session, ticker, outcome (`recovered` or `quarantined`), ordered attempt evidence, and ordered signal identity evidence. Add `MetricStore.save_candidate_bar_recovery(record)` and `MetricStore.read_candidate_bar_recoveries(epoch_id, session=None, limit=1000)`.

- [ ] Step 1: Add a two-attempt ALX quarantined fixture. Assert save/read date and timestamp round-trip, deterministic ordering, and duplicate identical save idempotence.
- [ ] Step 2: Assert a different payload with the same recovery id raises `ValueError` containing `unequal payload`; assert overlong text or more than two attempts/signals over the stated cap is rejected.
- [ ] Step 3: Run `/Users/potalora/ai_workspace/trading_agents/.venv/bin/python -m pytest tests/test_metrics_store.py -q`; observe red.
- [ ] Step 4: Add a `candidate_bar_recoveries` table (recovery_id primary key, epoch_id, session, payload_json) plus epoch/session index. Update read-only schema validation. Validate XNYS session, uppercase bounded ticker, allowed outcome, one/two attempts, permitted bounded evidence keys, and bounded signal identities before using the existing immutable insert helper.
- [ ] Step 5: Read deterministically ordered by session, ticker, recovery id, and preserve the existing `limit` validation.
- [ ] Step 6: Re-run the metric tests and require green.
- [ ] Step 7: Root review and commit `feat(metrics): audit candidate bar recovery`.

### Task 3: Separate governed bars from candidate-only staging

**Files:** Modify `tradingagents/strategies/orchestration/session_executor.py`, `tradingagents/strategies/orchestration/cohort_orchestrator.py`, `tradingagents/strategies/orchestration/multi_strategy_engine.py`, `tradingagents/strategies/state/state.py`, `tests/test_cohort_lifecycle.py`, and `tests/test_cohort_failure_reporting.py`.

**Interfaces:** Governed tickers use existing execution validation. Candidate-only tickers use Task 1. The orchestrator converts Task 1 outcomes into Task 2 records, filters quarantined candidates from all horizon signal lists, and returns per-cohort `degraded`, `execution_valid`, `staging_valid`, and `candidate_bar_quarantines` fields.

- [ ] Step 1: Build a completed-P0 fixture containing governed AAPL plus candidate ALX/MSFT. Mock ALX invalid on bulk and retry. Assert each horizon removes only ALX, MSFT reaches `screen_and_stage`, one metric record exists, `execution_valid` is true, `staging_valid` false, the epoch stays open, and no critical-gap marker exists.
- [ ] Step 2: Add a recovered-ALX case that reaches staging, a governed-bar failure case that retains the current critical-gap path, and an overlap-AAPL case that reuses P0 bars without a candidate fetch.
- [ ] Step 3: Add a failure-reporting assertion: degraded is reportable and not reclassified as all-cohort execution failure or silent strategy.
- [ ] Step 4: Run `/Users/potalora/ai_workspace/trading_agents/.venv/bin/python -m pytest tests/test_cohort_lifecycle.py tests/test_cohort_failure_reporting.py -q`; observe red.
- [ ] Step 5: Derive governed tickers only from validated P0 execution data. Set candidate-only tickers to screened-signal tickers minus governed tickers; reuse the bundle for overlap. Do not call `_stop_for_critical_market_data_gap()` for a known candidate-only quarantine. Preserve all present critical-gap handling for governed/unknown provider errors.
- [ ] Step 6: Persist every recovery outcome before signal filtering. Filter every horizon's signals and pass surviving candidate bars in `_execution_reference_bars`. Merge the explicit status fields into cohort results without overwriting staged signals/trades.
- [ ] Step 7: Remove eager `StateManager.save_regime_snapshot()` from `MultiStrategyEngine.screen_and_enrich()`. Persist once through the orchestrator only after candidate resolution with session/epoch/status metadata and idempotent replay behavior; a quarantined state cannot be promoted as clean screening evidence.
- [ ] Step 8: Update generation failure reporting so the run is visible as degraded while a completed P0 account session remains valid.
- [ ] Step 9: Re-run both test files and require green.
- [ ] Step 10: Run the cross-boundary suite: `/Users/potalora/ai_workspace/trading_agents/.venv/bin/python -m pytest tests/test_market_data_contract.py tests/test_metrics_store.py tests/test_session_executor.py tests/test_cohort_lifecycle.py tests/test_cohort_failure_reporting.py -q`.
- [ ] Step 11: Root review and commit `feat(orchestration): quarantine transient candidate data`.

### Task 4: Explain the candidate-data policy and hand off gen_006 safely

**Files:** Modify `README.md`; modify `tests/test_cohort_failure_reporting.py` only if needed for the human-facing classification.

- [ ] Step 1: Add a non-technical paper-trading-safety paragraph: governed raw-price defects block execution; a new candidate gets one fresh check, then is excluded only for that session with durable evidence. State that a quarantine is a visible data failure, not a clean performance observation.
- [ ] Step 2: Run `git diff --check` and `/Users/potalora/ai_workspace/trading_agents/.venv/bin/python -m pytest tests/test_cohort_failure_reporting.py -q`; require success.
- [ ] Step 3: Root review and commit `docs: explain candidate data quarantine`.

## Final Verification and Handoff

- [ ] Run `/Users/potalora/ai_workspace/trading_agents/.venv/bin/python -m pytest tests/ -q`.
- [ ] Run `git diff private/main...HEAD --check`, inspect `git status --short`, and obtain whole-branch review. Resolve all Critical/Important findings, rerun covering tests, and re-review.
- [ ] Push only `codex/market-data-recovery` to `private` and open a PR. Do not merge.
- [ ] After Pedro reviews and merges, verify private-main parity. Only then, with separate explicit authorization, back up Hermes, deploy the merged commit, start `gen_006`, and retain `gen_004`/`gen_005` unchanged.
