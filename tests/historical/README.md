# Retained historical source acceptance

Run the public subset without credentials or network:

```bash
python scripts/run_historical_source_acceptance.py --report /tmp/historical-public.json
```

The nine public fixtures are actual NOAA, USDA, drought and CFTC observations.
Eight are compact normalized extracts from accepted captures; the NOAA `.dly`
file contains three unchanged public fixed-width monthly records. It is not an
Access API JSON response. FRED, Yahoo, Finnhub, Alpaca SIP and other full provider
captures remain outside Git. No missing source is replaced with invented data.

Development/release acceptance additionally requires an explicit private
directory containing these exact originals, with hashes pinned in `manifest.json`:

```text
gen022-20261015-source-inputs.json
native-20261016-source-inputs.json
alpaca-sip-20261016.json
```

The first envelope was acquired October 16 for reference session October 15 and
vintage October 15. The second was acquired October 18 for reference session
October 16 and vintage October 18. The 508-byte Alpaca response was acquired
October 18 for exact October 16 SIP/raw bars. Keep that distinction; acquisition
time does not establish historical point-in-time availability.

```bash
python scripts/run_historical_source_acceptance.py \
  --private-corpus /explicit/private/corpus \
  --require-full-supported-scope --report /tmp/historical-full.json
```

The strict command fails for absent, changed or unsupported required originals.
`public_scope_passed` and `full_supported_scope_passed` are separate. Full scope
means offline source-contract replay, not successful current provider coverage,
native workflow, financial correctness, authentication, model quality or live
readiness. All source identities must be present, including recorded failed or
disabled-policy records. Their presence does not require or imply health:
Congress remains disclosure-only, EDGAR retains its recorded failure, and OpenBB
retains its enrichment-only declaration. Current NOAA failure stays failed;
weather candidates remain journal-only. Older missing capability receipts are
not replaced with newer ones.

The runner uses native immutable-envelope validation, source projection,
semantic validators and deterministic strategy consumers. Retained FRED series
and Yahoo SDK frames are compared with native return values. Only Alpaca uses an
actual captured native HTTP response. Its injected GET uses adapter-local inert
header-presence sentinels; no real credentials are read and authentication is
never asserted. Network, native curl and subprocess/model-worker fallback are
denied by default. Reports contain hashes, provenance, coverage and counts, not
private payloads, private paths or profiles. They bind Git HEAD and dirty-tree
diff identity.

For a newly observed source behavior, retain the original bytes privately first.
Record source URL, original and selected hashes, acquisition/reference/vintage
times, selection and any redactions. Public extracts require a public-data and
privacy review; government hosting alone does not prove redistribution rights.
Add an observed baseline and its grounded expected contract outcome to the
manifest, then regression tests. Controlled corruptions reference a real parent
case and remain in `controlled_mutations`; they are not upstream observations.
Record source/code/network attribution and unresolved uncertainty in the source
reliability workflow. Missing raw wires remain an explicit boundary, never a
fabricated HTTP baseline or a synthetic success receipt.

## Explicit yearly NOAA route

The prospective stabilization profile now selects `ghcnd_yearly_v1`. Its separate
raw-file gate runs before this historical gate in both CI environments:

```bash
python scripts/verify_noaa_yearly_source.py --report /tmp/noaa-yearly.json
```

For development acceptance, `--full-corpus /explicit/noaa-originals` requires
the pinned complete station catalog, element inventory and 2026 annual gzip.
It passes those actual bodies through the selected native adapter with external
IO denied. Missing or changed originals fail. See
[`docs/noaa-yearly-delivery.md`](../../docs/noaa-yearly-delivery.md) for names,
provenance, resource limits and the distinct numeric policy.

The older normalized source captures above retain their original failed or
successful NOAA outcomes. The new raw-file gate does not rewrite them or certify
historical availability. Constructed scheduled tests replay the public raw
subset under a labeled test clock; native current acquisition is separate
evidence. Production activation still requires a new generation.
