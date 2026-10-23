# Linked publication policy integration — 2026-10-22

`linked_publications_v1` is an explicit research-only policy. No defaults,
generation profiles, disabled strategies, or production configuration are
activated by its implementation.

The full engine configuration requires this exact combination:

```json
{
  "autoresearch": {
    "regulation_publication_policy": "linked_publications_v1",
    "regulation_evidence_policy": "complete_document_text_v1",
    "regulation_retrieval_policy": "verified_government_copy_v4",
    "decision_clock_policy": "prospective_next_open_v1"
  },
  "execution": {"mode": "paper", "execution_enabled": false}
}
```

Runtime V4 retrieval without the publication policy is rejected before creating
state. This prevents a current mutable publisher rendition from entering the
legacy posted-date decision clock. Standalone low-level V4 parser/acquisition
qualification remains a separate path and grants no historical availability or
execution authority.

The engine forwards the publication policy through `EventMonitor` to the source,
retaining the returned `CoverageRecords.regulation_evidence` graph. Linked
research does not acquire regulations when every declared regulatory consumer
is disabled, retired at the strategy level, or has its regulations capability
explicitly excluded. Explicit source retirement still
prevents acquisition, and a retired regulatory source cannot simultaneously
declare the publication policy. Existing reduced operating profiles explicitly
reject the new flag. Source selection without this new policy keeps its existing
behavior.

Linked source and accepted-session stores use a finite 32 MiB / 500,000-node
codec bound for regulations and the combined daily bundle; unrelated provider
stores keep their existing bounds. A broader existing full-corpus contract
retains its existing combined-bundle capacity. The typed immutable codec retains both the
root corpus and deduplicated publication nodes. Configuration fingerprints
include the publication policy through the existing complete-config identity.

Operational reports replay the retained originals and require the actual frozen
generation configuration for linked graphs. A graph cannot declare its own
policy authority. The graph version, evidence policy, retrieval policy, and
publication policy must match that configuration. Source coverage carries the
validated graph/bundle manifests into health comparison. Recovered government
copies match the entire failed-transport inventory; shared publication nodes
count once across root bundles. Extra failures remain unresolved.

`tests/test_regulation_publication_policy.py` covers invalid configurations before
state creation, policy propagation, excluded-consumer isolation, explicit source
retirement, reduced-profile rejection, immutable graph roundtrips above the
legacy byte limit, actual archived-publication report replay without acquisition,
frozen-policy mismatches, and shared-node transport accounting. Legacy source
codec, retirement, stabilization, regulatory replay and report regressions are
also exercised. These tests make no authenticated model calls and do not
establish production deployment or full strategy qualification.
