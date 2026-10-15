# October 14 first-session incident and release requirements

The first scheduled gen_020 run completed accounting and staging for all sixteen
paper books, with zero fills. It did not establish complete input or decision
evidence. The original report, accepted source bundle, native model responses,
worker failures and accounting state are retained unchanged.

Pedro authorized stopping the repair loop, repairing source and evidence failures,
deploying tested changes and checking the next actual scheduled run. Repair intake
and its looping broker were stopped before investigation. The deployment must use
a new generation because the recognition prompts and regulatory retrieval policy
affect output. A passing PR or isolated rehearsal does not establish deployment or
scheduled-session acceptance.

## Proven causes

| Failure | Evidence | Required repair or limitation |
| --- | --- | --- |
| Repair service repeatedly reactivated | Projection ran before queue consumption. A failed projection left a JSON request satisfying `PathExistsGlob`; `ExecStopPost` repeated projection. | Persist the projection failure, consume rejected admissions durably, require explicit reconciliation, bound path activation even when storage cannot be reconciled. |
| Root service could not inspect generation Git identity | The installed trusted application helper raised `RuntimeLockInvalid`; its Git cause was dubious ownership of the Hermes-owned checkout. | Test the real account boundary and scope Git owner trust to the validated canonical repository in trusted contract subprocesses. No global Git exception. |
| Projection startup exceeded its per-call allowance | The wrapper imposed five seconds inside a nominal thirty-second projection budget. | One bounded total budget, bounded private diagnostics, and process-group cleanup. A busy lock is distinct from a failed identity or helper. |
| Drought source returned no states | HTTP 200, all ten states missing; the October 7–14 request excluded the latest published October 6 observation. | Use the actual Tuesday-observation/Thursday-publication calendar and retain typed acquisition failures. |
| Regulatory report contradicted retained evidence | Replaying the full accepted regulation graph and eight health records failed at the empty SEC company map in deployed c52. The already-tested report fix accepts the same incomplete acquisition honestly. | Deploy the report fix; do not rewrite the old report or claim that missing rules were acquired. |
| Two official EPA copies failed parsing | Retained complete publisher bodies contained numeric superscripts, an initial page marker, or no decorative separator. | Support those finite representations while checking full identity, page sequence and content boundaries. |
| Three document assessments failed | Native replay reproduced a row-label colon mismatch and two citations that were not literal substrings of their own transcription. Fixed four-attempt prompt campaigns passed 2/4, 3/4 and 3/4; remaining failures included inconsistent labels/units, interleaved table headers and a citation omitting an intervening cell. | Preserve validator refusals, specify linear table transcription and exact labels, and stop asking the model to duplicate a short page's transcription byte for byte. Test fresh native output before release. |
| Document attempt diagnostics lost specificity | The analyzer converted fixed document-decision rejection codes to generic `invalid_bounded_document`, although the original exception remained available to the engine. | Preserve only allowlisted fixed failure codes in the audit attempt; never copy arbitrary exception text. |

The proposed `verified_government_copy_v2` policy additionally binds the retained
FDA notice and two FTC notices. The FTC document without a printed catalogue
docket requires the official Federal Register document cross-reference, including
matching document, docket, agency, publication date, pages, CFR and RIN. It retains
and replays that original response. Five bounded live publisher probes (the two
EPA repairs and three v2 notices) passed acquisition and identity validation.
These checks cover those five originals, not future catalogue completeness.

Recognition request-v3 explicitly permits exactly `{"scope":"page"}` for a
probe citation when its own page transcription is at most 4,096 characters.
The validator expands that selector to the unchanged transcription before the
existing literal, number, unit, row, column and independent agreement checks.
Full-page literal citations were already permitted. Raw responses and their
hashes remain retained; validated citations remain text. Old request-v2 reviews
replay with their original blind prompt bytes, and failed responses are never
converted into v3 evidence. This removes a redundant copying failure; it does
not prove visual accuracy, semantic association or whole-document completeness.

The first fixed v3 campaign passed recognition in all four samples and blind
verification in all three returned responses, but only two complete analyses
passed. One neutral Sol response used numbered list markers that violated the
existing numeric-claim rule; its prompt now states that rule explicitly. The
other blind call returned no response. Its exact historical transport cause is
unrecoverable because the analyzer discarded sanitized transport metadata.
Future failed document stages retain only revalidated fixed error codes and
bounded numeric status/capacity fields. This adds diagnostics, not retry authority.

EDGAR bulk research remains deliberately disabled by the approved rollback. Its
coverage loss must remain visible. A document containing unavailable graphics or
an inaccessible, unbound PDF remains unusable even if its title and some text are
available. HTTP 403 on a publisher download is not evidence that ChatGPT sign-in
expired. Separate provider access, unsupported representation, validation failure
and application defects in operational conclusions.

## Why the previous checks missed this

The previous verification was broad but did not join the boundaries that failed.
Source fixtures placed weekly observations inside seven days. Document fixtures
mostly supplied schema-perfect model answers. Native sign-in probes demonstrated
model access without exercising an actual incident against the Hermes-owned Git
checkout. Broker tests did not run a real systemd path with a pending request and
a failing pre-claim projection. An idle, healthy controller was therefore weaker
evidence than the first scheduled financial session.

The midday acceptance additions also remained on the candidate branch; they were
not part of deployed c52. Verification records must identify candidate and deployed
commits separately. I accepted the combination of component, synthetic integration
and connectivity checks as stronger operational evidence than it was. Stating that
the first daily run was unproven did not replace testing the known host and failure
boundaries before enabling unattended repair.

## Required evidence before and after this release

- The named scheduled acceptance gate includes the retained native refusals,
  publisher representations, publication-calendar boundary and genuine report
  projection. Only external responses and acquisition time are replaced where
  needed; production validators stay authoritative.
- An isolated Linux/systemd drill uses the actual worker, account ownership,
  `PathExistsGlob`, `PathChanged` and stop callback. Failed projection, timeout,
  saturation, explicit reconciliation and later valid intake must have observable,
  bounded outcomes. The drill must not start a model repair or touch production.
- Real retained-document checks use EventEdge's own existing SIWC profile, the
  unchanged original and page image, and fresh answer-free model calls. Record
  every result; do not retry until success or substitute failed evidence.
- Verify the exact committed application and infrastructure revisions, frozen
  profile, installed runtime, native schedules and stopped/active boundaries.
  Preserve a rollback snapshot and all previous generation history.
- Accept the next ordinary scheduled session only after reading its actual
  attempt, sixteen-book accounting/staging, evidence/report consistency, source
  coverage, model outcomes and repair receipts. A fresh heartbeat, zero exit code
  or zero fills alone is insufficient. Document residual source limitations
  explicitly; never relabel them as successful coverage.

The accompanying repair-specific evidence documents and private release receipts
record test results and exact deployment identity. This incident note does not
itself assert that deployment or the next scheduled session has passed.
