# Public SEC ownership originals

These are exact official SEC XML response bodies acquired on October 22, 2026.
`manifest.json` records the observed URLs, timestamps, byte counts and SHA256s.
There are no redactions, transformations, model outputs or synthetic edits in
these three original files. Their SEC ownership disclosures are public.

- `aapl_original.xml`: original Form 4, eleven transactions including five
  common-stock sales and four derivative transactions, eleven footnotes and an
  affirmative 10b5-1 indicator. Footnote F4 gives the disclosed plan adoption date.
- `aapl_amendment.xml`: actual Form 4/A with an original-submission date and two
  transactions. It tests conservative amendment-window exclusion.
- `berkshire_joint.xml`: actual LEN ownership disclosure by two reporting owners,
  fifteen transactions across Class A/Class B common stock and eighteen footnotes.
  Its filing date, October 5, is corroborated by the retained Berkshire submissions
  response; the archive directory identifies the reporting entity, not LEN.

Tests create explicitly synthetic variants in memory for malformed/missing data,
numeric grammar and overflow, unknown fields, holdings, and source tampering.
Those controls are not presented as official originals. Tests never acquire data.
The fixtures establish source/extraction regression coverage, not historical
decision-time availability, whole-market coverage, model quality or trading
authority.
