# Assumption-audit repair acceptance

This change repairs the 9 October candidate audit and fixes the portfolio/research conventions before a new prospective experiment. The base is `5611ad8`; pending PR #49's congressional-disclosure repair is included. No historical ledger, production generation, service or schedule is changed. These output-affecting changes require a new generation and metric epoch.

The [forward-test protocol](2026-10-09-forward-test-protocol.md) fixes the $100k three-month book as primary, treats the other books as dependent sensitivity scenarios, and distinguishes operational continuity from evidence of alpha. The implementation does not optimize thresholds against past P&L or enable automatic learning/promotion.

## Finding-to-regression map

Audit IDs are qualified by component because the source and strategy audits each use `S` numbers. Tests exercise provider-shaped transport payloads, durable ledger state and native compositions; existing fixtures were corrected when they assumed unavailable evidence or obsolete semantics.

| Audit findings | Implemented contract | Principal regression coverage |
|---|---|---|
| Accounting ACC1–2, ACC6 | Durable net daily loss; exact opening valuation after splits; prospective fees/slippage in cash, equity and all held/pending policy weights | `test_execution_audit_repairs.py`, portfolio-policy execution invariants |
| Accounting ACC3, ACC7 | Explicit action absence; splits before post-split per-share distributions; repeated action IDs cannot inflate opening equity | Execution audit, corporate-actions and ledger acceptance tests |
| Accounting ACC4, ACC8 | Delayed short resume uses accepted market clock; ACT/365 carried costs; durable configured ticker cooldown | Execution audit, short-risk and recovery suites |
| Accounting ACC5 | Opposing signals cannot manufacture agreement or short conviction | Strategy audit and committee/short-gate tests |
| Accounting ACC9 | Compare takes the canonical shared lock and reads temporary DB/WAL snapshots without creating or changing source state | `test_generation_manager.py` comparison cases |
| Accounting ACC10 | Untraded next-open outcomes apply continuously evidenced splits and distributions, with explicit gross shareholder-return basis | `test_metrics_audit_repairs.py`, native multi-session outcome tests |
| Strategies S01 | Immutable late observations enter the first eligible session's normal candidate checks; one timely offer per event; original facts and model output retained despite reacquisition or moving windows | Strategy audit COT/weather cases; 16-book moving-weather-window and replay test in `test_source_reliability_pipeline.py` |
| Strategies S02–3 | Full response validation before candidate mutation; exhaustive required analysis dispatch; individual failures isolated; required text/analysis absent means journal-only | Strategy audit, LLM boundary and enrichment integration tests |
| Strategies S04–5 | Direction-aware strategy exits; missing/mismatched COT evidence cannot imply normalization | Strategy audit, short exits and commodity tests |
| Strategies S06–7; Sources S01–2 | No occurrence-only WARN inference; real SEC primary document text; Form 4 A/D and transaction identity retained; only verified nonderivative open-market transactions qualify; distinct owners form clusters | Source and strategy audit tests, including native XML → screen derivative exclusion |
| Strategies S08–12 | FRED Series/dict normalization; percent inflation and month-aligned real rates; exact three-month baseline; native catalyst keys; unknown regime preserved; distinct filing events retained | Strategy audit, commodity, regime and event-identity tests |
| Sources S03–6 | Disjoint drought categories; regional weather days; full growing-season frost dates; complete rolling COT history | Source audit and provider semantic/numeric contracts |
| Sources S07–9 | Exhaustive EFTS/USA windows under one budget; explicit bounded per-issuer Form 4/Court/Regulations/Congress scope; FRED vintage; observation and acquisition clocks separated; unsupported fresh historical reconstruction refused | Source audit, frozen coverage → strategy-health compositions, live-shape offline contracts |
| Sources S10–11 | Unsupported futures-curve inference retired; ETF price-return cache binds label/ticker universe and declares its return basis | Source audit and adapter tests |
| Metrics M01–2 | Immutable adjacent same-vintage SPY/BIL pairs chained into a total-return index; old unpaired history remains unsupported; full scoped mature outcome population | Metrics audit, portfolio/epoch/service tests; >1,000-outcome case |
| Metrics M03–5 | Adjusted descriptive event studies with exact exchange-session windows including the estimation gap; unsupported IID inference withheld; accurate insufficient-history/zero-variance reasons | Metrics audit, event-study and dashboard/email/report tests |
| Clef C01–3 | Atomic evidence claims; explicit six-family scope and coverage; provider-bound untruncated evidence; conflicting claim variants preserved; no trading dependency | Strategy audit, shadow and native pipeline tests |
| Legacy L01–2 | Prompt overrides reach actual analyzers; direction-signed outcomes are interpreted once | Strategy audit and journal failure tests |
| Orchestration ORCH1–4 | Canonical lock around every lifecycle mutation; malformed manifests preserved and rejected; New York default session dates; supported entrypoint and corrected docs | Generation-manager, calendar, shell-preflight and CLI checks |

## Additional blind spots and availability repairs

