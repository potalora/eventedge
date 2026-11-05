# November 4 Congress incident and source-repair follow-up

Review candidate only. No production release, projection reconciliation, receipt reset, trading rerun or notification was performed. gen_027 remains at `dba04fbaf7bee827f5c61d5d8431677c3d65cb10`.

## Findings

The Congress source acquired a valid snapshot with one publisher coverage gap. Automatic code repair never started: a pre-existing intervention-projection failure blocked admission, consumed the request into a terminal operator-assessment receipt, and left an older empty intervention proposal readable as healthy. A separate real-evidence export check also exposed missing frozen-profile propagation; repairing projection alone would not make this generation's automatic repair admission succeed.

### Source evidence and present diagnostic

The accepted November 3 source envelope has complete Congress audit coverage. The accepted November 4 envelope has a full `audit_snapshot`, `acquisition_complete=true`, `integrity_valid=true`, `house_ids_reconciled=false`, and the explicit error `Congress audit House filing reconciliation gap [invalid_response]`. This was not an unidentified HTTP outage or malformed snapshot. One House filing, `20035763`, was absent from the publisher's rows. The supplemental acquisition captured its official 68,651-byte PDF without substituting publisher rows. Congress remained display/audit only, with model context and trading signals disabled.

Both accepted source-envelope payload digests were independently recomputed. November 4's exact JSON + NUL + Markdown hash is `a4301bc1acae0436736d0559cb008bb52995ca62da4781a5a90a041c0fb0491c`, matching the proposal. Accounting and staging remained valid; all sixteen paper books completed. Historical evidence is preserved.

A single bounded source-only diagnostic on November 5, 15:03:57–15:04:01 UTC, used the unchanged deployed adapter in a clean environment without credentials. For the same October 5–November 4 window it validated a complete audit snapshot: 40 House filings, 8 Senate filings, 896 printed rows, and no missing/additional House IDs. It made eight physical HTTP requests across five provider operations, within the existing 90-second deadline. No model calls occurred.

The publisher revision was `6603df15a17afe56fffa2deddd9b6ab0781112bf`, generated and source-checked November 5 at 11:54:02.925 UTC. Filing `20035763` now appears once, with one extracted transaction row. Its publisher raw-PDF SHA256 equals the official original retained on November 4: `5b9d92eca165bc2ae8943024429a18a4d02ec3b22106bceadfeb65cb35b81fe3`. This establishes matching document bytes, not independent semantic verification of extraction.

This new diagnostic is not a replacement for November 4 accepted inputs and does not mark the production incident recovered. A subsequent ordinary, accepted session must establish that. No Congress adapter change is justified by these findings.

### Actual intake and projection timeline

| UTC time, November 4 | Retained evidence |
|---|---|
| 23:12:58 | Canonical report-directory activity starts the source-repair service. |
| 23:14:06 | The root-owned intervention proposal is published as available with no cases. |
| 23:14:15 | Canonical JSON/Markdown report files are replaced; their final bytes still match the published proposal hash. |
| 23:15:07 | Projection exits 2 with a generic evidence-unavailable console; the broker persists a reconciliation-required marker. |
| 23:33:34 | The Congress request reaches the broker and is rejected at global preflight, before admission or worker start. |

The authoritative repair incident is `4ab8e8c71340f8ccb808be9368c4a89812c5df0e9653a98dc7fb86025b6b307e`. Its receipt is `requires_user`, `reason=preflight_reconciliation`, `reason_code=evidence_unavailable`, `admission_rejection=true`. There is no job directory or publication for this incident. The report's source-alert ID is a different identity; transport queue acknowledgement is not a worker-admission receipt.

The persistent marker makes subsequent automatic projections fail immediately. The broker deliberately drains a failed preflight's level-triggered queue into terminal receipts to prevent a restart loop. It never retries a terminal incident. The existing `reconcile-projection` command clears only the projection marker after successful re-publication; it does not revive this consumed incident or launch a worker.

### Why the monitor looked healthy

The intervention proposal still matches the report bytes, but predates the new terminal broker outcome. The native consumer validates report identity/hash and case structure without checking whether a newer outcome is missing from that proposal. Trading runtime heartbeat health is a separate check and does not describe repair-broker readiness.

A read-only comparison against the actual production files on November 5 at 15:17 UTC returned:

| Reader | Cases | Errors |
|---|---:|---|
| Existing native reader | 0 | none |
| Candidate native reader | 0 | `proposal_outcome_mismatch` |

Only `read_proposals` ran. No poll, delivery, reminder or receipt-writing function was invoked.

### Initiating projection failure: reproduced mechanism, historical uncertainty

