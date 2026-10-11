# Source reliability verification and release notes

## October 11 supported-document candidate status

The source-reliability results below are historical. The latest committed XML repair is `8f69cc9`: 5,362 local tests passed, and its CI passed 5,359 tests with three skips. Neither result validates the new topic-branch candidate. The final local non-live suite passed **5,860 tests**, with four deselected, one existing websockets dependency warning, in 220.10 seconds. The closed log is `data/forward-readiness/supported-documents-full-suite.txt`. The exact-current-head CI result is recorded on [PR #50](https://github.com/potalora/eventedge/pull/50) and must pass before a new native freeze. Independent review accepted the filing changes with 186 focused tests, including all 25 reviewer cases; the raw-capture changes with 113 focused tests; and the final regulatory changes with 123 focused tests, including all 23 reviewer cases. No reproduced review blocker remained. These review results do not establish native access, source completeness or accepted replay.

The new opt-in `verified_supported_documents_v1` keeps the exact TEN quarantine. Only independently source-verified visual PDF obligations, closed ambiguous ownership lineages and publisher-reported CBL contradictions can be deferred. Deferral keeps strict full coverage false and removes that source from complete thesis analysis. Unknown capabilities and all retrieval, parser, model, deadline and budget failures remain failed.

SEC HTML tables, redlines, images and capability gaps do not become supported text through flattening. PDF text support is narrow native Type1 text. Valid fully inventoried lossless raster or vector content can establish visual deferral. Opaque codecs, masks and unverified graphics fail. Ownership evidence requires closed subject history and archives, complete roster/security identity, all applicable base/intervening/incorporated originals, strict scalar XML and markup-aware references. Annual prior rules do not change.

Regulatory `complete_document_text_v1` requires the full rule and attachments. It has no first-three or title fallback. Its narrow inert HTML subset rejects CSS, style, forms, tables, lists and embeds. Regulatory PDF acquisition is not wired and fails. Prose issuer bridges and automatic directional regulatory targets are not supported. Complete unresolved-fact-free evidence can support a valid journal-only no-directional-thesis result; missing issuer evidence cannot be called no thesis.

Failed model attempts retain up to 256 KiB of exact response text plus full hash and provenance. Oversized attempts remain failed, and capture must retain their complete response separately. A prefix is never full-response evidence.

The new independent raw audit requires every completed original, including failed and quarantined originals. It verifies private regular files, full hashes, one read-only mapping at a time, regenerated source proofs, ownership lineages and supported dispositions. The caller must provide one positive remaining-time check and a governed resource boundary. All in-pipeline scopes remain provisional until native raw audit and complete immutable replay pass.

The exact advertised EPA HTML download returned 403 on the intended native host at `2026-10-11T16:21:44Z`. Its complete response was 919 bytes, SHA-256 `62cd994ff88bf477e4f335491b90b098617a38587d2c3b5d4f18142b39f1811e`. Three earlier Mac HTML checks returned 403; the metadata API returned 200. The closed diagnostic is `data/forward-readiness/regulations-detail-contract-probe-20261011/native-host-html-accessibility.json`. It is an external-access blocker, not proof of its cause. Do not use metadata as full rule text or launch blind retries.

The closed earlier parent run at `1d8c615` completed acquisition/freeze of 3,016 originals. NOAA took 105.905 seconds for ten states and 7,877 stations. At the stop, 182 model jobs had returned and 32 were in flight; these are not signal counts. The operator stopped the run after filing analyses returned insufficient. Full sixteen-book acceptance was false and accepted replay was ineligible. Closed retrieval verified 5,115 files and 3,358,688,422 bytes. These retained parent facts do not establish a new candidate result or a completed analysis census.

No new native run was launched. The new policy has no verified four-horizon, sixteen-book, model-call or immutable-replay result. A new freeze and capture-adapter wiring are required before launch. Original full coverage remains false; scoped acceptance is pending. Source/model/call limits of 900/2,400/120 seconds, two SEC workers, 16 MiB document, 512 MiB corpus, 4 GiB spool and 8 GiB private-evidence limits remain fixed. Not merged. Not deployed.

The all-original capture must fit the unchanged 4 GiB physical spool cap, which includes retained hard-link originals. The historical corpus contains 7,298,823,168 uncompressed raw bytes and cannot fit that cap. A compressed alternative is unproven and not implemented. This does not authorize a higher cap or omitted originals. The regulatory HTTP 403 remains the primary external blocker.

## Historical problem and resulting behavior

Several provider adapters converted exhausted requests or malformed responses
into empty data. Strategy health could then appear healthy, and persisted health
failures did not reliably affect the daily outcome. Separate screening and daily
attempts also repeated requests and could lose earlier incidents in reporting.
Yahoo raw daily OHLC validation rejected repeated AYI/BR responses.

The current pipeline uses validated Alpaca SIP for raw daily prices. Event
adapters distinguish successful empty results, partial data, and failures.
Shared bounded acquisition retries transient errors and reuses only successful,
recent, identity-matched cache entries. A daily source bundle is frozen before
screening; successful additional volatility history is frozen before staging.
Resumes reuse accepted observations, including after a worker interruption.

Required source failures degrade the affected strategy/horizon books while
preserving independently valid accounting. Candidate quarantine, staging
completion, staging validity, input coverage, and accounting validity remain
separate. Deterministic JSON and Markdown reports reconcile all attempts,
strategy health, accepted inputs, candidate issues, and all 16 ledger books.
Earlier preflight incidents remain visible after a successful daily attempt.

## Evidence and test boundaries

`tests/test_source_reliability_pipeline.py` exercises all 12 production
strategies, all 16 books, and the 48 strategy/horizon health records. Only
HTTP/SDK transports and model responses are fixtures. Source adapters, screening,
health persistence, ledger execution, staging, the V2 worker envelope,
GenerationManager attempt archival, and operational reporting execute normally.

The fixture bridges the manager's subprocess call into the real worker in the
same process so transport fixtures remain active. It checks the inherited lock
descriptor but does not prove operating-system process isolation. The separate
`test_run_evidence_subprocess.py` suite exercises real subprocess evidence.
The incident harness blocks external sockets and counts attempted forbidden
provider calls, including exceptions that adapters might otherwise catch.

The 13 campaign cases cover:

- Healthy empty-source controls and an actual NVDA earnings candidate.
- EDGAR HTTP 500 and 429 recovery, exhausted timeout, malformed HTTP 200, and
  usable partial results with explicit impaired coverage.
- Incoherent and unsupported SIP candidate bars with durable quarantine.
- Duplicate invocation and next-session fills in every book.
- Interruption after partial staging, timeout evidence archival, and a clean
  resume with unchanged accepted inputs and no additional fills or fetches.
- Missing or corrupt volatility bundles after partial staging, including an
  interruption before the first book completes. These block resumed staging
  without reacquisition or changing completed accounting.

Focused regressions also cover malformed numeric series, provider limits and
Retry-After, immutable publication, cache identity/freshness, incomplete source
scope, missing source observations, later resting-stop fills, and a quarantine
whose only affected horizon already completed before a resume.

The earlier tests did not cover these boundaries together. Some replaced whole
fetch functions or used synthetic strategy rosters, so they could not expose
error-to-empty behavior inside an adapter. Healthy health-row fixtures also
missed the distinction between shared metrics and cohort-local ledger tables.
The full interruption campaign exposed volatility history that was accepted for
staging but persisted only in memory. Independent review added regressions for
report completeness and the earliest accepted staging context.

## Verification commands

```bash
python -m pytest -q -m 'not live' tests
python -m pytest -q tests/test_source_reliability_pipeline.py
python -m compileall -q tradingagents scripts/generate_operational_report.py
bash -n scripts/daily_trading.sh
git diff --check
```

The October 6 integrated non-live suite passed 2,638 tests with four live tests deselected
and one existing websockets deprecation warning. The acceptance campaign passed
all 13 cases; independent review refreshed 226 focused checks with no remaining
blockers. Compilation, shell syntax, and whitespace checks passed. PR checks
are recorded on the published pull request. No live provider calls or production
changes are part of these offline verification claims.

## Operational contract

`paper_ledger.pricing_version=raw-alpaca-sip-v1` is the current default. Supply
Alpaca credentials and request SIP/raw only after the exact XNYS session's close
plus 15 minutes. Missing or invalid governed prices block execution. Yahoo still
provides corporate actions, dividend-adjusted benchmark closes, research
history, volatility, and VIX; their failures remain relevant.

Provider calls use at most three attempts within the shared acquisition budget.
Authentication, bad-request, and payload-contract failures are terminal.
Provider pacing is process-local. An in-flight SDK call that lacks cancellation
cannot be forcibly stopped; the deadline prevents subsequent attempts and the
fanout caller can reject overdue work. These checks do not establish future
provider availability or diagnose an upstream outage's cause.

The operational cache lives outside Git at `data/source_cache` for managed
workers, with a maximum five-minute TTL. The immutable accepted bundles live
under each generation's state directory in `source_inputs/`, including
`source_inputs/staging_volatility/`. Corruption or missing accepted staging
evidence is an error, not permission to refresh the session's inputs.

`scripts/daily_trading.sh` generates reports on exit, including after a governed
gate or daily failure. To regenerate a report without trading, while the runtime
is idle:

```bash
python scripts/generate_operational_report.py --repo-root /path/to/repo \
  --all-active --date YYYY-MM-DD --output-dir /path/to/repo/docs/reports
```

The command holds the existing shared runtime lock, opens SQLite read-only, and
writes `YYYY-MM-DD-gen_NNN-operational-report.{json,md}`. Exit 0 means a complete
clean or degraded report; exit 2 means failed or incomplete evidence. Reports
describe separate scenario books and distinct entry/exit fills. They do not
calculate a combined fund return or interpret exit fills as fully closed lots.

## Release boundary

This behavior change needs a new generation. A topic-branch PR is not a
deployment, and no old observations or ledgers are rewritten. Follow the
repository's explicit production authorization and managed release procedure.
Do not replay trading to fill evidence gaps.

The companion monitor contract is maintained in reviewed private configuration.
It requires these generated reports: release and verify EventEdge first, then
install the approved monitor files through the native Hermes update flow.
Missing or stale reports alert. Preserve the existing schedule and delivery.

Offline tests prove the tested contracts, not prospective continuity or trading
profitability. Observe actual subsequent sessions after an authorized release.
The separate Clef/Jev assessment proposes a shadow evaluation; this release
does not change models, prompts, or trading decisions based on those models.
