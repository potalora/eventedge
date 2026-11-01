# Real workflow acceptance

This standalone tool runs a separate pinned candidate with real sources and live
models. It never changes production trading code/state or deploys anything. Its
normal canonical model authentication may refresh the shared auth directory.
Captures and logs are private and remain outside Git.

Acceptance evidence comes from the complete native workflow using real providers,
real source data and the configured live models. Each claim stays bound to the
observed candidate, frozen profile, runtime and session. Offline CI checks,
recorded replay and injected faults provide separate code or diagnostic evidence;
they do not establish live readiness. The November 1 clarification makes this the
completion policy: additional captures solely for replay and planned offline fault
campaigns are stopped. Preserve their existing artifacts without treating missing
replay coverage as a live acceptance gap.

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
  --git-sha256 ROOT_VERIFIED_GIT_SHA256 \
  --uname-sha256 ROOT_VERIFIED_UNAME_SHA256 \
  --session 2026-10-30 --destination /private/new-campaign
```

The trusted supervisor must verify `/usr/bin/git` and `/usr/bin/uname` are regular,
root-owned executables that are not writable by group or others before entering
the user namespace, then supply their digests. Inside the namespace the tool
requires those exact digests.
An unmapped owner is accepted only when it equals the kernel's overflow UID and
the file is on a read-only mount. The supervisor also verifies the mapped file's
device and inode against the host file; an inside ownership number alone does
not establish host ownership.

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

Optional `--baseline-destination` captures the complete prepared campaign before acquisition,
including its Git/worktree/runtime identities, frozen configuration and every
state/cache/result path. SQLite files use coherent backups including committed WAL
contents. The separate private baseline retains byte/mode inventories, a content
receipt and source-stability evidence; each scenario restores a fresh copy at the
original absolute path inside its supervisor namespace. No accepted tables are
deleted to invent a pristine state. Keep the receipt hash outside the baseline.
This preservation option is not a requirement for live workflow acceptance.

## External boundary and single native attempt

Run as UID 2001 inside the separately reviewed OS sandbox. The supervisor must
make production state/home, Docker/SSH/notification sockets and credentials
inaccessible, mount only the private campaign and verified dependencies, enforce
the enabled transport families' broker-write guards, deny notification delivery,
and contain/kill all descendants with a bounded
cgroup. The verified supervisor uses 3500 seconds, 4 GiB/no swap and 256 tasks
for the live lane; offline workers retain a 64-task limit. The first live campaign
hit the kernel's 64-task ceiling while the native Yahoo client launched its
threads. Preserve that failed campaign and use a fresh campaign after correcting
the test boundary. Resource counters sampled before unit teardown cannot exclude
a later limit hit.
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

## Evidence

```sh
python scripts/run_live_workflow_acceptance.py summary --root /private/new-campaign
```

The summary declares that acceptance requires live sources, real data and live
models. `coverage_gaps` records unobserved real workflow paths, while
`diagnostic_coverage_gaps` retains optional replay/recorder/fault limitations.
Recorded source and model exceptions are counted separately from clock-recorder
failures; the original aggregate and all underlying events remain available.

## Optional diagnostic: accepted-state replay

This command checks preserved accepted state without making a new live observation:

```sh
python scripts/run_live_workflow_acceptance.py accepted-replay \
  --root /private/new-campaign --session 2026-10-30 \
  --boundary-receipt /supervisor/offline-boundary.json \
  --credential-env ALPACA_API_KEY --credential-env ALPACA_SECRET_KEY \
  --credential-env FRED_API_KEY --credential-env FINNHUB_API_KEY \
  --credential-env COURTLISTENER_TOKEN --credential-env USDA_NASS_API_KEY
