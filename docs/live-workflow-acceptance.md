# Real workflow acceptance

This standalone tool runs a separate pinned candidate with real sources and live
models. It never changes production trading code/state or deploys anything. Its
normal canonical model authentication may refresh the shared auth directory.
Captures and logs are private and remain outside Git.

The tool checkout can be based on older main: it imports no application modules.
Every application child imports the disposable candidate clone. The clone copies
Git objects without hardlinks/alternates, removes remotes, disables hooks and
retains the exact supplied candidate SHA. A fresh owned virtualenv exposes only
an explicitly fingerprinted dependency site and a copied minimal hook package.
Dependency `.pth`/sitecustomize startup files are not executed. The trusted
supervisor mounts those dependencies read-only; never use a synthetic acceptance
venv, date shim, SDK fixture, copied auth grant or paid fallback.

## Prepare

Use the accepted Python 3.12 interpreter and site-packages directory; the default
Python must not silently stand in for that runtime. Fingerprinting hashes all
installed non-bytecode file contents, including native libraries and metadata.
It may take time for a large dependency tree. Source and clone code must be clean;
ignored untracked application code is also refused. The destination must not exist.
The disposable clone locally excludes the candidate's generated
`data/operational/eventedge-runtime.lock`; other new operational files and
untracked application code still fail clean-candidate validation.

```sh
python scripts/run_live_workflow_acceptance.py fingerprint \
  --dependency-site /accepted/venv/lib/python3.12/site-packages

python scripts/run_live_workflow_acceptance.py prepare \
  --candidate-root /reviewed/candidate \
  --candidate-commit FULL_40_CHARACTER_SHA \
  --profile-file /private/accepted-generation-config.json \
  --profile-sha256 EXACT_STABILIZATION_V3_FILE_SHA256 \
  --dependency-site /accepted/venv/lib/python3.12/site-packages \
  --dependency-sha256 VERIFIED_FINGERPRINT \
  --python /absolute/resolved/python3.12 --python-version 3.12 \
  --session 2026-10-30 --destination /private/new-campaign
```

Use the actual accepted frozen profile when it differs from the repository
template, including its declared EDGAR contact. `--profile-file` selects those
exact hash-pinned bytes; omitting it selects the candidate's stabilization-v3
template. Environment overrides cannot change a frozen contact.

Only five existing state/cache/result path fields change in the frozen profile.
The substitution audit records every change; the original profile bytes remain
intact. Sources, thresholds, strategies, sixteen-book topology, native model
routes and efforts stay unchanged. The canonical
`/home/hermes/.config/eventedge/plan-auth.json` path remains unchanged. Paper
execution, disabled alerts with empty channels and disabled decision shadow are
mandatory. Creation calls the candidate's actual
`GenerationManager.start_generation()` and retains its native timestamp,
manifest, generation worktree and frozen config. Source acquisition uses the
verified deployment's explicit 900-second wait; native model defaults must remain
2400 seconds per phase and 120 seconds per call. No ambient overrides are inherited.

Optional `--observer-root/--observer-commit` and
`--native-root/--native-commit` pin additional clean reviewed checkouts. These inputs
identify external composition code; they do not claim it has run.

## External boundary and single native attempt

Run as UID 2001 inside the separately reviewed OS sandbox. The supervisor must
make production state/home, Docker/SSH/notification sockets and credentials
inaccessible, mount only the private campaign and verified dependencies, enforce
the enabled transport families' broker-write guards, deny notification delivery,
and contain/kill all descendants with a bounded
cgroup. Suggested native bounds are 3500 seconds, 4 GiB/no swap and 64 tasks.
The tighter campaign cap is a test constraint, not production-equivalent proof.
The live boundary trusts the reviewed frozen candidate, disabled paper execution
and verified Requests/urllib/HTTPX/HTTPX2/curl transport guards. FINRA and
USAspending legitimately use POST. No external TLS gateway or hostile-code
containment is claimed; an arbitrary direct-socket implementation is outside the
live guard coverage. A Python hook and this tool's process-group cleanup do not
replace OS filesystem/process containment. Offline replay additionally requires
kernel network denial for every descendant/native transport.

