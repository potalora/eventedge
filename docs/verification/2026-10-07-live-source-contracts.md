# Live source and Clef contract verification

The first source preflight after the reliability release rejected congressional,
NOAA and USDA responses even though their HTTP requests succeeded. Credentialed
Clef validation separately returned a valid choice that our parser rejected.
These were adapter defects, not evidence that the providers were unavailable.

## Repairs

- Congress accepts validated non-public asset disclosures without a stock symbol
  and excludes them from tradable events. Public assets missing a symbol and
  malformed envelopes still fail. The two free-tier page limits are unchanged.
- NOAA follows response metadata through the complete observation set instead of
  stopping after five pages. The existing shared acquisition deadline remains in
  force. Repeated observations, stalled or inconsistent pagination, and more than
  100,000 observations per state remain explicit failures.
- USDA winter wheat can have a reporting year following its observation year.
  That narrow exception preserves actual dates and crop classes. Future years,
  older observations and prior-year records for other crops/classes are rejected.
- Clef confidence is a separate finite value in `[0, 1]`. Option probabilities
  still require valid bounds, a normalized sum and selection of a maximum.
- Native screen preflight results now retain fixed source/reason identities,
  failed HTTP status, attempt count and bounded operation count through the worker,
  archive and operational report. A semantic failure after HTTP 200 records a
  null failed status. Optional OpenBB remains outside required-source failures.

## Credentialed checks on October 7

The repaired modules were loaded from temporary review files on the verified VPS;
these calls did not modify the active generation or run trading. Each receipt
records the tested module's SHA-256. Provider acquisition used the same request
policy and credentials as the application, with no model calls for source checks.

| Contract | Result |
| --- | --- |
| FMP congressional disclosures | Two successful operations; 30 tradable records after excluding non-public assets |
| USDA crop conditions | Corn 197, soybeans 192 and wheat 227 complete groups; final narrowed date contract passed a second live check |
| NOAA agricultural weather | All ten states; 201 successful operations in 141.582 seconds; 169,953 temperature/precipitation observations contributing to the summary |
| Clef evidence shadow | Valid supported choice in 0.514 seconds; confidence 0.9029, chosen probability 0.9668; usage 250 input and zero output tokens |

The saved USDA public response also produced all 227 complete groups from 1,135
rows, including 16 legitimate prior-calendar-year winter-wheat groups. Regression
fixtures use small anonymized records rather than copying the disclosure feed.

## Verification and deployment boundary

Targeted regressions failed before each repair and passed afterward. Independent
review caught the overly broad USDA calendar exception; seven additional cases
now reject that defect while preserving the live sample. Review found no remaining
blocking issues. The final full non-live suite passed **2,760 tests**, with four
live tests deselected and one existing websockets deprecation warning. Python
compilation, shell syntax and whitespace checks passed.

Source acquisition and Clef smoke checks do not establish successful staging,
profitable decisions, calibrated model probabilities or future provider uptime.
The deployment procedure requires a new empty generation, exact frozen commit and
tree identity, actual native screen and governed preflights, unchanged historical
state, and restoration of the previously active scheduled entrypoints. No daily
trading replay is part of validation.
