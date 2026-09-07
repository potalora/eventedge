# Source-fetch timeout repair

> **For agentic workers:** Use the subagent-driven-development workflow for bounded tests and review; the root agent owns the behavior change and verification.

**Goal:** Prevent completed source results or exceptions from disappearing as healthy empty data at the timeout boundary.

**Architecture:** Replace the `as_completed` timeout plus a second `done()` scan with one `concurrent.futures.wait` completed/pending partition. Consume completed results and exceptions; classify every pending source as timed out even if it finishes while the result is being assembled. This is a scheduler observation boundary, not a guarantee of exact wall-clock completion timestamps.

**Tech stack:** Existing Python standard-library futures and pytest. No dependency changes.

## Constraints

- Preserve all 12 strategies and all 16 portfolios, source ordering, result/error formats, timeout values, and nonblocking executor shutdown.
- Preserve genuine successful empty results; explicit timeout and exception payloads distinguish unavailable data.
- Running threads cannot be cancelled by this helper. Continue cancelling queued work on shutdown, without waiting for running providers.
- This changes accepted source data at a race boundary. Deployment requires a new generation and Pedro's explicit deployment instruction. This task ends at a reviewed PR.
- Branch from fetched `origin/main`; do not merge, push main, or modify production.

## Task and acceptance

Files: `tradingagents/strategies/orchestration/multi_strategy_engine.py`,
`tests/test_fetch_timeout_boundary.py`, `tests/test_strategy_health.py`.

- [x] Reproduce lost successful and exceptional futures using real `Future` objects and a controlled scheduler boundary. The existing helper must fail assertions requiring their preserved payloads.
- [x] Replace iterator/exception bookkeeping with `done, pending = wait(futures, timeout=timeout_s)`. For every submitted future, use membership in `done` to consume its result with existing exception conversion; otherwise emit the existing timeout payload. Never re-check `done()` to admit late data.
- [x] Verify pending successes and exceptions that finish after the snapshot remain timeouts; genuine empty successes remain empty; mixed inputs retain every source in submission order; no source requires a blocking `result()` call unless it belongs to the completed set.
- [x] Replace the source-health timeout test's short sleep with an event-controlled worker. Release its event in `finally`; verify the resulting health classification is `data_failure`.
- [x] Run focused source-fetch/health tests on Python 3.11 and 3.12, then the non-live suite on both runtimes. Check lint for new code and changed-line scope. Record any failures truthfully.
- [x] Obtain independent review, commit only the topic-branch files, push, and open a PR. Record verification and the new-generation deployment boundary.

The alternative of rescanning completed futures after `as_completed` times out
could recover lost data, but would keep a second admission decision sensitive to
handler scheduling. One partition makes that decision explicit and reduces the
number of paths that must classify a source.
