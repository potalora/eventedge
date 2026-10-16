# Regulatory graphics Sol route implementation plan

**Goal:** Use GPT-6.1 Sol high for regulatory graphics recognition and independent review, preserving Luna for other document workflows, then verify a fresh current-data generation and its next scheduled run.

**Authorization:** Pedro approved this specific model change in annotation 1 on October 16, 2026. His earlier authorization covers tested production repairs and a fresh generation. Production activation remains conditional on acceptance; no merge or push to main is permitted.

**Architecture:** Keep the existing compact content schema and bounded correction chain. Add the finite profile opt-in `autoresearch.regulation_graphics_model_policy=sol_high_v1` and pass that exact marker only through the graphics call path. Route-free requests and historical receipts retain their original interpretation.

**Tech stack:** Existing Python application, ChatGPT plan transport, pytest, GitHub Actions, and managed Ansible deployment.

## Global constraints

- Recognition and independent graphics review use `gpt-6.1-sol`, `high`, bounded role, logical max tokens 4096 and temperature 0.0. Base autoresearch remains `gpt-6-luna` high; thesis/committee routing is unchanged.
- No paid API fallback, new dependency, source truncation, relaxed fidelity validation, relabelled failure, or old-receipt migration.
- Preserve one correction, at most four native stages per target, 240 seconds per call, four target workers per document, eight global stages and the same absolute 9000-second phase deadline.
- Retain V3/V9 Luna plan/request bytes and legacy V1–V5 behavior. New Sol plans use V8/request V14 with individually attributed note fragments and the note-applicability clarification; retain exact replay of V4/V10, V5/V11 and V7/V13 attempts. Historical V6/V12 uses its retained sealed 14da implementation and is rejected by the new constructor. The explicit model policy and fixed prompt pair are bound into plan/request hashes.
- Keep old generations and pending paper intents unchanged. Deploy behavior changes only into a fresh generation after exact-candidate CI and native evidence pass.

## Shared interfaces

```python
# Pure graphics contract; omitted policy preserves the existing Luna plan.
prepare_execution_plan(assets, *, model_policy=None)
model_policy(plan)  # Strict validated finite route: None or 'sol_high_v1'.

# Private native call chain; omitted marker preserves all existing callers.
_call_llm(..., regulation_graphics_model_policy=None)
call_analysis_model(..., regulation_graphics_model_policy=None)
stream_plan_response(..., regulation_graphics_model_policy=None)
```

The Sol plan and request context add `model_policy: sol_high_v1`. Transport payloads and native provenance use `regulation_graphics_model_policy: sol_high_v1`. No local route marker is sent as an unsupported provider API field. The marker is rejected when orphaned, unknown, or incompatible with the required profile, model, effort, originals, billing mode or limits.

The first native V10 attempt failed on unstated geometry cardinality. The earlier allegation that its comma differed from a printed period was incorrect; the original prints a comma. A finite whole-contract audit also found implicit foreign-note pointer restrictions. V11 adds the existing mechanical requirements to the Sol prompts, preserving all validators and budgets. Historical V10 responses stay unchanged; their exact failed chain remains replayable. New tests cover the observed structural defects, fixed version/prompt pairing and cross-version reuse refusal before a new sealed native attempt.

## Task 1: Graphics contract and analyzer routing

Owner files: `data_sources/regulation_graphics_grid.py`, `learning/regulation_graphics_grid_runtime.py`, `learning/llm_analyzer.py`, and focused graphics contract/runtime tests.

- [x] First add tests that fail because the Sol route is unavailable: route-specific request/provenance, historical Luna stability, cross-route receipt rejection, unchanged fork/base model and complete failed-chain replay.
- [x] Add strict plan/request versions, compact-owned logical request/provenance helpers and profile selection. Pass the marker only from bound graphics stages to the isolated analyzer worker.
- [x] Bind route into invocation memo identity and preserve all limits, content validation and native failure retention.
- [x] Run focused tests; retain red/green evidence and hand the diff to an independent reviewer.

## Task 2: Transport admission

Owner files: `llm_utils.py`, `transport_worker.py`, `chatgpt_plan_transport.py`, an optional small shared route validator, and dedicated transport route tests.

