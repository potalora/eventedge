# Conditional repairs and parked experiments

Updated November 1, 2026, America/New_York. The deployed baseline and current
reliability work are in [operating status](reliability-status.md). gen_027 remains
the observation target. No successor is approved or assigned a generation number.

Deploy a successor only for a demonstrated necessary run repair, or after current
reliability acceptance and approval of a specific experiment. The detailed
[deployment gates](reliability-status.md#gates-for-a-successor) apply to every
item below. Recording work, implementing a tool or passing CI does not promote it.

## Conditional repair work

There is no newly demonstrated production code defect assigned to a successor
in this record. Add a repair only with a failing generation/session, original
failure receipt, impact, cause confidence, smallest correction, verification and
a reason a generation change is needed. Preserve unknown causes as unknown.

The FINRA Query API change in PR #64 is already deployed in gen_027. The older
rounded-zero and source-recovery application repairs are in its ancestry. Do not
queue their deployment again because their PRs remain unmerged.

## Parked application optimization

[PR #66](https://github.com/potalora/eventedge/pull/66), commit
`e3c697e851d446720cc1b740c329f3cdf61558ec`, improves CI and application startup.
Its lazy-import change is runtime code. The isolated real-data run and full CI
passed, but no demonstrated production run failure currently requires this
change. It remains undeployed. Test/CI improvements may be reviewed separately
without automatically advancing the production generation.

## Parked research

The seventeen ideas below retain their original evidence and limitations in the
[October 24 backlog at a fixed commit](https://github.com/potalora/eventedge/blob/58414d6f512d6530956f2fc045738b40c835f02c/docs/future-generations.md).
That record concerned gen_025 and is historical. Its proposed replay checks are
diagnostics; they cannot establish current live readiness. Nothing below is
authorized for production implementation or promotion by this inventory.

| Idea | Next qualification question |
|---|---|
| Supply-chain selection diversity | Does broader prospective selection improve useful evidence under equal budgets? |
| Government-award diversity/materiality | Is wider issuer coverage worth displacing larger awards? |
| Earnings eligibility before truncation | Which excluded events were actually eligible and relevant at acquisition time? |
| Repeated evidence and safe reuse | When does new portfolio or event context invalidate an otherwise identical input? |
| Held/pending-company relevance | Can historical decision-time membership be verified before measuring benefit? |
| Complete Form 4 XML | Can complete original filings, amendments and transaction semantics be qualified in a bounded journal-only pilot? |
| Regulatory issuer applicability | Can the issuer/activity relationship be independently established before directional use? |
| Regulatory graphics/document semantics | Can complete originals and independent labels establish meaningful interpretation? |
| Earnings calls, Q&A and prior context | Does complete, authorized original material change grounded findings? |
| Disclosure-backed supply relationships | Is each dated relationship supported by original disclosures? |
| Litigation originals and case updates | Can original documents and update timing improve independently labelled relevance? |
| Congress filing audit | Can original/row completeness and first-observed availability be verified while retaining audit-only scope? |
| State-economic pilot | Is there a qualified fresh release and documented issuer exposure for a narrow opt-in comparison? |
| Maturity-specific futures curves | Is there a concrete consumer question, entitlement and complete comparable chain? |
| Quantum and complete annual-document research | Can each separate hypothesis obtain complete originals and independent exposure/semantic evidence? |
| Conditional weather expansion | What documented exposure needs geography or crops beyond the intentional sample? |
| Award-recipient ownership cleanup | Can the unresolved entity receive a verified ownership disposition without inventing a listed parent? |

Before an experiment, record its intent, frozen comparison, point-in-time inputs,
budget, acceptance measures and stopping rules. Afterwards, retain results,
rejected alternatives, failures and unmeasured claims. Passing the current
reliability gate permits choosing an experiment; each experiment still needs
qualification, review and explicit deployment authorization.
