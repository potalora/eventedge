# CourtListener historical timeout review

Reviewed October 24, 2026 Pacific. **Cause unresolved; subsequent accepted
acquisition healthy; no verified production repair is justified by this evidence.**

The gen_025 October 23 screen ran from 3:00:06 to 3:01:25 p.m. Pacific and reported
one failed CourtListener operation, reason `timeout`, HTTP status unknown, after
three physical attempts. No screen runtime failure was recorded. The accepted
daily source envelope later recorded CourtListener success at 3:02:36 p.m.
Pacific. Its canonical report has no open source incident and correctly retains
`preflight_failure_requires_review`.

The exact failed request, exception subtype and any response body are absent
from the retained attempt. The evidence cannot distinguish remote latency,
network path, connection/read timeout or exhaustion of an aggregate time budget.
It establishes neither a provider-wide outage nor a code defect. Three physical
attempts are one screen failure observation, not three independent outages.

The [sanitized lesson proposal](2026-10-24-courtlistener-review.json) records the
original attempt's SHA-256, the later report hash, fixed metadata and an
`unresolved` classification. `operation` is `unspecified`, because the exact
request was not retained. `accepted_observation_id` remains null: the later
successful accepted acquisition is a different observation and cannot serve as
the identity of this failed preflight.

The proposal is for reviewed catalog integration on the matching source-learning
baseline. The main branch used for this change predates the deployed catalog
loader, so this PR retains the lesson alongside its evidence rather than
backporting that subsystem. No active catalog, canonical report, incident,
notification receipt, acknowledgement, source policy or generation was modified.
The historical live review flag remains visible.

Validation at 11:40 a.m. Pacific used the deployed revision's existing pure
catalog/report projection in a local checkout, with network access denied.
The proposed case passed catalog validation; adding it in memory left the
source-reliability projection identical to the canonical report. The successful
accepted-source observation and unbound preflight review diagnostic were
preserved, with no invented current attribution. Original evidence hashes were
unchanged. The deployed revision's existing source-learning and preflight
diagnostic regressions also passed: **67 tests**.

These checks made zero network or model calls and performed no production
mutation. They are supplemental checks against `e33cc9e`, separate from this
PR's main-branch test suite. No external retry, source substitution or newly
acquired provider response is part of this review.

Revisit only when stronger retained evidence or a new normal scheduled observation
changes the diagnosis. Any justified adapter repair needs a failing fixture,
focused regression and recovery evidence, reviewed Git changes, and separate
authorization before production mutation.