| Finding | Implemented contract | Focused regression |
|---|---|---|
| B1/B2: inventory and protection across gaps | Effective-dated action catch-up for held inventory, exactly-once split reconciliation, standing stops retained, missed next-open intents expired; no fabricated gap-day fills | `test_inventory_continuity.py`, native skipped/invalid session controls |
| Dividend cash timing | Signed ex-date receivable/payable contributes equity; cash waits for verified payment date; unknown entitlement cannot finance orders | `test_inventory_continuity.py`, long/short phase-crash settlement cases |
| B3/B4: committee semantics and evidence | Successful empty response abstains; invalid/error response degrades and holds cash; all admitted theses and marked positions visible; immutable decision before staging | `test_decision_visibility.py`, `test_decision_visibility_pipeline.py`, partial staging replay |
| B5: hidden sampling | Deterministic discovered/admitted/excluded manifests for bounded screens and Finnhub acquisition, including article limits | Decision visibility and acquisition admission regressions |
| B6: modern ownership filings | Modern/legacy base queries include amendments, accession identities retained, subject verification still required | `test_provider_meaning_contracts.py` |
| B7: old award edits as new wins | Base obligation window and native award identity; amount explicitly means observed cumulative obligations on a newly originated award | Provider meaning and USAspending source tests |
| B8: unsupported wheat geography | Declared crop/class/state comparison scope and season; full current active coverage; explicit inactive classes | `test_environmental_contract_repairs.py`, USDA and source reliability pipeline tests |
| B9: opinion identity | Cluster, docket and nested opinion identities remain distinct | Provider meaning contracts |
| B10: misleading diagnostic populations | Immutable analysis/journal eligibility carried to outcomes; provisional, validated, selected and executed populations with explicit missingness | `test_population_diagnostics.py`, cross-epoch retained-obligation reporting |
| NOAA/USDM availability | Complete catalog/inventory bulk NOAA acquisition inside 90 seconds, measurement-quality quarantine, native state FIPS/camel-case drought response | Environmental contracts, source audit and native source reliability pipeline |
| Outcome-only global failures | Independent immutable per-ticker/session outcome acquisition; only entry/maturity prices plus continuous actions; healthy books/outcomes survive unrelated diagnostic gaps | `test_outcome_availability.py`, 30-day replay/critical-gap cases |
| Research interpretation | Cost stress, realized contribution concentration, lagged exposure attribution, dependence-aware uncertainty and explicit empirical-evidence limits | `test_research_diagnostics.py` |

The execution model settles prior open exits/gap stops before opening entries, then intraday stops. Known carrying charges and entry costs affect admission; marked short collateral blocks further exposure on deficiency. This is a conservative daily-bar paper model, without locates, broker liquidation, intraday trajectories or option-accounting claims.

## Explicit exclusions and availability limits

- State economics is `disabled_by_policy` with its declared retirement reason and zero signals; coverage/readiness require that exact classification. Contractor momentum, filing-occurrence layoff/long proxies and inferred futures curves are removed. Schedule 13D/G subject attribution remains journal-only when unverified.
- Form 4 is a bounded issuer sample, not an exhaustive market feed. Its per-issuer limit, counts, date scope and possible archived records survive freezing and reach health evidence. Other intentionally sampled feeds disclose their scope too.
- NOAA uses the latest fully covered contiguous lookback window ending within seven calendar days of acquisition date, without imputing missing observations. COT, NOAA, drought and USDA cannot reconstruct unsupported historical vintages from today's source. Accepted frozen evidence remains reusable.
- Outcome-only acquisition failures stay scoped to their diagnostic obligations. The first successful or failed acquisition remains immutable; stored replay never refetches absent historical evidence. Portfolio-critical failures retain precedence even when later acquisition steps also fail. Prior-epoch obligations remain visible without endorsing invalid portfolio performance.
- Earlier raw signal returns and independently adjusted benchmark observations retain their original labels. Unsupported historical benchmark comparison is explicitly unavailable. The repair does not relabel old results as current-method performance.
- Clef supports source-claim assessment for earnings, filings, regulations, peer news, litigation and awards. Other families remain explicit unsupported coverage; support probabilities are not probabilities of profit.

## Verification

Final command: `.venv/bin/python -m pytest -q -m 'not live' tests -o faulthandler_timeout=30 --tb=short --show-capture=no`: **3,076 passed, 4 live tests deselected, 1 existing websockets deprecation warning**, in 113.50 seconds. Python compilation, shell syntax, supported CLI help, TOML parsing and diff checks passed. The suite adds 122 cases over the earlier audit candidate, with parameterized boundary checks and existing native fixtures.

Independent economic and source/decision reviews have no unresolved blocker in their assigned scopes. Their adversarial reproductions prompted fixes for late historical diagnostic acquisition, governed failure loss after action-fetch exceptions, invisible prior-epoch obligations, ungrounded committee output, and nonfinite admission ordering. Fresh regressions verify each correction. Final integration also preserves USDA reporting-year validation before future-row filtering and keeps acquisition metadata from falsely truncating shadow source evidence. The native 16-book suite covers successful abstention, model failure, unsupported recommendations and interrupted staging with providers blocked on replay.

Bounded read-only candidate checks on 9 October verified the repaired provider contracts: NOAA returned a complete ten-state, 30-day window ending October 7 in 70.83 seconds, with 7,877 selected inventory stations, 201,250 usable measurements and 37 quality exclusions. USDM returned all ten states for October 6. USDA returned 197 corn, 192 soybean and 227 wheat observations; current corn/soy scopes were active and complete, while all wheat classes were explicitly inactive on October 9. These are acquisition snapshots, not availability guarantees.

SEC base-name searches returned modern initial/amended forms (204 13D hits and 531 13G hits in the bounded check's window). One concurrent 13G request received HTTP 500; a separate bounded follow-up passed. USAspending returned 50 new-award records, and CourtListener parsed 20 opinion clusters with nested identities. Those one-page probes validate response semantics, not exhaustive market coverage. Provider-shaped offline regressions cover full-window completeness, errors and replay. No production state or trading was touched.

The [forward-test protocol](2026-10-09-forward-test-protocol.md) records the full economic and acquisition policies, prospective research readouts and empirical evidence still required to assess alpha.
