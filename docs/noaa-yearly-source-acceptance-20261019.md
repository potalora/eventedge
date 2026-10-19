# NOAA yearly source acceptance, October 19, 2026

The opt-in yearly-file adapter acquired complete current NOAA coverage without
credentials. The source-only registry run took 36.385 seconds, with one HTTP 200
request for each catalog and the 2026 archive. It selected 7,877 stations across
all ten agricultural states and produced 201,243 usable observations in the
September 17–October 16 thirty-day window. The October 19 reference date had
three days of observation lag. This is development evidence, not deployment or
a completed financial workflow.

The freshly acquired bodies exactly matched the three retained originals pinned
in the fixture provenance. The annual body was 116,905,885 compressed bytes and
contained 25,571,346 rows / 923,809,128 decoded bytes. The full-roster selection
digest was `42c495e70e2c229ae0428a1f183a78a5050a2cc2d555fd16aaa3baf52ed4ea71`.

After the final gzip-header deadline fix, the complete originals passed offline
replay through the selected registry adapter in 34.968 seconds, with all three
bodies consumed and closed and zero external-network or subprocess attempts.
The replay reference is October 18 and its explicit replay vintage is October
19; it obtains the same observation window with two days of lag. Replay timestamps
are distinct from the pinned original acquisitions. The native network worker
was unchanged by that decoder fix.

Final adapter SHA-256:
`95cb2552abf736da91467007dd691e9086857a1913f083b938ed1edd4910d3a9`.
Network-worker SHA-256:
`c4c5e10e814299f399aec15efe0ce864e4a1b43575ef5cfff62e25e3eb675b73`.
The final full replay bound all 123 application/runner Python sources with
digest `070ba6c9409e6df9954501b40b1d7c0783ec806724714c3e14d4f1ae56d07531`.
Environment: Python 3.11.16, HTTPX 0.28.1, httpcore 1.0.9, requests 2.34.2,
urllib3 2.8.0, certifi 2026.7.22.

Sixty focused tests passed on the final implementation: 44 adapter/worker,
12 selection/budget, and four CLI acceptance tests. They include actual
cross-year records and missingness, independent `.dly` values/flags, partial
element station eligibility, original mixed-case/hyphen/underscore station
identities, UTF-8 station names, strict complete gzip validation, quality/trace
handling, resource caps, corruption, cache identity, and cleanup.

Independent review verified that an injected ten-second DNS stall terminates in
0.504 seconds under a 0.5-second budget and leaves no worker or temporary files.
Both two-megabyte gzip filename/comment controls terminate in 0.051 seconds
under a 0.05-second budget. Ordinary scheduling/cleanup overhead is included in
those observations. FIFO, symlink, and oversized acceptance metadata are rejected
without blocking or descriptor leaks. No material review findings remain.

Full files, raw acquisition receipts and detailed local reports remain outside
Git. Only compact public original-line fixtures and safe provenance are committed.
The original failed query-service capture remains unchanged. The yearly delivery
and exact metric conversion are explicit new-generation policies; Access JSON
rounding parity, historical availability, multi-session continuity, model quality,
and production readiness are not established by these checks. Complete CI results
belong to the final PR commits and their retained workflow artifacts.