- [x] First demonstrate failure for explicit Sol original-image calls and rejection for invalid tuples before credential access or SDK dispatch.
- [x] Admit only the finite marked Sol route with labelled original PNG inputs. Keep unmarked Sol attachments rejected and ordinary Luna behavior unchanged.
- [x] Carry route metadata through the private worker and returned provenance; keep it out of the actual Responses request options. Preserve exact original bytes/order, response identity checks and call deadlines.
- [x] Run focused transport tests and inspect an offline real-SDK serialization audit.

## Task 3: Profile/replay binding and CI fixture isolation

Owner files: `orchestration/regulation_inputs.py`, `orchestration/regulation_validation.py`, `data_sources/regulation_prompt_budget.py`, affected profile-forwarding callers, and abstention/profile-binding tests.

- [x] Reproduce the six compact abstention CI failures with an empty temporary plan-auth environment; identify the actual pre-model exception.
- [x] Give constructed workflow fixtures an explicit inert plan identity through the existing client boundary. Do not use real credentials or weaken production authentication. Verify all six cases and the legacy cases under isolated auth.
- [x] Reject orphan/unknown route policy and compare retained plan policy with the declared profile through assessment, reporting and replay. Add positive Sol and cross-route negative cases.
- [x] Run the focused workflow/binding suite and retain evidence for review.

## Task 4: Integrated acceptance and deployment

Root owns integration, independent final review, private profile/verifier updates and production operations.

- [x] Review all task diffs against the approved route and historical compatibility. Run meaningful integrated checks once after fixes.
- [ ] Build a new sealed native runner from the exact candidate and original retained images. Verify serialization offline, then run the dense-table target before the remaining finite representative targets. Preserve every raw result and stop on a demonstrated blocker.
- [ ] Commit and push only the existing topic branch, update PR 50 with truthful results, and require both exact-candidate CI jobs to finish successfully. Retain checkout/tree proof and counts.
- [ ] Build immutable runtime/environment artifacts bound to the new commit, prepare a new generation with current source data in an eligible session window, and deploy using the managed workflow.
- [ ] Verify native source/model evidence, all sixteen books, full required graphics/document review, thesis/committee stages, reports, read-only replay and preservation of prior state. Restore intakes only after acceptance, then verify the next actual scheduled run.

## Execution notes

The three implementation tasks own disjoint files and run concurrently; root resolves shared interfaces and owns integration. This scales the workflow to the user's request for bounded work and concurrent delegation. A model choice alone is not evidence of native accuracy. Constructed tests, actual native probes, full generation acceptance and scheduled-run verification are reported separately.

Offline integrated verification on October 16: 342 tests and 72 subtests passed, with three existing dependency deprecation warnings. The six earlier CI fixture failures were reproduced and corrected with an inert plan identity while preserving production authentication checks. Native acceptance and deployment remain pending.

The V11 mechanical-contract update passed 408 local integration tests and 79
subtests. Its actual first target completed recognition and review. A later
pixel-level reassessment corrected our independent period allegation: the
source mark is a comma. All 92 literal fields and 45 numeric values match. The
original positive runtime receipt and original erroneous reports are preserved,
with explicit correction records. A real remaining gap is typed applicability:
a note body explicitly qualifies adviser counts, but its scopes reach only the
asset-value column. Incidental citation italics are not a standalone blocker.

The V12 literal-witness experiment was prompted by that erroneous punctuation
assessment. It passed local checks but failed its actual native challenge when
the final reviewer read AUM for RAUM. No accepted V12 material exists. Its sealed
code and results remain available for exact historical replay.

The V13 successor removes that unnecessary second transcription and restores
ordinary complete-candidate review. A generic instruction requires both stages
to distinguish printed marker anchors from all quantities qualified by the note
body. It changes no validator, model, limit or correction budget. See
`2026-10-16-sol-note-applicability.md`. Every successor still needs its own exact
CI, fresh finite source acceptance, complete native run and release evidence.
Parent `409b1e5` passed both CI jobs in run `38935591882`; this is not proof for a
new candidate.

Pedro also requested a full clean run before realistic daily-source perturbation
and healing tests. Prepare their bounded plan now; execute them only against
isolated copies after complete native baseline acceptance. Normal updates,
actual repairs and correct safe refusals must be reported separately. A repair
PR does not mean the patch was deployed or the workflow recovered.

The approved Friday successor extends only the historical-document expiry by
one second to `2026-10-17T04:00:00Z`; it admits the October 16 session and no later
session. New profile and helper preparation must preserve all old generation
identities and evidence. The Thursday-session launch window has closed, and a
Friday launch requires the completed session and actual governed source
availability after the existing SIP delay.