```

The offline receipt uses the same shared fields and additionally requires
`"kernel_network_denied": true`. Select exactly the source credential names used
for acquisition; the example shows the October 31 campaign's selection. The
native accepted-input identity includes effective source credentials, so removing
or changing them must fail validation. A trusted supervisor supplies the existing
selected values only in memory. Names are recorded; values never enter command
arguments, captures or receipts. Unselected ambient credentials are discarded.
The model-auth directory remains inaccessible. Application logging is scrubbed
before imports and the parent redacts stdout/stderr before persistent writes.
Kernel network denial and all provider/model-call prohibitions remain active.

A separate 120-second worker takes the real
candidate runtime lease, makes a coherent SQLite-backup state copy (including
committed WAL rows), and invokes the real cohort replay over accepted state.
Provider, model, Requests/urllib/HTTPX/HTTPX2/curl, socket and arbitrary subprocess
attempts are prohibited and counted. The candidate's exact local Git common-dir
query is allowed once to locate its real runtime lock. The pinned OpenBB version
also performs two fixed local Git checks and one standard-library `uname -p`
platform check during import. Each allowed command has exact arguments, a fixed
clean environment and a separate invocation cap and counter. The receipt counts
these local queries separately; zero forbidden attempts does not mean zero
subprocess launches. All sixteen books must return `replayed`.
Every original database table's typed logical contents and schema, all accepted
inputs, replay-copy files and original native artifacts must remain identical.
The native inventory includes frozen-profile result/source/data-cache paths,
`data/runtime_recovery`, every candidate `data/logs` file (including daily and
run-attempt logs), the generation manifest and canonical reports. Runner-owned
receipts and console logs remain outside the candidate root.
No table exemptions or reconstructed success are permitted.

For a retained campaign whose recording hooks predate a replay repair, keep its
contract, hooks, tape and failed attempt unchanged. A separately reviewed offline
supervisor can take a coherent full copy under the original canonical lease,
verify every copied file, and mount that private copy at the original campaign
path. It must keep the host baseline read-only and verify it again after the
check. The replay worker then runs inside that boundary with a new empty private
output directory outside the copied campaign:

```sh
/usr/bin/python3.12 -I -S /reviewed/tools/live_workflow/replay_worker.py \
  --root /original/campaign/path --output-dir /private/new-replay-output \
  --session 2026-10-30 --contract-sha256 ORIGINAL_CONTRACT_SHA256 \
  --worker-sha256 REVIEWED_WORKER_SHA256 --uname-sha256 ROOT_VERIFIED_UNAME_SHA256 \
  --credential-env ALPACA_API_KEY --credential-env ALPACA_SECRET_KEY \
  --credential-env FRED_API_KEY --credential-env FINNHUB_API_KEY \
  --credential-env COURTLISTENER_TOKEN --credential-env USDA_NASS_API_KEY
