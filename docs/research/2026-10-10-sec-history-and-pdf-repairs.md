# SEC history and supplemental-PDF compatibility repairs

Two captured HTTP 200 responses exposed narrow validation defects. Both original failed observations and complete raw captures remain retained; the repairs below were verified offline and are not a new source run or deployment.

## Legacy history with missing filenames

Pillarstone CIK `0000928953` returned 346 valid filing identities. Rows 297–345 have literal empty `primaryDocument` strings; the first is accession `0000912057-00-023981`, form `10QSB`, filed `2000-05-12`. The validator previously rejected the entire history because it required a nonempty filename.

`_history_rows` now preserves a literal empty string as missing metadata. It still validates every parallel array, accession, form, date, duplicate identity, and every nonempty filename. No filename, path, or filing is inferred. The complete-submission route remains accession-bound, and a predecessor still requires its actual complete body before evidence is available.

Offline replay preserves all 346 identities and all 49 empty fields. Under the unchanged nearest-strictly-earlier exact-form rule, the current annual report selects accession `0001437749-26-027474`, filed `2026-08-13`. The three current quarterly reports remain `ambiguous_prior_date` because three quarterly predecessors share that date. This repair does not choose among them or alter initial-report policy.

Captured history SHA-256: `cbb093da7fbe18938058f4f18ad21510c27608fb01f9b896daa6322c0d7bf0ab`.

## Official narrative followed by an unofficial PDF

Carnival accession `0000815097-26-000107`, filed `2026-09-29`, contains 56 documents. Two carry type `10-Q`: sequence 1 `ccl-20260831.htm` and sequence 12 `pdfofform10q.pdf`. The parser previously rejected this as an ambiguous primary document. Independently retained SEC submissions metadata identifies the HTML filename as this exact accession's primary document.

SEC's `INVALID_UNOFFICIAL_PDF` rule requires an official ASCII/HTML document before its supplemental PDF attachment. Official-PDF exceptions do not include primary 10-K, 10-Q, or 8-K reports. [SEC error-message guidance](https://www.sec.gov/submit-filings/filer-support-resources/how-do-i-guides/understand-messages-reported-edgar), [SEC filing-format restrictions](https://www.sec.gov/submit-filings/filer-support-resources/how-do-i-guides/observe-data-process-filing-limits).

For those three form families and their amendments only, multiple same-form entries can now resolve to exactly one `.htm`, `.html`, or `.txt` document when every other entry is a `.pdf` with one complete native `<PDF>` wrapper and follows it in both physical document order and sequence number. Multiple text candidates, earlier PDFs, unknown representations, malformed wrappers, and other form families keep the ambiguity failure. All earlier identity, framing, count, role, size, filename, and sequence checks remain in force.

This identifies the official representation without asserting PDF content equivalence. All complete-submission bytes, all 56 inventory entries, hashes, and byte offsets remain intact. An explicitly required PDF still causes insufficient evidence under existing document-format/size checks. Offline replay selects the official HTML and four linked exhibits; model adequacy and dependency assessment remain `not_assessed`.

Captured submission SHA-256: `47652b671ffbc0caa2a15f1d51bad7dc587f2d179b051d517de1360c5735ba91`. Corroborating history response SHA-256: `f82d9bc721785d7e2ed4a11e4d54d6ffda7148c37bd8c42f23065955843002f0`.

## Verification

New regressions cover missing metadata versus malformed values, exact accession predecessor acquisition with an absent filename, unavailable predecessor bodies, unchanged same-date ambiguity, accepted form/representation boundaries, all rejected PDF order/wrapper cases, complete inventory/hash/offset retention, and explicitly required PDFs remaining insufficient. The new tests failed before their respective repairs.

```text
.venv/bin/python -B -m pytest -q tests/test_filing_unofficial_pdf.py tests/test_sec_history_missing_document.py tests/test_filing_evidence.py tests/test_filing_hydration.py --tb=short
```

Result: **166 passed in 1.13 seconds**. Full captured response replay separately verified all 346 history identities and all 56 Carnival document spans/hashes. No new network request was used for those replays; only the linked public SEC documentation was consulted online.

The combined repair, including guarded ASCII DOM reuse, subsequently passed the complete offline suite: **4,509 passed**, four live tests deselected, one existing dependency warning, in 194.20 seconds. Independent review passed 326 focused checks, including 34 additional adversarial and retained-response controls, without an actionable finding.

## Fresh native verification

The committed repaired runtime `9f339ff8a836c0dc6682d6c7f67516ea71f8f4a1` subsequently passed CI: **4,509 tests**, four live tests deselected and one existing warning, in 598.31 seconds. A separately supervised native diagnostic made exactly three fixed SEC requests, with one attempt each and unchanged 120-second worker / 150-second whole limits. All three bodies were byte-identical to the original diagnostic, and all three current native validators returned successfully.

Pillarstone returned all 346 identities with the 49 empty names preserved. Carnival returned all 56 document inventory entries and five full supported units. The Saratoga control preserved its complete native evidence after only a comparison-time projection of `observed_at`; both stored observations remain untouched. Its build time fell from 10.649 to 5.593 seconds on these exact bytes. Collection took 8.949 seconds and supervised closure 22.821 seconds. The supervisor independently replayed all three native validations without acquiring data.

Root independently verified all 165 original closed files and the complete archive (`81ddd2c2a7a6858f58c49a9b13165334d8c9ea14231b7cec203b49f0f458b57e`). All 153 approved files and 1,209 production files remained unchanged; the owned process group was empty. This verifies the selected repairs and selected-body performance. Complete source acquisition within 600 seconds, the missing/ambiguous predecessor cases, model throughput and full end-to-end acceptance remain separate unresolved requirements. Nothing was merged or deployed.
