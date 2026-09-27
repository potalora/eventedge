# USDA request and weekly-condition audit — 2026-09-26

The September 25 daily run completed cleanly, but its USDA warning was caused by malformed requests rather than a demonstrated provider outage. Production was inspected read-only on `hermes-vps`, verified as `vps-2a10b9f4`; no scheduled run or ledger was modified.

## Reproduction

Bounded authenticated GET probes used the existing credential without printing credentials or request URLs. All requested corn CONDITION/WEEKLY data for 2026:

| Request difference | HTTP | Result |
| --- | --- | --- |
| Production parameters: comma-separated states, unit `PCT OF AREA PLANTED` | 500 | HTML error |
| Single state IA, same unit | 400 | `bad request - invalid query` |
| Single state IA, unit `PCT GOOD` | 200 | 18 rows |
| States `IA,IL`, unit `PCT GOOD` | 500 | HTML error |
| Repeated state parameters IA and IL, unit `PCT GOOD`, STATE aggregation | 200 | 36 rows |
| Single IA, no unit filter, STATE aggregation | 200 | 90 rows; all five condition categories |

The corrected full ten-state request was also probed for each configured crop: corn HTTP 200 with 885 records (latest September 20, ten states); soybeans HTTP 200 with 860 records (latest September 20, ten states); wheat HTTP 200 with 1,135 records (latest August 30, nine reporting states). Wheat separates WINTER, SPRING/DURUM, and SPRING/EXCLUDING DURUM classes; seasonal older observations are not replaced or relabeled.

The credential worked. The latest valid corn/soybean QuickStats observation was September 20. Condition records use `PCT EXCELLENT`, `PCT GOOD`, `PCT FAIR`, `PCT POOR`, and `PCT VERY POOR`; the original query requested a different unit while its parser expected these five.

## Fallback evidence and impact

The exact report loaded during September 25 was `https://esmis.nal.usda.gov/sites/default/release-files/796060/prog3726.txt`: 68,053 bytes, published September 14, observations for September 13. The ESMIS landing page still listed this as its first text link when checked September 26. Thus a successful download did not mean current weekly coverage: observations were 12 days old at the daily run, while QuickStats had September 20.

Its 18 corn state rows were internally complete (each five-category total was 100), but the fallback ignored the configured default ten-state region. It contained `Soybean Condition` (singular), which the plural mapping missed. Wheat condition sections were absent, consistent with the report's seasonal contents; no missing wheat values were manufactured.

A primary failure also set `_unavailable`, causing all subsequent commodities to return empty without trying the available ESMIS report. Finally, the weather gate interpreted the final two state rows as consecutive weeks. In the exact September 25 fallback, TX good+excellent was 35 and WI was 61; the erroneous calculation was -26, clamped to zero. **No false positive from that comparison was demonstrated for this run.** A synthetic same-week IA80/IL40 reproduction incorrectly produced a 40-point decline.

## Corrections and verification

- Encode states as repeated query parameters, request STATE aggregation and all condition units.
- Include normalized state scope in the cache key; apply the same default region to ESMIS.
- Continue ESMIS for other commodities after primary failure; correct singular Soybean/Peanut headers and reject another year's report.
- Preserve wheat class identity and compare matching state/class observations exactly seven days apart, using the latest observed week. Ignore missing, conflicting, undated, or invalid observations rather than compare different states or classes.

Eight new incident regressions failed against the original implementation before repair. USDA/weather focused suites passed after repair (85 tests, including additional class, malformed-observation, and source-to-gate completeness coverage). Existing fixtures were corrected to include the state/date identity supplied by the actual source. No live network calls were added to tests.

This change introduces no substitute data vendor, historical replay, or new fallback freshness threshold. A one-week ESMIS fallback cannot establish weekly decline; dates remain explicit. The publication mirror can still lag, so full primary/fallback parity and multi-session continuity must not be inferred from HTTP success alone. These output-affecting corrections require a new generation.

## Independent review follow-up

A further source-to-gate reproduction found that the pivot initialized missing condition categories to zero. With IA September 13 GOOD=60/EXCELLENT=20, but September 20 only EXCELLENT=20, it manufactured a 60-point decline. Invalid percentages and contradictory duplicate categories could similarly reach the gate. The pivot now requires explicit valid GOOD and EXCELLENT observations and rejects any conflicting or invalid category in that state/week/class group; it no longer fills missing fields with zero. The gate also rejects non-string or empty state/class identities before using them as mapping keys.

New regression selection: 13 failed and 4 passed before this follow-up; all 82 USDA/weather tests passed afterward. Tests cover missing/suppressed/malformed/out-of-range GOOD, duplicate ordering, and invalid state/class shapes.

A three-week follow-up also showed that simply removing an invalid newest group could silently reuse an older decline (September 6 to 13 after invalid September 20 was dropped). Invalid groups now retain their date/state/class boundary with `condition_valid=false` and no numeric values. The gate chooses the latest observed dated week before numerical validation. Three missing/conflicting/invalid-only newest-week regressions failed before repair and passed afterward; final USDA/weather suite: 85 passed.

## Analysis prompt consistency

Consumer review found the LLM prompt still used the last two raw rows and defaulted missing numbers to zero. An invalid September 20 marker after September 13 GOOD=60/EXCELLENT=10 became a fabricated `0% Good/Excellent (change: -70pp)` in the actual analysis prompt. Both numerical consumers now share `validated_condition_observations`: it preserves the latest observed dated boundary, matches state and crop class, and invalidates missing or contradictory numerical observations. The prompt names each latest state/class/week, explicitly says unavailable for invalid data, and reports a weekly change only for valid matching observations exactly seven days apart.

Four source-to-prompt/ordering/single-week regressions failed before this correction. Final focused command: `python -m pytest -q tests/test_usda_source.py tests/test_weather_ag.py tests/test_usda_prompt.py` — **89 passed**. No live provider calls occur in these tests.
