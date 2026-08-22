# PR 3 old-versus-new orchestration parity

This record compares the deterministic incident corpus on the merged PR 2
behavior with the reviewed PR 3 implementation. The comparison uses the same
test files, the same collected node IDs, the same local Python environment, and
the same synthetic fixtures. These tests assert complete result dictionaries
and durable ledger/metric state, not only process exit status.

## Compared revisions

- Old behavior commit: `144061fdb293549910d6bd8642437e98ab9ebf01`
- Merged PR 2 commit: `18dd856057a0a334a61272f96f867339c1528c2e`
- PR 3 reviewed head before this evidence-only commit:
  `333546b948d24bc972a57eaf16787fd78a28a68c`

The old behavior commit and merged PR 2 have the identical tree:
`1ea8e5565c7b84ea249ceea887682b8e5e4fc931`. The preserved
`candidate-input-isolation` worktree was therefore an exact source snapshot of
the PR 3 base behavior, despite pointing to the reviewed PR head rather than
the merge commit.

## Incident corpus

The identical command was run in the preserved PR 2 worktree and the PR 3
worktree:

```bash
PYTHONPATH=. .venv/bin/pytest -q \
  tests/test_cohort_lifecycle.py \
  tests/test_shared_policy_volatility_evidence.py \
  tests/test_cohort_failure_reporting.py \
  tests/test_generation_manager.py \
  tests/test_30day_simulation.py
```

The collected test-node roster was hashed in both worktrees with:

```bash
PYTHONPATH=. .venv/bin/pytest --collect-only -q <the five files above> \
  | rg '::' | shasum -a 256
```

Both revisions produced the same roster hash:

```text
04ace2436ff0c48a12beb2c16a794cca0076db5b1d9832120aa23e76f6da2959
```

Both revisions produced the same test result:

```text
271 passed, 4 warnings
```

The warnings were the same dependency deprecations from websockets, OpenBB,
and Pydantic. There were no product warnings or skipped incident tests.

## Behavior frozen by the comparison

The shared 271-test corpus covers:

- complete, partial, and stored-execution replay;
- candidate reference-bar recovery, quarantine, immutable evidence, tamper,
  identity conflict, governed overlap, and zero-provider-I/O replay;
- candidate volatility recovery, quarantine, exact XNYS session evidence,
  cache mutation followed by provider error, governed overlap, and no-refetch
  replay;
- critical-gap creation/completion, invalid metric epochs, due outcomes, and
  ledger mutation boundaries;
- exact worker envelope, result lifecycle fields, clean/degraded/failed
  classification, return-code contradictions, run-level issue deduplication,
  and manifest history projection;
- 30-day execution, idempotency, partial resume, borrow evidence, staging, and
  accounting invariants.

PR 3 also adds `tests/test_daily_pipeline_characterization.py`, whose six
table-driven cases assert exact canonical summaries for clean, reference-bar
degraded, volatility degraded, candidate-plus-real-failure, governed recovery,
and governed failure results. That test has no old implementation dependency;
it freezes the new shared reporting boundary used by both the worker and
generation manager.

## Reviewed-head verification

At `333546b948d24bc972a57eaf16787fd78a28a68c`:

- focused PR 3 gate: `483 passed, 4 warnings`;
- full non-live suite: `1953 passed, 4 deselected, 4 warnings`;
- dependency check, compileall, changed-file Ruff, shell syntax, and diff check:
  passed;
- production surface: 4,930 physical and 4,603 nonblank lines;
- `CohortOrchestrator.run_daily()`: 91 lines.

This document changes evidence only. The implementation tree used for the
old-versus-new execution comparison is the reviewed head named above.
