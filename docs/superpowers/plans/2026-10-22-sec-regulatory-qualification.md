# SEC and regulatory evidence qualification

The approved follow-up is to repair the SDK compatibility failure, prioritize
SEC filing and regulatory-document evidence, keep Congress audit-only, and defer
futures curves and state-event strategies. This plan implements bounded,
reviewable qualification paths without enabling strategies or deploying code.

## Design and acceptance boundaries

The SDK repair is in the parent source-reliability branch: Anthropic remains on
the existing 1.x API generation, with the locked package unchanged. Existing
authentication and request-identity regressions verify compatibility.

Form 4 qualification uses a new `complete_ownership_xml_v1` policy. Retain exact
originals, all reporters, transactions, holdings, numeric literals, source paths,
footnotes, plan indicators, dates, and unsupported fields. Admission is narrower
than acquisition: one reporter, original Form 4, an explicit issuer/security-title
binding, and supported non-derivative purchase/sale values. An amendment holds
its issuer window pending resolution; joint ownership and ambiguous classes do
not produce actionable transactions. Purchase/sale coding alone does not prove
exchange execution. Complete relevant filing context reaches prospective model
request preparation without the legacy five/ten-row truncation.

Regulatory qualification adds `verified_government_copy_v4` for the actual CFPB
request for information and preserves earlier extraction policies. A separate
opt-in `linked_publications_v1` graph retains original, correction, and archived
repost identities independently. Relationship receipts must prove each edge;
all required originals, attachments, and graphics must be present. Reposts remain
historical material; current mutable publisher renditions carry their actual
acquisition times and cannot establish historical decision-time availability.
No correction is represented as the complete original notice.

Every new evidence graph is revalidated from retained bytes through consumer
request preparation and offline replay. Pure hashes/status flags are not a
substitute for recomputation. Missing or unsupported material remains explicit.
Existing `disabled_v1` filing policy and excluded strategies remain unchanged.
Structural-only annual/material-event HTML is still barred from full-document
model assessment; existing bounded page research is a separate scope.

## Implementation sequence

1. Verify, commit, and push the SDK dependency repair to PR #56; run fresh CI.
2. Create this isolated topic from fetched `origin/main`, stacked on the repair
   branch, with no changes to `main` or production.
3. Implement Form 4 evidence, bounded acquisition, consumer inputs, and a
   zero-model qualification/replay command. Use real original, amendment, and
   joint-reporter fixtures plus explicitly synthetic negative controls.
4. Implement linked regulatory publications, source acquisition, validated
   analysis inputs, and conservative availability clocks. Verify real SEC
   correction/original and EPA archived-repost cases, including tampering and
   incomplete dependencies.
5. Implement and verify CFPB v4 extraction using the actual official detail/body
   pair, strict authority/action/CFR identity, full source replay, and negative
   controls. Preserve v1-v3 behavior.
6. Independently review integration; run focused source, policy-isolation,
   identity, immutable-codec, and replay regressions plus required CI gates.
7. Record measured qualification results and remaining limits; push only the
   topic branch and open a stacked PR. Do not merge or deploy.

## Ownership

The SEC agent owns Form 4 evidence/acquisition, insider consumer inputs and
qualification command/tests. The regulatory agent owns publication bundles,
source/validator integration, regulatory consumer inputs and event-time hooks.
The primary agent owns CFPB government-copy v4 parsing/tests, integration,
independent acceptance, commits, and PR publication. Shared-file changes require
coordination. Verification uses public originals and isolated scratch state;
there are no authenticated model calls or production writes.
