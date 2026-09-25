# September 23–24 worker reporting failure

The frozen gen_015 release at `7950fec` completed daily accounting for September
23 and 24, then failed to emit the worker result. The worker logged
`invalid cohort reporting payload`; its manager recorded
`invalid daily worker result`. Neither message meant trading had not run.

Read-only copies of all 16 portfolio databases showed completed session runs,
all nine phases, staging records, and valid account snapshots for both days.
There were no session invalidations or critical gaps. September 24 included
four filled intents, and all cohorts had pending September 25 intents. Do not
replay these sessions or reset the generation to repair reporting.

## Cause and correction

The aggregate validator counted repeated references to a shared candidate
issue against a global 256-item limit. September 23 had 20 distinct issues
expanded to 280 references across scenario books. September 24 had 21 expanded
to 324. The persisted references reproduced the rejection on frozen release
code. Raising only the aggregate limit in an isolated audit process made the
same records pass; production state was not changed.

The repair caps distinct issue identities at 256 and each portfolio's carrier
list at 256. The existing 64-cohort limit bounds total input. Identity,
conflict, session, epoch, coverage, and affected-cohort checks remain intact.
This changes reporting acceptance only; it does not change trading decisions
or classify degraded sessions as clean.

## Verification

`tests/test_candidate_issue_reporting_volume.py` reproduces both observed
cardinalities through the real worker emitter and manager parser. It also
checks 256 shared distinct issues, rejects 257 distinct issues split across
carriers, and rejects an oversized individual carrier. The production incident
failed before the repair and passed afterward. Existing reporting and manager
tests cover malformed and conflicting records.

The non-live GitHub Actions workflow runs the full suite for this PR. Local
validation covers both the main-based repair branch and the exact deployed
`7950fec` release with this patch applied. Passing these tests does not prove a
future scheduled session or continuity; those remain operational acceptance
checks.

The first integrated-release run was interrupted after 920 passing tests when
the valid-score LLM boundary cases contacted SEC through the default registry.
Those tests now receive an explicit empty registry and reject DNS/socket
connections. Their 28 assertions pass offline. This is test isolation only;
production ticker validation is unchanged.

The main-based repair suite passed 1,976 tests (4 live tests deselected). The
integrated `7950fec` release plus reporting repair and test isolation passed
2,310 tests (4 live tests deselected) in 103 seconds. Critical Ruff checks and
whitespace checks passed. These results cover all final runtime changes.

## Separate provider history gap

The quarantines themselves are valid. Fresh direct Yahoo chart responses for
IBM and HUBG on September 25 returned a September 22 timestamp with null OHLCV
and adjusted close, including requests ending September 25 and `range=3mo`.
Adjacent September 21, 23, and 24 bars were valid. The same missing September 22
history session appeared in all 20 September 24 volatility-history issues.
This is independent of the reporting failure.

Current volatility estimation requires 61 consecutive raw closes. The existing
SIP recovery contract covers incoherent current-session daily bars, not missing
historical closes. Do not forward-fill, relax history checks, rewrite old
decisions, or silently extend that contract as part of this patch.

A separate source-policy change can reuse the SIP adapter to resolve missing
or null required historical closes. It needs versioned immutable history
evidence, exact window/source binding, offline replay, shared cohort coverage,
and provider-failure tests. Acceptance must exercise candidate staging and the
next session's governed holdings, since current preflight does not validate
volatility history. It also needs an explicit policy for whether validated
recovery is an expected source mode or remains degraded; continually degraded
recovery cannot satisfy the five-clean-session readiness gate. Under repository
policy, that behavior change requires a new generation.

## Deployment scope

This PR is not a deployment. Recommend a separately authorized in-place
reporting repair to gen_015, preserving the ledger, pending intents, historical
failure labels, data-source policy, and timers. No trading replay or generation
restart is needed for this reporting defect. Provider-history recovery remains
a separate unresolved change.

The readiness gate also requires the frozen worktree to match its recorded
commit exactly. An uncommitted in-place source patch would block that gate even
if reporting succeeds. Do not bypass the check or change the historical metric
epoch's identity to conceal the patch. Any operational patch must disclose that
limitation; the next reviewed source-policy release needs a clean immutable
generation and fresh observed continuity.
