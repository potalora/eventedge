# Luna/high analysis and USDA repair

The September 25 scheduled gen_015 run completed in 392.91 seconds. All 16
portfolios had valid completed sessions, all nine phases, account snapshots,
and staging records. The reporting fix emitted a clean worker result. There
were no candidate-input issues or critical gaps that day; prior failure
evidence remains intact. Fresh IBM and HUBG Yahoo history probes now contain
the previously missing September 22 observations.

The USDA warning exposed malformed QuickStats parameters and separate fallback
and weekly-comparison defects. See [the USDA audit](2026-09-26-usda-audit.md)
for the reproduced failures and repair. A clean run envelope did not establish
that the source adapter or the crop comparison was correct.

## Model change

New generations select `gpt-6-luna` with `llm_effort=high` for event analysis
and the portfolio committee. Both callers now route this model to OpenAI
Responses with the original system/user prompts and `store=false`. Sampling
parameters are omitted. The maximum output budget is at least 16,384 tokens
because it includes reasoning; incomplete, failed, refused, or empty responses
are rejected before the existing JSON parser and failure fallback. Requests
use a 120-second read timeout, 10-second connect timeout, and no SDK retries;
the committee's existing rate-limit retry policy remains unchanged.

Explicit legacy Claude models retain their prior request behavior. Unrelated
cache/research model settings are unchanged. Metric behavior identity now
includes reasoning effort and the effective committee override, in addition
to the main analysis model and frozen source commit.

References: [Luna model contract](https://developers.openai.com/api/docs/models/gpt-6-luna)
and [migration guidance](https://developers.openai.com/api/docs/guides/latest-model).

## Acceptance

Final local non-live suite: **2,358 passed, 4 live tests deselected** in 101.31
seconds, with one existing websockets deprecation warning. Independent review
passed 224 focused tests across USDA source, gate, prompt, Luna callers, and
metric identity. Critical Ruff checks and `git diff --check` passed.

Regression tests exercise the actual analyzer and committee callers, provider
selection, high reasoning, omitted sampling fields, response failures, legacy
override behavior, and semantic identity. The deployment host's credential was
verified with a small completed Responses request reporting model `gpt-6-luna`
and effort `high`; no trading state was involved.

The user requested discarding old generations. Cutover retires them from
active use while retaining their historical files and manifests as archived
evidence. The new generation starts with empty state and a clean frozen
worktree. It does not inherit old positions, pending intents, or reported
performance, and no historical session is replayed. Live trading acceptance
still requires a completed scheduled session using the new model.
