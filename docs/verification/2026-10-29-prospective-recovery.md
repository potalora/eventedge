# Prospective recovery after the rounded-zero staging failure

Prepared October 29, 2026. **Not deployed.** This procedure starts a new paper
experiment after explicit operator authorization. It does not complete the
failed October 27 session or reset its operator incident.

## Verified starting state

The read-only October 29, 21:28 UTC audit found sole active generation `gen_025`
at `e33cc9eed38aa668dbde5f5e2844b7fbfb9391ac`, with clean tracked code and frozen
profile SHA256
`ecf94154a063fc1af5b160f51bb2a61acf8384f7a2571f47d41377d91a5b82a9`.
Accounting was valid in all sixteen books through October 27; twelve books
staged, while the four $10k books retained the
[rounded-zero failure](2026-10-28-staging-rounding-incident.md). The last fully
completed workflow was October 26.

The ledgers contain 54 open long paper lots and 75 pending intents. No October
28-or-later execution effects, daily-attempt receipts or canonical reports were
found in the inspected locations. The October 27 terminal incident still reads
`requires_user / checkpoint_invalid`; the October 28 incident is unstarted.
The audit compared 58 durable generation files and the separate metrics files
without changing them. It was not one atomic snapshot across all books.

October 27's eligible-open deadline has passed. Existing replay loads the
already-frozen bad decisions, and there is no public terminal-reconciliation
operation. An upstream policy correction alone cannot release this incident.
Do not edit a decision, ledger, report, journal disposition or acknowledgement
to make it appear recovered.

## Candidate and qualification

Branch `codex/rounded-zero-recovery-release` preserves the exact deployed
ancestry and applies the policy correction from PR 61. Its only application
runtime difference from the deployed commit is the final rounded-weight guard
in `portfolio_policy.py`. Model routes, budgets, source policy, precision,
thresholds and the sixteen-book profile are unchanged. The deployment receipt
must pin the final full commit, not the branch name.

The lifecycle rehearsal uses the real generation manager, Git worktrees,
identity checks, controller, locks, journal and portfolio ledgers. It verifies
retirement with retained worktree/state, rejection of a stale old worker,
sixteen independent fresh books, one current-session launch, repeat safety,
and no pre-window launch or historical backfill. Worker responses and completion
reports are constructed fixtures. This proves lifecycle behavior, not native
source/model availability or canonical financial completion.

Release replay also exposed obsolete award proofs in the unchanged October
15/16 private captures. The deployed validator already rejects those proofs.
The harness now binds their exact original hashes, crosswalk versions, retained
proofs, diagnostic recomputation hashes and expected rejection. It never replaces
the original proof or treats the obsolete proof as currently valid. The initial
failed replay report remains retained privately.

A separate unchanged October 26 gen_025 capture must pass the current award
validator and all thirteen represented source contracts. Omitting or corrupting
that capture fails full-scope acceptance. Accepted normalized captures do not
establish native HTTP wire contracts; historical source failures remain failures.

The broad suite also reproduced nineteen failures in the existing synthetic
FINRA fixture after the real calendar advanced beyond October 27. Its September
archives are valid for the fixture's October 22 acquisition context, while the
current production calendar correctly requires the October 15 settlement. The
shared fixture now pins only its default acquisition clock and preserves explicit
pipeline/publication clocks. The isolated comparison across all fixture importers
and calendar controls changed from 19 failed / 135 passed / 1 optional-SDK skip
to 154 passed / 1 optional-SDK skip. Production freshness rules are unchanged.

Before deployment, require the final candidate's public and private historical
replays, scheduled workflow acceptance, remaining non-live suite, baseline and
pinned optional-SDK CI, focused rounding/lifecycle regressions, and independent
review. Preserve any failures and the exact successful report fingerprints.
The matching immutable repair archive must pass its reviewed offline builder,
complete manifest verification, exact committed-source comparison and relocated
baseline. Build preparation must not install the archive or alter managed units.

## Authorized transition

This section is a deployment plan, not permission to execute it. Production is
`/home/hermes/trading_agents`; its authoritative state is `data`, and the private
development shadow is never a deployment target.

1. Obtain Pedro's direct deployment instruction for the exact candidate and a
   **fresh sixteen-book paper reset**. Recheck the production commit, active
   generation, frozen profile, pending intents and actual session boundary.
   Any intervening run or changed identity requires a new review of that state.
2. Through the reviewed Ansible workflow, hold the managed trading, preflight
   and repair-intake triggers. Let existing writers finish naturally and verify
   that no repair worker or queued request can launch. Do not kill a trading
   process or clear a failure. Record original unit states.
3. Under the native controller/runtime locks, take and verify a private recovery
   backup containing the manifest, journals, generation configuration, all
   ledgers with their WAL files, frozen evidence, reports and Git state. Record
   file hashes and the old lots/intent census. Keep credentials out of Git and
   development handoffs.
4. Stage the exact topic commit and unchanged production profile through the
   reviewed deployment workflow. Keep the accepted application interpreter and
   its dependencies unchanged. Install only the matching verified repair archive
   via Ansible; keep its prior verified bundle for rollback. Use the current
   verified native-tool paths, not an operator's ephemeral package-cache path.
5. With triggers still held, use the native lifecycle to retire `gen_025` with
   `--keep-worktree`, then start the next unused generation from the pinned
   candidate and the byte-identical production profile. Assert that it is the
   sole active generation. Retain gen_025's original code, journal incidents,
   54 lots, 75 pending intents and all evidence. Recheck these counts against
   the pre-transition snapshot rather than assuming they have not changed.
6. Verify new state contains only its frozen profile before ordinary execution.
   Fresh books receive their normal initial cash. Do not transfer old cash,
   holdings, pending intents, reports or rehearsal data. This neither liquidates
   gen_025 nor recovers its pending orders.
7. Restore the ordinary managed schedule only after the identity, preservation
   and repair-package checks pass. The real controller admits the current
   exchange session at or after 18:00 America/New_York; before that window it
   must remain idle. Use the actual activation date and exchange calendar.
   Never force October 27/28/29, fake the clock or backfill a missed session.
8. Verify the actual managed invocation, all sixteen books' accounting/staging,
   governed source evidence, model census and canonical completion. Reproduce
   canonical reports offline and compare bytes; recheck historical preservation.
   A clean controller projection before the first run is only an idle check.
   Native first-session acceptance and five consecutive qualifying sessions
   remain unestablished until their respective evidence exists.

## Stop and rollback boundaries

Keep triggers held if any deployment identity, backup, source, model, financial
or preservation gate fails. Preserve the failure evidence. Once the new generation
has begun, do not replay an uncertain attempt or overwrite its manifest/ledger
with a pre-transition copy. Pause the new generation through the native lifecycle
and retain both generations while reviewing the boundary.

Retirement has no public inverse operation. Do not edit the manifest to unretire
gen_025 or automatically resume its unsafe pending work. Restoring a prior
launcher or repair package does not make the frozen October 27 sample valid.
The safe rollback outcome can therefore be stopped research with intact evidence.
The monitor continues to report actual state; silencing it is not recovery.
