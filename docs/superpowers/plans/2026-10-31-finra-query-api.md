# FINRA public query API and Congress acceptance

Pedro approved these two recovery recommendations on October 31. This work prepares reviewed changes; deployment requires a separate approved new generation.

## Contract

Use FINRA's official `consolidatedShortInterest` POST endpoint for the exact uncached symbol population and required published settlement. Preserve the reviewed publication calendar and normalized output. Bound symbols, batches, response bytes and retries within the inherited 60-second deadline. Reject redirects, incomplete bodies, duplicate rows and identity/date mismatches. Missing rows are explicit failures, never zero positions. Do not fall back to the blocked archive path.

Acquisition schema 4 records query population, exact accepted/missing/invalid symbols, publication context, request/row hashes and timing. It establishes only requested current-cycle coverage, with no historical-completeness claim. Keep schemas 1–3 readable and retain the historical adapter for explicit compatibility tests. Publish no new cache entries from an interrupted acquisition. Cache hits remain separately counted and cannot establish fresh recovery evidence.

Project schema 4 into the existing enrichment-monitor v1 envelope. Update the native consumer in a companion PR without changing durable notification identities. Recovery must intersect accepted API symbols with accepted enrichment facts. Preserve frozen replay receipts, historical errors and acknowledgements.

## Work and acceptance

1. Add transport and strict schema validation with failing tests, then prove bounded API acquisition and malformed-response rejection.
2. Integrate batch/scalar routing, cache admission, retries and monitor projection; verify actual committee replay retains its original receipt.
3. Qualify the twelve observed public symbols and a September 30 native control using isolated evidence. Run focused and broader integration tests, including legacy contracts and producer/native interoperability.
4. Open application and native-consumer PRs, documenting compatibility ordering: native consumer and observer first, then separately approved new generation. Do not merge or deploy.
5. Document Congress's next ordinary accepted collection and exact closure evidence. The October 31 upstream snapshot expires before Monday; Monday's November 2 window requires fresh publisher evidence. Preserve the current incident until production accepts that evidence.

The application branch depends on the reviewed source-recovery branch (PR #63). The root checkout and active generation remain unchanged.
