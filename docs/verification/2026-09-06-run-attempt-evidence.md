# Run attempt evidence verification — September 6, 2026

Validated in an isolated EventEdge checkout on `codex/matrix-run-evidence`, based
on fetched `origin/main` at `0ffd2cf`. Python 3.12.14; dependencies installed from
the existing project metadata into a fresh local virtual environment. No runtime
dependencies or production settings were changed.

## Results

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

## Commands

```bash
.venv/bin/python -m pytest -m 'not live' -q
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
