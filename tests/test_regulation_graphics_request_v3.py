"""V3 representation instructions preserve strict literals and V1/V2 replay."""
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
V3='regulation-graphics-request-v3'


def actual_failure():
    return json.loads((FIXTURES/'regulation_graphics_v2_native_literal_failure.json').read_text())['attempt']


def native_assets():
    meta=actual_failure()['graphics_failure']['request_identity']['assets'][0]
    return [{k:meta[k] for k in ('id','filename','sha256','context')}|{
        'data':(FIXTURES/'regulation_graphics'/meta['filename']).read_bytes()}]


def test_v3_requests_plain_literal_blocks_and_exact_substring_consistency():
    request=graphics.prepare_graphics_recognition(assets(),request_version=V3)
    assert graphics.LITERAL_REQUEST_VERSION==V3 and graphics.SCOPED_REQUEST_VERSION==V2
    assert {V1,V2,V3} <= graphics.REQUEST_VERSIONS
    assert json.loads(request.context_json)['version']==V3
    assert 'plain literal cell/label blocks in reading order' in request.system
    assert 'exact within-cell line breaks' in request.system
    assert 'Do not use Markdown or HTML tables' in request.system
    assert 'including whitespace, punctuation and newlines' in request.system
    assert 'Do not invent nonprinted annotations as printed text' in request.system
    assert 'Check these exact substring relationships before emitting JSON' in request.system
    assert 'complete supplied original asset' in request.system
    assert request.document_inputs[0].data==assets()[0]['data']
    recognized=graphics.validate_graphics_recognition(json.dumps(recognition_response(request)),request,provenance(request))
    review=graphics.prepare_graphics_review(assets(),recognized)
    assert json.loads(review.context_json)['version']==V3
    assert 'plain literal cell/label blocks in reading order' in review.system
    checked=graphics.validate_graphics_review(json.dumps(review_response(review,recognized)),review,recognized,provenance(review))
    assert graphics.replay_graphics_receipt(assets(),{'recognition':recognized,'review':checked})


def test_actual_v2_literal_failure_and_exact_old_request_replay_unchanged(monkeypatch):
    attempt=actual_failure();before=deepcopy(attempt);failure=attempt['graphics_failure']
    request=graphics.prepare_graphics_recognition(native_assets(),request_version=V2)
    assert runtime._logical(request)==failure['model_request']
    assert hashlib.sha256(request.system.encode()).hexdigest()==failure['model_request']['system_sha256']
    assert 'plain literal cell/label blocks in reading order' not in request.system
    raw=json.loads(failure['response_text']);row=raw['graphics'][0]
    assert row['status']=='complete' and row['unresolved']==[]
    assert next(e['text'] for e in row['elements'] if e['id']=='col_all')=='All Registered\nClosed-end\nFunds'
    assert 'All Registered Closed-end Funds' in row['transcript']
    with pytest.raises(ValueError,match='invalid_regulation_graphics_literal'):
        graphics.validate_graphics_recognition(failure['response_text'],request,failure['model_provenance'])
    from tradingagents.strategies.data_sources import regulation_graphics_inputs
    monkeypatch.setattr(regulation_graphics_inputs,'graphics_assets',lambda evidence:native_assets())
    assert runtime.replay_graphics_attempt({},attempt) is None
    assert attempt==before and failure['validation_status']=='failed'


def test_v2_complete_recognition_review_and_recorded_version_remain_byte_exact():
    bundle=json.loads((FIXTURES/'regulation_graphics_v2_constructed_bundle.json').read_text())['bundle']
    original=deepcopy(bundle)
    request=graphics.prepare_graphics_recognition(assets(),request_version=V2)
    review=graphics.prepare_graphics_review(assets(),bundle['recognition'])
    assert json.loads(review.context_json)['version']==V2
    assert runtime._logical(request)==bundle['recognition']['model_request']
    assert runtime._logical(review)==bundle['review']['model_request']
    assert graphics.replay_graphics_receipt(assets(),bundle)
    assert bundle==original
    with pytest.raises(ValueError,match='invalid_regulation_graphics_request_version'):
        graphics.prepare_graphics_review(assets(),bundle['recognition'],request_version=V3)


@pytest.mark.parametrize('version',[V1,V2,V3])
@pytest.mark.parametrize('source',['image','context'])
def test_exact_newline_literal_validator_is_strict_for_every_request_version(version,source):
    values=assets();values[0]['context']='Year\nLabel'
    request=graphics.prepare_graphics_recognition(values,request_version=version)
    raw=recognition_response(request)
    row=raw['graphics'][0]
    column=next(e for e in row['elements'] if e['id']=='year')
    if source=='image':
        row['transcript']=row['transcript'].replace('Year','Year Label')
        column['text']='Year\nLabel'
    else:
        column.update(text='Year Label',source='context')
    with pytest.raises(ValueError,match='invalid_regulation_graphics_literal'):
        graphics.validate_graphics_recognition(json.dumps(raw),request,provenance(request))


def test_v3_prompt_does_not_make_native_v2_inconsistent_representation_acceptable():
    request=graphics.prepare_graphics_recognition(native_assets(),request_version=V3)
    # A constructed V3 response with the same inconsistency is also refused;
    # the original retained V2 response and failure are never rewritten.
    raw=json.loads(actual_failure()['graphics_failure']['response_text'])
    raw['request_identity_sha256']=json.loads(request.context_json)['request_identity_sha256']
    with pytest.raises(ValueError,match='invalid_regulation_graphics_literal'):
        graphics.validate_graphics_recognition(json.dumps(raw),request,provenance(request))


@pytest.mark.parametrize('issue',[
    'Visible clipped glyph in the final cell.',
    'A row or curve ends visibly clipped at the original image edge.',
    'A required unit or footnote is explicitly missing from the supplied context.',
])
def test_v3_keeps_reported_material_clipping_and_uncertainty_failed(issue):
    request=graphics.prepare_graphics_recognition(assets(),request_version=V3)
    raw=recognition_response(request);raw['graphics'][0].update(status='partial',unresolved=[issue])
    with pytest.raises(ValueError,match='invalid_regulation_graphics_incomplete'):
        graphics.validate_graphics_recognition(json.dumps(raw),request,provenance(request))
