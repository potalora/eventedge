# P1 Portfolio Policy Integration Amendment

**Status:** Approved by Pedro on 2026-08-01 after the merged P0 foundation was audited.

This amendment is binding wherever it conflicts with
`2026-07-31-portfolio-policy.md`. It preserves the approved P1 behavior while
integrating it with the immutable execution ledger and metric-epoch contracts
that now exist on `main`.

## Scope Boundary

- This branch produces a code candidate only. Do not create `gen_005`, deploy
  to Hermes, mutate production state, or change timers/services.
- P1 remains paper-only, API-free and LLM-free inside policy evaluation, with
  automated learning disabled and cohorts evaluated independently.
- Preserve the exact P0 `SignalRecord` and `OrderIntent` schemas. Do not add
  `order_eligible` to `SignalRecord` or policy metadata to `OrderIntent`.

## Attribution and Persistence

- `Candidate` and `TradeRecommendation` carry `event_key`,
  `source_event_keys`, `strategy_tags`, `risk_tags`, and `journal_only`.
  `source_event_keys` are vendor/native disclosure event keys; they are not P0
  ledger signal IDs.
- `OrderIntent.signal_ids` continues to contain only materialized P0
  `SignalRecord.id` values and must keep the existing strict provenance checks.
- The committee derives recommendation attribution from the contributing
  candidates after both the LLM and rule-based ranking paths. It must not trust
  pre-populated recommendation attribution as authoritative.
- Persist P1 signal eligibility and intent policy provenance in append-only
  companion ledger tables keyed by the immutable P0 signal/intent IDs. Writes
  are insert-once/idempotent and conflicts fail closed. Include policy version,
  normalized attribution, decision/rejection reason codes, and the canonical
  bound-context digest needed for restart-safe validation.
- Candidate provenance must survive `screen_and_enrich()`, signal
  materialization, staging, ledger reopen, and due-intent execution.

## Policy Configuration and Epochs

- Use one authoritative factory to derive `PortfolioPolicyConfig` from the
  cohort's `PortfolioSizeProfile`; the exact per-size values in Task 1 govern.
  Callers must not silently fall back to $100k-like defaults.
- The normalized portfolio-policy document and version are part of the metric
  epoch semantic policy and the bound execution-session context. A behavioral
  policy change therefore creates an epoch boundary.
- Policy-enabled execution fails closed when a required risk context, bound
  policy document, or provenance record is missing or mismatched.

## Risk Context and Execution Ordering

- Immutable policy types must be deeply immutable. Store normalized tuples (or
  an equivalently immutable representation), not mutable dictionaries inside a
  frozen dataclass. Add mutation-resistance coverage.
- Build portfolio context from authoritative ledger projections: current lots,
  the latest persisted raw marks, lot attribution, and every outstanding entry
  intent. Do not use only due intents and do not use the screening cache as the
  book's mark source.
- Add bounded ledger projection APIs needed to reconstruct that context without
  scanning unbounded history.
- The common committee post-pass runs exactly once after either ranking path.
  A policy-enabled call without context fails closed.
- Due intents execute before same-session screening. Fill-time policy
  revalidation belongs in `SessionExecutor`, immediately before fills, not in
  the removed `run_paper_trade_phase()` flow.
- At staging, persist the immutable intent attribution and policy version. At
  each execution session, bind the normalized policy configuration and the
  canonical ledger-derived context payload/digest. A restart of that session
  reuses the same bound payload; a later session may bind refreshed persisted
  marks and book state. Any digest/config mismatch fails closed.

## Congressional Events

- Prefer a normalized native disclosure ID; otherwise use a canonicalized
  disclosure URL plus stable disclosure facts. Deduplicate the same disclosure
  across vendors before consumed-event enforcement. Vendor name alone must not
  create a newly consumable event.
- A timezone-aware publication timestamp is eligible only when it is at or
  before the decision cutoff. A date-only publication becomes eligible on the
  next XNYS trading session, preventing same-day look-ahead.
- Keep component disclosure keys in `source_event_keys`; reserve `signal_ids`
  for P0 ledger records.
- Journal-only sales are persisted and reportable but never stage an order.
  Consumed-event enforcement operates on the canonical event identity.

## Tests, Documentation, and Operations

- Replace the obsolete `object.__new__(CohortOrchestrator)` test fixture with a
  fully constructed fixture or a narrow helper test that reaches the intended
  behavior.
- Rename/split cap tests so strategy, risk-tag, congressional, and position-risk
  constraints are each proved directly.
- Add companion-table reopen, idempotency/conflict, restart, context-digest,
  publication-cutoff, cross-vendor dedupe, and epoch-boundary coverage.
- `tests/test_dashboard.py` does not exist; update the closest existing
  dashboard/capability tests instead. Existing truthful covered-call copy is a
  refinement baseline, not a required RED failure.
- Add operator/reporting acceptance coverage for policy trims/rejections,
  journal-only sales, consumed-event blocks, policy version, and metric epoch.
  Daily reports remain Codex-generated directly from ledger/state; do not invoke
  `scripts/generate_daily_report.py`.

## Delivery

- Each task uses a fresh implementer and an independent task reviewer. Project
  instructions prohibit subagent commits, so the root agent owns task commits
  after verifying each subagent's work and evidence.
- Finish with focused tests, the complete offline suite, performance/cost
  evidence, a whole-branch review, a topic-branch push, and a ready PR. Do not
  merge or deploy from the agent session.
