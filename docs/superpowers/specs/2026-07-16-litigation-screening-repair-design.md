# Litigation Screening Repair Design

## Problem

The 2026-07-16 `gen_003` VPS run fetched 60 CourtListener dockets but produced no litigation signals. The run completed successfully; the failure was in candidate selection.

`LitigationStrategy._is_class_action()` currently treats every case name containing `" v. "` as a class action. That admitted 57 of the 60 fetched dockets, including ordinary disputes with no public-company defendant. The strategy then truncated the list to `max_positions=3` before LLM enrichment. The first three cases had no public-company ticker, so enrichment returned blank tickers and `MultiStrategyEngine._resolve_signals()` discarded them. Public-company cases for Five Below, Regeneron, Apple, and Zillow appeared later in the same result set and never reached enrichment.

The replay also exposed a false-positive risk in EDGAR name resolution. Its prefix fallback mapped generic or ambiguous defendants to valid but unrelated tickers, including `United States` to `USLM` and `Butler` to `BUKS`.

## Goals

- Restore litigation coverage when qualifying public-company cases are present.
- Reject ordinary `X v. Y` cases unless another litigation signal qualifies them.
- Prevent ambiguous EDGAR prefix matches from becoming litigation tickers.
- Deduplicate dockets returned by multiple CourtListener queries.
- Keep LLM work bounded by the existing `max_positions` setting.
- Add enough logging to distinguish a legitimate no-event day from filtering or data failure.
- Apply the same tested source change to the durable main checkout and the existing VPS `gen_003` worktree without creating a new generation.

## Non-goals

- Change portfolio sizing, short eligibility, committee thresholds, or risk gates.
- Change CourtListener queries or API request volume.
- Move candidate limiting into the shared orchestration engine.
- Re-run the completed 2026-07-16 trading day or mutate its recorded portfolio state.
- Repair unrelated Finnhub, FRED, USDA, or EDGAR transport failures observed in the run.

## Design

### Candidate classification

`LitigationStrategy` will classify a docket as eligible only when at least one of these conditions holds:

1. The defendant resolves exactly to an SEC-listed company after normal company-suffix normalization.
2. The case name explicitly contains class-action or securities-litigation language.
3. The normalized nature-of-suit text contains a configured high-signal litigation keyword.

The generic `" v. "` condition will be removed from class-action detection. Nature-of-suit matching will normalize punctuation so CourtListener values such as `850 Securities/Commodities` and `410 Anti-Trust` match the intended securities and antitrust categories.

### Exact ticker resolution

`EDGARSource.name_to_ticker()` will accept a backward-compatible keyword argument controlling prefix fallback. Existing callers retain the current default behavior. `LitigationStrategy` will disable prefix fallback and therefore accept only exact matches after EDGAR's existing suffix normalization.

This keeps the behavioral change local to litigation while avoiding private-method access or a second company-name map. Exact matching continues to resolve examples such as `Apple Inc.`, `Regeneron Pharmaceuticals, Inc.`, `Five Below, Inc.`, and `Zillow Group Inc.` because corporate suffixes are already normalized.

### Deduplication and ordering

CourtListener dockets will be deduplicated inside `LitigationStrategy.screen()` before classification, using `docket_id` when present and a stable case-name/date fallback otherwise. The first occurrence is retained.

Eligible candidates will be ranked before applying `max_positions`:

1. SEC enforcement releases.
2. CourtListener cases with an exact public-company ticker.
3. Unresolved but explicitly high-signal or class-action cases that require LLM ticker resolution.

Within each tier, higher base score comes first and source order breaks ties. This preserves the existing LLM-call ceiling while ensuring broad, unresolved results cannot crowd out identified public companies.

### Observability

Each litigation screen will emit one concise INFO line containing:

- fetched docket count;
- unique docket count;
- eligible CourtListener count;
- SEC release count;
- selected candidate count;
- selected ticker-resolved and unresolved counts.

This line must contain no raw API payloads or secrets.

## Tests

Tests will be added before production changes and must fail against the current implementation.

Regression coverage will prove that:

- an ordinary `X v. Y` case is not a class action;
- exact company defendants resolve while generic prefix defendants do not;
- duplicate docket IDs produce one candidate;
- the July 16 ordering selects valid public-company cases instead of the first three irrelevant cases;
- coded securities and anti-trust nature strings are recognized;
- the selected candidate count remains bounded by `max_positions`;
- SEC enforcement candidates receive the documented priority;
- the litigation screen emits classification counts.

Tests will seed EDGAR's in-memory company-name map and will not call live APIs or an LLM.

## Verification

Local verification will run the new focused tests, the relevant strategy and signal-resolution tests, and then the complete test suite. The patch will be checked with `git diff --check`.

Deployment verification will copy only the tested source and regression-test files to the VPS main checkout and `.worktrees/gen_003`, preserving the existing `gen_003/tradingagents/default_config.py` modification. The focused tests will run in both VPS checkouts. A read-only live CourtListener replay in `gen_003` will confirm that the July 16-shaped result set selects valid public-company candidates and logs screening counts. No daily trading rerun will be started.

## Deployment and rollback

The durable source change will be committed on the current local branch. The identical files will be hot-deployed to the VPS main checkout and detached `gen_003` worktree. Generation manifest and state directories will not be changed, so the active generation remains `gen_003`.

Before deployment, checksums of the local tested files will be captured. After deployment, the VPS copies must match those checksums. Existing unrelated dirty files will not be staged, overwritten, or reverted.

If verification fails, the hot deployment will stop before any trading run. Rollback consists of restoring only the patched litigation, EDGAR, and regression-test files from their pre-deployment `gen_003` commit versions while preserving the existing configuration override and state.
