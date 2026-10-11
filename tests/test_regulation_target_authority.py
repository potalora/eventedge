"""Exact prose and ticker membership do not certify operative applicability."""
import json
import pytest
from test_regulation_evidence import DOC, STAMP, fixture
from test_regulation_assessment import universe
from tradingagents.strategies.data_sources.regulation_evidence import build_regulation_evidence
from tradingagents.strategies.data_sources.regulation_assessment import (
    bind_regulation_target, validate_target_bindings, prepare_request,
)


@pytest.mark.parametrize('sentence', [
    'This action applies to ACME CORPORATION.',
    'No rule applies to ACME CORPORATION.',
    'It is false that this action applies to ACME CORPORATION.',
    'If this action applies to ACME CORPORATION; further analysis is required.',
    'The following claim is false.</p><p>This action applies to ACME CORPORATION.',
])
def test_prose_bridge_cannot_be_created_or_supplied(sentence):
    row, detail, bodies = fixture()
    bodies[DOC] = ('<html><body><p>' + sentence + '</p></body></html>').encode()
    detail['data']['attributes']['fileFormats'][0]['size'] = len(bodies[DOC])
    evidence = build_regulation_evidence(row, json.dumps(detail).encode(), bodies, observed_at=STAMP)
    assets, companies = universe()
    quote = 'This action applies to ACME CORPORATION.'
    with pytest.raises(ValueError, match='applicability_authority_unavailable'):
        bind_regulation_target(evidence, assets, companies, 'ACME',
            unit_id=evidence['units'][0]['unit_id'], quote=quote)
    forged = {'ticker': 'ACME', 'issuer_cik': '0000001234',
        'applicability': {'unit_id': evidence['units'][0]['unit_id'], 'quote': quote}}
    with pytest.raises(ValueError, match='target_authority_unavailable'):
        validate_target_bindings(evidence, [forged], assets, companies)
    with pytest.raises(ValueError, match='target_authority_unavailable'):
        prepare_request(evidence, target_bindings=[forged])
