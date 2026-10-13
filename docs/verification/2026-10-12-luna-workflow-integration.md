# Bounded document workflow and operational simulation

PDF/image recognition now has an opt-in caller in the daily research pipeline.
Engineering acceptance uses finite operational checks; five actual live sessions
are not a prerequisite. The optional real-clock continuity experiment remains
paused. This work is on [PR #50](https://github.com/potalora/eventedge/pull/50),
not merged or deployed.

## Research behavior

`autoresearch.bounded_document_research_policy = bounded_document_research_v1`
adds a strategy and a separate `bounded_documents` source. Its
explicit registry contains at most four retained originals, exact acquisition
timestamps, original/render hashes, selected page identities and finite
extraction questions. Intake performs no discovery, rendering or network fetch.
Registry coverage means only that the configured inputs were loaded; it does
not certify complete-document extraction. The default research mode remains
journal-only. The opt-in next-generation
[`document-informed-paper.json`](../../configs/document-informed-paper.json)
also enables `bounded_document_decision_policy = verified_scoped_claims_v1`.
Combine that template with a private retained registry and answer-free decision
scopes, then freeze the complete configuration through the ordinary generation
configuration interface. Missing or invalid scopes cannot become trading
candidates. A complete credential-free local autonomous profile with the scanned
original and source/issuer/date/scope policy is defined privately. Its registry
contains no review answers, expected values or extracted claims. It has not
started a generation; its finite decision window is explicit and expires.

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

Decision eligibility binds the particular claims and their required context:
issuer name/CIK and ticker, original/render
and acquisition identities, declared purpose/economic date, required pages and
claim IDs, and a bounded effective window of at most 31 days. The review binds
exact printed values/precision, units/scales, full row/column associations and
citations, plus coverage of the declared context. Uncertainty, material missing
context, expired scope, conflicting claims or mismatched identity excludes the
dependent decision. Perfect transcription of unneeded pages is not required.

New documents do not require somebody to enter the extracted answers. The
runtime producer is `LLMAnalyzer.analyze_bounded_document_evidence`: after
primary recognition, `prepare_automatic_verification` prepares a separate blind
Luna/high attachment request using only the original pages/probes and declared
source/issuer/date/context policy. It receives neither the primary transcription
and answers nor test gold. `build_automatic_review` validates that response,
source/provenance bindings, context sufficiency and conservative typed-claim
agreement, then produces a sealed `automatic_review_v1` receipt. Eligibility is
rechecked before scoped Sol analysis. Verification completion time is read after
the blind response, so a call crossing the session cutoff cannot be backdated.

This is a separate request to the same model family, not independent model
diversity or guaranteed visual accuracy. Correlated model risk is explicit.
Wrong identities, conflicting readings, uncertainty, missing material context,
bad provenance or exhausted deadlines exclude the dependent decision. There is
one additional bounded verification request per supplied document, within the
existing model/per-call/resource limits, with no new retry loop.

Previously configured human or independent visual reviews remain an optional
compatible input path. Their authorship is a configuration trust boundary, not
something the pure verifier can authenticate. The retained scan review was a
separate retrospective root comparison, not human approval. Manual receipts
alone do not prove the unattended path. All runtime/generated review and call
receipts are retained; source/review answers never become Luna recognition gold.

Generic count-unit handling accepts `Shares` and `Number of Shares`. A full
descriptive count label can reduce to shares/securities only when its suffix
is independently bound in the matched row/column context. Share and security
counts, unknown units, currencies, scales and per-unit rates remain distinct.
No numeric fuzzy matching, per-document answer exception or model retry is used.
Both historical frozen native verdicts remain failed.

Only verified claims and declared context reach the decision-eligible Sol prompt.
Sol must identify the claims used and assess context sufficiency; unsupported
directional claims fail, while neutral/no-edge assessments abstain. A validated
directional candidate then follows ordinary admission, committee, sizing/risk,
current price/account/time safeguards and governed PAPER staging/execution. No
document or model response bypasses those gates. Original parser failures,
unassessed pages and disabled bulk-filing research remain visible and are not
cleared wholesale by this bounded capability.

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
| New supplied document, no runtime expected claim values | Runtime producer creates a verification receipt, then ordinary Sol/committee/risk stages and fills a PAPER entry | Answer-free registry and blind prompts checked externally; all five simulated sessions and sixteen books completed entry/fill/carry/stop/replay. Model responses and prices were constructed; test gold was external to runtime configuration. |
| Blind check finds insufficient context | No Sol call or dependent entry; unrelated work and protective exits continue | Paired answer-free case passed across all sixteen books, including next-session/replay and unrelated earnings/protective-stop controls. |
| Independently verified document claim, directional scoped thesis | Change ordinary decision inputs; committee/risk stage a PAPER entry, next-session fill, carry, stop and replay | Five simulated October 12–16 sessions passed across all sixteen books. Review hash, recognition receipt, verified claim, thesis claim IDs, signal, intent and fill lineage were checked. |
| Uncertain extraction, expired scope, issuer mismatch or conflicting reviewed value | Suppress the dependent entry while unrelated work and protective exits continue | Four paired public cases passed; unrelated earnings entered/filled and independently accepted protective stops executed across all sixteen books. |
| Actual retained native scan response with reviewed claims | Same causal pipeline using unchanged real source/Luna bytes, without forcing the actual native thesis to trade | Private positive five-session case passed; separate conflicting-value case suppressed only the dependent entry. Directional Sol, committee responses and prices were constructed; the real native Sol neutral response is preserved unchanged. |
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

The new causal decision tests use constructed Microsoft prices publicly and
constructed Cadre prices in the retained-response replay. They do not relabel
KTOS history as another issuer's prices. Their directional responses prove
decision causality and guard behavior, not that the actual historical pledge
provides a current trading edge. The earlier journal-only checkpoint at
`b4f66a4` is preserved and is superseded for feature completion by this explicit
decision-eligible policy.

The causal harness uses five recent observations for unrelated core-market,
VIX and daily FRED research context. Monthly and weekly economic histories,
candidate, benchmark and protective-position volatility requests retain their
full histories and ordinary validation. This avoids
repeatedly hashing a large unrelated fixture during each sixteen-book replay;
the wider source-reliability harness retains its full provider histories.

## Verification and remaining limits

- Historical/lifecycle, retry, recovery and hold selection: 56 passed.
- Final affected intake, recognition, eligibility, workflow and report selection:
  223 passed, including 66 eligibility cases.
- Two additional paper-only preflight cases passed before any model call;
  the ordinary positive lifecycle passed again after that guard was added.
- Public positive/four-negative causal selection: five passed; the positive
  passed again after making the same harness reusable for retained inputs.
- Actual retained native response: positive causal case passed, then the paired
  conflicting-value case passed, with no new native calls.
- Unattended producer: answer-free positive lifecycle and blind-context-failure
  cases passed. The final positive rerun retained raw verification responses and
  checked that runtime registry bytes stayed answer-free and unchanged.
- Unattended analyzer normal/after-cutoff cases passed; late verification never
  reached Sol. Scoped Sol numeric-fact substitution controls also passed.
- Earlier journal-only source interruption/recovery replay selection: ten passed.
  Counts overlap and are not additive.
- Required non-live CI runs both baseline and pinned OpenBB environments on the
  exact topic-branch commit. Its final status is recorded on PR #50.
  The pinned OpenBB job has a 40-minute aggregate limit after a slower hosted
  runner reached 95% at the former 30-minute cutoff. Baseline remains at 30
  minutes; test selection, runner resources and source/model/runtime budgets
  are unchanged.

Generated originals, native captures, preparations, visual review, supervision
and JUnit receipts remain private under
`data/forward-readiness/luna-workflow-integration-20261012/`. No credentials or
generated evidence are committed. Public tests and the sanitized historical
fixture are reproducible without those private documents.

The automatic verifier is implemented and exercised through constructed model
boundaries; no additional native blind-verification call or broad accuracy
campaign was run. Existing real native recognition and its neutral Sol result
remain separate preserved evidence. The frozen exact-string quality criterion
did not pass. Broader unseen
document accuracy, complete transcription, whole-filing recovery and immutable
provider revisions remain unverified. Deployment, server-specific authentication,
scheduler/alert delivery and investment performance remain separate decisions.
Any later authorized rollout uses a new generation and verifies scheduler
freshness, bounded completion, native auth/model success, accounting/prices,
staging, optional-source recovery, replay and resource headroom. Report meaningful
failures/recovery; do not create automatic retry storms or relax guards.
