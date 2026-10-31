# Congress source recovery assessment, October 31, 2026

The October 30 `gen_026` report records a real publisher coverage gap and a
separate repair-admission qualification. Original-document capture succeeded.
The diagnostic changes in this review improve the explanation without changing
accepted evidence, source coverage, incident history, the active generation,
or production.

## Current gap and completed fallback

The accepted October 30 publisher snapshot is revision
`0a81960b1c8140151c99df4755c92da5997782fe`, generated
`2026-10-30T11:33:54.671Z`, acquired at
`2026-10-30T22:01:48.533246+00:00`. The official House index matched 43
publisher filings and identified one missing filing, `20035727`. Publisher
freshness passed independently of that coverage gap.

EventEdge captured the missing [official House original](https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20035727.pdf)
at `2026-10-30T22:01:52.139077+00:00`: HTTP 200, 98,048 bytes, SHA256
`0d7865d4d8b7c9b6800368afeb40e196796a6d65b1fdfed4f802bef2017efa30`.
This preserved the original for audit. It did not populate publisher transaction
rows or establish independently validated extraction, so `partial_audit_gap`
and the original `invalid_response` incident correctly remained visible.
Congress remained display/audit only: signals, model context and order
authority were disabled. The report's strategy/cohort source-health failure
list was empty; its general degradation must not be attributed to Congress as
a trading block.

At `2026-10-31T04:55:31Z`, a bounded public check found the same
[publisher revision](https://huggingface.co/api/datasets/austin-starks/congressional-stock-trades/revision/main).
Both revision-bound 2026 native Parquet files matched the publisher manifest's
byte counts and SHA256 values. The filings file contained 593 rows and the
transactions file 11,847 rows; neither contained document ID `20035727`. This
confirms the accepted gap persisted upstream at that check time. A future
publisher refresh may include it, but no schedule or recovery is guaranteed.

## Why automatic repair did not run

The source incident first failed on October 29 and persisted on October 30.
The exporter requires an accepted preceding-session observation to prove the
incident origin before admitting a worker. It therefore looked for October 28
evidence under `gen_026`. That generation was created at
`2026-10-30T01:34:56.984374` and has only October 29 and October 30 accepted
source envelopes. There is no October 28 baseline.

A read-only reproduction of the exporter's exact no-follow capture read
confirmed the missing October 28 path and the resulting fixed
`missing_origin_evidence` qualification. October 29 acquisition was at
`2026-10-30T01:42:29.070758+00:00`, still October 29 in New York; it does not
introduce an acquisition/session mismatch. The refusal is the documented
first-generation evidence boundary, rather than a justified permission to skip
proof or synthesize history.

The host receipt recorded `admission_rejection=true`, `state=requires_user`,
`reason_code=evidence_unavailable`. The broker discarded the application's
precise diagnostic, and the intervention classified this generic result as
operator assessment while recommending restoration of access. Those diagnostic
defects hid the actionable distinction: automatic repair was never started
because origin evidence was absent; the provider did not reject access.

## Prepared correction and recovery boundary

Retain the three fixed application qualifications through the trusted broker
and native intervention validators. Render their specific explanations and
evidence-assessment recommendation, while preserving exact v1 option IDs,
numbering, fixed stored descriptors and all existing admission checks. Label
report-wide outcome separately from source-attributed strategy/cohort effects.

The bounded automatic fallback already captured the official original. Further
recovery requires a prospectively acquired publisher snapshot that passes the
existing reconciliation, or a separately reviewed source/extraction design.
A healthy later observation may resolve the incident through normal source
projection; retaining a PDF alone cannot. Do not fabricate a healthy October 28
input, rewrite the immutable October 29/30 observations, reset the incident, or
allow a worker to bypass the proof boundary. A source design change requires a
new generation and Pedro's separate production decision.

Focused deterministic verification covers the three classifications, generic
fallback, source-evidence refusal, fixed option tampering, legacy access cases
and report-wide/provider scope separation. No production deployment or live
repair attempt forms part of this change.
