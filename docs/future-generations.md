# Future-generation experiment backlog

Recorded October 24, 2026. Reliability is the current priority: deterministic CI,
source-evidence integrity, and service and ledger integrity. These ideas are
parked until that work passes its acceptance checks. Relative ordering below is
a research priority signal; this record authorizes no implementation or deployment.

Keep gen_025 and its experiment intent unchanged. Any promoted behavior change
requires review and Pedro's direct, explicit authorization for a new generation.
A branch, PR, or merge is not deployment authorization. An active-generation patch
requires separate explicit authorization and must restore documented behavior,
preserve the experiment's intent, and have regression coverage.

## Evidence baseline

The offline selection work is [PR #59](https://github.com/potalora/eventedge/pull/59).
Its [report at commit 757b31cbb68ff7d9e951ab05c68f550faac307db](https://github.com/potalora/eventedge/blob/757b31cbb68ff7d9e951ab05c68f550faac307db/docs/experiments/2026-10-24-selection/README.md)
and [validation record at that commit](https://github.com/potalora/eventedge/blob/757b31cbb68ff7d9e951ab05c68f550faac307db/docs/experiments/2026-10-24-selection/VALIDATION.md)
are stable references. It is offline research and has not been deployed.

The two retained sessions have overlapping source windows. Their four horizons
repeat selections; 24 replay cases do not establish 24 independent samples.
Ticker counts measure an issuer proxy. Consolidated legal-issuer diversity and
substantive economic-event diversity have not been established.

Focused validation recorded 64 passing tests; the full non-live run recorded
2,822 passes and two clock-sensitive failures reproduced on the unchanged base.
That historical full-suite result is not green. Reliability repairs need their
own current verification and must not be inferred from the experiment's checks.

## Selection and context

1. Supply-chain prospective selection pilot — parked; first selection evaluation after reliability.
   - Hypothesis/evidence: broader selected symbol coverage may improve useful evidence coverage. Offline selection changed AAPL alone to AAPL, AMZN, BA and CAT at the same four-candidate cap and equal input-score sum. Investment value is unmeasured.
   - Smallest experiment: freeze the policy before collecting prospective sessions; run an isolated, equal-budget comparison and independently assess selected evidence and interpretations. Report overlapping windows and exclusions.
   - Promotion gate/dependencies: reliable retained inputs, causal clocks, prospective results, independently grounded relevance, deterministic selection, and measured model calls, tokens, retries, latency and cost. A separately authorized new generation is required for daily integration.

2. Government-award diversity versus materiality — parked; judgment required before a policy choice.
   - Hypothesis/evidence: broader symbol coverage may justify admitting smaller awards. Three UNH awards became UNH, VVX and IBM, with a 48.14% reduction in summed raw screening score. That score is a capped cumulative-obligation measure, not calibrated return or quality.
   - Smallest experiment: compare predeclared diversity and materiality-aware alternatives on independent awards, preserving three candidates and deterministic screening; examine the displaced awards and issuer-relative significance.
   - Promotion gate/dependencies: explicit acceptance of the coverage/materiality tradeoff, original award and ownership evidence, prospective relevance checks, and preservation of legitimate multiple awards per issuer. Do not promote one common policy across strategies from the two retained sessions.

3. Earnings acquisition eligibility and relevance — parked; separate upstream experiment.
   - Hypothesis/evidence: eligibility before source truncation may surface useful events. Vendor calendars were 314 and 303 records, each reduced to ten acquired events before listed-equity qualification; only four reached analysis. Selection changes left earnings membership unchanged.
   - Smallest experiment: inspect a bounded excluded same-day sample, then compare predeclared eligible/relevant acquisition orders within the existing ten-event acquisition and four-candidate analysis caps.
   - Promotion gate/dependencies: verified point-in-time eligibility, preserved complete calendar and exclusion reasons, causal availability, equal budgets and measured costs. Do not label all vendor records eligible opportunities or assume a higher analysis cap recovers excluded upstream evidence.

4. Across-session unchanged evidence, updates and safe reuse — parked; research design needed.
   - Hypothesis/evidence: repeated unchanged evidence may consume attention. Each policy repeated all four selected supply hypotheses and all three selected awards across the two sessions. No selected hypothesis changed its retained-content fingerprint.
   - Smallest experiment: label unchanged bytes, changed bytes and substantive new evidence separately; compare a predeclared repetition rule. Evaluate response reuse only in an isolated replay with explicit context binding.
   - Promotion gate/dependencies: bind reuse to source identity/version, issuer, direction, model/prompt/configuration, horizon, cutoff and relevant portfolio/pending context; invalidate or abstain on mismatch or missing context. Changed bytes alone do not prove new economics, and unchanged source bytes alone do not justify reusing a decision.

5. Held and pending-company relevance — parked; historical context missing.
   - Hypothesis/evidence: relevance to existing or pending exposure may improve admission. The offline experiment did not establish predecision membership snapshots, so this benefit remains unmeasured.
   - Smallest experiment: retain verified point-in-time held/pending snapshots for prospective isolated comparisons; test a predeclared relevance feature within unchanged caps.
   - Promotion gate/dependencies: event-time and identity binding, complete exclusions, no substitution of present-day positions for historical state, and independently reviewed concentration/risk effects. Keep context-dependent reuse tied to those same snapshots.

## Evidence acquisition and interpretation

6. Complete Form 4 XML integration — parked; narrow journal-only research first.
   - Hypothesis/evidence: the existing complete-window XML path can improve insider evidence. Bounded Apple qualification retained two originals, five selected sales and eleven transactions/footnotes in a prepared request; it made zero model calls. Daily acquisition does not use that path.
   - Smallest experiment: connect a small reviewed issuer registry and complete declared filing windows to an isolated journal-only profile. Retain amendments, joint owners, unsupported classes and derivatives with explicit dispositions.
   - Promotion gate/dependencies: original purchase and sale examples, amendment/class negative controls, complete discovery, footnote/plan interpretation, causal original acquisition and identity proof, reviewed model output, zero-new-call replay, then a scratch four-horizon/sixteen-book run. Do not enable the legacy fixed-eight-ticker/bulk path or infer exchange venue from transaction codes.

7. Regulatory issuer-applicability bridge — parked; attribution precedes directional use.
   - Hypothesis/evidence: complete original rule text could support issuer research if applicability is independently proven. Bounded originals exist; the current assessment contract intentionally rejects every nonempty issuer binding.
   - Smallest experiment: one rule family, one documented issuer/activity relationship and existing complete text originals; label action type, authority, publication/effective dates, dependencies and abstentions independently.
   - Promotion gate/dependencies: versioned point-in-time applicability evidence, exact originals and target identity, unknown/expired handling, fresh supported discovery window, bounded failures, grounded assessment and replay. Preserve RFI and repost meanings; model confidence or ticker mentions cannot establish applicability.

8. Regulatory graphics and broader document semantics — parked; later, separate qualification.
   - Hypothesis/evidence: visual content may contain material meaning. Retained linked-publication evidence contains 34 SEC images and prepared requests, with zero model calls and incomplete semantic assessment.
   - Smallest experiment: after text/applicability qualification, independently label a bounded set of complete original graphics and test retained recognition, review and final assessment.
   - Promotion gate/dependencies: complete original bytes/pages, graphic-to-text provenance, independent semantic labels, failure/abstention reporting and replay. Capture or request preparation alone does not qualify visual understanding or broader annual-document semantics.

9. Earnings original calls, Q&A and prior-quarter context — parked.
   - Hypothesis/evidence: prepared financial commentary may omit management answers and changes against the prior quarter; complete grounded interpretation has not been demonstrated.
   - Smallest experiment: one issuer with authorized current and prior original call materials, including Q&A; compare grounded findings under a fixed budget.
   - Promotion gate/dependencies: issuer/fiscal-period identity, original bytes and availability, explicit completeness and rights, human-grounded comparison and replay. Additional calls alone do not establish better evidence.

10. Disclosure-backed supply relationships — parked.
    - Hypothesis/evidence: issuer news and sector peers do not establish a supplier/customer graph. Existing coverage does not prove dated first-degree economic exposure.
    - Smallest experiment: one issuer and one to three first-degree relationships supported by dated original disclosures; retain relationship direction, scope and unresolved attribution.
    - Promotion gate/dependencies: point-in-time exposure evidence, exact counterparty identities, original provenance, grounded relevance and replay. Do not infer commercial relationships solely from sector similarity or a model's familiarity.

11. Litigation originals and older-case updates — parked.
    - Hypothesis/evidence: docket titles and new-case filing searches miss document meaning and later updates to existing cases. The audited 26 dockets were context, not candidates; the SEC-enforcement branch is separate.
    - Smallest experiment: one or two known dockets with already-public original complaints/orders and update-time tracking; compare labels against the complete retained originals.
    - Promotion gate/dependencies: issuer/event binding, document and update availability, explicit completeness, independent interpretation and replay. Any paid document acquisition requires separate scope and authorization.

## Deferred scope and separate cleanup

12. Congress original-filing qualification — parked; audit-only scope.
    - Hypothesis/evidence: original/row checks may improve audit confidence. House IDs matched 35/35, while six Senate filings were publisher-only; transaction extraction and historical first-observed availability remain unvalidated.
    - Smallest experiment: a bounded original-filing/row audit with independent extraction labels, only when audit confidence is the chosen objective.
    - Promotion gate/dependencies: original provenance, validated row completeness and availability evidence. Retain audit-only use; this item does not qualify a trading/model strategy.

13. Scoped state-economic pilot — parked.
    - Hypothesis/evidence: a 51-state/DC opt-in contract exists, but source/exposure qualification and a fresh release example remain necessary. A source probe and one issuer exposure do not establish a deployed strategy.
    - Smallest experiment: one fresh release, one state and one independently documented issuer exposure, in an isolated opt-in profile.
    - Promotion gate/dependencies: exact release/availability identity, qualified source and exposure, complete dispositions, grounded assessment and replay before any broader universe.

14. Optional maturity-specific futures curves — parked; consumer question and entitlement first.
    - Hypothesis/evidence: a specific commodity question may need curve structure. Continuous-price history lacks maturity-specific evidence, and no blocked candidate attributable to retired optional curve enrichment was demonstrated.
    - Smallest experiment: define the consumer question, confirm data entitlement, and retain a bounded maturity-specific chain with explicit contract identities and clocks.
    - Promotion gate/dependencies: complete comparable maturities, roll/expiry handling, provenance and measured incremental usefulness. Acquisition alone does not justify restoring committee enrichment.

15. Quantum research and complete annual-document meaning — parked; substantial design work.
    - Hypothesis/evidence: these may support future research, but quantum needs stronger exposure/research design and full annual semantics need substantially broader complete-document interpretation.
    - Smallest experiment: define one falsifiable quantum exposure question, or separately label one complete annual original including visual evidence; keep the two qualification claims distinct.
    - Promotion gate/dependencies: explicit scope, original completeness, issuer exposure where applicable, independent grounded assessment and replay. Selected pages or structural HTML cannot establish full semantic coverage; Form 4 qualification cannot qualify these consumers.

16. Conditional weather expansion — parked; one exposure question required.
    - Hypothesis/evidence: broader geography/crops may help a demonstrated issuer exposure. The ten-state/declared-crop sample is intentional; broad expansion has no established incremental benefit.
    - Smallest experiment: one documented issuer/geography exposure question and a bounded additional original dataset.
    - Promotion gate/dependencies: dated exposure, geographic/crop identity, causal observations, complete coverage/failure accounting and independently assessed relevance. Expansion remains conditional on that question.

17. Remaining award-recipient ownership cleanup — parked; separate audit-completeness task.
    - Hypothesis/evidence: Veterans Development Corporation remains unresolved; its official overview names an individual owner and matches the retained recipient DUNS. The award was below the retained admission cutoff. Another recipient leaving the rolling window was not an attribution repair.
    - Smallest experiment: prepare a dated, entity-bound private-owner/no-listed disposition for narrow review; retain exact ownership evidence and unresolved alternatives.
    - Promotion gate/dependencies: verified recipient/ownership binding and reviewed registry change. Do not invent a listed parent. This cleanup would not add a candidate under the retained cap and is separate from a new trading strategy.

## Record and promotion policy

Keep every idea here until an explicit disposition is recorded. Add results,
rejected alternatives and reasons, inconclusive findings, evidence/commit links,
remaining limitations, and the next decision. Preserve failed or abandoned
hypotheses so later generations do not repeat them without new evidence.

Before starting an experiment, record its exact intent, frozen comparison,
budget, point-in-time inputs, acceptance measures and stopping rules. Afterward,
record whether the hypothesis survived and which claims remain unmeasured.

Revisit priorities only after current reliability acceptance: deterministic CI,
retained source evidence with visible failures, and verified service/ledger
integrity. Promoting an idea then requires its own qualification, review and
Pedro's explicit new-generation authorization; this backlog changes none of those gates.
