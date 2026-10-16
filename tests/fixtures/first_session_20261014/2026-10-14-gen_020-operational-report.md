# EventEdge gen_020 — 2026-10-14

Outcome: **incomplete**. Accounting valid: true; input coverage valid: false; staging complete: true; staging valid: true.

Exact-session attempts: 1 daily, 3 preflight.

Counts are per scenario book; books share signals and source observations.

| Book | Accounting valid | Staging complete | Entry fills | Exit fills |
| --- | --- | --- | ---: | ---: |
| horizon_1y_size_100k | true | true | 0 | 0 |
| horizon_1y_size_10k | true | true | 0 | 0 |
| horizon_1y_size_50k | true | true | 0 | 0 |
| horizon_1y_size_5k | true | true | 0 | 0 |
| horizon_30d_size_100k | true | true | 0 | 0 |
| horizon_30d_size_10k | true | true | 0 | 0 |
| horizon_30d_size_50k | true | true | 0 | 0 |
| horizon_30d_size_5k | true | true | 0 | 0 |
| horizon_3m_size_100k | true | true | 0 | 0 |
| horizon_3m_size_10k | true | true | 0 | 0 |
| horizon_3m_size_50k | true | true | 0 | 0 |
| horizon_3m_size_5k | true | true | 0 | 0 |
| horizon_6m_size_100k | true | true | 0 | 0 |
| horizon_6m_size_10k | true | true | 0 | 0 |
| horizon_6m_size_50k | true | true | 0 | 0 |
| horizon_6m_size_5k | true | true | 0 | 0 |

## Daily attempt evidence

- 20261014T221733.284770Z_96b4378aad804888af0c01be7a40a81e.json: degraded; completed; return code 0.

## Preflight attempt evidence

Preflight modes are independent; a successful governed price check does not establish source recovery.

- 20261014T154510.582950Z_13eb245fb5334775b96fe5fd192f982b.json: screen (source screening); failed; completed; return code 1.
- 20261014T220328.006618Z_96895f952e0844e29d7f692f1c6414cb.json: screen (source screening); failed; completed; return code 1.
- 20261014T220331.645906Z_34f842b880854736909d339484559a3b.json: governed (price readiness); passed; completed; return code 0.

Policy exclusions: congressional_trades (display_audit_only_v1), state_economics (unsupported_state_event_proxy).

Regulatory acquisition complete: false; 4 of 13 document occurrences acquired completely; 9 failed occurrences.
Regulatory retrieval policy: verified_government_copy_v1.

Performance claims withheld because completed, consistent evidence is unavailable.

## Source scope limitations

Input coverage validity applies to these declared scopes; it is not a marketwide coverage claim.
- Congressional disclosures: display/audit only; trading signals and model context disabled. 32 House and 7 Senate filings, 844 printed transaction rows; audit status partial_audit_gap. Senate coverage, extraction accuracy and historical first availability remain unverified.
- CourtListener: docket metadata only for declared issuer and case queries, not marketwide; 1 issuers, 0 cases, 24 dockets; 0 omitted issuers. No full-document or marketwide analysis is implied.
- USAspending: verified listed targets only; 12 verified listed, 12 proven no-listed, 23 unresolved awards. Unresolved awards remain nonactionable attribution gaps, not proven exclusions.

## Source coverage failures