Mount the **whole canonical auth parent directory read-write at the same path**,
with its real shared inodes/lock and atomic refresh lifecycle. No grant copies,
file-only bind or alternate lock domain. This permits ordinary authentication
writes; production trading state remains inaccessible.

The trusted supervisor supplies a reviewed boundary receipt with exact fields:

```json
{
  "schema": "live-workflow-external-boundary-v1",
  "root": "/private/new-campaign",
  "uid": 2001,
  "private_mounts_verified": true,
  "production_state_inaccessible": true,
  "dependencies_readonly": true,
  "cgroup_limits_enforced": true,
  "descendant_cleanup_enforced": true,
  "transport_write_guards_verified": true,
  "canonical_auth_parent_shared_rw": true,
  "notifications_denied": true
}
```

A receipt is an explicit supervisor assertion, not self-verifying evidence that
these kernel controls exist. Root must inspect the actual boundary before live
execution. The supervisor should own the read-only receipt so the candidate UID
cannot replace the assertion. Provider/model request caps, quota and storage
bounds also belong to that supervisor; the profile's dollar budget is not a hard
provider spending cap.

Source credentials are read only when explicitly selected by name. The supervisor
can populate its private process environment and use repeated `--credential-env`
arguments; values never appear in argv, the contract or a retained dotenv:

```sh
python scripts/run_live_workflow_acceptance.py native \
  --root /private/new-campaign --session 2026-10-30 \
  --boundary-receipt /supervisor/live-boundary.json \
  --credential-env ALPACA_API_KEY --credential-env ALPACA_SECRET_KEY \
  --credential-env FRED_API_KEY --credential-env FINNHUB_API_KEY \
  --credential-env COURTLISTENER_TOKEN
```

The complete allowlist is ALPACA_API_KEY, ALPACA_SECRET_KEY, FRED_API_KEY,
FINNHUB_API_KEY, FMP_API_KEY, COURTLISTENER_TOKEN, NOAA_CDO_TOKEN and
USDA_NASS_API_KEY. An optional `--credentials-json` accepts only these names from
a UID-owned, single-link, private 0600 regular JSON file (prefer a private tmpfs or
FD-backed path). It never sources shell text or copies a production dotenv.
The clone's `.env` remains empty. Explicit source credential values and their
URL-encoded forms are scrubbed from application logging formatters and captured
stdout before retained log writes; process receipts label that sanitized
representation. Native auth grants are never read by the tool for redaction. Other ambient secrets, paid keys, notification
tokens, proxy/loader/Python overrides and synthetic acceptance flags are discarded.

The native operation first claims its one attempt durably, validates the real
latest-completed-session window, and observes an ordinary idle controller tick on
a non-session day. It then calls the unchanged `recover_runtime.py` prospective
CLI with exact generation, commit, frozen-profile and creation-time pins. All
native calendar, fresh-state, inherited lease, intent and preflight guards remain
active. Failed attempts stay claimed: no automatic rerun or expanded retry loop.
Recordings distinguish screen/governed preflight, daily, controller and report
processes; identical requests use tape occurrence identities. Real `date`, system
clock, source adapters and configured native model clients remain active.

## Evidence and accepted-state replay

```sh
python scripts/run_live_workflow_acceptance.py summary --root /private/new-campaign

python scripts/run_live_workflow_acceptance.py accepted-replay \
  --root /private/new-campaign --session 2026-10-30 \
  --boundary-receipt /supervisor/offline-boundary.json
```

