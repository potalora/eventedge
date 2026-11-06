# Supplemental enrichment monitoring

Optional enrichment can fail while accounting and staging finish safely. The
source incident and automatic adapter-repair contracts cover accepted screening
sources; they do not cover FINRA short-interest enrichment. A healthy runtime
controller therefore does not establish complete enrichment or paper continuity.

The trusted `scripts/source_repair.py interventions` refresh now also publishes
`docs/reports/source-repair/<session>-<generation>-enrichment-monitor.json`.
This separate `eventedge-enrichment-monitor-v1` projection leaves the original
operational JSON/Markdown and accepted inputs unchanged. Its result appears in
CLI stdout under `enrichment_monitors`. It cannot enqueue a repair or change the
source-provider allowlist, trading decisions, completion policy or incident state.

Before publishing a complete or degraded observation, the reader checks the
frozen generation/profile identity, source envelope and its canonical projection,
metric epoch, accepted screening and decision inputs, eligibility binding and
all sixteen native committee, accounting and staging-clock bindings. It reuses
`operational_report._accepted_enrichment_scope`; committee summary bits alone
cannot establish enrichment health. Invalid or missing evidence yields
`unavailable`, never recovery. Database reads use the existing read-only proof
path. Verification uses disposable state or an explicitly isolated snapshot.

The sidecar binds the exact canonical report-pair digest and accepted enrichment
digest. Bounded `healthy_scope` rows identify successful operations and symbols
from the validated selected facts. Fixed failure rows identify provider, operation, symbol, reason and an
explicitly retained HTTP status, if any. FINRA uses provider `finra` and operation
`short_interest`; other supported optional operations retain `openbb`. Private
error text, source URLs and native cache paths are excluded. It reports observed
`workflow_complete` and `continuity_eligible` separately, and always declares
`automatic_repair_supported=false`.

Validated FINRA acquisition schemas 1 and 2 remain readable without mutation.
Schema 3 adds a separately labeled post-download HTTP probe. Its later response
is diagnostic evidence, never the original downloader's HTTP status, archive
validity or a successful recovery. Earlier failed attempts do not invalidate a
later successful terminal acquisition. A terminal error contradicting a claimed
complete optional scope is unavailable rather than silently healthy.

The producer derives `valid_from` from the exact XNYS session close and
`valid_until` from the next XNYS session close. The native consumer checks its
actual aware clock against that trusted interval. Holidays, weekends, early
closes and DST use the same exchange calendar as the application; the native
consumer does not invent another holiday calendar. A future or expired complete
sidecar cannot close a retained notice. No interval may exceed ten days.

The separately managed native Hermes consumer reads this schema through its
existing intervention poll and sends one bounded capability notice using its
verified existing route. Separate private receipts deduplicate unchanged
failures across polls and session rollover. An uncertain send remains pending
and cannot be retried or cleared by a later success. Only a later positively validated complete observation covering every previously
failed operation and symbol resolves a confirmed notice. No-candidate sessions,
different symbols and missing or invalid evidence cannot resolve it. FINRA
recovery additionally requires a schema-2/3 terminal acquisition with current
archive coverage; schema-1 and cache-only receipts remain unqualified for that
recovery claim. Prior failure scopes remain retained across later different
failures. Research holds and unspecified failure scopes remain for operator
assessment until a relevant positive-proof contract is available. Implicit legacy generations without a frozen
configuration are outside this supplemental observer's scope.

Releasing these observer changes requires matching application runtime and native
consumer versions. The immutable observer runtime can read an unchanged active
generation at its recorded commit; installing that runtime is separate from
changing generation code. Changes to FINRA acquisition code remain prospective
unless separately approved under the generation deployment policy. Never rewrite
accepted sessions or delete repair/notification receipts to make an alert clear.