```

This internal worker requires the supervisor's kernel network denial, read-only
tooling and dependencies, absent model-auth mount, process containment and snapshot
checks. `-I -S` prevents execution of the retained virtualenv's recording hooks;
the same accepted interpreter, dependencies and candidate code are loaded
explicitly. Its receipt binds the original contract and hook hashes to the
separate replay implementation. New attempt, state-copy and result files belong
only to the separate output directory. The guard stays installed through worker
exit; the parent performs the final checkout and baseline verification.

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

## Optional diagnostic: controlled edge cases

For a separately needed diagnostic investigation after a native attempt has
finished, this command runs inside the reviewed offline sandbox with kernel
network denial. It is not part of live acceptance:

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
with attempted cases replaces that generic diagnostic gap with any specific failed
and unexercised diagnostic gaps. No matrix clears an unobserved live workflow path.

The summary keeps unobserved real workflow behavior in `coverage_gaps`, including
ordinary sessions, continuity, actual observer composition, timer firing and
subsequent-session fills. Missing or failed replay, controlled mutations and
clock-recording support belong to `diagnostic_coverage_gaps`; they cannot qualify
or disqualify an otherwise established live observation. Root may run the exact candidate's
`scripts/source_repair.py interventions --repo-root ...`, then the pinned native
consumer with a private StateStore and fake send callback, and retain separate
receipts. No real notifications may be sent. Accepted-state replay is **not** a
cold source/model replay, a new native observation, a five-session qualification
or evidence of future fills. Transport replay/mutations must retain exact parent
identity and fail on missing or changed requests; never reuse a captured model
answer for a changed prompt.

Unit tests use dummy repositories, private SQLite WAL data and controlled local
subprocesses. They read no credentials and contact no live providers or production.

## Optional diagnostic: cold recorded workflow

`tools/live_workflow/cold_worker.py` runs the unchanged candidate prospective
controller, shell, screen/governed checks, daily command and independent report
builder against a complete pristine baseline and an immutable completed tape.
It requires an external supervisor to restore that baseline at its canonical
absolute path, deny kernel networking, hide model auth and production state,
mount dependencies/tooling/tape read-only and contain all descendants. Invoke it
with the accepted base interpreter and `-I -S`; it refuses ordinary interpreter
startup. The separate output and private restored campaign preserve every attempt.

The worker verifies the baseline receipt, contract, runtime, candidate, profile
and exact tape identity before installing a separately recorded startup overlay
in the private copy. Curl responses use the actual installed response classes.
Unsupported streaming/metadata forms fail closed. Recorded requests retain phase,
public request and duplicate occurrence; a missing request remains a sticky
failure even when application code catches it. Model responses never satisfy a
changed prompt. The initial worker uses the real clock and therefore may be
blocked when original source queries no longer match; a blocked run is evidence
of a replay limitation, not successful workflow coverage.

Fresh hooks also observe the wall-clock inputs used by an explicit list of
candidate source/model modules and Yahoo SDK modules during screen preflight,
governed preflight and daily execution. Each live call returns the exact native
object it obtained. Module-local facades preserve constructors and instance
checks; global datetime and pandas classes are unchanged. The trace binds the
logical parent/worker entry point, source hash, call site, timezone, public
source/ticker/window context and duplicate occurrence. Ambiguous overlapping
calls and unsupported values remain capture failures.

Offline cold replay uses those exact typed observations only when the verified
tape contains them. It retains full source and model request matching. Controller
admission, authentication expiry, monotonic budgets and execution deadlines keep
their native clocks. Clock events are counted separately from provider/model
calls. Bounded runtime receipts expose capture failures and potential local
imports outside the hook; failed or missing clock receipts block cold admission.
Those local-import declarations are static scope limits, not claims that the
branches executed. Legacy tapes cannot be retroactively given a clock trace.

Anonymous Yahoo authentication is a separate boundary: cookies and crumbs were
not recorded. An explicitly reported offline-only placeholder replaces just the
SDK's anonymous crumb acquisition; all source request construction and response
decoding remain native and exact, and every lower authentication/network attempt
is denied. This does not establish replay of the authentication lifecycle.
The model client's public profile identity has a separate offline adapter. It
returns only the unique identity already present in the verified model tape, for
the exact canonical auth path. It never loads an auth grant or refreshes a token;
the native client and full model request matching remain active. Both adapters
record their invocation counts and explicitly exclude authentication lifecycle
coverage.

A baseline passes only after complete tape consumption, all sixteen-book native
checks, independent canonical report reproduction and process quiescence. Fault
workers additionally require a hash-pinned successful baseline with the same
starting content, contract, session, tape and tool implementation. They modify one
identified recorded response. Source faults must reach the governed SIP request,
produce its matching native ticker rejection and valid independent report, and
commit no economic effect. Model malformed-output cases run the owning native
parser as part of the full workflow. Unreached mutations, unknown downstream
requests, corrupt databases, report failures and lingering descendants cannot
pass. The supervisor verifies original evidence again after cgroup cleanup.

Cold replay still does not establish actual ordinary timer firing, naturally
aligned observer acceptance, multi-session continuity or future fills. Those
claims require their own real observations; a successful empty portfolio session
must not be presented as nonzero fill coverage.

## Ordinary sessions and continuity

Prepare a separate campaign with the first intended trading session as its
`--session`. Under the reviewed live sandbox, invoke:

```sh
python scripts/run_live_workflow_acceptance.py ordinary \
  --root /private/ordinary-campaign --session 2026-11-02 \
  --boundary-receipt /supervisor/live-boundary.json \
  --credential-env ALPACA_API_KEY --credential-env ALPACA_SECRET_KEY \
  --credential-env FRED_API_KEY --credential-env FINNHUB_API_KEY \
  --credential-env COURTLISTENER_TOKEN --credential-env USDA_NASS_API_KEY
