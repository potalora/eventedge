# Local validation — October 24, 2026 Pacific

The experiment's **64 focused tests pass**. The full non-live suite reports
**2,822 passed, two failed, four deselected** under Python 3.12. Both failures
also reproduce on the unchanged base commit
`5611ad84c60712671d9fe0240f7f5f20caaf473a`; the full suite is not green.

## Commands and results

Run from the experiment checkout with the repository dependencies available:

```bash
PYTHONPATH=. python -m pytest -q \
  tests/test_research_selection.py \
  tests/test_selection_comparison.py \
  tests/test_selection_fixture_export.py
# 64 passed in 4.37s

PYTHONPATH=. python -m pytest -q -m 'not live' --tb=short tests
# 2 failed, 2822 passed, 4 deselected, 1 warning in 500.13s
```

The two failures are in the existing `tests/test_primary_sip_price_source.py`:

- `test_primary_governed_bar_needs_no_recovery_record`
- `test_runtime_preflight_uses_current_primary_factory_and_real_resolver`

They compare historical observations from their October 6 `NOW` fixture with
the real current clock at the governed resolver and preflight validation
boundaries. Both fail on the clean base checkout with the same interpreter and
environment. In a separate diagnostic process, pinning both validation clocks
to the tests' own historical `NOW` makes both pass. That diagnostic does not
replace the full-suite result above. Existing tests and runtime clock logic
were not changed in this PR.

## Evidence verification

- All 24 recorded legacy selections match exactly, including their order.
- Permuting all candidate rows and case order reproduces the complete comparison.
- Regenerating the projection from the frozen source, screening and decision
  envelopes produces both input files byte for byte.
- Stored comparison and latency artifacts bind to the exact fixture SHA-256;
  all four artifact hashes in `SHA256SUMS.json` verify.
- Exporter negative controls reject mismatched decision/health epochs, source
  ticker rebinding, altered payloads, incomplete partitions, ambiguous content,
  future clocks and unsafe output locations.
- Independent review's epoch-binding and issuer-binding reproductions reject
  before output is written; no remaining review findings were identified.
- No provider, model, trading or deployment operation was executed by the
  experiment or its comparison. Local selector latency is reported separately.

Verified input hashes:

```text
selection-fixture.json
f0cc7c28194deddcd2ae51dc18e649802cf396da761a567893401dbb72fa4636

acquisition-evidence.json
bdd67305d27d3d65fe107355ca8c0ca93688eee38e6b88059674eb7c52034def
```

## Production preservation

Before/after checks found no change in 89 protected gen_025 state, configuration,
report and service-unit files. Its tracked code matches HEAD and remains at
`e33cc9eed38aa668dbde5f5e2844b7fbfb9391ac`. Detailed operational preservation
receipts remain local; portfolio and account state are not published in this PR.

This verifies preservation and the offline experiment. It does not qualify an
alternative live strategy, prove model-quality or return improvements, or
authorize deployment.
