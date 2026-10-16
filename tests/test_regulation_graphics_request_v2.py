"""Prospective supplied-asset scope; exact V1 successes/failures replay unchanged."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from test_regulation_graphics import assets, provenance, recognition_response, review_response
from tradingagents.strategies.data_sources import regulation_graphics as graphics
from tradingagents.strategies.learning import regulation_graphics_runtime as runtime

FIXTURES=Path(__file__).parent/'fixtures'
V1='regulation-graphics-request-v1'
V2='regulation-graphics-request-v2'


def retained_failure():
    return json.loads((FIXTURES/'regulation_graphics_v1_native_border_partial.json').read_text())['attempt']


def native_assets():
    meta=retained_failure()['graphics_failure']['request_identity']['assets'][0]
    return [{key:meta[key] for key in ('id','filename','sha256','context')} | {
        'data':(FIXTURES/'regulation_graphics'/meta['filename']).read_bytes()}]


def test_v2_bounds_complete_supplied_asset_but_preserves_material_clipping_gate():
    request=graphics.prepare_graphics_recognition(assets(),request_version=V2)
    assert graphics.LEGACY_REQUEST_VERSION==V1 and graphics.SCOPED_REQUEST_VERSION==V2
    assert json.loads(request.context_json)['version']==V2
    assert 'complete supplied original asset' in request.system
    assert 'outer edge or ending border' in request.system
    assert 'hypothetical content outside the supplied asset' in request.system
    assert 'visibly clipped glyph, cell, row, or curve' in request.system
    assert 'missing material explicitly indicated by the supplied source context' in request.system
    assert request.document_inputs[0].data==assets()[0]['data']
    assert 'Numeric literals require printed precision' in request.system
    assert 'Every table cell references its row AND column' in request.system
    recognized=graphics.validate_graphics_recognition(json.dumps(recognition_response(request)),request,provenance(request))
    review=graphics.prepare_graphics_review(assets(),recognized)
    assert json.loads(review.context_json)['version']==V2
    assert 'complete supplied original asset' in review.system
    assert 'visibly clipped glyph, cell, row, or curve' in review.system
    assert 'no missing/uncertain material' in review.system
    accepted=graphics.validate_graphics_review(json.dumps(review_response(review,recognized)),review,recognized,provenance(review))
    assert graphics.replay_graphics_receipt(assets(),{'recognition':recognized,'review':accepted})


def test_exact_v1_native_partial_request_rebuild_and_failure_stay_unchanged(monkeypatch):
    attempt=retained_failure();original=deepcopy(attempt)
    failure=attempt['graphics_failure']
    request=graphics.prepare_graphics_recognition(native_assets(),request_version=V1)
    assert request.context_json==runtime._canonical(failure['request_identity'])
    assert runtime._logical(request)==failure['model_request']
    assert hashlib.sha256(request.system.encode()).hexdigest()==failure['model_request']['system_sha256']
    assert 'complete supplied original asset' not in request.system
    with pytest.raises(ValueError,match='invalid_regulation_graphics_incomplete'):
        graphics.validate_graphics_recognition(failure['response_text'],request,failure['model_provenance'])
    # The fixture supplies actual source bytes, and substitutes only extraction
    # of its exact retained source context to isolate the pure attempt replay.
    from tradingagents.strategies.data_sources import regulation_graphics_inputs
    monkeypatch.setattr(regulation_graphics_inputs,'graphics_assets',lambda evidence:native_assets())
    assert runtime.replay_graphics_attempt({},attempt) is None
    assert attempt==original and failure['validation_status']=='failed'


def test_complete_v1_recognition_and_review_replay_exact_old_prompt_bytes():
    value=json.loads((FIXTURES/'regulation_graphics_v1_constructed_bundle.json').read_text())['bundle']
    before=deepcopy(value)
    recognition=graphics.prepare_graphics_recognition(assets(),request_version=V1)
    review=graphics.prepare_graphics_review(assets(),value['recognition'],request_version=V1)
    assert runtime._logical(recognition)==value['recognition']['model_request']
    assert runtime._logical(review)==value['review']['model_request']
    assert graphics.replay_graphics_receipt(assets(),value)
    assert value==before


@pytest.mark.parametrize('version',[V1,V2])
def test_historical_failed_review_replays_its_recorded_version_without_upgrade(version):
    label='v1' if version==V1 else 'v2'
    bundle=json.loads((FIXTURES/('regulation_graphics_'+label+'_constructed_bundle.json')).read_text())['bundle']
    reviewed=bundle['review']; response=json.loads(reviewed['response_text'])
    response['graphics'][0].update(material_complete=False,errors=[{
        'element_id':None,'issue':'Constructed visible cell clipping'}])
    raw=json.dumps(response); body=raw.encode()
    stage={'version':'regulation-graphics-failed-attempt-v1','asset_ids':[a['id'] for a in assets()],
        'stage':'review','request_identity':reviewed['request_identity'],
        'model_request':reviewed['model_request'],'model_provenance':reviewed['model_provenance'],
        'model_call_started':True,'response_received':True,'response_text':raw,
        'response_sha256':hashlib.sha256(body).hexdigest(),'response_bytes':len(body),
        'response_encoding':'utf-8','response_over_limit':False,'validation_status':'failed',
        'failure_reason':'invalid_regulation_graphics_review_incomplete',
        'validated_recognition':bundle['recognition']}
    stage['receipt_sha256']=runtime._seal(stage); before=deepcopy(stage)
    runtime._failed_stage(assets(),stage)
    assert stage==before
    request=graphics.prepare_graphics_review(assets(),bundle['recognition'])
    assert json.loads(request.context_json)['version']==version
    with pytest.raises(ValueError,match='invalid_regulation_graphics_review_incomplete'):
        graphics.validate_graphics_review(raw,request,bundle['recognition'],provenance(request))


@pytest.mark.parametrize('version',[None,'other',True,{},[]])
def test_unknown_or_untyped_request_version_is_rejected(version):
    with pytest.raises(ValueError,match='invalid_regulation_graphics_'):
        graphics.prepare_graphics_recognition(assets(),request_version=version)


@pytest.mark.parametrize('issue',[
    'A visibly clipped glyph cuts off the final numeric cell.',
    'A visible table row is clipped at the supplied original asset edge.',
    'The plotted curve is visibly cut off before its required endpoint.',
    'The supplied source context explicitly indicates a missing footnote.',
    'The graphic ends at the lower edge; hypothetical content beyond it cannot be assessed.',
])
def test_v2_does_not_override_any_model_reported_partial_or_uncertainty(issue):
    request=graphics.prepare_graphics_recognition(assets(),request_version=V2)
    raw=recognition_response(request);raw['graphics'][0].update(status='partial',unresolved=[issue])
    with pytest.raises(ValueError,match='invalid_regulation_graphics_incomplete'):
        graphics.validate_graphics_recognition(json.dumps(raw),request,provenance(request))


def test_v2_review_still_rejects_visible_clipping_or_any_reported_uncertainty():
    request=graphics.prepare_graphics_recognition(assets(),request_version=V2)
    recognized=graphics.validate_graphics_recognition(json.dumps(recognition_response(request)),request,provenance(request))
    review=graphics.prepare_graphics_review(assets(),recognized)
    raw=review_response(review,recognized)
    raw['graphics'][0].update(material_complete=False,errors=[{
        'element_id':None,'issue':'Visible clipped glyph in final cell'}])
    with pytest.raises(ValueError,match='invalid_regulation_graphics_review_incomplete'):
        graphics.validate_graphics_review(json.dumps(raw),review,recognized,provenance(review))


def test_review_version_cannot_relabel_old_recognition_or_cross_bound_prompt():
    old=json.loads((FIXTURES/'regulation_graphics_v1_constructed_bundle.json').read_text())['bundle']
    with pytest.raises(ValueError,match='invalid_regulation_graphics_'):
        graphics.prepare_graphics_review(assets(),old['recognition'],request_version=V2)
    request=graphics.prepare_graphics_recognition(assets())
    raw=recognition_response(request)
    old_identity=json.loads(graphics.prepare_graphics_recognition(assets(),request_version=V1).context_json)
    raw['request_identity_sha256']=old_identity['request_identity_sha256']
    with pytest.raises(ValueError,match='invalid_regulation_graphics_identity'):
        graphics.validate_graphics_recognition(json.dumps(raw),request,provenance(request))
