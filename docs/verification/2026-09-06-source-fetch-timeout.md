# Source-fetch timeout verification

## Problem and boundary

The earlier expanded testing of PR #37 exposed an existing race in
`_gather_with_timeout`. If a future finished between `as_completed` raising its
timeout and the handler inspecting `future.done()`, neither the payload nor the
exception was collected. Its source entry remained `{}` and could be classified
as `legitimate_no_event`.

This independent branch starts at fetched `origin/main` commit `0ffd2cf`. It
does not depend on the archive changes in PR #37. Production was not accessed or
changed during this repair, and the race is not asserted to explain a particular
historical VPS incident.

## Fix

One `concurrent.futures.wait` call supplies the completed/pending partition.
Every completed source retains its result or existing exception payload. Every
pending source receives the existing timeout payload, even if it completes while
the response dictionary is assembled. The returned wait partition defines
admission; it does not represent independently timestamped provider completions
or a guaranteed exact wall-clock cutoff.

The input dictionary's source order, successful empty responses, source parameters,
timeout values, and `shutdown(wait=False, cancel_futures=True)` remain unchanged.
Queued work is cancelled; running provider threads cannot be safely stopped by
this helper. Completed-result processing and exception logging happen after the
collective wait; the sole production caller consumes only the final dictionary.

## Regression evidence

- The unchanged source-health suite passed 16 tests, and the existing fetch/health
  suites passed 28 tests on Python 3.11 after replacing the short-sleep health
  fixture with an event released in `finally`.
- A deterministic synthetic reproducer confirmed both nonempty success and
  provider exception being discarded by the original helper.
- The new boundary suite first produced **three failures and one pass** against
  the original implementation. The failures required preserved completed results,
  explicit timeouts for late completions, and complete mixed-source outcomes.
- Tests use real `Future` objects with a controlled scheduler boundary, including
  an unfinished future that fails immediately if incorrectly consumed. They
  retain genuine successful `{}` results and verify source ordering and shutdown.
- After the fix, **65 focused tests passed on Python 3.12**, covering the new
  boundary cases, existing fetch timeouts, source-health classification, and
  cohort construction/integration. Existing assertions cover 12 active strategies
  and 16 portfolios.
- Independent final review accepted the implementation and separately passed
  **32 fetch/health/boundary tests**.
- The complete non-live Python 3.11.16 suite passed **1,975 tests**, with four
  live tests deselected and one upstream `websockets.legacy` deprecation warning,
  in 90.34 seconds.
- The complete non-live Python 3.12.14 suite also passed **1,975 tests**, with
  the same four live tests deselected and upstream warning, in 90.19 seconds.
- Focused code-error Ruff checks passed for all changed Python files; full
  configured Ruff and formatting checks passed for the new boundary test file.
  `git diff --check` passed. Strategy definitions and cohort construction have
  no diff from `origin/main`.

These are local macOS results. Live provider calls, Linux/VPS execution, and
prospective research qualification were not exercised.

## Verification commands

Run against this checkout using its dependencies and an explicit `PYTHONPATH`
when reusing an environment installed for a different checkout:

```sh
PYTHONPATH="$PWD" python -m pytest tests/test_fetch_timeout_boundary.py tests/test_fetch_timeout.py tests/test_strategy_health.py tests/test_cohort_redesign.py -q
PYTHONPATH="$PWD" python -m pytest -m 'not live' -q
python -m ruff check --select E4,E7,E9,F tradingagents/strategies/orchestration/multi_strategy_engine.py tests/test_strategy_health.py tests/test_fetch_timeout_boundary.py
python -m ruff check --target-version py310 tests/test_fetch_timeout_boundary.py
git diff --check
```

## Deployment boundary

The change is prepared for a topic-branch PR only. It changes source admission at
the race boundary and therefore requires a new generation if deployed. All 12
strategies and 16 portfolios remain intact. Historical results are not rewritten;
prospective VPS qualification and live-provider reliability remain separate work.