The offline receipt uses the same shared fields and additionally requires
`"kernel_network_denied": true`. A separate 120-second worker takes the real
candidate runtime lease, makes a coherent SQLite-backup state copy (including
committed WAL rows), and invokes the real cohort replay over accepted state.
Provider, model, Requests/urllib/HTTPX/HTTPX2/curl, socket and subsequent subprocess
attempts are prohibited and counted. All sixteen books must return `replayed`.
Every original database table's typed logical contents and schema, all accepted
inputs, replay-copy files and original native artifacts must remain identical.
The native inventory includes frozen-profile result/source/data-cache paths,
`data/runtime_recovery`, every candidate `data/logs` file (including daily and
run-attempt logs), the generation manifest and canonical reports. Runner-owned
receipts and console logs remain outside the candidate root.
No table exemptions or reconstructed success are permitted.

`native-report.json` preserves the candidate's independent accounting, staging,
source and accepted-input validators. `report-reproduction.json` compares the
canonical shell JSON and Markdown to a read-only rebuild; missing/divergent output
is retained as a failed comparison. `summary` separately lists all sixteen books,
accounting/staging validity, source completeness and recorded model role/model/
effort calls. Interrupted reservations and unobserved roles remain visible.
Natural empty sources, no candidates, abstention and zero intents/fills are valid
observations, never reasons to inject success or force a model call.
The `native` command exits successfully only with a `clean` report outcome and
when all sixteen books, accounting,
staging completion and validity, evidence completeness, source completeness, both
canonical report comparisons and zero unfinished tape reservations pass. A
completed command with incomplete or degraded acceptance evidence exits nonzero
while retaining its observations. Earlier recorded provider failures and absent
model roles do not independently fail this gate. `summary` remains informational.

## Controlled edge cases

After the native attempt has finished, invoke this command inside the reviewed
offline sandbox with kernel network denial:

```sh
python scripts/run_live_workflow_acceptance.py edges \
  --root /private/new-campaign \
  --boundary-receipt /supervisor/offline-boundary.json
```

The command calls the edge driver against the retained native tape and writes
`edge-cases/matrix.json`. It never changes the copied native recording hooks or
reuses a model response for a changed request. Each private derivative retains
its parent digest, exact request, phase and original duplicate occurrence.
The driver copies at most one body per case, limited to 1 MiB after mutation.
An oversized derivative is recorded as unexercised without writing its body or
launching a worker. Missing eligible captures also remain unexercised.

The seventeen rows distinguish transport checks, JSON decoding, the exact
candidate FINRA adapter, native model-helper replay and tape-binding checks.
FINRA semantic mutations require the unmodified parent to pass that adapter
first. Model malformed-output checks identify the candidate analyzer JSON decoder
explicitly and set `role_specific_parser_coverage: false`; they do not establish
committee or strategy-specific parsing behavior. Every row retains
`whole_workflow_coverage: false`. These cases do not perform cold workflow replay,
calendar/startup recovery, duplicate daily execution or subsequent-session fills.
The command exits nonzero when any row fails; a zero exit can still include
unexercised rows, so inspect the matrix counts.

`summary` accepts only a terminal matrix bound to this campaign's current immutable
tape, exact case roster, phase/request/occurrence identities and consistent counts.
It reports pass/fail/unexercised counts by exercised level and lists remaining
unexercised or failed cases. A plan alone, invalid/stale matrix, or an entirely
unexercised matrix retains `controlled_mutations_not_executed`. A valid matrix
with attempted cases replaces that generic gap with any specific failed and
unexercised gaps. No matrix clears the whole-workflow or cold-replay coverage limit.

The initial tool reports explicit gaps for observer sidecars/native whole-poll
composition, cold acquisition replay, controlled mutations, actual timer firing
and subsequent-session fills. Root may run the exact candidate's
`scripts/source_repair.py interventions --repo-root ...`, then the pinned native
consumer with a private StateStore and fake send callback, and retain separate
receipts. No real notifications may be sent. Accepted-state replay is **not** a
cold source/model replay, a new native observation, a five-session qualification
or evidence of future fills. Transport replay/mutations must retain exact parent
identity and fail on missing or changed requests; never reuse a captured model
answer for a changed prompt.

Unit tests use dummy repositories, private SQLite WAL data and controlled local
subprocesses. They read no credentials and contact no live providers or production.