The old CLI collapses several exceptions into one generic message, so the precise November 4 initiating exception cannot be recovered. The timestamps are consistent with concurrent report replacement, but do not prove it caused that invocation's failure.

A deterministic disposable reproduction using exact application commit `dba04fb` and the installed broker replaces report JSON with identical bytes immediately before final `_Capture.verify()`. It raises `ValueError: evidence changed during capture` after publishing an available empty sidecar. The installed broker records the same generic exit-2 console and persistent lockout. A later terminal outcome remains invisible to the native reader until stable reprojection. This proves a real defect and the monitoring blind spot independently of the historical hypothesis.

Report publication and projection can overlap: their runtime guards are shared readers, and report JSON/Markdown use separate atomic replacements. The candidate preserves `_Capture` identity and byte checks; it does not accept a mixed or changed capture.

### Additional admission blocker found with actual evidence

Using the installed validated owner-trust contract environment, read-only evidence export rejected the November 3 healthy baseline with `source projection unavailable`. A bounded diagnostic traced the swallowed exception to `filing_research_policy.declared_scope`: `invalid_filing_research_policy`.

Normal reports load the frozen generation profile and pass it to `_sources`. The repair exporter omitted that context, causing the valid supported-profile filing-research declaration to be checked against an empty configuration. This is an independently reproduced application defect, not missing source history or a credential problem. The initial operator diagnostic without the installed contract environment also failed Git owner trust; that setup failure is retained separately and is not attributed to the production incident.

## Candidate changes and limits

1. **Projection-only retry:** distinguish evidence replacement from malformed or unsafe evidence, propagate that classification through projection handlers, and return exit 75 only for the `interventions` operation. The installed broker supplies fresh subprocesses and its existing maximum 120-second budget. Requests, export, worker execution, uncertain completion and terminal receipts gain no retries.
2. **Frozen-profile export:** capture the canonical frozen profile with its exact manifest/report hash, validate it through the existing configuration/profile contracts, retain it in the private bundle, and pass it into source-history projection. Preserve legacy exports without a profile and reject missing, mismatched or unsafe declared profiles.
3. **Native outcome consistency:** derive current incident identities from report evidence and compare root-owned bounded sanitized outcomes with their projected cases. Missing/contradictory cases or changed outcomes produce fixed readiness errors; the reader does not fabricate or send cards.

The native guard is not complete broker-health telemetry. A global lockout with no current open incident can still require a separate operator check. These read/recheck guards are not a new atomic report/outcome transaction. A future reviewed design can add outcome-bound publication readiness and dedicated artifact serialization. Do not silently upgrade the shared trading runtime lock.

Preflight deferral is also a separate policy decision. Changing an unstarted rejection into a resumable pending request needs bounded storage/backoff, fresh admission validation and durable proof that no worker started. This candidate preserves all existing receipts and one-attempt rules.

## Review boundary

Final local application regressions passed: **148 passed, 1 skipped** across capture-retry, frozen-profile export, source-repair, intervention, enrichment and endurance suites. The earlier scheduled-enrichment integration run passed both variants before the exporter-only addition. Independent reviews found no blocking issues in either application fix.

The installed broker, redirected entirely to disposable state, recovered a single report replacement on two fresh subprocesses without a marker. Continuous replacement exhausted a shortened four-second test budget and retained its marker; invalid identity failed on one attempt without retry. These are controlled regression runs, not live repair-worker executions.

The corrected exporter subsequently passed against the actual accepted November 3–4 envelopes in 26.842 seconds. An independent check verified all ten exported files, their hashes and mode `0400`, including the exact frozen profile. The private bundle contains no databases. No request was submitted and no worker was started by this diagnostic.

The [native companion PR](https://github.com/potalora/private-agent-context/pull/33) passed **124 EventEdge consumer tests**, including 18 new freshness regressions. Independent review checked 1,040 producer/consumer outcome-classification combinations with no disagreement. Before/after hashing found **83 protected production files unchanged**, including the manifest, accepted source envelopes, financial databases/journals, canonical report/proposal, broker receipts and projection blocker.

After code review and verification, the next production decision is a managed release of the compatible source-repair runtime and native reader, followed by guarded projection reconciliation and verification. It is not a trading-source substitution or a replay of November 4. Production remains unchanged until Pedro explicitly authorizes that step. The ordinary monitors remain enabled, and the next accepted session must independently establish source recovery.

Private raw source bodies, broker receipts and diagnostic artifacts remain outside Git. The reproducible regression tests use simulated evidence; the live source diagnostic and read-only production checks are distinguished above.
