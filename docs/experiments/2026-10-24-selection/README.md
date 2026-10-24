# Fixed-budget issuer and source-event selection experiment

This offline experiment increases selected symbol coverage for supply-chain
news and government awards on the retained October 22 and 23, 2026 sessions.
It does not establish better investment decisions. Award coverage comes with
a **48.14% decrease in the sum of raw screening scores**. Earnings membership,
selected source-event counts, and repeated evidence across sessions are unchanged.

**Not deployed.** No daily strategy caller, default, generation, source policy,
portfolio, model, prompt, service or timer changes. gen_025 remains at
`e33cc9eed38aa668dbde5f5e2844b7fbfb9391ac`. This proposal is an offline research
API and reproducible comparison; using it in daily analysis requires integration,
qualification, review and an explicitly authorized new generation.

## Results

The caps remain four earnings candidates, four supply-chain candidates and three
award candidates **per horizon**. Every recorded baseline is reproduced exactly:
two sessions × four horizons × three strategies = 24 replay cases. They are
**two overlapping retrospective sessions, not 24 independent samples**. The four
horizons produce the same selections, so this table shows each session once.

| Session | Strategy | Cap used, both policies | Eligible symbols, legacy → alternative | Native source groups, legacy → alternative | Screening-score sum, legacy → alternative |
|---|---|---:|---|---:|---:|
| Oct 22 | Earnings | 4 | AAL, AMTB, ASB, AVBC → unchanged | 4 → 4 | 3.221 → 3.221 |
| Oct 23 | Earnings | 4 | BAH, FCNCA, FLG, GRC → unchanged | 4 → 4 | 4.000 → 4.000 |
| Both | Supply chain | 4 | AAPL → AAPL, AMZN, BA, CAT | 4 → 4 | 2.400 → 2.400 |
| Both | Government awards | 3 | UNH → UNH, VVX, IBM | 3 → 3 | 2.500699 → 1.296775 |

Supply selection reduces the maximum symbol share from 100% to 25%; awards
reduce it from 100% to one third. The legacy award score is a capped function
of cumulative award obligations. The decrease shows that broader coverage
displaces larger awards; it is not a calibrated return or quality estimate.
Scores cannot be compared across strategies.

On October 23, all four selected supply hypotheses and all three selected award
hypotheses repeat their respective policy's October 22 retained content. This
policy does **not** solve across-session repeat selection or implement response
reuse. Earnings contributes four new fiscal issuer events under both policies.
No selected hypothesis in this sample has a changed retained-content fingerprint.
Different fingerprints would establish changed retained bytes, not a substantive
economic update.

The full supply populations contain 42 issuer hypotheses over 40 source groups
on October 22 and 43 over 42 on October 23. Some articles legitimately concern
more than one issuer. Both policies select four different source groups in this
sample; the event-awareness controls are demonstrated by adversarial tests,
not by an observed increase in event coverage here.

## Policy fixed before the comparison run

The audit had already exposed both sessions. They are development examples;
October 23 is not relabeled as held-out validation. No policy tuning or return
optimization uses these results.

`legacy_v1` reconstructs gen_025's ranking from the frozen admission manifest:
descending finite score, ticker, then discovery ID; government awards place
nonjournal hypotheses first. Only records originally marked `admitted` or
`analysis_budget` can enter either selection. All other original dispositions
remain excluded and are retained unchanged.

`issuer_event_v1` operates on that same population and cap:

1. Preserve nonjournal priority ahead of journal-only hypotheses.
2. Select the candidate whose ticker has appeared least often in this selection.
3. Among those, prefer the native source-event group that has appeared least often.
4. Break remaining ties with the legacy ranking, including its stable discovery ID.
5. Continue until the cap is filled or qualifying distinct hypotheses are exhausted.

This is a soft diversification order, not a one-event-per-issuer limit. Multiple
events for one issuer, one event affecting several issuers, opposing directions,
and changed content versions can all remain selected. Higher scores can yield
to symbol or source-event diversity; that tradeoff is deliberate and visible.

The alternative excludes an exact repeated source hypothesis only when event
key, ticker, direction **and a known content SHA-256** match. It records
`duplicate_source_hypothesis` and its representative instead of discarding the
record. Missing fingerprints never justify deduplication. Duplicate discovery
IDs are rejected rather than choosing one record by input order. These duplicate
rules do not remove any actual rows in the two retained comparison sessions.

Ticker is an **issuer proxy**, not consolidated legal-issuer identity across
share classes. Native source groups are fiscal issuer periods, news article
identities and award identities. Different article headlines or awards are not
merged because they mention the same company or theme. Substantive economic-event
grouping remains unmeasured. The implementation supports scalar nonnegative
candidate budgets for these three audited strategies; it does not add a daily
configuration option or per-direction policy.

## Evidence, clocks and acquisition boundaries

