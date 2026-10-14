# Scheduled workflow acceptance

The 2026-10-14 midday failure exposed a gap between independently tested pieces:
the shell test substituted Python, preflight tests substituted shared acquisition,
and prospective integration tests started after a completed market session. None
combined the frozen production policy with the midday entrypoint and real clock
guards. Healthy idle controller observations did not establish that combination.

## Release gate

Run the named gate from the candidate checkout with its installed dependencies:

```sh
python -m pytest -q -m 'scheduled_acceptance and not live' tests
python -m pytest -q -m 'not live and not scheduled_acceptance' tests
```

CI runs both commands in the baseline and pinned optional-SDK environments. A
failed gate blocks acceptance. Skipped, expected-failure, or omitted scenarios do
not establish the corresponding requirement.

The subprocess harness creates an independent temporary Git repository, frozen
generation worktree, profile, state directories and virtual environment. Its
fixture commit identifies that test snapshot, never the deployed release. It
uses the real controller, shell wrappers, generation manager, worker, locks,
source adapters, clock/calendar rules, orchestration, ledgers and report code.
Only external responses and the current-time source are replaced. All descendant
Python processes receive the same fixture boundaries and network denial,
including Python sockets and native curl transports. Negative transport tests
verify that unexpected calls fail before contacting a provider. The
wall-clock injection does not replace calendar calculations, admission checks or
the monotonic clock used for termination deadlines.

An explicit empty `.env` in the disposable repository bounds the manager's real
dotenv discovery. Its environment, home, temporary files and dummy authentication
storage are isolated. A synthetic ancestor-variable regression verifies that
normal loading cannot inherit configuration from the surrounding checkout.

## Profile provenance

The retained fixture is the original non-secret deployment profile, SHA256
`cfd010bec8effa9936df71a9fe87f72eac9403c43219163998d78d917314673d`.
Runtime copies substitute disposable filesystem/authentication paths and
constructed local document bytes with their matching digests. The harness records
and checks those substitutions. The normalized fixture has its own hash; it is
not represented as the exact deployed profile.

The separate environment fixture retains the effective `GOOGL,GOOG` compliance
exclusions recorded by the generation's October 14, 15:45 UTC preflight. It
includes that non-secret log excerpt and the retained evidence digests. The
harness checks the actual worker configuration and its accepted source binding;
this environment policy is declared separately from profile substitutions.

Model/effort selection, strategy exclusions, prospective clocks, source policies,
paper execution settings and the document's finite effective window remain in
force. Synthetic provider/model responses do not prove live provider availability,
account authentication, model extraction accuracy or financial performance.

The approved bulk-filing rollback deliberately keeps EDGAR research unavailable.
The full-profile rehearsal must preserve that documented coverage loss and its
degraded status. Acceptance requires completed accounting/staging for all sixteen
books and positive independent decision evidence while accurately reporting the
EDGAR exclusion; it must not make the source appear healthy to obtain a clean
label. The report can have complete, internally consistent evidence while its
input coverage remains degraded. Unexpected additional failures remain
acceptance failures.

## Contracts covered

- Complete scheduled evening chain, including the screen warning, governed hard
  gate, sixteen scenario books, durable attempt records and final report. A
  second ordinary session fills the first session's positive paper intents;
  repeating the controller must leave financial rows and accepted inputs intact.
  Every document and committee role retains its configured model and effort.
- Before-close and after-close behavior, the existing fifteen-minute historical
  SIP delay, early closes, holidays, daylight-saving changes and delayed timer
  wakeups. A successful diagnostic screen cannot authorize trading.
- A failed midday probe followed by a legitimate same-session evening run, with
  the original failure retained. Fixed runtime reasons remain distinct from
  provider failures and source-repair proposals.
- Receipt-backed deduplication and supported resolution of runtime preflight
  observations. A missing report, different configuration or unrelated successful
  probe cannot erase an incident or authorize a duplicate delivery.
- Actual process interruption and lock handoff, with no unintended changes to
  manifests, ledgers or accepted inputs. Nested source transports are included
  in the preflight timeout, worker-crash and outer-controller timeout cases.
  The outer controller's existing termination grace applies to its whole group,
  allowing a manager to forward termination after the shell leader exits.
- The document's exact finite expiry, with no automatic renewal or broader
  authority. Date-only document eligibility uses New York end-of-day. For this
  profile, October 16's final fractional second exceeds the declared
  `2026-10-17T03:59:59Z` endpoint, so date-only October 16 is conservatively
  excluded; an explicit instant at the endpoint remains within the window.

The harness does not run systemd or prove live provider/model availability. Its
expiry cases exercise the eligibility boundary directly. Calendar cases cover
diagnostic acquisition, admission boundaries, and real controller dispatch at
18:00 New York after daylight-saving changes and an early close. Those two
dispatch cases stop at a deliberately stalled external transport; the complete
positive financial path uses ordinary October sessions. An uncatchable kill of
the manager itself cannot execute its signal forwarding; service containment remains
the systemd control group's responsibility.

## Deployment boundary

Passing these checks establishes isolated candidate behavior. It does not deploy
the candidate, repair an active generation, install an alert consumer, change a
timer, or prove a live financial session. Production changes still require
Pedro's explicit deployment or narrowly scoped patch authorization and the
repository's governed release process.
