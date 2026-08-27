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

The same response contains Alternus Clean Energy candidates `ALCED` and
`ACLEW`. Its 2026-08-14 10-Q identifies common stock as `ALCE` and `ACLEW` as
warrants, but `ALCE` is absent from `company_tickers.json`. A resolver cannot
safely invent the absent common ticker or choose lexically between the two
unrelated candidates, so this issuer must remain unresolved.

## Goals

- Resolve only an unambiguous duplicate SEC issuer name to a deterministic
  base ticker rather than SEC payload order, and fail closed for ambiguity.
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
- Do not infer a missing common-equity ticker from Yahoo availability, SEC
  filing text, or a lexical tie-break.
- Do not migrate `gen_012` ledger state into the replacement generation.

## Resolver change

`EDGARSource._ensure_name_map()` continues to expose the existing
normalized-name-to-string interface and `name_to_ticker()` retains its public
signature. It first builds a normalized-name-to-`set[str]` candidate map, then
adds only resolved names to the existing `dict[str, str]` cache.

The pure selector has the exact signature
`_select_company_ticker(tickers: set[str]) -> str | None` and applies this
strict rule:

```python
if len(tickers) == 1:
    return next(iter(tickers))
base_tickers = {
    ticker for ticker in tickers
    if any(other.startswith(ticker) for other in tickers if other != ticker)
}
return next(iter(base_tickers)) if len(base_tickers) == 1 else None
```

Thus `GL`/`GL-PD`, `NEXR`/`NEXRW`, and `GOOG`/`GOOGL` resolve to their sole
strict-prefix base ticker. Unrelated candidates such as `ALCED`/`ACLEW`, and
multi-base chains such as `A`/`AB`/`ABC`, return `None`. This intentionally
does not attempt to classify preferred shares, warrants, rights, or units from
their suffixes.

Only non-empty string `title` and `ticker` fields are stripped and normalized;
null and non-string fields are skipped before conversion. This preserves the
prior malformed-row skip intent without allowing `str(None)` or numeric values
to create bogus name mappings.

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
- Alternus resolves to `None` in both feed orders;
- null, numeric, and blank title/ticker fields do not create mappings;
- unique tickers and a unique base-extension pair resolve, while unrelated
  candidates and multi-base chains fail closed;
- ordinary single-ticker exact and prefix behavior remains unchanged;
- the focused `tests/test_litigation_strategy.py -q` suite has 18 passing
  tests and makes no live API call.

A read-only acceptance check may fetch the official SEC file and current Yahoo
history after tests pass, but live data will not be part of unit acceptance.

## Production recovery

Production mutation begins only after review, merge, and exact commit identity
verification.

1. Before disabling anything, capture `trade.timer`'s exact next trigger along
   with scheduler/service state. Then disable the three automatic entry points,
   install the established runtime manual-start barriers for all six units, and
   prove no worker or trigger is active. `trade.timer` has `Persistent=true`,
   so the captured trigger is a recovery precondition, not merely diagnostic
   data.
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
9. Run the time-appropriate no-write preflight. Restore automatic entry points
   only after all checks pass *and* the exact trigger captured in step 1 remains
   future. Do not invoke a current-date duplicate or historical daily run. If
   that trigger has elapsed, keep all six runtime barriers and all three
   automatic entry points disabled pending an explicitly controlled
   next-session restoration; never let `Persistent=true` catch up into a
   duplicate run.

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
- Scheduler entry points return to their recorded state only when the exact
  pre-disable `trade.timer` trigger remains future; otherwise all three remain
  disabled with all six runtime barriers installed, without a duplicate or
  historical run.
