# CFPB original-document qualification

`verified_government_copy_v4` admits the demonstrated CFPB mortgage-credit
request for information while retaining the complete official text and its
actual action, `Notice; request for information.` It does not infer an adopted
rule from the catalogue's `Proposed Rule` classification.

The parser binds agency, docket, Federal Register number/date/pages, both
printed CFR parts (`12 CFR Parts 1024 and 1026`), and the catalogue/original
titles through one explicit prefix transformation. It preserves the complete
preformatted text, footnotes, bullets, links, and decoded publisher email
notation. The demonstrated CFPB billing identifier is checked exactly. An
unsupported authority, action, missing page, boundary, or material dependency
still fails. Earlier retrieval policies are unchanged; v4 retains v3's SEC
graphic acquisition and missing-asset rejection.

## Native result

On October 22, 2026, the candidate source adapter acquired the explicit CFPB
posting window July 9–10, 2026. It discovered one notice,
`CFPB-2026-0018-0001`, and qualified its complete original in four adapter
requests and 2.329 seconds. The Regulations.gov download returned 403; the
retained verified GovInfo copy returned its complete 38,648-character text.
The source accounted for 133,717 consumed bytes, including discovery and
failure receipts, within its 32 MiB limit. An outer 90-second budget and an
eight-request cap bounded the diagnostic.

The frozen source passed validation and consumer-request preparation with
networking blocked. There were zero replay network calls, zero model calls,
and no production changes. The production tree was mounted read-only in the
diagnostic's private mount namespace; only the existing source credential was
read. Public results are in
[the qualification receipt](2026-10-22-cfpb-v4-qualification.json).

This is a current acquisition of a historical publication. It does not prove
what was available to a decision on July 9. Different captures can contain
different publisher email-obfuscation bytes; each retained original has its own
digest and acquisition timestamp. The source request's `as_of` bounds posting
selection, not the observation clock. Runtime v4 use is separately governed by
the linked-publications prospective research contract.

## Regression evidence

The new CFPB cases first failed because v4 did not exist. After implementation,
272 tests passed across the CFPB, v1/v2 government-copy, SEC, graphic inventory,
acquisition, integration, independent extraction, and EPA replay selections.
Controls cover incorrect metadata and body identity, action replacement,
missing sections/pages, unsupported markup, tampered frozen text, legacy policy
rejection, all 34 SEC interval-fund graphics, and a missing SEC asset. Fixtures
are actual captured public bytes; all mutated rejection controls are synthetic.

This result qualifies one declared CFPB notice/window. It does not certify all
CFPB document forms, establish DOL body coverage, activate any strategy, or
deploy the change.
