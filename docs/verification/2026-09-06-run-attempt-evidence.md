# Run attempt evidence verification — September 6, 2026

Validated in an isolated EventEdge checkout on `codex/matrix-run-evidence`, based
on fetched `origin/main` at `0ffd2cf`. Python 3.12.14; dependencies installed from
the existing project metadata into a fresh local virtual environment. No runtime
dependencies or production settings were changed.

## Initial implementation results

- Baseline manager/preflight/shell contracts: 105 passed.
- New CLI regressions first reproduced missing governed reasons and evidence
  pointers: 8 failed before implementation, then passed with existing shell gates.
- New archive regressions first reproduced missing immutable files, preflight
  capture, timeout/launch diagnostics, state-isolation guarantees, and diagnostic
  pointers. The implementer recorded 9 expected failures before the fix.
- Independent review found quoted credentials containing whitespace or punctuation
  could survive redaction. Four reproductions failed before the fix; the complete
  quoted-value/escape-aware fix then passed. The reviewer independently verified
  five direct cases and cleared the final branch.
- Collision and publication-failure injection proves an earlier artifact cannot
  be overwritten and failed publication leaves no final or temporary artifact.
- Final full suite: **1,995 passed, 4 live tests deselected**, 105.27 seconds.
  One existing third-party `websockets.legacy` deprecation warning was emitted.
- Ruff code-error rules passed across every changed Python file. Full configured
  rules passed on all four new Python files. Existing full-rule style warnings in
  the older manager/CLI/tests were not expanded into unrelated changes (including
  shebang permissions and pre-existing naive timestamps).
- `git diff --check` passed. Root reviewed source boundaries and matrix preservation;
  independent review returned spec compliance and code quality pass.

## Expanded testing after PR review

The follow-up testing request added adversarial, multiprocess, fault-injection,
and real-child-process coverage. Every input is synthetic and all worker state is
under pytest temporary directories; no VPS, provider API, or live trading run was
used.

### Defects reproduced and corrected

- The first adversarial corpus produced **25 failures and 117 passes**. Escaped
  environment values, truncated quotes, Digest/AWS authorization headers,
  credential-name variants, and dynamic dictionary keys could leak credentials;
  broad token matching also erased useful usage counters.
- Independent review caught a regression in suffix-bearing environment names.
  Three additional tests first failed for `AWS_SECRET_ACCESS_KEY`,
  `APCA_API_KEY_ID`, and `API_KEY_PRIMARY`, then passed after broad environment
  matching was restored. All **145 adversarial cases** now pass.
- Two fault-injection tests demonstrated that temporary-file unlink failures
  masked either a successful publication or the original serialization error.
  Cleanup now logs a warning and preserves the primary result.
- Review corrected a test-harness queue deadlock: child results must be drained
  before joining writers. A long-directory-path case exercises messages larger
  than a pipe buffer.

### Coverage and boundaries

- Each concurrency scenario launches **8 writers with 25 attempts each** and a
  simultaneous reader. Normal and long directory paths each produce 200 distinct
  complete JSON files, with no leftover temporary files. Under `umask(0)`, files
  are `0600` and the archive directory is `0700`.
- Deterministic process barriers place SIGKILL immediately before and after the
  hard-link publication boundary. No partial final JSON is exposed. A private
  temporary file can remain, and killing before publication leaves no final file.
- Serialization, flush, fsync, hard-link, filename-collision, and cleanup failures
  are injected separately. Existing archives remain byte-identical.
- Actual Python child processes exercise success/failure/success for the same
  session, partial stdout/stderr on timeout, child reaping, missing executables,
  malformed preflight output in all three modes, and a real unusable archive path.
  All 16 sentinel SQLite files and generation-state metadata remain byte-identical;
  completed worker outcomes survive archival failures.
- With 64 synthetic environment credentials, 2,098,941 bytes of ordinary output
  sanitized in 1.120 seconds and 2,280,010 bytes of truncated quoted output in
  0.817 seconds on this Mac. Assertions checked redaction and ordinary diagnostic
  preservation; these are observations, not performance guarantees.

### Runtime compatibility

The project declares Python `>=3.10`. A fresh Python 3.10.21 environment passed
**178 final diagnostic tests** in 18.59 seconds. The broader suite stopped during collection with ten
errors: nine existing test modules import Python 3.11's `datetime.UTC`, and the
installed SciPy 1.15.3 binary could not load on this macOS version. The unchanged
baseline contains the incompatible imports. These are separate compatibility
findings; this patch does not change interpreter requirements or dependencies.