```

Admission requires the actual New York date to equal the requested session, at
or after 18:00, and the candidate's own XNYS calendar to accept it. The first
session must match the prepared contract; each later invocation must be the
exact next trading session after the prior accepted result. Source credentials
are selected explicitly under the same live boundary as the prospective lane.
Before a later session may claim or launch, the prior sequence's attempt,
calendar, report, reproduction, process and console evidence is revalidated.
A surviving success result cannot hide missing or damaged supporting artifacts.
The command runs the unchanged ordinary `recover_runtime.py --repo-root ...`
entry point, without prospective flags or a clock override.

Every session claims `ordinary-sessions/DATE` before launching a child. Its
calendar observation, process receipts, independent report, canonical comparison
and final result are retained. A failed or interrupted attempt cannot be retried
or skipped to advance this campaign. Existing prospective attempts and existing
native reports or run attempts for that session are rejected; previously accepted
outputs cannot stand in for a new observation. Success requires both child
processes, exact identities, all native sixteen-book checks and report comparisons.
Reports alone cannot turn a failed child into a passing CLI exit.

The cumulative tape retains the original campaign identity; each result also
counts only the newly recorded source/model events for that session. Natural zero
fills are valid. This command does not install or claim a timer: results explicitly
retain `actual_timer_dispatch: false`. Real timer dispatch, aligned observer
delivery, future fills and several successive sessions still need their own
observations. Unit tests of these gates are not evidence that future sessions ran.

## Actual timer dispatch

The separate `idle` operation exercises the unchanged ordinary controller on an
actual non-session day before acquisition. It accepts no session or credential
override. It observes the candidate calendar before and after the tick, requires
the generation state to remain exactly its frozen configuration, checks the
native journal and proves zero new source/model calls and unfinished recordings.
It also binds a fresh healthy native status to that child process's execution
interval and final console JSON. An exit-zero invocation skipped because of a
busy controller lock cannot borrow the preceding tick's heartbeat. Each attempt
retains its own immutable status snapshot.
Each `idle-ticks/ATTEMPT` directory is claimed exclusively; failures and incomplete
attempts block later idle observations. Historical process/log hashes are checked
before another tick starts.

```sh
python scripts/run_live_workflow_acceptance.py idle \
  --root /private/ordinary-campaign \
  --boundary-receipt /supervisor/live-boundary.json \
  --attempt-id SUPERVISOR_RUN_ID
```

`tools/live_workflow/scheduled.py` is the standalone root dispatcher for the
reviewed external supervisor. It installs no units. Copy it into the protected
supervisor directory, pin it and all controls in an immutable `schedule.json`,
and use its exact command as the test service's `Type=exec` entry point:

```sh
/usr/bin/python3.12 -I -B /reviewed-supervisor/scheduled.py \
  --manifest /reviewed-supervisor/schedule.json --slot REVIEWED_SLOT_ID
