# SEC Primary Ticker Resolution Design

**Date:** 2026-08-27

**Status:** Approved (2026-08-27)

**Base revision:** `ef1bb2dda08e225b92108cd13a070a98b1d121c8`

## Incident

The scheduled 2026-08-27 `gen_012` run completed all 16 cohorts with valid
execution and snapshots, but every cohort was degraded and staging-invalid.
The only candidate-input issues were missing volatility history for `GL-PD`
and `NEXRW`, both emitted by the litigation strategy. No external orders were
submitted for either symbol.

The SEC `company_tickers.json` feed contains more than one ticker for a single
normalized issuer name. `EDGARSource._ensure_name_map()` currently assigns each
entry into a one-value dictionary, so the last entry silently overwrites every
earlier one. In the current SEC feed:

- Globe Life Inc. appears as common stock `GL` and preferred stock `GL-PD`;
- Nexera Technologies Ltd. appears as common stock `NEXR` and warrant `NEXRW`.

The later preferred/warrant entries therefore replaced the common tickers.
Both common tickers have complete 2026-06-01 through 2026-08-26 daily history,
while the selected instruments have none. The downstream volatility gate
correctly failed closed; the defect is the upstream issuer-to-ticker choice.

## Goals

- Resolve duplicate SEC issuer names to a deterministic primary standard
  ticker rather than SEC payload order.
- Resolve the observed issuers to `GL` and `NEXR`.
- Keep litigation exact-name matching and every existing market-data safety
  gate unchanged.
- Prove the selection is stable when duplicate SEC entries arrive in a
  different order.
- Ship the reviewed merge only through a fresh immutable VPS generation.
- Preserve the full `gen_012` incident record and explicitly terminalize its
  reviewed pending intents before retirement.

## Non-goals

- Do not backdate or replay the 2026-08-27 session.
- Do not patch the detached `gen_012` worktree in place.
- Do not weaken candidate-input, governed-data, execution, or staging gates.
- Do not add another live API dependency to litigation screening.
- Do not infer a security type from Yahoo availability or silently discard an
  issuer that has a usable common-equity ticker.
- Do not migrate `gen_012` ledger state into the replacement generation.

## Resolver change

`EDGARSource._ensure_name_map()` will continue to expose the existing
normalized-name-to-string interface. While building that map, it will compare
every duplicate candidate with a deterministic preference key:

1. all-letter symbols before symbols containing punctuation or other
   non-letter characters;
2. shorter symbols before longer symbols;
3. lexical order as the final stable tie-break.

This policy selects a standard base symbol over the usual preferred, warrant,
right, or unit extension without encoding an unsafe list of suffix letters.
That distinction matters because valid common tickers can themselves end in
`R`, `U`, or `W`. Equal-class cases such as multiple common share classes use
the lexical tie-break; either class still represents common equity and the
result is independent of SEC response order.

The cache remains one dictionary and `name_to_ticker()` retains its public
signature. Exact and compatibility prefix lookups therefore receive the same
stable resolution. No caller or state format changes.

## Defense in depth

The existing candidate volatility-history fetch remains authoritative. A
resolved ticker without the required governed history will still create a
durable candidate-input issue and make staging invalid. The resolver change
prevents known derivative collisions; it does not convert the SEC name map
into a market-data guarantee.

No Yahoo probe will be added to name resolution. That alternative would add
latency and turn a transient provider failure into a change in candidate
identity before the governed gate can record the failure.

## Regression coverage

Tests will inject SEC-shaped records into the existing in-memory session cache
and will not call a live API. They will prove that:

- Globe Life resolves to `GL` instead of `GL-PD`;
- Nexera Technologies resolves to `NEXR` instead of `NEXRW`;
- reversing the duplicate feed order produces the same result;
- ordinary single-ticker exact and prefix behavior remains unchanged;
- the complete non-live suite remains green.

A read-only acceptance check may fetch the official SEC file and current Yahoo
history after tests pass, but live data will not be part of unit acceptance.

## Production recovery

Production mutation begins only after review, merge, and exact commit identity
verification.

1. Capture scheduler/service state, disable automatic entry points, install the
   established runtime manual-start barriers, and prove no worker or trigger is
   active.
2. Capture the `gen_012` manifest entry, detached commit, state paths, database
   hashes, row counts, and the complete pending-intent set. Refuse unless the
   reviewed set is exactly 32 paper intents: one BA buy and one LDOS buy in
   each of 16 cohorts, all eligible 2026-08-28, with no external order IDs and
   no external-order rows.
3. Create and verify a collision-refusing rollback archive of the complete
   `gen_012` state and relevant manifest evidence before any ledger mutation.
4. Open each cohort through `PortfolioLedger` and cancel only the captured
   pending intent IDs using one timezone-aware timestamp and the audit reason
   `operator incident recovery: retire gen_012 before primary ticker fix`.
5. Verify all 32 cancellation transitions, no remaining pending intent in any
   cohort, and no unrelated change to signals, fills, lots, marks, snapshots,
   or external orders.
6. Retire `gen_012` while keeping its detached worktree and state as evidence.
7. Update the VPS root checkout to the reviewed merge commit while preserving
   the inventoried mode-only installer change and untracked runtime `data/`.
8. Start `gen_013` from that exact commit. Require root `HEAD`, manifest commit,
   and detached generation `HEAD` parity, a clean generation worktree, and an
   empty fresh state directory.
9. Run the time-appropriate no-write preflight and restore scheduler state only
   after all checks pass. Do not invoke a current-date duplicate or historical
   daily run; the next normal timer owns the next production session.

If cancellation verification fails, keep entry points disabled and runtime
barriers installed, preserve the partial state for forensics, restore the
entire generation state from the verified archive, and verify the original
database hashes. Any other failed identity, archive, test, or fresh-state check
also leaves the scheduler disabled for investigation.

## Acceptance criteria

- The real collision regression fails before the change and passes after it.
- Duplicate issuer resolution is independent of feed order.
- `GL` and `NEXR` retain complete required history in the read-only acceptance
  check, while the 2026-08-27 evidence for `GL-PD` and `NEXRW` remains intact.
- Focused tests and the complete non-live suite pass at the reviewed commit.
- The fix is reviewed, merged, and deployed at one exact SHA.
- Exactly the reviewed 32 unsubmitted paper intents are durably cancelled; no
  unrelated ledger content changes.
- `gen_012` remains available with its worktree, run history, logs, verified
  rollback archive, and audited post-cancellation state.
- `gen_013` is the sole active, clean, empty generation at the reviewed commit.
- Scheduler entry points return to their recorded state without a duplicate or
  historical run.