The initial expanded Python 3.11.16 full run finished with **2,151 passed, one
failed, four live tests deselected** in 425.74 seconds. It began before the final
three credential-name regressions and long-path concurrency case were added.
Its failure was the existing
`test_timed_out_fetch_is_retained_and_classified_as_data_failure`: the timeout
handler reported no unfinished futures, yet the source result was still empty.

### Separate source-fetch timeout race

In `_gather_with_timeout`, a future can finish between `as_completed` raising its
timeout and the handler checking `future.done()`. The handler marks only unfinished
futures as failed, so the newly finished future is neither collected nor marked
as a failure. Its result remains `{}`, which can appear to be healthy empty data.
The function is unchanged from `origin/main`; this is a newly exposed existing
defect, not a claim about the cause of the historical VPS incidents.

The synthetic [reproducer](reproduce_fetch_timeout_race.py) confirms on both
Python 3.11 and 3.12 that a completed non-empty result and a completed exception
are each discarded. It uses real `Future` objects and controls the timeout
boundary without threads or sleeps. Run it with
`.venv/bin/python docs/verification/reproduce_fetch_timeout_race.py`. Its assertions
confirm the defect exists; this is not a passing correctness test for that path.

Remediation should be a separate reviewed behavior change for a new generation:
track which futures were consumed, then explicitly classify every unconsumed
source at the deadline. Freeze whether late completions are accepted or counted
as timeouts; do not let a second scheduler-dependent `done()` check choose the
research input. Regressions must cover late successful results, late exceptions,
and still-running sources without relying on short sleeps. This diagnostic PR
does not change source selection or hide the failed compatibility run.

The final focused diagnostic suite also passed **178 tests on Python 3.11.16** in
20.82 seconds. Independent review accepted the redaction and cleanup changes after
the credential-name and queue-draining corrections. Full configured Ruff rules
pass on the archive helper and new tests; code-error rules pass on the previously
modified manager, CLI, and manager tests.

The final Python 3.12.14 full suite passed **2,156 tests**, with four live tests
deselected and one upstream `websockets.legacy` deprecation warning, in 340.45
seconds. This is 161 additional tests beyond the initial implementation run. The
passing run does not invalidate the separately reproduced timeout race: its
existing sleep-based test can pass when the race is not triggered.

Final source review, full configured Ruff on new Python files, focused code-error
lint on previously modified files, formatting checks, and `git diff --check`
passed. No CI checks are currently configured on PR #37; these results are local
macOS verification, not Linux/VPS or live-provider qualification. Production,
generation state, strategy definitions, and portfolio construction were not changed.

## Commands

```bash
.venv/bin/python -m pytest -m 'not live' -q
# Focused cross-version suite (run with each fresh environment's Python):
python -m pytest tests/test_run_evidence*.py -q
.venv/bin/python docs/verification/reproduce_fetch_timeout_race.py
.venv/bin/python -m ruff check --select E4,E7,E9,F \
  scripts/run_generations.py \
  tradingagents/strategies/orchestration/generation_manager.py \
  tradingagents/strategies/orchestration/run_evidence.py \
  tests/test_generation_manager.py tests/test_run_evidence.py \
  tests/test_run_evidence_cli.py tests/test_run_evidence_publish.py
.venv/bin/python -m ruff check --target-version py310 \
  tradingagents/strategies/orchestration/run_evidence.py \
  tests/test_run_evidence.py tests/test_run_evidence_cli.py \
  tests/test_run_evidence_publish.py
git diff --check
```

## Scope and limits

The full 12-strategy/16-portfolio matrix, worker result interpretation, process
exit behavior, and economic decision paths remain unchanged. Preflight retains
its complete generation-tree identity regression; only external operational
archives are added. Logging errors cannot turn completed economic work into a
retry or rewrite its outcome.

This verifies evidence retention and CLI diagnostics, not source reliability,
research eligibility, historical incident causes, or prospective performance.
The original September 1–2 detailed exceptions and September 4 preflight failure
cannot be reconstructed from these changes. The matrix reliability design records
the follow-on work. No deployment, production replay, timer/service change, or
generation mutation was performed.
