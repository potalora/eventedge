# Bounded document workflow and operational simulation

PDF/image recognition now has an opt-in caller in the daily research pipeline.
Engineering acceptance uses finite operational checks; five actual live sessions
are not a prerequisite. The optional real-clock continuity experiment remains
paused. This work is on [PR #50](https://github.com/potalora/eventedge/pull/50),
not merged or deployed.

## Research behavior

`autoresearch.bounded_document_research_policy = bounded_document_research_v1`
adds a journal-only strategy and a separate `bounded_documents` source. Its
explicit registry contains at most four retained originals, exact acquisition
timestamps, original/render hashes, selected page identities and finite
extraction questions. Intake performs no discovery, rendering or network fetch.
Registry coverage means only that the configured inputs were loaded; it does
not certify complete-document extraction. A credential-free local profile with
one PDF and one scan is defined privately; it has not started a generation.

The analyzer sends attachments to native Luna/high, validates strict response
identity, page/probe coverage, printed number precision, explicit units and
literal citations, then sends the recognized selected-page receipt to native
Sol/high for scoped analysis. Numeric strings, including scientific/abbreviated
forms, require units. Malformed or uncertain recognition stops before Sol.
Document calls bypass text-only response reuse. Both model stages retain request
identity, response hashes and provenance within existing deadlines.

The pipeline retains complete original/input bytes once in its immutable daily
source bundle and compact identity in candidate checkpoints. The existing
committee, sixteen cohort ledgers, staging, accounting and replay remain in use.
Skipped analysis still carries valid event identity; an exhausted deadline
cannot attribute a preceding document's attempt to the next candidate. Health
coverage uses the exact configured strategy set.

Every document observation remains journal-only with `trading_authority=false`.
Structural checks do not independently prove transcription or row association.
Original parser failures, unassessed pages, and the separate disabled bulk EDGAR
research disposition survive successful selected-page analysis.

## Native scan result, including failed criteria

The new sample is page 18 of an 18-page scanned pledge exhibit. Its economic date
is December 16, 2021; the original was acquired October 10, 2026. It is not a
current-holdings assertion. The complete original SHA256 is
`e54577e3a617c5029eb7b8b9f55ae9acd9c86f056eccf38e0caacb8ddbc4059c`;
the selected render SHA256 is
`cd8cde17010889be8c8a244e975bf6d788e463a3a9dbe291e0b2d3d5d8b21ee5`.
Three independent reference claims were visually checked and frozen before
inference. Expected values were never included in a model request.

| Attempt | Observed result | Disposition |
| --- | --- | --- |
| First, previously unsampled scan | Correct three numbers and citations, but all three unit fields were null. The initial structural validator admitted it to Sol. | Frozen independent acceptance failed. Added test-first unit rejection; preserved the failure. |
| One corrective call, same source and independent reference | Correct values, explicit printed unit labels, valid selected-page runtime receipt, then a neutral scoped Sol observation. | Frozen exact-string acceptance still failed: `Number of Shares` differed from `Shares`, and full securities labels differed from `securities`. Preserved this failure too. |
| Separate review of the corrective capture | Original scan re-inspected: 4,000,000 Cadre Holdings Inc. shares; totals A and C each 15,866,703 securities, with correct same-class/all-class association. | Three selected claims visually verified after inference. This is retrospective review, not a prospective or unseen-sample pass. |

The corrective preparation was frozen under SHA256
`d76dca7cb02769789c3c411310f369cb7682fe536cb0f3418f68228cc898db1f`.
There were exactly two new native Luna calls, each with a native scoped Sol call,
and no new source downloads or bulk acquisition. Each had a 110-second model
budget, 120-second outer watchdog, 2 GiB sampled aggregate RSS, twelve processes,
64 KiB console and three-second cleanup bounds. Both supervisors retained
`worker_failed` because the independent quality checker failed, unchanged code
and zero remaining PIDs. Returned model aliases matched; revisions were unpinned.
No further native call was made. Prior accepted PDF/PNG pilot controls are reused
without alteration; see the [earlier pilot record](2026-10-12-luna-document-pilot.md).

## Finite scenario results

| Scenario | Expected | Observed evidence |
| --- | --- | --- |
| Valid document, ordinary committee/accounting | Complete all sixteen books without granting document order authority | Four horizon observations stayed journal-only; no document signal entered an order intent. |
| Uncertain/malformed recognition, shared research hold or refused dispatch | Exclude affected analysis while safely completing accounting/staging | Focused composed regressions passed; uncertainty/malformed responses made zero thesis calls and unaffected earnings work continued. |
| Interruption after four staging commits, then resume/replay | Reuse accepted inputs and avoid duplicate effects | Passed with exact retained source bytes and actual corrective Luna response replayed only against frozen request hashes. All ledger dumps and accepted bytes stayed unchanged on replay. |
| Two optional CFTC outages, then recovery | Preserve accepted history and complete usable degraded runs | Accelerated October 12–14 sequence completed all sixteen books each session; commodity health recovered on the third session. Each replay made zero new provider/model calls and preserved ledgers. |
| Timeout/disconnect, 429 Retry-After and temporary 5xx | Bound actual request attempts/deadlines and recover where allowed | Existing request-policy, CFTC transport and composed source-fault checks passed. |
| Stale/empty/partial/malformed/duplicate/out-of-order source data; bad prices | Explicit exclusion or safe hold, protective exits preserved | Existing adapter, source pipeline and research-hold checks retained; mandatory price/accounting/time/risk guards remain blocking. |
| Historical decision, next-open fill, carry, protective stop, flat | Real execution/accounting phases across sixteen books without future rows | Retained KTOS OHLC sequence: October 2 decision, October 5 fill, October 6 carry, October 7 stop, October 8 flat. Each session replay preserved fills, snapshots and accepted evidence. |

The composed native-response replay uses actual retained original/render and Luna
response bytes. Other provider boundaries and regime-aware Sol responses are
constructed fixtures; they are not additional native executions. The historical
sequence uses Yahoo data acquired October 10, retrospectively. Catalyst/model
responses, clocks, 5% stop, benchmark, action absence, prior volatility history and
SIP trade count are explicitly constructed. These separate sequences establish
operational behavior, not contemporaneous PIT evidence, executable fills, alpha
or a historical model backtest.

## Verification and remaining limits

- Historical/lifecycle, retry, recovery and hold selection: 56 passed.
- Initial composed related selection: 168 passed, before the final unit fix.
- Missing-unit correction plus original composed selection: 61 passed; final
  scientific/abbreviated-unit correction: 55 focused tests passed, including the
  two added cases. Counts overlap and are not additive.
- Final retained-response replay plus changed composed fixture: 10 passed,
  including two private retained-data cases and eight public workflow cases.
- Required non-live CI runs both baseline and pinned OpenBB environments on the
  exact topic-branch commit. Its final status is recorded on PR #50.

Generated originals, native captures, preparations, visual review, supervision
and JUnit receipts remain private under
`data/forward-readiness/luna-workflow-integration-20261012/`. No credentials or
generated evidence are committed. Public tests and the sanitized historical
fixture are reproducible without those private documents.

The frozen native exact-string quality criterion did not pass. Broader unseen
document accuracy, complete transcription, whole-filing recovery and immutable
provider revisions remain unverified. Deployment, server-specific authentication,
scheduler/alert delivery and investment performance remain separate decisions.
Any later authorized rollout uses a new generation and verifies scheduler
freshness, bounded completion, native auth/model success, accounting/prices,
staging, optional-source recovery, replay and resource headroom. Report meaningful
failures/recovery; do not create automatic retry storms or relax guards.
