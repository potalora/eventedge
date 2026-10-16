"""Nonprinted visual descriptions; immutable earlier successes/failures replay."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from test_regulation_graphics import assets, provenance, recognition_response, review_response
from test_regulation_graphics_runtime import harness, memo_client, candidate_run
from tradingagents.strategies.data_sources import regulation_graphics as graphics
from tradingagents.strategies.learning import regulation_graphics_runtime as runtime

FIXTURES=Path(__file__).parent/'fixtures'
V4='regulation-graphics-request-v4'
V5='regulation-graphics-request-v5'


def load(name):
    return json.loads((FIXTURES/name).read_text())


def bound_assets(identity):
    return [{k:row[k]for k in ('id','filename','sha256','context')}|{
        'data':(FIXTURES/'regulation_graphics'/row['filename']).read_bytes()}
        for row in identity['assets']]


def test_default_v5_explicitly_describes_unprinted_axes_without_inventing_labels():
    request=graphics.prepare_graphics_recognition(assets())
    assert graphics.REQUEST_VERSION==V5
    assert graphics.REQUEST_VERSIONS==frozenset(f'regulation-graphics-request-v{i}'for i in range(1,6))
    assert 'nonempty positional description' in request.system
    assert 'source=visual and precision=qualitative' in request.system
    assert 'not a printed axis label' in request.system
    assert 'Never invent an axis label, unit, tick value or number' in request.system
    assert 'Self-check every element against the schema before emitting JSON' in request.system
    assert 'null ONLY for an actually blank cell or unprinted row/column label' in request.system
    assert 'exact within-cell line breaks' in request.system
    recognized=graphics.validate_graphics_recognition(json.dumps(recognition_response(request)),request,provenance(request))
    review=graphics.prepare_graphics_review(assets(),recognized)
    assert graphics._VISUAL_STRUCTURE_V5 in review.system
    checked=graphics.validate_graphics_review(json.dumps(review_response(review,recognized)),review,recognized,provenance(review))
    assert graphics.replay_graphics_receipt(assets(),{'recognition':recognized,'review':checked})


def test_actual_dense_v4_null_axis_failure_stays_failed_with_exact_request(monkeypatch):
    attempt=load('regulation_graphics_v4_native_null_axis_failure.json')['attempt']
    before=deepcopy(attempt);failure=attempt['graphics_failure']
    values=bound_assets(failure['request_identity'])
    request=graphics.prepare_graphics_recognition(values,request_version=V4)
    assert runtime._logical(request)==failure['model_request']
    assert 'nonempty positional description' not in request.system
    raw=json.loads(failure['response_text'])
    assert all(row['status']=='complete'and row['unresolved']==[]for row in raw['graphics'])
    nulls=[(row['id'],e)for row in raw['graphics']for e in row['elements']if e['text']is None]
    assert nulls==[('EP06OC26.072',{'id':'xl','kind':'axis','text':None,
        'source':'visual','references':[],'precision':'qualitative'})]
    with pytest.raises(ValueError,match='invalid_regulation_graphics_text'):
        graphics.validate_graphics_recognition(failure['response_text'],request,failure['model_provenance'])
    from tradingagents.strategies.data_sources import regulation_graphics_inputs
    monkeypatch.setattr(regulation_graphics_inputs,'graphics_assets',lambda evidence:values)
    assert runtime.replay_graphics_attempt({},attempt)is None
    assert attempt==before and failure['validation_status']=='failed'


@pytest.mark.parametrize('fixture,native',[
    ('regulation_graphics_v4_constructed_bundle.json',False),
    ('regulation_graphics_v4_native_small_bundle.json',True)])
def test_v4_success_replay_stays_exact_after_v5_default(fixture,native):
    bundle=load(fixture)['bundle'];before=deepcopy(bundle)
    values=bound_assets(bundle['recognition']['request_identity'])if native else assets()
    request=graphics.prepare_graphics_recognition(values,request_version=V4)
    review=graphics.prepare_graphics_review(values,bundle['recognition'])
    assert runtime._logical(request)==bundle['recognition']['model_request']
    assert runtime._logical(review)==bundle['review']['model_request']
    assert json.loads(review.context_json)['version']==V4
    assert graphics.replay_graphics_receipt(values,bundle)
    assert bundle==before
    with pytest.raises(ValueError,match='invalid_regulation_graphics_request_version'):
        graphics.prepare_graphics_review(values,bundle['recognition'],request_version=V5)


@pytest.mark.parametrize('text',[None,'',' '])
def test_v5_still_rejects_null_or_empty_visual_axis(text):
    request=graphics.prepare_graphics_recognition(assets());raw=recognition_response(request)
    raw['graphics'][0]['elements'].append({'id':'visual_axis','kind':'axis','text':text,
        'source':'visual','references':[],'precision':'qualitative'})
    with pytest.raises(ValueError,match='invalid_regulation_graphics_text'):
        graphics.validate_graphics_recognition(json.dumps(raw),request,provenance(request))


def test_constructed_nonprinted_axis_uses_description_and_still_requires_review():
    request=graphics.prepare_graphics_recognition(assets());raw=recognition_response(request)
    raw['graphics'][0]['elements'].append({'id':'visual_axis','kind':'axis',
        'text':'Horizontal axis along the bottom of the chart','source':'visual',
        'references':[],'precision':'qualitative'})
    recognized=graphics.validate_graphics_recognition(json.dumps(raw),request,provenance(request))
    review=graphics.prepare_graphics_review(assets(),recognized);checked=review_response(review,recognized)
    checked['graphics'][0].update(material_complete=False,errors=[{
        'element_id':'visual_axis','issue':'Constructed ambiguous axis geometry.'}])
    with pytest.raises(ValueError,match='invalid_regulation_graphics_review_incomplete'):
        graphics.validate_graphics_review(json.dumps(checked),review,recognized,provenance(review))


def test_v5_never_upgrades_a_reported_uncertainty_or_partial_result():
    request=graphics.prepare_graphics_recognition(assets());raw=recognition_response(request)
    raw['graphics'][0].update(status='partial',unresolved=['Unclear axis direction.'])
    with pytest.raises(ValueError,match='invalid_regulation_graphics_incomplete'):
        graphics.validate_graphics_recognition(json.dumps(raw),request,provenance(request))


def test_visual_schema_policy_change_cannot_hit_prior_graphics_memo(harness,monkeypatch):
    from tradingagents.strategies.candidate_response_reuse import candidate_response_memo
    memo_client(harness,monkeypatch)
    with candidate_response_memo():
        candidate_run(harness);count=len(harness.calls)
        monkeypatch.setattr(graphics,'_VISUAL_STRUCTURE_V5',graphics._VISUAL_STRUCTURE_V5+'\nConstructed schema clarification.\n')
        candidate_run(harness)
        assert len(harness.calls)==2*count
