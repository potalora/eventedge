# NOAA full-roster acquisition throughput

The retained 15 October source screen selected all 7,877 eligible GHCN stations
across the ten declared agricultural states, then reported a timeout and
incomplete coverage. Partial observations do not establish a valid summary.
That evidence is retained in
`data/forward-readiness/deployment-20261014/scheduled-20261015-audit/source-screen-diagnostics.json`.
It establishes deadline exhaustion. A subsequent four-batch read-only probe
through the actual generation environment returned HTTP 200 for all batches.
The early/middle batches took 7.372, 4.439 and 4.025 seconds, while final batch
78 (77 USW stations) took 33.259 seconds and supplied 2,496 TMAX, 2,496 TMIN and
2,402 precipitation measurements. Total probe duration was 33.268 seconds,
within its independent 45-second cap. The retained metadata is
`data/forward-readiness/deployment-20261014/fresh-generation-20261015/noaa-native-probe-20261015.json`.
This demonstrates heterogeneous late-batch latency; the fifteen-second requests
read timeout is an inactivity bound, not a total body acquisition limit. A
four-batch probe does not establish full-roster availability.

The initial eight-worker candidate returned complete sample coverage in a
full native probe, but total runtime was 188.134 seconds against the declared
180-second budget. It reported 193,094 observations, forty quality exclusions
and all ten states in the thirty-day window ending October12. That receipt
(`noaa-full-native-probe-20261015.json` in the same private evidence directory)
does not prove deadline compliance: it lacks separate acquisition/aggregation
timings, and the initial adapter had no deadline check after a returned HTTP
body, regional collection or summary aggregation. Its complete result is not
accepted evidence of a bounded native pass.

The current candidate raises the bulk executor ceiling from four to sixteen
workers and checks the unchanged absolute deadline after each decoded batch,
after regional collection, and before accepting the completed summary. A late
sample remains incomplete and its actionable weather metrics are withheld.
All 79 original batches, the complete inventory selection, 100-station batch
bound, 180-second total acquisition deadline, shorter parent deadlines,
transient retries, and the shared rolling admission limits of five request
slots per second and 10,000 per day are preserved. Measurement quality, contiguous observation
window, seven-day freshness limit, state-day arithmetic means, and incomplete
coverage rejection retain their existing definitions. Concurrency improves
throughput for slow healthy responses; persistent upstream failures still fail.

The deterministic full-roster regression models twenty-four seconds per healthy
batch, charging amortized wall time at the actual executor concurrency. This
constructed scenario requires approximately 240 seconds with eight workers and
120 seconds with sixteen, plus catalog acquisition. The initial four-worker
latency regression failed with all-state timeouts before its concurrency repair.
Three additional regressions first demonstrated that otherwise complete late
bodies, late regional collection and late summary aggregation were accepted by
the initial eight-worker candidate; each now rejects those late results. The
full-roster test verifies all 7,877 station identities in exactly 79 batches, complete ten-state samples, and the contiguous thirty-day window.
Separate tests retain shorter-parent and terminal-retry failures. A real-thread
transport test verifies sixteen simultaneously outstanding requests while the
shared rolling limiter admits at most five requests per second and 10,000 per
day, counting the station catalog and inventory as well as bulk requests. The
test records the actual history append under the limiter lock, rather than a
later transport callback timestamp. A deterministic ten-millisecond pause
between reservation and dispatch demonstrates why otherwise compliant slots
can appear as six callback entries in a sliding second. Operating-system
scheduling, HTTP setup and network delivery can shift actual dispatch or server
arrival times; this limiter does not guarantee their exact rolling counts. The
provider quota remains five requests per second and 10,000 per day. The test
correction adds no tolerance or quota increase; it corrects the measured
boundary and leaves that dispatch-jitter limitation explicit. An exhausted
daily admission quota is separately checked to prevent another transport call.

The shared request policy also rechecks the remaining budget after admission,
before invoking HTTP or SDK transport. A scheduling pause that consumes the
remaining budget now prevents dispatch instead of passing a nonpositive value
to a minimum socket timeout. Regression cases cover exact expiry, elapsed
expiry and an unchanged positive timeout. This remains a cooperative check;
it cannot prevent operating-system suspension after the check itself.

Validation: 174 focused tests passed across `test_noaa_regional_budget.py`,
`test_request_policy.py`,
`test_source_audit_repairs.py`, `test_environmental_contract_repairs.py`,
`test_live_source_contracts.py`, `test_gen005_fixes.py`, and
`test_source_reliability_pipeline.py`. These offline results do not establish
a complete new-generation workflow.

A subsequent native source-only probe of the exact sixteen-worker candidate
SHA256 `f85e1c61c3e76f46c0fc696e232bc6b5a21fb271cfc8e0dd571830bb7399adf2`
completed in 116.619 seconds, within the unchanged 180-second deadline. It
returned all 7,877 catalog stations and all ten reporting states, 193,094
contributing observations, forty quality exclusions, a three-day observation
lag and no missing state/date/type samples. The receipt is
`data/forward-readiness/deployment-20261014/fresh-generation-20261015/noaa-full-native-probe-16-20261015.json`.
Root executed the candidate in memory against the native generation environment
with an old-source hash guard; this did not deploy code or write generation
state. This is native NOAA source acceptance only. The new-generation daily,
required model/research stages, sixteen-book accounting/staging and monitor
acceptance remain independent gates.
