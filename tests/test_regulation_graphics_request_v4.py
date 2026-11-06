"""Prospective material legibility; exact earlier prompts/failures stay unchanged."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from test_regulation_graphics import assets, provenance, recognition_response, review_response
from test_regulation_graphics_runtime import harness, memo_client, candidate_run
from tradingagents.strategies.data_sources import regulation_graphics as graphics
from tradingagents.strategies.learning import regulation_graphics_runtime as runtime

FIXTURES=Path(__file__).parent/'fixtures'
V3='regulation-graphics-request-v3'
V4='regulation-graphics-request-v4'


def actual_failure():
    return json.loads((FIXTURES/'regulation_graphics_v3_native_readable_clipping_failure.json').read_text())['attempt']


def native_assets():
    meta=actual_failure()['graphics_failure']['request_identity']['assets'][0]
    return [{k:meta[k] for k in ('id','filename','sha256','context')}|{
        'data':(FIXTURES/'regulation_graphics'/meta['filename']).read_bytes()}]


def test_v4_material_legibility_is_explicit_in_both_image_backed_stages():
    request=graphics.prepare_graphics_recognition(assets(),request_version=V4)
    assert graphics.LEGIBLE_REQUEST_VERSION==V4
    assert json.loads(request.context_json)['version']==V4
    assert 'unambiguously legible whole label or number' in request.system
    assert 'cropped stroke pixels alone' in request.system
    assert 'any ambiguous character, word, number, unit, or association' in request.system
    assert 'Never guess or reconstruct missing characters' in request.system
    assert 'actually missing visible content' in request.system
    assert 'exact within-cell line breaks' in request.system
    assert request.document_inputs[0].data==assets()[0]['data']
    recognized=graphics.validate_graphics_recognition(json.dumps(recognition_response(request)),request,provenance(request))
    review=graphics.prepare_graphics_review(assets(),recognized)
    assert json.loads(review.context_json)['version']==V4
    assert graphics._MATERIAL_LEGIBILITY_V4 in review.system
    checked=graphics.validate_graphics_review(json.dumps(review_response(review,recognized)),review,recognized,provenance(review))
    assert graphics.replay_graphics_receipt(assets(),{'recognition':recognized,'review':checked})


def test_actual_v3_readable_title_clipping_failure_replays_exact_request_and_stays_failed(monkeypatch):
    attempt=actual_failure();before=deepcopy(attempt);failure=attempt['graphics_failure']
    request=graphics.prepare_graphics_recognition(native_assets(),request_version=V3)
    assert runtime._logical(request)==failure['model_request']
    assert hashlib.sha256(request.system.encode()).hexdigest()==failure['model_request']['system_sha256']
    assert 'cropped stroke pixels alone' not in request.system
    raw=json.loads(failure['response_text']);row=raw['graphics'][0]
    assert row['status']=='partial' and row['unresolved']==[
        'The title lettering is visibly clipped at the upper image edge; any clipped glyph detail cannot be recovered from the supplied image.']
    assert row['transcript'].startswith('Table 3: Counts and Net Assets of Registered Closed-end Funds')
    with pytest.raises(ValueError,match='invalid_regulation_graphics_incomplete'):
        graphics.validate_graphics_recognition(failure['response_text'],request,failure['model_provenance'])
    from tradingagents.strategies.data_sources import regulation_graphics_inputs
    monkeypatch.setattr(regulation_graphics_inputs,'graphics_assets',lambda evidence:native_assets())
    assert runtime.replay_graphics_attempt({},attempt) is None
    assert attempt==before and failure['validation_status']=='failed'


def test_constructed_v3_success_replays_exact_historical_bytes_after_default_advances():
    bundle=json.loads((FIXTURES/'regulation_graphics_v3_constructed_bundle.json').read_text())['bundle']
    before=deepcopy(bundle)
    request=graphics.prepare_graphics_recognition(assets(),request_version=V3)
    review=graphics.prepare_graphics_review(assets(),bundle['recognition'])
    assert json.loads(review.context_json)['version']==V3
    assert runtime._logical(request)==bundle['recognition']['model_request']
    assert runtime._logical(review)==bundle['review']['model_request']
    assert graphics.replay_graphics_receipt(assets(),bundle)
    assert bundle==before
    with pytest.raises(ValueError,match='invalid_regulation_graphics_request_version'):
        graphics.prepare_graphics_review(assets(),bundle['recognition'],request_version=V4)


@pytest.mark.parametrize('issue', ['Ambiguous character in a clipped title word.',
    'The final digit of a value is unreadable.', 'A unit or footnote is actually missing.',
    'The row/column association cannot be resolved without guessing.',
    'A chart curve or visible table content is actually missing.'])
def test_v4_keeps_any_reported_material_ambiguity_or_omission_failed(issue):
    request=graphics.prepare_graphics_recognition(assets());raw=recognition_response(request)
    raw['graphics'][0].update(status='partial',unresolved=[issue])
    with pytest.raises(ValueError,match='invalid_regulation_graphics_incomplete'):
        graphics.validate_graphics_recognition(json.dumps(raw),request,provenance(request))


def test_new_material_scope_never_converts_retained_partial_output_to_success():
    request=graphics.prepare_graphics_recognition(native_assets())
    raw=json.loads(actual_failure()['graphics_failure']['response_text'])
    raw['request_identity_sha256']=json.loads(request.context_json)['request_identity_sha256']
    with pytest.raises(ValueError,match='invalid_regulation_graphics_incomplete'):
        graphics.validate_graphics_recognition(json.dumps(raw),request,provenance(request))


@pytest.mark.parametrize('malformation', ['newlines','precision','links'])
def test_v4_preserves_literal_numeric_and_cell_association_validators(malformation):
    request=graphics.prepare_graphics_recognition(assets());raw=recognition_response(request)
    row=raw['graphics'][0];cell=next(e for e in row['elements'] if e['kind']=='cell')
    if malformation=='newlines':cell['text']=cell['text'][:1]+'\n'+cell['text'][1:]
    elif malformation=='precision':cell['precision']='approximate'
    else:cell['references']=[]
    with pytest.raises(ValueError,match='invalid_regulation_graphics_'):
        graphics.validate_graphics_recognition(json.dumps(raw),request,provenance(request))


def test_unknown_future_request_version_cannot_enter_a_receipt():
    with pytest.raises(ValueError,match='invalid_regulation_graphics_request_version'):
        graphics.prepare_graphics_recognition(assets(),request_version='regulation-graphics-request-v6')


def test_v4_image_backed_review_still_fails_for_reported_ambiguous_character():
    request=graphics.prepare_graphics_recognition(assets())
    recognized=graphics.validate_graphics_recognition(json.dumps(recognition_response(request)),request,provenance(request))
    review=graphics.prepare_graphics_review(assets(),recognized);raw=review_response(review,recognized)
    raw['graphics'][0].update(material_complete=False,errors=[{
        'element_id':None,'issue':'A title character is ambiguous and would require guessing.'}])
    with pytest.raises(ValueError,match='invalid_regulation_graphics_review_incomplete'):
        graphics.validate_graphics_review(json.dumps(raw),review,recognized,provenance(review))


def test_material_definition_change_cannot_reuse_previously_reviewed_batches(harness,monkeypatch):
    from tradingagents.strategies.candidate_response_reuse import candidate_response_memo
    memo_client(harness,monkeypatch)
    with candidate_response_memo():
        candidate_run(harness);first=len(harness.calls)
        monkeypatch.setattr(graphics,'_MATERIAL_LEGIBILITY_V4',graphics._MATERIAL_LEGIBILITY_V4+'\nConstructed changed policy.\n')
        candidate_run(harness)
        assert len(harness.calls)==first*2