```

The bounded manifest contains at most seven ordered slots: initial idle checks,
then ordinary sessions at 18:00 America/New_York. Before launching the pinned
supervisor, the dispatcher checks its actual systemd service PID and invocation,
the associated timer and recent monotonic trigger, the real scheduled window,
unchanged controls and the accepted predecessor. A lock prevents overlap and an
exclusive durable claim prevents retries. Worker completion additionally binds
the real unit result, empty removed cgroup and exact campaign receipt. The
external dispatch receipt establishes actual timer firing; the native runner's
own manual-entry metadata stays unchanged.

Runtime test timers are separate from `trade.service` and `trade.timer` and expire
after their specified dates. Transient timers do not survive a host reboot;
campaign state and receipts remain on disk. A missed or failed predecessor blocks
the sequence instead of silently catching up, resetting state or inventing a
successful session. Verify the installed schedules, actual timer firings and
future results independently. The real calendar, provider responses and model
outputs are never replaced to shorten the observation period.

Actual native observer readers and notification transport are separate checks.
Retain unavailable/stale proposal findings from a prospective run. A single
explicitly authorized, labelled test message can verify the existing native
private-chat route without opening production intervention state or inventing a
source case. A confirmed platform receipt and exact message readback establish
transport delivery, not human reading or positive ordinary-report consumption.

## October 31, 2026 execution evidence

The successful disposable campaign used application commit
`dba04fbaf7bee827f5c61d5d8431677c3d65cb10`, the accepted stabilization-v3 profile
with original SHA256
`ecf94154a063fc1af5b160f51bb2a61acf8384f7a2571f47d41377d91a5b82a9`, and the accepted
Python 3.12 dependencies. Recording hooks came from tool commit
`3ae155c892571e79073272f6ccd536f42b02dc3f`. The reference session was October 30;
acquisition and execution occurred on October 31 under the actual clock and the
candidate's prospective recovery guards. This was one isolated native workflow,
not an ordinary production timer firing.

| Check | Observed result |
| --- | --- |
| Native controller and independent report rebuild | Passed; all sixteen books clean, accounting/staging/evidence/source checks valid, canonical JSON and Markdown identical |
| Recorded source calls | 358 completed responses across Requests, HTTPX and curl; no recorded transport exceptions or unfinished captures |
| Live model calls | 39 completed: 8 thesis and 16 committee calls to GPT-6.1 Sol, plus 15 bounded calls to GPT-6 Luna; configured high reasoning retained |
| Recorded-data edge matrix | 17 passed, 0 failed, 0 unexercised; adapter, model-helper and request-binding checks, each explicitly marked as component coverage |
| Packaged observer and native whole-poll consumer | Ran with a fake delivery sink; correctly withheld the proposal because Saturday acquisition did not match the Friday report session's New York date; no notifications sent |
| Accepted-state replay | Passed for all sixteen books with zero forbidden I/O attempts; all logical tables, accepted inputs and native artifacts identical |

The native run retained eight handled Yahoo HTTP 401 responses and one HTTP 404;
zero transport exceptions does not mean every HTTP response succeeded. Its report
qualified the configured supported workflow while preserving declared research
exclusions. The edge matrix does not establish seventeen complete workflow runs,
cold acquisition replay, strategy-specific model parser coverage, actual timer
execution, subsequent-session fills or multi-session continuity.

Earlier failed campaigns remain preserved. They exposed a cleaned NOAA worker
startup bug in the recording hook, an insufficient 64-task live sandbox limit,
and replay restrictions on local Git/platform queries. The corrected live run
used a 256-task bound. A later credential-free replay reached the native accepted
input validator and failed because source identities bind the original source
credential configuration. No captured envelope, identity or historical failure
was rewritten to manufacture a pass.

The successful offline replay used separately reviewed tool commit
`c06a180207d32f6ec8a45b34cb57ba615d991685` and the same six selected source
credentials in memory, with kernel network denial and no model-auth mount.
Its 106-second unit completed without timeout or abort, and all descendants
exited. It recorded four permitted local queries separately from zero forbidden
I/O attempts. A coherent copy covered 5,171 entries and 635,553,716 bytes.
The parent audit confirmed the original inventory unchanged and every private
file's bytes/mode unchanged; seventeen SQLite shared-memory files had only mtime
changes in the private copy. The original recording hooks and failed attempts
remain intact. Accepted-state replay receipt SHA256:
`22a3f741517d66ce4f50ad0d8615c17750eb335662702d50b529e7390715372a`.

Private raw captures and operational receipts remain outside Git. The successful
campaign is `eventedge-live-workflow-20261031-case-v4`; its primary, postcheck and
separate replay supervisor directories retain exact code/configuration hashes,
boundary probes, unit results and preservation receipts. Normal production
session acceptance remains separate from these isolated tests.

## November 1 diagnostic investigation

The first complete cold attempt used the original pre-acquisition supervisor
archive, whose saved checksum and file inventory were independently verified.
That archive contains the prepared isolated environment before any source or
model calls; it is not a reconstruction from accepted state. The completed
recordings were mounted read-only alongside a separate restored copy.

The attempt consumed 253 of 397 recorded events and ended **blocked**, with 26
exact-request lookup failures. Its independent report reproduced the canonical
JSON and Markdown but was degraded, so it did not qualify. The supervisor
confirmed the original archive, receipt, campaign contract and completed tape
unchanged, then removed the empty process cgroup. No live provider or model call
was made by this replay.

Twenty-four missing requests were repeated thesis calls: the offline public
profile adapter covered model transport but omitted the native memo's independent
namespace lookup. The correction preserves the native memo and exact prompt
matching. Two older lookup failures lack the metadata needed for retrospective
attribution; new receipts retain only bounded request hashes and operation labels.

The old tape also omitted HTTP redirect targets. New recordings retain sanitized
redirect metadata and replay the native redirect sequence; missing historical
metadata is explicitly unsupported. No target is inferred from a later request.
Finally, the native committee prompt includes a generated regime timestamp and
source acquisition timestamps. Exact replay therefore needs genuinely observed
clock inputs as well as responses. A new capture without those inputs would not
close the diagnostic gap. This blocked run is preserved. Following the user's
November 1 clarification, the planned cold baseline/fault campaigns and a further
capture solely for replay are stopped. The ongoing real-source/live-model run
is evaluated independently; an incomplete clock trace does not turn a recorder
limitation into a source or trading failure.

## November 1 real-source/live-model observation

The separate `eventedge-live-workflow-20261101-case-v5` campaign completed the
unchanged native controller with actual sources/data and live configured models.
It retained candidate `dba04fbaf7bee827f5c61d5d8431677c3d65cb10`, the same accepted
frozen profile and fingerprinted Python 3.12 runtime. The recording implementation
was pinned at `28acdaf5a952470b7f3736478bb4cabad9660527`; later reporting changes did
not modify the run or its evidence.

The native controller returned zero. The independent report process returned zero
at `2026-11-01T01:54:53Z`, with sixteen books, accounting, staging validity,
staging completeness, source completeness and evidence completeness all passing.
Canonical JSON and Markdown reproduced exactly. All sandbox descendants exited,
and the supervisor removed their empty cgroup.

The tape contains 359 source calls: 124 Requests, 229 curl and 6 HTTPX calls.
One real Requests `ReadTimeout` during USAspending screen acquisition is retained;
the same request's next recorded occurrence returned HTTP 200, and the real screen
and governed preflights ultimately passed. The other 358 source
calls returned responses, including native handled HTTP error responses. These
counts do not imply that every HTTP request succeeded. All 39 live model calls
returned: 15 bounded GPT-6 Luna calls, 8 thesis GPT-6.1 Sol calls and 16 committee
GPT-6.1 Sol calls, with the configured high reasoning effort. No model failure or
unfinished recording was observed.

This evidence covers the frozen profile's six supported strategy flows across
four horizons and four sizes. The sixteen books share source inputs and signals;
they are not sixteen independent live observations. Deferred or excluded strategy
flows remain outside this acceptance claim. The reference session was October 30,
while source acquisition occurred November 1 UTC / October 31 New York under the
native prospective next-open guard. This does not establish Friday point-in-time
acquisition, an ordinary session, actual timer/observer dispatch, multi-session
continuity or later fills.

The completed private paper ledgers contain 24 pending intents eligible on
November 2 and zero fills, consistent with the canonical report. These are real
native decisions from the acquired data and live models, retained as pending
paper intents. They establish neither broker orders nor successful future fills.

Five unsupported clock-recording observations remain an explicit diagnostic
limitation. They did not replace native values, and the original native report
and process results passed independently. A read-only assessment of those original
artifacts separates real coverage gaps from diagnostic limits and records their
hashes; its SHA256 is
`d4497c124ea486248b68ceb9681dde58d93d5677412a237cabb2fb3e8a718d2d`.
No further capture solely for replay or offline fault campaign was started after
the user's clarification. Production was unchanged by this work.
