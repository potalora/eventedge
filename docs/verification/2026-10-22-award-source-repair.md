# USAspending ownership review, 2026-10-22

The October 21 `gen_023` acquisition contained 37 awards, with 18 unresolved
awards belonging to 12 distinct recipient UEIs. A public, read-only review of all
18 native award-detail endpoints confirmed every frozen recipient/parent UEI
pair. Eleven recipient crosswalk additions resolve 17 awards: Ketchum maps to
Omnicom common stock, and ten recipients have verified nonlisted ownership.
Lydig remains unresolved because the explicit current controlling-owner evidence
is weaker. Acquisition success and the remaining ownership coverage gap stay
separate.

The prospective classification of that unchanged 37-award population is:

| Disposition | Previous crosswalk | Reviewed crosswalk |
| --- | ---: | ---: |
| Verified listed target | 9 | 10 |
| Verified no listed target | 10 | 26 |
| Unresolved | 18 | 1 |

These are local development results. No frozen production input, generation,
service, manifest, or live state was changed. Deployment requires the separately
authorized new-generation workflow.

The [public evidence record](2026-10-22-award-source-repair.json) retains each
recipient's sources, review date, ownership explanation, limits, affected award
keys, public response digests, and native recipient/parent identity projections.
The regression fixture contains only public source fields from the October 21
population; it omits the previous computed issuer attribution and scope. The
original encoded 50-award fixture remains byte-for-byte unchanged.

## Reviewed additions

| Recipient UEI | Recipient | Ownership conclusion | Awards |
| --- | --- | --- | ---: |
| `CZJKHV86FSB2` | Ketchum Inc. | Omnicom Group Inc., `OMC`, CIK `0000029989` | 1 |
| `QQBXQR679KM9` | Clark Construction Group LLC | Privately held shareholders | 3 |
| `LFH4SUCVA379` | Environmental Chemical Corporation | Employee owned | 1 |
| `W31DQKBPJCD3` | Southwest Valley Constructors Co. | Privately held Kiewit group | 5 |
| `XXNJCWMAKNH8` | BL Harbert International LLC | Private family ownership | 1 |
| `MYEKECP4M5H4` | Peachtree Lumber Company, Inc. | Locally owned S corporation | 1 |
| `LBM4GLJNDK31` | Kaléo, Inc. | Marathon-managed funds | 1 |
| `N9NJK877QJK9` | Guidehouse Inc. | Bain Capital Private Equity affiliate | 1 |
| `FHKLJV1NP651` | Smiths Detection Inc. | CVC-advised funds | 1 |
| `TWVGJRMK6Z78` | Brasfield & Gorrie LLC | Privately held shareholders | 1 |
| `SQLVDBFQ4SD8` | Workwear Outfitters, LLC | Redwood investment subsidiary | 1 |

Ketchum's [same-award native detail](https://api.usaspending.gov/api/v2/awards/CONT_AWD_75D30126F21224_7523_47QRAA23D0052_4732/)
identifies Omnicom Group Inc. as parent. Omnicom's [2025 SEC Form 10-K](https://www.sec.gov/Archives/edgar/data/29989/000002998926000006/omc-20251231.htm)
identifies Ketchum within its group and establishes the common-stock symbol and
listing. Its significant-subsidiaries Exhibit 21 does not name Ketchum; the
ownership evidence does not claim otherwise.

Clark's [current company page](https://www.clarkconstruction.com/clark-industrial)
explicitly establishes private status. The historical 2015 management transfer
is supporting history, not a claim about current individual shareholder holdings.

Three ownership boundaries need particular care:

- Smiths [completed its Detection disposal on June 30, 2026](https://www.smiths.com/news-and-insights/news/2026/completion-of-smiths-detection-sale),
  before the September 28 award. The former Smiths listing does not identify the
  current recipient's listed parent. CVC's role as fund adviser does not prove
  ownership of fund assets by its listed management company.
- Guidehouse's [Bain acquisition closed in December 2023](https://guidehouse.com/news/corporate-news/2023/guidehouse-completes-transaction-with-bain-capital),
  while native award detail retains the Veritas parent name and UEI. Workwear's
  [sale from VF closed in June 2021](https://www.vfc.com/investors/news-events-presentations/press-releases/detail/1768/vf-corporation-completes-the-sale-of-the-occupational),
  while native detail retains the VF Imagewear parent. The new mappings apply to
  the reviewed recipients; they do not assign those legacy parent UEIs to Bain,
  Redwood, or VFC globally.
- ECC's [official About page](https://www.ecc.net/about-us/) uses an animated
  ownership counter. Its underlying counter has `data-to-value="100"` before the
  employee-ownership label. An intermediate rendered percentage is not evidence
  of partial ownership. [GAO-21-372, printed page 20](https://www.gao.gov/assets/720/715122.pdf)
  establishes Southwest Valley's Kiewit Infrastructure West relationship;
  [Kiewit's own employee-ownership description](https://www.kiewit.com/careers/)
  establishes privately held status.

All entries describe prospective current ownership reviewed on October 22, not
historical ownership for every award date. Earlier mappings retain their existing
October 10 review date. No recipient-name matching, supplied ticker, private flag,
or inferred fund-manager ticker can grant issuer authority.

## Deferred recipient

`T9G9DELGRKX5`, Lydig Construction, remains unresolved. The
[company history](https://www.lydig.com/people/) identifies a 1996 ownership
transition. The company's [2023 CFO announcement](https://www.linkedin.com/posts/lydig-construction_we-are-proud-to-announce-the-promotion-of-activity-7065036382557130752-fE5J)
and direct CFO interviews support management co-ownership, but do not establish a
current controlling shareholder or complete current ownership chain. This
coverage gap remains visible and nonactionable.

## Verification

Test-first verification initially produced 15 expected failures for missing
ownership mappings and classifications. After adding the reviewed entries, 11
tests failed specifically because the resolver still returned the old global
review date. Reading the matched entry's review date fixed those failures while
preserving prior dates.

The focused offline suite passes: **120 tests** across
`test_award_ownership_review_20261022.py`, `test_award_ownership_dispositions.py`,
`test_award_verified_subset_policy.py`, `test_award_issuer_attribution.py`,
`test_award_universe_disposition.py`, and `test_usaspending_source.py`.
It exercises the 37-award partition, the unchanged encoded 50-award population,
listed/private/unresolved screening, supplied-ticker rejection, native-parent
safety, and prior review-date preservation.

```bash
python -m pytest tests/test_award_ownership_review_20261022.py \
  tests/test_award_ownership_dispositions.py tests/test_award_verified_subset_policy.py \
  tests/test_award_issuer_attribution.py tests/test_award_universe_disposition.py \
  tests/test_usaspending_source.py -q
```