- generation-config-cfd010bec8effa9936df71a9fe87f72eac9403c43219163998d78d917314673d:health:1y / bounded_document_research: data_failure (analysis).
- generation-config-cfd010bec8effa9936df71a9fe87f72eac9403c43219163998d78d917314673d:health:1y / commodity_macro: data_failure (regulations).
- generation-config-cfd010bec8effa9936df71a9fe87f72eac9403c43219163998d78d917314673d:health:1y / filing_analysis: data_failure (edgar).
- generation-config-cfd010bec8effa9936df71a9fe87f72eac9403c43219163998d78d917314673d:health:1y / insider_activity: data_failure (edgar).
- generation-config-cfd010bec8effa9936df71a9fe87f72eac9403c43219163998d78d917314673d:health:1y / quantum_readiness: data_failure (edgar).
- generation-config-cfd010bec8effa9936df71a9fe87f72eac9403c43219163998d78d917314673d:health:1y / regulatory_pipeline: data_failure (regulations).
- generation-config-cfd010bec8effa9936df71a9fe87f72eac9403c43219163998d78d917314673d:health:1y / weather_ag: data_failure (drought_monitor).
- generation-config-cfd010bec8effa9936df71a9fe87f72eac9403c43219163998d78d917314673d:health:30d / bounded_document_research: data_failure (analysis).
- generation-config-cfd010bec8effa9936df71a9fe87f72eac9403c43219163998d78d917314673d:health:30d / commodity_macro: data_failure (regulations).
- generation-config-cfd010bec8effa9936df71a9fe87f72eac9403c43219163998d78d917314673d:health:30d / filing_analysis: data_failure (edgar).
- generation-config-cfd010bec8effa9936df71a9fe87f72eac9403c43219163998d78d917314673d:health:30d / insider_activity: data_failure (edgar).
- generation-config-cfd010bec8effa9936df71a9fe87f72eac9403c43219163998d78d917314673d:health:30d / quantum_readiness: data_failure (edgar).
- generation-config-cfd010bec8effa9936df71a9fe87f72eac9403c43219163998d78d917314673d:health:30d / regulatory_pipeline: data_failure (regulations).
- generation-config-cfd010bec8effa9936df71a9fe87f72eac9403c43219163998d78d917314673d:health:30d / weather_ag: data_failure (drought_monitor).
- generation-config-cfd010bec8effa9936df71a9fe87f72eac9403c43219163998d78d917314673d:health:3m / commodity_macro: data_failure (regulations).
- generation-config-cfd010bec8effa9936df71a9fe87f72eac9403c43219163998d78d917314673d:health:3m / filing_analysis: data_failure (edgar).
- generation-config-cfd010bec8effa9936df71a9fe87f72eac9403c43219163998d78d917314673d:health:3m / insider_activity: data_failure (edgar).
- generation-config-cfd010bec8effa9936df71a9fe87f72eac9403c43219163998d78d917314673d:health:3m / quantum_readiness: data_failure (edgar).
- generation-config-cfd010bec8effa9936df71a9fe87f72eac9403c43219163998d78d917314673d:health:3m / regulatory_pipeline: data_failure (regulations).
- generation-config-cfd010bec8effa9936df71a9fe87f72eac9403c43219163998d78d917314673d:health:3m / weather_ag: data_failure (drought_monitor).
- generation-config-cfd010bec8effa9936df71a9fe87f72eac9403c43219163998d78d917314673d:health:6m / bounded_document_research: data_failure (analysis).
- generation-config-cfd010bec8effa9936df71a9fe87f72eac9403c43219163998d78d917314673d:health:6m / commodity_macro: data_failure (regulations).
- generation-config-cfd010bec8effa9936df71a9fe87f72eac9403c43219163998d78d917314673d:health:6m / filing_analysis: data_failure (edgar).
- generation-config-cfd010bec8effa9936df71a9fe87f72eac9403c43219163998d78d917314673d:health:6m / insider_activity: data_failure (edgar).
- generation-config-cfd010bec8effa9936df71a9fe87f72eac9403c43219163998d78d917314673d:health:6m / quantum_readiness: data_failure (edgar).
- generation-config-cfd010bec8effa9936df71a9fe87f72eac9403c43219163998d78d917314673d:health:6m / regulatory_pipeline: data_failure (regulations).
- generation-config-cfd010bec8effa9936df71a9fe87f72eac9403c43219163998d78d917314673d:health:6m / weather_ag: data_failure (drought_monitor).

## Unresolved acquisition

- regulations: http_error; HTTP 403; attempts 1.
- congress: invalid_response; HTTP unknown; attempts unknown.
- drought_monitor: provider_error; HTTP unknown; attempts unknown.
- edgar: provider_error; HTTP unknown; attempts unknown.
- regulations: batch_failure; HTTP unknown; attempts unknown.

## Source incident policy

Policy: source_incidents_v1; assessment: available; previous-session history: missing.
Incident status is operational evidence and does not change trading permissions.
- congress: **repair_required**.
- drought_monitor: **degraded**.
- edgar: **degraded**.
- regulations: **repair_required**.
- Event opened: congress (18e456fa0b1d74784b206b841b438cbf21c23fc0921b5d0ce7f4d41693e447d6).
- Event opened: drought_monitor (40c79d4452292c4b2d0a0379fa77205f964067fe49ee0c44f3cb320d18a84a59).
- Event opened: edgar (58a2e19f91b4927711ce768a3ac19a48105d161045ac6156e2305aa69ce0c8a8).
- Event opened: regulations (a48b61786a2959b9cbcdb55e2c44c06199b3598fcee006838e92b79099fb72c1).
Repair procedure and monitor contract: docs/source-incident-policy.md.

## Preflight incidents

- 20261014T154510.582950Z_13eb245fb5334775b96fe5fd192f982b.json: screen (source screening); return code 1; 
- 20261014T220328.006618Z_96895f952e0844e29d7f692f1c6414cb.json: screen (source screening); return code 1; drought_monitor provider_error HTTP unknown; edgar provider_error HTTP unknown; regulations batch_failure HTTP 403

## Clef evidence shadow

Status: complete; assessed events: 5; ok: 5.
This optional evidence check does not affect trading, accounting validity or source coverage. Its probabilities describe evidence support, not trading returns.
Answers: insufficient: 1, supported: 4.
Session sampling cap excluded 8 additional events.

## Evidence diagnostics

- regulation_evidence_conflict
