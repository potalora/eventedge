# Bounded SEC Form 4 qualification

The prospective `complete_ownership_xml_v1` path qualifies a declared issuer/form
subset without enabling SEC research or changing production. The existing
`disabled_v1` provider policy and its three excluded strategies remain unchanged.
Legacy Form 4 extraction, request truncation and frozen records keep their old
behavior; the new path is explicit and journal-only.

## Implemented evidence and consumer contract

Every accepted XML original retains its exact bytes/hash, actual observation
clock, all reporting owners, source field paths, transactions, holdings, security
titles, printed numeric literals, transaction dates, derivative context,
footnotes and field-to-footnote links, remarks and the 10b5-1 flag. Source replay
reconstructs the complete semantic view and field inventory from those bytes.
Unknown schema capabilities, invalid familiar-tag placement and unfamiliar
relationship booleans remain explicit unsupported findings.

Admission requires an original Form 4, one reporting owner, an exact reviewed
issuer/security-title/source binding, the matching native trading symbol, and
supported non-derivative purchase/sale values. Numeric admission uses bounded
ASCII decimal syntax and finite positive projections, gross values and group
aggregates. Native strings remain retained even when a row is excluded. Any
Form 4/A in an issuer window holds that issuer's window pending amendment
resolution. Joint reporting owners and unresolved classes remain excluded.

Purchase/sale coding does not establish exchange execution. The consumer gets
all relevant original filing context, without the legacy five/ten-row excerpts.
Qualified buys report an economic purchase count and unknown exchange execution;
they receive no open-market score premium or open-market purchase-count claim.
The legacy branch retains its earlier scoring and metadata. A synthetic positive
buy-context regression verifies both branches and complete request preparation.
The actual engine rebinds candidate context and display rows against frozen
shared source evidence and, when supplied, the equity universe before calling
the analyzer. The prospective prompt permits disclosed plan facts and forbids
unsupported modification, termination, cooling-off and news-timing claims.
Qualification candidates retain `journal_only=true` and no trading authority.

## Exact public regression originals

`tests/fixtures/form4_evidence/manifest.json` records original URLs, observations,
hashes and sizes. The three XML files have no modifications or redactions.

| Original | Verified scope |
| --- | --- |
| Apple `0001140361-26-040127` | 23,004 bytes; eleven transactions, including five stock sales and four derivative rows; eleven footnotes; positive 10b5-1 flag; linked February 6, 2026 adoption statement |
| Apple `0000320193-22-000078` | 5,992 bytes; actual Form 4/A; two transactions; original-submission date August 19, 2022 |
| Berkshire/LEN `0001193125-26-414744` | 35,817 bytes; two reporters; fifteen transactions across Class A/Class B common stock; eighteen footnotes; filing date October 5, corroborated by retained issuer submissions metadata |

Synthetic in-memory controls are identified as such. They cover malformed and
misplaced fields, unresolved/duplicate footnotes, derivative holdings, unknown
capabilities, date/issuer/class faults, float underflow/overflow, aggregate
overflow, non-XSD numeric literals and forged self-consistent candidate context.

## Native bounded acquisition and immutable replay

The final October 23 `scripts/qualify_sec_form4.py capture` command acquired
Apple's complete declared October 19 Form 4 window using one submissions response
and two native XML documents. It used the existing `EDGARSource` default because no
`EDGAR_USER_AGENT` was configured; the CLI supports that environment variable or
an explicit override. No contact/header value was printed or changed.

The prospective caps were four HTTP attempts, a shared sixty-second source
deadline, 4 MiB per body and 16 MiB aggregate source bytes, with a ninety-second
outer shell timeout. Actual acquisition used **three requests and 205,472 bytes**.
The three body-completion clocks span 0.433300 seconds; this span is not the total
qualification/replay duration.

Both discovered originals were retained: `0001140361-26-040126` has nine
transactions/nine footnotes; `0001140361-26-040127` has eleven/eleven. Only the
second original had a prospectively reviewed source/class binding. Its five
stock sales were admitted for context and generated one journal-only request
containing all eleven transactions and eleven footnotes. The other original's
rows remain excluded with unresolved-class findings.

The native receipt binds eight executed source-file hashes, complete metadata
and XML originals, exact discovery/qualification results, source-codec roundtrip,
candidate context and the complete prepared request. There were **zero model
calls**. This verifies request preparation, not model correctness.

Capture and independently executed offline replay are byte-identical:

```text
capture: /var/tmp/eventedge-sec-form4-qualification-20261022/native-capture-v2.json
replay:  /var/tmp/eventedge-sec-form4-qualification-20261022/native-replay-v2.json
file SHA256: 794b2c5256767fb2f5ac13a777c11b8e73eece207e33eb491af571fd0301cc6e
qualification receipt: b4c1f6f1b4a7cfc2d7e4d60b98b4a3397561a6f03386cc7f1fac1a9816607597
insider_activity.py: 87080a482f7ad9c6bcdccfc062337c32093696f2f94092f27a8fe13c5938bc37
```

The earlier October 22 v1 capture/replay remains retained with file SHA256
`fa06891258803e5e27ffed816effc431c703e34fe5a8aee47c6c1d60c3640dbb`.
It predates the qualified-buy presentation repair; v2 binds the repaired code.

The independent reviewer verified that file hash and reproduced the receipt
with HTTP disabled. Replay recomputes discovery from complete native metadata,
reparses original XML, requalifies every row and reconstructs consumer requests.
It requires the independently retained file hash and current bound source-file
hashes. Receipt creation uses exclusive mode-0600 files; replay checks a 32 MiB
read limit before parsing and rejects duplicate JSON fields.

## Verification and limits

The four new test modules passed **57 checks in 6.64 seconds**. The combined new
and existing source, disabled-policy, event-identity and codec selection passed
**226 checks in 8.07 seconds**. Initial missing implementation, numerical,
future-window and source-binding regressions were observed failing before their
repairs. Independent review reproduced the numeric and future-window fixes and
passed the preceding 56 checks, then replayed the preceding native capture without
HTTP calls. The final qualified-buy regression was also observed failing before
repair: an unjustified 0.30 score became 0.15, while legacy remained 0.30.
The final independent review passed eleven consumer checks in 2.31 seconds,
verified the v2 capture and repaired source hashes, and reproduced v2 with HTTP
prohibited. It found no remaining blocking issue in the qualified-buy delta.

This is a declared issuer/window qualification, not whole-market research,
amendment-chain resolution, joint-owner allocation, broker/SIP eligibility
certification or investment accuracy. Fresh metadata and old filing bodies carry
their actual acquisition clocks (October 23 for the final v2 capture); no
historical decision-time availability is claimed. Structural-only
annual/material-event HTML remains
barred from complete-filing model assessment; bounded page research is separate.

The command read public SEC endpoints and wrote isolated `/var/tmp` artifacts.
It has no production checkout, state or service integration. No policy profile,
generation, production checkout, service, timer or production state was changed.
This work is not deployed.
