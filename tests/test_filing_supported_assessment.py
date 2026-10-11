"""Ownership dependencies are source-bound full units, never annual priors."""
import base64
from copy import deepcopy
import json

import pytest

from test_filing_ownership_lineage import filing, case, lineage
from test_filing_assessment import binding, response
from tradingagents.strategies.data_sources.filing_assessment import prepare_request, validate_assessment


def ownership_inputs():
    base = filing(1, '2026-01-01')
    middle = filing(2, '2026-05-01', amendment=1, previous=base[0]['accession'])
    current = filing(3, '2026-09-30', amendment=2, previous=base[0]['accession'])
    unrelated = filing(4, '2026-06-01', people=(('Unrelated reporter', '0000000099'),))
    for evidence, facts, body in (base, middle, current, unrelated):
        evidence.update(supported_document_policy='verified_supported_documents_v1', pdf_evidence={},
            ownership_primary_body=base64.b64encode(body).decode('ascii'), ownership_facts=facts)
    submissions, facts, histories, archives, bodies = case(base, middle, current, unrelated)
    receipt = lineage.ownership_lineage(current[0], facts, submissions, histories, archives)
    assert receipt['status'] == 'complete'
    bundle = dict(receipt=receipt, facts_by_accession=facts, submissions=submissions,
                  histories=histories, archives=archives, primary_bodies=bodies)
    return current[0], base[0], middle[0], unrelated[0], bundle


def prepare(current, base, middle, bundle):
    return prepare_request('activist_stake', current,
        issuer_binding=binding(current, base, middle), ownership_bundles={current['accession']: bundle})


def test_all_ownership_chain_units_rendered_and_cited_without_annual_prior():
    current, base, middle, unrelated, bundle = ownership_inputs()
    request = prepare(current, base, middle, bundle)
    payload = json.loads(request.user)
    assert payload['prior'] == []
    assert {entry['accession'] for entry in payload['ownership_dependencies']} == {base['accession'], middle['accession']}
    assert unrelated['accession'] not in {entry['accession'] for entry in payload['ownership_dependencies']}
    assert payload['ownership_lineage_bindings'][current['accession']] == bundle['receipt']
    for entry in payload['current'] + payload['ownership_dependencies']:
        assert 'ownership_primary_body' not in entry
        assert entry['units'][0]['text'] == bundle['submissions'][entry['accession']]['units'][0]['text']
    assert request.context.source_provenance['unit_count'] == 3
    value = response(request)
    assert validate_assessment(json.dumps(value), request.context)['filing_evidence_status'] == 'sufficient'
    value['citations'].pop()
    with pytest.raises(ValueError, match='invalid_filing_citations'):
        validate_assessment(json.dumps(value), request.context)


@pytest.mark.parametrize('damage', ['missing_bundle', 'missing_intervening', 'forged_fact', 'forged_receipt'])
def test_ownership_analysis_requires_closed_raw_verified_lineage(damage):
    current, base, middle, unrelated, bundle = ownership_inputs()
    if damage == 'missing_bundle':
        with pytest.raises(ValueError, match='invalid_filing_ownership_lineage'):
            prepare_request('activist_stake', current, issuer_binding=binding(current))
        return
    if damage == 'missing_intervening':
        del bundle['submissions'][middle['accession']]
    elif damage == 'forged_fact':
        bundle['facts_by_accession'][base['accession']]['reporters'][0]['name'] = 'Forged identity'
    elif damage == 'forged_receipt':
        bundle['receipt']['chain_accessions'].remove(middle['accession'])
    with pytest.raises(ValueError, match='invalid_filing_ownership_lineage'):
        prepare(current, base, middle, bundle)
