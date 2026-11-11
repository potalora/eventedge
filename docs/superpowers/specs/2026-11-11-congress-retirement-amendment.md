# Congress retirement with preserved gen_028 books

Pedro explicitly selected retirement after requesting that the eventual decision
be applied to the current generation. This authorizes the source-only change,
not a balance reset, new experiment funding, source recovery claim or historical
rewrite. Congress is already excluded from model context and trading.

The amendment removes Congress acquisition, its display-only declaration and
audit-continuity warning, and records `congress_source_retired_v1` through the
existing explicit retirement policy. All other configuration stays identical.
The 16 existing ledger paths, policy identities, balances, positions, pending
intents and historical outcomes remain authoritative.

## Profile proof

The new immutable profile is `generation-config-congress-retired.json` beside
the existing `generation-config.json`, which is preserved byte-for-byte. Its
top-level `source_retirement` object contains exactly `version`,
`effective_session`, `previous_config_sha256` and `previous_config_text`.
Version is `congress-audit-retirement-v1`. The text is the exact original
secret-free profile, bound by SHA256. After removing the proof object, the only
permitted differences from that original are: Congress's disabled reason becomes
`congress_source_retired_v1`; its exact dependent strategy is added to
`retired_sources`; `congress_disclosure_policy` and
`congress_audit_continuity_policy` become null. The original profile must itself
validate as the existing supported sixteen-book profile.

This narrowly verified amendment uses the original profile digest for ledger
policy identity. The new profile digest remains the actual acquisition, worker,
report and amendment identity. No arbitrary ledger identity override is allowed.

## Generation boundary

The generation manifest's live commit/configuration point to the new reviewed
code and profile. Optional `source_retirement` metadata contains exactly:
`version`, `effective_session`, `previous_commit`, `previous_config_file`,
`previous_config_sha256`, `previous_worktree_path`, `commit`, `config_file`,
`config_sha256`. The current fields must match the manifest. The old worktree is
retained at `.worktrees/gen_028-before-congress-retirement`; the current worktree
retains `.worktrees/gen_028`. State directory and generation ID never change.

One shared standard-library validator resolves the recorded commit, frozen
profile and worktree for any session. Historical attempts, accepted inputs,
reports, source exports and runtime incident identities use that session's
recorded revision. Actual runtime verification still requires clean exact Git
HEAD, worktree backlink, canonical paths and the selected frozen file hash.
Unknown fields, conflicting identities and overlapping/second amendments fail
closed. Existing manifests/profiles keep their original behavior.

The effective boundary must follow every already accepted daily session. A
pending transaction journal precedes any worktree/profile/manifest changes;
identical retry can finish only its own proven transaction. It cannot overwrite
foreign state. Deployment occurs with the existing scheduler idle/quiesced,
complete recovery material and preservation fingerprints. Rollback remains
available before any new accepted session exists.

## Metrics and incidents

A new metric epoch records the execution/configuration change. Earlier pending
outcomes remain bound to their original epoch and must continue to mature; they
must not be silently abandoned or reassigned. Historical Congress incidents and
owner preparation/terminal receipts remain retained. Retirement is an explicit
exclusion, never a successful source observation or a recovered alert.

## Acceptance

- Old profiles and accepted historical report regeneration remain unchanged.
- The amended profile accepts only the precise non-trading retirement delta.
- Congress causes zero publisher/index/PDF/provider calls, model context,
  candidates or signals; all 16 books show the exact retired exclusion.
- Populated financial books and pending intents survive byte-for-byte through
  the amendment; older outcome obligations remain processable.
- Complete/failed/unknown past evidence and owner preparations are preserved.
- Runtime and report consumers verify the session-bound identity and reject
  tampering, dirty worktrees, wrong hashes, foreign journals and repeated changes.
- Apply/retry/crash recovery are tested on an isolated state copy before live
  deployment; a second successful apply is unchanged.
- No accepted daily session is manually rerun. Verification distinguishes
  fixture transport/model evidence from actual provider/model execution.
