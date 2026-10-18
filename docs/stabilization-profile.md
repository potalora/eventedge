# Sixteen-book stabilization profile

`config/profiles/stabilization-v1.json` is a prospective paper-trading profile for
four horizons and four portfolio sizes. It requires a new generation and direct
deployment authorization. This PR does not deploy it or activate schedules.

## Supported scope

The six supported strategies are commodity macro, earnings calls, government
contracts, focused litigation, supply chains and weather/agriculture. Existing
market-data, financial, risk, time and accepted-state integrity checks remain.
Thesis and portfolio committee requests use Sol 6.1 at high effort through the
existing ChatGPT-plan transport.

The reviewed GOOGL/GOOG compliance blocklist is explicit in the profile. The
worker rejects a conflicting environment/CLI blocklist or EDGAR contact header;
historical runs are not reinterpreted using current environment values. Release
preparation must freeze the intended contact identity before computing the
generation profile hash. Credentials still use the existing launcher boundary.

The profile explicitly excludes bulk-filing strategies (insider activity, filing
analysis and quantum readiness), regulatory document research, congressional
display-only data and the unsupported state-event proxy. Bounded document
research and decision shadow are off. Commodity macro omits its optional
regulatory-news boost; its CFTC/FRED rule and Finnhub catalyst remain. The filing
exclusion does not remove issuer identity checks used by focused litigation.

These declarations bind the frozen profile and health evidence. Unknown disable
reasons, missing health rows, model holds or incomplete staging cannot qualify as
successful supported work. `workflow_complete` describes the supported workflow;
`research_coverage_complete` remains false for the declared exclusions.

## Source and restart boundaries

The opt-in source capability policy preserves original provider failures and
hashes while admitting only independently validated capabilities. Optional FRED
failures cannot erase valid HY-spread, yield-curve and VIX observations. Required
regime failure still holds research. Yahoo VIX is independent of its atomic
research-price basket; a failed basket never becomes a survivor-only universe.
Unknown consumers require complete provider coverage.

A shared immutable receipt accepts the economic bundle, full epoch, code/profile
identity, roster and per-book starting states before the first book's economic
effects. Recovery validates every book before resuming any. It can bind unstarted
books from that seal, but cannot invent a missing committee decision after its
cutoff. Cancellation and initial context binding share one ledger transaction.
Historical profiles retain their prior recovery rules.

Daily and preflight workers share the existing process-group owner. It terminates
descendants on timeout or leader exit and preserves full worker result output.

## Verification and release evidence

Run recorded historical source replay before the supported scheduled gate:

```bash
.venv/bin/python scripts/run_historical_source_acceptance.py --report /tmp/eventedge-public-history.json
.venv/bin/python scripts/run_historical_source_acceptance.py --private-corpus /absolute/private/corpus --require-full-supported-scope --report /absolute/private/full-history.json
.venv/bin/python -m pytest -q -m 'scheduled_acceptance and not document_qualification and not live' tests
```

The exact-profile acceptance loads the checked-in profile and audits every path,
inert-auth and contact-header substitution. It exercises the real controller, shell, manager,
worker, two sessions, sixteen books, positive paper fills and replay without new
external calls. Focused tests cover first/middle/last interruptions, invalid seals,
required and optional source failures, numeric corruption and hung descendants.

The full 98-image document workflow is a separate deferred qualification:

```bash
.venv/bin/python -m pytest -q -m 'document_qualification and not live' tests
```

Document unit and replay regressions remain in ordinary non-live CI. Both hosted
environments require the public historical replay before the scheduled and
remaining non-live suites. Full supported-source acceptance additionally requires
the exact private corpus; missing artifacts fail rather than skip. See the
[acceptance policy](scheduled-workflow-acceptance.md) and manifest under
`tests/historical/` for provenance, source-format boundaries and coverage gaps.
The historical source baseline uses recorded data. Constructed external IO in
the scheduler scenarios supplies repeatable clock, interruption and fault cases;
it cannot substitute for that baseline or establish empirical source reliability.

Stabilization workers emit `EVENTEDGE_RUNTIME_IDENTITY_V1` on stderr, retained in
the existing immutable attempt. It binds generation/code/profile, Python version,
the existing installed-distribution fingerprint and the checkout's lockfile hash.
This is provenance, not a claim that installed dependencies match the lockfile.
Compare it with the reviewed release environment during explicit deployment.

Constructed acceptance, a native first-run acceptance, five observed qualifying
sessions and strategy performance are separate claims. The implementation and
offline tests establish no live provider/model health, elapsed multi-day
continuity or research-performance result.
