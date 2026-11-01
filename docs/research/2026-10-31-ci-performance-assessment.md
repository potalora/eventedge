# CI performance assessment — 2026-10-31

The linked CI article supports measuring EventEdge's slow acceptance stage before
changing infrastructure. It does not justify changing market-data sources,
weakening acceptance coverage, or bypassing the failed checks on PR #64. The
approved FINRA rollout remains contingent on resolving those failures and passing
the existing supported-environment gates.

## External evidence and limits

Owen Gretzinger's [October 30 post](https://x.com/owengretzinger/status/2116291924219060319)
links to [“How I made our CI 20x faster (steal this)”](https://x.com/i/article/2116240180571172864).
Direct X retrieval returned HTTP 403; the full article text was retrieved through
the [public FxTwitter mirror](https://api.fxtwitter.com/owengretzinger/status/2116291924219060319).
Its attribution and performance figures are therefore mirror-retrieved,
self-reported evidence, not independently verified Boardy measurements.

The article reports reducing CI from 40 minutes to 2 through faster runners,
lint/typecheck tools, sharding, reduced repeated initialization, setup/cache
fixes, parallel jobs, affected-project selection, and local hooks. It also reports
monthly spending rising from $230 to $4,300. Neither its speedup nor its cost
tradeoff can be assumed to transfer to EventEdge.

The linked [Linear engineering account](https://linear.app/now/ci-bottleneck-reworked)
corroborates the general value of measuring setup and critical-path work. It
identifies shared module state as its highest-risk optimization and retains
isolation for unsafe tests. Linear also found rebuilding dependencies faster
than restoring `node_modules`, unlike Boardy's result: benchmark locally.
[GitHub's cache reference](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching)
documents fallback to default-branch caches; PR cache scoping alone does not prove
that every PR must start cold. [Vitest's isolation option](https://vitest.dev/config/isolate.html)
is specific to that runner and conditional on side effects.

## EventEdge measurements

Source: [PR #64 run 41185778628](https://github.com/potalora/eventedge/actions/runs/41185778628),
commit `bfd21fa81080b53157b6b5043022b6e96151215f`, retrieved with
`gh run view 41185778628 --json status,conclusion,jobs,url,createdAt,updatedAt,headSha`
and `gh run view 41185778628 --log`. Both jobs failed; this is a failed-run
baseline, not the duration of a complete passing pipeline.

| Stage | Standard environment | Pinned OpenBB environment |
| --- | ---: | ---: |
| Run creation to job start | 4 s | 4 s |
| Checkout | 1 s | 2 s |
| Python setup | 1 s | 1 s |
| Dependency installation/build | 27 s | 46 s |
| NOAA and historical replays plus evidence uploads | 4 s | 6 s |
| Scheduled acceptance step | 34 min 22 s | 45 min 5 s |
| Remaining non-live suite | skipped after failure | skipped after failure |
| Total job interval | 34 min 59 s | 46 min 4 s |

Step times use GitHub's second-resolution timestamps. Pytest itself reports
2,059.61 s and 2,702.62 s respectively, each with 422 passed and 1 failed.
Both logs report a pip cache miss. Setup remained under one minute even with
those misses; runner queueing and dependency setup are not the dominant delay
in this sample. The longest standard test calls were the retained-production
evening workflow (200.24 s) and stabilization v2/v3 scenarios (194.18/193.25 s).
The corresponding OpenBB calls took 245.09 s and 271.63/272.07 s.

The candidate `.github/workflows/nonlive-tests.yml` already runs the two Python
environments concurrently, uses pip caching, reports the ten slowest scheduled
tests, and sets a finite 90-minute limit per job. It does not use the article's
JavaScript lint/typecheck/test stack. A single failed run cannot establish cache
miss prevalence, normal queue latency, or a safe speedup estimate.

## Safe next experiment

First repair the acceptance failure and obtain a complete passing baseline in
both environments. Then, as a separate development experiment, repeat the same
commit and exact test selection while collecting phase timings for the three
slow scenario families: fixture/profile creation, process startup, scheduled
worker execution, and replay/verification. Keep private temporary directories
per test and environment, preserve all assertions and supported profiles, and
compare elapsed time and runner-minutes across repeated runs.

The subsequent request excludes buying runners and sharding. The bounded
[November 1 follow-up](2026-11-01-ci-performance.md) measures repeated import
work while preserving both environment gates, the exact existing selections,
all assertions, and historical replay evidence. Do not remove isolation, skip
tests by changed paths, shorten real acceptance behavior, buy runners, shard,
or swap tooling as part of this work.

This assessment identifies no new FINRA or Congress ingestion defect. FINRA's
published-settlement and symbol-coverage checks remain required. Congress
recovery still requires a subsequent accepted collection; faster CI supplies no
evidence of upstream filing availability or operational recovery.
