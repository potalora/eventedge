# Explicit one-shot prospective native launch

This is an opt-in entrypoint on the existing native controller. Ordinary timer
dispatch, missed-session behavior and the default daily wrapper are unchanged.
It does not change a running generation, repair a failed receipt, renew document
expiry, or establish native acceptance. The implementation is local preparation;
deployment requires the reviewed commit/profile, CI and actual native evidence.

`scripts/recover_runtime.py --prospective-session YYYY-MM-DD` requires exact
`--expected-generation`, `--expected-commit`, `--expected-profile-sha256` and
`--expected-created-at` pins. These cannot be combined with status, worker or the
ordinary date mode. Creation is an exact manifest identity string; the native
manager may write it without a timezone. No UTC assumption or arbitrary
created-after-close rule is added.

The controller takes its existing controller lock, then the canonical exclusive
runtime lease. Before any launch intent or provider/model command, it requires:

- The requested XNYS session is the native latest completed reference, and the
  actual clock is strictly before its next XNYS opening. A requested weekend or
  holiday is invalid; the latest Friday on an actual weekend can be valid.
- Exactly one active generation, also the newest manifest generation, with the
  pinned clean worktree/profile and exact creation identity. The actual root
  checkout that supplies the controller/wrapper must have the same commit and
  no tracked changes.
- A frozen paper-only `prospective_next_open_v1` profile. A decision-bearing
  bounded-document registry requires `verified_scoped_claims_next_open_v2`.
- An unused generation state containing only its regular frozen configuration,
  empty run history, no pending lifecycle and no journal incident for that
  generation in any session. A full journal is refused rather than pruned.

The final real-clock/freshness check, durable intent and wrapper dispatch remain
under the same runtime lease. The existing journal receives a strictly validated
finite `prospective_once_v1` marker and `prospective-native-launch-v1` intent,
including the exact creation identity and actual start time. The record is
flushed before dispatch. Other generation records are preserved. A repeated
explicit request always refuses once any incident exists, including a failed or
uncertain start. A normal timer never resumes an interrupted one-shot; it marks
it operator-required without calling a recovery or daily launcher. Ordinary
unmarked records retain their existing recovery behavior.

The bounded native launcher passes the selected reference as literal arguments
to the maintained `daily_trading.sh`. Its hidden validation mode performs only
identity/configuration/calendar/journal reads and lock checks; it constructs no
book, provider or model. It verifies that the inherited descriptor already
holds the canonical exclusive lease, not merely that an unlocked descriptor can
acquire one, and requires the exact matching running one-shot intent. The wrapper
checks before loading `.env`, rejects changes to guarded lease/Python/generation
environment fields, and checks again before any provider command. The chosen
reference propagates to screen preflight, governed preflight, daily execution
and the exit-trap operational report. It does not replace the real clock or
mutable source vintage. Governed preflight already supports a completed prior
reference after its close/SIP availability delay; no new market-data gate is
needed. Its failure still blocks the daily command.

Normal completion uses the native operational report classification, retaining
approved degraded outcomes as such. A reporting/start exception or incomplete
result is operator-required, never a retry permission. Fatal interruption keeps
the durable running intent. Completion/reporting after the eligible opening is
not relabeled invalid solely by wall time: the report must validate the original
publication/cutoff evidence. New dispatch and wrapper admission still refuse at
or after the opening, and existing live staging enforces its own opening bound.
Terminal controller classification does not replace full source/model,
16-book accounting/staging, committee, replay and consumer acceptance.

The retained gen22 native failure exposed a separate controller precedence
defect: valid accounting plus source failures was classified accepted even with
12 of 16 books unstaged and an explicitly failed daily outcome. The minimized
fixture retains original report SHA
`fa6d5e982fe2739457c342ddf30500c7e4eac618b8268dc8efa0e04969b1e078`.
Explicit failed outcomes now require operator review before any source-degraded
classification; accepted degraded outcomes require completed staging. An
incomplete checkpoint without an explicitly failed attempt stays nonterminal
for the existing persisted-only P0 recovery path. This preserves legitimate
completed EDGAR degradation without disguising failed model/committee/staging
work or starting a repair loop for the failed native attempt. Original native
receipts remain failed; this change applies only to the next reviewed code.

Configured-profile recovery also reads the actual engine ledger policy ID for
committee/staging checkpoints. The horizon-qualified health policy ID belongs
to strategy-health records and is not interchangeable with it. A real 16-book
offline interruption test seals committee decisions, blocks all further
providers/models, and requires staging recovery under the exact configured
ledger policy without any new inference or acquisition.

Focused offline tests exercise pre-open/calendar boundaries, Friday-on-weekend,
exact identity and fresh-state refusal, real registered Git worktree and runtime
lease checks, root-code drift, any prior incident, durable interruption/no rerun,
strict new journal corruption, unchanged legacy history, dotenv lease overrides,
and actual shell argument/report propagation with local subprocess stubs. They
invoke no live source, model, service or broker. Existing scheduler and shell
tests must continue passing. These constructed results do not claim a new native
generation run or deployment.

Local verification:
`.venv/bin/python -B -m pytest -q -p no:cacheprovider tests/test_prospective_native_launch.py tests/test_shell_preflight_contract.py tests/test_runtime_recovery.py tests/test_runtime_lock.py`
passed **104 tests in 25.28 seconds**, with one existing websockets deprecation
warning. `bash -n scripts/daily_trading.sh` and `git diff --check` passed.
