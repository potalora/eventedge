"""All reviewed graphic material must reach the ordinary complete-rule thesis."""
from copy import deepcopy
import json

import pytest

from test_regulation_evidence import build
from test_regulation_graphics import assets, bundle
from tradingagents.strategies.data_sources import regulation_assessment as assessment
from tradingagents.strategies.data_sources import regulation_graphics_inputs as inputs


@pytest.fixture
def graphic_source(monkeypatch):
    # Image contracts use their real pure parser/replayer. This fixture isolates
    # thesis integration from separately tested publisher acquisition.
    monkeypatch.setattr(inputs, 'graphics_assets', lambda evidence: assets())
    return build()


def test_graphic_source_cannot_enter_thesis_without_all_image_reviews(graphic_source):
    with pytest.raises(ValueError, match='invalid_regulation_graphics'):
        assessment.prepare_request(graphic_source)


def test_all_material_elements_become_separately_citable_thesis_units(graphic_source):
    receipt = bundle()
    request = assessment.prepare_request(graphic_source, graphics_batches=[receipt])
    payload = json.loads(request.user)
    assert payload['contract_version'] == 'regulation-assessment-v2'
    assert len(payload['units']) == len(graphic_source['units']) + 1
    graphic = payload['units'][-1]
    assert graphic['representation'] == 'image_backed_machine_reviewed_graphic'
    assert '"cell"' in graphic['text'] and '"column"' in graphic['text']
    assert 'Counts are reported as units.' in graphic['text']
    assert 'machine' in request.system and 'approximate' in request.system
    assert json.loads(request.context.provenance_json)['graphics_batches_sha256']
    response = {'contract_version':'regulation-assessment-v2',
        'document_id':graphic_source['identity']['document_id'],
        'outcome':'assessed_no_directional_thesis','direction':'neutral','conviction':0.5,
        'target_ticker':None,'target_binding_sha256':None,
        'rationale':'Complete supplied material assessed.', 'evidence_claim':'Proposed rule',
        'citations':[{'unit_id':unit['unit_id'],'quote':unit['text'][:15]} for unit in payload['units']],
        'unresolved_material_dependencies':[]}
    result = assessment.validate_assessment(json.dumps(response), request.context)
    assert result['regulation_evidence_status'] == 'sufficient'
    response['citations'].pop()
    with pytest.raises(ValueError, match='invalid_regulation_citations'):
        assessment.validate_assessment(json.dumps(response), request.context)


@pytest.mark.parametrize('fault',['missing_review','changed_response','extra_batch','wrong_image'])
def test_missing_resealed_or_foreign_review_never_authorizes_graphic_thesis(graphic_source, fault):
    receipts = [deepcopy(bundle())]
    if fault == 'missing_review': receipts[0].pop('review')
    elif fault == 'changed_response': receipts[0]['recognition']['response_text'] += ' '
    elif fault == 'extra_batch': receipts *= 2
    else: receipts[0]['recognition']['result']['graphics'][0]['sha256'] = '0' * 64
    with pytest.raises(ValueError, match='invalid_regulation_graphics'):
        assessment.prepare_request(graphic_source, graphics_batches=receipts)


def test_legacy_request_is_unchanged_and_refuses_unsolicited_graphics_receipts():
    evidence = build()
    request = assessment.prepare_request(evidence)
    assert json.loads(request.user)['contract_version'] == 'regulation-assessment-v1'
    assert 'graphics_batches_sha256' not in json.loads(request.context.provenance_json)
    with pytest.raises(ValueError, match='invalid_regulation_graphics'):
        assessment.prepare_request(evidence, graphics_batches=[bundle()])


def graphic_attempt(evidence):
    import hashlib
    receipts = [bundle()]
    request = assessment.prepare_request(evidence, graphics_batches=receipts)
    units = json.loads(request.user)['units']
    raw = json.dumps({'contract_version':'regulation-assessment-v2',
        'document_id':evidence['identity']['document_id'],
        'outcome':'assessed_no_directional_thesis','direction':'neutral','conviction':0.5,
        'target_ticker':None,'target_binding_sha256':None,
        'rationale':'Complete supplied material assessed.', 'evidence_claim':'Proposed rule',
        'citations':[{'unit_id':u['unit_id'],'quote':u['text'][:15]} for u in units],
        'unresolved_material_dependencies':[]})
    return {'version':'regulation-model-attempt-v2','source_provenance':json.loads(request.context.provenance_json),
        'model_provenance':{},'model_call_started':True,'response_received':True,
        'response_text':raw,'response_sha256':hashlib.sha256(raw.encode()).hexdigest(),
        'response_bytes':len(raw.encode()),'response_encoding':'utf-8','response_over_limit':False,
        'validation_status':'validated','failure_reason':None,'graphics_batches':receipts}


def test_retained_graphics_attempt_replays_from_images_without_models(graphic_source, monkeypatch):
    from tradingagents.strategies.orchestration.regulation_validation import _attempt
    attempt = graphic_attempt(graphic_source)
    result = _attempt({'model_attempt':attempt,'model_provenance':{}},graphic_source,None,{})
    assert result['regulation_evidence_status'] == 'sufficient'


@pytest.mark.parametrize('fault', ['no_receipts','changed_image_response','downgrade','extra_batch'])
def test_regulatory_replay_rejects_graphic_attempt_tampering(graphic_source, monkeypatch, fault):
    from tradingagents.strategies.orchestration.regulation_validation import _attempt
    attempt = graphic_attempt(graphic_source)
    if fault == 'no_receipts': attempt.pop('graphics_batches')
    elif fault == 'changed_image_response': attempt['graphics_batches'][0]['review']['response_text'] += ' '
    elif fault == 'downgrade': attempt['version'] = 'regulation-model-attempt-v1'
    else: attempt['graphics_batches'] *= 2
    with pytest.raises(ValueError):
        _attempt({'model_attempt':attempt,'model_provenance':{}},graphic_source,None,{})
