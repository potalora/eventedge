"""Real rule evidence, not ticker existence, authorizes a regulatory thesis."""
from datetime import datetime, timezone
from copy import deepcopy
import importlib
import json

import pytest

from tradingagents.strategies.data_sources.equity_universe import EquityUniverse, normalize_assets
from test_regulation_evidence import DOC, build


def module():
    return importlib.import_module('tradingagents.strategies.data_sources.regulation_assessment')


def universe():
    snapshot = normalize_assets([{'class': 'us_equity', 'symbol': 'ACME', 'exchange': 'NYSE',
                                  'status': 'active', 'tradable': True}],
        observed_at=datetime(2026, 10, 11, tzinfo=timezone.utc), response_sha256='a' * 64)
    companies = {'0': {'cik_str': 1234, 'ticker': 'ACME', 'title': 'ACME CORPORATION'}}
    return EquityUniverse(snapshot, companies), companies


def neutral(evidence):
    return {'contract_version': 'regulation-assessment-v1', 'document_id': DOC,
            'outcome': 'assessed_no_directional_thesis', 'direction': 'neutral', 'conviction': 0.6,
            'target_ticker': None, 'target_binding_sha256': None,
            'rationale': 'The complete supplied rule does not establish a directional thesis.',
            'evidence_claim': 'Proposed rule',
            'citations': [{'unit_id': unit['unit_id'], 'quote': unit['text'][:15]}
                          for unit in evidence['units']], 'unresolved_material_dependencies': []}


def test_complete_no_thesis_has_no_invented_ticker():
    evidence = build()
    request = module().prepare_request(evidence)
    result = module().validate_assessment(json.dumps(neutral(evidence)), request.context)
    assert result['document_assessment'] == 'assessed_no_directional_thesis'
    assert result['target_ticker'] is None
    assert result['security_resolution'] == 'not_required_for_no_thesis'
    assert result['source_provenance']['evidence_sha256'] == evidence['manifest_sha256']


def test_frozen_title_only_empty_summary_is_not_complete_no_thesis():
    with pytest.raises(ValueError):
        module().prepare_request({'title': 'Truckee Meadows', 'summary': '', 'document_id': DOC})


def test_real_ticker_with_unproved_exposure_does_not_validate_aep_shape():
    evidence = build()
    response = neutral(evidence)
    response.update(outcome='assessed_directional', direction='short', conviction=.35,
                    target_ticker='AEP', target_binding_sha256='a' * 64,
                    evidence_claim='Proposed rule')
    request = module().prepare_request(evidence)
    with pytest.raises(ValueError, match='invalid_regulation_target'):
        module().validate_assessment(json.dumps(response), request.context)


def test_explicit_named_sentence_and_membership_do_not_certify_applicability():
    evidence = build()
    assets, companies = universe()
    with pytest.raises(ValueError, match='invalid_regulation_applicability_authority_unavailable'):
        module().bind_regulation_target(evidence, assets, companies, 'ACME',
            unit_id=evidence['units'][0]['unit_id'], quote='This action applies to ACME CORPORATION.')


def test_company_name_alone_is_not_applicability_evidence():
    evidence = build()
    assets, companies = universe()
    with pytest.raises(ValueError, match='invalid_regulation_applicability'):
        module().bind_regulation_target(evidence, assets, companies, 'ACME',
            unit_id=evidence['units'][0]['unit_id'], quote='ACME CORPORATION')


def test_a_stitched_source_quote_cannot_validate_citation():
    evidence = build()
    request = module().prepare_request(evidence)
    response = neutral(evidence)
    response['citations'][0]['quote'] = 'Proposed rule ACME CORPORATION'
    with pytest.raises(ValueError, match='invalid_regulation_citations'):
        module().validate_assessment(json.dumps(response), request.context)


def test_every_attachment_needs_its_own_exact_citation():
    evidence = build(attachment=True)
    request = module().prepare_request(evidence)
    response = neutral(evidence)
    response['citations'].pop()
    with pytest.raises(ValueError, match='invalid_regulation_citations'):
        module().validate_assessment(json.dumps(response), request.context)


def test_insufficient_is_explicit_and_cannot_be_neutral_success():
    evidence = build()
    request = module().prepare_request(evidence)
    response = neutral(evidence)
    response.update(outcome='insufficient', direction=None, conviction=None,
                    unresolved_material_dependencies=[{'reference': 'Prior applicability order',
                                                       'reason': 'Necessary reference is not supplied.'}])
    result = module().validate_assessment(json.dumps(response), request.context)
    assert result['document_assessment'] == 'insufficient'
    assert result['regulation_evidence_status'] == 'insufficient'
    response['direction'] = 'neutral'
    with pytest.raises(ValueError, match='invalid_regulation_response'):
        module().validate_assessment(json.dumps(response), request.context)


def test_no_thesis_cannot_hide_unresolved_material_facts():
    evidence = build()
    request = module().prepare_request(evidence)
    response = neutral(evidence)
    response['unresolved_material_dependencies'] = [{'reference': 'Company applicability',
                                                    'reason': 'Unknown applicability.'}]
    with pytest.raises(ValueError, match='invalid_regulation_material_dependency'):
        module().validate_assessment(json.dumps(response), request.context)


def test_hand_constructed_prose_proof_is_rejected_before_assessment_and_replay():
    evidence = build()
    assets, companies = universe()
    proof = {'ticker': 'ACME', 'applicability': {'unit_id': evidence['units'][0]['unit_id'],
        'quote': 'This action applies to ACME CORPORATION.'}}
    with pytest.raises(ValueError, match='invalid_regulation_target_authority_unavailable'):
        module().validate_target_bindings(evidence, [proof], assets, companies)
    with pytest.raises(ValueError, match='invalid_regulation_target_authority_unavailable'):
        module().prepare_request(evidence, target_bindings=[proof])


def test_retained_request_context_recreates_exact_immutable_assessment():
    evidence = build()
    assets, companies = universe()
    regime = {'timestamp': '2026-10-11T10:00:00Z', 'vix': 17.5}
    request = module().prepare_request(evidence, target_bindings=[], regime_context=regime)
    response = json.dumps(neutral(evidence))
    assessment = module().validate_assessment(response, request.context)
    provenance = assessment['source_provenance']
    assert provenance['target_bindings'] == []
    assert provenance['regime_context'] == regime
    targets = module().validate_target_bindings(evidence, provenance['target_bindings'], assets, companies)
    replay = module().prepare_request(evidence, target_bindings=targets, regime_context=provenance['regime_context'])
    assert replay.system == request.system and replay.user == request.user
    assert module().validate_assessment(response, replay.context) == assessment


def test_changed_retained_context_cannot_recreate_original_assessment():
    evidence = build()
    assets, companies = universe()
    request = module().prepare_request(evidence, target_bindings=[], regime_context={'vix': 17.5})
    response = json.dumps(neutral(evidence))
    assessment = module().validate_assessment(response, request.context)
    damaged = deepcopy(assessment['source_provenance'])
    damaged['target_bindings'] = [{'ticker': 'ACME', 'binding_sha256': 'b' * 64}]
    with pytest.raises(ValueError, match='invalid_regulation_target'):
        module().validate_target_bindings(evidence, damaged['target_bindings'], assets, companies)
    changed = module().prepare_request(evidence, target_bindings=[], regime_context={'vix': 40})
    changed_result = module().validate_assessment(response, changed.context)
    assert changed_result != assessment
    assert changed_result['source_provenance']['request_sha256'] != assessment['source_provenance']['request_sha256']