[selection-fixture.json](selection-fixture.json) preserves the full discovered
candidate populations, original admission reasons, listed-equity dispositions,
source identities, content fingerprints and clocks. Source, screening and decision
envelope paths and byte hashes bind it to the retained native evidence. Source
article text is hashed rather than reproduced; no balances, account identifiers,
model responses or private reports are included.

The projection keeps source publication, raw observation, envelope acquisition
and decision cutoff distinct. It checks that every known evidence clock is at
or before the retained decision cutoff. Acquisition is a conservative bound on
availability, not proof of first public availability. In particular, the October
22 session's retained acquisition took place on October 23; it is not backdated
to the session label. Source-content fingerprints exclude repeat-acquisition
clocks; exact definitions are recorded in the fixture's `evidence_hash_policy`.

Neither ranking uses subsequent sessions, portfolio outcomes, prices or returns.
Repeat metrics consult earlier retained sessions only, separately for each
strategy, horizon and policy. Held/pending-company relevance is explicitly
unmeasured because no verified predecision membership snapshot was established.
Present-day portfolio state is not substituted for historical context.

[acquisition-evidence.json](acquisition-evidence.json) retains the complete
upstream calendars and article-admission dispositions. Earnings acquisition
remains 314 → 10 on October 22 and 303 → 10 on October 23, followed by the same
four-candidate analysis cap. The alternative cannot recover evidence never
acquired. It makes no acquisition-policy change, additional provider request,
model request or live financial decision. These are equal **candidate caps**;
actual token use, retries, model latency and dollar cost of an alternative daily
run have not been measured. Government award screening is deterministic.

## Reproduce and inspect

From the repository root with the repository's Python dependencies installed:

```bash
python scripts/compare_candidate_selection.py \
  --fixture docs/experiments/2026-10-24-selection/selection-fixture.json \
  --output /tmp/eventedge-selection-comparison.json \
  --benchmark-output /tmp/eventedge-selection-latency.json \
  --repetitions 100
python -m pytest -q tests/test_research_selection.py \
  tests/test_selection_comparison.py tests/test_selection_fixture_export.py
```

[comparison.json](comparison.json) contains deterministic per-case results,
selected IDs, additions/removals, coverage and repeat metrics, and every original,
baseline and alternative disposition. The runner rejects any mismatch between
the reconstructed legacy selection and recorded admission. It also rejects
future or timezone-naive evidence clocks and duplicate cases.

[latency.json](latency.json) records 100 local selector invocations per policy
and case, alternating execution order. Timing includes validation and copying,
but excludes providers, models, portfolio analysis and trading. Machine load
affects timings; they are an engineering overhead check, not a trading-latency
or model-cost claim. The selector uses a simple quadratic greedy order for these
small populations; a broader universe would require a fresh performance check.
Across the recorded cases, the largest median was 1.765 ms for legacy selection
and 2.339 ms for the alternative; the largest observed p95 was 2.905 ms and
4.168 ms, respectively. These are local measurements, not service guarantees.

To regenerate projections from a retained **read-only** repository snapshot:

```bash
python scripts/export_selection_fixture.py \
  --repository-root /path/to/retained/eventedge \
  --generation gen_025 \
  --expected-commit e33cc9eed38aa668dbde5f5e2844b7fbfb9391ac \
  --session 2026-10-22 --session 2026-10-23 \
  --output-dir /tmp/eventedge-selection-inputs
```

The exporter supports this pinned deployed schema, validates envelope identities
and retained digests, and refuses output beneath the source repository. It reads
retained files only; it never imports or executes deployed strategy code.

## Acceptance and remaining qualification

Tests exercise input-order invariance, caps including zero, source exclusions,
journal priority, multi-issuer articles, later events for one issuer, opposing
directions, changed versions, missing fingerprints, exact duplicate dispositions,
malformed inputs, baseline mismatch, causal clocks and complete record retention.
Real-fixture regressions assert the observed membership and score tradeoffs for
all 24 cases. [Local validation](VALIDATION.md) records 64 passing focused tests
and the full non-live result: 2,822 passed and two clock-sensitive failures that
also reproduce on the unchanged base. The full suite is not green.

The most promising next evaluation is a frozen prospective supply-chain
comparison: symbol coverage increases here without sacrificing its equal input
scores. Awards need an explicit judgment about the observed coverage/score
tradeoff. Do not promote the common policy to all three strategies on these two
sessions. Preserve the policy before collecting new session results, report
window overlap, and independently assess evidence relevance and selected
interpretations under unchanged budgets. Add reliable predecision portfolio
context and substantive event/update adjudication before claiming those benefits.

Earlier generations have different revisions and source contracts; they were
not presented as independent held-out gen_025 evidence. No additional gen_025
session, prospective model comparison or return result is claimed by this PR.
