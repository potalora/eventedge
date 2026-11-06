# FINRA publication and archive repair, 2026-10-22

September 30 settlement data is current on October 22. FINRA's
[official reporting schedule](https://www.finra.org/filing-reporting/regulatory-filing-systems/short-interest)
publishes that cycle on October 9 and the October 15 cycle on October 26. Age
alone cannot establish stale short-interest data.

The inspected OpenBB FINRA 1.6.1 implementation has two distinct defects: its
archive inventory adjusts weekends but misses some holidays, and it omits the
entire current month even after a mid-month cycle is published. Its native
downloader also silently returns on a non-200 response. A completed preparation
call therefore does not establish complete archives or current symbol data.

The adapter now uses the reviewed publication table, corrects SDK archive dates
`2024-01-15` to `2024-01-12` and `2024-03-29` to `2024-03-28`, and includes the
required published cycle even when absent from the SDK inventory. Nasdaq's
[January 2024 announcement](https://ir.nasdaq.com/node/107016/pdf) and
[April 2024 announcement](https://ir.nasdaq.com/node/107396/pdf) independently
corroborate those corrected settlement dates.

Only a missing required current archive is downloaded using the supported
native downloader. This populates its native cache; it does not mutate SDK
globals. Optional historical backfill is skipped so it cannot consume the
current query's deadline. Existing historical rows remain in the bounded,
read-only SQLite snapshot. The corrected requested historical inventory and any
remaining historical gaps are retained separately. A missing current archive
fails acquisition before per-symbol normalization, including when the native
downloader silently skips an unavailable file.

Per-symbol normalization checks the required cycle independently. A current
archive containing other symbols does not certify an old symbol's row. Missing,
stale, and not-yet-published rows cannot enter successful scalar caches; cached
rows are revalidated against the same captured acquisition clock. Healthy
current values remain usable when requested historical archives are incomplete.
Durable acquisition schema 2 marks those archive attempts `partial`, records
`current_complete` and `historical_complete` separately, and names the historical
scope explicitly. Archived schema 1 remains readable without rewriting it.

## Publication boundary and expiry

FINRA's schedule supplies publication dates without a release timestamp. The
adapter conservatively admits a cycle on the following New York calendar day
and marks publication-day ambiguity. It does not infer midnight availability or
apply an exchange's timestamp to every FINRA archive. On October 26, September 30
remains the required conservative cycle; October 15 becomes eligible on October
27 in New York. Both the requested reference date and actual aware acquisition
timestamp limit visibility. The actual acquisition timestamp is retained.

**This reviewed calendar expires for new acquisitions on January 1, 2027, in
America/New_York.** A 2027 calendar review is required before then, including the
December 31, 2026 settlement published in January. The adapter fails closed after
expiry; passing an older reference date cannot reactivate it. The table does not
authorize extrapolated holidays or future calendar use.

## Evidence and verification

The [public evidence record](2026-10-22-finra-source-repair.json) contains the
reviewed schedule, correction sources, and bounded read-only CDN probes. The
corrected January 12 and March 28 archives and September 30, 2026 archive returned
HTTP 206 CSV prefixes. The SDK's two incorrect dates and unpublished October 15
archive returned HTTP 403. Those responses are recorded as 403, not invented
404s; authoritative settlement evidence establishes the holiday corrections.

Test-first checks cover publication-day ambiguity, New York boundaries,
reference/acquisition cutoffs, calendar expiry, both holiday corrections,
current-month SDK omissions, silent missing-current downloads, stale/missing
symbols, historical scope tampering, and schema 1 compatibility. A regression
test makes historical backfill exhaust a synthetic deadline and proves the
current query never starts that download.

```bash
python -m pytest tests/test_finra_schedule.py tests/test_finra_archive_coverage.py \
  tests/test_finra_bulk.py tests/test_finra_current_admission.py \
  tests/test_openbb_semantic_admission.py tests/test_openbb_source.py -q
```

These are development changes and public read-only probes. No production cache,
generation, manifest, service, or live state was modified.
