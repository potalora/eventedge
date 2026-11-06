# NOAA yearly-file delivery

`autoresearch.noaa_delivery_policy: ghcnd_yearly_v1` explicitly selects official
NOAA GHCNd annual gzip CSV files. An absent policy keeps the existing source;
other supplied values fail configuration validation. There is no automatic
fallback to or from the query API. A profile opting in requires a new generation.
A branch, PR, or successful source check is not a deployment.

## Scientific contract

The adapter selects every station in the ten agricultural states with a core
element inventory overlapping the observation years. A station need not report
all three elements. Coverage requires a sample for every state, date, and element
(`TMAX`, `TMIN`, `PRCP`) in the newest complete contiguous requested window, with
at most seven days of lag. The supported lookback is 1–60 days. State/date/element
means are arithmetic means; heat and frost counts use unique regional days.
Missing observations never become zeros or healthy partial coverage.

`ghcnd_metric_exact_to_standard_v1` converts raw tenths of degrees Celsius to
Fahrenheit as `(9 * raw + 1600) / 50`, and tenths of millimeters to inches as
`raw / 254`, before the existing summary rounding. This is mathematically exact
conversion with ordinary floating-point storage. NOAA Access JSON rounding
parity has not been established. Nonblank quality flags and `-9999` missing
values are excluded; trace precipitation is retained. Measurement/source flags
and observation time remain in provenance; observation time is not publication
or acquisition time.

The official files are mutable current vintages. An observation/reference date
can precede acquisition; it cannot turn a new download into a historical
information set. An explicitly requested vintage must be the current New York
calendar date. File URLs, hashes, byte counts, acquisition times, selected-roster
identity, numeric policy, exclusions, and coverage accompany the summary.

## Bounds and failures

One 180-second acquisition/decode/aggregation deadline is narrowed by any parent
provider deadline. Required station and inventory catalogs and every intersecting
year are acquired under that same deadline. Native HTTP runs in a disposable
worker so stalled DNS, headers, or trickling bodies can be terminated. Deadline
cleanup allows normal process scheduling; timed-out work cannot produce metrics.
The worker receives no provider credentials, only the explicit URL and request
bounds.

Limits are 64 MiB per catalog, 256 MiB compressed and 2 GiB decoded per year,
40 million annual rows, 1,024 bytes per line, two years, 12,000 selected stations,
and two million selected identities. Each gzip must pass complete EOF/CRC
validation. Repeated selected identities, malformed selected records, exhausted
budgets, failed required files, and incomplete coverage withhold all actionable
weather metrics and retain a typed failure. Temporary files and workers are
closed or reaped on every exit.

## Reproducible checks

Run the compact public-record replay without network or credentials:

```bash
python scripts/verify_noaa_yearly_source.py --report /tmp/noaa-public.json
python -m pytest -q tests/test_noaa_yearly_source.py tests/test_noaa_yearly_registry.py \
  tests/test_noaa_yearly_budget.py tests/test_noaa_yearly_acceptance.py
```

The fixture manifest pins original selected lines and documents deterministic
gzip recompression. Independent `.dly` records check integer values and flags;
actual cross-year missingness is retained. Controlled corruption tests are
labeled separately from observed provider data.

For full retained-file acceptance, provide the exact originals named below in
an explicit directory outside Git. Their hashes and original acquisition times
are pinned in `tests/fixtures/noaa_ghcnd_yearly/provenance.json`:

```text
stations.txt
inventory.txt
2026.csv.gz
```

```bash
python scripts/verify_noaa_yearly_source.py --full-corpus /explicit/noaa-originals \
  --report /tmp/noaa-full.json
```

Missing or changed originals fail; the command never substitutes the small
fixture. It runs the selected registry adapter against the complete raw files,
replacing only HTTP delivery, with sockets, SDK network, and subprocess fallback
denied. Reports bind source-code identity, dependencies, original files, replay
clock, coverage, transport consumption/closure, and external-IO counters.
The full replay targets the recorded September 17–October 16 observation window;
it does not claim that those current-vintage bytes were available historically.

This gate establishes raw-file source behavior. It does not certify model output,
paper-book continuity, financial correctness, or production activation. Keep the
original failed API captures; a successful bulk replay does not repair or rewrite
them.

## Official format references

- [Yearly CSV layout](https://www.ncei.noaa.gov/pub/data/ghcn/daily/by_year/readme-by_year.txt)
- [GHCNd units, flags, station and inventory layouts](https://www.ncei.noaa.gov/pub/data/ghcn/daily/readme.txt)
- [NOAA Access service documentation](https://www.ncei.noaa.gov/support/access-data-service-api-user-documentation)
