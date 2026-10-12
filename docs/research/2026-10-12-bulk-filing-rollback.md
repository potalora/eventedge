# Finish scope after the bulk-filing rollback

Pedro approved removing bulk full-filing/media acquisition while retaining the useful trading and research-resilience repairs on October 12, 2026. This supersedes the exhaustive acquisition campaign and its requirement to keep repairing and retrying until every discovered filing is captured. It does not convert any failed historical campaign into a pass.

## Prospective configuration

Apply `configs/research-without-bulk-filings.json` to a fresh generation's autoresearch configuration. `filing_research_policy="disabled_v1"` returns an explicit unavailable EDGAR research source before discovery, filing text, complete submissions, exhibits, ownership history or Form 4 acquisition. The existing `research_resilience_policy="dependency_isolation_v1"` excludes that source and strategies that require it before screening. Independent strategies can continue; missing shared prerequisites can still hold new entries. Accounting, trusted prices, persistence and evidence/replay integrity remain blocking.

The exclusion covers **all EDGAR research, including Form 4**, because the current containment boundary is the provider. SEC company-identity lookup for another provider is still permitted; it does not acquire filing bodies. This is a deliberate coverage loss, not evidence that no SEC events occurred. Do not infer full research coverage or profitability from a completed degraded run.

Start from the ordinary configuration plus the approved unrelated source/model settings. Remove the old `filing_evidence_policy`, `filing_comparison_policy`, `filing_parser_policy`, `filing_attribution_policy`, `filing_acquisition_policy`, `filing_material_policy` and associated filing manifests. The disabled policy rejects those conflicting declarations and requires dependency isolation. Use a new generation and fresh source/state directories; never reinterpret an earlier full-filing envelope under the reduced policy.

The historical acquisition/parser code and regression tests remain available to interpret old evidence. They are not enabled by this profile. No production defaults, active generation, services or production state are changed by adding the profile.

## Retained work and historical outcome

The rollback retains accounting/execution repairs, supported model authentication, source attribution and diagnostics, research dependency isolation, safe holds, immutable evidence checks and reproducible CI. The pre-rollback application is preserved at `8a11ca22f3652f6a0b730b19b4dbaa8c0882f32d` on local branch `codex/archive-document-loop-20261012`.

The final bulk campaign, local R7, failed after 914.24 seconds. Its source deadline expired before an accepted source envelope was published. The independent audit verified 2,747 completed SEC originals, 6,804,881,488 uncompressed bytes and 2,941,774,685 stored bytes. Incomplete acquisition and spool/request lifetime closure remained failures; all 80 recorded owned processes were absent after terminal cleanup. No campaign model analysis, committee staging or accepted immutable replay followed. Initial accounting across sixteen empty paper books is separate from those missing stages. Preserve the failure evidence; do not retry that campaign.

## Bounded closeout

Verify the exclusion and retained resilience with focused regression/integration checks and the existing CI. Perform at most one fresh isolated local operational check under this reduced profile and unchanged time/resource limits. Record its actual outcome and excluded coverage. A remaining defect becomes a specific follow-up, not another expanding repair loop. Finish the existing PR and full-system guide with these limits; do not claim live operational acceptance or replay unless separately demonstrated.

This remains topic-branch work. No merge, VPS action, production mutation or paid API fallback is authorized by this rollback.
