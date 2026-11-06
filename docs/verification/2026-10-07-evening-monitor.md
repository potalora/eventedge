# October 7 evening monitor investigation

The gen_019 daily run completed with valid accounting and degraded source
coverage. The monitor correctly reported the Congress failure, but incorrectly
called the preceding source preflight incident resolved by a later price
preflight. These are different checks.

## Verified evidence

Production and the frozen gen_019 worktree both identify commit
`5611ad84c60712671d9fe0240f7f5f20caaf473a`. Read-only copies of the report, four
exact-session attempts, accepted source bundles and Clef sidecar were checked
against their source SHA-256 hashes. The accepted source bundle's internal digest
also verified. No daily run was repeated.

| Attempt finished (UTC) | Mode | Result |
| --- | --- | --- |
| 15:47:35 | Source screening | Passed |
| 22:02:42 | Source screening | Congress semantic failure; USDA timeout |
| 22:02:46 | Governed price readiness | Passed; does not establish source recovery |
| 22:12:17 | Daily | Degraded; Congress unresolved, USDA successful, accounting valid |

The operational report discarded the archived `preflight_mode` when projecting
attempts and incidents. The repair retains and validates that field in both JSON
lists and labels every preflight mode and result in Markdown. Unknown or
conflicting modes make the evidence incomplete. No source-recovery status is
inferred from a successful price check.

## Congress disclosure scope

The accepted daily bundle retained the error
`batch_failure; house-latest:invalid_response`, with HTTP 200 for both chambers.
A read-only FMP probe at 23:36 UTC reproduced the rejection: three valid
`Government Securities` disclosures for Treasury/municipal bonds and one
`Other Securities` disclosure for an investment LLC had empty stock symbols.

The adapter already excluded validated symbol-free `Other` and `Non-Public Stock`
records. It now applies that rule to the two additional observed categories.
Dates, transaction type, amount, representative and asset description still must
be valid. Missing or ill-typed symbols on public or unknown asset types remain
visible failures. Page limits and request counts are unchanged.

Offline replay of the captured 50 records retains 26 stock disclosures, including
19 recent trades that match the accepted daily records exactly. The captured
response has SHA-256
`3ca5b366cf722b9d7b5ff39c5f3c6437421bf39ddf341205b602a136cb361038`.
Regression fixtures contain small anonymized instrument shapes, not the full
disclosure feed. Positive and negative cases exercise the native adapter, engine
health classification and source preflight.

## Other observations

HLSQ has 48 of the 61 required historical sessions, with 13 internal gaps; its
October 7 SIP bar was accepted. VIPZ had one rejected SIP response with no
accepted OHLC values. The retained evidence does not identify why its response
was rejected or why HLSQ's history has gaps. The two shared candidate issues
affect all 16 books and remain quarantined.

Clef assessed five of 32 unique events: two supported and three insufficient.
All five directions were neutral. One insufficient AAPL-linked item retained an
article about an Ascent Global Logistics appointment without an established
Apple connection. The bounded sample helps identify evidence gaps; it cannot
establish classifier accuracy or trading performance. Clef remains informational.

## Verification and release boundary

The report regression failed before the repair and passed afterward. Independent
review found no blocking issues and passed 158 focused tests, including validation
of all four captured native attempts. The final full non-live suite passed
**2,790 tests**, with four live tests deselected and one existing websockets
deprecation warning. Python compilation and whitespace checks passed.

The first full run also exposed two existing clock-dependent SIP tests. Their
fixed October 6 acquisition time exceeded the 24-hour freshness limit after
October 7 at 22:00 UTC. Both failures reproduce on `origin/main`; binding their
two completion-validation clocks to the fixture time repairs the tests without
changing production freshness enforcement.

This repair is proposed through a topic branch and pull request. It has not been
merged or deployed. No production checkout, generation, historical evidence,
accounting state or scheduler was changed during this investigation.
