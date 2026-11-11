# Retiring Congress from the existing books

Congress retirement stops publisher, official-index and disclosure-PDF acquisition.
It removes the display audit and its continuity warning, and declares
`congress_source_retired_v1` for `congressional_trades`. Congress already had no
model, signal or order authority. Retirement does not establish recovery or
resolve the missing historical origin evidence.

This is one explicit, source-only amendment of the supported sixteen-book
profile. All other settings stay identical. The original frozen profile remains
at `generation-config.json`; the new profile at
`generation-config-congress-retired.json` embeds its exact original bytes and
hash. Validation rejects every other configuration change. Only the ledger's
policy identity retains the original hash; new execution, acquisition and report
evidence uses the actual new profile hash and reviewed code commit.

The manifest records the effective session and both revisions. Earlier sessions
continue to use the original profile, commit and archived worktree
`.worktrees/gen_NNN-before-congress-retirement`. Sessions from the boundary use
the amended profile and active worktree. Reports, runtime receipts and monitoring
must select the recorded revision for their session. A new metrics epoch records
the change; earlier signals and pending outcomes retain their original epoch.

## Operator transaction

Use the reviewed host deployment playbook to pause the relevant scheduling
triggers, wait for workers to finish, preserve a complete recovery copy, install
the matching trusted runtime and apply the amendment as the application owner.
Do not use generic generation cutover or funding commands.

The application CLI defaults to a validating dry run:

```sh
python scripts/retire_congress.py \
  --repo /canonical/eventedge \
  --generation gen_NNN \
  --effective-session YYYY-MM-DD \
  --expected-old-commit OLD_COMMIT \
  --expected-old-profile-sha256 OLD_PROFILE_SHA256 \
  --commit REVIEWED_NEW_COMMIT
```

Apply requires the exact returned `new_profile_sha256`, `--apply` and
`--scheduler-quiesced`. The CLI also acquires the real exclusive runtime lock,
checks accepted-session and book-completion boundaries, verifies all sixteen
books, and writes a durable transaction journal before changing code or pins.
An identical retry resumes only its own verified progress. Foreign or ambiguous
state requires investigation. Keep the recovery copy until the amendment and
subsequent scheduled behavior have been verified.

Preserve the original profile, all financial databases and pending intents,
accepted source inputs, reports, failed attempts, terminal repair receipts and
owner case/preparation records. Never rerun an already accepted session to make
the historical report appear healthy. Verify retirement from explicit exclusion
and absence of Congress acquisition in the next ordinary run.
